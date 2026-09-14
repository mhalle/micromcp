"""Type hints -> JSON Schema, JSON -> typed values, and the validator between."""

from __future__ import annotations

import dataclasses
import datetime
import enum
import functools
import inspect
import json
import pathlib
import re
import sys
import types
import typing

from ._constants import MAX_DEPTH
from .docstrings import _parse_doc
from .markers import _MARKERS, Context, Principal

_PRIM = {int: "integer", float: "number", str: "string", bool: "boolean",
         list: "array", dict: "object"}
_FORMATS = {datetime.datetime: "date-time", datetime.date: "date", datetime.time: "time"}
_FORMAT_PARSERS = {"date-time": datetime.datetime.fromisoformat,
                   "date": datetime.date.fromisoformat, "time": datetime.time.fromisoformat}
_STRINGISH = (bytes, bytearray, datetime.date, datetime.datetime, datetime.time,
              pathlib.PurePath)
_MISSING = object()


# ---------------------------------------------------------------- hint resolution
def _target(fn):
    """The function whose annotations describe `fn`'s parameters."""
    if isinstance(fn, functools.partial):
        return _target(fn.func)
    if inspect.isfunction(fn) or inspect.ismethod(fn):
        return fn
    call = getattr(fn, "__call__", None)  # noqa: B004  (we want the bound method)
    return call if inspect.ismethod(call) else fn


def _globals(obj) -> dict:
    g = getattr(obj, "__globals__", None)
    if g is None:
        mod = sys.modules.get(getattr(obj, "__module__", ""))
        g = vars(mod) if mod else {}
    return g


def _raw_annotations(obj) -> dict:
    try:
        return dict(inspect.get_annotations(obj))
    except Exception:
        pass
    try:  # 3.14+: deferred annotations that fail to evaluate eagerly
        import annotationlib
        return dict(annotationlib.get_annotations(obj, format=annotationlib.Format.STRING))
    except Exception:
        return {}


def _hints(obj) -> dict:
    """Resolved type hints, tolerating `from __future__ import annotations`.

    If the whole signature cannot be resolved (one TYPE_CHECKING-only import is
    enough), fall back to resolving each annotation on its own, so a single bad
    name degrades only that one parameter's schema and nothing else.
    """
    obj = _target(obj) if callable(obj) and not isinstance(obj, type) else obj
    try:
        return typing.get_type_hints(obj, include_extras=True)
    except Exception:
        pass
    out, g = {}, _globals(obj)
    for k, v in _raw_annotations(obj).items():
        if isinstance(v, str):
            try:
                v = eval(v, dict(g))  # developer-authored source, not client input
            except Exception:
                continue
        out[k] = v
    return out


def _fname(f) -> str:
    return getattr(f, "__name__", None) or getattr(getattr(f, "func", None), "__name__", None) \
        or type(f).__name__


# ---------------------------------------------------------------- markers
_WRAP_RE = re.compile(r"^(?:[\w.]+\.)?(Optional|Union|Annotated)\[(.*)\]$", re.S)


def _split_top(s: str) -> list[str]:
    """Split on commas at bracket depth 0."""
    out, depth, cur = [], 0, []
    for ch in s:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return [p for p in out if p]


def _string_marker(ann: str) -> str | None:
    """If an UNRESOLVED string annotation names `Principal`/`Context` — bare,
    dotted, or wrapped in Optional[], Union[...], `| ...`, or Annotated[..] —
    return the marker name. `list[Principal]` or `PrincipalLike` is not a
    marker: no injection could satisfy them, so they are an app's own type."""
    s = ann.strip()
    m = _WRAP_RE.match(s)
    if m:
        inner = _split_top(m.group(2))
        for p in (inner[:1] if m.group(1) == "Annotated" else inner):
            hit = _string_marker(p)
            if hit:
                return hit
        return None
    for p in (q.strip() for q in s.split("|")):
        if _WRAP_RE.match(p):
            hit = _string_marker(p)
            if hit:
                return hit
        elif re.fullmatch(r"[\w.]+", p) and p.rsplit(".", 1)[-1] in ("Principal", "Context"):
            return p.rsplit(".", 1)[-1]
    return None


def _unwrap(ann):
    """Strip Annotated[] and PEP 695 `type` aliases down to the real annotation."""
    while True:
        if typing.get_origin(ann) is typing.Annotated:
            ann = typing.get_args(ann)[0]
        elif type(ann).__name__ == "TypeAliasType" and hasattr(ann, "__value__"):
            ann = ann.__value__
        else:
            return ann


def _is_marker(ann, marker) -> bool:
    """True when a RESOLVED annotation is `marker`, also through
    Optional/Union/Annotated/type-alias wrappers."""
    ann = _unwrap(ann)
    if ann is marker:
        return True
    if typing.get_origin(ann) in (typing.Union, types.UnionType):
        return any(_is_marker(a, marker) for a in typing.get_args(ann))
    return False


def _marker_subclass(ann):
    """A RESOLVED annotation that is a strict subclass of a marker (also through
    wrappers). Such a parameter is neither injected nor safely exposable."""
    ann = _unwrap(ann)
    if isinstance(ann, type) and ann not in _MARKERS and issubclass(ann, _MARKERS):
        return ann
    if typing.get_origin(ann) in (typing.Union, types.UnionType):
        for a in typing.get_args(ann):
            hit = _marker_subclass(a)
            if hit:
                return hit
    return None


# ---------------------------------------------------------------- hints -> schema
def _schema(ann, closed=False) -> dict:
    """Map a type annotation to a JSON Schema fragment. `closed` adds
    additionalProperties:false to nested objects (used for inputs)."""
    ann = _unwrap(ann)
    if ann is inspect.Parameter.empty or ann is None or ann is typing.Any \
            or isinstance(ann, str):
        return {}
    if ann in _PRIM:
        return {"type": _PRIM[ann]}
    if hasattr(ann, "__supertype__"):                       # NewType
        return _schema(ann.__supertype__, closed)
    if isinstance(ann, type):
        if issubclass(ann, enum.Enum):
            vals = [m.value for m in ann]
            out = {"enum": vals}
            kinds = {type(v) for v in vals}
            if len(kinds) == 1 and next(iter(kinds)) in _PRIM:
                out["type"] = _PRIM[next(iter(kinds))]
            return out
        if ann in _FORMATS:
            return {"type": "string", "format": _FORMATS[ann]}
        if issubclass(ann, _STRINGISH):
            return {"type": "string"}
        if callable(getattr(ann, "model_json_schema", None)):  # pydantic, no import
            try:
                out = ann.model_json_schema()
            except Exception:
                return {"type": "object"}
            if closed:
                out.setdefault("additionalProperties", False)
                for d in out.get("$defs", {}).values():
                    if d.get("type") == "object":
                        d.setdefault("additionalProperties", False)
            return out
    if typing.is_typeddict(ann) or dataclasses.is_dataclass(ann):
        return _object_schema(ann, closed)
    origin = typing.get_origin(ann)
    if origin is typing.Literal:
        vals = [a.value if isinstance(a, enum.Enum) else a for a in typing.get_args(ann)]
        try:
            json.dumps(vals, allow_nan=False)
        except (TypeError, ValueError):
            raise TypeError(f"Literal{list(typing.get_args(ann))!r} has a member "
                            f"that is not JSON") from None
        out = {"enum": vals}
        kinds = {type(v) for v in vals}
        if len(kinds) == 1 and next(iter(kinds)) in _PRIM:
            out["type"] = _PRIM[next(iter(kinds))]
        return out
    if origin in (typing.Union, types.UnionType):
        return {"anyOf": [_schema(a, closed) if a is not type(None) else {"type": "null"}
                          for a in typing.get_args(ann)]}
    args = typing.get_args(ann)
    if origin is tuple and args and args[-1] is not Ellipsis:
        return {"type": "array", "prefixItems": [_schema(a, closed) for a in args],
                "minItems": len(args), "maxItems": len(args)}
    if origin in (list, set, tuple, frozenset):
        out = {"type": "array", **({"items": _schema(args[0], closed)} if args else {})}
        if origin in (set, frozenset):
            out["uniqueItems"] = True
        return out
    if origin is dict:
        return {"type": "object",
                **({"additionalProperties": _schema(args[1], closed)} if len(args) == 2 else {})}
    return {}


def _fields(ann):
    """(name -> hint, optional-name set) for a dataclass or TypedDict,
    excluding dataclass fields the constructor does not accept."""
    hints = _hints(ann)
    if dataclasses.is_dataclass(ann):
        fs = [f for f in dataclasses.fields(ann) if f.init]
        names = [f.name for f in fs]
        optional = {f.name for f in fs
                    if f.default is not dataclasses.MISSING
                    or f.default_factory is not dataclasses.MISSING}  # type: ignore[misc]
    else:
        names = list(hints) or list(_raw_annotations(ann))
        optional = set(names) - set(getattr(ann, "__required_keys__", names))
    return {k: hints.get(k) for k in names}, optional


def _object_schema(ann, closed=False) -> dict:
    """Build an object schema from a TypedDict or dataclass."""
    fields, optional = _fields(ann)
    out = {"type": "object", "properties": {k: _schema(v, closed) for k, v in fields.items()}}
    required = [k for k in fields if k not in optional]
    if required:
        out["required"] = required
    if closed:
        out["additionalProperties"] = False
    return out


def _localize(node, prefix: str, defs: dict):
    """Pull nested `$defs` (pydantic emits them per model) out of `node` into
    `defs`, namespaced by `prefix` so two models' same-named inner classes
    cannot collide, and rewrite the `$ref`s that point at them."""
    if isinstance(node, dict):
        for k, v in node.pop("$defs", {}).items():
            defs[prefix + k] = _localize(v, prefix, defs)
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            node["$ref"] = "#/$defs/" + prefix + ref[8:]
        for k in list(node):
            node[k] = _localize(node[k], prefix, defs)
    elif isinstance(node, list):
        return [_localize(v, prefix, defs) for v in node]
    return node


def _hoist_defs(schema: dict, prefix: str = "") -> dict:
    defs: dict = {}
    schema = _localize(schema, prefix, defs)
    if defs:
        schema["$defs"] = {**schema.get("$defs", {}), **defs}
    return schema


def _jsonable_default(v):
    """A JSON-safe rendering of a parameter default, or _MISSING to omit it."""
    if isinstance(v, enum.Enum):
        v = v.value
    elif isinstance(v, (datetime.date, datetime.datetime, datetime.time)):
        v = v.isoformat()
    elif isinstance(v, pathlib.PurePath):
        v = str(v)
    elif dataclasses.is_dataclass(v) and not isinstance(v, type):
        v = dataclasses.asdict(v)
    try:
        json.dumps(v, allow_nan=False)
        return v
    except (TypeError, ValueError):
        return _MISSING


def _input_schema(fn) -> tuple[dict, dict]:
    """Return (inputSchema, inject) where inject records the Principal/Context
    parameter names and the resolved hints used to deserialize arguments."""
    hints = _hints(fn)
    _desc, docs = _parse_doc(inspect.getdoc(fn))
    bound = set(fn.keywords) if isinstance(fn, functools.partial) else set()
    props, required, defs = {}, [], {}
    inject = {"principal": None, "context": None, "hints": hints}
    var_kw = False
    sig = inspect.signature(fn)
    if sig.parameters and all(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
                              for p in sig.parameters.values()):
        raise TypeError(f"{_fname(fn)}: signature is only *args/**kwargs, so no schema "
                        f"can be derived (a decorator without functools.wraps?)")
    for name, p in sig.parameters.items():
        if p.kind is p.VAR_KEYWORD:
            var_kw = True
            continue
        if p.kind is p.VAR_POSITIONAL or name == "self" or name in bound:
            continue
        if p.kind is p.POSITIONAL_ONLY:
            raise TypeError(f"{_fname(fn)}: parameter {name!r} is positional-only; "
                            f"MCP arguments are passed by name")
        ann = hints.get(name, p.annotation)
        if _is_marker(ann, Principal):
            inject["principal"] = name    # injected, never exposed to the model
            continue
        if _is_marker(ann, Context):
            inject["context"] = name
            continue
        sub = _marker_subclass(ann)
        if sub:
            raise TypeError(f"{_fname(fn)}: parameter {name!r} is annotated with "
                            f"{sub.__name__}, a subclass of a micromcp marker; annotate "
                            f"with the marker itself (subclasses are neither injected "
                            f"nor exposed)")
        if isinstance(ann, str) and _string_marker(ann):
            # Reads like a marker but does not resolve to micromcp's class: it
            # is either an app's own Principal (must stay a client argument)
            # or ours under a TYPE_CHECKING-only import (must be injected).
            # Guessing either way is a security bug, so refuse.
            raise TypeError(f"{_fname(fn)}: annotation {ann!r} on parameter {name!r} "
                            f"does not resolve at runtime; import micromcp."
                            f"{_string_marker(ann)} normally, or rename your own class")
        s = _hoist_defs(_schema(ann, closed=True), f"{name}__")
        defs.update(s.pop("$defs", {}))
        if name in docs:
            s = {**s, "description": docs[name]}
        if p.default is inspect.Parameter.empty:
            required.append(name)
        else:
            d = _jsonable_default(p.default)
            if d is not _MISSING:
                s = {**s, "default": d}
        props[name] = s
    out = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    if not var_kw:
        out["additionalProperties"] = False
    if defs:
        out["$defs"] = defs
    return out, inject


def _output_schema(fn, explicit):
    if explicit is not None:
        return _hoist_defs(dict(explicit))
    ret = _hints(fn).get("return")
    if ret is not None and (typing.is_typeddict(ret) or dataclasses.is_dataclass(ret)):
        return _hoist_defs(_object_schema(ret))
    return None          # a bare `-> dict` says nothing worth declaring


# ---------------------------------------------------------------- JSON -> typed values
def _from_json(v, ann, depth=0):
    """Inverse of `_schema`: turn validated JSON into what the handler declared
    (dates, paths, enums, sets, tuples, dataclasses, pydantic models)."""
    if depth > MAX_DEPTH:
        raise ValueError("nested too deeply")
    ann = _unwrap(ann)
    if v is None or ann is None or ann is inspect.Parameter.empty or ann is typing.Any \
            or isinstance(ann, str) or ann in _PRIM:
        return v
    if hasattr(ann, "__supertype__"):
        return _from_json(v, ann.__supertype__, depth)
    if isinstance(ann, type):
        if issubclass(ann, enum.Enum):
            return ann(v)
        if ann in _FORMATS and isinstance(v, str):
            return ann.fromisoformat(v)
        if issubclass(ann, pathlib.PurePath) and isinstance(v, str):
            return ann(v)
        if issubclass(ann, (bytes, bytearray)) and isinstance(v, str):
            return ann(v.encode())
        if callable(getattr(ann, "model_validate", None)):
            return ann.model_validate(v)
        if (dataclasses.is_dataclass(ann) or typing.is_typeddict(ann)) and isinstance(v, dict):
            fields, _opt = _fields(ann)
            out = {k: _from_json(x, fields.get(k), depth + 1) for k, x in v.items()}
            return ann(**out) if dataclasses.is_dataclass(ann) else out
        return v
    origin, args = typing.get_origin(ann), typing.get_args(ann)
    if origin is typing.Literal:
        for a in args:                       # map a Literal[Enum.X] value back to the member
            if isinstance(a, enum.Enum) and a.value == v and type(a.value) is type(v):
                return a
        return v
    if origin in (typing.Union, types.UnionType):
        for a in args:
            if a is type(None):
                continue
            if _check(v, _schema(a, closed=True)) is None:
                return _from_json(v, a, depth)
        return v
    if origin is list and args:
        return [_from_json(x, args[0], depth + 1) for x in v]
    if origin in (set, frozenset):
        return origin(_from_json(x, args[0], depth + 1) if args else x for x in v)
    if origin is tuple:
        if args and args[-1] is not Ellipsis:
            return tuple(_from_json(x, a, depth + 1) for x, a in zip(v, args, strict=False))
        return tuple(_from_json(x, args[0], depth + 1) if args else x for x in v)
    if origin is dict and len(args) == 2:
        return {k: _from_json(x, args[1], depth + 1) for k, x in v.items()}
    return v


# ---------------------------------------------------------------- validator
_CHECKS = {
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def _same(a, b) -> bool:
    """Equality that does not let True == 1 or 1.0 == 1."""
    return a == b and type(a) is type(b)


@functools.lru_cache(maxsize=256)
def _pattern(p: str):
    return re.compile(p)


def _check(value, schema, path="", root=None, depth=0) -> str | None:
    """Validate `value` against JSON Schema: the subset `_schema` emits plus the
    value constraints pydantic emits. Unknown constructs fail closed.

    Returns a human-readable problem, or None when the value conforms.
    """
    if not schema:
        return None
    where = path or "value"
    if depth > MAX_DEPTH:
        return f"{where} is nested too deeply"
    root = root if root is not None else schema
    if "$ref" in schema:
        ref = schema["$ref"]
        target = (root.get("$defs", {}).get(ref[8:])
                  if isinstance(ref, str) and ref.startswith("#/$defs/") else None)
        if target is None:
            return f"{where}: unresolvable $ref {ref!r}"
        return _check(value, target, path, root, depth + 1)
    if "anyOf" in schema:
        if any(_check(value, s, path, root, depth + 1) is None for s in schema["anyOf"]):
            return None
        return f"{where} matches none of the allowed types"
    if "oneOf" in schema:
        hits = sum(_check(value, s, path, root, depth + 1) is None for s in schema["oneOf"])
        if hits != 1:
            return f"{where} must match exactly one alternative (matched {hits})"
    for s in schema.get("allOf", ()):
        err = _check(value, s, path, root, depth + 1)
        if err:
            return err
    if "const" in schema and not _same(value, schema["const"]):
        return f"{where} must be {schema['const']!r}"
    if "enum" in schema and not any(_same(value, e) for e in schema["enum"]):
        return f"{where} must be one of {schema['enum']!r}"
    t = schema.get("type")
    if t is not None and (not isinstance(t, str) or t not in _CHECKS):
        return f"{where}: unsupported schema type {t!r}"
    if t and not _CHECKS[t](value):
        return f"{where} must be {t}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        lo, hi = schema.get("minimum"), schema.get("maximum")
        xlo, xhi = schema.get("exclusiveMinimum"), schema.get("exclusiveMaximum")
        if lo is not None and value < lo:
            return f"{where} must be >= {lo}"
        if hi is not None and value > hi:
            return f"{where} must be <= {hi}"
        if xlo is not None and value <= xlo:
            return f"{where} must be > {xlo}"
        if xhi is not None and value >= xhi:
            return f"{where} must be < {xhi}"
        mult = schema.get("multipleOf")
        if mult and (value / mult) % 1 not in (0, 0.0):
            return f"{where} must be a multiple of {mult}"
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            return f"{where} must be at least {schema['minLength']} characters"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"{where} must be at most {schema['maxLength']} characters"
        if "pattern" in schema and not _pattern(schema["pattern"]).search(value):
            return f"{where} does not match the required pattern"
        parser = _FORMAT_PARSERS.get(schema.get("format"))
        if parser:
            try:
                parser(value)
            except ValueError:
                return f"{where} is not a valid {schema['format']}"
    if t == "array":
        if "minItems" in schema and len(value) < schema["minItems"]:
            return f"{where} needs at least {schema['minItems']} items"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"{where} allows at most {schema['maxItems']} items"
        if schema.get("uniqueItems"):
            seen = [json.dumps(v, sort_keys=True, default=str) for v in value]
            if len(set(seen)) != len(seen):
                return f"{where} must not contain duplicates"
        for i, v in enumerate(value):
            sub = (schema["prefixItems"][i] if i < len(schema.get("prefixItems", ()))
                   else schema.get("items"))
            err = _check(v, sub, f"{where}[{i}]", root, depth + 1) if sub else None
            if err:
                return err
    if t == "object":
        props = schema.get("properties", {})
        prefix = f"{path}." if path else ""
        for k in schema.get("required", ()):
            if k not in value:
                return f"missing required {prefix}{k}"
        extra_schema = schema.get("additionalProperties")
        extra = sorted(set(value) - set(props))
        if extra_schema is False and extra:
            return f"unexpected {prefix and prefix[:-1] + ': '}{extra!r}"
        for k, v in value.items():
            sub = props.get(k, extra_schema if isinstance(extra_schema, dict) else None)
            err = _check(v, sub, f"{prefix}{k}", root, depth + 1) if sub else None
            if err:
                return err
    return None
