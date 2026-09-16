"""A dashboard the model drives, with a charting library from a CDN.

No bundler: ECharts is loaded from jsdelivr, so `Widget` declares that origin for the host,
and the widget's own script and stylesheet are inlined from files. The model opens the
dashboard and switches metrics with tools; the widget asks the app-only `dashboard_data`
tool for the series it draws, and `record_event` pushes into an open dashboard over a
channel, so an event the model records appears without a reload.

    python examples/mcp_app_charts.py
    open http://127.0.0.1:8775/devhost?mcp=/mcp
"""
import math
import os
import pathlib
import threading
from socketserver import ThreadingMixIn
from typing import Literal
from wsgiref.simple_server import WSGIServer, make_server

from micromcp import MCP, Server
from micromcp.apps import DEVHOST_HTML, Channel, Widget

HERE = pathlib.Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8775"))
ORIGIN = f"http://127.0.0.1:{PORT}"
ECHARTS = "https://cdn.jsdelivr.net/npm/echarts@6.0.0/dist/echarts.min.js"

METRICS = {"revenue": ("Revenue", "$", 4200, 900), "signups": ("Signups", "", 120, 45),
           "latency": ("API latency p95", " ms", 240, 60)}
RANGES = {"7d": 7, "30d": 30, "90d": 90}

_lock = threading.Lock()
_events: list[str] = []

mcp = MCP("signal", "1.0.0")
feed = Channel(mcp, "events")
dashboard = Widget(
    "signal", title="Signal",
    body='<div class="wrap">'
         '<div class="head"><h1 id="title">Revenue</h1><span class="range" id="range"></span></div>'
         '<div class="kpi"><span class="value" id="value"></span>'
         '<span class="delta" id="delta"></span></div>'
         '<div id="chart"></div>'
         '<div class="chips">'
         '<button data-metric="revenue">Revenue</button>'
         '<button data-metric="signups">Signups</button>'
         '<button data-metric="latency">Latency</button></div>'
         '<p class="live" id="live"></p></div>',
    styles=[HERE / "charts_ui" / "app.css"],
    scripts=[ECHARTS, HERE / "charts_ui" / "app.js"],   # the CDN origin is declared for you
)


def series(metric: str, days: int = 30) -> dict:
    """A deterministic series, so the example needs no data source."""
    label, unit, base, swing = METRICS[metric]
    seed = sum(map(ord, metric))
    values = [round(base + swing * math.sin((seed + i) / 5) / 2
                    + swing * math.cos((seed + i) / 11) / 3 + len(_events) * base * 0.01)
              for i in range(days)]
    half = max(1, days // 2)
    now, before = sum(values[-half:]), sum(values[:half]) or 1
    return {"metric": metric, "label": label, "unit": unit,
            "range": f"Last {days} days", "days": [f"day {i + 1}" for i in range(days)],
            "values": values, "total": sum(values) if unit != " ms" else round(sum(values) / days),
            "change": round((now - before) / before * 100, 1)}


@dashboard.tool(mcp, read_only=True)     # the model calls this; the host shows the widget
def show_dashboard(metric: Literal["revenue", "signups", "latency"] = "revenue") -> str:
    """Open the dashboard on one metric."""
    d = series(metric)
    return f"{d['label']}, {d['range']}: {d['total']}{d['unit']} ({d['change']:+}%)."


@mcp.tool(visibility="app")              # the widget calls this; the model never sees it
def dashboard_data(metric: Literal["revenue", "signups", "latency"],
                   days: Literal[7, 30, 90] = 30) -> dict:
    """The series behind one metric."""
    return series(metric, days)


@mcp.tool
def record_event(event: str) -> str:
    """Record an event; open dashboards pick it up at once."""
    with _lock:
        _events.append(event)
        count = len(_events)
    feed.broadcast_json({"event": event, "count": count})
    return f"Recorded {event!r} ({count} so far)."


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
