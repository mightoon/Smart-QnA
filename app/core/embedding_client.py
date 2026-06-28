"""Embedding 向量化客户端。

调用 OpenAI 兼容的 /embeddings 接口，将文本转向量，
供 Milvus 向量检索使用。
"""

from __future__ import annotations

from typing import Optional

from openai import AsyncOpenAI, OpenAIError

from app.core.exceptions import LLMError
from app.core.metrics import measure


class EmbeddingClient:
    """Embedding 模型调用客户端（异步）。"""

    def __init__(self, config: dict):
        self.api_base = config.get("api_base", "")
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "text-embedding-3-small")
        self._client: Optional[AsyncOpenAI] = None

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            if not self.api_key:
                raise LLMError("Embedding api_key 未配置")
            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.api_base or None,
            )
        return self._client

    def reconfigure(self, config: dict) -> None:
        self.api_base = config.get("api_base", "")
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "text-embedding-3-small")
        self._client = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.model)

    async def embed(self, text: str) -> list[float]:
        """将单段文本转向量，返回浮点列表。"""
        async with measure("embedding", "embed") as m:
            try:
                resp = await self.client.embeddings.create(
                    model=self.model,
                    input=text,
                )
                m["tokens"] = resp.usage.total_tokens if resp.usage else 0
                return resp.data[0].embedding
            except OpenAIError as exc:
                raise LLMError(f"Embedding 调用失败: {exc}") from exc
            except Exception as exc:  # noqa: BLE001
                raise LLMError(f"Embedding 调用异常: {exc}") from exc

    async def validate(self) -> dict:
        if not self.configured:
            return {"ok": False, "message": "embedding 未完整配置（缺 api_key/model）"}
        try:
            vec = await self.embed("ping")
            return {"ok": True, "message": f"embedding 可用: {self.model} (dim={len(vec)})"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"验证失败: {exc}"}
