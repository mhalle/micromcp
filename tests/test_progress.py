"""SSE progress: ordering, cancellation, no-token behavior, WSGI degradation.

Driven by the official mcp SDK client where possible, and by a raw SSE reader
where the client abstracts away what we need to assert.
"""
import asyncio, io, json, socket, threading, time
import httpx
from micromcp import (MCP, Server, ASGIServer, Context, PROTOCOL, META_VER, META_CAPS, META_LOG,
                      META_SUB)
from _helpers import free_port

mcp = MCP("demo", "0.1.0")
CANCELLED = threading.Event()


@mcp.tool
async def crunch(steps: int = 5, ctx: Context = None) -> dict:
    """Report progress across N steps, then return."""
    for i in range(1, steps + 1):
        await asyncio.sleep(0.02)
        await ctx.report_progress(i, steps, message=f"step {i}")
    await ctx.info("finished crunching")
    return {"processed": steps}


@mcp.tool
async def forever(ctx: Context = None) -> dict:
    """Runs until cancelled; sets a flag if it is."""
    try:
        for i in range(1000):
            await asyncio.sleep(0.02)
            await ctx.report_progress(i, 1000)
        return {"done": True}
    except asyncio.CancelledError:
        CANCELLED.set()
        raise


@mcp.tool
async def levels(ctx: Context = None) -> dict:
    """One message at each of three levels."""
    for level in ("debug", "warning", "error"):
        await ctx.log(level, level)
    return {"level": ctx.log_level}


@mcp.tool
async def misspelt(ctx: Context = None) -> dict:
    """Logs at a level the spec does not have."""
    await ctx.log("warn", "oops")
    return {"unreachable": True}


@mcp.tool
def plain(a: int, b: int) -> dict:
    """No Context -> never streams."""
    return {"sum": a + b}


asgi_app = ASGIServer(mcp)
wsgi_app = Server(mcp)

OK = FAIL = 0
def check(label, got, want):
    global OK, FAIL
    good = got == want
    OK, FAIL = OK + good, FAIL + (not good)
    print(f"  {'PASS' if good else 'FAIL'}  {label}")
    if not good:
        print(f"        got  {got!r}\n        want {want!r}")


def wait(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close(); return
        except OSError:
            time.sleep(0.15)


def serve(app, port):
    import uvicorn
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                        log_level="critical"))
    threading.Thread(target=srv.run, daemon=True).start()
    wait(port)
    return srv


def req_body(tool, args=None, token=None, log_level=None):
    meta = {META_VER: PROTOCOL, META_CAPS: {}}
    if token is not None:
        meta["progressToken"] = token
    if log_level is not None:
        meta[META_LOG] = log_level
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args or {}, "_meta": meta}}


HDRS = lambda tool: {"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     "MCP-Protocol-Version": PROTOCOL,
                     "Mcp-Method": "tools/call", "Mcp-Name": tool}


async def read_sse(url, tool, args=None, token=None, stop_after=None, log_level=None):
    """Collect SSE frames. If stop_after is set, hang up after N frames."""
    frames = []
    async with httpx.AsyncClient(timeout=30) as c:
        async with c.stream("POST", url, json=req_body(tool, args, token, log_level),
                            headers=HDRS(tool)) as r:
            ctype = r.headers.get("content-type", "")
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[6:]))
                    if stop_after and len(frames) >= stop_after:
                        return ctype, frames, r.headers
            return ctype, frames, r.headers


async def main():
    port = free_port()
    srv = serve(asgi_app, port)
    url = f"http://127.0.0.1:{port}/mcp"

    print("— streaming shape —")
    ctype, frames, hdrs = await read_sse(url, "crunch", {"steps": 5}, token="p1",
                                         log_level="info")
    check("content-type is SSE", ctype.split(";")[0], "text/event-stream")
    check("x-accel-buffering set", hdrs.get("x-accel-buffering"), "no")
    progress = [f for f in frames if f.get("method") == "notifications/progress"]
    logs = [f for f in frames if f.get("method") == "notifications/message"]
    results = [f for f in frames if "result" in f]
    check("5 progress notifications", len(progress), 5)
    check("1 log notification", len(logs), 1)
    check("log level", logs[0]["params"]["level"], "info")
    check("1 final result", len(results), 1)
    check("result is last frame", "result" in frames[-1], True)
    check("progress counts up", [p["params"]["progress"] for p in progress],
          [1.0, 2.0, 3.0, 4.0, 5.0])
    check("total carried", progress[0]["params"]["total"], 5.0)
    check("token echoed", {p["params"]["progressToken"] for p in progress}, {"p1"})
    check("message carried", progress[0]["params"]["message"], "step 1")
    check("final payload", results[0]["result"]["structuredContent"], {"processed": 5})

    print("\n— client sends no progressToken —")
    ctype, frames, _ = await read_sse(url, "crunch", {"steps": 3}, log_level="info")
    check("still SSE", ctype.split(";")[0], "text/event-stream")
    check("no progress frames", [f for f in frames
                                 if f.get("method") == "notifications/progress"], [])
    # Logging is opted into by logLevel, not by the progress token.
    check("log frames still flow", len([f for f in frames
                                        if f.get("method") == "notifications/message"]), 1)
    check("result still delivered", frames[-1]["result"]["structuredContent"],
          {"processed": 3})

    print("\n— log messages follow the request's logLevel —")
    def logged(frames):
        return [f["params"]["level"] for f in frames if f.get("method") == "notifications/message"]
    _, frames, _ = await read_sse(url, "levels")
    check("no logLevel, no log frames", logged(frames), [])
    check("... and ctx.log_level is None", frames[-1]["result"]["structuredContent"],
          {"level": None})
    _, frames, _ = await read_sse(url, "levels", log_level="warning")
    check("logLevel=warning drops debug", logged(frames), ["warning", "error"])
    _, frames, _ = await read_sse(url, "levels", log_level="debug")
    check("logLevel=debug admits all", logged(frames), ["debug", "warning", "error"])
    async with httpx.AsyncClient(timeout=10) as c:
        for bad in ("warn", "WARNING", 3, None):
            b = req_body("levels")
            b["params"]["_meta"][META_LOG] = bad
            r = await c.post(url, json=b, headers=HDRS("levels"))
            check(f"logLevel {bad!r} is 400 / -32602",
                  (r.status_code, r.json().get("error", {}).get("code")), (400, -32602))
    _, frames, _ = await read_sse(url, "misspelt")
    check("an unknown level in the handler is an in-band error, listener or not",
          frames[-1]["result"].get("isError"), True)

    print("\n— tool without Context never streams —")
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, json=req_body("plain", {"a": 2, "b": 3}),
                         headers=HDRS("plain"))
    check("plain JSON response", r.headers["content-type"].split(";")[0],
          "application/json")
    check("plain result", r.json()["result"]["structuredContent"], {"sum": 5})

    print("\n— cancellation on client hang-up —")
    CANCELLED.clear()
    await read_sse(url, "forever", token="p2", stop_after=3)
    await asyncio.sleep(0.5)
    check("handler was cancelled", CANCELLED.is_set(), True)

    print("\n— official SDK client sees the progress —")
    from mcp import Client
    seen = []
    async with Client(url) as cl:
        async def on_progress(progress, total=None, message=None):
            seen.append((progress, total, message))
        r = await cl.call_tool("crunch", {"steps": 4},
                               progress_callback=on_progress)
    check("SDK client got result", r.structured_content, {"processed": 4})
    check("SDK client got progress", len(seen), 4)

    print("\n— subscriptions/listen closes gracefully —")
    def listen_body(rid, subs):
        return {"jsonrpc": "2.0", "id": rid, "method": "subscriptions/listen",
                "params": {"_meta": {META_VER: PROTOCOL, META_CAPS: {}},
                           "notifications": subs}}
    LH = {"Content-Type": "application/json",
          "Accept": "application/json, text/event-stream",
          "MCP-Protocol-Version": PROTOCOL, "Mcp-Method": "subscriptions/listen"}
    frames = []
    async with httpx.AsyncClient(timeout=10) as c:
        async with c.stream("POST", url, headers=LH,
                            json=listen_body("sub-1", {"toolsListChanged": True,
                                                       "resourceSubscriptions": ["x://y"]})) as r:
            ctype = r.headers.get("content-type", "")
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[6:]))
    check("listen is SSE", ctype.split(";")[0], "text/event-stream")
    check("two frames then close", len(frames), 2)
    ack, res = (frames + [{}, {}])[:2]
    check("ack first", ack.get("method"), "notifications/subscriptions/acknowledged")
    check("ack carries subscription id", ack.get("params", {}).get("_meta", {}).get(META_SUB), "sub-1")
    check("nothing honored", ack.get("params", {}).get("notifications"), {})
    check("result closes the subscription", res.get("id"), "sub-1")
    check("result complete", res.get("result", {}).get("resultType"), "complete")
    check("result carries subscription id", res.get("result", {}).get("_meta", {}).get(META_SUB), "sub-1")
    try:
        from mcp_types._v2026_07_28 import (SubscriptionsAcknowledgedNotification,
                                            SubscriptionsListenResult)
        SubscriptionsAcknowledgedNotification.model_validate(ack)
        SubscriptionsListenResult.model_validate(res["result"])
        check("strict wire models accept both frames", True, True)
    except Exception as e:
        check("strict wire models accept both frames", repr(e), True)
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, json=listen_body(2, {}), headers={**LH, "Accept": "application/json"})
        check("JSON-only client gets a JSON result",
              r.headers["content-type"].split(";")[0], "application/json")
        check("JSON result carries subscription id", r.json()["result"]["_meta"][META_SUB], 2)
        r = await c.post(url, json=listen_body(3, {"toolsListChanged": "yes"}), headers=LH)
        check("bad filter is 400", r.status_code, 400)
        check("bad filter is -32602", r.json()["error"]["code"], -32602)
        r = await c.post(url, json=listen_body(4, ["x"]), headers=LH)
        check("non-object filter is 400", r.status_code, 400)

    srv.should_exit = True

    print("\n— WSGI degrades honestly —")
    raw = json.dumps(req_body("crunch", {"steps": 3}, token="p3")).encode()
    env = {"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(raw)),
           "wsgi.input": io.BytesIO(raw), "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL,
           "HTTP_MCP_METHOD": "tools/call", "HTTP_MCP_NAME": "crunch"}
    box = {}
    out = b"".join(wsgi_app(env, lambda s, h: box.update(s=s, h=dict(h))))
    check("WSGI status 200", box["s"], "200 OK")
    check("WSGI is JSON", box["h"]["Content-Type"], "application/json")
    check("WSGI result correct", json.loads(out)["result"]["structuredContent"],
          {"processed": 3})
    raw = json.dumps(listen_body(9, {})).encode()
    env = {"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(raw)),
           "wsgi.input": io.BytesIO(raw), "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL,
           "HTTP_MCP_METHOD": "subscriptions/listen",
           "HTTP_ACCEPT": "application/json, text/event-stream"}
    box = {}
    out = b"".join(wsgi_app(env, lambda s, h: box.update(s=s, h=dict(h))))
    check("WSGI listen is JSON", box["h"]["Content-Type"], "application/json")
    check("WSGI listen result", json.loads(out)["result"]["_meta"][META_SUB], 9)

    print(f"\n{OK} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


asyncio.run(main())
