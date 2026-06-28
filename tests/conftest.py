"""pytest 全局配置与 mock 工具。"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import AsyncIterator

import pytest

from app.api import dependencies as deps
from app.core.config_manager import ConfigManager
from app.retrieval.orchestrator import Orchestrator
from app.schemas.responses import SourceItem

SENSITIVE_PLAIN = "super-secret-key"


@pytest.fixture()
def tmp_config_file(tmp_path: Path) -> Path:
    enc = "b64:" + base64.b64encode(SENSITIVE_PLAIN.encode()).decode()

    def llm_item(name, api_base="https://api.openai.com/v1"):
        return {
            "name": name, "api_base": api_base, "api_key": enc,
            "model": "gpt-4o-mini", "entity_model": "gpt-4o-mini",
            "temperature": 0.7, "max_tokens": 2048,
        }

    config = {
        "llm": {"active": "default", "items": {
            "default": llm_item("默认"),
            "backup": llm_item("备用", "https://backup.local/v1"),
        }},
        "elasticsearch": {"active": "default", "items": {
            "default": {"name": "默认", "url": "http://localhost:9200", "username": "elastic",
                        "password": enc, "article_index": "article",
                        "article_fields": {"body": "content", "title": "title"},
                        "qna_index": "qna",
                        "qna_fields": {"body": "answer", "title": "question"},
                        "verify_certs": False},
        }},
        "neo4j": {"active": "default", "items": {
            "default": {"name": "默认", "uri": "bolt://localhost:7687", "username": "neo4j", "password": enc,
                        "database": "neo4j", "node_key": "name", "max_neighbors": 10,
                        "excluded_relations": ["contains", "references"]},
        }},
        "rerank": {"active": "default", "items": {
            "default": {"name": "默认", "api_base": "", "api_key": "", "model": "", "top_n": 5},
        }},
        "embedding": {"active": "default", "items": {
            "default": {"name": "默认", "api_base": "", "api_key": enc, "model": "text-embedding-3-small"},
        }},
        "milvus": {"active": "default", "items": {
            "default": {"name": "默认", "uri": "http://localhost:19530", "collection_name": "documents",
                        "vector_field": "embedding", "text_field": "content", "metric_type": "COSINE"},
        }},
        "server": {"host": "0.0.0.0", "port": 8000},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture()
def config_manager(tmp_config_file: Path) -> ConfigManager:
    return ConfigManager(config_path=tmp_config_file)


# ---------------------------------------------------------------------- #
# Mock 客户端
# ---------------------------------------------------------------------- #
class MockLLMClient:
    def __init__(self, *args, **kwargs):
        pass

    async def stream_chat(self, query, context="", history=None) -> AsyncIterator[str]:
        for word in ("这是", "一段", "模拟", "回答。"):
            yield word

    async def chat(self, query, context="", history=None) -> str:
        return "这是一段模拟回答。"

    async def extract_entities(self, query: str) -> list[str]:
        return [w for w in ["实体A", "实体B"] if w]

    async def validate(self) -> dict:
        return {"ok": True, "message": "mock ok"}


class MockESClient:
    async def search_article(self, query, top_k=5):
        return [SourceItem(source="article", title="文章1", content=f"文章:{query}", score=1.2)]

    async def search_qna(self, query, top_k=5):
        return [SourceItem(source="qna", title="问题1", content=f"问答:{query}", score=0.9)]

    async def close(self):
        pass

    async def validate(self) -> dict:
        return {"ok": True, "message": "mock ok"}


class MockKGClient:
    async def search_by_entities(self, entities, top_k=5):
        return [SourceItem(source="kg", title=e, content=f"图谱:{e}") for e in entities]

    async def close(self):
        pass

    async def validate(self) -> dict:
        return {"ok": True, "message": "mock ok"}


class MockRerankClient:
    async def rerank(self, query, sources, default_top_n=5):
        return sources[:default_top_n]

    async def validate(self) -> dict:
        return {"ok": True, "message": "mock ok"}


class MockEmbeddingClient:
    async def embed(self, text):
        return [0.1, 0.2, 0.3]

    async def validate(self) -> dict:
        return {"ok": True, "message": "mock ok"}


class MockVecClient:
    """mock vec：模拟多路检索——原始 query 返回 1 条，每个"主题"各返回 1 条。"""

    def __init__(self, *args, **kwargs):
        pass

    async def search(self, query, top_k=5):
        # 模拟多路检索效果：返回 query 本身 + 按 query 拆分后的多个结果
        # 这里简化为返回多条不同 content 的结果
        return [
            SourceItem(source="vec", title=None, content=f"向量:{query}", score=0.95),
            SourceItem(source="vec", title=None, content=f"向量相关:主题1", score=0.88),
        ][:top_k]

    async def close(self):
        pass

    async def validate(self) -> dict:
        return {"ok": True, "message": "mock ok"}


@pytest.fixture()
def mock_clients(monkeypatch):
    """用 mock 替换依赖注入中的真实客户端。"""
    monkeypatch.setattr(deps, "get_llm_client", lambda: MockLLMClient())
    monkeypatch.setattr(deps, "get_es_client", lambda: MockESClient())
    monkeypatch.setattr(deps, "get_kg_client", lambda: MockKGClient())
    monkeypatch.setattr(deps, "get_rerank_client", lambda: MockRerankClient())
    monkeypatch.setattr(deps, "get_embedding_client", lambda: MockEmbeddingClient())
    monkeypatch.setattr(deps, "get_vec_client", lambda: MockVecClient())
    monkeypatch.setattr(
        deps,
        "get_orchestrator",
        lambda: Orchestrator(
            MockLLMClient(), MockESClient(), MockKGClient(),
            MockRerankClient(), MockVecClient(),
        ),
    )


# ---------------------------------------------------------------------- #
# FastAPI 测试客户端
# ---------------------------------------------------------------------- #
@pytest.fixture()
def client(mock_clients, config_manager):
    from fastapi.testclient import TestClient

    deps.set_config_manager(config_manager)

    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c
