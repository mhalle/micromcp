"""Opt-in 2025-era serving (`legacy="stateless"`) and the OAuth handoff
(`Unauthorized`, `resource_metadata=`), on WSGI and ASGI, with raw HTTP and
the official mcp client in both of its modes.
"""
import asyncio, io, json, socket, threading, time
import httpx
from micromcp import (MCP, Server, ASGIServer, Context, Error, Unauthorized, PROTOCOL, META_VER,
                      META_CAPS, META_CLIENT, LEGACY_VERSIONS, WELL_KNOWN)
from _helpers import free_port

OK = FAIL = 0


def check(label, got, want):
    global OK, FAIL
    good = got == want
    OK, FAIL = OK + good, FAIL + (not good)
    print(f"  {'PASS' if good else 'FAIL'}  {label}")
    if not good:
        print(f"        got  {got!r}\n        want {want!r}")


def build():
    mcp = MCP("legacy-demo", "0.1.0")

    @mcp.tool
    def add(a: int, b: int) -> dict:
        """Add two integers."""
        return {"sum": a + b}

    @mcp.tool
    async def crunch(steps: int, ctx: Context) -> dict:
        """Streams progress, reports who called."""
        for i in range(1, steps + 1):
            await ctx.report_progress(i, steps)
        return {"processed": steps, "client": ctx.client_info}

    @mcp.tool
    def secret() -> dict:
        """Raises the challenge from inside a handler."""
        raise Unauthorized(scope="secret")

    def vault_guard(who):
        if who is None:
            raise Unauthorized(scope="vault")
        return True

    @mcp.tool(guards=[vault_guard])
    def vault() -> dict:
        """Guarded: the guard raises the challenge."""
        return {"ok": True}

    @mcp.tool
    async def leaky(ctx: Context) -> dict:
        """Raises the challenge after the stream is committed."""
        await ctx.info("starting")
        raise Unauthorized(scope="leaky")

    @mcp.resource("schema://demo", mime_type="application/json")
    def schema() -> str:
        """A resource."""
        return "{}"
    return mcp


def rpc(method, params=None, rid=1):
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}


def wsgi(app, body, headers=None, method="POST", path="/"):
    raw = json.dumps(body).encode() if body is not None else b""
    env = {"REQUEST_METHOD": method, "PATH_INFO": path, "CONTENT_LENGTH": str(len(raw)),
           "wsgi.input": io.BytesIO(raw), "HTTP_HOST": "example.test",
           "HTTP_ACCEPT": "application/json, text/event-stream"}
    if raw:
        env["CONTENT_TYPE"] = "application/json"
    for k, v in (headers or {}).items():
        env["HTTP_" + k.upper().replace("-", "_")] = v
    box = {}
    out = b"".join(app(env, lambda s, h: box.update(
        s=int(s.split()[0]), h={k.lower(): v for k, v in h})))
    return box["s"], (json.loads(out) if out else None), box["h"]


MODERN_META = {META_VER: PROTOCOL, META_CAPS: {}, META_CLIENT: {"name": "probe", "version": "0"}}


def modern(method, params=None, name=None):
    p = dict(params or {}); p["_meta"] = MODERN_META
    h = {"MCP-Protocol-Version": PROTOCOL, "Mcp-Method": method}
    if name:
        h["Mcp-Name"] = name
    return rpc(method, p), h


def wait(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close(); return
        except OSError:
            time.sleep(0.15)


def serve(app, port):
    import uvicorn
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    threading.Thread(target=srv.run, daemon=True).start()
    wait(port)
    return srv


def strict_section():
    print("— default posture: handshake era refused —")
    app = Server(build())
    s, j, _ = wsgi(app, rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}}))
    check("initialize refused", (s, j["error"]["code"]), (400, -32022))
    check("supported lists only the modern revision", j["error"]["data"]["supported"], [PROTOCOL])
    s, j, _ = wsgi(app, {"jsonrpc": "2.0", "id": 1, "result": {}})
    check("a posted JSON-RPC response is 202", (s, j), (202, None))
    s, j, _ = wsgi(app, rpc("tools/list"), {"MCP-Protocol-Version": "2025-11-25"})
    check("claim-less list refused on the modern ladder", (s, j["error"]["code"]), (400, -32602))
    try:
        Server(build(), legacy="sessions"); bad = False
    except ValueError:
        bad = True
    check("legacy='sessions' rejected at construction", bad, True)


def legacy_wsgi_section():
    print("\n— legacy='stateless' over WSGI —")
    app = Server(build(), legacy="stateless")
    V = {"MCP-Protocol-Version": "2025-11-25"}

    s, j, h = wsgi(app, rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                            "clientInfo": {"name": "old", "version": "1"}}))
    r = j.get("result", {})
    check("initialize answered", s, 200)
    check("echoes a supported version", r.get("protocolVersion"), "2025-11-25")
    check("serverInfo present", r.get("serverInfo"), {"name": "legacy-demo", "version": "0.1.0"})
    check("capabilities present", sorted(r.get("capabilities", {})),
          ["logging", "prompts", "resources", "tools"])
    check("no modern result fields", [k for k in ("resultType", "ttlMs", "cacheScope", "_meta") if k in r], [])
    check("no session id issued", "mcp-session-id" in h, False)
    CI = {"capabilities": {}, "clientInfo": {"name": "old", "version": "1"}}
    s, j, h = wsgi(app, rpc("initialize", {"protocolVersion": "2025-06-18", **CI}))
    check("older supported version echoed", j["result"]["protocolVersion"], "2025-06-18")
    check("response header names the served version", h.get("mcp-protocol-version"), "2025-06-18")
    s, j, _ = wsgi(app, rpc("initialize", {"protocolVersion": "2024-11-05", **CI}))
    check("unknown version answered with newest legacy", j["result"]["protocolVersion"], LEGACY_VERSIONS[0])
    s, j, _ = wsgi(app, rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}}))
    check("initialize without clientInfo is -32602", (s, j["error"]["code"]), (400, -32602))
    s, j, _ = wsgi(app, {"jsonrpc": "2.0", "id": 5, "method": "notifications/initialized"})
    check("an id-bearing initialized notification is -32600", (s, j["error"]["code"]), (400, -32600))
    s, j, _ = wsgi(app, {"jsonrpc": "2.0", "id": 1, "result": {}}, V)
    check("posted response is 202 in legacy mode too", (s, j), (202, None))
    s, j, _ = wsgi(app, rpc("initialize", {"capabilities": {}}))
    check("missing protocolVersion is -32602", (s, j["error"]["code"]), (400, -32602))
    s, j, _ = wsgi(app, rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": []}))
    check("non-object capabilities is -32602", (s, j["error"]["code"]), (400, -32602))

    s, j, _ = wsgi(app, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    check("initialized notification is 202", (s, j), (202, None))
    s, j, _ = wsgi(app, rpc("ping"), V)
    check("ping answers {}", (s, j["result"]), (200, {}))

    s, j, h = wsgi(app, rpc("tools/list"), V)
    check("tools/list served", (s, sorted(t["name"] for t in j["result"]["tools"])),
          (200, ["add", "crunch", "leaky", "secret"]))   # vault hidden: its guard denies anonymous
    check("legacy list response stamped with the request's version", h.get("mcp-protocol-version"), "2025-11-25")
    check("list result carries no modern fields", [k for k in ("resultType", "ttlMs") if k in j["result"]], [])
    s, j, _ = wsgi(app, rpc("tools/list"))
    check("no version header at all is served", s, 200)
    s, j, _ = wsgi(app, rpc("tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}}), V)
    check("tools/call served", j["result"].get("structuredContent"), {"sum": 5})
    s, j, _ = wsgi(app, rpc("tools/call", {"name": "nope", "arguments": {}}), V)
    check("unknown tool still 404/-32601", (s, j["error"]["code"]), (404, -32601))
    s, j, _ = wsgi(app, rpc("tools/call", {"name": "add", "arguments": {"a": "x", "b": 3}}), V)
    check("argument validation still applies", (s, j["error"]["code"]), (400, -32602))
    s, j, _ = wsgi(app, rpc("resources/read", {"uri": "schema://demo"}), V)
    check("resources/read served", j["result"]["contents"][0]["text"], "{}")
    s, j, _ = wsgi(app, rpc("tools/list"), {**V, "Mcp-Method": "tools/call"})
    check("a legacy Mcp-Method that disagrees with the body is -32020", (s, j["error"]["code"]), (400, -32020))
    s, j, _ = wsgi(app, rpc("tools/call", {"name": "add", "arguments": {"a": 1, "b": 1}}), {**V, "Mcp-Name": "crunch"})
    check("a legacy Mcp-Name that disagrees with the body is -32020", (s, j["error"]["code"]), (400, -32020))
    s, j, _ = wsgi(app, rpc("tools/call", {"name": "add", "arguments": {"a": 1, "b": 1}}), {**V, "Mcp-Name": "add"})
    check("a legacy Mcp-Name that agrees is fine", s, 200)
    s, j, _ = wsgi(app, rpc("tools/list", {"_meta": {META_VER: None}}), V)
    check("a null envelope claim is a modern request (-32020)", (s, j["error"]["code"]), (400, -32020))
    s, j, _ = wsgi(app, rpc("tools/list"), {**V, "Mcp-Session-Id": "abc"})
    check("a stray session id is ignored", s, 200)

    s, j, _ = wsgi(app, rpc("tools/list"), {"MCP-Protocol-Version": "2023-01-01"})
    check("unknown legacy header is 400/-32022", (s, j["error"]["code"]), (400, -32022))
    check("its supported list names both eras", j["error"]["data"]["supported"], [PROTOCOL, *LEGACY_VERSIONS])
    s, j, _ = wsgi(app, rpc("tools/list"), {"MCP-Protocol-Version": PROTOCOL})
    check("modern header without envelope stays on the modern ladder", (s, j["error"]["code"]), (400, -32602))
    s, j, _ = wsgi(app, rpc("server/discover"), V)
    check("claim-less discover is not a 2025 method", (s, j["error"]["code"]), (404, -32601))
    s, j, _ = wsgi(app, rpc("subscriptions/listen", {"notifications": {}}), V)
    check("claim-less listen is not a 2025 method", (s, j["error"]["code"]), (404, -32601))
    s, j, _ = wsgi(app, None, V, method="GET")
    check("GET is 405", s, 405)
    s, j, _ = wsgi(app, None, V, method="DELETE")
    check("DELETE is 405", s, 405)

    body, hdrs = modern("server/discover")
    s, j, _ = wsgi(app, body, hdrs)
    check("modern discover advertises both eras", j["result"]["supportedVersions"], [PROTOCOL, *LEGACY_VERSIONS])
    check("modern results still stamped", j["result"].get("resultType"), "complete")
    body, hdrs = modern("initialize", {"protocolVersion": "2025-11-25"})
    s, j, _ = wsgi(app, body, hdrs)
    check("initialize inside a modern envelope is still refused", (s, j["error"]["code"]), (400, -32022))
    body, hdrs = modern("tools/call", {"name": "crunch", "arguments": {"steps": 1}}, name="crunch")
    s, j, _ = wsgi(app, body, hdrs)
    check("ctx.client_info from the modern envelope",
          j["result"]["structuredContent"]["client"], {"name": "probe", "version": "0"})


async def legacy_asgi_section():
    print("\n— legacy='stateless' over ASGI: streaming and the official client —")
    SEEN = []

    class Spy(ASGIServer):
        async def respond(self, req, emit=None, stop=None, headers=None):
            SEEN.append((req[0], req[5]))
            return await super().respond(req, emit, stop, headers)

    port = free_port()
    srv = serve(Spy(build(), legacy="stateless"), port)
    url = f"http://127.0.0.1:{port}/mcp"
    V = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
         "MCP-Protocol-Version": "2025-11-25"}
    frames = []
    async with httpx.AsyncClient(timeout=30) as c:
        body = rpc("tools/call", {"name": "crunch", "arguments": {"steps": 3},
                                  "_meta": {"progressToken": "p1"}})
        async with c.stream("POST", url, json=body, headers=V) as r:
            ctype = r.headers.get("content-type", "")
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[6:]))
    check("legacy Context tool streams SSE", ctype.split(";")[0], "text/event-stream")
    progress = [f for f in frames if f.get("method") == "notifications/progress"]
    check("progress notifications flow with the legacy token", [p["params"]["progressToken"] for p in progress], ["p1"] * 3)
    check("final legacy result", frames[-1].get("result", {}).get("structuredContent"),
          {"processed": 3, "client": {}})
    check("legacy stream result unstamped", "resultType" in frames[-1].get("result", {}), False)
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, json=rpc("tools/call", {"name": "crunch", "arguments": {"steps": 1}}),
                         headers={**V, "Accept": "application/json"})
        check("JSON-only Accept never gets SSE", r.headers["content-type"].split(";")[0], "application/json")
        check("JSON-only call still answered", r.json()["result"]["structuredContent"]["processed"], 1)
        r = await c.post(url, json=rpc("tools/call", {"name": "crunch", "arguments": {"steps": 1}}),
                         headers={k: v for k, v in V.items() if k != "Accept"})
        check("absent Accept never gets SSE", r.headers["content-type"].split(";")[0], "application/json")

    from mcp import Client
    SEEN.clear()
    async with Client(url, mode="legacy") as cl:
        names = sorted(t.name for t in (await cl.list_tools()).tools)
        r = await cl.call_tool("add", {"a": 2, "b": 3})
        rr = (await cl.read_resource("schema://demo")).contents
    check("SDK client in legacy mode lists tools", names, ["add", "crunch", "leaky", "secret"])
    check("SDK client in legacy mode calls a tool", r.structured_content, {"sum": 5})
    check("SDK client in legacy mode reads a resource", rr[0].text, "{}")
    check("legacy client opened with initialize", ("initialize", "legacy") in SEEN, True)
    check("legacy client never probed discover", any(m == "server/discover" for m, _ in SEEN), False)

    SEEN.clear()
    async with Client(url) as cl:               # mode="auto": modern preferred
        r = await cl.call_tool("add", {"a": 5, "b": 6})
    check("SDK client in auto mode still calls", r.structured_content, {"sum": 11})
    check("auto client chose the modern era", ("server/discover", "modern") in SEEN, True)
    check("auto client sent no initialize", any(m == "initialize" for m, _ in SEEN), False)
    srv.should_exit = True


def oauth_wsgi_section():
    print("\n— OAuth handoff over WSGI —")
    DOC = {"resource": "https://example.test/mcp", "authorization_servers": ["https://as.example"],
           "bearer_methods_supported": ["header"]}

    def authenticate(headers):
        auth = headers.get("authorization", "")
        if not auth:
            raise Unauthorized(scope="read")
        if auth == "Bearer returned":
            return Unauthorized.invalid()          # the mistaken idiom: returned, not raised
        if auth != "Bearer ok":
            raise Unauthorized.invalid()
        return {"sub": "alice"}

    app = Server(build(), authenticate=authenticate, path="/mcp", resource_metadata=DOC,
                 allowed_origins={"https://app.example"})
    body, hdrs = modern("tools/list")
    s, j, h = wsgi(app, body, hdrs, path="/mcp")
    check("no token is 401", s, 401)
    check("JSON-RPC body carries -32001", j["error"]["code"], -32001)
    check("challenge names the URL derived from resource_metadata['resource']", h.get("www-authenticate"),
          f'Bearer resource_metadata="https://example.test{WELL_KNOWN}/mcp", scope="read"')
    s, j, h2 = wsgi(app, body, {**hdrs, "Host": "evil.example:8443", "X-Forwarded-Proto": "javascript"}, path="/mcp")
    check("Host and X-Forwarded-Proto cannot steer the challenge URL", h2.get("www-authenticate"), h.get("www-authenticate"))
    s, j, h = wsgi(app, body, {**hdrs, "Authorization": "Bearer returned"}, path="/mcp")
    check("an Unauthorized RETURNED from authenticate is still a 401", (s, "www-authenticate" in h), (401, True))
    s, j, h = wsgi(app, body, {**hdrs, "Authorization": "Bearer bad"}, path="/mcp")
    check("bad token is 401 with error=invalid_token", (s, 'error="invalid_token"' in h["www-authenticate"]), (401, True))
    s, j, h = wsgi(app, body, {**hdrs, "Authorization": "Bearer ok"}, path="/mcp")
    check("good token is served", (s, "result" in j), (200, True))
    s, j, h = wsgi(app, body, {**hdrs, "Origin": "https://app.example"}, path="/mcp")
    check("challenge exposed to browsers", "WWW-Authenticate" in h.get("access-control-expose-headers", ""), True)
    body, hdrs = modern("tools/call", {"name": "secret", "arguments": {}}, name="secret")
    s, j, h = wsgi(app, body, {**hdrs, "Authorization": "Bearer ok"}, path="/mcp")
    check("Unauthorized from a handler is 401 too", (s, 'scope="secret"' in h.get("www-authenticate", "")), (401, True))
    guarded = Server(build(), path="/mcp", resource_metadata=DOC)     # no authenticate: principal None
    body, hdrs = modern("tools/call", {"name": "vault", "arguments": {}}, name="vault")
    s, j, h = wsgi(guarded, body, hdrs, path="/mcp")
    check("Unauthorized from a guard is a 401 before any byte", (s, 'scope="vault"' in h.get("www-authenticate", "")), (401, True))
    shared = Unauthorized(scope="shared")
    _ = guarded._error_reply(shared, 1, {})
    check("a shared Unauthorized is never mutated", shared.resource_metadata, None)
    check("its challenge takes the server default without mutation",
          shared.challenge(guarded.metadata_url)[0][1], f'Bearer resource_metadata="https://example.test{WELL_KNOWN}/mcp", scope="shared"')

    s, j, h = wsgi(app, None, method="GET", path=WELL_KNOWN)
    check("well-known document served at the bare path", (s, j), (200, DOC))
    check("well-known readable cross-origin", h.get("access-control-allow-origin"), "*")
    s, j, h = wsgi(app, None, method="GET", path=WELL_KNOWN + "/mcp")
    check("well-known served at the path-suffixed form", (s, j), (200, DOC))
    s, j, h = wsgi(app, None, method="GET", path=WELL_KNOWN + "/other")
    check("other suffixes are not ours", s, 404)
    s, j, h = wsgi(app, rpc("tools/list"), method="POST", path=WELL_KNOWN)
    check("POST to well-known is 405 with an honest Allow", (s, h.get("allow")), (405, "GET, HEAD, OPTIONS"))
    s, j, h = wsgi(app, None, method="HEAD", path=WELL_KNOWN)
    check("HEAD on well-known is 200 without a body", (s, j), (200, None))
    s, j, h = wsgi(app, None, method="OPTIONS", path=WELL_KNOWN)
    check("OPTIONS preflight on well-known is 204 with CORS",
          (s, h.get("access-control-allow-origin"), h.get("access-control-allow-methods")), (204, "*", "GET, HEAD, OPTIONS"))
    s, j, h = wsgi(app, None, {"Origin": "https://app.example"}, method="GET", path=WELL_KNOWN)
    check("well-known CORS is * even for an allowed origin, once", h.get("access-control-allow-origin"), "*")
    hosted = Server(build(), resource_metadata=DOC, allowed_hosts={"example.test"})
    s, j, h = wsgi(hosted, None, {"Host": "evil.example"}, method="GET", path=WELL_KNOWN)
    check("well-known honors allowed_hosts", s, 403)
    s, j, h = wsgi(hosted, None, method="GET", path=WELL_KNOWN)
    check("well-known served to the allowed host", s, 200)

    plain = Server(build(), authenticate=authenticate)
    s, j, h = wsgi(plain, *modern("tools/list"))
    check("no well-known configured: challenge has no URL", h.get("www-authenticate"), 'Bearer scope="read"')
    s, j, h = wsgi(plain, None, method="GET", path=WELL_KNOWN)
    check("no well-known configured: path is not served", s, 405)

    e = Unauthorized(scope='read" \r\nX-Injected: yes', resource_metadata="https://x/y")
    check("header parameters are sanitized", "\n" in e.headers[0][1] or '\\"' in e.headers[0][1], False)
    check("sanitized challenge still names the URL", e.headers[0][1].startswith('Bearer resource_metadata="https://x/y"'), True)
    e = Unauthorized(scope="a\x00b\x7fc\u00e9d\u2028")
    check("control characters and non-ASCII never reach the header", e.headers[0][1], 'Bearer scope="abcd"')
    try:
        Error(1, "x", headers=[("Bad Name", "v")]); bad = False
    except ValueError:
        bad = True
    check("Error rejects an invalid header name", bad, True)
    check("Error strips CRLF from header values", Error(1, "x", headers=[("X", "a\r\nSet-Cookie: b")]).headers,
          [("X", "aSet-Cookie: b")])
    try:
        Server(build(), resource_metadata=["not", "a", "dict"]); bad = False
    except TypeError:
        bad = True
    check("resource_metadata must be a dict", bad, True)
    for doc in ({}, {"resource": "example.test/mcp"}, {"resource": "ftp://x/y"}, {"resource": "https://x/y?q=1"}):
        try:
            Server(build(), resource_metadata=doc); bad = False
        except ValueError:
            bad = True
        check(f"resource_metadata requires a clean absolute resource URL ({doc})", bad, True)


async def oauth_asgi_section():
    print("\n— OAuth handoff over ASGI —")
    DOC = {"resource": "http://127.0.0.1/mcp", "authorization_servers": ["https://as.example"]}

    def authenticate(headers):
        if headers.get("authorization") != "Bearer ok":
            raise Unauthorized()
        return {"sub": "alice"}

    port = free_port()
    srv = serve(ASGIServer(build(), authenticate=authenticate, path="/mcp", resource_metadata=DOC), port)
    base = f"http://127.0.0.1:{port}"
    body, hdrs = modern("tools/list")
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(base + "/mcp", json=body, headers={**hdrs, "Accept": "application/json, text/event-stream"})
        check("ASGI 401", r.status_code, 401)
        check("ASGI challenge names this server's document",
              r.headers.get("www-authenticate"), f'Bearer resource_metadata="http://127.0.0.1{WELL_KNOWN}/mcp"')
        lb, lh = modern("tools/call", {"name": "leaky", "arguments": {}}, name="leaky")
        async with c.stream("POST", base + "/mcp", json=lb, headers={**lh, "Accept": "application/json, text/event-stream",
                                                                     "Authorization": "Bearer ok"}) as rs:
            ctype = rs.headers.get("content-type", "")
            frames = [json.loads(line[6:]) async for line in rs.aiter_lines() if line.startswith("data: ")]
        check("a challenge raised mid-stream is in-band (documented limitation)",
              (ctype.split(";")[0], frames[-1].get("error", {}).get("code")), ("text/event-stream", -32001))
        r = await c.get(base + WELL_KNOWN)
        check("ASGI serves the document", (r.status_code, r.json()), (200, DOC))
        r = await c.post(base + "/mcp", json=body,
                         headers={**hdrs, "Accept": "application/json, text/event-stream", "Authorization": "Bearer ok"})
        check("ASGI authenticated request served", r.status_code, 200)
    srv.should_exit = True


async def main():
    strict_section()
    legacy_wsgi_section()
    await legacy_asgi_section()
    oauth_wsgi_section()
    await oauth_asgi_section()
    print(f"\n{OK} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


asyncio.run(main())
