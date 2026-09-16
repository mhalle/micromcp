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

@mcp.tool(visibility="app")               # hidden from the model; the widget still calls it
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

`scripts=` are classic scripts and `modules=` are `type="module"` ones, so a
package's `.esm.js` build belongs in `modules=` and its UMD or global build in
`scripts=`. Vendor a library with `npm pack htmx.org@4` (it unpacks to
`package/dist/...`), and inline your own images yourself, since there is no
bundler to do it:

```python
LOGO = "data:image/png;base64," + base64.b64encode(Path("logo.png").read_bytes()).decode()
```

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
bare host, or credentials is refused — declare a tile server or an API by its
origin (`https://tile.openstreetmap.org`), never by the URL template your
code builds from it. A complete `html=` page needs this:
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
`route=` and `fetch=` are covered below. A tool with no `visibility=` is
callable by both the model and the widget, which is the spec's default;
`visibility="app"` only hides it from the model, and only because the host
does so. Any other MCP client still lists and calls it, so guard what it
does, exactly as you would an ordinary tool. A widget's resource is
`ui://<name>`
unless `uri=` says otherwise, and the name takes letters, digits, `.`, `_`,
and `-`; two widgets cannot share a URI on one server.

Widgets must be static: hosts fetch a `ui://` resource under their own
identity and cache it per connector, so per-user data belongs in tool results
and fragments (see the core README's UI apps section).

## Writing the HTML

`fragment()` answers an app-only tool with HTML for the widget to swap in;
`fragment(html, status=, content_type=, context=)` sets the synthesized
response's status and media type, and `context=` tells the model what changed
in the same result. `Widget(body=)` and `Widget(html=)` also take a
`pathlib.Path`, which is read when the widget is built.

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
`mcp.setContext(...)`, and `mcp.channel(...)`. Install micromcp as the server
needs it (`pip install micromcp`, or from git until the first release), and
the bundler as the app needs it — `npm init -y && npm i -D vite` — then, with
Vite (checked with 8.3):

```js
// vite.config.js — package.json needs "type": "module", or name this file .mjs
import { defineConfig } from "vite";

export default defineConfig({
  build: {
    target: "es2022",                 // top-level await, if the app uses it
    cssCodeSplit: false,              // one stylesheet, once there is more than one entry
    assetsInlineLimit: 100_000_000,   // images become data: URLs — but see fonts, below
    rollupOptions: {
      input: "src/main.ts",           // no index.html: micromcp writes the page
      output: {
        codeSplitting: false,         // one file (Vite 8+ also spells it rolldownOptions)
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
shows it in the dev host, are in "The dev host" below, and
`examples/mcp_app_bundled.py` is exactly this recipe as a file you can run.

Two things about that config are worth knowing. `assetFileNames:
"widget.[ext]"` gives stable names to pass, but it also strips the content
hash micromcp's leftover-file warning looks for, so check `dist/` yourself:
anything besides `widget.js` and `widget.css` was left behind. And the inline
limit inlines fonts as well as images, which a host's `font-src` then refuses
— keep web fonts on a declared https origin, and micromcp warns if one ends
up inlined. A misspelled `codeSplitting` is ignored silently and the chunks
come back, so if a refusal names `./assets/...`, check that key first.

For one complete page instead, `vite-plugin-singlefile` inlines the JS and
CSS; the assets still need the limit, and `index.html` stays the entry:

```js
// vite.config.js, for the single-file shape
import { defineConfig } from "vite";
import { viteSingleFile } from "vite-plugin-singlefile";

export default defineConfig({
  plugins: [viteSingleFile()],
  build: { target: "es2022", cssCodeSplit: false, assetsInlineLimit: 100_000_000 },
});
```

**With esbuild** (checked with 0.28), the same two files come out of one
command, the stylesheet from the entry's `import "./app.css"`:

```sh
npx esbuild src/main.js --bundle --minify --format=esm --target=es2022 \
    --loader:.png=dataurl --outfile=dist/widget.js
```

Give `--loader:...=dataurl` for every asset extension the app imports; a
`url()` in bundled CSS is inlined the same way. esbuild leaves `</script` raw
in its output, as any JS bundler does, so pass `escape_scripts=True` with it.

**With Bun** (checked with 1.4.0), prefer the single-file shape: `bun build
--compile --target=browser --production index.html --outdir dist` writes a
self-contained `dist/index.html`, images imported from scripts included, to
pass as `html=` with `bridge=True`, since a Bun build brings no MCP Apps
client. `--compile` is what makes it one file; without it Bun emits split,
content-hashed files and a stub `index.html` of relative references, which
micromcp refuses. `--target=browser` is not optional either: without it
`--compile` means Bun's standalone-executable build, and it fails with
`cannot use --compile with --outdir`.

The one-module shape works too. `bun build src/main.tsx --outdir dist
--production`, with the CSS imported from the entry, writes `dist/main.js`
and `dist/main.css` — named after the entry, not "widget" — and they need
`escape_scripts=True`, because Bun leaves `</script` raw in a `.js` output
and React's production build contains one:

```python
chart = Widget("chart", body='<div id="root"></div>', escape_scripts=True,
               modules=[UI / "main.js"], styles=[UI / "main.css"])
```

That shape does not inline everything: an image imported from a script stays
a separate file, so keep a Bun module's images in CSS, where the default
loader inlines them. (Bun's CLI has no `--loader` flag — esbuild's
`--loader:.png=dataurl` is accepted and silently ignored — and setting
`loader: {".png": "dataurl"}` through `Bun.build()` makes such an import an
empty string and rewrites a CSS `url()` to an absolute filesystem path, so
leave the loader alone.) Do not add `--asset-naming "[name].[ext]"` either:
it strips the content hash that micromcp's leftover-file warning looks for,
which is the clearest signal that an asset was left behind. Pass
`--production` in either shape, or React ships its development build (1.1 MB
rather than 215 KB).

**The bridge.** Wait for the handshake before the first call: `await
mcp.ready`. The bridge also dispatches an `mcp:ready` DOM event when the
handshake completes, which is what `hx-trigger="mcp:ready"` fires on in the
hypermedia examples. Then `mcp.callTool(name, args?)` resolves to `{content,
structuredContent, isError}` — a tool that ran and failed resolves with
`isError: true`, while a call that could not be made at all (no such tool,
arguments the schema refuses, a timeout) **rejects**, so catch it or a stray
argument becomes an unhandled rejection, `mcp.setContext(text, data?)` tells the model
what the user sees, `mcp.say(text)` posts as the user, `mcp.channel(name)`
opens a channel, and `mcp.fetch(url, init?)` routes a request through the
host. A widget built from `body=` always has the bridge; a complete `html=`
page gets it with `bridge=True`.

For TypeScript, `BRIDGE_TYPES` declares `window.mcp`:

```sh
python -c "from micromcp.apps import BRIDGE_TYPES; print(BRIDGE_TYPES)" > src/mcp-bridge.d.ts
```

It needs no import: it declares the global. Neither Vite nor Bun typechecks
while bundling, so run `npx tsc --noEmit` yourself; a widget with type errors
builds and ships. With Vite, add `"types": ["vite/client"]` to
`tsconfig.json`, or importing `./logo.png` is an error; with Bun, add `"DOM"`
to `lib` and declare the asset modules yourself. A tool result's
`structuredContent` is optional, as the types say, so read it as
`r.structuredContent?.rolled` or narrow it once.

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
host has no origin to fetch it from. The first five rows below are refusals,
raised as a `ValueError` when the `Widget` is built; the rest are warnings on
the `micromcp.apps` logger (Python prints them to stderr unless you configure
logging). Arguments are validated too, and those refusals say what they want:
a name's character set, an https-only URL, the `csp=` buckets, `fetch=`, the
size of a context update, and a `ui://` already taken.

| micromcp sees | Result | What to do |
| --- | --- | --- |
| a relative `src`/`href`/`srcset`/`background`, `url()`, `image-set()`, `@import`, import-map entry, or `srcdoc`/`<template>` content | refused | inline the asset as a data URL, or load it from an https URL |
| a module whose static `import` names a relative path or a bare specifier (`import {clone} from "lodash-es"`) | refused | bundle the dependency in, or map it with `imports={"lodash-es": "https://esm.sh/lodash-es"}` |
| `</script` inside an inlined script | refused | `escape_scripts=True` rewrites it, as bundlers do |
| an inlined script that ends inside `<!--` … `<script` (the tag name followed by a space, `/`, or `>`) | refused | `escape_scripts=True` closes it with `//-->`; this is what Vue 3's development build needs |
| a complete `html=` page whose script would swallow the rest of it | refused | rebuild it with a bundler that escapes the `<` (esbuild and Vite do) |
| a relative or root-absolute path (`./x.png`, `/x.png`) inside a classic script | warned | it may be a mere string; if a bundler wrote it, raise the inline limit |
| a script that fetches a chunk at run time — `import("./x.js")` — | warned | harmless if it never runs; otherwise bundle the chunk in, or map it with `imports=` |
| an asset file next to what you passed that carries a content hash, sits in an `assets/`-style folder, or is named like a worker | warned | you left part of the build behind: pass it, or inline it |
| a font inlined as a `data:` URL | warned | a host's `font-src` refuses it; serve the font from a declared https origin |
| no MCP Apps client micromcp recognises in an `html=` page | warned | pass `bridge=True`; if the page bundles a client micromcp cannot see, ignore it |
| micromcp's own bridge already in the page, plus `bridge=True` | refused | leave `bridge=True` off |
| micromcp's bridge beside another client it recognises | warned | keep one |

Links, hypermedia attributes (`hx-get`, `fx-action`), `<link rel="icon">`,
and `<noscript>` content are not loads. Loads after a valid https `<base
href>` resolve against it. `page()` is checked like `Widget`; `fragment()` is
not, so a fragment's URLs are yours to keep absolute. React's development
build trips the string warning by itself (it mentions `./MyComponent` in an
error message), which is one more reason to build for production.

The leftover scan looks beside an `html=`, `body=`, or `modules=` path, and
skips a folder that holds Python sources or a `package.json`, since that is a
source folder rather than a build folder. A `dist/` beside your server module
is scanned; a package's own `static/` is not.

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

Two things a sandboxed frame will not do, whatever your toolkit. A `<form>`
cannot submit — the frame has no `allow-forms`, so the browser blocks it
before htmx sees a `submit` event: put `hx-post` on the button and name the
fields with `hx-include="#new-card"`. And an `hx-trigger` filter such as
`keyup[key=='Enter']` is evaluated as JavaScript, which the default policy
refuses; bind the key yourself in a small script and click the button.

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
Any other URL goes to the widget's `route=` tool, which the page carries as
`<meta name="mcp-route">`. micromcp warns when the host reads a widget whose
`route=` names no tool on that server, which is the first moment it can tell; a browser error naming that meta tag means the
widget has no `route=`, or that an `hx-get` dropped the `tool:` scheme where
`hx-post` would have kept it. This is how existing Django views serve a
widget:

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

A context update carries at most 16 000 bytes (`CONTEXT_LIMIT`), counting
`text` and `data` together, and a larger one is refused when the result is
built. `context=` also takes a bare string when there is nothing to put in
`data`.

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

Pushing HTML into a hypermedia widget takes one more step than assigning
`innerHTML`, which would leave the new markup unwired: hand it to the toolkit
instead, `htmx.swap({text: msg.html, target: "#board", swap: "innerMorph"})`,
so the `hx-*` attributes in it are picked up. If the action that broadcast
also returned a fragment, that widget would swap twice, so leave it out:
`broadcast_json(payload, exclude=conn)` skips one connection, and a page knows
its own as `mcp.channel("board").id`. Send the id with the call — a hidden
field and `hx-include`, or an argument — and the tool has it to hand:

```python
@mcp.tool(visibility="app")
def card_add(column: str = "todo", text: str = "", conn: str = ""):
    board.add(column, text)
    sync.broadcast_json({"html": board_html()}, exclude=conn)   # everyone else
    return fragment(render_board())                             # and this page
```

`examples/mcp_app_board.py` does exactly this.

Two practical notes. A context update is debounced, and that includes one a
tool sent with `fragment(context=...)`, so two updates in the same instant
coalesce into the last: send snapshots, not deltas. And a widget holding a
channel open is never "network idle" — the receive call is a long poll — so a
browser automation that waits for idle will time out; wait for an element
instead.

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

With a channel, prefer `ASGIServer` and an ASGI server such as uvicorn; the
dev-host branch is then a `scope`/`send` pair that writes `DEVHOST_HTML`
before delegating the rest to the endpoint.

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

To drive the same server from a script — to seed state, or to act as the
model does while a widget is open — a stateless call carries its version in
`_meta` and repeats the method and name in headers:

```python
import json, urllib.request

VER = "2026-07-28"

def call(method, params, endpoint="http://127.0.0.1:8770/mcp"):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": dict(params)}
    body["params"].setdefault("_meta", {}).update({
        "io.modelcontextprotocol/protocolVersion": VER,
        "io.modelcontextprotocol/clientCapabilities": {},
    })
    req = urllib.request.Request(endpoint, json.dumps(body).encode(), {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": VER,
        "Mcp-Method": method,                                  # headers match the body
        "Mcp-Name": params.get("name") or params.get("uri") or "",
    })
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

call("tools/call", {"name": "roll", "arguments": {"sides": 20}})
```

The dev host has no controls. It loads the first tool that shows a widget and
is visible to the model; `?tool=<name>` picks another, `?args=<json>` passes
that tool's arguments, and `?csp=eval` adds `unsafe-eval` to the policy.
`examples/mcp_app_hypermedia.py` serves it over ASGI beside its demos (the
todo widget with tools and with Django views, the toolkit lab, and the
context counter).

## Examples

- `examples/mcp_app_bundled.py`: a Vite build (`examples/bundled_ui/`) plugged in as
  one module and one stylesheet, with its image inlined — the recipe below, complete
  and runnable
- `examples/mcp_app_charts.py`: a dashboard drawn by ECharts from a CDN, whose data
  comes from an app-only tool and whose live events arrive on a channel
- `examples/mcp_app_map.py`: a shared Leaflet map, with the tile origin declared by
  hand and the model dropping markers into an open widget
- `examples/mcp_app_board.py`: a kanban board with no bundler and no app logic —
  every click a tool call answered with `fragment()`, two open boards kept in step
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
