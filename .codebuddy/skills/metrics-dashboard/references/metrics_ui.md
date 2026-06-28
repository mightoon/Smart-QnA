# 指标 API 与前端图表

## API 接口

```python
@router.get("/metrics")
async def get_metrics_api(range_hours: int = 1):
    now = time.time()
    start = now - range_hours * 3600
    metrics = get_metrics()
    return JSONResponse({
        "summary": metrics.summary(start, now),
        "timeseries": metrics.timeseries(start, now),
    })
```

## 响应结构

```json
{
  "summary": {
    "total": 100,
    "errors": 2,
    "error_rate": 0.02,
    "total_tokens": 15200,
    "categories": {
      "llm": { "count": 50, "errors": 0, "avg_ms": 320.5, "tokens": 12000 },
      "es": { "count": 30, "errors": 1, "avg_ms": 12.3, "tokens": 0 }
    },
    "latency": { "avg_ms": 350, "p50_ms": 300, "p95_ms": 800, "p99_ms": 1200 }
  },
  "timeseries": { "labels": [...], "counts": [...], "errors": [...], "latencies": [...] }
}
```

## 前端汇总卡片

```javascript
function renderCards(s) {
    const lat = s.latency || {};
    cards.innerHTML = [
        card("总请求", s.total),
        card("错误数", s.errors + " (" + (s.error_rate * 100).toFixed(1) + "%)"),
        card("平均延迟", lat.avg_ms + " ms"),
        card("P95", lat.p95_ms + " ms"),
        card("P99", lat.p99_ms + " ms"),
        card("Token", s.total_tokens),
    ].join("");
}
```

## Chart.js 图表

### 请求量趋势（折线图）
```javascript
new Chart(ctx, {
    type: "line",
    data: {
        labels: ts.labels,
        datasets: [
            { label: "请求数", data: ts.counts, borderColor: "#6366f1", fill: true, tension: .3 },
            { label: "错误数", data: ts.errors, borderColor: "#ef4444", fill: true, tension: .3 },
        ],
    },
});
```

### 延迟趋势（折线图 + 参考线）
```javascript
datasets: [
    { label: "平均延迟", data: ts.latencies, tension: .3 },
    { label: "P95", data: Array(n).fill(summary.latency.p95_ms), borderDash: [5,5], pointRadius: 0 },
    { label: "P99", data: Array(n).fill(summary.latency.p99_ms), borderDash: [5,5], pointRadius: 0 },
]
```

### 各服务调用次数（柱状图）
```javascript
new Chart(ctx, {
    type: "bar",
    data: {
        labels: catKeys.map(k => CAT_LABELS[k]),
        datasets: [{ data: catKeys.map(k => cats[k].count), backgroundColor: catKeys.map(k => CAT_COLORS[k]) }],
    },
});
```

### 自动刷新
```javascript
autoTimer = setInterval(loadMetrics, 30000);  // 30s
```

## HTML 结构

```html
<div class="metrics-bar">
    <select id="metricsRange">
        <option value="1">最近 1 小时</option>
        <option value="24">最近 24 小时</option>
    </select>
    <button id="refreshMetricsBtn">刷新</button>
    <input id="autoRefreshCheck" type="checkbox" checked /> 自动刷新(30s)
</div>
<div class="metrics-cards" id="metricsCards"></div>
<div class="metrics-charts">
    <canvas id="chartRequests"></canvas>
    <canvas id="chartLatency"></canvas>
    <canvas id="chartCategories"></canvas>
    <canvas id="chartTokens"></canvas>
</div>
```

Chart.js via CDN: `https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js`
