"""The `MCP` registry: tools, resources, templates, prompts, and their guards."""

from __future__ import annotations

import inspect
import re

from ._constants import _NAME_RE, INVALID_PARAMS, log
from .docstrings import _parse_doc
from .errors import Error
from .schema import _fname, _hints, _input_schema, _output_schema

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


def _allowed(entry, principal) -> bool:
    """Evaluate an entry's guards. A guard that raises denies (fail closed)."""
    try:
        return all(g(principal) for g in entry.get("_guards", ()))
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
             destructive=None, idempotent=None, replace=False):
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
            out = _output_schema(f, output_schema)
            if out:
                entry["outputSchema"] = out
            self.tools[n] = entry
            return f
        return wrap(fn) if fn else wrap

    def resource(self, uri: str, *, mime_type="text/plain", title=None, guards=(),
                 replace=False):
        """Register a resource. A `{braced}` segment makes it a template:

            @mcp.resource("crash://{crash_id}")
            def one(crash_id: int) -> str: ...

        Template parameters are coerced using the handler's type hints. When
        several templates match a URI, the one with the longest literal prefix
        wins. `Principal` and `Context` parameters are injected as for tools.
        A handler returning `bytes` is delivered as a base64 `blob`. Guards are
        evaluated on read and on listing, before any parameter is parsed; a
        denied resource is indistinguishable from a missing one.
        """
        def wrap(f):
            rx, params = _compile_template(uri)
            _schema_unused, inject = _input_schema(f)
            entry = {"name": _fname(f), "mimeType": mime_type,
                     "_fn": f, "_guards": guards, "_hints": inject["hints"],
                     "_principal": inject["principal"], "_context": inject["context"]}
            if title:
                entry["title"] = title
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

    def prompt(self, fn=None, *, name=None, title=None, guards=(), replace=False):
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
