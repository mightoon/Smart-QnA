"""FastAPI 应用入口。

挂载 API 路由与 UI 静态资源，并注册全局异常处理。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import dependencies as deps
from app.api.routes import router as api_router
from app.core.exceptions import AppException

# UI 静态目录（项目根 /ui）。
UI_DIR = Path(__file__).resolve().parent.parent / "ui"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：预构造核心组件，提前暴露配置错误。
    try:
        deps.get_config_manager()
        deps.get_orchestrator()
    except AppException:
        # 启动期配置缺失不应直接崩溃，允许通过 UI 修正配置。
        pass
    yield
    # 关闭：释放外部连接。
    try:
        await deps.get_es_client().close()
    except Exception:  # noqa: BLE001
        pass
    try:
        await deps.get_kg_client().close()
    except Exception:  # noqa: BLE001
        pass


def create_app() -> FastAPI:
    app = FastAPI(
        title="Smart QnA API",
        description="基于 RAG 的智能问答后端服务",
        version=__version__,
        lifespan=lifespan,
    )

    # 全局异常处理
    @app.exception_handler(AppException)
    async def app_exception_handler(request: Request, exc: AppException):
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    # API 路由
    app.include_router(api_router)

    # UI 静态资源（禁用缓存，确保修改后浏览器获取最新文件）
    if UI_DIR.exists():
        app.mount(
            "/",
            StaticFiles(directory=str(UI_DIR), html=True),
            name="ui",
        )

    @app.middleware("http")
    async def no_cache_static(request: Request, call_next):
        response = await call_next(request)
        # 对 js/css/html 禁用缓存
        if request.url.path.endswith((".js", ".css", ".html")):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host=host, port=port, reload=False)
