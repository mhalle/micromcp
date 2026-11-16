"""Skills over MCP (`io.modelcontextprotocol/skills`): a skill's files
snapshotted at registration, its SKILL.md frontmatter read by a strict YAML
subset, and the `Skill` entries `skills/list` and `skills/get` return."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import mimetypes
import os
import pathlib
import re

from ._constants import SKILL_MAX_BYTES, SKILL_MAX_FILES

# Agent Skills `name`: 1-64 of a-z, 0-9 and single inner hyphens. Matched with
# fullmatch: `$` would also accept a trailing newline.
_SKILL_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# Path segments (prefix and file paths) are kept to RFC 3986 unreserved
# characters, so every skill URI is its own spelling: nothing to percent-encode.
_SEGMENT_RE = re.compile(r"[A-Za-z0-9._~-]+")

# --------------------------------------------------------------- frontmatter
# Hosts parse SKILL.md frontmatter with their own YAML library and compare it
# field by field with the entry, so the entry must be what any YAML parser
# reads. The subset below covers the Agent Skills fields (top-level scalars, a
# one-level `metadata` map, `|`/`>` block scalars) and refuses the rest, and
# refuses plain scalars that YAML 1.1 and 1.2 parsers read differently.
_KEY_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*):(?:[ \t]+(.*))?$")
_YAML11_BOOL = {"y", "Y", "yes", "Yes", "YES", "n", "N", "no", "No", "NO",
                "on", "On", "ON", "off", "Off", "OFF"}
_FM_INT_RE = re.compile(r"^[-+]?[0-9]+$")
_FM_FLOAT_RE = re.compile(r"^[-+]?(?:\.[0-9]+|[0-9]+\.[0-9]*)$")
_AMBIGUOUS_RE = re.compile(
    r"^[-+]?0[0-9]+$"                                   # 1.1 octal, 1.2 decimal
    r"|^[-+]?0[obxOBX]"                                 # base prefixes
    r"|^[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)[eE][-+]?[0-9]+$"   # exponents
    r"|^[-+]\.[0-9]"                                    # signed .5: a string to 1.1
    r"|^[-+]?[0-9]+(?::[0-5]?[0-9])+(?:\.[0-9]*)?$"     # 1.1 sexagesimal
    r"|^[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:$|[Tt \t])"     # 1.1 timestamps (not JSON)
    r"|^[-+]?\.(?:inf|Inf|INF|nan|NaN|NAN)$")           # not JSON
# A token of digits, dots, colons and underscores is a number to YAML 1.1 when
# it holds an underscore (1_000, 1.0_, 1_:0) and a string to 1.2. Tested with
# one linear match plus membership checks: nested quantifiers here backtrack.
_NUMERIC_CHARS_RE = re.compile(r"[-+]?[0-9._:]+")
# What YAML does not read as an ordinary character on a line: C0 controls but
# tab (a lone CR is a line break to every YAML), DEL, C1 (NEL is a line break to
# YAML 1.1), the Unicode line and paragraph separators (also line breaks to 1.1),
# a BOM, and the noncharacters YAML excludes.
_FM_BAD_CHAR_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\ufeff\ufffe\uffff]")
# Far above what the Agent Skills fields need (a description is at most 1024
# characters), and a bound on everything the parser does.
_FRONTMATTER_MAX = 64 * 1024


class FrontmatterError(ValueError):
    """SKILL.md frontmatter outside the subset this server reads exactly."""


def _plain(text: str, where: str):
    """Resolve a plain scalar the way both YAML 1.1 and 1.2 do, or refuse it."""
    if text in ("", "~", "null", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if text in _YAML11_BOOL or _AMBIGUOUS_RE.search(text) or (
            "_" in text and _NUMERIC_CHARS_RE.fullmatch(text)
            and any(c.isdigit() for c in text)):
        raise FrontmatterError(f"{where}: {text!r} reads differently across YAML parsers; "
                               f"quote it")
    if _FM_INT_RE.match(text):
        return int(text)
    if _FM_FLOAT_RE.match(text):
        value = float(text)
        if not math.isfinite(value):           # 1e400 written out: inf, which JSON lacks
            raise FrontmatterError(f"{where}: {text!r} is too large to be a JSON number; "
                                   f"quote it")
        return value
    return text


def _scalar(rest: str, where: str):
    """The value after `key:` on one line: quoted, or plain with an
    optional trailing comment."""
    if rest.startswith("'"):
        m = re.match(r"^'((?:[^']|'')*)'[ \t]*(?:#.*)?$", rest)
        if not m:
            raise FrontmatterError(f"{where}: a single-quoted value must close on its line")
        return m.group(1).replace("''", "'")
    if rest.startswith('"'):
        m = re.match(r'^"((?:[^"\\]|\\.)*)"[ \t]*(?:#.*)?$', rest)
        if not m:
            raise FrontmatterError(f"{where}: a double-quoted value must close on its line")
        for esc in re.finditer(r"\\(?:u([0-9a-fA-F]{4})|.)", m.group(1)):
            # JSON joins an escaped surrogate pair into one character; YAML
            # reads each escape as a code point of its own.
            if esc.group(1) and 0xD800 <= int(esc.group(1), 16) <= 0xDFFF:
                raise FrontmatterError(f"{where}: \\u{esc.group(1)} is a surrogate, which YAML "
                                       f"and JSON read differently; write the character itself")
        try:                                  # YAML's JSON-compatible escapes only
            return json.loads('"' + m.group(1) + '"', strict=False)
        except ValueError:
            raise FrontmatterError(f"{where}: unsupported escape in {rest!r}; use the "
                                   f"JSON escapes (\\n, \\t, \\\", \\\\, \\uXXXX)") from None
    text = re.split(r"[ \t]#", rest, maxsplit=1)[0].strip(" \t")
    if text[:1] in tuple("[]{},&*!|>%@`") or text[:2] in ("- ", "? ", ": ") \
            or text in ("-", "?", ":"):
        raise FrontmatterError(f"{where}: {text!r} starts with a YAML indicator; quote it")
    if ": " in text or text.endswith(":"):
        raise FrontmatterError(f"{where}: {text!r} contains ': '; quote it")
    return _plain(text, where)


def _block(lines, i, header, parent, where):
    """A `|` or `>` block scalar starting after line i; returns (value, next i)."""
    m = re.match(r"^([|>])(-?)[ \t]*(?:#.*)?$", header)
    if not m:
        raise FrontmatterError(f"{where}: only |, |-, > and >- block scalars are supported")
    style, strip = m.groups()
    body, j, indent, blank_widths = [], i + 1, None, []
    while j < len(lines):
        line = lines[j]
        if line.strip(" \t") == "":
            if "\t" in line:
                raise FrontmatterError(f"{where}: a blank line in a block scalar holds a tab")
            blank_widths.append(len(line))
            body.append("")
            j += 1
            continue
        lead = len(line) - len(line.lstrip(" "))
        if line[:lead + 1].count("\t"):
            raise FrontmatterError(f"{where}: tabs may not indent a block scalar")
        if lead <= parent:
            break
        if indent is None:
            indent = lead
        if lead < indent:
            raise FrontmatterError(f"{where}: a block scalar line is less indented than its first")
        if style == ">" and lead > indent:
            raise FrontmatterError(f"{where}: more-indented lines in a > block are not supported; "
                                   f"use |")
        body.append(line[indent:])
        j += 1
    if any(width > (indent or 0) for width in blank_widths):
        # Spaces past the block's indentation make a "blank" line content to
        # YAML, which keeps them; refuse rather than read it differently.
        raise FrontmatterError(f"{where}: a blank line in a block scalar is indented past the "
                               f"block; remove its trailing spaces")
    while body and body[-1] == "":
        body.pop()
    if style == "|":
        text = "\n".join(body)
    else:
        parts, prev = [], False                # joined once: += here is quadratic
        for line in body:
            if line == "":
                parts.append("\n")
                prev = False
            else:
                parts.append((" " if prev else "") + line)
                prev = True
        text = "".join(parts)
    if body and not strip:
        text += "\n"
    return text, j


def _mapping(lines, i, indent, where):
    """Key/value lines at exactly `indent`, from line i; returns (dict, next i)."""
    out = {}
    while i < len(lines):
        line = lines[i]
        stripped = line.strip(" \t")
        if stripped == "" or stripped.startswith("#"):
            i += 1
            continue
        lead = len(line) - len(line.lstrip(" "))
        if "\t" in line[:lead + 1]:
            raise FrontmatterError(f"{where}: tabs may not indent frontmatter")
        if lead < indent:
            break
        if lead > indent:
            raise FrontmatterError(f"{where}: unexpected indentation at {stripped!r}; "
                                   f"multi-line plain values are not supported (quote it or use >)")
        m = _KEY_RE.match(line[lead:])
        if not m:
            raise FrontmatterError(f"{where}: expected 'key: value', got {stripped!r} "
                                   f"(lists, flow collections and quoted keys are not supported)")
        key, rest = m.group(1), (m.group(2) or "").strip(" \t")
        here = f"{where} {key!r}"
        if not isinstance(_plain(key, here), str):
            # true:, 1:, null: are booleans, numbers, and null as YAML keys.
            raise FrontmatterError(f"{where}: key {key!r} is not a string to YAML; rename it")
        if key in out:
            raise FrontmatterError(f"{where}: duplicate key {key!r}")
        if rest[:1] in ("|", ">"):
            out[key], i = _block(lines, i, rest, lead, here)
            continue
        if rest == "" or rest.startswith("#"):
            j = i + 1
            while j < len(lines) and (lines[j].strip(" \t") == ""
                                      or lines[j].strip(" \t").startswith("#")):
                j += 1
            child = len(lines[j]) - len(lines[j].lstrip(" ")) if j < len(lines) else 0
            if j < len(lines) and child > lead:
                if indent > 0:
                    raise FrontmatterError(f"{here}: maps nest one level only")
                out[key], i = _mapping(lines, j, child, here)
                continue
            out[key], i = None, i + 1
            continue
        out[key] = _scalar(rest, here)
        i += 1
    return out, i


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split SKILL.md into (frontmatter dict, body). The frontmatter must
    open the file with a `---` line and close with another."""
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in text.split("\n")]
    if not lines or lines[0] != "---":
        raise FrontmatterError("SKILL.md must begin with a '---' line opening its YAML frontmatter")
    try:
        end = next(k for k in range(1, len(lines)) if lines[k] in ("---", "..."))
    except StopIteration:
        raise FrontmatterError("SKILL.md frontmatter is not closed by a '---' line") from None
    if sum(len(ln) + 1 for ln in lines[1:end]) > _FRONTMATTER_MAX:
        raise FrontmatterError(f"SKILL.md frontmatter is over {_FRONTMATTER_MAX} characters")
    for k in range(1, end):
        bad = _FM_BAD_CHAR_RE.search(lines[k])
        if bad:
            # The server and a host's YAML parser would see different lines,
            # and so different keys, from the same bytes.
            raise FrontmatterError(f"SKILL.md frontmatter line {k + 1} holds "
                                   f"U+{ord(bad.group()):04X}, which YAML reads as a line "
                                   f"break or does not allow; remove it")
    data, _ = _mapping(lines[1:end], 0, 0, "SKILL.md frontmatter")
    return data, "\n".join(lines[end + 1:])


def check_frontmatter(fm: dict, where: str) -> None:
    """The Agent Skills rules a host will hold the frontmatter to."""
    name = fm.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= 64 or not _SKILL_NAME_RE.fullmatch(name):
        raise ValueError(f"{where}: frontmatter name must be 1-64 lowercase letters, digits and "
                         f"single hyphens, not starting or ending with one; got {name!r}")
    desc = fm.get("description")
    if not isinstance(desc, str) or not 1 <= len(desc) <= 1024:
        raise ValueError(f"{where}: frontmatter description must be a string of 1-1024 characters")
    for key, limit in (("license", None), ("compatibility", 500), ("allowed-tools", None)):
        bad = not isinstance(fm.get(key), str) or (limit and not 1 <= len(fm[key]) <= limit)
        if key in fm and bad:
            raise ValueError(f"{where}: frontmatter {key} must be a string"
                             + (f" of 1-{limit} characters" if limit else ""))
    meta = fm.get("metadata")
    if "metadata" in fm and (not isinstance(meta, dict)
                             or not all(isinstance(v, str) for v in meta.values())):
        raise ValueError(f"{where}: frontmatter metadata must map keys to strings "
                         f"(quote values such as version: \"1.0\")")


# ------------------------------------------------------------------ snapshot
def _segments(path: str, what: str) -> list[str]:
    parts = path.split("/")
    for seg in parts:
        if seg in ("", ".", "..") or not _SEGMENT_RE.fullmatch(seg):
            raise ValueError(f"{what} {path!r}: each segment must be letters, digits, '.', '_', "
                             f"'~' or '-' (and not '.' or '..')")
    return parts


def _hidden(parts) -> bool:
    return any(p.startswith(".") or p == "__pycache__" for p in parts)


def _too_big() -> ValueError:
    return ValueError(f"a skill may hold at most {SKILL_MAX_FILES} files and "
                      f"{SKILL_MAX_BYTES} bytes, the limits every host must accept")


def _read_dir(root: pathlib.Path) -> dict[str, bytes]:
    """Every regular file under root, by '/'-joined relative path. Dot-files,
    dot-directories and __pycache__ are skipped; a symlink is followed only
    when it stays inside the skill directory and does not lead to one of
    those. The limits are enforced as files are found and read, so a huge or
    sparse file is refused without being read whole."""
    real = root.resolve()
    files: dict[str, bytes] = {}
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = pathlib.Path(dirpath)
        keep = []
        for d in sorted(dirnames):
            if d.startswith(".") or d == "__pycache__":
                continue
            if (here / d).is_symlink():
                raise ValueError(f"skill {root}: symlinked directory {here / d} is not supported")
            keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            if f.startswith("."):
                continue
            p = here / f
            target = p.resolve()
            if not target.is_relative_to(real):
                raise ValueError(f"skill {root}: {p} links outside the skill directory")
            if _hidden(target.relative_to(real).parts):
                raise ValueError(f"skill {root}: {p} links to a dot-file or __pycache__, "
                                 f"which a skill does not serve")
            if not target.is_file():
                continue
            if len(files) >= SKILL_MAX_FILES or total + target.stat().st_size > SKILL_MAX_BYTES:
                raise _too_big()
            with open(target, "rb") as fh:        # bounded: the file may have grown since
                data = fh.read(SKILL_MAX_BYTES - total + 1)
            total += len(data)
            if total > SKILL_MAX_BYTES:
                raise _too_big()
            files[p.relative_to(root).as_posix()] = data
    return files


def _mime(rel: str, data: bytes) -> str:
    if rel.endswith(".md"):
        return "text/markdown"
    guessed = mimetypes.guess_type(rel, strict=False)[0]
    if guessed:
        return guessed
    return "text/plain" if _as_text(data) is not None else "application/octet-stream"


def _as_text(data: bytes):
    """The str a client re-encodes to exactly these bytes, or None for binary."""
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def build_skill(source, prefix=None) -> dict:
    """Snapshot a skill from a directory path or a {relative path: str|bytes}
    mapping. Returns the record the registry stores: its URI, root, entry,
    and per-file bytes and MIME types."""
    if isinstance(source, (str, os.PathLike)):
        root = pathlib.Path(source)
        if not (root / "SKILL.md").is_file():
            raise ValueError(f"skill {root}: no SKILL.md in that directory")
        files, dirname = _read_dir(root), root.resolve().name
    elif isinstance(source, dict):
        files, dirname = {}, None
        for rel, data in source.items():
            if not isinstance(rel, str):
                raise ValueError(f"skill file paths must be strings, got {rel!r}")
            if isinstance(data, str):
                data = data.encode("utf-8")
            if not isinstance(data, (bytes, bytearray)):
                raise ValueError(f"skill file {rel!r} must be str or bytes")
            files[rel] = bytes(data)
    else:
        raise TypeError("a skill is a directory path or a {path: content} dict")
    if "SKILL.md" not in files:
        raise ValueError("a skill needs a SKILL.md at its root")
    folded: dict[str, str] = {}
    dirs = set()
    for rel in files:
        parts = _segments(rel, "skill file")
        dirs.update("/".join(parts[:k]) for k in range(1, len(parts)))
        other = folded.setdefault(rel.casefold(), rel)
        if other != rel:
            # One of the two is lost, or swapped, wherever a host writes them
            # to a case-insensitive filesystem.
            raise ValueError(f"skill files {other!r} and {rel!r} differ only in case")
    clash = sorted(dirs.intersection(files))
    if clash:
        raise ValueError(f"skill path {clash[0]!r} is both a file and a directory")
    if len(files) > SKILL_MAX_FILES or sum(map(len, files.values())) > SKILL_MAX_BYTES:
        raise _too_big()
    text = _as_text(files["SKILL.md"])
    if text is None:
        raise ValueError("SKILL.md must be UTF-8 text")
    fm, _body = parse_frontmatter(text)
    check_frontmatter(fm, "SKILL.md")
    name = fm["name"]
    if dirname is not None and dirname != name:
        raise ValueError(f"skill {source}: frontmatter name {name!r} must match its "
                         f"directory name {dirname!r}")
    pre = "/".join(_segments(prefix, "skill prefix")) + "/" if prefix else ""
    root_uri = f"skill://{pre}{name}"
    out_files = {f"{root_uri}/{rel}": (data, _mime(rel, data))
                 for rel, data in sorted(files.items())}
    entry = {
        "uri": f"{root_uri}/SKILL.md",
        "frontmatter": fm,
        "resources": [{"uri": u, "digest": "sha256:" + hashlib.sha256(d).hexdigest(),
                       "size": len(d)} for u, (d, _m) in out_files.items()],
    }
    return {"uri": entry["uri"], "root": root_uri, "name": name, "entry": entry,
            "files": out_files}


def read_contents(uri: str, data: bytes, mime: str) -> dict:
    """A `resources/read` contents item: text when the bytes are UTF-8 (so a
    host re-encodes exactly them), else a base64 blob."""
    text = _as_text(data)
    item = {"uri": uri, "mimeType": mime}
    if text is not None:
        item["text"] = text
    else:
        item["blob"] = base64.b64encode(data).decode()
    return item
