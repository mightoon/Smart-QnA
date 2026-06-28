# 客户端演示脚本说明 (client.md)

本文档详细说明 `client_demo.py` 客户端演示脚本的设计意图、每个 case 测试的场景、调用参数、期望输出与实际输出示例。

## 1. 脚本概述

`client_demo.py` 是一个基于 `httpx` 的命令行客户端，用于端到端验证 Smart QnA 服务的全部功能。脚本依次调用 `/api/chat` 的 8 种参数模式，对每种模式打印请求参数、HTTP 响应、检索来源、模型回答、耗时等完整链路信息。

### 1.1 用法

```bash
# 默认连接 localhost:8000，执行全部 8 个 case
python client_demo.py

# 指定服务地址
python client_demo.py --base-url http://your-host:8000

# 跳过指定 case（按名称关键词匹配，不区分大小写）
python client_demo.py --skip kg vec merge
```

### 1.2 执行流程

```
1. 健康检查 GET /api/health
   ├─ 成功 → 继续执行 case
   └─ 失败 → 打印"服务不可用"并退出

2. 逐个执行 CASES 列表（8 个 case）
   ├─ stream=False → call_blocking（非流式）
   └─ stream=True  → call_streaming（流式 SSE）

3. 汇总打印成功/失败/跳过数
```

### 1.3 打印格式

每个 case 以 `=` 分隔的 banner 开头，内部每步带时间戳 `[HH:MM:SS.mmm]`：

```
======================================================================
  CASE: article 文章检索（非流式）
======================================================================
  [08:00:15.123] 请求参数: {"query": "...", "mode": "article", ...}
  [08:00:15.124] POST http://localhost:8000/api/chat
  [08:00:15.890] 收到响应: HTTP 200  耗时 766ms
  [08:00:15.891] mode=article  entities=[]
  [08:00:15.891] 检索来源:
    [1] 来源=article  标题=...  score=1.2345
        内容预览...
  [08:00:15.892] 模型回答:
    回答内容...
  [08:00:15.893] 完成  端到端耗时 766ms
```

---

## 2. Case 详解

### Case 0: 健康检查

| 项目 | 说明 |
|---|---|
| **测试场景** | 验证服务是否正常启动、可连接 |
| **调用方式** | `GET /api/health` |
| **请求参数** | 无（query parameter 无） |
| **期望输出** | HTTP 200，`{"status":"ok","version":"0.1.0","services":{}}` |
| **实际输出示例** | `✓ 服务正常  status=ok  version=0.1.0` |
| **失败行为** | 打印 `✗ 连接失败` 或 `✗ 服务异常`，脚本退出（exit 1） |

---

### Case 1: 纯对话（无 mode）

| 项目 | 说明 |
|---|---|
| **测试场景** | 不进行任何检索，纯 LLM 对话。验证 LLM 连接配置是否正确、`CHAT_SYSTEM_PROMPT` 是否生效（模型应自由作答而非要求参考资料） |
| **调用方式** | `POST /api/chat`（非流式） |
| **请求参数** | `{"query": "你好，请用一句话介绍你自己", "mode": null, "stream": false}` |
| **输入说明** | mode 为 null → 不触发检索；stream 为 false → 返回完整 JSON |
| **期望输出** | HTTP 200，`mode: null`，`sources: []`，`entities: []`，`answer` 为模型自由回答 |
| **实际输出示例** | |

```
  [08:00:01.234] 请求参数: {"query": "你好，请用一句话介绍你自己", "mode": null, "stream": false}
  [08:00:01.235] POST http://localhost:8000/api/chat
  [08:00:02.567] 收到响应: HTTP 200  耗时 1.33s
  [08:00:02.568] mode=None  entities=[]
  [08:00:02.568] 检索来源:
    （无检索来源）
  [08:00:02.568] 模型回答:
    你好！我是智能问答助手，可以友好、准确地回答你的各种问题，很高兴为你服务！
  [08:00:02.569] 完成  端到端耗时 1.33s
```

**验证要点**：
- `mode=None` 确认未走检索分支
- `sources=[]` 确认无检索来源
- 回答内容是自由介绍（非"请提供参考资料"），确认使用了 `CHAT_SYSTEM_PROMPT` 而非 `RAG_SYSTEM_PROMPT`

---

### Case 2: article 文章检索

| 项目 | 说明 |
|---|---|
| **测试场景** | 仅检索 ES `article` 索引，BM25 关键字匹配，将命中文章作为参考资料给 LLM 作答 |
| **调用方式** | `POST /api/chat`（非流式） |
| **请求参数** | `{"query": "什么是 RAG 检索增强生成", "mode": "article", "stream": false, "top_k": 3}` |
| **输入说明** | mode=article → 仅检索 ES article 索引；top_k=3 → 最多返回 3 条命中 |
| **期望输出** | HTTP 200，`mode: "article"`，`sources` 含 1~3 条 `source="article"` 的结果（有 score），`entities: []`，`answer` 依据文章内容回答并用 `[n]` 标注引用 |
| **实际输出示例** | |

```
  [08:00:03.123] 请求参数: {"query": "什么是 RAG 检索增强生成", "mode": "article", "stream": false, "top_k": 3}
  [08:00:03.124] POST http://localhost:8000/api/chat
  [08:00:03.890] 收到响应: HTTP 200  耗时 766ms
  [08:00:03.891] mode=article  entities=[]
  [08:00:03.891] 检索来源:
    [1] 来源=article  标题=RAG 技术综述  score=12.3456
        RAG（Retrieval-Augmented Generation）是一种结合检索与生成的技术...
    [2] 来源=article  标题=检索增强生成实践  score=10.1234
        在实际应用中，RAG 通过外部知识库增强大模型的回答能力...
  [08:00:03.892] 模型回答:
    RAG（检索增强生成）是一种结合信息检索与大语言模型生成的技术 [1]。它通过从外部知识库检索相关文档，将检索结果作为上下文提供给模型，从而增强回答的准确性 [2]。
  [08:00:03.893] 完成  端到端耗时 766ms
```

**验证要点**：
- `mode="article"` 确认走了 article 分支
- `sources` 中每条 `source="article"`，有 `score`（ES `_score`）
- 回答中含 `[1]`、`[2]` 引用标注，确认使用了 `RAG_SYSTEM_PROMPT`
- 若 ES 未配置/不可达，sources 为空，回答变为"资料不足"提示（不报错）

---

### Case 3: qna 问答检索

| 项目 | 说明 |
|---|---|
| **测试场景** | 仅检索 ES `qna` 索引（问答对），用命中问答作为参考资料 |
| **调用方式** | `POST /api/chat`（非流式） |
| **请求参数** | `{"query": "如何部署 FastAPI 应用", "mode": "qna", "stream": false, "top_k": 3}` |
| **输入说明** | mode=qna → 仅检索 ES qna 索引；字段映射 body=answer, title=question |
| **期望输出** | HTTP 200，`mode: "qna"`，`sources` 含 `source="qna"` 结果（title 为问题、content 为答案），`entities: []` |
| **实际输出示例** | |

```
  [08:00:04.234] 请求参数: {"query": "如何部署 FastAPI 应用", "mode": "qna", "stream": false, "top_k": 3}
  [08:00:04.890] 收到响应: HTTP 200  耗时 656ms
  [08:00:04.891] mode=qna  entities=[]
  [08:00:04.891] 检索来源:
    [1] 来源=qna  标题=FastAPI 如何部署到生产环境？  score=8.9012
        可以使用 uvicorn 或 gunicorn 配合 uvicorn worker 部署，建议配合 Nginx 反向代理...
  [08:00:04.892] 模型回答:
    FastAPI 应用可通过 uvicorn 部署 [1]，生产环境建议配合 gunicorn 和 Nginx 反向代理 [1]。
  [08:00:04.893] 完成  端到端耗时 656ms
```

**验证要点**：
- `source="qna"`，title 显示的是问题（question 字段），content 是答案（answer 字段）
- 与 article 的区别在于索引与字段映射不同（均可在配置中自定义 `article_fields`/`qna_fields`）

---

### Case 4: kg 知识图谱检索

| 项目 | 说明 |
|---|---|
| **测试场景** | 先调用 LLM 抽取实体，再用实体检索 Neo4j 图谱节点及邻居关系，将图谱知识作为参考资料 |
| **调用方式** | `POST /api/chat`（非流式） |
| **请求参数** | `{"query": "DeepSeek 和 GPT-4 的区别", "mode": "kg", "stream": false, "top_k": 3}` |
| **输入说明** | mode=kg → 先 extract_entities 再 search_by_entities；top_k=3 |
| **期望输出** | HTTP 200，`mode: "kg"`，`entities` 含抽取的实体（如 `["DeepSeek", "GPT-4"]`），`sources` 含 `source="kg"` 结果（title 为节点名，content 为格式化的图谱关系） |
| **实际输出示例** | |

```
  [08:00:05.234] 请求参数: {"query": "DeepSeek 和 GPT-4 的区别", "mode": "kg", "stream": false, "top_k": 3}
  [08:00:06.890] 收到响应: HTTP 200  耗时 1.66s
  [08:00:06.891] mode=kg  entities=['DeepSeek', 'GPT-4']
  [08:00:06.891] 检索来源:
    [1] 来源=kg  标题=DeepSeek
        [Company/Model] DeepSeek
          -(develops)-> DeepSeek-V3
          -(type)-> 开源大模型
    [2] 来源=kg  标题=GPT-4
        [Company/Model] GPT-4
          -(developed_by)-> OpenAI
          -(type)-> 闭源大模型
  [08:00:06.892] 模型回答:
    DeepSeek 是开源大模型 [1]，而 GPT-4 是 OpenAI 开发的闭源大模型 [2]。
  [08:00:06.893] 完成  端到端耗时 1.66s
```

**验证要点**：
- `entities` 非空，确认 LLM 实体抽取成功
- `source="kg"`，content 为 `[标签] 节点名 + 关系列表` 格式
- 耗时比单路 ES 长（因多了一次 LLM 实体抽取调用）
- 若实体抽取失败，`entities=[]`，sources 为空，不报错（降级）

---

### Case 5: vec 向量检索

| 项目 | 说明 |
|---|---|
| **测试场景** | 将 query 经 embedding 模型向量化后检索 Milvus 向量库，召回语义相似文段 |
| **调用方式** | `POST /api/chat`（非流式） |
| **请求参数** | `{"query": "向量数据库的工作原理", "mode": "vec", "stream": false, "top_k": 5}` |
| **输入说明** | mode=vec → embedding.embed(query) + MilvusClient.search；top_k=5 |
| **期望输出** | HTTP 200，`mode: "vec"`，`sources` 含 `source="vec"` 结果（title 为 null，content 为文本字段内容，score 为 distance），`entities: []` |
| **实际输出示例** | |

```
  [08:00:07.234] 请求参数: {"query": "向量数据库的工作原理", "mode": "vec", "stream": false, "top_k": 5}
  [08:00:08.123] 收到响应: HTTP 200  耗时 889ms
  [08:00:08.124] mode=vec  entities=[]
  [08:00:08.124] 检索来源:
    [1] 来源=vec  score=0.9521
        向量数据库通过将文本转换为高维向量，利用近似最近邻算法实现语义相似度检索...
    [2] 来源=vec  score=0.8734
        Milvus 是一款开源向量数据库，支持多种索引类型和距离度量方式...
  [08:00:08.125] 模型回答:
    向量数据库将文本转为向量后，通过近似最近邻（ANN）算法实现语义检索 [1]。Milvus 是典型的开源向量数据库 [2]。
  [08:00:08.126] 完成  端到端耗时 889ms
```

**验证要点**：
- `source="vec"`，title 为 null（vec 检索无标题字段）
- score 为 Milvus distance（COSINE 时越大越相似）
- 不触发实体抽取（`entities=[]`，但 vec 内部会用 LLM 做主题拆分）
- 多路检索：若 query 含多个主题，sources 可能来自不同主题的检索结果
- 若 embedding/milvus 未配置，会返回 502 错误

---

### Case 6: all 三路并行

| 项目 | 说明 |
|---|---|
| **测试场景** | article + qna + kg 三路并行检索（不含 vec），结果直接拼接。验证两阶段并发调度与容错降级 |
| **调用方式** | `POST /api/chat`（非流式） |
| **请求参数** | `{"query": "大模型微调的最佳实践", "mode": "all", "stream": false, "top_k": 3}` |
| **输入说明** | mode=all → _three_way_retrieve（实体抽取 + ES 两路并行 → KG）；top_k=3 |
| **期望输出** | HTTP 200，`mode: "all"`，`entities` 非空，`sources` 含 article + qna + kg 三种来源混合 |
| **实际输出示例** | |

```
  [08:00:09.234] 请求参数: {"query": "大模型微调的最佳实践", "mode": "all", "stream": false, "top_k": 3}
  [08:00:11.567] 收到响应: HTTP 200  耗时 2.33s
  [08:00:11.568] mode=all  entities=['大模型微调', '最佳实践']
  [08:00:11.568] 检索来源:
    [1] 来源=article  标题=大模型微调指南  score=9.4567
        微调是在大规模预训练模型基础上，使用领域数据进一步训练...
    [2] 来源=qna  标题=如何微调大模型？  score=7.1234
        选择合适的基座模型，准备高质量的领域数据集...
    [3] 来源=kg  标题=Fine-tuning
        [Technique] Fine-tuning
          -(applies_to)-> 大语言模型
  [08:00:11.569] 模型回答:
    大模型微调是在预训练基础上用领域数据进一步训练 [1]，需选择合适基座模型和高质量数据 [2]，Fine-tuning 是常用技术 [3]。
  [08:00:11.570] 完成  端到端耗时 2.33s
```

**验证要点**：
- `sources` 同时含 article/qna/kg 三种来源（确认三路都执行了）
- `entities` 非空（实体抽取被触发）
- 耗时约等于最慢一路（并发效果），非三路串行之和
- **不含 vec**（all 模式 by design 不含向量检索）
- 若某路失败（如 ES 不可达），该路贡献 0 条，其他路仍正常返回（容错降级）

---

### Case 7: merge 四路融合

| 项目 | 说明 |
|---|---|
| **测试场景** | article + qna + kg + vec 四路并行检索后，经 rerank 模型（或 RRF 兜底）融合打分，取 top_n 高分项 |
| **调用方式** | `POST /api/chat`（非流式） |
| **请求参数** | `{"query": "知识图谱与向量检索的优缺点", "mode": "merge", "stream": false, "top_k": 3}` |
| **输入说明** | mode=merge → _four_way_retrieve（含 vec）→ rerank.rerank(query, sources, top_k)；top_k=3 |
| **期望输出** | HTTP 200，`mode: "merge"`，`entities` 非空，`sources` 为融合后截断的结果（≤ top_n 条，score 为 rerank 分数或 RRF 分数） |
| **实际输出示例** | |

```
  [08:00:12.234] 请求参数: {"query": "知识图谱与向量检索的优缺点", "mode": "merge", "stream": false, "top_k": 3}
  [08:00:15.890] 收到响应: HTTP 200  耗时 3.66s
  [08:00:15.891] mode=merge  entities=['知识图谱', '向量检索']
  [08:00:15.891] 检索来源:
    [1] 来源=vec  score=0.9812
        知识图谱擅长结构化关系推理，向量检索擅长语义相似匹配...
    [2] 来源=article  标题=知识图谱 vs 向量检索  score=0.9234
        知识图谱提供精确的关系查询，向量检索提供模糊的语义召回...
    [3] 来源=kg  标题=KnowledgeGraph
        [Concept] KnowledgeGraph
          -(complement_to)-> VectorSearch
  [08:00:15.892] 模型回答:
    知识图谱擅长结构化关系推理 [2]，向量检索擅长语义相似匹配 [1]，二者互补 [3]。
  [08:00:15.893] 完成  端到端耗时 3.66s
```

**验证要点**：
- `sources` 来源混合（含 vec，确认四路都执行了）
- `sources` 条数 ≤ top_k（3），确认 rerank 融合后截断
- score 为 rerank 分数（若用 RRF 兜底则为 RRF 分数 `1/(60+rank)`，值很小如 0.0167）
- 耗时比 all 长（多了 vec 的 embedding + rerank 调用）
- 若 rerank 未配置，自动降级 RRF，不报错

---

### Case 8: 流式 SSE（all）

| 项目 | 说明 |
|---|---|
| **测试场景** | all 模式 + 流式输出，验证 SSE 事件序列（meta→token→done）与前端解析逻辑 |
| **调用方式** | `POST /api/chat`（流式，`Accept: text/event-stream`） |
| **请求参数** | `{"query": "解释一下 embedding 向量化", "mode": "all", "stream": true, "top_k": 3}` |
| **输入说明** | stream=true → 返回 `text/event-stream`；mode=all → 三路检索 |
| **期望输出** | HTTP 200，`content-type: text/event-stream`，先收 `meta` 事件（含 sources/entities），再逐个收 `token` 事件（逐字输出），最后 `done` 事件 |
| **实际输出示例** | |

```
  [08:00:16.234] 请求参数: {"query": "解释一下 embedding 向量化", "mode": "all", "stream": true, "top_k": 3}
  [08:00:16.235] POST http://localhost:8000/api/chat  (text/event-stream)
  [08:00:16.345] 连接建立: HTTP 200  content-type=text/event-stream; charset=utf-8
  [08:00:18.567] [meta 事件] 耗时 2.33s  mode=all
    抽取实体: ['embedding', '向量化']
  检索来源:
    [1] 来源=article  标题=Embedding 技术入门  score=8.1234
        Embedding 是将文本转换为向量表示的技术...
    [2] 来源=qna  标题=什么是 embedding？  score=6.5678
        embedding 将文字映射到高维空间...
  [08:00:18.568] 开始接收 token...
  [08:00:18.890] 首 token 延迟: 2.66s
  Embedding 是将文本转换为高维向量表示的技术 [1]，使得语义相近的文本在向量空间中距离更近 [2]。
  [08:00:19.567] [done 事件]
  [08:00:19.568] token 数: 42
  [08:00:19.568] 首 token 延迟: 2.66s
  [08:00:19.569] 总耗时: 3.34s
```

**验证要点**：
- `content-type` 含 `text/event-stream`
- `meta` 事件先于 token 到达，含完整 sources 和 entities
- 首 token 延迟 = meta 完成时间 + LLM 首个 token（约等于检索耗时 + LLM TTFT）
- token 逐字实时打印到终端（打字机效果）
- `done` 事件标志结束，打印 token 总数与总耗时
- 若流式过程中出错，收到 `error` 事件而非中断连接

---

## 3. 非流式 vs 流式打印差异

| 维度 | 非流式（Case 1~7） | 流式（Case 8） |
|---|---|---|
| HTTP 调用 | `client.post()` 一次性等待 | `client.stream()` 持续读取 |
| 响应类型 | `application/json` | `text/event-stream` |
| 来源展示 | 响应 JSON 中的 `sources` | `meta` 事件的 `sources` |
| 回答展示 | 响应 JSON 中的 `answer`，一次性打印 | `token` 事件逐字 `print(content, end="")` |
| 耗时统计 | 端到端总耗时 | 首 token 延迟 + 总耗时 |
| 异常处理 | HTTP 非 200 打印错误 | `error` 事件打印错误 |

---

## 4. 输出中的关键字段解读

### 4.1 sources 数组

每条来源的 `source` 字段标识检索路：

| source 值 | 来源 | score 含义 | title 含义 |
|---|---|---|---|
| `article` | ES article 索引 | ES `_score`（BM25 相关度） | 文章标题 |
| `qna` | ES qna 索引 | ES `_score` | 问题文本 |
| `kg` | Neo4j 图谱 | 无（null） | 图谱节点名 |
| `vec` | Milvus 向量库 | distance（COSINE 时越大越相似） | null |

merge 模式下 score 会被 rerank 分数或 RRF 分数覆盖。

### 4.2 entities 数组

仅 `kg`/`all`/`merge` 模式非空，为 LLM 从 query 中抽取的实体列表。`article`/`qna`/`vec`/纯对话模式为空。

### 4.3 耗时指标

| 指标 | 含义 |
|---|---|
| 端到端耗时 | 从发起到收到完整响应的总时间 |
| 首 token 延迟（流式） | 从发起到收到第一个 `token` 事件的时间（≈ 检索耗时 + LLM TTFT） |
| meta 事件耗时（流式） | 从发起到收到 `meta` 事件的时间（≈ 检索阶段耗时） |

---

## 5. 异常场景说明

| 场景 | 表现 |
|---|---|
| 服务未启动 | 健康检查 `✗ 连接失败`，脚本退出 |
| LLM 未配置/鉴权失败 | HTTP 200 但回答为错误提示；流式时收到 `error` 事件 |
| ES 未配置 | article/qna 来源为空，不报错（降级） |
| Neo4j 未配置 | kg 来源为空，实体抽取可能失败但降级为空列表 |
| Milvus/embedding 未配置 | vec 模式返回 502 错误；merge 模式 vec 路降级为空 |
| Rerank 未配置 | merge 模式自动用 RRF 兜底，不报错 |
| 空 query | HTTP 422（Pydantic 校验失败） |

---

## 6. 命令行参数

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `--base-url` | string | `http://localhost:8000` | 服务地址 |
| `--skip` | string[] | `[]` | 跳过的 case 名称关键词（不区分大小写，匹配 case name） |

**skip 匹配示例**：
- `--skip kg` → 跳过 "kg 知识图谱检索"
- `--skip vec merge` → 跳过 "vec 向量检索" 和 "merge 四路融合"
- `--skip 流式` → 跳过 "流式 SSE（all）"
