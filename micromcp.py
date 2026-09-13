"""micromcp — a minimum-viable MCP server for the stateless 2026-07-28 revision.

Pure stdlib. No dependencies. No framework. Two transports over one core:
`Server` is a WSGI app (Django, Flask, gunicorn, waitress, wsgiref) and
`ASGIServer` is a native ASGI app (Starlette, FastAPI, Litestar, uvicorn).
Handlers may be sync or `async def` on either.

Scope: tools, resources, prompts. JSON responses only.
Out of scope (deliberately): SSE/progress, MRTR/elicitation, subscriptions,
OAuth, legacy session-based protocol revisions.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import inspect
import io
import json
import re
import threading
import types
import typing

PROTOCOL = "2026-07-28"
META_VER = "io.modelcontextprotocol/protocolVersion"
META_CAPS = "io.modelcontextprotocol/clientCapabilities"

# JSON-RPC / MCP error codes
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
HEADER_MISMATCH = -32020         # spec-allocated
UNSUPPORTED_VERSION = -32022     # spec-allocated; must carry data.supported


# ---------------------------------------------------------------- schema gen
_PRIM = {int: "integer", float: "number", str: "string", bool: "boolean",
         list: "array", dict: "object"}


class Context:
    """Injected handler parameter for streaming progress and log messages.

        @mcp.tool
        async def segment(volume: str, ctx: Context) -> dict:
            for i, step in enumerate(steps, 1):
                await ctx.report_progress(i, len(steps), step.name)
            await ctx.info("done")

    Declaring this parameter is what makes a tool stream: the ASGI transport
    answers such a call with an SSE stream instead of a single JSON object.
    Like Principal it is filled server-side and hidden from the input schema.

    Under WSGI — which has no portable way to detect a client hang-up, and so
    cannot honor the spec's cancellation rule — notifications are dropped and
    the call still returns its normal JSON result.
    """

    def __init__(self, emit=None, progress_token=None):
        self._emit = emit
        self._token = progress_token

    @property
    def streaming(self) -> bool:
        """False when nothing is listening (WSGI, or a client sending no token)."""
        return self._emit is not None

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
    """


def _hints(fn) -> dict:
    """Resolved type hints, tolerating `from __future__ import annotations`."""
    try:
        return typing.get_type_hints(fn)
    except Exception:
        return {}


def _schema(ann) -> dict:
    """Map a type annotation to a JSON Schema fragment."""
    if ann is inspect.Parameter.empty or ann is None:
        return {}
    if ann in _PRIM:
        return {"type": _PRIM[ann]}
    if typing.is_typeddict(ann) or dataclasses.is_dataclass(ann):
        return _object_schema(ann)
    origin = typing.get_origin(ann)
    if origin in (typing.Union, types.UnionType):
        return {"anyOf": [_schema(a) if a is not type(None) else {"type": "null"}
                          for a in typing.get_args(ann)]}
    if origin in (list, set, tuple):
        args = typing.get_args(ann)
        return {"type": "array", **({"items": _schema(args[0])} if args else {})}
    if origin is dict:
        return {"type": "object"}
    return {}


def _object_schema(ann) -> dict:
    """Build an object schema from a TypedDict or dataclass — used for outputSchema."""
    if dataclasses.is_dataclass(ann):
        fields = {f.name: f.type for f in dataclasses.fields(ann)}
        optional = {f.name for f in dataclasses.fields(ann)
                    if f.default is not dataclasses.MISSING
                    or f.default_factory is not dataclasses.MISSING}  # type: ignore[misc]
    else:
        fields = typing.get_type_hints(ann)
        optional = set(fields) - set(getattr(ann, "__required_keys__", fields))
    resolved = typing.get_type_hints(ann) if not dataclasses.is_dataclass(ann) else None
    props = {k: _schema(resolved[k] if resolved else _resolve(ann, k, v))
             for k, v in fields.items()}
    out = {"type": "object", "properties": props}
    required = [k for k in fields if k not in optional]
    if required:
        out["required"] = required
    return out


def _resolve(owner, name, ann):
    """Dataclass field types can be strings; resolve them against the module."""
    if not isinstance(ann, str):
        return ann
    try:
        return typing.get_type_hints(owner)[name]
    except Exception:
        return None


def _input_schema(fn) -> tuple[dict, dict]:
    """Return (inputSchema, {"principal": name|None, "context": name|None})."""
    hints = _hints(fn)
    props, required = {}, []
    inject = {"principal": None, "context": None}
    for name, p in inspect.signature(fn).parameters.items():
        if name in ("self", "request"):
            continue
        ann = hints.get(name, p.annotation)
        if ann is Principal:
            inject["principal"] = name    # injected, never exposed to the model
            continue
        if ann is Context:
            inject["context"] = name
            continue
        s = _schema(ann)
        if p.default is inspect.Parameter.empty:
            required.append(name)
        else:
            s = {**s, "default": p.default}
        props[name] = s
    out = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out, inject


def _output_schema(fn, explicit):
    if explicit is not None:
        return explicit
    ret = _hints(fn).get("return")
    if ret is not None and (typing.is_typeddict(ret) or dataclasses.is_dataclass(ret)):
        return _object_schema(ret)
    return None          # a bare `-> dict` says nothing worth declaring


# ---------------------------------------------------------------- registry
_TEMPLATE_RE = re.compile(r"\{(\w+)\}")


def _compile_template(uri: str):
    """`crash://{crash_id}` -> a regex capturing crash_id."""
    parts = _TEMPLATE_RE.split(uri)
    if len(parts) == 1:
        return None, []
    rx = "".join(f"(?P<{p}>[^/]+)" if i % 2 else re.escape(p)
                 for i, p in enumerate(parts))
    return re.compile(f"^{rx}$"), parts[1::2]


def _coerce(value: str, ann):
    if ann is int:
        return int(value)
    if ann is float:
        return float(value)
    if ann is bool:
        return value.lower() in ("1", "true", "yes")
    return value


class MCP:
    def __init__(self, name: str, version: str = "0.1.0"):
        self.name, self.version = name, version
        self.tools: dict[str, dict] = {}
        self.resources: dict[str, dict] = {}
        self.templates: dict[str, dict] = {}
        self.prompts: dict[str, dict] = {}

    def tool(self, fn=None, *, name=None, title=None, guards=(),
             annotations=None, output_schema=None, read_only=None,
             destructive=None, idempotent=None):
        """Register a tool.

        title       human-readable label for UIs
        read_only / destructive / idempotent
                    shorthands for the standard annotation hints clients use to
                    decide what to auto-approve; merged into `annotations`
        output_schema
                    declares the shape of structuredContent; inferred from a
                    TypedDict or dataclass return annotation when omitted
        """
        def wrap(f):
            n = name or f.__name__
            schema, inject = _input_schema(f)
            hints = {"readOnlyHint": read_only, "destructiveHint": destructive,
                     "idempotentHint": idempotent}
            ann = {**{k: v for k, v in hints.items() if v is not None},
                   **(annotations or {})}
            entry = {
                "name": n,
                "description": inspect.getdoc(f) or "",
                "inputSchema": schema,
                "_fn": f, "_guards": guards,
                "_principal": inject["principal"], "_context": inject["context"],
            }
            if title:
                entry["title"] = title
            if ann:
                entry["annotations"] = ann
            out = _output_schema(f, output_schema)
            if out:
                entry["outputSchema"] = out
            self.tools[n] = entry
            return f
        return wrap(fn) if fn else wrap

    def resource(self, uri: str, *, mime_type="text/plain", title=None, guards=()):
        """Register a resource. A `{braced}` segment makes it a template:

            @mcp.resource("crash://{crash_id}")
            def one(crash_id: int) -> str: ...

        Template parameters are coerced using the handler's type hints.
        """
        def wrap(f):
            entry = {"name": f.__name__, "mimeType": mime_type,
                     "_fn": f, "_guards": guards}
            if title:
                entry["title"] = title
            rx, params = _compile_template(uri)
            if rx is not None:
                hints = _hints(f)
                self.templates[uri] = {**entry, "uriTemplate": uri,
                                       "_re": rx, "_params": params, "_hints": hints}
            else:
                self.resources[uri] = {**entry, "uri": uri}
            return f
        return wrap

    def prompt(self, fn=None, *, name=None, title=None):
        def wrap(f):
            n = name or f.__name__
            entry = {
                "name": n,
                "description": inspect.getdoc(f) or "",
                "arguments": [
                    {"name": k, "required": p.default is inspect.Parameter.empty}
                    for k, p in inspect.signature(f).parameters.items()
                ],
                "_fn": f,
            }
            if title:
                entry["title"] = title
            self.prompts[n] = entry
            return f
        return wrap(fn) if fn else wrap

    def match_resource(self, uri: str):
        """Resolve a URI to (entry, kwargs). Exact hits win over templates."""
        if uri in self.resources:
            return self.resources[uri], {}
        for entry in self.templates.values():
            m = entry["_re"].match(uri)
            if m:
                kw = {k: _coerce(v, entry["_hints"].get(k, str))
                      for k, v in m.groupdict().items()}
                return entry, kw
        return None, {}


# ---------------------------------------------------------------- transport
class Error(Exception):
    def __init__(self, code, message, status=400, data=None):
        self.code, self.message, self.status, self.data = code, message, status, data


def _decode_hdr(v: str | None) -> str | None:
    """Undo the =?base64?...?= sentinel the spec defines for unsafe values."""
    if v and v.startswith("=?base64?") and v.endswith("?="):
        return base64.b64decode(v[9:-2]).decode()
    return v


def _as_content(value) -> dict:
    if isinstance(value, (dict, list)):
        out = {"content": [{"type": "text", "text": json.dumps(value)}]}
        if isinstance(value, dict):
            out["structuredContent"] = value
        return out
    return {"content": [{"type": "text", "text": str(value)}]}


class _Core:
    """Transport-neutral MCP logic: validate, dispatch, wrap.

    Both transports below feed it a lowercase header mapping and a parsed body,
    and get back (http_status, json_payload). Nothing here knows about WSGI,
    ASGI, sockets, or frameworks.
    """

    def __init__(self, mcp: MCP, *, allowed_origins=(), authenticate=None):
        self.mcp = mcp
        self.allowed_origins = set(allowed_origins)
        self.authenticate = authenticate  # (headers: dict[str, str]) -> principal | None

    # -- validation required by the spec ---------------------------------
    def _validate(self, headers, body):
        origin = headers.get("origin")
        if origin and origin not in self.allowed_origins:
            raise Error(HEADER_MISMATCH, "Origin not allowed", status=403)

        ver = headers.get("mcp-protocol-version")
        method = body.get("method")
        params = body.get("params") or {}
        meta = params.get("_meta") or {}

        if not ver:
            raise Error(HEADER_MISMATCH, "missing MCP-Protocol-Version header")
        if ver != meta.get(META_VER):
            raise Error(HEADER_MISMATCH,
                        f"MCP-Protocol-Version {ver!r} != body _meta {meta.get(META_VER)!r}")
        if ver != PROTOCOL:
            # -32022, not -32020: the client reads data.supported to renegotiate.
            raise Error(UNSUPPORTED_VERSION,
                        f"unsupported protocol version {ver!r}",
                        data={"supported": [PROTOCOL]})
        if META_CAPS not in meta:
            raise Error(INVALID_PARAMS, f"request _meta missing {META_CAPS}")

        if _decode_hdr(headers.get("mcp-method")) != method:
            raise Error(HEADER_MISMATCH, "Mcp-Method header does not match body method")

        if method in ("tools/call", "resources/read", "prompts/get"):
            want = params.get("name") or params.get("uri")
            if _decode_hdr(headers.get("mcp-name")) != want:
                raise Error(HEADER_MISMATCH,
                            f"Mcp-Name header does not match body value {want!r}")
        return method, params

    # -- dispatch ---------------------------------------------------------
    async def _dispatch(self, method, params, principal, emit=None):
        """Async so handlers may be `async def`. Sync handlers work unchanged."""
        m = self.mcp

        async def run(fn, kwargs=None):
            out = fn(**(kwargs or {}))
            return await out if inspect.isawaitable(out) else out

        if method == "server/discover":
            # Era negotiation. A modern client probes this FIRST; answering it
            # is what stops the client falling back to the legacy handshake.
            return {
                "supportedVersions": [PROTOCOL],
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": {"name": m.name, "version": m.version},
            }
        if method == "tools/list":
            return {"tools": [{k: v for k, v in t.items() if not k.startswith("_")}
                              for t in m.tools.values()]}
        if method == "resources/list":
            return {"resources": [{k: v for k, v in r.items() if not k.startswith("_")}
                                  for r in m.resources.values()]}
        if method == "resources/templates/list":
            return {"resourceTemplates": [
                {k: v for k, v in t.items() if not k.startswith("_")}
                for t in m.templates.values()]}
        if method == "prompts/list":
            return {"prompts": [{k: v for k, v in p.items() if not k.startswith("_")}
                                for p in m.prompts.values()]}

        if method == "tools/call":
            entry = m.tools.get(params.get("name"))
            if not entry:
                raise Error(METHOD_NOT_FOUND, f"no such tool {params.get('name')!r}", 404)
            for g in entry["_guards"]:
                if not g(principal):
                    # Tool-level failures are in-band results, not JSON-RPC errors.
                    return {"content": [{"type": "text",
                                         "text": f"Permission denied for tool "
                                                 f"{entry['name']!r}"}],
                            "isError": True}
            kwargs = dict(params.get("arguments") or {})
            if entry["_principal"]:
                kwargs[entry["_principal"]] = principal
            if entry["_context"]:
                token = (params.get("_meta") or {}).get("progressToken")
                kwargs[entry["_context"]] = Context(emit, token)
            try:
                return _as_content(await run(entry["_fn"], kwargs))
            except Exception as exc:
                return {"content": [{"type": "text", "text": str(exc)}], "isError": True}

        if method == "resources/read":
            uri = params.get("uri")
            entry, kwargs = m.match_resource(uri)
            if not entry:
                raise Error(METHOD_NOT_FOUND, f"no such resource {uri!r}", 404)
            return {"contents": [{"uri": uri, "mimeType": entry["mimeType"],
                                  "text": await run(entry["_fn"], kwargs)}]}

        if method == "prompts/get":
            entry = m.prompts.get(params.get("name"))
            if not entry:
                raise Error(METHOD_NOT_FOUND, f"no such prompt {params.get('name')!r}", 404)
            out = await run(entry["_fn"], params.get("arguments"))
            msgs = out if isinstance(out, list) else [
                {"role": "user", "content": {"type": "text", "text": out}}]
            return {"messages": msgs}

        raise Error(METHOD_NOT_FOUND, f"unknown method {method!r}", 404)

    # -- one request, start to finish -------------------------------------
    def wants_stream(self, raw) -> bool:
        """True when the addressed tool declares a Context parameter.

        Decided from the request alone, BEFORE any bytes are written — once an
        SSE stream is open there is no way back to a JSON error response.
        """
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            return False
        if body.get("method") != "tools/call":
            return False
        entry = self.mcp.tools.get((body.get("params") or {}).get("name"))
        return bool(entry and entry.get("_context"))

    async def handle(self, http_method, headers, raw, emit=None):
        """Return (status_code, payload). The only entry point transports need.

        `emit(method, params)` is an async sink for notifications; None means
        nothing is listening and Context silently drops them.
        """
        if http_method != "POST":
            # Legacy GET/DELETE session mechanics are gone in this revision.
            return 405, {"error": "POST only"}
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            return 400, {"jsonrpc": "2.0", "id": None,
                         "error": {"code": -32700, "message": "Parse error"}}

        rid = body.get("id")
        try:
            method, params = self._validate(headers, body)
            principal = self.authenticate(headers) if self.authenticate else None
            result = await self._dispatch(method, params, principal, emit)
            # 2026-07-28 requires these on EVERY result. They have SDK-side
            # defaults when parsing, but the strict per-version schema that
            # governs a live connection demands them explicitly.
            result = {"resultType": "complete", "ttlMs": 0,
                      "cacheScope": "private", **result}
            return 200, {"jsonrpc": "2.0", "id": rid, "result": result}
        except Error as e:
            err = {"code": e.code, "message": e.message}
            if e.data:
                err["data"] = e.data
            return e.status, {"jsonrpc": "2.0", "id": rid, "error": err}
        except Exception as e:  # pragma: no cover
            return 500, {"jsonrpc": "2.0", "id": rid,
                         "error": {"code": -32603, "message": str(e)}}


_STATUS = {200: "200 OK", 400: "400 Bad Request", 403: "403 Forbidden",
           404: "404 Not Found", 405: "405 Method Not Allowed",
           500: "500 Internal Server Error"}


class _Loop:
    """A dedicated background event loop, so the WSGI path can await handlers.

    Started on the first request and reused for the life of the process. WSGI
    has no event loop of its own, so this is what lets `async def` handlers work
    under gunicorn or waitress.
    """

    def __init__(self):
        self._loop = None
        self._lock = threading.Lock()

    def run(self, coro):
        with self._lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._loop.run_forever,
                                 daemon=True, name="micromcp-loop").start()
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()


class Server(_Core):
    """WSGI app serving one MCP endpoint.

    Async handlers are supported here too, but they run on a background loop —
    fine for occasional use, though ASGIServer is the better home for them.
    """

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._runner = _Loop()

    def __call__(self, environ, start_response):
        headers = {k[5:].replace("_", "-").lower(): v
                   for k, v in environ.items() if k.startswith("HTTP_")}
        n = int(environ.get("CONTENT_LENGTH") or 0)
        raw = environ["wsgi.input"].read(n) if n else b""

        status, payload = self._runner.run(
            self.handle(environ.get("REQUEST_METHOD", "POST"), headers, raw))

        data = json.dumps(payload).encode()
        start_response(_STATUS.get(status, f"{status} Error"),
                       [("Content-Type", "application/json"),
                        ("Content-Length", str(len(data)))])
        return [data]


class ASGIServer(_Core):
    """Native ASGI app serving one MCP endpoint.

    No threadpool bridge: `async def` handlers are awaited on the caller's own
    event loop, so a slow tool yields instead of occupying a worker thread.
    Mount it directly in Starlette/FastAPI/Litestar, or run it under uvicorn.
    """

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return

        if scope["type"] != "http":  # websocket etc.
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}

        raw, more = b"", True
        while more:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            raw += msg.get("body", b"")
            more = msg.get("more_body", False)

        http_method = scope.get("method", "POST")
        if http_method == "POST" and self.wants_stream(raw):
            return await self._stream(headers, raw, receive, send)

        status, payload = await self.handle(http_method, headers, raw)

        data = json.dumps(payload).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(data)).encode())]})
        await send({"type": "http.response.body", "body": data})

    async def _stream(self, headers, raw, receive, send):
        """Answer one request with a request-scoped SSE stream.

        Notifications first, then the final JSON-RPC response, which closes the
        stream. Closing the stream from the client side is cancellation: the
        handler task is cancelled and nothing further is sent.
        """
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"text/event-stream"),
            (b"cache-control", b"no-cache"),
            # Tell nginx and friends not to buffer, or frames arrive in a lump.
            (b"x-accel-buffering", b"no"),
        ]})

        queue: asyncio.Queue = asyncio.Queue()

        async def emit(method, params):
            await queue.put({"jsonrpc": "2.0", "method": method, "params": params})

        async def run():
            try:
                _status, payload = await self.handle("POST", headers, raw, emit=emit)
                await queue.put(payload)
            finally:
                await queue.put(None)          # end-of-stream sentinel

        async def disconnected():
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    return

        worker = asyncio.create_task(run())
        watcher = asyncio.create_task(disconnected())
        try:
            while True:
                nxt = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait({nxt, watcher},
                                             return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:            # client hung up: stop work, send nothing
                    nxt.cancel()
                    worker.cancel()
                    return
                item = nxt.result()
                if item is None:
                    break
                await send({"type": "http.response.body",
                            "body": b"data: " + json.dumps(item).encode() + b"\n\n",
                            "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            watcher.cancel()
            if not worker.done():
                worker.cancel()


# ---------------------------------------------------------------- adapters
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
        chunks = server(environ, lambda s, h: box.update(status=s))
        return HttpResponse(b"".join(chunks), status=int(box["status"].split()[0]),
                            content_type="application/json")

    return view


def django_async_view(server: ASGIServer):
    """Wrap an ASGIServer as an async Django view, awaiting handlers natively."""
    from django.http import HttpResponse
    from django.views.decorators.csrf import csrf_exempt

    @csrf_exempt
    async def view(request):
        headers = {k[5:].replace("_", "-").lower(): v
                   for k, v in request.META.items() if k.startswith("HTTP_")}
        status, payload = await server.handle(request.method, headers, request.body)
        return HttpResponse(json.dumps(payload), status=status,
                            content_type="application/json")

    return view
