---
name: metrics-dashboard
description: >-
  内存指标采集与可视化技能。提供非侵入式指标埋点（measure 上下文管理器）、内存环形缓冲区存储、
  按时间范围聚合统计（P50/P95/P99/错误率/token 消耗）、分桶时间序列、Chart.js 前端图表展示的
  完整实现模式。适用于需要轻量级服务监控、无需引入 Prometheus 等重基础设施的 Python Web 服务。
  当用户提到指标采集、性能监控、延迟统计、P95/P99、可视化监控面板时触发。
---

# 内存指标采集与可视化

## 概述

本技能提供轻量级服务监控的完整模式：通过 `async with measure()` 上下文管理器非侵入式埋点，内存环形缓冲区存储事件，按时间范围聚合统计（总数/错误率/P50/P95/P99/token），Chart.js 前端图表展示。无需 Prometheus/Grafana 等外部依赖。

## 何时使用

- Python Web 服务需要轻量级性能监控
- 需要统计请求延迟分布（P50/P95/P99）
- 需要监控各组件调用次数、token 消耗、错误率
- 需要可视化监控面板但不引入重基础设施

## 核心设计

### 非侵入式埋点

通过 `async with measure(category, operation)` 自动记录耗时与状态，不污染业务代码：

```python
async with measure("llm", "stream_chat") as m:
    result = await self.client.chat.completions.create(...)
    m["tokens"] = resp.usage.total_tokens  # 可选记录 token
```

上下文管理器自动：
- 开始时记录时间戳
- 结束时计算耗时、记录成功/失败状态
- 异常时标记 success=False 并记录错误信息
- 写入内存缓冲区

### 指标事件结构

```python
@dataclass
class MetricEvent:
    timestamp: float      # epoch seconds
    category: str         # chat / llm / es / kg / milvus / ...
    operation: str        # 具体操作名
    duration_ms: float
    success: bool = True
    tokens: int = 0
    error: str = ""
```

### 聚合统计

详细的 MetricsCollector 实现（环形缓冲区、summary/timeseries 聚合、分位数计算）、API 接口与前端图表，参见：
- `references/metrics_collector.md` —— 采集器实现与聚合算法
- `references/metrics_ui.md` —— API 接口与 Chart.js 图表实现

## 实现清单

1. 实现 `measure` 上下文管理器（asynccontextmanager）
2. 实现 MetricsCollector（deque 环形缓冲区 + RLock 线程安全）
3. 实现 `summary(start, end)`：总数/错误率/按类别统计/延迟分位数
4. 实现 `timeseries(start, end, buckets)`：分桶时间序列
5. 提供 `GET /api/metrics?range_hours=N` API
6. 前端 Chart.js 渲染：汇总卡片 + 4 图表（请求量/延迟/调用次数/token）
7. 各业务客户端用 `async with measure(...)` 包裹关键操作

## 适用场景示例

- LLM 服务：监控大模型调用耗时、token 消耗、错误率
- 检索服务：监控 ES/Neo4j/Milvus 检索延迟
- API 网关：监控请求量、P95/P99 延迟、错误率趋势
