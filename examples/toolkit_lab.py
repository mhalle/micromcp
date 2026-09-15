"""Toolkit lab: one todo widget built with nine client toolkits, each testing itself.

Every variant is a static MCP Apps widget whose requests travel as
host-proxied `tools/call`. On load it probes the host's policy (is `eval`
allowed, does inline-script injection run), drives its own UI (load, add,
toggle an item that arrived in a swapped-in fragment, plus toolkit-specific
checks), tries `ui/update-model-context`, and sends its results to the
app-only `lab_report` tool. The model-visible `lab_results` returns them all.

Mounted by `mcp_app_hypermedia.py` at /lab/mcp; the dev host runs a variant
with `/devhost?mcp=/lab/mcp&tool=lab_<kit>&csp=strict|eval`.
"""
import html
import json
import pathlib
import threading
import time
from urllib.parse import urlencode

HERE = pathlib.Path(__file__).resolve().parent
NONCE = "labnonce"
KITS = {
    "fixi": "fixi 0.9.4",
    "htmx": "htmx 4 (ctx.fetch)",
    "htmx_django": "htmx 4 + Django views",
    "live_plain": "htmx 4 + hx-live, no eval fix",
    "live_csp": "htmx 4 + hx-live + hx-csp safeEval",
    "live_shim": "htmx 4 + hx-live + injection extension",
    "alpine": "htmx 4 + Alpine CSP build",
    "datastar": "Datastar 1.0.3",
    "datastar_csp": "Datastar 1.0.3, CSP mode",
}
HTMX_KITS = {"htmx", "htmx_django", "live_plain", "live_csp", "live_shim", "alpine"}
LIVE_KITS = {"live_plain", "live_csp", "live_shim"}

STYLE = """<style>
:root{color-scheme:light dark}body{font:13px system-ui;margin:10px}
h1{font-size:15px;margin:0 0 6px}.bar{display:flex;gap:6px;margin:4px 0}input{flex:1;font:inherit}
ul{list-style:none;padding:0;margin:4px 0}li.done span{text-decoration:line-through;opacity:.6}
#res{font:12px ui-monospace,monospace;margin:6px 0;padding-left:18px}small{opacity:.7}
</style>"""

# htmx 4 evaluates hx-live/hx-on expressions with new AsyncFunction (needs unsafe-eval). This
# extension swaps the constructor through htmx's initSecurity hook for inline-script injection,
# which a policy allowing 'unsafe-inline' scripts permits. Same trust as eval: escape fragments.
EVAL_EXT_JS = r"""
htmx.registerExtension("mcp-eval", {
  init(api) {
    let n = 0; const cache = new Map();
    const make = isAsync => function (...keys) {
      const body = keys.pop(), key = (isAsync ? "a" : "s") + keys.join(",") + "|" + body;
      let fn = cache.get(key);
      if (!fn) {
        const name = "__mcp_eval_" + (++n), s = document.createElement("script");
        s.textContent = "window." + name + " = " + (isAsync ? "async " : "") +
                        "function(" + keys.join(",") + ") {\n" + body + "\n}";
        document.head.appendChild(s); s.remove();
        fn = window[name]; delete window[name];
        if (typeof fn !== "function") throw new EvalError("inline script injection is blocked by the host policy");
        cache.set(key, fn);
      }
      return fn;
    };
    api.initSecurity(null, make(false), make(true));
  }
});
"""  # noqa: E501

SELFTEST_JS = r"""
(() => {
  const KIT = __KIT__;
  const R = {kit: KIT, started: new Date().toISOString(), host: null, serverTools: null,
             eval: null, inject: null, libs: {}, steps: {}, violations: [], errors: []};
  document.addEventListener("securitypolicyviolation", e =>
    R.violations.push((e.violatedDirective + " " + (e.blockedURI || "") + " " + (e.sample || "")).slice(0, 120)));
  window.addEventListener("error", e => R.errors.push(String(e.message).slice(0, 160)));
  window.addEventListener("unhandledrejection", e =>
    R.errors.push("rejection: " + String((e.reason && e.reason.message) || e.reason).slice(0, 160)));
  const $ = s => document.querySelector(s);
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  async function until(fn, ms = 6000) {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) { try { const v = fn(); if (v) return v; } catch (e) {} await sleep(50); }
    throw new Error("timed out");
  }
  const out = $("#res");
  function show(name, ok, info) {
    const li = document.createElement("li");
    li.textContent = (ok === true ? "PASS " : ok === false ? "FAIL " : "INFO ") + name + (info ? " - " + info : "");
    out.append(li);
  }
  async function step(name, fn) {
    try { const info = await fn(); R.steps[name] = {ok: true, info: info === undefined ? null : String(info)}; show(name, true, info); }
    catch (e) { R.steps[name] = {ok: false, info: String((e && e.message) || e)}; show(name, false, R.steps[name].info); }
  }
  const items = () => [...document.querySelectorAll("li[data-id]")];
  const liWith = text => items().find(li => li.textContent.includes(text));
  const visible = el => !!el && getComputedStyle(el).display !== "none";
  function type(el, v) { el.value = v; el.dispatchEvent(new Event("input", {bubbles: true})); el.dispatchEvent(new Event("change", {bubbles: true})); }

  mcp.ready.then(async res => {
    R.host = res.hostInfo || null;
    R.serverTools = !!(res.hostCapabilities && res.hostCapabilities.serverTools);
    try { R.eval = new Function("return 1")() === 1; } catch (e) { R.eval = false; }
    try { const s = document.createElement("script"); s.textContent = "window.__labInject = 1";
          document.head.append(s); s.remove(); R.inject = window.__labInject === 1; } catch (e) { R.inject = false; }
    R.libs = {htmx: typeof htmx !== "undefined", Alpine: typeof Alpine !== "undefined"};
    show("host", null, JSON.stringify(R.host) + " serverTools=" + R.serverTools + " eval=" + R.eval + " inject=" + R.inject);
    await sleep(150);
    const stamp = "lab-" + KIT + "-" + Math.random().toString(36).slice(2, 7);
    await step("load", async () => { $("#load").click(); await until(() => items().length); return items().length + " items"; });
    await step("add", async () => { type($("#text"), stamp); $("#add").click(); await until(() => liWith(stamp)); });
    await step("toggle swapped-in item", async () => {
      const li = liWith(stamp); if (!li) throw new Error("no item to toggle");
      li.querySelector("button").click();
      await until(() => { const x = liWith(stamp); return x && x.classList.contains("done"); });
    });
    if (KIT.startsWith("live_")) {
      await step("hx-live static binding", async () => {
        type($("#draft"), "echo-" + stamp); await until(() => $("#echo").textContent === "echo-" + stamp, 3000); });
      await step("hx-live binding in fragment", async () => {
        await until(() => $("#nlive") && $("#nlive").textContent === String(items().length), 3000); return $("#nlive").textContent; });
    }
    if (KIT === "alpine") {
      await step("alpine local toggle", async () => {
        const li = items()[0]; li.querySelector(".det").click(); await until(() => visible(li.querySelector("em")), 3000); });
      await step("alpine state survives innerMorph", async () => {
        const id = items()[0].dataset.id;
        type($("#text"), stamp + "-b"); $("#add").click(); await until(() => liWith(stamp + "-b"));
        const li = document.querySelector('li[data-id="' + id + '"]');
        if (!visible(li && li.querySelector("em"))) throw new Error("details closed after the swap");
        return "still open";
      });
    }
    if (KIT.startsWith("datastar")) {
      await step("datastar signal binding", async () => {
        type($("#text"), "sig-" + stamp); await until(() => $("#echo").textContent === "sig-" + stamp, 3000); });
    }
    if (KIT === "htmx_django") {
      await step("django footer", async () => { const s = $("#app small"); if (!s) throw new Error("no footer"); return s.textContent.trim(); });
    }
    await step("ui/update-model-context", async () => {
      const summary = Object.entries(R.steps).map(([k, v]) => k + "=" + (v.ok ? "ok" : "FAIL")).join(", ");
      await mcp.request("ui/update-model-context", {
        content: [{type: "text", text: "Toolkit lab " + KIT + ": " + summary}],
        structuredContent: {kit: KIT, steps: R.steps}}, 5000);
      return "host accepted";
    });
    R.finished = new Date().toISOString();
    show("violations", null, R.violations.length ? R.violations.slice(0, 3).join(" | ") : "none");
    out.dataset.json = JSON.stringify(R);
    try { await mcp.callTool("lab_report", {kit: KIT, results: JSON.stringify(R)}); show("reported", true); }
    catch (e) { show("reported", false, e.message); }
    out.dataset.done = "1";
  }).catch(e => { show("handshake", false, String(e)); out.dataset.done = "1"; });
})();
"""  # noqa: E501


def _vendor(name: str) -> str:
    return (HERE / "vendor" / name).read_text()


class _Store:
    """Per-kit todo lists, so variants never see each other's items."""

    def __init__(self):
        self.lock = threading.Lock()
        self.items = {k: [{"id": i + 1, "text": "seed item", "done": False}]
                      for i, k in enumerate(KITS)}
        self.next = len(KITS) + 1

    def add(self, kit, text):
        text = text.strip()[:120]
        if text:
            with self.lock:
                self.items[kit].append({"id": self.next, "text": text, "done": False})
                self.next += 1

    def toggle(self, kit, id_):
        with self.lock:
            for t in self.items[kit]:
                if str(t["id"]) == str(id_):
                    t["done"] = not t["done"]


STORE = _Store()
REPORTS: list = []


def _act(kit, tool, **q):
    """The toolkit's attribute dialect for 'call this tool and update #app'."""
    url = f"tool:{tool}?" + urlencode({"kit": kit, **q})
    if kit == "fixi":
        return (f'fx-action="{html.escape(url)}" fx-method="post" fx-target="#app" '
                f'fx-swap="innerHTML"')
    if kit.startswith("datastar"):
        return f'data-on:click="{html.escape(f"@post({url!r})")}"'
    nonce = f' hx-nonce="{NONCE}"' if kit == "live_csp" else ""
    return f'hx-post="{html.escape(url)}" hx-target="#app" hx-swap="innerMorph"{nonce}'


def _render(kit):
    rows = []
    for t in STORE.items[kit]:
        mark = "&#9745;" if t["done"] else "&#9744;"
        xdata = ' x-data="{open: false}"' if kit == "alpine" else ""
        extra = (' <button type="button" class="det" @click="open = !open">i</button>'
                 '<em x-show="open"> details</em>') if kit == "alpine" else ""
        rows.append(f'<li class="{"done" if t["done"] else ""}" data-id="{t["id"]}"{xdata}>'
                    f'<button type="button" {_act(kit, "lab_toggle", id=t["id"])}>{mark}</button> '
                    f'<span>{html.escape(t["text"])}</span>{extra}</li>')
    if kit.startswith("datastar"):
        return f'<ul id="list">{"".join(rows)}</ul>'
    out = f'<ul>{"".join(rows)}</ul>'
    if kit in LIVE_KITS:
        nonce = f' hx-nonce="{NONCE}"' if kit == "live_csp" else ""
        out += (f'<p>items (hx-live): <b id="nlive"{nonce} '
                f'hx-live:text="document.querySelectorAll(\'#app li\').length"></b></p>')
    return out


def _body(kit):
    if kit == "htmx_django":
        add = 'hx-post="/django/lab/todos/add/" hx-target="#app" hx-swap="innerMorph"'
        load = 'hx-get="/django/lab/todos/" hx-target="#app" hx-swap="innerMorph"'
    else:
        add, load = _act(kit, "lab_add"), _act(kit, "lab_list")
    nonce = f' hx-nonce="{NONCE}"' if kit == "live_csp" else ""
    if kit.startswith("datastar"):
        field = '<input id="text" data-bind:text placeholder="New todo" autocomplete="off">'
        app = '<div id="app"><ul id="list"></ul></div>'
        extra = '<p>signal echo: <b id="echo" data-text="$text"></b></p>'
    else:
        field = '<input id="text" name="text" placeholder="New todo" autocomplete="off">'
        app = '<div id="app"><em>not loaded</em></div>'
        extra = ""
    if kit in LIVE_KITS:
        extra = (f'<p>live echo: <input id="draft" placeholder="type"> <b id="echo"{nonce} '
                 f'hx-live:text="document.getElementById(\'draft\').value"></b></p>')
    return (f"<h1>{html.escape(KITS[kit])}</h1>"
            f'<form class="bar">{field}<button type="button" id="add" {add}>Add</button>'
            f'<button type="button" id="load" {load}>Reload</button></form>'
            f'{app}{extra}<ol id="res"></ol><small data-mcp-status>waiting for the host</small>')


def _page(kit):
    from micromcp.apps import BRIDGE_JS
    n = f' nonce="{NONCE}"' if kit in ("live_csp", "datastar_csp") else ""

    def tag(src, module=False):
        if "</script" in src.lower():
            raise ValueError("inlined script contains '</script'")
        kind = ' type="module"' if module else ""
        return f"<script{kind}{n}>{src}</script>"

    head = [f"<title>Toolkit lab: {html.escape(KITS[kit])}</title>", STYLE]
    if kit.startswith("datastar"):
        head.append('<meta name="mcp-fetch" content="global">')
    if kit == "live_csp":
        head.append('<meta name="htmx-config" content=\'extensions:"hx-csp, hx-live",safeEval:true\'>')
    if kit == "htmx_django":
        head.append('<meta name="mcp-route" content="lab_django_http">')
    libs = []
    if kit == "fixi":
        libs.append(tag(_vendor("fixi.js")))
    if kit in HTMX_KITS:
        libs.append(tag(_vendor("htmx.min.js")))
    if kit == "live_shim":
        libs.append(tag(EVAL_EXT_JS))
    if kit == "live_csp":
        libs.append(tag(_vendor("hx-csp.min.js")))
    if kit in LIVE_KITS:
        libs.append(tag(_vendor("hx-live.min.js")))
    if kit == "alpine":
        libs.append(tag(_vendor("alpine-csp.min.js")))
    if kit == "datastar_csp":
        libs.append(tag(f'document.documentElement.setAttribute("data-nonce", "{NONCE}");'))
    if kit.startswith("datastar"):
        libs.append(tag(_vendor("datastar.js"), module=True))
    libs.append(tag(SELFTEST_JS.replace("__KIT__", json.dumps(kit))))
    return ('<!doctype html><html><head><meta charset="utf-8">' + "".join(head) + tag(BRIDGE_JS)
            + "</head><body>" + _body(kit) + "".join(libs) + "</body></html>")


def _show_tool(kit):
    def show() -> str:
        return (f"Opened the toolkit-lab widget for {KITS[kit]}. It tests itself and "
                f"reports to lab_results.")
    show.__name__ = f"lab_{kit}"
    show.__doc__ = f"Open the self-testing toolkit-lab widget built with {KITS[kit]}."
    return show


def _widget(page_html, kit):
    def widget() -> str:
        return page_html
    widget.__name__ = f"lab_{kit}_widget"
    widget.__doc__ = f"Static toolkit-lab widget ({KITS[kit]})."
    return widget


def lab_mcp():
    from micromcp import MCP
    from micromcp.apps import Widget, fragment
    from micromcp.apps.django import django_routes

    mcp = MCP("hm-lab", "0.1.0")
    for kit in KITS:
        uri = f"ui://hm-lab/{kit}-v1"
        mcp.resource(uri, title=f"Toolkit lab: {KITS[kit]}")(_widget(_page(kit), kit))
        mcp.tool(_show_tool(kit), name=f"lab_{kit}", read_only=True,
                 title=f"Toolkit lab: {KITS[kit]}",
                 meta={"ui": {"resourceUri": uri}, "ui/resourceUri": uri})

    def frag(kit):
        if kit not in KITS or kit == "htmx_django":
            return fragment("unknown kit", status=400)
        return fragment(_render(kit))

    @mcp.tool(visibility="app", read_only=True)
    def lab_list(kit: str, text: str = ""):
        """Render a lab list (widget only)."""
        return frag(kit)

    @mcp.tool(visibility="app")
    def lab_add(kit: str, text: str = ""):
        """Add a lab item (widget only)."""
        if kit in KITS:
            STORE.add(kit, text)
        return frag(kit)

    @mcp.tool(visibility="app")
    def lab_toggle(kit: str, id: str = "", text: str = ""):
        """Toggle a lab item (widget only)."""
        if kit in KITS:
            STORE.toggle(kit, id)
        return frag(kit)

    @mcp.tool(visibility="app")
    def lab_report(kit: str, results: str) -> str:
        """Record a widget's self-test results (widget only)."""
        if kit not in KITS or len(results) > 32_000:
            return "refused"
        data = json.loads(results)
        if not isinstance(data, dict):
            return "refused"
        REPORTS.append({"kit": kit, "at": time.strftime("%H:%M:%S"), "results": data})
        del REPORTS[:-60]
        steps = data.get("steps") or {}
        print(f"LAB {kit} host={data.get('host')} eval={data.get('eval')} inject={data.get('inject')} "
              + " ".join(f"{k}={'ok' if v.get('ok') else 'FAIL'}" for k, v in steps.items()),
              flush=True)
        return "recorded"

    @mcp.tool(read_only=True)
    def lab_results() -> dict:
        """Every toolkit-lab self-test report received so far, newest last."""
        return {"reports": REPORTS}

    # htmx loaded from a CDN: Widget declares the URL's origin in _meta.ui.csp.resourceDomains,
    # which is what lets a host (and the dev host) allow it.
    cdn = Widget("lab-cdn", title="Toolkit lab: htmx from a CDN", border=True,
                 scripts=["https://cdn.jsdelivr.net/npm/htmx.org@4.0.0/dist/htmx.min.js"],
                 body='<h1>htmx from a CDN</h1><div id="app" hx-post="tool:lab_list?kit=htmx" '
                      'hx-trigger="mcp:ready" hx-target="#app" hx-swap="innerMorph">'
                      '<em>loading&hellip;</em></div><small data-mcp-status></small>')

    @cdn.tool(mcp, read_only=True, title="Toolkit lab: htmx from a CDN")
    def lab_cdn() -> str:
        """Open a widget that loads htmx from a CDN instead of inlining it."""
        return "Opened the CDN widget; it lists the htmx variant's items."

    django_routes(mcp, prefixes=["/django/lab/"], name="lab_django_http", host="localhost")
    add_counter(mcp)
    return mcp


# --- CDN lab: do hosts load https scripts whose origin the widget declares? ------------------
#
# Two widgets load htmx from jsdelivr. `cdn-declared` is a Widget, which declares the origin in
# _meta.ui.csp.resourceDomains; `cdn-undeclared` is the same page with no declaration (the
# control: if it also loads, the host is not enforcing declarations). Each reports whether htmx
# loaded and any policy violations to cdn_report, which logs a LABCDN line.

HTMX_CDN = "https://cdn.jsdelivr.net/npm/htmx.org@4.0.0/dist/htmx.min.js"

CDN_REPORT_JS = r"""
mcp.ready.then(async () => {
  await new Promise(r => setTimeout(r, 2000));
  const v = window.__cdnViolations;
  const report = {htmx: typeof htmx !== "undefined", items: document.querySelectorAll("li[data-id]").length,
                  violations: v.slice(0, 3), host: mcp.hostInfo};
  document.querySelector("[data-mcp-status]").textContent = "htmx loaded: " + report.htmx +
    " | items: " + report.items + " | violations: " + (v.join(" | ") || "none");
  mcp.callTool("cdn_report", {variant: window.CDN_VARIANT, report: JSON.stringify(report)}).catch(() => {});
});
"""  # noqa: E501


def _cdn_scripts(variant):
    watch = (f"window.CDN_VARIANT = {json.dumps(variant)}; window.__cdnViolations = []; "
             "document.addEventListener('securitypolicyviolation', e => "
             "window.__cdnViolations.push(e.violatedDirective + ' ' + e.blockedURI));")
    return [watch, HTMX_CDN, CDN_REPORT_JS]      # the listener is in place before the CDN tag


def cdn_mcp():
    from micromcp import MCP
    from micromcp.apps import Widget, fragment, page

    mcp = MCP("hm-cdn", "0.1.0")

    def body(title):
        return (f"<h1>{html.escape(title)}</h1>"
                '<div id="app" hx-post="tool:lab_list?kit=htmx" hx-trigger="mcp:ready" '
                'hx-target="#app" hx-swap="innerMorph"><em>waiting for htmx&hellip;</em></div>'
                "<small data-mcp-status>checking&hellip;</small>")

    declared = Widget("cdn-declared", title="CDN test: origin declared", border=True,
                      scripts=_cdn_scripts("declared"), body=body("htmx from a CDN (declared)"))
    undeclared = Widget("cdn-undeclared", title="CDN test: origin not declared", border=True,
                        html=page(body("htmx from a CDN (NOT declared)"),
                                  title="CDN test: origin not declared",
                                  scripts=_cdn_scripts("undeclared")))

    @declared.tool(mcp, read_only=True, title="CDN test: origin declared")
    def cdn_declared() -> str:
        """Open a widget that loads htmx from a CDN, with the origin declared in its csp."""
        return "Opened the declared-origin CDN widget; it reports whether htmx loaded."

    @undeclared.tool(mcp, read_only=True, title="CDN test: origin not declared")
    def cdn_undeclared() -> str:
        """Open the control widget: the same CDN script with no origin declared."""
        return "Opened the undeclared-origin CDN widget; it reports whether htmx loaded."

    @mcp.tool(visibility="app", read_only=True)
    def lab_list(kit: str, text: str = ""):
        """Render the htmx lab list (widget only)."""
        return fragment(_render("htmx"))

    @mcp.tool(visibility="app")
    def lab_toggle(kit: str, id: str = "", text: str = ""):
        """Toggle an htmx lab item (widget only)."""
        STORE.toggle("htmx", id)
        return fragment(_render("htmx"))

    @mcp.tool(visibility="app")
    def cdn_report(variant: str, report: str) -> str:
        """Log what a CDN widget saw (widget only)."""
        print(f"LABCDN {variant[:20]} {report[:400]}", flush=True)
        return "ok"

    return mcp


# --- context lab: what reaches the model through ui/update-model-context and ui/message ----
#
# The count is reported ONLY through model context: no model-visible tool returns it. Every push
# carries an update number and a codeword that exists only in the structured data, so asking
# the model which codewords it sees tells whether structuredContent reaches it, and whether
# successive pushes replace each other or accumulate. The server log (LABCTX) records every push.

_CTX = {"count": 0, "seq": 0}
_CTX_LOCK = threading.Lock()
_WORDS = ("amber", "birch", "cobalt", "delta", "ember", "fjord", "garnet", "harbor",
          "indigo", "juniper", "kelp", "lumen", "maple", "nectar", "onyx", "pollen")

CTX_JS = r"""
function note(t) {
  const li = document.createElement("li");
  li.textContent = new Date().toLocaleTimeString() + " " + t;
  document.getElementById("log").prepend(li);
  mcp.callTool("ctx_event", {event: t.slice(0, 300)}).catch(() => {});
}
mcp.on("mcp:context", e => note("context push -> " + e.outcome + ": " + e.text));
mcp.on("mcp:say", e => note("ui/message -> " + e.outcome));
document.addEventListener("click", e => {
  if (e.target.id !== "pushnote") return;
  const v = document.getElementById("note").value.trim();
  if (v) mcp.setContext("Counter widget local note (typed by the user): " + v, {note: v}, 0);
});
"""


def _ctx_render(n, seq):
    say = html.escape(f"Counter check-in: the widget shows count {n} (update #{seq}). Reply with "
                      f"the count and every counter codeword you can see in your context.")
    act = 'hx-target="#app" hx-swap="innerMorph"'
    return (f'<p style="font-size:18px">Count: <b id="count">{n}</b> <small>update #{seq}</small></p>'
            f'<button type="button" hx-post="tool:ctx_inc" {act}>+1</button> '
            f'<button type="button" hx-post="tool:ctx_reset" {act}>Reset</button> '
            f'<button type="button" data-mcp-say="{say}">Ask Claude (ui/message)</button>')


def add_counter(mcp):
    from micromcp.apps import fragment, page

    uri = "ui://hm-lab/context-v1"
    body = ('<h1>Context lab: counter</h1>'
            '<div id="app" hx-post="tool:ctx_view" hx-trigger="mcp:ready" hx-target="#app" '
            'hx-swap="innerMorph"><em>connecting&hellip;</em></div>'
            '<p>Local note: <input id="note" placeholder="text for the model"> '
            '<button type="button" id="pushnote">Push note (setContext)</button></p>'
            '<ol id="log" style="font:12px ui-monospace,monospace"></ol>'
            '<small data-mcp-status>waiting for the host</small>')
    widget_html = page(body, title="Context lab: counter", head=STYLE,
                       scripts=[_vendor("htmx.min.js"), CTX_JS])

    def push(change):
        with _CTX_LOCK:
            change()
            _CTX["seq"] += 1
            n, seq = _CTX["count"], _CTX["seq"]
        word = _WORDS[(seq - 1) % len(_WORDS)]
        print(f"LABCTX push #{seq} count={n} codeword={word}", flush=True)
        return fragment(_ctx_render(n, seq), context={
            "text": f"Counter widget update #{seq}: the count is {n}.",
            "data": {"update": seq, "count": n, "codeword": word}})

    @mcp.resource(uri, title="Context lab: counter")
    def ctx_widget() -> str:
        """Static counter widget for the model-context experiment."""
        return widget_html

    @mcp.tool(title="Context lab: counter", read_only=True,
              meta={"ui": {"resourceUri": uri}, "ui/resourceUri": uri})
    def ctx_open() -> str:
        """Open the counter widget for the model-context experiment."""
        return ("Opened the counter widget. This result deliberately omits the count: the widget "
                "reports it only through model context.")

    @mcp.tool(visibility="app")
    def ctx_view():
        """Render the counter (widget only)."""
        return push(lambda: None)

    @mcp.tool(visibility="app")
    def ctx_inc():
        """Increment the counter (widget only)."""
        return push(lambda: _CTX.__setitem__("count", _CTX["count"] + 1))

    @mcp.tool(visibility="app")
    def ctx_reset():
        """Reset the counter (widget only)."""
        return push(lambda: _CTX.__setitem__("count", 0))

    @mcp.tool(visibility="app")
    def ctx_event(event: str) -> str:
        """Log what the widget saw happen (widget only)."""
        print(f"LABCTX widget {event[:300]!r}", flush=True)
        return "ok"


LAB_TEMPLATES = {"lab_todos.html": """<ul>{% for t in todos %}<li class="{% if t.done %}done{% endif %}" data-id="{{ t.id }}"><button type="button" hx-post="{% url 'lab-toggle' t.id %}" hx-target="#app" hx-swap="innerMorph">{% if t.done %}&#9745;{% else %}&#9744;{% endif %}</button> <span>{{ t.text }}</span></li>{% endfor %}</ul>
<small>Django {{ version }} &middot; HX-Request seen: {{ hx }}</small>"""}  # noqa: E501


def django_urls():
    import django
    from django.shortcuts import redirect, render
    from django.urls import path
    from django.views.decorators.http import require_POST

    def lab_list(request):
        return render(request, "lab_todos.html", {
            "todos": STORE.items["htmx_django"], "version": django.get_version(),
            "hx": request.headers.get("HX-Request", "no")})

    @require_POST
    def lab_add(request):
        STORE.add("htmx_django", request.POST.get("text", ""))
        return redirect("lab-list")

    @require_POST
    def lab_toggle(request, id):
        STORE.toggle("htmx_django", id)
        return redirect("lab-list")

    return [path("django/lab/todos/", lab_list, name="lab-list"),
            path("django/lab/todos/add/", lab_add, name="lab-add"),
            path("django/lab/todos/<int:id>/toggle/", lab_toggle, name="lab-toggle")]
