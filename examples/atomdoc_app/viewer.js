// The widget for server.py: three.js renders an atomdoc thin client's store. The client's
// WebSocket is micromcp's mcp.WebSocket, so atomdoc frames travel over the "atomdoc" channel.
// Bundled with atomdoc-ts and zod by build.sh; three.js comes from the page's import map.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { AtomDocClient } from "atomdoc-ts";

// --- three.js view ---------------------------------------------------------------------
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

const geometry = kind => ({
  sphere: () => new THREE.SphereGeometry(0.5, 32, 16),
  cone: () => new THREE.ConeGeometry(0.5, 1, 32),
  cylinder: () => new THREE.CylinderGeometry(0.5, 0.5, 1, 32),
  torus: () => new THREE.TorusGeometry(0.4, 0.15, 16, 48),
}[kind] || (() => new THREE.BoxGeometry(1, 1, 1)))();

// --- atomdoc ---------------------------------------------------------------------------
const client = new AtomDocClient("mcp:atomdoc", {webSocket: mcp.WebSocket});
let objects = [], selected = null, version = 0;
const fmt = v => Number(v).toFixed(2).replace(/\.?0+$/, "");
const describe = o => `a ${o.color} ${o.kind} at (${fmt(o.x)}, ${fmt(o.y)}, ${fmt(o.z)}), size ${fmt(o.size)}`;

function readStore() {
  const store = client.getStore();
  return store.getChildren(store.getRootId(), "shapes").map(id => ({id, ...client.getState(id)}));
}

function build() {
  objects = readStore();
  for (const m of [...group.children]) { m.geometry.dispose(); m.material.dispose(); group.remove(m); }
  for (const o of objects) {
    const m = new THREE.Mesh(geometry(o.kind), new THREE.MeshStandardMaterial({color: o.color, roughness: 0.5}));
    m.position.set(o.x, o.y, o.z);
    m.scale.setScalar(o.size);
    m.userData.id = o.id;
    group.add(m);
  }
  $("list").replaceChildren(...objects.map(o => {           // DOM APIs only: labels are untrusted
    const li = document.createElement("li"), b = document.createElement("button");
    b.type = "button"; b.dataset.id = o.id;
    b.textContent = o.label || o.kind;
    b.addEventListener("click", () => select(o.id));
    li.append(b);
    return li;
  }));
  if (selected !== null && !objects.some(o => o.id === selected)) selected = null;
  highlight();
}

let queued = false;
const schedule = () => { if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; build(); }); } };

function highlight() {
  for (const m of group.children) m.material.emissive.setHex(m.userData.id === selected ? 0x444444 : 0);
  for (const b of $("list").querySelectorAll("button")) b.classList.toggle("sel", b.dataset.id === selected);
  const o = objects.find(o => o.id === selected);
  $("info").textContent = o ? `${o.label ? o.label + ": " : ""}${describe(o)}` : "Click an object to select it.";
  $("del").disabled = $("recolor").disabled = $("ask").disabled = !o;
}

function select(id) {
  selected = id;
  highlight();
  const o = objects.find(o => o.id === id);
  mcp.setContext(o ? `3D scene widget (atomdoc): the user has selected ${o.id}, ${describe(o)}; ` +
                     `the scene has ${objects.length} objects.`
                   : `3D scene widget (atomdoc): nothing selected; ${objects.length} objects.`,
                 {selected: o || null, objectCount: objects.length});
}

let down = null;
const ray = new THREE.Raycaster(), ptr = new THREE.Vector2();
renderer.domElement.addEventListener("pointerdown", e => { down = [e.clientX, e.clientY]; });
renderer.domElement.addEventListener("pointerup", e => {
  if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return;
  const r = renderer.domElement.getBoundingClientRect();
  ptr.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(ptr, camera);
  const hit = ray.intersectObjects(group.children)[0];
  select(hit ? hit.object.userData.id : null);
});

$("del").addEventListener("click", () => { if (selected) client.deleteNode(selected); });
$("recolor").addEventListener("click", () => {
  if (selected) client.setField(selected, "color", "#" + Math.floor(Math.random() * 0xffffff).toString(16).padStart(6, "0"));
});
$("undo").addEventListener("click", () => client.undo());
$("ask").addEventListener("click", () => {
  const o = objects.find(o => o.id === selected);
  if (o) mcp.say(`In the 3D scene I selected ${o.label ? `"${o.label}", ` : ""}${describe(o)} (id ${o.id}). ` +
                 "Add something that goes with it.");
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

client.onConnected(() => { schedule(); mcp.status("connected: atomdoc over mcp.channel"); });
client.onPatch(v => {
  version = v;
  schedule();
  mcp.status(`atomdoc v${version} - patch at ${new Date().toLocaleTimeString()}`);
});
client.onError(err => mcp.status(`atomdoc ${err.code}: ${err.message}`));
client.connect().catch(e => mcp.status("atomdoc connect failed: " + (e?.error?.message || e)));
