# micromcp.apps

MCP Apps widgets for [micromcp](../README.md) servers, in the experimental
`micromcp.apps` subpackage. A widget is a static
HTML page the host renders in a sandboxed iframe next to the conversation;
the host proxies its tool calls to your server. This subpackage builds those
pages and the server side they talk to:

- `Widget`: a page around the bridge, published as a `ui://` resource by the
  tools that show it
- `fragment()`: HTML for the widget to swap in, from an app-only tool
- hypermedia over tool calls: htmx 4, fixi, Datastar, and existing Django views
- model context: telling the model what the user sees, from the server or the page
- `Channel`: WebSocket-style messaging between open widgets and server code
- a development MCP Apps host (`DEVHOST_HTML`)

It ships in the micromcp wheel but stays apart from the core: `import
micromcp` does not load it, its names are not in `micromcp.__all__`, it uses
only micromcp's public API (`MCP.tool(meta=, visibility=)`, `MCP.resource`,
`result`; a test enforces this), and it is not part of the single-file
bundle. Like the core it needs nothing outside the standard library; Django
is optional. It is **experimental**: it follows the MCP Apps extension
(2026-01-26) and the behavior of the hosts that implement it, and may change
in a minor release.

## Widgets

Declare a widget once and register the tools that show it through it:

```python
from pathlib import Path
from micromcp import MCP
from micromcp.apps import Widget, fragment

mcp = MCP("todos")
board = Widget("todos", title="Todos", scripts=[Path("htmx.min.js")], body="""
    <div id="app" hx-post="tool:todo_list" hx-trigger="mcp:ready" hx-target="#app"></div>""")

@board.tool(mcp, read_only=True)          # the model calls this; the host shows the widget
def show_todos() -> str:
    return f"{todos.open_count()} open todos"

@mcp.tool(visibility="app")               # the widget calls this; the model never sees it
def todo_list():
    return fragment(render(todos))        # HTML for the widget: a str or components
```

`Widget(name, ...)` builds the page around `BRIDGE_JS`, or takes `html=` for
a complete document of your own. `board.tool(mcp, **options)` is
`mcp.tool(**options)` with the widget named in the tool's `_meta`
(`ui.resourceUri`, plus the legacy `ui/resourceUri` key); the first such
tool publishes the widget as `ui://<name>` with the MCP App MIME type. For a
tool you register yourself, `board.register(mcp)` publishes the resource and
`board.tool_meta` is the pointer to pass as `meta=`.

Assets in `scripts=`, `modules=`, and `styles=` are source text,
`pathlib.Path`s (inlined), or https URLs (loaded; their origins are declared
in the resource's `_meta.ui.csp.resourceDomains` for you). Scripts and
modules follow the body, so they can reach its elements. A string that looks
like a URL but is not a clean one (a stray space or quote, credentials, a
protocol-relative `//host/...`, or plain `http://`) is refused rather than
inlined as source. Every widget needs either `body=` or `html=`; assets
alone are refused.

`csp=` declares the origins micromcp cannot infer, as a dict of the MCP Apps
buckets, each a list of plain origins:

```python
chart = Widget("chart", body='<div id="root"></div>', csp={
    "connectDomains":  ["https://api.example.com", "wss://live.example.com:8443"],
    "resourceDomains": ["https://cdn.jsdelivr.net", "https://*.example.com"],
})
```

The keys are `connectDomains`, `resourceDomains`, `frameDomains`, and
`baseUriDomains`; any other key is refused, and an entry carrying a path, a
bare host, or credentials is refused. A complete `html=` page needs this:
micromcp declares origins for the https URLs you hand to `scripts=`,
`modules=`, `styles=`, and `imports=`, but it does not go looking through a
page you built yourself, so anything that page loads from a CDN must be
declared here or the host blocks it. Claude enforces the
declaration: on 2026-09-14 htmx loaded from jsdelivr in a `Widget`, and the
same page without the declaration was blocked (`script-src-elem`), exactly
as in the dev host. `imports=` writes an import map, so modules can
`import ... from "three"` (its origins are declared the same way; see
`examples/mcp_app_3d.py`, a three.js scene the model builds with tools and the
user selects in). `csp=` adds origins, `border=` sets `prefersBorder`,
`route=` and `fetch=` are covered below.

Widgets must be static: hosts fetch a `ui://` resource under their own
identity and cache it per connector, so per-user data belongs in tool results
and fragments (see the core README's UI apps section).

## Writing the HTML

`fragment()`, `page()`, and `Widget(body=, head=, html=)` take a str or any
object that renders itself through `__html__`, the protocol Jinja and
markupsafe use: FastHTML's components (`fastcore.xml`, which has no
dependencies of its own, or `fasthtml.common`), htpy elements, and
`markupsafe.Markup`. Those libraries escape text and attribute values, so
user-written data can go into an element's text or an attribute's value:

```python
from fastcore.xml import Button, Div, Li, Span, Ul
from micromcp.apps import tool_url

def render(todos):
    return Div(Ul(*[Li(Button("done", hx_post=tool_url("todo_toggle", id=t.id), hx_target="#app"),
                       Span(t.text))                 # t.text is escaped
                    for t in todos]))
```

Escaping does not protect a value that is parsed again after the browser
decodes it. Build `tool:` URLs with `tool_url(name, **args)`, which
percent-encodes the arguments: in `f"tool:item_remove?name={name}"`, a name
such as `milk&role=admin` adds an argument. Build `hx_vals` with
`json.dumps({...})`, never by concatenating a JSON string (`fasthtml.common`
components also take the dict itself; `fastcore.xml` renders a dict as
`key:value` text, which is not JSON). And never take
attribute names or tag names from user data: components interpolate names
unescaped, and a forged `data-mcp-say` posts text into the chat as the user.

Pass one object, so wrap siblings in an element. It is rendered once, when
the page or fragment is built, and its output is trusted as markup, exactly
as Jinja trusts it. A str is used as is: escape what you interpolate into one
yourself (`html.escape`). Scripts and styles belong in `scripts=` and
`styles=`: `fastcore.xml`'s `Script` and `Style` escape their text
(`a && b` arrives as `a &amp;&amp; b`), unlike `fasthtml.common`'s.

## Bundling widgets: Vite, esbuild, Bun

A widget is one HTML document with no origin of its own, so the host cannot
fetch anything it refers to by a relative URL: a bundler's split chunks,
hashed asset files, and `public/` files all fail silently in the frame. Give
micromcp either one script and one stylesheet, or one complete page, with
everything else inlined (or loaded from an https URL). Any bundler can do
that; it needs one output file per kind, assets as data URLs, and names
without hashes.

**One module and one stylesheet; micromcp writes the page.** The page then
carries the bridge, so the app calls `mcp.callTool(...)`,
`mcp.setContext(...)`, and `mcp.channel(...)`. With Vite (checked with 8.3):

```js
// vite.config.js — package.json needs "type": "module", or name this file .mjs
import { defineConfig } from "vite";

export default defineConfig({
  build: {
    target: "es2022",                 // top-level await, if the app uses it
    cssCodeSplit: false,              // one stylesheet, once there is more than one entry
    assetsInlineLimit: 100_000_000,   // images and fonts become data: URLs
    rollupOptions: {
      input: "src/main.ts",           // no index.html: micromcp writes the page
      output: {
        codeSplitting: false,         // one file (before Vite 8: inlineDynamicImports: true)
        entryFileNames: "widget.js",
        assetFileNames: "widget.[ext]",
      },
    },
  },
});
```

`npx vite build` then writes `dist/widget.js` and `dist/widget.css`, which is
what the widget takes:

```python
UI = Path(__file__).parent / "ui" / "dist"
chart = Widget("chart", title="Chart", body='<div id="root"></div>',
               modules=[UI / "widget.js"], styles=[UI / "widget.css"])
```

There is nothing else to configure: the widget's tools, and a server that
shows it in the dev host, are in "The dev host" below.

**With esbuild** (checked with 0.28), the same two files come out of one
command, the stylesheet from the entry's `import "./app.css"`:

```sh
npx esbuild src/main.js --bundle --minify --format=esm --target=es2022 \
    --loader:.png=dataurl --outfile=dist/widget.js
```

Give `--loader:...=dataurl` for every asset extension the app imports; a
`url()` in bundled CSS is inlined the same way.

**With Bun** (checked with 1.4.0), prefer the single-file shape: `bun build
--compile --target=browser --production index.html --outdir dist` writes a
self-contained `dist/index.html`, images imported from scripts included, to
pass as `html=`. `--compile` is what makes it one file; without it Bun emits
split, content-hashed files and a stub `index.html` of relative references,
which micromcp refuses. The one-module shape works too (`bun build
src/main.ts --outdir dist --production`, with the CSS imported from the
entry), but it does not inline everything: an image imported from a script
becomes a separate file, or an empty string under the `dataurl` loader, and
images in CSS are inlined only under the default loader, so keep a Bun
module's images in CSS. Do not add `--asset-naming "[name].[ext]"`: it strips
the content hash that micromcp's leftover-file warning looks for, which is
the one thing that would have told you an asset was left behind. Pass
`--production` in either shape, or React ships its development build (1.1 MB
rather than 215 KB).

**The bridge.** Wait for the handshake before the first call: `await
mcp.ready`. Then `mcp.callTool(name, args?)` resolves to `{content,
structuredContent, isError}`, `mcp.setContext(text, data?)` tells the model
what the user sees, `mcp.say(text)` posts as the user, `mcp.channel(name)`
opens a channel, and `mcp.fetch(url, init?)` routes a request through the
host. A widget built from `body=` always has the bridge; a complete `html=`
page gets it with `bridge=True`.

For TypeScript, `BRIDGE_TYPES` declares `window.mcp`:

```sh
python -c "from micromcp.apps import BRIDGE_TYPES; print(BRIDGE_TYPES)" > src/mcp-bridge.d.ts
```

It needs no import: it declares the global. With Vite, add `"types":
["vite/client"]` to `tsconfig.json` as well, or importing `./logo.png` from
TypeScript is an error.

**One complete page.** A single-file build (Vite with
`vite-plugin-singlefile`, say, which inlines the assets itself) is used
verbatim: `Widget("chart", html=Path("ui/dist/index.html"))`. Such a page
must bring its own MCP Apps client, typically
`@modelcontextprotocol/ext-apps`; otherwise add `bridge=True` to put
micromcp's first in its head, which is also what `route=` and `fetch=` apply
to. Use one bridge per page, never both, and note that `escape_scripts=` and
the other page options belong to `body=` pages: an `html=` page is not
rewritten.

**What micromcp checks when the widget is built.** A page that loads a
relative URL is refused, naming the URL and where it appears, because the
host has no origin to fetch it from. The rest is warnings, on the
`micromcp.apps` logger (Python prints them to stderr unless you configure
logging).

| micromcp sees | Result | What to do |
| --- | --- | --- |
| a relative `src`/`href`/`srcset`/`background`, `url()`, `image-set()`, `@import`, import-map entry, or `srcdoc`/`<template>` content | refused | inline the asset as a data URL, or load it from an https URL |
| a module whose static `import` names a relative path or a bare specifier (`import {clone} from "lodash-es"`) | refused | bundle the dependency in, or map it with `imports={"lodash-es": "https://esm.sh/lodash-es"}` |
| `</script` inside an inlined script | refused | `escape_scripts=True` rewrites it, as bundlers do |
| an inlined script that ends inside `<!--` … `<script` | refused | `escape_scripts=True` closes it with `//-->`; this is what Vue 3's development build needs |
| a complete `html=` page whose script would swallow the rest of it | refused | rebuild it with a bundler that escapes the `<` (esbuild and Vite do) |
| a relative path or asset string inside a classic script | warned | it may be a mere string; if a bundler wrote it, raise the inline limit |
| a dynamic `import("./x.js")` | warned | it may never run; bundle it in if it does |
| an asset file next to what you passed that carries a content hash, sits in an `assets/`-style folder, or is named like a worker | warned | you left part of the build behind: pass it, or inline it |
| no MCP Apps client in an `html=` page | warned | `bridge=True`, or bundle a client |
| two clients (micromcp's bridge and another) | warned | keep one |

Links, hypermedia attributes (`hx-get`, `fx-action`), `<link rel="icon">`,
and `<noscript>` content are not loads. Loads after a valid https `<base
href>` resolve against it. `page()` is checked like `Widget`; `fragment()` is
not, so a fragment's URLs are yours to keep absolute. React's development
build trips the string warning by itself (it mentions `./MyComponent` in an
error message), which is one more reason to build for production.

The leftover scan looks beside an `html=`, `body=`, or `modules=` path, and
skips a folder that holds Python sources (or whose parent does), so a build
written next to your server module is not flagged and not scanned.

Files are read when the `Widget` is built, so restart the server after a
rebuild, and remember that hosts cache a widget per connector.

**Workers, WASM, and URLs built at run time.** `codeSplitting: false` does
not inline a worker created as `new Worker(new URL("./w.js",
import.meta.url))`; import it as `"./w.js?worker&inline"` instead (for
monaco-editor, return such workers from `MonacoEnvironment.getWorker`). An
inlined worker runs from a `blob:` URL, and inlined WASM is fetched from a
`data:` URL or compiled from bytes, so both also need a host whose policy
allows them (`blob:` workers, `data:` fetches, `'wasm-unsafe-eval'`): under
the stricter default policy the MCP Apps spec describes, which the dev host
applies, they are blocked. Images and CSS inlined as data URLs work under
either policy, but **inlined fonts do not**: that policy's `font-src` lists
only the origins you declared, so a `data:` woff2 is refused and the text
falls back to a system face. Fonts have to come from a declared https origin.

A library that builds asset URLs at run time cannot be checked at all.
Leaflet is the cautionary case: it derives `imagePath` from wherever
`leaflet.css` was loaded, so with the stylesheet from a CDN its default
markers simply work. Point them at data URLs instead and Leaflet concatenates
its prefix onto them, so clear it first: `L.Icon.Default.imagePath = ""`,
then `mergeOptions({iconUrl, iconRetinaUrl, shadowUrl})`.

**Vendored CSS that loads fonts.** katex, leaflet, and the like ship a
stylesheet whose `url()`s point at a `fonts/` or `images/` folder beside it.
Passing that file to `styles=` is refused, correctly: passing the file
inlines the CSS but not the fonts beside it, and the widget has no origin to
fetch them from. Load the CSS from its CDN as an https URL instead
(`styles=["https://cdn.jsdelivr.net/npm/katex@0.16/dist/katex.min.css"]`),
which serves its fonts too and whose origin micromcp declares for you.
Bundling the stylesheet does inline the fonts, but as `data:` URLs, which
that policy's `font-src` then refuses, so the CDN is the remedy that
actually renders.

**When the widget is blank.** Nothing rendered and `window.mcp` undefined
means the page has no client: add `bridge=True`. Nothing rendered with the
bridge present usually means the app never got past its first call: `await
mcp.ready` first. Calls that fail with `-32020 Origin not allowed` are the
server's `allowed_origins=`, not the widget. An image missing in the frame
with only a warning in the log is an asset the bundler left as a file: raise
the inline limit and rebuild. And a widget that will not change no matter
what you rebuild is the host's cache, one copy per connector.

## Hypermedia widgets: htmx, fixi, Django views

A widget can be a static page whose HTML the server renders: every click
becomes a `tools/call` the host proxies to this server, and the tool answers
with an HTML fragment that is swapped in. The widget has no app logic and no
network access; all state stays on the server.

```python
@mcp.tool(visibility="app")                     # hidden from the model, callable by the widget
def todo_add(text: str = ""):
    todos.add(text)
    return fragment(render(todos))
```

The bridge completes the MCP Apps handshake and gives `fetch()`-shaped
requests a tool-call transport (`mcp.fetch`): `tool:name?a=1` calls that tool
with the query, form, or JSON body as arguments. It is wired into htmx 4
(`ctx.fetch`) and fixi (`fx:config`), which is `fetch="hooks"`, the default;
`Widget(fetch="global")` also replaces `window.fetch`, for libraries without
a hook (Datastar). With htmx, call
tools with `hx-post`: htmx rewrites a GET URL to its path, dropping `tool:`.
Any other URL goes to the widget's `route=` tool — which is how existing
Django views serve a widget:

```python
from micromcp.apps.django import django_routes

board = Widget("todos", scripts=[Path("htmx.min.js")],
               route=django_routes(mcp, prefixes=["/app/"]),    # registers "django_http"
               body='<div id="app" hx-get="/app/todos/" hx-trigger="mcp:ready" '
                    'hx-target="#app"></div>')
```

`django_routes` runs each request through Django's full handler in process
(URL resolver, middleware, views, templates), follows same-host redirects,
forwards `HX-*`/`FX-*`/`Datastar-*` headers so `django-htmx` works, and serves
only paths under `prefixes` (after decoding and normalization). The request
carries no cookies: the MCP principal is `request.mcp_principal`, and CSRF is
off because nothing ambient authenticates the request. `fragment()` results
from app-only tools reach only the widget, never the model's context.

Verified 2026-09-14 in the Claude chat with nine self-testing widgets
(`examples/toolkit_lab.py`): fixi, htmx 4, htmx 4 + Django views, htmx 4 +
Alpine's CSP build (state survives `innerMorph`), Datastar, and hx-live all
work. Claude's widget policy allows `eval` and inline-script injection; the
MCP Apps spec's default does not, and under it stock hx-live and stock
Datastar fail while everything above in eval-free form (Datastar's CSP mode,
hx-live with an injection extension) passes. htmx's `hx-csp` extension does
not work over this transport: it strips every element that arrives in a
fragment, because a synthesized `Response` has no URL to verify.

## Telling the model what the user sees

A widget is opaque to the model, but it can report to it:

```python
return fragment(render(todos), context={
    "text": f"Todo widget, current state: {n_open} open, {n_done} done.",
    "data": {"items": [...]}})
```

The bridge forwards `context` as `ui/update-model-context`; the model sees it
on its next turn, and no turn is started. In the browser, `mcp.setContext(text,
data)` does the same for client-side state (debounced), and `mcp.say(text)` or
`<button data-mcp-say="...">` posts a message into the chat as the user,
which does start a turn. From Django, `set_mcp_context(response, text, data)`
(in `micromcp.apps.django`) on any response in a redirect chain has the same
effect.

What Claude did with them on 2026-09-14: each update **replaces** the
widget's previous one (as the spec says), while separate widgets keep separate
contexts — so send a snapshot of the view, not a change; the model saw only
the **text** of an update, never `structuredContent`, so the bridge also sends
`data` as a labeled JSON text block; and `ui/message` needed `content` as an
array of blocks (the spec shows one block), so the bridge tries that first.
None of this happens in the Claude Code desktop tab, which does not render MCP
Apps. Treat context as data: anything users wrote reaches the model.

The bridge acts on attributes anywhere in the page, including HTML swapped in
later: a click on any `data-mcp-say` element posts its text as the user, and
htmx and fixi issue tool calls from `hx-*`/`data-hx-*`/`fx-*` attributes. So
never swap in user-authored HTML, even sanitized: common sanitizers keep
`data-*` attributes by default. Escape user text into your templates instead.

## Channels: pushing to open widgets

A widget cannot open a socket, and the host delivers a tool result only to the
widget that call opened, so changes made elsewhere — by the model, by another
user — need a way in. A channel is that way in, with the WebSocket API on the
widget's side:

```python
from micromcp.apps import Channel

scene = Channel(mcp, "scene")

@scene.on_connect
def joined(conn):                        # conn.params: the query the widget connected with
    conn.send_json(snapshot())

@scene.on_message
def received(conn, text): ...            # text frames, in order

@mcp.tool
def scene_add(...) -> str:
    ...
    scene.broadcast_json(snapshot())     # from any tool, sync or async, any thread
```

```js
const ws = mcp.channel("scene");         // WebSocket-compatible: onmessage, send, close, readyState
ws.onmessage = e => render(JSON.parse(e.data));

new SomeClient("mcp:scene?partial=1", {webSocket: mcp.WebSocket});   // for libraries that take one
```

Underneath are four app-only tools shared by every channel of a server,
registered with its first channel: `channel_open`, `channel_send`,
`channel_recv`, and `channel_close`. `channel_recv` is a long poll: it
returns as soon as a frame is queued, or after `wait` seconds (20 by default),
so an idle widget makes one request per 20 s and a change arrives at once. A
connection is bound to the principal that opened it (`guards=` run on open),
is dropped after `idle` seconds without a request (90), and is closed if it
falls `max_queue` frames (1000) or `max_bytes` (16 MiB) behind. A channel
holds at most `max_connections` (1000); further opens are refused until some
close or time out. Callbacks may be sync (run on a worker thread) or async,
and `send`/`broadcast` are safe from any thread. A callback that raises is
logged and its connection closed: the widget sees the socket close, never the
exception text.

Channels live in one process: with several workers or replicas, a widget's
requests must reach the process holding its connection, and a broadcast
reaches only that process's connections. Under WSGI each waiting
`channel_recv` holds a worker thread; prefer the ASGI server for channels.

Mind the host's widget cache when you change a widget. Claude keeps serving a
connector's widget as first fetched, so after a redeploy an old widget still
calls the tools it knew: remove one and it fails with "no such tool" (seen on
2026-09-14 when the 3D example moved from polling to a channel). Keep app-only
tools an older widget uses working, and reach the new widget through a new
connector (a new path or host).

Verified 2026-09-14 in the Claude chat: the host held `channel_recv`-style
requests open for the full 20 s, and a model edit reached an open widget as
soon as it was made. That test used atomdoc, a server-authoritative document
library whose Python transport interface maps onto a channel and whose
TypeScript client takes `mcp.WebSocket` unchanged; the same pattern drives
`examples/mcp_app_3d.py`.

## The dev host

`DEVHOST_HTML` is a stand-in MCP Apps host for development: it reads a tool's
`ui://` widget, renders it in a sandboxed iframe under the policy a host
derives from the widget's `_meta.ui.csp` (the spec's default plus the declared
origins), completes the handshake, proxies the widget's `tools/call` for tools
whose visibility includes `app`, and shows what the model would receive. It is
a page of bytes, so serve it beside your MCP endpoint yourself:

```python
# server.py: the widget, its tools, and the dev host on one stdlib server
import secrets
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

from micromcp import MCP, Server
from micromcp.apps import DEVHOST_HTML, Widget

PORT = 8770
ORIGIN = f"http://127.0.0.1:{PORT}"

mcp = MCP("dice", "1.0.0")
dice = Widget("dice", title="Dice", body='<div id="root">rolling...</div>', scripts=["""
    (async () => {
      await mcp.ready;                                  // the handshake finishes first
      const r = await mcp.callTool("roll", {sides: 20});
      document.getElementById("root").textContent = r.structuredContent.rolled;
    })();
"""])

@dice.tool(mcp, read_only=True)          # the model calls this; the host shows the widget
def show_dice() -> str:
    return "Dice open."

@mcp.tool(visibility="app")              # the widget calls this; the model never sees it
def roll(sides: int = 6) -> dict:
    return {"rolled": secrets.randbelow(sides) + 1}

endpoint = Server(mcp, path="/mcp", allowed_origins={ORIGIN})

def app(environ, start_response):
    if environ.get("PATH_INFO") == "/devhost":
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(DEVHOST_HTML)))])
        return [DEVHOST_HTML]                           # bytes, not str
    return endpoint(environ, start_response)

class Threaded(ThreadingMixIn, WSGIServer):             # the host calls concurrently
    daemon_threads = True

make_server("127.0.0.1", PORT, app, server_class=Threaded).serve_forever()
```

Then open `http://127.0.0.1:8770/devhost?mcp=/mcp`. Two details cost more
time than anything else in this page if you miss them: the dev host is a
browser client, so its origin must be in `allowed_origins=` or every call
fails with `-32020 Origin not allowed` and an empty tool list; and
`DEVHOST_HTML` is `bytes`, ready to write to the socket.

Its policy is a host's, not a browser's default: inline scripts run (a
widget's own scripts are inline), `eval` does not, and `font-src` and
`connect-src` list only declared origins. So something that works here can
still fail under a stricter host, and `?csp=eval` shows what Claude's more
permissive policy allows.

The dev host has no controls. It loads the first tool that shows a widget and
is visible to the model; `?tool=<name>` picks another, `?args=<json>` passes
that tool's arguments, and `?csp=eval` adds `unsafe-eval` to the policy.
`examples/mcp_app_hypermedia.py` serves it over ASGI beside its demos (the
todo widget with tools and with Django views, the toolkit lab, and the
context counter).

## Examples

- `examples/mcp_app_hypermedia.py`: the todo widget rendered with FastHTML
  components by app-only tools, and by Django views, plus the toolkit lab and
  the dev host
- `examples/toolkit_lab.py`: nine self-testing toolkit variants and a
  model-context counter
- `examples/mcp_app_3d.py`: a shared three.js scene over a channel
- `examples/vendor/`: the pinned toolkit builds the examples inline

## Tests

`tests/test_apps.py`, run by the repository's `pytest` with the core suites
(it skips itself against the single-file bundle). The bridge's JavaScript is
syntax-checked with `node --check` when node is installed and exercised end
to end in the dev host.
