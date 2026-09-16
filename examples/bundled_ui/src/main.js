import "./app.css";
import logo from "./logo.png";            // inlined as a data: URL by the bundler

const root = document.getElementById("root");
let sensor = "kitchen";

const path = (values, close) => {
  const lo = Math.min(...values), hi = Math.max(...values), span = hi - lo || 1;
  const pt = (v, i) => [(i / (values.length - 1)) * 100, 60 - ((v - lo) / span) * 52];
  const line = values.map((v, i) => `${i ? "L" : "M"}${pt(v, i).map(n => n.toFixed(2))}`).join(" ");
  return close ? `${line} L100,60 L0,60 Z` : line;
};

const view = ({ sensor: name, unit, values, label }) => {
  const now = values[values.length - 1];
  const [x, y] = [100, 60 - ((now - Math.min(...values)) /
    ((Math.max(...values) - Math.min(...values)) || 1)) * 52];
  return `
    <header><img src="${logo}" alt=""><h1>${label}</h1></header>
    <div class="reading"><span class="now">${now.toFixed(1)}</span><span class="unit">${unit}</span></div>
    <div class="range">${values.length} readings · low ${Math.min(...values).toFixed(1)} · high ${Math.max(...values).toFixed(1)}</div>
    <svg viewBox="0 0 100 62" preserveAspectRatio="none" aria-label="${label} over time">
      <path class="area" d="${path(values, true)}"/><path class="line" d="${path(values)}"/>
      <circle cx="${x}" cy="${y.toFixed(2)}" r="2.5"/>
    </svg>
    <div class="chips">${["kitchen", "attic", "cellar"].map(s =>
      `<button data-sensor="${s}" aria-pressed="${s === name}">${s}</button>`).join("")}</div>`;
};

async function show(name) {
  try {
    const r = await mcp.callTool("readings", { sensor: name });   // rejects if the call fails
    const data = r.structuredContent;
    sensor = data.sensor;
    root.innerHTML = view(data);
    mcp.setContext(`The user is watching the ${data.label.toLowerCase()}: ` +
      `${data.values[data.values.length - 1].toFixed(1)}${data.unit} now.`, data);
  } catch (e) {
    root.innerHTML = `<p class="err">${e.message}</p>`;
  }
}

root.addEventListener("click", e => {
  const b = e.target.closest("button[data-sensor]");
  if (b && b.dataset.sensor !== sensor) show(b.dataset.sensor);
});

await mcp.ready;                           // the handshake first, then any call
show(sensor);
