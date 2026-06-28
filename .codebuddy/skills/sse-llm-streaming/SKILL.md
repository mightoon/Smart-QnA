---
name: sse-llm-streaming
description: >-
  SSE 流式 LLM 接口技能。提供 FastAPI 后端 SSE 流式输出 + openai SDK 流式调用 + 前端 SSE 解析的
  完整端到端实现模式。涵盖 SSE 事件序列设计（meta→token→done→error）、流式生成器、前端 ReadableStream
  解析、错误降级等。适用于需要构建流式 LLM 问答接口、实时输出、打字机效果的场景。当用户提到 SSE、
  流式输出、streaming、打字机效果、实时 token 推送时触发。
---

# SSE 流式 LLM 接口

## 概述

本技能提供 FastAPI 后端通过 Server-Sent Events (SSE) 流式输出 LLM token 的完整端到端模式，包括后端流式生成器设计、SSE 事件协议、前端 ReadableStream 解析、错误处理。

## 何时使用

- 构建 LLM 流式问答接口（打字机效果）
- 需要实时推送处理进度/检索结果/生成 token
- FastAPI + openai SDK 的流式集成

## SSE 事件协议设计

### 事件序列

```
event: meta
data: {"mode":"all","entities":["实体A"],"sources":[...]}

event: token
data: {"content":"这是"}

event: token
data: {"content":"一段"}

event: done
data: {}

event: error
data: {"error":{"code":"llm_error","message":"..."}}
```

### 设计要点
- `meta` 事件先于 token，推送检索元信息（来源/实体），前端可先渲染来源
- `token` 事件多次推送，逐 token 输出
- `done` 事件标志正常结束
- `error` 事件用于流式过程中的异常降级（不中断连接）

## 后端实现

详细的 FastAPI StreamingResponse + openai stream_chat + SSE 格式化实现，参见：
- `references/backend_sse.md` —— 后端流式生成器与 SSE 格式化

## 前端实现

详细的前端 fetch + ReadableStream + SSE 事件解析实现，参见：
- `references/frontend_sse.md` —— 前端 SSE 解析与实时渲染

## 实现清单

1. 设计 SSE 事件协议（meta/token/done/error）
2. 后端：FastAPI StreamingResponse + async generator 产出 SSE 事件
3. 后端：openai SDK stream=True 流式调用，逐 chunk 提取 token
4. 前端：fetch + ReadableStream reader 读取流
5. 前端：按 `\n\n` 分割 SSE 块，解析 event/data
6. 错误处理：后端 try/except 产 error 事件，前端 onError 回调
7. 响应头设置：Cache-Control: no-cache, X-Accel-Buffering: no（Nginx 不缓冲）
