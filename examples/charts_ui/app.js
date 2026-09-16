// ECharts comes from a CDN, so `Widget` declares its origin for the host; the widget's data
// comes from an app-only tool, and new events arrive on a channel.
(async () => {
  const el = (id) => document.getElementById(id);
  const fmt = (n, unit) => unit === "$" ? "$" + n.toLocaleString() : n.toLocaleString() + (unit || "");
  let metric = "revenue", chart = null, live = 0;

  const dark = () => matchMedia("(prefers-color-scheme: dark)").matches;
  const paint = (d) => {
    el("title").textContent = d.label;
    el("range").textContent = d.range;
    el("value").textContent = fmt(d.total, d.unit);
    const delta = el("delta");
    delta.textContent = (d.change >= 0 ? "+" : "") + d.change.toFixed(1) + "% vs previous";
    delta.className = "delta " + (d.change >= 0 ? "up" : "down");
    for (const b of document.querySelectorAll("button[data-metric]"))
      b.setAttribute("aria-pressed", String(b.dataset.metric === d.metric));
    chart.setOption({
      grid: { left: 2, right: 2, top: 10, bottom: 2, containLabel: true },
      xAxis: { type: "category", data: d.days, boundaryGap: false,
               axisLine: { lineStyle: { color: dark() ? "#393d48" : "#d8dbe2" } },
               axisLabel: { color: dark() ? "#99a0ab" : "#6b7280" } },
      yAxis: { type: "value", splitLine: { lineStyle: { color: dark() ? "#23252c" : "#eef0f4" } },
               axisLabel: { color: dark() ? "#99a0ab" : "#6b7280" } },
      tooltip: { trigger: "axis" },
      series: [{ type: "line", data: d.values, smooth: true, showSymbol: false,
                 lineStyle: { width: 2, color: dark() ? "#6f9bff" : "#2563eb" },
                 areaStyle: { color: dark() ? "rgba(111,155,255,.16)" : "rgba(37,99,235,.10)" } }],
    }, true);
    mcp.setContext(`The user is looking at ${d.label.toLowerCase()} (${d.range}): ` +
                   `${fmt(d.total, d.unit)}, ${d.change >= 0 ? "up" : "down"} ` +
                   `${Math.abs(d.change).toFixed(1)}% on the previous period.`, d);
  };

  const show = async (name) => {
    try {
      const r = await mcp.callTool("dashboard_data", { metric: name });  // rejects if it fails
      metric = r.structuredContent.metric;
      paint(r.structuredContent);
    } catch (e) {
      el("live").textContent = e.message;
    }
  };

  await mcp.ready;                                  // the handshake first, then any call
  chart = echarts.init(el("chart"), null, { renderer: "svg" });
  addEventListener("resize", () => chart.resize());
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => show(metric));
  document.querySelector(".chips").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-metric]");
    if (b && b.dataset.metric !== metric) show(b.dataset.metric);
  });

  const feed = mcp.channel("events");               // the model's events, pushed in
  feed.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.event) {
      live += 1;
      el("live").textContent = `${live} live event${live > 1 ? "s" : ""} · latest ${m.event}`;
      show(metric);
    }
  };
  show(metric);
})();
