"""Django adapters. Django is imported lazily, when a view is built."""

from __future__ import annotations

import io
import json
import re

from .._constants import log

from ..asgi import ASGIServer
from ..core import _encode
from ..markers import Principal   # module level: annotations resolve against globals
from ..wsgi import Server


def django_view(server: Server):
    """Wrap a Server as a Django view (sync). Works on WSGI and ASGI alike."""
    from django.http import HttpResponse
    from django.views.decorators.csrf import csrf_exempt

    @csrf_exempt
    def view(request):
        box = {}
        environ = dict(request.META)
        environ["REQUEST_METHOD"] = request.method
        environ["wsgi.input"] = io.BytesIO(request.body)
        environ["CONTENT_LENGTH"] = str(len(request.body))
        chunks = server(environ, lambda s, h: box.update(status=s, headers=h))
        body = b"".join(chunks)
        resp = HttpResponse(body, status=int(box["status"].split()[0]),
                            content_type="application/json" if body else None)
        for k, v in box["headers"]:
            if k.lower() not in ("content-type", "content-length"):
                resp[k] = v
        return resp

    return view


def django_async_view(server: ASGIServer):
    """Wrap an ASGIServer as an async Django view, awaiting handlers natively.

    Context tools stream: the view returns a StreamingHttpResponse of SSE
    frames, and Django closing the response (client gone) cancels the handler.
    Streaming needs Django's ASGI handler; under Django's WSGI handler the
    frames are delivered in one buffered response.
    """
    from django.http import HttpResponse, StreamingHttpResponse
    from django.views.decorators.csrf import csrf_exempt

    @csrf_exempt
    async def view(request):
        headers = {k[5:].replace("_", "-").lower(): v
                   for k, v in request.META.items() if k.startswith("HTTP_")}
        for k in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            if request.META.get(k):
                headers[k.replace("_", "-").lower()] = request.META[k]
        early = server.well_known(request.method, request.path, headers)
        if early is None:
            early, req = await server.prepare(request.method, headers, request.body)
        else:
            req = None
        if early is None and server.wants_stream(req, headers):
            resp = StreamingHttpResponse(server.frames(req, headers),
                                         content_type="text/event-stream")
            resp["Cache-Control"] = "no-cache"
            resp["X-Accel-Buffering"] = "no"
            for k, v in server.stream_headers(req, headers):
                resp[k] = v
            return resp
        out = early if early is not None else await server.respond(req, headers=headers)
        status, payload = out
        status, body = _encode(status, payload)
        resp = HttpResponse(b"" if request.method == "HEAD" else body, status=status,
                            content_type="application/json" if body else None)
        if request.method == "HEAD":
            resp["Content-Length"] = str(len(body))
        for k, v in server.extra_headers(request.method, headers, status,
                                         getattr(out, "headers", ())):
            resp[k] = v
        return resp

    return view


MCP_CONTEXT_HEADER = "X-MCP-Context"


def set_mcp_context(response, text: str, data: dict | None = None):
    """Attach model context to a view's response; `django_routes` forwards it
    to the widget, which pushes it to the model (`ui/update-model-context`).
    Works on redirects too: the last one set along a redirect chain wins.
    Returns the response, so a view can `return set_mcp_context(redirect(...), ...)`."""
    ctx = {"text": text} if data is None else {"text": text, "data": data}
    response[MCP_CONTEXT_HEADER] = json.dumps(ctx, ensure_ascii=True, allow_nan=False)
    return response


_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_CTYPES = frozenset({"", "application/x-www-form-urlencoded", "application/json"})
_FORWARD_RE = re.compile(r"^(hx|fx|datastar)-[a-z0-9-]{1,64}$")
_REDIRECTS = frozenset({301, 302, 303, 307, 308})


def django_routes(mcp, *, prefixes, name: str = "django_http", host: str = "localhost",
                  guards=(), visibility="app", max_redirects: int = 5):
    """Register an app-only tool that serves Django views to an MCP Apps widget.

    The widget's hypermedia requests (`fx-action="/app/todos/"`, sent by
    `micromcp.apps.BRIDGE_JS` when the page has `<meta name="mcp-route"
    content="django_http">`) arrive as tool calls `{method, path, body}` and
    are dispatched in-process through Django's full handler — URL resolver,
    middleware, views, templates — the way Django's test client does. The
    response body comes back as a `fragment()`; same-host redirects are
    followed. Only paths under one of `prefixes` are served.

    The request carries no cookies, so session auth does not apply; the MCP
    request's authenticated principal is set as `request.mcp_principal`, and
    CSRF checks are off because nothing ambient authenticates the request.
    `host` is the Host the views see: it must pass `ALLOWED_HOSTS`.

    Returns the tool's name, for `Widget(route=django_routes(mcp, prefixes=[...]))`.
    """
    import posixpath
    import threading
    from urllib.parse import unquote, urljoin, urlsplit

    from ..apps import fragment

    prefixes = tuple(prefixes)
    if not prefixes or any(not (isinstance(p, str) and p.startswith("/") and p.endswith("/"))
                           for p in prefixes):
        raise ValueError("django_routes: prefixes must be paths like '/app/' "
                         "(leading and trailing /)")
    state = {"handler": None}
    lock = threading.Lock()

    def handler():
        with lock:
            if state["handler"] is None:
                from django.core.handlers.wsgi import WSGIHandler
                state["handler"] = WSGIHandler()        # loads the middleware chain once
            return state["handler"]

    def clean(path):
        """(decoded normalized path, query), or None when outside the prefixes."""
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") \
                or "\\" in path or len(path) > 2048:
            return None
        parts = urlsplit(path)
        if parts.scheme or parts.netloc:
            return None
        decoded = unquote(parts.path)
        if "\x00" in decoded:
            return None
        norm = posixpath.normpath(decoded)
        if decoded.endswith("/") and not norm.endswith("/"):
            norm += "/"
        if not any(norm.startswith(p) or norm + "/" == p for p in prefixes):
            return None
        return norm, parts.query

    def dispatch(method, target, data, who, extra, ctype):
        from django.core import signals
        from django.core.handlers.wsgi import WSGIHandler, WSGIRequest
        p, query = target
        environ = {
            **extra,
            "REQUEST_METHOD": method, "SCRIPT_NAME": "",
            "PATH_INFO": p.encode("utf-8").decode("latin-1"), "QUERY_STRING": query,
            "SERVER_NAME": host, "SERVER_PORT": "443", "SERVER_PROTOCOL": "HTTP/1.1",
            "HTTP_HOST": host, "HTTP_ACCEPT": "text/html", "HTTP_FX_REQUEST": "true",
            "wsgi.url_scheme": "https", "wsgi.input": io.BytesIO(data),
            "wsgi.errors": io.StringIO(), "CONTENT_LENGTH": str(len(data)),
        }
        if data:
            environ["CONTENT_TYPE"] = ctype or "application/x-www-form-urlencoded"
        h = handler()
        signals.request_started.send(sender=WSGIHandler, environ=environ)
        request = WSGIRequest(environ)
        request._dont_enforce_csrf_checks = True   # no cookies ride along; see docstring
        request.mcp_principal = who
        response = h.get_response(request)
        try:
            body = (b"".join(response.streaming_content) if response.streaming
                    else response.content)
        finally:
            response.close()                          # sends request_finished
        return response, body

    def route(method: str, path: str, who: Principal, body: str = "",
              headers: dict[str, str] | None = None, content_type: str = ""):
        """Serve a Django view to this server's MCP App widget (app-only).

        Args:
            method: HTTP method.
            path: Absolute path, optionally with a query string.
            body: Request body, form-encoded unless content_type says JSON.
            headers: Hypermedia request headers (HX-*, FX-*, Datastar-*) for the view.
            content_type: application/x-www-form-urlencoded (default) or application/json.
        """
        method = method.upper()
        if method not in _METHODS:
            return fragment(f"method {method} not allowed", status=405)
        if content_type not in _CTYPES:
            return fragment("unsupported content type", status=415)
        extra = {}
        for k, v in (headers or {}).items():
            k = k.lower()
            if len(extra) >= 16 or not _FORWARD_RE.match(k) or len(v) > 1024 \
                    or not v.isprintable():
                return fragment(f"header {k[:64]!r} is not forwarded to Django", status=400)
            extra["HTTP_" + k.upper().replace("-", "_")] = v
        data = body.encode("utf-8")
        pushed = None                                 # set_mcp_context() anywhere in the chain
        for _ in range(max_redirects + 1):
            target = clean(path)
            if target is None:
                return fragment("path not served to MCP Apps", status=404)
            response, content = dispatch(method, target, data, who, extra, content_type)
            pushed = response.get(MCP_CONTEXT_HEADER) or pushed
            loc = response.get("Location") if response.status_code in _REDIRECTS else None
            if not loc:
                break
            nxt = urlsplit(urljoin(target[0], loc))
            if nxt.netloc and nxt.netloc != host:
                return fragment("redirect leaves the application", status=502)
            path = nxt.path + (f"?{nxt.query}" if nxt.query else "")
            code = response.status_code
            if code == 303 or (code in (301, 302) and method == "POST"):
                method, data = "GET", b""
        else:
            return fragment("too many redirects", status=508)
        text = content.decode(response.charset or "utf-8", errors="replace")
        ctype = response.get("Content-Type", "text/html; charset=utf-8")
        if pushed:
            try:
                return fragment(text, status=response.status_code, content_type=ctype,
                                context=json.loads(pushed))
            except ValueError:
                log.warning("dropping an invalid %s header from %s", MCP_CONTEXT_HEADER, path)
        return fragment(text, status=response.status_code, content_type=ctype)

    mcp.tool(route, name=name, guards=guards, visibility=visibility,
             title="Django views for the MCP App")
    return name
