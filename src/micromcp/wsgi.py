"""The WSGI transport: `Server`, plus the background loop that runs it."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading

from ._constants import INTERNAL_ERROR, INVALID_REQUEST, log
from .core import _STATUS, _Core, _Fork, _encode
from .errors import _err
from .registry import _is_async


class _Loop:
    """A dedicated background event loop, so the WSGI path can await handlers.

    Started on the first request and reused for the life of the process. WSGI
    has no event loop of its own, so this is what lets `async def` handlers work
    under gunicorn or waitress. The loop thread does not survive fork(), so a
    child process (gunicorn --preload, os.fork) gets a fresh one; the
    process-wide fork generation tracks that, so instances are not pinned. A
    loop that stopped for any other reason is rebuilt too.
    """

    def __init__(self):
        self._loop = None
        self._thread = None
        self._gen = None
        self._lock = threading.Lock()

    def run(self, coro, timeout=None):
        if self._gen != _Fork.gen:            # forked: the old lock may be held
            self._lock = threading.Lock()
        with self._lock:
            if (self._loop is None or self._gen != _Fork.gen or self._loop.is_closed()
                    or not self._thread.is_alive()):
                self._loop = asyncio.new_event_loop()
                self._gen = _Fork.gen
                self._thread = threading.Thread(target=self._loop.run_forever,
                                                daemon=True, name="micromcp-loop")
                self._thread.start()
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise


class Server(_Core):
    """WSGI app serving one MCP endpoint.

    Async handlers are awaited on a background loop; sync handlers run on the
    server's thread pool, so concurrent requests overlap either way. With
    `path=` and `fallback=`, another WSGI app answers every other path.
    """

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        if self.fallback is not None and _is_async(self.fallback):
            raise TypeError("Server's fallback must be a WSGI app; an ASGI app (an async "
                            "callable) goes to ASGIServer(fallback=...)")
        self._runner = _Loop()

    def __call__(self, environ, start_response):
        headers = {k[5:].replace("_", "-").lower(): v
                   for k, v in environ.items() if k.startswith("HTTP_")}
        for k in ("CONTENT_TYPE", "CONTENT_LENGTH"):       # not HTTP_-prefixed in WSGI
            if environ.get(k):
                headers[k.replace("_", "-").lower()] = environ[k]
        http_method = environ.get("REQUEST_METHOD", "POST")
        full_path = (environ.get("SCRIPT_NAME", "") or "") + (environ.get("PATH_INFO", "") or "")

        if self.to_fallback(full_path):
            # A Host value never holds a comma: one means repeated headers
            # joined into one (as gunicorn does), so count it as more than one.
            reply = self.fallback_refusal(headers, 1 + headers.get("host", "").count(","))
            if reply is None:
                return self.fallback(environ, start_response)
        else:
            reply = self.document(http_method, full_path, headers)
        if reply is not None:
            pass
        elif not self.path_ok(full_path):
            reply = _err(404, None, INVALID_REQUEST, "no MCP endpoint at this path")
        else:
            try:
                n = int(environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                n = 0
            chunked = "chunked" in environ.get("HTTP_TRANSFER_ENCODING", "").lower()
            if n > self.max_body:
                reply = self.too_large()
            elif http_method == "POST" and n <= 0 and chunked \
                    and not environ.get("wsgi.input_terminated"):
                reply = _err(411, None, INVALID_REQUEST, "Content-Length required")
            else:
                if n > 0:
                    raw = environ["wsgi.input"].read(n)
                elif environ.get("wsgi.input_terminated"):   # de-chunked by the server
                    raw = environ["wsgi.input"].read(self.max_body + 1)
                else:
                    raw = b""
                if len(raw) > self.max_body:
                    reply = self.too_large()
                else:
                    try:
                        reply = self._runner.run(
                            self.handle(http_method, headers, raw), self.timeout)
                    except concurrent.futures.TimeoutError:
                        log.error("request exceeded timeout=%ss", self.timeout)
                        reply = _err(500, None, INTERNAL_ERROR, "Internal error")

        status, payload = reply
        status, data = _encode(status, payload)
        own = getattr(reply, "headers", ())
        # A 304's length would be the 200's, so it sends none; an answer that
        # names its own Content-Type (the Server Card) keeps it.
        hdrs = [] if status == 304 else [("Content-Length", str(len(data)))]
        if data and not any(k.lower() == "content-type" for k, _ in own):
            hdrs.insert(0, ("Content-Type", "application/json"))
        if http_method == "HEAD":                     # same head as GET, no body
            data = b""
        hdrs += self.extra_headers(http_method, headers, status, own)
        start_response(_STATUS.get(status, f"{status} Error"), hdrs)
        return [data]
