/* 可视化监控 tab 逻辑：拉取 /api/metrics，用 Chart.js 渲染图表。 */
(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  let charts = {};
  let autoTimer = null;

  // 切换到 metrics tab 时触发
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      if (tab.dataset.tab === "metrics") {
        loadMetrics();
        startAutoRefresh();
      } else {
        stopAutoRefresh();
      }
    });
  });

  $("#refreshMetricsBtn").addEventListener("click", loadMetrics);
  $("#autoRefreshCheck").addEventListener("change", (e) => {
    if (e.target.checked) startAutoRefresh();
    else stopAutoRefresh();
  });

  function startAutoRefresh() {
    stopAutoRefresh();
    autoTimer = setInterval(loadMetrics, 30000);
  }
  function stopAutoRefresh() {
    if (autoTimer) clearInterval(autoTimer);
    autoTimer = null;
  }

  async function loadMetrics() {
    const rangeHours = $("#metricsRange").value;
    try {
      const resp = await fetch("/api/metrics?range_hours=" + rangeHours);
      const data = await resp.json();
      if (!resp.ok) return;
      renderCards(data.summary);
      renderCharts(data.summary, data.timeseries);
    } catch (e) {
      // 静默失败
    }
  }

  const CAT_LABELS = {
    chat: "问答请求",
    llm: "大模型",
    embedding: "Embedding",
    rerank: "Rerank",
    es: "Elasticsearch",
    kg: "Neo4j",
    milvus: "Milvus",
  };
  const CAT_COLORS = {
    chat: "#6366f1",
    llm: "#0ea5e9",
    embedding: "#8b5cf6",
    rerank: "#f59e0b",
    es: "#22c55e",
    kg: "#ec4899",
    milvus: "#14b8a6",
  };

  function renderCards(s) {
    const cards = $("#metricsCards");
    const lat = s.latency || {};
    cards.innerHTML = [
      card("总请求", s.total, CAT_COLORS.chat),
      card("错误数", s.errors + " (" + (s.error_rate * 100).toFixed(1) + "%)", s.errors > 0 ? "#ef4444" : "#22c55e"),
      card("平均延迟", lat.avg_ms + " ms", "#0ea5e9"),
      card("P95 延迟", lat.p95_ms + " ms", "#f59e0b"),
      card("P99 延迟", lat.p99_ms + " ms", "#ef4444"),
      card("Token 消耗", s.total_tokens, "#8b5cf6"),
    ].join("");
  }

  function card(title, value, color) {
    return '<div class="m-card"><div class="m-card-val" style="color:' + color + '">' +
      value + '</div><div class="m-card-title">' + title + "</div></div>";
  }

  function renderCharts(summary, ts) {
    const baseOpts = { responsive: true, maintainAspectRatio: false, plugins: { legend: { labels: { color: "#94a3b8" } } }, scales: { x: { ticks: { color: "#64748b", maxRotation: 45, autoSkip: true, maxTicksLimit: 12 }, grid: { color: "#1e293b" } }, y: { ticks: { color: "#64748b" }, grid: { color: "#1e293b" }, beginAtZero: true } } };

    // 1. 请求量趋势
    updateChart("chartRequests", "line", {
      labels: ts.labels,
      datasets: [
        { label: "请求数", data: ts.counts, borderColor: CAT_COLORS.chat, backgroundColor: "rgba(99,102,241,.15)", fill: true, tension: .3 },
        { label: "错误数", data: ts.errors, borderColor: "#ef4444", backgroundColor: "rgba(239,68,68,.15)", fill: true, tension: .3 },
      ],
    }, baseOpts);

    // 2. 延迟趋势
    updateChart("chartLatency", "line", {
      labels: ts.labels,
      datasets: [
        { label: "平均延迟", data: ts.latencies, borderColor: "#0ea5e9", tension: .3, fill: false },
        { label: "P95", data: Array(ts.labels.length).fill(summary.latency.p95_ms), borderColor: "#f59e0b", borderDash: [5, 5], fill: false, pointRadius: 0 },
        { label: "P99", data: Array(ts.labels.length).fill(summary.latency.p99_ms), borderColor: "#ef4444", borderDash: [5, 5], fill: false, pointRadius: 0 },
      ],
    }, baseOpts);

    // 3. 各服务调用次数
    const cats = summary.categories || {};
    const catKeys = Object.keys(cats).filter((k) => k !== "chat");
    updateChart("chartCategories", "bar", {
      labels: catKeys.map((k) => CAT_LABELS[k] || k),
      datasets: [{
        label: "调用次数",
        data: catKeys.map((k) => cats[k].count),
        backgroundColor: catKeys.map((k) => CAT_COLORS[k] || "#64748b"),
      }],
    }, baseOpts);

    // 4. Token 消耗
    updateChart("chartTokens", "bar", {
      labels: catKeys.map((k) => CAT_LABELS[k] || k),
      datasets: [{
        label: "Token 消耗",
        data: catKeys.map((k) => cats[k].tokens || 0),
        backgroundColor: catKeys.map((k) => CAT_COLORS[k] || "#64748b"),
      }],
    }, baseOpts);
  }

  function updateChart(canvasId, type, data, options) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return;
    if (charts[canvasId]) charts[canvasId].destroy();
    charts[canvasId] = new Chart(ctx, { type, data, options });
  }

  // 页面加载时如果默认在 metrics tab（一般不是），也加载
  if ($("#tab-metrics").classList.contains("active")) loadMetrics();
})();
