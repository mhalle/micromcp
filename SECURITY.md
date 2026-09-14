# Security

micromcp is a small server that sits between a language model and your code.
Its trust boundaries are: the HTTP client, the validation ladder,
`authenticate`, per-entry guards, argument validation and conversion, the
handler, result serialization, and the SSE stream. `test_hardening.py` and
`test_defender.py` pin the behavior at each boundary; the defender suite was
derived from a mutation run, so reverting any of those defenses turns a check
red.

## What the server does not do

- It is not an OAuth authorization server. Put a gateway in front of anything
  internet-facing and verify bearer tokens in `authenticate`.
- Guards run inline on the event loop and must be cheap. A slow guard
  serializes requests; do I/O in `authenticate` instead.
- A `def` handler cannot be interrupted once it is running. Long loops should
  poll `ctx.cancelled`; a full pool answers `503` rather than queueing.
- Under WSGI, two folded `Mcp-Name` headers whose fold equals the body value
  cannot be told apart from one header. ASGI rejects duplicates on the raw
  header pairs.

## Reporting

Please report vulnerabilities privately to the maintainers rather than in a
public issue. Include a minimal reproduction; the adversarial PoC scripts
under `tests/` show the expected shape.
