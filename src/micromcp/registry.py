"""The `MCP` registry: tools, resources, templates, prompts, and their guards."""

from __future__ import annotations

import inspect
import json
import re

from ._constants import _NAME_RE, INVALID_PARAMS, log

from .docstrings import _parse_doc
from .errors import Error
from .schema import _fname, _hints, _input_schema, _output_schema

_LABEL = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?"
_META_KEY_RE = re.compile(rf"^(?:({_LABEL}(?:\.{_LABEL})*)/)?"
                          r"[a-zA-Z0-9](?:[a-zA-Z0-9._-]*[a-zA-Z0-9])?$")
_APP_MIME = "text/html;profile=mcp-app"


def _meta_ok(meta, what: str) -> dict:
    """Validate a `_meta` object the way the 2026 MetaObject schema does and
    return a detached JSON-clean copy. Reserved prefixes (`io.modelcontextprotocol/`,
    any `*.mcp/`) are refused: they belong to the protocol, not to handlers."""
    if not isinstance(meta, dict):
        raise ValueError(f"{what} meta must be a dict")
    for k in meta:
        m = _META_KEY_RE.match(k) if isinstance(k, str) else None
        if not m:
            raise ValueError(f"{what} meta key {k!r} is not a valid _meta key")
        labels = (m.group(1) or "").split(".")
        if len(labels) >= 2 and labels[1] in ("modelcontextprotocol", "mcp"):
            raise ValueError(f"{what} meta key {k!r} uses a reserved prefix")
    try:
        return json.loads(json.dumps(meta, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} meta is not JSON-serializable: {exc}") from None


_TEMPLATE_RE = re.compile(r"\{([^{}]*)\}")


def _compile_template(uri: str):
    """`crash://{crash_id}` -> a regex capturing crash_id, or (None, []) for a
    plain URI. Raises ValueError for placeholders this server cannot serve.

    Each placeholder is a possessive run that stops at `/` and at the first
    character of the literal that follows it, and two placeholders may not be
    adjacent, so matching is linear in the URI length.
    """
    parts = _TEMPLATE_RE.split(uri)
    if len(parts) == 1:
        return None, []
    names = parts[1::2]
    for n in names:
        if n[:1] in "?+#./;&=,!@|":
            raise ValueError(f"resource template {uri!r}: RFC 6570 operator in "
                             f"{{{n}}} is not supported; use a plain {{name}}")
        if not n.isidentifier():
            raise ValueError(f"resource template {uri!r}: {{{n}}} is not a valid "
                             f"parameter name")
    if len(set(names)) != len(names):
        raise ValueError(f"resource template {uri!r}: duplicate parameter name")
    if any(not lit for lit in parts[2:-1:2]):
        raise ValueError(f"resource template {uri!r}: placeholders must be separated "
                         f"by at least one literal character")
    rx = []
    for i, p in enumerate(parts):
        if i % 2:
            nxt = parts[i + 1][:1] if i + 1 < len(parts) else ""
            rx.append(f"(?P<{p}>[^/{re.escape(nxt) if nxt else ''}]++)")
        else:
            rx.append(re.escape(p))
    return re.compile(f"^{''.join(rx)}$"), names


_INT_RE = re.compile(r"^-?[0-9]+$")
_FLOAT_RE = re.compile(r"^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$")
_BOOL = {"1": True, "true": True, "yes": True, "0": False, "false": False, "no": False}


def _coerce(value: str, ann):
    """Coerce a string from the wire to the handler's declared type."""
    if ann is int:
        if not _INT_RE.match(value):
            raise ValueError(f"{value!r} is not an integer")
        return int(value)
    if ann is float:
        if not _FLOAT_RE.match(value):
            raise ValueError(f"{value!r} is not a number")
        f = float(value)
        if f != f or f in (float("inf"), float("-inf")):
            raise ValueError(f"{value!r} is out of range")
        return f
    if ann is bool:
        if value.lower() not in _BOOL:
            raise ValueError(f"{value!r} is not a boolean")
        return _BOOL[value.lower()]
    return value


def _is_async(fn) -> bool:
    return (inspect.iscoroutinefunction(fn)
            or inspect.iscoroutinefunction(getattr(fn, "__call__", None)))  # noqa: B004


def _allowed(entry, principal, strict=False) -> bool:
    """Evaluate an entry's guards. A guard that raises denies (fail closed).
    When the entry is being invoked (`strict`), a guard that raises an
    `Error` such as `Unauthorized` chooses the answer instead — a 401 with its
    challenge; in a listing the entry is simply hidden."""
    try:
        return all(g(principal) for g in entry.get("_guards", ()))
    except Error:
        if strict:
            raise
        return False
    except Exception:
        log.exception("guard for %r raised; denying", entry.get("name"))
        return False


def _wire_name(kind, explicit, f, registry, replace) -> str:
    n = explicit if explicit is not None else _fname(f)
    if not _NAME_RE.match(n):
        raise ValueError(f"{kind} name {n!r} must match {_NAME_RE.pattern} "
                         f"(pass name=... to override)")
    if n in registry and not replace:
        raise ValueError(f"{kind} {n!r} is already registered (pass replace=True)")
    return n


class MCP:
    """A registry of tools, resources, and prompts. Hand it to a transport."""

    def __init__(self, name: str, version: str = "0.1.0"):
        self.name, self.version = name, version
        self.tools: dict[str, dict] = {}
        self.resources: dict[str, dict] = {}
        self.templates: dict[str, dict] = {}
        self.prompts: dict[str, dict] = {}

    def tool(self, fn=None, *, name=None, title=None, guards=(),
             annotations=None, output_schema=None, read_only=None,
             destructive=None, idempotent=None, replace=False, meta=None,
             visibility=None, widget=None):
        """Register a tool.

        title       human-readable label for UIs
        read_only / destructive / idempotent
                    shorthands for the standard annotation hints clients use to
                    decide what to auto-approve; merged into `annotations`
        output_schema
                    declares the shape of structuredContent; inferred from a
                    TypedDict or dataclass return annotation when omitted.
                    Results are validated against it before being sent.
        replace     allow re-registering an existing name (default: error)
        meta        published as the tool's `_meta` (e.g. an MCP Apps
                    `{"ui": {"resourceUri": "ui://..."}}` pointer)
        visibility  MCP Apps audience: "model", "app", or both (the spec's
                    default). An app-only tool is hidden from the model by
                    the host and callable by the server's widgets; it is still
                    an ordinary tool to any other client, so guard it.
        widget      a `micromcp.Widget` this tool shows: its `ui://` resource
                    is registered (once) and named in the tool's `_meta`

        Arguments are validated against the generated schema and converted to
        the declared Python types (dates, enums, sets, dataclasses, pydantic
        models) before the handler runs. A Google/NumPy-style `Args:` docstring
        section becomes per-parameter `description`s.
        """
        def wrap(f):
            n = _wire_name("tool", name, f, self.tools, replace)
            schema, inject = _input_schema(f)
            hints = {"readOnlyHint": read_only, "destructiveHint": destructive,
                     "idempotentHint": idempotent}
            ann = {**{k: v for k, v in hints.items() if v is not None},
                   **(annotations or {})}
            entry = {
                "name": n,
                "description": _parse_doc(inspect.getdoc(f))[0],
                "inputSchema": schema,
                "_fn": f, "_guards": guards, "_hints": inject["hints"],
                "_principal": inject["principal"], "_context": inject["context"],
            }
            if title:
                entry["title"] = title
            if ann:
                entry["annotations"] = ann
            if meta:
                entry["_meta_out"] = _meta_ok(meta, f"tool {n!r}")
            if widget is not None:
                uri = getattr(widget, "uri", None)
                if not isinstance(uri, str) or not callable(getattr(widget, "_attach", None)):
                    raise TypeError(f"tool {n!r}: widget must be a micromcp.Widget")
                mo = entry.setdefault("_meta_out", {})
                ui = mo.get("ui", {})
                if not isinstance(ui, dict):
                    raise ValueError(f"tool {n!r}: meta 'ui' must be a dict to add a widget")
                if ui.get("resourceUri", uri) != uri or mo.get("ui/resourceUri", uri) != uri:
                    raise ValueError(f"tool {n!r}: widget= conflicts with meta's resourceUri")
                # the spec's key, plus the flat legacy key hosts of the reference servers read
                mo["ui"] = {**ui, "resourceUri": uri}
                mo["ui/resourceUri"] = uri
            if visibility is not None:
                vis = [visibility] if isinstance(visibility, str) else list(visibility)
                if not vis or len(set(vis)) != len(vis) \
                        or any(v not in ("model", "app") for v in vis):
                    raise ValueError(f"tool {n!r}: visibility must be 'model', 'app', or both")
                out = entry.setdefault("_meta_out", {})
                ui = out.get("ui", {})
                if not isinstance(ui, dict):
                    raise ValueError(f"tool {n!r}: meta 'ui' must be a dict to add visibility")
                if "visibility" in ui and ui["visibility"] != vis:
                    raise ValueError(f"tool {n!r}: visibility= conflicts with meta ui.visibility")
                out["ui"] = {**ui, "visibility": vis}
            out = _output_schema(f, output_schema)
            if out:
                entry["outputSchema"] = out
            if widget is not None:
                widget._attach(self)           # last, so a refused tool registers nothing
            self.tools[n] = entry
            return f
        return wrap(fn) if fn else wrap

    def resource(self, uri: str, *, mime_type=None, title=None, guards=(),
                 replace=False, meta=None):
        """Register a resource. A `{braced}` segment makes it a template:

            @mcp.resource("crash://{crash_id}")
            def one(crash_id: int) -> str: ...

        Template parameters are coerced using the handler's type hints. When
        several templates match a URI, the one with the longest literal prefix
        wins. `Principal` and `Context` parameters are injected as for tools.
        A handler returning `bytes` is delivered as a base64 `blob`. Guards are
        evaluated on read and on listing, before any parameter is parsed; a
        denied resource is indistinguishable from a missing one. `meta` is
        published as `_meta` in listings and on the read contents.

        `mime_type` defaults to `text/plain`, or to `text/html;profile=mcp-app`
        for a `ui://` URI (an MCP App widget). Widgets are fetched by the host
        under its own identity and cached, so they should be static: no
        guards, no template parameters, no per-user content — put that in the
        guarded tool's result instead. Both are warned about at registration.
        """
        def wrap(f):
            rx, params = _compile_template(uri)
            _schema_unused, inject = _input_schema(f)
            mime = mime_type or (_APP_MIME if uri.startswith("ui://") else "text/plain")
            if uri.startswith("ui://"):
                if guards:
                    log.warning("ui resource %r has guards: hosts prefetch widgets with their "
                                "own identity, so it will not render; keep widgets static", uri)
                if rx is not None:
                    log.warning("ui resource %r is a template: URI parameters must never be "
                                "rendered into widget HTML (XSS); keep widgets static", uri)
            entry = {"name": _fname(f), "mimeType": mime,
                     "_fn": f, "_guards": guards, "_hints": inject["hints"],
                     "_principal": inject["principal"], "_context": inject["context"]}
            if title:
                entry["title"] = title
            if meta:
                entry["_meta_out"] = _meta_ok(meta, f"resource {uri!r}")
            registry = self.templates if rx is not None else self.resources
            if uri in registry and not replace:
                raise ValueError(f"resource {uri!r} is already registered (pass replace=True)")
            if rx is not None:
                self.templates[uri] = {**entry, "uriTemplate": uri, "_re": rx,
                                       "_params": params,
                                       "_prefix": len(_TEMPLATE_RE.split(uri)[0])}
            else:
                self.resources[uri] = {**entry, "uri": uri}
            return f
        return wrap

    def prompt(self, fn=None, *, name=None, title=None, guards=(), replace=False,
               meta=None):
        """Register a prompt. Arguments arrive as strings and are coerced to the
        handler's `int`/`float`/`bool` hints; `Principal`/`Context` parameters
        are injected exactly as for tools."""
        def wrap(f):
            n = _wire_name("prompt", name, f, self.prompts, replace)
            desc, docs = _parse_doc(inspect.getdoc(f))
            _schema_unused, inject = _input_schema(f)
            skip = {inject["principal"], inject["context"], "self"}
            args = []
            for k, p in inspect.signature(f).parameters.items():
                if k in skip or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                    continue
                a = {"name": k, "required": p.default is inspect.Parameter.empty}
                if k in docs:
                    a["description"] = docs[k]
                args.append(a)
            entry = {"name": n, "description": desc, "arguments": args,
                     "_fn": f, "_guards": guards, "_hints": inject["hints"],
                     "_principal": inject["principal"], "_context": inject["context"]}
            if title:
                entry["title"] = title
            if meta:
                entry["_meta_out"] = _meta_ok(meta, f"prompt {n!r}")
            self.prompts[n] = entry
            return f
        return wrap(fn) if fn else wrap

    def match_resource(self, uri: str):
        """Resolve a URI to (entry, kwargs). Exact hits win over templates;
        among templates, the longest literal prefix wins."""
        entry, raw = self._match(uri)
        return (entry, self.coerce_params(entry, uri, raw)) if entry else (None, {})

    def _match(self, uri: str):
        """(entry, raw string captures) without coercion, so a caller can run
        the entry's guards before anything about its parameters is revealed."""
        if uri in self.resources:
            return self.resources[uri], {}
        for entry in sorted(list(self.templates.values()), key=lambda e: -e["_prefix"]):
            m = entry["_re"].match(uri)
            if m:
                return entry, m.groupdict()
        return None, {}

    @staticmethod
    def coerce_params(entry, uri: str, raw: dict) -> dict:
        try:
            return {k: _coerce(v, entry["_hints"].get(k, str)) for k, v in raw.items()}
        except ValueError as exc:
            raise Error(INVALID_PARAMS, f"resource URI {uri!r}: {exc}") from None


# Keep `_hints` importable from here for callers that resolve template hints.
__all__ = ["MCP", "_allowed", "_coerce", "_compile_template", "_hints", "_is_async"]
