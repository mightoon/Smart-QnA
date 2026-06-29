#!/usr/bin/env python
"""ES + Neo4j 测试数据注入脚本。

向 Elasticsearch 注入 article / qna 索引的 mapping 与测试数据，
向 Neo4j 注入知识图谱节点与关系。

用法：
  python seed_data.py --es-url http://localhost:9200 --neo4j-uri bolt://localhost:7687 --neo4j-user neo4j --neo4j-password yourpass

  # 只注入 ES
  python seed_data.py --es-only --es-url http://localhost:9200

  # 只注入 Neo4j（使用独立 database 隔离数据，需 Enterprise 版）
  python seed_data.py --neo4j-only --neo4j-uri bolt://localhost:7687 --neo4j-user neo4j --neo4j-password yourpass --neo4j-database qna --neo4j-create-db

  # 只注入 Neo4j（Community 版，使用默认 neo4j database，仅清理特定 label）
  python seed_data.py --neo4j-only --neo4j-uri bolt://localhost:7687 --neo4j-user neo4j --neo4j-password yourpass

安全说明：
  - Neo4j 注入仅删除带 QnaSeed 专属标签的节点，不影响用户已有数据。
  - 所有注入节点均打上 QnaSeed 标签 + 业务标签（如 Model/Company/Concept/Tool）。
  - Community 版直接在默认 neo4j database 注入，通过 QnaSeed 标签逻辑隔离。
"""

from __future__ import annotations

import argparse
import sys

# ====================================================================== #
# ES 测试数据
# ====================================================================== #

ARTICLE_MAPPING = {
    "mappings": {
        "properties": {
            "title":   {"type": "text", "analyzer": "standard"},
            "content": {"type": "text", "analyzer": "standard"},
        }
    }
}

QNA_MAPPING = {
    "mappings": {
        "properties": {
            "question": {"type": "text", "analyzer": "standard"},
            "answer":   {"type": "text", "analyzer": "standard"},
        }
    }
}

ARTICLES = [
    {
        "title": "RAG 检索增强生成技术综述",
        "content": (
            "RAG（Retrieval-Augmented Generation，检索增强生成）是一种结合信息检索与大语言模型生成的技术。"
            "其核心思想是：在用户提问时，先从外部知识库中检索相关文档，再将检索结果作为上下文提供给大模型，"
            "由大模型基于这些资料生成准确的回答。RAG 有效缓解了大模型幻觉问题，使回答可溯源。"
            "典型的 RAG 流程包括：文档分块、向量化（embedding）、存储到向量数据库、检索、重排序（rerank）、生成。"
            "RAG 相比纯微调的优势在于无需重新训练模型，知识可动态更新。"
        ),
    },
    {
        "title": "大模型微调最佳实践指南",
        "content": (
            "大模型微调（fine-tuning）是在预训练模型基础上，使用领域特定数据进一步训练模型的过程。"
            "常见微调方法包括全参数微调（Full Fine-tuning）、LoRA（Low-Rank Adaptation）、QLoRA 等。"
            "最佳实践包括：选择合适的基座模型（如 LLaMA、DeepSeek）；准备高质量的指令微调（SFT）数据集；"
            "使用 LoRA 降低显存需求；设置合适的学习率（通常 1e-4 到 5e-5）；使用 early stopping 防止过拟合。"
            "微调后的模型在特定领域表现更优，但可能丧失通用能力。"
        ),
    },
    {
        "title": "DeepSeek 与 GPT-4 模型对比分析",
        "content": (
            "DeepSeek 是一款开源大语言模型，采用 MoE（Mixture of Experts）架构，支持 67B 参数规模。"
            "GPT-4 是 OpenAI 开发的闭源大模型，能力强大但 API 调用收费。"
            "两者区别：DeepSeek 开源可部署，GPT-4 闭源仅 API；DeepSeek 成本低，GPT-4 效果略优；"
            "DeepSeek 支持本地化部署，GPT-4 需联网调用。在代码生成、数学推理等任务上两者表现接近。"
            "DeepSeek-V3 是其最新版本，性能对标 GPT-4o。"
        ),
    },
    {
        "title": "Milvus 向量数据库工作原理与应用",
        "content": (
            "Milvus 是一款开源向量数据库，专门用于存储和检索高维向量。其工作原理是：将文本通过 embedding 模型转换为向量，"
            "然后使用近似最近邻（ANN）算法在向量空间中查找语义相似的文档。Milvus 支持多种索引类型（IVF_FLAT、HNSW、IVF_SQ8）"
            "和距离度量方式（COSINE、L2、IP）。向量检索的优势在于能匹配语义相似但字面不同的内容，"
            "如'大模型'能匹配到'LLM'。Milvus 适用于语义搜索、推荐系统、图文检索等场景。"
        ),
    },
    {
        "title": "知识图谱构建与 Neo4j 实践",
        "content": (
            "知识图谱（Knowledge Graph）以图结构组织知识，节点表示实体，边表示实体间的关系。"
            "Neo4j 是主流的图数据库，使用 Cypher 查询语言。构建知识图谱的流程包括：实体抽取、关系抽取、"
            "实体对齐、知识存储。知识图谱的优势在于支持精确的关系查询和多跳推理，如'DeepSeek 的竞争对手是谁'。"
            "知识图谱与向量检索互补：图谱擅长结构化关系，向量擅长语义相似。两者结合（如 merge 模式）能提升问答质量。"
        ),
    },
    {
        "title": "FastAPI 异步流式接口开发指南",
        "content": (
            "FastAPI 是现代 Python Web 框架，原生支持异步处理和流式输出。通过 StreamingResponse 可实现 SSE（Server-Sent Events）"
            "流式接口，实时推送数据到客户端。FastAPI 部署推荐使用 uvicorn 或 gunicorn + uvicorn worker。"
            "生产环境建议配合 Nginx 反向代理，注意 SSE 需关闭 proxy_buffering。"
            "FastAPI 自动生成 Swagger UI（/docs）和 ReDoc（/redoc）API 文档，便于调试。"
        ),
    },
    {
        "title": "Embedding 向量化模型原理与选择",
        "content": (
            "Embedding 是将文本转换为高维向量表示的技术，使得语义相近的文本在向量空间中距离更近。"
            "常见的 embedding 模型包括 OpenAI text-embedding-3-small（1536维）、text-embedding-3-large（3072维）、"
            "BGE、E5 等。选择 embedding 模型时需考虑：向量维度（影响存储和检索速度）、语言支持（中英文）、"
            "模型大小和推理速度。embedding 质量直接影响向量检索的效果。"
        ),
    },
    {
        "title": "Rerank 重排序模型在 RAG 中的应用",
        "content": (
            "Rerank（重排序）是 RAG 流程中的关键环节，对多路检索结果统一打分排序，仅保留高分项作为最终上下文。"
            "常用 rerank 模型包括 Cohere rerank-multilingual-v3.0、Jina rerank 等。"
            "rerank 的作用是：多路检索（如 ES + 向量 + 图谱）各召回一批结果，质量参差不齐，"
            "rerank 模型能根据 query 与文档的相关性重新打分，过滤低质量结果。"
            "当 rerank 模型不可用时，可使用 RRF（Reciprocal Rank Fusion）作为无模型兜底方案。"
        ),
    },
]

QNAS = [
    {
        "question": "什么是 RAG 检索增强生成？",
        "answer": (
            "RAG 是一种结合检索与生成的技术。在用户提问时，先从知识库检索相关文档，"
            "再将检索结果作为上下文交给大模型生成回答。RAG 能减少幻觉，使回答可溯源，且知识可动态更新。"
        ),
    },
    {
        "question": "如何部署 FastAPI 应用到生产环境？",
        "answer": (
            "使用 uvicorn 或 gunicorn + uvicorn worker 部署 FastAPI。生产环境建议配合 Nginx 反向代理，"
            "注意 SSE 流式接口需关闭 proxy_buffering。可通过 --workers N 启动多进程。"
        ),
    },
    {
        "question": "DeepSeek 和 GPT-4 有什么区别？",
        "answer": (
            "DeepSeek 是开源大模型，采用 MoE 架构，可本地部署，成本低。GPT-4 是 OpenAI 闭源模型，仅 API 调用，效果略优但收费。"
            "两者在代码生成、数学推理等任务上表现接近。DeepSeek-V3 性能对标 GPT-4o。"
        ),
    },
    {
        "question": "向量数据库的工作原理是什么？",
        "answer": (
            "向量数据库将文本通过 embedding 模型转为向量，使用 ANN 算法在向量空间查找语义相似的文档。"
            "支持 COSINE、L2、IP 等距离度量。优势是能匹配语义相似但字面不同的内容。"
        ),
    },
    {
        "question": "知识图谱和向量检索各自的优缺点？",
        "answer": (
            "知识图谱擅长精确的关系查询和多跳推理（如 A 的竞争对手是谁），但构建成本高。"
            "向量检索擅长语义相似匹配，构建简单，但不擅长关系推理。两者互补，结合使用效果最佳。"
        ),
    },
    {
        "question": "大模型微调有哪些方法？",
        "answer": (
            "常见微调方法包括全参数微调、LoRA、QLoRA。LoRA 通过低秩矩阵降低显存需求，是最常用的方案。"
            "微调需准备高质量的 SFT 数据集，设置合适学习率，使用 early stopping 防止过拟合。"
        ),
    },
    {
        "question": "什么是 embedding 向量化？",
        "answer": (
            "Embedding 是将文本转为高维向量的技术，使语义相近的文本在向量空间距离更近。"
            "常见模型有 OpenAI text-embedding-3-small、BGE、E5。embedding 质量直接影响向量检索效果。"
        ),
    },
    {
        "question": "rerank 重排序模型的作用是什么？",
        "answer": (
            "Rerank 对多路检索结果统一打分排序，仅保留高分项。常用模型有 Cohere、Jina。"
            "rerank 不可用时可使用 RRF（倒数排名融合）作为兜底方案。"
        ),
    },
]


def seed_es(es_url: str, es_user: str = "", es_password: str = "") -> None:
    try:
        from elasticsearch import Elasticsearch
    except ImportError:
        print("  [!] 未安装 elasticsearch，跳过 ES 注入")
        return

    print("\n===== ES 注入开始 =====")

    # 构建连接参数
    es_kwargs = dict(
        verify_certs=False,
        ssl_show_warn=False,
        request_timeout=30,
    )
    if es_user and es_password:
        es_kwargs["basic_auth"] = (es_user, es_password)

    es = Elasticsearch(es_url, **es_kwargs)

    # 先检测连通性和版本
    try:
        info = es.info()
        version = info.get("version", {}).get("number", "unknown")
        cluster = info.get("cluster_name", "unknown")
        print(f"  连接成功: cluster={cluster}, version={version}")
    except Exception as e:
        print(f"  [!] 无法连接 ES: {e}")
        print("  提示: ES 8.x 默认开启安全认证，可能需要:")
        print("    1. 使用 https:// 而非 http://")
        print("    2. 提供 --es-user 和 --es-password 参数")
        print("    3. 或在 elasticsearch.yml 中关闭 xpack.security.enabled")
        return

    # article 索引
    if es.indices.exists(index="article"):
        es.indices.delete(index="article")
        print("  删除旧 article 索引")
    es.indices.create(index="article", mappings=ARTICLE_MAPPING["mappings"])
    print(f"  创建 article 索引，注入 {len(ARTICLES)} 条文档")
    for i, doc in enumerate(ARTICLES):
        es.index(index="article", id=i + 1, document=doc)
    es.indices.refresh(index="article")

    # qna 索引
    if es.indices.exists(index="qna"):
        es.indices.delete(index="qna")
        print("  删除旧 qna 索引")
    es.indices.create(index="qna", mappings=QNA_MAPPING["mappings"])
    print(f"  创建 qna 索引，注入 {len(QNAS)} 条问答")
    for i, doc in enumerate(QNAS):
        es.index(index="qna", id=i + 1, document=doc)
    es.indices.refresh(index="qna")

    print("===== ES 注入完成 =====\n")


# ====================================================================== #
# Neo4j 测试数据
# ====================================================================== #

NEO4J_CYPHER = """
// 仅清理本脚本注入的节点（通过专属标签 QnaSeed 过滤，绝不影响其他数据）
MATCH (n:QnaSeed) DETACH DELETE n;

// ===== 节点：模型（均打上 QnaSeed 专属标签）=====
CREATE (dsv3:QnaSeed:Model {name: 'DeepSeek-V3', type: '开源', params: '671B', architecture: 'MoE'});
CREATE (dsv4:QnaSeed:Model {name: 'DeepSeek-V4-Flash', type: '开源', params: '671B', architecture: 'MoE'});
CREATE (gpt4:QnaSeed:Model {name: 'GPT-4', type: '闭源', params: '未知', architecture: 'Transformer'});
CREATE (gpt4o:QnaSeed:Model {name: 'GPT-4o', type: '闭源', params: '未知', architecture: 'Transformer'});
CREATE (llama:QnaSeed:Model {name: 'LLaMA-3', type: '开源', params: '70B', architecture: 'Transformer'});
CREATE (glm:QnaSeed:Model {name: 'GLM-4', type: '开源', params: '130B', architecture: 'Transformer'});

// ===== 节点：公司 =====
CREATE (ds:QnaSeed:Company {name: 'DeepSeek', founded: '2023'});
CREATE (openai:QnaSeed:Company {name: 'OpenAI', founded: '2015'});
CREATE (meta:QnaSeed:Company {name: 'Meta', founded: '2004'});
CREATE (zhipu:QnaSeed:Company {name: '智谱AI', founded: '2019'});

// ===== 节点：技术概念 =====
CREATE (rag:QnaSeed:Concept {name: 'RAG', description: '检索增强生成'});
CREATE (ft:QnaSeed:Concept {name: 'Fine-tuning', description: '模型微调技术'});
CREATE (emb:QnaSeed:Concept {name: 'Embedding', description: '文本向量化'});
CREATE (kg:QnaSeed:Concept {name: 'KnowledgeGraph', description: '知识图谱'});
CREATE (vec:QnaSeed:Concept {name: 'VectorSearch', description: '向量检索'});
CREATE (rerank:QnaSeed:Concept {name: 'Rerank', description: '重排序融合'});
CREATE (moe:QnaSeed:Concept {name: 'MoE', description: '混合专家架构'});
CREATE (sft:QnaSeed:Concept {name: 'SFT', description: '指令微调'});
CREATE (lora:QnaSeed:Concept {name: 'LoRA', description: '低秩适配微调'});

// ===== 节点：框架/工具 =====
CREATE (milvus:QnaSeed:Tool {name: 'Milvus', description: '向量数据库'});
CREATE (neo4j:QnaSeed:Tool {name: 'Neo4j', description: '图数据库'});
CREATE (es:QnaSeed:Tool {name: 'Elasticsearch', description: '搜索引擎'});
CREATE (fastapi:QnaSeed:Tool {name: 'FastAPI', description: 'Python Web 框架'});
CREATE (cohere:QnaSeed:Tool {name: 'Cohere', description: 'Rerank API 服务'});

// ===== 关系：公司 develops 模型 =====
MATCH (ds:QnaSeed:Company {name:'DeepSeek'}), (dsv3:QnaSeed:Model {name:'DeepSeek-V3'}) CREATE (ds)-[:develops]->(dsv3);
MATCH (ds:QnaSeed:Company {name:'DeepSeek'}), (dsv4:QnaSeed:Model {name:'DeepSeek-V4-Flash'}) CREATE (ds)-[:develops]->(dsv4);
MATCH (openai:QnaSeed:Company {name:'OpenAI'}), (gpt4:QnaSeed:Model {name:'GPT-4'}) CREATE (openai)-[:develops]->(gpt4);
MATCH (openai:QnaSeed:Company {name:'OpenAI'}), (gpt4o:QnaSeed:Model {name:'GPT-4o'}) CREATE (openai)-[:develops]->(gpt4o);
MATCH (meta:QnaSeed:Company {name:'Meta'}), (llama:QnaSeed:Model {name:'LLaMA-3'}) CREATE (meta)-[:develops]->(llama);
MATCH (zhipu:QnaSeed:Company {name:'智谱AI'}), (glm:QnaSeed:Model {name:'GLM-4'}) CREATE (zhipu)-[:develops]->(glm);

// ===== 关系：模型 competes_with 模型 =====
MATCH (dsv3:QnaSeed:Model {name:'DeepSeek-V3'}), (gpt4:QnaSeed:Model {name:'GPT-4'}) CREATE (dsv3)-[:competes_with]->(gpt4);
MATCH (dsv4:QnaSeed:Model {name:'DeepSeek-V4-Flash'}), (gpt4o:QnaSeed:Model {name:'GPT-4o'}) CREATE (dsv4)-[:competes_with]->(gpt4o);
MATCH (llama:QnaSeed:Model {name:'LLaMA-3'}), (gpt4:QnaSeed:Model {name:'GPT-4'}) CREATE (llama)-[:competes_with]->(gpt4);
MATCH (glm:QnaSeed:Model {name:'GLM-4'}), (gpt4:QnaSeed:Model {name:'GPT-4'}) CREATE (glm)-[:competes_with]->(gpt4);

// ===== 关系：模型 uses_architecture 概念 =====
MATCH (dsv3:QnaSeed:Model {name:'DeepSeek-V3'}), (moe:QnaSeed:Concept {name:'MoE'}) CREATE (dsv3)-[:uses_architecture]->(moe);
MATCH (dsv4:QnaSeed:Model {name:'DeepSeek-V4-Flash'}), (moe:QnaSeed:Concept {name:'MoE'}) CREATE (dsv4)-[:uses_architecture]->(moe);

// ===== 关系：概念 related_to 概念 =====
MATCH (rag:QnaSeed:Concept {name:'RAG'}), (emb:QnaSeed:Concept {name:'Embedding'}) CREATE (rag)-[:uses]->(emb);
MATCH (rag:QnaSeed:Concept {name:'RAG'}), (vec:QnaSeed:Concept {name:'VectorSearch'}) CREATE (rag)-[:uses]->(vec);
MATCH (rag:QnaSeed:Concept {name:'RAG'}), (kg:QnaSeed:Concept {name:'KnowledgeGraph'}) CREATE (rag)-[:uses]->(kg);
MATCH (rag:QnaSeed:Concept {name:'RAG'}), (rerank:QnaSeed:Concept {name:'Rerank'}) CREATE (rag)-[:uses]->(rerank);
MATCH (ft:QnaSeed:Concept {name:'Fine-tuning'}), (sft:QnaSeed:Concept {name:'SFT'}) CREATE (ft)-[:includes]->(sft);
MATCH (ft:QnaSeed:Concept {name:'Fine-tuning'}), (lora:QnaSeed:Concept {name:'LoRA'}) CREATE (ft)-[:includes]->(lora);
MATCH (vec:QnaSeed:Concept {name:'VectorSearch'}), (emb:QnaSeed:Concept {name:'Embedding'}) CREATE (vec)-[:depends_on]->(emb);
MATCH (kg:QnaSeed:Concept {name:'KnowledgeGraph'}), (vec:QnaSeed:Concept {name:'VectorSearch'}) CREATE (kg)-[:complement_to]->(vec);
MATCH (rerank:QnaSeed:Concept {name:'Rerank'}), (rag:QnaSeed:Concept {name:'RAG'}) CREATE (rerank)-[:used_in]->(rag);

// ===== 关系：工具 used_by 概念 =====
MATCH (milvus:QnaSeed:Tool {name:'Milvus'}), (vec:QnaSeed:Concept {name:'VectorSearch'}) CREATE (milvus)-[:implements]->(vec);
MATCH (neo4j:QnaSeed:Tool {name:'Neo4j'}), (kg:QnaSeed:Concept {name:'KnowledgeGraph'}) CREATE (neo4j)-[:implements]->(kg);
MATCH (es:QnaSeed:Tool {name:'Elasticsearch'}), (rag:QnaSeed:Concept {name:'RAG'}) CREATE (es)-[:used_in]->(rag);
MATCH (fastapi:QnaSeed:Tool {name:'FastAPI'}), (rag:QnaSeed:Concept {name:'RAG'}) CREATE (fastapi)-[:used_in]->(rag);
MATCH (cohere:QnaSeed:Tool {name:'Cohere'}), (rerank:QnaSeed:Concept {name:'Rerank'}) CREATE (cohere)-[:implements]->(rerank);
"""


def seed_neo4j(uri: str, user: str, password: str, database: str, create_db: bool = False) -> None:
    try:
        from neo4j import GraphDatabase
    except ImportError:
        print("  [!] 未安装 neo4j，跳过 Neo4j 注入")
        return

    print("\n===== Neo4j 注入开始 =====")
    driver = GraphDatabase.driver(uri, auth=(user, password))

    # 连通性检测
    try:
        driver.verify_connectivity()
    except Exception as exc:
        print(f"  [!] 无法连接 Neo4j: {exc}")
        driver.close()
        return

    # 可选：创建新 database（仅 Enterprise Edition 支持）
    if create_db and database != "neo4j":
        with driver.session(database="system") as session:
            try:
                session.run(f"CREATE DATABASE `{database}` IF NOT EXISTS")
                print(f"  创建/确认 database: {database}")
            except Exception as exc:
                print(f"  [!] 创建 database 失败（可能为 Community 版不支持多 database）: {exc}")
                print(f"  将使用默认 neo4j database")
                database = "neo4j"

    # 安全确认：显示目标 database 和已有数据量
    with driver.session(database=database) as session:
        existing_nodes = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        existing_rels = session.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
    print(f"  目标 database: {database}")
    print(f"  当前已有数据: {existing_nodes} 个节点, {existing_rels} 条关系")
    print(f"  本脚本仅删除带 QnaSeed 标签的节点，不影响其他数据。")

    # 按分号拆分逐条执行
    statements = [s.strip() for s in NEO4J_CYPHER.split(";") if s.strip()]
    print(f"  共 {len(statements)} 条 Cypher 语句，开始执行...")

    with driver.session(database=database) as session:
        for i, stmt in enumerate(statements, 1):
            session.run(stmt)

    # 统计
    with driver.session(database=database) as session:
        nodes = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        rels = session.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
    print(f"  注入完成: 当前 database 共 {nodes} 个节点, {rels} 条关系")

    driver.close()
    print("===== Neo4j 注入完成 =====\n")


# ====================================================================== #
# Milvus 测试数据（可选，需 pymilvus + embedding）
# ====================================================================== #

MILVUS_DOCUMENTS = [a["content"] for a in ARTICLES[:6]]  # 复用 ES 文章内容


def seed_milvus(uri: str, collection: str, embedding_base: str, embedding_key: str, embedding_model: str) -> None:
    try:
        from pymilvus import MilvusClient, DataType
        from openai import OpenAI
    except ImportError:
        print("  [!] 未安装 pymilvus 或 openai，跳过 Milvus 注入")
        return

    if not embedding_key:
        print("  [!] 未提供 embedding api_key，跳过 Milvus 注入")
        return

    print("\n===== Milvus 注入开始 =====")

    # 1. 生成 embedding
    print("  生成 embedding...")
    client = OpenAI(api_key=embedding_key, base_url=embedding_base or None)
    docs = MILVUS_DOCUMENTS
    embeddings = []
    for doc in docs:
        resp = client.embeddings.create(model=embedding_model, input=doc)
        embeddings.append(resp.data[0].embedding)
    dim = len(embeddings[0])
    print(f"  生成 {len(embeddings)} 条向量, 维度 {dim}")

    # 2. 创建 collection
    mc = MilvusClient(uri=uri)
    try:
        mc.drop_collection(collection_name=collection)
    except Exception:
        pass

    from pymilvus import MilvusClient as MC
    schema = MC.create_schema(auto_id=True, enable_dynamic_field=False)
    schema.add_field("id", DataType.INT64, is_primary=True)
    schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=dim)
    schema.add_field("content", DataType.VARCHAR, max_length=4096)

    mc.create_collection(
        collection_name=collection,
        schema=schema,
    )
    # 创建索引
    index_params = mc.prepare_index_params()
    index_params.add_index(field_name="embedding", index_type="AUTOINDEX", metric_type="COSINE")
    mc.create_index(collection_name=collection, index_params=index_params)

    # 3. 插入数据
    for doc, vec in zip(docs, embeddings):
        mc.insert(collection_name=collection, data={"embedding": vec, "content": doc})

    mc.load_collection(collection_name=collection)
    print(f"  注入完成: {len(docs)} 条文档到 {collection}")
    mc.close()
    print("===== Milvus 注入完成 =====\n")


# ====================================================================== #
# 主入口
# ====================================================================== #

def main():
    parser = argparse.ArgumentParser(description="Smart QnA 测试数据注入")
    parser.add_argument("--es-url", default="http://localhost:9200", help="ES 地址")
    parser.add_argument("--es-user", default="", help="ES 用户名（ES 8.x 安全认证）")
    parser.add_argument("--es-password", default="", help="ES 密码（ES 8.x 安全认证）")
    parser.add_argument("--neo4j-uri", default="bolt://localhost:7687", help="Neo4j 地址")
    parser.add_argument("--neo4j-user", default="neo4j", help="Neo4j 用户名")
    parser.add_argument("--neo4j-password", default="", help="Neo4j 密码")
    parser.add_argument("--neo4j-database", default="neo4j", help="Neo4j 数据库名（建议用独立 database 隔离数据）")
    parser.add_argument("--neo4j-create-db", action="store_true", help="自动创建新 database（仅 Enterprise 版支持）")
    parser.add_argument("--milvus-uri", default="http://localhost:19530", help="Milvus 地址")
    parser.add_argument("--milvus-collection", default="documents", help="Milvus collection 名")
    parser.add_argument("--embedding-base", default="", help="Embedding API 地址")
    parser.add_argument("--embedding-key", default="", help="Embedding API key")
    parser.add_argument("--embedding-model", default="text-embedding-3-small", help="Embedding 模型名")
    parser.add_argument("--es-only", action="store_true", help="只注入 ES")
    parser.add_argument("--neo4j-only", action="store_true", help="只注入 Neo4j")
    parser.add_argument("--milvus-only", action="store_true", help="只注入 Milvus")
    args = parser.parse_args()

    do_all = not (args.es_only or args.neo4j_only or args.milvus_only)

    if do_all or args.es_only:
        seed_es(args.es_url, args.es_user, args.es_password)

    if do_all or args.neo4j_only:
        if not args.neo4j_password:
            print("[!] 未提供 --neo4j-password，跳过 Neo4j 注入")
        else:
            seed_neo4j(args.neo4j_uri, args.neo4j_user, args.neo4j_password, args.neo4j_database, args.neo4j_create_db)

    if do_all or args.milvus_only:
        seed_milvus(
            args.milvus_uri, args.milvus_collection,
            args.embedding_base, args.embedding_key, args.embedding_model,
        )

    print("注入流程结束。")


if __name__ == "__main__":
    main()
