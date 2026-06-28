"""Pydantic 输出模型。"""

from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, Field


class SourceItem(BaseModel):
    """单条检索来源。"""

    source: str = Field(..., description="来源类型：article / qna / kg")
    title: Optional[str] = Field(None, description="标题或实体名")
    content: str = Field("", description="命中内容片段")
    score: Optional[float] = Field(None, description="相关度分数")
    meta: Optional[dict] = Field(None, description="附加元数据")


class ChatResult(BaseModel):
    """非流式对话返回结果。"""

    answer: str = Field(..., description="模型回答")
    sources: List[SourceItem] = Field(default_factory=list, description="检索来源")
    entities: List[str] = Field(default_factory=list, description="抽取的实体")
    mode: Optional[str] = Field(None, description="实际使用的检索模式")


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = ""
    services: dict = Field(default_factory=dict)


class ValidateResult(BaseModel):
    ok: bool
    message: str = ""


class ErrorResponse(BaseModel):
    error: dict
