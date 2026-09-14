"""Hypermedia MCP Apps: fixi over tool calls, with and without Django.

The widget is a static page that never touches the network. `micromcp.page()`
inlines the MCP Apps bridge and fixi; every `fx-action` becomes a `tools/call`
the host proxies to this server, and the tool's HTML fragment is swapped in.

    /mcp          plain micromcp: `fx-action="tool:todo_add"` calls app-only
                  tools that render fragments with html.escape
    /django/mcp   Django behind micromcp: `fx-action="/django/ui/todos/add/"`
                  goes through one app-only tool (`django_routes`) into ordinary
                  Django views and templates, in-process

Locally (needs uvicorn and django):

    python examples/mcp_app_hypermedia.py
    open http://127.0.0.1:8770/devhost?mcp=/mcp
    open http://127.0.0.1:8770/devhost?mcp=/django/mcp

On Modal (each path is its own custom connector in Claude's settings):

    modal deploy examples/mcp_app_hypermedia.py

The widget's footer reports the host's name and whether it advertised
`serverTools` — the capability this pattern depends on.
"""
import html
import os
import pathlib
import threading

HERE = pathlib.Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8770"))
ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}   # the local dev host

STYLE = """<style>
:root{color-scheme:light dark}
body{font:14px system-ui;margin:12px;color:var(--color-text-primary,#222)}
h1{font-size:16px;margin:0 0 8px}ul{list-style:none;padding:0;margin:0 0 8px}
li{margin:2px 0}li.done span{text-decoration:line-through;opacity:.6}
button{font:inherit;cursor:pointer}form{display:flex;gap:6px;margin:6px 0}
input{flex:1;font:inherit}small{display:block;margin-top:8px;opacity:.7}
</style>"""

STATUS_JS = """
mcp.ready.then(r => mcp.status("host " + ((r.hostInfo && r.hostInfo.name) || "?") +
  " · serverTools " + (r.hostCapabilities && r.hostCapabilities.serverTools ? "yes" : "NO")));
document.addEventListener("fx:swapped", () => mcp.status("updated " + new Date().toLocaleTimeString() +
  " via tools/call · host " + ((mcp.hostInfo && mcp.hostInfo.name) || "?")));
"""


def widget(title: str, first_action: str, route: str | None = None) -> str:
    from micromcp import page
    head = STYLE + (f'<meta name="mcp-route" content="{html.escape(route)}">' if route else "")
    body = (f"<h1>{html.escape(title)}</h1>"
            f'<div id="app" fx-action="{html.escape(first_action)}" fx-trigger="mcp:ready" '
            f'fx-swap="innerHTML"><em>connecting&hellip;</em></div>'
            f"<small data-mcp-status>waiting for the host handshake</small>")
    return page(body, title=title, head=head,
                scripts=[(HERE / "vendor" / "fixi.js").read_text(), STATUS_JS])


class Todos:
    """Shared in-memory state (one container on Modal)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.items = [{"id": 1, "text": "Try fixi inside an MCP App", "done": True},
                      {"id": 2, "text": "Put Django behind micromcp", "done": False}]
        self.next = 3

    def add(self, text):
        text = text.strip()[:200]
        if text:
            with self.lock:
                self.items.append({"id": self.next, "text": text, "done": False})
                self.next += 1

    def toggle(self, id_):
        with self.lock:
            for t in self.items:
                if str(t["id"]) == str(id_):
                    t["done"] = not t["done"]

    def clear(self):
        with self.lock:
            self.items = [t for t in self.items if not t["done"]]

    def summary(self):
        return "; ".join(f"[{'x' if t['done'] else ' '}] {t['text']}" for t in self.items) or "(empty)"


def todo_context(todos, action):
    """What the model should know after a widget action. Each push replaces the last, so it
    is a snapshot of the whole list: a sentence the server writes, plus the user-written
    item text as data."""
    done = sum(t["done"] for t in todos.items)
    return {"text": f"Todo widget, current state: {len(todos.items) - done} open and {done} "
                    f"done (last action: the user {action}).",
            "data": {"items": [{"text": t["text"], "done": t["done"]} for t in todos.items]}}


# --- plain micromcp: tools render fragments ------------------------------------------

def plain_mcp():
    from micromcp import MCP, fragment

    mcp, todos, uri = MCP("hm-plain", "0.1.0"), Todos(), "ui://hm-plain/todos-v1"
    target = 'fx-target="#app" fx-swap="innerHTML"'

    def render():
        rows = "".join(
            f'<li class="{"done" if t["done"] else ""}"><button type="button" fx-action="tool:todo_toggle" '
            f'name="id" value="{t["id"]}" {target}>{"&#9745;" if t["done"] else "&#9744;"}</button> '
            f'<span>{html.escape(t["text"])}</span></li>' for t in todos.items)
        return (f"<ul>{rows}</ul>"
                f'<form><input name="text" placeholder="New todo" autocomplete="off">'
                f'<button type="button" fx-action="tool:todo_add" {target}>Add</button></form>'
                f'<button type="button" fx-action="tool:todo_clear" {target}>Clear done</button>')

    page_html = widget("Todos (tools)", "tool:todo_list")

    @mcp.resource(uri, title="Todos widget (tools)")
    def todos_widget() -> str:
        """Static widget; all data arrives through app-only tool calls."""
        return page_html

    @mcp.tool(title="Show todos", read_only=True, meta={"ui": {"resourceUri": uri}, "ui/resourceUri": uri})
    def show_todos() -> str:
        """Show the interactive todo list to the user."""
        return "Todo list: " + todos.summary()

    @mcp.tool(visibility="app", read_only=True)
    def todo_list():
        """Render the todo list (for the widget)."""
        return fragment(render())

    @mcp.tool(visibility="app")
    def todo_add(text: str = ""):
        """Add a todo (for the widget)."""
        todos.add(text)
        return fragment(render(), context=todo_context(todos, "added an item"))

    @mcp.tool(visibility="app")
    def todo_toggle(id: str):
        """Toggle a todo (for the widget)."""
        todos.toggle(id)
        return fragment(render(), context=todo_context(todos, "toggled an item"))

    @mcp.tool(visibility="app")
    def todo_clear():
        """Remove finished todos (for the widget)."""
        todos.clear()
        return fragment(render(), context=todo_context(todos, "cleared finished items"))

    return mcp


# --- Django behind micromcp: views render fragments ------------------------------------

TEMPLATES = {"todos.html": """<ul>{% for t in todos %}
<li class="{% if t.done %}done{% endif %}"><button type="button" fx-method="post"
  fx-action="{% url 'todo-toggle' t.id %}" fx-target="#app" fx-swap="innerHTML">{% if t.done %}&#9745;{% else %}&#9744;{% endif %}</button>
  <span>{{ t.text }}</span></li>{% endfor %}</ul>
<form><input name="text" placeholder="New todo" autocomplete="off">
<button type="button" fx-method="post" fx-action="{% url 'todo-add' %}" fx-target="#app" fx-swap="innerHTML">Add</button></form>
<button type="button" fx-method="post" fx-action="{% url 'todo-clear' %}" fx-target="#app" fx-swap="innerHTML">Clear done</button>
<small>rendered by Django {{ version }}</small>"""}


class URLs:
    urlpatterns: list = []


def django_asgi():
    import django
    from django.conf import settings
    if not settings.configured:
        settings.configure(
            DEBUG=False, SECRET_KEY="demo-only", ROOT_URLCONF=URLs,
            ALLOWED_HOSTS=["localhost", "127.0.0.1", ".modal.run"],
            INSTALLED_APPS=[], DATABASES={}, MIDDLEWARE=["django.middleware.common.CommonMiddleware"],
            TEMPLATES=[{"BACKEND": "django.template.backends.django.DjangoTemplates",
                        "OPTIONS": {"loaders": [("django.template.loaders.locmem.Loader", TEMPLATES)]}}],
        )
    django.setup()
    from django.core.asgi import get_asgi_application
    from django.shortcuts import redirect, render
    from django.urls import path
    from django.views.decorators.http import require_POST
    from micromcp import MCP, ASGIServer
    from micromcp.contrib.django import django_async_view, django_routes, set_mcp_context

    mcp, todos, uri = MCP("hm-django", "0.1.0"), Todos(), "ui://hm-django/todos-v1"
    django_routes(mcp, prefixes=["/django/ui/"], host="localhost")
    page_html = widget("Todos (Django)", "/django/ui/todos/", route="django_http")

    @mcp.resource(uri, title="Todos widget (Django)")
    def todos_widget() -> str:
        """Static widget; all data arrives through Django views."""
        return page_html

    @mcp.tool(title="Show todos", read_only=True, meta={"ui": {"resourceUri": uri}, "ui/resourceUri": uri})
    def show_todos() -> str:
        """Show the interactive todo list to the user."""
        return "Todo list: " + todos.summary()

    def todo_list(request):
        return render(request, "todos.html", {"todos": todos.items, "version": django.get_version()})

    def changed(action):             # redirect back to the list, telling the model what happened
        ctx = todo_context(todos, action)
        return set_mcp_context(redirect("todo-list"), ctx["text"], ctx["data"])

    @require_POST
    def todo_add(request):
        todos.add(request.POST.get("text", ""))
        return changed("added an item")

    @require_POST
    def todo_toggle(request, id):
        todos.toggle(id)
        return changed("toggled an item")

    @require_POST
    def todo_clear(request):
        todos.clear()
        return changed("cleared finished items")

    server = ASGIServer(mcp, path="/django/mcp", allowed_origins=ORIGINS)
    URLs.urlpatterns = [
        path("django/mcp", django_async_view(server)),
        path("django/ui/todos/", todo_list, name="todo-list"),
        path("django/ui/todos/add/", todo_add, name="todo-add"),
        path("django/ui/todos/<int:id>/toggle/", todo_toggle, name="todo-toggle"),
        path("django/ui/todos/clear/", todo_clear, name="todo-clear"),
    ]
    return get_asgi_application()


# --- one ASGI app for both connectors ---------------------------------------------------

def build(devhost: bool = False):
    import sys
    sys.path.insert(0, str(HERE))
    import toolkit_lab
    from micromcp import MCP, ASGIServer
    TEMPLATES.update(toolkit_lab.LAB_TEMPLATES)
    plain = ASGIServer(plain_mcp(), path="/mcp", allowed_origins=ORIGINS)
    dj = django_asgi()
    URLs.urlpatterns += toolkit_lab.django_urls()
    lab = ASGIServer(toolkit_lab.lab_mcp(), path="/lab/mcp", allowed_origins=ORIGINS)
    ctx_mcp = MCP("hm-context", "0.1.0")          # the counter alone, as its own connector
    toolkit_lab.add_counter(ctx_mcp)
    ctx = ASGIServer(ctx_mcp, path="/ctx/mcp", allowed_origins=ORIGINS)
    dev = (HERE / "devhost.html").read_bytes() if devhost else None

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                await send({"type": msg["type"] + ".complete"})
                if msg["type"] == "lifespan.shutdown":
                    return
        p = scope.get("path", "")
        if scope["type"] == "http" and os.environ.get("WIRE_LOG"):   # one line per request, headers only
            h = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            print(f"WIRE {scope['method']} {p} method={h.get('mcp-method', '-')} "
                  f"name={h.get('mcp-name', '-')} ua={h.get('user-agent', '-')[:40]!r}", flush=True)
        if p.startswith("/django/"):
            return await dj(scope, receive, send)
        if p.startswith("/lab/"):
            return await lab(scope, receive, send)
        if p.startswith("/ctx/"):
            return await ctx(scope, receive, send)
        if dev and p == "/devhost":
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/html; charset=utf-8")]})
            return await send({"type": "http.response.body", "body": dev})
        return await plain(scope, receive, send)
    return app


try:
    import modal
except ImportError:
    modal = None

if modal is not None:
    image = (modal.Image.debian_slim(python_version="3.12")
             .pip_install("django>=5.2")
             .env({"PYTHONPATH": "/root/src", "WIRE_LOG": "1"})
             .add_local_dir(HERE.parent / "src", "/root/src")
             .add_local_dir(HERE / "vendor", "/root/vendor")
             .add_local_file(HERE / "toolkit_lab.py", "/root/toolkit_lab.py"))
    app = modal.App("micromcp-hypermedia")

    @app.function(image=image, max_containers=1, timeout=600)
    @modal.concurrent(max_inputs=50)
    @modal.asgi_app()
    def web():
        return build()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(build(devhost=True), host="127.0.0.1", port=PORT)
