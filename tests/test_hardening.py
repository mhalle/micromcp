"""Regression suite for the 2026-09 adversarial review: one check per fixed
finding, keyed C1-C7 (critical) and H1-H9 (high). See README "Hardening".

In-process WSGI where a finding is transport-neutral; uvicorn, waitress, and
gunicorn where it was not.   needs: uvicorn, httpx, waitress, gunicorn, mcp
"""
import asyncio, contextlib, dataclasses, datetime, enum, io, json, os, signal, socket, subprocess
import sys, threading, time, types, typing
import httpx
from micromcp import (MCP, Server, ASGIServer, Context, Principal,
                      PROTOCOL, META_VER, META_CAPS)
from _helpers import free_port

OK = FAIL = 0
def check(label, got, want):
    global OK, FAIL
    good = got == want
    OK, FAIL = OK + good, FAIL + (not good)
    print(f"  {'PASS' if good else 'FAIL'}  {label}")
    if not good:
        print(f"        got  {got!r}\n        want {want!r}")


# ── fixtures ────────────────────────────────────────────────────────────────
class Color(enum.Enum):
    RED = "red"; BLUE = "blue"

class CrashCount(typing.TypedDict):
    street: str
    count: int

@dataclasses.dataclass
class Box:
    name: str
    n: int

mcp = MCP("hardening", "0.1.0")
CANCELLED = threading.Event()
RAN = []

@mcp.tool
def add(a: int, b: int) -> dict:
    """Add two integers."""
    return {"sum": a + b}

@mcp.tool
def boxed() -> Box:
    """Returns a dataclass instance; must become structuredContent."""
    return Box("a", 1)

@mcp.tool
def wrong_shape() -> CrashCount:
    """Declares CrashCount, returns something else."""
    return {"totally": "wrong"}  # type: ignore[return-value]

@mcp.tool
def when(d: datetime.date = datetime.date(2020, 1, 1), c: Color = Color.RED) -> dict:
    """Non-JSON defaults must not kill tools/list."""
    return {"d": str(d), "c": c if isinstance(c, str) else c.value}

@mcp.tool
def slow() -> dict:
    """Sync handler that sleeps; must not serialize or block the loop."""
    time.sleep(0.3)
    return {"thread": threading.current_thread().name}

@mcp.tool
def side_effect() -> dict:
    """Must never run for a notification (no id)."""
    RAN.append(1)
    return {"ok": True}

@mcp.tool
async def crunch(steps: int = 3, ctx: Context = None) -> dict:
    """Streams progress."""
    for i in range(1, steps + 1):
        await ctx.report_progress(i, steps)
    return {"processed": steps}

@mcp.tool
async def bad_note(ctx: Context = None) -> dict:
    """Emits a non-serializable log payload; must not tear the stream down."""
    await ctx.info({"ts": datetime.datetime.now()})
    return {"unreachable": True}

@mcp.tool
async def tight(ctx: Context = None) -> dict:
    """Emits with no awaits between frames except the queue itself."""
    try:
        for i in range(10_000_000):
            await ctx.report_progress(i, 10_000_000)
        return {"done": True}
    except asyncio.CancelledError:
        CANCELLED.set()
        raise

@mcp.resource("secret://vault", guards=[lambda p: p is not None])
def vault() -> str:
    return "TOP SECRET"

@mcp.resource("schema://public")
def public() -> str:
    return "public"

@mcp.resource("boom://res")
def boom_res() -> str:
    raise RuntimeError("password=hunter2")

@mcp.resource("crash://{crash_id}")
def one_crash(crash_id: int) -> str:
    return json.dumps({"id": crash_id})

@mcp.resource("event://{year}-{month}-{day}-{seq}-{kind}")
def event(year: str, month: str, day: str, seq: str, kind: str) -> str:
    return "x"

@mcp.prompt(guards=[lambda p: p is not None])
def secret_prompt(topic: str) -> str:
    return f"secret {topic}"

# C1: a module using PEP 563 with an unresolvable forward reference in the
# SAME signature as a Principal parameter.
_FUTURE_SRC = '''
from __future__ import annotations
from typing import TYPE_CHECKING
from micromcp import Principal, Context
if TYPE_CHECKING:
    from nowhere import Row
def audit(street: str, who: Principal, rows: list[Row] = None) -> dict:
    return {"principal": who}
def opt(street: str, who: Principal | None = None) -> dict:
    return {"principal": who}
'''
_future = types.ModuleType("future_tools")
sys.modules["future_tools"] = _future
exec(compile(_FUTURE_SRC, "<future_tools>", "exec"), _future.__dict__)
mcp.tool(_future.audit)
mcp.tool(_future.opt)


def auth(headers):
    return {"sub": "real"} if headers.get("authorization") == "Bearer good" else None

wsgi_app = Server(mcp, allowed_origins={"https://ok.example"}, authenticate=auth)
asgi_app = ASGIServer(mcp, allowed_origins={"https://ok.example"}, authenticate=auth)

if os.environ.get("MICROMCP_WARMUP"):      # C6: import-time request before fork
    _raw = json.dumps({"jsonrpc": "2.0", "id": 0, "method": "tools/list",
                       "params": {"_meta": {META_VER: PROTOCOL, META_CAPS: {}}}}).encode()
    wsgi_app({"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(_raw)),
              "wsgi.input": io.BytesIO(_raw), "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL,
              "HTTP_MCP_METHOD": "tools/list"}, lambda s, h: None)
    _WARM_PID = os.getpid()

    @mcp.tool
    def warmed() -> dict:
        """Exists only when the warm-up ran; reports which process ran it."""
        return {"warm_pid": _WARM_PID, "pid": os.getpid()}


# ── helpers ─────────────────────────────────────────────────────────────────
def body(method, params=None, *, rid=1, meta=True, token=None):
    params = dict(params or {})
    if meta:
        params["_meta"] = {META_VER: PROTOCOL, META_CAPS: {}}
        if token is not None:
            params["_meta"]["progressToken"] = token
    b = {"jsonrpc": "2.0", "method": method, "params": params}
    if rid is not None:
        b["id"] = rid
    return b

def rpc(method, params=None, *, raw=None, hdrs=None, auth_hdr=None, name=None, rid=1):
    """Drive the WSGI app in-process."""
    b = body(method, params, rid=rid)
    raw = raw if raw is not None else json.dumps(b).encode()
    p = b["params"]
    env = {"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(raw)),
           "wsgi.input": io.BytesIO(raw), "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL,
           "HTTP_MCP_METHOD": method}
    nm = name if name is not None else (p.get("uri") if method == "resources/read" else p.get("name"))
    if nm is not None:
        env["HTTP_MCP_NAME"] = nm
    if auth_hdr:
        env["HTTP_AUTHORIZATION"] = auth_hdr
    for k, v in (hdrs or {}).items():
        env["HTTP_" + k.upper().replace("-", "_")] = v
    box = {}
    out = b"".join(wsgi_app(env, lambda s, h: box.update(s=s, h=dict(h))))
    return int(box["s"].split()[0]), (json.loads(out) if out else None)

def err(resp):
    return resp[0], resp[1]["error"]["code"]

def wait(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close(); return True
        except OSError:
            time.sleep(0.1)
    return False

def serve_uvicorn(app, port):
    import uvicorn
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    threading.Thread(target=srv.run, daemon=True).start()
    wait(port)
    return srv

def read_child(rd, pid, timeout):
    """Read one JSON value a forked child wrote to `rd`, or kill it after `timeout`."""
    import select
    ready, _, _ = select.select([rd], [], [], timeout)
    if not ready:
        os.kill(pid, signal.SIGKILL); os.waitpid(pid, 0)
        return f"child hung after fork ({timeout}s)"
    data = os.read(rd, 65536); os.waitpid(pid, 0)
    return json.loads(data or b'"nothing"')


HDRS = lambda tool, **extra: {"Content-Type": "application/json",
                              "Accept": "application/json, text/event-stream",
                              "MCP-Protocol-Version": PROTOCOL,
                              "Mcp-Method": "tools/call", "Mcp-Name": tool, **extra}


def main():
    global OK, FAIL
    # ── in-process checks ───────────────────────────────────────────────────────
    print("— C1 Principal detection survives unresolvable forward refs —")
    tools = {t["name"]: t for t in rpc("tools/list")[1]["result"]["tools"]}
    check("audit: who is not in the schema", "who" in tools["audit"]["inputSchema"]["properties"], False)
    check("opt: Optional[Principal] not in the schema", "who" in tools["opt"]["inputSchema"]["properties"], False)
    check("client-supplied who is rejected",
          err(rpc("tools/call", {"name": "audit", "arguments": {"street": "x", "who": {"sub": "attacker"}}})),
          (400, -32602))
    check("real principal injected",
          rpc("tools/call", {"name": "audit", "arguments": {"street": "x"}}, auth_hdr="Bearer good")
          [1]["result"]["structuredContent"], {"principal": {"sub": "real"}})

    print("— C2 guards on resources, prompts, and listings —")
    check("guarded resource, anonymous -> 404", err(rpc("resources/read", {"uri": "secret://vault"})), (404, -32601))
    check("guarded resource, principal -> 200",
          rpc("resources/read", {"uri": "secret://vault"}, auth_hdr="Bearer good")[1]["result"]["contents"][0]["text"],
          "TOP SECRET")
    check("guarded prompt, anonymous -> 404",
          err(rpc("prompts/get", {"name": "secret_prompt", "arguments": {"topic": "t"}})), (404, -32601))
    check("guarded prompt hidden from anonymous prompts/list",
          [p["name"] for p in rpc("prompts/list")[1]["result"]["prompts"]], [])
    check("guarded resource hidden from anonymous resources/list",
          "secret://vault" in [r["uri"] for r in rpc("resources/list")[1]["result"]["resources"]], False)

    print("— C3 -32022 carries data.requested —")
    s, r = rpc("initialize")
    check("handshake refusal has requested", r["error"]["data"], {"supported": [PROTOCOL], "requested": PROTOCOL})
    s, r = rpc("tools/list", hdrs={"MCP-PROTOCOL-VERSION": "2025-11-25"},
               raw=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                               "params": {"_meta": {META_VER: "2025-11-25", META_CAPS: {}}}}).encode())
    check("old version refusal has requested", r["error"]["data"], {"supported": [PROTOCOL], "requested": "2025-11-25"})
    try:
        from mcp.client._probe import _parse_supported
        check("official SDK parses the refusal", _parse_supported(r["error"]["data"]), [PROTOCOL])
    except ImportError:
        print("  skip  (mcp not installed)")

    print("— C4 malformed envelopes are 400s, not crashes —")
    for raw in (b"[]", b"null", b"5", b'"x"', b"true", b'[{"jsonrpc":"2.0","id":1,"method":"tools/list"}]'):
        check(f"body {raw.decode()!r} -> 400/-32600", err(rpc("tools/list", raw=raw)), (400, -32600))
    check("id=true -> 400/-32600", err(rpc("tools/list", rid=True)), (400, -32600))
    check("id=null -> 400/-32600",
          err(rpc("tools/list", raw=json.dumps({**body("tools/list"), "id": None}).encode())), (400, -32600))
    check("params=list -> 400/-32602",
          err(rpc("tools/list", raw=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": []}).encode())),
          (400, -32602))
    check("name=list -> 400/-32602",
          err(rpc("tools/call", raw=json.dumps(body("tools/call", {"name": ["add"]})).encode(), name="x")), (400, -32602))
    check("arguments=list -> 400/-32602",
          err(rpc("tools/call", raw=json.dumps(body("tools/call", {"name": "add", "arguments": [1]})).encode(), name="add")),
          (400, -32602))
    _box = {}
    _out = b"".join(wsgi_app({"REQUEST_METHOD": "POST", "CONTENT_LENGTH": "abc", "wsgi.input": io.BytesIO(b""),
                              "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL, "HTTP_MCP_METHOD": "tools/list"},
                             lambda st, h: _box.update(s=st)))
    check("Content-Length=abc -> no crash (empty body is a notification -> 202)", _box["s"], "202 Accepted")
    s, r = rpc("resources/read", {"uri": "boom://res"})
    check("resource exception -> 500 with constant message", (s, r["error"]["message"]), (500, "resource handler failed"))

    print("— C7 non-JSON defaults —")
    check("tools/list survives date/enum defaults", "when" in tools, True)
    check("date default rendered", tools["when"]["inputSchema"]["properties"]["d"],
          {"type": "string", "format": "date", "default": "2020-01-01"})
    check("enum default rendered", tools["when"]["inputSchema"]["properties"]["c"],
          {"enum": ["red", "blue"], "type": "string", "default": "red"})

    print("— H1 Mcp-Name binds to uri on resources/read —")
    check("decoy name does not satisfy Mcp-Name",
          err(rpc("resources/read", {"name": "schema://public", "uri": "secret://vault"}, name="schema://public",
                  auth_hdr="Bearer good")), (400, -32020))

    print("— H3 notifications are acknowledged, never dispatched —")
    s, r = rpc("tools/call", {"name": "side_effect", "arguments": {}}, rid=None)
    check("no id -> 202 empty body", (s, r), (202, None))
    check("handler did not run", RAN, [])

    print("— H4 Accept is honored —")
    check("Accept without application/json -> 406",
          rpc("tools/list", hdrs={"ACCEPT": "text/plain"})[0], 406)
    check("no Accept header is permissive", rpc("tools/list")[0], 200)

    print("— H7 structured output —")
    s, r = rpc("tools/call", {"name": "boxed", "arguments": {}})
    check("dataclass return -> structuredContent", r["result"].get("structuredContent"), {"name": "a", "n": 1})
    s, r = rpc("tools/call", {"name": "wrong_shape", "arguments": {}})
    check("outputSchema mismatch -> isError", r["result"].get("isError"), True)
    check("...naming the problem", "outputSchema" in r["result"]["content"][0]["text"], True)

    print("— H8 argument validation —")
    check("add(1,2) ok", rpc("tools/call", {"name": "add", "arguments": {"a": 1, "b": 2}})[1]["result"]["structuredContent"], {"sum": 3})
    check('add("1","2") -> -32602', err(rpc("tools/call", {"name": "add", "arguments": {"a": "1", "b": "2"}})), (400, -32602))
    check("bool for int -> -32602", err(rpc("tools/call", {"name": "add", "arguments": {"a": True, "b": 2}})), (400, -32602))
    check("missing arg -> -32602", err(rpc("tools/call", {"name": "add", "arguments": {"a": 1}})), (400, -32602))
    check("extra arg -> -32602", err(rpc("tools/call", {"name": "add", "arguments": {"a": 1, "b": 2, "c": 3}})), (400, -32602))
    check("schema closes additionalProperties", tools["add"]["inputSchema"].get("additionalProperties"), False)

    print("— H9 templates are linear; bad coercion is -32602 —")
    long_uri = "event://" + "-" * 400 + "/"
    t0 = time.time(); s, _ = rpc("resources/read", {"uri": long_uri}); dt = time.time() - t0
    check("400-byte non-matching URI -> 404 fast", (s, dt < 0.5), (404, True))
    check("crash://abc -> 400/-32602", err(rpc("resources/read", {"uri": "crash://abc"})), (400, -32602))
    check("crash://42 still works", json.loads(rpc("resources/read", {"uri": "crash://42"})[1]["result"]["contents"][0]["text"]), {"id": 42})


    # ── C5: sync handlers overlap on WSGI (waitress) and don't block uvicorn ────
    print("— C5 sync handlers do not serialize / block —")
    import waitress
    wport = free_port()
    threading.Thread(target=waitress.serve, kwargs=dict(app=wsgi_app, host="127.0.0.1", port=wport,
                                                         _quiet=True, threads=8), daemon=True).start()
    wait(wport)
    def call_slow():
        r = httpx.post(f"http://127.0.0.1:{wport}/mcp", json=body("tools/call", {"name": "slow", "arguments": {}}),
                       headers=HDRS("slow"), timeout=30)
        return r.json()["result"]["structuredContent"]["thread"]
    t0 = time.time(); ths = [threading.Thread(target=call_slow) for _ in range(8)]
    [t.start() for t in ths]; [t.join() for t in ths]
    dt = time.time() - t0
    check(f"waitress: 8 concurrent 0.3s sync calls overlap ({dt:.2f}s)", dt < 1.0, True)


    async def async_checks():
        port = free_port()
        srv = serve_uvicorn(asgi_app, port)
        url = f"http://127.0.0.1:{port}/mcp"
        async with httpx.AsyncClient(timeout=30) as c:
            t0 = time.time()
            await asyncio.gather(*[c.post(url, json=body("tools/call", {"name": "slow", "arguments": {}}),
                                          headers=HDRS("slow")) for _ in range(4)])
            dt = time.time() - t0
            check(f"uvicorn: 4 concurrent sync calls overlap ({dt:.2f}s)", dt < 0.9, True)

            print("— H5 validation runs before the stream is committed —")
            r = await c.post(url, json=body("tools/call", {"name": "crunch", "arguments": {}}),
                             headers=HDRS("crunch", Origin="https://evil.example"))
            check("bad Origin on Context tool -> 403 JSON", (r.status_code, r.headers["content-type"].split(";")[0]),
                  (403, "application/json"))
            r = await c.post(url, json=body("tools/call", {"name": "crunch", "arguments": {}}),
                             headers=HDRS("wrong"))
            check("Mcp-Name mismatch on Context tool -> 400 JSON", (r.status_code, r.json()["error"]["code"]), (400, -32020))

            print("— H4 Context tool without text/event-stream falls back to JSON —")
            r = await c.post(url, json=body("tools/call", {"name": "crunch", "arguments": {}}),
                             headers=HDRS("crunch", Accept="application/json"))
            check("JSON fallback", (r.headers["content-type"].split(";")[0], r.json()["result"]["structuredContent"]),
                  ("application/json", {"processed": 3}))

            print("— H6 bad notification payload does not kill the stream —")
            frames = []
            async with c.stream("POST", url, json=body("tools/call", {"name": "bad_note", "arguments": {}}),
                                headers=HDRS("bad_note")) as r:
                async for line in r.aiter_lines():
                    if line.startswith("data: "):
                        frames.append(json.loads(line[6:]))
            check("stream ended cleanly with one frame", len(frames), 1)
            check("...which is an in-band isError", frames[0]["result"].get("isError"), True)

            print("— H2 tight emitter is cancelled on hang-up —")
            CANCELLED.clear()
            async with c.stream("POST", url, json=body("tools/call", {"name": "tight", "arguments": {}}, token="t1"),
                                headers=HDRS("tight")) as r:
                n = 0
                async for line in r.aiter_lines():
                    n += 1
                    if n >= 5:
                        break
            for _ in range(60):
                if CANCELLED.is_set():
                    break
                await asyncio.sleep(0.05)
            check("handler cancelled within 3s", CANCELLED.is_set(), True)
            r = await c.post(url, json=body("tools/call", {"name": "add", "arguments": {"a": 1, "b": 1}}),
                             headers=HDRS("add"))
            check("server still responsive", r.json()["result"]["structuredContent"], {"sum": 2})
        srv.should_exit = True

    asyncio.run(async_checks())


    # ── C6: gunicorn --preload with an import-time request ──────────────────────
    print("— C6 gunicorn --preload survives an import-time warmup —")
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0)); gport = sk.getsockname()[1]
    check("gunicorn port is free beforehand", wait(gport, timeout=0.3), False)
    env = {**os.environ, "MICROMCP_WARMUP": "1"}
    p = subprocess.Popen([sys.executable, "-m", "gunicorn", "--preload", "-b", f"127.0.0.1:{gport}", "-w", "2",
                          "--timeout", "10", "--log-level", "error", "test_hardening:wsgi_app"],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         cwd=os.path.dirname(os.path.abspath(__file__)), start_new_session=True)
    got = None
    try:
        check("gunicorn came up", wait(gport), True)
        try:
            r = httpx.post(f"http://127.0.0.1:{gport}/mcp", json=body("tools/call", {"name": "warmed", "arguments": {}}),
                           headers=HDRS("warmed"), timeout=8)
            got = r.json()["result"]["structuredContent"]
        except Exception as e:
            got = f"{type(e).__name__}: {e}"
        # The warm-up must have run (the tool exists) in the MASTER (--preload), i.e.
        # before the fork: losing either --preload or the env var fails this check.
        check("request served after fork by a pool warmed pre-fork",
              isinstance(got, dict) and got.get("warm_pid") not in (None, got.get("pid")), True)
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(p.pid, signal.SIGKILL)
        p.wait(timeout=10)
        if not (isinstance(got, dict) and "warm_pid" in got):
            print("        got:", got, "\n        gunicorn stderr:", p.stderr.read().decode()[-1500:])



# ═══════════════════════════════════════════════════════════════════════════
# Medium findings (M1-M15) from the same review.
# ═══════════════════════════════════════════════════════════════════════════
import micromcp

def medium():
    global OK, FAIL
    print("\n— M2 serverInfo travels in _meta on every result —")
    s, r = rpc("tools/list")
    check("_meta serverInfo on tools/list", r["result"]["_meta"][micromcp.META_SERVER],
          {"name": "hardening", "version": "0.1.0"})
    s, r = rpc("server/discover")
    check("discover has no top-level serverInfo", "serverInfo" in r["result"], False)
    try:
        from mcp_types._v2026_07_28 import DiscoverResult
        check("SDK DiscoverResult validates", bool(DiscoverResult.model_validate(r["result"])), True)
    except ImportError:
        print("  skip  (mcp_types not installed)")

    print("— M4 body limits —")
    small = Server(mcp, max_body=64)
    big = json.dumps(body("tools/list", {"pad": "x" * 200})).encode()
    box = {}
    b"".join(small({"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(big)), "wsgi.input": io.BytesIO(big),
                    "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL, "HTTP_MCP_METHOD": "tools/list"},
                   lambda st, h: box.update(s=st)))
    check("oversized body -> 413 before reading", box["s"], "413 Payload Too Large")
    box = {}
    b"".join(wsgi_app({"REQUEST_METHOD": "POST", "wsgi.input": io.BytesIO(b""), "HTTP_TRANSFER_ENCODING": "chunked",
                       "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL, "HTTP_MCP_METHOD": "tools/list"},
                      lambda st, h: box.update(s=st)))
    check("chunked without Content-Length -> 411", box["s"], "411 Length Required")

    print("— M5 duplicate routing headers —")
    check("WSGI comma-joined Mcp-Method -> -32020",
          err(rpc("tools/list", hdrs={"MCP-METHOD": "prompts/list, tools/list"})), (400, -32020))

    print("— M8 cursors are rejected —")
    check("cursor -> -32602", err(rpc("tools/list", {"cursor": "abc"})), (400, -32602))

    print("— M9 validation ladder —")
    s, r = rpc("tools/list", raw=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode())
    check("missing _meta -> -32602 naming both keys", (s, r["error"]["code"], META_VER in r["error"]["message"]),
          (400, -32602, True))
    check("resources/read without uri -> 404, not a header complaint",
          err(rpc("resources/read", {}, name=None)), (404, -32601))

    print("— M10 Content-Type / CORS / Allow / Host —")
    check("Content-Type text/plain -> 400", err(rpc("tools/list", hdrs={"CONTENT-TYPE": "text/plain"})), (400, -32600))
    check("Content-Type application/json; charset=utf-8 ok",
          rpc("tools/list", hdrs={"CONTENT-TYPE": "application/json; charset=utf-8"})[0], 200)
    check("empty Origin is untrusted", rpc("tools/list", hdrs={"ORIGIN": ""})[0], 403)
    box = {}
    b"".join(wsgi_app({"REQUEST_METHOD": "OPTIONS", "wsgi.input": io.BytesIO(b""), "HTTP_ORIGIN": "https://ok.example",
                       "HTTP_ACCESS_CONTROL_REQUEST_METHOD": "POST"}, lambda st, h: box.update(s=st, h=dict(h))))
    check("preflight from allowed origin -> 204 + CORS", (box["s"], box["h"].get("Access-Control-Allow-Origin"),
                                                          box["h"].get("Access-Control-Allow-Methods")),
          ("204 No Content", "https://ok.example", "POST, OPTIONS"))
    box = {}
    b"".join(wsgi_app({"REQUEST_METHOD": "OPTIONS", "wsgi.input": io.BytesIO(b""), "HTTP_ORIGIN": "https://evil.example"},
                      lambda st, h: box.update(s=st, h=dict(h))))
    check("preflight from other origin -> 403, no CORS", (box["s"], "Access-Control-Allow-Origin" in box["h"]),
          ("403 Forbidden", False))
    s, r = rpc("tools/list", hdrs={"ORIGIN": "https://ok.example"})
    box = {}
    b"".join(wsgi_app({"REQUEST_METHOD": "GET", "wsgi.input": io.BytesIO(b"")}, lambda st, h: box.update(s=st, h=dict(h))))
    check("405 carries Allow", (box["s"], box["h"].get("Allow")), ("405 Method Not Allowed", "POST, OPTIONS"))
    check("responses carry MCP-Protocol-Version", box["h"].get("MCP-Protocol-Version"), PROTOCOL)
    hosted = Server(mcp, allowed_hosts={"mcp.example"})
    def host_call(host):
        raw = json.dumps(body("tools/list")).encode(); bx = {}
        b"".join(hosted({"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
                         "HTTP_HOST": host, "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL, "HTTP_MCP_METHOD": "tools/list"},
                        lambda st, h: bx.update(s=st)))
        return bx["s"]
    check("allowed_hosts admits host:port", host_call("mcp.example:8443"), "200 OK")
    check("allowed_hosts rejects a rebound host", host_call("attacker.example"), "403 Forbidden")

    print("— M11 path option —")
    pathed = Server(mcp, path="/mcp")
    raw = json.dumps(body("tools/list")).encode()
    def at(p):
        bx = {}
        b"".join(pathed({"REQUEST_METHOD": "POST", "PATH_INFO": p, "CONTENT_LENGTH": str(len(raw)),
                         "wsgi.input": io.BytesIO(raw), "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL,
                         "HTTP_MCP_METHOD": "tools/list"}, lambda st, h: bx.update(s=st)))
        return bx["s"]
    check("path=/mcp serves /mcp and /mcp/", (at("/mcp"), at("/mcp/")), ("200 OK", "200 OK"))
    check("path=/mcp 404s elsewhere", at("/favicon.ico"), "404 Not Found")

    print("— M12 a parameter named `request` is a normal argument —")
    m2 = MCP("m2")
    @m2.tool
    def search(request: str, limit: int = 10) -> dict:
        return {"q": request}
    check("request in schema", list(m2.tools["search"]["inputSchema"]["properties"]), ["request", "limit"])

    print("— M13 template registration and coercion —")
    for uri in ("q://{?q}", "f://{+path}", "a://{1x}", "a://{x}/{x}", "a://{a-b}"):
        try:
            m2.resource(uri)(lambda **k: "")
            check(f"{uri} rejected at registration", "no error", "ValueError")
        except ValueError:
            check(f"{uri} rejected at registration", "ValueError", "ValueError")
    @m2.resource("x://{a}")
    def short(a: str) -> str: return "short"
    @m2.resource("x://item/{a}")
    def longer(a: str) -> str: return "long"
    check("longest literal prefix wins", m2.match_resource("x://item/1")[0]["name"], "longer")
    for bad in ("+12", " 12 ", "1_2", "١٢"):
        try:
            micromcp._coerce(bad, int); got = "accepted"
        except ValueError:
            got = "ValueError"
        check(f"int coercion rejects {bad!r}", got, "ValueError")
    try:
        micromcp._coerce("banana", bool); got = "accepted"
    except ValueError:
        got = "ValueError"
    check("bool coercion rejects 'banana'", got, "ValueError")

    print("— M14 registration footguns —")
    try:
        @m2.tool
        def search(request: str) -> dict: return {}
        got = "silently replaced"
    except ValueError:
        got = "ValueError"
    check("duplicate tool name -> ValueError", got, "ValueError")
    m2.tool(replace=True)(search)
    check("replace=True allowed", "search" in m2.tools, True)
    for badname in ("has space", "a/b", "x" * 65, ""):
        try:
            m2.tool(name=badname)(lambda: {}); got = "accepted"
        except ValueError:
            got = "ValueError"
        check(f"tool name {badname[:12]!r} -> ValueError", got, "ValueError")
    try:
        m2.tool(lambda: {}); got = "accepted"
    except ValueError:
        got = "ValueError"
    check("lambda (<lambda>) -> ValueError", got, "ValueError")
    try:
        @m2.tool
        def posonly(a: int, /) -> dict: return {}
        got = "accepted"
    except TypeError:
        got = "TypeError"
    check("positional-only -> TypeError", got, "TypeError")
    @m2.tool
    def star(a: int, *args, **kwargs) -> dict: return {}
    check("*args/**kwargs not in schema", list(m2.tools["star"]["inputSchema"]["properties"]), ["a"])
    check("**kwargs keeps additionalProperties open", "additionalProperties" in m2.tools["star"]["inputSchema"], False)

    print("— M15 schema coverage and docstrings —")
    m3 = MCP("m3")
    UserId = typing.NewType("UserId", int)
    @m3.tool
    def typed(pair: tuple[int, str], tags: set[str], counts: dict[str, int], mode: typing.Literal["a", "b"],
              uid: UserId, anything: typing.Any = None) -> dict:
        """Do a thing.

        Args:
            pair: A pair of things.
            tags (set): Some tags,
                continued on the next line.
            mode: The mode.

        Returns:
            Nothing useful.
        """
        return {}
    sch = m3.tools["typed"]["inputSchema"]["properties"]
    check("tuple -> prefixItems", sch["pair"], {"type": "array", "prefixItems": [{"type": "integer"}, {"type": "string"}],
                                               "minItems": 2, "maxItems": 2, "description": "A pair of things."})
    check("set -> uniqueItems", sch["tags"], {"type": "array", "items": {"type": "string"}, "uniqueItems": True,
                                             "description": "Some tags, continued on the next line."})
    check("dict[str,int] -> additionalProperties", sch["counts"], {"type": "object", "additionalProperties": {"type": "integer"}})
    check("Literal -> enum", sch["mode"], {"enum": ["a", "b"], "type": "string", "description": "The mode."})
    check("NewType -> supertype", sch["uid"], {"type": "integer"})
    check("Any -> {}", sch["anything"], {"default": None})
    check("Args section stripped from description", m3.tools["typed"]["description"],
          "Do a thing.\n\nReturns:\n    Nothing useful.")
    check("tuple length enforced", micromcp._check([1, "x", 3], sch["pair"], "pair"), "pair allows at most 2 items")
    check("uniqueItems enforced", micromcp._check(["a", "a"], sch["tags"], "tags"), "tags must not contain duplicates")
    check("dict values enforced", micromcp._check({"k": "v"}, sch["counts"], "counts"), "counts.k must be integer")

    @m3.prompt
    def review(street: str, year: int = 2025, who: Principal = None) -> str:
        """Review a street.

        Args:
            street: Street name.
            year: Four-digit year.
        """
        return f"{street}/{year}/{type(year).__name__}/{who}"
    check("prompt args carry descriptions, principal hidden", m3.prompts["review"]["arguments"],
          [{"name": "street", "required": True, "description": "Street name."},
           {"name": "year", "required": False, "description": "Four-digit year."}])
    srv3 = Server(m3, authenticate=lambda h: {"sub": "p"})
    def prpc(args):
        raw = json.dumps(body("prompts/get", {"name": "review", "arguments": args})).encode(); bx = {}
        out = b"".join(srv3({"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
                             "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL, "HTTP_MCP_METHOD": "prompts/get",
                             "HTTP_MCP_NAME": "review"}, lambda st, h: bx.update(s=st)))
        return int(bx["s"].split()[0]), json.loads(out)
    s, r = prpc({"street": "W", "year": "2024"})
    check("prompt int arg coerced, principal injected", r["result"]["messages"][0]["content"]["text"],
          "W/2024/int/{'sub': 'p'}")
    check("prompt bad int -> -32602", err(prpc({"street": "W", "year": "abc"})), (400, -32602))
    check("prompt cannot supply principal", err(prpc({"street": "W", "who": "spoof"})), (400, -32602))
    check("prompt non-string arg -> -32602", err(prpc({"street": 5})), (400, -32602))

    print("— M6 keepalive / M5 duplicate headers / M7 Django (over the wire) —")
    quiet = MCP("quiet")
    @quiet.tool
    async def silent(ctx: Context = None) -> dict:
        await asyncio.sleep(1.0)
        return {"ok": True}
    qapp = ASGIServer(quiet, keepalive=0.3)

    async def wire():
        port = free_port()
        srv = serve_uvicorn(qapp, port)
        url = f"http://127.0.0.1:{port}/mcp"
        async with httpx.AsyncClient(timeout=30) as c:
            lines = []
            async with c.stream("POST", url, json=body("tools/call", {"name": "silent", "arguments": {}}),
                                headers=HDRS("silent")) as r:
                ka = r.headers.get("mcp-protocol-version")
                async for line in r.aiter_lines():
                    lines.append(line)
            check("keepalive comments during silence", sum(1 for l in lines if l.startswith(": keepalive")) >= 2, True)
            check("...then the result", any('"result"' in l for l in lines), True)
            check("SSE response carries MCP-Protocol-Version", ka, PROTOCOL)
            # duplicate header on ASGI: httpx sends a list of pairs verbatim
            r = await c.post(url, json=body("tools/list"), headers=[
                ("MCP-Protocol-Version", PROTOCOL), ("Mcp-Method", "prompts/list"), ("Mcp-Method", "tools/list")])
            check("ASGI duplicate Mcp-Method -> 400/-32020", (r.status_code, r.json()["error"]["code"]), (400, -32020))
        srv.should_exit = True
    asyncio.run(wire())

    try:
        import django
        from django.conf import settings
    except ImportError:
        print("  skip  (django not installed)")
        return
    if not settings.configured:
        settings.configure(DEBUG=False, SECRET_KEY="x", ALLOWED_HOSTS=["*"], ROOT_URLCONF=__name__,
                           MIDDLEWARE=["django.middleware.csrf.CsrfViewMiddleware"])
        django.setup()
    from django.urls import path
    from django.test import AsyncClient
    from micromcp import django_async_view
    global urlpatterns
    urlpatterns = [path("mcp", django_async_view(asgi_app))]

    async def dj():
        client = AsyncClient()
        h = {k: v for k, v in HDRS("crunch").items() if k != "Content-Type"}
        resp = await client.post("/mcp", data=json.dumps(body("tools/call", {"name": "crunch", "arguments": {}}, token="p")),
                                 content_type="application/json", headers=h)
        check("django_async_view streams SSE", (resp.status_code, resp["Content-Type"]), (200, "text/event-stream"))
        frames = []
        async for chunk in resp.streaming_content:
            for line in chunk.decode().splitlines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[6:]))
        check("...3 progress + result", (len([f for f in frames if "method" in f]), frames[-1]["result"]["structuredContent"]),
              (3, {"processed": 3}))
        h2 = {**h, "Mcp-Name": "add"}
        resp = await client.post("/mcp", data=json.dumps(body("tools/call", {"name": "add", "arguments": {"a": 1, "b": 2}})),
                                 content_type="application/json", headers=h2)
        check("django_async_view plain JSON", (resp.status_code, json.loads(resp.content)["result"]["structuredContent"]),
              (200, {"sum": 3}))
    asyncio.run(dj())


# ═══════════════════════════════════════════════════════════════════════════
# Round-two findings (E1-E17 protocol/security, F1-F14 runtime/API).
# ═══════════════════════════════════════════════════════════════════════════
import functools, inspect, logging, pathlib

_ACL_SRC = """
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from myapp.acl import Principal
def grant(role: str, principal: Principal) -> dict: return {}
def lst(items: list[Principal]) -> dict: return {}
"""


def round2():
    global OK, FAIL
    print("\n— E1/E10 class-based and partial handlers —")
    m = MCP("r2")
    class Audit:
        who: str = "class attr"
        def __call__(self, street: str, who: Principal) -> dict:
            return {"who": who}
    m.tool(name="audit")(Audit())
    check("callable instance: Principal detected from __call__",
          (list(m.tools["audit"]["inputSchema"]["properties"]), m.tools["audit"]["_principal"]), (["street"], "who"))
    def raw_handler(street: str, db: str = "prod", who: Principal = None) -> dict:
        return {"db": db, "who": who}
    m.tool(name="bound")(functools.partial(raw_handler, db="prod-conn"))
    check("partial: bound keyword hidden, Principal detected",
          (list(m.tools["bound"]["inputSchema"]["properties"]), m.tools["bound"]["_principal"]), (["street"], "who"))
    s2 = Server(m, authenticate=lambda h: {"sub": "p"})
    def call(srv, tool, args):
        raw = json.dumps(body("tools/call", {"name": tool, "arguments": args})).encode(); bx = {}
        out = b"".join(srv({"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
                            "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL, "HTTP_MCP_METHOD": "tools/call",
                            "HTTP_MCP_NAME": tool}, lambda st, h: bx.update(s=st)))
        return int(bx["s"].split()[0]), json.loads(out)
    check("partial: client cannot override bound db", err(call(s2, "bound", {"street": "x", "db": "../etc"})), (400, -32602))
    check("partial: handler sees bound value + principal",
          call(s2, "bound", {"street": "x"})[1]["result"]["structuredContent"], {"db": "prod-conn", "who": {"sub": "p"}})

    print("— E2 adjacent placeholders —")
    try:
        m.resource("k://{a}{b}{c}END")(lambda a="", b="", c="": "")
        got = "accepted"
    except ValueError:
        got = "ValueError"
    check("adjacent placeholders rejected at registration", got, "ValueError")
    @m.resource("k://{a}-{b}-{c}-{d}-{e}END")
    def kk(a: str, b: str, c: str, d: str, e: str) -> str: return ""
    t0 = time.time(); m.match_resource("k://" + "x" * 2000); dt = time.time() - t0
    check(f"5-placeholder template, 2000-byte miss is linear ({dt*1000:.1f} ms)", dt < 0.05, True)

    print("— E3 arguments are deserialized to declared types —")
    class Color(enum.Enum):
        RED = "red"; BLUE = "blue"
    @dataclasses.dataclass
    class Inner:
        n: int
    @dataclasses.dataclass
    class Outer:
        inner: Inner
        label: str = "x"
    @m.tool
    def rich(d: datetime.date, c: Color, tags: set[str], pair: tuple[int, str], p: pathlib.Path,
             o: Outer, maybe: datetime.date | None = None) -> dict:
        return {"d": d.year, "c": c.name, "tags": sorted(tags & {"a"}), "pair": pair[1], "p": p.name,
                "o": o.inner.n, "maybe": maybe}
    s, r = call(s2, "rich", {"d": "2020-05-06", "c": "red", "tags": ["a", "b"], "pair": [1, "x"],
                             "p": "/tmp/f.txt", "o": {"inner": {"n": 7}}})
    check("date/enum/set/tuple/path/dataclass arrive typed", r["result"].get("structuredContent"),
          {"d": 2020, "c": "RED", "tags": ["a"], "pair": "x", "p": "f.txt", "o": 7, "maybe": None})
    check("bad ISO date -> -32602", err(call(s2, "rich", {"d": "2020-13-99", "c": "red", "tags": [], "pair": [1, "x"],
                                                        "p": "x", "o": {"inner": {"n": 1}}})), (400, -32602))
    check("nested dataclass rejects unknown keys (E17)",
          err(call(s2, "rich", {"d": "2020-01-01", "c": "red", "tags": [], "pair": [1, "x"], "p": "x",
                                "o": {"inner": {"n": 1, "zzz": 1}}})), (400, -32602))
    try:
        import pydantic
        class PInner(pydantic.BaseModel):
            n: int
        class POuter(pydantic.BaseModel):
            inner: PInner
        @m.tool
        def pyd(o: POuter) -> dict:
            return {"n": o.inner.n, "kind": type(o).__name__}
        sch = m.tools["pyd"]["inputSchema"]
        check("pydantic $defs hoisted to root", ("$defs" in sch, "$defs" in sch["properties"]["o"]), (True, False))
        check("pydantic model constructed for the handler", call(s2, "pyd", {"o": {"inner": {"n": 3}}})[1]["result"]["structuredContent"],
              {"n": 3, "kind": "POuter"})
        check("pydantic nested validated via $ref", err(call(s2, "pyd", {"o": {"inner": "bogus"}})), (400, -32602))
        check("pydantic model rejects unknown keys", err(call(s2, "pyd", {"o": {"inner": {"n": 1}, "zzz": 1}})), (400, -32602))
    except ImportError:
        print("  skip  (pydantic not installed)")

    print("— E4 marker-looking annotations that do not resolve —")
    mod = types.ModuleType("acl_tools"); sys.modules["acl_tools"] = mod
    exec(compile(_ACL_SRC, "<acl_tools>", "exec"), mod.__dict__)
    try:
        m.tool(mod.grant); got = "registered"
    except TypeError:
        got = "TypeError"
    check("foreign 'Principal' string annotation refused at registration", got, "TypeError")
    m.tool(mod.lst)
    check("list[Principal] string is an ordinary (schemaless) param", list(m.tools["lst"]["inputSchema"]["properties"]), ["items"])

    print("— E5/E6/E9 routing headers —")
    @m.resource("data:text/plain,hi")
    def comma() -> str: return "hi"
    def read(srv, uri, hdr, extra=None):
        raw = json.dumps(body("resources/read", {"uri": uri})).encode(); bx = {}
        env = {"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
               "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL, "HTTP_MCP_METHOD": "resources/read", "HTTP_MCP_NAME": hdr}
        env.update(extra or {})
        out = b"".join(srv(env, lambda st, h: bx.update(s=st)))
        return int(bx["s"].split()[0]), json.loads(out)
    check("comma-bearing URI readable verbatim", read(s2, "data:text/plain,hi", "data:text/plain,hi")[0], 200)
    check("Mcp-Method is compared verbatim (no base64)",
          err(read(s2, "data:text/plain,hi", "data:text/plain,hi", {"HTTP_MCP_METHOD": "=?base64?cmVzb3VyY2VzL3JlYWQ=?="})),
          (400, -32020))
    check("non-canonical base64 sentinel -> None", micromcp._decode_hdr("=?base64?cm1=?="), None)
    check("empty sentinel is not a sentinel", micromcp._decode_hdr("=?base64?="), "=?base64?=")
    check("canonical sentinel decodes", micromcp._decode_hdr("=?base64?aGk=?="), "hi")

    print("— E7 notifications still pass transport checks —")
    raw = json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}).encode(); bx = {}
    b"".join(wsgi_app({"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
                       "HTTP_ORIGIN": "https://evil.example"}, lambda st, h: bx.update(s=st)))
    check("notification from bad Origin -> 403", bx["s"], "403 Forbidden")
    bx = {}
    b"".join(wsgi_app({"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
                       "CONTENT_TYPE": "text/plain"}, lambda st, h: bx.update(s=st)))
    check("notification with text/plain -> 400", bx["s"], "400 Bad Request")

    print("— E11 docstrings —")
    def f1(a: int) -> dict:
        """Do a thing.

        Args:
            a: the first
        Returns:
            dict: the answer
        """
    def f4(a: int, b: str) -> dict:
        """Numpy.

        Parameters
        ----------
        a : int
            The first one.
        b : str
            The second one.

        Returns
        -------
        dict
        """
    def g(a: int) -> dict:
        """Example.

        Usage::

            Args:
                a: inside a code block, leave alone

        Args:
            a: the real one
        """
    check("Returns: kept when it follows Args: directly", micromcp._parse_doc(inspect.getdoc(f1)),
          ("Do a thing.\n\nReturns:\n    dict: the answer", {"a": "the first"}))
    check("NumPy types are not shipped as descriptions", micromcp._parse_doc(inspect.getdoc(f4))[1],
          {"a": "The first one.", "b": "The second one."})
    check("NumPy Returns kept", "Returns" in micromcp._parse_doc(inspect.getdoc(f4))[0], True)
    check("indented Args: inside an example is left alone", micromcp._parse_doc(inspect.getdoc(g)),
          ("Example.\n\nUsage::\n\n    Args:\n        a: inside a code block, leave alone", {"a": "the real one"}))

    print("— E12/E13 strictness —")
    check("True does not satisfy Literal[1,2,3]", micromcp._check(True, {"enum": [1, 2, 3]}), "value must be one of [1, 2, 3]")
    check("1.0 does not satisfy Literal[1]", micromcp._check(1.0, {"enum": [1]}), "value must be one of [1]")
    for bad in ("nan", "inf", "1e400", "1_0", "١٢", " 3.5 "):
        try:
            micromcp._coerce(bad, float); got = "accepted"
        except ValueError:
            got = "ValueError"
        check(f"float coercion rejects {bad!r}", got, "ValueError")
    check("float coercion accepts 3.5e2", micromcp._coerce("3.5e2", float), 350.0)

    print("— E14/E15/E16/F7 transport details —")
    pathed = Server(mcp, path="/mcp")
    check("//mcp normalized", pathed.path_ok("//mcp"), True)
    check("/mcp//x rejected", pathed.path_ok("/mcp//x"), False)
    raw = json.dumps(body("tools/list")).encode(); bx = {}
    b"".join(pathed({"REQUEST_METHOD": "POST", "SCRIPT_NAME": "/mcp", "PATH_INFO": "", "CONTENT_LENGTH": str(len(raw)),
                     "wsgi.input": io.BytesIO(raw), "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL, "HTTP_MCP_METHOD": "tools/list"},
                    lambda st, h: bx.update(s=st)))
    check("path=/mcp works when mounted (SCRIPT_NAME)", bx["s"], "200 OK")
    bx = {}
    b"".join(wsgi_app({"REQUEST_METHOD": "OPTIONS", "wsgi.input": io.BytesIO(b""), "HTTP_ORIGIN": "https://ok.example",
                       "HTTP_ACCESS_CONTROL_REQUEST_HEADERS": "cookie, x-admin"}, lambda st, h: bx.update(s=st, h=dict(h))))
    check("preflight does not echo requested headers", bx["h"]["Access-Control-Allow-Headers"], micromcp.CORS_HEADERS)
    bx = {}
    b"".join(wsgi_app({"REQUEST_METHOD": "OPTIONS", "wsgi.input": io.BytesIO(b""), "HTTP_ORIGIN": "https://evil.example"},
                      lambda st, h: bx.update(s=st, h=dict(h))))
    check("Vary: Origin on the 403 too", bx["h"].get("Vary"), "Origin")
    bx = {}
    b"".join(wsgi_app({"REQUEST_METHOD": "POST", "wsgi.input": io.BytesIO(raw), "wsgi.input_terminated": True,
                       "HTTP_TRANSFER_ENCODING": "chunked", "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL,
                       "HTTP_MCP_METHOD": "tools/list"}, lambda st, h: bx.update(s=st)))
    check("de-chunked body (gunicorn) is read, not 411", bx["s"], "200 OK")

    print("— F6 Principal/Context in resources; F10/F14 result shapes —")
    @m.resource("who://me")
    def whoami_res(who: Principal) -> str: return json.dumps(who)
    @m.resource("crash://{cid}")
    def crash_res(cid: int, who: Principal) -> str: return f"{cid}/{who['sub']}"
    check("Principal injected into a resource", json.loads(read(s2, "who://me", "who://me")[1]["result"]["contents"][0]["text"]), {"sub": "p"})
    check("Principal injected into a template resource", read(s2, "crash://7", "crash://7")[1]["result"]["contents"][0]["text"], "7/p")
    @dataclasses.dataclass
    class Box:
        n: int
    @m.tool
    def boxes() -> list:
        return [Box(1), Box(2)]
    @m.tool
    def gen() -> dict:
        yield {"a": 1}
    @m.tool
    async def agen() -> dict:
        yield 1; yield 2
    check("list[dataclass] serializes", json.loads(call(s2, "boxes", {})[1]["result"]["content"][0]["text"]), [{"n": 1}, {"n": 2}])
    check("generator handler is collected", json.loads(call(s2, "gen", {})[1]["result"]["content"][0]["text"]), [{"a": 1}])
    check("async generator handler is collected", json.loads(call(s2, "agen", {})[1]["result"]["content"][0]["text"]), [1, 2])

    print("— F8 wraps-less decorator —")
    def nowrap(f):
        def inner(*a, **k): return f(*a, **k)
        return inner
    try:
        m.tool(name="nw")(nowrap(lambda table: {}))
        got = "registered"
    except TypeError:
        got = "TypeError"
    check("*args/**kwargs-only handler refused", got, "TypeError")

    print("— F1/F2/F4 pool sizing, timeout, authenticate off-loop (over the wire) —")
    slow_auth = MCP("slowauth")
    @slow_auth.tool
    def nap() -> dict:
        time.sleep(0.3); return {"ok": True}
    @slow_auth.tool
    def fast() -> dict:
        return {"ok": True}
    @slow_auth.tool
    def long_nap() -> dict:
        time.sleep(3); return {"ok": True}      # far past the 0.2s timeout below: no race
    def auth_sleep(h):
        time.sleep(0.2); return {"sub": "x"}
    big_pool = ASGIServer(slow_auth, workers=64, authenticate=auth_sleep)

    async def wire():
        port = free_port()
        srv = serve_uvicorn(big_pool, port)
        url = f"http://127.0.0.1:{port}/mcp"
        async with httpx.AsyncClient(timeout=60) as c:
            t0 = time.time()
            await asyncio.gather(*[c.post(url, json=body("tools/call", {"name": "fast", "arguments": {}}),
                                          headers=HDRS("fast")) for _ in range(20)])
            dt = time.time() - t0
            check(f"20 concurrent calls with a 0.2s sync authenticate overlap ({dt:.2f}s)", dt < 1.0, True)
            t0 = time.time()
            await asyncio.gather(*[c.post(url, json=body("tools/call", {"name": "nap", "arguments": {}}),
                                          headers=HDRS("nap")) for _ in range(64)])
            dt = time.time() - t0
            check(f"workers=64: 64 concurrent 0.3s sync calls overlap ({dt:.2f}s; serial 19.2s)", dt < 5.0, True)
        # httpx refuses to under-send a declared Content-Length, so go raw.
        def raw_cl():
            sk = socket.create_connection(("127.0.0.1", port), 5)
            sk.sendall(b"POST /mcp HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                       b"Content-Length: 999999999\r\n\r\n" + b"x" * 10)
            sk.settimeout(5)
            try:
                head = sk.recv(200)
            except OSError:
                head = b""
            sk.close()
            return head.split(b"\r\n")[0]
        check("ASGI declared Content-Length over max_body -> 413 before reading",
              (await asyncio.to_thread(raw_cl))[:12], b"HTTP/1.1 413")
        srv.should_exit = True
    asyncio.run(wire())

    timed = Server(slow_auth, timeout=0.2)
    t0 = time.time()
    s, r = call(timed, "long_nap", {})
    check(f"WSGI timeout fails loudly with 500 ({time.time()-t0:.2f}s)",
          (s, r.get("error", {}).get("code", "no error")), (500, -32603))

    print("— F3 abandoned sync handler is logged; ctx.cancelled visible —")
    seen = []
    class Capture(logging.Handler):
        def emit(self, record): seen.append(record.getMessage())
    micromcp.log.addHandler(Capture())
    flag = {}
    stuck = MCP("stuck")
    @stuck.tool
    def grind(ctx: Context = None) -> dict:
        t_end = time.time() + 5
        while time.time() < t_end and not ctx.cancelled:
            time.sleep(0.05)
        flag["cancelled"] = ctx.cancelled
        return {"ok": True}
    sapp = ASGIServer(stuck)

    async def hang():
        port = free_port()
        srv = serve_uvicorn(sapp, port)
        async with httpx.AsyncClient(timeout=30) as c:
            try:
                async with c.stream("POST", f"http://127.0.0.1:{port}/mcp",
                                    json=body("tools/call", {"name": "grind", "arguments": {}}, token="t"),
                                    headers=HDRS("grind")):
                    await asyncio.sleep(0.3)
            except Exception:
                pass
        for _ in range(60):
            if "cancelled" in flag:
                break
            await asyncio.sleep(0.05)
        srv.should_exit = True
    asyncio.run(hang())
    check("sync handler saw ctx.cancelled and stopped early", flag.get("cancelled"), True)
    check("abandonment was logged", any("abandoned on cancellation" in x for x in seen), True)


# ═══════════════════════════════════════════════════════════════════════════
# Round-three findings (G1-G14 protocol/security, H1-H7 runtime, defender).
# ═══════════════════════════════════════════════════════════════════════════
_TC_SRC = """
from __future__ import annotations
import typing as t
from typing import TYPE_CHECKING, Annotated, Union
if TYPE_CHECKING:
    from micromcp import Principal, Context
def c_alias(role: str, who: t.Optional[Principal]) -> dict: return {"who": who}
def d_union(role: str, who: Union[Principal, None] = None) -> dict: return {"who": who}
def e_annot(role: str, who: Annotated[Principal, "inject"]) -> dict: return {"who": who}
def f_ctx(role: str, c: Annotated[Context, "x"] = None) -> dict: return {}
"""


def round3():
    global OK, FAIL
    import pydantic
    m = MCP("r3")
    s3 = Server(m, authenticate=lambda h: {"sub": "p"} if h.get("authorization") == "Bearer good" else None)
    def call(srv, tool, args, auth=None):
        raw = json.dumps(body("tools/call", {"name": tool, "arguments": args})).encode(); bx = {}
        env = {"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
               "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL, "HTTP_MCP_METHOD": "tools/call", "HTTP_MCP_NAME": tool}
        if auth: env["HTTP_AUTHORIZATION"] = auth
        out = b"".join(srv(env, lambda st, h: bx.update(s=st)))
        return int(bx["s"].split()[0]), json.loads(out)
    def code(resp):
        return resp[0], resp[1].get("error", {}).get("code", "no error")

    print("\n— G1 every marker spelling is injected or refused, never exposed —")
    mod = types.ModuleType("tc3"); sys.modules["tc3"] = mod
    exec(compile(_TC_SRC, "<tc3>", "exec"), mod.__dict__)
    for fn in (mod.c_alias, mod.d_union, mod.e_annot, mod.f_ctx):
        try:
            m.tool(fn); got = f"exposed {list(m.tools[fn.__name__]['inputSchema']['properties'])}"
        except TypeError:
            got = "TypeError"
        check(f"{fn.__name__}: TYPE_CHECKING-only wrapped marker -> TypeError", got, "TypeError")
    src2 = "import typing as t\nfrom typing import Annotated\nfrom micromcp import Principal\n" \
           "def e2(role: str, who: Annotated[Principal, 'x'], r: 'Row' = None) -> dict: return {'who': who}\n"
    mod2 = types.ModuleType("tc3b"); sys.modules["tc3b"] = mod2
    exec(compile("from __future__ import annotations\n" + src2, "<tc3b>", "exec"), mod2.__dict__)
    m.tool(mod2.e2)
    check("Annotated[Principal] via the eval fallback is injected", (list(m.tools["e2"]["inputSchema"]["properties"]), m.tools["e2"]["_principal"]), (["role", "r"], "who"))
    if sys.version_info >= (3, 12):
        ns = {}
        exec("type Whoever = Principal", {"Principal": Principal}, ns)
        def aliased(role: str, who: object) -> dict: return {"who": who}
        aliased.__annotations__ = {"role": str, "who": ns["Whoever"], "return": dict}
        m.tool(aliased)
        check("PEP 695 alias of Principal is injected", (list(m.tools["aliased"]["inputSchema"]["properties"]), m.tools["aliased"]["_principal"]), (["role"], "who"))
    else:
        print("  skip  PEP 695 alias (needs Python 3.12)")
    class Admin(Principal): pass
    def sub(role: str, who: Admin) -> dict: return {}
    try:
        m.tool(sub); got = "registered"
    except TypeError:
        got = "TypeError"
    check("subclass of Principal refused", got, "TypeError")

    print("— G3/G4 $defs namespacing and output schemas —")
    class InnerA(pydantic.BaseModel):
        n: int
    class InnerB(pydantic.BaseModel):
        label: str
    InnerA.__name__ = InnerB.__name__ = "Inner"
    class A(pydantic.BaseModel):
        inner: InnerA
    class B(pydantic.BaseModel):
        inner: InnerB
    @m.tool
    def two(a: A, b: B) -> dict: return {"ok": 1}
    sch = m.tools["two"]["inputSchema"]
    check("$defs are namespaced per parameter", all(k.startswith(("a__", "b__")) for k in sch["$defs"]), True)
    check("both models validate against their own defs", call(s3, "two", {"a": {"inner": {"n": 1}}, "b": {"inner": {"label": "x"}}})[0], 200)
    check("cross-model shape rejected", code(call(s3, "two", {"a": {"inner": {"label": "pwn"}}, "b": {"inner": {"label": "x"}}})), (400, -32602))
    class Deep(pydantic.BaseModel):
        z: int
    class Mid(pydantic.BaseModel):
        deep: Deep
    @dataclasses.dataclass
    class Top:
        inner: Mid
    @m.tool
    def out() -> Top: return {"inner": {"deep": {"z": "NOT-AN-INT"}}}  # type: ignore
    check("outputSchema $defs hoisted to root", "$defs" in m.tools["out"]["outputSchema"] and '"$defs":' not in json.dumps(m.tools["out"]["outputSchema"]["properties"]), True)
    check("nested garbage caught by outputSchema", call(s3, "out", {})[1]["result"].get("isError"), True)
    check("unresolvable $ref is a problem", micromcp._check({}, {"$ref": "#/definitions/X"}) is None, False)

    print("— G5 constructor/validator text stays server-side —")
    class Secretive(pydantic.BaseModel):
        v: int
        @pydantic.field_validator("v")
        @classmethod
        def chk(cls, v):
            raise ValueError("postgres://svc:S3cr3t@db/prod unreachable")
    @m.tool
    def leak(s: Secretive) -> dict: return {}
    s, r = call(s3, "leak", {"s": {"v": 1}})
    check("-32602 without the validator text", (s, r["error"]["code"], "S3cr3t" in r["error"]["message"]), (400, -32602, False))

    print("— G6/G7 constraints and depth —")
    class Strict(pydantic.BaseModel):
        n: int = pydantic.Field(ge=1, le=10, multiple_of=2)
        s: str = pydantic.Field(min_length=3, pattern="^z")
        k: typing.Literal["a"] = "a"
    @m.tool
    def strict(x: Strict) -> dict: return {"ok": 1}
    for bad, label in (({"n": 11, "s": "zzz"}, "maximum"), ({"n": 3, "s": "zzz"}, "multipleOf"), ({"n": 2, "s": "zz"}, "minLength"),
                       ({"n": 2, "s": "abc"}, "pattern"), ({"n": 2, "s": "zzz", "k": "b"}, "const")):
        check(f"{label} enforced by _check", code(call(s3, "strict", {"x": bad})), (400, -32602))
    check("valid strict payload accepted", call(s3, "strict", {"x": {"n": 2, "s": "zzz"}})[0], 200)
    @m.tool
    def dated(d: datetime.date) -> dict: return {"y": d.year}
    check("format: date enforced by _check", "valid date" in call(s3, "dated", {"d": "banana"})[1]["error"]["message"], True)
    class Node(pydantic.BaseModel):
        kids: list["Node"] = []
    @m.tool
    def rec(n: Node) -> dict: return {}
    d = {"kids": []}
    for _ in range(400): d = {"kids": [d]}
    check("depth-400 payload -> -32602, not 500", code(call(s3, "rec", {"n": d})), (400, -32602))

    print("— G8/G10/G11 Literal[Enum], closed union probe, init=False —")
    class Mode(str, enum.Enum):
        FAST = "fast"; SLOW = "slow"
    @m.tool
    def lit(mo: typing.Literal[Mode.FAST, Mode.SLOW]) -> dict: return {"mo": mo.name if isinstance(mo, enum.Enum) else mo}
    check("Literal[Enum] schema uses values", m.tools["lit"]["inputSchema"]["properties"]["mo"], {"enum": ["fast", "slow"], "type": "string"})
    check("Literal[Enum] value maps back to the member", call(s3, "lit", {"mo": "fast"})[1]["result"]["structuredContent"], {"mo": "FAST"})
    @dataclasses.dataclass
    class Small:
        n: int
    @dataclasses.dataclass
    class Big:
        n: int
        extra: str = "d"
    @m.tool
    def un(v: Small | Big) -> dict: return {"got": type(v).__name__}
    check("union probe uses closed schemas", call(s3, "un", {"v": {"n": 1, "extra": "boom"}})[1]["result"]["structuredContent"], {"got": "Big"})
    @dataclasses.dataclass
    class Rec:
        a: int
        b: int = dataclasses.field(init=False, default=0)
    @m.tool
    def recd(r: Rec) -> dict: return {"a": r.a}
    check("init=False field not advertised", list(m.tools["recd"]["inputSchema"]["properties"]["r"]["properties"]), ["a"])

    print("— G12/G13/G14 OPTIONS host, host case, docstring prose —")
    hosted = Server(m, allowed_hosts={"mcp.example"})
    bx = {}
    b"".join(hosted({"REQUEST_METHOD": "OPTIONS", "wsgi.input": io.BytesIO(b""), "HTTP_HOST": "evil.example"}, lambda st, h: bx.update(s=st)))
    check("OPTIONS with a rebound Host -> 403", bx["s"], "403 Forbidden")
    bx = {}
    raw = json.dumps(body("tools/list")).encode()
    b"".join(hosted({"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw), "HTTP_HOST": "MCP.Example",
                     "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL, "HTTP_MCP_METHOD": "tools/list"}, lambda st, h: bx.update(s=st)))
    check("Host compared case-insensitively", bx["s"], "200 OK")
    def prose(a: int, b: str) -> dict:
        """Do.

        Args:
            a: alpha
            note that b is special
            b: bravo
        """
    check("prose line inside Args attaches to the parameter above", micromcp._parse_doc(inspect.getdoc(prose)),
          ("Do.", {"a": "alpha note that b is special", "b": "bravo"}))

    print("— H1/H2/H5 pools: auth lane, 503 when full, daemon threads, fork-aware —")
    sm = MCP("pools")
    @sm.tool
    def block() -> dict:
        time.sleep(1.5); return {"ok": True}
    @sm.tool
    def quick() -> dict:
        return {"ok": True}
    papp = ASGIServer(sm, workers=2, authenticate=lambda h: {"sub": "x"})
    async def pools():
        port = free_port()
        srv = serve_uvicorn(papp, port)
        url = f"http://127.0.0.1:{port}/mcp"
        async with httpx.AsyncClient(timeout=10) as c:
            blockers = [asyncio.create_task(c.post(url, json=body("tools/call", {"name": "block", "arguments": {}}), headers=HDRS("block"))) for _ in range(2)]
            await asyncio.sleep(0.3)
            t0 = time.time()
            r = await c.post(url, json=body("tools/list"), headers={**HDRS("x"), "Mcp-Method": "tools/list"})
            check(f"tools/list answers while the handler pool is full ({time.time()-t0:.2f}s)", (r.status_code, "tools" in r.json()["result"]), (200, True))
            r = await c.post(url, json=body("tools/call", {"name": "quick", "arguments": {}}), headers=HDRS("quick"))
            check("sync call against a full pool -> 503, not a queue", (r.status_code, r.json()["error"]["message"]), (503, "server busy"))
            await asyncio.gather(*blockers)
            r = await c.post(url, json=body("tools/call", {"name": "quick", "arguments": {}}), headers=HDRS("quick"))
            check("pool recovers once the blockers return", r.status_code, 200)
        srv.should_exit = True
    asyncio.run(pools())
    check("pool threads are daemons", all(t.daemon for t in threading.enumerate() if t.name.startswith("micromcp-")), True)
    fsrv = Server(sm, workers=2, timeout=3, authenticate=lambda h: {"sub": "x"})
    check("parent warmup on the pool", call(fsrv, "quick", {})[0], 200)
    rd, wr = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(rd)
        try:
            os.write(wr, json.dumps(call(fsrv, "quick", {})[0]).encode())
        except BaseException as e:
            os.write(wr, json.dumps(f"exc {e}").encode())
        os._exit(0)
    os.close(wr)
    got = read_child(rd, pid, 10)
    check("forked child serves on a rebuilt pool", got, 200)

    print("— H3/H6/H7 stream budget, registry snapshot, timeout=0, loop rebuild —")
    bm = MCP("budget")
    @bm.tool
    async def chatty(ctx: Context = None) -> dict:
        for i in range(10_000):
            await ctx.info("x" * 100)
        return {"done": True}
    bapp = ASGIServer(bm, stream_budget=50_000)
    async def budget():
        port = free_port()
        srv = serve_uvicorn(bapp, port)
        frames = []
        async with httpx.AsyncClient(timeout=30) as c:
            async with c.stream("POST", f"http://127.0.0.1:{port}/mcp", json=body("tools/call", {"name": "chatty", "arguments": {}}, token="t"),
                                headers=HDRS("chatty")) as r:
                async for line in r.aiter_lines():
                    if line.startswith("data: "):
                        frames.append(json.loads(line[6:]))
        srv.should_exit = True
        return frames
    fr = asyncio.run(budget())
    check("stream budget ends the call with an in-band error", (len(fr) < 1000, fr[-1].get("result", {}).get("isError")), (True, True))
    check("timeout=0 means no cap", Server(bm, timeout=0).timeout, None)
    lp = micromcp._Loop()
    lp.run(asyncio.sleep(0))
    lp._loop.call_soon_threadsafe(lp._loop.stop); time.sleep(0.2)
    check("a stopped loop is rebuilt", lp.run(asyncio.sleep(0), 2), None)


# ═══════════════════════════════════════════════════════════════════════════
# UI apps (MCP-UI / MCP Apps): tool/resource/prompt meta, content pass-through,
# result _meta. Every emitted shape is validated against mcp-types.
# ═══════════════════════════════════════════════════════════════════════════
def apps():
    global OK, FAIL
    from micromcp import embedded_resource, META_SERVER
    try:
        from mcp_types.methods import validate_server_result
    except ImportError:
        validate_server_result = None
    HTML = "<html><body><h1>widget</h1></body></html>"
    ma = MCP("ui-demo", "0.1.0")
    @ma.resource("ui://widget", mime_type="text/html;profile=mcp-app", meta={"ui": {"csp": {"resourceDomains": []}}})
    def widget() -> str: return HTML
    @ma.resource("ui://chart/{cid}", mime_type="text/html", meta={"kind": "chart"})
    def chart(cid: int) -> str: return f"<svg id='{cid}'/>"
    @ma.tool(meta={"ui": {"resourceUri": "ui://widget"}})
    def show() -> dict:
        """Render the widget."""
        return {"content": [{"type": "text", "text": "rendering"},
                            embedded_resource("ui://widget", text=HTML, mime_type="text/html;profile=mcp-app",
                                              meta={"ui": {"prefersBorder": True}})],
                "structuredContent": {"shown": True}, "_meta": {"ui": {"height": 300}}}
    @ma.tool
    def blobby() -> dict:
        return {"content": [embedded_resource("ui://img", blob=b"\x89PNG", mime_type="image/png")]}
    @ma.tool
    def plain_dict_with_content_key() -> dict:
        return {"content": "not a list of blocks", "other": 1}       # data, not a result
    @ma.tool
    def failing() -> dict:
        return {"content": [{"type": "text", "text": "nope"}], "isError": True}
    @ma.tool(output_schema={"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]})
    def declared_no_structured() -> dict:
        return {"content": [{"type": "text", "text": "x"}]}
    @ma.prompt(meta={"ui": {"kind": "wizard"}})
    def wiz(topic: str) -> str: return topic
    srv = Server(ma)
    def rpc(method, params=None):
        params = dict(params or {}); params["_meta"] = {META_VER: PROTOCOL, META_CAPS: {}}
        raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        env = {"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
               "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL, "HTTP_MCP_METHOD": method}
        nm = params.get("uri") if method == "resources/read" else params.get("name")
        if nm: env["HTTP_MCP_NAME"] = nm
        bx = {}; out = b"".join(srv(env, lambda s, h: bx.update(s=s)))
        r = json.loads(out)
        if validate_server_result and "result" in r:
            try:
                validate_server_result(method, PROTOCOL, r["result"])
            except Exception as e:
                check(f"mcp-types conformance for {method}", str(e)[:120], "conforms")
        return r
    print("\n— UI apps: meta on tools/resources/prompts —")
    t = rpc("tools/list")["result"]["tools"]
    show_t = next(x for x in t if x["name"] == "show")
    check("tool meta published as _meta", show_t.get("_meta"), {"ui": {"resourceUri": "ui://widget"}})
    check("no other underscore keys leak", [k for k in show_t if k.startswith("_")], ["_meta"])
    check("tools without meta have no _meta", "_meta" in next(x for x in t if x["name"] == "blobby"), False)
    check("resource meta in resources/list", rpc("resources/list")["result"]["resources"][0].get("_meta"), {"ui": {"csp": {"resourceDomains": []}}})
    check("template meta in templates/list", rpc("resources/templates/list")["result"]["resourceTemplates"][0].get("_meta"), {"kind": "chart"})
    rd = rpc("resources/read", {"uri": "ui://widget"})["result"]["contents"][0]
    check("resource read carries mimeType + _meta", (rd["mimeType"], rd.get("_meta")), ("text/html;profile=mcp-app", {"ui": {"csp": {"resourceDomains": []}}}))
    check("template read carries meta", rpc("resources/read", {"uri": "ui://chart/7"})["result"]["contents"][0].get("_meta"), {"kind": "chart"})
    check("prompt meta in prompts/list", rpc("prompts/list")["result"]["prompts"][0].get("_meta"), {"ui": {"kind": "wizard"}})

    print("— UI apps: content pass-through and result _meta —")
    c = rpc("tools/call", {"name": "show", "arguments": {}})["result"]
    check("content blocks passed through", [b["type"] for b in c["content"]], ["text", "resource"])
    check("embedded resource shape", c["content"][1]["resource"],
          {"uri": "ui://widget", "mimeType": "text/html;profile=mcp-app", "text": HTML, "_meta": {"ui": {"prefersBorder": True}}})
    check("structuredContent kept", c.get("structuredContent"), {"shown": True})
    check("result _meta merged with serverInfo", c["_meta"], {"ui": {"height": 300}, META_SERVER: {"name": "ui-demo", "version": "0.1.0"}})
    check("blob embedded resource is base64", rpc("tools/call", {"name": "blobby", "arguments": {}})["result"]["content"][0]["resource"].get("blob"), "iVBORw==")
    d = rpc("tools/call", {"name": "plain_dict_with_content_key", "arguments": {}})["result"]
    check("a dict whose content is not blocks is data", (d["content"][0]["type"], d.get("structuredContent")),
          ("text", {"content": "not a list of blocks", "other": 1}))
    check("isError passes through", rpc("tools/call", {"name": "failing", "arguments": {}})["result"].get("isError"), True)
    check("declared outputSchema still requires structuredContent",
          rpc("tools/call", {"name": "declared_no_structured", "arguments": {}})["result"].get("isError"), True)


if __name__ == "__main__":
    main()
    medium()
    round2()
    round3()
    apps()
    print(f"\n{OK} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)
