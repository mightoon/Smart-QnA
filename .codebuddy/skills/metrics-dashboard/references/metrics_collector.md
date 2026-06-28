# MetricsCollector 实现

## 环形缓冲区

```python
from collections import deque

class MetricsCollector:
    def __init__(self, max_events=50000):
        self._events = deque(maxlen=max_events)  # 自动丢弃旧事件
        self._lock = threading.RLock()
```

## measure 上下文管理器

```python
from contextlib import asynccontextmanager
import time

@asynccontextmanager
async def measure(category: str, operation: str):
    start = time.monotonic()
    info = {"tokens": 0, "success": True, "error": ""}
    try:
        yield info
    except Exception as exc:
        info["success"] = False
        info["error"] = str(exc)
        raise
    finally:
        get_metrics().record(
            category=category,
            operation=operation,
            duration_ms=(time.monotonic() - start) * 1000,
            success=info["success"],
            tokens=info["tokens"],
            error=info["error"],
        )
```

## summary 聚合

```python
def summary(self, start, end) -> dict:
    events = self._query_events(start, end)
    by_cat = defaultdict(list)
    for e in events:
        by_cat[e.category].append(e)

    categories = {}
    for cat, evs in by_cat.items():
        durations = [e.duration_ms for e in evs]
        categories[cat] = {
            "count": len(evs),
            "errors": sum(1 for e in evs if not e.success),
            "avg_ms": sum(durations) / len(durations),
            "tokens": sum(e.tokens for e in evs),
        }

    chat_durations = [e.duration_ms for e in by_cat.get("chat", [])]
    return {
        "total": len(events),
        "errors": sum(1 for e in events if not e.success),
        "categories": categories,
        "latency": {
            "avg_ms": avg(chat_durations),
            "p50_ms": percentile(chat_durations, 50),
            "p95_ms": percentile(chat_durations, 95),
            "p99_ms": percentile(chat_durations, 99),
        },
    }
```

## 分位数计算（线性插值）

```python
def _percentile(sorted_values, p):
    if not sorted_values:
        return 0
    k = (len(sorted_values) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)
```

## timeseries 分桶

```python
def timeseries(self, start, end, buckets=60) -> dict:
    span = end - start
    bucket_size = max(1, span / buckets)
    labels, counts, errors, latencies = [], [], [], []

    for i in range(buckets):
        b_start = start + i * bucket_size
        b_end = b_start + bucket_size
        b_events = [e for e in events if b_start <= e.timestamp < b_end]
        labels.append(time.strftime("%H:%M", time.localtime(b_start)))
        counts.append(len(b_events))
        errors.append(sum(1 for e in b_events if not e.success))
        latencies.append(avg([e.duration_ms for e in b_events]) if b_events else 0)

    return {"labels": labels, "counts": counts, "errors": errors, "latencies": latencies}
```

## 埋点位置示例

| 类别 | 操作 | 位置 |
|---|---|---|
| chat | request | orchestrator.run / run_stream |
| llm | stream_chat | llm_client.stream_chat |
| llm | extract_entities | llm_client.extract_entities |
| es | search_article | es_client._search |
| kg | search_by_entities | kg_client.search_by_entities |
| milvus | search | vec_client.search |
| embedding | embed | embedding_client.embed |
| rerank | rerank | rerank_client.rerank |
