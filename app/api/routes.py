"""核心 API 路由定义。

* `POST /api/chat`                 —— 智能问答（默认 SSE 流式）
* `GET  /api/health`               —— 健康检查
* `GET  /api/config`               —— 读取全部配置（脱敏）
* `POST /api/config/{s}/items`     —— 新增配置项
* `PUT  /api/config/{s}/items/{id}`—— 修改配置项
* `DELETE /api/config/{s}/items/{id}` —— 删除配置项
* `PUT  /api/config/{s}/active`    —— 切换生效配置项
* `POST /api/config/{s}/validate`  —— 验证配置项可用性
* `PUT  /api/config/server`        —— 更新 server 段
"""

from __future__ import annotations

import json
import re
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from app.api import dependencies as deps
from app.core.config_manager import ITEM_SECTIONS, ConfigManager
from app.core.exceptions import AppException
from app.core.llm_client import LLMClient
from app.core.embedding_client import EmbeddingClient
from app.core.metrics import get_metrics
from app.retrieval.es_client import ESClient
from app.retrieval.kg_client import KGClient
from app.retrieval.ng_client import NGClient
from app.retrieval.rerank_client import RerankClient
from app.retrieval.vec_client import VecClient
from app.schemas.requests import ChatRequest, ServerConfigRequest, SetActiveRequest
from app.schemas.responses import ChatResult, HealthResponse

router = APIRouter(prefix="/api", tags=["smart-qna"])


# ---------------------------------------------------------------------- #
# 健康检查
# ---------------------------------------------------------------------- #
@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    from app import __version__

    return HealthResponse(status="ok", version=__version__, services={})


# ---------------------------------------------------------------------- #
# 智能问答
# ---------------------------------------------------------------------- #
@router.post("/chat")
async def chat(req: ChatRequest):
    orchestrator = deps.get_orchestrator()
    if req.stream:
        return StreamingResponse(
            _chat_stream(orchestrator, req),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )
    try:
        result: ChatResult = await orchestrator.run(req)
    except AppException as exc:
        raise _to_http(exc)
    return JSONResponse(result.model_dump())


async def _chat_stream(orchestrator, req: ChatRequest) -> AsyncIterator[bytes]:
    try:
        async for event in orchestrator.run_stream(req):
            yield event.encode("utf-8")
    except AppException as exc:
        err = json.dumps({"error": exc.to_dict()["error"]}, ensure_ascii=False)
        yield f"event: error\ndata: {err}\n\n".encode("utf-8")
    except Exception as exc:  # noqa: BLE001
        err = json.dumps({"error": {"code": "internal_error", "message": str(exc)}},
                         ensure_ascii=False)
        yield f"event: error\ndata: {err}\n\n".encode("utf-8")


# ---------------------------------------------------------------------- #
# 配置管理
# ---------------------------------------------------------------------- #
@router.get("/config")
async def get_config() -> JSONResponse:
    manager = deps.get_config_manager()
    return JSONResponse(manager.masked())


@router.post("/config/{section}/items")
async def create_item(section: str, body: dict):
    _check_section(section)
    manager = deps.get_config_manager()
    item_id = _gen_item_id(manager, section, body.get("name") or "item")
    try:
        manager.upsert_item(section, item_id, body, create=True)
        deps.rebuild_clients()
    except AppException as exc:
        raise _to_http(exc)
    return JSONResponse(manager.masked_section(section))


@router.put("/config/{section}/items/{item_id}")
async def update_item(section: str, item_id: str, body: dict):
    _check_section(section)
    manager = deps.get_config_manager()
    try:
        manager.upsert_item(section, item_id, body, create=False)
        deps.rebuild_clients()
    except AppException as exc:
        raise _to_http(exc)
    return JSONResponse(manager.masked_section(section))


@router.delete("/config/{section}/items/{item_id}")
async def delete_item(section: str, item_id: str):
    _check_section(section)
    manager = deps.get_config_manager()
    try:
        manager.delete_item(section, item_id)
        deps.rebuild_clients()
    except AppException as exc:
        raise _to_http(exc)
    return JSONResponse(manager.masked_section(section))


@router.put("/config/{section}/active")
async def set_active(section: str, payload: SetActiveRequest):
    _check_section(section)
    manager = deps.get_config_manager()
    try:
        manager.set_active(section, payload.id)
        deps.rebuild_clients()
    except AppException as exc:
        raise _to_http(exc)
    return JSONResponse(manager.masked_section(section))


@router.post("/config/{section}/validate")
async def validate_item(section: str, body: dict):
    """验证配置项可用性。

    body 可包含 `id`（验证已存储项，使用真实凭据）或直接内联字段（验证草稿）。
    """
    _check_section(section)
    manager = deps.get_config_manager()
    item_id = body.get("id")
    fields = {k: v for k, v in body.items() if k != "id"}
    try:
        if item_id:
            if fields:
                config = manager.resolve_item(section, item_id, fields)
            else:
                config = manager.get_item(section, item_id)
        else:
            config = fields
    except AppException as exc:
        raise _to_http(exc)
    result = await _validate_section(section, config)
    return JSONResponse(result)


@router.put("/config/server")
async def update_server(payload: ServerConfigRequest):
    manager = deps.get_config_manager()
    fields = payload.model_dump(exclude_none=True)
    try:
        manager.update_server(fields)
    except AppException as exc:
        raise _to_http(exc)
    return JSONResponse(manager.masked_section("server"))


@router.put("/config/graph_db")
async def update_graph_db(body: dict):
    """切换图数据库类型（neo4j / nebula）。"""
    manager = deps.get_config_manager()
    graph_db = body.get("graph_db", "neo4j")
    if graph_db not in ("neo4j", "nebula"):
        raise HTTPException(status_code=400, detail="graph_db 必须为 neo4j 或 nebula")
    config = manager.get()
    config["graph_db"] = graph_db
    manager.save(config)
    deps.rebuild_clients()
    return JSONResponse({"graph_db": graph_db})


# ---------------------------------------------------------------------- #
# 辅助
# ---------------------------------------------------------------------- #
def _check_section(section: str) -> None:
    if section not in ITEM_SECTIONS:
        raise HTTPException(status_code=404, detail=f"未知配置段: {section}")


def _gen_item_id(manager: ConfigManager, section: str, name: str) -> str:
    """由名称生成唯一配置项 ID（slug）。"""
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(name).strip()).strip("-").lower() or "item"
    try:
        items = (manager.get_section(section).get("items") or {})
    except AppException:
        items = {}
    item_id = base
    n = 2
    while item_id in items:
        item_id = f"{base}-{n}"
        n += 1
    return item_id


async def _validate_section(section: str, config: dict) -> dict:
    if section == "llm":
        return await LLMClient(config).validate()
    if section == "elasticsearch":
        return await ESClient(config).validate()
    if section == "neo4j":
        return await KGClient(config).validate()
    if section == "nebula":
        return await NGClient(config).validate()
    if section == "rerank":
        return await RerankClient(config).validate()
    if section == "embedding":
        return await EmbeddingClient(config).validate()
    if section == "milvus":
        return await VecClient(config, deps.get_embedding_client()).validate()
    return {"ok": False, "message": f"未知配置段: {section}"}


# ---------------------------------------------------------------------- #
# 指标查询
# ---------------------------------------------------------------------- #
@router.get("/metrics")
async def get_metrics_api(range_hours: int = 1):
    """返回指定时间范围内的聚合指标与时间序列。"""
    import time as _time

    now = _time.time()
    start = now - range_hours * 3600
    metrics = get_metrics()
    return JSONResponse({
        "summary": metrics.summary(start, now),
        "timeseries": metrics.timeseries(start, now),
    })


def _to_http(exc: AppException) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.to_dict()["error"])
