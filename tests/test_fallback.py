"""`fallback=`: another app answers every path but the MCP endpoint and the
documents served beside it, on both transports."""
import asyncio
import io
import json
import sys

from micromcp import MCP, META_CAPS, META_VER, PROTOCOL, ASGIServer, Server

OK = FAIL = 0


def check(label, got, want):
    global OK, FAIL
    good = got == want
    OK, FAIL = OK + good, FAIL + (not good)
    print(f"  {'PASS' if good else 'FAIL'}  {label}")
    if not good:
        print(f"        got  {got!r}\n        want {want!r}")


def j(raw):
    """The JSON body, or None when the answer was not JSON (the fallback's)."""
    try:
        return json.loads(raw)
    except ValueError:
        return None


def raises(fn, exc):
    try:
        fn()
    except exc as e:
        return type(e).__name__
    except Exception as e:  # noqa: BLE001 - the test reports the wrong type
        return f"wrong: {type(e).__name__}: {e}"
    return None


mcp = MCP("fb", "1.0", description="Fallback test server")


@mcp.tool
def ping() -> str:
    """Answer pong."""
    return "pong"


RPC = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                  "params": {"_meta": {META_VER: PROTOCOL, META_CAPS: {}}}}).encode()
MCP_HDRS = {"MCP-Protocol-Version": PROTOCOL, "Mcp-Method": "tools/list",
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
CARD = {"url": "https://example.test/mcp", "name": "test.example/fb"}
META = {"resource": "https://example.test/mcp", "authorization_servers": ["https://as.example.test"]}

# ── WSGI ─────────────────────────────────────────────────────────────────────
seen = []


def render(environ, start_response):
    """A plain WSGI app: records what it was handed, echoes the body it reads."""
    body = environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH") or 0))
    seen.append({"path": environ.get("PATH_INFO"), "script": environ.get("SCRIPT_NAME"),
                 "method": environ["REQUEST_METHOD"], "body": body})
    start_response("200 OK", [("Content-Type", "image/png"), ("X-From", "fallback")])
    return [b"PNG" + body]


def wsgi(app, path, *, method="GET", body=b"", headers=None, script=""):
    env = {"REQUEST_METHOD": method, "PATH_INFO": path, "SCRIPT_NAME": script,
           "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body),
           "HTTP_HOST": "example.test"}
    for k, v in (headers or {}).items():
        key = k.upper().replace("-", "_")
        env[key if key in ("CONTENT_TYPE", "CONTENT_LENGTH") else "HTTP_" + key] = v
    box = {}
    out = b"".join(app(env, lambda s, h: box.update(s=int(s.split()[0]), h=dict(h))))
    return box["s"], box["h"], out


print("construction")
check("fallback without path= is refused", raises(lambda: Server(mcp, fallback=render), ValueError),
      "ValueError")
check("a fallback that is not callable is refused",
      raises(lambda: Server(mcp, path="/mcp", fallback="render"), TypeError), "TypeError")


async def asgi_app(scope, receive, send):
    pass


check("an ASGI app given to Server is refused",
      raises(lambda: Server(mcp, path="/mcp", fallback=asgi_app), TypeError), "TypeError")
check("a WSGI app given to ASGIServer is refused",
      raises(lambda: ASGIServer(mcp, path="/mcp", fallback=render), TypeError), "TypeError")


class AsgiClass:
    async def __call__(self, scope, receive, send):
        pass


check("an ASGI app with an async __call__ is accepted",
      type(ASGIServer(mcp, path="/mcp", fallback=AsgiClass())).__name__, "ASGIServer")

print("\nWSGI routing")
app = Server(mcp, path="/mcp", fallback=render, server_card=CARD, resource_metadata=META)
s, h, out = wsgi(app, "/r/abc.png")
check("another path reaches the fallback", (s, h.get("X-From"), out), (200, "fallback", b"PNG"))
s, h, out = wsgi(app, "/render.png", method="POST", body=b"hello",
                 headers={"Content-Type": "text/plain"})
check("... with its body unread, for the fallback to read", (out, seen[-1]["body"]), (b"PNGhello", b"hello"))
s, h, out = wsgi(app, "/mcp", method="POST", body=RPC, headers=MCP_HDRS)
check("the endpoint is still MCP", (s, [t["name"] for t in (j(out) or {}).get("result", {}).get("tools", [])]),
      (200, ["ping"]))
s, h, out = wsgi(app, "/mcp/", method="POST", body=RPC, headers=MCP_HDRS)
check("... with a trailing slash too", s, 200)
n = len(seen)
s, h, out = wsgi(app, "/mcp/server-card")
check("the Server Card stays micromcp's", (s, (j(out) or {}).get("name"), len(seen)), (200, "test.example/fb", n))
s, h, out = wsgi(app, "/.well-known/oauth-protected-resource/mcp")
check("the RFC 9728 document stays micromcp's", (s, (j(out) or {}).get("resource"), len(seen)),
      (200, META["resource"], n))
s, h, out = wsgi(app, "/.well-known/something-else")
check("another well-known path is the fallback's", (s, len(seen)), (200, n + 1))
s, h, out = wsgi(Server(mcp, path="/mcp"), "/r/abc.png")
check("without a fallback, other paths are still 404", s, 404)
wsgi(app, "/r/x.png", script="/app")
check("a mounted app's SCRIPT_NAME reaches the fallback as it was", seen[-1]["script"], "/app")

print("\nWSGI allowed_hosts")
guarded = Server(mcp, path="/mcp", fallback=render, allowed_hosts={"example.test"})
n = len(seen)
s, h, out = wsgi(guarded, "/r/a.png", headers={"Host": "evil.test"})
check("a disallowed Host is refused before the fallback", (s, (j(out) or {}).get("error", {}).get("message"), len(seen)),
      (403, "Host not allowed", n))
s, h, out = wsgi(guarded, "/r/a.png", headers={"Host": "example.test, evil.test"})
check("a repeated Host is refused", (s, len(seen)), (400, n))
s, h, out = wsgi(guarded, "/r/a.png", headers={"Host": "example.test:8000"})
check("an allowed Host (with a port) reaches the fallback", (s, len(seen)), (200, n + 1))
s, h, out = wsgi(app, "/r/a.png", headers={"Host": "anything.test, at.all"})
check("without allowed_hosts, the fallback judges Host itself", s, 200)

print("\nwhat counts as an allowed Host (every surface)")
n = len(seen)
for host in ("example.test:@evil.test", "example.test:x@evil.test/", "example.test:80a",
             "example.test:", "example.test:\uff18\uff10", "example.test:123456"):
    s, h, out = wsgi(guarded, "/r/a.png", headers={"Host": host})
    check(f"fallback: Host {host!r} is not the allowed name plus a port", (s, len(seen)), (403, n))
full = Server(mcp, path="/mcp", fallback=render, allowed_hosts={"example.test", "[::1]"},
              server_card=CARD, resource_metadata=META)
for label, path, method, body, hdrs in [
        ("endpoint", "/mcp", "POST", RPC, MCP_HDRS),
        ("Server Card", "/mcp/server-card", "GET", b"", {}),
        ("RFC 9728 document", "/.well-known/oauth-protected-resource/mcp", "GET", b"", {})]:
    bad = wsgi(full, path, method=method, body=body, headers={**hdrs, "Host": "example.test:@evil.test"})[0]
    folded = wsgi(full, path, method=method, body=body,
                  headers={**hdrs, "Host": "example.test:1,evil.test"})[0]
    good = wsgi(full, path, method=method, body=body, headers={**hdrs, "Host": "example.test:8443"})[0]
    check(f"{label}: 'name:junk' and a folded repeat are refused, 'name:port' admitted",
          (bad, folded in (400, 403), good), (403, True, 200))
check("a bracketed IPv6 name with a port is admitted",
      wsgi(full, "/r/a.png", headers={"Host": "[::1]:8000"})[0], 200)

print("\na fallback without allowed_hosts says so")
import logging  # noqa: E402
records = []
handler = logging.Handler()
handler.emit = records.append
logging.getLogger("micromcp").addHandler(handler)
Server(mcp, path="/mcp", fallback=render)
check("warned", any("allowed_hosts" in r.getMessage() for r in records), True)
records.clear()
Server(mcp, path="/mcp", fallback=render, allowed_hosts={"example.test"})
Server(mcp, path="/mcp")
check("not warned with allowed_hosts, or without a fallback", records, [])
logging.getLogger("micromcp").removeHandler(handler)

# ── ASGI ─────────────────────────────────────────────────────────────────────
print("\nASGI routing")
calls = []


async def asgi_render(scope, receive, send):
    if scope["type"] == "websocket":
        calls.append(("websocket", scope["path"]))
        await send({"type": "websocket.accept"})
        return
    msg = await receive()
    calls.append((scope["type"], scope["path"], msg.get("body", b"")))
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"image/png")]})
    await send({"type": "http.response.body", "body": b"PNG"})


async def asgi(app, path, *, kind="http", method="GET", body=b"", headers=None):
    hdrs = [(b"host", b"example.test")] if headers is None else headers
    scope = {"type": kind, "method": method, "path": path, "headers": hdrs}
    sent, inbox = [], [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return inbox.pop(0) if inbox else {"type": "http.disconnect"}

    async def send(msg):
        sent.append(msg)

    await app(scope, receive, send)
    return sent


async def run_asgi():
    aapp = ASGIServer(mcp, path="/mcp", fallback=asgi_render, server_card=CARD)
    sent = await asgi(aapp, "/r/abc.png", method="POST", body=b"data")
    check("another path reaches the fallback, body unread",
          (sent[0]["status"], calls[-1]), (200, ("http", "/r/abc.png", b"data")))
    hdrs = [(b"host", b"example.test"), *((k.lower().encode(), v.encode()) for k, v in MCP_HDRS.items())]
    sent = await asgi(aapp, "/mcp", method="POST", body=RPC, headers=hdrs)
    check("the endpoint is still MCP", (sent[0]["status"], ((j(sent[1]["body"]) or {}).get("result", {}).get("tools") or [{}])[0].get("name")),
          (200, "ping"))
    n = len(calls)
    sent = await asgi(aapp, "/mcp/server-card")
    check("the Server Card stays micromcp's", (sent[0]["status"], len(calls)), (200, n))
    sent = await asgi(aapp, "/live", kind="websocket")
    check("a WebSocket on another path reaches the fallback",
          (sent[0]["type"], calls[-1]), ("websocket.accept", ("websocket", "/live")))
    sent = await asgi(aapp, "/mcp", kind="websocket")
    check("a WebSocket on the endpoint is still refused", sent[0], {"type": "websocket.close", "code": 1003})

    lifespan = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    sent, n = [], len(calls)

    async def receive():
        return lifespan.pop(0)

    async def send(msg):
        sent.append(msg["type"])

    await aapp({"type": "lifespan"}, receive, send)
    check("the lifespan stays micromcp's", (sent, len(calls)),
          (["lifespan.startup.complete", "lifespan.shutdown.complete"], n))

    print("\nASGI allowed_hosts")
    guarded = ASGIServer(mcp, path="/mcp", fallback=asgi_render, allowed_hosts={"example.test"})
    n = len(calls)
    sent = await asgi(guarded, "/r/a.png", headers=[(b"host", b"evil.test")])
    check("a disallowed Host is refused before the fallback", (sent[0]["status"], len(calls)), (403, n))
    sent = await asgi(guarded, "/r/a.png", headers=[(b"host", b"example.test"), (b"host", b"evil.test")])
    check("a repeated Host is refused", (sent[0]["status"], len(calls)), (400, n))
    sent = await asgi(guarded, "/r/a.png", headers=[])
    check("a missing Host is refused", (sent[0]["status"], len(calls)), (403, n))
    sent = await asgi(guarded, "/live", kind="websocket", headers=[(b"host", b"evil.test")])
    check("a WebSocket from a disallowed Host is refused at the handshake",
          (sent[0], len(calls)), ({"type": "websocket.close", "code": 1008}, n))
    sent = await asgi(guarded, "/r/a.png")
    check("an allowed Host reaches the fallback", (sent[0]["status"], len(calls)), (200, n + 1))
    sent = await asgi(guarded, "/r/a.png", headers=[(b"host", b"example.test:@evil.test")])
    check("'name:junk' is not the allowed name plus a port", (sent[0]["status"], len(calls)), (403, n + 1))


asyncio.run(run_asgi())

print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
