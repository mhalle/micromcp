"""End-to-end interop: drive micromcp with the official mcp SDK client.

Complements conform.py — that one validates shapes in-process, this one proves
a real client completes version negotiation and round-trips over HTTP.
"""
import asyncio, json, threading
from wsgiref.simple_server import make_server, WSGIRequestHandler, WSGIServer
from socketserver import ThreadingMixIn
from micromcp import MCP, Server
from _helpers import free_port

mcp = MCP("newton-crashes", "0.1.0")

@mcp.tool
def severity_breakdown(street: str | None = None, limit: int = 20) -> dict:
    """Count crashes by severity, optionally for one street."""
    return {"street": street or "all", "counts": {"pdo": 7, "injury": 3, "fatal": 1},
            "limit": limit}

@mcp.tool
def boom() -> dict:
    """Always raises, to check in-band error reporting."""
    raise ValueError("Crash matching query does not exist.")

@mcp.resource("schema://crash", mime_type="application/json")
def crash_schema() -> str:
    return json.dumps({"fields": ["street", "severity"]})

@mcp.prompt
def summarize(topic: str) -> str:
    """Summarize a topic."""
    return f"Please summarize: {topic}"


class Quiet(WSGIRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

class Threaded(ThreadingMixIn, WSGIServer):
    daemon_threads = True

PORT = free_port()
srv = make_server("127.0.0.1", PORT, Server(mcp),
                  server_class=Threaded, handler_class=Quiet)
threading.Thread(target=srv.serve_forever, daemon=True).start()


OK = FAIL = 0
def check(label, got, want):
    global OK, FAIL
    good = got == want
    OK, FAIL = OK + good, FAIL + (not good)
    print(f"  {'PASS' if good else 'FAIL'}  {label}")
    if not good:
        print(f"        got  {got!r}\n        want {want!r}")


async def main():
    from mcp import Client
    print("official mcp SDK client\n")

    async with Client(f"http://127.0.0.1:{PORT}/mcp") as c:
        tools = (await c.list_tools()).tools
        print(f"list_tools -> {[t.name for t in tools]}")
        check("both tools listed", sorted(t.name for t in tools), ["boom", "severity_breakdown"])
        t = [x for x in tools if x.name == "severity_breakdown"][0]
        print(f"  description: {t.description}")
        print(f"  schema: {json.dumps(t.input_schema)}")
        check("description from docstring", t.description, "Count crashes by severity, optionally for one street.")
        check("schema closed with defaults", (t.input_schema.get("additionalProperties"), t.input_schema["properties"]["limit"]),
              (False, {"type": "integer", "default": 20}))

        r = await c.call_tool("severity_breakdown", {"street": "Washington"})
        print(f"\ncall_tool -> {r.content[0].text}")
        print(f"  structured: {getattr(r, 'structured_content', None)}")
        check("structured result", r.structured_content,
              {"street": "Washington", "counts": {"pdo": 7, "injury": 3, "fatal": 1}, "limit": 20})
        check("not an error", r.is_error, False)

        r = await c.call_tool("boom", {})
        print(f"\nerror tool -> is_error={r.is_error} text={r.content[0].text!r}")
        check("in-band error", (r.is_error, r.content[0].text), (True, "Crash matching query does not exist."))

        res = (await c.list_resources()).resources
        print(f"\nlist_resources -> {[str(x.uri) for x in res]}")
        rr = (await c.read_resource("schema://crash")).contents
        print(f"read_resource -> {rr[0].text}")
        check("resource listed and readable", ([str(x.uri) for x in res], json.loads(rr[0].text)),
              (["schema://crash"], {"fields": ["street", "severity"]}))

        ps = (await c.list_prompts()).prompts
        print(f"\nlist_prompts -> {[p.name for p in ps]}")
        pr = await c.get_prompt("summarize", {"topic": "traffic"})
        print(f"get_prompt -> {pr.messages[0].content.text}")
        check("prompt rendered", pr.messages[0].content.text, "Please summarize: traffic")

    print(f"\n{OK} passed, {FAIL} failed")
    if FAIL:
        raise SystemExit(1)
    print("INTEROP OK (official SDK client)")

asyncio.run(main())
srv.shutdown()
