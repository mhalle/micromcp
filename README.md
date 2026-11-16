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

A tool whose guard denies the caller is left out of `tools/list` and answers a
call exactly as a missing tool does (see Auth below). A handler that raises
returns `isError: true` in-band rather than an HTTP error, which is what lets a
model reason about it.

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

### Images and other content

A tool that returns anything other than JSON-able data builds its content
blocks with `result()` and the helpers named after the spec's block types:

```python
from micromcp import result, image_content, text_content, resource_link

@mcp.tool
def crash_map(street: str) -> dict:
    """Map of crashes on a street."""
    png = render_map(street)
    return result([
        image_content(png, "image/png", annotations={"audience": ["user"]}),
        text_content(f"{street}: 11 crashes, mostly at the Elm St intersection",
                     annotations={"audience": ["assistant"]}),
        resource_link(f"crash://street/{street}", f"{street} crashes", mime_type="text/csv"),
    ])
```

`image_content` and `audio_content` take raw bytes and base64-encode them;
the MIME type must be `image/*` or `audio/*`. `resource_link` points at a
resource instead of inlining it, and `embedded_resource` inlines one. A tool
that returns bare `bytes` is an in-band `isError` naming these helpers, since
the MIME type cannot be guessed. Every helper takes `annotations`, the spec's
hints for the client: `audience` (`["user"]`, `["assistant"]`, or both),
`priority` (0 to 1), and `lastModified` (RFC 3339 with an offset, such as
`2026-01-12T15:00:58Z`, or a timezone-aware `datetime`). Hosts
may ignore them. Unknown keys are refused, and hand-written blocks passed to
`result()` get the same checks. The helpers also work inside prompt messages:
`{"role": "user", "content": image_content(png, "image/png")}`.

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
wins. An entry the caller's guards hide is skipped, so the next match serves
the URI exactly as if the hidden one were not registered. Templates are reported via `resources/templates/list`. Placeholders must
be plain identifiers — RFC 6570 operators (`{?q}`, `{+path}`) and duplicate
names are rejected at registration — and matching is linear in the URI length.
`int` and `bool` parameters are coerced strictly (`+12`, ` 12 `, `banana` are
`-32602`, not silently `12` or `False`). A handler returning `bytes` is
delivered as a base64 `blob`. `annotations=` on `@mcp.resource` (audience,
priority, lastModified) is published in `resources/list` and
`resources/templates/list`.

Prompt arguments arrive as strings and are coerced to the handler's `int`,
`float`, or `bool` hints; `Principal` and `Context` parameters are injected
exactly as for tools, and docstring `Args:` become argument descriptions.

## Server instructions

```python
mcp = MCP("newton-civic", "1.0.0",
          instructions="Look up a street with find_street before asking for its crashes.")
```

Guidance for the model on using the server as a whole; clients may put it in
a system prompt. It goes out in the `server/discover` result, and in the
`initialize` result for 2025-era clients under `legacy="stateless"`. Keep it to
what tool descriptions can't say on their own, such as how the tools fit
together. It is one string for every caller: discover is answered before any
tool is called and may be cached by the client.

## Server identity and Server Cards

```python
mcp = MCP("newton-civic", "1.0.0",
          title="Newton civic data",
          description="Crash and permit data for Newton, MA",
          website_url="https://example.com/newton",
          icons=[{"src": "https://example.com/newton.png", "mimeType": "image/png",
                  "sizes": ["48x48"]}])

@mcp.tool(icons=[{"src": "https://example.com/crash.svg", "mimeType": "image/svg+xml",
                  "sizes": ["any"]}])
def crash_count(street: str) -> dict: ...
```

These are how hosts present a server to people: a display name, one sentence on
what it is for, a link, and icons. They go out in `serverInfo` beside `name` and
`version` (in every result's `_meta`, and in `initialize` under
`legacy="stateless"`), and are left out when unset. An icon's `src` is an
http(s) URL without user info or a base64 image `data:` URI
(`data:image/png;base64,...`); `mimeType` (an `image/*` type), `sizes` (`"48x48"`, `"any"`),
and `theme` (`"light"` or `"dark"`) are optional. `icons=` also works on
`@mcp.tool`, `@mcp.resource` (templates included), and `@mcp.prompt`. All of it
is checked when the server or entry is defined: a misspelled key, a size like
`"48"`, or a `javascript:` URL raises there instead of being ignored by a host.

A **Server Card** (the `io.modelcontextprotocol/server-card` extension, Beta)
is a public JSON document that tells a client where and how to connect before
it connects, so a registry or an IDE can index and configure the server:

```python
app = ASGIServer(mcp, path="/mcp",
                 server_card={"url": "https://mcp.example.com/mcp",       # this endpoint, public
                              "name": "com.example/newton-civic",         # reverse-DNS namespace/name
                              "repository": {"url": "https://github.com/example/newton",
                                             "source": "github"}})        # optional
```

The card is served at `<url>/server-card` (here `/mcp/server-card`) as
`application/mcp-server-card+json`. Everything but `url`, `name`, and
`repository` comes from the registry, so the card cannot disagree with what
the server says once connected: version, title, description, website and
icons from `MCP(...)`, and `supportedProtocolVersions` from what the server
serves (both eras under `legacy="stateless"`). `MCP(description=...)` is
required, at most 100 characters, and the version must be one version, not a
range. The card is answered before authentication and guards, since it holds
public metadata only; the `allowed_hosts` check still applies. It is readable
from any origin (`Access-Control-Allow-Origin: *`), cached for an hour, and
carries an `ETag`, so a client revalidating with `If-None-Match` gets an empty
`304`. As with `resource_metadata=`, `url` is the operator's configuration,
never derived from `Host` or a proxy header; a `url` whose path differs from
`path=` is logged as a warning, since the card is served under the `url`'s path.
Primitives (tools, resources, prompts) are deliberately not in a card: clients
still list them after connecting. Domain-wide discovery, an AI Catalog at
`/.well-known/ai-catalog.json` that links to cards, belongs to the site rather
than to one MCP server, and is not served here.

## Skills

```python
mcp.skill(Path("skills/pdf-processing"))              # -> "skill://pdf-processing/SKILL.md"
mcp.skill({"SKILL.md": SKILL_TEXT, "examples/email.md": EMAIL},
          prefix="acme/billing", guards=[is_staff])     # -> "skill://acme/billing/refunds/SKILL.md"
```

An [Agent Skill](https://agentskills.io/specification) is a directory of
workflow instructions for the model, a `SKILL.md` plus any references,
scripts, or assets. `mcp.skill()` serves one under the Skills over MCP
extension (`io.modelcontextprotocol/skills`, Beta): registering one declares
the extension in `capabilities.extensions` (with `directoryRead`), and the
server answers `skills/list` and `skills/get` with each skill's verbatim
frontmatter and a complete manifest of its files (a SHA-256 digest and size
for each), `resources/read` for the files themselves, and
`resources/directory/read` for any directory level under `skill://`. Hosts
use the manifest to verify what they read and to tie a user's approval to the
exact content, so files are read once, at registration: every request serves
those bytes, and a skill changed on disk needs `replace=True` or a restart.
Dot-files and `__pycache__` are left out; a symlink leading out of the
directory, or to one of those, is refused, and so are two files whose names
differ only in case. The 512-file and 16 MiB limits are checked as files are
read, so an oversized file is refused without being read whole.

Registration checks what a host will check. The frontmatter needs a `name`
(lowercase letters, digits, and single hyphens, matching the directory) and a
`description`; `metadata` values must be strings; file names are limited to
letters, digits, `.`, `_`, `~`, and `-`, so each URI is its own spelling; a
skill holds at most 512 files and 16 MiB. Because hosts compare the
frontmatter against their own YAML parser's reading of `SKILL.md`, it is read
by a strict subset — `key: value` lines, a one-level `metadata` map, quoted
strings, and `|`/`>` blocks — that refuses everything else and every plain
value YAML parsers disagree on (`yes`, `on`, `012`, `1e3`, `1_000`, `2024-01-01`):
quote those. Keys must be strings to YAML too (`true:` and `1:` are refused), and
so is any character a YAML parser reads as a line break or does not allow (a
lone CR, NEL, U+2028, control characters), since a host parsing the same bytes
would see different lines. The frontmatter is capped at 64 KiB. `guards=` hide a skill and its files the way they hide resources. Its
`SKILL.md` appears in `resources/list` with the frontmatter's name and
description; the other files are readable but not listed. `prefix=` groups
skills under a path (`acme/billing`); a skill nested inside another may be
registered as well, provided its files are byte-identical.

**Other extensions.** `MCP(..., extensions={...})` declares extensions this
server supports, published as `capabilities.extensions` in `server/discover`
and, under `legacy="stateless"`, in `initialize`:

```python
mcp = MCP("newton-civic", "1.0.0", extensions={"io.modelcontextprotocol/ui": {}})
```

MCP Apps widgets do not declare `io.modelcontextprotocol/ui` automatically:
the MCP Apps specification makes the server's declaration optional, and the
widget verification in Claude above ran without it. Pass it as above if you
want it advertised.

## UI apps (MCP Apps / MCP-UI)

A widget is a static HTML page the host renders in a sandboxed iframe next to
the conversation. The core provides the protocol pieces below;
`micromcp.apps`, an experimental subpackage ([docs/apps.md](docs/apps.md)),
builds on them with `Widget`, HTML fragments, hypermedia over tool calls (htmx,
fixi, Django views), model context, WebSocket-style channels to open widgets,
and a development host. `import micromcp` does not load it, and it is not part
of the single-file bundle.

A widget is a resource and a pointer to it:

```python
@mcp.resource("ui://crash-widget",         # mime defaults to text/html;profile=mcp-app
              meta={"ui": {"prefersBorder": True}})
def crash_widget() -> str:
    return HTML                            # static: no guards, no templates, no user data

@mcp.tool(meta={"ui": {"resourceUri": "ui://crash-widget"}})   # published as the tool's _meta
def show_crashes(street: str) -> dict:
    by_year = lookup(street)
    if by_year is None:
        return result([text_content(f"no data for {street}")], is_error=True)
    return result([text_content(f"Crashes on {street}")],
                  structured={"street": street, "by_year": by_year})
```

`meta=` on a tool, resource, template, or prompt is validated at registration
(a dict, JSON-serializable, valid `_meta` keys, no reserved
`io.modelcontextprotocol/` prefix) and published as its `_meta`; a resource's
meta also rides on its read contents. `result(content, structured=,
is_error=, meta=)` is the explicit way to return a finished tool result; its
blocks are checked against the five content-block types before they leave
(as strictly as the helpers build them), and its `_meta` is merged under the
server's identity stamp. A result sets `content`, `structuredContent`,
`isError`, and `_meta` only; the protocol fields (`resultType`, `ttlMs`,
`cacheScope`) are the server's. A malformed one becomes an in-band `isError`. A plain dict is
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

A tool meant for widgets rather than the model says so:

```python
@mcp.tool(visibility="app")        # hidden from the model by the host, callable by widgets
def crash_rows(street: str) -> dict: ...
```

`visibility=` (`"model"`, `"app"`, or both, the spec's default) is published as
`_meta.ui.visibility` and merged with any `ui` object in `meta=`. An app-only
tool is still an ordinary tool to any other client, so guard it.

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
  committed, so a rejection keeps its real HTTP status. The response head
  also waits up to a second for the handler's first notification, so an
  `Error` a tool raises at its start, such as a `403` from a scope check,
  keeps its status and headers too; once the stream is open, errors travel
  in-band.
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
  result is still delivered normally.
- Log messages require the client to name a level in
  `_meta["io.modelcontextprotocol/logLevel"]`, as the spec requires: without
  one, `ctx.info()` and the rest send nothing, and with one, only messages at
  or above it go out. An unknown level is `-32602` from the client and a
  `ValueError` from the handler. `ctx.log_level` is the level asked for, or
  `None`, so a handler can skip building a costly message. 2025-era requests
  under `legacy="stateless"` have no such field and get every message. The
  spec has deprecated logging (earliest removal 2027-07-28) in favor of the
  server's own logs or OpenTelemetry.
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
inline and must be cheap; a guard may be `async def` (or return any awaitable),
and its result is awaited before it is judged. Handler threads are daemons and the pool knows how
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
when empty; a listed name matches with or without a numeric port
(`mcp.example.com:8443`), and nothing else after a colon counts as one.
`Content-Type` must be `application/json` when present.

Guards are predicates over the principal only — they cannot see call arguments,
so per-record authorization belongs inside the handler. They apply to tools,
resources, and prompts alike: a guarded tool, resource, or prompt the caller
may not use is indistinguishable from one that does not exist (`400` /
`-32602`: `Unknown tool: <name>`, `Unknown prompt: <name>`, or `Resource not
found` with the URI in `data`, as 2026-07-28 specifies; `404`/`-32601` is kept
for methods the server does not implement), and listings show only what the
caller may use. A guard that
raises denies; a guard that raises `Unauthorized` (or another `Error`) chooses
the answer when the entry is invoked, and hides it in listings.

**OAuth handoff.** Being an authorization server is out of scope, but the
handoff to one is not. Raise `Unauthorized` from `authenticate` (or from a
handler) and the answer is `401` with a `WWW-Authenticate: Bearer` challenge,
which is what makes an OAuth-capable client (Claude.ai, the Inspector, the
SDKs) start its flow. `Unauthorized.insufficient_scope("files:write")` is the
`403` form with `error="insufficient_scope"`: the token is fine but lacks a
scope, and the client may step up to it. Pass `resource_metadata=` to serve the RFC 9728
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
raise `Unauthorized` too, and do so before any byte is committed; when
several guards on one entry raise `insufficient_scope`, the `403` names all
their scopes. A streaming tool may raise it at its start, before any
notification (the head waits up to a second for one); after that it cannot
change the status any more and the error travels in-band. A request refused for its protocol version (`-32022`) is answered
by `authenticate` first, so an SDK client that falls back to the 2025
handshake after a failed probe sees the `401` or `503`, not a version error.

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

**Tokens from an identity provider.** `micromcp.contrib.oauth` is the
resource server's side of MCP authorization for tokens that a provider such
as Auth0, WorkOS, Keycloak, or Okta issues (`pip install "micromcp[oauth]"`,
which adds PyJWT for the signature checks):

```python
from micromcp import MCP, ASGIServer, Principal
from micromcp.contrib.oauth import OAuth

auth = OAuth(issuer="https://auth.example.com/",           # exactly as the provider writes it
             resource="https://todos.example.com/mcp",     # this server: the tokens' audience
             scopes=["todos:read"])
app = ASGIServer(mcp, path="/mcp", authenticate=auth, resource_metadata=auth.metadata)

@mcp.tool(guards=[auth.requires("todos:write")])       # hidden without the scope; 403 if called
def todo_add(who: Principal, text: str) -> dict: ...
```

On the first token it fetches the provider's metadata from the URLs MCP
clients try, refusing a document whose `issuer` differs, then checks each
JWT's signature against the provider's published keys, its issuer, its
audience (`resource`, or `audience=`), and its expiry and not-before (with
30 seconds of clock-skew `leeway=`), with asymmetric algorithms only. The
principal is `{"sub", "client_id", "scopes", "claims"}`, a copy per request.
A missing or bad token is `401` with the challenge. A provider that is
unreachable, slower than `timeout=` (10 seconds), or answers something
unusable (an empty key set included) is `503`, so clients do not start a new
sign-in for an outage; after a failed discovery, requests answer `503` for
10 seconds before it is tried again. `auth.discover()` fetches the metadata
at startup instead, and logs a warning for what would stop MCP clients from
signing in: no PKCE `S256`, or neither Client ID Metadata Documents nor
dynamic client registration.

`OAuth` is an async `authenticate`, awaited on the event loop: a request that
needs nothing from the provider (no token, a cached key, a cached
introspection answer) never waits for a thread, and fetches from the
provider run one at a time per document on a few threads of their own. Keys
are refreshed every five minutes in the background, the old ones in use
meanwhile; a token naming an unknown key triggers a refresh at most every 30
seconds.

`auth.requires(...)` is a guard: a tool the token cannot use is left out of
listings, and a direct call is `403 insufficient_scope` naming every scope
the tool needs, which lets the client step up; several `requires()` on one
tool answer with one challenge naming all their scopes. `auth.check(who,
...)` does the same inside a handler, for a tool that should stay listed; in
a streaming (`Context`) tool, call it first thing, before any notification.
`implies=` declares a scope hierarchy (`{"todos:admin": ["todos:write"]}`).
`introspection=(client_id, secret)` validates opaque tokens at the
provider's introspection endpoint instead; answers must name this server in
`aud` and are reused for up to a minute, so a token revoked at the provider
keeps working that long. For development and tests,
`OAuth.static({"dev-token": {"sub": "me", "scope": "todos:write"}})` stands in
for a provider and yields the same principal.

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

**Other routes, no framework.** `fallback=` hands every path but the endpoint
to another app of the same kind, so one process serves `/mcp` and the plain
URLs its tools return: rendered images, downloads, reports.

```python
def files(environ, start_response):            # any WSGI app: here, the standard library's own
    blob = RENDERS.get(environ["PATH_INFO"].removeprefix("/r/"))
    if blob is None:
        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"not found"]
    start_response("200 OK", [("Content-Type", "image/png")])
    return [blob]

app = Server(mcp, path="/mcp", fallback=files,  # ASGIServer takes an ASGI app the same way
             allowed_hosts={"localhost", "127.0.0.1"})

from wsgiref.simple_server import make_server
make_server("127.0.0.1", 8000, app).serve_forever()
```

The fallback needs `path=`, since without it every path is the endpoint. It
gets the request as it came, body unread and with no MCP checks, so its own
answers, headers, and CORS are its own. The one guard that still applies is
`allowed_hosts`: a Host it does not admit, or a repeated Host, is refused before
the fallback is called. Set it whenever there is a fallback. The endpoint is
also shielded from DNS rebinding by its Origin check, but a rebinding page's
plain GET carries no Origin, so without `allowed_hosts` it can read whatever
the fallback serves; a server built that way logs a warning. The Server Card and the RFC 9728 document stay micromcp's.
On `ASGIServer`, WebSocket connections to other paths go to the fallback too
(one to the endpoint is still refused), and the lifespan stays micromcp's: the
fallback gets no startup or shutdown events. A WSGI app given to `ASGIServer`,
or an ASGI app given to `Server`, is refused at construction.

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
| Protocol | `PROTOCOL` (`"2026-07-28"`), `LEGACY_VERSIONS`, `WELL_KNOWN`, `SERVER_CARD`, `SERVER_CARD_MIME`, `SERVER_CARD_SCHEMA` |
| Skills | `SKILLS_EXT`, `DIRECTORY_MIME`, `SKILL_MAX_FILES`, `SKILL_MAX_BYTES` |
| `_meta` keys | `META_VER`, `META_CAPS`, `META_CLIENT`, `META_SERVER`, `META_SUB`, `META_LOG` (and its `LOG_LEVELS`) |
| Error codes | `PARSE_ERROR`, `INVALID_REQUEST`, `METHOD_NOT_FOUND`, `INVALID_PARAMS`, `INTERNAL_ERROR`, `HEADER_MISMATCH`, `UNSUPPORTED_VERSION`, `UNAUTHORIZED` |
| Methods and headers | `HANDSHAKE_METHODS`, `LIST_METHODS`, `ROUTING_HEADERS`, `SINGLETON_HEADERS`, `CORS_HEADERS` |
| Tunable defaults | `MAX_BODY`, `MAX_URI`, `MAX_DEPTH`, `OFFLOAD_BYTES`, `QUEUE_SIZE`, `STREAM_BUDGET`, `KEEPALIVE`, `CANCEL_GRACE`, `WORKERS` |

The tunables are defaults only: change them per server with the constructor
arguments shown under Auth, not by assigning to the module. `log` is the
`"micromcp"` logger (`logging.getLogger("micromcp")` is the same object).

## Scope

Implemented: `server/discover`, `tools/list`, `tools/call`, `resources/list`,
`resources/read`, `resources/templates/list`, `prompts/list`, `prompts/get`,
plus progress and logging notifications; with a skill registered, `skills/list`,
`skills/get`, and `resources/directory/read`.

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
terminated session, a missing resource is that era's `-32002` (unknown tools
and prompts are `-32602` in both eras), and GET and DELETE stay `405`. The envelope and the
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
conform.py          12 checks against the strict mcp-types wire schema
test_identity.py    83 checks: identity fields, icons, the Server Card on both transports
test_guards.py      30 checks: async guards, hidden streaming tools, path=, template shadowing
test_fallback.py    42 checks: fallback= on both transports, Host guard, WebSockets, lifespan
test_skills.py      154 checks: skills, directory reads, the frontmatter subset (against PyYAML)
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
