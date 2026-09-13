"""Exercise micromcp through a real WSGI stack (wsgiref), asserting spec behavior."""
import json, threading, urllib.request, urllib.error
from wsgiref.simple_server import make_server, WSGIRequestHandler
from micromcp import MCP, Server, PROTOCOL, META_VER, META_CAPS

mcp = MCP("demo", "0.1.0")

@mcp.tool
def severity_breakdown(street: str | None = None, limit: int = 20) -> dict:
    """Count crashes by severity, optionally for one street."""
    return {"street": street or "all", "counts": {"pdo": 7, "injury": 3}, "limit": limit}

@mcp.tool(guards=[lambda p: p is not None])
def protected(x: int) -> dict:
    """Requires a principal."""
    return {"x": x}

@mcp.tool
def boom() -> dict:
    """Always raises."""
    raise ValueError("Crash matching query does not exist.")

@mcp.resource("schema://crash", mime_type="application/json")
def crash_schema() -> str:
    return json.dumps({"fields": ["street", "severity"]})

@mcp.prompt
def summarize(topic: str) -> str:
    """Summarize a topic."""
    return f"Please summarize: {topic}"

def auth(headers):
    tok = headers.get("authorization", "")
    return {"sub": "42"} if tok == "Bearer good" else None

app = Server(mcp, allowed_origins={"https://ok.example"}, authenticate=auth)


class Quiet(WSGIRequestHandler):
    def log_message(self, *a): pass

srv = make_server("127.0.0.1", 8222, app, handler_class=Quiet)
threading.Thread(target=srv.serve_forever, daemon=True).start()


def post(method, params=None, *, hdrs=None, meta=True, name=None, caps=True):
    params = dict(params or {})
    if meta:
        params["_meta"] = {META_VER: PROTOCOL}
        if caps:
            params["_meta"][META_CAPS] = {}
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream",
         "MCP-Protocol-Version": PROTOCOL, "Mcp-Method": method}
    nm = name if name is not None else (params.get("name") or params.get("uri"))
    if nm: h["Mcp-Name"] = nm
    h.update(hdrs or {})
    req = urllib.request.Request("http://127.0.0.1:8222/mcp",
                                 data=json.dumps(body).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


ok = fail = 0
def check(label, got, want):
    global ok, fail
    good = got == want
    ok, fail = ok + good, fail + (not good)
    print(f"  {'PASS' if good else 'FAIL'}  {label}")
    if not good:
        print(f"        got  {got!r}\n        want {want!r}")

print("— happy path —")
s, r = post("tools/list")
names = sorted(t["name"] for t in r["result"]["tools"])
check("tools/list returns all tools", names, ["boom", "protected", "severity_breakdown"])
sch = [t for t in r["result"]["tools"] if t["name"] == "severity_breakdown"][0]["inputSchema"]
check("schema: str|None -> anyOf", sch["properties"]["street"],
      {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None})
check("schema: int default", sch["properties"]["limit"], {"type": "integer", "default": 20})
check("schema: no required", "required" not in sch, True)
check("docstring -> description",
      [t for t in r["result"]["tools"] if t["name"] == "severity_breakdown"][0]["description"],
      "Count crashes by severity, optionally for one street.")

s, r = post("tools/call", {"name": "severity_breakdown", "arguments": {"street": "Washington"}})
check("tools/call structuredContent", r["result"]["structuredContent"]["street"], "Washington")
check("tools/call text content", json.loads(r["result"]["content"][0]["text"])["counts"]["pdo"], 7)

s, r = post("resources/list")
check("resources/list", r["result"]["resources"][0]["uri"], "schema://crash")
s, r = post("resources/read", {"uri": "schema://crash"})
check("resources/read", json.loads(r["result"]["contents"][0]["text"])["fields"], ["street", "severity"])
s, r = post("prompts/get", {"name": "summarize", "arguments": {"topic": "traffic"}})
check("prompts/get", r["result"]["messages"][0]["content"]["text"], "Please summarize: traffic")

print("— errors are in-band, not transport errors —")
s, r = post("tools/call", {"name": "boom", "arguments": {}})
check("raising tool -> isError", (s, r["result"]["isError"]), (200, True))
check("raising tool -> message", r["result"]["content"][0]["text"],
      "Crash matching query does not exist.")

print("— auth guard —")
s, r = post("tools/call", {"name": "protected", "arguments": {"x": 1}})
check("guard blocks anonymous", r["result"]["isError"], True)
s, r = post("tools/call", {"name": "protected", "arguments": {"x": 1}},
            hdrs={"Authorization": "Bearer good"})
check("guard allows principal", r["result"]["structuredContent"], {"x": 1})

print("— spec-mandated validation —")
s, r = post("tools/call", {"name": "severity_breakdown"}, hdrs={"Mcp-Method": "tools/list"})
check("Mcp-Method mismatch -> 400/-32020", (s, r["error"]["code"]), (400, -32020))
s, r = post("tools/call", {"name": "severity_breakdown"}, name="wrong")
check("Mcp-Name mismatch -> 400/-32020", (s, r["error"]["code"]), (400, -32020))
s, r = post("tools/list", hdrs={"MCP-Protocol-Version": "2025-06-18"})
check("version header/body mismatch -> 400", (s, r["error"]["code"]), (400, -32020))
s, r = post("tools/list", meta=False)
check("missing _meta entirely -> -32020", (s, r["error"]["code"]), (400, -32020))
s, r = post("tools/list", caps=False)
check("missing clientCapabilities -> -32602", (s, r["error"]["code"]), (400, -32602))
s, r = post("nope/nope")
check("unknown method -> 404/-32601", (s, r["error"]["code"]), (404, -32601))
s, r = post("tools/call", {"name": "ghost"})
check("unknown tool -> 404/-32601", (s, r["error"]["code"]), (404, -32601))
s, r = post("tools/list", hdrs={"Origin": "https://evil.example"})
check("bad Origin -> 403", s, 403)
s, r = post("tools/list", hdrs={"Origin": "https://ok.example"})
check("good Origin -> 200", s, 200)

req = urllib.request.Request("http://127.0.0.1:8222/mcp", method="GET")
try:
    urllib.request.urlopen(req); code = 200
except urllib.error.HTTPError as e:
    code = e.code
check("GET -> 405 (no legacy stream)", code, 405)

print(f"\n{ok} passed, {fail} failed")
srv.shutdown()
raise SystemExit(1 if fail else 0)
