"""The exceptions the core raises, and the JSON-RPC error envelope."""

from __future__ import annotations

import re

from ._constants import UNAUTHORIZED

_TOKEN_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")   # RFC 9110 token
_MAX_HEADER_VALUE = 1024


def _hval(v) -> str:
    """A value safe inside a quoted-string header parameter: printable ASCII
    only, no quote or backslash, bounded — so a caller-supplied scope can
    never inject a header or put a byte on the wire that a server refuses
    (control characters and non-ASCII crash or disconnect real containers)."""
    s = "".join(ch for ch in str(v) if 0x20 <= ord(ch) < 0x7F and ch not in '"\\')
    if len(s) > _MAX_HEADER_VALUE:
        raise ValueError(f"header parameter longer than {_MAX_HEADER_VALUE} characters")
    return s


def _header(name, value) -> tuple[str, str]:
    """Validate one response header: a token name, and a value with no control
    characters or non-ASCII (the transports encode latin-1; CR/LF would split)."""
    if not isinstance(name, str) or not _TOKEN_RE.fullmatch(name):
        raise ValueError(f"invalid header name {name!r}")
    value = "".join(ch for ch in str(value) if 0x20 <= ord(ch) < 0x7F)
    if len(value) > _MAX_HEADER_VALUE * 4:
        raise ValueError(f"header value longer than {_MAX_HEADER_VALUE * 4} characters")
    return name, value


class Error(Exception):
    """A JSON-RPC error with the HTTP status it should travel under."""

    def __init__(self, code, message, status=400, data=None, headers=None):
        self.code, self.message, self.status, self.data = code, message, status, data
        self.headers = [_header(k, v) for k, v in (headers or [])]   # extra response headers


class Unauthorized(Error):
    """Raise from `authenticate` (or a handler, or a guard) to answer 401 with a
    `WWW-Authenticate: Bearer ...` challenge, which is what makes an OAuth-capable
    client (Claude.ai, the Inspector, the SDKs) start its authorization flow.
    The `insufficient_scope` form answers 403 instead (RFC 6750 §3.1): the token
    is fine but lacks a scope, and the client may step up to it.

        def authenticate(headers):
            token = headers.get("authorization", "").removeprefix("Bearer ")
            if not token:
                raise Unauthorized(scope="read")
            principal = store.lookup(token)
            if principal is None:
                raise Unauthorized.invalid()
            return principal

    `resource_metadata` is the RFC 9728 document URL. Leave it out on a server
    built with `resource_metadata=`: the server derives its own well-known URL
    from that document's `resource`. `scope`, `error` ("invalid_token",
    "insufficient_scope") and `error_description` are the RFC 6750 parameters.
    Every parameter is reduced to printable ASCII before it reaches a header.

    A tool that streams (a `Context` tool answered over SSE) keeps the status
    and challenge when it raises at its start, before any notification (the
    response head waits up to a second for one); after that the error travels
    in-band as a `-32001` frame without the challenge. Several guards that raise the
    `insufficient_scope` form answer with one 403 naming all their scopes.
    """

    def __init__(self, message="Authentication required", *, resource_metadata=None,
                 scope=None, error=None, error_description=None):
        super().__init__(UNAUTHORIZED, message, 401)
        self.resource_metadata = resource_metadata
        self.scope, self.error, self.error_description = scope, error, error_description
        self.refresh()                      # validates every parameter now

    @classmethod
    def invalid(cls, description="The access token is invalid or expired", **k):
        """The `error="invalid_token"` form. Raise it; do not return it."""
        return cls("Invalid token", error="invalid_token", error_description=description, **k)

    @classmethod
    def insufficient_scope(cls, scope, description="The access token lacks a scope this "
                           "operation needs", **k):
        """The `error="insufficient_scope"` form, answered 403. `scope` names every
        scope the operation needs, so the client can ask for them in one step-up."""
        return cls("Insufficient scope", scope=scope, error="insufficient_scope",
                   error_description=description, **k)

    def challenge(self, default_resource_metadata=None) -> list[tuple[str, str]]:
        """The `WWW-Authenticate` header for this error, as a fresh list. Pure:
        an instance can be shared or module-level without one request's
        default leaking into the next."""
        url = self.resource_metadata or default_resource_metadata
        attrs = [(k, v) for k, v in (("resource_metadata", url), ("scope", self.scope),
                                     ("error", self.error),
                                     ("error_description", self.error_description)) if v]
        value = "Bearer" + (" " + ", ".join(f'{k}="{_hval(v)}"' for k, v in attrs)
                            if attrs else "")
        return [("WWW-Authenticate", value)]

    @property
    def status(self):
        """401, or 403 for `insufficient_scope`: derived from `error`, so the
        status and the challenge can never disagree."""
        return 403 if self.error == "insufficient_scope" else 401

    @status.setter
    def status(self, value):             # Error.__init__ assigns one; `error` decides
        pass

    def refresh(self):
        """Recompute `headers` from the fields (after changing one)."""
        self.headers = self.challenge()
        return self


class _Reply(tuple):
    """A finished (status, payload) answer that may carry response headers.
    Unpacks like the plain pair every transport already expects."""

    headers: list

    def __new__(cls, status, payload, headers=()):
        self = super().__new__(cls, (status, payload))
        self.headers = list(headers)
        return self


def _err(status, rid, code, message, data=None, headers=None):
    err = {"code": code, "message": message}
    if data:
        err["data"] = data
    return _Reply(status, {"jsonrpc": "2.0", "id": rid, "error": err}, headers or ())
