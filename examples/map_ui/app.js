(async () => {
  const $ = (id) => document.getElementById(id);
  const fmt = (n) => Number(n).toFixed(4);

  await mcp.ready;

  // mcp.callTool rejects on a protocol error (bad argument type, unknown tool),
  // it does not merely return {isError}. Never leave one unhandled.
  async function call(name, args) {
    try { return await mcp.callTool(name, args); }
    catch (e) { fail(`${name}: ${e.message || e}`); return null; }
  }
  function fail(msg) {
    const f = document.getElementById("foot");
    f.classList.remove("live");
    f.lastElementChild.textContent = msg;
  }

  const TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
  const ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

  const state = (await call("map_state")) || {};
  let model = state.structuredContent || { markers: [], view: { lat: 0, lon: 0, zoom: 2 }, selected: null };

  const map = L.map("map", { zoomControl: true, attributionControl: true })
    .setView([model.view.lat, model.view.lon], model.view.zoom);

  L.tileLayer(TILES, { maxZoom: 19, attribution: ATTR }).addTo(map);

  const layer = L.layerGroup().addTo(map);
  const pins = new Map();

  function draw() {
    layer.clearLayers();
    pins.clear();
    for (const m of model.markers) {
      const mk = L.marker([m.lat, m.lon], { title: m.label })
        .bindPopup(`<b>${esc(m.label)}</b>${m.note ? "<br>" + esc(m.note) : ""}`)
        .addTo(layer);
      mk.on("click", () => select(m.id));
      pins.set(m.id, mk);
    }
    if (model.selected && pins.has(model.selected)) pins.get(model.selected).openPopup();
    drawList();
    report();
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function drawList() {
    const ul = $("pins");
    ul.textContent = "";
    if (!model.markers.length) {
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = "No markers yet";
      ul.append(li);
      return;
    }
    for (const m of model.markers) {
      const li = document.createElement("li");
      li.setAttribute("aria-selected", String(m.id === model.selected));
      const nm = document.createElement("span");
      nm.className = "nm";
      nm.textContent = m.label;
      const co = document.createElement("span");
      co.className = "co";
      co.textContent = `${fmt(m.lat)}, ${fmt(m.lon)}`;
      li.append(nm, co);
      li.onclick = () => { map.panTo([m.lat, m.lon]); select(m.id); };
      ul.append(li);
    }
    const sel = ul.querySelector('li[aria-selected="true"]');
    if (sel) sel.scrollIntoView({ block: "nearest" });
  }

  async function select(id) {
    model.selected = id;
    drawList();
    if (pins.has(id)) pins.get(id).openPopup();
    const r = await call("map_select", { marker_id: id });
    if (r && r.structuredContent) { model = r.structuredContent; draw(); }
  }

  function report() {
    const c = map.getCenter(), b = map.getBounds();
    const sel = model.markers.find((m) => m.id === model.selected);
    const lines = [
      `Map widget: centre ${fmt(c.lat)}, ${fmt(c.lng)} at zoom ${Math.round(map.getZoom())}.`,
      `Visible box: S ${fmt(b.getSouth())} W ${fmt(b.getWest())} to N ${fmt(b.getNorth())} E ${fmt(b.getEast())}.`,
      `${model.markers.length} marker(s): ${model.markers.map((m) => m.label).join(", ") || "none"}.`,
      sel ? `Selected: ${sel.label} at ${fmt(sel.lat)}, ${fmt(sel.lon)}.` : "Nothing selected.",
    ];
    mcp.setContext(lines.join("\n"), {
      center: { lat: c.lat, lon: c.lng }, zoom: Math.round(map.getZoom()),
      bounds: { south: b.getSouth(), west: b.getWest(), north: b.getNorth(), east: b.getEast() },
      markers: model.markers, selected: model.selected,
    });
    $("where").textContent = `${fmt(c.lat)}, ${fmt(c.lng)}  z${Math.round(map.getZoom())}`;
  }

  let moveTimer = null;
  map.on("moveend zoomend", () => {
    clearTimeout(moveTimer);
    moveTimer = setTimeout(async () => {
      const c = map.getCenter(), b = map.getBounds();
      await call("map_view", {
        lat: c.lat, lon: c.lng, zoom: Math.round(map.getZoom()),   // getZoom() is fractional mid-flight
        south: b.getSouth(), west: b.getWest(), north: b.getNorth(), east: b.getEast(),
      });
      report();
    }, 250);
  });

  map.on("click", async (e) => {
    const r = await call("map_click", { lat: e.latlng.lat, lon: e.latlng.lng });
    if (r && r.structuredContent) { model = r.structuredContent; draw(); }
  });

  const ws = mcp.channel("map");
  ws.onopen = () => $("foot").classList.add("live");
  ws.onclose = () => $("foot").classList.remove("live");
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.view && msg.fly) {
      map.flyTo([msg.view.lat, msg.view.lon], msg.view.zoom, { duration: 0.8 });
    }
    model = msg.state;
    draw();
  };

  draw();
  window.__mapReady = true;
})();
