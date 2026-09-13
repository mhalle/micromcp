"""Conformance harness using only mcp-types (MIT, 6 packages).

Drives micromcp in-process over WSGI — no sockets, no client library — and
validates every result against the STRICT per-version wire surface. This is
the check that catches results which parse loosely but are rejected by a real
client: notably a result missing resultType / ttlMs / cacheScope.
"""
import io, json
from mcp_types.methods import validate_server_result, validate_client_request
from micromcp import MCP, Server, PROTOCOL, META_VER, META_CAPS

mcp = MCP("newton-crashes", "0.1.0")

@mcp.tool
def severity_breakdown(street: str | None = None) -> dict:
    """Count crashes by severity."""
    return {"street": street or "all", "counts": {"pdo": 7}}

@mcp.resource("schema://crash", mime_type="application/json")
def crash_schema() -> str:
    return json.dumps({"fields": ["street"]})

@mcp.resource("crash://{crash_id}")
def one_crash(crash_id: int) -> str:
    return json.dumps({"id": crash_id})

@mcp.prompt
def summarize(topic: str) -> str:
    """Summarize a topic."""
    return f"Please summarize: {topic}"

server = Server(mcp)


def call(method, params=None):
    """Invoke the WSGI app in-process. No sockets, no client library."""
    params = dict(params or {})
    params["_meta"] = {META_VER: PROTOCOL, META_CAPS: {}}
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    raw = json.dumps(body).encode()
    environ = {
        "REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(raw)),
        "wsgi.input": io.BytesIO(raw),
        "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL, "HTTP_MCP_METHOD": method,
    }
    nm = params.get("name") or params.get("uri")
    if nm:
        environ["HTTP_MCP_NAME"] = nm
    box = {}
    chunks = server(environ, lambda s, h: box.update(status=s))
    return int(box["status"].split()[0]), json.loads(b"".join(chunks))


CASES = [
    ("server/discover", None),
    ("tools/list", None),
    ("tools/call", {"name": "severity_breakdown", "arguments": {"street": "Washington"}}),
    ("resources/list", None),
    ("resources/read", {"uri": "schema://crash"}),
    ("resources/templates/list", None),
    ("resources/read", {"uri": "crash://42"}),
    ("prompts/list", None),
    ("prompts/get", {"name": "summarize", "arguments": {"topic": "traffic"}}),
]

ok = fail = 0
print(f"validating against the strict {PROTOCOL} surface\n")
for method, params in CASES:
    status, resp = call(method, params)
    try:
        validate_server_result(method, PROTOCOL, resp["result"])
        print(f"  CONFORMS   {method}")
        ok += 1
    except KeyError:
        print(f"  no schema   {method} (nothing to check against)")
    except Exception as e:
        first = str(e).strip().split("\n")[1] if "\n" in str(e) else str(e)
        print(f"  REJECTED   {method}: {first[:90]}")
        fail += 1

# Also validate a request we construct, in the client direction.
try:
    validate_client_request("tools/call", PROTOCOL, {
        "name": "severity_breakdown", "arguments": {},
        "_meta": {META_VER: PROTOCOL, META_CAPS: {}}})
    print("\n  CONFORMS   (client request shape)")
except Exception as e:
    print(f"\n  REJECTED   client request: {str(e)[:90]}")

print(f"\n{ok} conforming, {fail} rejected")
raise SystemExit(1 if fail else 0)
