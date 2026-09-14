"""micromcp as an MCP App on Modal — verified rendering in the Claude desktop chat.

    modal deploy examples/mcp_app_modal.py     # prints a public HTTPS URL
    modal app logs micromcp-ui -f              # one WIRE line per request

Add `<url>/mcp` as a custom connector in Claude's settings and ask a chat for
"the crash chart for Beacon St". What the wire log shows, from a 2026-09-14
run: Anthropic's connector validator opens with `initialize` at 2025-11-25, is
refused with -32022, and falls back to `server/discover` at 2026-07-28; the
chat host ("Claude-User") then speaks 2026-07-28 only, prefetches the `ui://`
widget named in the tool's `_meta` before the first `tools/call` on a
connector, and feeds the tool result to the iframe as
`ui/notifications/tool-result`. The host caches the fetched widget per
connector, so a changed widget needs a new connector identity (a different
path works) to be fetched again.

The widget is a vanilla MCP Apps client written against the 2026-01-26
extension spec: it accepts messages only from its parent, completes
`ui/initialize` -> `ui/notifications/initialized`, answers `ping` and
`ui/resource-teardown`, renders only from host-delivered tool results (via
DOM APIs, never innerHTML), shows errors, applies the host's theme, and
reports its size from a ResizeObserver once initialized.

Deployment notes: pin MICROMCP_REF to a release tag for reproducible builds;
this demo has no `authenticate=` because it serves only fixture data — a real
deployment sets it, plus `allowed_origins=`/`allowed_hosts=`.
"""
import modal

MICROMCP_REF = "main"      # pin to a tag (e.g. "v0.1.0") for reproducible images
image = (modal.Image.debian_slim(python_version="3.12")
         .pip_install(f"https://github.com/mhalle/micromcp/archive/refs/heads/{MICROMCP_REF}.zip"))
app = modal.App("micromcp-ui")


@app.function(image=image, timeout=600)
@modal.asgi_app()
def web():
    import json, os, time
    from micromcp import MCP, ASGIServer, result

    mcp = MCP("newton-civic-ui", "0.1.0")

    HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Crash widget</title>
<style>
:root{color-scheme:light dark}
body{font:14px system-ui;margin:16px;color:var(--fg,#222);background:var(--bg,transparent)}
h1{font-size:18px;margin:0 0 8px}.row{margin:4px 0}
.bar{height:14px;background:#4a7;border-radius:3px;display:inline-block;vertical-align:middle}
.err{color:#b00}small{color:var(--muted,#666);display:block;margin-top:10px}
</style></head>
<body><h1 id="t">Crashes by year</h1><div id="bars"><em>waiting for host data&hellip;</em></div>
<small id="s">micromcp widget loaded; no host handshake yet</small>
<script>
// A vanilla MCP Apps client (spec 2026-01-26): JSON-RPC over postMessage with the host.
const HOST = window.parent;                       // the only window we talk to or listen to
const APP_PROTOCOL = "2026-01-26";
let nextId = 1, initialized = false;
const pending = {};
const $ = id => document.getElementById(id);
const status = t => { $("s").textContent = t; };
const send = msg => { if (HOST !== window) HOST.postMessage(msg, "*"); };   // sandbox origin is opaque
const notify = (method, params) => send({jsonrpc: "2.0", method, params});
function request(method, params, timeoutMs = 5000) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { delete pending[id]; reject(new Error(method + " timed out")); }, timeoutMs);
    pending[id] = m => { clearTimeout(timer); m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result || {}); };
    send({jsonrpc: "2.0", id, method, params});
  });
}
function applyHostContext(ctx) {
  if (!ctx) return;
  if (ctx.theme) document.documentElement.style.colorScheme = ctx.theme;
  const v = (ctx.styles && ctx.styles.variables) || {};
  for (const [k, val] of Object.entries(v)) if (typeof val === "string") document.documentElement.style.setProperty(k, val);
  const dims = ctx.containerDimensions || {};
  if (dims.maxHeight) document.documentElement.style.maxHeight = typeof dims.maxHeight === "number" ? dims.maxHeight + "px" : dims.maxHeight;
}
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
function renderChart(street, data) {
  $("t").textContent = "Crashes by year" + (street ? " on " + street : "");
  const bars = $("bars"); clear(bars);
  for (const [y, n] of Object.entries(data)) {          // DOM APIs only: result data is untrusted text
    const count = Number(n) || 0;
    const row = document.createElement("div"); row.className = "row";
    const bar = document.createElement("span"); bar.className = "bar"; bar.style.width = (count * 18) + "px";
    row.append(document.createTextNode(String(y) + " "), bar, document.createTextNode(" " + count));
    bars.append(row);
  }
}
function renderError(text) {
  $("t").textContent = "Crash chart unavailable";
  const bars = $("bars"); clear(bars);
  const e = document.createElement("div"); e.className = "err"; e.textContent = text || "tool returned an error";
  bars.append(e);
}
function reportSize() {
  if (!initialized) return;
  const root = document.documentElement, prev = root.style.height;
  root.style.height = "max-content";                  // so the measurement can shrink below the viewport
  const height = Math.ceil(root.getBoundingClientRect().height);
  root.style.height = prev;
  notify("ui/notifications/size-changed", {width: window.innerWidth, height});
}
window.addEventListener("message", ev => {
  if (ev.source !== HOST) return;                     // ignore any other frame
  const m = ev.data; if (!m || m.jsonrpc !== "2.0") return;
  if (m.id !== undefined && !m.method) { const cb = pending[m.id]; if (cb) { delete pending[m.id]; cb(m); } return; }
  if (m.id !== undefined && m.method) {                // a request from the host: answer it
    if (m.method === "ping" || m.method === "ui/resource-teardown") send({jsonrpc: "2.0", id: m.id, result: {}});
    else send({jsonrpc: "2.0", id: m.id, error: {code: -32601, message: "method not found"}});
    if (m.method === "ui/resource-teardown") status("host is tearing this widget down");
    return;
  }
  switch (m.method) {
    case "ui/notifications/tool-result": {
      const r = m.params || {}, sc = r.structuredContent || {};
      if (r.isError) renderError((r.content || []).filter(b => b.type === "text").map(b => b.text).join(" "));
      else if (sc.by_year) { renderChart(sc.street, sc.by_year); status("host delivered tool-result for " + (sc.street || "?")); }
      else renderError("tool result had no by_year data");
      reportSize(); break;
    }
    case "ui/notifications/tool-input":
      status("host delivered tool-input: " + JSON.stringify((m.params && m.params.arguments) || {})); break;
    case "ui/notifications/tool-cancelled":
      renderError("tool call was cancelled"); reportSize(); break;
    case "ui/notifications/host-context-changed":
      applyHostContext(m.params); reportSize(); break;
    default:
      status("host says: " + m.method);
  }
});
request("ui/initialize", {appInfo: {name: "crash-widget", version: "0.2.0"},
                          appCapabilities: {availableDisplayModes: ["inline"]}, protocolVersion: APP_PROTOCOL})
  .then(res => {
    applyHostContext(res.hostContext);
    notify("ui/notifications/initialized", {});
    initialized = true;
    new ResizeObserver(reportSize).observe(document.body);
    reportSize();
    status("handshake done with " + JSON.stringify(res.hostInfo || "host"));
  })
  .catch(err => status("initialize failed: " + err.message));
</script></body></html>"""

    @mcp.resource("ui://crash-widget-v4", title="Crash widget",
                  meta={"ui": {"prefersBorder": True,
                               "csp": {"resourceDomains": [], "connectDomains": []}}})
    def crash_widget() -> str:
        """The widget's HTML, rendered by an MCP Apps host in a sandboxed iframe.
        Static on purpose: hosts fetch it with their own identity and cache it."""
        return HTML

    DATA = {"Washington St": {"2020": 5, "2021": 9, "2022": 12, "2023": 7, "2024": 11},
            "Beacon St": {"2020": 2, "2021": 4, "2022": 6, "2023": 9, "2024": 5}}

    def _by_year(street: str, since: int = 2020) -> dict | None:
        years = DATA.get(street)
        if years is None or not 1900 <= since <= 2100:
            return None
        return {y: n for y, n in years.items() if int(y) >= since}

    # `ui.resourceUri` is the spec's key; the flat `ui/resourceUri` is deprecated
    # but still emitted by the reference servers, so it is kept until hosts drop it.
    @mcp.tool(title="Show crash chart", read_only=True,
              meta={"ui": {"resourceUri": "ui://crash-widget-v4"}, "ui/resourceUri": "ui://crash-widget-v4"})
    def show_crash_chart(street: str = "Washington St") -> dict:
        """Show a chart of crashes by year for a street. The host loads the widget
        from the ui:// resource named in this tool's _meta and feeds it this result.

        Args:
            street: Street name; only streets in the fixture dataset are known.
        """
        by = _by_year(street)
        if by is None:
            return result([{"type": "text", "text": f"No crash data for {street!r}."}], is_error=True)
        return result([{"type": "text", "text": f"Crash chart for {street}: "
                                                + ", ".join(f"{y}={n}" for y, n in by.items())}],
                      structured={"street": street, "by_year": by})

    @mcp.tool(read_only=True)
    def crash_count(street: str, since: int = 2020) -> dict:
        """Count crashes on a street in years >= `since` (plain, no UI)."""
        by = _by_year(street, since)
        if by is None:
            return result([{"type": "text", "text": f"No crash data for {street!r} / since={since}."}],
                          is_error=True)
        return {"street": street, "since": since, "count": sum(by.values())}

    inner = ASGIServer(mcp, path="/mcp")
    log_bodies = bool(os.environ.get("WIRE_BODIES"))   # request bodies are user content; off by default

    async def logged(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        chunks, sent = [], []
        async def recv():
            m = await receive()
            if m["type"] == "http.request":
                chunks.append(m.get("body", b""))
            return m
        async def snd(m):
            sent.append(m)
            await send(m)
        try:
            await inner(scope, recv, snd)
        finally:
            try:
                hdrs = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
                body = b"".join(chunks).decode(errors="replace")
                try:
                    j = json.loads(body)
                    method = j.get("method")
                    name = (j.get("params") or {}).get("name") or (j.get("params") or {}).get("uri")
                except Exception:
                    method, name = None, None
                status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
                print(f"WIRE {time.strftime('%H:%M:%S')} {scope['method']} {scope['path']} method={method} "
                      f"name={name} ua={hdrs.get('user-agent', '-')[:60]!r} "
                      f"mcp-protocol-version={hdrs.get('mcp-protocol-version', '-')} -> {status}", flush=True)
                if log_bodies and (method in (None, "initialize") or (status or 0) >= 400):
                    print("WIRE   body:", body[:400], flush=True)
            except Exception as exc:                 # logging must never break a request
                print(f"WIRE log error: {exc!r}", flush=True)
    return logged
