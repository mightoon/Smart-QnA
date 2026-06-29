"""Elasticsearch 交互逻辑。

针对 `article` 与 `qna` 两个索引提供关键字检索（BM25），
返回统一结构的命中结果。使用官方 async client。

检索前会加载外部同义词词典（data/synonyms.json），对 query 做双向同义词扩展：
用户问题中包含词典中任一词，自动扩展为该同义词组的全部词，以 OR 组合查询。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from app.core.exceptions import ESConnectionError, RetrievalError
from app.core.metrics import measure
from app.schemas.responses import SourceItem

# 不同索引的字段映射默认值（可在配置中覆盖）。
_DEFAULT_INDEX_FIELDS = {
    "article": {"body": "content", "title": "title"},
    "qna": {"body": "answer", "title": "question"},
}

# 同义词词典文件路径
_SYNONYM_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "synonyms.json"
# 双向同义词映射缓存：word -> [所有同义词（含自身）]
_synonym_map: Optional[dict[str, list[str]]] = None


def _load_synonyms() -> dict[str, list[str]]:
    """加载同义词词典，展开为双向映射。

    词典格式（synonyms.json）：
        {"微调": ["fine-tuning", "SFT"], "大模型": ["LLM", "大语言模型"]}

    展开后双向映射：
        "微调" -> ["微调", "fine-tuning", "SFT"]
        "fine-tuning" -> ["微调", "fine-tuning", "SFT"]
        "SFT" -> ["微调", "fine-tuning", "SFT"]
        "大模型" -> ["大模型", "LLM", "大语言模型"]
        ...
    """
    global _synonym_map
    if _synonym_map is not None:
        return _synonym_map

    _synonym_map = {}
    if not _SYNONYM_PATH.exists():
        return _synonym_map

    try:
        raw = json.loads(_SYNONYM_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _synonym_map

    # 每组同义词（key + values）构成一个等价组，组内每个词都映射到全组
    for key, syns in raw.items():
        group = [key] + [s for s in syns if s]
        for word in group:
            # 避免重复词；一个词可能出现在多个组中，合并
            existing = _synonym_map.get(word, [])
            for g in group:
                if g not in existing:
                    existing.append(g)
            _synonym_map[word] = existing

    return _synonym_map


def _expand_query(query: str) -> str:
    """用双向同义词词典扩展 query，返回 OR 组合的查询字符串。

    遍历词典中的每个词，若出现在 query 中，则将该词的全部同义词加入查询。
    原始 query 始终保留（作为整体短语匹配）。
    """
    syn_map = _load_synonyms()
    if not syn_map:
        return query

    expanded = {query}  # 保留原始完整 query
    for word, syns in syn_map.items():
        if word in query:
            expanded.update(syns)

    return " OR ".join(expanded)


class ESClient:
    """Elasticsearch 异步客户端封装。"""

    def __init__(self, es_config: dict):
        self.url = es_config.get("url", "http://localhost:9200")
        self.version = es_config.get("version", "v8")
        self.username = es_config.get("username", "")
        self.password = es_config.get("password", "")
        self.article_index = es_config.get("article_index", "article")
        self.qna_index = es_config.get("qna_index", "qna")
        self.verify_certs = bool(es_config.get("verify_certs", False))
        # 字段映射从配置读取，未配置则用默认值
        self.article_fields = es_config.get("article_fields") or _DEFAULT_INDEX_FIELDS["article"]
        self.qna_fields = es_config.get("qna_fields") or _DEFAULT_INDEX_FIELDS["qna"]
        self._client: Any = None

    # ------------------------------------------------------------------ #
    @property
    def client(self):
        if self._client is None:
            try:
                from elasticsearch import AsyncElasticsearch
            except ImportError as exc:  # pragma: no cover
                raise RetrievalError("未安装 elasticsearch 依赖") from exc
            kwargs: dict = {"hosts": [self.url]}
            if self.username:
                kwargs["basic_auth"] = (self.username, self.password)
            kwargs["verify_certs"] = self.verify_certs
            kwargs["ssl_show_warn"] = False
            self._client = AsyncElasticsearch(**kwargs)
        return self._client

    def reconfigure(self, es_config: dict) -> None:
        self.url = es_config.get("url", "http://localhost:9200")
        self.version = es_config.get("version", "v8")
        self.username = es_config.get("username", "")
        self.password = es_config.get("password", "")
        self.article_index = es_config.get("article_index", "article")
        self.qna_index = es_config.get("qna_index", "qna")
        self.verify_certs = bool(es_config.get("verify_certs", False))
        self.article_fields = es_config.get("article_fields") or _DEFAULT_INDEX_FIELDS["article"]
        self.qna_fields = es_config.get("qna_fields") or _DEFAULT_INDEX_FIELDS["qna"]
        self._client = None

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    # ------------------------------------------------------------------ #
    # 可用性验证
    # ------------------------------------------------------------------ #
    async def validate(self) -> dict:
        if not self.url:
            return {"ok": False, "message": "url 未配置"}
        try:
            info = await self.client.info()
            ver = (info.get("version") or {}).get("number", "?")
            name = (info.get("cluster_name") or "")
            return {"ok": True, "message": f"连接成功: ES {ver} {name}".strip()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"验证失败: {exc}"}
        finally:
            await self.close()

    # ------------------------------------------------------------------ #
    async def search_article(self, query: str, top_k: int = 5) -> list[SourceItem]:
        return await self._search(self.article_index, self.article_fields, "article", query, top_k)

    async def search_qna(self, query: str, top_k: int = 5) -> list[SourceItem]:
        return await self._search(self.qna_index, self.qna_fields, "qna", query, top_k)

    async def _search(
        self, index: str, fields: dict, source_type: str, query: str, top_k: int
    ) -> list[SourceItem]:
        expanded_query = _expand_query(query)
        body = {
            "size": top_k,
            "query": {
                "multi_match": {
                    "query": expanded_query,
                    "fields": [f"{fields['body']}^3", fields["title"]],
                    "type": "best_fields",
                }
            },
        }
        async with measure("es", f"search_{source_type}"):
            try:
                resp = await self.client.search(index=index, body=body)
            except Exception as exc:  # noqa: BLE001
                raise ESConnectionError(f"Elasticsearch 检索失败 ({index}): {exc}") from exc

        items: list[SourceItem] = []
        hits = resp.get("hits", {}).get("hits", [])
        for hit in hits:
            src = hit.get("_source", {}) or {}
            items.append(
                SourceItem(
                    source=source_type,
                    title=src.get(fields["title"]) or src.get("title"),
                    content=str(src.get(fields["body"], "")),
                    score=hit.get("_score"),
                    meta={"_id": hit.get("_id"), "index": index},
                )
            )
        return items
