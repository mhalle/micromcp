# Changelog

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
  `ping`, unstamped results, GET/DELETE `405`. `server/discover` and the
  `-32022` refusal then list both eras. Verified against the TypeScript SDK
  2.0.0 client's default mode and the Python client's `mode="legacy"`.
- `ctx.client_info` exposes the envelope's client identity.

### Auth
- `Unauthorized` (raise from `authenticate` or a handler) answers `401` with
  a sanitized `WWW-Authenticate: Bearer` challenge; `Unauthorized.invalid()`
  is the `error="invalid_token"` form. `resource_metadata=` serves the RFC
  9728 document at `/.well-known/oauth-protected-resource[<path>]` and fills
  the challenge's URL in from the request's scheme and Host. The challenge
  header is CORS-exposed. Error replies can now carry response headers.

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
