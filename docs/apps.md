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
protocol-relative `//host/...`) is refused rather than inlined as source, and
`csp=` entries must be plain origins (`https://api.example.com`,
`https://*.example.com`, `wss://live.example.com:8443`). Claude enforces the
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
`markupsafe.Markup`. Those libraries escape the text you put in them, so
user-written data goes straight in:

```python
from fastcore.xml import Button, Div, Li, Span, Ul

def render(todos):
    return Div(Ul(*[Li(Button("done", hx_post=f"tool:todo_toggle?id={t.id}", hx_target="#app"),
                       Span(t.text))                 # t.text is escaped
                    for t in todos]))
```

Pass one object, so wrap siblings in an element. It is rendered once, when
the page or fragment is built, and its output is trusted as markup, exactly
as Jinja trusts it. A str is used as is: escape what you interpolate into one
yourself (`html.escape`).

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
(`ctx.fetch`) and fixi (`fx:config`); `Widget(fetch="global")` also replaces
`window.fetch`, for libraries without a hook (Datastar). With htmx, call
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
origins; `?csp=eval` adds `unsafe-eval`), completes the handshake, proxies the
widget's `tools/call` for tools whose visibility includes `app`, and shows
what the model would receive. Serve it next to your MCP endpoint;
`python examples/mcp_app_hypermedia.py` does, at
`http://127.0.0.1:8770/devhost?mcp=/mcp`, beside the demos (the todo widget
with tools and with Django views, the toolkit lab, and the context counter).

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
