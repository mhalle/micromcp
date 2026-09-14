"""MCP Apps helpers: tool visibility, fragments, model context, widget pages,
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

from micromcp import (BRIDGE_JS, CONTEXT_META, META_CAPS, META_SERVER, META_VER, MCP, PROTOCOL,
                      Server, Widget, django_routes, fragment, page, set_mcp_context)

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


# ── visibility ─────────────────────────────────────────────────────────────
print("visibility")
mcp = MCP("apps-test")


@mcp.tool(visibility="app")
def app_only() -> str:
    """Widget-only."""
    return "x"


@mcp.tool(visibility=["model", "app"], meta={"ui": {"resourceUri": "ui://w"}})
def both() -> str:
    """Model and widget."""
    return "x"


@mcp.tool(visibility="app")
def frag_tool():
    """Returns a fragment with context."""
    return fragment("<b>1</b>", context={"text": "one", "data": {"n": 1}})


rpc = client(mcp)
tools = {t["name"]: t for t in rpc("tools/list")[1]["result"]["tools"]}
check("visibility='app' is published as _meta.ui.visibility",
      tools["app_only"]["_meta"]["ui"], {"visibility": ["app"]})
check("visibility merges into meta's ui object",
      tools["both"]["_meta"]["ui"], {"resourceUri": "ui://w", "visibility": ["model", "app"]})
for label, kwargs in [("unknown audience", {"visibility": "user"}),
                      ("empty", {"visibility": []}),
                      ("duplicates", {"visibility": ["app", "app"]}),
                      ("conflicts with meta", {"visibility": "app",
                                               "meta": {"ui": {"visibility": ["model"]}}}),
                      ("meta ui not a dict", {"visibility": "app", "meta": {"ui": "x"}})]:
    check(f"visibility refused: {label}",
          raises(lambda kw=kwargs: mcp.tool(lambda: 1, name="bad", **kw)), "ValueError")

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
              "/secret/", "app/list/", "/app/list/\x00", "/ap"]:
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
        ("a script of the wrong type", lambda: Widget("x", body="x", scripts=[42]), TypeError)]:
    check(f"Widget refused: {label}", raises(make, exc), exc.__name__)

wm = MCP("widget-test")


@wm.tool(widget=w, read_only=True)
def show_board() -> str:
    """Show the board."""
    return "ok"


@wm.tool(widget=w, visibility=["model", "app"])
def show_board_too() -> str:
    """Show the board again."""
    return "ok"


wrpc = client(wm)
wt = {t["name"]: t["_meta"] for t in wrpc("tools/list")[1]["result"]["tools"]}
check("tool(widget=) names the resource under both keys",
      wt["show_board"], {"ui": {"resourceUri": "ui://board"}, "ui/resourceUri": "ui://board"})
check("widget= and visibility= merge", wt["show_board_too"]["ui"],
      {"resourceUri": "ui://board", "visibility": ["model", "app"]})
listed = wrpc("resources/list")[1]["result"]["resources"]
check("two tools, one resource, with the MCP App MIME type",
      [(r["uri"], r["mimeType"]) for r in listed], [("ui://board", "text/html;profile=mcp-app")])
check("the listing carries the widget's meta", listed[0].get("_meta"), w.meta)
read = wrpc("resources/read", {"uri": "ui://board"})[1]["result"]["contents"][0]
check("resources/read returns the page", read["text"], w.html)
check("a different widget at the same uri is refused",
      raises(lambda: wm.tool(lambda: 1, name="other", widget=Widget("board", body="other"))),
      "ValueError")
check("a refused tool registers nothing", "other" in wm.tools, False)
check("meta naming another resource conflicts with widget=",
      raises(lambda: wm.tool(lambda: 1, name="x3", widget=w,
                             meta={"ui": {"resourceUri": "ui://elsewhere"}})), "ValueError")
check("a widget must be a Widget",
      raises(lambda: wm.tool(lambda: 1, name="x4", widget="ui://board"), TypeError), "TypeError")
check("one widget can serve several servers",
      raises(lambda: MCP("second").tool(lambda: 1, name="y", widget=w)), None)
if django:
    check("django_routes returns its tool name, for Widget(route=)",
          django_routes(MCP("named"), prefixes=["/app/"]), "django_http")

print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
