"""micromcp-apps: fragments, model context, widget pages, Widget, channels,
and Django views served to widgets through django_routes.

The bridge's JavaScript is exercised end to end in examples/devhost.html (see
the README); here it is syntax-checked when node is installed.
"""
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

from micromcp import META_CAPS, META_SERVER, META_VER, MCP, PROTOCOL, Server
from micromcp_apps import BRIDGE_JS, CONTEXT_META, Channel, Widget, fragment, page
from micromcp_apps.django import django_routes, set_mcp_context

OK = FAIL = 0


def check(label, got, want):
    global OK, FAIL
    good = got == want
    OK, FAIL = OK + good, FAIL + (not good)
    print(f"  {'PASS' if good else 'FAIL'}  {label}")
    if not good:
        print(f"        got  {got!r}\n        want {want!r}")


def raises(fn, exc=ValueError):
    try:
        fn()
    except exc as e:
        return type(e).__name__
    except Exception as e:  # noqa: BLE001 - the test reports the wrong type
        return f"wrong: {type(e).__name__}: {e}"
    return None


def client(mcp, authenticate=None):
    srv = Server(mcp, authenticate=authenticate)

    def rpc(method, params=None, auth=None):
        params = dict(params or {})
        params["_meta"] = {META_VER: PROTOCOL, META_CAPS: {}}
        raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        env = {"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(raw)),
               "wsgi.input": io.BytesIO(raw),
               "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL, "HTTP_MCP_METHOD": method}
        if params.get("name") or params.get("uri"):
            env["HTTP_MCP_NAME"] = params.get("name") or params["uri"]
        if auth:
            env["HTTP_AUTHORIZATION"] = auth
        box = {}
        out = b"".join(srv(env, lambda s, h: box.update(s=s)))
        return int(box["s"].split()[0]), json.loads(out)
    return rpc


mcp = MCP("apps-test")


@mcp.tool(visibility="app")
def frag_tool():
    """Returns a fragment with context."""
    return fragment("<b>1</b>", context={"text": "one", "data": {"n": 1}})


rpc = client(mcp)

# ── fragments and context ──────────────────────────────────────────────────
print("fragments")
f = fragment("<p>hi</p>")
check("fragment is one text block", f["content"], [{"type": "text", "text": "<p>hi</p>"}])
check("fragment carries its status", f["_meta"]["micromcp/http"],
      {"status": 200, "contentType": "text/html; charset=utf-8"})
check("fragment under 400 is not an error", "isError" in f, False)
check("fragment at 404 is an error", fragment("x", status=404).get("isError"), True)
check("fragment refuses a non-status", raises(lambda: fragment("x", status=99)), "ValueError")
check("fragment refuses bytes", raises(lambda: fragment(b"x"), TypeError), "TypeError")
check("context from a str", fragment("x", context="hi")["_meta"][CONTEXT_META], {"text": "hi"})
check("context from a dict", fragment("x", context={"text": "t", "data": {"n": 1}})
      ["_meta"][CONTEXT_META], {"text": "t", "data": {"n": 1}})
for label, bad in [("text not a str", {"text": 1}), ("data not a dict", {"text": "t", "data": [1]}),
                   ("unknown key", {"text": "t", "extra": 1}), ("too large", "a" * 20_000),
                   ("NaN in data", {"text": "t", "data": {"x": float("nan")}}),
                   ("not a str or dict", 5)]:
    check(f"context refused: {label}", raises(lambda b=bad: fragment("x", context=b)), "ValueError")
res = rpc("tools/call", {"name": "frag_tool", "arguments": {}})[1]["result"]
check("tools/call returns the fragment", res["content"], [{"type": "text", "text": "<b>1</b>"}])
check("tools/call carries the context", res["_meta"][CONTEXT_META],
      {"text": "one", "data": {"n": 1}})
check("tools/call keeps the server stamp", META_SERVER in res["_meta"], True)

# ── widget pages ───────────────────────────────────────────────────────────
print("pages")
doc = page("<p>x</p>", title="A & B", head="<style></style>", scripts=["var mine = 1;"])
check("page inlines the bridge before the page's scripts",
      0 < doc.index(BRIDGE_JS) < doc.index("var mine = 1;"), True)
check("page escapes the title", "<title>A &amp; B</title>" in doc, True)
check("page refuses a script that would close its tag",
      raises(lambda: page("", scripts=["x</SCRIPT >"])), "ValueError")
node = shutil.which("node")
if node:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as tmp:
        tmp.write(BRIDGE_JS)
    proc = subprocess.run([node, "--check", tmp.name], capture_output=True, text=True)
    os.unlink(tmp.name)
    check("BRIDGE_JS parses (node --check)", (proc.returncode, proc.stderr[:300]), (0, ""))
else:
    print("  skip  BRIDGE_JS syntax (node not installed)")

# ── django_routes ──────────────────────────────────────────────────────────
print("django_routes")
try:
    import django
    from django.conf import settings
except ImportError:
    django = None
    print("  skip  (django not installed)")

if django:
    class URLs:
        urlpatterns: list = []

    settings.configure(DEBUG=False, SECRET_KEY="test", ROOT_URLCONF=URLs, ALLOWED_HOSTS=["localhost"],
                       INSTALLED_APPS=[], DATABASES={},
                       MIDDLEWARE=["django.middleware.csrf.CsrfViewMiddleware",
                                   "django.middleware.common.CommonMiddleware"])
    django.setup()
    from django.http import HttpResponse
    from django.shortcuts import redirect
    from django.urls import path

    seen = {}

    def listing(request):
        return HttpResponse(f"list hx={request.headers.get('HX-Request', '-')} "
                            f"who={request.mcp_principal} m={request.method}")

    def add(request):
        seen["add"] = (request.method, request.POST.get("text"))
        return set_mcp_context(redirect("listing"), "added", {"text": request.POST.get("text")})

    def js(request):
        return HttpResponse("got " + json.loads(request.body)["k"])

    def away(request):
        return redirect("https://evil.example/x")

    def loop(request):
        return redirect("loop")

    def badctx(request):
        r = HttpResponse("ok")
        r["X-MCP-Context"] = "not json"
        return r

    def secret(request):
        return HttpResponse("SECRET")

    URLs.urlpatterns = [path("app/list/", listing, name="listing"), path("app/add/", add),
                        path("app/json/", js), path("app/away/", away),
                        path("app/loop/", loop, name="loop"), path("app/badctx/", badctx),
                        path("secret/", secret)]
    dmcp = MCP("dj")
    django_routes(dmcp, prefixes=["/app/"])
    drpc = client(dmcp, authenticate=lambda h: {"sub": "u1"}
                  if h.get("authorization") == "Bearer t" else None)

    def call(auth="Bearer t", **args):
        r = drpc("tools/call", {"name": "django_http", "arguments": args}, auth=auth)[1]
        r = r.get("result") or r
        meta = r.get("_meta") or {}
        return (r.get("isError", False), meta.get("micromcp/http", {}).get("status"),
                "".join(b.get("text", "") for b in r.get("content", [])), meta.get(CONTEXT_META))

    dtools = {t["name"]: t for t in drpc("tools/list")[1]["result"]["tools"]}
    check("the route tool is app-only", dtools["django_http"]["_meta"]["ui"], {"visibility": ["app"]})
    check("GET reaches the view with forwarded headers and the principal",
          call(method="GET", path="/app/list/", headers={"hx-request": "true"}),
          (False, 200, "list hx=true who={'sub': 'u1'} m=GET", None))
    check("an anonymous call reaches the view with no principal",
          call(auth=None, method="GET", path="/app/list/")[2], "list hx=- who=None m=GET")
    check("POST passes CSRF, follows the redirect as GET, and carries the view's context",
          call(method="POST", path="/app/add/", body="text=milk"),
          (False, 200, "list hx=- who={'sub': 'u1'} m=GET", {"text": "added", "data": {"text": "milk"}}))
    check("the POST view saw the form", seen.get("add"), ("POST", "milk"))
    check("a JSON body reaches the view",
          call(method="POST", path="/app/json/", body='{"k": "v"}',
               content_type="application/json")[:3], (False, 200, "got v"))
    check("an unsupported content type is refused",
          call(method="POST", path="/app/json/", body="x", content_type="text/plain")[:2], (True, 415))
    check("an unknown method is refused", call(method="TRACE", path="/app/list/")[:2], (True, 405))
    for p in ["/app/../secret/", "/app/%2e%2e/secret/", "/app/%2E%2E/secret/",
              "https://evil.example/app/list/", "//evil.example/app/list/", "/app\\..\\secret/",
              "/secret/", "app/list/", "/app/list/\x00", "/ap", "/app/x%0d%0aSet-Cookie:a=b/",
              "/app/list/%00", "/app/%ff/", "/app/list/?a=\r\n", "/app/\tlist/"]:
        check(f"path confined: {p!r}", call(method="GET", path=p)[:3],
              (True, 404, "path not served to MCP Apps"))
    check("a redirect off the host is refused", call(method="GET", path="/app/away/")[:2], (True, 502))
    check("a redirect loop ends", call(method="GET", path="/app/loop/")[:2], (True, 508))
    check("a non-hypermedia header is refused",
          call(method="GET", path="/app/list/", headers={"cookie": "a=b"})[:2], (True, 400))
    check("a header value with a newline is refused",
          call(method="GET", path="/app/list/", headers={"hx-request": "a\nb"})[:2], (True, 400))
    check("more than 16 headers are refused",
          call(method="GET", path="/app/list/",
               headers={f"hx-h{i}": "x" for i in range(17)})[:2], (True, 400))
    check("an invalid X-MCP-Context header is dropped, the page still served",
          call(method="GET", path="/app/badctx/"), (False, 200, "ok", None))
    check("prefixes must be slash-delimited paths",
          raises(lambda: django_routes(MCP("x"), prefixes=["app"])), "ValueError")
    check("prefixes must not be empty", raises(lambda: django_routes(MCP("x"), prefixes=[])),
          "ValueError")

# ── Widget ─────────────────────────────────────────────────────────────────
print("Widget")
lib = pathlib.Path(tempfile.mkdtemp()) / "lib.js"
lib.write_text("window.libLoaded = 1;")
w = Widget("board", title="Board", body='<div id="app"></div>', styles="body{margin:0}",
           scripts=[lib, "https://cdn.example.org/x/htmx.min.js"],
           modules=["https://esm.example.net/ds.js"], route="django_http", fetch="global",
           csp={"connectDomains": ["https://api.example.org"]}, border=True)
doc = w.html
check("uri defaults to ui://<name>", w.uri, "ui://board")
check("route and fetch metas come before the bridge runs",
      doc.index('name="mcp-route" content="django_http"') < doc.index(BRIDGE_JS)
      and doc.index('name="mcp-fetch" content="global"') < doc.index(BRIDGE_JS), True)
check("a Path script is read and inlined after the bridge",
      doc.index(BRIDGE_JS) < doc.index("<script>window.libLoaded = 1;</script>"), True)
check("an https script loads by src",
      '<script src="https://cdn.example.org/x/htmx.min.js"></script>' in doc, True)
check("an https module loads as type=module",
      '<script type="module" src="https://esm.example.net/ds.js"></script>' in doc, True)
check("styles are inlined", "<style>body{margin:0}</style>" in doc, True)
check("resource meta: asset origins declared, csp merged, border set", w.meta,
      {"ui": {"csp": {"connectDomains": ["https://api.example.org"],
                      "resourceDomains": ["https://cdn.example.org", "https://esm.example.net"]},
              "prefersBorder": True}})
wi = Widget("three", body="x", modules=["import * as T from 'three';"],
            imports={"three": "https://cdn.example.org/three/three.module.js",
                     "three/addons/": "https://cdn.example.org/three/jsm/"})
check("imports= writes an import map before the modules",
      0 < wi.html.index('<script type="importmap">{"imports": {"three": ')
      < wi.html.index("<script type=\"module\">import * as T from 'three';</script>"), True)
check("the import map's origins are declared", wi.meta,
      {"ui": {"csp": {"resourceDomains": ["https://cdn.example.org"]}}})
check("imports must be https URLs",
      raises(lambda: Widget("x", body="x", imports={"three": "http://cdn.example.org/t.js"})),
      "ValueError")
check("imports must be a dict",
      raises(lambda: Widget("x", body="x", imports=["three"]), TypeError), "TypeError")
check("a // comment is still inline source",
      "<script>// setup\nwindow.a = 1;</script>" in Widget("x", body="x",
                                                           scripts=["// setup\nwindow.a = 1;"]).html,
      True)
check("CSS with a pseudo-class is still inline source",
      "<style>a:hover{color:red}</style>" in Widget("x", body="x", styles=["a:hover{color:red}"]).html,
      True)
check("csp takes subdomain wildcards, wss, and ports",
      Widget("x", body="x", csp={"connectDomains": ["https://*.example.org",
                                                    "wss://live.example.org:8443"]}).meta,
      {"ui": {"csp": {"connectDomains": ["https://*.example.org", "wss://live.example.org:8443"]}}})
check("html= is used verbatim", Widget("own", html="<p>mine</p>").html, "<p>mine</p>")
check("no csp and no border means no resource meta", Widget("plain", body="x").meta, None)
for label, make, exc in [
        ("neither body nor html", lambda: Widget("x"), TypeError),
        ("both body and html", lambda: Widget("x", body="a", html="b"), TypeError),
        ("html with page options", lambda: Widget("x", html="<p>", scripts=["a"]), TypeError),
        ("a name with spaces", lambda: Widget("no spaces", body="x"), ValueError),
        ("a uri that is not ui://", lambda: Widget("x", body="x", uri="https://x"), ValueError),
        ("an unknown fetch mode", lambda: Widget("x", body="x", fetch="all"), ValueError),
        ("an unknown csp key", lambda: Widget("x", body="x", csp={"scriptDomains": []}), ValueError),
        ("a csp value that is a str", lambda: Widget("x", body="x", csp={"connectDomains": "https://a"}),
         ValueError),
        ("an http script", lambda: Widget("x", body="x", scripts=["http://insecure.example/a.js"]),
         ValueError),
        ("a path passed as a str", lambda: Widget("x", body="x", scripts=["vendor/htmx.min.js"]),
         ValueError),
        ("a script that closes its tag", lambda: Widget("x", body="x", scripts=["a</script>b"]),
         ValueError),
        ("a style that closes its tag", lambda: Widget("x", body="x", styles=["a</style>"]), ValueError),
        ("a route that is not a tool name", lambda: Widget("x", body="x", route="no spaces!"), ValueError),
        ("a URL with a trailing space",
         lambda: Widget("x", body="x", scripts=["https://cdn.example.org/a.js "]), ValueError),
        ("a URL with a quote",
         lambda: Widget("x", body="x", scripts=['https://cdn.example.org/a.js?y="2']), ValueError),
        ("a protocol-relative URL",
         lambda: Widget("x", body="x", scripts=["//cdn.example.org/a.js"]), ValueError),
        ("a URL with credentials",
         lambda: Widget("x", body="x", scripts=["https://u:p@cdn.example.org/a.js"]), ValueError),
        ("an import with credentials",
         lambda: Widget("x", body="x", imports={"t": "https://u@cdn.example.org/t.js"}), ValueError),
        ("a csp wildcard", lambda: Widget("x", body="x", csp={"connectDomains": ["*"]}), ValueError),
        ("a csp entry carrying a directive",
         lambda: Widget("x", body="x", csp={"resourceDomains": ["https://a.example; script-src *"]}),
         ValueError),
        ("a csp entry with another scheme",
         lambda: Widget("x", body="x", csp={"frameDomains": ["javascript:alert(1)"]}), ValueError),
        ("a script of the wrong type", lambda: Widget("x", body="x", scripts=[42]), TypeError)]:
    check(f"Widget refused: {label}", raises(make, exc), exc.__name__)

wm = MCP("widget-test")


@w.tool(wm, read_only=True)
def show_board() -> str:
    """Show the board."""
    return "ok"


@w.tool(wm, visibility=["model", "app"])
def show_board_too() -> str:
    """Show the board again."""
    return "ok"


wrpc = client(wm)
wt = {t["name"]: t["_meta"] for t in wrpc("tools/list")[1]["result"]["tools"]}
check("Widget.tool names the resource under both keys",
      wt["show_board"], {"ui": {"resourceUri": "ui://board"}, "ui/resourceUri": "ui://board"})
check("Widget.tool and visibility= merge", wt["show_board_too"]["ui"],
      {"resourceUri": "ui://board", "visibility": ["model", "app"]})
check("tool_meta is the same pointer, for a tool registered by hand", w.tool_meta,
      wt["show_board"])
listed = wrpc("resources/list")[1]["result"]["resources"]
check("two tools, one resource, with the MCP App MIME type",
      [(r["uri"], r["mimeType"]) for r in listed], [("ui://board", "text/html;profile=mcp-app")])
check("the listing carries the widget's meta", listed[0].get("_meta"), w.meta)
read = wrpc("resources/read", {"uri": "ui://board"})[1]["result"]["contents"][0]
check("resources/read returns the page", read["text"], w.html)
check("register() is idempotent", (w.register(wm), len(wm.resources)), ("ui://board", 1))
check("a different widget at the same uri is refused",
      raises(lambda: Widget("board", body="other").tool(wm, lambda: 1, name="other")),
      "ValueError")
check("a refused tool registers nothing", "other" in wm.tools, False)
check("meta naming another resource is refused",
      raises(lambda: w.tool(wm, lambda: 1, name="x3",
                            meta={"ui": {"resourceUri": "ui://elsewhere"}})), "ValueError")
m2 = MCP("refused")
check("a tool the registry refuses ...",
      raises(lambda: w.tool(m2, lambda: 1, name="no spaces")), "ValueError")
check("... publishes no widget", "ui://board" in m2.resources, False)
check("one widget can serve several servers",
      raises(lambda: w.tool(MCP("second"), lambda: 1, name="y")), None)
if django:
    check("django_routes returns its tool name, for Widget(route=)",
          django_routes(MCP("named"), prefixes=["/app/"]), "django_http")

# ── channels ───────────────────────────────────────────────────────────────
print("channels")
import threading  # noqa: E402
import time  # noqa: E402

cm = MCP("channel-test")
events = []
room = Channel(cm, "room")


@room.on_connect
def room_joined(conn):
    events.append(("connect", conn.params, conn.principal))
    conn.send_json({"hello": conn.params.get("who", "?")})


@room.on_message
async def room_message(conn, text):
    events.append(("message", text))
    conn.send("echo:" + text)


@room.on_disconnect
def room_left(conn):
    events.append(("disconnect", conn.id))


Channel(cm, "private", guards=[lambda p: p is not None])
small = Channel(cm, "small", max_queue=3)
brief = Channel(cm, "brief", idle=0.2)
crpc = client(cm, authenticate=lambda h: {"sub": "u1"}
              if h.get("authorization") == "Bearer t" else None)


def ctool(name, args, auth=None):
    r = crpc("tools/call", {"name": name, "arguments": args}, auth=auth)[1]
    return (r.get("result") or {}).get("structuredContent") or r


listed = {t["name"]: t for t in crpc("tools/list")[1]["result"]["tools"]}
check("four channel tools, registered once, all app-only",
      sorted((n, listed[n]["_meta"]["ui"]["visibility"][0]) for n in listed if n.startswith("channel_")),
      [("channel_close", "app"), ("channel_open", "app"), ("channel_recv", "app"),
       ("channel_send", "app")])
check("a channel name is registered once", raises(lambda: Channel(cm, "room")), "ValueError")
check("a channel name must be a tool-style name", raises(lambda: Channel(cm, "no spaces")),
      "ValueError")
o = ctool("channel_open", {"channel": "room", "params": "who=ann&partial=1"})
check("open runs on_connect and returns its frames",
      (bool(o["conn"]), o["frames"], o["closed"]), (True, ['{"hello":"ann"}'], False))
check("on_connect sees the URL query and the principal", events[0], ("connect", {"who": "ann", "partial": "1"}, None))
ctool("channel_send", {"conn": o["conn"], "data": "hi"})
r = ctool("channel_recv", {"conn": o["conn"], "wait": 0})
check("a sent frame reaches on_message, and its reply comes back through recv",
      (events[-1], r["frames"]), (("message", "hi"), ["echo:hi"]))
t0 = time.monotonic()
threading.Timer(0.3, lambda: room.broadcast_json({"n": 1})).start()
r = ctool("channel_recv", {"conn": o["conn"], "wait": 5})
check("a waiting recv returns as soon as something is broadcast",
      (r["frames"], time.monotonic() - t0 < 2), (['{"n":1}'], True))
t0 = time.monotonic()
r = ctool("channel_recv", {"conn": o["conn"], "wait": 0.5})
check("an idle recv waits out its wait and returns empty",
      (r["frames"], r["closed"], 0.4 < time.monotonic() - t0 < 2), ([], False, True))
a = ctool("channel_open", {"channel": "room"}, auth="Bearer t")
check("another principal cannot use a connection",
      ctool("channel_recv", {"conn": a["conn"], "wait": 0})["closed"], True)
check("its own principal can", ctool("channel_recv", {"conn": a["conn"], "wait": 0}, auth="Bearer t")
      ["closed"], False)
room.broadcast("to all")
check("broadcast reaches every open connection",
      (ctool("channel_recv", {"conn": o["conn"]})["frames"],
       ctool("channel_recv", {"conn": a["conn"]}, auth="Bearer t")["frames"]),
      (["to all"], ["to all"]))
check("guards refuse an open", ctool("channel_open", {"channel": "private"})["conn"], None)
check("guards admit a permitted principal",
      bool(ctool("channel_open", {"channel": "private"}, auth="Bearer t")["conn"]), True)
check("an unknown channel refuses", ctool("channel_open", {"channel": "nowhere"})["conn"], None)
ctool("channel_close", {"conn": o["conn"]})
check("close runs on_disconnect", events[-1], ("disconnect", o["conn"]))
check("a closed connection reports closed",
      ctool("channel_recv", {"conn": o["conn"], "wait": 0})["closed"], True)
b = ctool("channel_open", {"channel": "room"})
[server_side] = [c for c in room.connections if c.id == b["conn"]]
server_side.send("last words")
server_side.close()
r = ctool("channel_recv", {"conn": b["conn"], "wait": 0})
check("a server-side close delivers what was queued, then closes",
      (r["frames"], r["closed"], events[-1]), (["last words"], True, ("disconnect", b["conn"])))
x = ctool("channel_open", {"channel": "small"})
for i in range(4):
    small.broadcast(str(i))
r = ctool("channel_recv", {"conn": x["conn"], "wait": 0})
check("a connection that falls max_queue behind is closed",
      (r["frames"], r["closed"]), (["0", "1", "2"], True))
y = ctool("channel_open", {"channel": "brief"})
time.sleep(0.35)
ctool("channel_open", {"channel": "brief"})             # any channel request sweeps
check("an idle connection is dropped", ctool("channel_recv", {"conn": y["conn"]})["closed"], True)
check("Connection.send takes text", raises(lambda: a and room.connections[0].send({"x": 1}), TypeError),
      "TypeError")
check("a channel's wait must not be negative",
      raises(lambda: Channel(cm, "bad", wait=-1)), "ValueError")
taken = MCP("taken")
taken.tool(lambda: 1, name="channel_open")
check("a server whose tool names are taken refuses its first channel",
      (raises(lambda: Channel(taken, "x")), "channel_send" in taken.tools), ("ValueError", False))
check("channels are per server", bool(Channel(MCP("other"), "room")), True)


for label, kw in [("an infinite wait", {"wait": float("inf")}), ("a NaN wait", {"wait": float("nan")}),
                  ("a NaN idle", {"idle": float("nan")}), ("zero max_bytes", {"max_bytes": 0}),
                  ("zero max_connections", {"max_connections": 0})]:
    check(f"Channel refuses {label}", raises(lambda kw=kw: Channel(MCP("limits"), "c", **kw)),
          "ValueError")
short = Channel(cm, "short", wait=0.3)
s = ctool("channel_open", {"channel": "short"})["conn"]
for label, w in [("NaN", float("nan")), ("Infinity", float("inf"))]:
    t0 = time.monotonic()
    ctool("channel_recv", {"conn": s, "wait": w})     # refused by the parser, or capped here
    check(f"recv with wait={label} returns within the channel's cap", time.monotonic() - t0 < 1.5, True)
t0 = time.monotonic()
asyncio_run = __import__("asyncio").run
asyncio_run([c for c in short.connections if c.id == s][0]._wait(float("nan")))
check("a NaN wait does not wait", time.monotonic() - t0 < 0.2, True)

fragile = Channel(cm, "fragile")
gone = []


@fragile.on_message
def fragile_message(conn, text):
    raise RuntimeError("db password is hunter2")


@fragile.on_disconnect
def fragile_left(conn):
    gone.append(conn.id)


f = ctool("channel_open", {"channel": "fragile"})["conn"]
raw_send = crpc("tools/call", {"name": "channel_send", "arguments": {"conn": f, "data": "x"}})[1]
check("an on_message that raises closes the connection",
      raw_send["result"]["structuredContent"]["closed"], True)
check("... without sending the exception to the client", "hunter2" in json.dumps(raw_send), False)
check("... and runs on_disconnect", gone, [f])

sized = Channel(cm, "sized", max_bytes=10)
z = ctool("channel_open", {"channel": "sized"})["conn"]
sized.broadcast("12345")
sized.broadcast("é1234")                 # 6 bytes in UTF-8: over the 10-byte budget
r = ctool("channel_recv", {"conn": z, "wait": 0})
check("a connection over max_bytes (UTF-8) is closed after what fit",
      (r["frames"], r["closed"]), (["12345"], True))

few = Channel(cm, "few", max_connections=2)
k1 = ctool("channel_open", {"channel": "few"})["conn"]
k2 = ctool("channel_open", {"channel": "few"})["conn"]
check("an open beyond max_connections is refused",
      ctool("channel_open", {"channel": "few"})["conn"], None)
ctool("channel_close", {"conn": k1})
check("... until one closes", bool(ctool("channel_open", {"channel": "few"})["conn"]), True)

print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
