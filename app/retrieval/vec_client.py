"""Milvus 向量检索客户端。

支持两种检索策略：
1. 单路检索：将 query 整体向量化后检索 Milvus。
2. 多路检索（默认）：先通过 LLM 从 query 中抽取主题/关键概念，
   对原始 query 和每个主题分别向量化，多路并行检索 Milvus，结果合并去重。
   解决多主题 query（如 "A+B+C"）整体向量化后与各子主题向量距离均较远、
   可能漏召回的问题。

返回统一 SourceItem。
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.exceptions import RetrievalError, ESConnectionError
from app.core.metrics import measure
from app.schemas.responses import SourceItem


class VecClient:
    """Milvus 向量检索客户端。"""

    def __init__(self, milvus_config: dict, embedding_client, llm_client=None):
        self.uri = milvus_config.get("uri", "http://localhost:19530")
        self.collection_name = milvus_config.get("collection_name", "documents")
        self.vector_field = milvus_config.get("vector_field", "embedding")
        self.text_field = milvus_config.get("text_field", "content")
        self.metric_type = milvus_config.get("metric_type", "COSINE")
        self._embedding = embedding_client
        self._llm = llm_client  # 可选，用于主题拆分
        self._client: Any = None

    @property
    def client(self):
        if self._client is None:
            try:
                from pymilvus import MilvusClient
            except ImportError as exc:  # pragma: no cover
                raise RetrievalError("未安装 pymilvus 依赖") from exc
            try:
                self._client = MilvusClient(uri=self.uri)
            except Exception as exc:  # noqa: BLE001
                raise ESConnectionError(f"Milvus 连接失败: {exc}") from exc
        return self._client

    def reconfigure(self, milvus_config: dict) -> None:
        self.uri = milvus_config.get("uri", "http://localhost:19530")
        self.collection_name = milvus_config.get("collection_name", "documents")
        self.vector_field = milvus_config.get("vector_field", "embedding")
        self.text_field = milvus_config.get("text_field", "content")
        self.metric_type = milvus_config.get("metric_type", "COSINE")
        self._client = None

    async def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def validate(self) -> dict:
        if not self.uri:
            return {"ok": False, "message": "uri 未配置"}
        try:
            c = self.client
            cols = c.list_collections()
            return {
                "ok": True,
                "message": f"连接成功: {self.uri} (collections: {len(cols)})",
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"验证失败: {exc}"}
        finally:
            await self.close()

    # ------------------------------------------------------------------ #
    # 检索主入口
    # ------------------------------------------------------------------ #
    async def search(self, query: str, top_k: int = 5) -> list[SourceItem]:
        """向量检索：优先多路（主题拆分），LLM 不可用时降级为单路。"""
        if self._llm is not None:
            try:
                return await self._multi_topic_search(query, top_k)
            except Exception:  # noqa: BLE001
                # 主题拆分失败，降级为单路检索
                pass
        return await self._single_search(query, top_k)

    # ------------------------------------------------------------------ #
    # 多路检索（主题拆分）
    # ------------------------------------------------------------------ #
    async def _multi_topic_search(self, query: str, top_k: int) -> list[SourceItem]:
        """先抽取主题，对原始 query + 各主题分别向量化检索，结果合并去重。"""
        # 1. 复用 LLM 实体抽取能力拆分主题
        topics = await self._llm.extract_entities(query)

        # 2. 构建检索 queries：原始 query 始终保留 + 各主题
        queries = [query] + [t for t in topics if t != query]
        # 去重保序
        seen = set()
        unique_queries = []
        for q in queries:
            if q not in seen:
                seen.add(q)
                unique_queries.append(q)

        # 3. 每路分配配额
        per_query = max(1, top_k // max(1, len(unique_queries)))

        # 4. 并行向量化 + 检索
        import asyncio

        tasks = [self._single_search(q, per_query) for q in unique_queries]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 5. 合并去重（按 content 去重，保留首次出现的）
        merged: list[SourceItem] = []
        seen_contents: set[str] = set()
        for res in results:
            if isinstance(res, Exception):
                continue
            for item in res:
                key = item.content.strip()[:200]  # 取前 200 字符作为去重 key
                if key not in seen_contents:
                    seen_contents.add(key)
                    merged.append(item)
                    if len(merged) >= top_k:
                        return merged
        return merged

    # ------------------------------------------------------------------ #
    # 单路检索
    # ------------------------------------------------------------------ #
    async def _single_search(self, query: str, top_k: int) -> list[SourceItem]:
        """将单条 query 向量化后检索 Milvus，返回相似文段。"""
        # 1. 向量化
        query_vec = await self._embedding.embed(query)

        # 2. 向量检索
        async with measure("milvus", "search"):
            try:
                results = self.client.search(
                    collection_name=self.collection_name,
                    data=[query_vec],
                    limit=top_k,
                    output_fields=[self.text_field],
                    search_params={"metric_type": self.metric_type},
                )
            except Exception as exc:  # noqa: BLE001
                raise ESConnectionError(f"Milvus 检索失败: {exc}") from exc

        # 3. 解析结果
        return self._parse_results(results)

    def _parse_results(self, results) -> list[SourceItem]:
        """解析 MilvusClient.search 返回结果。"""
        items: list[SourceItem] = []
        # MilvusClient.search 返回 list[list[dict]]，外层对应每条 query
        for hit in (results[0] if results else []):
            entity = hit.get("entity", {}) or {}
            distance = hit.get("distance")
            items.append(
                SourceItem(
                    source="vec",
                    title=None,
                    content=str(entity.get(self.text_field, "")),
                    score=distance,
                    meta={"collection": self.collection_name, "id": hit.get("id")},
                )
            )
        return items
