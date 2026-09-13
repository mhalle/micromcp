"""Mount micromcp under every common Python web harness and prove each one works.

Each case starts a real server on a real port and does a real HTTP tools/call.
"""
import json, socket, subprocess, sys, threading, time
import urllib.request, urllib.error

from micromcp import MCP, Server, ASGIServer, PROTOCOL, META_VER, META_CAPS

mcp = MCP("demo", "0.1.0")

@mcp.tool
def add(a: int, b: int) -> dict:
    """Add two integers."""
    return {"sum": a + b}

@mcp.tool
async def aadd(a: int, b: int) -> dict:
    """Async handler — works on both transports."""
    import asyncio; await asyncio.sleep(0)
    return {"sum": a + b}

wsgi_app = Server(mcp)          # WSGI callable
asgi_app = ASGIServer(mcp)      # native ASGI callable


def call(port, path="/mcp", tool="add"):
    """Real HTTP tools/call. Returns the tool's structured result."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": {"a": 2, "b": 3},
                       "_meta": {META_VER: PROTOCOL, META_CAPS: {}}}}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "MCP-Protocol-Version": PROTOCOL,
                 "Mcp-Method": "tools/call", "Mcp-Name": tool})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["result"]["structuredContent"]


def wait(port, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


RESULTS = []
def record(name, detail, fn):
    try:
        got = fn()
        good = got == {"sum": 5}
        RESULTS.append((good, name, detail, got if not good else ""))
    except Exception as e:
        RESULTS.append((False, name, detail, f"{type(e).__name__}: {str(e)[:70]}"))


# 1 ── stdlib wsgiref -------------------------------------------------------
def case_wsgiref():
    from wsgiref.simple_server import make_server, WSGIRequestHandler, WSGIServer
    from socketserver import ThreadingMixIn
    class Q(WSGIRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a): pass
    class T(ThreadingMixIn, WSGIServer): daemon_threads = True
    srv = make_server("127.0.0.1", 8501, wsgi_app, server_class=T, handler_class=Q)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        return call(8501)
    finally:
        srv.shutdown()

# 2 ── Flask (mounted alongside normal routes) ------------------------------
def case_flask():
    from flask import Flask
    from werkzeug.middleware.dispatcher import DispatcherMiddleware
    from werkzeug.serving import make_server as wz_make_server
    flask_app = Flask(__name__)

    @flask_app.route("/")
    def index():
        return "a normal Flask route"

    combined = DispatcherMiddleware(flask_app, {"/mcp": wsgi_app})
    srv = wz_make_server("127.0.0.1", 8502, combined, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # the ordinary Flask route still works
        with urllib.request.urlopen("http://127.0.0.1:8502/", timeout=5) as r:
            assert b"normal Flask route" in r.read()
        return call(8502, "/mcp/")
    finally:
        srv.shutdown()

# 3 ── Starlette / ASGI via a WSGI bridge -----------------------------------
def case_starlette():
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Mount, Route
    from a2wsgi import WSGIMiddleware

    async def home(request):
        return PlainTextResponse("a normal Starlette route")

    app = Starlette(routes=[
        Route("/", home),
        Mount("/mcp", app=WSGIMiddleware(wsgi_app)),
    ])
    cfg = uvicorn.Config(app, host="127.0.0.1", port=8503, log_level="critical")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()
    wait(8503)
    try:
        # Mount("/mcp") issues a 307 to "/mcp/"; urllib will not replay a POST
        # across a redirect, so address the canonical path directly.
        return call(8503, "/mcp/")
    finally:
        srv.should_exit = True

# 3b ── Starlette mounting the NATIVE ASGI app (no bridge) ------------------
def case_starlette_native():
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Mount
    app = Starlette(routes=[Mount("/mcp", app=asgi_app)])
    cfg = uvicorn.Config(app, host="127.0.0.1", port=8506, log_level="critical")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()
    wait(8506)
    try:
        return call(8506, "/mcp/", tool="aadd")   # async handler, natively awaited
    finally:
        srv.should_exit = True

# 4 ── waitress (production WSGI server) ------------------------------------
def case_waitress():
    import waitress
    t = threading.Thread(target=waitress.serve,
                         kwargs=dict(app=wsgi_app, host="127.0.0.1", port=8504,
                                     _quiet=True, threads=4), daemon=True)
    t.start()
    wait(8504)
    return call(8504, tool="aadd")   # async handler via the WSGI background loop

# 5 ── gunicorn (separate process, as in production) ------------------------
def case_gunicorn():
    p = subprocess.Popen(
        [sys.executable, "-m", "gunicorn", "-b", "127.0.0.1:8505",
         "-w", "2", "--log-level", "error", "harnesses:wsgi_app"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait(8505, timeout=20)
        return call(8505)
    finally:
        p.terminate(); p.wait(timeout=10)


def main():
    for name, detail, fn in [
        ("wsgiref",   "stdlib, zero install",                case_wsgiref),
        ("Flask",     "DispatcherMiddleware, / still works", case_flask),
        ("Starlette", "WSGI app via a2wsgi bridge",           case_starlette),
        ("Starlette+", "NATIVE ASGI mount, async handler",     case_starlette_native),
        ("waitress",  "prod WSGI + async handler on bg loop", case_waitress),
        ("gunicorn",  "2 workers, separate process",         case_gunicorn),
    ]:
        record(name, detail, fn)

    print()
    for good, name, detail, err in RESULTS:
        print(f"  {'OK  ' if good else 'FAIL'}  {name:<10} {detail}")
        if err:
            print(f"          {err}")
    bad = sum(1 for g, *_ in RESULTS if not g)
    print(f"\n{len(RESULTS)-bad}/{len(RESULTS)} harnesses served micromcp")
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
