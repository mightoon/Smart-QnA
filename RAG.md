# RAG 检索逻辑剖析 (RAG.md)

本文档剖析 Smart QnA 系统中 Elasticsearch 检索、Neo4j 知识图谱检索，以及三路并行检索（`all` 模式）的详细实现逻辑。

涉及源码：
- `app/retrieval/es_client.py` —— ES 检索客户端
- `app/retrieval/kg_client.py` —— KG 图谱检索客户端
- `app/retrieval/vec_client.py` —— Milvus 向量检索客户端
- `app/retrieval/rerank_client.py` —— Rerank 融合客户端（rerank API + RRF 兜底）
- `app/retrieval/orchestrator.py` —— RAG 调度器（检索策略编排 + 上下文组装）
- `app/core/llm_client.py` —— 实体抽取（KG 检索前置步骤）
- `app/core/embedding_client.py` —— Embedding 向量化（vec 检索前置步骤）
- `app/core/metrics.py` —— 指标采集（埋点 + 聚合）
- `app/schemas/responses.py` —— `SourceItem` 统一来源模型

---

## 1. 统一来源模型 SourceItem

三路检索的产出全部归一化为 `SourceItem`，便于后续上下文组装与前端展示：

```python
class SourceItem(BaseModel):
    source: str          # "article" / "qna" / "kg"
    title: Optional[str] # 标题或实体名
    content: str         # 命中内容片段
    score: Optional[float] # 相关度分数（ES 有，KG 无）
    meta: Optional[dict] # 附加元数据（ES: _id/index；KG: labels/relations）
```

---

## 2. Elasticsearch 检索（article / qna）

### 2.1 设计目标
针对两个独立索引提供关键字检索：
- `article` 索引：文章正文（`content` 字段）+ 标题（`title` 字段）
- `qna` 索引：问答对，答案（`answer` 字段）+ 问题（`question` 字段）

### 2.2 字段映射（可配置）
不同索引的字段名不同，通过字段映射统一抽象。映射从 ES 配置项读取，未配置则用默认值：

```python
# 默认值（es_client.py）
_DEFAULT_INDEX_FIELDS = {
    "article": {"body": "content",  "title": "title"},
    "qna":     {"body": "answer",   "title": "question"},
}

# 配置覆盖（data/config.json elasticsearch.items.xxx）
"article_fields": {"body": "content", "title": "title"},
"qna_fields":     {"body": "answer",  "title": "question"},
```

`body` 是实际参与检索的文本字段，`title` 用于展示与辅助匹配。字段名可配置，适配不同 ES 索引 schema。

### 2.3 同义词扩展（查询前置）

ES 检索前，服务代码对用户 query 做双向同义词扩展，解决 BM25 字面匹配无法覆盖同义词/近义词的问题。

**词典加载**：启动时加载 `data/synonyms.json`，展开为双向映射——每个词映射到其所在同义词组的全部词。

```json
// synonyms.json
"微调": ["fine-tuning", "fine tuning", "指令微调", "SFT"]

// 展开后的双向映射
"微调"        -> ["微调", "fine-tuning", "fine tuning", "指令微调", "SFT"]
"fine-tuning" -> ["微调", "fine-tuning", "fine tuning", "指令微调", "SFT"]
"SFT"         -> ["微调", "fine-tuning", "fine tuning", "指令微调", "SFT"]
```

**扩展逻辑**（`_expand_query`）：遍历词典，若 query 中包含某词，则将该词全部同义词以 `OR` 组合加入查询。原始 query 始终保留。

```
用户 query "SFT最佳实践"
  → 词典命中 "SFT"
  → 扩展为：SFT最佳实践 OR SFT OR 微调 OR fine-tuning OR fine tuning OR 指令微调
```

此扩展在服务代码侧完成（`es_client.py`），不依赖 ES 内置 synonym filter，与 ES 索引配置解耦。

### 2.4 检索查询体
使用 `multi_match` + `best_fields` 策略，正文字段加权 3 倍。查询前先经同义词扩展：

```json
{
  "size": "<top_k>",
  "query": {
    "multi_match": {
      "query": "<扩展后的查询（原始query OR 同义词...）>",
      "fields": ["<body字段>^3", "<title字段>"],
      "type": "best_fields"
    }
  }
}
```

**`best_fields` 语义**：对每个文档，取匹配得分最高的字段作为该文档得分（而非所有字段得分之和）。正文 `^3` 加权意味着正文命中比标题命中贡献更高分值，优先返回正文相关的文档。

> 说明：当前为 BM25 关键字检索（含同义词扩展），未使用向量检索（dense vector / kNN）。语义检索由 `vec` 模式（Milvus 向量库）覆盖，`merge` 模式融合两者。

### 2.5 结果解析
遍历 `hits.hits`，每条命中构造 `SourceItem`（字段名从配置的 fields 映射读取）：
- `title` ← `_source[fields.title]`（article 默认 `title`，qna 默认 `question`）
- `content` ← `_source[fields.body]`（article 默认 `content`，qna 默认 `answer`）
- `score` ← `_score`（ES 相关度）
- `meta` ← `{_id, index}`

### 2.6 两个入口方法
```python
async def search_article(self, query, top_k=5)  # 检索 article_index
async def search_qna(self, query, top_k=5)      # 检索 qna_index
```
二者均委托给私有 `_search(index, source_type, query, top_k)`，区别仅在索引名与字段映射。

### 2.7 连接与异常
- 客户端懒加载：首次访问 `client` 属性时创建 `AsyncElasticsearch`，支持 basic_auth 与 verify_certs。
- 检索失败抛 `ESConnectionError`（502），在 `all` 模式下被 `gather(return_exceptions=True)` 捕获降级。

---

## 3. Neo4j 知识图谱检索（kg）

### 3.1 设计目标
基于从用户问题中抽取的实体，在知识图谱中检索相关节点及其邻居关系，为 LLM 提供结构化的图谱知识。

### 3.2 前置：实体抽取
KG 检索不能直接用原始 query 匹配（图谱节点是规范实体名），需先调用 LLM 抽取核心实体：

```
用户问题 "DeepSeek 和 GPT-4 的区别是什么？"
  → LLM 实体抽取 → ["DeepSeek", "GPT-4"]
  → 图谱检索这些实体节点
```

抽取逻辑（`LLMClient.extract_entities`）：
- 使用专属 `ENTITY_EXTRACTION_PROMPT`，要求模型返回严格 JSON `{"entities": [...]}`
- `temperature=0.0` 保证稳定输出
- 解析容错：先 `json.loads`，失败则正则抽取首个 `{...}` 再解析；仍失败返回空列表

### 3.3 Cypher 查询模板（动态构建）

Cypher 模板由 `_build_query(node_key, max_neighbors)` 动态构建，支持可配置的节点主键属性、邻居数量限制、关系类型过滤：

```cypher
MATCH (n)
WHERE toLower(n.{node_key}) CONTAINS toLower($entity)
OPTIONAL MATCH (n)-[r]-(m)
WHERE NOT type(r) IN $excluded
  AND coalesce(m.name, '') <> ''
WITH n, collect(distinct [type(r), coalesce(m.name, '')]) AS all_rels
WITH n, all_rels[..{max_neighbors}] AS rels
RETURN n.{node_key} AS name, labels(n) AS labels, rels
LIMIT $limit
```

**逐句解析**：
1. `MATCH (n)` —— 遍历图中所有节点
2. `WHERE toLower(n.{node_key}) CONTAINS toLower($entity)` —— 节点的 `node_key` 属性（默认 `name`，可配置）大小写不敏感地**包含**实体文本
3. `OPTIONAL MATCH (n)-[r]-(m)` —— 可选地匹配该节点的所有邻居关系（无方向限制）
4. `WHERE NOT type(r) IN $excluded AND coalesce(m.name, '') <> ''` —— **过滤排除的关系类型**（如 contains/references）+ **去除空目标名的关系**
5. `collect(distinct [type(r), coalesce(m.name, '')])` —— 聚合去重，收集每条关系的 `[关系类型, 目标节点名]`
6. `all_rels[..{max_neighbors}]` —— **限制邻居数量**，取前 `max_neighbors` 条（默认 10，可配置）
7. `RETURN n.{node_key} AS name, labels(n) AS labels, rels` —— 返回节点名、标签列表、关系集合
8. `LIMIT $limit` —— 限制每个实体返回的节点数

**可配置项**（`data/config.json` neo4j 段）：
| 配置项 | 默认值 | 说明 |
|---|---|---|
| `database` | `neo4j` | 数据库名，`session(database=...)` 使用 |
| `node_key` | `name` | 节点主键属性名，用于实体匹配 |
| `max_neighbors` | `10` | 每个节点最多保留的邻居关系数 |
| `excluded_relations` | `["contains", "references"]` | 排除的关系类型列表 |

### 3.4 配额分配
当有多个实体时，按实体数均分 top_k 配额：

```python
per_entity = max(1, top_k // max(1, len(entities)))
```

例如 `top_k=5`、3 个实体 → 每个实体最多返回 1 条。每个实体独立执行一次 Cypher，结果累计，达到 `top_k` 提前返回。

### 3.5 结果格式化
`_format_record` 将图谱记录转为可读文本：

```
[Person/Company] DeepSeek
  -(develops)-> DeepSeek-V3
  -(competes_with)-> GPT-4
```

第一行是 `[标签] 节点名`，后续每行一条关系 `-(关系类型)-> 目标`。该文本作为 `SourceItem.content` 传给 LLM。

### 3.6 推理策略
图谱仅做 1 跳邻居检索（提供事实性节点+关系），**推理交给 LLM**。LLM 根据返回的图谱知识与用户问题自行推理（如"DeepSeek 和 GPT-4 的区别"→ LLM 从两者的 `type`/`competes_with` 关系中推理出区别）。这是轻量方案，不涉及多跳路径查询或图算法。

### 3.7 降级与异常
- 实体抽取失败（`_safe_entities`）→ 返回空列表，KG 检索返回空，**不阻断主流程**
- 无实体 → `search_by_entities` 直接返回空列表
- 图谱检索失败抛 `KGConnectionError`（502），在 `all` 模式下被降级

---

## 4. 向量检索（vec 模式）

### 4.1 设计目标
将用户问题经 embedding 模型向量化后，查询 Milvus 向量数据库，召回语义相似的文段。补充 ES 关键字检索无法覆盖的语义近似场景。

### 4.2 前置：Embedding 向量化
`EmbeddingClient.embed(text)` 调用 OpenAI 兼容的 `/embeddings` 接口，将文本转为浮点向量。配置独立于 LLM（可使用不同模型/服务）。

### 4.3 多路检索策略（主题拆分）

**问题背景**：用户问题若涉及多个主题（如 "A+B+C"），整体向量化后得到的 `vec(A+B+C)` 在向量空间中是各子主题的"中间地带"，与 `vec(A)`、`vec(B)`、`vec(C)` 都有一定距离，可能导致各子主题的最相关文档不被召回。

**解决策略**：`VecClient.search(query, top_k)` 默认采用多路检索：
1. 调用 LLM `extract_entities(query)` 抽取主题/关键概念（复用实体抽取能力）
2. 构建检索 queries：原始 query 始终保留 + 各主题，去重
3. 每路分配配额 `per_query = max(1, top_k // len(queries))`
4. 各路并行向量化 + Milvus ANN 检索（`asyncio.gather`）
5. 结果按 content 前 200 字符去重，合并取 top_k

```
用户 query "知识图谱与向量检索的优缺点"
  → LLM 抽取主题 → ["知识图谱", "向量检索"]
  → 检索 queries：["知识图谱与向量检索的优缺点", "知识图谱", "向量检索"]
  → 各路 embed + Milvus ANN 检索
  → 合并去重 → top_k 条
```

**降级**：LLM 不可用或主题拆分失败时，自动降级为单路检索（`_single_search`，整体向量化）。

### 4.4 单路检索 `_single_search`
将单条 query 向量化后检索 Milvus：
1. `embedding_client.embed(query)` 获取查询向量
2. `MilvusClient.search()` ANN 检索
3. 解析结果，每条命中构造 `SourceItem(source="vec", content=text_field, score=distance)`

### 4.5 Milvus 配置
| 字段 | 说明 |
|---|---|
| uri | Milvus 地址（如 `http://localhost:19530`） |
| collection_name | 检索的 collection |
| vector_field | 向量字段名 |
| text_field | 文本字段名（用于返回内容） |
| metric_type | 距离度量（COSINE / L2 / IP） |

### 4.6 独立性
vec 检索与 ES 检索、KG 检索完全独立，可任意并行。vec 内部的多路检索也是并行的。

---

## 5. 多路并行检索（all / merge 模式）

### 5.1 调度编排
`all` 与 `merge` 模式利用 `asyncio` 并发执行多路检索，最小化总延迟：
- `all`：三路并行（article + qna + kg），**不含 vec**，结果直接拼接
- `merge`：四路并行（article + qna + kg + vec），结果经 rerank 融合取 top_n

### 5.2 依赖关系分析
ES 的 article/qna、vec 检索与 KG 的实体抽取**互相独立**，可立即并行启动；但 KG 的图谱检索**依赖实体抽取结果**，必须等实体抽取完成后才能发起。因此采用两阶段并发：

```
阶段1（并行）：
  ├─ 实体抽取（LLM）
  ├─ ES article 检索
  ├─ ES qna 检索
  └─ Milvus vec 检索（仅 merge）

阶段2（实体抽取完成后）：
  └─ KG 图谱检索
      ↓
阶段3：多路结果汇总
```

### 5.3 实现代码
`all` 模式调用 `_three_way_retrieve`（不含 vec），`merge` 调用 `_four_way_retrieve`（含 vec）：

```python
# merge 模式四路检索
async def _four_way_retrieve(self, query, top_k):
    entity_task = asyncio.create_task(self._safe_entities(query))
    article_task = asyncio.create_task(self.es.search_article(query, top_k))
    qna_task = asyncio.create_task(self.es.search_qna(query, top_k))
    vec_task = asyncio.create_task(self.vec.search(query, top_k))  # vec 与其他并行
    entities = await entity_task
    kg_task = asyncio.create_task(self.kg.search_by_entities(entities, top_k))
    results = await asyncio.gather(
        article_task, qna_task, kg_task, vec_task, return_exceptions=True
    )
    sources = []
    for res in results:
        sources.extend(_unwrap(res))
    return sources, entities
```

调度入口：
```python
if mode == ChatMode.ALL:
    sources, entities = await self._three_way_retrieve(query, top_k)     # 三路（无vec）
elif mode == ChatMode.MERGE:
    sources, entities = await self._four_way_retrieve(query, top_k)      # 四路（含vec）
    sources = await self.rerank.rerank(query, sources, top_k)            # 融合
```

**关键点**：
- `asyncio.create_task` 立即调度协程，不等 await 即开始执行
- `await entity_task` 仅阻塞等待实体抽取，期间 ES/vec 检索并行进行
- `asyncio.gather(..., return_exceptions=True)` 收集多路结果，任一路异常返回 Exception 对象而非抛出，保证单路失败不影响整体

### 5.4 异常降级 `_unwrap`
```python
def _unwrap(result) -> list[SourceItem]:
    if isinstance(result, Exception):
        return []      # 该路失败，贡献 0 条来源
    if isinstance(result, list):
        return result
    return []
```

效果：例如 ES 不可用时，article/qna 返回空，但 KG/vec 仍可贡献来源，整体不中断。

### 5.5 单路模式对比
- `article`：仅 `es.search_article`，同步等待
- `qna`：仅 `es.search_qna`，同步等待
- `kg`：先 `_safe_entities` 抽实体，再 `kg.search_by_entities`，串行
- `vec`：`vec.search`（内部先 embedding 再 Milvus 检索），串行
- `all`：三路并发（不含 vec），结果直接拼接
- `merge`：四路并发（含 vec）+ rerank 融合（见第 6 节）

---

## 6. 融合检索（merge 模式）

### 6.1 设计目标
`all` 模式将多路结果简单拼接（可能冗余、低质条目混杂）。`merge` 模式在四路检索后引入**重排融合**：用 rerank 模型对全部候选统一打分，仅保留高分项作为 LLM 上下文，提升答案精度。

### 6.2 流程
```
四路检索结果（最多 top_k × 4 条）
        │
        ▼
  ┌──────────────────────────┐
  │  RerankClient.rerank()   │
  │  ┌─ rerank 模型已配置？  │
  │  │  是 → 调用 rerank API │
  │  │       (Cohere/Jina)   │
  │  │  否/失败 → RRF 兜底   │
  │  └───────────────────────│
  │  输出：top_n 条高分项     │
  └──────────┬───────────────┘
             │
        融合后 sources
             │
        _build_context → LLM
```

### 6.3 rerank API 调用（Cohere / Jina 兼容）
当 rerank 完整配置（`api_base` + `api_key` + `model`）时，调用 rerank 服务：

**请求**
```json
POST {api_base}/rerank
Authorization: Bearer {api_key}
{
  "model": "...",
  "query": "用户原始问题",
  "documents": ["doc1内容", "doc2内容", ...],
  "top_n": 5
}
```

**响应**
```json
{
  "results": [
    {"index": 2, "relevance_score": 0.98},
    {"index": 0, "relevance_score": 0.85}
  ]
}
```

按 `relevance_score` 排序，映射回 `SourceItem` 并更新其 `score` 字段。

### 6.4 RRF 兜底（无模型融合）
当 rerank 未配置或调用失败时，降级为 **Reciprocal Rank Fusion**，无需任何模型：

```python
score(d) = 1 / (60 + rank_in_its_retriever)
```

- 按检索路（article/qna/kg）分组
- ES 路组内按 `score` 降序排，KG 路保持原序
- 每条来源按其在所属路内的排名计算 RRF 分数
- 跨路统一按 RRF 分数降序，取 top_n

**效果**：三路结果均匀交错（每路 rank=0 的项优先），避免单路霸占上下文。常数 60 是业界经验值，平衡头部与长尾。

### 6.5 top_n 与 top_k 的关系
| 参数 | 来源 | 作用 |
|---|---|---|
| `top_k` | 请求参数 | 每路检索召回量（如 5 → 三路最多 15 条候选） |
| `top_n` | rerank 配置 | 融合后保留量（如 5 → 最终仅 5 条进 LLM 上下文） |

`top_n` 未配置（0）时回退到请求的 `top_k`。

### 6.6 配置管理
rerank 作为独立配置段（`rerank`），与 llm/es/neo4j 同样支持多份配置（items + active）、增删改查、切换、验证。字段：

| 字段 | 说明 |
|---|---|
| api_base | rerank 服务地址（如 `https://api.cohere.ai/v1`） |
| api_key（敏感） | 鉴权密钥 |
| model | rerank 模型名 |
| top_n | 融合后保留条数 |

验证逻辑：发起一次最小 rerank 请求（query="ping", documents=["test"]），成功即可用。

---

## 7. 上下文组装与 LLM 生成

### 7.1 上下文组装 `_build_context`
将 `SourceItem` 列表编号拼接为 LLM 可读的参考资料块：

```
[1] (来源:article | 标题:DeepSeek 技术报告)
DeepSeek-V3 采用 MoE 架构...

[2] (来源:kg | 标题:DeepSeek)
[Company] DeepSeek
  -(develops)-> DeepSeek-V3

[3] (来源:qna | 标题:DeepSeek 和 GPT-4 区别)
DeepSeek 是开源模型，GPT-4 是闭源...
```

每块格式：`[序号] (来源:类型 | 标题:xxx)\n内容`，块间空行分隔。序号对应 LLM 提示词中要求的 `[n]` 引用标注。

### 7.2 提示词选择
- 有 context（检索模式，含 merge）→ `RAG_SYSTEM_PROMPT`：依据资料作答，资料不足明确说明，用 `[n]` 标注引用
- 无 context（纯对话）→ `CHAT_SYSTEM_PROMPT`：通用对话，自由作答

### 7.3 输出流程
- 非流式 `run()`：`llm.chat()` 收集完整回答，返回 `ChatResult`
- 流式 `run_stream()`：先发 `meta` 事件（融合后来源+实体），再逐 token 发 `token` 事件，最后 `done`

---

## 8. 数据流全景

```
用户请求 {query, mode, top_k}
        │
        ▼
  ┌─────────────┐
  │ Orchestrator│
  └──────┬──────┘
         │ mode 判断
   ┌─────┼──────┬──────────────┐
   │     │      │              │
[无]  [单路]  [all/merge]    ...
   │     │      │
   │     │   ┌──▼──────────────────┐
   │     │   │ _three_way_retrieve  │
   │     │   │  ├─ 实体抽取(LLM)     │
   │     │   │  ├─ ES article(BM25) │ ← 并行
   │     │   │  ├─ ES qna(BM25)     │
   │     │   │  └─ KG 图谱(Cypher)  │ ← 实体就绪后
   │     │   └──┬───────────────────┘
   │     │      │ SourceItem[] 汇总
   │     │      │
   │     │   ┌──▼──────┐
   │     │   │ all: 直接│
   │     │   │ merge:  │ rerank 融合 → top_n
   │     │   │   rerank │ (API / RRF兜底)
   │     │   └──┬──────┘
   │     │      │
   │     └──────┴──┐
   │               ▼
   │        ┌──────────────┐
   │        │_build_context │ 编号块 [n]
   │        └──────┬───────┘
   │               │ context
   ▼               ▼
┌──────────┐  ┌──────────┐
│CHAT提示词│  │RAG提示词  │
└────┬─────┘  └────┬─────┘
     │             │
     └──────┬──────┘
            ▼
       LLM 生成
            │
    流式 token / 完整回答
            ▼
    SSE 事件流 (meta→token→done) / JSON
```

---

## 9. 设计要点总结

| 维度 | 设计决策 | 理由 |
|---|---|---|
| ES 检索 | BM25 `multi_match` + 正文加权 | 关键字检索简单可靠；正文权重高于标题 |
| KG 检索 | LLM 抽实体 + Cypher 包含匹配 | 图谱需规范实体；包含匹配提升召回率 |
| vec 检索 | Embedding 向量化 + Milvus ANN | 语义相似召回，补充关键字检索盲区 |
| 并发 | 两阶段 `asyncio` 并发 | 实体抽取是 KG 前置依赖，ES/vec 与之并行最大化并发 |
| 融合(merge) | rerank 模型打分 + RRF 兜底 | rerank 精度高；RRF 保证无模型时仍可用 |
| all 不含 vec | 三路不含 vec | vec 需额外 embedding 开销，all 保持轻量 |
| top_n/top_k | 分离配置 | 召回量与精排量解耦，灵活控制上下文质量 |
| 容错 | `gather(return_exceptions=True)` + `_unwrap` | 单路失败不阻断整体，提升鲁棒性 |
| 统一模型 | `SourceItem` 归一化 | 多路异构结果统一，便于组装与展示 |
| 上下文 | 编号块 + `[n]` 引用 | 让 LLM 答案可溯源 |
| 降级 | 实体抽取/rerank 失败均降级 | 保证主流程不因辅助步骤崩溃 |
| 指标埋点 | `measure` 上下文管理器 | 自动采集各操作耗时/token/成功状态，可视化监控 |
