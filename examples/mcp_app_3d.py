"""A shared 3D scene as an MCP App.

The model builds the scene with tools (`scene_add`, `scene_remove`, `scene_clear`); the widget
renders it with three.js, loaded from jsdelivr through an import map that `Widget` declares
for the host. Every change is pushed to open widgets over a channel (`mcp.channel("scene")`),
so objects the model adds appear at once. Click an object (or its name in the list) to select it: the widget tells the model
what is selected through model context, "Delete selected" calls the same `scene_remove` tool the
model uses, and "Ask Claude" posts a question about the selection into the chat.

    python examples/mcp_app_3d.py
    open http://127.0.0.1:8771/devhost?mcp=/mcp

    modal deploy examples/mcp_app_3d.py      # then add <url>/mcp as a custom connector
"""
import os
import pathlib
import re
import threading
from typing import Literal

HERE = pathlib.Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8771"))
ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}   # the local dev host
THREE = "https://cdn.jsdelivr.net/npm/three@0.186.0/"

Shape = Literal["box", "sphere", "cone", "cylinder", "torus"]
_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}|[a-z]{3,20}")


class Scene:
    """The scene lives on the server; the model and the widget both change it through tools."""

    def __init__(self):
        self.lock = threading.Lock()
        self.objects, self.next, self.version = [], 1, 0
        for obj in [("box", "#8b5a2b", 0, 0.5, 0, 1, "crate"),         # size scales every axis
                    ("sphere", "#d94a4a", 0, 1.3, 0, 0.6, "apple"),      # resting on the crate
                    ("cone", "#4aa84a", 3, 1, -2, 2, "tree")]:
            self.add(*obj)

    def add(self, shape, color, x, y, z, size, label):
        if not _COLOR_RE.fullmatch(color):
            raise ValueError("color must be #rrggbb or a CSS color name")
        clamp = lambda v, lo, hi: max(lo, min(hi, float(v)))     # noqa: E731
        with self.lock:
            obj = {"id": self.next, "shape": shape, "color": color, "x": clamp(x, -20, 20),
                   "y": clamp(y, -5, 20), "z": clamp(z, -20, 20), "size": clamp(size, 0.1, 10),
                   "label": label.strip()[:60]}
            self.objects.append(obj)
            self.next += 1
            self.version += 1
            return obj

    def remove(self, id_):
        with self.lock:
            before = len(self.objects)
            self.objects = [o for o in self.objects if o["id"] != id_]
            self.version += 1
            return len(self.objects) < before

    def clear(self):
        with self.lock:
            self.objects, self.version = [], self.version + 1

    def snapshot(self):
        with self.lock:
            return {"version": self.version, "objects": [dict(o) for o in self.objects]}

    def summary(self):
        objs = self.snapshot()["objects"]
        if not objs:
            return "The scene is empty."
        return f"{len(objs)} objects: " + "; ".join(
            f"#{o['id']} {o['color']} {o['shape']} at ({o['x']:g}, {o['y']:g}, {o['z']:g}) "
            f"size {o['size']:g}" + (f" labeled {o['label']!r}" if o["label"] else "") for o in objs)


CSS = """
:root{color-scheme:light dark}
body{font:13px system-ui;margin:0;padding:8px;color:var(--color-text-primary,#222)}
#view{height:380px;border-radius:8px;overflow:hidden;background:#1d2330}
.bar{display:flex;gap:8px;align-items:center;margin:8px 0}#info{flex:1}
button{font:inherit;cursor:pointer}button:disabled{opacity:.5;cursor:default}
#list{list-style:none;padding:0;margin:0;display:flex;flex-wrap:wrap;gap:4px}
#list button{border:1px solid #8884;border-radius:12px;padding:1px 8px;background:none;color:inherit}
#list button.sel{background:#4a90d9;color:#fff}small{opacity:.7}
"""

BODY = """<div id="view"></div>
<div class="bar"><span id="info">Click an object to select it.</span>
<button type="button" id="del" disabled>Delete selected</button>
<button type="button" id="ask" disabled>Ask Claude about it</button></div>
<ul id="list"></ul><small data-mcp-status>connecting&hellip;</small>"""

VIEWER_JS = r"""
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const $ = id => document.getElementById(id);
const view = $("view");
const renderer = new THREE.WebGLRenderer({antialias: true});
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
view.append(renderer.domElement);
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x1d2330);
const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 200);
camera.position.set(6, 5, 8);
const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 1, 0);
controls.enableDamping = true;
scene.add(new THREE.HemisphereLight(0xffffff, 0x445566, 1.4));
const sun = new THREE.DirectionalLight(0xffffff, 1.6);
sun.position.set(5, 10, 7);
scene.add(sun);
scene.add(new THREE.GridHelper(20, 20, 0x667788, 0x334455));
const group = new THREE.Group();
scene.add(group);

const geometry = shape => ({
  sphere: () => new THREE.SphereGeometry(0.5, 32, 16),
  cone: () => new THREE.ConeGeometry(0.5, 1, 32),
  cylinder: () => new THREE.CylinderGeometry(0.5, 0.5, 1, 32),
  torus: () => new THREE.TorusGeometry(0.4, 0.15, 16, 48),
}[shape] || (() => new THREE.BoxGeometry(1, 1, 1)))();

let version = -1, objects = [], selected = null;
const describe = o => `a ${o.color} ${o.shape} at (${o.x}, ${o.y}, ${o.z}), size ${o.size}`;

function build(snap) {
  for (const m of [...group.children]) { m.geometry.dispose(); m.material.dispose(); group.remove(m); }
  objects = snap.objects;
  for (const o of objects) {
    const m = new THREE.Mesh(geometry(o.shape), new THREE.MeshStandardMaterial({color: o.color, roughness: 0.5}));
    m.position.set(o.x, o.y, o.z);
    m.scale.setScalar(o.size);
    m.userData.id = o.id;
    group.add(m);
  }
  const list = $("list");
  list.replaceChildren(...objects.map(o => {             // DOM APIs only: labels are untrusted text
    const li = document.createElement("li"), b = document.createElement("button");
    b.type = "button"; b.dataset.id = o.id;
    b.textContent = `#${o.id} ${o.label || o.shape}`;
    b.addEventListener("click", () => select(o.id));
    li.append(b);
    return li;
  }));
  if (selected !== null && !objects.some(o => o.id === selected)) selected = null;
  highlight();
}

function highlight() {
  for (const m of group.children) m.material.emissive.setHex(m.userData.id === selected ? 0x444444 : 0);
  for (const b of $("list").querySelectorAll("button")) b.classList.toggle("sel", Number(b.dataset.id) === selected);
  const o = objects.find(o => o.id === selected);
  $("info").textContent = o ? `#${o.id} ${o.label ? o.label + ": " : ""}${describe(o)}` : "Click an object to select it.";
  $("del").disabled = $("ask").disabled = !o;
}

function select(id) {
  selected = id;
  highlight();
  const o = objects.find(o => o.id === id);
  // A snapshot of what the user is looking at; the label (possibly user-written) stays in the data.
  mcp.setContext(o ? `3D scene widget: the user has selected object #${o.id}, ${describe(o)}; ` +
                     `the scene has ${objects.length} objects.`
                   : `3D scene widget: nothing selected; the scene has ${objects.length} objects.`,
                 {selected: o || null, objectCount: objects.length});
}

let down = null;
const ray = new THREE.Raycaster(), ptr = new THREE.Vector2();
renderer.domElement.addEventListener("pointerdown", e => { down = [e.clientX, e.clientY]; });
renderer.domElement.addEventListener("pointerup", e => {   // a click, not the end of an orbit drag
  if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return;
  const r = renderer.domElement.getBoundingClientRect();
  ptr.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(ptr, camera);
  const hit = ray.intersectObjects(group.children)[0];
  select(hit ? hit.object.userData.id : null);
});

$("del").addEventListener("click", async () => {
  if (selected === null) return;
  await mcp.callTool("scene_remove", {id: selected});         // the change arrives on the channel
  select(null);
});
$("ask").addEventListener("click", () => {
  const o = objects.find(o => o.id === selected);
  if (o) mcp.say(`In the 3D scene I selected object #${o.id}${o.label ? ` ("${o.label}")` : ""}, ${describe(o)}. ` +
                 "What would you add next to it? Go ahead and add it.");
});

function resize() {
  const w = view.clientWidth, h = view.clientHeight;
  renderer.setSize(w, h);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(view);
resize();
renderer.setAnimationLoop(() => { controls.update(); renderer.render(scene, camera); });

// The server pushes a snapshot on connect and after every change.
const feed = mcp.channel("scene");
feed.onmessage = e => {
  const snap = JSON.parse(e.data);
  if (snap.version !== version) { version = snap.version; build(snap); }
  mcp.status(`${objects.length} objects, scene version ${version} - pushed ${new Date().toLocaleTimeString()}`);
};
feed.onclose = e => mcp.status(`disconnected (${e.code}${e.reason ? ": " + e.reason : ""})`);
"""  # noqa: E501


def scene_mcp():
    from micromcp import MCP, Widget

    mcp, scene = MCP("hm-3d", "0.1.0"), Scene()
    viewer = Widget("scene3d", title="3D scene", border=True, styles=CSS, body=BODY,
                    imports={"three": THREE + "build/three.module.js",
                             "three/addons/": THREE + "examples/jsm/"},
                    modules=[VIEWER_JS])
    feed = mcp.channel("scene")               # open widgets get every change pushed to them

    @feed.on_connect
    def joined(conn):
        conn.send_json(scene.snapshot())

    def changed():
        feed.broadcast_json(scene.snapshot())

    @mcp.tool(widget=viewer, title="Show the 3D scene", read_only=True)
    def show_scene() -> str:
        """Show the shared 3D scene to the user. They can orbit it, select objects (you will be
        told what they select), delete objects, and ask you about them."""
        return scene.summary()

    @mcp.tool(title="Add a 3D object")
    def scene_add(shape: Shape, color: str = "#4a90d9", x: float = 0, y: float = 0.5,
                  z: float = 0, size: float = 1, label: str = "") -> str:
        """Add an object to the shared 3D scene; an open scene widget shows it within seconds.

        Args:
            shape: box, sphere, cone, cylinder, or torus.
            color: #rrggbb or a CSS color name.
            x: Left-right position; the scene spans about -10..10.
            y: Height of the object's center; the ground is y=0, so a size-1 box sits at y=0.5.
            z: Front-back position.
            size: Scale in scene units (a size-1 box is 1 unit on each side).
            label: A short name shown in the widget.
        """
        obj = scene.add(shape, color, x, y, z, size, label)
        changed()
        return f"Added object #{obj['id']}. {scene.summary()}"

    @mcp.tool(visibility=["model", "app"], title="Remove a 3D object")
    def scene_remove(id: int) -> str:
        """Remove an object from the shared 3D scene by its id (the widget's Delete button too)."""
        removed = scene.remove(id)
        changed()
        return ("Removed." if removed else f"No object #{id}.") + " " + scene.summary()

    @mcp.tool(destructive=True, title="Clear the 3D scene")
    def scene_clear() -> str:
        """Remove every object from the shared 3D scene."""
        scene.clear()
        changed()
        return "The scene is empty."

    return mcp


def build(devhost: bool = False):
    from micromcp import ASGIServer
    inner = ASGIServer(scene_mcp(), path="/mcp", allowed_origins=ORIGINS)
    dev = (HERE / "devhost.html").read_bytes() if devhost else None

    async def app(scope, receive, send):
        if dev and scope["type"] == "http" and scope["path"] == "/devhost":
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/html; charset=utf-8")]})
            return await send({"type": "http.response.body", "body": dev})
        if scope["type"] == "http" and os.environ.get("WIRE_LOG"):
            h = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            print(f"WIRE {scope['method']} {scope['path']} method={h.get('mcp-method', '-')} "
                  f"name={h.get('mcp-name', '-')} ua={h.get('user-agent', '-')[:30]!r}", flush=True)
        return await inner(scope, receive, send)
    return app


try:
    import modal
except ImportError:
    modal = None

if modal is not None:
    image = (modal.Image.debian_slim(python_version="3.12")
             .env({"PYTHONPATH": "/root/src", "WIRE_LOG": "1"})
             .add_local_dir(HERE.parent / "src", "/root/src"))
    app = modal.App("micromcp-3d")

    @app.function(image=image, max_containers=1, timeout=600)
    @modal.concurrent(max_inputs=50)
    @modal.asgi_app()
    def web():
        return build()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(build(devhost=True), host="127.0.0.1", port=PORT)
