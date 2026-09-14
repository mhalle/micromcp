"""Hypermedia MCP Apps with micromcp: htmx over tool calls, with and without Django.

A `Widget` is a static page the host renders in a sandboxed iframe, and
`@mcp.tool(widget=...)` attaches it to the tool that shows it. Inside the page,
htmx requests travel as host-proxied `tools/call`, and each app-only tool
answers with an HTML `fragment`, which also tells the model what changed.

    /mcp          app-only tools render the fragments (html.escape)
    /django/mcp   Django views and templates render them, through `django_routes`
    /lab/mcp      the toolkit lab (toolkit_lab.py); /ctx/mcp, its model-context counter

Locally (needs uvicorn and django):

    python examples/mcp_app_hypermedia.py
    open http://127.0.0.1:8770/devhost?mcp=/mcp        # or /django/mcp, /lab/mcp, /ctx/mcp

On Modal (each path is its own custom connector in Claude's settings):

    modal deploy examples/mcp_app_hypermedia.py
"""
import html
import os
import pathlib
import threading

HERE = pathlib.Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8770"))
ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}   # the local dev host
HTMX = HERE / "vendor" / "htmx.min.js"                              # htmx 4.0.0, inlined

CSS = """
:root{color-scheme:light dark}
body{font:14px system-ui;margin:12px;color:var(--color-text-primary,#222)}
h1{font-size:16px;margin:0 0 8px}ul{list-style:none;padding:0;margin:0 0 8px}
li{margin:2px 0}li.done span{text-decoration:line-through;opacity:.6}
button{font:inherit;cursor:pointer}form{display:flex;gap:6px;margin:6px 0}
input{flex:1;font:inherit}small{display:block;margin-top:8px;opacity:.7}
"""
SWAP = 'hx-target="#app" hx-swap="innerMorph"'


def todo_widget(title: str, load: str, route: str | None = None):
    """The same page for both servers; `load` is the htmx attribute that fetches the list."""
    from micromcp import Widget
    return Widget("todos", title=title, styles=CSS, scripts=[HTMX], route=route, border=True,
                  body=(f"<h1>{html.escape(title)}</h1>"
                        f'<div id="app" {load} hx-trigger="mcp:ready" {SWAP}>'
                        f"<em>connecting&hellip;</em></div><small data-mcp-status></small>"))


class Todos:
    """Shared in-memory state (one container on Modal)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.items = [{"id": 1, "text": "Try htmx inside an MCP App", "done": True},
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

    def context(self, action):
        """What the model should know after a widget action. Each update replaces the last,
        so it is a snapshot: a sentence the server writes, and the user-written items as data."""
        done = sum(t["done"] for t in self.items)
        return {"text": f"Todo widget, current state: {len(self.items) - done} open and {done} "
                        f"done (last action: the user {action}).",
                "data": {"items": [{"text": t["text"], "done": t["done"]} for t in self.items]}}


# --- plain micromcp: app-only tools render the fragments ------------------------------

def plain_mcp():
    from micromcp import MCP, fragment

    mcp, todos = MCP("hm-plain", "0.1.0"), Todos()

    def render():
        rows = "".join(
            f'<li class="{"done" if t["done"] else ""}"><button type="button" '
            f'hx-post="tool:todo_toggle?id={t["id"]}" {SWAP}>'
            f'{"&#9745;" if t["done"] else "&#9744;"}</button> '
            f'<span>{html.escape(t["text"])}</span></li>' for t in todos.items)
        return (f"<ul>{rows}</ul>"
                f'<form><input name="text" placeholder="New todo" autocomplete="off">'
                f'<button type="button" hx-post="tool:todo_add" {SWAP}>Add</button></form>'
                f'<button type="button" hx-post="tool:todo_clear" {SWAP}>Clear done</button>')

    @mcp.tool(widget=todo_widget("Todos", 'hx-post="tool:todo_list"'),
              title="Show todos", read_only=True)
    def show_todos() -> str:
        """Show the interactive todo list to the user."""
        return todos.context("opened the list")["text"]

    @mcp.tool(visibility="app", read_only=True)
    def todo_list():
        """Render the todo list (widget only)."""
        return fragment(render())

    @mcp.tool(visibility="app")
    def todo_add(text: str = ""):
        """Add a todo (widget only)."""
        todos.add(text)
        return fragment(render(), context=todos.context("added an item"))

    @mcp.tool(visibility="app")
    def todo_toggle(id: str):
        """Toggle a todo (widget only)."""
        todos.toggle(id)
        return fragment(render(), context=todos.context("toggled an item"))

    @mcp.tool(visibility="app")
    def todo_clear():
        """Remove finished todos (widget only)."""
        todos.clear()
        return fragment(render(), context=todos.context("cleared finished items"))

    return mcp


# --- Django behind micromcp: views and templates render the fragments ------------------

TEMPLATES = {"todos.html": """<ul>{% for t in todos %}
<li class="{% if t.done %}done{% endif %}"><button type="button" hx-post="{% url 'todo-toggle' t.id %}"
  hx-target="#app" hx-swap="innerMorph">{% if t.done %}&#9745;{% else %}&#9744;{% endif %}</button>
  <span>{{ t.text }}</span></li>{% endfor %}</ul>
<form><input name="text" placeholder="New todo" autocomplete="off">
<button type="button" hx-post="{% url 'todo-add' %}" hx-target="#app" hx-swap="innerMorph">Add</button></form>
<button type="button" hx-post="{% url 'todo-clear' %}" hx-target="#app" hx-swap="innerMorph">Clear done</button>
<small>rendered by Django {{ version }}</small>"""}  # noqa: E501


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
    from micromcp import MCP, ASGIServer, django_async_view, django_routes, set_mcp_context

    mcp, todos = MCP("hm-django", "0.1.0"), Todos()
    route = django_routes(mcp, prefixes=["/django/ui/"], host="localhost")

    @mcp.tool(widget=todo_widget("Todos (Django)", 'hx-get="/django/ui/todos/"', route=route),
              title="Show todos", read_only=True)
    def show_todos() -> str:
        """Show the interactive todo list to the user."""
        return todos.context("opened the list")["text"]

    def todo_list(request):
        return render(request, "todos.html", {"todos": todos.items, "version": django.get_version()})

    def changed(action):             # back to the list, telling the model what the list is now
        ctx = todos.context(action)
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


# --- one ASGI app for every connector ---------------------------------------------------

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
