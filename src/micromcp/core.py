"""Transport-neutral request handling: validate, authenticate, dispatch, stream."""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import contextvars
import dataclasses
import datetime
import enum
import functools
import inspect
import json
import os
import pathlib
import queue
import re
import threading

from ._constants import (
    CANCEL_GRACE, CORS_HEADERS, HANDSHAKE_METHODS, HEADER_MISMATCH, INTERNAL_ERROR,
    INVALID_PARAMS, INVALID_REQUEST, KEEPALIVE, LIST_METHODS, MAX_BODY, MAX_URI,
    META_CAPS, META_SERVER, META_VER, METHOD_NOT_FOUND, OFFLOAD_BYTES, PARSE_ERROR,
    PROTOCOL, QUEUE_SIZE, ROUTING_HEADERS, SINGLETON_HEADERS, STREAM_BUDGET,
    UNSUPPORTED_VERSION, WORKERS, log,
)
from .errors import Error, _err
from .markers import Context
from .registry import MCP, _allowed, _coerce, _is_async
from .schema import _check, _from_json

_SENTINEL_RE = re.compile(r"^=\?base64\?(.*)\?=$")


def _decode_hdr(v: str | None) -> str | None:
    """Undo the =?base64?...?= sentinel the spec defines for unsafe values.

    Mirrors the SDK codec: strict base64, canonical (re-encodes to the same
    bytes), valid UTF-8. Anything else decodes to None, which can never equal
    a body value — so a corrupt header is a mismatch, not a crash.
    """
    if not v:
        return v
    m = _SENTINEL_RE.match(v)
    if not m:
        return v
    try:
        raw = base64.b64decode(m.group(1), validate=True)
        if base64.b64encode(raw).decode() != m.group(1):
            return None
        return raw.decode()
    except Exception:
        return None


def _accepts(headers, mime: str) -> bool:
    """RFC 7231 Accept check with wildcards. No Accept header accepts anything."""
    accept = headers.get("accept")
    if not accept:
        return True
    major = mime.split("/", 1)[0]
    for part in accept.split(","):
        m = part.split(";", 1)[0].strip().lower()
        if m in (mime, "*/*", f"{major}/*"):
            return True
    return False


def _jsonable(o):
    """json.dumps default= for handler results: dataclasses, pydantic, sets, dates."""
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    if callable(getattr(o, "model_dump", None)):
        return o.model_dump(mode="json")
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    if isinstance(o, (datetime.date, datetime.datetime, datetime.time)):
        return o.isoformat()
    if isinstance(o, (pathlib.PurePath, enum.Enum)):
        return o.value if isinstance(o, enum.Enum) else str(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def embedded_resource(uri: str, *, mime_type: str = "text/html", text: str | None = None,
                      blob: bytes | None = None, meta: dict | None = None) -> dict:
    """An embedded-resource content block, the shape MCP-UI and MCP Apps hosts
    render: return it (inside a `content` list) from a tool handler.

        return {"content": [embedded_resource("ui://chart/1", text=html,
                                               mime_type="text/html;profile=mcp-app")]}
    """
    res: dict = {"uri": uri, "mimeType": mime_type}
    if blob is not None:
        res["blob"] = base64.b64encode(blob).decode()
    else:
        res["text"] = text if text is not None else ""
    if meta:
        res["_meta"] = dict(meta)
    return {"type": "resource", "resource": res}


def _is_result(value) -> bool:
    """A handler may hand back a finished result — a dict whose `content` is a
    list of typed blocks — to control content types, `isError`, or `_meta`
    itself. Anything else is data and gets wrapped."""
    return (isinstance(value, dict) and isinstance(value.get("content"), list)
            and all(isinstance(b, dict) and isinstance(b.get("type"), str)
                    for b in value["content"])
            and set(value) <= {"content", "structuredContent", "isError", "_meta"})


def _as_content(value) -> dict:
    if _is_result(value):
        return dict(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    elif callable(getattr(value, "model_dump", None)):      # pydantic, no import
        value = value.model_dump(mode="json")
    if isinstance(value, (dict, list)):
        text = json.dumps(value, allow_nan=False, default=_jsonable)
        out = {"content": [{"type": "text", "text": text}]}
        if isinstance(value, dict):
            out["structuredContent"] = json.loads(text)   # the JSON-clean form
        return out
    return {"content": [{"type": "text", "text": str(value)}]}


def _encode(status, payload) -> tuple[int, bytes]:
    """Serialize a response. A payload that will not serialize becomes -32603
    rather than an unframed transport error."""
    if payload is None:
        return status, b""
    try:
        return status, json.dumps(payload, allow_nan=False).encode()
    except (TypeError, ValueError):
        log.exception("response payload is not JSON-serializable")
        rid = payload.get("id") if isinstance(payload, dict) else None
        status, payload = _err(500, rid, INTERNAL_ERROR, "Internal error")
        return status, json.dumps(payload).encode()


def _norm_path(p: str) -> str:
    return re.sub(r"/{2,}", "/", p or "/").rstrip("/")


_STATUS = {200: "200 OK", 202: "202 Accepted", 204: "204 No Content",
           400: "400 Bad Request", 403: "403 Forbidden", 404: "404 Not Found",
           405: "405 Method Not Allowed", 406: "406 Not Acceptable",
           411: "411 Length Required", 413: "413 Payload Too Large",
           500: "500 Internal Server Error", 503: "503 Service Unavailable"}


# ---------------------------------------------------------------- fork awareness
class _Fork:
    """A process-wide generation counter bumped in every forked child, so
    thread-backed state (pools, the WSGI loop) knows to rebuild itself."""
    gen = 0

    @classmethod
    def _after_fork(cls):
        cls.gen += 1


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_Fork._after_fork)


class _Pool:
    """A small thread pool built for this server's needs, where the stdlib
    executor is the wrong shape:

    - threads are daemons, so a handler the client abandoned can never hold
      the process open at exit;
    - it knows how busy it is, so a full pool refuses new work (503) instead
      of queueing it behind threads that will never come back;
    - it rebuilds itself after fork(), like `_Loop`, instead of inheriting a
      parent's dead threads.

    `submit()` returns a concurrent.futures.Future, which is all
    `loop.run_in_executor` needs.
    """

    def __init__(self, size: int, name: str):
        self.size, self.name = max(1, size), name
        self._lock = threading.Lock()
        self._reset()

    def _reset(self):
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._threads: list[threading.Thread] = []
        self.inflight = 0
        self._gen = _Fork.gen

    @property
    def full(self) -> bool:
        return self.inflight >= self.size

    def submit(self, fn, *args):
        fut: concurrent.futures.Future = concurrent.futures.Future()
        with self._lock:
            if self._gen != _Fork.gen:            # forked: the old threads are gone
                self._reset()
            self.inflight += 1
            if len(self._threads) < self.size and self.inflight > len(self._threads):
                t = threading.Thread(target=self._worker, daemon=True,
                                     name=f"{self.name}-{len(self._threads)}")
                self._threads.append(t)
                t.start()
        self._q.put((fut, fn, args))
        return fut

    def _worker(self):
        while True:
            fut, fn, args = self._q.get()
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn(*args))
                except BaseException as exc:
                    fut.set_exception(exc)
            with self._lock:
                self.inflight -= 1

    def shutdown(self):
        with self._lock:
            self._reset()


class _Core:
    """Transport-neutral MCP logic: validate, dispatch, wrap.

    Both transports feed it a lowercase header mapping and a parsed body, and
    get back (http_status, json_payload). Nothing here knows about WSGI, ASGI,
    sockets, or frameworks.

    allowed_origins   browser Origins admitted (and given CORS headers)
    allowed_hosts     Host values admitted; empty disables the check
    authenticate      (headers) -> principal | None; may be `async def`. A sync
                      callback runs on its own small pool, so it may block
                      without competing with handlers.
    path              if set, only this path is served (404 elsewhere)
    max_body          request body cap in bytes
    workers           thread-pool size for sync handlers; a full pool answers
                      503 rather than queueing
    timeout           seconds a WSGI request may take before it fails with 500
                      (None or 0: no cap)
    stream_budget     notification bytes one streaming call may emit
    keepalive         seconds of SSE silence before a comment frame is sent
    cancel_grace      seconds a cancelled handler gets before it is logged
    queue_size        SSE frames buffered ahead of the client
    offload_bytes     bodies larger than this are parsed off the event loop
    """

    def __init__(self, mcp: MCP, *, allowed_origins=(), allowed_hosts=(),
                 authenticate=None, path=None, max_body=MAX_BODY,
                 workers=WORKERS, timeout=300.0, stream_budget=STREAM_BUDGET,
                 keepalive=KEEPALIVE, cancel_grace=CANCEL_GRACE,
                 queue_size=QUEUE_SIZE, offload_bytes=OFFLOAD_BYTES):
        self.mcp = mcp
        self.allowed_origins = set(allowed_origins)
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self.authenticate = authenticate
        self.path = _norm_path(path) if path else None
        self.max_body = max_body
        self.timeout = timeout or None
        self.stream_budget = stream_budget
        self.keepalive = keepalive
        self.cancel_grace = cancel_grace
        self.queue_size = queue_size
        self.offload_bytes = offload_bytes
        self._pool = _Pool(workers, "micromcp-worker")
        # Pre-dispatch work (authenticate, parsing large bodies) gets its own
        # lane, so untrusted handler code can never starve the auth path.
        self._aux = _Pool(max(2, workers // 4), "micromcp-aux")

    async def offload(self, fn, *args, aux=False):
        """Run a blocking callable off the loop, with contextvars. A full pool
        is refused up front: queueing behind abandoned threads only hides it."""
        pool = self._aux if aux else self._pool
        if pool.full:
            raise Error(INTERNAL_ERROR, "server busy", status=503)
        ctx = contextvars.copy_context()
        return await asyncio.get_running_loop().run_in_executor(
            pool, functools.partial(ctx.run, fn, *args))

    def close(self):
        """Release the worker pools. Idempotent; the ASGI lifespan calls it."""
        self._pool.shutdown()
        self._aux.shutdown()

    # -- HTTP-level answers -----------------------------------------------
    def _origin_ok(self, headers) -> bool:
        origin = headers.get("origin")
        return origin is None or origin in self.allowed_origins

    def _host_ok(self, headers) -> bool:
        host = headers.get("host", "").lower()
        return (not self.allowed_hosts or host in self.allowed_hosts
                or host.rsplit(":", 1)[0] in self.allowed_hosts)

    def extra_headers(self, http_method, headers, status) -> list[tuple[str, str]]:
        """Headers every response carries: protocol version, Allow on 405,
        CORS, and Connection: close on a 413 whose body we will not read."""
        out = [("MCP-Protocol-Version", PROTOCOL)]
        if status == 405:
            out.append(("Allow", "POST, OPTIONS"))
        if status == 413:
            out.append(("Connection", "close"))
        origin = headers.get("origin")
        if origin is not None:
            out.append(("Vary", "Origin"))
        if origin and origin in self.allowed_origins:
            out += [("Access-Control-Allow-Origin", origin),
                    ("Access-Control-Expose-Headers", "MCP-Protocol-Version")]
            if http_method == "OPTIONS":
                out += [("Access-Control-Allow-Methods", "POST, OPTIONS"),
                        ("Access-Control-Allow-Headers", CORS_HEADERS),
                        ("Access-Control-Max-Age", "600")]
        return out

    def path_ok(self, path: str) -> bool:
        return self.path is None or _norm_path(path) == self.path

    def reject_duplicates(self, names):
        """Early answer for repeated headers that identify the caller or route
        the request (first- and last-copy readers would disagree)."""
        dup = sorted(n for n in names if n in SINGLETON_HEADERS)
        if dup:
            code = HEADER_MISMATCH if dup[0] in ROUTING_HEADERS else INVALID_REQUEST
            return _err(400, None, code, f"{dup[0]} header appears more than once")
        return None

    def too_large(self):
        return _err(413, None, INVALID_REQUEST,
                    f"request body exceeds {self.max_body} bytes")

    # -- validation required by the spec ---------------------------------
    def _validate_http(self, headers):
        """Transport-level rungs that apply to every POST, notifications included."""
        if not self._origin_ok(headers):
            raise Error(HEADER_MISMATCH, "Origin not allowed", status=403)
        if not self._host_ok(headers):
            raise Error(HEADER_MISMATCH, "Host not allowed", status=403)
        ctype = headers.get("content-type")
        if ctype and ctype.split(";", 1)[0].strip().lower() != "application/json":
            raise Error(INVALID_REQUEST, "Content-Type must be application/json")
        if not _accepts(headers, "application/json"):
            raise Error(INVALID_REQUEST, "Accept must admit application/json", status=406)
        for h in ("mcp-protocol-version", "mcp-method"):
            # These never legitimately contain a comma, so one means a server
            # folded two copies. Mcp-Name may (data: URIs), so it is exempt;
            # ASGI catches its duplicates on the raw pairs instead.
            if "," in headers.get(h, ""):
                raise Error(HEADER_MISMATCH, f"{h} header appears more than once")

    def _validate(self, headers, body):
        ver = headers.get("mcp-protocol-version")
        method = body.get("method")
        if not isinstance(method, str):
            raise Error(INVALID_REQUEST, "Invalid Request: method must be a string")
        params = body.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise Error(INVALID_PARAMS, "params must be an object")
        meta = params.get("_meta")
        if meta is not None and not isinstance(meta, dict):
            raise Error(INVALID_PARAMS, "params._meta must be an object")

        if method in HANDSHAKE_METHODS:
            # A handshake-era client. This server is stateless-only by choice, so
            # answer with -32022 and our version list rather than a header
            # complaint: -32022 is the code a dual-era client reads as "modern
            # peer, renegotiate", which turns a confusing failure into an
            # accurate "no mutually supported version".
            raise Error(UNSUPPORTED_VERSION,
                        f"this server implements only the stateless MCP revision "
                        f"{PROTOCOL}; {method!r} belongs to the superseded "
                        f"initialize-handshake era",
                        data={"supported": [PROTOCOL],
                              "requested": ver or (meta or {}).get(META_VER) or ""})

        # Envelope first (the SDK's rung 1), then headers, then version.
        if meta is None:
            raise Error(INVALID_PARAMS,
                        f"params._meta missing {META_VER} and {META_CAPS}")
        if not ver:
            raise Error(HEADER_MISMATCH, "missing MCP-Protocol-Version header")
        if ver != meta.get(META_VER):
            raise Error(HEADER_MISMATCH,
                        f"MCP-Protocol-Version {ver!r} != body _meta {meta.get(META_VER)!r}")
        if ver != PROTOCOL:
            # -32022, not -32020: the client reads data.supported to renegotiate.
            raise Error(UNSUPPORTED_VERSION,
                        f"unsupported protocol version {ver!r}",
                        data={"supported": [PROTOCOL], "requested": ver})
        if META_CAPS not in meta:
            raise Error(INVALID_PARAMS, f"request _meta missing {META_CAPS}")
        token = meta.get("progressToken")
        if token is not None and (isinstance(token, bool) or not isinstance(token, (str, int))):
            raise Error(INVALID_PARAMS, "_meta.progressToken must be a string or integer")

        # Mcp-Method is compared verbatim (as the SDK does); only Mcp-Name
        # carries the base64 sentinel.
        if headers.get("mcp-method") != method:
            raise Error(HEADER_MISMATCH, "Mcp-Method header does not match body method")

        if method in ("tools/call", "resources/read", "prompts/get"):
            # Bind the header to the field this method actually dispatches on;
            # a decoy `name` in a resources/read body must not satisfy the check.
            field = "uri" if method == "resources/read" else "name"
            want = params.get(field)
            if want is not None:
                if not isinstance(want, str):
                    raise Error(INVALID_PARAMS, f"params.{field} must be a string")
                if len(want) > MAX_URI:
                    raise Error(INVALID_PARAMS, f"params.{field} is too long")
                if _decode_hdr(headers.get("mcp-name")) != want:
                    raise Error(HEADER_MISMATCH,
                                f"Mcp-Name header does not match body value {want!r}")
            args = params.get("arguments")
            if args is not None and not isinstance(args, dict):
                raise Error(INVALID_PARAMS, "params.arguments must be an object")
        if method in LIST_METHODS and params.get("cursor") is not None:
            # We never issue cursors, so any cursor is one we did not issue.
            raise Error(INVALID_PARAMS, "unknown cursor")
        return method, params

    async def _bind_call(self, params, principal, big):
        """Validate and convert a tools/call's arguments before any response
        byte is committed, so a -32602 keeps its HTTP 400 even on a tool that
        would otherwise stream. Returns the kwargs the handler will receive,
        or None when the tool is unknown/denied (dispatch answers those)."""
        entry = self.mcp.tools.get(params.get("name"))
        if not entry or not _allowed(entry, principal):
            return None
        name, kwargs = entry["name"], dict(params.get("arguments") or {})
        problem = (await self.offload(_check, kwargs, entry["inputSchema"]) if big
                   else _check(kwargs, entry["inputSchema"]))
        if problem:
            raise Error(INVALID_PARAMS, f"invalid arguments for tool {name!r}: {problem}")
        try:
            return {k: _from_json(v, entry["_hints"].get(k)) for k, v in kwargs.items()}
        except Exception:
            # Constructor/validator text can carry server-side detail; keep it here.
            log.info("argument conversion for tool %r failed", name, exc_info=True)
            raise Error(INVALID_PARAMS, f"invalid arguments for tool {name!r}") from None

    # -- dispatch ---------------------------------------------------------
    async def _dispatch(self, method, params, principal, emit=None, stop=None, bound=None):
        """Async so handlers may be `async def`. Sync handlers run on the pool."""
        m = self.mcp

        async def run(entry, kwargs=None):
            fn, kwargs = entry["_fn"], kwargs or {}
            if _is_async(fn):
                out = fn(**kwargs)
            else:
                try:
                    out = await self.offload(functools.partial(fn, **kwargs))
                except asyncio.CancelledError:
                    # Threads cannot be interrupted: the call keeps running
                    # until it returns. Make that visible.
                    log.warning("sync handler %r abandoned on cancellation; its thread "
                                "runs until it returns (poll ctx.cancelled to stop early)",
                                entry.get("name"))
                    raise
            if inspect.isawaitable(out):
                out = await out
            if inspect.isgenerator(out):
                out = await self.offload(list, out)
            elif inspect.isasyncgen(out):
                out = [x async for x in out]
            return out

        def public(entries):
            out = []
            for e in list(entries):
                if not _allowed(e, principal):
                    continue
                item = {k: v for k, v in e.items() if not k.startswith("_")}
                if e.get("_meta_out"):
                    item["_meta"] = e["_meta_out"]
                out.append(item)
            return out

        def inject(entry, kwargs):
            if entry["_principal"]:
                kwargs[entry["_principal"]] = principal
            if entry["_context"]:
                token = (params.get("_meta") or {}).get("progressToken")
                kwargs[entry["_context"]] = Context(emit, token, stop)

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
            }
        if method == "tools/list":
            return {"tools": public(m.tools.values())}
        if method == "resources/list":
            return {"resources": public(m.resources.values())}
        if method == "resources/templates/list":
            return {"resourceTemplates": public(m.templates.values())}
        if method == "prompts/list":
            return {"prompts": public(m.prompts.values())}

        if method == "tools/call":
            name = params.get("name")
            entry = m.tools.get(name)
            if not entry:
                raise Error(METHOD_NOT_FOUND, f"no such tool {name!r}", 404)
            if not _allowed(entry, principal):
                # Tool-level failures are in-band results, not JSON-RPC errors.
                return {"content": [{"type": "text",
                                     "text": f"Permission denied for tool {name!r}"}],
                        "isError": True}
            kwargs = bound if bound is not None else await self._bind_call(params, principal, False)
            inject(entry, kwargs)
            try:
                result = _as_content(await run(entry, kwargs))
            except Error:
                raise                      # transport-level answers (503 busy) keep their status
            except Exception as exc:
                return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            if "outputSchema" in entry:
                sc = result.get("structuredContent")
                problem = ("no structuredContent returned" if sc is None
                           else _check(sc, entry["outputSchema"]))
                if problem:
                    return {"content": [{"type": "text",
                                         "text": f"tool {name!r} result does not match "
                                                 f"its outputSchema: {problem}"}],
                            "isError": True}
            return result

        if method == "resources/read":
            uri = params.get("uri")
            entry, raw = m._match(uri or "")
            if not entry or not _allowed(entry, principal):
                raise Error(METHOD_NOT_FOUND, f"no such resource {uri!r}", 404)
            kwargs = m.coerce_params(entry, uri, raw)   # after the guard: -32602 reveals it
            inject(entry, kwargs)
            try:
                value = await run(entry, kwargs)
            except Error:
                raise
            except Exception:
                log.exception("resource handler for %r failed", uri)
                raise Error(INTERNAL_ERROR, "resource handler failed", 500) from None
            item = {"uri": uri, "mimeType": entry["mimeType"]}
            if isinstance(value, (bytes, bytearray)):
                item["blob"] = base64.b64encode(value).decode()
            else:
                item["text"] = value if isinstance(value, str) else str(value)
            if entry.get("_meta_out"):
                item["_meta"] = entry["_meta_out"]
            return {"contents": [item]}

        if method == "prompts/get":
            name = params.get("name")
            entry = m.prompts.get(name)
            if not entry or not _allowed(entry, principal):
                raise Error(METHOD_NOT_FOUND, f"no such prompt {name!r}", 404)
            args = dict(params.get("arguments") or {})
            declared = {a["name"] for a in entry["arguments"]}
            extra = sorted(set(args) - declared)
            if extra:
                raise Error(INVALID_PARAMS, f"prompt {name!r}: unexpected {extra!r}")
            for k, v in list(args.items()):
                if not isinstance(v, str):
                    raise Error(INVALID_PARAMS, f"prompt {name!r}: argument {k!r} must be a string")
                try:
                    args[k] = _coerce(v, entry["_hints"].get(k))
                except ValueError as exc:
                    raise Error(INVALID_PARAMS,
                                f"prompt {name!r}: argument {k!r}: {exc}") from None
            inject(entry, args)
            try:
                inspect.signature(entry["_fn"]).bind(**args)
            except TypeError as exc:
                raise Error(INVALID_PARAMS,
                            f"invalid arguments for prompt {name!r}: {exc}") from None
            try:
                out = await run(entry, args)
            except Error:
                raise
            except Exception:
                log.exception("prompt handler for %r failed", name)
                raise Error(INTERNAL_ERROR, "prompt handler failed", 500) from None
            if isinstance(out, dict) and "role" in out and "content" in out:
                out = [out]
            msgs = out if isinstance(out, list) else [
                {"role": "user", "content": {"type": "text", "text": str(out)}}]
            return {"messages": msgs}

        raise Error(METHOD_NOT_FOUND, f"unknown method {method!r}", 404)

    # -- one request, start to finish -------------------------------------
    async def prepare(self, http_method, headers, raw):
        """Parse, validate, authenticate, and bind tool arguments — everything
        that must happen before a single response byte is committed.

        Returns (early, req): `early` is a finished (status, payload) answer
        (payload None means an empty body), otherwise `req` is the validated
        (method, params, principal, id, bound-kwargs) tuple for `respond`.
        """
        if http_method == "OPTIONS":                       # CORS preflight
            if not self._origin_ok(headers) or not self._host_ok(headers):
                return _err(403, None, HEADER_MISMATCH, "Origin or Host not allowed"), None
            return (204, None), None
        if http_method != "POST":
            # Legacy GET/DELETE session mechanics are gone in this revision.
            return _err(405, None, INVALID_REQUEST, "POST only"), None
        try:
            self._validate_http(headers)
        except Error as e:
            return _err(e.status, None, e.code, e.message, e.data), None
        if len(raw) > self.max_body:
            return self.too_large(), None
        big = len(raw) > self.offload_bytes
        rid = None
        try:
            try:
                body = (await self.offload(json.loads, raw, aux=True) if big
                        else json.loads(raw or b"{}"))
            except Error:
                raise
            except Exception:
                return _err(400, None, PARSE_ERROR, "Parse error"), None
            if not isinstance(body, dict):
                return _err(400, None, INVALID_REQUEST,
                            "Invalid Request: body must be a single JSON-RPC object"), None
            if "id" not in body:
                # A notification. Acknowledge it and do NOT dispatch: notifications
                # get no response, and a side-effecting tool must not run without
                # an id to correlate its result to.
                return (202, None), None
            rid = body.get("id")
            if isinstance(rid, bool) or not isinstance(rid, (str, int)):
                return _err(400, None, INVALID_REQUEST,
                            "Invalid Request: id must be a string or integer"), None
            method, params = self._validate(headers, body)
            principal = None
            if self.authenticate:
                if _is_async(self.authenticate):
                    principal = await self.authenticate(headers)
                else:
                    principal = await self.offload(self.authenticate, headers, aux=True)
            bound = (await self._bind_call(params, principal, big)
                     if method == "tools/call" else None)
        except Error as e:
            return _err(e.status, rid, e.code, e.message, e.data), None
        except Exception:
            log.exception("request preparation failed")
            return _err(500, rid, INTERNAL_ERROR, "Internal error"), None
        return None, (method, params, principal, rid, bound)

    async def respond(self, req, emit=None, stop=None):
        """Dispatch a prepared request. Returns (status_code, payload)."""
        method, params, principal, rid, bound = req
        try:
            result = await self._dispatch(method, params, principal, emit, stop, bound)
            # 2026-07-28 requires these on EVERY result. They have SDK-side
            # defaults when parsing, but the strict per-version schema that
            # governs a live connection demands them explicitly. Server
            # identity travels in _meta on every result, not in discover.
            result = {"resultType": "complete", "ttlMs": 0, "cacheScope": "private",
                      **result}
            result["_meta"] = {**result.get("_meta", {}),
                               META_SERVER: {"name": self.mcp.name,
                                             "version": self.mcp.version}}
            return 200, {"jsonrpc": "2.0", "id": rid, "result": result}
        except Error as e:
            return _err(e.status, rid, e.code, e.message, e.data)
        except Exception:
            log.exception("dispatch of %r failed", method)
            return _err(500, rid, INTERNAL_ERROR, "Internal error")

    async def handle(self, http_method, headers, raw, emit=None):
        """Return (status_code, payload). The only entry point transports need.

        `emit(method, params)` is an async sink for notifications; None means
        nothing is listening and Context silently drops them.
        """
        early, req = await self.prepare(http_method, headers, raw)
        if early is not None:
            return early
        return await self.respond(req, emit)

    def wants_stream(self, req, headers) -> bool:
        """True when a validated, argument-bound tools/call addresses a
        Context-declaring tool AND the client accepts text/event-stream.
        Decided only after `prepare` succeeded — once an SSE stream is open
        there is no way back to a JSON error response with the right status."""
        method, params = req[0], req[1]
        if method != "tools/call" or not _accepts(headers, "text/event-stream"):
            return False
        entry = self.mcp.tools.get(params.get("name"))
        return bool(entry and entry.get("_context"))

    def spawn(self, req):
        """Start `req` on a task that feeds SSE frames (bytes) into a bounded
        queue; None is the end-of-stream sentinel. Returns (queue, worker, stop)."""
        q: asyncio.Queue = asyncio.Queue(maxsize=self.queue_size)
        stop = threading.Event()
        sent = [0]

        async def emit(method, params):
            # Serialize here, on the handler's side of the queue: a bad payload
            # raises into the handler instead of killing an open stream. The
            # byte budget is the backstop for servers whose send() never
            # blocks (daphne buffers without limit).
            frame = json.dumps({"jsonrpc": "2.0", "method": method, "params": params},
                               allow_nan=False).encode()
            sent[0] += len(frame)
            if sent[0] > self.stream_budget:
                raise RuntimeError(f"stream budget of {self.stream_budget} bytes exceeded")
            await q.put(b"data: " + frame + b"\n\n")

        async def run():
            try:
                _status, payload = await self.respond(req, emit=emit, stop=stop)
                await q.put(b"data: " + _encode(200, payload)[1] + b"\n\n")
            finally:
                await q.put(None)

        return q, asyncio.create_task(run()), stop

    async def finish(self, q, worker, stop, req):
        """Stop a spawned worker: signal sync handlers, cancel, let a blocked
        put() unwind, and log a handler that ignores cancellation."""
        stop.set()
        if not worker.done():
            worker.cancel()
            while not q.empty():
                q.get_nowait()
        done, _ = await asyncio.wait({worker}, timeout=self.cancel_grace)
        if not done:
            log.warning("handler for %r ignored cancellation and is still running",
                        req[1].get("name"))

    async def frames(self, req):
        """Async generator of SSE bytes for a validated streaming request, with
        keepalive comments during silence. Closing the generator cancels the
        handler. Used by adapters that stream but have no ASGI receive channel."""
        q, worker, stop = self.spawn(req)
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), self.keepalive)
                except TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                if item is None:
                    return
                yield item
        finally:
            await self.finish(q, worker, stop, req)
