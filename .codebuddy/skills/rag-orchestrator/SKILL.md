---
name: rag-orchestrator
description: >-
  RAG 多源检索编排技能。提供多路检索（Elasticsearch 关键字 + Neo4j 图谱 + Milvus 向量）的并行调度、
  rerank 融合（API + RRF 兜底）、LLM 实体抽取前置、上下文组装等 RAG 核心模式的实现指导。
  适用于需要构建检索增强生成服务、多源知识检索、智能问答后端的场景。当用户提到 RAG、多路检索、
  知识图谱检索、向量检索融合、rerank 重排、检索策略编排时触发。
---

# RAG 多源检索编排

## 概述

本技能提供构建 RAG（Retrieval-Augmented Generation）多源检索服务的完整编排模式，涵盖多路并行检索调度、实体抽取前置、rerank 融合、容错降级、上下文组装等核心环节。适用于任何需要组合多种检索源（关键字/图谱/向量）并通过 LLM 生成答案的后端服务。

## 何时使用

- 构建 RAG 智能问答后端服务
- 需要组合 Elasticsearch + Neo4j + Milvus 等多源检索
- 需要实现多路检索结果的 rerank 融合
- 需要设计检索策略编排（单路/并行/融合模式）

## 核心架构模式

### 检索模式设计

设计多种检索模式供调用方选择，典型模式包括：

| 模式 | 检索来源 | 实体抽取 | 向量化 | 说明 |
|---|---|---|---|---|
| 无 | 不检索 | 否 | 否 | 纯 LLM 对话 |
| 单路 | 单一来源 | 视来源 | 否 | 仅检索一个源 |
| 并行 | 多源并行 | 是 | 否 | 结果直接拼接 |
| 融合 | 多源并行 + rerank | 是 | 是 | 统一打分取 top_n |

### 两阶段并发调度

多路检索中，部分检索路存在依赖关系（如图谱检索依赖实体抽取结果）。采用两阶段并发最大化并行度：

```
阶段1（立即并行）：
  ├─ 实体抽取（LLM）
  ├─ 关键字检索（ES）
  └─ 向量检索（Milvus，需先 embedding）

阶段2（实体抽取完成后）：
  └─ 图谱检索（Neo4j，基于抽取实体）

阶段3：汇总
  asyncio.gather(return_exceptions=True)
```

实现要点：
- `asyncio.create_task` 立即调度协程，不等 await 即开始执行
- `await entity_task` 仅阻塞等待实体抽取，期间其他检索路并行进行
- `asyncio.gather(..., return_exceptions=True)` 收集结果，单路异常返回 Exception 而非抛出

### 容错降级

每路检索独立可能失败，需保证单路失败不阻断整体：

```python
def _unwrap(result) -> list:
    if isinstance(result, Exception):
        return []  # 该路失败，贡献 0 条结果
    if isinstance(result, list):
        return result
    return []
```

实体抽取失败时降级为空列表，不阻断主流程：
```python
async def _safe_entities(self, query: str) -> list[str]:
    try:
        return await self.llm.extract_entities(query)
    except Exception:
        return []
```

### Rerank 融合

融合模式将多路结果统一打分，仅保留高分项：

1. **rerank API 路径**（Cohere/Jina 兼容）：`POST {base}/rerank`，对 (query, documents) 打分，按 `relevance_score` 排序
2. **RRF 兜底**（无模型）：`score = 1/(60+rank)`，按检索路分组，组内排序后跨路统一 RRF 分数降序

### 上下文组装

检索结果统一为 SourceItem 列表，按编号组装为 LLM 可读的参考资料块：

```
[1] (来源:article | 标题:xxx)
内容...

[2] (来源:kg | 标题:实体名)
[Label] 实体名
  -(关系类型)-> 目标
```

序号对应 LLM 提示词中的 `[n]` 引用标注要求。

### 双系统提示词

- 纯对话（无检索上下文）：通用对话提示词，允许模型自由作答
- RAG（有检索上下文）：严格依据资料作答，资料不足明确说明，用 `[n]` 标注引用

## 详细参考

完整的检索逻辑剖析、Cypher 查询模板、rerank API 格式、RRF 算法细节等内容，参见：
- `references/retrieval_patterns.md` —— 各检索源的详细实现模式与查询模板
- `references/fusion_strategies.md` —— rerank API 与 RRF 融合的完整实现

## 实现清单

构建 RAG 编排服务时，按以下步骤实施：

1. 定义统一来源模型（SourceItem：source/title/content/score/meta）
2. 实现各检索客户端（ES/Neo4j/Milvus），统一返回 SourceItem 列表
3. 实现 LLM 实体抽取（JSON 输出 + 容错解析）
4. 实现 Rerank 融合客户端（API + RRF 兜底）
5. 实现编排器：模式分发 → 两阶段并发 → 容错汇总 → 上下文组装 → LLM 生成
6. 各客户端提供 `validate()` 方法用于可用性验证
