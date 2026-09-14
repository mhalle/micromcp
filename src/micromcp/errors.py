"""The exceptions the core raises, and the JSON-RPC error envelope."""

from __future__ import annotations

from ._constants import UNAUTHORIZED


class Error(Exception):
    """A JSON-RPC error with the HTTP status it should travel under."""

    def __init__(self, code, message, status=400, data=None, headers=None):
        self.code, self.message, self.status, self.data = code, message, status, data
        self.headers = list(headers or [])       # extra response headers


def _hval(v) -> str:
    """A value safe inside a quoted-string header parameter: no quotes, no
    line breaks, so a caller-supplied scope can never inject a header."""
    return str(v).replace("\\", "").replace('"', "").replace("\r", "").replace("\n", "")


class Unauthorized(Error):
    """Raise from `authenticate` (or a handler) to answer 401 with a
    `WWW-Authenticate: Bearer ...` challenge, which is what makes an OAuth-capable
    client (Claude.ai, the Inspector, the SDKs) start its authorization flow.

        def authenticate(headers):
            token = headers.get("authorization", "").removeprefix("Bearer ")
            if not token:
                raise Unauthorized(resource_metadata="https://api.example/.well-known/oauth-protected-resource")
            return lookup(token) or Unauthorized.invalid()

    `resource_metadata` is the RFC 9728 document URL. Leave it out on a server
    built with `resource_metadata=`: the server fills in its own well-known URL
    from the request's Host. `scope`, `error` ("invalid_token",
    "insufficient_scope") and `error_description` are the RFC 6750 parameters.
    """

    def __init__(self, message="Authentication required", *, resource_metadata=None,
                 scope=None, error=None, error_description=None):
        super().__init__(UNAUTHORIZED, message, 401)
        self.resource_metadata = resource_metadata
        self.scope, self.error, self.error_description = scope, error, error_description
        self.refresh()

    @classmethod
    def invalid(cls, description="The access token is invalid or expired", **k):
        return cls("Invalid token", error="invalid_token", error_description=description, **k)

    def refresh(self):
        """Rebuild the challenge header from the fields (after the server fills
        in a default `resource_metadata`)."""
        attrs = [(k, v) for k, v in (("resource_metadata", self.resource_metadata),
                                     ("scope", self.scope), ("error", self.error),
                                     ("error_description", self.error_description)) if v]
        challenge = "Bearer" + (" " + ", ".join(f'{k}="{_hval(v)}"' for k, v in attrs)
                                if attrs else "")
        self.headers = [("WWW-Authenticate", challenge)]
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
