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
Django views serve a widget (`micromcp_apps.django.django_routes`). In the
page, `mcp.setContext(text, data)` and `mcp.say(text)` talk to the model;
from the server, `fragment(..., context=...)` does.
"""

from __future__ import annotations

import html as _html
import json
import os
import pathlib
import re
import weakref
from urllib.parse import urlsplit

from micromcp import Result, result

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")   # the tool-name grammar micromcp enforces

FRAGMENT_META = "micromcp/http"
CONTEXT_META = "micromcp/context"
CONTEXT_LIMIT = 16_000     # bytes of JSON: context rides along on the model's later turns

BRIDGE_JS = (pathlib.Path(__file__).with_name("bridge.js")).read_text(encoding="utf-8")


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


def _script(source, module=False):
    if "</script" in source.lower():
        raise ValueError("an inlined script contains '</script'; it would end the tag early")
    kind = ' type="module"' if module else ""
    return f"<script{kind}>{source}</script>"


def _document(body, *, title, head, scripts, modules, styles, route, fetch, imports=None):
    """The widget page and the https origins it loads from."""
    if not isinstance(body, str):
        raise TypeError("body must be a str")
    if fetch not in ("hooks", "global"):
        raise ValueError("fetch must be 'hooks' (htmx, fixi) or 'global' (replace window.fetch)")
    if route is not None and (not isinstance(route, str) or not _NAME_RE.match(route)):
        raise ValueError(f"route must be a tool name, got {route!r}")
    origins = set()
    parts = ['<meta charset="utf-8">', f"<title>{_html.escape(title)}</title>"]
    if route:
        parts.append(f'<meta name="mcp-route" content="{_html.escape(route)}">')
    if fetch == "global":
        parts.append('<meta name="mcp-fetch" content="global">')
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
                tail.append(_script(text, module))
    return ("<!doctype html><html><head>" + "".join(parts) + "</head><body>" + body
            + "".join(tail) + "</body></html>"), origins


def page(body: str, *, title: str = "", head: str = "", scripts=(), modules=(), styles=(),
         imports: dict | None = None, route: str | None = None, fetch: str = "hooks") -> str:
    """A complete widget document around `body`: `styles`, `head`, the
    `imports` map and `BRIDGE_JS` in the head, then `body`, then `scripts` and
    `modules` in order, so a script can reach the page's elements. Each
    asset is source text, a `pathlib.Path` (inlined), or an https URL (loaded;
    the host must allow its origin — `Widget` declares that for you).
    `imports` maps module specifiers to https URLs (`{"three": ".../three.module.js"}`)
    so modules can `import ... from "three"`. `route` names the tool that
    serves non-`tool:` URLs; `fetch="global"` lets libraries without a hook
    (Datastar) reach tools through `window.fetch`. Most code wants `Widget`."""
    return _document(body, title=title, head=head, scripts=scripts, modules=modules,
                     styles=styles, route=route, fetch=fetch, imports=imports)[0]


class Widget:
    """An MCP Apps widget: a static page, published as a `ui://` resource and
    shown by the tools registered with `@widget.tool(mcp)`.

        Widget("todos", body="...", scripts=[Path("htmx.min.js")])   # a page around the bridge
        Widget("chart", html=open("chart.html").read())              # your own complete document

    name     the resource is `ui://<name>` unless `uri=` says otherwise
    title    the page title and the resource title
    body / scripts / modules / styles / imports / head / route / fetch
             build the page with `page()`; https URLs among the assets and the
             import map become `csp.resourceDomains` entries automatically
    html     a complete document used verbatim (bring your own bridge)
    csp      extra `_meta.ui.csp` origins: connectDomains, resourceDomains,
             frameDomains, baseUriDomains
    border   `_meta.ui.prefersBorder`

    Widgets are static: hosts fetch them under their own identity and cache
    them per connector, so a changed page needs a new connector to show up.
    Per-user data belongs in tool results and fragments.
    """

    def __init__(self, name: str, *, body: str | None = None, html: str | None = None,
                 title: str = "", scripts=(), modules=(), styles=(), head: str = "",
                 imports: dict | None = None, route: str | None = None, fetch: str = "hooks",
                 csp: dict | None = None,
                 border: bool | None = None, uri: str | None = None):
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
            if scripts or modules or styles or head or imports or route or fetch != "hooks":
                raise TypeError("Widget(html=...) is used verbatim; scripts/modules/styles/"
                                "head/route/fetch apply only to body=")
            if not isinstance(html, str):
                raise TypeError("html must be a str")
            self.html, origins = html, set()
        else:
            self.html, origins = _document(body, title=self.title, head=head, scripts=scripts,
                                           modules=modules, styles=styles, route=route,
                                           fetch=fetch, imports=imports)
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


def fragment(html: str, *, status: int = 200,
             content_type: str = "text/html; charset=utf-8", context=None) -> Result:
    """A tool result carrying an HTML fragment for the widget to swap in. A
    status of 400 or more marks the result `isError`; the bridge then reports
    the text instead of swapping it. The HTML goes to the widget only when the
    tool is app-only (`visibility="app"`); escape everything you interpolate.

    `context` (a str, or `{"text": str, "data": dict}`) is what the model
    should know about the view after this action — "2 of 5 rows selected". The
    bridge forwards it as `ui/update-model-context`, so it reaches the model's
    next turn without starting one. Each update replaces the widget's previous
    one, so describe the whole current state, not just the change. `data` is
    sent both as `structuredContent` and as a labeled JSON text block, since
    Claude shows the model only text; keep what users wrote in `data`, and
    keep the sentence yours."""
    if not isinstance(html, str):
        raise TypeError("fragment: html must be a str")
    if not isinstance(status, int) or not 100 <= status <= 599:
        raise ValueError("fragment: status must be an HTTP status code")
    meta = {FRAGMENT_META: {"status": status, "contentType": content_type}}
    if context is not None:
        meta[CONTEXT_META] = _context(context)
    return result([{"type": "text", "text": html}], is_error=status >= 400, meta=meta)
