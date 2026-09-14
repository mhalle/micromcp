"""micromcp — a minimum-viable MCP server for the stateless 2026-07-28 revision.

Pure stdlib. No dependencies. No framework. Two transports over one core:
`Server` is a WSGI app (Django, Flask, gunicorn, waitress, wsgiref) and
`ASGIServer` is a native ASGI app (Starlette, FastAPI, Litestar, uvicorn).
Handlers may be sync or `async def` on either; sync handlers run on a
per-server thread pool so they never block the event loop.

Scope: tools, resources, prompts, plus SSE progress/logging on ASGI.
Out of scope (deliberately): MRTR/elicitation, subscriptions, OAuth,
pagination, legacy session-based protocol revisions.

SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

from ._constants import (
    CANCEL_GRACE, CORS_HEADERS, HANDSHAKE_METHODS, HEADER_MISMATCH, INTERNAL_ERROR,
    INVALID_PARAMS, INVALID_REQUEST, KEEPALIVE, LIST_METHODS, MAX_BODY, MAX_DEPTH,
    MAX_URI, META_CAPS, META_SERVER, META_VER, METHOD_NOT_FOUND, OFFLOAD_BYTES,
    PARSE_ERROR, PROTOCOL, QUEUE_SIZE, ROUTING_HEADERS, SINGLETON_HEADERS,
    STREAM_BUDGET, UNSUPPORTED_VERSION, WORKERS, log,
)
from .asgi import ASGIServer
from .core import _Core, _Pool, _decode_hdr, _encode  # noqa: F401  (private; used by the suites)
from .docstrings import _parse_doc  # noqa: F401
from .errors import Error
from .markers import Context, Principal
from .registry import MCP, _coerce  # noqa: F401
from .schema import _check  # noqa: F401
from .wsgi import Server, _Loop  # noqa: F401

__version__ = "0.1.0"

__all__ = [
    "MCP", "Server", "ASGIServer", "Context", "Principal", "Error",
    "PROTOCOL", "META_VER", "META_CAPS", "META_SERVER",
    "PARSE_ERROR", "INVALID_REQUEST", "METHOD_NOT_FOUND", "INVALID_PARAMS",
    "INTERNAL_ERROR", "HEADER_MISMATCH", "UNSUPPORTED_VERSION",
    "HANDSHAKE_METHODS", "LIST_METHODS", "ROUTING_HEADERS", "SINGLETON_HEADERS",
    "CORS_HEADERS", "MAX_BODY", "MAX_URI", "MAX_DEPTH", "OFFLOAD_BYTES",
    "QUEUE_SIZE", "STREAM_BUDGET", "KEEPALIVE", "CANCEL_GRACE", "WORKERS",
    "django_view", "django_async_view", "log", "__version__",
]


def django_view(server):
    """See `micromcp.contrib.django.django_view`."""
    from .contrib.django import django_view as _v
    return _v(server)


def django_async_view(server):
    """See `micromcp.contrib.django.django_async_view`."""
    from .contrib.django import django_async_view as _v
    return _v(server)
