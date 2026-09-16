"""Test: atomdoc as the sync layer of an MCP App, over a micromcp channel.

The document is an atomdoc `Doc` held by an atomdoc `Session`, whose transport is a
micromcp channel (`ChannelTransport`, below). Widgets are atomdoc thin clients handed
`mcp.WebSocket` as their WebSocket, so atomdoc's wire protocol runs unchanged over MCP tool
calls. The model edits the same `Doc` through ordinary tools, and the session pushes every
commit to every widget as a patch.

    ./examples/atomdoc_app/build.sh        # bundle viewer.js with atomdoc-ts + zod
    python examples/atomdoc_app/server.py  # needs atomdoc[server], pydantic, uvicorn
    open http://127.0.0.1:8776/devhost?mcp=/mcp
"""
import json
import os
import pathlib
from typing import Any, Literal

from pydantic import BaseModel, Field

from atomdoc import Array, ClientConnection, Doc, Session, Transport, UndoManagerConfig, node

HERE = pathlib.Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8776"))
ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}   # the local dev host
THREE = "https://cdn.jsdelivr.net/npm/three@0.186.0/"
BUNDLE = HERE / "build" / "viewer.bundle.js"
Kind = Literal["box", "sphere", "cone", "cylinder", "torus"]


# --- the schema -----------------------------------------------------------------------

@node
class Shape(BaseModel):
    kind: Kind = "box"
    color: str = Field("#4a90d9", pattern=r"^(#[0-9a-fA-F]{6}|[a-z]{3,20})$")
    x: float = Field(0.0, ge=-20, le=20)
    y: float = Field(0.5, ge=-5, le=20)
    z: float = Field(0.0, ge=-20, le=20)
    size: float = Field(1.0, gt=0, le=10)
    label: str = Field("", max_length=60)


@node
class Scene:
    shapes: Array[Shape] = []


# --- atomdoc over a micromcp channel ------------------------------------------------------

class _Client(ClientConnection):
    """An atomdoc client connection that is one micromcp channel connection."""

    def __init__(self, conn):
        self.conn = conn

    @property
    def client_id(self) -> str:
        return self.conn.id

    @property
    def wants_partial(self) -> bool:           # the thick client connects with ?partial=1
        return self.conn.params.get("partial") in ("1", "true")

    async def send(self, message: dict[str, Any]) -> None:
        self.conn.send_json(message)

    async def close(self) -> None:
        self.conn.close()


class ChannelTransport(Transport):
    """atomdoc's Transport, carried by a micromcp Channel: connect, message, and disconnect
    map one to one. The session binds on first use, so a widget may connect first."""

    def __init__(self, channel, session: Session):
        self.channel, self.session = channel, session
        self.clients: dict[str, _Client] = {}
        self.callbacks = None
        channel.on_connect(self._connect)
        channel.on_message(self._message)
        channel.on_disconnect(self._disconnect)

    async def ready(self) -> None:
        if self.callbacks is None:
            await self.session.bind(self)

    async def start(self, on_connect, on_message, on_disconnect) -> None:
        self.callbacks = (on_connect, on_message, on_disconnect)

    async def stop(self) -> None:
        for client in list(self.clients.values()):
            client.conn.close()

    async def _connect(self, conn) -> None:
        await self.ready()
        self.clients[conn.id] = client = _Client(conn)
        await self.callbacks[0](client)

    async def _message(self, conn, text: str) -> None:
        client = self.clients.get(conn.id)
        if client is not None:
            await self.callbacks[1](client, json.loads(text))

    async def _disconnect(self, conn) -> None:
        client = self.clients.pop(conn.id, None)
        if client is not None:
            await self.callbacks[2](client)


# --- the MCP server -----------------------------------------------------------------------

CSS = (HERE.parent / "mcp_app_3d.py").read_text().split('CSS = """', 1)[1].split('"""', 1)[0]
BODY = """<div id="view"></div>
<div class="bar"><span id="info">Click an object to select it.</span>
<button type="button" id="recolor" disabled>Recolor</button>
<button type="button" id="del" disabled>Delete</button>
<button type="button" id="undo">Undo my last edit</button>
<button type="button" id="ask" disabled>Ask Claude</button></div>
<ul id="list"></ul><small data-mcp-status>connecting&hellip;</small>"""


def scene_mcp():
    from micromcp import MCP
    from micromcp.apps import Channel, Widget

    if not BUNDLE.exists():
        raise SystemExit(f"{BUNDLE} is missing: run examples/atomdoc_app/build.sh first")
    doc = Doc(Scene(shapes=[Shape(kind="box", color="#8b5a2b", y=0.5, label="crate"),
                            Shape(kind="sphere", color="#d94a4a", y=1.3, size=0.6, label="apple"),
                            Shape(kind="cone", color="#4aa84a", x=3, y=1, z=-2, size=2,
                                  label="tree")]),
              undo_manager=UndoManagerConfig(max_steps=100))
    mcp = MCP("atomdoc-scene", "0.1.0")
    session = Session(doc)
    transport = ChannelTransport(Channel(mcp, "atomdoc"), session)
    viewer = Widget("atomdoc-scene-v2", title="3D scene (atomdoc)", border=True, styles=CSS,
                    body=BODY, modules=[BUNDLE],
                    imports={"three": THREE + "build/three.module.js",
                             "three/addons/": THREE + "examples/jsm/"})

    ready = transport.ready

    def summary():
        shapes = list(doc.root.shapes)
        if not shapes:
            return "The scene is empty."
        return f"{len(shapes)} objects: " + "; ".join(
            f"{s.id} {s.color} {s.kind} at ({s.x:g}, {s.y:g}, {s.z:g}) size {s.size:g}"
            + (f" labeled {s.label!r}" if s.label else "") for s in shapes)

    # Model tools: the model edits the Doc directly; the session broadcasts each commit.
    # They are async so they run on the event loop, where the session schedules broadcasts.

    @viewer.tool(mcp, title="Show the 3D scene", read_only=True)
    async def show_scene() -> str:
        """Show the shared 3D scene. The user can orbit it, select, recolor and delete objects,
        undo their own edits, and ask you about a selection (you are told what they select)."""
        await ready()
        return summary()

    @mcp.tool(title="Add a 3D object")
    async def scene_add(kind: Kind, color: str = "#4a90d9", x: float = 0, y: float = 0.5,
                        z: float = 0, size: float = 1, label: str = "") -> str:
        """Add an object; open scene widgets show it at once.

        Args:
            kind: box, sphere, cone, cylinder, or torus.
            color: #rrggbb or a CSS color name.
            x: Left-right position (the scene spans about -10..10).
            y: Height of the center; the ground is y=0, so a size-1 box sits at y=0.5.
            z: Front-back position.
            size: Scale; it applies to every axis.
            label: A short name shown in the widget.
        """
        await ready()
        with doc.transaction():
            shape = doc.create_node(Shape, kind=kind, color=color, x=x, y=y, z=z, size=size,
                                    label=label)
            doc.root.shapes.append(shape)
        return f"Added {shape.id}. {summary()}"

    @mcp.tool(title="Remove a 3D object")
    async def scene_remove(id: str) -> str:
        """Remove an object by its id."""
        await ready()
        shape = doc.get_node_by_id(id)
        if shape is None or shape is doc.root:
            return f"No object {id}. {summary()}"
        with doc.transaction():
            shape.delete()
        return f"Removed {id}. {summary()}"

    @mcp.tool(title="Undo the last change made through these tools")
    async def scene_undo() -> str:
        """Undo the most recent change made with scene_add or scene_remove (the user's own
        edits in the widget have their own undo there)."""
        await ready()
        doc.undo_manager.undo()
        return summary()

    return mcp


def build(devhost: bool = False):
    from micromcp import ASGIServer
    from micromcp.apps import DEVHOST_HTML
    mcp = scene_mcp()
    inner = ASGIServer(mcp, path="/mcp", allowed_origins=ORIGINS)
    # The same server at a second path: a new connector there fetches the current widget,
    # where the host's cached copy for /mcp may be an older one.
    v2 = ASGIServer(mcp, path="/v2/mcp", allowed_origins=ORIGINS)
    dev = DEVHOST_HTML if devhost else None

    async def app(scope, receive, send):
        if dev and scope["type"] == "http" and scope["path"] == "/devhost":
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/html; charset=utf-8")]})
            return await send({"type": "http.response.body", "body": dev})
        server = v2 if scope.get("path", "").startswith("/v2/") else inner
        return await server(scope, receive, send)
    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(build(devhost=True), host="127.0.0.1", port=PORT)
