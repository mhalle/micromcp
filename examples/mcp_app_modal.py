"""micromcp as an MCP App on Modal — verified rendering in the Claude desktop chat.

    modal deploy examples/mcp_app_modal.py     # prints a public HTTPS URL
    modal app logs micromcp-ui -f              # one WIRE line per request

Add the URL (any path; micromcp serves them all) as a custom connector in
Claude's settings and ask a chat for "the crash chart for Beacon St". What the
wire log shows, from a 2026-09-14 run: Anthropic's connector validator opens
with `initialize` at 2025-11-25, is refused with -32022, and falls back to
`server/discover` at 2026-07-28; the chat host ("Claude-User") then speaks
2026-07-28 only, prefetches the `ui://` widget named in the tool's `_meta`
before the first `tools/call` on a connector, and feeds the tool result to the
iframe as `ui/notifications/tool-result`. The host caches the fetched widget
per connector, so a changed widget needs a new connector identity (a different
path works) to be fetched again.

The widget below is a vanilla MCP Apps client: `ui/initialize` ->
`ui/notifications/initialized`, render on `tool-result`, report
`size-changed`. It draws nothing until the host delivers data.
"""
import modal

image = (modal.Image.debian_slim(python_version="3.12")
         .pip_install("https://github.com/mhalle/micromcp/archive/refs/heads/main.zip"))
app = modal.App("micromcp-ui")


@app.function(image=image, min_containers=1, timeout=600)
@modal.asgi_app()
def web():
    import json, sys, time
    from micromcp import MCP, ASGIServer, embedded_resource

    mcp = MCP("newton-civic-ui", "0.1.0")

    HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Crash widget</title>
<style>body{font:14px system-ui;margin:16px;color:#222;background:#fff}h1{font-size:18px;margin:0 0 8px}
.row{margin:4px 0}.bar{height:14px;background:#4a7;border-radius:3px;display:inline-block;vertical-align:middle}
small{color:#666;display:block;margin-top:10px}</style></head>
<body><h1 id="t">Crashes by year</h1><div id="bars"><em>waiting for host data&hellip;</em></div><small id="s">micromcp widget loaded; no host handshake yet</small>
<script>
// A vanilla MCP Apps client: JSON-RPC over postMessage with the host.
// Nothing is drawn until the host delivers the tool result: a widget that
// paints plausible placeholder numbers would hide a broken data path.
let nextId = 1; const pending = {};
function send(msg) { if (window.parent !== window) window.parent.postMessage(msg, "*"); }
function request(method, params) { const id = nextId++; send({jsonrpc:"2.0", id, method, params}); return new Promise(res => pending[id] = res); }
function notify(method, params) { send({jsonrpc:"2.0", method, params}); }
function status(t) { document.getElementById("s").textContent = t; }
function render(street, data) {
  document.getElementById("t").textContent = "Crashes by year" + (street ? " on " + street : "");
  const bars = document.getElementById("bars"); bars.innerHTML = "";
  for (const [y, n] of Object.entries(data)) {
    const d = document.createElement("div"); d.className = "row";
    d.innerHTML = y + " <span class=bar style='width:" + (n * 18) + "px'></span> " + n; bars.appendChild(d);
  }
  notify("ui/notifications/size-changed", {height: document.documentElement.scrollHeight, width: document.documentElement.scrollWidth});
}
window.addEventListener("message", ev => {
  const m = ev.data; if (!m || m.jsonrpc !== "2.0") return;
  if (m.id !== undefined && pending[m.id]) { pending[m.id](m); delete pending[m.id]; return; }
  if (m.method === "ui/notifications/tool-result") {
    const r = m.params || {}; const sc = r.structuredContent || (r.result && r.result.structuredContent) || {};
    if (sc.by_year) { render(sc.street, sc.by_year); status("host delivered tool-result for " + (sc.street || "?")); }
    else { document.getElementById("bars").innerHTML = "<em>tool result had no by_year data</em>"; status("tool-result without structuredContent"); }
  } else if (m.method === "ui/notifications/tool-input") {
    const a = (m.params && (m.params.arguments || m.params)) || {}; status("host delivered tool-input: " + JSON.stringify(a));
  } else if (m.method) {
    status("host says: " + m.method);
  }
});
request("ui/initialize", {appInfo: {name: "crash-widget", version: "0.1.0"}, appCapabilities: {}, protocolVersion: "2025-11-25"})
  .then(resp => { notify("ui/notifications/initialized", {}); status("handshake done with " + JSON.stringify((resp.result || {}).hostInfo || "host")); })
  .catch(() => status("initialize failed"));
</script></body></html>"""

    @mcp.resource("ui://crash-widget-v3", mime_type="text/html;profile=mcp-app", title="Crash widget",
                  meta={"ui": {"csp": {"resourceDomains": [], "connectDomains": []}}})
    def crash_widget() -> str:
        """The widget's HTML, rendered by an MCP Apps host in a sandboxed iframe."""
        return HTML

    DATA = {"Washington St": {"2020": 5, "2021": 9, "2022": 12, "2023": 7, "2024": 11},
            "Beacon St": {"2020": 2, "2021": 4, "2022": 6, "2023": 9, "2024": 5}}

    def _by_year(street: str, since: int = 2020) -> dict:
        years = DATA.get(street) or {"2020": 1, "2021": 3, "2022": 3, "2023": 2, "2024": 4}
        return {y: n for y, n in years.items() if int(y) >= since}

    @mcp.tool(title="Show crash chart", read_only=True,
              meta={"ui": {"resourceUri": "ui://crash-widget-v3"}, "ui/resourceUri": "ui://crash-widget-v3"})
    def show_crash_chart(street: str = "Washington St") -> dict:
        """Show a chart of crashes by year for a street (reference-style: the host
        loads the widget from ui://crash-widget-v3 and feeds it this result).

        Args:
            street: Street name.
        """
        by = _by_year(street)
        return {"content": [{"type": "text", "text": f"Crash chart for {street}: " + ", ".join(f"{y}={n}" for y, n in by.items())}],
                "structuredContent": {"street": street, "by_year": by},
                "_meta": {"ui": {"height": 240}}}

    @mcp.tool(title="Show crash chart (embedded)", read_only=True,
              meta={"ui": {"resourceUri": "ui://crash-widget-v3"}, "ui/resourceUri": "ui://crash-widget-v3"})
    def show_crash_chart_embedded(street: str = "Washington St") -> dict:
        """Same chart, but the result also carries the widget as an embedded resource block."""
        by = _by_year(street)
        return {"content": [{"type": "text", "text": f"Crash chart for {street}: " + ", ".join(f"{y}={n}" for y, n in by.items())},
                            embedded_resource("ui://crash-widget-v3", text=HTML, mime_type="text/html;profile=mcp-app")],
                "structuredContent": {"street": street, "by_year": by},
                "_meta": {"ui": {"height": 240}}}

    @mcp.tool(read_only=True)
    def crash_count(street: str, since: int = 2020) -> dict:
        """Count crashes on a street since a given year (plain, no UI)."""
        return {"street": street, "since": since, "count": sum(_by_year(street, since).values())}

    inner = ASGIServer(mcp)

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
        hdrs = {k.decode(): v.decode() for k, v in scope["headers"]}
        try:
            await inner(scope, recv, snd)
        finally:
            body = b"".join(chunks).decode(errors="replace")
            try:
                j = json.loads(body); method = j.get("method"); name = (j.get("params") or {}).get("name") or (j.get("params") or {}).get("uri")
            except Exception:
                method, name = None, None
            status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
            print(f"WIRE {time.strftime('%H:%M:%S')} {scope['method']} {scope['path']} method={method} name={name} "
                  f"ua={hdrs.get('user-agent','-')[:60]!r} mcp-protocol-version={hdrs.get('mcp-protocol-version','-')} "
                  f"accept={hdrs.get('accept','-')[:40]!r} -> {status}", flush=True)
            if method in (None, "initialize") or (status or 0) >= 400:
                print("WIRE   body:", body[:400], flush=True)
                for m in sent:
                    if m.get("body"):
                        print("WIRE   resp:", m["body"][:400].decode(errors="replace"), flush=True)
    return logged
