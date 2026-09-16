"""A shared map, with no bundler.

Leaflet comes from a CDN, so `Widget` declares its origin for the host and the stylesheet
brings its own marker icons and fonts; the tile server is the one origin to declare by hand
(`csp={"resourceDomains": [...]}` takes an origin, never a URL template). The model drops
markers and flies the view with tools, the widget reports what the user clicked and where
the view sits with model context, and a channel pushes the model's changes into an open map.

    python examples/mcp_app_map.py
    open http://127.0.0.1:8773/devhost?mcp=/mcp
"""
import os
import itertools
import threading
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

from micromcp import MCP, Server
from micromcp.apps import DEVHOST_HTML, Channel, Widget

PORT = int(os.environ.get("PORT", "8773"))
ORIGIN = f"http://127.0.0.1:{PORT}"
HERE = Path(__file__).parent
LEAFLET = "https://unpkg.com/leaflet@1.9.4/dist"

mcp = MCP("map-explorer", "1.0.0")

# ---------------------------------------------------------------- state
_lock = threading.Lock()
_ids = itertools.count(1)
STATE = {"view": {"lat": 42.3601, "lon": -71.0589, "zoom": 12}, "markers": [], "selected": None}


def snapshot():
    with _lock:
        return {"view": dict(STATE["view"]),
                "markers": [dict(m) for m in STATE["markers"]],
                "selected": STATE["selected"]}


def describe(s):
    ms = ", ".join(f"{m['label']} ({m['lat']:.4f}, {m['lon']:.4f})" for m in s["markers"]) or "none"
    sel = next((m for m in s["markers"] if m["id"] == s["selected"]), None)
    return (f"Map widget: centre {s['view']['lat']:.4f}, {s['view']['lon']:.4f} "
            f"at zoom {s['view']['zoom']}. Markers: {ms}. "
            + (f"Selected: {sel['label']}." if sel else "Nothing selected."))


# ---------------------------------------------------------------- widget
explorer = Widget(
    "map",
    title="Map explorer",
    body=(HERE / "map_ui" / "body.html"),
    styles=[f"{LEAFLET}/leaflet.css", HERE / "map_ui" / "style.css"],
    scripts=[f"{LEAFLET}/leaflet.js", HERE / "map_ui" / "app.js"],
    csp={"resourceDomains": ["https://tile.openstreetmap.org"]},
)

channel = Channel(mcp, "map")


@channel.on_connect
def joined(conn):
    conn.send_json({"state": snapshot()})


def push(fly=False):
    s = snapshot()
    channel.broadcast_json({"state": s, "fly": fly, "view": s["view"]})


# ---------------------------------------------------------------- model tools
@explorer.tool(mcp, read_only=True)
def show_map(lat: float = 42.3601, lon: float = -71.0589, zoom: int = 12) -> str:
    """Open the shared map explorer at a location."""
    with _lock:
        STATE["view"] = {"lat": lat, "lon": lon, "zoom": zoom}
    push(fly=True)
    return f"Map open at {lat:.4f}, {lon:.4f}, zoom {zoom}."


@mcp.tool
def map_add_marker(lat: float, lon: float, label: str, note: str = "") -> str:
    """Drop a labelled marker on the map."""
    with _lock:
        m = {"id": next(_ids), "lat": lat, "lon": lon, "label": label, "note": note}
        STATE["markers"].append(m)
        STATE["selected"] = m["id"]
    push()
    return f"Marker {m['id']} '{label}' at {lat:.4f}, {lon:.4f}."


@mcp.tool
def map_fly_to(lat: float, lon: float, zoom: int = 13) -> str:
    """Fly the shared map view to a place."""
    with _lock:
        STATE["view"] = {"lat": lat, "lon": lon, "zoom": zoom}
    push(fly=True)
    return f"Flying to {lat:.4f}, {lon:.4f} at zoom {zoom}."


@mcp.tool(read_only=True)
def map_markers() -> dict:
    """List the markers currently on the map."""
    return snapshot()


# ---------------------------------------------------------------- app-only tools
@mcp.tool(visibility="app", read_only=True)
def map_state() -> dict:
    """Current map state, for the widget on load."""
    return snapshot()


@mcp.tool(visibility="app")
def map_click(lat: float, lon: float) -> dict:
    """The user clicked the map: drop a pin there."""
    with _lock:
        n = sum(1 for m in STATE["markers"] if m["label"].startswith("Pin "))
        m = {"id": next(_ids), "lat": lat, "lon": lon,
             "label": f"Pin {n + 1}", "note": "dropped by the user"}
        STATE["markers"].append(m)
        STATE["selected"] = m["id"]
    s = snapshot()
    channel.broadcast_json({"state": s, "fly": False, "view": s["view"]})
    return s


@mcp.tool(visibility="app")
def map_view(lat: float, lon: float, zoom: int,
             south: float = 0.0, west: float = 0.0,
             north: float = 0.0, east: float = 0.0) -> dict:
    """The user moved the map: record the new view."""
    with _lock:
        STATE["view"] = {"lat": lat, "lon": lon, "zoom": zoom,
                         "south": south, "west": west, "north": north, "east": east}
    return {"ok": True}


@mcp.tool(visibility="app")
def map_select(marker_id: int) -> dict:
    """The user selected a marker."""
    with _lock:
        STATE["selected"] = marker_id
    s = snapshot()
    channel.broadcast_json({"state": s, "fly": False, "view": s["view"]})
    return s


# ---------------------------------------------------------------- serve
endpoint = Server(mcp, path="/mcp", allowed_origins={ORIGIN})


def app(environ, start_response):
    if environ.get("PATH_INFO") == "/devhost":
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(DEVHOST_HTML)))])
        return [DEVHOST_HTML]
    return endpoint(environ, start_response)


class Threaded(ThreadingMixIn, WSGIServer):
    daemon_threads = True


if __name__ == "__main__":
    print(f"dev host: {ORIGIN}/devhost?mcp=/mcp", flush=True)
    make_server("127.0.0.1", PORT, app, server_class=Threaded).serve_forever()
