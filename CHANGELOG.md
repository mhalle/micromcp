# Changelog

## 0.3.0 — 2026-11-16

A security release. A November 2026 adversarial review found that an
`async def` guard let every caller in, that `allowed_hosts` accepted a
crafted Host value, and that a guard-hidden resource could shadow a visible
template; these and the review's other findings are fixed below, several
dating to 0.1.0. **Upgrade if you use async guards or `allowed_hosts`.** New
too: server instructions, Server Cards and identity metadata, Skills over
MCP, declared extensions, content-block helpers with annotations, and
`fallback=` for serving another app beside the MCP endpoint. **Behavior
change:** an unknown tool, prompt, or resource is now `400`/`-32602`, as the
spec says, not `404`/`-32601` (see Core).

### Security
- **An `async def` guard always allowed.** A guard's return value was judged
  without being awaited, and a coroutine is truthy, so any tool, resource,
  template, prompt, or skill guarded by a coroutine function was listed and
  callable by everyone. Guards may now be `async def` (or return any
  awaitable) and are awaited. Affects 0.2.0 and earlier.
- A guard-hidden tool that takes a `Context` streamed its answer (a `200`
  event stream) under `django_async_view` on Django's WSGI handler, which told
  it apart from a missing tool (`400`). A tool the caller may not use is never
  streamed.
- `path=` must start with `/`. `path="mcp"` could never match a request, so
  every request was a `404`, or, with `fallback=`, handed to the fallback with
  its bearer token.
- `allowed_hosts` admitted any Host of the form `<allowed>:<anything>`, such as
  `localhost:@evil.test`, and handed that value on (to `fallback=`, among
  others). A listed name now matches only alone or with a numeric port, and a
  Host holding a comma (two copies folded into one by a WSGI server) is never
  admitted; the endpoint, the Server Card, the RFC 9728 document, and the
  fallback all apply the same rule. Affects 0.2.0 and earlier.
- `fallback=` without `allowed_hosts` logs a warning: the endpoint's Origin
  check also shields it from DNS rebinding, but a rebinding page's plain GET
  carries no Origin, so only `allowed_hosts` protects what the fallback
  serves. The README example now sets it.
- Skills (#19): SKILL.md frontmatter could read one way to micromcp and
  another to a host's YAML parser, which hosts compare against the entry and
  bind approvals to. A lone CR (or NEL, U+2028, U+2029) splits a line for YAML
  but not for the server, so a skill could carry an `allowed-tools` or a
  different `name` that its entry did not show. Such characters, control
  characters, non-string keys (`true:`, `1:`), YAML 1.1 underscore numbers
  (`1.0_`, `1_:0`), surrogate escapes, and over-indented blank block lines
  are now refused; only spaces and tabs are trimmed.
- Skills: a frontmatter number too large for a float (it became `inf`)
  answered every `skills/list` with `500`; it is refused at registration.
  Registration no longer backtracks for seconds on long numeric-looking values
  or folds `>` blocks in quadratic time, and frontmatter is capped at 64 KiB.
  Names, prefixes, and file paths no longer accept a trailing newline. Files are
  counted and sized before they are read (a sparse 1 GiB file was read whole).
  A symlink to a dot-file or `__pycache__` inside the skill (which would have
  served, say, `.env`), two files differing only in case, and a path that is
  both a file and a directory are refused.
- In the single-file bundle (`build/micromcp.py`), the frontmatter parser read
  numbers with `registry.py`'s patterns, which share its names and came later
  in the file, so the bundle and the package could disagree about a skill's
  frontmatter. The names are now distinct, and `tools/bundle.py` refuses to
  build when two modules define the same top-level name.
- A guard-hidden resource or template shadowed a visible template matching
  the same URI: a read answered "Resource not found" where the template would
  have served it, which also let a caller map hidden URIs by probing. Reads
  now skip what the caller may not use and take the next match (a skill file,
  the exact resource, then templates by longest prefix); with nothing usable
  the most specific entry's guards still choose the answer, so a `401`
  sign-in challenge is kept. Affects 0.2.0 and earlier.
- A `Result` could set `resultType`, `ttlMs`, and `cacheScope` (an invalid
  negative `ttlMs`, or `cacheScope: public` on per-user data), and hand-written
  blocks were checked only for their required fields. A result now holds
  `content`, `structuredContent`, `isError`, and `_meta` only, and each block is
  checked as its helper builds it: field types, the `image/*` or `audio/*`
  type, valid base64, exactly one of `text` and `blob`, and `_meta` keys. A
  result that is not JSON is an in-band `isError`, as for a plain dict, rather
  than a `-32603`.
- Icons: a `data:` `src` must be a base64 image (`data:text/html` was
  accepted and published, before authentication, on the Server Card),
  `mimeType` must be `image/*`, and `sizes` no longer accept a trailing
  newline. An http(s) URL (`src`, `website_url`) may not carry user info
  (`https://good.example@evil.example` goes to `evil.example`).
- `lastModified` must be RFC 3339 with an offset (`fromisoformat` also took
  week dates and offset-less times JavaScript cannot read or reads as local
  time); a `datetime` without a timezone is refused.
- MCP Apps: an import-map key that is itself a path (`./`), or one only under
  `scopes`, no longer silences the warnings about paths a widget cannot load.
  A page holding a lone surrogate no longer crashes the size check (#22).
- `django_view`'s empty answers (a Server Card `304`) no longer carry Django's
  default `Content-Type: text/html`.
- CI: the workflow token is read-only by default, actions are pinned to
  commits, `twine` is pinned (it runs in the job whose build is published), and
  macOS runs its oldest and newest Python only, since macOS runners were
  starving and cancelling jobs.

### Core
- **Behavior change:** an unknown or guard-hidden tool (`tools/call`), prompt
  (`prompts/get`), or resource (`resources/read`) is answered `400` with
  `-32602`, as 2026-07-28 specifies (`Unknown tool: <name>`, `Unknown prompt:
  <name>`, `Resource not found` with `data.uri`). It used to be `404` with
  `-32601`, which the transport reserves for a method the server does not
  implement and which a client may read as an unsupported method. Hidden and
  missing entries still answer identically. A 2025-era request under
  `legacy="stateless"` gets `-32002` for a missing resource, the code its
  revision defined; unknown methods keep `404`/`-32601`. The dev host's
  unknown-tool reply to widgets matches.
- Log messages follow the request's `io.modelcontextprotocol/logLevel`, as
  2026-07-28 requires. A request without it gets no `notifications/message`
  (they used to go to every streaming call), one with it gets only messages
  at or above that level, and an unknown or null level is `-32602`.
  `Context.log()` raises `ValueError` for a level the spec does not define,
  and `ctx.log_level` reports what the client asked for. 2025-era requests
  under `legacy="stateless"` still get every message. New constants
  `META_LOG` and `LOG_LEVELS`.
- `MCP(..., title=, description=, website_url=, icons=)` publishes the spec's
  optional identity fields in `serverInfo` (every result's `_meta`, and
  `initialize` under `legacy="stateless"`); unset ones are left out.
  `MCP.server_info()` returns the object.
- `icons=` on `@mcp.tool`, `@mcp.resource` (templates too), and `@mcp.prompt`,
  published in listings. Icons are validated where they are defined: known
  keys only, `src` an http(s) URL or a base64 `data:` URI, `sizes` like
  `"48x48"` or `"any"`, `theme` `"light"` or `"dark"`.
- Server Cards (`io.modelcontextprotocol/server-card`):
  `server_card={"url", "name", "repository"}` on `Server` and `ASGIServer`
  serves a card at `<url>/server-card` as `application/mcp-server-card+json`,
  built from the registry (so it matches `serverInfo` and the versions
  served), checked at construction, answered before authentication, readable
  from any origin, cacheable, and revalidated by `ETag` with `304`. The
  Django adapters serve it too. New constants `SERVER_CARD`,
  `SERVER_CARD_MIME`, `SERVER_CARD_SCHEMA`. New suite `tests/test_identity.py`,
  which also validates cards against the extension's published JSON Schema
  (vendored in `tests/schemas/`); `conform.py` now sets every identity field
  and icon.
- Skills over MCP (`io.modelcontextprotocol/skills`): `mcp.skill(path_or_files,
  prefix=, guards=, replace=)` serves an Agent Skill as `skill://` resources
  and answers `skills/list`, `skills/get`, and `resources/directory/read`.
  Entries carry the verbatim frontmatter and a complete manifest with SHA-256
  digests and sizes; files are snapshotted at registration so served bytes
  always match them. Registration enforces the Agent Skills naming rules,
  the per-skill limits (512 files, 16 MiB), URI-safe file names, and no
  symlinks out of the directory. Frontmatter is read by a strict YAML subset
  that refuses constructs it does not support and plain values YAML 1.1 and
  1.2 parsers read differently, so the entry is what a host parses; the test
  suite cross-checks it against PyYAML. New module `micromcp.skills`, new
  constants `SKILLS_EXT`, `DIRECTORY_MIME`, `SKILL_MAX_FILES`,
  `SKILL_MAX_BYTES`, and new suite `tests/test_skills.py`.
- `MCP(..., extensions={identifier: settings})` declares extensions, published
  as `capabilities.extensions` in `server/discover` and in `initialize` under
  `legacy="stateless"`; a registered skill adds its own. MCP Apps widgets do
  not declare `io.modelcontextprotocol/ui` automatically.
- `MCP(name, version, instructions="...")` publishes server instructions:
  in the `server/discover` result, and in `initialize` under
  `legacy="stateless"`. Omitted when unset; a non-string is a `TypeError` at
  construction.
- Content-block helpers `text_content`, `image_content`, `audio_content`,
  and `resource_link`, beside `embedded_resource`. The media helpers take
  raw bytes and base64-encode them. All five take `annotations=`
  (audience, priority, lastModified), which are validated: unknown keys,
  out-of-range priority, and unparseable dates are refused.
- `@mcp.resource(annotations=...)`, published in resource and template
  listings.
- A tool that returns bare `bytes` is now an in-band `isError` naming the
  helpers. It used to send the bytes' Python repr as text.
- `result()` checks hand-written blocks more closely: image/audio `data`
  and `mimeType`, and resource-link `uri` and `name`, must be strings, and
  any `annotations` get the same validation as the helpers'.

- `fallback=` on `Server` (a WSGI app) and `ASGIServer` (an ASGI app) answers
  every path but the endpoint, so one process can serve `/mcp` and the plain
  URLs its tools return, such as rendered images. It requires `path=`. The
  request reaches it untouched; only `allowed_hosts` applies first (a refused
  or repeated Host never reaches it). The Server Card and the RFC 9728
  document stay micromcp's; on ASGI, WebSockets to other paths go to the
  fallback and the lifespan does not. New suite `tests/test_fallback.py`.

### MCP Apps
- A specifier the page's own import map resolves is no longer reported as a
  path a widget cannot load. A bundle that leaves `three` and `three/addons/`
  external, with `imports=` mapping both, drew a warning about
  `three/addons/controls/OrbitControls.js` although the map covers it.
- A widget whose page runs to a few hundred KB is warned about, with its size
  and that of its largest script. A host fetches a widget whole, in one
  `resources/read` result, and caches it per connector, so an inlined
  dependency is paid for in a way an ordinary web page's is not — a namespace
  import of a validation library, for one, can cost several hundred KB where
  named imports cost a few.

## 0.2.0 — 2026-09-16 (tagged; not yet on PyPI)

MCP Apps widgets, an OAuth token-validation helper, and the core fixes that
building them turned up. The core API is unchanged apart from a trimmed
`__all__`; everything new sits beside it. `micromcp.apps` is **experimental**
and may change in a minor release.

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
  an async `authenticate`, so no request waits for a thread unless it needs
  the provider, and fetches from the provider are single-flight and bounded
  by `timeout`; keys refreshed in the background, unknown key ids forcing a
  refresh at most every 30 s; `503` rather than `401` (or `500`) when the
  provider is unreachable, slow, or answers something unusable;
  `requires()` guards and `check()` for the `403` step-up challenge, with
  `implies=` scope hierarchies; opaque tokens by introspection;
  `OAuth.static()` for development; `discover()` warnings for providers MCP
  clients cannot sign in with; and `metadata` for `resource_metadata=`.
  Not in the single-file bundle. New suites `tests/test_oauth.py` and
  `tests/test_oauth_sdk.py` (the official SDK client, end to end).
- `Unauthorized.status` is derived from `error`, so it can no longer
  disagree with the challenge; an authentication error answered while
  preparing a 2025-era request names that era in `MCP-Protocol-Version`.
- A streaming (`Context`) tool's response head waits up to a second for its
  first notification, so an `Error` raised at the tool's start, such as a
  `403 insufficient_scope` from a scope check, keeps its status and headers
  instead of travelling in-band. `begin_stream()`
  gives adapters the same; the Django async view uses it under ASGI.
- Several guards on one entry that raise `insufficient_scope` answer with a
  single `403` naming all their scopes, so a client steps up once.
- A request refused for its protocol version (`-32022`) is answered by
  `authenticate` first: an SDK client that falls back to the 2025 handshake
  after a failed modern probe sees the `401` or `503`, not a version error.

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
- Bundled widgets: `Widget(html=Path(...))` reads a complete page from a file
  (`body=` takes a path too), and `bridge=True` puts the bridge first in its
  head, with `route=`/`fetch=`, ahead of anything the page runs and carrying
  its own charset. A page that loads a relative URL, which a widget cannot
  fetch, is refused when the widget is built (`src`/`href`/`srcset`/
  `background` on loading elements, `srcdoc` and `<template>` content,
  `url()`/`image-set()`/`@import` in styles, import maps, preloads and
  prefetches); loads after a valid https `<base href>` pass, and `page()` is
  checked like `Widget`. A module whose own static `import` names a relative
  path or a bare specifier is refused too: with no origin and no bundler it
  would never run. Warnings, not refusals: relative paths inside a classic
  script (Vite 8's backtick strings included), a bundler's leftovers beside an
  `html=`/`body=`/`modules=` path (a hashed file or one in `assets/`), and
  another MCP Apps client beside micromcp's bridge. `escape_scripts=True`
  rewrites `</script` in inlined scripts and closes a `<!--` ... `<script`
  sequence with `//-->`, which makes Vue 3's development build usable; a page
  whose script would swallow the rest of it is refused. `BRIDGE_TYPES`
  declares `window.mcp` for TypeScript. `docs/apps.md` covers Vite (checked
  with 8.3), workers and WASM, vendored CSS that loads fonts, and Bun
  (checked with 1.4.0).
- Documentation, after agents reproduced it from scratch: `docs/apps.md` now
  carries a runnable server that shows a widget in the dev host (the dev host
  is a browser client, so it needs the server's `allowed_origins=`; without
  that every call failed with `-32020` and the section could not be
  followed), states the bridge API and that `await mcp.ready` comes first,
  gives the checks as a table of what is refused, what is warned about, and
  what to do, documents `csp=` as the dict of origin buckets it is (its
  previous example strings were all refused), and adds an esbuild recipe
  (checked with 0.28). Two claims were wrong and are corrected: fonts
  inlined as `data:` URLs are blocked by the default policy's `font-src`, so
  a vendored stylesheet belongs on its CDN, and Leaflet needs
  `L.Icon.Default.imagePath = ""` before its icons are pointed at data URLs.
- `Channel.broadcast(text, exclude=)` and `broadcast_json(value, exclude=)`
  leave one connection out, and a widget knows its own as
  `mcp.channel(name).id`. Without it, an action that both answered with a
  fragment and broadcast made the acting widget swap twice; the kanban example
  now swaps once and the other open boards still update.
- A widget whose `route=` names no tool on the server it is registered with is
  warned about when the host reads the widget — the first moment it can be
  known, since tools may be registered after the widget is built. It used to
  surface only as an `mcp-route` error in the frame.
- Four new examples, each a shape the docs describe and none of which existed:
  `mcp_app_bundled.py` (a Vite build plugged in), `mcp_app_charts.py` (a charting
  library from a CDN, an app-only tool for data, live events over a channel),
  `mcp_app_map.py` (Leaflet, a tile origin declared by hand, the model dropping
  markers into an open map), and `mcp_app_board.py` (no bundler at all: every
  click a tool call answered with `fragment()`, two boards kept in step). A test
  imports every example and fails if one stops building or starts warning.
- `examples/mcp_app_bundled.py`: the bundling recipe as a runnable file. Its
  `examples/bundled_ui/` is an ordinary Vite project whose build writes the two
  files the widget takes, with the logo inlined as a `data:` URL in both the
  module and the stylesheet; the build is committed, so the example runs
  without node installed. There was no bundled-widget example before, which is
  what readers of that section kept having to invent.
- Three example apps were built against the docs — a charts dashboard, a
  Leaflet map, and a server-rendered kanban board — and fixed what they hit.
  `mcp.callTool` forwards a `fragment(context=...)` update, which until now
  reached the model only when the widget went through `mcp.fetch`, so a
  bundled app got nothing. The dev host names a tool it cannot find instead
  of blaming `visibility=`. A word ending in `.js` is no longer read as a
  path (ECharts' `"Node.js"` drew a warning), and the paths in a warning are
  quoted, so a list of them reads as a list. `docs/apps.md` gains what the
  three apps had to discover for themselves: that `callTool` rejects when the
  call cannot be made and resolves with `isError` when the tool ran and
  failed, that a tool with no `visibility=` is callable by the widget too and
  that hiding one from the model is the host's doing rather than the wire's,
  that `csp=` takes an origin and never a URL template, that a sandboxed
  frame will not submit a `<form>` or evaluate an `hx-trigger` filter, how to
  swap pushed HTML so its `hx-*` attributes stay wired, that context updates
  from either path share one debounce, that a widget holding a channel is
  never "network idle", and how to call your own server from a script.
- A second pass of the same exercise, against the revised page: a font
  inlined as a `data:` URL is warned about (a host's `font-src` refuses it,
  which the docs knew and the library did not check), a run-time `import()`
  is told apart from an asset string so each carries its own remedy, an asset
  path without a leading `./` is seen, another vendor's MCP Apps client is
  recognised so its page is not told to add a second bridge, a build folder
  beside a server module is scanned again, and a refused relative URL names
  the remedy that fits: a split build, a vendored stylesheet, or an asset.
- A widget whose page carries no MCP Apps client at all is now warned about,
  since `window.mcp` would be undefined; a bundler's leftovers are also
  recognised when the hash holds no digit (Vite 8 writes `lazy-DuOUKcfe.js`)
  or the file is named like a worker; `Widget` with neither `body=` nor
  `html=` says so rather than complaining about both; and a relative URL in
  a stylesheet points at the CDN remedy that works.
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
