"""Server identity (title, description, website_url, icons), icons on tools,
resources, and prompts, and the Server Card at <endpoint>/server-card."""
import asyncio
import io
import json
import logging
import pathlib

from micromcp import (MCP, META_CAPS, META_SERVER, META_VER, PROTOCOL, SERVER_CARD_MIME,
                      SERVER_CARD_SCHEMA, ASGIServer, Server)

OK = FAIL = 0
ICON = {"src": "https://example.com/icon.png", "mimeType": "image/png", "sizes": ["48x48", "any"],
        "theme": "dark"}
DATA_ICON = {"src": "data:image/png;base64,iVBORw0KGgo="}
URL = "https://mcp.example.com/mcp"


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


def wsgi(app, method="GET", path="/", headers=None, body=b""):
    """(status code, header dict, body bytes) from a WSGI call."""
    env = {"REQUEST_METHOD": method, "PATH_INFO": path, "CONTENT_LENGTH": str(len(body)),
           "wsgi.input": io.BytesIO(body), "HTTP_HOST": "mcp.example.com"}
    env.update({"HTTP_" + k.upper().replace("-", "_"): v for k, v in (headers or {}).items()})
    box = {}
    out = b"".join(app(env, lambda s, h: box.update(s=s, h=h)))
    return int(box["s"].split()[0]), dict(box["h"]), out


def asgi(app, method="GET", path="/", headers=None):
    hdrs = [(b"host", b"mcp.example.com")] + [(k.lower().encode(), v.encode())
                                              for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": method, "path": path, "headers": hdrs}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        sent.append(m)
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    return (start["status"], {k.decode().lower(): v.decode() for k, v in start["headers"]},
            b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body"))


def rpc(app, method, params=None):
    params = {**(params or {}), "_meta": {META_VER: PROTOCOL, META_CAPS: {}}}
    raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    hdrs = {"Mcp-Protocol-Version": PROTOCOL, "Mcp-Method": method}
    if params.get("name"):
        hdrs["Mcp-Name"] = params["name"]
    return json.loads(wsgi(app, "POST", "/mcp" if app.path else "/", hdrs, raw)[2])


def build(**ident):
    mcp = MCP("newton", "1.2.0", **ident)

    @mcp.tool(icons=[ICON])
    def crashes(street: str) -> dict:
        """Count crashes."""
        return {"n": 1}

    @mcp.resource("schema://crash", icons=[DATA_ICON])
    def schema() -> str:
        return "{}"

    @mcp.resource("crash://{cid}", icons=[ICON])
    def one(cid: int) -> str:
        return "{}"

    @mcp.prompt(icons=[ICON])
    def review(street: str) -> str:
        """Review a street."""
        return street
    return mcp


IDENT = {"title": "Newton civic data", "description": "Crash and permit data for Newton, MA",
         "website_url": "https://example.com/newton", "icons": [ICON]}

print("identity fields reach serverInfo")
mcp = build(**IDENT)
app = Server(mcp)
info = rpc(app, "server/discover")["result"]["_meta"][META_SERVER]
check("discover serverInfo carries every field", info,
      {"name": "newton", "version": "1.2.0", "title": "Newton civic data",
       "description": "Crash and permit data for Newton, MA",
       "websiteUrl": "https://example.com/newton", "icons": [ICON]})
check("... and so does every other result",
      rpc(app, "tools/list")["result"]["_meta"][META_SERVER], info)
check("unset fields are left out", MCP("bare", "1").server_info(), {"name": "bare", "version": "1"})
check("server_info() is a copy", (lambda i: (i["icons"].clear(), mcp.icons)[1])(mcp.server_info()),
      [ICON])
legacy = Server(mcp, legacy="stateless")
raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                             "clientInfo": {"name": "c", "version": "1"}}}).encode()
init = json.loads(wsgi(legacy, "POST", "/", {"Content-Type": "application/json"}, raw)[2])
check("legacy initialize carries them too", init["result"]["serverInfo"], info)

print("\nicons on tools, resources, templates, and prompts")
check("tool icons", rpc(app, "tools/list")["result"]["tools"][0]["icons"], [ICON])
check("resource icons (data: URI)", rpc(app, "resources/list")["result"]["resources"][0]["icons"],
      [DATA_ICON])
check("template icons",
      rpc(app, "resources/templates/list")["result"]["resourceTemplates"][0]["icons"], [ICON])
check("prompt icons", rpc(app, "prompts/list")["result"]["prompts"][0]["icons"], [ICON])

print("\nidentity and icons are checked at registration")
check("title must be a str", raises(lambda: MCP("x", title=3), TypeError), "TypeError")
check("description must be a str", raises(lambda: MCP("x", description=["a"]), TypeError),
      "TypeError")
check("website_url must be http(s)", raises(lambda: MCP("x", website_url="ftp://a.b")),
      "ValueError")
check("website_url must be absolute", raises(lambda: MCP("x", website_url="/about")),
      "ValueError")
for label, bad in [
    ("icons must be a list", {"src": "https://a.b/i.png"}),
    ("icons must not be empty", []),
    ("an icon must be a dict", ["https://a.b/i.png"]),
    ("an unknown icon key is refused", [{"src": "https://a.b/i.png", "size": "48x48"}]),
    ("src is required", [{"mimeType": "image/png"}]),
    ("src must be http(s) or data:", [{"src": "javascript:alert(1)"}]),
    ("a data: src must be base64", [{"src": "data:image/svg+xml,<svg/>"}]),
    ("sizes must be WxH or any", [{"src": "https://a.b/i.png", "sizes": ["48"]}]),
    ("sizes must be a list", [{"src": "https://a.b/i.png", "sizes": "48x48"}]),
    ("theme must be light or dark", [{"src": "https://a.b/i.png", "theme": "blue"}]),
    ("mimeType must look like one", [{"src": "https://a.b/i.png", "mimeType": "png"}]),
]:
    check(label, raises(lambda bad=bad: MCP("x", icons=bad)), "ValueError")
m2 = MCP("x")
check("tool icons are checked", raises(lambda: m2.tool(icons=[{"src": "x"}])(lambda: None)),
      "ValueError")
check("resource icons are checked",
      raises(lambda: m2.resource("a://b", icons=[{"src": "x"}])(lambda: "")), "ValueError")
check("prompt icons are checked", raises(lambda: m2.prompt(icons=[{"src": "x"}])(lambda: "")),
      "ValueError")

print("\nthe Server Card")
CARD = {"url": URL, "name": "com.example/newton",
        "repository": {"url": "https://github.com/example/newton", "source": "github"}}
srv = Server(mcp, path="/mcp", server_card=CARD)
status, h, body = wsgi(srv, "GET", "/mcp/server-card",
                       {"Accept": "application/mcp-server-card+json"})
card = json.loads(body)
check("GET <endpoint>/server-card is 200", status, 200)
check("served as the card media type", h.get("Content-Type"), SERVER_CARD_MIME)
check("card body", card,
      {"$schema": SERVER_CARD_SCHEMA, "name": "com.example/newton", "version": "1.2.0",
       "title": "Newton civic data", "description": "Crash and permit data for Newton, MA",
       "websiteUrl": "https://example.com/newton", "icons": [ICON],
       "repository": {"url": "https://github.com/example/newton", "source": "github"},
       "remotes": [{"type": "streamable-http", "url": URL,
                    "supportedProtocolVersions": [PROTOCOL]}]})
check("readable from any origin",
      (h.get("Access-Control-Allow-Origin"), h.get("Access-Control-Expose-Headers")), ("*", "ETag"))
check("cacheable", h.get("Cache-Control"), "public, max-age=3600")
etag = h.get("ETag")
check("strong quoted ETag", (etag[0], etag[-1], etag.startswith("W/")), ('"', '"', False))
status, h2, body = wsgi(srv, "GET", "/mcp/server-card", {"If-None-Match": etag})
check("If-None-Match with the ETag is 304, empty", (status, body, h2.get("ETag")), (304, b"", etag))
check("... with no Content-Length", "Content-Length" in h2, False)
check("a weak or listed match is 304 too",
      wsgi(srv, "GET", "/mcp/server-card", {"If-None-Match": f'"x", W/{etag}'})[0], 304)
check("* is 304", wsgi(srv, "GET", "/mcp/server-card", {"If-None-Match": "*"})[0], 304)
check("another tag is 200", wsgi(srv, "GET", "/mcp/server-card", {"If-None-Match": '"x"'})[0], 200)
status, h, body = wsgi(srv, "HEAD", "/mcp/server-card")
check("HEAD: same head, no body", (status, h.get("Content-Type"), body), (200, SERVER_CARD_MIME, b""))
status, h, _ = wsgi(srv, "OPTIONS", "/mcp/server-card", {"Origin": "https://anywhere.example"})
check("OPTIONS is a 204 preflight for any origin",
      (status, h.get("Access-Control-Allow-Origin"), h.get("Access-Control-Allow-Headers")),
      (204, "*", "Content-Type, If-None-Match"))
status, h, _ = wsgi(srv, "POST", "/mcp/server-card", body=b"{}")
check("POST is 405 naming GET", (status, h.get("Allow")), (405, "GET, HEAD, OPTIONS"))
check("the MCP endpoint is unchanged",
      rpc(srv, "tools/list")["result"]["tools"][0]["name"], "crashes")
check("a GET on the endpoint is still 405", wsgi(srv, "GET", "/mcp")[0], 405)

print("\nthe card is public: no authentication, no guards")


def deny(headers):
    from micromcp import Unauthorized
    raise Unauthorized()


locked = Server(mcp, path="/mcp", server_card=CARD, authenticate=deny)
check("served without credentials", wsgi(locked, "GET", "/mcp/server-card")[0], 200)
check("while the endpoint still challenges",
      wsgi(locked, "POST", "/mcp", {"Mcp-Protocol-Version": PROTOCOL, "Mcp-Method": "tools/list"},
           json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                       "params": {"_meta": {META_VER: PROTOCOL, META_CAPS: {}}}}).encode())[0],
      401)
hosted = Server(mcp, server_card=CARD, allowed_hosts={"other.example"})
check("the Host guard still applies", wsgi(hosted, "GET", "/mcp/server-card")[0], 403)

print("\nwhere the card is served")
check("no card configured: the path is not special",
      wsgi(Server(mcp, path="/mcp"), "GET", "/mcp/server-card")[0], 404)
rooted = Server(mcp, server_card={**CARD, "url": "https://mcp.example.com/"})
check("an endpoint at the root serves /server-card", wsgi(rooted, "GET", "/server-card")[0], 200)
check("a legacy server lists both eras",
      json.loads(wsgi(Server(mcp, server_card=CARD, legacy="stateless"), "GET",
                      "/mcp/server-card")[2])["remotes"][0]["supportedProtocolVersions"],
      [PROTOCOL, "2025-11-25", "2025-06-18"])
seen = []
handler = logging.Handler()
handler.emit = lambda rec: seen.append(rec.getMessage())
logging.getLogger("micromcp").addHandler(handler)
Server(mcp, path="/api", server_card=CARD)
logging.getLogger("micromcp").removeHandler(handler)
check("a url path that differs from path= is warned about",
      any("/mcp/server-card" in s for s in seen), True)

print("\nover ASGI")
asrv = ASGIServer(mcp, path="/mcp", server_card=CARD)
status, h, body = asgi(asrv, "GET", "/mcp/server-card")
check("ASGI serves the same card",
      (status, h.get("content-type"), json.loads(body)), (200, SERVER_CARD_MIME, card))
status, h, body = asgi(asrv, "GET", "/mcp/server-card", {"If-None-Match": h["etag"]})
check("ASGI 304, no Content-Length", (status, body, "content-length" in h), (304, b"", False))
check("ASGI POST is 405", asgi(asrv, "POST", "/mcp/server-card")[0], 405)

print("\nthrough the Django adapters")
try:
    import django
    from django.conf import settings
except ImportError:
    print("  SKIP  django is not installed")
else:
    settings.configure(DEBUG=False, SECRET_KEY="x", ALLOWED_HOSTS=["*"], ROOT_URLCONF=__name__)
    django.setup()
    from django.test import AsyncClient, Client
    from django.urls import path as route

    from micromcp import django_async_view, django_view
    asrv_a = ASGIServer(mcp, server_card={**CARD, "url": "https://mcp.example.com/a/mcp"})
    urlpatterns = [route("mcp/server-card", django_view(srv)),
                   route("a/mcp/server-card", django_async_view(asrv_a))]
    resp = Client().get("/mcp/server-card")
    check("django_view serves the card",
          (resp.status_code, resp["Content-Type"], json.loads(resp.content)),
          (200, SERVER_CARD_MIME, card))
    again = Client().get("/mcp/server-card", HTTP_IF_NONE_MATCH=resp["ETag"])
    check("django_view's 304 carries no Content-Type (not Django's text/html)",
          (again.status_code, again.has_header("Content-Type"), again.content), (304, False, b""))
    resp = asyncio.run(AsyncClient().get("/a/mcp/server-card"))
    check("django_async_view serves the card",
          (resp.status_code, resp["Content-Type"], json.loads(resp.content)["remotes"][0]["url"]),
          (200, SERVER_CARD_MIME, "https://mcp.example.com/a/mcp"))

print("\nthe card is checked at construction")
for label, spec, kw in [
    ("a dict is required", "com.example/newton", {}),
    ("an unknown key is refused", {**CARD, "title": "override"}, {}),
    ("name must be namespace/name", {**CARD, "name": "newton"}, {}),
    ("name must have one slash", {**CARD, "name": "a/b/c"}, {}),
    ("url is required", {"name": "com.example/newton"}, {}),
    ("url must be absolute http(s)", {**CARD, "url": "/mcp"}, {}),
    ("url may not carry a query", {**CARD, "url": URL + "?x=1"}, {}),
    ("repository needs url and source", {**CARD, "repository": {"url": "https://a.b"}}, {}),
    ("repository url must be http(s)", {**CARD, "repository": {"url": "x", "source": "github"}}, {}),
]:
    exc = TypeError if label == "a dict is required" else ValueError
    check(label, raises(lambda spec=spec, kw=kw: Server(mcp, server_card=spec, **kw), exc),
          exc.__name__)
check("a description is required",
      raises(lambda: Server(build(), server_card=CARD)), "ValueError")
check("... of at most 100 characters",
      raises(lambda: Server(build(description="x" * 101), server_card=CARD)), "ValueError")
for rng in ("^1.2.3", "~1.2", ">=1.0", "1.x", "1.*"):
    check(f"version range {rng!r} is refused",
          raises(lambda rng=rng: Server(MCP("n", rng, description="d"), server_card=CARD)),
          "ValueError")
check("a prerelease version is fine",
      Server(MCP("n", "2.1.0-alpha", description="d"), server_card=CARD).server_card["version"],
      "2.1.0-alpha")

print("\nthe card validates against the published schema")
try:
    import jsonschema
except ImportError:
    print("  SKIP  jsonschema is not installed")
else:
    schema = json.loads((pathlib.Path(__file__).parent / "schemas" / "server-card.schema.json")
                        .read_text())
    errors = sorted(e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(card))
    check("full card", errors, [])
    minimal = json.loads(wsgi(Server(MCP("n", "1.0.0", description="d"),
                                     server_card={"url": URL, "name": "com.example/n"}),
                              "GET", "/mcp/server-card")[2])
    check("minimal card",
          sorted(e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(minimal)),
          [])

print("\nNovember review: icons are images, and URLs carry no user info")
for label, icon in [
        ("a data: page", {"src": "data:text/html;base64,PHNjcmlwdD4="}),
        ("a data: URI with a header injection", {"src": "data:\r\nX-Evil: 1;base64,AA=="}),
        ("a data: image that is not base64", {"src": "data:image/png;base64,<svg onload=x>"}),
        ("a non-image mimeType", {"src": "https://example.com/i.png", "mimeType": "text/html"}),
        ("a size with a trailing newline", {"src": "https://example.com/i.png", "sizes": ["48x48\n"]}),
        ("user info in the src", {"src": "https://good.example@evil.example/i.png"})]:
    check(f"icon refused: {label}", raises(lambda i=icon: MCP("x", icons=[i])), "ValueError")
check("an SVG data: icon is still accepted (hosts sanitize SVG)",
      MCP("x", icons=[{"src": "data:image/svg+xml;base64,PHN2Zy8+", "mimeType": "image/svg+xml"}])
      .icons[0]["mimeType"], "image/svg+xml")
check("website_url with user info is refused",
      raises(lambda: MCP("x", website_url="https://good.example@evil.example")), "ValueError")

print(f"\n{OK} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
