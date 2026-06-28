"""自定义异常与全局错误码定义。"""

from typing import Any, Optional


class AppException(Exception):
    """所有应用异常的基类。

    携带 HTTP 状态码与可选的详细上下文，便于在路由层统一转换为
    结构化错误响应。
    """

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, status_code: Optional[int] = None,
                 detail: Optional[Any] = None):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        self.detail = detail

    def to_dict(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "detail": self.detail,
            }
        }


class ConfigError(AppException):
    status_code = 500
    code = "config_error"


class ConfigNotFoundError(ConfigError):
    status_code = 404
    code = "config_not_found"


class LLMError(AppException):
    status_code = 502
    code = "llm_error"


class RetrievalError(AppException):
    status_code = 502
    code = "retrieval_error"


class ValidationError(AppException):
    status_code = 422
    code = "validation_error"


class ESConnectionError(RetrievalError):
    code = "es_connection_error"


class KGConnectionError(RetrievalError):
    code = "kg_connection_error"
