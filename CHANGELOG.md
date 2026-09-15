# Changelog

## Unreleased

### Core
- Requests carrying `NaN`, `Infinity`, or a number that overflows to
  infinity are refused as parse errors (`400`/`-32700`). JSON has no such
  values, but Python's parser accepts them, so they used to reach handlers
  (a `NaN` timeout never expires; a `NaN` comparison is always false).
- `micromcp.__all__` is now the everyday API only: `MCP`, `Server`,
  `ASGIServer`, `Context`, `Principal`, `Error`, `Unauthorized`, `result`,
  `Result`, `embedded_resource`, `MCP_APP_MIME`, `django_view`,
  `django_async_view`. Protocol constants, error codes, tunable defaults, and
  `log` are still importable from `micromcp` by name (only
  `from micromcp import *` no longer brings them in) and are documented in
  the README's new "Constants" section.
- `@mcp.tool(visibility="app" | "model" | [...])` publishes
  `_meta.ui.visibility`; app-only tools are hidden from the model by the host
  and callable by the server's widgets. New suite `tests/test_ui.py`.
- `Unauthorized(error="insufficient_scope")` and the new
  `Unauthorized.insufficient_scope(scope)` answer `403`, as RFC 6750 and the
  MCP authorization spec ask for a token that lacks a scope; the other
  `Unauthorized` forms stay `401`.
- `micromcp.contrib.oauth.OAuth` validates tokens from an identity provider
  (`pip install "micromcp[oauth]"`, PyJWT 2.14+): discovery in the MCP
  clients' URL order with an exact issuer check; signature, issuer,
  audience, expiry, and not-before checks with asymmetric algorithms only;
  cached keys with rate-limited refetches; `503` rather than `401` when the
  provider is unreachable; `requires()` guards and `check()` for the `403`
  step-up challenge, with `implies=` scope hierarchies; opaque tokens by
  introspection; `OAuth.static()` for development; `discover()` warnings
  for providers MCP clients cannot sign in with; and `metadata` for
  `resource_metadata=`. Not in the single-file bundle. New suite
  `tests/test_oauth.py`.

### MCP Apps: `micromcp.apps` (experimental)
- A subpackage imported only on request (`from micromcp.apps import ...`),
  built only on micromcp's public API (a test enforces this), and left out of
  `micromcp.__all__` and the single-file bundle.
- `Widget(name, body= | html=, scripts=, modules=, styles=, imports=, route=,
  fetch=, csp=, border=)` declares a widget once (`imports=` writes an import
  map, e.g. for three.js from a CDN). `@widget.tool(mcp, ...)` registers a
  tool that shows it, naming it in the tool's `_meta` (`ui.resourceUri` and
  the legacy `ui/resourceUri`) and publishing its `ui://` resource on first
  use; `widget.register(mcp)` and `widget.tool_meta` serve tools registered
  by hand. Assets are source text, `pathlib.Path`s (inlined), or https URLs
  (their origins declared in `csp.resourceDomains` automatically); scripts
  follow the body.
- `page()` builds a widget document with `BRIDGE_JS` inlined (MCP Apps
  2026-01-26 handshake, ping/teardown, theme, resize, `mcp.callTool`, and
  `mcp.fetch`, a `fetch()`-shaped tool-call transport wired into htmx 4 and
  fixi); `fragment(html, status=, context=)` returns HTML for the widget to
  swap in. The bridge ships as `bridge.js` in the package.
- HTML arguments (`fragment()`, `page()`, and `Widget`'s `body=`, `head=`,
  `html=`) take a str or any object whose class defines `__html__`, the
  markupsafe protocol (`SupportsHTML`): FastHTML components, htpy elements,
  `markupsafe.Markup`. Those escape text and attribute values, so a widget
  needs no hand-escaped f-strings. As in markupsafe, `__html__` wins over a
  str's own value. Output is normalized to an exact str, so a `Markup` body
  no longer escapes the page built around it.
- `tool_url(name, **args)` builds a `tool:` URL with percent-encoded
  arguments, so user text in a value can neither add arguments nor change
  others (an f-string URL lets `milk&role=admin` add one).
- Model context: `fragment(context=)` and `mcp.setContext()` send
  `ui/update-model-context` (text plus data as a labeled JSON block);
  `mcp.say()` and `data-mcp-say` send `ui/message`.
- Channels: `Channel(mcp, name, guards=, wait=, idle=, max_queue=)` is a
  WebSocket-style channel between widgets and server code (`on_connect`,
  `on_message`, `on_disconnect`; `Connection.send`/`send_json`/`close`;
  `broadcast`/`broadcast_json`, thread-safe). In the widget,
  `mcp.channel(name)` and `mcp.WebSocket` follow the WebSocket API. Four
  shared app-only tools carry it, `channel_recv` being a long poll (20 s by
  default, verified in Claude); connections are bound to their principal,
  dropped when idle, and closed when too far behind.
- Django: `micromcp.apps.django.django_routes(mcp, prefixes=)` serves Django
  views to widgets in process (prefix-confined, redirects followed,
  hypermedia headers forwarded, JSON bodies); `set_mcp_context(response,
  text, data)`.
- `DEVHOST_HTML`, a development MCP Apps host page.
- Hardening from adversarial tests: channels cap queued bytes per connection
  (`max_bytes`, 16 MiB) and open connections per channel (`max_connections`,
  1000), sweep idle connections at most every second, ignore non-finite
  `wait` values, and close a connection whose `on_message` raises (logged,
  never echoed to the client). `Widget` refuses near-miss URLs instead of
  inlining them as source, URLs with credentials, and `csp=` entries that are
  not plain origins. `django_routes` refuses control characters and invalid
  UTF-8 in paths. The README warns against swapping user-authored HTML into a
  widget.
- Examples: `mcp_app_3d.py` (a shared three.js scene the model builds and
  the user selects in), `mcp_app_hypermedia.py` (todo widget rendered with
  FastHTML components by tools, and by Django views), `toolkit_lab.py` (nine
  self-testing toolkit variants and a model-context counter). Suite
  `tests/test_apps.py`.

## 0.1.0 — 2026-09-14 (tagged; not yet on PyPI)

First packaged release. Same public API as the original single file:
`from micromcp import MCP, Server, ASGIServer, Context, Principal`.

### Packaging
- `src/` layout, nine modules, hatchling build, Apache-2.0, Python 3.11+.
- Django adapters live in `micromcp.contrib.django` (still re-exported from
  `micromcp` for existing code).
- `tools/bundle.py` regenerates a single vendorable `micromcp.py`; CI
  regenerates it and runs every suite against it.
- The per-server tunables (`keepalive`, `cancel_grace`, `queue_size`,
  `offload_bytes`, `stream_budget`, `workers`, `timeout`, `max_body`) are
  constructor options; the module constants are only their defaults.

### Interop
- `subscriptions/listen` is accepted and closed gracefully (acknowledgment
  with an empty honored set, then a completion result carrying the
  subscription id) instead of answering 404 / `-32601`, which Go SDK 1.7
  clients treated as a failed connection.
- `legacy="stateless"` (off by default) serves 2025-era clients per request
  and without sessions, the posture the official SDKs call stateless legacy
  serving: `initialize` answered from the registry, initialized `202`,
  `ping`, unstamped results stamped with the era served, GET/DELETE `405`.
  `server/discover` and the `-32022` refusal then list both eras. Routing
  headers sent by a legacy client must agree with the body. Verified against
  the TypeScript SDK 2.0.0 client's default mode and the Python client's
  `mode="legacy"`. `2025-03-26` is not served (it mandated batch arrays).
- A posted JSON-RPC response is acknowledged with `202`; a null envelope
  claim routes modern; SSE requires an explicit, non-`q=0`
  `text/event-stream` in `Accept` (parsed, not substring-matched); the
  `logging` capability is declared; legacy errors and streams name the era
  served and a legacy `-32601` travels under `200`; the envelope ladder's
  `-32022` lists modern revisions only; a hidden tool answers like an
  unknown one; WSGI-folded duplicates of `Authorization`/`Host`/`Origin` are
  refused.
- `ctx.client_info` exposes the envelope's client identity.

### Auth
- `Unauthorized` (raise from `authenticate` or a handler) answers `401` with
  a sanitized `WWW-Authenticate: Bearer` challenge; `Unauthorized.invalid()`
  is the `error="invalid_token"` form (raise it; an `Error` returned from
  `authenticate` is treated as raised, never as a principal). Guards may raise
  it. `resource_metadata=` serves the RFC 9728 document at
  `/.well-known/oauth-protected-resource[<path>]` (GET/HEAD/OPTIONS, CORS `*`,
  honoring `allowed_hosts`) and supplies the challenge URL, derived from the
  document's `resource` rather than from `Host`, and the document is served
  at exactly that derived path (`well_known_path`; HEAD supported, honest
  `Allow`, CORS on every answer). Challenge parameters and every `Error`
  header are reduced to printable ASCII and refused when over-long;
  exceptions are never mutated; `authenticate` returning an exception class
  is refused. Error replies can carry response headers; a 401 raised after an
  SSE stream opened is delivered in-band and logged.

### UI apps
- `meta=` on tools, resources, templates, and prompts is validated at
  registration (JSON, valid `_meta` keys, no reserved prefixes) and published
  as `_meta`, detached from the caller's dict.
- `result(content, structured=, is_error=, meta=)` is the explicit way to
  return a finished tool result; blocks are checked against the five content
  types and reserved `_meta` keys are refused. Plain dicts are always data.
- `embedded_resource()` validates text/blob and defaults to
  `text/html;profile=mcp-app`; `ui://` resources default to that profile and
  registration warns when they carry guards or template parameters.
- `ctx.request_meta` / `ctx.client_capabilities` expose the request's `_meta`.
- Verified rendering as an MCP App in the Claude desktop chat;
  `examples/mcp_app_modal.py` is a spec-2026-01-26 widget with a sender check,
  DOM-only rendering, error handling, theme, and ResizeObserver sizing.

### Hardening (three rounds of adversarial review, 2026-09-13)
- Principal/Context detection is structural and fail-closed: works through
  Optional, Union, Annotated, PEP 695 aliases, callable instances, partials;
  unresolvable marker-looking annotations and marker subclasses are refused.
- Guards apply to resources, prompts, and listings; denials are not-found.
- Arguments are validated against the published schema (constraints
  included, `$defs` namespaced per parameter) and converted to the declared
  Python types before the handler runs; outputs are validated against
  `outputSchema`. Validation happens before any response byte is committed.
- Transport: envelope validation, notifications acknowledged with 202,
  duplicate-header rejection, body limits with 413-before-read, Content-Type,
  Host, CORS, Accept handling, constant error text outside `tools/call`.
- Runtime: per-server daemon thread pools that are fork-aware and refuse with
  503 when full; a separate lane for `authenticate`; WSGI `timeout`; SSE
  backpressure, keepalives, per-call byte budget, cooperative cancellation
  via `ctx.cancelled`.
