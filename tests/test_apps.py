"""micromcp.apps: fragments, model context, widget pages, Widget, channels,
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

try:
    import micromcp.apps  # noqa: F401
except ModuleNotFoundError:            # the single-file bundle carries only the core
    print("skip: micromcp.apps is not part of the single-file bundle")
    sys.exit(0)

from micromcp import META_CAPS, META_SERVER, META_VER, MCP, PROTOCOL, Server
from micromcp.apps import (BRIDGE_JS, BRIDGE_TYPES, CONTEXT_META, Channel, SupportsHTML, Widget,
                           fragment, page, tool_url)
from micromcp.apps.django import django_routes, set_mcp_context

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


def message(fn):
    """The text of whatever a call raises: some messages are the feature."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - the message is what is under test
        return str(e)
    return ""


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

# ── markup objects (__html__) ──────────────────────────────────────────────
print("markup objects")


class Html:                       # the markupsafe protocol, as FastHTML and htpy implement it
    def __init__(self, s):
        self.s = s

    def __html__(self):
        return self.s


class Marked(str):                # like markupsafe.Markup: escapes whatever is added to it
    def __html__(self):
        return self

    def __radd__(self, other):
        return Marked(other.replace("<", "&lt;") + str(self))


class Anything:                   # answers every attribute, __html__ included
    def __getattr__(self, name):
        return lambda: "<p>not markup</p>"


class Bytes:
    def __html__(self):
        return b"<p>bytes</p>"


f = fragment(Html("<p>hi</p>"))
check("fragment renders an __html__ object", f["content"],
      [{"type": "text", "text": "<p>hi</p>"}])
check("fragment text is an exact str", type(fragment(Marked("<i>"))["content"][0]["text"]), str)
for label, bad in [("an int", 5), ("__html__ from __getattr__", Anything()),
                   ("__html__ returning bytes", Bytes())]:
    check(f"fragment refuses {label}", raises(lambda b=bad: fragment(b), TypeError), "TypeError")
doc = page(Html('<div id="app"></div>'), head=Html('<meta name="x">'))
check("page renders body and head objects",
      ('<meta name="x">' in doc, '<div id="app"></div>' in doc), (True, True))
doc = page(Marked("<p>m</p>"))
check("a Markup-like body does not escape the page around it",
      ("<head><meta charset" in doc, "<p>m</p>" in doc), (True, True))
check("Widget(body=) renders an object", "<b>w</b>" in Widget("mk1", body=Html("<b>w</b>")).html,
      True)
check("Widget(html=) uses an object verbatim",
      Widget("mk2", html=Html("<!doctype html><p>v</p>")).html, "<!doctype html><p>v</p>")
check("Widget(body=) refuses an int", raises(lambda: Widget("mk3", body=5), TypeError),
      "TypeError")
import enum  # noqa: E402


class Status(str, enum.Enum):     # a str that renders itself: __html__ wins, as in Jinja
    OPEN = "open"

    def __html__(self):
        return f'<span class="badge">{self.value}</span>'


class PlainText(str):             # text that escapes itself
    def __html__(self):
        return self.replace("&", "&amp;").replace("<", "&lt;")


class MetaAnything(type):         # a metaclass that answers every name, __html__ included
    def __getattr__(cls, name):
        return lambda *a: "<p>not markup</p>"


class ViaMeta(metaclass=MetaAnything):
    pass


class NoneHtml:
    __html__ = None               # Python's spelling of "not supported"


class Static:
    @staticmethod
    def __html__():
        return "<p>static</p>"


class Pretend:                    # a proxy that claims to be a str (Mock(spec=str), SimpleLazyObject)
    @property
    def __class__(self):
        return str


class EscapingStr(str):           # like Markup: its replace() escapes the replacement
    def replace(self, old, new, count=-1):
        return EscapingStr(str.replace(self, old, new.replace("&", "&amp;"), count))


text = lambda v: fragment(v)["content"][0]["text"]
inst = Html("<p>class</p>")
inst.__html__ = lambda: "<p>instance</p>"
check("a str subclass's own __html__ wins", text(Status.OPEN), '<span class="badge">open</span>')
check("text that escapes itself is escaped", text(PlainText("<b>")), "&lt;b>")
check("the class's __html__ wins over the instance's", text(inst), "<p>class</p>")
check("a staticmethod __html__ renders", text(Static()), "<p>static</p>")
for label, bad in [("__html__ from a metaclass __getattr__", ViaMeta()),
                   ("__html__ = None", NoneHtml()), ("a proxy that claims to be a str", Pretend())]:
    check(f"fragment refuses {label}", raises(lambda b=bad: fragment(b), TypeError), "TypeError")
check("a Markup-like title is escaped once",
      "<title>A &amp; B</title>" in page("", title=EscapingStr("A & B")), True)
check("page refuses a title that is not a str", raises(lambda: page("", title=5), TypeError),
      "TypeError")
check("SupportsHTML is exported", SupportsHTML.__name__, "SupportsHTML")

# ── tool_url ───────────────────────────────────────────────────────────────
print("tool_url")
from urllib.parse import parse_qs  # noqa: E402

u = tool_url("item_remove", name="milk&role=admin", n=3, q="a+b c")
check("tool_url percent-encodes values", u,
      "tool:item_remove?name=milk%26role%3Dadmin&n=3&q=a%2Bb%20c")
check("... which decode back to exactly the arguments given", parse_qs(u.split("?", 1)[1]),
      {"name": ["milk&role=admin"], "n": ["3"], "q": ["a+b c"]})
if shutil.which("node"):          # the bridge parses the query with URLSearchParams
    js = "console.log(JSON.stringify(Object.fromEntries(new URLSearchParams(process.argv[1]))))"
    proc = subprocess.run([shutil.which("node"), "-e", js, u.split("?", 1)[1]],
                          capture_output=True, text=True)
    check("... and so does the bridge's URLSearchParams", json.loads(proc.stdout or "null"),
          {"name": "milk&role=admin", "n": "3", "q": "a+b c"})
else:
    print("  skip  URLSearchParams decoding (node not installed)")
check("tool_url with no arguments", tool_url("todo_list"), "tool:todo_list")
for label, fn, exc in [("a bad tool name", lambda: tool_url("a b"), ValueError),
                       ("a name with a trailing newline", lambda: tool_url("ok\n"), ValueError),
                       ("a dict value", lambda: tool_url("t", x={"a": 1}), TypeError),
                       ("a bool value", lambda: tool_url("t", x=True), TypeError)]:
    check(f"tool_url refuses {label}", raises(fn, exc), exc.__name__)

# ── bundled widgets ────────────────────────────────────────────────────────
print("bundled widgets")
import logging  # noqa: E402

bundle_dir = pathlib.Path(tempfile.mkdtemp())
(bundle_dir / "index.html").write_text(
    '<!doctype html><html><head><title>t</title></head><body><div id="root"></div>'
    '<script type="module">window.built = 1</script></body></html>')
(bundle_dir / "body.html").write_text('<div id="root"></div>')
w = Widget("bundled", html=bundle_dir / "index.html")
check("html= takes a path to a complete page", '<div id="root"></div>' in w.html, True)
check("... used verbatim, without the bridge", BRIDGE_JS in w.html, False)
doc = Widget("bridged", html=bundle_dir / "index.html", bridge=True, route="django_http",
             fetch="global").html
check("bridge=True puts the bridge first in the head",
      doc.index("<head>") < doc.index(BRIDGE_JS) < doc.index("<title>"), True)
check("... after its route and fetch settings",
      doc.index('name="mcp-route"') < doc.index('name="mcp-fetch"') < doc.index(BRIDGE_JS), True)
check("a page without a head gets the bridge too",
      BRIDGE_JS in Widget("headless", html="<p>x</p>", bridge=True).html, True)
check("bridge=True refuses a page that already has the bridge",
      raises(lambda: Widget("twice", html=doc, bridge=True)), "ValueError")
check("route= with html= needs bridge=True",
      raises(lambda: Widget("r", html="<p>x</p>", route="django_http"), TypeError), "TypeError")
check("bridge= is for html= pages",
      raises(lambda: Widget("b", body="<p>x</p>", bridge=True), TypeError), "TypeError")
check("body= takes a path too",
      '<div id="root"></div>' in Widget("bp", body=bundle_dir / "body.html").html, True)
for label, kw in [
        ("a script src", {"html": '<script src="/assets/index-a1b2.js"></script>'}),
        ("a stylesheet link", {"html": '<link rel="stylesheet" href="./widget.css">'}),
        ("a modulepreload link", {"html": '<link rel="modulepreload" href="/assets/chunk.js">'}),
        ("an img src", {"body": '<img src="logo.png">'}),
        ("an img srcset", {"body": '<img srcset="data:image/png;base64,AAAA 1x, logo@2x.png 2x">'}),
        ("a url() in styles=", {"body": "<p>x</p>", "styles": ["#root{background:url(./logo.svg)}"]}),
        ("an @import in styles=", {"body": "<p>x</p>", "styles": ['@import "./base.css";']}),
        ("a url() in a style attribute", {"body": "<div style=\"background: url('img/bg.png')\"></div>"}),
        ("an import map", {"html": '<script type="importmap">{"imports": {"x": "./x.js"}}</script>'}),
        ("a protocol-relative src", {"body": '<img src="//cdn.example.com/x.png">'})]:
    check(f"a relative URL in {label} is refused", raises(lambda k=kw: Widget("rel", **k)), "ValueError")
for label, kw in [
        ("data: and https: URLs", {"body": '<img src="data:image/png;base64,AAAA">'
                                           '<img src="https://cdn.example.com/x.png">'}),
        ("links, fragments, and hypermedia attributes",
         {"body": '<a href="/docs">docs</a><svg><use href="#icon"/></svg>'
                  '<button hx-get="/app/todos/" fx-action="/x">go</button>'}),
        ("an icon link", {"html": '<link rel="icon" href="/vite.svg"><p>x</p>'}),
        ("an https base", {"html": '<head><base href="https://cdn.example.com/app/"></head>'
                                   '<script src="assets/x.js"></script>'})]:
    check(f"{label}: accepted", raises(lambda k=kw: Widget("fine", **k)), None)
heard = []


class Heard(logging.Handler):
    def emit(self, record):
        heard.append(record.getMessage())


logging.getLogger("micromcp.apps").addHandler(Heard())
Widget("warned", body="<p>x</p>",
       modules=['const logo = "./logo.svg"; await import("./chunk-a1.js");'])
said = " ".join(m for m in heard if "'warned'" in m)
check("relative paths inside a script are logged, not refused",
      ("./logo.svg" in said, "./chunk-a1.js" in said), (True, True))
check("... an asset and a run-time import are told apart",
      [("imports" in m, "refers to" in m) for m in heard if "'warned'" in m],
      [(True, False), (False, True)])
check("an inlined script with </script is refused by default",
      raises(lambda: Widget("s", body="<p>x</p>", modules=['window.t = "</SCRIPT>";'])),
      "ValueError")
escaped = Widget("s2", body="<p>x</p>", modules=['window.t = "</SCRIPT>";'],
                 escape_scripts=True).html
check("escape_scripts=True rewrites it as <\\/script", 'window.t = "<\\/SCRIPT>";' in escaped, True)
check("... leaving one end tag per script element",
      escaped.lower().count("</script>"), escaped.lower().count("<script"))
check("BRIDGE_TYPES declares window.mcp",
      ("interface MCPBridge" in BRIDGE_TYPES, "var mcp: MCPBridge" in BRIDGE_TYPES), (True, True))

# regressions from the adversarial round on the check
import time  # noqa: E402

for label, kw in [
        ("a srcset with a non-breaking space, beside a relative script",
         {"html": '<img srcset="\xa0x.png"><script src="/assets/app.js"></script>'}),
        ("an import map that is a list, beside a relative script",
         {"html": '<script type="importmap">[]</script><script src="/assets/app.js"></script>'}),
        ("the first of two src attributes",
         {"html": '<script src="app.js" src="https://cdn.example.com/app.js"></script>'}),
        ("an <image> element", {"body": '<image src="logo.png">'}),
        ("a body background", {"html": '<body background="bg.png"></body>'}),
        ("an SVG script href", {"body": '<svg><script href="app.js"></script></svg>'}),
        ("an input of type image", {"body": '<input type="image" src="go.png">'}),
        ("@import with no space", {"body": "<p>x</p>", "styles": ['@import"base.css";']}),
        ("image-set()", {"body": "<div style=\"background-image: image-set('bg.png' 1x)\"></div>"}),
        ("a base inside <template>",
         {"html": '<template><base href="https://cdn.example.com/"></template>'
                  '<script src="app.js"></script>'}),
        ("a base with no host", {"html": '<base href="https://"><script src="app.js"></script>'}),
        ("a load before the base",
         {"html": '<script src="app.js"></script><base href="https://cdn.example.com/">'}),
        ("an iframe srcdoc", {"body": '<iframe srcdoc="&lt;img src=logo.png&gt;"></iframe>'}),
        ("a URL behind a non-breaking space", {"body": '<img src="\xa0https://cdn.example.com/x.png">'})]:
    check(f"refused: {label}", raises(lambda k=kw: Widget("adv", **k)), "ValueError")
for label, kw in [
        ("a url() in a CSS comment",
         {"body": "<p>x</p>", "styles": ["/* was: url(bg.png) */ p{color:red}"]}),
        ("noscript content", {"body": '<noscript><img src="pixel.gif"></noscript>'}),
        ("an input that is not an image", {"body": '<input type="text" src="icon.png">'}),
        ("a tab inside the scheme", {"body": '<img src="ht\ttps://cdn.example.com/x.png">'})]:
    check(f"accepted: {label}", raises(lambda k=kw: Widget("adv", **k)), None)
for label, page_html, before, after in [
        ("a comment mentioning <head> before the head",
         "<!-- the bridge goes in <head> --><html><head><title>t</title></head>"
         "<body><script>window.app=1</script></body></html>", "--><html><head>", "<title>"),
        ("a head attribute containing >",
         '<html><head data-note="a>b"><title>t</title></head><body></body></html>',
         'data-note="a>b">', "<title>"),
        ("no head tag, and a script string with <head>",
         '<!doctype html><script>var s = "<head>";</script>', "<!doctype html>", "<script>var s"),
        ("a <head-nav> element and no head",
         "<!doctype html><body><head-nav></head-nav><script>window.app=1</script></body>",
         "<!doctype html>", "<head-nav>")]:
    doc = Widget("placed", html=page_html, bridge=True).html
    check(f"bridge placement: {label}",
          doc.index(before) + len(before) <= doc.index(BRIDGE_JS) < doc.index(after), True)
(bundle_dir / "bom.html").write_bytes(b"\xef\xbb\xbf<!doctype html><html><head></head></html>")
check("a BOM in a page file is dropped",
      Widget("bom", html=bundle_dir / "bom.html").html.startswith("<!doctype"), True)
check("html= given a file name as a str is refused",
      raises(lambda: Widget("strpath", html="ui/dist/index.html")), "ValueError")
# a script's end state decides: a closed <!-- <script> --> region is harmless
check("a script ending inside '<!--' + '<script' is refused",
      raises(lambda: Widget("dbl", body="<p>x</p>", modules=['window.a = "<!--<script>";'])),
      "ValueError")
check("a closed '<!-- <script> -->' region is accepted",
      raises(lambda: Widget("dbl2", body="<p>x</p>",
                            modules=['var a = "<!-- <script> -->", b = "<script> and <style>";'])),
      None)
esc = Widget("dbl3", body="<p>x</p>", modules=['window.a = "<!--<script>";'],
             escape_scripts=True).html
check("escape_scripts closes the sequence instead of refusing",
      (esc.endswith("\n//--></script></body></html>"), '"<!--<script>"' in esc), (True, True))

# a module's own imports have to resolve: a widget has no origin and no bundler
ESM = "https://esm.sh/three"
for label, kw, want in [
        ("a bare specifier in a module",
         {"modules": ['import {clone} from "lodash-es";\nclone({});']}, "ValueError"),
        ("a relative import in a module", {"modules": ['import "./chunk.js";']}, "ValueError"),
        ("a re-export from a relative path", {"modules": ['export {x} from "./y.js";']},
         "ValueError"),
        ("a root-absolute import", {"modules": ['import "/assets/c.js";']}, "ValueError"),
        ("an import written inside a string", {"scripts": ['var s = \'import "y"\';']}, None),
        ("a mapped specifier", {"modules": ['import "three";'], "imports": {"three": ESM}}, None),
        ("a specifier under a mapped prefix",
         {"modules": ['import "three/addons/x.js";'], "imports": {"three/": ESM}}, None),
        ("an https import", {"modules": ['import "https://esm.sh/x";']}, None),
        ("an import mentioned mid-statement",
         {"modules": ['var doc = "run import \'./x.js\' first";']}, None)]:
    check(f"module imports: {label}", raises(lambda k=kw: Widget("mod", body="<p>x</p>", **k)), want)
heard.clear()
Widget("dyn", body="<p>x</p>", modules=['if (window.x) import("./late.js");'])
check("a dynamic import in a module is warned about, not refused",
      any("./late.js" in m for m in heard if "'dyn'" in m), True)
heard.clear()
Widget("vite8", body="<p>x</p>",
       modules=["import(`./a.js`); new Worker(new URL(`/assets/w-B0.js`,``+import.meta.url));"
                ' img.src = `/public-logo.png`; const m = {"./keyed.js": 1, "./method.js"(x) {}};'])
said = " ".join(m for m in heard if "'vite8'" in m)
check("Vite 8's backtick forms are warned about",
      ("./a.js" in said, "/assets/w-B0.js" in said, "/public-logo.png" in said), (True, True, True))
check("... but not object keys or methods", ("./keyed.js" in said, "./method.js" in said),
      (False, False))
build = pathlib.Path(tempfile.mkdtemp())
(build / "widget.js").write_text("window.w = 1")
(build / "widget.css").write_text("p{color:red}")
heard.clear()
Widget("clean-build", body="<p>x</p>", modules=[build / "widget.js"], styles=[build / "widget.css"])
check("a build folder holding only what was passed is quiet",
      [m for m in heard if "'clean-build'" in m], [])
(build / "assets").mkdir()
(build / "assets" / "w-a1.js").write_text("x")
(build / "widget.js.map").write_text("{}")
heard.clear()
Widget("leftover", body="<p>x</p>", modules=[build / "widget.js"], styles=[build / "widget.css"])
check("files left beside a module are warned about (source maps are not)",
      [("assets/w-a1.js" in m, ".map" in m) for m in heard if "'leftover'" in m], [(True, False)])
check("passing neither body= nor html= says so",
      "neither" in message(lambda: Widget("none", modules=["window.a = 1"])), True)
check("passing both says so",
      "both" in message(lambda: Widget("two", body="<p>x</p>", html="<html></html>")), True)
for label, doc in [
        ("a bundled ext-apps client",
         '<html><head></head><body><script>const c = require("@modelcontextprotocol/ext-apps");'
         '</script></body></html>'),
        ("a client that installs the global",
         '<html><head></head><body><script>window.mcp = makeClient();</script></body></html>'),
        ("a client that speaks the handshake",
         '<html><head></head><body><script>post("ui/initialize", {});</script></body></html>')]:
    heard.clear()
    Widget("client2", html=doc)
    check(f"no-client warning stays quiet for {label}",
          [m for m in heard if "'client2'" in m], [])
heard.clear()
Widget("wordly", body="<p>x</p>",
       scripts=['var n = navigator.userAgent.indexOf("Node.js") > -1, t = "image/png";'])
check("a word that ends in .js is not a path", [m for m in heard if "'wordly'" in m], [])
heard.clear()
Widget("quoted", body="<p>x</p>", scripts=['var a = "./one.js", b = "two/three.png";'])
check("the paths in a warning are quoted",
      any("'./one.js', 'two/three.png'" in m for m in heard if "'quoted'" in m), True)
heard.clear()
Widget("barepath", body="<p>x</p>", scripts=['var p = "img/logo.png";'])
check("an asset path without ./ is warned about too",
      any("img/logo.png" in m for m in heard if "'barepath'" in m), True)
heard.clear()
Widget("mediatype", body="<p>x</p>", scripts=['var t = "image/png", u = "application/json";'])
check("... but a media type is not a path", [m for m in heard if "'mediatype'" in m], [])
beside = pathlib.Path(tempfile.mkdtemp())
(beside / "server.py").write_text("")           # the ordinary layout: dist beside the server
(beside / "dist").mkdir()
(beside / "dist" / "widget.js").write_text("window.w = 1")
(beside / "dist" / "chunk-A1b2C3d4.js").write_text("x")
heard.clear()
Widget("beside", body="<p>x</p>", modules=[beside / "dist" / "widget.js"])
check("a build folder beside the server module is still scanned",
      any("chunk-A1b2C3d4.js" in m for m in heard if "'beside'" in m), True)
check("a split build is told to rebuild as one file",
      "one file" in message(lambda: Widget(
          "stub", html='<html><head><link rel=stylesheet href="./index-5yxhrva4.css">'
                       '</head><body></body></html>')), True)
heard.clear()
Widget("webfont", body="<p>x</p>",
       styles=['@font-face{font-family:K;src:url(data:font/woff2;base64,AA) format("woff2")}'])
check("a font inlined as a data: URL is warned about",
      any("font-src" in m for m in heard if "'webfont'" in m), True)
heard.clear()
Widget("inlineimg", body="<p>x</p>", styles=['p{background:url(data:image/png;base64,AA)}'])
check("... but an inlined image is not", [m for m in heard if "'inlineimg'" in m], [])
vendored_css = pathlib.Path(tempfile.mkdtemp()) / "katex.min.css"
vendored_css.write_text('@font-face{src:url(fonts/KaTeX.woff2)}')
check("a vendored stylesheet is pointed at its CDN, not at a Path",
      "CDN" in message(lambda: Widget("katex", body="<p>x</p>", styles=[vendored_css])), True)
hexdir = pathlib.Path(tempfile.mkdtemp())
(hexdir / "widget.js").write_text("window.w = 1")
(hexdir / "chunk-deadbeef.js").write_text("x")   # a hex hash, no digits
(hexdir / "sw.js").write_text("x")               # a worker, never inlined
heard.clear()
Widget("hexworker", body="<p>x</p>", modules=[hexdir / "widget.js"])
said = " ".join(m for m in heard if "'hexworker'" in m)
check("a hex hash and a worker both count as leftovers",
      ("chunk-deadbeef.js" in said, "sw.js" in said), (True, True))

vite8 = pathlib.Path(tempfile.mkdtemp())
(vite8 / "widget.js").write_text("window.w = 1")
(vite8 / "lazy-DuOUKcfe.js").write_text("x")     # a Vite 8 hash, all letters
heard.clear()
Widget("vitehash", body="<p>x</p>", modules=[vite8 / "widget.js"])
check("a letter-only content hash counts as leftover output",
      any("lazy-DuOUKcfe.js" in m for m in heard if "'vitehash'" in m), True)

# a complete page needs some MCP Apps client, or window.mcp is undefined
plain = '<!doctype html><html><head></head><body><div id=app></div></body></html>'
heard.clear()
Widget("noclient", html=plain)
check("a page with no MCP Apps client is warned about",
      any("no MCP Apps client" in m for m in heard if "'noclient'" in m), True)
heard.clear()
Widget("withbridge", html=plain, bridge=True)
check("... but not when bridge=True adds one",
      [m for m in heard if "'withbridge'" in m], [])
heard.clear()
Widget("extapps", html='<!doctype html><html><head></head><body><script>'
                       'const M = "ui/notifications/initialized";</script></body></html>')
check("... nor when the page brings its own client",
      [m for m in heard if "'extapps'" in m], [])

templates = pathlib.Path(tempfile.mkdtemp())
for n in ("a.html", "b.html"):
    (templates / n).write_text("<p>x</p>")
(templates / "shared.js").write_text("window.s = 1")
(templates / "server.py").write_text("")
heard.clear()
Widget("hand-written", body=templates / "a.html")
check("a source folder of hand-written pages is quiet",
      [m for m in heard if "'hand-written'" in m], [])
heard.clear()
Widget("two", html='<html><head></head><body><script type="module">'
                   'const M = "ui/notifications/initialized";</script></body></html>', bridge=True)
check("another MCP Apps client beside bridge=True is warned about",
      any("'two'" in m and "two handshakes" in m for m in heard), True)
# what a build folder holds: a bundler's output warns, hand-kept files do not
dist = pathlib.Path(tempfile.mkdtemp())
(dist / "widget.js").write_text("window.w = 1")
(dist / "assets").mkdir()
(dist / "assets" / "vendor-chunk.js").write_text("x")       # a split chunk, plain name
(dist / "main.9f8e7d6c.js").write_text("x")                 # a hashed chunk
(dist / "favicon.ico").write_bytes(b"\0")                   # an icon breaks nothing
(dist / "notes.html").write_text("<p>x</p>")                # a sibling page
heard.clear()
Widget("dist", body="<p>x</p>", modules=[dist / "widget.js"])
said = " ".join(m for m in heard if "'dist'" in m)
check("a build folder's leftovers warn, its icon and sibling page do not",
      ("assets/vendor-chunk.js" in said, "main.9f8e7d6c.js" in said,
       "favicon" in said, "notes.html" in said), (True, True, False, False))
vendored = pathlib.Path(tempfile.mkdtemp())
for n in ("purify.min.js", "purify.cjs.js", "three.module.js", "logo.png"):
    (vendored / n).write_text("x")
heard.clear()
Widget("vendored", body="<p>x</p>", modules=[vendored / "purify.min.js"])
check("a folder of hand-kept libraries is quiet", [m for m in heard if "'vendored'" in m], [])
pkg = pathlib.Path(tempfile.mkdtemp())
(pkg / "myapp").mkdir()
(pkg / "myapp" / "__init__.py").write_text("")
(pkg / "myapp" / "static").mkdir()
(pkg / "myapp" / "static" / "widget.html").write_text("<p>x</p>")
(pkg / "myapp" / "static" / "logo.png").write_bytes(b"\0")
heard.clear()
Widget("pkgstatic", body=pkg / "myapp" / "static" / "widget.html")
check("a Python package's static folder is quiet", [m for m in heard if "'pkgstatic'" in m], [])

heard.clear()
Widget("heavy", body='<div id="app"></div>', modules=['const pad = "' + "x" * 300_000 + '";'])
said = " ".join(m for m in heard if "'heavy'" in m)
check("a page big enough to be felt is warned about, with its size",
      ("KB" in said, "per connector" in said, "largest script" in said), (True, True, True))
heard.clear()
Widget("light", body="<p>x</p>", modules=["window.app = 1;"])
check("... and an ordinary page is not", [m for m in heard if "'light'" in m], [])

# the second-client warning reads scripts, not prose
heard.clear()
Widget("prose", body="<p>The client sends <code>ui/notifications/initialized</code>.</p>")
Widget("logview", body='<pre>{"method":"ui/notifications/initialized"}</pre>')
check("a page that only mentions the method name is quiet",
      [m for m in heard if "'prose'" in m or "'logview'" in m], [])
heard.clear()
Widget("client", html='<html><head></head><body><script>'
                      'const M = "ui/notifications/initialized";</script></body></html>',
       bridge=True)
check("a real second client in a script still warns",
      any("two handshakes" in m for m in heard if "'client'" in m), True)

# CSS and JS heuristics
check("@import with a layer() prelude is refused",
      raises(lambda: Widget("layer", body="<p>x</p>", styles=['@import layer(base) "theme.css";'])),
      "ValueError")
heard.clear()
Widget("jsdoc", body="<p>x</p>", scripts=["// see `line.from`/`line.to` for the range\nvar a = 1;"])
check("a JSDoc backtick path is not reported as a load", [m for m in heard if "'jsdoc'" in m], [])

# an unreadable file says which argument, and is never blocked on
bad = pathlib.Path(tempfile.mkdtemp())
(bad / "latin.html").write_bytes(b"<p>caf\xe9</p>")
(bad / "sub").mkdir()
unreadable = [("a missing file", bad / "nope.html"), ("a directory", bad / "sub"),
              ("a non-UTF-8 file", bad / "latin.html")]
if hasattr(os, "mkfifo"):
    os.mkfifo(bad / "pipe.html")
    unreadable.append(("a pipe", bad / "pipe.html"))
for label, path in unreadable:
    check(f"html= refused: {label}", raises(lambda p=path: Widget("badfile", html=p)), "ValueError")
check("the read error names the argument",
      "html" in message(lambda: Widget("badfile", html=bad / "nope.html")), True)
check("a page name with a query is refused",
      raises(lambda: Widget("q", html="dist/index.html?v=2")), "ValueError")
check("a page given as a URL says to fetch it",
      "fetch" in message(lambda: Widget("u", html="https://example.com/report.html")), True)


class Rendered:                        # renders itself, as markupsafe.Markup does
    def __init__(self, s): self.s = s
    def __html__(self): return self.s


check("an __html__ object that looks like a file name is still markup",
      raises(lambda: Widget("markup", body=Rendered("index.html"))), None)

# what a browser parses, parsed the same way (round 2: a browser was the oracle)
for label, kw in [
        ("a load after a nested noscript",
         {"html": '<noscript><noscript></noscript><img src="probe.png"></noscript>'}),
        ("a prefetched chunk",
         {"body": "<p>x</p>", "head": '<link rel=prefetch href="chunk.js">'}),
        ("a stylesheet hidden behind <style/>",
         {"html": '<style/>div{background:url(probe.png)}</style>'}),
        ("a style left unclosed at the end", {"html": '<body><style>@import "late.css";'}),
        ("an <image srcset>", {"body": '<image srcset="logo.png 1x">'}),
        ("a frame", {"html": '<frameset><frame src="panel.html"></frameset>'}),
        ("a scheme-relative http: URL", {"body": '<img src="http:probe.png">'}),
        ("a page whose script swallows the rest",
         {"html": '<!doctype html><html><head></head><body><script>'
                  'var a="<!--";var b="<script>";</script><div id=late></div></body></html>'})]:
    check(f"refused: {label}", raises(lambda k=kw: Widget("browser", **k)), "ValueError")
check("text inside <textarea/> is not markup",
      raises(lambda: Widget("rcdata", html='<textarea/><img src="probe.png"></textarea>')), None)

# the bridge goes ahead of anything the page runs, and carries the charset
doc = Widget("early", html='<!doctype html><script>window.app=1</script><head></head><body>x',
             bridge=True).html
check("the bridge precedes a script that comes before the head",
      doc.index(BRIDGE_JS) < doc.index("window.app"), True)
doc = Widget("ns", html='<!doctype html><html><body><noscript><head></noscript>'
                        '<script>window.app=1</script></body></html>', bridge=True).html
check("a <head> inside <noscript> is not the head",
      doc.index(BRIDGE_JS) < doc.index("<noscript>"), True)
doc = Widget("charset", html='<!doctype html><html><head><meta charset="UTF-8"><title>t</title>'
                             '</head><body>caf\u00e9</body></html>', bridge=True).html
check("a bridged page still declares its charset in the first 1024 bytes",
      0 < doc.find("charset") < 1024, True)

# imports=, csp=, and the other arguments
w = Widget("gen", body="<p>x</p>",
           csp={"connectDomains": (o for o in ["https://a.example.com"])})
check("a csp list given as a generator survives",
      w.meta["ui"]["csp"]["connectDomains"], ["https://a.example.com"])
check("csp that is not a dict is refused",
      raises(lambda: Widget("c", body="<p>x</p>", csp=["https://a.example.com"]), TypeError),
      "TypeError")
check("an import map key that is not a specifier says so",
      "keys" in message(lambda: page("<p>x</p>", imports={1: "https://cdn.example.com/a.js"})),
      True)
built = page("<p>x</p>", head='<script type="importmap">{"imports":{}}</script>',
             imports={"three": "https://esm.sh/three"})
check("the generated import map comes before head=",
      built.index("esm.sh/three") < built.index('{"imports":{}}'), True)
check("page() refuses a relative URL as Widget does",
      raises(lambda: page('<img src="/logo.png">')), "ValueError")
check("a route must be a whole tool name",
      raises(lambda: Widget("r", body="<p>x</p>", route="ok\n", bridge=False)), "ValueError")
check("a uri with a space is refused",
      raises(lambda: Widget("u2", body="<p>x</p>", uri="ui://a b")), "ValueError")
check("border must be a flag",
      raises(lambda: Widget("b", body="<p>x</p>", border="no"), TypeError), "TypeError")
check("bytes are not an asset list",
      raises(lambda: Widget("by", body="<p>x</p>", scripts=b"var a=1"), TypeError), "TypeError")
check("a fragment's content type must be a header value",
      raises(lambda: fragment("<p>x</p>", content_type="text/html\r\nX: 1")), "ValueError")
bom = pathlib.Path(tempfile.mkdtemp()) / "w.css"
bom.write_bytes(b"\xef\xbb\xbfbody{margin:0}")
check("a BOM in a stylesheet file is dropped",
      "\ufeff" in Widget("bom2", body="<p>x</p>", styles=[bom]).html, False)
deep = "<div>x</div>"
for _ in range(400):
    deep = '<iframe srcdoc="' + deep.replace("&", "&amp;").replace('"', "&quot;") + '"></iframe>'
heard.clear()
Widget("deep", body=deep)
check("deeply nested srcdoc does not give up on the page",
      [m for m in heard if "could not be checked" in m], [])
check("a template-literal import is not taken for a real one",
      raises(lambda: Widget("tmpl", body="<p>x</p>", modules=[
          "const code = `\nimport { x as _x } from 'vue'\n`; window.c = code;"])), None)

# inputs crafted to make a regex backtrack
t1 = time.monotonic()
for bad_css in ["/*a" * 60000, "image-set(" * 12000]:
    raises(lambda c=bad_css: Widget("slowcss", body="<p>x</p>", styles=[c]))
raises(lambda: Widget("slowset", body='<img srcset="' + "a 1x, " * 80000 + '">'))
check("unclosed comments, image-set and a long srcset stay fast", time.monotonic() - t1 < 3.0,
      True)

t0 = time.monotonic()
raises(lambda: Widget("slow1", body="<p>x</p>", styles=["url(" * 20000]))
raises(lambda: Widget("slow2", body="<p>x</p>", modules=["import" + " " * 40000]))
check("pathological styles and scripts stay fast", time.monotonic() - t0 < 2.0, True)

# ── widget pages ───────────────────────────────────────────────────────────
print("pages")
doc = page("<p>x</p>", title="A & B", head="<style></style>", scripts=["var mine = 1;"])
check("page inlines the bridge before the page's scripts",
      0 < doc.index(BRIDGE_JS) < doc.index("var mine = 1;"), True)
check("page escapes the title", "<title>A &amp; B</title>" in doc, True)
check("page refuses a script that would close its tag",
      raises(lambda: page("", scripts=["x</SCRIPT >"])), "ValueError")
# the bundled example: its committed build is what the docs' recipe produces
EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / "examples"
heard.clear()
example = Widget("readings", title="Readings", body='<div class="wrap" id="root"></div>',
                 modules=[EXAMPLES / "bundled_ui" / "dist" / "widget.js"],
                 styles=[EXAMPLES / "bundled_ui" / "dist" / "widget.css"])
check("examples/bundled_ui builds a widget with nothing left behind",
      ([m for m in heard if "'readings'" in m], example.html.count("data:image/png")), ([], 2))

# every example builds its widgets, and none of them warns
import importlib.util  # noqa: E402

sys.path.insert(0, str(EXAMPLES))
for path in sorted(EXAMPLES.glob("*.py")):
    heard.clear()
    spec = importlib.util.spec_from_file_location(f"example_{path.stem}", path)
    try:
        spec.loader.exec_module(importlib.util.module_from_spec(spec))
        outcome = [m for m in heard if "widget" in m]
    except ImportError as e:                      # an example whose optional dep is absent
        outcome = f"skip ({e.name} not installed)"
    check(f"examples/{path.name} builds cleanly", outcome,
          [] if isinstance(outcome, list) else outcome)
sys.path.pop(0)

check("the page can read its channel connection id",
      ('Object.defineProperty(this, "id"' in BRIDGE_JS, "readonly id: string | null" in BRIDGE_TYPES),
      (True, True))
route_mcp = MCP("routed", "1.0.0")
Widget("routed", body="<p>x</p>", route="django_http").register(route_mcp)
page = next(v for v in route_mcp.resources["ui://routed"].values() if callable(v))
heard.clear()
page()
check("a route= that names no tool is warned about when the host reads the widget",
      any("not a tool on this server" in m for m in heard), True)
have_route = MCP("routed2", "1.0.0")
have_route.tool(lambda: "x", name="django_http")
Widget("routed2", body="<p>x</p>", route="django_http").register(have_route)
heard.clear()
next(v for v in have_route.resources["ui://routed2"].values() if callable(v))()
check("... and a route= that names a real tool is not", heard, [])

check("a tool result's context reaches the model on both call paths",
      BRIDGE_JS.count("pushContext(") >= 3, True)
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
second = ctool("channel_open", {"channel": "room", "params": "who=bo"})
room.broadcast_json({"n": 2}, exclude=o["conn"])       # the widget that caused it already knows
check("a broadcast can skip the connection that caused it",
      (ctool("channel_recv", {"conn": o["conn"], "wait": 0})["frames"],
       ctool("channel_recv", {"conn": second["conn"], "wait": 0})["frames"]), ([], ['{"n":2}']))
ctool("channel_close", {"conn": second["conn"]})
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

# ── the boundary with the core ─────────────────────────────────────────────
print("boundary")
import ast  # noqa: E402

import micromcp  # noqa: E402

used = set()
for src in (pathlib.Path(micromcp.__file__).parent / "apps").glob("*.py"):
    for node in ast.walk(ast.parse(src.read_text())):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module \
                and node.module.split(".")[0] == "micromcp":
            used |= ({a.name for a in node.names} if node.module == "micromcp"
                     else {f"<{node.module}>"})
        elif isinstance(node, ast.ImportFrom) and node.level > 1:
            used.add(f"<{'.' * node.level}{node.module or ''}>")
check("micromcp.apps imports only micromcp's public API", sorted(used - set(micromcp.__all__)), [])
check("import micromcp does not load micromcp.apps",
      subprocess.run([sys.executable, "-c", "import micromcp, sys; print('micromcp.apps' in sys.modules)"],
                     capture_output=True, text=True).stdout.strip(), "False")

print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
