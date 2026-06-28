# Smart QnA —— 智能问答后端服务

基于 **FastAPI** 构建的 RAG（检索增强生成）智能问答 API 服务，组合 **Elasticsearch**（关键字检索）、**Neo4j**（知识图谱检索）、**Milvus**（向量检索），通过大语言模型（LLM）流式生成结构化答案。支持多路检索融合（rerank）、多配置管理、可视化监控，附带轻量级 B/S 管理控制台。

## 1. 功能特性

* **七种检索模式**：`article` / `qna` / `kg` / `vec` / `all` / `merge` / 无参数（纯对话）。
* **SSE 流式输出**：基于 Server-Sent Events 实时推送模型 token，事件序列 `meta→token→done`。
* **多路并行检索**：`all`/`merge` 模式下 ES + Neo4j + Milvus 通过 `asyncio` 两阶段并发，降低延迟。
* **实体抽取**：`kg`/`all`/`merge` 模式先调用 LLM 抽取查询实体（temperature=0，JSON 输出），再检索图谱。
* **向量检索**：`vec` 模式将 query 经 embedding 向量化后检索 Milvus，召回语义相似文段。
* **同义词扩展**：ES 检索前自动加载外部同义词词典（`data/synonyms.json`），对 query 做双向扩展，解决字面不匹配问题。
* **融合检索**：`merge` 模式四路检索后经 rerank 模型（Cohere/Jina 兼容）融合打分取 top_n；rerank 未配置时自动降级为 RRF。
* **多配置管理**：每个工具（LLM/Embedding/ES/Neo4j/Milvus/Rerank）支持多份命名配置，在线增删改查、切换生效、可用性验证。
* **敏感字段保护**：`api_key`/`password` 以 `b64:` 前缀 + Base64 落盘，展示自动脱敏，明文填写亦可识别。
* **双系统提示词**：纯对话用通用提示词自由作答，RAG 用严格提示词依据资料作答并标注引用 `[n]`。
* **可视化监控**：非侵入式指标埋点，内存聚合 P50/P95/P99/错误率/token 消耗，Chart.js 图表展示。
* **轻量控制台**：纯静态 HTML/JS/CSS，零构建，由 FastAPI 直接挂载提供。

## 2. 技术栈

| 模块 | 技术 |
|---|---|
| 核心框架 | FastAPI + Uvicorn |
| 大模型 | openai SDK（AsyncOpenAI，兼容 OpenAI 格式） |
| 关键字检索 | elasticsearch (async client) |
| 图谱检索 | neo4j (async driver) |
| 向量检索 | pymilvus (MilvusClient) |
| Rerank | httpx (Cohere/Jina 兼容 API) |
| 数据建模 | Pydantic v2 |
| 图表 | Chart.js 4.x (CDN) |
| 测试 | pytest + pytest-asyncio + httpx |
| UI | 原生 HTML / JS / CSS |

## 3. 目录结构

```text
smart-QnA/
├── app/
│   ├── main.py                 # FastAPI 入口（挂载 API + UI + lifespan + 全局异常处理）
│   ├── api/
│   │   ├── routes.py           # 路由（/api/chat, /api/health, /api/config/**, /api/metrics）
│   │   └── dependencies.py     # 依赖注入（lru_cache 单例客户端 + rebuild）
│   ├── core/
│   │   ├── config_manager.py   # 多配置读写 + b64: 加解密 + CRUD + 脱敏
│   │   ├── llm_client.py       # LLM 封装（流式/实体抽取/validate/双提示词/日志）
│   │   ├── embedding_client.py # Embedding 向量化 + validate
│   │   ├── metrics.py          # 指标采集（measure 埋点 + summary/timeseries 聚合）
│   │   └── exceptions.py       # 自定义异常体系
│   ├── retrieval/
│   │   ├── es_client.py        # Elasticsearch BM25 检索
│   │   ├── kg_client.py        # Neo4j Cypher 图谱检索
│   │   ├── vec_client.py       # Milvus 向量检索
│   │   ├── rerank_client.py    # Rerank 融合（API + RRF 兜底）
│   │   └── orchestrator.py     # RAG 调度（七模式 + 两阶段并发 + SSE）
│   └── schemas/
│       ├── requests.py         # 输入模型
│       └── responses.py        # 输出模型
├── ui/
│   ├── index.html              # 主界面（问答测试 + 配置管理 + 可视化监控）
│   ├── js/app.js               # 前端逻辑（SSE 解析、配置 CRUD）
│   ├── js/metrics.js           # 可视化监控（Chart.js 图表）
│   └── css/style.css           # 样式（深色主题）
├── tests/
│   ├── conftest.py             # pytest 配置 + mock 客户端
│   ├── test_api.py             # API 各模式 + 配置 CRUD + 指标测试
│   └── test_config.py          # 配置管理测试
├── data/
│   ├── config.json            # 配置文件（多配置结构模板）
│   └── synonyms.json          # 同义词词典（ES 检索双向扩展）
├── .codebuddy/skills/          # 项目级 Skills（4 个可复用技能）
├── client_demo.py              # 客户端演示脚本（8 种模式端到端打印）
├── requirements.txt
├── README.md
├── RAG.md                      # RAG 检索逻辑剖析
└── spec.md                     # 完整技术规格说明
```

## 4. 快速开始

### 4.1 环境要求

* Python ≥ 3.10
* （可选）Elasticsearch 8.x、Neo4j 5.x、Milvus 2.x

### 4.2 安装依赖

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

### 4.3 配置

编辑 `data/config.json`，填入各工具连接信息。每个工具支持多份配置（items + active），敏感字段（api_key/password）可直接填写明文，保存时自动 Base64 编码；也可启动后通过控制台「配置管理」页在线修改。

> 首次使用仅需配置 `llm` 段（api_base + api_key + model）即可启用纯对话模式（mode 留空）。ES/Neo4j/Milvus 为懒连接，未配置不影响启动。

### 4.4 启动服务

```bash
python -m app.main
# 或
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

启动后：

* 管理控制台：http://localhost:8000/
* API 文档：http://localhost:8000/docs

### 4.5 客户端演示

```bash
python client_demo.py                          # 默认 localhost:8000，依次执行 8 种模式
python client_demo.py --base-url http://host:8000
python client_demo.py --skip kg vec            # 跳过指定 case
```

## 5. API 说明

### 5.1 智能问答 `POST /api/chat`

**请求体**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `query` | string | 是 | — | 用户问题 |
| `mode` | string | 否 | null | `article`/`qna`/`kg`/`vec`/`all`/`merge`，缺省纯对话 |
| `top_k` | int | 否 | 5 | 每来源返回条数（1~50） |
| `stream` | bool | 否 | true | 是否 SSE 流式 |
| `history` | array | 否 | null | 历史对话 `[{role, content}]` |

**流式响应（SSE）**

```
event: meta
data: {"mode":"all","entities":["实体A"],"sources":[...]}

event: token
data: {"content":"这是"}

event: done
data: {}
```

**非流式响应（JSON）**

```json
{
  "answer": "这是一段回答。",
  "sources": [{"source":"article","title":"...","content":"...","score":1.2}],
  "entities": ["实体A"],
  "mode": "all"
}
```

### 5.2 健康检查 `GET /api/health`

返回 `{"status":"ok","version":"0.1.0"}`。

### 5.3 配置管理

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/config` | 读取全部配置（脱敏） |
| POST | `/api/config/{section}/items` | 新增配置项 |
| PUT | `/api/config/{section}/items/{id}` | 修改配置项 |
| DELETE | `/api/config/{section}/items/{id}` | 删除配置项 |
| PUT | `/api/config/{section}/active` | 切换生效配置 |
| POST | `/api/config/{section}/validate` | 验证可用性 |
| PUT | `/api/config/server` | 更新 server 段 |

`{section}` ∈ {`llm`, `elasticsearch`, `neo4j`, `rerank`, `embedding`, `milvus`}。

### 5.4 指标查询 `GET /api/metrics?range_hours=24`

返回指定时间范围内的汇总统计（总数/错误率/P50/P95/P99/token/各服务调用次数）与分桶时间序列。

## 6. 检索模式说明

| 模式 | 检索来源 | 实体抽取 | 向量化 | 说明 |
|---|---|---|---|---|
| （无） | 不检索 | 否 | 否 | 纯 LLM 对话 |
| article | ES article 索引 | 否 | 否 | BM25 关键字检索 |
| qna | ES qna 索引 | 否 | 否 | BM25 关键字检索 |
| kg | Neo4j 图谱 | 是 | 否 | 先抽实体再检索节点及邻居 |
| vec | Milvus 向量库 | 否 | 是 | query 向量化后 ANN 检索 |
| all | article+qna+kg 并行 | 是 | 否 | 三路并行（不含 vec），直接拼接 |
| merge | article+qna+kg+vec 并行 | 是 | 是 | 四路并行 + rerank 融合取 top_n |

> `all` 不含 vec（保持轻量）；`merge` 含 vec 并经 rerank 融合。rerank 未配置时自动降级为 RRF（倒数排名融合）。

## 7. 管理控制台

三个 Tab，深色主题：

* **问答测试**：对话沙盒，模式/TopK/流式参数，实时 SSE 渲染，显示检索来源与调用接口参数。
* **配置管理**：六个工具模块纵向排列，每模块支持多配置增删改查/切换/验证；编辑表单纵向排列；切换/修改前验证可用性。
* **可视化监控**：时间范围选择 + 自动刷新；汇总卡片（总请求/错误率/P95/P99/Token）；4 个 Chart.js 图表（请求量/延迟/各服务调用/Token 消耗）。

顶栏 QnA logo 点击打开 `/docs`；右上角健康状态灯每 30 秒刷新。

## 8. 运行测试

测试使用 mock 客户端，无需真实服务，可离线运行：

```bash
pytest -v
```

共 29 个测试用例，覆盖七种检索模式、SSE 流式、配置 CRUD/加解密/脱敏/切换/删除、指标接口等。

## 9. 部署

### 9.1 直接运行

* 开发：`python -m app.main` 或 `uvicorn app.main:app --reload`
* 生产：`uvicorn app.main:app --workers N`，建议配合 Nginx（SSE 需 `proxy_buffering off;`）
* 环境变量 `HOST`/`PORT` 覆盖监听地址
* ES/Neo4j/Milvus 懒连接，未配置不报错
* 指标存内存，多 worker 各自独立统计

### 9.2 Docker 部署

项目已提供 `Dockerfile`，基于 `python:3.12-slim`：

**构建镜像**
```bash
docker build -t smart-qna .
```

**启动容器**
```bash
# 默认端口 8000，使用容器内默认配置
docker run -d --name smart-qna -p 8000:8000 smart-qna

# 挂载本地配置文件（推荐，便于外部修改）
docker run -d --name smart-qna -p 8000:8000 \
  -v $(pwd)/data/config.json:/app/data/config.json \
  smart-qna

# 自定义端口
docker run -d --name smart-qna -p 9000:9000 -e PORT=9000 smart-qna
```

启动后访问 http://localhost:8000/（管理控制台）或 http://localhost:8000/docs（API 文档）。

**Dockerfile 说明**
- 基础镜像 `python:3.12-slim`，体积小
- 依赖单独一层缓存，代码变更不重装依赖
- 仅复制 `app/`、`ui/`、`data/`，排除测试/缓存/文档（`.dockerignore`）
- 默认 `HOST=0.0.0.0 PORT=8000`，可通过 `-e` 覆盖

**docker-compose 示例**（可选，搭配外部服务）

```yaml
version: "3.8"
services:
  smart-qna:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - ./data/config.json:/app/data/config.json
    environment:
      - HOST=0.0.0.0
      - PORT=8000
    restart: unless-stopped
    # 如需连接宿主机上的 ES/Neo4j/Milvus，使用 host.docker.internal
    # extra_hosts:
    #   - "host.docker.internal:host-gateway"
```

```bash
docker-compose up -d
```

## 10. 文档

* [spec.md](spec.md) —— 完整技术规格说明（架构/配置模型/API/检索策略/提示词/指标/异常/测试）
* [RAG.md](RAG.md) —— RAG 检索逻辑剖析（ES/KG/Vec 检索细节 + 同义词扩展 + 融合策略 + 数据流）
* [配置手册.md](配置手册.md) —— ES 索引配置手册（中文分词器/停用词/同义词词典/精确度调优）
* [client.md](client.md) —— 客户端演示脚本说明（8 种模式 case 详解）
* [API 文档](http://localhost:8000/docs) —— Swagger UI（启动后访问）

## 11. 项目 Skills

项目提取了 4 个可复用 skill（`.codebuddy/skills/`）：

| Skill | 说明 |
|---|---|
| `rag-orchestrator` | RAG 多源检索编排（并行调度/rerank 融合/RRF 兜底/容错降级） |
| `fastapi-multi-config` | 多配置管理（items+active/b64:加解密/CRUD/验证切换） |
| `sse-llm-streaming` | SSE 流式 LLM 接口（事件序列/后端流式/前端解析） |
| `metrics-dashboard` | 内存指标采集与可视化（measure 埋点/P95/P99/Chart.js） |
