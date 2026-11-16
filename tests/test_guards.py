"""Guards that are coroutines, a hidden streaming tool, and path= that cannot
match: three fail-open paths found in the November 2026 security review.
Each check here failed before the fix."""
import asyncio
import functools
import io
import json
import sys
import warnings

from micromcp import (MCP, META_CAPS, META_VER, PROTOCOL, ASGIServer, Context, Server,
                      Unauthorized)

OK = FAIL = 0


def check(label, got, want):
    global OK, FAIL
    good = got == want
    OK, FAIL = OK + good, FAIL + (not good)
    print(f"  {'PASS' if good else 'FAIL'}  {label}")
    if not good:
        print(f"        got  {got!r}\n        want {want!r}")


def raises(fn, exc):
    try:
        fn()
    except exc as e:
        return type(e).__name__
    except Exception as e:  # noqa: BLE001 - the test reports the wrong type
        return f"wrong: {type(e).__name__}: {e}"
    return None


def rpc(app, method, params=None, *, auth=None, accept="application/json"):
    params = dict(params or {})
    params["_meta"] = {META_VER: PROTOCOL, META_CAPS: {}}
    raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    env = {"REQUEST_METHOD": "POST", "PATH_INFO": "/", "CONTENT_LENGTH": str(len(raw)),
           "wsgi.input": io.BytesIO(raw), "HTTP_ACCEPT": accept,
           "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL, "HTTP_MCP_METHOD": method}
    name = params.get("name") or params.get("uri")
    if name:
        env["HTTP_MCP_NAME"] = name
    if auth:
        env["HTTP_AUTHORIZATION"] = auth
    box = {}
    out = b"".join(app(env, lambda s, h: box.update(s=int(s.split()[0]))))
    return box["s"], json.loads(out)


def err(reply):
    return reply[0], reply[1].get("error", {}).get("code"), reply[1].get("error", {}).get("message")


# ── async guards ─────────────────────────────────────────────────────────────
print("async guards are awaited, not taken as a truthy coroutine")
calls = []


async def deny(principal):
    calls.append("deny")
    await asyncio.sleep(0)
    return False


async def staff(principal):
    await asyncio.sleep(0)
    return principal == "staff"


async def needs_login(principal):
    raise Unauthorized(resource_metadata="https://example.test/.well-known/oauth-protected-resource")


async def needs_scope(principal):
    raise Unauthorized.insufficient_scope(
        "admin", resource_metadata="https://example.test/.well-known/oauth-protected-resource")


def sync_returns_coroutine(principal):
    return deny(principal)          # a plain function handing back an awaitable


mcp = MCP("guards", "1")


@mcp.tool(guards=[deny])
def wipe() -> str:
    """Destructive."""
    return "DELETED"


@mcp.tool(guards=[staff])
def staff_only() -> str:
    """For staff."""
    return "ok"


@mcp.tool(guards=[functools.partial(deny)])
def via_partial() -> str:
    """Guarded through functools.partial of an async guard."""
    return "DELETED"


@mcp.tool(guards=[sync_returns_coroutine])
def via_wrapper() -> str:
    """Guarded by a sync function that returns a coroutine."""
    return "DELETED"


@mcp.tool(guards=[needs_login])
def login() -> str:
    """Needs a sign-in."""
    return "ok"


@mcp.tool(guards=[needs_scope])
def scoped() -> str:
    """Needs a scope."""
    return "ok"


@mcp.resource("notes://secret", guards=[deny])
def secret_note() -> str:
    return "SECRET"


@mcp.resource("notes://{nid}/private", guards=[deny])
def private_note(nid: str) -> str:
    return "SECRET"


@mcp.prompt(guards=[deny])
def hidden_prompt() -> str:
    return "SECRET"


mcp.skill({"SKILL.md": "---\nname: hidden\ndescription: Hidden skill\n---\n", "a.md": "SECRET"},
          guards=[deny])

app = Server(mcp, authenticate=lambda h: h.get("authorization", "").removeprefix("Bearer ") or None)
with warnings.catch_warnings():
    warnings.simplefilter("error", RuntimeWarning)       # "coroutine was never awaited" fails loudly
    tools = sorted(t["name"] for t in rpc(app, "tools/list")[1]["result"]["tools"])
    check("tools/list hides async-guarded tools (a raising guard hides too)", tools, [])
    for name in ("wipe", "via_partial", "via_wrapper"):
        check(f"tools/call {name}: refused like a missing tool",
              err(rpc(app, "tools/call", {"name": name, "arguments": {}})),
              (400, -32602, f"Unknown tool: {name}"))
    check("an async guard that allows lets the right caller in",
          rpc(app, "tools/call", {"name": "staff_only", "arguments": {}}, auth="Bearer staff")[1]
          ["result"]["content"][0]["text"], "ok")
    check("... and keeps others out",
          err(rpc(app, "tools/call", {"name": "staff_only", "arguments": {}}, auth="Bearer guest"))[:2],
          (400, -32602))
    check("an async guard raising Unauthorized answers 401",
          err(rpc(app, "tools/call", {"name": "login", "arguments": {}}))[:2], (401, -32001))
    check("an async guard raising insufficient_scope answers 403",
          err(rpc(app, "tools/call", {"name": "scoped", "arguments": {}}))[:2], (403, -32001))
    check("resources/list hides an async-guarded resource",
          [r["uri"] for r in rpc(app, "resources/list")[1]["result"]["resources"]], [])
    check("resources/templates/list hides an async-guarded template",
          rpc(app, "resources/templates/list")[1]["result"]["resourceTemplates"], [])
    check("resources/read of it is not found",
          err(rpc(app, "resources/read", {"uri": "notes://secret"}))[:2], (400, -32602))
    check("... and of the template",
          err(rpc(app, "resources/read", {"uri": "notes://x/private"}))[:2], (400, -32602))
    check("prompts/list and prompts/get hide an async-guarded prompt",
          (rpc(app, "prompts/list")[1]["result"]["prompts"],
           err(rpc(app, "prompts/get", {"name": "hidden_prompt"}))[:2]), ([], (400, -32602)))
    check("skills/list, skills/get, the file, and the directory hide an async-guarded skill",
          (rpc(app, "skills/list")[1]["result"]["skills"],
           err(rpc(app, "skills/get", {"uri": "skill://hidden/SKILL.md"}))[:2],
           err(rpc(app, "resources/read", {"uri": "skill://hidden/a.md"}))[:2],
           err(rpc(app, "resources/directory/read", {"uri": "skill://hidden"}))[:2]),
          ([], (400, -32602), (400, -32602), (400, -32602)))
check("the async guards actually ran", "deny" in calls, True)

# ── a hidden streaming tool ──────────────────────────────────────────────────
print("\na guard-hidden Context tool does not stream")
smcp = MCP("stream", "1")


@smcp.tool(guards=[lambda who: who is not None])
async def secret_stream(ctx: Context) -> str:
    """Hidden, and would stream."""
    await ctx.report_progress(1, 1)
    return "SECRET"


@smcp.tool
async def open_stream(ctx: Context) -> str:
    """Visible, streams."""
    await ctx.report_progress(1, 1)
    return "ok"


aserver = ASGIServer(smcp)


def prepared(name):
    async def go():
        params = {"name": name, "arguments": {}, "_meta": {META_VER: PROTOCOL, META_CAPS: {}}}
        raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}).encode()
        headers = {"mcp-protocol-version": PROTOCOL, "mcp-method": "tools/call", "mcp-name": name,
                   "accept": "application/json, text/event-stream", "content-type": "application/json"}
        early, req = await aserver.prepare("POST", headers, raw)
        return early, (aserver.wants_stream(req, headers) if req else None)
    return asyncio.run(go())


check("wants_stream: a hidden Context tool is not streamed", prepared("secret_stream"), (None, False))
check("wants_stream: a visible Context tool still is", prepared("open_stream"), (None, True))
check("wants_stream: a missing tool is not", prepared("nope"), (None, False))

try:
    import django
    from django.conf import settings
except ImportError:
    print("  SKIP  django is not installed")
else:
    settings.configure(DEBUG=False, SECRET_KEY="x", ALLOWED_HOSTS=["*"], ROOT_URLCONF=__name__,
                       MIDDLEWARE=[])
    django.setup()
    from django.test import Client
    from django.urls import path as route

    from micromcp import django_async_view

    urlpatterns = [route("mcp", django_async_view(ASGIServer(smcp)))]

    def dj(name):
        params = {"name": name, "arguments": {}, "_meta": {META_VER: PROTOCOL, META_CAPS: {}}}
        r = Client().post("/mcp", data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                    "params": params}),
                          content_type="application/json",
                          HTTP_ACCEPT="application/json, text/event-stream",
                          HTTP_MCP_PROTOCOL_VERSION=PROTOCOL, HTTP_MCP_METHOD="tools/call",
                          HTTP_MCP_NAME=name)
        return r.status_code, r["Content-Type"]

    check("Django async view on the WSGI handler: hidden streaming tool answers like a missing one",
          dj("secret_stream"), dj("nope"))
    check("... which is a 400 JSON error", dj("nope"), (400, "application/json"))
    check("... while a visible one still streams", dj("open_stream")[1], "text/event-stream")

# ── path= must start with a slash ────────────────────────────────────────────
print("\npath= that could never match")


def files(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"fallback"]


check("path='mcp' is refused", raises(lambda: Server(mcp, path="mcp"), ValueError), "ValueError")
check("... also with a fallback, which would have received MCP traffic and its token",
      raises(lambda: Server(mcp, path="mcp", fallback=files), ValueError), "ValueError")
check("path='/mcp' is fine", type(Server(mcp, path="/mcp")).__name__, "Server")

# ── a hidden resource does not shadow a visible template ────────────────────
print("\na hidden resource or template is passed over, not answered")
rmcp = MCP("shadow", "1")


@rmcp.resource("notes://{nid}")
def note(nid: str) -> str:
    return f"note {nid}"


@rmcp.resource("notes://payroll", guards=[deny])
def payroll() -> str:
    return "SECRET"


@rmcp.resource("notes://admin-{rest}", guards=[lambda who: False])
def admin(rest: str) -> str:
    return "SECRET"


@rmcp.resource("files://{name}", guards=[needs_login])
def signed_in_file(name: str) -> str:
    return "SECRET"


rmcp.skill({"SKILL.md": "---\nname: hidden\ndescription: Hidden\n---\n", "a.md": "SECRET"},
           guards=[deny])


@rmcp.resource("skill://hidden/{rest}")
def skill_fallback(rest: str) -> str:
    return f"public {rest}"


rapp = Server(rmcp)


def text_of(uri):
    s, body = rpc(rapp, "resources/read", {"uri": uri})
    return s, (body.get("result") or {}).get("contents", [{}])[0].get("text") or body["error"]["message"]


check("the visible template serves an ordinary URI", text_of("notes://hello"), (200, "note hello"))
check("a hidden exact resource's URI is served by the template, like any other",
      text_of("notes://payroll"), (200, "note payroll"))
check("so is a hidden template's", text_of("notes://admin-x"), (200, "note admin-x"))
check("a hidden skill file's URI falls to a matching template too",
      text_of("skill://hidden/a.md"), (200, "public a.md"))
check("with nothing else to serve it, a guard that raises Unauthorized still answers 401",
      rpc(rapp, "resources/read", {"uri": "files://x"})[0], 401)
check("and a URI nothing serves is still not found",
      err(rpc(rapp, "resources/read", {"uri": "nothing://x"}))[:2], (400, -32602))

print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
