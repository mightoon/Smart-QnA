"""RAG 调度逻辑。

根据请求模式 (`article` / `qna` / `kg` / `vec` / `all` / `merge` / 无) 决定检索策略：
* `article`：仅检索 ES 文章索引
* `qna`：仅检索 ES 问答索引
* `kg`：先抽取实体，再检索 Neo4j 图谱
* `vec`：将 query 向量化后检索 Milvus 向量数据库
* `all`：并行执行 article + qna + kg（不含 vec），结果直接拼接
* `merge`：并行四路检索（含 vec）后，经 rerank 融合取 top_n
* 无：不检索，纯 LLM 对话

检索结果汇总为上下文，交给 LLM 流式生成答案，并以 SSE 事件流返回。
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Optional

from app.core.llm_client import LLMClient
from app.core.metrics import measure
from app.retrieval.es_client import ESClient
from app.retrieval.kg_client import KGClient
from app.retrieval.rerank_client import RerankClient
from app.retrieval.vec_client import VecClient
from app.schemas.requests import ChatMode, ChatRequest
from app.schemas.responses import ChatResult, SourceItem


class Orchestrator:
    """RAG 调度器。"""

    def __init__(
        self,
        llm_client: LLMClient,
        es_client: ESClient,
        kg_client: KGClient,
        rerank_client: RerankClient,
        vec_client: VecClient,
    ):
        self.llm = llm_client
        self.es = es_client
        self.kg = kg_client
        self.rerank = rerank_client
        self.vec = vec_client

    # ------------------------------------------------------------------ #
    # 非流式
    # ------------------------------------------------------------------ #
    async def run(self, request: ChatRequest) -> ChatResult:
        async with measure("chat", "request") as m:
            sources, entities = await self._retrieve(request)
            context = self._build_context(sources)
            answer = await self.llm.chat(
                query=request.query, context=context, history=self._history(request)
            )
            m["tokens"] = 0  # token 由 LLM 内部埋点记录
        return ChatResult(
            answer=answer,
            sources=sources,
            entities=entities,
            mode=request.mode.value if request.mode else None,
        )

    # ------------------------------------------------------------------ #
    # 流式 (SSE)
    # ------------------------------------------------------------------ #
    async def run_stream(self, request: ChatRequest) -> AsyncIterator[str]:
        async with measure("chat", "request"):
            sources, entities = await self._retrieve(request)
            context = self._build_context(sources)

            # 1) 先推送检索来源与实体（元信息事件）
            yield _sse("meta", {
                "mode": request.mode.value if request.mode else None,
                "entities": entities,
                "sources": [s.model_dump() for s in sources],
            })

            # 2) 流式推送模型 token
            async for token in self.llm.stream_chat(
                query=request.query, context=context, history=self._history(request)
            ):
                yield _sse("token", {"content": token})

            # 3) 结束事件
            yield _sse("done", {})

    # ------------------------------------------------------------------ #
    # 检索调度
    # ------------------------------------------------------------------ #
    async def _retrieve(self, request: ChatRequest) -> tuple[list[SourceItem], list[str]]:
        mode = request.mode
        top_k = request.top_k
        sources: list[SourceItem] = []
        entities: list[str] = []

        if mode is None:
            return sources, entities

        if mode == ChatMode.ALL:
            # all 模式：article + qna + kg（不含 vec）
            sources, entities = await self._three_way_retrieve(request.query, top_k)
        elif mode == ChatMode.MERGE:
            # merge 模式：四路检索（含 vec）+ rerank 融合
            sources, entities = await self._four_way_retrieve(request.query, top_k)
            sources = await self.rerank.rerank(request.query, sources, top_k)
        elif mode == ChatMode.VEC:
            sources = await self.vec.search(request.query, top_k)
        elif mode == ChatMode.ARTICLE:
            sources = await self.es.search_article(request.query, top_k)
        elif mode == ChatMode.QNA:
            sources = await self.es.search_qna(request.query, top_k)
        elif mode == ChatMode.KG:
            entities = await self._safe_entities(request.query)
            sources = await self.kg.search_by_entities(entities, top_k)

        return sources, entities

    async def _three_way_retrieve(
        self, query: str, top_k: int
    ) -> tuple[list[SourceItem], list[str]]:
        """三路并行检索（article + qna + kg），不含 vec。"""
        entity_task = asyncio.create_task(self._safe_entities(query))
        article_task = asyncio.create_task(self.es.search_article(query, top_k))
        qna_task = asyncio.create_task(self.es.search_qna(query, top_k))
        entities = await entity_task
        kg_task = asyncio.create_task(self.kg.search_by_entities(entities, top_k))
        results = await asyncio.gather(
            article_task, qna_task, kg_task, return_exceptions=True
        )
        sources: list[SourceItem] = []
        for res in results:
            sources.extend(_unwrap(res))
        return sources, entities

    async def _four_way_retrieve(
        self, query: str, top_k: int
    ) -> tuple[list[SourceItem], list[str]]:
        """四路并行检索（article + qna + kg + vec），用于 merge 模式。

        vec 检索与 ES 两路、实体抽取互相独立，可立即并行；
        KG 检索依赖实体抽取结果，待实体就绪后发起。
        """
        entity_task = asyncio.create_task(self._safe_entities(query))
        article_task = asyncio.create_task(self.es.search_article(query, top_k))
        qna_task = asyncio.create_task(self.es.search_qna(query, top_k))
        vec_task = asyncio.create_task(self.vec.search(query, top_k))
        entities = await entity_task
        kg_task = asyncio.create_task(self.kg.search_by_entities(entities, top_k))
        results = await asyncio.gather(
            article_task, qna_task, kg_task, vec_task, return_exceptions=True
        )
        sources: list[SourceItem] = []
        for res in results:
            sources.extend(_unwrap(res))
        return sources, entities

    async def _safe_entities(self, query: str) -> list[str]:
        """实体抽取失败时降级为空列表，不阻断主流程。"""
        try:
            return await self.llm.extract_entities(query)
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _history(request: ChatRequest) -> Optional[list[dict]]:
        if not request.history:
            return None
        return [m.model_dump() for m in request.history]

    # ------------------------------------------------------------------ #
    # 上下文组装
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_context(sources: list[SourceItem]) -> str:
        if not sources:
            return ""
        blocks: list[str] = []
        for idx, s in enumerate(sources, start=1):
            title = s.title or s.source
            blocks.append(
                f"[{idx}] (来源:{s.source} | 标题:{title})\n{s.content}"
            )
        return "\n\n".join(blocks)


# ---------------------------------------------------------------------- #
# 辅助函数
# ---------------------------------------------------------------------- #
def _unwrap(result) -> list[SourceItem]:
    """从 gather(return_exceptions=True) 的结果中安全取出列表。"""
    if isinstance(result, Exception):
        return []
    if isinstance(result, list):
        return result
    return []


def _sse(event: str, data: dict) -> str:
    """格式化为 SSE 事件字符串。"""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"
