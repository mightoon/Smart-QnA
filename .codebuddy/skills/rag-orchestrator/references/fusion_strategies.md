# 融合策略

## Rerank API（Cohere/Jina 兼容）

当 rerank 模型完整配置（api_base + api_key + model）时调用：

**请求**
```json
POST {api_base}/rerank
Authorization: Bearer {api_key}
{
  "model": "...",
  "query": "用户问题",
  "documents": ["doc1", "doc2", ...],
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

按 relevance_score 排序，映射回 SourceItem 并更新 score 字段。

## RRF 兜底（无模型融合）

rerank 未配置或调用失败时降级为 Reciprocal Rank Fusion：

```python
_RRF_K = 60

def _rrf_fallback(sources, top_n):
    groups = defaultdict(list)
    for s in sources:
        groups[s.source].append(s)
    # ES 路按 score 降序，其他路保持原序
    for key, items in groups.items():
        if key in ("article", "qna"):
            items.sort(key=lambda x: x.score or 0, reverse=True)

    scored = []
    for items in groups.values():
        for rank, s in enumerate(items):
            rrf = 1.0 / (_RRF_K + rank)
            scored.append((rrf, s))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:top_n]]
```

**RRF 语义**：每条结果按其在所属检索路内的排名计算分数 `1/(60+rank)`，跨路统一排序。rank=0 的项得分最高（1/60），保证每路的最佳结果都能进入 top_n。常数 60 是业界经验值。

## top_n 与 top_k 的关系

- `top_k`（请求参数）：每路检索召回量（如 5 → 四路最多 20 条候选）
- `top_n`（rerank 配置）：融合后保留量（如 5 → 最终仅 5 条进 LLM 上下文）

top_n 未配置（0）时回退到 top_k。

## 融合流程

```
多路检索结果（最多 top_k × N 条）
    │
    ├─ rerank 已配置？─ 是 → 调用 rerank API
    │                   否/失败 → RRF 兜底
    │
    ▼
top_n 条高分项
    │
    ▼
组装上下文 → LLM 生成
```
