"""Prove ASGIServer works natively: async handlers, direct uvicorn, and mounted
inside Starlette with no WSGI bridge. Driven by the official mcp SDK client.
"""
import asyncio, json, socket, threading, time
from micromcp import MCP, ASGIServer

mcp = MCP("demo", "0.1.0")


@mcp.tool
def add(a: int, b: int) -> dict:
    """Sync handler — still works."""
    return {"sum": a + b}


@mcp.tool
async def slow_add(a: int, b: int) -> dict:
    """Async handler — awaited on the caller's own loop, no threadpool."""
    await asyncio.sleep(0.05)
    return {"sum": a + b, "awaited": True}


@mcp.resource("schema://demo", mime_type="application/json")
async def demo_schema() -> str:
    """Async resource reader."""
    await asyncio.sleep(0)
    return json.dumps({"ok": True})


asgi_app = ASGIServer(mcp)


def wait(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


def serve(app, port):
    import uvicorn
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()
    wait(port)
    return srv


async def exercise(url, label):
    from mcp import Client
    async with Client(url) as c:
        tools = sorted(t.name for t in (await c.list_tools()).tools)
        r1 = await c.call_tool("add", {"a": 2, "b": 3})
        r2 = await c.call_tool("slow_add", {"a": 2, "b": 3})
        rr = (await c.read_resource("schema://demo")).contents
    print(f"  {label}")
    print(f"    tools     : {tools}")
    print(f"    sync tool : {r1.structured_content}")
    print(f"    async tool: {r2.structured_content}")
    print(f"    async res : {rr[0].text}")
    assert r1.structured_content == {"sum": 5}
    assert r2.structured_content == {"sum": 5, "awaited": True}


async def concurrency_check(url):
    """Ten concurrent slow calls should overlap, not serialize."""
    from mcp import Client
    async with Client(url) as c:
        t0 = time.time()
        await asyncio.gather(*[
            c.call_tool("slow_add", {"a": 1, "b": 1}) for _ in range(10)])
        dt = time.time() - t0
    serial = 10 * 0.05
    print(f"    10 concurrent 50ms calls took {dt:.2f}s (serial would be {serial:.2f}s)")
    assert dt < serial, "async handlers serialized — not running natively"


async def main():
    # 1. ASGIServer mounted directly under uvicorn
    s1 = serve(asgi_app, 8601)
    await exercise("http://127.0.0.1:8601/mcp", "ASGIServer direct under uvicorn")
    await concurrency_check("http://127.0.0.1:8601/mcp")
    s1.should_exit = True

    # 2. Mounted inside Starlette alongside ordinary routes — no WSGI bridge
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Mount, Route

    async def home(request):
        return PlainTextResponse("a normal Starlette route")

    app = Starlette(routes=[Route("/", home), Mount("/mcp", app=asgi_app)])
    s2 = serve(app, 8602)
    await exercise("http://127.0.0.1:8602/mcp/", "mounted in Starlette (native ASGI)")
    s2.should_exit = True

    print("\nASGI OK")


asyncio.run(main())
