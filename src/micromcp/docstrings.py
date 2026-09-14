"""Docstring parsing: the description the model reads, and per-parameter text."""

from __future__ import annotations

import re

_SECTION_RE = re.compile(r"^(Args|Arguments|Parameters|Params)\s*:?\s*$", re.I)
_PARAM_RE = re.compile(r"^(\w+)\s*(?:\(([^)]*)\))?\s*:\s*(.*)$")      # Google
_NUMPY_PARAM_RE = re.compile(r"^(\w+)\s*:\s*(\S.*)?$")               # NumPy `name : type`


def _parse_doc(doc: str | None) -> tuple[str, dict[str, str]]:
    """Split a docstring into (description, {param: description}).

    Understands Google style (`Args:` at column 0, then indented
    `name (type): text` with deeper-indented continuation lines) and NumPy
    style (`Parameters` + dashes, then `name : type` with an indented
    description). Only a column-0 header opens the section, so an `Args:`
    inside an indented example block is left alone; the section ends at the
    next column-0 line (a `Returns:` immediately after is kept). Prose lines
    inside the section attach to the parameter above them. The Args section
    is removed from the description; everything else is kept.
    """
    if not doc:
        return "", {}
    lines = doc.splitlines()
    keep, params, i = [], {}, 0
    while i < len(lines):
        line = lines[i]
        if not line[:1].isspace() and _SECTION_RE.match(line.strip()):
            i += 1
            numpy = i < len(lines) and lines[i].strip() and set(lines[i].strip()) <= {"-", "="}
            if numpy:
                i += 1
            current, param_indent = None, 0
            while i < len(lines):
                raw = lines[i]
                if not raw.strip():
                    i += 1
                    continue
                indent = len(raw) - len(raw.lstrip())
                if numpy and indent == 0:
                    m = _NUMPY_PARAM_RE.match(raw)
                    if not m:
                        break
                    current = m.group(1)
                    params[current] = ""
                elif not numpy and indent == 0:
                    break                                    # next column-0 line ends the section
                elif not numpy and (current is None or indent <= param_indent) \
                        and _PARAM_RE.match(raw.strip()):
                    m = _PARAM_RE.match(raw.strip())
                    current, param_indent = m.group(1), indent
                    params[current] = m.group(3).strip()
                elif current is not None:
                    params[current] = (params[current] + " " + raw.strip()).strip()
                i += 1
            continue
        keep.append(line)
        i += 1
    desc = "\n".join(keep).strip()
    return desc, {k: v for k, v in params.items() if v}
