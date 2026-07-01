"""大模型统一调用封装。

基于 `openai` SDK（兼容 OpenAI 格式的各类大模型），提供：
* 流式 / 非流式对话补全
* 实体抽取（用于知识图谱检索）
"""

from __future__ import annotations

import json
import re
from typing import AsyncIterator, Optional

from openai import AsyncOpenAI, OpenAIError

from app.core.exceptions import LLMError
from app.core.metrics import measure

# 实体抽取的系统提示词，要求模型返回严格的 JSON。
ENTITY_EXTRACTION_PROMPT = (
    "你是一个实体抽取助手。从用户问题中提取用于知识图谱检索的核心实体（如人名、"
    "组织、产品、技术、地点、概念等）。\n"
    "只返回 JSON，不要任何额外说明，格式为：\n"
    '{"entities": ["实体1", "实体2"]}\n'
    "如果没有明显实体，返回 {\"entities\": []}。"
)

# RAG 答题系统提示词（仅在有检索上下文时使用）。
RAG_SYSTEM_PROMPT = (
    "你是一个严谨的智能问答助手。请根据下方【参考资料】回答用户问题。\n"
    "要求：\n"
    "1. 仅依据参考资料作答，不要编造；若资料不足，请明确说明。\n"
    "2. 在关键信息后用 [n] 标注引用来源编号。\n"
    "3. 回答简洁、条理清晰。"
)

# 纯对话系统提示词（无检索上下文时使用，允许模型自由作答）。
CHAT_SYSTEM_PROMPT = (
    "你是一个智能问答助手，请友好、准确地回答用户的问题。"
)


def _parse_extra(value: object) -> dict:
    """将 extra 字段解析为 dict，兼容字符串（JSON）和 dict 输入。"""
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


class LLMClient:
    """大模型调用客户端（异步）。"""

    def __init__(self, llm_config: dict):
        self.api_base = llm_config.get("api_base", "")
        self.api_key = llm_config.get("api_key", "")
        self.model = llm_config.get("model", "gpt-4o-mini")
        self.entity_model = llm_config.get("entity_model", self.model)
        # temperature / max_tokens 允许空值，空时用默认值
        _temp = llm_config.get("temperature", 0.7)
        self.temperature = float(_temp) if _temp != "" and _temp is not None else 0.7
        _max_tok = llm_config.get("max_tokens", 2048)
        self.max_tokens = int(_max_tok) if _max_tok != "" and _max_tok is not None else 2048
        # extra: 附加调用参数（JSON 对象），支持字符串或 dict 输入
        self.extra = _parse_extra(llm_config.get("extra"))
        self._client: Optional[AsyncOpenAI] = None

    # ------------------------------------------------------------------ #
    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            # 本地模型可能无需 api_key，传占位值满足 SDK 要求
            self._client = AsyncOpenAI(
                api_key=self.api_key or "not-needed",
                base_url=self.api_base or None,
            )
        return self._client

    def reconfigure(self, llm_config: dict) -> None:
        """配置变更后重置内部 client。"""
        self.api_base = llm_config.get("api_base", "")
        self.api_key = llm_config.get("api_key", "")
        self.model = llm_config.get("model", "gpt-4o-mini")
        self.entity_model = llm_config.get("entity_model", self.model)
        _temp = llm_config.get("temperature", 0.7)
        self.temperature = float(_temp) if _temp != "" and _temp is not None else 0.7
        _max_tok = llm_config.get("max_tokens", 2048)
        self.max_tokens = int(_max_tok) if _max_tok != "" and _max_tok is not None else 2048
        self.extra = _parse_extra(llm_config.get("extra"))
        self._client = None

    # ------------------------------------------------------------------ #
    # 可用性验证
    # ------------------------------------------------------------------ #
    async def validate(self) -> dict:
        """发起一次极小请求验证模型可用性，返回 {ok, message}。"""
        try:
            await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=8,
                temperature=0,
                stream=False,
                extra_body=self.extra or None,
            )
            return {"ok": True, "message": f"模型可用: {self.model} @ {self.api_base or '默认'}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"验证失败: {exc}"}

    # ------------------------------------------------------------------ #
    # 对话补全
    # ------------------------------------------------------------------ #
    async def stream_chat(
        self,
        query: str,
        context: str = "",
        history: Optional[list[dict]] = None,
    ) -> AsyncIterator[str]:
        """流式对话补全，逐 token 产出文本片段。"""
        messages = self._build_messages(query, context, history)
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        # 附加参数（如 chat_template_kwargs）通过 extra_body 传递，
        # 避免被 SDK 的 create() 方法签名校验拒绝
        extra_body = self.extra or None
        self._log_request("stream_chat", body)
        async with measure("llm", "stream_chat"):
            try:
                stream = await self.client.chat.completions.create(**body, extra_body=extra_body)
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    content = getattr(delta, "content", None)
                    if content:
                        yield content
            except OpenAIError as exc:
                raise LLMError(f"大模型调用失败: {exc}") from exc
            except Exception as exc:  # noqa: BLE001
                raise LLMError(f"大模型调用异常: {exc}") from exc

    async def chat(
        self,
        query: str,
        context: str = "",
        history: Optional[list[dict]] = None,
    ) -> str:
        """非流式对话补全，返回完整文本。"""
        chunks: list[str] = []
        async for token in self.stream_chat(query, context, history):
            chunks.append(token)
        return "".join(chunks)

    # ------------------------------------------------------------------ #
    # 实体抽取
    # ------------------------------------------------------------------ #
    async def extract_entities(self, query: str) -> list[str]:
        """调用 LLM 从 query 中抽取核心实体，返回实体列表。"""
        messages = [
            {"role": "system", "content": ENTITY_EXTRACTION_PROMPT},
            {"role": "user", "content": query},
        ]
        body = {
            "model": self.entity_model,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": 256,
            "stream": False,
        }
        extra_body = self.extra or None
        self._log_request("extract_entities", body)
        async with measure("llm", "extract_entities") as m:
            try:
                resp = await self.client.chat.completions.create(**body, extra_body=extra_body)
                m["tokens"] = resp.usage.total_tokens if resp.usage else 0
                text = resp.choices[0].message.content or ""
            except OpenAIError as exc:
                raise LLMError(f"实体抽取失败: {exc}") from exc
            except Exception as exc:  # noqa: BLE001
                raise LLMError(f"实体抽取异常: {exc}") from exc
        return self._parse_entities(text)

    @staticmethod
    def _parse_entities(text: str) -> list[str]:
        """从模型输出中解析实体 JSON，容错处理。"""
        text = text.strip()
        # 尝试直接解析
        try:
            data = json.loads(text)
            return [str(e) for e in data.get("entities", []) if e]
        except json.JSONDecodeError:
            pass
        # 尝试抽取首个 JSON 对象
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
                return [str(e) for e in data.get("entities", []) if e]
            except json.JSONDecodeError:
                pass
        return []

    # ------------------------------------------------------------------ #
    def _log_request(self, tag: str, body: dict) -> None:
        """打印发往大模型的完整请求体与连接信息，便于排查。"""
        key_preview = (self.api_key[:8] + "...") if self.api_key else "(空)"
        print(
            "\n========== [LLM 请求] %s ==========\n"
            "base_url : %s\n"
            "api_key  : %s (len=%d)\n"
            "POST body:\n%s\n"
            "===============================================\n"
            % (
                tag,
                self.api_base or "(默认 OpenAI)",
                key_preview,
                len(self.api_key),
                json.dumps(body, ensure_ascii=False, indent=2),
            ),
            flush=True,
        )

    # ------------------------------------------------------------------ #
    def _build_messages(
        self,
        query: str,
        context: str,
        history: Optional[list[dict]],
    ) -> list[dict]:
        messages: list[dict] = []
        if context:
            messages.append({
                "role": "system",
                "content": f"{RAG_SYSTEM_PROMPT}\n\n【参考资料】\n{context}",
            })
        else:
            messages.append({"role": "system", "content": CHAT_SYSTEM_PROMPT})
        if history:
            for item in history[-10:]:
                role = item.get("role")
                content = item.get("content")
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": query})
        return messages
