# micromcp

A minimum-viable MCP server for the stateless `2026-07-28` protocol revision.
Pure standard library — no runtime dependencies, no framework.

One core, two transports:

- `Server` — a WSGI app (Django, Flask, gunicorn, waitress, wsgiref)
- `ASGIServer` — a native ASGI app (Starlette, FastAPI, Litestar, uvicorn)

Handlers may be `def` or `async def` on either.

## Quick start

```python
from micromcp import MCP, ASGIServer

mcp = MCP("newton-civic", "1.0.0")

@mcp.tool
def crash_count(street: str, since: int = 2020) -> dict:
    """Count crashes on a street since a given year."""
    return {"street": street, "count": 11}

app = ASGIServer(mcp)          # uvicorn micromcp_app:app
```

Type hints become JSON Schema, docstrings become tool descriptions, and a
returned `dict` becomes both `content` text and `structuredContent`.

## Tools

```python
@mcp.tool(title="Crash count by street", read_only=True, idempotent=True)
def crash_count(street: str, since: int = 2020) -> CrashCount: ...

@mcp.tool(name="severity-breakdown")     # override the wire name
def _internal(street: str | None = None) -> dict: ...

@mcp.tool(guards=[lambda principal: principal is not None])
def protected(x: int) -> dict: ...
```

`read_only` / `destructive` / `idempotent` become the standard annotation hints
clients use to decide what to auto-approve. `outputSchema` is inferred from a
`TypedDict` or dataclass return annotation, or passed explicitly via
`output_schema=`.

Guard failures return `isError: true` in-band rather than an HTTP error, which
is what lets a model reason about them.

## Resources and prompts

```python
@mcp.resource("schema://crash", mime_type="application/json")
def crash_schema() -> str: ...

@mcp.resource("crash://{crash_id}")      # template; crash_id coerced to int
def one_crash(crash_id: int) -> str: ...

@mcp.prompt
def review(street: str, year: int = 2025) -> list:
    return [{"role": "user", "content": {"type": "text", "text": "..."}}]
```

Exact URIs win over templates. Templates are reported via
`resources/templates/list`.

## Injected parameters

Two annotations are filled server-side and excluded from the input schema, so
the model never sees them and cannot supply them:

```python
from micromcp import Principal, Context

@mcp.tool
def whoami(who: Principal) -> dict:
    return {"sub": who["sub"] if who else None}

@mcp.tool
async def segment(volume: str, ctx: Context) -> dict:
    for i, step in enumerate(steps, 1):
        await ctx.report_progress(i, len(steps), message=step.name)
    await ctx.info("done")
    return {"labels": 117}
```

`Principal` receives whatever the `authenticate(headers)` callback returned.

Declaring `ctx: Context` is what makes a tool **stream**: on `ASGIServer` the
call is answered with a request-scoped SSE stream carrying
`notifications/progress` and `notifications/message` frames, then the final
JSON-RPC response. Client hang-up cancels the handler task.

Two behaviors worth knowing:

- Progress requires the client to send a `progressToken`; without one the
  notifications are dropped (there is nothing to correlate them to) while the
  result is still delivered normally. Logging carries no token and is unaffected.
- Under WSGI, notifications are dropped entirely and the call returns ordinary
  JSON. WSGI has no portable client-disconnect signal, so it cannot honor the
  spec's cancellation rule; shipping a stream that keeps computing after the
  client is gone would be worse than not streaming.

## Auth

```python
def authenticate(headers):                 # lowercase header mapping
    return verify(headers.get("authorization", ""))

app = ASGIServer(mcp, authenticate=authenticate,
                 allowed_origins={"https://app.example.com"})
```

Guards are predicates over the principal only — they cannot see call arguments,
so per-record authorization belongs inside the handler.

## Mounting

```python
# Flask, alongside ordinary routes
from werkzeug.middleware.dispatcher import DispatcherMiddleware
combined = DispatcherMiddleware(flask_app, {"/mcp": Server(mcp)})

# Starlette / FastAPI, native
from starlette.routing import Mount
routes = [Mount("/mcp", app=ASGIServer(mcp))]

# Django
from micromcp import django_view, django_async_view
urlpatterns = [path("mcp", django_async_view(ASGIServer(mcp)))]
```

Mount at the exact path clients will use: a framework `Mount("/mcp")` typically
307-redirects `/mcp` to `/mcp/`, and clients will not replay a POST across a
redirect.

## Scope

Implemented: `server/discover`, `tools/list`, `tools/call`, `resources/list`,
`resources/read`, `resources/templates/list`, `prompts/list`, `prompts/get`,
plus progress and logging notifications.

Deliberately not implemented: the initialize-handshake era (`2024-11-05` through
`2025-11-25`), sessions, resumable streams, `subscriptions/listen`,
`completion/complete`, MRTR/elicitation, pagination cursors, and OAuth.

**Client compatibility.** This server speaks only `2026-07-28`. It works with
the Python `mcp` SDK 2.x and FastMCP 4.x. The TypeScript SDK tops out at
`2025-11-25` as of this writing, so TS-based clients — Claude Desktop, Cursor,
Zed, MCP Inspector — cannot yet connect. They are refused with `-32022` and a
`data.supported` list naming this revision, which is the code a dual-era client
reads as "modern peer, renegotiate", so they will work once their SDK ships
2026 support.

OAuth is the boundary where rolling your own stops being sensible. Bearer tokens
against your own store are fine; being an OAuth 2.1 authorization server (RFC
9728 / 8414 / 7591, PKCE) is not 600 lines and is not code to hand-roll.

## Tests

```
python3 test_micromcp.py                  # 28 unit assertions, no deps
python3 conform.py                        # 11 checks   (needs: mcp-types)
python3 interop.py                        # end-to-end  (needs: mcp)
python3 test_asgi.py                      # native ASGI (needs: mcp, uvicorn, starlette)
python3 test_progress.py                  # 24 SSE      (needs: mcp, uvicorn, httpx)
python3 harnesses.py                      # 6 servers   (needs: flask, starlette,
                                          #   uvicorn, waitress, gunicorn, a2wsgi)
python3 ergonomics.py                     # API tour, no deps
```

`conform.py` validates every result and notification against the official
per-version wire schema from `mcp-types` (MIT, 6 packages). It is the cheapest
and most precise check: it names the failing field, and it is versioned against
the spec, so bumping `mcp-types` tells you exactly what a future revision breaks.
Run it on every commit.
