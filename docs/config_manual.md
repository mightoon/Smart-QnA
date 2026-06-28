# ES 索引配置手册 (配置手册.md)

本文档说明 Elasticsearch 索引侧的推荐配置，以提升检索质量。这些配置在 **ES 侧完成**，不涉及服务代码改动，服务端通过 `data/config.json` 的 `elasticsearch` 段配置索引名和连接信息即可。

---

## 1. 前置：服务端 ES 配置回顾

服务端 `data/config.json` 中 ES 段：

```json
"elasticsearch": {
  "active": "default",
  "items": {
    "default": {
      "name": "默认",
      "url": "http://localhost:9200",
      "username": "",
      "password": "",
      "article_index": "article",
      "article_fields": {"body": "content", "title": "title"},
      "qna_index": "qna",
      "qna_fields": {"body": "answer", "title": "question"},
      "verify_certs": false
    }
  }
}
```

服务端字段映射通过配置项 `article_fields` / `qna_fields` 设置，未配置时使用默认值：

| 索引 | 检索字段（body，默认） | 展示字段（title，默认） |
|---|---|---|
| article | `content` | `title` |
| qna | `answer` | `question` |

> 如 ES 索引字段名与默认值不同（如正文字段叫 `text` 而非 `content`），修改配置中的 `article_fields` / `qna_fields` 即可，无需改代码。

---

## 2. 中文分词器配置

### 2.1 为什么需要

ES 默认的 `standard` 分词器对中文按单字切分，"检索增强生成"会被切成"检""索""增""强""生""成"，导致：
- 单字匹配噪声大（"生"能匹配大量无关文档）
- 无法识别中文词语边界

### 2.2 安装 IK 分词器插件

```bash
# ES 安装目录下执行（版本需与 ES 版本一致）
./bin/elasticsearch-plugin install https://github.com/medcl/elasticsearch-analysis-ik/releases/download/v8.12.0/elasticsearch-analysis-ik-8.12.0.zip

# 重启 ES
```

验证安装：
```bash
GET /_cat/plugins
# 应输出: node analysis-ik 8.12.0
```

### 2.3 IK 分词模式

| 模式 | 说明 | 示例（"检索增强生成技术"） |
|---|---|---|
| `ik_smart` | 粗粒度，倾向整体 | 检索 / 增强 / 生成 / 技术 |
| `ik_max_word` | 细粒度，穷尽切分 | 检索 / 检 / 索 / 增强 / 增 / 强 / 生成 / 生 / 成 / 技术 |

推荐：**索引时用 `ik_max_word`（最大化召回），搜索时用 `ik_smart`（减少噪声）**。

---

## 3. 停用词过滤

### 3.1 为什么需要

用户问题如"什么是 RAG 检索增强生成"中的"什么""是"为高频停用词，虽然 BM25 的 IDF 机制会自动降权，但主动过滤更干净，减少无意义匹配。

### 3.2 配置方式

在索引 settings 中自定义 analyzer，串联停用词过滤器：

```json
PUT /article
{
  "settings": {
    "analysis": {
      "filter": {
        "my_stop": {
          "type": "stop",
          "stopwords": ["什么", "是", "的", "了", "在", "和", "与", "及", "或", "如何", "怎么", "哪些", "请", "请问", "一下", "可以", "吗", "呢", "吧", "啊"]
        }
      }
    }
  }
}
```

ES 也内置了中文停用词表，可直接用：
```json
"my_stop": {
  "type": "stop",
  "stopwords": "_chinese_"
}
```

---

## 4. 同义词扩展（外部词典方式）

### 4.1 为什么需要

BM25 是字面匹配，"大模型"搜不到只写"LLM"的文档，"微调"搜不到只写"fine-tuning"的文档。系统通过**外部同义词词典**在查询前对 query 做双向扩展，无需配置 ES 索引，无需改 ES settings。

### 4.2 实现原理

同义词扩展在**服务代码侧**完成（`es_client.py` 的 `_expand_query` 函数），不依赖 ES 内置 synonym filter：

1. 启动时加载 `data/synonyms.json` 词典
2. 将词典展开为**双向映射**——每个词映射到其所在同义词组的全部词
3. 检索前遍历词典，若 query 中包含某词，则将该词的全部同义词以 `OR` 组合加入查询
4. 原始 query 始终保留（作为整体短语匹配）

### 4.3 词典文件格式

文件位置：`data/synonyms.json`

```json
{
  "大模型": ["LLM", "大语言模型", "large language model"],
  "微调": ["fine-tuning", "fine tuning", "指令微调", "SFT"],
  "检索增强生成": ["RAG", "retrieval augmented generation"],
  "知识图谱": ["KG", "knowledge graph", "图谱"]
}
```

- key 是主词，value 是同义词列表
- key 和 value 中的所有词构成一个**等价组**，组内任意一词出现在 query 中，都会扩展出全组所有词
- 双向：用户问"微调"能扩展出"SFT"，用户问"SFT"也能扩展出"微调"

### 4.4 扩展效果示例

词典配置：`"微调": ["fine-tuning", "fine tuning", "指令微调", "SFT"]`

| 用户 query | 扩展后 ES 查询 |
|---|---|
| `大模型微调方法` | `大模型微调方法 OR 大模型 OR LLM OR 大语言模型 OR large language model OR 微调 OR fine-tuning OR fine tuning OR 指令微调 OR SFT` |
| `SFT最佳实践` | `SFT最佳实践 OR SFT OR 微调 OR fine-tuning OR fine tuning OR 指令微调` |

### 4.5 维护方式

直接编辑 `data/synonyms.json` 文件即可增删同义词组。修改后需重启服务生效（当前为进程级缓存）。

> 与 ES 内置 synonym filter 的对比见 §4.6。

### 4.6 外部词典 vs ES 内置 synonym 对比

| 维度 | 外部词典（当前方案） | ES 内置 synonym filter |
|---|---|---|
| 配置位置 | `data/synonyms.json` 文件 | ES 索引 settings |
| 改同义词 | 改文件，重启服务 | 改 ES settings，需重建索引或热更新 |
| 双向扩展 | 自动双向 | 需用逗号语法（`a, b, c`）才双向 |
| 索引侧扩展 | 否（仅查询侧） | 是（索引和查询双向） |
| ES 版本要求 | 无 | ES 5.x+ |
| 依赖 ES 配置 | 不依赖 | 依赖 |

当前系统采用**外部词典方式**，与 ES 索引配置解耦。如同时需要在索引侧也做同义词扩展（如索引时就把"LLM"存为"大模型"），可额外配置 ES 内置 synonym filter，两者不冲突。

---

## 5. minimum_should_match 提升精确度

当前服务端查询模板未设置 `minimum_should_match`，即只需匹配一个词即返回。可通过修改 `es_client.py` 添加该参数，要求至少匹配一定比例的词：

```python
# es_client.py 中 _search 方法的 body
body = {
    "size": top_k,
    "query": {
        "multi_match": {
            "query": query,
            "fields": [f"{fields['body']}^3", fields["title"]],
            "type": "best_fields",
            "minimum_should_match": "60%"   # 新增：至少匹配 60% 的词
        }
    },
}
```

| 值 | 效果 |
|---|---|
| `"1"` (默认) | 至少匹配 1 个词，召回高但噪声多 |
| `"60%"` | 至少匹配 60% 的词，平衡召回与精度 |
| `"75%"` | 至少匹配 75% 的词，精度高但可能漏召回 |

> 此项需要改代码，不属于纯 ES 配置范畴，但可作为可选优化。

---

## 6. 验证分词效果

配置完成后，用 `_analyze` API 验证 ES 分词与停用词过滤效果：

```bash
# 验证分词
GET /article/_analyze
{
  "analyzer": "search_analyzer",
  "text": "什么是大模型微调"
}

# 预期输出（停用词被过滤）：
# token: 大 / 模型 / 微调（若用 IK 分词器则为：大模型 / 微调）
# "什么""是" 被停用词过滤器移除
```

同义词扩展效果验证（服务代码侧，非 ES API）：

```bash
# 在项目根目录执行
python -c "from app.retrieval.es_client import _expand_query; print(_expand_query('大模型微调'))"
# 输出：大模型微调 OR 大模型 OR LLM OR 大语言模型 OR large language model OR 微调 OR fine-tuning OR fine tuning OR 指令微调 OR SFT
```

---

## 8. Neo4j 图谱检索配置

KG 检索的连接信息与查询参数从 `data/config.json` 的 `neo4j` 段读取：

```json
"neo4j": {
  "active": "default",
  "items": {
    "default": {
      "name": "默认",
      "uri": "bolt://localhost:7687",
      "username": "neo4j",
      "password": "b64:...",
      "database": "neo4j",
      "node_key": "name",
      "max_neighbors": 10,
      "excluded_relations": ["contains", "references"]
    }
  }
}
```

### 8.1 配置项说明

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `database` | `neo4j` | 数据库名，`session(database=...)` 使用，支持多数据库隔离 |
| `node_key` | `name` | 节点主键属性名，用于实体匹配。如图谱节点主键叫 `title` 则改为 `title` |
| `max_neighbors` | `10` | 每个节点最多保留的邻居关系数，避免热门节点关系过多导致上下文膨胀 |
| `excluded_relations` | `["contains", "references"]` | 排除的关系类型列表，过滤语义价值低的结构性关系 |

### 8.2 关系过滤

Cypher 查询中通过 `WHERE NOT type(r) IN $excluded` 过滤排除的关系类型，同时通过 `coalesce(m.name, '') <> ''` 去除空目标名的关系。

默认排除 `contains` 和 `references`——这两种通常是图谱构建时的结构性关系（如文档包含段落、引用其他节点），对问答无语义价值。如需排除更多关系类型，在配置中追加：

```json
"excluded_relations": ["contains", "references", "belongs_to", "part_of"]
```

### 8.3 邻居数量限制

`max_neighbors` 控制每个节点返回的邻居关系数。Cypher 中通过 `all_rels[..{max_neighbors}]` 截取前 N 条。设小值可减少噪声，设大值可提供更多上下文：

| 值 | 效果 |
|---|---|
| `5` | 精简，仅核心关系 |
| `10`（默认） | 平衡 |
| `20` | 详尽，但可能导致上下文膨胀 |

### 8.4 节点主键属性

不同图谱的节点主键属性可能不同（如 `name`/`title`/`id`/`label`），通过 `node_key` 配置适配：

```json
"node_key": "title"
```

Cypher 中 `WHERE toLower(n.{node_key}) CONTAINS toLower($entity)` 会动态使用配置的属性名。

---

## 9. 配置清单速查

| 配置项 | 位置 | 解决问题 | 是否改代码 |
|---|---|---|---|
| IK 中文分词器 | ES 索引 settings | 中文按词切分而非单字 | 否（ES 侧） |
| 停用词过滤 | ES 索引 settings | 过滤"什么/是/的"等噪声词 | 否（ES 侧） |
| 同义词扩展（外部词典） | `data/synonyms.json` | "大模型"↔"LLM" 字面不匹配问题 | 否（已内置） |
| ES 字段映射 | `config.json` elasticsearch 段 | 适配不同 ES 索引字段名 | 否（已内置） |
| KG database/节点主键/邻居数/关系过滤 | `config.json` neo4j 段 | 适配不同图谱 schema + 结果过滤 | 否（已内置） |
| minimum_should_match | es_client.py 查询模板 | 要求多词匹配才返回 | 是 |

前五项为配置即可生效；最后一项需改代码，按需选择。
