"""Django adapters. Django is imported lazily, when a view is built."""

from __future__ import annotations

import io

from ..asgi import ASGIServer
from ..core import _encode
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
        own = ()
        if early is None and server.wants_stream(req, headers):
            resp = StreamingHttpResponse(server.frames(req, headers),
                                         content_type="text/event-stream")
            resp["Cache-Control"] = "no-cache"
            resp["X-Accel-Buffering"] = "no"
            status = 200
        else:
            out = early if early is not None else await server.respond(req, headers=headers)
            status, payload = out
            own = getattr(out, "headers", ())
            status, body = _encode(status, payload)
            resp = HttpResponse(body, status=status,
                                content_type="application/json" if body else None)
        for k, v in server.extra_headers(request.method, headers, status, own):
            resp[k] = v
        return resp

    return view
