"""Widgets, hypermedia fragments, and model context.

An MCP Apps widget is a static HTML page the host renders in a sandboxed
iframe. It talks to the host over postMessage JSON-RPC, and the host proxies
its `tools/call` requests to this server. A `Widget` is that page plus its
`ui://` resource; attach it to the tools that show it:

    board = Widget("todos", title="Todos", scripts=[Path("htmx.min.js")], body=
        '<div id="app" hx-post="tool:todo_list" hx-trigger="mcp:ready" hx-target="#app"></div>')

    @board.tool(mcp)                        # registers ui://todos, fills in the tool's _meta
    def show_todos() -> str: ...

    @mcp.tool(visibility="app")             # hidden from the model, callable by the widget
    def todo_list(): return fragment(render())

The page carries `BRIDGE_JS`, which completes the handshake and gives
hypermedia libraries a tool-call transport: `hx-post="tool:todo_add"` calls
the tool `todo_add` with the form's fields as arguments and swaps its
`fragment()` into the page. Other URLs go to the `route=` tool, which is how
Django views serve a widget (`micromcp.apps.django.django_routes`). In the
page, `mcp.setContext(text, data)` and `mcp.say(text)` talk to the model;
from the server, `fragment(..., context=...)` does.
"""

from __future__ import annotations

import html as _html
import itertools
import json
import logging
import os
import pathlib
import re
import weakref
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import quote, urlencode, urlsplit

from micromcp import Result, result

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")   # the tool-name grammar micromcp enforces

FRAGMENT_META = "micromcp/http"
CONTEXT_META = "micromcp/context"
CONTEXT_LIMIT = 16_000     # bytes of JSON: context rides along on the model's later turns

BRIDGE_JS = (pathlib.Path(__file__).with_name("bridge.js")).read_text(encoding="utf-8")
# TypeScript declarations for `window.mcp`, to copy into a bundled widget's project.
BRIDGE_TYPES = (pathlib.Path(__file__).with_name("bridge.d.ts")).read_text(encoding="utf-8")

log = logging.getLogger("micromcp.apps")


_HTTPS_RE = re.compile(r"https://[^\s\"'<>]+")
_PATHLIKE_RE = re.compile(r"[\w./-]+\.(?:m?js|css)")
_WIDGET_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_CSP_KEYS = ("connectDomains", "resourceDomains", "frameDomains", "baseUriDomains")
# An origin the way a host's CSP takes it: scheme, host (optionally *.-prefixed), optional port.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_ORIGIN_RE = re.compile(rf"(?:https?|wss?)://(?:\*\.)?{_LABEL}(?:\.{_LABEL})*(?::[0-9]{{1,5}})?")
# A string that was meant as a URL, so must not be inlined as source: it starts with http(s):,
# or it is a lone protocol-relative URL. A `// comment` line and CSS such as `a:hover` do not.
_URLISH_RE = re.compile(r"\s*(?:https?:|//[\w-]+\.[\w.-]+(?:[/?#]\S*)?\s*$)", re.I)


def _origin(url, what):
    """The origin of an asset URL, refused unless it is a plain one (no credentials,
    nothing a host could mistake for a CSP directive)."""
    u = urlsplit(url)
    origin = f"{u.scheme}://{u.netloc}"
    if "@" in u.netloc or not _ORIGIN_RE.fullmatch(origin):
        raise ValueError(f"{what} {url!r}: its origin {origin!r} is not a plain host[:port]")
    return origin


def _items(value):
    return [value] if isinstance(value, (str, os.PathLike)) else list(value or ())


def _asset(item, what):
    """An asset is inline source (str), a file (Path), or an https URL (str).
    Returns (href, origin) for a URL, (text, None) for source."""
    if isinstance(item, os.PathLike):
        return pathlib.Path(item).read_text(encoding="utf-8"), None
    if not isinstance(item, str):
        raise TypeError(f"{what} must be source text, a pathlib.Path, or an https URL")
    if _HTTPS_RE.fullmatch(item):
        return item, _origin(item, what)
    if item.startswith("http://"):
        raise ValueError(f"{what} {item!r}: widgets load only https URLs")
    if _URLISH_RE.match(item):
        raise ValueError(f"{what} {item!r} looks like a URL but is not a usable one: widgets "
                         f"load https URLs with no spaces or quotes (anything else is inlined "
                         f"as source text)")
    if _PATHLIKE_RE.fullmatch(item):
        raise ValueError(f"{what} {item!r} looks like a file name; pass pathlib.Path({item!r}) "
                         f"to inline the file, or an https URL to load it")
    return item, None


_SCRIPT_END_RE = re.compile(r"</(script)", re.I)
_SCRIPT_START_RE = re.compile(r"<script[\s/>]")


def _double_escaped(source) -> bool:
    """`<!--` followed by `<script` before the next `-->`: inside a script
    element that makes a browser skip the next `</script>`, so the rest of
    the page is swallowed as script."""
    low = source.lower()
    i = low.find("<!--")
    while i >= 0:
        end = low.find("-->", i + 4)
        if _SCRIPT_START_RE.search(low, i + 4, len(low) if end < 0 else end):
            return True
        if end < 0:
            return False
        i = low.find("<!--", end + 3)
    return False


def _script(source, module=False, escape=False):
    """An inline script element. `</script` inside the source would end the
    element early: it is refused, or with `escape` rewritten as `<\\/script`,
    which means the same in the strings, templates, and regular expressions
    where it occurs (bundlers such as Vite emit it that way), except in the
    raw text of a `String.raw` template, which keeps the backslash. `<!--`
    followed by `<script` is refused either way."""
    if _double_escaped(source):
        raise ValueError("an inlined script contains '<!--' followed by '<script', which makes "
                         "a browser swallow the rest of the page; break the sequence up in the "
                         "source")
    if escape:
        source = _SCRIPT_END_RE.sub(r"<\\/\1", source)
    elif "</script" in source.lower():
        raise ValueError("an inlined script contains '</script'; it would end the tag early "
                         "(escape_scripts=True rewrites it as '<\\/script', as bundlers do)")
    kind = ' type="module"' if module else ""
    return f"<script{kind}>{source}</script>"


class SupportsHTML(Protocol):
    """Anything that renders itself as HTML (the markupsafe protocol):
    FastHTML's components, htpy elements, `markupsafe.Markup`."""

    def __html__(self) -> str: ...


# What the HTML arguments take: a str or a `SupportsHTML`. Spelled `Any` because
# fastcore attaches `FT.__html__` at runtime, where static checkers cannot see it.
HTMLLike = Any


def _html_method(value):
    """`value.__html__`, found the way Python finds special methods: in the
    class's MRO, not through a `__getattr__` (the instance's or the
    metaclass's) and not in the instance's own dict. None when absent or
    set to None, Python's spelling of "not supported"."""
    for klass in type(value).__mro__:
        if "__html__" in vars(klass):
            attr = vars(klass)["__html__"]
            if attr is None:
                return None
            get = getattr(type(attr), "__get__", None)
            return get(attr, value, type(value)) if get is not None else attr
    return None


def _markup(value, what) -> str:
    """HTML from an object whose class defines `__html__`, or from a str. As
    in markupsafe, `__html__` wins, so a str subclass that renders itself (a
    badge enum, a text type that escapes itself) is rendered through it, not
    used as its plain str value. Such an object vouches that its output is
    escaped, so it is used as rendered. A proxy that only claims to be a str is refused. The result
    is an exact str: a subclass such as `Markup` escapes whatever is
    concatenated with it, which would escape the page around it."""
    render = _html_method(value)
    if render is not None:
        value = render()
        if not issubclass(type(value), str):
            raise TypeError(f"{what}: __html__() returned {type(value).__name__}, not str")
    elif not issubclass(type(value), str):
        raise TypeError(f"{what} must be a str or an object with __html__ (FastHTML "
                        f"components, htpy, markupsafe.Markup), not {type(value).__name__}")
    return str.__str__(value)


def tool_url(name: str, /, **args) -> str:
    """A `tool:` URL for a hypermedia attribute (`hx-post`, `fx-action`), its
    arguments percent-encoded so that a value can neither add arguments nor
    change others: `tool_url("item_remove", name="milk&role=admin")` carries
    one `name`. The bridge decodes it back exactly (`URLSearchParams`).
    Values travel as text, the way form fields do."""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError(f"tool_url: {name!r} is not a tool name")
    for k, v in args.items():
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            raise TypeError(f"tool_url: {k}={v!r}; values travel as text, so pass a str or "
                            f"a number")
    query = urlencode({k: str(v) for k, v in args.items()}, quote_via=quote)
    return f"tool:{name}?{query}" if query else f"tool:{name}"


# ── pages from bundlers ─────────────────────────────────────────────────────

_FILENAME_RE = re.compile(r"[^<>\s]+\.html?", re.I)


def _read(value, what):
    """A page argument given as a path is the file's text, a leading BOM
    dropped (in a page built around it, it would put the browser in quirks
    mode). A str that is only a file name is refused, not taken for a page."""
    if isinstance(value, os.PathLike):
        return pathlib.Path(value).read_text(encoding="utf-8-sig")
    if isinstance(value, str) and _FILENAME_RE.fullmatch(value.strip()):
        raise ValueError(f"{what} {value!r} looks like a file name; pass "
                         f"pathlib.Path({value!r}) to read the file")
    return value


def _bridge_parts(route, fetch) -> list:
    """The bridge's settings, as the meta elements it reads."""
    if fetch not in ("hooks", "global"):
        raise ValueError("fetch must be 'hooks' (htmx, fixi) or 'global' (replace window.fetch)")
    if route is not None and (not isinstance(route, str) or not _NAME_RE.match(route)):
        raise ValueError(f"route must be a tool name, got {route!r}")
    parts = []
    if route:
        parts.append(f'<meta name="mcp-route" content="{_html.escape(route)}">')
    if fetch == "global":
        parts.append('<meta name="mcp-fetch" content="global">')
    return parts


class _Start(HTMLParser):
    """Where a page's head begins, found by parsing, so that a `<head>` in a
    comment, a script, or an attribute value is not taken for it: the end of
    the first `<head>` start tag, else of the `<html>` start tag, else of the
    doctype."""

    def __init__(self, doc):
        super().__init__(convert_charrefs=True)
        self._lines = [0] + [m.end() for m in re.finditer("\n", doc)]
        self.head = self.html = self.doctype = None
        self.feed(doc)
        self.close()

    def _at(self):
        line, col = self.getpos()                 # where the construct being handled starts
        return self._lines[line - 1] + col

    def handle_starttag(self, tag, attrs):
        end = self._at() + len(self.get_starttag_text())
        if tag == "head" and self.head is None:
            self.head = end
        elif tag == "html" and self.html is None:
            self.html = end

    def handle_decl(self, decl):
        if self.doctype is None and decl.lower().startswith("doctype"):
            self.doctype = self._at() + len(decl) + 3        # "<!" decl ">"


def _with_bridge(doc, route, fetch) -> str:
    """A complete document with the bridge, and its settings, first in its head."""
    if BRIDGE_JS in doc:
        raise ValueError("html already carries micromcp's bridge; leave bridge=True off")
    prelude = "".join(_bridge_parts(route, fetch)) + _script(BRIDGE_JS)
    start = _Start(doc)
    at = next((p for p in (start.head, start.html, start.doctype) if p is not None), 0)
    return doc[:at] + prelude + doc[at:]


# Attributes whose URL a page loads, by element. `a`, `form`, and hypermedia
# attributes (hx-get, fx-action) navigate or go through the bridge; they are not loads.
# `<image>` is HTML's alias of `<img>`; `input src` loads only for type=image.
_LOADING = {"script": ("src", "href", "xlink:href"), "img": ("src", "srcset"),
            "image": ("src", "href", "xlink:href"), "source": ("src", "srcset"),
            "video": ("src", "poster"), "audio": ("src",), "track": ("src",),
            "iframe": ("src",), "embed": ("src",), "object": ("data",),
            "use": ("href", "xlink:href"), "feimage": ("href", "xlink:href"),
            "body": ("background",), "table": ("background",), "td": ("background",),
            "th": ("background",)}
_LINK_LOADS = {"stylesheet", "modulepreload", "preload"}   # a missing icon breaks nothing
_FOREIGN = {"svg", "math"}                  # a <base> in there is not the page's base
_SCHEME_RE = re.compile(r"[a-z][a-z0-9+.-]*:", re.I)
_URL_TRIM = "".join(map(chr, range(0x21)))  # what a browser trims around a URL
_HTML_WS = " \t\n\r\f"
_HTML_TOKEN_RE = re.compile(r"[^ \t\n\r\f]+")
_CSS_COMMENT_RE = re.compile(r"/\*[^*]*\*+(?:[^/*][^*]*\*+)*/")
_CSS_URL_RE = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)\s("']*))\s*\)"""
                         r"""|@import\s*["']([^"']*)["']""", re.I)
_CSS_IMAGE_SET_RE = re.compile(r"image-set\(([^)]*)\)", re.I)
_CSS_STRING_RE = re.compile(r"""["']([^"']*)["']""")
# Inline-script heuristics. Quotes include backticks, which Vite 8 writes for most
# strings; a string followed by `:` or `(` is an object key or method, not a load.
_JS_IMPORT_RE = re.compile(r"""(?:\bfrom|\bimport)\s*(?:\(\s*)?(["'`])"""
                           r"""((?:\.{1,2})?/[^"'`\s$]*)\1""")
_JS_META_URL_RE = re.compile(r"""\bnew\s+URL\(\s*(?:/\*[^*]*\*+(?:[^/*][^*]*\*+)*/\s*)?"""
                             r"""(["'`])([^"'`$]+)\1\s*,\s*(?:(?:``|""|'')\s*\+\s*)?"""
                             r"""(?:self\.location|import\.meta\.url)""")
_JS_ASSET_RE = re.compile(r"""(["'`])((?:\.{1,2})?/[\w./@%+-]+\.(?:png|jpe?g|gif|svg|webp|"""
                          r"""avif|ico|bmp|woff2?|ttf|otf|eot|css|m?js|json|wasm|mp3|mp4|"""
                          r"""webm|ogg|wav))\1(?!\s*[:(])""", re.I)


def _relative(url) -> bool:
    """As a browser's URL parser sees it: C0 controls and spaces trimmed, tabs
    and newlines removed, then relative unless it has a scheme or is a
    #fragment."""
    url = re.sub(r"[\t\n\r]", "", url.strip(_URL_TRIM))
    return bool(url) and not url.startswith("#") and not _SCHEME_RE.match(url)


def _srcset(value) -> list:
    """The URLs of a srcset: comma-separated candidates, each a URL and then
    optional descriptors (a data: URL may itself contain a comma). Only HTML
    whitespace separates them, as in a browser."""
    urls, rest = [], value
    while True:
        rest = rest.lstrip(_HTML_WS + ",")
        if not rest:
            return urls
        url = _HTML_TOKEN_RE.match(rest).group(0)
        rest = rest[len(url):]
        if url.endswith(","):
            url = url.rstrip(",")
        else:
            k = rest.find(",")
            rest = "" if k < 0 else rest[k + 1:]
        urls.append(url)


def _css_urls(css) -> list:
    """The URLs a stylesheet loads: `url()`, `@import`, and `image-set()`
    strings, comments aside."""
    css = _CSS_COMMENT_RE.sub(" ", css)
    urls = [next(g for g in m.groups() if g is not None) for m in _CSS_URL_RE.finditer(css)]
    for m in _CSS_IMAGE_SET_RE.finditer(css):
        urls += [s.group(1) for s in _CSS_STRING_RE.finditer(m.group(1))]
    return urls


class _Loads(HTMLParser):
    """The URLs a page loads (with where they appear), its `<base href>` and
    how many loads come before it, and the text of its inline scripts."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.base = self.base_at = None
        self.loads, self.scripts, self._open, self._buf = [], [], None, []
        self._noscript = self._template = self._foreign = 0

    def handle_starttag(self, tag, attrs):
        a = {}
        for k, v in attrs:                        # a browser keeps the first of a duplicate
            a.setdefault(k, "" if v is None else v)
        if tag == "noscript":
            self._noscript += 1
        elif tag == "template":
            self._template += 1
        elif tag in _FOREIGN:
            self._foreign += 1
        if tag == "base":                         # the first with href is the page's base
            if self.base is None and "href" in a and not (
                    self._noscript or self._template or self._foreign):
                self.base, self.base_at = a["href"].strip(_URL_TRIM), len(self.loads)
            return
        if self._noscript:                        # text, not elements, while scripts run
            return
        names = _LOADING.get(tag, ())
        if tag == "input" and a.get("type", "").strip().lower() == "image":
            names = ("src",)
        elif tag == "link" and set(a.get("rel", "").lower().split()) & _LINK_LOADS:
            names = ("href", "imagesrcset")
        for name in names:
            if a.get(name):
                for url in (_srcset(a[name]) if name.endswith("srcset") else [a[name]]):
                    self.loads.append((url, f"<{tag} {name}>"))
        for url in _css_urls(a.get("style", "")):
            self.loads.append((url, f"<{tag} style>"))
        if tag == "iframe" and a.get("srcdoc"):   # it resolves against this page
            inner = _Loads()
            inner.feed(a["srcdoc"])
            inner.close()
            self.loads += [(url, f"<iframe srcdoc> {where}") for url, where in inner.loads]
        if tag in ("script", "style"):
            self._open, self._buf = (tag, a.get("type", "").strip().lower()), []

    def handle_data(self, data):
        if self._open:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "noscript" and self._noscript:
            self._noscript -= 1
        elif tag == "template" and self._template:
            self._template -= 1
        elif tag in _FOREIGN and self._foreign:
            self._foreign -= 1
        if not self._open or tag != self._open[0]:
            return
        (kind, kind_type), text, self._open = self._open, "".join(self._buf), None
        if kind == "style":
            self.loads += [(url, "<style>") for url in _css_urls(text)]
        elif kind_type == "importmap":
            try:
                spec = json.loads(text)
            except ValueError:
                return
            if not isinstance(spec, dict):
                return
            maps = [spec.get("imports")]
            if isinstance(spec.get("scopes"), dict):
                maps += list(spec["scopes"].values())
            self.loads += [(url, "<script type=importmap>") for m in maps if isinstance(m, dict)
                           for url in m.values() if isinstance(url, str)]
        elif kind_type in ("", "module", "text/javascript", "application/javascript"):
            self.scripts.append(text)


def _check_urls(doc, what):
    """Refuse a page that loads a relative URL: a widget is a document with no
    origin, so the host cannot fetch it (a bundler's split chunks, hashed
    assets, and `public/` files all end up this way). Relative paths inside
    inline scripts are only logged, since they may be mere strings. Loads that
    follow a valid https `<base href>` resolve against it and pass."""
    parser = _Loads()
    try:
        parser.feed(doc)
        parser.close()
    except Exception as e:               # never a silent pass: say the page went unchecked
        log.warning("%s: its page could not be checked for relative URLs (%s: %s)", what,
                    type(e).__name__, e)
        return
    base = urlsplit(parser.base or "")
    exempt = parser.base_at if base.scheme.lower() == "https" and base.hostname else None
    bad = [(url, where) for i, (url, where) in enumerate(parser.loads)
           if _relative(url) and (exempt is None or i < exempt)]
    if bad:
        url, where = bad[0]
        more = f" (and {len(bad) - 1} more)" if len(bad) > 1 else ""
        raise ValueError(f"{what} loads {url!r} ({where}){more}, a relative URL, and a widget "
                         f"has no origin to load it from: inline it (data: URLs, or "
                         f"scripts=/styles= with a pathlib.Path) or load it from an https URL. "
                         f"See 'Bundling widgets' in docs/apps.md")
    if exempt is not None:
        return
    for source in parser.scripts:
        refs = {m.group(2) for m in _JS_IMPORT_RE.finditer(source)}
        refs |= {m.group(2) for m in _JS_META_URL_RE.finditer(source) if _relative(m.group(2))}
        refs |= {m.group(2) for m in _JS_ASSET_RE.finditer(source)}
        if refs:
            log.warning("%s: an inline script refers to %s, relative paths a widget cannot "
                        "load (ignore this if they are only strings)", what,
                        ", ".join(sorted(refs)[:5]) + (" ..." if len(refs) > 5 else ""))


_ASSET_SUFFIXES = {".js", ".mjs", ".css", ".wasm", ".json", ".png", ".jpg", ".jpeg", ".gif",
                   ".svg", ".webp", ".avif", ".ico", ".bmp", ".woff", ".woff2", ".ttf", ".otf",
                   ".eot", ".mp3", ".mp4", ".webm", ".ogg", ".wav"}


def _leftovers(what, checked, passed):
    """Warn about assets next to a page or module that was passed as a path but
    not passed themselves: a working build folder holds only what the widget
    inlines, so a split chunk, a worker, or a copied `public/` file there is
    something the widget will fail to load. Source folders (a package.json,
    pyproject.toml, or Python file) are skipped, and so are sibling pages."""
    passed = {pathlib.Path(p).resolve() for p in passed if isinstance(p, os.PathLike)}
    for folder in {pathlib.Path(p).resolve().parent for p in checked if isinstance(p, os.PathLike)}:
        if (folder / "package.json").exists() or (folder / "pyproject.toml").exists() \
                or next(folder.glob("*.py"), None):
            continue
        extra = []
        for f in itertools.islice(folder.rglob("*"), 500):
            rel = f.relative_to(folder)
            if len(rel.parts) <= 2 and f.suffix.lower() in _ASSET_SUFFIXES and f not in passed \
                    and not any(p.startswith(".") or p == "node_modules" for p in rel.parts) \
                    and f.is_file():
                extra.append(rel.as_posix())
        if extra:
            log.warning("%s: %s also holds %s, which the widget cannot load (a bundler's split "
                        "chunks, workers, and public/ files end up there); see 'Bundling "
                        "widgets' in docs/apps.md", what, folder,
                        ", ".join(sorted(extra)[:5]) + (" ..." if len(extra) > 5 else ""))


def _document(body, *, title, head, scripts, modules, styles, route, fetch, imports=None,
              escape_scripts=False):
    """The widget page and the https origins it loads from."""
    body, head = _markup(_read(body, "body"), "body"), _markup(head, "head")
    if not issubclass(type(title), str):
        raise TypeError("title must be a str")
    title = str.__str__(title)           # plain text: a Markup title would be escaped twice
    settings = _bridge_parts(route, fetch)
    origins = set()
    parts = ['<meta charset="utf-8">', f"<title>{_html.escape(title)}</title>", *settings]
    for item in _items(styles):
        text, origin = _asset(item, "style")
        if origin:
            origins.add(origin)
            parts.append(f'<link rel="stylesheet" href="{_html.escape(text)}">')
        elif "</style" in text.lower():
            raise ValueError("an inlined style contains '</style'; it would end the tag early")
        else:
            parts.append(f"<style>{text}</style>")
    parts.append(head)
    if imports:                                  # an import map, before any module script
        if not isinstance(imports, dict):
            raise TypeError("imports must map module specifiers to https URLs")
        for spec, url in imports.items():
            if not isinstance(spec, str) or not spec or not isinstance(url, str) \
                    or not _HTTPS_RE.fullmatch(url):
                raise ValueError(f"imports[{spec!r}] must be an https URL, got {url!r}")
            origins.add(_origin(url, f"imports[{spec!r}]"))
        parts.append('<script type="importmap">'
                     + json.dumps({"imports": imports}).replace("</", "<\\/") + "</script>")
    parts.append(_script(BRIDGE_JS))
    # The page's own scripts follow the body, so they can reach its elements when they run;
    # the bridge and the import map stay in the head, ahead of anything that needs them.
    tail = []
    for module, group in ((False, scripts), (True, modules)):
        for item in _items(group):
            text, origin = _asset(item, "module" if module else "script")
            if origin:
                origins.add(origin)
                kind = ' type="module"' if module else ""
                tail.append(f'<script{kind} src="{_html.escape(text)}"></script>')
            else:
                tail.append(_script(text, module, escape_scripts))
    return ("<!doctype html><html><head>" + "".join(parts) + "</head><body>" + body
            + "".join(tail) + "</body></html>"), origins


def page(body: HTMLLike, *, title: str = "", head: HTMLLike = "",
         scripts=(), modules=(), styles=(), imports: dict | None = None,
         route: str | None = None, fetch: str = "hooks", escape_scripts: bool = False) -> str:
    """A complete widget document around `body`: `styles`, `head`, the
    `imports` map and `BRIDGE_JS` in the head, then `body`, then `scripts` and
    `modules` in order, so a script can reach the page's elements. Each
    asset is source text, a `pathlib.Path` (inlined), or an https URL (loaded;
    the host must allow its origin — `Widget` declares that for you).
    `imports` maps module specifiers to https URLs (`{"three": ".../three.module.js"}`)
    so modules can `import ... from "three"`. `route` names the tool that
    serves non-`tool:` URLs; `fetch="global"` lets libraries without a hook
    (Datastar) reach tools through `window.fetch`. `body` and `head` are a
    str or an object with `__html__`, as for `fragment`; `body` may also be a
    path. `escape_scripts=True` rewrites `</script` inside inlined scripts as
    `<\\/script` instead of refusing them. Most code wants `Widget`."""
    return _document(body, title=title, head=head, scripts=scripts, modules=modules,
                     styles=styles, route=route, fetch=fetch, imports=imports,
                     escape_scripts=escape_scripts)[0]


class Widget:
    """An MCP Apps widget: a static page, published as a `ui://` resource and
    shown by the tools registered with `@widget.tool(mcp)`.

        Widget("todos", body="...", scripts=[Path("htmx.min.js")])   # a page around the bridge
        Widget("chart", html=open("chart.html").read())              # your own complete document

    name     the resource is `ui://<name>` unless `uri=` says otherwise
    title    the page title and the resource title
    body / scripts / modules / styles / imports / head / route / fetch / escape_scripts
             build the page with `page()`; https URLs among the assets and the
             import map become `csp.resourceDomains` entries automatically
    html     a complete document used verbatim, such as a bundler's single-file
             build: bring your own bridge, or `bridge=True` to put micromcp's
             first in its head (`route`/`fetch` then apply to it)
    csp      extra `_meta.ui.csp` origins: connectDomains, resourceDomains,
             frameDomains, baseUriDomains
    border   `_meta.ui.prefersBorder`

    `body`, `head`, and `html` are a str or an object with `__html__`
    (FastHTML components, htpy, `markupsafe.Markup`), rendered once, here;
    `body` and `html` may also be a path to a file. A page that loads a
    relative URL is refused: a widget has no origin to load it from.

    Widgets are static: hosts fetch them under their own identity and cache
    them per connector, so a changed page needs a new connector to show up.
    Per-user data belongs in tool results and fragments.
    """

    def __init__(self, name: str, *, body: HTMLLike | None = None,
                 html: HTMLLike | None = None,
                 title: str = "", scripts=(), modules=(), styles=(),
                 head: HTMLLike = "",
                 imports: dict | None = None, route: str | None = None, fetch: str = "hooks",
                 csp: dict | None = None,
                 border: bool | None = None, uri: str | None = None,
                 bridge: bool = False, escape_scripts: bool = False):
        if not isinstance(name, str) or not _WIDGET_NAME_RE.fullmatch(name):
            raise ValueError(f"widget name {name!r}: letters, digits, '.', '_' and '-' only")
        self.name, self.title = name, title or name
        self.uri = f"ui://{name}" if uri is None else uri
        if not isinstance(self.uri, str) or not self.uri.startswith("ui://") or "{" in self.uri:
            raise ValueError(f"widget uri {self.uri!r} must be a plain ui:// URI")
        if (body is None) == (html is None):
            raise TypeError("Widget: give body= (a page built around the bridge) or html= "
                            "(a complete document of your own), not both")
        if html is not None:
            if scripts or modules or styles or head or imports or escape_scripts:
                raise TypeError("Widget(html=...) is used verbatim; scripts/modules/styles/"
                                "head/imports/escape_scripts apply only to body=")
            if not bridge and (route or fetch != "hooks"):
                raise TypeError("route/fetch are settings of micromcp's bridge: with html=, "
                                "they need bridge=True")
            self.html, origins = _markup(_read(html, "html"), "html"), set()
            if bridge:
                self.html = _with_bridge(self.html, route, fetch)
        else:
            if bridge:
                raise TypeError("a body= page always carries the bridge; bridge= is for html=")
            self.html, origins = _document(body, title=self.title, head=head, scripts=scripts,
                                           modules=modules, styles=styles, route=route,
                                           fetch=fetch, imports=imports,
                                           escape_scripts=escape_scripts)
        what = f"widget {name!r}"
        _check_urls(self.html, what)
        if BRIDGE_JS in self.html and \
                "ui/notifications/initialized" in self.html.replace(BRIDGE_JS, "", 1):
            log.warning("%s: the page carries another MCP Apps client as well as micromcp's "
                        "bridge (it names ui/notifications/initialized): that is two "
                        "handshakes with the host. Use one: drop bridge=True, or the other "
                        "client", what)
        _leftovers(what, [html, body, *_items(modules)],
                   [html, body, *_items(scripts), *_items(modules), *_items(styles)])
        domains = {}
        for k, v in (csp or {}).items():
            if k not in _CSP_KEYS:
                raise ValueError(f"csp key {k!r}; expected one of {', '.join(_CSP_KEYS)}")
            if isinstance(v, str) or not all(isinstance(d, str) and _ORIGIN_RE.fullmatch(d)
                                             for d in v):
                raise ValueError(f"csp {k} must be a list of origins such as "
                                 f"https://api.example.com or https://*.example.com")
            domains[k] = list(v)
        if origins:
            domains["resourceDomains"] = sorted(set(domains.get("resourceDomains", ())) | origins)
        ui = {}
        if domains:
            ui["csp"] = domains
        if border is not None:
            ui["prefersBorder"] = bool(border)
        self.meta = {"ui": ui} if ui else None

        self._servers = weakref.WeakSet()      # the MCP registries this widget is published on

    def __repr__(self):
        return f"Widget({self.name!r}, uri={self.uri!r})"

    @property
    def tool_meta(self) -> dict:
        """The `_meta` naming this widget, for a tool registered by hand:
        the spec's `ui.resourceUri`, plus the flat legacy `ui/resourceUri`
        that hosts of the reference servers read."""
        return {"ui": {"resourceUri": self.uri}, "ui/resourceUri": self.uri}

    def _check(self, mcp) -> bool:
        """True when this widget is already published on `mcp`; refuses a
        different resource at the same URI."""
        if mcp in self._servers:
            return True
        if self.uri in mcp.resources:
            raise ValueError(f"resource {self.uri!r} is already registered; give this widget "
                             f"another name or uri=")
        return False

    def register(self, mcp) -> str:
        """Publish this widget's `ui://` resource on `mcp` (once) and return
        its URI. `tool()` does this for you."""
        if self._check(mcp):
            return self.uri
        doc = self.html

        def widget() -> str:
            return doc
        widget.__name__ = re.sub(r"\W", "_", self.name) + "_widget"
        widget.__doc__ = f"MCP Apps widget {self.title!r}."
        mcp.resource(self.uri, title=self.title, meta=self.meta)(widget)
        self._servers.add(mcp)
        return self.uri

    def tool(self, mcp, fn=None, *, meta: dict | None = None, **options):
        """Register a tool on `mcp` that shows this widget: `mcp.tool(**options)`
        with the widget named in its `_meta`, and the widget's resource
        published on first use.

            @board.tool(mcp, read_only=True)
            def show_todos() -> str: ...
        """
        merged = dict(meta or {})
        ui = merged.get("ui", {})
        if not isinstance(ui, dict):
            raise ValueError("meta 'ui' must be a dict to add a widget")
        if ui.get("resourceUri", self.uri) != self.uri \
                or merged.get("ui/resourceUri", self.uri) != self.uri:
            raise ValueError(f"meta names another resource than this widget's {self.uri!r}")
        merged["ui"] = {**ui, "resourceUri": self.uri}
        merged["ui/resourceUri"] = self.uri

        def wrap(f):
            published = self._check(mcp)       # refuse a conflict before registering anything
            mcp.tool(f, meta=merged, **options)
            if not published:
                self.register(mcp)
            return f
        return wrap(fn) if fn is not None else wrap


def _context(context) -> dict:
    """Normalize model context to {"text": str, "data"?: dict}, JSON-clean and bounded."""
    if isinstance(context, str):
        context = {"text": context}
    if not isinstance(context, dict) or not set(context) <= {"text", "data"} \
            or not isinstance(context.get("text", ""), str) \
            or not isinstance(context.get("data", {}), dict):
        raise ValueError("context must be a str or {'text': str, 'data': dict}")
    try:
        encoded = json.dumps(context, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"context is not JSON-serializable: {exc}") from None
    if len(encoded) > CONTEXT_LIMIT:
        raise ValueError(f"context is {len(encoded)} bytes; keep it under {CONTEXT_LIMIT}")
    return json.loads(encoded)


def fragment(html: HTMLLike, *, status: int = 200,
             content_type: str = "text/html; charset=utf-8", context=None) -> Result:
    """A tool result carrying an HTML fragment for the widget to swap in. A
    status of 400 or more marks the result `isError`; the bridge then reports
    the text instead of swapping it. The HTML goes to the widget only when the
    tool is app-only (`visibility="app"`).

    `html` is a str, or an object whose class defines `__html__` (the protocol
    Jinja and markupsafe use): FastHTML components, htpy elements,
    `markupsafe.Markup`. Those escape text and attribute values; a str is used
    as is, so escape everything you interpolate into one. Escaping does not
    protect a value that is parsed again: build `tool:` URLs with `tool_url()`
    and `hx_vals` with `json.dumps()`, never by concatenating strings.

    `context` (a str, or `{"text": str, "data": dict}`) is what the model
    should know about the view after this action — "2 of 5 rows selected". The
    bridge forwards it as `ui/update-model-context`, so it reaches the model's
    next turn without starting one. Each update replaces the widget's previous
    one, so describe the whole current state, not just the change. `data` is
    sent both as `structuredContent` and as a labeled JSON text block, since
    Claude shows the model only text; keep what users wrote in `data`, and
    keep the sentence yours."""
    html = _markup(html, "fragment: html")
    if not isinstance(status, int) or not 100 <= status <= 599:
        raise ValueError("fragment: status must be an HTTP status code")
    meta = {FRAGMENT_META: {"status": status, "contentType": content_type}}
    if context is not None:
        meta[CONTEXT_META] = _context(context)
    return result([{"type": "text", "text": html}], is_error=status >= 400, meta=meta)
