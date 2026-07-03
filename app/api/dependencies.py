"""依赖注入：构造并复用单例服务组件。"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from app.core.config_manager import ConfigManager
from app.core.exceptions import AppException
from app.core.llm_client import LLMClient
from app.core.embedding_client import EmbeddingClient
from app.retrieval.es_client import ESClient
from app.retrieval.kg_client import KGClient
from app.retrieval.ng_client import NGClient
from app.retrieval.orchestrator import Orchestrator
from app.retrieval.rerank_client import RerankClient
from app.retrieval.vec_client import VecClient

_config_manager: Optional[ConfigManager] = None


def get_config_manager() -> ConfigManager:
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager()
    return _config_manager


def set_config_manager(manager: ConfigManager) -> None:
    global _config_manager
    _config_manager = manager


@lru_cache(maxsize=1)
def get_llm_client() -> LLMClient:
    cfg = get_config_manager().get_active_item("llm")
    return LLMClient(cfg)


@lru_cache(maxsize=1)
def get_es_client() -> ESClient:
    cfg = get_config_manager().get_active_item("elasticsearch")
    return ESClient(cfg)


@lru_cache(maxsize=1)
def get_kg_client() -> KGClient:
    cfg = get_config_manager().get_active_item("neo4j")
    return KGClient(cfg)


@lru_cache(maxsize=1)
def get_ng_client() -> NGClient:
    cfg = get_config_manager().get_active_item("nebula")
    return NGClient(cfg, get_llm_client())


def get_graph_client():
    """根据配置中的 graph_db 选择返回 Neo4j 或 Nebula 客户端。"""
    config = get_config_manager().get()
    graph_db = config.get("graph_db", "neo4j")
    if graph_db == "nebula":
        return get_ng_client()
    return get_kg_client()


@lru_cache(maxsize=1)
def get_rerank_client() -> RerankClient:
    try:
        cfg = get_config_manager().get_active_item("rerank")
    except AppException:
        cfg = {}
    return RerankClient(cfg)


@lru_cache(maxsize=1)
def get_embedding_client() -> EmbeddingClient:
    try:
        cfg = get_config_manager().get_active_item("embedding")
    except AppException:
        cfg = {}
    return EmbeddingClient(cfg)


@lru_cache(maxsize=1)
def get_vec_client() -> VecClient:
    try:
        milvus_cfg = get_config_manager().get_active_item("milvus")
    except AppException:
        milvus_cfg = {}
    return VecClient(milvus_cfg, get_embedding_client(), get_llm_client())


@lru_cache(maxsize=1)
def get_orchestrator() -> Orchestrator:
    return Orchestrator(
        get_llm_client(),
        get_es_client(),
        get_graph_client(),
        get_rerank_client(),
        get_vec_client(),
    )


def rebuild_clients() -> None:
    """配置更新后重建各客户端。"""
    for fn in (
        get_llm_client, get_es_client, get_kg_client, get_ng_client,
        get_rerank_client, get_embedding_client, get_vec_client,
        get_orchestrator,
    ):
        clear = getattr(fn, "cache_clear", None)
        if callable(clear):
            clear()
    try:
        get_orchestrator()
    except Exception:  # noqa: BLE001
        pass
