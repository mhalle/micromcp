"""A bundled widget: a Vite build plugged into an MCP App.

The smallest complete version of the recipe in docs/apps.md. `examples/bundled_ui/` is an
ordinary Vite project whose build writes two files, `dist/widget.js` and `dist/widget.css`,
with the logo inlined as a data: URL in both; `Widget(modules=..., styles=...)` wraps them in
a page around the bridge. The model opens the widget with `show_readings` and the widget calls
the app-only `readings` tool for its data, so nothing but tool calls crosses the frame.

    python examples/mcp_app_bundled.py
    open http://127.0.0.1:8772/devhost?mcp=/mcp

The committed build is what the example runs; rebuild it after editing `bundled_ui/src`:

    cd examples/bundled_ui && npm install && npm run build
"""
import math
import os
import pathlib
from socketserver import ThreadingMixIn
from typing import Literal
from wsgiref.simple_server import WSGIServer, make_server

from micromcp import MCP, Server
from micromcp.apps import DEVHOST_HTML, Widget

HERE = pathlib.Path(__file__).resolve().parent
UI = HERE / "bundled_ui" / "dist"
PORT = int(os.environ.get("PORT", "8772"))
ORIGIN = f"http://127.0.0.1:{PORT}"

SENSORS = {"kitchen": ("Kitchen temperature", "°C", 21.5, 2.5),
           "attic": ("Attic temperature", "°C", 14.0, 6.0),
           "cellar": ("Cellar humidity", "%", 62.0, 4.0)}

mcp = MCP("readings", "1.0.0")
readings_widget = Widget("readings", title="Readings",
                         body='<div class="wrap" id="root"></div>',
                         modules=[UI / "widget.js"], styles=[UI / "widget.css"])


def series(sensor: str) -> dict:
    """A deterministic day of readings, so the example needs no data source."""
    label, unit, base, swing = SENSORS[sensor]
    seed = sum(map(ord, sensor))
    values = [round(base + swing * math.sin((seed + i * 7) / 9) / 2
                    + swing * math.sin((seed + i) / 3) / 6, 2) for i in range(24)]
    return {"sensor": sensor, "label": label, "unit": unit, "values": values}


@readings_widget.tool(mcp, read_only=True)     # the model calls this; the host shows the widget
def show_readings(sensor: Literal["kitchen", "attic", "cellar"] = "kitchen") -> str:
    """Show the readings widget for one sensor."""
    data = series(sensor)
    return f"{data['label']}: {data['values'][-1]}{data['unit']} now."


@mcp.tool(visibility="app")                    # the widget calls this; the model never sees it
def readings(sensor: Literal["kitchen", "attic", "cellar"]) -> dict:
    """The last 24 readings for one sensor."""
    return series(sensor)


endpoint = Server(mcp, path="/mcp", allowed_origins={ORIGIN})   # the dev host is a browser page


def app(environ, start_response):
    if environ.get("PATH_INFO") == "/devhost":
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(DEVHOST_HTML)))])
        return [DEVHOST_HTML]                                   # bytes, not str
    return endpoint(environ, start_response)


class Threaded(ThreadingMixIn, WSGIServer):                     # the host calls concurrently
    daemon_threads = True


if __name__ == "__main__":
    print(f"open {ORIGIN}/devhost?mcp=/mcp")
    make_server("127.0.0.1", PORT, app, server_class=Threaded).serve_forever()
