"""Pydantic 输入模型。"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class ChatMode(str, Enum):
    """RAG 检索策略模式。"""

    ARTICLE = "article"
    QNA = "qna"
    KG = "kg"
    VEC = "vec"
    ALL = "all"
    MERGE = "merge"


class ChatMessage(BaseModel):
    role: str = Field(..., description="消息角色：user / assistant")
    content: str = Field(..., description="消息内容")


class ChatRequest(BaseModel):
    """`/api/chat` 请求体。"""

    query: str = Field(..., min_length=1, description="用户问题")
    mode: Optional[ChatMode] = Field(
        None, description="检索模式：article / qna / kg / all，缺省为纯对话"
    )
    top_k: int = Field(5, ge=1, le=50, description="每个来源返回的最多条数")
    stream: bool = Field(True, description="是否以 SSE 流式返回")
    cite_sources: bool = Field(True, description="回答中是否显示 [n] 来源标记")
    history: Optional[List[ChatMessage]] = Field(None, description="历史对话上下文")


class SetActiveRequest(BaseModel):
    """设置当前生效配置项。"""

    id: str = Field(..., description="配置项 ID")


class ServerConfigRequest(BaseModel):
    """server 段配置更新。"""

    host: Optional[str] = None
    port: Optional[int] = None
