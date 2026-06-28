"""Rerank 融合客户端。

用于 merge 模式：对三路检索结果统一打分排序，取高分项作为最终上下文。

策略：
1. 若 rerank 模型已完整配置（api_base + api_key + model），调用 rerank API
   （Cohere / Jina 兼容格式）对 (query, documents) 打分，按分排序取 top_n。
2. 若未配置或调用失败，降级为 RRF（Reciprocal Rank Fusion，1/(60+rank)），
   按各检索路内的排名做无模型融合，保证 merge 模式始终可用。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from app.core.metrics import measure
from app.schemas.responses import SourceItem

# RRF 常数，业界经验值 60。
_RRF_K = 60


class RerankClient:
    """Rerank 融合客户端。"""

    def __init__(self, config: dict):
        self.api_base = config.get("api_base", "")
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "")
        self.top_n = int(config.get("top_n", 0))  # 0 表示使用调用方传入的默认值

    # ------------------------------------------------------------------ #
    @property
    def configured(self) -> bool:
        return bool(self.api_base and self.api_key and self.model)

    def reconfigure(self, config: dict) -> None:
        self.api_base = config.get("api_base", "")
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "")
        self.top_n = int(config.get("top_n", 0))

    # ------------------------------------------------------------------ #
    # 融合主入口
    # ------------------------------------------------------------------ #
    async def rerank(
        self, query: str, sources: list[SourceItem], default_top_n: int = 5
    ) -> list[SourceItem]:
        """对检索结果融合排序，返回 top_n 条。

        优先调用 rerank API；不可用或失败时降级为 RRF。
        """
        if not sources:
            return []
        top_n = self.top_n if self.top_n > 0 else default_top_n
        if not self.configured:
            return self._rrf_fallback(sources, top_n)
        async with measure("rerank", "rerank"):
            try:
                return await self._rerank_api(query, sources, top_n)
            except Exception:  # noqa: BLE001
                # rerank 调用失败，降级 RRF，不阻断主流程
                return self._rrf_fallback(sources, top_n)

    # ------------------------------------------------------------------ #
    # rerank API（Cohere / Jina 兼容）
    # ------------------------------------------------------------------ #
    async def _rerank_api(
        self, query: str, sources: list[SourceItem], top_n: int
    ) -> list[SourceItem]:
        import httpx

        documents = [s.content for s in sources]
        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.post(
                self.api_base.rstrip("/") + "/rerank",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "query": query,
                    "documents": documents,
                    "top_n": min(top_n, len(documents)),
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results", [])
        fused: list[SourceItem] = []
        for r in results:
            idx = r.get("index")
            score = r.get("relevance_score")
            if idx is not None and 0 <= idx < len(sources):
                item = sources[idx].model_copy()
                item.score = score
                fused.append(item)
        return fused

    # ------------------------------------------------------------------ #
    # RRF 兜底（无模型融合）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _rrf_fallback(sources: list[SourceItem], top_n: int) -> list[SourceItem]:
        """按检索路分组，组内按分数排序，跨组用 RRF 融合取 top_n。"""
        groups: dict[str, list[SourceItem]] = defaultdict(list)
        for s in sources:
            groups[s.source].append(s)
        # ES 路按 score 降序；KG 路保持原序
        for key, items in groups.items():
            if key in ("article", "qna"):
                items.sort(key=lambda x: x.score or 0, reverse=True)

        scored: list[tuple[float, SourceItem]] = []
        for items in groups.values():
            for rank, s in enumerate(items):
                rrf = 1.0 / (_RRF_K + rank)
                scored.append((rrf, s))
        scored.sort(key=lambda x: x[0], reverse=True)

        limit = min(top_n, len(scored))
        return [s for _, s in scored[:limit]]

    # ------------------------------------------------------------------ #
    # 可用性验证
    # ------------------------------------------------------------------ #
    async def validate(self) -> dict:
        if not self.configured:
            return {
                "ok": False,
                "message": "rerank 未完整配置（缺 api_base/api_key/model），merge 模式将使用 RRF 兜底",
            }
        try:
            from app.schemas.responses import SourceItem as SI

            await self._rerank_api("ping", [SI(source="test", content="test")], 1)
            return {"ok": True, "message": f"rerank 可用: {self.model} @ {self.api_base}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"验证失败: {exc}"}
