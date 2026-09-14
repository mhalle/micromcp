"""micromcp — a minimum-viable MCP server for the stateless 2026-07-28 revision.

Pure stdlib. No dependencies. No framework. Two transports over one core:
`Server` is a WSGI app (Django, Flask, gunicorn, waitress, wsgiref) and
`ASGIServer` is a native ASGI app (Starlette, FastAPI, Litestar, uvicorn).
Handlers may be sync or `async def` on either; sync handlers run on a
per-server thread pool so they never block the event loop.

Scope: tools, resources, prompts, SSE progress/logging on ASGI, the OAuth
challenge handoff (`Unauthorized`, `resource_metadata=`), and opt-in per-request
serving of 2025-era clients (`legacy="stateless"`).
Out of scope (deliberately): MRTR/elicitation, subscriptions, being an OAuth
authorization server, pagination, sessions.

SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

# Protocol constants, error codes, and tunable defaults: importable from here
# (`from micromcp import PROTOCOL, MAX_BODY`), documented in the README's
# "Constants" section, and left out of `__all__`, which is the everyday API.
from ._constants import (  # noqa: F401
    CANCEL_GRACE, CORS_HEADERS, HANDSHAKE_METHODS, HEADER_MISMATCH, INTERNAL_ERROR,
    INVALID_PARAMS, INVALID_REQUEST, KEEPALIVE, LEGACY_VERSIONS, LIST_METHODS, MAX_BODY,
    MAX_DEPTH, MAX_URI, META_CAPS, META_CLIENT, META_SERVER, META_SUB, META_VER,
    METHOD_NOT_FOUND, OFFLOAD_BYTES, PARSE_ERROR, PROTOCOL, QUEUE_SIZE, ROUTING_HEADERS,
    SINGLETON_HEADERS, STREAM_BUDGET, UNAUTHORIZED, UNSUPPORTED_VERSION, WELL_KNOWN,
    WORKERS, log,
)
from .apps import BRIDGE_JS, CONTEXT_META, Channel, Connection, Widget, fragment, page
from .asgi import ASGIServer
from .core import (  # noqa: F401
    MCP_APP_MIME, Result, _Core, _Pool, _decode_hdr, _encode, embedded_resource, result,
)
from .docstrings import _parse_doc  # noqa: F401
from .errors import Error, Unauthorized
from .markers import Context, Principal
from .registry import MCP, _coerce  # noqa: F401
from .schema import _check  # noqa: F401
from .wsgi import Server, _Loop  # noqa: F401

__version__ = "0.1.0"

__all__ = [
    "MCP", "Server", "ASGIServer", "Context", "Principal", "Error", "Unauthorized",
    "result", "Result", "embedded_resource", "MCP_APP_MIME",
    "page", "fragment", "BRIDGE_JS", "Widget", "Channel", "Connection", "CONTEXT_META", "django_routes", "set_mcp_context",
    "django_view", "django_async_view", "__version__",
]


def django_view(server):
    """See `micromcp.contrib.django.django_view`."""
    from .contrib.django import django_view as _v
    return _v(server)


def django_async_view(server):
    """See `micromcp.contrib.django.django_async_view`."""
    from .contrib.django import django_async_view as _v
    return _v(server)


def django_routes(mcp, **kwargs):
    """See `micromcp.contrib.django.django_routes`."""
    from .contrib.django import django_routes as _r
    return _r(mcp, **kwargs)


def set_mcp_context(response, text, data=None):
    """See `micromcp.contrib.django.set_mcp_context`."""
    from .contrib.django import set_mcp_context as _s
    return _s(response, text, data)
