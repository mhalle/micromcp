"""The two parameter annotations the server fills in itself."""

from __future__ import annotations


class Context:
    """Injected handler parameter for streaming progress and log messages.

        @mcp.tool
        async def segment(volume: str, ctx: Context) -> dict:
            for i, step in enumerate(steps, 1):
                await ctx.report_progress(i, len(steps), step.name)
            await ctx.info("done")

    Declaring this parameter is what makes a tool stream: the ASGI transport
    answers such a call with an SSE stream instead of a single JSON object,
    provided the client's Accept header admits text/event-stream. Like
    Principal it is filled server-side and hidden from the input schema.

    Under WSGI — which has no portable way to detect a client hang-up, and so
    cannot honor the spec's cancellation rule — notifications are dropped and
    the call still returns its normal JSON result.

    Notification payloads must be JSON-serializable; a bad one raises inside
    the handler (surfacing as an in-band isError) rather than tearing the
    stream down. A sync (`def`) handler cannot be interrupted by cancellation;
    poll `ctx.cancelled` from long loops to stop cooperatively.
    """

    def __init__(self, emit=None, progress_token=None, stop=None, meta=None):
        self._emit = emit
        self._token = progress_token
        self._stop = stop
        self._meta = dict(meta or {})

    @property
    def request_meta(self) -> dict:
        """The request's `_meta` as sent by the client (a copy)."""
        return dict(self._meta)

    @property
    def client_capabilities(self) -> dict:
        """`_meta["io.modelcontextprotocol/clientCapabilities"]`, e.g. to check for
        `extensions["io.modelcontextprotocol/ui"]` and degrade to a text-only result."""
        return dict(self._meta.get("io.modelcontextprotocol/clientCapabilities") or {})

    @property
    def client_info(self) -> dict:
        """`_meta["io.modelcontextprotocol/clientInfo"]` (name/version), when the
        client sent it; empty for a 2025-era client, whose identity travelled in
        an initialize this stateless server did not keep."""
        return dict(self._meta.get("io.modelcontextprotocol/clientInfo") or {})

    @property
    def streaming(self) -> bool:
        """False when nothing is listening (WSGI, or a client sending no token)."""
        return self._emit is not None

    @property
    def cancelled(self) -> bool:
        """True once the client has gone away. Sync handlers should poll this."""
        return self._stop is not None and self._stop.is_set()

    async def report_progress(self, progress, total=None, message=None):
        # No token means the client did not opt in; there is nothing to
        # correlate the notification to, so dropping it is the correct behavior.
        if self._emit is None or self._token is None:
            return
        params = {"progressToken": self._token, "progress": float(progress)}
        if total is not None:
            params["total"] = float(total)
        if message is not None:
            params["message"] = message
        await self._emit("notifications/progress", params)

    async def log(self, level, data, logger=None):
        if self._emit is None:
            return
        params = {"level": level, "data": data}
        if logger:
            params["logger"] = logger
        await self._emit("notifications/message", params)

    async def debug(self, data, **k): await self.log("debug", data, **k)
    async def info(self, data, **k): await self.log("info", data, **k)
    async def warning(self, data, **k): await self.log("warning", data, **k)
    async def error(self, data, **k): await self.log("error", data, **k)


class Principal:
    """Marker annotation: this parameter receives the authenticated principal.

        @mcp.tool
        def whoami(who: Principal) -> dict:
            return {"sub": who["sub"] if who else None}

    Parameters annotated this way are filled server-side and are excluded from
    the input schema, so the model never sees them and cannot supply them.
    Detection works through Optional, Union, Annotated, and PEP 695 aliases,
    and through an otherwise-unresolvable signature (PEP 563 / 649) as long as
    `micromcp.Principal` itself is importable at runtime. An annotation that
    merely *reads* `Principal` but does not resolve to this class, or a
    subclass of it, is refused at registration rather than guessed at.
    """


_MARKERS = (Principal, Context)
