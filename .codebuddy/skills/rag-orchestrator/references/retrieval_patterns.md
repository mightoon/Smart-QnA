# 检索源实现模式

## Elasticsearch 关键字检索

### 索引字段映射
不同索引字段名不同，通过映射表统一：
```python
_INDEX_FIELDS = {
    "article": {"body": "content", "title": "title"},
    "qna":     {"body": "answer",  "title": "question"},
}
```

### BM25 multi_match 查询
正文字段加权，使用 best_fields 策略（取匹配最高的字段得分）：
```python
body = {
    "size": top_k,
    "query": {
        "multi_match": {
            "query": query,
            "fields": [f"{body_field}^3", title_field],
            "type": "best_fields",
        }
    },
}
```

## Neo4j 图谱检索

### 前置：LLM 实体抽取
图谱节点是规范实体名，需先从用户问题抽取实体：
- 使用专属 system prompt，要求返回严格 JSON `{"entities": [...]}`
- temperature=0.0 保证稳定
- 解析容错：json.loads 失败则正则抽取首个 `{...}`

### Cypher 查询模板
```cypher
MATCH (n)
WHERE toLower(n.name) CONTAINS toLower($entity)
OPTIONAL MATCH (n)-[r]-(m)
WITH n, collect(distinct [type(r), coalesce(m.name, '')]) AS rels
RETURN n.name AS name, labels(n) AS labels, rels
LIMIT $limit
```
- `CONTAINS` 做大小写不敏感包含匹配，提升召回率
- `OPTIONAL MATCH` 获取邻居关系
- `collect(distinct ...)` 聚合去重

### 多实体配额分配
```python
per_entity = max(1, top_k // max(1, len(entities)))
```

## Milvus 向量检索

### 前置：Embedding 向量化
调用 OpenAI 兼容 /embeddings 接口将 query 转向量。

### ANN 检索
```python
results = client.search(
    collection_name=collection,
    data=[query_vec],
    limit=top_k,
    output_fields=[text_field],
    search_params={"metric_type": "COSINE"},
)
```

### 独立性
向量检索不依赖实体抽取，与 ES/KG 检索完全独立，可任意并行。

## 统一来源模型

```python
class SourceItem:
    source: str          # "article" / "qna" / "kg" / "vec"
    title: Optional[str]
    content: str
    score: Optional[float]
    meta: Optional[dict]
```

各检索路产出归一化为 SourceItem，便于后续组装与展示。
