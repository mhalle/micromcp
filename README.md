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

## UI apps (MCP Apps / MCP-UI)

A widget is a static HTML page the host renders in a sandboxed iframe next to
the conversation. Declare it once as a `Widget` and attach it to the tools
that show it:

```python
from pathlib import Path
from micromcp import MCP, Widget, fragment

board = Widget("todos", title="Todos", scripts=[Path("htmx.min.js")], body="""
    <div id="app" hx-post="tool:todo_list" hx-trigger="mcp:ready" hx-target="#app"></div>""")

@mcp.tool(widget=board, read_only=True)       # the model calls this; the host shows the widget
def show_todos() -> str:
    return f"{todos.open_count()} open todos"

@mcp.tool(visibility="app")                   # the widget calls this; the model never sees it
def todo_list():
    return fragment(render(todos))            # HTML for the widget; escape what you interpolate
```

`Widget(name, ...)` builds the page around `BRIDGE_JS` — or takes `html=` for
a complete document of your own — and the first tool that names it registers
it as `ui://<name>` with the MCP App MIME type and fills in the tool's
`_meta` (`ui.resourceUri`, plus the legacy `ui/resourceUri` key). Assets in
`scripts=`, `modules=`, and `styles=` are source text, `pathlib.Path`s
(inlined), or https URLs (loaded; their origins are declared in the
resource's `_meta.ui.csp.resourceDomains` for you). Claude enforces that
declaration: on 2026-09-14 htmx loaded from jsdelivr in a `Widget`, and the
same page without the declaration was blocked (`script-src-elem`), exactly
as in the dev host. `imports=` writes an import map, so modules can
`import ... from "three"` (its origins are declared the same way; see
`examples/mcp_app_3d.py`, a three.js scene the model builds with tools and the
user selects in). `csp=` adds origins, `border=` sets `prefersBorder`,
`route=` and `fetch=` are covered below.

Under the hood a widget is a resource and a pointer to it, and both can be
written by hand:

```python
@mcp.resource("ui://crash-widget",         # mime defaults to text/html;profile=mcp-app
              meta={"ui": {"prefersBorder": True}})
def crash_widget() -> str:
    return HTML                            # static: no guards, no templates, no user data

@mcp.tool(meta={"ui": {"resourceUri": "ui://crash-widget"}})   # published as the tool's _meta
def show_crashes(street: str) -> dict:
    by_year = lookup(street)
    if by_year is None:
        return result([{"type": "text", "text": f"no data for {street}"}], is_error=True)
    return result([{"type": "text", "text": f"Crashes on {street}"}],
                  structured={"street": street, "by_year": by_year})
```

`meta=` on a tool, resource, template, or prompt is validated at registration
(a dict, JSON-serializable, valid `_meta` keys, no reserved
`io.modelcontextprotocol/` prefix) and published as its `_meta`; a resource's
meta also rides on its read contents. `result(content, structured=,
is_error=, meta=)` is the explicit way to return a finished tool result; its
blocks are checked against the five content-block types before they leave,
and its `_meta` is merged under the server's identity stamp. A plain dict is
always data and is wrapped as JSON text, so client-derived data can never be
mistaken for a result. `embedded_resource(uri, text=|blob=)` builds the block
that MCP-UI-style hosts render from the result itself; MCP Apps hosts instead
load the widget from the `ui://` resource the tool's `_meta` names. A declared
`outputSchema` still requires `structured`.

Widgets must be static. Hosts fetch a `ui://` resource under their own
identity, with none of the end user's credentials, and cache it per
connector: a guarded widget is simply never rendered, and a templated one
would interpolate URI text into HTML the host runs. Registration warns about
both. Every piece of per-user data belongs in the guarded tool's result, which
the host delivers to the widget as `ui/notifications/tool-result`. Inside a
tool, `ctx.client_capabilities` exposes what the client declared, so a handler
can check for `extensions["io.modelcontextprotocol/ui"]` and return a
text-only result to hosts without UI. A tool's `_meta.ui.visibility` is
passed through for the host to honor; `guards=` remains the only server-side
enforcement.

Verified 2026-09-14 in the Claude desktop chat via a custom connector
(`examples/mcp_app_modal.py`, deployed on Modal), twice: first with an early
widget, then with the current one, which completes the 2026-01-26 handshake,
answers `ping` and `ui/resource-teardown`, renders only host-delivered
results, and reports its size from a `ResizeObserver`. On a fresh connector
the host prefetches the `ui://` widget before its first `tools/call`, calls
the tool, delivers the result to the iframe as `ui/notifications/tool-result`,
and renders it. Two host behaviors worth knowing: the connector validator negotiates by
sending `initialize`, taking the `-32022` refusal, and retrying with
`server/discover`; and the host caches a connector's widget after its first
fetch, so a changed widget needs a new connector identity to be picked up.
The reference servers still emit the deprecated flat `ui/resourceUri` key
alongside `ui.resourceUri`; the example does the same until hosts drop it.

### Hypermedia widgets: htmx, fixi, Django views

A widget can be a static page whose HTML the server renders: every click
becomes a `tools/call` the host proxies to this server, and the tool answers
with an HTML fragment that is swapped in. The widget has no app logic and no
network access; all state stays on the server.

```python
@mcp.tool(visibility="app")                     # hidden from the model, callable by the widget
def todo_add(text: str = ""):
    todos.add(text)
    return fragment(render(todos))
```

The bridge completes the MCP Apps handshake and gives `fetch()`-shaped
requests a tool-call transport (`mcp.fetch`): `tool:name?a=1` calls that tool
with the query, form, or JSON body as arguments. It is wired into htmx 4
(`ctx.fetch`) and fixi (`fx:config`); `Widget(fetch="global")` also replaces
`window.fetch`, for libraries without a hook (Datastar). With htmx, call
tools with `hx-post`: htmx rewrites a GET URL to its path, dropping `tool:`.
Any other URL goes to the widget's `route=` tool — which is how existing
Django views serve a widget:

```python
from micromcp import Widget, django_routes

board = Widget("todos", scripts=[Path("htmx.min.js")],
               route=django_routes(mcp, prefixes=["/app/"]),    # registers "django_http"
               body='<div id="app" hx-get="/app/todos/" hx-trigger="mcp:ready" '
                    'hx-target="#app"></div>')
```

`django_routes` runs each request through Django's full handler in process
(URL resolver, middleware, views, templates), follows same-host redirects,
forwards `HX-*`/`FX-*`/`Datastar-*` headers so `django-htmx` works, and serves
only paths under `prefixes` (after decoding and normalization). The request
carries no cookies: the MCP principal is `request.mcp_principal`, and CSRF is
off because nothing ambient authenticates the request. `fragment()` results
from app-only tools reach only the widget, never the model's context.

Verified 2026-09-14 in the Claude chat with nine self-testing widgets
(`examples/toolkit_lab.py`): fixi, htmx 4, htmx 4 + Django views, htmx 4 +
Alpine's CSP build (state survives `innerMorph`), Datastar, and hx-live all
work. Claude's widget policy allows `eval` and inline-script injection; the
MCP Apps spec's default does not, and under it stock hx-live and stock
Datastar fail while everything above in eval-free form (Datastar's CSP mode,
hx-live with an injection extension) passes. htmx's `hx-csp` extension does
not work over this transport: it strips every element that arrives in a
fragment, because a synthesized `Response` has no URL to verify.

### Telling the model what the user sees

A widget is opaque to the model, but it can report to it:

```python
return fragment(render(todos), context={
    "text": f"Todo widget, current state: {n_open} open, {n_done} done.",
    "data": {"items": [...]}})
```

The bridge forwards `context` as `ui/update-model-context`; the model sees it
on its next turn, and no turn is started. In the browser, `mcp.setContext(text,
data)` does the same for client-side state (debounced), and `mcp.say(text)` or
`<button data-mcp-say="...">` posts a message into the chat as the user,
which does start a turn. From Django, `set_mcp_context(response, text, data)`
on any response in a redirect chain has the same effect.

What Claude did with them on 2026-09-14: each update **replaces** the
widget's previous one (as the spec says), while separate widgets keep separate
contexts — so send a snapshot of the view, not a change; the model saw only
the **text** of an update, never `structuredContent`, so the bridge also sends
`data` as a labeled JSON text block; and `ui/message` needed `content` as an
array of blocks (the spec shows one block), so the bridge tries that first.
None of this happens in the Claude Code desktop tab, which does not render MCP
Apps. Treat context as data: anything users wrote reaches the model.

### Channels: pushing to open widgets

A widget cannot open a socket, and the host delivers a tool result only to the
widget that call opened, so changes made elsewhere — by the model, by another
user — need a way in. A channel is that way in, with the WebSocket API on the
widget's side:

```python
scene = mcp.channel("scene")

@scene.on_connect
def joined(conn):                        # conn.params: the query the widget connected with
    conn.send_json(snapshot())

@scene.on_message
def received(conn, text): ...            # text frames, in order

@mcp.tool
def scene_add(...) -> str:
    ...
    scene.broadcast_json(snapshot())     # from any tool, sync or async, any thread
```

```js
const ws = mcp.channel("scene");         // WebSocket-compatible: onmessage, send, close, readyState
ws.onmessage = e => render(JSON.parse(e.data));

new SomeClient("mcp:scene?partial=1", {webSocket: mcp.WebSocket});   // for libraries that take one
```

Underneath are four app-only tools shared by every channel: `channel_open`,
`channel_send`, `channel_recv`, and `channel_close`. `channel_recv` is a long
poll: it returns as soon as a frame is queued, or after `wait` seconds (20 by
default), so an idle widget makes one request per 20 s and a change arrives at
once. A connection is bound to the principal that opened it (`guards=` run on
open), is dropped after `idle` seconds without a request (90), and is closed if
it falls `max_queue` frames behind (1000). Callbacks may be sync (run on a
worker thread) or async, and `send`/`broadcast` are safe from any thread.

Mind the host's widget cache when you change a widget. Claude keeps serving a
connector's widget as first fetched, so after a redeploy an old widget still
calls the tools it knew: remove one and it fails with "no such tool" (seen on
2026-09-14 when the 3D example moved from polling to a channel). Keep app-only
tools an older widget uses working, and reach the new widget through a new
connector (a new path or host).

Verified 2026-09-14 in the Claude chat: the host held `channel_recv`-style
requests open for the full 20 s, and a model edit reached an open widget as
soon as it was made. That test used atomdoc, a server-authoritative document
library whose Python transport interface maps onto a channel and whose
TypeScript client takes `mcp.WebSocket` unchanged; the same pattern drives
`examples/mcp_app_3d.py`.

### The dev host

`examples/devhost.html` is a stand-in MCP Apps host for development: it reads
a tool's `ui://` widget, renders it in a sandboxed iframe under the policy a
host derives from the widget's `_meta.ui.csp` (the spec's default plus the
declared origins; `?csp=eval` adds `unsafe-eval`), completes the handshake,
proxies the widget's `tools/call` for tools whose visibility includes `app`,
and shows what the model would receive. `python examples/mcp_app_hypermedia.py`
serves it at `http://127.0.0.1:8770/devhost?mcp=/mcp` next to the demos (the
todo widget with tools and with Django views, the toolkit lab, and the context
counter).

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
resources, and prompts alike: a guarded tool, resource, or prompt the caller
may not use is indistinguishable from one that does not exist (`404` /
`-32601`), and listings show only what the caller may use. A guard that
raises denies; a guard that raises `Unauthorized` (or another `Error`) chooses
the answer when the entry is invoked, and hides it in listings.

**OAuth handoff.** Being an authorization server is out of scope, but the
handoff to one is not. Raise `Unauthorized` from `authenticate` (or from a
handler) and the answer is `401` with a `WWW-Authenticate: Bearer` challenge,
which is what makes an OAuth-capable client (Claude.ai, the Inspector, the
SDKs) start its flow. Pass `resource_metadata=` to serve the RFC 9728
document at `/.well-known/oauth-protected-resource` (and at the
path-suffixed form when `path=` is set); the challenge then names that
document's URL by default, derived at construction from the document's own
`resource` (its origin plus the well-known path plus its path), never from
`Host` or a proxy header, and the document is served at exactly that path
(`server.well_known_path`), so the challenge always names something this
server answers. If an outer router mounts the server under a prefix, requests
for the well-known path never reach it: give the outer app a route for it, or
run the server at the root with `path=`. A challenge with no URL at all is logged once as a
warning: MCP clients cannot start OAuth from a bare `Bearer`. Guards may
raise `Unauthorized` too, and do so before any byte is committed. A tool that
raises it after its SSE stream has opened cannot change the status any more;
the error travels in-band, which is why authentication and guards are the
place for it.

```python
from micromcp import Unauthorized

def authenticate(headers):
    token = headers.get("authorization", "").removeprefix("Bearer ")
    if not token:
        raise Unauthorized(scope="read")                 # start the OAuth flow
    principal = store.lookup(token)
    if principal is None:
        raise Unauthorized.invalid()                     # error="invalid_token"
    return principal

app = ASGIServer(mcp, authenticate=authenticate, path="/mcp",
                 resource_metadata={"resource": "https://mcp.example.com/mcp",
                                    "authorization_servers": ["https://auth.example.com"],
                                    "scopes_supported": ["read"],
                                    "bearer_methods_supported": ["header"]})
```

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

## Constants

The names above are the everyday API (and `micromcp.__all__`). Protocol
constants and the defaults behind the constructor's tunables are importable
from `micromcp` too, for tests, proxies, and clients that speak the wire
format:

| Group | Names |
|---|---|
| Protocol | `PROTOCOL` (`"2026-07-28"`), `LEGACY_VERSIONS`, `WELL_KNOWN` |
| `_meta` keys | `META_VER`, `META_CAPS`, `META_CLIENT`, `META_SERVER`, `META_SUB` |
| Error codes | `PARSE_ERROR`, `INVALID_REQUEST`, `METHOD_NOT_FOUND`, `INVALID_PARAMS`, `INTERNAL_ERROR`, `HEADER_MISMATCH`, `UNSUPPORTED_VERSION`, `UNAUTHORIZED` |
| Methods and headers | `HANDSHAKE_METHODS`, `LIST_METHODS`, `ROUTING_HEADERS`, `SINGLETON_HEADERS`, `CORS_HEADERS` |
| Tunable defaults | `MAX_BODY`, `MAX_URI`, `MAX_DEPTH`, `OFFLOAD_BYTES`, `QUEUE_SIZE`, `STREAM_BUDGET`, `KEEPALIVE`, `CANCEL_GRACE`, `WORKERS` |

The tunables are defaults only: change them per server with the constructor
arguments shown under Auth, not by assigning to the module. `log` is the
`"micromcp"` logger (`logging.getLogger("micromcp")` is the same object).

## Scope

Implemented: `server/discover`, `tools/list`, `tools/call`, `resources/list`,
`resources/read`, `resources/templates/list`, `prompts/list`, `prompts/get`,
plus progress and logging notifications.

Deliberately not implemented: sessions, resumable streams, change
notifications (`subscriptions/listen` is accepted and closed gracefully),
`completion/complete`, MRTR/elicitation, pagination cursors, and being an
OAuth authorization server. The initialize-handshake era is refused by
default and served per request, without sessions, with `legacy="stateless"`.

Requests without an `id` (JSON-RPC notifications) pass the Origin, Host, and
Content-Type checks, are acknowledged with `202`, and are never dispatched. Bodies that are not a single JSON-RPC object, including
batch arrays, are `400` / `-32600`; a repeated `Mcp-*` routing header is
`-32020` (`Mcp-Method` is compared verbatim; only `Mcp-Name` carries the
base64 sentinel, decoded exactly as the SDK does), and on WSGI, which folds
duplicates into one comma-joined value, a comma in `MCP-Protocol-Version`,
`Mcp-Method`, `Authorization`, `Host`, or `Origin` is refused for the same
reason; any pagination `cursor` is `-32602` because this server never issues
one. Server identity travels in `result._meta` on every result, as the 2026
revision expects. Handler exceptions outside `tools/call` are logged
server-side and answered with a constant `-32603` message.

**Client compatibility.** This server speaks `2026-07-28`, and refuses
everything else unless told otherwise. Verified
against the Python `mcp` SDK 2.x, FastMCP 4.x, and Claude Code 2.1.258, whose
first request is a `server/discover` probe at `2026-07-28`; it then lists
prompts, resources, and tools, calls tools (a `Context` tool is answered with
an SSE stream carrying its progress and log notifications), and reads
resources. A client that only speaks the initialize-handshake era is refused
with `-32022` and a `data.supported` / `data.requested` pair, which is what a
dual-era client parses as "modern peer, renegotiate".

`subscriptions/listen` is accepted and closed gracefully: the server never
emits change notifications (every `listChanged` it advertises is false), so
it acknowledges with an empty honored set, answers the request with a
completion result carrying the subscription id, and closes the stream. That
matters for Go SDK 1.7 clients such as Crush, which open a listen stream
whenever the host registers a list-changed handler and treat any error as a
failed connection; Go SDK 1.8 gates the request on the advertised capability.

Which clients can reach a server that speaks only this revision, as of
2026-09-14: Claude.ai and Claude desktop connectors, Claude Code 2.1.232+,
GitHub Copilot CLI 1.0.81+, Goose 1.50+, Crush 0.88+, and anything built on
the Python `mcp` 2.x, Go 1.7+, C# 2.x, or Ruby 1.2+ SDK clients, whose
default is to probe `server/discover` first. TypeScript SDK 2.x and Rust
`rmcp` 3.x can, but only when the host opts in (Codex CLI needs its
`mcp_2026_07_28` feature flag; Mastra needs `protocolVersion: 'auto'`).
Hosts still on TypeScript SDK 1.x (VS Code, Cursor as far as is known,
Gemini CLI, Cline, LibreChat, Open WebUI), Zed, ChatGPT connectors, and the
Java, Kotlin, and Swift SDKs cannot, unless the server opts in below.

**Legacy clients, opt-in.** `Server(mcp, legacy="stateless")` (and the same
on `ASGIServer`) serves 2025-era clients the way the official SDKs' stateless
legacy mode does: per request, with no sessions. A request without the
per-request envelope is answered on the 2025 ladder: `initialize` returns the
capabilities and server identity from the registry every time (echoing a
requested `2025-11-25` or `2025-06-18`; anything else, including
`2025-03-26`, whose batch arrays this server refuses, gets `2025-11-25`), the
initialized notification is `202`, `ping` is `{}`, results carry no
`resultType`/`ttlMs`/`cacheScope`, every response (results, errors, and
SSE streams) names the era served in `MCP-Protocol-Version`, a `-32601`
travels under `200` because the 2025-era SDK clients read `404` as a
terminated session, and GET and DELETE stay `405`. The envelope and the
`Mcp-Method`/`Mcp-Name` routing headers are not required of such requests,
but when a legacy request does send them they must agree with the body. Note
for gateways that authorize on `Mcp-Name`: a claim-less request carries no
such header at all, so a gateway must deny claim-less POSTs if it relies on
the header rather than the body. A `Context` tool still streams SSE with its
progress notifications, but only to a client whose `Accept` names
`text/event-stream` explicitly. A request that names `2026-07-28` in its
header or envelope (even with a null value) always takes the modern ladder,
and `server/discover` then advertises both eras. Verified with the
Python `mcp` 2.x client in both `mode="legacy"` and `mode="auto"` (which
still picks the modern era), the TypeScript SDK 2.0.0 client in its default
legacy mode, and the Go SDK 1.7.0 client. The default stays refuse: turning
the fallback on is a one-word decision, and the README should not make it
for you.

OAuth is the boundary where rolling your own stops being sensible. Bearer tokens
against your own store, the `401` challenge, and the RFC 9728 document are
here; being an OAuth 2.1 authorization server (RFC 8414 / 7591, PKCE) is not
600 lines and is not code to hand-roll.

## Tests

```
pip install -e . --group test            # or: uv pip install -e . --group test
pytest                                   # all ten suites, in their own processes
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
test_progress.py    40 SSE checks: ordering, cancellation, subscriptions/listen, WSGI degradation
test_legacy.py      legacy="stateless" serving, OAuth handoff, two review rounds, both transports
harnesses.py        wsgiref, Flask, Starlette, waitress, gunicorn
ergonomics.py       API tour, 12 assertions
test_hardening.py   247 regression checks from four adversarial reviews + UI apps
test_defender.py    60 checks from the defensive audit (mutation-derived)
```

`conform.py` validates every result and notification against the official
per-version wire schema from `mcp-types` (MIT, 6 packages). It is the cheapest
and most precise check: it names the failing field, and it is versioned against
the spec, so bumping `mcp-types` tells you exactly what a future revision breaks.
Run it on every commit.
