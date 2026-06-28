# 后端 SSE 流式实现

## FastAPI StreamingResponse

```python
from fastapi.responses import StreamingResponse

@router.post("/chat")
async def chat(req: ChatRequest):
    if req.stream:
        return StreamingResponse(
            _chat_stream(orchestrator, req),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # Nginx 不缓冲
                "Connection": "keep-alive",
            },
        )
    # 非流式...
```

## 流式生成器

```python
async def _chat_stream(orchestrator, req) -> AsyncIterator[bytes]:
    try:
        async for event in orchestrator.run_stream(req):
            yield event.encode("utf-8")
    except AppException as exc:
        err = json.dumps({"error": exc.to_dict()["error"]}, ensure_ascii=False)
        yield f"event: error\ndata: {err}\n\n".encode("utf-8")
    except Exception as exc:
        err = json.dumps({"error": {"code": "internal_error", "message": str(exc)}}, ensure_ascii=False)
        yield f"event: error\ndata: {err}\n\n".encode("utf-8")
```

## SSE 格式化

```python
def _sse(event: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"
```

## openai SDK 流式调用

```python
async def stream_chat(self, query, context="", history=None) -> AsyncIterator[str]:
    messages = self._build_messages(query, context, history)
    stream = await self.client.chat.completions.create(
        model=self.model,
        messages=messages,
        temperature=self.temperature,
        max_tokens=self.max_tokens,
        stream=True,
    )
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        content = getattr(delta, "content", None)
        if content:
            yield content
```

## Orchestrator 流式调度

```python
async def run_stream(self, request) -> AsyncIterator[str]:
    sources, entities = await self._retrieve(request)
    context = self._build_context(sources)

    yield _sse("meta", {"mode": ..., "entities": entities, "sources": [...]})

    async for token in self.llm.stream_chat(query, context, history):
        yield _sse("token", {"content": token})

    yield _sse("done", {})
```

关键：检索阶段同步完成后才开始流式推送，meta 事件先发，token 事件紧随。
