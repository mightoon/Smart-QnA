# Product Requirement Document & Technical Specification (spec.md)

## 1. 项目概述 (Project Overview)

本项目是一个基于 Python 构建的**核心后端智能问答服务（API Service）**。该服务主要供其他业务模块调用。
系统主要包含两大模块，存在明确的主次关系：

1. **核心后端服务 (Backend API) [核心]**：接收前端/外部模块的 HTTP POST 请求，根据传入的参数（`article`, `qna`, `kg`, `vec`, `all`, `merge`, 或无参数）执行不同的 RAG（检索增强生成）策略，组合 Elasticsearch、Neo4j、Milvus 的检索结果，并通过大语言模型（LLM）将总结后的答案以结构化（默认流式）的方式返回给调用方。
2. **辅助管理控制台 (Admin & Testing UI) [配套]**：一个基于 B/S 架构的轻量级 Web 界面。主要作用是方便管理员管理后端服务各项基础工具（LLM / Embedding / ES / Neo4j / Milvus / Rerank）的多份配置（增删改查、切换生效、可用性验证），提供一个沙盒供开发者测试 API 链路，以及提供可视化监控面板查看访问量、延迟、错误率、token 消耗等运行指标。

### 1.1 核心能力
* **多策略 RAG**：`article` / `qna` / `kg` / `vec` / `all` / `merge` / 无参数 七种检索模式。
* **SSE 流式输出**：基于 Server-Sent Events 实时推送模型 token，事件序列 `meta→token→done`（异常时 `error`）。
* **并行检索**：`all` / `merge` 模式下多路检索通过 `asyncio.create_task` + `asyncio.gather(return_exceptions=True)` 并发执行，降低延迟。
* **实体抽取**：`kg` / `all` / `merge` 模式先调用 LLM 抽取查询实体（temperature=0.0，JSON 输出），再检索知识图谱；失败降级为空列表不阻断主流程。
* **向量检索**：`vec` 模式将 query 经 embedding 模型向量化后检索 Milvus 向量数据库，召回语义相似文段。
* **融合检索**：`merge` 模式四路检索后经 rerank 模型（Cohere/Jina 兼容 API）融合打分取 top_n；rerank 未配置或失败时降级为 RRF（Reciprocal Rank Fusion，`1/(60+rank)`）。
* **多配置管理**：每个基础工具支持多份命名配置（items + active 结构），可在线增删改查、切换生效项；切换与修改前均需通过可用性验证。
* **敏感字段保护**：`api_key` / `password` 落盘时以 `b64:` 前缀 + Base64 编码存储，读取时自动解码，对外展示自动脱敏（`前2位+****+后2位`）；手动填写明文（无 `b64:` 前缀）亦可正常识别。
* **双系统提示词**：纯对话模式使用通用对话提示词 `CHAT_SYSTEM_PROMPT`，允许模型自由作答；RAG 模式使用 `RAG_SYSTEM_PROMPT`，要求严格依据资料作答并用 `[n]` 标注引用。
* **可视化监控**：通过 `async with measure(category, operation)` 上下文管理器非侵入式埋点，内存环形缓冲区（deque, maxlen=50000）存储事件，提供 summary（P50/P95/P99/错误率/token）与分桶 timeseries 聚合，前端以 Chart.js 图表展示。
* **请求日志**：LLM 调用时在服务控制台打印完整 POST body（含 base_url、api_key 脱敏前缀、model、messages、temperature、max_tokens），便于排查。

## 2. 技术栈 (Technology Stack)
* **核心框架**：FastAPI + Uvicorn（原生支持异步处理和 SSE 流式输出）。
* **配套 Web UI**：**纯静态 HTML/JS + CSS**（通过 FastAPI 挂载 `static` 目录提供服务），零构建，避免 Vue/React 等重型工程化前端框架。
* **图表库**：Chart.js 4.x（via CDN `https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js`），用于可视化监控面板。
* **大模型调用**：`openai` SDK（`AsyncOpenAI`，兼容 OpenAI 格式的各类大模型）。
* **关键字检索**：`elasticsearch` (Python `AsyncElasticsearch` client)。
* **图谱检索**：`neo4j` (Python `AsyncGraphDatabase` driver)。
* **向量检索**：`pymilvus` (`MilvusClient`)。
* **Rerank 调用**：`httpx` (AsyncClient，调用 Cohere/Jina 兼容 rerank API)。
* **数据建模**：Pydantic v2。
* **测试框架**：`pytest` + `pytest-asyncio` + `httpx`（TestClient，含异步 API 与 SSE 流式测试）。

## 3. 系统逻辑架构 (Logical Architecture)

系统采用分层架构设计：

```
┌──────────────────────────────────────────────────────────────┐
│                    API Layer (接入层)                          │
│  /api/chat  /api/health  /api/config/**  /api/metrics        │
│  参数校验 · SSE 流式 · 配置 CRUD · 验证 · 指标查询              │
│  全局异常处理器 AppException → {error:{code,message,detail}}  │
└──────────┬─────────────────────────────────┬─────────────────┘
           │                                 │
   ┌───────▼──────────┐          ┌───────────▼──────────┐
   │ Orchestrator     │          │ Config Manager        │
   │ (调度层)          │          │ (配置管理层)           │
   │ 检索策略·并行调度  │          │ 多配置CRUD·加解密      │
   │ 上下文组装·SSE    │          │ 脱敏·活跃项切换        │
   │ 指标埋点          │          │ 合并保留原值           │
   └─┬─────┬────┬────┬┬─┘          └───────────────────────┘
     │     │    │    │
┌────▼─┐┌──▼─┐┌─▼──┐┌▼───────┐    ┌──────────────────┐
│ ES   ││ KG ││Vec ││Rerank  │    │ Metrics Collector│
│Client││Clt ││Clt ││Client  │    │ (指标采集)        │
│BM25  ││Cyph││ANN ││API+RRF │    │ summary/ts/埋点  │
│检索  ││检索││检索││融合    │    │ P50/P95/P99     │
│验证  ││验证││验证││验证    │    └──────────────────┘
└──────┘└────┘└────┘└────────┘
     │              │
     │     ┌────────┴────────┐
     │     │ LLM Service     │   Embedding Service
     │     │ (大模型层)       │   (向量化层)
     │     │ 流式/非流式      │   /embeddings API
     │     │ 实体抽取         │   validate
     │     │ validate         │
     │     │ 双提示词·日志    │
     │     └─────────────────┘   └──────────────┘
```

### 3.1 各层职责

* **API Layer (接入层)** `app/api/`：
  * 处理 HTTP 接口调用、Pydantic 参数校验、SSE 流式连接管理（`StreamingResponse` + `text/event-stream`）。
  * 提供配置项的增删改查、生效切换、可用性验证等管理接口。
  * 提供指标查询接口（`GET /api/metrics`）。
  * 全局异常处理器：`AppException` → 结构化 JSON `{error:{code,message,detail}}`；SSE 流式中异常以 `error` 事件返回。
  * 路由层通过模块属性访问依赖（`deps.get_xxx()`），便于测试 monkeypatch 注入 mock。

* **Orchestrator Layer (调度层)** `app/retrieval/orchestrator.py`：
  * 根据请求参数 (`article`, `qna`, `kg`, `vec`, `all`, `merge`, 无) 决定检索策略（并行/单一/不检索）和 LLM 调用策略。
  * 多路检索采用两阶段并发：阶段1 并行启动实体抽取 + ES 检索 + (merge 时) vec 检索；阶段2 实体就绪后启动 KG 检索；阶段3 `asyncio.gather` 汇总。
  * 单路失败通过 `gather(return_exceptions=True)` + `_unwrap()` 降级为空列表，不阻断整体。
  * 将检索结果按 `[n]` 编号组装为上下文，驱动 LLM 流式/非流式生成。
  * 通过 `async with measure("chat", "request")` 埋点。

* **Retrieval Layer (检索层)** `app/retrieval/`：
  * **Entity Extractor**（`llm_client.extract_entities`）：调用 LLM 提取用户 query 中的核心实体（仅 `kg` / `all` / `merge` 模式触发，temperature=0.0，要求返回 JSON `{"entities":[...]}`，解析容错：json.loads 失败则正则抽取首个 `{...}`；失败降级为空列表不阻断主流程）。
  * **ES Client**（`es_client.py`）：针对 `article` 和 `qna` index 进行 BM25 `multi_match` 关键字检索（正文字段 `^3` 加权，`best_fields` 策略）。
  * **Neo4j Client**（`kg_client.py`）：基于抽取实体在知识图谱中检索相关节点及邻居关系。Cypher 模板由 `_build_query` 动态构建，支持可配置 database、节点主键属性（`node_key`）、邻居数量限制（`max_neighbors`）、关系类型过滤（`excluded_relations`，排除 contains/references 等无关关系、去除空目标名）。
  * **Vec Client**（`vec_client.py`）：将 query 经 Embedding 向量化后检索 Milvus 向量数据库（`MilvusClient.search` ANN，支持 COSINE/L2/IP 度量），召回语义相似文段。默认采用多路检索（LLM 抽取主题 → 各主题分别向量化检索 → 合并去重），解决多主题 query 整体向量化漏召回问题；LLM 不可用时降级为单路检索。
  * **Rerank Client**（`rerank_client.py`）：对多路检索结果统一打分排序（rerank API 或 RRF 兜底），取 top_n。
  * 各客户端均提供 `validate()` 方法用于可用性验证、`close()` 方法释放连接、`reconfigure()` 方法重置内部连接。

* **LLM/Embedding Service Layer (模型层)** `app/core/`：
  * **LLMClient**：封装 `AsyncOpenAI` 调用；`stream_chat()` 流式逐 token 产出、`chat()` 非流式收集完整回答、`extract_entities()` 实体抽取（temperature=0.0, max_tokens=256）；区分纯对话与 RAG 两套系统提示词；`_log_request()` 打印完整 POST body 便于排查；`validate()` 发起极小请求验证。
  * **EmbeddingClient**：封装 `AsyncOpenAI` 调用 `/embeddings` 接口；`embed(text)` 返回浮点向量；`validate()` 发起 `embed("ping")` 验证并返回维度。

* **Config Manager (配置管理层)** `app/core/config_manager.py`：
  * 管理本地 JSON 文件读写（线程安全 `RLock`，写操作通过临时文件 + `os.replace` 保证原子性）。
  * 支持每个工具段多份命名配置（items + active 结构）。
  * 负责敏感字段的 `b64:` 前缀 Base64 加解密与对外脱敏。
  * 提供配置项 CRUD（`upsert_item`/`delete_item`）、活跃项切换（`set_active`）、合并保留原值（`_merge_sensitive`）、解析合并（`resolve_item`）等能力。

* **Metrics Collector (指标采集层)** `app/core/metrics.py`：
  * `MetricsCollector`：内存环形缓冲区（`deque(maxlen=50000)`），线程安全（`Lock`）。
  * `record()`：记录单条 `MetricEvent`（timestamp/category/operation/duration_ms/success/tokens/error）。
  * `summary(start, end)`：按类别聚合（count/errors/error_rate/avg_ms/tokens）+ chat 请求延迟分位数（P50/P95/P99，线性插值）。
  * `timeseries(start, end, buckets=60)`：分桶时间序列（labels/counts/errors/latencies/bucket_size_s）。
  * `measure(category, operation)`：`asynccontextmanager`，自动记录进入/退出时间、异常状态，非侵入式埋点。

## 4. 配置数据模型 (Configuration Model)

配置文件位于 `data/config.json`，采用多配置结构。每个基础工具段（`llm` / `elasticsearch` / `neo4j` / `rerank` / `embedding` / `milvus`）包含 `active`（当前生效项 ID）与 `items`（配置项字典，key 为配置项 ID）；`server` 段为普通扁平配置。

### 4.1 完整结构示例
```json
{
  "llm": {
    "active": "deepseek",
    "items": {
      "deepseek": {
        "name": "DeepSeek",
        "api_base": "https://tokenhub.tencentmaas.com/plan/v3",
        "api_key": "b64:c2stdHAtaEhqN05zVkNQT1hYS1NEM0pYTmJoRFpuamRVdXdZdnBlMVd6dk83T29nSnVXMVpz",
        "model": "deepseek-v4-flash",
        "entity_model": "deepseek-v4-flash",
        "temperature": 0.7,
        "max_tokens": 2048
      }
    }
  },
  "elasticsearch": {
    "active": "default",
    "items": {
      "default": {
        "name": "默认",
        "url": "http://localhost:9200",
        "username": "elastic",
        "password": "b64:...",
        "article_index": "article",
        "qna_index": "qna",
        "verify_certs": false
      }
    }
  },
  "neo4j": {
    "active": "default",
    "items": {
      "default": {
        "name": "默认",
        "uri": "bolt://localhost:7687",
        "username": "neo4j",
        "password": "b64:..."
      }
    }
  },
  "rerank": {
    "active": "default",
    "items": {
      "default": {
        "name": "默认",
        "api_base": "",
        "api_key": "",
        "model": "",
        "top_n": 5
      }
    }
  },
  "embedding": {
    "active": "default",
    "items": {
      "default": {
        "name": "默认",
        "api_base": "",
        "api_key": "b64:...",
        "model": "text-embedding-3-small"
      }
    }
  },
  "milvus": {
    "active": "default",
    "items": {
      "default": {
        "name": "默认",
        "uri": "http://localhost:19530",
        "collection_name": "documents",
        "vector_field": "embedding",
        "text_field": "content",
        "metric_type": "COSINE"
      }
    }
  },
  "server": { "host": "0.0.0.0", "port": 8000 }
}
```

### 4.2 敏感字段处理规则

**敏感字段定义**（`ITEM_SECTIONS` 映射）：
| 段 | 敏感字段 |
|---|---|
| llm | `api_key` |
| elasticsearch | `password` |
| neo4j | `password` |
| rerank | `api_key` |
| embedding | `api_key` |
| milvus | （无） |

**处理流程**：
* **编码（落盘 `save`）**：遍历所有 items 中的敏感字段，值带 `b64:` 前缀 + Base64 编码。已编码（已有前缀）的值不重复编码。写操作通过临时文件 `.json.tmp` + `os.replace` 保证原子性。
* **解码（读取 `reload`）**：仅对带 `b64:` 前缀的值解码；**无前缀则视为明文原样使用**，因此用户手动编辑配置文件填写的明文密钥也能正常工作。
* **脱敏（对外展示 `masked`）**：`GET /api/config` 返回脱敏快照，敏感字段显示为 `前2位 + **** + 后2位`（长度≤4 时全 `****`）。
* **修改保留（`_merge_sensitive`）**：更新配置项时（`upsert_item` create=False），若敏感字段值为空或含 `*`（脱敏占位），则保留已存储的原值，避免清空。判断逻辑：`_is_masked(value)` → `value is None or not str(value) or "*" in str(value)`。

### 4.3 各工具段字段说明

| 段 | 字段 | 类型 | 说明 |
|---|---|---|---|
| llm | name | str | 配置名称（展示用） |
| llm | api_base | str | OpenAI 兼容 API 地址（如 `https://api.openai.com/v1`） |
| llm | api_key | str(敏感) | 鉴权密钥 |
| llm | model | str | 对话模型名（如 `gpt-4o-mini`） |
| llm | entity_model | str | 实体抽取模型名（可与 model 不同） |
| llm | temperature | float | 生成温度（默认 0.7） |
| llm | max_tokens | int | 最大生成 token 数（默认 2048） |
| elasticsearch | name | str | 配置名称 |
| elasticsearch | url | str | ES 地址（如 `http://localhost:9200`） |
| elasticsearch | username | str | 用户名（可选） |
| elasticsearch | password | str(敏感) | 密码（可选） |
| elasticsearch | article_index | str | 文章索引名（默认 `article`） |
| elasticsearch | article_fields | dict | article 索引字段映射 `{"body":"content","title":"title"}`，未配置用默认 |
| elasticsearch | qna_index | str | 问答索引名（默认 `qna`） |
| elasticsearch | qna_fields | dict | qna 索引字段映射 `{"body":"answer","title":"question"}`，未配置用默认 |
| elasticsearch | verify_certs | bool | 是否验证证书（默认 false） |
| neo4j | name | str | 配置名称 |
| neo4j | uri | str | Bolt 地址（如 `bolt://localhost:7687`） |
| neo4j | username | str | 用户名（默认 `neo4j`） |
| neo4j | password | str(敏感) | 密码 |
| neo4j | database | str | 数据库名（默认 `neo4j`） |
| neo4j | node_key | str | 节点主键属性名（默认 `name`），用于实体匹配 |
| neo4j | max_neighbors | int | 每节点最大邻居关系数（默认 10） |
| neo4j | excluded_relations | array | 排除的关系类型（默认 `["contains","references"]`） |
| rerank | name | str | 配置名称 |
| rerank | api_base | str | Rerank 服务地址（如 `https://api.cohere.ai/v1`） |
| rerank | api_key | str(敏感) | 鉴权密钥 |
| rerank | model | str | Rerank 模型名（如 `rerank-multilingual-v3.0`） |
| rerank | top_n | int | 融合后保留条数（0 表示用 top_k） |
| embedding | name | str | 配置名称 |
| embedding | api_base | str | Embedding API 地址 |
| embedding | api_key | str(敏感) | 鉴权密钥 |
| embedding | model | str | 向量化模型名（如 `text-embedding-3-small`） |
| milvus | name | str | 配置名称 |
| milvus | uri | str | Milvus 地址（如 `http://localhost:19530`） |
| milvus | collection_name | str | 检索的 collection（默认 `documents`） |
| milvus | vector_field | str | 向量字段名（默认 `embedding`） |
| milvus | text_field | str | 文本字段名（默认 `content`） |
| milvus | metric_type | str | 距离度量（COSINE / L2 / IP，默认 COSINE） |
| server | host | str | 服务监听地址 |
| server | port | int | 服务监听端口 |

### 4.4 ConfigManager 方法

| 方法 | 说明 |
|---|---|
| `reload()` | 从磁盘加载配置，解码敏感字段 |
| `save(config)` | 编码敏感字段后原子写入磁盘 |
| `get()` | 返回内部明文配置深拷贝 |
| `get_section(section)` | 返回指定段 |
| `get_active_item(section)` | 返回当前生效配置项（明文）；active 无效时回退首个 |
| `get_item(section, item_id)` | 返回指定配置项（明文） |
| `masked()` | 返回脱敏快照 |
| `masked_section(section)` | 返回指定段脱敏快照 |
| `upsert_item(section, item_id, fields, create)` | 新增(create=True)或更新(create=False，合并敏感字段)配置项；首次自动设为 active |
| `delete_item(section, item_id)` | 删除配置项；删除生效项后 active 回退到剩余首项 |
| `set_active(section, item_id)` | 切换生效配置项 |
| `resolve_item(section, item_id, fields)` | 合并已存储项与传入字段（敏感字段保留原值） |
| `update_server(fields)` | 更新 server 段 |

## 5. 工程目录结构 (Directory Structure)
```text
project_root/
├── app/
│   ├── __init__.py             # 包定义 (__version__)
│   ├── main.py                 # FastAPI 应用入口 (挂载 API 路由 + UI 静态资源 + lifespan + 全局异常处理器)
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes.py           # API 路由 (/api/chat, /api/health, /api/config/**, /api/metrics)
│   │   └── dependencies.py     # 依赖注入 (lru_cache 单例客户端 + 配置管理 + rebuild_clients)
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config_manager.py   # 多配置读写 + Base64(b64:前缀)加解密 + CRUD + 脱敏 + 合并保留原值
│   │   ├── llm_client.py       # LLM 封装 (stream_chat/chat + extract_entities + validate + 双提示词 + _log_request + 指标埋点)
│   │   ├── embedding_client.py # Embedding 向量化 (embed + validate + 指标埋点)
│   │   ├── metrics.py          # 内存指标采集 (MetricsCollector + measure 上下文管理器 + summary/timeseries 聚合)
│   │   └── exceptions.py       # 自定义异常体系 (AppException 基类 + 子类)
│   ├── retrieval/
│   │   ├── __init__.py
│   │   ├── es_client.py        # Elasticsearch BM25 检索 + validate + close + 指标埋点
│   │   ├── kg_client.py        # Neo4j 图谱检索 (动态Cypher+database/节点主键/邻居数/关系过滤可配置) + validate + 指标埋点
│   │   ├── vec_client.py       # Milvus 向量检索 + validate + close + 指标埋点
│   │   ├── rerank_client.py    # Rerank 融合 (API + RRF兜底) + validate + 指标埋点
│   │   └── orchestrator.py     # RAG 调度 (article/qna/kg/vec/all/merge/无) + 两阶段并发 + SSE 组装 + 指标埋点
│   └── schemas/
│       ├── __init__.py
│       ├── requests.py         # Pydantic 输入模型 (ChatRequest, ChatMode, ChatMessage, SetActiveRequest, ServerConfigRequest)
│       └── responses.py        # Pydantic 输出模型 (ChatResult, SourceItem, HealthResponse, ValidateResult, ErrorResponse)
├── ui/
│   ├── index.html              # 管理与测试主界面 (问答沙盒 + 配置管理 + 可视化监控 + docs 链接)
│   ├── js/app.js               # 前端逻辑 (SSE 解析、配置 CRUD 交互、验证、问答渲染)
│   ├── js/metrics.js           # 可视化监控逻辑 (Chart.js 图表渲染 + 自动刷新)
│   └── css/style.css           # 样式 (深色主题、纵向卡片布局、限宽居中、图表样式)
├── tests/
│   ├── conftest.py             # pytest 配置 + mock 客户端 (LLM/ES/KG/Rerank/Embedding/Vec) + 多配置结构 fixture
│   ├── test_api.py             # /api/chat 各模式 + /api/config CRUD + /api/metrics 测试
│   └── test_config.py          # 配置管理测试 (加解密/脱敏/CRUD/明文加载/合并/切换/删除回退)
├── data/
│   ├── config.json             # 本地配置文件 (多配置结构模板)
│   └── synonyms.json           # 同义词词典 (ES 检索双向扩展)
├── .codebuddy/skills/          # 项目级 Skills (rag-orchestrator, fastapi-multi-config, sse-llm-streaming, metrics-dashboard)
├── requirements.txt
├── client_demo.py              # 客户端演示脚本 (8 种参数模式端到端打印)
├── README.md
├── RAG.md                      # RAG 检索逻辑剖析文档
├── 配置手册.md                   # ES 索引配置手册 (分词器/停用词/同义词)
└── spec.md                     # 本文档
```

## 6. API 接口规范 (API Specification)

### 6.1 智能问答 `POST /api/chat`

**请求体**（Pydantic `ChatRequest`）：

| 字段 | 类型 | 必填 | 默认 | 约束 | 说明 |
|---|---|---|---|---|---|
| `query` | string | 是 | — | min_length=1 | 用户问题 |
| `mode` | string | 否 | null | enum: article/qna/kg/vec/all/merge | 检索模式，缺省为纯对话 |
| `top_k` | int | 否 | 5 | ge=1, le=50 | 每个来源最多返回条数 |
| `stream` | bool | 否 | true | — | 是否 SSE 流式 |
| `history` | array | 否 | null | — | 历史对话 `[{role: "user"/"assistant", content: string}]` |

**流式响应（SSE，`media_type=text/event-stream`）**

响应头：
```
Cache-Control: no-cache
X-Accel-Buffering: no
Connection: keep-alive
```

事件序列：

| 事件 | data 结构 | 时机 | 说明 |
|---|---|---|---|
| `meta` | `{"mode": "all", "entities": ["实体A"], "sources": [SourceItem...]}` | 检索完成后、token 前 | 推送检索元信息，前端可先渲染来源 |
| `token` | `{"content": "这是"}` | 多次 | 模型逐 token 输出 |
| `done` | `{}` | 最后 | 正常结束标志 |
| `error` | `{"error": {"code": "llm_error", "message": "..."}}` | 异常时 | 鉴权失败/检索失败等，不中断连接 |

SSE 格式：`event: {event}\ndata: {json}\n\n`

**非流式响应（JSON，`ChatResult`）**
```json
{
  "answer": "这是一段回答...",
  "sources": [
    {
      "source": "article",
      "title": "文章标题",
      "content": "文章内容...",
      "score": 1.2345,
      "meta": {"_id": "abc", "index": "article"}
    }
  ],
  "entities": ["实体A", "实体B"],
  "mode": "all"
}
```

**SourceItem 结构**：
| 字段 | 类型 | 说明 |
|---|---|---|
| source | string | 来源类型：article / qna / kg / vec |
| title | string/null | 标题或实体名（vec 为 null） |
| content | string | 命中内容片段 |
| score | float/null | 相关度分数（ES: _score；KG: 无；vec: distance；merge: rerank 分数） |
| meta | dict/null | 附加元数据（ES: {_id, index}；KG: {labels, relations}；vec: {collection, id}） |

### 6.2 健康检查 `GET /api/health`
返回 `{"status": "ok", "version": "0.1.0", "services": {}}`。

### 6.3 配置管理接口

| 方法 | 路径 | 请求体 | 响应 | 说明 |
|---|---|---|---|---|
| GET | `/api/config` | — | 全部配置（脱敏） | 读取所有段 |
| POST | `/api/config/{section}/items` | 配置字段 | 该段脱敏快照 | 新增配置项；id 由 name 生成 slug（`[^a-zA-Z0-9_-]+`→`-`，小写），冲突追加 `-2`/`-3` |
| PUT | `/api/config/{section}/items/{item_id}` | 配置字段 | 该段脱敏快照 | 修改配置项；敏感字段空/脱敏时保留原值 |
| DELETE | `/api/config/{section}/items/{item_id}` | — | 该段脱敏快照 | 删除配置项；删除生效项自动回退到剩余首项 |
| PUT | `/api/config/{section}/active` | `{"id": "xxx"}` | 该段脱敏快照 | 切换生效配置项 |
| POST | `/api/config/{section}/validate` | `{"id": "xxx"}` 或内联字段 | `{"ok": bool, "message": str}` | 验证可用性；含 id 验证已存储项（用真实凭据），含内联字段验证草稿 |
| PUT | `/api/config/server` | `{"host?": str, "port?": int}` | server 段脱敏快照 | 更新 server 段 |

其中 `{section}` ∈ {`llm`, `elasticsearch`, `neo4j`, `rerank`, `embedding`, `milvus`}。未知 section 返回 404。

**各段验证逻辑**：
| 段 | 验证方式 | 成功消息示例 |
|---|---|---|
| llm | 发起 `chat.completions.create`（model, messages=[{role:user, content:"ping"}], max_tokens=8, temperature=0, stream=false） | `模型可用: deepseek-v4-flash @ https://...` |
| elasticsearch | `client.info()` 获取集群版本与名称 | `连接成功: ES 8.12.0 my-cluster` |
| neo4j | `driver.verify_connectivity()` | `连接成功: bolt://localhost:7687` |
| rerank | 发起最小 rerank 请求（query="ping", documents=["test"], top_n=1）；未配置时返回提示 | `rerank 可用: rerank-multilingual-v3.0 @ https://...` |
| embedding | `embed("ping")` 向量化，返回维度 | `embedding 可用: text-embedding-3-small (dim=1536)` |
| milvus | `list_collections()` 获取 collection 数 | `连接成功: http://localhost:19530 (collections: 3)` |

验证后自动 `close()` 释放连接。返回 `{"ok": bool, "message": string}`。

### 6.4 指标查询 `GET /api/metrics`

**查询参数**：`range_hours`（int，默认 1），指定统计时间范围（小时）。

**响应**
```json
{
  "summary": {
    "total": 100,
    "errors": 2,
    "error_rate": 0.02,
    "total_tokens": 15200,
    "categories": {
      "chat":      {"count": 20, "errors": 0, "error_rate": 0.0, "avg_ms": 350.5, "tokens": 0},
      "llm":       {"count": 50, "errors": 0, "error_rate": 0.0, "avg_ms": 320.5, "tokens": 12000},
      "embedding": {"count": 5,  "errors": 1, "error_rate": 0.2, "avg_ms": 60.0,  "tokens": 3200},
      "rerank":    {"count": 3,  "errors": 0, "error_rate": 0.0, "avg_ms": 200.0, "tokens": 0},
      "es":        {"count": 30, "errors": 1, "error_rate": 0.0333, "avg_ms": 12.3, "tokens": 0},
      "kg":        {"count": 10, "errors": 0, "error_rate": 0.0, "avg_ms": 45.0,  "tokens": 0},
      "milvus":    {"count": 5,  "errors": 0, "error_rate": 0.0, "avg_ms": 80.0,  "tokens": 0}
    },
    "latency": {"avg_ms": 350.0, "p50_ms": 300.0, "p95_ms": 800.0, "p99_ms": 1200.0},
    "start": 1719436800.0,
    "end": 1719523200.0
  },
  "timeseries": {
    "labels": ["00:00", "00:24", ...],
    "counts": [5, 3, ...],
    "errors": [0, 1, ...],
    "latencies": [320.5, 280.0, ...],
    "bucket_size_s": 1440.0
  }
}
```

**latency 分位数**：仅统计 `category=="chat"` 的事件（即整体请求），使用线性插值法计算 P50/P95/P99。

### 6.5 自动文档
FastAPI 自动生成 Swagger UI：`GET /docs`；ReDoc：`GET /redoc`。UI 顶栏 QnA logo 链接到 `/docs`。

## 7. 检索策略说明 (Retrieval Strategies)

### 7.1 模式总览

| 模式 | 检索来源 | 抽取实体 | 向量化 | 并发 | 结果处理 | 说明 |
|---|---|---|---|---|---|---|
| （无） | 不检索 | 否 | 否 | — | — | 纯 LLM 对话，使用 `CHAT_SYSTEM_PROMPT` |
| article | ES `article` 索引 | 否 | 否 | — | 直接拼接 | BM25 关键字检索 |
| qna | ES `qna` 索引 | 否 | 否 | — | 直接拼接 | BM25 关键字检索 |
| kg | Neo4j 图谱 | 是 | 否 | — | 直接拼接 | 先抽实体再检索节点及邻居关系 |
| vec | Milvus 向量库 | 否 | 是 | — | 直接拼接 | 多路检索（抽主题→各自向量化→合并去重），降级单路 |
| all | article + qna + kg | 是 | 否 | 三路并行 | 直接拼接 | **不含 vec**，保持轻量 |
| merge | article + qna + kg + vec | 是 | 是 | 四路并行 | rerank 融合取 top_n | 四路候选统一打分 |

### 7.2 关键设计决策
* `all` 不含 vec：保持轻量，避免额外 embedding 开销；三路结果直接拼接（by design，不做融合）。
* `merge` 含 vec：四路候选经 rerank 统一打分，融合后仅保留高分项，提升答案精度。
* 检索结果统一为 `SourceItem` 列表（`source` ∈ article/qna/kg/vec），按 `[n]` 编号组装为上下文。

### 7.3 两阶段并发调度（all / merge）

**依赖关系**：ES 检索、vec 检索与实体抽取互相独立，可立即并行；KG 检索依赖实体抽取结果，须等实体就绪后发起。

```
阶段1（立即并行 create_task）：
  ├─ 实体抽取（LLM，extract_entities）
  ├─ ES article 检索（search_article）
  ├─ ES qna 检索（search_qna）
  └─ Milvus vec 检索（vec.search，仅 merge）

阶段2（await entity_task 后）：
  └─ KG 图谱检索（search_by_entities，基于实体）

阶段3（asyncio.gather 汇总）：
  gather(article_task, qna_task, kg_task, [vec_task], return_exceptions=True)
  → 逐路 _unwrap → 合并 sources
```

**容错**：`gather(return_exceptions=True)` 使单路异常返回 Exception 对象而非抛出；`_unwrap(result)` 将 Exception 降级为空列表。效果：如 ES 不可用时 article/qna 贡献 0 条，但 KG/vec 仍可贡献来源，整体不中断。

### 7.4 ES 检索细节

**同义词扩展（查询前置）**：
ES 检索前，服务代码（`es_client.py` 的 `_expand_query`）加载外部词典 `data/synonyms.json`，对 query 做双向同义词扩展——词典中每个词映射到其所在同义词组的全部词，query 中命中任一词则扩展出全组同义词，以 `OR` 组合查询。原始 query 始终保留。此扩展在服务代码侧完成，不依赖 ES 内置 synonym filter。

**索引字段映射**（从配置 `article_fields`/`qna_fields` 读取，未配置用默认值）：
| source_type | body（检索字段，默认） | title（展示字段，默认） |
|---|---|---|
| article | content | title |
| qna | answer | question |

**查询体**（query 为扩展后的查询字符串，fields 从配置映射读取）：
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
* `best_fields`：取匹配得分最高的字段作为文档得分。
* `content^3`：正文字段加权 3 倍，正文命中比标题命中贡献更高分值。

**结果解析**：每条命中构造 `SourceItem(source, title←_source.title/question, content←_source.content/answer, score←_score, meta←{_id, index})`。

### 7.5 KG 检索细节

**连接**：`session(database=self.database)`，database 从配置读取（默认 `neo4j`）。

**Cypher 查询模板**（由 `_build_query(node_key, max_neighbors)` 动态构建，支持可配置节点主键、邻居数量限制、关系类型过滤）：
```cypher
MATCH (n)
WHERE toLower(n.{node_key}) CONTAINS toLower($entity)
OPTIONAL MATCH (n)-[r]-(m)
WHERE NOT type(r) IN $excluded AND coalesce(m.name, '') <> ''
WITH n, collect(distinct [type(r), coalesce(m.name, '')]) AS all_rels
WITH n, all_rels[..{max_neighbors}] AS rels
RETURN n.{node_key} AS name, labels(n) AS labels, rels
LIMIT $limit
```
逐句：遍历节点 → `node_key` 属性（默认 `name`，可配置）大小写不敏感包含匹配 → 可选匹配邻居关系 → **过滤排除的关系类型**（`$excluded`）+ **去除空目标名** → 聚合去重收集 `[关系类型, 目标名]` → **限制邻居数量**（`max_neighbors`，默认 10） → 返回节点名/标签/关系集 → 限数。

**可配置项**：`database`（数据库名）、`node_key`（节点主键属性）、`max_neighbors`（最大邻居数）、`excluded_relations`（排除的关系类型列表，默认 `["contains","references"]`）。

**多实体配额分配**：`per_entity = max(1, top_k // max(1, len(entities)))`，每个实体独立查询，累计达 top_k 提前返回。

**结果格式化** `_format_record`：
```
[Person/Company] DeepSeek
  -(develops)-> DeepSeek-V3
  -(competes_with)-> GPT-4
```

### 7.6 vec 检索细节

**多路检索（默认）**：解决多主题 query 整体向量化漏召回问题。
1. `llm.extract_entities(query)` → 抽取主题/关键概念列表
2. 构建检索 queries：原始 query + 各主题，去重保序
3. 每路配额：`per_query = max(1, top_k // len(queries))`
4. 各路并行：`embedding_client.embed(q)` → `MilvusClient.search(data=[vec], limit=per_query, ...)`（`asyncio.gather`）
5. 结果按 content 前 200 字符去重，合并取 top_k

**单路检索（降级）**：LLM 不可用或主题拆分失败时。
1. `embedding_client.embed(query)` → 查询向量
2. `MilvusClient.search(collection_name, data=[query_vec], limit=top_k, output_fields=[text_field], search_params={"metric_type": "COSINE"})`
3. 解析 `results[0]`，每条命中构造 `SourceItem(source="vec", content←entity.text_field, score←distance, meta←{collection, id})`

### 7.7 上下文组装

`_build_context(sources)` 将 SourceItem 列表编号拼接：
```
[1] (来源:article | 标题:DeepSeek 技术报告)
DeepSeek-V3 采用 MoE 架构...

[2] (来源:kg | 标题:DeepSeek)
[Company] DeepSeek
  -(develops)-> DeepSeek-V3

[3] (来源:vec | 标题:vec)
向量检索命中的语义相似文段...
```
块间空行分隔。序号对应 LLM 提示词中 `[n]` 引用标注要求。无来源时返回空字符串（触发纯对话提示词）。

## 8. LLM 提示词策略 (Prompt Strategy)

### 8.1 三套提示词

**CHAT_SYSTEM_PROMPT**（纯对话，无 context）：
```
你是一个智能问答助手，请友好、准确地回答用户的问题。
```
行为：允许模型自由作答。

**RAG_SYSTEM_PROMPT**（RAG，有 context）：
```
你是一个严谨的智能问答助手。请根据下方【参考资料】回答用户问题。
要求：
1. 仅依据参考资料作答，不要编造；若资料不足，请明确说明。
2. 在关键信息后用 [n] 标注引用来源编号。
3. 回答简洁、条理清晰。
```
使用时拼接为：`{RAG_SYSTEM_PROMPT}\n\n【参考资料】\n{context}`

**ENTITY_EXTRACTION_PROMPT**（实体抽取）：
```
你是一个实体抽取助手。从用户问题中提取用于知识图谱检索的核心实体（如人名、组织、产品、技术、地点、概念等）。
只返回 JSON，不要任何额外说明，格式为：
{"entities": ["实体1", "实体2"]}
如果没有明显实体，返回 {"entities": []}。
```

### 8.2 提示词选择逻辑

`_build_messages(query, context, history)`：
* 有 context（检索模式，含 merge）→ system 用 `RAG_SYSTEM_PROMPT` + 参考资料
* 无 context（纯对话）→ system 用 `CHAT_SYSTEM_PROMPT`
* history（最近 10 条）追加为 user/assistant 消息
* query 追加为最后一条 user 消息

### 8.3 实体抽取容错解析

`_parse_entities(text)`：
1. `text.strip()` → `json.loads` → 取 `entities`
2. 失败则 `re.search(r"\{.*\}", text, re.DOTALL)` 抽取首个 JSON 对象 → `json.loads`
3. 仍失败返回空列表

### 8.4 LLM 调用参数

| 操作 | model | temperature | max_tokens | stream |
|---|---|---|---|---|
| stream_chat（对话） | `self.model` | `self.temperature`（默认 0.7） | `self.max_tokens`（默认 2048） | true |
| extract_entities（实体抽取） | `self.entity_model` | 0.0 | 256 | false |
| validate（验证） | `self.model` | 0 | 8 | false |

### 8.5 请求日志

`_log_request(tag, body)` 在每次 LLM 调用前打印：
```
========== [LLM 请求] stream_chat ==========
base_url : https://...
api_key  : sk-tp-hH... (len=54)
POST body:
{完整 JSON}
===============================================
```
api_key 仅显示前 8 位 + `...`。

## 9. 融合检索 (merge 模式)

### 9.1 流程
四路检索结果（最多 top_k × 4 条）→ `RerankClient.rerank(query, sources, top_k)` → top_n 条高分项 → 组装上下文 → LLM。

### 9.2 Rerank API（Cohere/Jina 兼容）

触发条件：rerank 完整配置（`api_base` + `api_key` + `model` 三者均非空，`configured` 属性为 true）。

**请求**：
```
POST {api_base}/rerank
Authorization: Bearer {api_key}
Content-Type: application/json

{
  "model": "{model}",
  "query": "用户问题",
  "documents": ["doc1内容", "doc2内容", ...],
  "top_n": min(top_n, len(documents))
}
```

**响应**：
```json
{
  "results": [
    {"index": 2, "relevance_score": 0.98},
    {"index": 0, "relevance_score": 0.85}
  ]
}
```

按 `relevance_score` 排序，通过 `index` 映射回 SourceItem，用 `model_copy()` 复制后更新 `score` 字段。超时 30 秒。

### 9.3 RRF 兜底（无模型融合）

触发条件：rerank 未配置（`configured` 为 false）或 API 调用失败（`except Exception`）。

算法（`_rrf_fallback`）：
1. 按 `source` 分组（article/qna/kg/vec）
2. ES 路组内按 `score` 降序排序；KG/vec 路保持原序
3. 每条结果按其在所属路内的排名计算 RRF 分数：`rrf = 1.0 / (60 + rank)`（rank 从 0 开始）
4. 跨路统一按 RRF 分数降序排序
5. 取前 `top_n` 条

常数 60 是业界经验值，平衡头部（rank=0 得分 1/60≈0.0167）与长尾。

**效果**：三路结果均匀交错（每路 rank=0 的项优先），避免单路霸占上下文。

### 9.4 top_n 与 top_k 的关系
* `top_k`（请求参数）：每路检索召回量（如 5 → 四路最多 20 条候选）。
* `top_n`（rerank 配置 `top_n` 字段）：融合后保留量（如 5 → 最终仅 5 条进 LLM 上下文）。
* `top_n` 为 0 时回退到请求的 `top_k`。

### 9.5 Rerank 配置管理
rerank 作为独立配置段（`rerank`），与 llm/es/neo4j/embedding/milvus 同样支持多份配置（items + active）、增删改查、切换、验证。未配置时 `configured` 为 false，merge 模式自动 RRF 兜底，不影响使用。

## 10. 管理控制台 UI (Admin UI)

纯静态前端，由 FastAPI 挂载 `/` 提供（`StaticFiles(directory=ui, html=True)`）。深色主题（CSS 变量 `--bg: #0f172a` 等）。三个 tab。

### 10.1 问答测试页

**布局**：左侧对话区 + 右侧参数栏（300px 宽）。

**对话区**：
* 消息列表（user 蓝色右对齐 / assistant 深色左对齐 / system 居中灰字 / error 红色居中）
* 输入框（textarea，Enter 发送、Shift+Enter 换行）
* 发送 / 清空按钮

**参数栏**：
* 模式选择 `<select>`：无（纯对话）/ article / qna / kg / vec / all / merge
* Top K `<input type="number">`（默认 5）
* 流式开关 `<input type="checkbox">`（默认开）
* 「本次来源」区域：流式时由 `meta` 事件渲染，非流式时由响应体渲染；显示 `[n] 来源 · 标题 (score)`
* 「调用接口」区域：显示**本服务被调用的接口** `POST /api/chat` 及本次请求参数 JSON（`<pre>` 格式化）

**流式渲染**：fetch + `ReadableStream` reader + `TextDecoder`；按 `\n\n` 分割 SSE 块；`event:` / `data:` 行解析；token 逐字追加到 assistant 消息 DOM，自动滚动。

### 10.2 配置管理页

**布局**：六个工具模块（LLM / Embedding / Elasticsearch / Neo4j / Milvus / Rerank）+ Server，**自上而下纵向排列**，卡片间 `<hr>` 水平分隔线隔开；内容区 `.config-inner` 限宽 820px 居中，两侧留白；滚动条 `.config-scroll` 位于页面最右（全宽）。

**每个工具模块**：
* 卡片头部：标题 + 「+ 新增配置」按钮
* 配置项列表：每项一行（`.cfg-item`），生效项绿色边框 + 「生效中」badge
* 每项按钮：设为生效（success）/ 验证 / 编辑 / 删除（danger）；验证结果内联显示（✓ 可用 / ✗ 原因）

**编辑表单**（`.cfg-editor`，展开式）：
* 各配置属性**自上而下纵向单列排列**（`flex-direction: column`，非横向 grid）
* 敏感字段 `type=password`，placeholder「留空则不修改」
* 操作按钮：保存 / 验证 / 取消 + 内联消息（✓/✗）

**交互逻辑**：
* **设为生效**：先调 `POST /api/config/{section}/validate {id}` 验证，通过才调 `PUT /api/config/{section}/active {id}`；未通过则不切换并显示原因。
* **修改保存**：先验证（编辑已存在项时用 id 验证真实凭据，敏感字段未改则用 id 验证；新值则内联验证），通过才调 `PUT`/`POST` 保存。
* **敏感字段处理**：编辑时显示脱敏值（含 `*`）；保存时若值含 `*` 或为空则不提交该字段（后端保留原值）。
* 保存/删除/切换后自动 `loadConfig()` 刷新全部。

**Server 段**：Host + Port 输入框 + 保存按钮。

### 10.3 可视化监控页

**控制栏**：时间范围 `<select>`（1h / 6h / 24h / 7d）+ 刷新按钮 + 自动刷新开关（30s `setInterval`）。

**汇总卡片**（6 个 `.m-card`）：总请求 / 错误数(含错误率%) / 平均延迟 / P95 / P99 / Token 消耗。

**图表**（Chart.js 4.x via CDN，4 个 `.chart-box` 2×2 grid）：
| 图表 | 类型 | 数据 |
|---|---|---|
| 请求量趋势 | line | 请求数 + 错误数（双数据集，fill） |
| 延迟趋势 | line | 平均延迟（实时）+ P95/P99 参考线（`borderDash:[5,5]`，`pointRadius:0`） |
| 各服务调用次数 | bar | LLM / Embedding / Rerank / ES / KG / Milvus（按类别颜色） |
| Token 消耗 | bar | 同上类别 |

图表配置：`responsive: true, maintainAspectRatio: false`，深色主题坐标轴（`ticks.color: #64748b`，`grid.color: #1e293b`）。

切换到 metrics tab 时自动加载 + 启动自动刷新；切出时停止。

### 10.4 顶栏

* 左上角紫色 `QnA` logo 为 `<a>` 链接，`target="_blank"` 打开 `/docs`（Swagger UI），hover 颜色加深。
* 中间三个 tab 按钮（问答测试 / 配置管理 / 可视化监控）。
* 右上角健康状态指示灯（`.dot` 绿/红/灰）+ 文字，每 30 秒 `fetch("/api/health")` 刷新。

## 11. 指标采集 (Metrics)

### 11.1 数据结构

```python
@dataclass
class MetricEvent:
    timestamp: float      # epoch seconds (time.time())
    category: str         # chat / llm / embedding / rerank / es / kg / milvus
    operation: str        # 具体操作名
    duration_ms: float    # 耗时毫秒 (time.monotonic 差值 * 1000)
    success: bool = True
    tokens: int = 0       # LLM/Embedding 记录 usage.total_tokens
    error: str = ""
```

### 11.2 采集方式

`async with measure(category, operation) as m`：
1. 进入时 `start = time.monotonic()`，`info = {"tokens":0, "success":True, "error":""}`
2. yield info 给业务代码，业务可设置 `m["tokens"] = resp.usage.total_tokens`
3. 异常时 `info["success"]=False, info["error"]=str(exc)`，re-raise
4. finally 中 `get_metrics().record(category, operation, (time.monotonic()-start)*1000, info["success"], info["tokens"], info["error"])`

### 11.3 埋点位置

| 类别 | 操作 | 位置 | 记录 token |
|---|---|---|---|
| chat | request | orchestrator.run / run_stream | 否（由 LLM 内部记录） |
| llm | stream_chat | llm_client.stream_chat | 否（流式无 usage） |
| llm | extract_entities | llm_client.extract_entities | 是 |
| embedding | embed | embedding_client.embed | 是 |
| rerank | rerank | rerank_client.rerank（仅 API 路径） | 否 |
| es | search_article / search_qna | es_client._search | 否 |
| kg | search_by_entities | kg_client.search_by_entities | 否 |
| milvus | search | vec_client.search（检索部分） | 否 |

### 11.4 存储

内存环形缓冲区 `deque(maxlen=50000)`，超出自动丢弃旧事件。线程安全（`threading.Lock`）。不持久化，重启清空。

### 11.5 聚合算法

**`summary(start, end)`**：
1. 过滤时间范围内事件
2. 按 `category` 分组，每组计算 count / errors / error_rate / avg_ms / tokens
3. 提取 `category=="chat"` 的事件耗时，排序后计算分位数

**分位数计算**（线性插值 `_percentile`）：
```python
k = (len(sorted_values) - 1) * p / 100
f = int(k)
c = min(f + 1, len(sorted_values) - 1)
if f == c: return sorted_values[f]
return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)
```

**`timeseries(start, end, buckets=60)`**：
1. `bucket_size = max(1, (end - start) / buckets)`
2. 每桶统计 `[b_start, b_end)` 内事件的 count / errors / avg latency
3. label 为 `time.strftime("%H:%M", localtime(b_start))`

## 12. 异常处理 (Error Handling)

### 12.1 异常体系

`app/core/exceptions.py`，`AppException` 基类携带 `status_code` / `code` / `message` / `detail`，`to_dict()` 返回 `{"error":{"code","message","detail"}}`。

| 异常 | 状态码 | code | 继承 | 说明 |
|---|---|---|---|---|
| `AppException` | 500 | internal_error | Exception | 基类 |
| `ConfigError` | 500 | config_error | AppException | 配置错误 |
| `ConfigNotFoundError` | 404 | config_not_found | ConfigError | 配置/配置项不存在 |
| `LLMError` | 502 | llm_error | AppException | 大模型/Embedding 调用失败 |
| `RetrievalError` | 502 | retrieval_error | AppException | 检索失败（依赖缺失等） |
| `ValidationError` | 422 | validation_error | AppException | 校验失败 |
| `ESConnectionError` | 502 | es_connection_error | RetrievalError | ES 连接/检索失败 |
| `KGConnectionError` | 502 | kg_connection_error | RetrievalError | Neo4j 连接/检索失败 |

### 12.2 异常处理流程

* **非流式**：路由 `try/except AppException` → `_to_http(exc)` → `HTTPException(status_code, detail=exc.to_dict()["error"])`
* **流式 SSE**：`_chat_stream` 内 `try/except`，异常时 yield `event: error\ndata: {"error":{...}}\n\n`
* **全局处理器**：`@app.exception_handler(AppException)` → `JSONResponse(status_code, content=exc.to_dict())`
* **Pydantic 校验失败**：FastAPI 自动返回 422
* **未知 section**：路由返回 `HTTPException(404)`

## 13. 依赖注入与生命周期 (Dependencies & Lifecycle)

### 13.1 依赖注入

`app/api/dependencies.py`：

| 函数 | 返回 | 配置来源 |
|---|---|---|
| `get_config_manager()` | ConfigManager 单例 | `data/config.json` |
| `get_llm_client()` | LLMClient | `get_active_item("llm")` |
| `get_es_client()` | ESClient | `get_active_item("elasticsearch")` |
| `get_kg_client()` | KGClient | `get_active_item("neo4j")` |
| `get_rerank_client()` | RerankClient | `get_active_item("rerank")`（失败用空配置→RRF 兜底） |
| `get_embedding_client()` | EmbeddingClient | `get_active_item("embedding")`（失败用空配置） |
| `get_vec_client()` | VecClient | `get_active_item("milvus")` + `get_embedding_client()` |
| `get_orchestrator()` | Orchestrator | 注入上述全部客户端 |

所有 getter 用 `@lru_cache(maxsize=1)` 单例化。`get_config_manager()` 用全局变量单例。

### 13.2 配置重建

`rebuild_clients()`：遍历所有 getter 调用 `cache_clear()`（对未装饰的函数如测试 mock 防御性 `getattr` 检查），然后 `get_orchestrator()` 触发重建。配置 CRUD 接口操作后自动调用。

### 13.3 生命周期 (lifespan)

`app/main.py` 的 `@asynccontextmanager lifespan`：
* **启动**：预构造 `get_config_manager()` + `get_orchestrator()`（配置缺失不崩溃，`except AppException: pass`，允许通过 UI 修正）。
* **关闭**：`await get_es_client().close()` / `get_kg_client().close()`（异常静默）。

### 13.4 路由依赖访问

路由层通过模块属性访问（`deps.get_xxx()`），而非直接 import 函数名，便于测试 monkeypatch 替换。

## 14. 测试策略 (Testing)

### 14.1 离线运行

测试通过 mock 客户端注入确定性返回，不依赖真实 ES / Neo4j / LLM / Milvus / Rerank 服务。

### 14.2 conftest.py

* **`tmp_config_file` fixture**：生成多配置结构临时 JSON（含全部 7 段），敏感字段用 `b64:` 前缀编码。
* **`config_manager` fixture**：基于临时文件的 ConfigManager。
* **Mock 客户端**：`MockLLMClient` / `MockESClient` / `MockKGClient` / `MockRerankClient` / `MockEmbeddingClient` / `MockVecClient`，均含 `validate()` 返回 `{ok:True}`。
  * MockLLM：`stream_chat` 产出 4 个 token，`chat` 返回固定字符串，`extract_entities` 返回 `["实体A","实体B"]`。
  * MockES：`search_article` / `search_qna` 各返回 1 条 SourceItem。
  * MockKG：`search_by_entities` 每实体返回 1 条。
  * MockRerank：`rerank` 按原序截断 `sources[:top_n]`。
  * MockVec：`search` 返回 1 条 `source="vec"`。
* **`mock_clients` fixture**：monkeypatch 替换 `deps` 模块的 getter 函数 + orchestrator。
* **`client` fixture**：`TestClient(create_app())`，注入 config_manager。

### 14.3 test_api.py（15 个用例）

| 用例 | 覆盖 |
|---|---|
| test_health | 健康检查 |
| test_chat_validation_empty_query | 空 query → 422 |
| test_chat_no_mode_blocking | 无 mode 纯对话 |
| test_chat_article_blocking | article 模式 |
| test_chat_qna_blocking | qna 模式 |
| test_chat_kg_blocking | kg 模式（含实体） |
| test_chat_all_blocking | all 三路并行 |
| test_chat_merge_blocking | merge 四路 + rerank 截断 |
| test_chat_vec_blocking | vec 向量检索 |
| test_chat_invalid_mode | 非法 mode → 422 |
| test_chat_stream_sse | 流式 SSE（meta/token/done 事件） |
| test_config_get_masked | 配置脱敏 |
| test_config_create_item | 新增配置 |
| test_config_update_item_preserves_sensitive | 修改保留敏感字段 |
| test_config_set_active | 切换生效 |
| test_config_delete_item | 删除配置 |
| test_config_unknown_section | 未知段 → 404 |
| test_metrics_endpoint | 指标接口（先 chat 产生指标） |

### 14.4 test_config.py（11 个用例）

| 用例 | 覆盖 |
|---|---|
| test_load_decodes_sensitive | 加载解码敏感字段 |
| test_masked_hides_secret | 脱敏 |
| test_save_encodes_sensitive | 保存编码 |
| test_update_preserves_sensitive_when_masked | 脱敏占位时保留原值 |
| test_set_active | 切换生效 |
| test_delete_item_reassigns_active | 删除生效项后回退 |
| test_create_item_id_collision | 重复 id 报错 |
| test_plaintext_sensitive_loaded_as_is | 明文（无 b64: 前缀）原样加载 |
| test_resolve_item_merges | resolve 合并 |
| test_get_item_not_found | 配置项不存在 → ConfigNotFoundError |
| test_missing_config_raises | 文件不存在 → ConfigNotFoundError |

共 29 个测试用例，全部通过。

## 15. 部署 (Deployment)

### 15.1 开发
```bash
pip install -r requirements.txt
python -m app.main
# 或
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 15.2 生产
```bash
uvicorn app.main:app --workers N
```
建议配合 Nginx 反向代理，SSE 需 `proxy_buffering off;`：
```nginx
location /api/chat {
    proxy_pass http://backend;
    proxy_buffering off;
    proxy_cache off;
    proxy_set_header Connection '';
    proxy_http_version 1.1;
    chunked_transfer_encoding off;
}
```

### 15.3 环境变量
* `HOST`：监听地址（默认 0.0.0.0）
* `PORT`：监听端口（默认 8000）

### 15.4 懒连接
ES / Neo4j / Milvus 客户端为懒连接（首次访问 `client` / `driver` 属性时创建），未配置时启动不报错；`mode` 留空即可纯 LLM 对话。

### 15.5 多 Worker 注意事项
指标数据存内存（`deque`），多 worker 部署时各 worker 独立统计。如需聚合需引入外部存储（如 Redis / Prometheus）。

## 16. 客户端演示脚本 (client_demo.py)

`client_demo.py` 依次调用 `/api/chat` 的 8 种参数模式，端到端打印每一步执行情况：

| # | case | mode | stream |
|---|---|---|---|
| 1 | 纯对话 | 无 | 否 |
| 2 | article 文章检索 | article | 否 |
| 3 | qna 问答检索 | qna | 否 |
| 4 | kg 知识图谱检索 | kg | 否 |
| 5 | vec 向量检索 | vec | 否 |
| 6 | all 三路并行 | all | 否 |
| 7 | merge 四路融合 | merge | 否 |
| 8 | 流式 SSE | all | 是 |

用法：
```bash
python client_demo.py                          # 默认 localhost:8000
python client_demo.py --base-url http://host:8000
python client_demo.py --skip kg vec            # 跳过指定 case
```

每个 case 打印：请求参数 → HTTP 状态/耗时 → mode/entities → 检索来源（含 score）→ 模型回答 → 端到端耗时。流式 case 额外打印：meta 事件耗时、首 token 延迟、token 逐字实时输出、token 总数、总耗时。

## 17. 项目级 Skills (.codebuddy/skills/)

项目提取了 4 个可复用 skill，位于 `.codebuddy/skills/`：

| Skill | 说明 |
|---|---|
| `rag-orchestrator` | RAG 多源检索编排（多路并行、两阶段并发、rerank 融合、RRF 兜底、容错降级） |
| `fastapi-multi-config` | FastAPI 多配置管理（items+active、b64: 前缀加解密、CRUD、验证切换、脱敏） |
| `sse-llm-streaming` | SSE 流式 LLM 接口（事件序列设计、后端 StreamingResponse、前端 ReadableStream 解析） |
| `metrics-dashboard` | 内存指标采集与可视化（measure 埋点、P50/P95/P99 聚合、Chart.js 图表） |

每个 skill 含 `SKILL.md`（YAML frontmatter + 触发词 + 核心设计 + 实现清单）和 `references/` 详细参考文档。
