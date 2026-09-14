"""Defender suite (round-three defensive audit, 2026-09-13).

Two groups, same `check()` style as test_hardening.py:

  U1-U10  pin defenses that exist in micromcp.py today but whose removal left
          the 171-check suite green in the mutation run (untested defenses).
          These PASS on the current code and FAIL on the corresponding mutant.
  N1-N9   pin invariants that the current code does NOT hold (fail-open paths
          found in the review). These FAIL on the current code and PASS with
          proposed_patch.diff applied.

Run from the project directory:  python test_defender.py
No fixed ports: everything is in-process except N9 (gunicorn), which picks a
free ephemeral port.
"""
import asyncio, io, json, logging, os, socket, subprocess, sys, time, types
import micromcp
from micromcp import MCP, Server, ASGIServer, Context, Principal, PROTOCOL, META_VER, META_CAPS

OK = FAIL = 0
def check(label, got, want):
    global OK, FAIL
    good = got == want
    OK, FAIL = OK + good, FAIL + (not good)
    print(f"  {'PASS' if good else 'FAIL'}  {label}")
    if not good:
        print(f"        got  {got!r}\n        want {want!r}")


# ── helpers (never raise on an unexpected shape: a failing check must not abort the run) ──
def body(method, params=None, *, rid=1, token=None, meta=True):
    params = dict(params or {})
    if meta:
        params["_meta"] = {META_VER: PROTOCOL, META_CAPS: {}}
        if token is not None:
            params["_meta"]["progressToken"] = token
    b = {"jsonrpc": "2.0", "method": method, "params": params}
    if rid is not None:
        b["id"] = rid
    return b

def wsgi(app, method, params=None, *, raw=None, env=None, auth=None, name=..., rid=1, token=None):
    b = body(method, params, rid=rid, token=token)
    raw = raw if raw is not None else json.dumps(b).encode()
    p = b["params"]
    if name is ...:
        name = p.get("uri") if method == "resources/read" else p.get("name")
    e = {"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
         "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL, "HTTP_MCP_METHOD": method}
    if isinstance(name, str):
        e["HTTP_MCP_NAME"] = name
    if auth:
        e["HTTP_AUTHORIZATION"] = auth
    for k, v in (env or {}).items():          # None deletes a key (e.g. "no Mcp-Method header")
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    box = {}
    out = b"".join(app(e, lambda s, h: box.update(s=s, h=dict(h))))
    return int(box["s"].split()[0]), (json.loads(out) if out else None), box["h"]

def code(resp):
    """(status, error code) — or (status, 'no error') so a wrong shape is a FAIL, not a crash."""
    s, r, _ = resp
    return s, (r or {}).get("error", {}).get("code", "no error")

def result(resp):
    s, r, _ = resp
    return (r or {}).get("result", "no result")

async def asgi(app, headers, raw, method="POST", script=None, path="/"):
    """Drive an ASGIServer in-process. `script` overrides the receive() messages."""
    scope = {"type": "http", "method": method, "path": path,
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers if v is not None]}
    msgs = list(script) if script is not None else [{"type": "http.request", "body": raw, "more_body": False}]
    calls = {"n": 0}
    async def receive():
        calls["n"] += 1
        if msgs:
            m = msgs.pop(0)
            if callable(m):
                m = await m()
            return m
        await asyncio.sleep(3600)
    sent = []
    async def send(m):
        sent.append(m)
    await app(scope, receive, send)
    status = sent[0]["status"]
    hdrs = {k.decode(): v.decode() for k, v in sent[0]["headers"]}
    data = b"".join(m.get("body", b"") for m in sent[1:])
    return status, hdrs, data, calls["n"]

def H(name, method="tools/call", *more):
    return [("content-type", "application/json"), ("accept", "application/json, text/event-stream"),
            ("mcp-protocol-version", PROTOCOL), ("mcp-method", method), ("mcp-name", name), *more]

class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(); self.seen = []
    def emit(self, record):
        self.seen.append(record.getMessage())


# ═══════════════════════════════════════════════════════════════════════════
# U: untested defenses (present today; mutation run showed no check pins them)
# ═══════════════════════════════════════════════════════════════════════════
def untested_defenses():
    logcap = LogCapture(); micromcp.log.addHandler(logcap)
    m = MCP("u")
    SECRET = "ldap://svc:hunter2@ldap.internal"

    @m.tool(guards=[lambda p: p is not None])
    def secret_tool() -> dict:
        return {"s": 1}

    def raising_guard(p):
        raise RuntimeError(SECRET)

    @m.tool(guards=[raising_guard])
    def gtool() -> dict:
        return {"leak": 1}

    @m.resource("g://res", guards=[raising_guard])
    def gres() -> str:
        return "leak"

    @m.prompt(guards=[raising_guard])
    def gprompt() -> str:
        return "leak"

    @m.prompt
    def boom_prompt(topic: str) -> str:
        raise RuntimeError("password=" + SECRET)

    class Unprintable:
        def __str__(self):
            raise RuntimeError("dsn=" + SECRET)

    @m.resource("odd://obj")
    def odd() -> object:
        return Unprintable()

    RAN = []
    @m.tool
    def side_effect() -> dict:
        RAN.append(1); return {"ok": True}

    def auth(h):
        if h.get("authorization") == "Bearer boom":
            raise RuntimeError("token store: " + SECRET)
        return {"sub": "p"} if h.get("authorization") == "Bearer good" else None
    srv = Server(m, authenticate=auth)

    print("— U1 tool guards are enforced (M08: no guarded tool in the suite) —")
    check("guarded tool, anonymous -> 404/-32601, indistinguishable from an unknown tool",
          code(wsgi(srv, "tools/call", {"name": "secret_tool", "arguments": {}})), (404, -32601))
    check("...same answer as a tool that does not exist",
          code(wsgi(srv, "tools/call", {"name": "no_such_tool", "arguments": {}})), (404, -32601))
    check("guarded tool, principal -> runs",
          result(wsgi(srv, "tools/call", {"name": "secret_tool", "arguments": {}}, auth="Bearer good")).get("structuredContent"), {"s": 1})
    names = lambda auth=None: sorted(t["name"] for t in result(wsgi(srv, "tools/list", auth=auth))["tools"])
    check("guarded tool hidden from anonymous tools/list", "secret_tool" in names(), False)
    check("guarded tool listed for a principal", "secret_tool" in names("Bearer good"), True)

    print("— U2 a guard that raises denies, everywhere, without leaking (M07) —")
    s, r, _ = wsgi(srv, "tools/call", {"name": "gtool", "arguments": {}}, auth="Bearer good")
    check("raising guard on a tool -> 404/-32601 (hidden)", (s, r["error"]["code"]), (404, -32601))
    check("...and the exception text stays server-side", SECRET in json.dumps(r), False)
    check("raising guard on a resource -> 404/-32601", code(wsgi(srv, "resources/read", {"uri": "g://res"}, auth="Bearer good")), (404, -32601))
    check("raising guard on a prompt -> 404/-32601",
          code(wsgi(srv, "prompts/get", {"name": "gprompt", "arguments": {}}, auth="Bearer good")), (404, -32601))
    check("raising guard hides from every listing",
          ("gtool" in names("Bearer good"),
           "g://res" in [x["uri"] for x in result(wsgi(srv, "resources/list", auth="Bearer good"))["resources"]],
           "gprompt" in [x["name"] for x in result(wsgi(srv, "prompts/list", auth="Bearer good"))["prompts"]]),
          (False, False, False))
    check("...and the guard failure is logged", any("guard for 'gtool' raised" in x for x in logcap.seen), True)

    print("— U3 constant error text on every 500 path (M25/M26/M27) —")
    s, r, _ = wsgi(srv, "prompts/get", {"name": "boom_prompt", "arguments": {"topic": "t"}})
    check("prompt handler exception -> constant message", (s, r["error"]["code"], r["error"]["message"]), (500, -32603, "prompt handler failed"))
    check("...no secret in the body", SECRET in json.dumps(r), False)
    s, r, _ = wsgi(srv, "tools/list", auth="Bearer boom")
    check("authenticate() exception -> 500 constant message", (s, r["error"]["code"], r["error"]["message"]), (500, -32603, "Internal error"))
    check("...no secret in the body", SECRET in json.dumps(r), False)
    s, r, _ = wsgi(srv, "resources/read", {"uri": "odd://obj"})
    check("exception in result wrapping (outside the handler try) -> constant message",
          (s, r["error"]["code"], r["error"]["message"]), (500, -32603, "Internal error"))
    check("...no secret in the body", SECRET in json.dumps(r), False)

    print("— U4 WSGI refuses an oversized Content-Length without touching wsgi.input (M17) —")
    class NoRead(io.BytesIO):
        def read(self, *a):
            raise AssertionError("body was read")
    small = Server(m, max_body=64)
    s, r, _ = wsgi(small, "tools/list", env={"CONTENT_LENGTH": "1000", "wsgi.input": NoRead()})
    check("declared Content-Length > max_body -> 413 before any read", (s, r["error"]["code"]), (413, -32600))
    big = io.BytesIO(b"x" * 10_000)
    s, r, _ = wsgi(small, "tools/list", env={"CONTENT_LENGTH": "", "wsgi.input": big, "wsgi.input_terminated": True,
                                              "HTTP_TRANSFER_ENCODING": "chunked"})
    check("de-chunked oversized body -> 413 after reading max_body+1 bytes only", (s, big.tell()), (413, 65))

    print("— U5 ASGI caps an undeclared (chunked) body while reading (M34) —")
    app = ASGIServer(m, max_body=4096, cancel_grace=0.3)
    chunk = {"type": "http.request", "body": b"x" * 1024, "more_body": True}
    st, hd, data, ncalls = asyncio.run(asgi(app, H("add"), b"", script=[chunk] * 100))
    check("chunked body over max_body -> 413", (st, json.loads(data)["error"]["code"]), (413, -32600))
    check("...without draining the rest (receive() calls)", ncalls <= 6, True)

    print("— U6 progressToken must be a string or integer (M35) —")
    for bad in ({"a": 1}, [1], True, 1.5):
        check(f"progressToken={bad!r} -> -32602", code(wsgi(srv, "tools/call", {"name": "side_effect", "arguments": {}}, token=bad)), (400, -32602))
    check("progressToken=7 accepted", code(wsgi(srv, "tools/call", {"name": "side_effect", "arguments": {}}, token=7))[0], 200)
    RAN.clear()

    print("— U7 MAX_URI / name length cap (M36) —")
    check("3000-byte uri -> -32602", code(wsgi(srv, "resources/read", {"uri": "x://" + "a" * 3000})), (400, -32602))
    check("3000-byte tool name -> -32602", code(wsgi(srv, "tools/call", {"name": "a" * 3000, "arguments": {}})), (400, -32602))

    print("— U8 pydantic nested $defs are closed (M47) —")
    try:
        import pydantic
        class PInner(pydantic.BaseModel):
            n: int
        class POuter(pydantic.BaseModel):
            inner: PInner
        @m.tool
        def pyd(o: POuter) -> dict:
            return {"n": o.inner.n}
        check("unknown key inside a nested pydantic model -> -32602",
              code(wsgi(srv, "tools/call", {"name": "pyd", "arguments": {"o": {"inner": {"n": 1, "zzz": 1}}}})), (400, -32602))
    except ImportError:
        print("  skip  (pydantic not installed)")

    print("— U9 a handler that ignores cancellation is logged (M45) —")
    @m.tool
    async def stubborn(ctx: Context = None) -> dict:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass                           # swallows the cancel
        await asyncio.sleep(0.6)           # then finishes late
        return {"late": True}
    async def hangup():
        await asyncio.sleep(0.2)
        return {"type": "http.disconnect"}
    try:
        st, hd, data, _ = asyncio.run(asgi(app, H("stubborn"), json.dumps(body("tools/call", {"name": "stubborn", "arguments": {}}, token="t")).encode(),
                                            script=[{"type": "http.request", "body": json.dumps(body("tools/call", {"name": "stubborn", "arguments": {}}, token="t")).encode(), "more_body": False}, hangup]))
    finally:
        pass
    check("stream opened then client hung up", (st, hd.get("content-type")), (200, "text/event-stream"))
    check("ignored cancellation was logged", any("ignored cancellation" in x for x in logcap.seen), True)

    print("— U10 the notification check proves the tool is wired (H3 strengthening) —")
    RAN.clear()
    s, r, _ = wsgi(srv, "tools/call", {"name": "side_effect", "arguments": {}}, rid=None)
    check("no id -> 202, handler did not run", (s, r, RAN), (202, None, []))
    s, r, _ = wsgi(srv, "tools/call", {"name": "side_effect", "arguments": {}})
    check("same call with an id -> handler ran once", (s, RAN), (200, [1]))
    micromcp.log.removeHandler(logcap)


# ═══════════════════════════════════════════════════════════════════════════
# N: invariants the current code does not hold (fail today, pass with the patch)
# ═══════════════════════════════════════════════════════════════════════════
_TC_SRC = '''
from __future__ import annotations
from typing import TYPE_CHECKING, Annotated, Union, Optional
if TYPE_CHECKING:
    from micromcp import Principal, Context
def ann(street: str, who: Annotated[Principal, "doc"]) -> dict: return {"who": who}
def uni(street: str, who: Union[Principal, None] = None) -> dict: return {"who": who}
def mix(street: str, who: Union[Principal, int]) -> dict: return {"who": who}
def ctx(street: str, c: Annotated[Context, "x"] = None) -> dict: return {}
def ok(street: str, rows: list[Principal]) -> dict: return {}
'''

def new_invariants():
    m = MCP("n")
    srv = Server(m, authenticate=lambda h: {"sub": "p"} if h.get("authorization") == "Bearer good" else None)

    print("— N1 every marker-looking string annotation is refused, not exposed —")
    mod = types.ModuleType("tc_tools"); sys.modules["tc_tools"] = mod
    exec(compile(_TC_SRC, "<tc_tools>", "exec"), mod.__dict__)
    for fn in (mod.ann, mod.uni, mod.mix, mod.ctx):
        try:
            m.tool(fn); got = f"registered with {list(m.tools[fn.__name__]['inputSchema']['properties'])}"
        except TypeError:
            got = "TypeError"
        check(f"{fn.__name__}: unresolvable wrapped Principal/Context -> TypeError at registration", got, "TypeError")
    m.tool(mod.ok)
    check("list[Principal] stays an ordinary (schemaless) parameter", list(m.tools["ok"]["inputSchema"]["properties"]), ["street", "rows"])

    print("— N2 a subclass of a marker is refused, not exposed as a schemaless argument —")
    class AdminPrincipal(Principal):
        pass
    for i, ann in enumerate((AdminPrincipal, AdminPrincipal | None)):
        def h(street: str, who: ann) -> dict:   # noqa: B023
            return {"who": who}
        h.__annotations__ = {"street": str, "who": ann, "return": dict}
        try:
            m.tool(name=f"sub{i}")(h); got = f"registered with {list(m.tools[f'sub{i}']['inputSchema']['properties'])}"
        except TypeError:
            got = "TypeError"
        check(f"{ann!r} -> TypeError at registration", got, "TypeError")

    print("— N3 a guarded template is indistinguishable from a missing one, even with a bad parameter —")
    @m.resource("crash://{cid}", guards=[lambda p: p is not None])
    def crash(cid: int) -> str:
        return f"crash {cid}"
    check("anonymous, bad int on a guarded template -> 404/-32601 (not -32602)",
          code(wsgi(srv, "resources/read", {"uri": "crash://abc"})), (404, -32601))
    check("principal, bad int -> -32602", code(wsgi(srv, "resources/read", {"uri": "crash://abc"}, auth="Bearer good")), (400, -32602))
    check("principal, good int -> 200", result(wsgi(srv, "resources/read", {"uri": "crash://7"}, auth="Bearer good"))["contents"][0]["text"], "crash 7")
    check("public API match_resource() still coerces", m.match_resource("crash://7")[1], {"cid": 7})

    print("— N4 a non-string or missing method is -32600, not a 500 or -32601 —")
    raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": ["tools/list"], "params": {"_meta": {META_VER: PROTOCOL, META_CAPS: {}}}}).encode()
    check("method=list -> 400/-32600", code(wsgi(srv, "tools/list", raw=raw)), (400, -32600))
    raw = json.dumps({"jsonrpc": "2.0", "id": 1, "params": {"_meta": {META_VER: PROTOCOL, META_CAPS: {}}}}).encode()
    s, r, _ = wsgi(srv, "tools/list", raw=raw, env={"HTTP_MCP_METHOD": None})
    check("method missing -> 400/-32600", (s, r["error"]["code"]), (400, -32600))

    print("— N5 _check fails closed on schema constructs it does not implement —")
    check("$ref outside #/$defs/ is a problem, not a pass", micromcp._check({"z": 1}, {"$ref": "#/definitions/X"}) is None, False)
    try:
        got = micromcp._check("s", {"type": ["string", "null"]}) is None
    except Exception as e:
        got = f"raises {type(e).__name__}"
    check("type given as a list is a problem, not an exception or a pass", got, False)
    check("unknown type name is a problem, not a pass", micromcp._check(5, {"type": "strng"}) is None, False)
    @m.tool(output_schema={"type": "object", "properties": {"x": {"$ref": "#/definitions/X"}}, "required": ["x"]})
    def refout() -> dict:
        return {"x": {"anything": 1}}
    check("explicit output_schema with an unresolvable $ref -> isError, not a vacuous pass",
          result(wsgi(srv, "tools/call", {"name": "refout", "arguments": {}})).get("isError"), True)

    print("— N6 a streaming tool with invalid arguments keeps its 400, not a 200 SSE frame —")
    @m.tool
    async def crunch(steps: int = 3, ctx: Context = None) -> dict:
        return {"processed": steps}
    app = ASGIServer(m)
    st, hd, data, _ = asyncio.run(asgi(app, H("crunch"), json.dumps(body("tools/call", {"name": "crunch", "arguments": {"steps": "three"}})).encode()))
    check("bad args on a Context tool -> 400 application/json -32602",
          (st, hd.get("content-type"), (json.loads(data) if hd.get("content-type") == "application/json" else {}).get("error", {}).get("code")),
          (400, "application/json", -32602))
    st, hd, data, _ = asyncio.run(asgi(app, H("crunch"), json.dumps(body("tools/call", {"name": "crunch", "arguments": {"steps": 2}}, token="t")).encode()))
    check("good args still stream", (st, hd.get("content-type")), (200, "text/event-stream"))

    print("— N7 ASGI rejects repeated Origin/Host/Authorization (no last-wins) —")
    cors = ASGIServer(m, allowed_origins={"https://ok.example"})
    raw = json.dumps(body("tools/list")).encode()
    st, hd, data, _ = asyncio.run(asgi(cors, H(None, "tools/list", ("origin", "https://evil.example"), ("origin", "https://ok.example")), raw))
    check("Origin evil then ok -> 400 (was 200)", (st, json.loads(data).get("error", {}).get("code", "no error")), (400, -32600))
    st, hd, data, _ = asyncio.run(asgi(cors, H(None, "tools/list", ("authorization", "a"), ("authorization", "b")), raw))
    check("Authorization twice -> 400", st, 400)
    st, hd, data, _ = asyncio.run(asgi(cors, H(None, "tools/list", ("origin", "https://ok.example")), raw))
    check("single allowed Origin still 200", st, 200)

    print("— N8 the thread pool survives fork() after use (C6 for the pool) —")
    if not hasattr(os, "fork"):
        print("  skip  (no fork)")
    else:
        @m.tool
        def add(a: int, b: int) -> dict:
            return {"sum": a + b}
        fsrv = Server(m, authenticate=lambda h: {"sub": "x"}, timeout=3)   # sync authenticate uses the pool
        check("parent warmup", result(wsgi(fsrv, "tools/call", {"name": "add", "arguments": {"a": 2, "b": 2}}))["structuredContent"], {"sum": 4})
        rd, wr = os.pipe()
        pid = os.fork()
        if pid == 0:                                       # child: answer within timeout=3
            os.close(rd)
            try:
                s, r, _ = wsgi(fsrv, "tools/call", {"name": "add", "arguments": {"a": 2, "b": 2}})
                os.write(wr, json.dumps([s, r.get("result", {}).get("structuredContent")]).encode())
            except BaseException as e:                     # pragma: no cover
                os.write(wr, json.dumps(["exc", str(e)]).encode())
            os._exit(0)
        os.close(wr)
        import select
        ready, _, _ = select.select([rd], [], [], 10)
        if ready:
            got = json.loads(os.read(rd, 65536) or b'"nothing"'); os.waitpid(pid, 0)
        else:
            os.kill(pid, 9); os.waitpid(pid, 0); got = "child hung after fork (10s)"
        check("child after fork answers on the pool (no hang, no 500)", got, [200, {"sum": 4}])

    print("— N9 gunicorn --preload check, rewritten to record PASS and to own its port —")
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0)); port = sk.getsockname()[1]
    def listening():
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close(); return True
        except OSError:
            return False
    check("port is free before gunicorn starts", listening(), False)
    p = subprocess.Popen([sys.executable, "-m", "gunicorn", "--preload", "-b", f"127.0.0.1:{port}", "-w", "2", "--timeout", "10",
                          "--log-level", "error", "test_defender:_gunicorn_app"],
                         env={**os.environ, "MICROMCP_WARMUP": "1"}, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         cwd=os.path.dirname(os.path.abspath(__file__)), start_new_session=True)   # own process group
    try:
        end = time.time() + 20
        while time.time() < end and not listening():
            time.sleep(0.1)
        check("gunicorn came up on the fresh port", listening(), True)
        try:
            import httpx
            r = httpx.post(f"http://127.0.0.1:{port}/", json=body("tools/call", {"name": "warmed", "arguments": {}}),
                           headers={"Content-Type": "application/json", "MCP-Protocol-Version": PROTOCOL, "Mcp-Method": "tools/call", "Mcp-Name": "warmed"}, timeout=8)
            got = r.json()["result"]["structuredContent"]
        except Exception as e:
            got = f"{type(e).__name__}: {e}"
        check("request served after fork by a pool warmed in the master (--preload)",
              isinstance(got, dict) and got.get("warm_pid") not in (None, got.get("pid")), True)
    finally:
        import contextlib, signal
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(p.pid, signal.SIGKILL)   # master AND workers: an orphan would keep the port and the pipe
        p.wait(timeout=10)


# gunicorn target for N9: a server whose pool and loop are used before the fork
_gm = MCP("gun")
@_gm.tool
def add(a: int, b: int) -> dict:
    return {"sum": a + b}
_gunicorn_app = Server(_gm, authenticate=lambda h: {"sub": "x"})
if os.environ.get("MICROMCP_WARMUP"):
    wsgi(_gunicorn_app, "tools/call", {"name": "add", "arguments": {"a": 1, "b": 1}})
    _WARM_PID = os.getpid()

    @_gm.tool
    def warmed() -> dict:
        return {"warm_pid": _WARM_PID, "pid": os.getpid()}


if __name__ == "__main__":
    untested_defenses()
    new_invariants()
    print(f"\n{OK} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)
