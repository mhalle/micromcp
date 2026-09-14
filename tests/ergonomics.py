"""Everything micromcp's decorators can express today — plus probes for the
things they can't, so the gaps are demonstrated rather than asserted.
"""
import asyncio, io, json
from typing import TypedDict
from micromcp import MCP, Server, Principal, PROTOCOL, META_VER, META_CAPS


class CrashCount(TypedDict):
    street: str
    since: int
    count: int

mcp = MCP("newton-civic", "1.0.0")
srv = Server(mcp, authenticate=lambda h: {"sub": "42"}
             if h.get("authorization") == "Bearer good" else None)


def rpc(method, params=None, principal_hdr=None):
    params = dict(params or {})
    params["_meta"] = {META_VER: PROTOCOL, META_CAPS: {}}
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    raw = json.dumps(body).encode()
    env = {"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(raw)),
           "wsgi.input": io.BytesIO(raw),
           "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL, "HTTP_MCP_METHOD": method}
    nm = params.get("name") or params.get("uri")
    if nm:
        env["HTTP_MCP_NAME"] = nm
    if principal_hdr:
        env["HTTP_AUTHORIZATION"] = principal_hdr
    box = {}
    out = b"".join(srv(env, lambda s, h: box.update(s=s)))
    return int(box["s"].split()[0]), json.loads(out)


# ══ 1. Tools ═══════════════════════════════════════════════════════════════
@mcp.tool(title="Crash count by street", read_only=True, idempotent=True)
def crash_count(street: str, since: int = 2020) -> CrashCount:
    """Count crashes on a street since a given year.

    The docstring becomes the tool description the model reads; the TypedDict
    return annotation becomes the declared outputSchema.
    """
    return {"street": street, "since": since, "count": 11}


@mcp.tool
async def slow_query(limit: int = 10) -> dict:
    """Async handlers need no special registration."""
    await asyncio.sleep(0)
    return {"rows": limit}


@mcp.tool(name="severity-breakdown")
def _internal_name(street: str | None = None) -> dict:
    """Override the wire name when the Python name isn't what you want."""
    return {"street": street or "all"}


@mcp.tool(guards=[lambda p: p is not None])
def protected(x: int) -> dict:
    """Guards run before the handler; failures return isError, not a 403."""
    return {"x": x}


# ══ 2. Resources ═══════════════════════════════════════════════════════════
@mcp.resource("schema://crash", mime_type="application/json")
def crash_schema() -> str:
    """Exact-URI resource."""
    return json.dumps({"fields": ["street", "severity"]})


@mcp.resource("docs://methodology", mime_type="text/markdown")
async def methodology() -> str:
    """Async resource reader."""
    return "# How counts are derived\n..."


# ══ 3. Prompts ═════════════════════════════════════════════════════════════
@mcp.prompt
def summarize(topic: str) -> str:
    """A single-message prompt."""
    return f"Please summarize: {topic}"


@mcp.prompt
def review(street: str, year: int = 2025) -> list:
    """Multi-message prompts: return the message list yourself."""
    return [
        {"role": "user", "content": {"type": "text",
                                     "text": f"Review {street} for {year}."}},
    ]


# ══ What works ═════════════════════════════════════════════════════════════
OK = FAIL = 0
def check(label, got, want):
    global OK, FAIL
    good = got == want
    OK, FAIL = OK + good, FAIL + (not good)
    print(f"  {'PASS' if good else 'FAIL'}  {label}")
    if not good:
        print(f"        got  {got!r}\n        want {want!r}")


print("WORKS TODAY")
s, r = rpc("tools/list")
tools = {t["name"]: t for t in r["result"]["tools"]}
for t in tools.values():
    req = t["inputSchema"].get("required", [])
    print(f"  tool     {t['name']:<20} required={req} "
          f"props={list(t['inputSchema']['properties'])}")
check("anonymous listing hides the guarded tool", sorted(tools), ["crash_count", "severity-breakdown", "slow_query"])
check("crash_count requires street only", tools["crash_count"]["inputSchema"]["required"], ["street"])
check("wire name override", "severity-breakdown" in tools, True)
s, r = rpc("resources/list")
print(f"  resources {[x['uri'] for x in r['result']['resources']]}")
check("resources listed", sorted(x["uri"] for x in r["result"]["resources"]), ["docs://methodology", "schema://crash"])
s, r = rpc("prompts/list")
for p in r["result"]["prompts"]:
    print(f"  prompt   {p['name']:<20} args={[a['name'] for a in p['arguments']]}")
check("prompt arguments", [[a["name"] for a in p["arguments"]] for p in r["result"]["prompts"] if p["name"] == "review"], [["street", "year"]])

s, r = rpc("tools/call", {"name": "crash_count", "arguments": {"street": "Washington"}})
print(f"  call     -> {r['result']['structuredContent']}")
check("call result", r["result"]["structuredContent"], {"street": "Washington", "since": 2020, "count": 11})
s, r = rpc("tools/call", {"name": "protected", "arguments": {"x": 1}})
print(f"  guard    -> HTTP {s} {r.get('error', {}).get('code')}")
check("guard hides the tool from an anonymous caller (404/-32601)", (s, r["error"]["code"]), (404, -32601))

# ══ What doesn't ═══════════════════════════════════════════════════════════
print("\nNOW SUPPORTED")

# (a) resource templates — parameterized URIs
@mcp.resource("crash://{crash_id}")
def one_crash(crash_id: int) -> str:
    return json.dumps({"id": crash_id})

s, r = rpc("resources/read", {"uri": "crash://42"})
print(f"  resource template 'crash://42' -> {s} "
      f"{r.get('error', {}).get('message', 'OK')}")
s, r = rpc("resources/templates/list")
print(f"  resources/templates/list       -> {s} "
      f"{r.get('error', {}).get('message', 'OK')}")
check("template read", json.loads(rpc("resources/read", {"uri": "crash://42"})[1]["result"]["contents"][0]["text"]), {"id": 42})
check("template listed", [t["uriTemplate"] for t in r["result"]["resourceTemplates"]], ["crash://{crash_id}"])

# (b) handler wants the caller's identity
@mcp.tool
def whoami(who: Principal) -> dict:
    """`who` is filled server-side and hidden from the model."""
    return {"principal": who}

s, r = rpc("tools/call", {"name": "whoami", "arguments": {}},
           principal_hdr="Bearer good")
print(f"  principal injection            -> {r['result']['structuredContent']}")
print(f"  whoami input schema (no leak)  -> "
      f"{[t['inputSchema']['properties'] for t in rpc('tools/list')[1]['result']['tools'] if t['name']=='whoami']}")
check("principal injected", r["result"]["structuredContent"], {"principal": {"sub": "42"}})
check("principal absent from schema", [t["inputSchema"]["properties"] for t in rpc("tools/list")[1]["result"]["tools"] if t["name"] == "whoami"], [{}])

# (c) pagination
s, r = rpc("tools/list", {"cursor": "abc"})
cursor_status = s
print(f"  tools/list with a cursor       -> {s} "
      f"{r.get('error', {}).get('message', 'OK')} (we never issue cursors)")

# (d) declared output schema / title / annotations
t = [x for x in rpc("tools/list")[1]["result"]["tools"]
     if x["name"] == "crash_count"][0]
print(f"  outputSchema on tool           -> {'outputSchema' in t}")
print(f"  title on tool                  -> {'title' in t}")
print(f"  annotations (readOnlyHint etc) -> {'annotations' in t}")
check("cursor rejected", cursor_status, 400)
check("outputSchema inferred from TypedDict", t["outputSchema"]["required"], ["street", "since", "count"])
check("title and annotations carried", (t.get("title"), t.get("annotations")),
      ("Crash count by street", {"readOnlyHint": True, "idempotentHint": True}))

print(f"\n{OK} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
