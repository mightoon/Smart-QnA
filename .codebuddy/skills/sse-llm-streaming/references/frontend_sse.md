# 前端 SSE 解析实现

## fetch + ReadableStream

```javascript
async function streamChat(reqBody) {
    const assistant = addMsg("assistant", "");
    let buf = "";
    let pending = "";

    const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify(reqBody),
    });

    if (!resp.ok || !resp.body) {
        addMsg("error", "请求失败：" + resp.status);
        return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();

    while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        pending += decoder.decode(value, { stream: true });
        // SSE 事件以双换行分隔
        const blocks = pending.split("\n\n");
        pending = blocks.pop(); // 最后一块可能不完整，保留
        for (const block of blocks) {
            handleEvent(block, {
                onMeta: (data) => renderSources(data.sources),
                onToken: (data) => {
                    buf += data.content;
                    assistant.textContent = buf;
                },
                onError: (data) => addMsg("error", data.error?.message),
                onDone: () => { if (!buf) assistant.textContent = "(空回复)"; },
            });
        }
    }
}
```

## SSE 块解析

```javascript
function handleEvent(block, cbs) {
    let event = "message";
    let dataStr = "";
    for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataStr += line.slice(5).trim();
    }
    if (!dataStr) return;
    let data = {};
    try { data = JSON.parse(dataStr); } catch (e) { return; }
    if (event === "meta") cbs.onMeta(data);
    else if (event === "token") cbs.onToken(data);
    else if (event === "error") cbs.onError(data);
    else if (event === "done") cbs.onDone(data);
}
```

## 关键要点

- `decoder.decode(value, { stream: true })` 保持多字节字符跨 chunk 完整
- `pending.split("\n\n")` 后 `pop()` 保留最后不完整块，下次拼接
- 每次收到 token 实时更新 DOM，实现打字机效果
- 注意 `messages.scrollTop = messages.scrollHeight` 自动滚动
