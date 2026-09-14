# micromcp

A minimum-viable MCP server for the stateless `2026-07-28` protocol revision.
Pure standard library — no runtime dependencies, no framework. Apache-2.0.

```
pip install micromcp            # Python 3.11+  (first release pending; until then:
pip install "micromcp[django]"  #  pip install git+https://github.com/mhalle/micromcp)
```

Prefer a single file you can vendor? `python tools/bundle.py` regenerates
`build/micromcp.py` from the package; CI regenerates it and runs every suite
against it.

One core, two transports:

- `Server` — a WSGI app (Django, Flask, gunicorn, waitress, wsgiref)
- `ASGIServer` — a native ASGI app (Starlette, FastAPI, Litestar, uvicorn)

Handlers may be `def` or `async def` on either. Sync handlers run on a thread
pool, so a slow one neither serializes requests nor blocks the event loop.

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

Arguments are validated against the generated `inputSchema` before the handler
runs: a wrong type, a missing key, or an unexpected one is `-32602` (the schema
closes `additionalProperties`, nested objects included). Validated JSON is then
converted to what the signature declares — `date`, `Enum`, `set`, `tuple`,
`Path`, dataclass, pydantic model — so the handler receives real objects, not
strings and lists. When a tool declares an `outputSchema`, its
`structuredContent` is checked against it and a mismatch is an in-band
`isError`; dataclass and pydantic return values are converted to dicts first.
Defaults that are not JSON (dates, enums, paths) are rendered as strings.
`Literal`, `Enum`, `tuple[int, str]`, `set[str]`, `dict[str, int]`, `NewType`,
dates, paths, and pydantic models all map to the right schema, and the
validator enforces what those schemas say — including the `minimum`,
`pattern`, `const`, `format`, and `$ref` constraints pydantic emits, with
nested `$defs` namespaced per parameter so two models' inner classes cannot
collide. Conversion errors are answered with a constant `-32602`; the
validator's text stays in the server log. A Google- or
NumPy-style `Args:` section in the docstring becomes per-parameter
`description`s and is removed from the tool description the model reads.

Registration is strict where silence would hurt: a duplicate name raises
unless you pass `replace=True`, a name outside `[A-Za-z0-9_-]{1,64}` (the
grammar LLM tool-calling APIs enforce) raises, positional-only parameters
raise, and `*args`/`**kwargs` are left out of the schema.

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

Exact URIs win over templates; among templates the longest literal prefix
wins. Templates are reported via `resources/templates/list`. Placeholders must
be plain identifiers — RFC 6570 operators (`{?q}`, `{+path}`) and duplicate
names are rejected at registration — and matching is linear in the URI length.
`int` and `bool` parameters are coerced strictly (`+12`, ` 12 `, `banana` are
`-32602`, not silently `12` or `False`). A handler returning `bytes` is
delivered as a base64 `blob`.

Prompt arguments arrive as strings and are coerced to the handler's `int`,
`float`, or `bool` hints; `Principal` and `Context` parameters are injected
exactly as for tools, and docstring `Args:` become argument descriptions.

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
Both annotations work on tools, resources, and prompts, on plain functions,
bound methods, callable instances, and `functools.partial`s, and through
`Optional`, `Union`, `Annotated`, and PEP 695 `type` aliases. Detection
survives an otherwise-unresolvable signature (PEP 563 / 649) as long as
`micromcp.Principal` itself is importable at runtime; an annotation that merely
reads `Principal` but does not resolve to this class — your own ACL type under
a `TYPE_CHECKING` import, say — or a subclass of it, is refused at registration
rather than guessed at, because guessing either way is an auth bug.

Declaring `ctx: Context` is what makes a tool **stream**: on `ASGIServer` the
call is answered with a request-scoped SSE stream carrying
`notifications/progress` and `notifications/message` frames, then the final
JSON-RPC response. Client hang-up cancels the handler task.

Behaviors worth knowing:

- Streaming needs the client's `Accept` to admit `text/event-stream`; otherwise
  the call is answered with plain JSON and notifications are dropped.
  Validation, Origin, and authentication all run before the stream is
  committed, so a rejection keeps its real HTTP status.
- The frame queue is bounded, so a chatty handler paces itself against the
  socket instead of growing memory, and always reaches a cancellation point.
  Cancellation is cooperative: an `async` handler that swallows
  `CancelledError` keeps running and is logged, and a `def` handler cannot be
  interrupted at all — its thread runs until it returns (logged), so long sync
  loops should poll `ctx.cancelled`. A `: keepalive` comment goes out after 15 s of
  silence so proxies keep a long-running call open. Notification payloads must be JSON-serializable; a bad
  one raises inside the handler rather than tearing the stream down.
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
                 allowed_origins={"https://app.example.com"},
                 allowed_hosts={"mcp.example.com"},     # DNS-rebinding guard
                 path="/mcp",                           # 404 elsewhere
                 max_body=1_000_000,                    # bytes; default 4 MiB
                 workers=64,                            # handler threads; default 32
                 timeout=120,                           # WSGI request cap, seconds
                 stream_budget=8_000_000,               # notification bytes per call
                 keepalive=15, cancel_grace=2,          # SSE tunables, seconds
                 queue_size=64, offload_bytes=65536)    # frames buffered; parse-off-loop size
```

`authenticate` may be `async def`; a sync one runs on a separate small pool,
so it may block on a token store without competing with handlers. Guards run
inline and must be cheap. Handler threads are daemons and the pool knows how
busy it is: when all `workers` are occupied a sync call is refused with `503`
rather than queued behind threads that may never return (a `def` handler
cannot be interrupted, so poll `ctx.cancelled` in long loops). Both pools
rebuild themselves after `fork()`. On WSGI a request that outlives `timeout`
fails with `500` instead of hanging a worker; `timeout=None` or `0` disables
the cap. `stream_budget` caps the notification bytes one streaming call may
emit — the backstop for servers such as daphne whose `send()` never blocks,
where the bounded queue alone cannot apply backpressure.

`allowed_origins` also drives CORS: preflights from a listed Origin get `204`
with `Access-Control-Allow-*`, other Origins (including an empty one) get
`403`, and no Origin header means a non-browser client. `allowed_hosts` is off
when empty. `Content-Type` must be `application/json` when present.

Guards are predicates over the principal only — they cannot see call arguments,
so per-record authorization belongs inside the handler. They apply to tools,
resources, and prompts alike: a guarded tool call is an in-band `isError`; a
guarded resource or prompt the caller may not use is indistinguishable from one
that does not exist (`404` / `-32601`), and listings show only what the caller
may use. A guard that raises denies.

## Mounting

```python
# Flask, alongside ordinary routes
from werkzeug.middleware.dispatcher import DispatcherMiddleware
combined = DispatcherMiddleware(flask_app, {"/mcp": Server(mcp)})

# Starlette / FastAPI, native
from starlette.routing import Mount
routes = [Mount("/mcp", app=ASGIServer(mcp))]

# Django (adapters live in micromcp.contrib.django; also re-exported from micromcp)
from micromcp.contrib.django import django_view, django_async_view
urlpatterns = [path("mcp", django_async_view(ASGIServer(mcp)))]
```

Mount at the exact path clients will use: a framework `Mount("/mcp")` typically
307-redirects `/mcp` to `/mcp/`, and some clients (`urllib`, not the `mcp` SDK)
will not replay a POST across a redirect. `django_async_view` streams
Context tools as a `StreamingHttpResponse` of SSE frames; `django_view` (sync)
returns plain JSON.

## Scope

Implemented: `server/discover`, `tools/list`, `tools/call`, `resources/list`,
`resources/read`, `resources/templates/list`, `prompts/list`, `prompts/get`,
plus progress and logging notifications.

Deliberately not implemented: the initialize-handshake era (`2024-11-05` through
`2025-11-25`), sessions, resumable streams, `subscriptions/listen`,
`completion/complete`, MRTR/elicitation, pagination cursors, and OAuth.

Requests without an `id` (JSON-RPC notifications) pass the Origin, Host, and
Content-Type checks, are acknowledged with `202`, and are never dispatched. Bodies that are not a single JSON-RPC object, including
batch arrays, are `400` / `-32600`; a repeated `Mcp-*` routing header is
`-32020` (`Mcp-Method` is compared verbatim; only `Mcp-Name` carries the
base64 sentinel, decoded exactly as the SDK does); any pagination `cursor` is `-32602` because this server never issues
one. Server identity travels in `result._meta` on every result, as the 2026
revision expects. Handler exceptions outside `tools/call` are logged
server-side and answered with a constant `-32603` message.

**Client compatibility.** This server speaks only `2026-07-28`. It works with
the Python `mcp` SDK 2.x and FastMCP 4.x. The TypeScript SDK tops out at
`2025-11-25` as of this writing, so TS-based clients — Claude Desktop, Cursor,
Zed, MCP Inspector — cannot yet connect. They are refused with `-32022` and a
`data.supported` / `data.requested` pair naming this revision, which is what a
dual-era client parses as "modern peer, renegotiate", so they will work once
their SDK ships 2026 support.

OAuth is the boundary where rolling your own stops being sensible. Bearer tokens
against your own store are fine; being an OAuth 2.1 authorization server (RFC
9728 / 8414 / 7591, PKCE) is not 600 lines and is not code to hand-roll.

## Tests

```
pip install -e . --group test            # or: uv pip install -e . --group test
pytest                                   # all nine suites, in their own processes
pytest -m "unit or conform"              # no servers, no optional clients
pytest -m "server or interop"            # uvicorn/waitress/gunicorn + the mcp client
```

The suites under `tests/` are self-contained scripts that print `PASS`/`FAIL`
lines and can be run directly (`cd tests && python test_micromcp.py`);
`test_scripts.py` is the pytest entry point that runs each in its own process
on ephemeral ports. Ports and servers are never shared between suites.

```
test_micromcp.py    29 unit assertions, stdlib only
conform.py          11 checks against the strict mcp-types wire schema
interop.py          end-to-end with the official mcp client, 9 assertions
test_asgi.py        native ASGI under uvicorn and mounted in Starlette
test_progress.py    24 SSE checks: ordering, cancellation, WSGI degradation
harnesses.py        wsgiref, Flask, Starlette, waitress, gunicorn
ergonomics.py       API tour, 12 assertions
test_hardening.py   212 regression checks from three adversarial reviews
test_defender.py    60 checks from the defensive audit (mutation-derived)
```

`conform.py` validates every result and notification against the official
per-version wire schema from `mcp-types` (MIT, 6 packages). It is the cheapest
and most precise check: it names the failing field, and it is versioned against
the spec, so bumping `mcp-types` tells you exactly what a future revision breaks.
Run it on every commit.
