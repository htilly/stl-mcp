// Measuring in the 3D view, shared by the viewer and the editor.
//  - Dimensions (auto): the size of every visible part as dimension lines, and its round holes and
//    bosses (from the server's measurement, /api/models/<id>/measure) as rings with Ø labels.
//  - Measure (manual): distance between two points, gap or angle between two faces, wall
//    thickness behind a face, and the angle at a corner. Points snap to corners and hole centres,
//    measurements stay until removed and follow their parts when the parts are spread.
// The page routes clicks here while a tool is on (click), calls update() every frame and reset()
// when it shows other geometry.
import * as THREE from "three";
import { esc, icon } from "/static/common.js";
import * as core from "/static/measure-core.js";

// the lines; the labels take the same colours from style.css (--measure, --measure-hole ...)
const COLORS = { item: 0xdb2777, dim: 0x3b82f6, hole: 0xff8a3c, boss: 0x10b981 };
const SNAP_PX = 10, CENTER_PX = 14;

export function createMeasure(st, {
  root,              // the stage element: gets .measuring while a tool is on
  parts,             // () => [{ name, obj }]: each part's frame (an Object3D holding its meshes)
  meshes,            // () => the meshes a click can hit
  box,               // optional (part) => THREE.Box3: a part's world bounds (default: its frame's)
  features,          // optional () => Promise<{ part: measurement }> for the version shown
  toast = () => {},
  list,              // optional: the element for the list of measurements
  summary,           // optional: the element for the holes and bosses of each part
  onComment,         // optional ({ part, point, text }) => void: "Ask Claude" on a measurement
  onChange,          // optional () => void: the tool or Dimensions was switched
}) {
  const v3 = (a) => new THREE.Vector3(...a);
  const arr = (v) => [v.x, v.y, v.z];
  const layer = Object.assign(document.createElement("div"), { className: "mlayer" });
  const bar = Object.assign(document.createElement("div"), { className: "mbar", hidden: true });
  root.append(layer, bar);
  const group = new THREE.Group();
  group.renderOrder = 10;
  st.scene.add(group);
  const autoGroup = new THREE.Group(), itemGroup = new THREE.Group(), liveGroup = new THREE.Group();
  group.add(autoGroup, itemGroup, liveGroup);

  let tool = null, auto = false, pending = [], items = [], nextId = 1;
  let feats = null, featsPromise = null, featsError = "", featsSeq = 0;
  let hover = null, sig = "", autoTags = [], itemTags = [], flashKey = null;
  const hoverTag = tag("item live");

  // ---------------------------------------------------------------- points
  // A point is kept in its part's own coordinates, so it moves with the part (spread).
  const partOf = (name) => parts().find((p) => p.name === name)?.obj;
  function world(p) {
    const obj = p.part && partOf(p.part);
    if (!obj) return p.local.clone();
    obj.updateMatrixWorld(true);
    return p.local.clone().applyMatrix4(obj.matrixWorld);
  }
  function worldNormal(p) {
    const obj = p.part && partOf(p.part);
    return obj && p.normal ? p.normal.clone().transformDirection(obj.matrixWorld) : p.normal?.clone();
  }
  function pointAt(part, w, nWorld) {
    const obj = part && partOf(part);
    if (!obj) return { part: null, local: w.clone(), normal: nWorld?.clone() || null };
    obj.updateMatrixWorld(true);
    const inv = obj.matrixWorld.clone().invert();
    return { part, local: w.clone().applyMatrix4(inv), normal: nWorld ? nWorld.clone().transformDirection(inv) : null };
  }

  function partName(o) {
    const frames = parts();
    for (; o; o = o.parent) { const p = frames.find((f) => f.obj === o); if (p) return p.name; }
    return null;
  }

  // What a click at this pointer position lands on: the mesh point, snapped to a hole centre or a
  // corner of the triangle under the pointer when they are close on screen (not with Alt).
  function pick(ev, snap) {
    const hit = st.ray(ev).intersectObjects(meshes(), false)[0];
    const r = st.canvas.getBoundingClientRect();
    const cur = { x: ev.clientX - r.left, y: ev.clientY - r.top };
    const near = (w, px) => { const s = st.project(w); return s && Math.hypot(s.x - cur.x, s.y - cur.y) < px ? Math.hypot(s.x - cur.x, s.y - cur.y) : null; };
    if (snap && !ev.altKey) {
      let best = null;
      for (const c of centers()) {
        const d = near(c.world, CENTER_PX);
        if (d !== null && (!best || d < best.d)) best = { d, ...c };
      }
      if (best) return { point: pointAt(best.part, best.world, best.normal), world: best.world, snapped: best.label };
    }
    if (!hit) return null;
    const part = partName(hit.object);
    const n = hit.face ? hit.face.normal.clone().transformDirection(hit.object.matrixWorld).normalize() : null;
    let w = hit.point.clone(), snapped = "";
    const pos = hit.object.geometry?.attributes?.position;
    if (snap && !ev.altKey && hit.face && pos) {
      let bd = SNAP_PX;
      for (const i of [hit.face.a, hit.face.b, hit.face.c]) {
        const vtx = new THREE.Vector3().fromBufferAttribute(pos, i).applyMatrix4(hit.object.matrixWorld);
        const d = near(vtx, bd);
        if (d !== null) { bd = d; w = vtx; snapped = "corner"; }
      }
    }
    return { point: pointAt(part, w, n), world: w, normal: n, snapped, hit };
  }

  // Hole and boss centres (both ends) of the visible parts, to snap to.
  function centers() {
    if (!feats) return [];
    const out = [];
    for (const { name, obj } of parts()) {
      if (!obj.visible || !feats[name]) continue;
      obj.updateMatrixWorld(true);
      for (const f of feats[name].features || []) {
        const axis = v3(f.axis).transformDirection(obj.matrixWorld);
        for (const e of f.ends) {
          out.push({ part: name, world: v3(e).applyMatrix4(obj.matrixWorld), normal: axis,
            label: `centre ${core.featureLabel(f)}` });
        }
      }
    }
    return out;
  }

  // Material thickness straight behind a face: the first surface a ray into the part meets.
  function thicknessAt(w, n) {
    const ray = new THREE.Raycaster(w.clone().addScaledVector(n, -1e-3), n.clone().negate());
    const ms = meshes();
    const sides = ms.map((m) => m.material.side);
    ms.forEach((m) => { m.material.side = THREE.DoubleSide; });
    try {
      const hit = ray.intersectObjects(ms, false).find((h) => h.distance > 1e-3);
      return hit ? hit.point : null;
    } finally {
      ms.forEach((m, i) => { m.material.side = sides[i]; });
    }
  }

  // ---------------------------------------------------------------- drawing
  function tag(cls) {
    const el = document.createElement("div");
    el.className = `mtag ${cls}`;
    el.hidden = true;
    layer.append(el);
    return el;
  }
  function setTag(el, text, detail = "", title = "") {
    el.innerHTML = esc(text) + (detail ? `<span class="d">${esc(detail)}</span>` : "");
    el.title = title || (detail ? `${text} (${detail})` : text);
    el._w = 0;
  }
  const lineMat = (color, opacity = 1) => new THREE.LineBasicMaterial({ color, depthTest: false, transparent: true, opacity });
  function line(g, pts, color, opacity) {
    const l = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), lineMat(color, opacity));
    l.renderOrder = 10;
    g.add(l);
    return l;
  }
  function dots(g, pts, color, size = 7) {
    const d = new THREE.Points(new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.PointsMaterial({ color, size, sizeAttenuation: false, depthTest: false, transparent: true }));
    d.renderOrder = 11;
    g.add(d);
  }
  function ring(g, c, axis, r, color, sides = 48) {
    const u = new THREE.Vector3().crossVectors(axis, Math.abs(axis.x) < 0.9 ? new THREE.Vector3(1, 0, 0) : new THREE.Vector3(0, 1, 0)).normalize();
    const v = new THREE.Vector3().crossVectors(axis, u);
    const pts = [];
    for (let i = 0; i <= sides; i++) {
      const a = (i / sides) * Math.PI * 2;
      pts.push(c.clone().addScaledVector(u, Math.cos(a) * r).addScaledVector(v, Math.sin(a) * r));
    }
    line(g, pts, color, 0.95);
  }
  function clearGroup(g) {
    g.traverse((o) => { o.geometry?.dispose(); o.material?.dispose(); });
    g.clear();
  }

  // Dimensions: per visible part its size, and its holes and bosses.
  function drawAuto() {
    clearGroup(autoGroup);
    autoTags.forEach((t) => t.el.remove());
    autoTags = [];
    if (!auto) return;
    for (const { name, obj } of parts()) {
      if (!obj.visible) continue;
      const b = box ? box(name, obj) : new THREE.Box3().setFromObject(obj);
      if (!b || b.isEmpty()) continue;
      const size = b.getSize(new THREE.Vector3());
      const gap = Math.max(3, 0.08 * Math.max(size.x, size.y, size.z));
      for (const d of core.boxDimensions(arr(b.min), arr(b.max), gap)) {
        const a = v3(d.a), e = v3(d.b);
        line(autoGroup, [a, e], COLORS.dim, 0.9);
        d.ext.forEach(([p, q]) => line(autoGroup, [v3(p), v3(q)], COLORS.dim, 0.45));
        dots(autoGroup, [a, e], COLORS.dim, 6);
        const el = tag("dim");
        setTag(el, `${core.mm(d.value)}`, "", `${name}: ${d.axis.toUpperCase()} ${core.mm(d.value)} mm`);
        autoTags.push({ el, at: a.clone().add(e).multiplyScalar(0.5), prio: 1 });
      }
      const fs = feats?.[name]?.features || [];
      obj.updateMatrixWorld(true);
      fs.forEach((f) => {
        const axis = v3(f.axis).transformDirection(obj.matrixWorld);
        const color = f.kind === "hole" ? COLORS.hole : COLORS.boss;
        const ends = f.ends.map((e) => v3(e).applyMatrix4(obj.matrixWorld));
        ends.forEach((c) => ring(autoGroup, c, axis, f.diameter / 2, color, f.sides || 48));
        if (ends.length > 1) line(autoGroup, [ends[0], ends[ends.length - 1]], color, 0.35);
        const el = tag(f.kind);
        setTag(el, core.featureLabel(f), "", `${name}: ${f.kind} Ø${core.mm(f.diameter)}${f.depth != null ? `, ${f.through ? "through, " : ""}${core.mm(f.depth)} mm ${f.kind === "hole" ? "deep" : "high"}` : ""}`
          + (f.across_flats ? `, ${core.mm(f.across_flats)} mm across flats` : "")
          + ` at ${f.center.map(core.mm).join(", ")}`);
        el.dataset.key = keyOf(name, f);
        autoTags.push({ el, at: ends[ends.length - 1], prio: 2 + 1 / (1 + f.diameter) });
      });
    }
    autoTags.sort((a, b) => a.prio - b.prio);
  }
  const keyOf = (part, f) => `${part}|${f.kind}|${f.diameter}|${f.depth}|${f.through}|${f.sides || ""}`;

  function drawItems() {
    clearGroup(itemGroup);
    itemTags.forEach((t) => t.el.remove());
    itemTags = [];
    items.forEach((m, i) => {
      const w = m.pts.map(world);
      if (m.kind === "thickness" || m.kind === "distance" || m.kind === "faces") line(itemGroup, [w[0], w[1]], COLORS.item);
      if (m.kind === "angle") line(itemGroup, w, COLORS.item);
      dots(itemGroup, w, COLORS.item);
      const el = tag("item");
      setTag(el, `${i + 1}  ${m.text}`, m.detail);
      const at = m.kind === "angle" ? w[1] : w[0].clone().add(w[w.length - 1]).multiplyScalar(0.5);
      itemTags.push({ el, at, keep: true });
    });
  }

  function drawLive() {
    clearGroup(liveGroup);
    hoverTag.hidden = true;
    hoverTag._at = null;
    const w = pending.map(world);
    if (hover) {
      dots(liveGroup, [hover.world], COLORS.item, 9);
      const all = [...w, hover.world];
      if (w.length && (tool === "distance" || tool === "angle")) line(liveGroup, all, COLORS.item, 0.7);
      const live = liveText(all);
      if (live || hover.snapped) { setTag(hoverTag, live || hover.snapped, live ? hover.snapped : ""); hoverTag._at = hover.world; }
    }
    if (w.length) dots(liveGroup, w, COLORS.item);
  }

  function liveText(all) {
    if (tool === "distance" && all.length === 2) return core.distance(arr(all[0]), arr(all[1])).text;
    if (tool === "angle" && all.length === 3) return core.deg(core.angleAt(arr(all[0]), arr(all[1]), arr(all[2])));
    return "";
  }

  // ---------------------------------------------------------------- tools
  function click(ev) {
    if (!tool) return;
    const snap = tool === "distance" || tool === "angle";
    const p = pick(ev, snap);
    if (!p) return;
    if ((tool === "faces" || tool === "thickness") && !p.normal) return;
    if (tool === "thickness") {
      const back = thicknessAt(p.hit.point, p.normal);
      if (!back) { toast("No material behind that point."); return; }
      const d = back.distanceTo(p.hit.point);
      add("thickness", [pointAt(p.point.part, p.hit.point, p.normal), pointAt(p.point.part, back, null)], `${core.mm(d)} mm`, "wall thickness");
      return;
    }
    pending.push(p.point);
    if (pending.length < core.TOOLS[tool].points) { drawLive(); return; }
    const pts = pending, w = pts.map(world);
    pending = [];
    if (tool === "distance") {
      const d = core.distance(arr(w[0]), arr(w[1]));
      add("distance", pts, d.text, d.detail);
    } else if (tool === "angle") {
      add("angle", pts, core.deg(core.angleAt(arr(w[0]), arr(w[1]), arr(w[2]))), "angle");
    } else if (tool === "faces") {
      const n1 = worldNormal(pts[0]), n2 = worldNormal(pts[1]);
      const f = core.faces(arr(w[0]), arr(n1), arr(w[1]), arr(n2));
      // parallel: draw the gap square to the faces, from the second point onto the first plane
      if (f.parallel) {
        const foot = w[1].clone().addScaledVector(n1, -w[1].clone().sub(w[0]).dot(n1));
        pts[0] = pointAt(pts[0].part, foot, n1);
      }
      add("faces", pts, f.text, f.detail);
    }
  }

  function add(kind, pts, text, detail) {
    items.push({ id: nextId++, kind, label: core.TOOLS[kind].label, pts, text, detail });
    drawItems(); drawLive(); renderList();
  }

  function move(ev) {
    if (!tool) return;
    const p = pick(ev, tool === "distance" || tool === "angle");
    hover = p ? { world: p.normal && (tool === "faces" || tool === "thickness") ? p.hit.point : p.world, snapped: p.snapped } : null;
    drawLive();
  }
  let moveReq = 0;
  st.canvas.addEventListener("pointermove", (ev) => {
    if (!tool || moveReq) return;
    moveReq = requestAnimationFrame(() => { moveReq = 0; move(ev); });
  });
  st.canvas.addEventListener("pointerleave", () => { hover = null; if (tool) drawLive(); });

  function setTool(t) {
    tool = t && core.TOOLS[t] ? t : null;
    pending = []; hover = null;
    root.classList.toggle("measuring", !!tool);
    if (tool) loadFeatures();
    renderBar(); drawLive();
    onChange?.();
  }

  function renderBar() {
    bar.hidden = !tool;
    if (!tool) return;
    bar.innerHTML = `<div class="seg" role="group" aria-label="Measure">${Object.entries(core.TOOLS).map(([k, t]) =>
      `<button data-t="${k}" class="${k === tool ? "on" : ""}" title="${esc(t.hint)} (${t.key})">${t.label}</button>`).join("")}</div>
      <span class="hint">${esc(core.TOOLS[tool].hint)}</span>`;
    bar.querySelectorAll("[data-t]").forEach((b) => { b.onclick = () => setTool(b.dataset.t); });
  }

  // Esc drops the points placed so far, then leaves the tool; 1-4 pick a tool; Delete drops the last one.
  function key(e) {
    if (!tool) return false;
    if (e.key === "Escape") { if (pending.length) { pending = []; drawLive(); } else setTool(null); return true; }
    const t = Object.keys(core.TOOLS).find((k) => core.TOOLS[k].key === e.key);
    if (t) { setTool(t); return true; }
    if (e.key === "Delete" || e.key === "Backspace") { if (items.length) remove(items[items.length - 1].id); return true; }
    return false;
  }

  function remove(id) {
    items = items.filter((m) => m.id !== id);
    drawItems(); renderList();
  }
  function clear() {
    items = []; pending = [];
    drawItems(); drawLive(); renderList();
  }

  // ---------------------------------------------------------------- dimensions and features
  function setAuto(on) {
    auto = on;
    if (auto) loadFeatures();
    drawAuto();
    onChange?.();
  }

  function loadFeatures() {
    if (!features || featsPromise) return featsPromise;
    const seq = ++featsSeq;   // a reset while loading: the answer is for geometry no longer shown
    featsPromise = features().then((r) => { if (seq === featsSeq) { feats = r || {}; featsError = ""; } },
      (e) => { if (seq === featsSeq) { feats = {}; featsError = e.message || String(e); } })
      .then(() => { if (seq === featsSeq) { drawAuto(); renderSummary(); } });
    return featsPromise;
  }

  // New geometry: measurements on the old one no longer mean anything.
  function reset({ keepItems = false } = {}) {
    feats = null; featsPromise = null; featsError = ""; featsSeq++;
    if (!keepItems) { items = []; pending = []; }
    sig = "";
    drawAuto(); drawItems(); drawLive(); renderList(); renderSummary();
    if (summary || auto || tool) loadFeatures();
  }

  // ---------------------------------------------------------------- side panel
  function renderList() {
    if (!list) return;
    list.innerHTML = items.length ? `
      <ol class="mlist">${items.map((m, i) => `
        <li data-id="${m.id}"><span class="badge">${i + 1}</span>
          <span><b>${esc(m.text)}</b> <span class="hint">${esc(m.detail || m.label.toLowerCase())}</span></span>
          <span class="acts">${onComment ? `<button class="icon quiet sm" data-a="comment" title="Leave this measurement as a comment for Claude">${icon("comment", "sm")}</button>` : ""}
          <button class="icon quiet sm" data-a="remove" title="Remove">${icon("close", "sm")}</button></span></li>`).join("")}</ol>
      <div class="row"><button class="sm" data-a="copy">${icon("copy", "sm")}Copy</button><button class="sm" data-a="clear">Clear</button></div>`
      : `<p class="hint">Choose <b>Measure</b> and click the model: distances, gaps between faces, wall thickness and angles.</p>`;
    list.querySelectorAll("li").forEach((li) => {
      const m = items.find((x) => x.id === +li.dataset.id);
      li.querySelector('[data-a="remove"]').onclick = () => remove(m.id);
      const c = li.querySelector('[data-a="comment"]');
      if (c) c.onclick = () => {
        const p = m.pts.find((q) => q.part) || m.pts[0];
        onComment({ part: p.part, point: p.local, text: `Measured ${m.text} (${m.detail || m.label.toLowerCase()}) here: ` });
      };
    });
    const copy = list.querySelector('[data-a="copy"]');
    if (copy) copy.onclick = () => { navigator.clipboard?.writeText(core.asText(items)); toast("Measurements copied"); };
    const cl = list.querySelector('[data-a="clear"]');
    if (cl) cl.onclick = clear;
  }

  function renderSummary() {
    if (!summary) return;
    if (!feats) { summary.innerHTML = features ? `<p class="hint">Measuring…</p>` : ""; return; }
    if (featsError) { summary.innerHTML = `<p class="hint">Could not measure: ${esc(featsError)}</p>`; return; }
    const names = Object.keys(feats);
    summary.innerHTML = names.map((n) => {
      const m = feats[n];
      const lines = (m.summary || []).map((s) => {
        const k = `${n}|${s.kind}|${s.diameter}|${s.depth}|${s.through}|${s.sides || ""}`;
        return `<li data-k="${esc(k)}" class="${s.kind}" title="Show them in the view">${esc(core.summaryLine(s))}</li>`;
      }).join("");
      return `<div class="mpart">${names.length > 1 ? `<b>${esc(n)}</b> ` : ""}<span class="hint">${m.extents_mm.map(core.mm).join(" × ")} mm</span>
        ${lines ? `<ul class="msum">${lines}</ul>` : `<p class="hint">No round holes or bosses found.</p>`}
        ${m.note ? `<p class="hint">${esc(m.note)}</p>` : ""}</div>`;
    }).join("");
    summary.querySelectorAll("li[data-k]").forEach((li) => { li.onclick = () => flash(li.dataset.k); });
  }

  // Point out the features of one summary line: Dimensions on, their labels highlighted.
  function flash(k) {
    if (!auto) setAuto(true);
    flashKey = k;
    clearTimeout(flash._t);
    flash._t = setTimeout(() => { flashKey = null; }, 2200);
  }

  // ---------------------------------------------------------------- every frame
  function update() {
    // redraw when parts move (spread) or are shown or hidden
    const s = parts().map((p) => `${p.name}:${p.obj.visible ? 1 : 0}:${p.obj.position.toArray().map((x) => x.toFixed(3))}`).join("|");
    if (s !== sig) { sig = s; if (auto) drawAuto(); if (items.length) drawItems(); }
    const placed = [];
    const all = [];
    if (tool && hoverTag._at) all.push({ el: hoverTag, at: hoverTag._at, live: true, keep: true });
    itemTags.forEach((t) => all.push(t));
    autoTags.forEach((t) => all.push(t));
    for (const t of all) {
      const sp = t.at && st.project(t.at);
      if (!sp) { placed.push(null); continue; }
      if (!t.el._w) { t.el.hidden = false; t.el._w = t.el.offsetWidth; t.el._h = t.el.offsetHeight; }
      placed.push({ x: sp.x - t.el._w / 2, y: sp.y - t.el._h / 2 - (t.live ? 18 : 0), w: t.el._w, h: t.el._h, keep: t.keep });
    }
    const show = core.cullOverlaps(placed);
    all.forEach((t, i) => {
      const r = placed[i];
      const on = show[i] || (flashKey && t.el.dataset.key === flashKey && r);
      t.el.hidden = !on;
      if (on) t.el.style.transform = `translate(${r.x}px, ${r.y}px)`;
      t.el.classList.toggle("flash", !!flashKey && t.el.dataset.key === flashKey);
    });
    if (!hoverTag._at || !tool) hoverTag.hidden = true;
  }

  renderList();
  return {
    click, key, update, reset, setTool, setAuto, clear, loadFeatures,
    refresh: () => { sig = ""; },     // the geometry changed in place: redraw on the next frame
    get tool() { return tool; },
    get auto() { return auto; },
    get items() { return items; },
  };
}
