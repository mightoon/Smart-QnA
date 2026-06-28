"""内存指标采集与聚合查询。

记录每次操作（LLM 调用、检索调用、整体请求等）的耗时、成功状态、token 消耗，
提供按时间范围聚合的统计接口，供可视化面板使用。

数据保存在内存中的环形缓冲区，不持久化，重启后清空。
"""

from __future__ import annotations

import bisect
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional


@dataclass
class MetricEvent:
    timestamp: float  # epoch seconds
    category: str     # chat / llm / embedding / rerank / es / kg / milvus
    operation: str    # 具体操作名
    duration_ms: float
    success: bool = True
    tokens: int = 0
    error: str = ""


class MetricsCollector:
    """线程安全的内存指标采集器。"""

    def __init__(self, max_events: int = 50000):
        self._events: deque[MetricEvent] = deque(maxlen=max_events)
        self._lock = threading.Lock()

    def record(
        self,
        category: str,
        operation: str,
        duration_ms: float,
        success: bool = True,
        tokens: int = 0,
        error: str = "",
    ) -> None:
        ev = MetricEvent(
            timestamp=time.time(),
            category=category,
            operation=operation,
            duration_ms=duration_ms,
            success=success,
            tokens=tokens,
            error=error,
        )
        with self._lock:
            self._events.append(ev)

    def _query_events(self, start: float, end: float) -> list[MetricEvent]:
        with self._lock:
            return [e for e in self._events if start <= e.timestamp <= end]

    def summary(self, start: float, end: float) -> dict:
        """返回时间范围内的聚合统计。"""
        events = self._query_events(start, end)
        if not events:
            return _empty_summary()

        # 按类别分组
        by_cat: dict[str, list[MetricEvent]] = defaultdict(list)
        chat_durations: list[float] = []
        for e in events:
            by_cat[e.category].append(e)
            if e.category == "chat":
                chat_durations.append(e.duration_ms)

        categories = {}
        for cat, evs in by_cat.items():
            durations = [e.duration_ms for e in evs]
            tokens = sum(e.tokens for e in evs)
            errors = sum(1 for e in evs if not e.success)
            categories[cat] = {
                "count": len(evs),
                "errors": errors,
                "error_rate": round(errors / len(evs), 4) if evs else 0,
                "avg_ms": round(sum(durations) / len(durations), 2) if durations else 0,
                "tokens": tokens,
            }

        total = len(events)
        errors = sum(1 for e in events if not e.success)
        total_tokens = sum(e.tokens for e in events)

        result = {
            "total": total,
            "errors": errors,
            "error_rate": round(errors / total, 4) if total else 0,
            "total_tokens": total_tokens,
            "categories": categories,
            "start": start,
            "end": end,
        }

        # chat 请求的延迟分位数
        if chat_durations:
            chat_durations.sort()
            result["latency"] = {
                "avg_ms": round(sum(chat_durations) / len(chat_durations), 2),
                "p50_ms": round(_percentile(chat_durations, 50), 2),
                "p95_ms": round(_percentile(chat_durations, 95), 2),
                "p99_ms": round(_percentile(chat_durations, 99), 2),
            }
        else:
            result["latency"] = {"avg_ms": 0, "p50_ms": 0, "p95_ms": 0, "p99_ms": 0}

        return result

    def timeseries(self, start: float, end: float, buckets: int = 60) -> dict:
        """返回时间范围内的分桶时间序列。"""
        events = self._query_events(start, end)
        span = end - start
        if span <= 0 or not events:
            return {"buckets": [], "labels": []}

        bucket_size = max(1, span / buckets)
        labels: list[str] = []
        counts: list[int] = []
        errors: list[int] = []
        latencies: list[float] = []

        for i in range(buckets):
            b_start = start + i * bucket_size
            b_end = b_start + bucket_size
            b_events = [e for e in events if b_start <= e.timestamp < b_end]
            labels.append(time.strftime("%H:%M", time.localtime(b_start)))
            counts.append(len(b_events))
            errors.append(sum(1 for e in b_events if not e.success))
            if b_events:
                latencies.append(round(sum(e.duration_ms for e in b_events) / len(b_events), 2))
            else:
                latencies.append(0)

        return {
            "labels": labels,
            "counts": counts,
            "errors": errors,
            "latencies": latencies,
            "bucket_size_s": round(bucket_size, 1),
        }


def _percentile(sorted_values: list[float], p: float) -> float:
    """计算分位数（线性插值）。"""
    if not sorted_values:
        return 0
    k = (len(sorted_values) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def _empty_summary() -> dict:
    return {
        "total": 0,
        "errors": 0,
        "error_rate": 0,
        "total_tokens": 0,
        "categories": {},
        "latency": {"avg_ms": 0, "p50_ms": 0, "p95_ms": 0, "p99_ms": 0},
        "start": 0,
        "end": 0,
    }


# 全局单例
_metrics: Optional[MetricsCollector] = None


def get_metrics() -> MetricsCollector:
    global _metrics
    if _metrics is None:
        _metrics = MetricsCollector()
    return _metrics


@asynccontextmanager
async def measure(category: str, operation: str) -> AsyncIterator[dict]:
    """异步上下文管理器：自动记录操作的耗时与状态。

    用法：
        async with measure("es", "search_article") as m:
            result = await ...
            m["tokens"] = resp.usage.total_tokens  # 可选
    """
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
