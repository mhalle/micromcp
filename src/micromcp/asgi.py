"""The native ASGI transport: `ASGIServer`, with request-scoped SSE streams."""

from __future__ import annotations

import asyncio

from ._constants import INVALID_REQUEST
from .core import _Core, _encode
from .errors import _err


class ASGIServer(_Core):
    """Native ASGI app serving one MCP endpoint.

    `async def` handlers are awaited on the caller's own event loop; `def`
    handlers are pushed to the server's thread pool so they cannot stall it.
    Mount it directly in Starlette/FastAPI/Litestar, or run it under uvicorn.
    """

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    self.close()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return

        if scope["type"] != "http":  # websocket etc.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1003})
            return

        http_method = scope.get("method", "POST")
        names = [k.decode("latin-1").lower() for k, _ in scope.get("headers", [])]
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}

        async def reply(status, payload, extra=()):
            status, data = _encode(status, payload)
            hdrs = [(b"content-length", str(len(data)).encode())]
            if data:
                hdrs.insert(0, (b"content-type", b"application/json"))
            if http_method == "HEAD":                 # same head as GET, no body
                data = b""
            hdrs += [(k.lower().encode("latin-1"), v.encode("latin-1", "replace"))
                     for k, v in self.extra_headers(http_method, headers, status, extra)]
            await send({"type": "http.response.start", "status": status, "headers": hdrs})
            await send({"type": "http.response.body", "body": data})

        early = self.reject_duplicates({n for n in names if names.count(n) > 1})
        if early is None:
            early = self.well_known(http_method, scope.get("path", "/"), headers)
        if early is None and not self.path_ok(scope.get("path", "/")):
            early = _err(404, None, INVALID_REQUEST, "no MCP endpoint at this path")
        try:
            declared = int(headers.get("content-length") or 0)
        except ValueError:
            declared = 0
        if early is None and declared > self.max_body:
            early = self.too_large()               # refuse before reading a byte
        if early is not None:
            return await reply(*early, getattr(early, "headers", ()))

        chunks, size, more = [], 0, True
        while more:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            chunk = msg.get("body", b"")
            size += len(chunk)
            if size > self.max_body:
                tl = self.too_large()
                return await reply(*tl, getattr(tl, "headers", ()))   # do not drain the rest
            chunks.append(chunk)
            more = msg.get("more_body", False)
        raw = b"".join(chunks)

        early, req = await self.prepare(http_method, headers, raw)
        if early is None and self.wants_stream(req, headers):
            return await self._stream(req, headers, receive, send)
        out = early if early is not None else await self.respond(req, headers=headers)
        status, payload = out
        await reply(status, payload, getattr(out, "headers", ()))

    async def _stream(self, req, headers, receive, send):
        """Answer one validated request with a request-scoped SSE stream.

        Notifications first, then the final JSON-RPC response, which closes the
        stream. Closing the stream from the client side is cancellation: the
        handler task is cancelled and nothing further is sent. The frame queue
        is bounded, so a chatty handler paces itself against the socket instead
        of growing memory — and always reaches a cancellation point. A comment
        frame goes out after `keepalive` seconds of silence so proxies keep the
        connection open through a long-running handler.
        """
        hdrs = [(b"content-type", b"text/event-stream"),
                (b"cache-control", b"no-cache"),
                # Tell nginx and friends not to buffer, or frames arrive in a lump.
                (b"x-accel-buffering", b"no")]
        hdrs += [(k.lower().encode("latin-1"), v.encode("latin-1", "replace"))
                 for k, v in self.stream_headers(req, headers)]
        await send({"type": "http.response.start", "status": 200, "headers": hdrs})

        q, worker, stop = self.spawn(req, headers)

        async def disconnected():
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    return

        async def write(chunk: bytes) -> bool:
            try:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
                return True
            except Exception:              # transport closed under us
                return False

        watcher = asyncio.create_task(disconnected())
        nxt = None
        gone = False
        try:
            while True:
                if nxt is None:
                    nxt = asyncio.create_task(q.get())
                done, _ = await asyncio.wait({nxt, watcher}, timeout=self.keepalive,
                                             return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:            # client hung up: stop work, send nothing
                    nxt.cancel()
                    gone = True
                    break
                if nxt not in done:            # silence: keep the connection alive
                    if not await write(b": keepalive\n\n"):
                        gone = True
                        break
                    continue
                item, nxt = nxt.result(), None
                if item is None:
                    break
                if not await write(item):
                    gone = True
                    break
            if not gone:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            watcher.cancel()
            if nxt is not None:
                nxt.cancel()
            await self.finish(q, worker, stop, req)
