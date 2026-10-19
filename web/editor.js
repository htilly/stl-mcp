import * as THREE from "three";
import { TransformControls } from "/static/vendor/three/TransformControls.js";
import { api, subscribe, esc, warnHtml, fmt, PART_COLORS } from "/static/common.js";
import { createStage, edgesFor, parseSTL, spreadX, toast as showToast } from "/static/stage.js";
import { createComments } from "/static/comments.js";
import {
  resolve, resolveVars, isExpr, isOp, kids, partEntries, setParts, indexTree, pointerOf, atPointer, isHole, directHole,
  label, setHole, group, ungroup, ungroupable, moveNode, solidsOf, holesOf, uniquePartName, primitive, foldScale,
  PALETTE, SCHEMA, COMMON, STRUCTURAL, OPS, TRANSFORM_KEYS, parseValue, formatValue, clone,
} from "/static/spec.js";

const id = decodeURIComponent(location.pathname.split("/").pop());
const $ = (s) => document.getElementById(s);
const SOLID = 0x4f8cff, HOLE = 0xff9a3c, GHOST = 0x9aa4b2, ERR = 0xe65a78;
const D2R = THREE.MathUtils.degToRad, R2D = THREE.MathUtils.radToDeg;
const r2 = (v) => Math.round(v * 100) / 100;

let model = null, baseVersion = null;
let spec = null;            // the recipe being edited; nodes are its own objects
let env = {};               // resolved vars
let sel = [];               // selected nodes
let dirty = false, viewResult = false, spread = true;
let showHoles = false;      // false: differences are drawn cut (as built); true: holes drawn as orange ghosts
try { showHoles = localStorage.getItem("editor.showHoles") === "1"; } catch {}
let tool = null;            // null | "measure" | "workplane" | "comment"
const undoStack = [], redoStack = [];
const objOf = new Map();    // node -> THREE.Group (its frame)
const errors = new Map();   // node -> message
let pickables = [];         // meshes a click can hit
let partGroups = [];        // [{name, group}]

// ------------------------------------------------------------ stage
const st = createStage($("stage"), { onFrame: frameOverlay });
const { scene, camera, controls: orbit, canvas } = st;
const world = new THREE.Group();
scene.add(world);
const toast = (msg, err) => showToast($("toast"), msg, err);

async function post(body) {
  try { return await api(`/api/models/${encodeURIComponent(id)}`, { method: "POST", body }); }
  catch (e) { toast(e.message, true); throw e; }
}

// Comments for Claude: pins on the parts, placed in Comment mode, listed in the inspector.
const comments = createComments(st, {
  root: $("stage"),
  parts: () => partGroups.map((p) => ({ name: p.name, obj: p.group })),
  meshes: () => {
    const out = [];
    partGroups.forEach((p) => p.group.traverseVisible((o) => { if (o.isMesh) out.push(o); }));
    return out;
  },
  comments: () => model?.comments || [],
  version: () => baseVersion,
  post, toast,
  list: $("comments"), count: $("ccount"), hint: $("chint"),
});

const tc = new TransformControls(camera, canvas);
tc.addEventListener("dragging-changed", (e) => { orbit.enabled = !e.value; });
const pivot = new THREE.Object3D();
scene.add(pivot);
scene.add(tc.getHelper ? tc.getHelper() : tc);

// ------------------------------------------------------------ transforms (match csg._transform)
// A node's frame: translate · mirror · rotate (X, then Y, then Z) · scale.
const vec3 = (v, d) => (v === undefined ? [d, d, d] : typeof v === "number" ? [v, v, v] : v);

function mirrorMatrix(n) {
  const v = new THREE.Vector3(...n).normalize();
  const m = new THREE.Matrix4();
  m.set(1 - 2 * v.x * v.x, -2 * v.x * v.y, -2 * v.x * v.z, 0,
    -2 * v.y * v.x, 1 - 2 * v.y * v.y, -2 * v.y * v.z, 0,
    -2 * v.z * v.x, -2 * v.z * v.y, 1 - 2 * v.z * v.z, 0, 0, 0, 0, 1);
  return m;
}

// Resolved transform keys of a node, or null when an expression in them fails.
function xformOf(node) {
  const raw = {};
  for (const k of TRANSFORM_KEYS) if (k in node) raw[k] = node[k];
  try { return resolve(raw, env); } catch { return null; }
}

function localMatrix(node) {
  const r = xformOf(node);
  const m = new THREE.Matrix4();
  if (!r) return m;
  const s = vec3(r.scale, 1), rot = vec3(r.rotate, 0), t = vec3(r.translate, 0);
  const q = new THREE.Quaternion().setFromEuler(new THREE.Euler(D2R(rot[0]), D2R(rot[1]), D2R(rot[2]), "ZYX"));
  m.compose(new THREE.Vector3(), q, new THREE.Vector3(...s));
  if (r.mirror) m.premultiply(mirrorMatrix(vec3(r.mirror, 0)));
  m.setPosition(...t);
  return m;
}

// Matrix of a node in its part's frame (ancestors included).
function asmOf(node, idx = indexTree(spec)) {
  const m = new THREE.Matrix4();
  for (let n = node; n && idx.has(n); n = idx.get(n).parent) m.premultiply(localMatrix(n));
  return m;
}
const parentAsm = (node, idx = indexTree(spec)) => {
  const p = idx.get(node)?.parent;
  return p ? asmOf(p, idx) : new THREE.Matrix4();
};

// Write a local matrix into a node's translate/rotate/scale. Components that did not change keep their
// original value (which may be an expression); keys are only added when they are needed.
function writeLocal(node, m, before = xformOf(node) || {}) {
  const lin = m.clone().setPosition(0, 0, 0);
  if (before.mirror) lin.premultiply(mirrorMatrix(vec3(before.mirror, 0)));
  const pos = new THREE.Vector3(), q = new THREE.Quaternion(), s = new THREE.Vector3();
  lin.decompose(pos, q, s);
  const e = new THREE.Euler().setFromQuaternion(q, "ZYX");
  const t = new THREE.Vector3().setFromMatrixPosition(m);
  put(node, "translate", t.toArray(), vec3(before.translate, 0), 0);
  put(node, "rotate", [R2D(e.x), R2D(e.y), R2D(e.z)], vec3(before.rotate, 0), 0, true);
  put(node, "scale", s.toArray(), vec3(before.scale, 1), 1);
}

function put(node, key, vals, prev, dflt, angle = false) {
  const same = (a, b) => Math.abs(angle ? ((a - b + 540) % 360) - 180 : a - b) < 1e-4;
  if (vals.every((v, i) => same(v, prev[i]))) return;
  const raw = node[key];
  const old = Array.isArray(raw) ? raw : vec3(raw, dflt);
  const out = vals.map((v, i) => (same(v, prev[i]) ? old[i] : r2(v)));
  if (!(key in node) && out.every((v) => v === dflt)) return;
  node[key] = out;
}

// ------------------------------------------------------------ scene from the recipe
const geomCache = new Map();     // request key -> Promise<BufferGeometry>
const lastGeom = new WeakMap();  // node -> last geometry shown (kept while a new one loads)

function strip(node) {
  const n = { ...node };
  for (const k of [...TRANSFORM_KEYS, "hidden", "name"]) delete n[k];
  return n;
}

function serverGeometry(node) {
  const body = { node, vars: spec.vars || {} };
  const key = JSON.stringify(body);
  if (!geomCache.has(key)) {
    geomCache.set(key, fetch(`/api/models/${encodeURIComponent(id)}/mesh`, {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: key,
    }).then(async (r) => {
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`);
      const g = parseSTL(await r.arrayBuffer());
      g.userData.shared = true;   // cached and reused across rebuilds: never disposed
      return g;
    }));
    if (geomCache.size > 300) geomCache.delete(geomCache.keys().next().value);
  }
  return geomCache.get(key);
}

function primGeometry(p) {
  let g;
  if (p.type === "box") g = new THREE.BoxGeometry(...p.size);
  else if (p.type === "sphere") g = new THREE.SphereGeometry(p.r, 48, 24);
  else { g = new THREE.CylinderGeometry(p.rTop, p.rBottom, p.h, 64); g.rotateX(Math.PI / 2); }
  if (p.center) g.translate(...p.center);
  return g;
}

function materialFor(ctx) {
  // Shapes inside a cut difference: invisible (the cut result shows them) until selected, then
  // see-through on top of it so they can be found and moved.
  if (ctx.proxy && !ctx.ghost) return new THREE.MeshStandardMaterial({ color: ctx.hole ? HOLE : ctx.color, roughness: 0.6, transparent: true, opacity: 0.35, depthWrite: false, depthTest: false, visible: false });
  if (ctx.ghost) return new THREE.MeshBasicMaterial({ color: GHOST, transparent: true, opacity: 0.18, depthWrite: false });
  // Holes show through the solids they cut (otherwise a hole inside a block is invisible).
  if (ctx.hole) return new THREE.MeshStandardMaterial({ color: HOLE, roughness: 0.6, transparent: true, opacity: 0.4, depthWrite: false, depthTest: false });
  return new THREE.MeshStandardMaterial({ color: ctx.color, roughness: 0.55, metalness: 0.05, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });
}

function setContent(obj, geom) {
  const mesh = obj.userData.content;
  if (!mesh) return;
  if (!mesh.geometry.userData.shared) mesh.geometry.dispose();
  mesh.geometry = geom;
  mesh.children.forEach((c) => { c.geometry.dispose(); c.material.dispose(); });
  mesh.clear();
  mesh.add(edgesFor(geom));
  refreshHighlight();
}

// Builds a node's frame and what it shows. Unions show their children. Differences show the cut result
// (built by the server) with their children as invisible proxies that still take clicks and appear
// when selected; with "Holes" on they show their children instead, holes ghosted in orange.
// Everything else shows one mesh, drawn here when it is a plain primitive and by the server otherwise,
// with its children as faint ghosts that appear when selected.
function buildNode(node, parent, ctx) {
  const obj = new THREE.Group();
  obj.matrixAutoUpdate = false;
  obj.matrix.copy(localMatrix(node));
  obj.userData.node = node;
  obj.userData.inCut = !!ctx.proxy;
  obj.visible = !node.hidden;
  objOf.set(node, obj);
  parent.add(obj);
  const plainOp = isOp(node) && ["union", "group", "difference"].includes(node.op) && !("array" in node) && !("polar" in node);
  if (plainOp && node.op === "difference" && !showHoles && !ctx.proxy && !ctx.ghost) {
    const mesh = new THREE.Mesh(lastGeom.get(node) || new THREE.BufferGeometry(), materialFor(ctx));
    mesh.userData.node = node;
    mesh.add(edgesFor(mesh.geometry));
    obj.userData.content = mesh;
    obj.add(mesh);
    meshFromServer(node, obj, mesh);
    kids(node).forEach((c, i) => buildNode(c, obj, { ...ctx, proxy: true, hole: ctx.hole || i > 0 }));
    return;
  }
  if (plainOp) {
    kids(node).forEach((c, i) => buildNode(c, obj, { ...ctx, hole: ctx.hole || (node.op === "difference" && i > 0) }));
    return;
  }
  let resolved = null;
  try { resolved = resolve(strip({ ...node, children: undefined }), env); errors.delete(node); }
  catch (e) { errors.set(node, e.message); }
  const prim = !isOp(node) && resolved ? primitive(resolved) : null;
  const geom = prim ? primGeometry(prim) : lastGeom.get(node) || new THREE.BufferGeometry();
  const mesh = new THREE.Mesh(geom, materialFor(ctx));
  mesh.userData.node = node;
  mesh.userData.ghost = !!ctx.ghost;
  mesh.userData.proxy = !!ctx.proxy && !ctx.ghost;
  if ((ctx.hole || ctx.proxy) && !ctx.ghost) mesh.renderOrder = 2;
  mesh.add(edgesFor(geom, 0x1b2a41, ctx.ghost ? 0.25 : 0.45));
  obj.userData.content = mesh;
  obj.add(mesh);
  if (ctx.ghost) mesh.visible = false;
  else pickables.push(mesh);
  if (!prim && (resolved || isOp(node))) meshFromServer(node, obj, mesh);
  if (isOp(node)) kids(node).forEach((c) => buildNode(c, obj, { ...ctx, ghost: true }));
}

function meshFromServer(node, obj, mesh) {
  serverGeometry(strip(node)).then((g) => {
    lastGeom.set(node, g);
    errors.delete(node);
    if (objOf.get(node) === obj) { setContent(obj, g); if (!spreadAt) layout(); refit(false); }
  }, (e) => {
    errors.set(node, e.message);
    if (objOf.get(node) === obj) { mesh.material.visible = true; mesh.material.color.set(ERR); renderTree(); fillInspector(); }
  });
}

function clearWorld() {
  world.traverse((o) => { if (o.isMesh || o.isLineSegments) { if (!o.geometry.userData.shared) o.geometry.dispose(); o.material.dispose(); } });
  world.clear();
  objOf.clear();
  pickables = [];
}

let resultSeq = 0;
function render() {
  try { env = resolveVars(spec.vars || {}); $("varerr").hidden = true; }
  catch (e) { env = {}; $("varerr").textContent = e.message; $("varerr").hidden = false; }
  clearWorld();
  const seq = ++resultSeq;
  partGroups = partEntries(spec).map(([name, node], i) => {
    const g = new THREE.Group();
    g.userData.part = name;
    world.add(g);
    const color = partEntries(spec).length > 1 ? PART_COLORS[i % PART_COLORS.length] : SOLID;
    if (viewResult) showResult(g, node, color, seq);
    else buildNode(node, g, { hole: false, ghost: false, color });
    return { name, group: g };
  });
  layout();
  sel = sel.filter((n) => objOf.has(n));
  attachGizmo();
  refreshHighlight();
  renderTree();
  renderVars();
  fillInspector();
  refreshButtons();
}

function showResult(g, node, color, seq) {
  const mesh = new THREE.Mesh(new THREE.BufferGeometry(), materialFor({ color }));
  g.add(mesh);
  serverGeometry({ ...node, hidden: undefined }).then((geom) => {
    if (seq !== resultSeq) return;
    mesh.geometry = geom;
    mesh.add(edgesFor(geom, 0x000000, 0.35, 25));
    layout();
  }, (e) => toast(`Could not build: ${e.message}`, true));
}

// Spread offsets are worked out once (when every part has its geometry) and then kept, so editing a
// part doesn't re-pack the others around it and moved shapes stay where they were put. They are
// worked out again when parts are added, removed or renamed, on load, and when Spread is toggled.
let spreadAt = null;   // {key, x: Map(part name -> offset)} | null
function layout() {
  const groups = partGroups.map((p) => p.group);
  const key = partGroups.map((p) => p.name).join("\n");
  const on = spread && partGroups.length > 1;
  if (on && spreadAt?.key === key) {
    partGroups.forEach((p) => { p.group.position.set(spreadAt.x.get(p.name), 0, 0); p.group.updateMatrixWorld(true); });
  } else {
    spreadX(groups, on);
    spreadAt = null;
    if (on && groups.every((g) => !bounds([g]).isEmpty())) spreadAt = { key, x: new Map(partGroups.map((p) => [p.name, p.group.position.x])) };
  }
  st.buildGrid(bounds());
  placeWorkplaneGrid();
}

function bounds(objs) {
  const b = new THREE.Box3();
  (objs || [world]).forEach((o) => { o.updateMatrixWorld(true); expandVisible(b, o); });
  return b;
}
// Like Box3.expandByObject, but skips hidden objects, ghosts and empty geometry.
function expandVisible(b, o) {
  if (!o.visible) return;
  if (o.isMesh && !o.userData.ghost && o.geometry.attributes.position) {
    o.geometry.computeBoundingBox();
    if (o.geometry.boundingBox && !o.geometry.boundingBox.isEmpty()) b.union(o.geometry.boundingBox.clone().applyMatrix4(o.matrixWorld));
  }
  o.children.forEach((c) => { if (!c.isLineSegments) expandVisible(b, c); });
}

let fitted = false;
function refit(force) {
  if (!force && fitted) return;
  const b = bounds();
  if (b.isEmpty()) return;
  st.frame(b, force === "iso" || !fitted ? "iso" : "keep", 1.3);
  fitted = true;
  st.buildGrid(b);
}

// ------------------------------------------------------------ selection
function refreshHighlight() {
  const chosen = new Set(), shown = new Set();
  sel.forEach((n) => {
    const obj = objOf.get(n);
    // Selecting a cut difference (or above) highlights its result; shapes inside it show when picked.
    obj?.traverse((o) => { if (o.isMesh) { chosen.add(o); if (obj.userData.inCut) shown.add(o); } });
  });
  world.traverse((o) => {
    if (!o.isMesh || !o.userData.node) return;
    const on = chosen.has(o);
    const edges = o.children[0];
    if (edges) { edges.material.color.set(on ? 0xffffff : 0x1b2a41); edges.material.opacity = on ? 0.9 : o.userData.ghost ? 0.25 : 0.45; }
    if (o.userData.ghost) o.visible = on;
    if (o.userData.proxy) { o.material.visible = shown.has(o); if (edges) edges.visible = shown.has(o); }
    if (o.material.emissive) o.material.emissive.set(on ? 0x223355 : 0x000000);
  });
}

function setSelection(list) {
  sel = list.filter((n, i) => objOf.has(n) && list.indexOf(n) === i);
  attachGizmo();
  refreshHighlight();
  renderTree();
  fillInspector();
  refreshButtons();
}

function select(node, additive = false) {
  if (!node) return setSelection([]);
  if (!additive) return setSelection([node]);
  setSelection(sel.includes(node) ? sel.filter((x) => x !== node) : [...sel, node]);
}

// The gizmo sits on a pivot: at the centre of the selection, turned like the shape when there is one.
// Moving the pivot moves every selected node rigidly; each node's new matrix is written back to its keys.
function attachGizmo() {
  tc.detach();
  if (viewResult || tool || !sel.length) return;
  if (sel.length > 1 && tc.mode === "scale") return;
  placePivot();
  tc.attach(pivot);
}

function placePivot() {
  const b = bounds(sel.map((n) => objOf.get(n)));
  const c = b.isEmpty() ? new THREE.Vector3().setFromMatrixPosition(objOf.get(sel[0]).matrixWorld) : b.getCenter(new THREE.Vector3());
  pivot.position.copy(c);
  pivot.quaternion.identity();
  if (sel.length === 1) {
    const q = new THREE.Quaternion();
    objOf.get(sel[0]).matrixWorld.decompose(new THREE.Vector3(), q, new THREE.Vector3());
    pivot.quaternion.copy(q);
  }
  pivot.scale.set(1, 1, 1);
  pivot.updateMatrixWorld(true);
}

let drag = null;
tc.addEventListener("mouseDown", () => {
  pushUndo();
  pivot.updateMatrixWorld(true);
  const inv = pivot.matrixWorld.clone().invert();
  drag = {
    start: pivot.position.clone(),
    items: sel.map((n) => {
      const obj = objOf.get(n);
      obj.updateMatrixWorld(true);
      return { n, obj, rel: inv.clone().multiply(obj.matrixWorld), parentInv: obj.parent.matrixWorld.clone().invert(), before: xformOf(n) || {}, hadScale: "scale" in n };
    }),
  };
  drag.box = bounds(sel.map((n) => objOf.get(n)));
  drag.others = magnetTargets();
});
tc.addEventListener("objectChange", () => {
  if (!drag) return;
  if (tc.mode === "translate") magnet();
  pivot.updateMatrixWorld(true);
  for (const it of drag.items) {
    const local = it.parentInv.clone().multiply(pivot.matrixWorld.clone().multiply(it.rel));
    writeLocal(it.n, local, it.before);
    it.obj.matrix.copy(localMatrix(it.n));
    it.obj.matrixWorldNeedsUpdate = true;
  }
  fillInspector(true);
});
tc.addEventListener("mouseUp", () => {
  if (!drag) return;
  if (tc.mode === "scale") for (const it of drag.items) foldResize(it);
  drag = null;
  markDirty();
  render();
});

// After a resize, put the size into the shape's own dimensions when that is exact (a 20 mm box made
// twice as wide becomes size [40, 20, 20], not scale [2, 1, 1]); snap them to the grid step.
function foldResize(it) {
  if (it.hadScale || !("scale" in it.n)) return;
  const s = vec3(it.n.scale, 1);
  if (!s.every((v) => typeof v === "number")) return;
  delete it.n.scale;
  if (!foldScale(it.n, s)) { it.n.scale = s; return; }
  const step = snapStep();
  if (!step) return;
  const sn = (v) => (typeof v === "number" ? Math.max(step, Math.round(v / step) * step) : v);
  if (Array.isArray(it.n.size)) it.n.size = it.n.size.map(sn);
  for (const k of ["d", "h"]) if (k in it.n) it.n[k] = sn(it.n[k]);
}

// ------------------------------------------------------------ magnet: snap to other shapes while moving
let altDown = false;
addEventListener("keydown", (e) => { if (e.key === "Alt") altDown = true; });
addEventListener("keyup", (e) => { if (e.key === "Alt") altDown = false; });
addEventListener("blur", () => { altDown = false; });

function magnetTargets() {
  const chosen = new Set();
  sel.forEach((n) => objOf.get(n)?.traverse((o) => chosen.add(o)));
  return pickables.filter((m) => !chosen.has(m) && m.visible).map((m) => bounds([m])).filter((b) => !b.isEmpty());
}

function magnet() {
  if (!$("magnet").classList.contains("on") || altDown || !drag.others.length) return;
  const delta = pivot.position.clone().sub(drag.start);
  const box = drag.box.clone().translate(delta);
  const tol = camera.position.distanceTo(orbit.target) * 0.012;
  for (const ax of ["x", "y", "z"]) {
    const mine = [box.min[ax], (box.min[ax] + box.max[ax]) / 2, box.max[ax]];
    let best = null;
    for (const o of drag.others) {
      for (const v of [o.min[ax], (o.min[ax] + o.max[ax]) / 2, o.max[ax]]) {
        for (const m of mine) {
          const d = v - m;
          if (Math.abs(d) < tol && (best === null || Math.abs(d) < Math.abs(best))) best = d;
        }
      }
    }
    if (ax === "z" && best === null && Math.abs(box.min.z) < tol) best = -box.min.z;
    if (best !== null) pivot.position[ax] += best;
  }
}

// ------------------------------------------------------------ undo / redo
const snapshot = () => JSON.stringify({ spec, sel: sel.map((n) => pointerOf(spec, n)) });
function pushUndo() {
  undoStack.push(snapshot());
  if (undoStack.length > 100) undoStack.shift();
  redoStack.length = 0;
  refreshButtons();
}
function restore(json) {
  const s = JSON.parse(json);
  spec = s.spec;
  sel = s.sel.map((p) => atPointer(spec, p)).filter(Boolean);
  render();
  markDirty();
}
function undo() { if (undoStack.length) { redoStack.push(snapshot()); restore(undoStack.pop()); } }
function redo() { if (redoStack.length) { undoStack.push(snapshot()); restore(redoStack.pop()); } }

// One edit: undo point, change, rebuild. fn may return an error message to show.
function edit(fn) {
  const before = snapshot();
  let err;
  try { err = fn(); } catch (e) { err = e.message; }
  if (err) { const s = JSON.parse(before); spec = s.spec; sel = s.sel.map((p) => atPointer(spec, p)).filter(Boolean); render(); toast(err, true); return false; }
  if (snapshot() === before) return true;   // nothing changed (e.g. a field left without editing it)
  undoStack.push(before);
  if (undoStack.length > 100) undoStack.shift();
  redoStack.length = 0;
  markDirty();
  render();
  return true;
}

// Move a node to another parent without it moving in space.
function relocate(node, target, at) {
  const idx = indexTree(spec);
  const before = asmOf(node, idx), bx = xformOf(node) || {};
  moveNode(spec, node, target, at);
  const idx2 = indexTree(spec);
  writeLocal(node, parentAsm(node, idx2).invert().multiply(before), bx);
}

// ------------------------------------------------------------ outliner
const ICON = { union: "∪", group: "∪", difference: "−", intersection: "∩", hull: "◠", minkowski: "⊕" };
let dragNode = null;
const collapsed = new WeakSet();

function renderTree() {
  const el = $("tree");
  if (!spec) { el.innerHTML = ""; return; }
  const idx = indexTree(spec);
  const multi = partEntries(spec).length > 1 || !!spec.parts;
  const frag = document.createDocumentFragment();
  const row = (node, depth, part, pi) => {
    const wrap = document.createElement("div");
    const r = document.createElement("div");
    r.className = "row-n";
    const hole = isHole(spec, node, idx), err = errors.has(node);
    if (hole) r.classList.add("hole");
    if (node.hidden) r.classList.add("off");
    if (err) r.classList.add("err");
    if (sel.includes(node)) r.classList.add("sel");
    r.style.paddingLeft = `${depth * 12 + 2}px`;
    const ks = kids(node);
    const kindTxt = isOp(node) ? (node.name ? node.op : "") : node.name ? node.shape : "";
    r.innerHTML = `<span class="caret">${ks.length ? (collapsed.has(node) ? "▸" : "▾") : ""}</span>`
      + (depth === 0 && multi ? `<span class="dot" style="background:${PART_COLORS[pi % PART_COLORS.length]}"></span>` : "")
      + `<span class="kind">${isOp(node) ? ICON[node.op] || "?" : hole ? "○" : "■"}</span>`
      + `<span class="lbl" title="${esc(err ? errors.get(node) : "")}">${esc(depth === 0 && multi ? part : label(node))}${kindTxt ? ` <span class="kind">${esc(kindTxt)}</span>` : ""}</span>`
      + (depth === 0 && warnCount(part) ? `<span class="badge" title="Print-check warnings in the last saved version">${warnCount(part)}</span>` : "")
      + (err ? `<span title="${esc(errors.get(node))}">⚠</span>` : "")
      + `<button class="eye" title="${node.hidden ? "Show (include in the build)" : "Hide (leave out of the build)"}">${node.hidden ? "◌" : "●"}</button>`;
    r.querySelector(".caret").onclick = (e) => { e.stopPropagation(); collapsed.has(node) ? collapsed.delete(node) : collapsed.add(node); renderTree(); };
    r.querySelector(".eye").onclick = (e) => { e.stopPropagation(); edit(() => { if (node.hidden) delete node.hidden; else node.hidden = true; }); };
    r.onclick = (e) => select(node, e.shiftKey || e.metaKey || e.ctrlKey);
    r.ondblclick = () => (depth === 0 && multi ? renamePart(part) : $("name").focus());
    r.draggable = depth > 0;
    r.ondragstart = (e) => { dragNode = node; e.dataTransfer.setData("text/x-node", "1"); e.dataTransfer.effectAllowed = "move"; };
    r.ondragend = () => { dragNode = null; };
    r.ondragover = (e) => {
      if (!dragNode || dragNode === node) return;
      e.preventDefault();
      const zone = dropZone(e, r, node, depth);
      r.classList.remove("drop-in", "drop-before", "drop-after");
      r.classList.add(`drop-${zone}`);
    };
    r.ondragleave = () => r.classList.remove("drop-in", "drop-before", "drop-after");
    r.ondrop = (e) => {
      e.preventDefault();
      r.classList.remove("drop-in", "drop-before", "drop-after");
      const n = dragNode;
      dragNode = null;
      if (!n || n === node) return;
      const zone = dropZone(e, r, node, depth);
      edit(() => {
        if (zone === "in") {
          const target = depth === 0 ? solidsOf(spec, part) : node;
          relocate(n, target, kids(target).length);
        } else {
          const { parent, index } = indexTree(spec).get(node);
          relocate(n, parent, zone === "before" ? index : index + 1);
        }
      });
    };
    wrap.appendChild(r);
    if (!collapsed.has(node)) ks.forEach((c) => wrap.appendChild(row(c, depth + 1, part, pi)));
    return wrap;
  };
  partEntries(spec).forEach(([name, node], i) => {
    const w = row(node, 0, name, i);
    w.classList.add("tpart");
    frag.appendChild(w);
  });
  el.replaceChildren(frag);
}

function dropZone(e, r, node, depth) {
  const b = r.getBoundingClientRect(), y = (e.clientY - b.top) / b.height;
  if (depth === 0) return "in";
  if (isOp(node) && y > 0.25 && y < 0.75) return "in";
  return y < 0.5 ? "before" : "after";
}

function warnCount(part) {
  const cur = model?.versions.find((v) => v.version === model.current);
  return cur?.parts?.[part]?.warnings?.length || 0;
}

function renamePart(part) {
  const name = prompt("Part name (the STL file name)", part)?.trim();
  if (!name || name === part) return;
  if (partEntries(spec).some(([k]) => k === name)) return toast("There is already a part with that name.", true);
  edit(() => {
    const e = partEntries(spec).map(([k, v]) => [k === part ? name : k, v]);
    spec.parts = Object.fromEntries(e);
    delete spec.root;
  });
}

$("addpart").onclick = () => {
  const name = prompt("Name of the new part (its own STL file)", uniquePartName(spec, "part"))?.trim();
  if (!name) return;
  if (partEntries(spec).some(([k]) => k === name)) return toast("There is already a part with that name.", true);
  edit(() => {
    const e = partEntries(spec);
    e.push([name, { op: "union", children: [clone(PALETTE.box.node)] }]);
    e.at(-1)[1].children[0].translate = [0, 0, 10];
    spec.parts = Object.fromEntries(e);
    delete spec.root;
    sel = [e.at(-1)[1].children[0]];
  });
};

// ------------------------------------------------------------ inspector
function partOf(node) { return indexTree(spec).get(node)?.part; }
const isPartRoot = (node) => indexTree(spec).get(node)?.parent === null;

function fillInspector(live = false) {
  $("nosel").hidden = sel.length > 0;
  $("sel").hidden = !sel.length;
  $("seltitle").textContent = sel.length > 1 ? `Selected (${sel.length})` : "Selected";
  $("multinote").hidden = sel.length < 2;
  $("alignbox").hidden = sel.length < 2;
  if (!sel.length) return;
  document.querySelectorAll("#xform input").forEach((el) => {
    if (live && document.activeElement === el) return;
    const k = el.dataset.k, i = +el.dataset.i, d = k === "scale" ? 1 : 0;
    const vals = sel.map((n) => (k in n ? (Array.isArray(n[k]) ? n[k][i] : n[k]) : d));
    const same = vals.every((v) => v === vals[0]);
    el.value = same ? formatValue(vals[0]) : "";
    el.placeholder = same ? "" : "—";
    el.classList.toggle("expr", same && isExpr(vals[0]));
    el.title = same && isExpr(vals[0]) ? `= ${r2(safeEval(vals[0]))}` : "";
  });
  if (live) return;
  const one = sel.length === 1 ? sel[0] : null;
  $("name").value = one?.name || "";
  $("name").disabled = !one;
  $("name").placeholder = one ? label(one) : "Several shapes";
  const idx = indexTree(spec);
  $("kind").textContent = one ? describe(one, idx) : "";
  const err = one && errors.get(one);
  $("nodeerr").hidden = !err;
  $("nodeerr").textContent = err || "";
  const direct = sel.map((n) => directHole(spec, n, idx));
  $("hole").checked = direct.every(Boolean);
  $("hole").indeterminate = direct.some(Boolean) && !direct.every(Boolean);
  $("hole").disabled = sel.some((n) => idx.get(n)?.parent === null);
  const hid = sel.map((n) => !!n.hidden);
  $("hidden").checked = hid.every(Boolean);
  $("hidden").indeterminate = hid.some(Boolean) && !hid.every(Boolean);
  renderParams(one);
  renderAlign();
}

function describe(n, idx) {
  const e = idx.get(n);
  const where = e.parent ? `in ${label(e.parent)}` : `part “${e.part}”`;
  const kind = isOp(n) ? `${n.op} of ${kids(n).length}` : n.shape;
  return `${kind} · ${where}${isHole(spec, n, idx) ? " · cuts" : ""}`;
}

function safeEval(v) { try { return resolve(v, env); } catch { return NaN; } }

// Number-or-expression text -> value, with "+=5" / "-=5" relative to the current number.
function fieldValue(text, cur) {
  const t = text.trim();
  const rel = /^([+-])=\s*(.+)$/.exec(t);
  if (rel) {
    const base = typeof cur === "number" ? cur : safeEval(cur);
    const d = Number(rel[2]);
    if (!Number.isFinite(base) || !Number.isFinite(d)) throw new Error("Can't add to an expression.");
    return r2(base + (rel[1] === "+" ? d : -d));
  }
  const v = parseValue(t, cur);
  if (v === undefined) return undefined;
  if (typeof v !== "number" && !isExpr(v)) throw new Error("Enter a number or an expression starting with =");
  return v;
}

document.querySelectorAll("#xform input").forEach((el) => {
  el.onchange = () => {
    const k = el.dataset.k, i = +el.dataset.i, d = k === "scale" ? 1 : 0;
    edit(() => {
      for (const n of sel) {
        const arr = Array.isArray(n[k]) ? [...n[k]] : vec3(n[k], d);
        const v = fieldValue(el.value, arr[i]);
        if (v === undefined) continue;
        arr[i] = v;
        if (arr.every((x) => x === d)) delete n[k]; else n[k] = arr;
      }
    });
  };
  stepper(el);
});

// ↑/↓ on a number field steps it (Shift: ×10, Alt: ×0.1) and applies.
function stepper(el) {
  el.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
    const v = Number(el.value);
    if (el.value.trim() === "" || !Number.isFinite(v)) return;
    e.preventDefault();
    const step = (e.shiftKey ? 10 : e.altKey ? 0.1 : 1) * (e.key === "ArrowUp" ? 1 : -1);
    el.value = String(r2(v + step));
    el.dispatchEvent(new Event("change"));
    requestAnimationFrame(() => el.focus());
  });
}

$("name").onchange = () => {
  const n = sel[0], v = $("name").value.trim();
  if (sel.length === 1) edit(() => { if (v) n.name = v; else delete n.name; });
};
$("hole").onchange = () => setHoles($("hole").checked);
$("hidden").onchange = () => edit(() => { const v = $("hidden").checked; sel.forEach((n) => { if (v) n.hidden = true; else delete n.hidden; }); });

// The parameters of one node: every key it has (known or not) plus an "add" list of known ones.
function renderParams(n) {
  const el = $("params");
  if (!n) { el.innerHTML = ""; return; }
  const schema = isOp(n) ? [] : SCHEMA[n.shape] || [];
  const known = new Map([...schema, ...COMMON].map(([k, d, opts]) => [k, { d, opts }]));
  const keys = Object.keys(n).filter((k) => !STRUCTURAL.has(k));
  const html = [];
  if (isOp(n)) {
    html.push(`<label>op</label><select data-op>${OPS.map((o) => `<option${o === n.op ? " selected" : ""}>${o}</option>`).join("")}</select><span></span>`);
  } else {
    html.push(`<label>shape</label><input value="${esc(n.shape)}" disabled><span></span>`);
  }
  for (const k of keys) {
    const v = n[k], opts = known.get(k)?.opts;
    let input;
    if (typeof v === "boolean") input = `<input type="checkbox" data-p="${esc(k)}"${v ? " checked" : ""} style="width:auto">`;
    else if (opts && typeof v === "string" && !isExpr(v)) input = `<select data-p="${esc(k)}">${[...new Set([...opts, v])].map((o) => `<option${o === v ? " selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
    else if (Array.isArray(v) && v.length <= 3 && v.every((x) => typeof x === "number" || isExpr(x)))
      input = `<div class="multi">${v.map((x, i) => `<input data-p="${esc(k)}" data-i="${i}" value="${esc(formatValue(x))}"${isExpr(x) ? ` class="expr" title="= ${r2(safeEval(x))}"` : ""}>`).join("")}</div>`;
    else if (v && typeof v === "object") input = `<textarea data-p="${esc(k)}" data-json>${esc(JSON.stringify(v))}</textarea>`;
    else input = `<input data-p="${esc(k)}" value="${esc(formatValue(v))}"${isExpr(v) ? ` class="expr" title="= ${r2(safeEval(v))}"` : ""}>`;
    html.push(`<label title="${esc(k)}">${esc(k)}</label>${input}<button class="x" data-rm="${esc(k)}" title="Remove ${esc(k)}">×</button>`);
  }
  const addable = [...known.keys()].filter((k) => !(k in n));
  if (addable.length) html.push(`<label>add</label><select data-add><option value="">parameter…</option>${addable.map((k) => `<option>${esc(k)}</option>`).join("")}</select><span></span>`);
  el.innerHTML = `<div class="prm">${html.join("")}</div>`;
  el.querySelectorAll("[data-p]").forEach((inp) => {
    const k = inp.dataset.p;
    if (inp.tagName === "INPUT" && inp.type !== "checkbox") stepper(inp);
    inp.onchange = () => edit(() => {
      if (inp.type === "checkbox") { n[k] = inp.checked; return; }
      if ("json" in inp.dataset) { n[k] = JSON.parse(inp.value); return; }
      if ("i" in inp.dataset) {
        const arr = [...n[k]];
        const v = fieldValue(inp.value, arr[+inp.dataset.i]);
        if (v === undefined) return "A value is needed.";
        arr[+inp.dataset.i] = v;
        n[k] = arr;
        return;
      }
      const v = parseValue(inp.value, n[k]);
      if (v === undefined) delete n[k]; else n[k] = v;
    });
  });
  el.querySelectorAll("[data-rm]").forEach((b) => { b.onclick = () => edit(() => { delete n[b.dataset.rm]; }); });
  const add = el.querySelector("[data-add]");
  if (add) add.onchange = () => { const k = add.value; if (k) edit(() => { n[k] = clone(known.get(k).d); }); };
  const op = el.querySelector("[data-op]");
  if (op) op.onchange = () => edit(() => { n.op = op.value; });
}

// ------------------------------------------------------------ align / distribute
function renderAlign() {
  const el = $("align");
  if (sel.length < 2) { el.innerHTML = ""; return; }
  el.innerHTML = ["x", "y", "z"].map((a) => `<label>${a.toUpperCase()}</label>`
    + `<button data-a="${a}" data-w="min" title="Align the ${a === "z" ? "bottoms" : "low sides"}">min</button>`
    + `<button data-a="${a}" data-w="mid" title="Align the centres">mid</button>`
    + `<button data-a="${a}" data-w="max" title="Align the ${a === "z" ? "tops" : "high sides"}">max</button>`
    + `<button data-a="${a}" data-w="dist" title="Space evenly (3 or more)"${sel.length < 3 ? " disabled" : ""}>even</button>`).join("");
  el.querySelectorAll("button").forEach((b) => { b.onclick = () => align(b.dataset.a, b.dataset.w); });
}

function shiftWorld(node, delta) {
  const idx = indexTree(spec);
  const m = new THREE.Matrix4().makeTranslation(delta.x, delta.y, delta.z).multiply(asmOf(node, idx));
  writeLocal(node, parentAsm(node, idx).invert().multiply(m));
}

function align(ax, how) {
  const boxes = sel.map((n) => bounds([objOf.get(n)]));
  if (boxes.some((b) => b.isEmpty())) return toast("Some of the selection has no shape to align yet.", true);
  const pick = (b) => (how === "min" ? b.min[ax] : how === "max" ? b.max[ax] : (b.min[ax] + b.max[ax]) / 2);
  edit(() => {
    if (how === "dist") {
      const order = sel.map((n, i) => ({ n, c: (boxes[i].min[ax] + boxes[i].max[ax]) / 2 })).sort((a, b) => a.c - b.c);
      const first = order[0].c, last = order.at(-1).c;
      order.forEach((o, i) => {
        const d = new THREE.Vector3();
        d[ax] = first + ((last - first) * i) / (order.length - 1) - o.c;
        shiftWorld(o.n, d);
      });
      return;
    }
    let target;
    if ($("alignto").value === "key") target = pick(boxes[0]);
    else {
      const all = boxes.reduce((a, b) => a.clone().union(b));
      target = pick(all);
    }
    sel.forEach((n, i) => { const d = new THREE.Vector3(); d[ax] = target - pick(boxes[i]); shiftWorld(n, d); });
  });
}

// ------------------------------------------------------------ vars
function renderVars() {
  const el = $("vars");
  const vars = spec?.vars || {};
  el.innerHTML = Object.entries(vars).map(([k, v]) => `<input data-vn="${esc(k)}" value="${esc(k)}" aria-label="Variable name">`
    + `<input data-vv="${esc(k)}" value="${esc(formatValue(v))}"${isExpr(v) ? ` class="expr" title="= ${r2(env[k])}"` : ""} aria-label="Value of ${esc(k)}">`
    + `<button class="x sm" data-vx="${esc(k)}" title="Remove">×</button>`).join("") || `<span class="hint" style="grid-column:1/-1">No variables.</span>`;
  el.querySelectorAll("[data-vv]").forEach((inp) => {
    stepper(inp);
    inp.onchange = () => edit(() => {
      const v = parseValue(inp.value, spec.vars[inp.dataset.vv]);
      if (v === undefined) return "A variable needs a value.";
      spec.vars[inp.dataset.vv] = v;
    });
  });
  el.querySelectorAll("[data-vn]").forEach((inp) => {
    inp.onchange = () => {
      const old = inp.dataset.vn, name = inp.value.trim();
      if (!/^[A-Za-z_]\w*$/.test(name)) { inp.value = old; return toast("Names are letters, digits and _ (not starting with a digit).", true); }
      if (name in spec.vars) { inp.value = old; return toast("That name is taken.", true); }
      edit(() => {
        spec.vars = Object.fromEntries(Object.entries(spec.vars).map(([k, v]) => [k === old ? name : k, v]));
        renameVar(spec, old, name);
      });
    };
  });
  el.querySelectorAll("[data-vx]").forEach((b) => { b.onclick = () => edit(() => { delete spec.vars[b.dataset.vx]; }); });
}

// Renames a variable in every expression (whole words only).
function renameVar(s, from, to) {
  const re = new RegExp(`\\b${from}\\b`, "g");
  const walk = (v) => {
    if (isExpr(v)) return v.replace(re, to);
    if (Array.isArray(v)) return v.map(walk);
    if (v && typeof v === "object") { for (const k of Object.keys(v)) v[k] = walk(v[k]); return v; }
    return v;
  };
  walk(s);
}

$("addvar").onclick = () => {
  const name = prompt("Variable name (use it as =name in any field)", "w")?.trim();
  if (!name) return;
  if (!/^[A-Za-z_]\w*$/.test(name)) return toast("Names are letters, digits and _ (not starting with a digit).", true);
  if (spec.vars && name in spec.vars) return toast("That name is taken.", true);
  edit(() => { spec.vars = { ...(spec.vars || {}), [name]: 10 }; });
};

// ------------------------------------------------------------ add / delete / duplicate / group
function snapStep() { return +$("snap").value; }
function applySnap() {
  const s = snapStep();
  tc.setTranslationSnap(s || null);
  tc.setRotationSnap(s ? D2R(15) : null);
}

const activePart = () => (sel[0] && partOf(sel[0])) || partEntries(spec)[0]?.[0];

function addShape(kind, at) {
  const p = PALETTE[kind];
  if (!p || !spec) return;
  const node = clone(p.node);
  edit(() => {
    const part = activePart();
    // Where it goes in the world: the drop point, on top of the selection (holes), or the origin.
    let W;
    if (at) W = new THREE.Matrix4().makeTranslation(at.x, at.y, at.z).multiply(workplaneRot());
    else if (p.hole && sel.length) {
      const b = bounds(sel.map((n) => objOf.get(n)));
      const c = b.getCenter(new THREE.Vector3());
      W = new THREE.Matrix4().makeTranslation(c.x, c.y, b.max.z);
    } else W = workplane.clone();
    W.multiply(new THREE.Matrix4().makeTranslation(0, 0, p.lift));
    const container = p.hole ? holesOf(spec, part) : solidsOf(spec, part);
    container.children.push(node);
    const pg = partGroups.find((g) => g.name === part)?.group;
    const partInv = pg ? pg.matrixWorld.clone().invert() : new THREE.Matrix4();
    writeLocal(node, asmOf(container).invert().multiply(partInv.multiply(W)), {});
    sel = [node];
  });
}

function removeSelected() {
  if (!sel.length) return;
  const parts = partEntries(spec);
  const roots = sel.filter(isPartRoot);
  if (roots.length && !confirm(roots.length === parts.length ? "That is everything. Delete it anyway?" : `Delete the part${roots.length > 1 ? "s" : ""} ${roots.map(partOf).join(", ")}?`)) return;
  edit(() => {
    const idx = indexTree(spec);
    const goneParts = new Set(roots.map((n) => idx.get(n).part));
    if (goneParts.size === parts.length) return "A model needs at least one part.";
    for (const n of sel) {
      if (isPartRoot(n)) continue;
      const e = indexTree(spec).get(n);
      if (!e) continue;
      e.parent.children.splice(e.index, 1);
      // An emptied group goes too; an emptied part is an error.
      let p = e.parent;
      while (p && !kids(p).length) {
        const pe = indexTree(spec).get(p);
        if (!pe.parent) return `The part “${pe.part}” would be empty. Delete the part instead.`;
        pe.parent.children.splice(pe.index, 1);
        p = pe.parent;
      }
    }
    if (goneParts.size) setParts(spec, parts.filter(([k]) => !goneParts.has(k)));
    sel = [];
  });
}

function duplicateSelected() {
  const list = sel.filter((n) => !isPartRoot(n));
  if (!list.length) return;
  const step = (snapStep() || 5) * 2;
  edit(() => {
    const copies = [];
    for (const n of list) {
      const e = indexTree(spec).get(n);
      const c = clone(n);
      e.parent.children.splice(e.index + 1, 0, c);
      shiftWorld(c, new THREE.Vector3(step, 0, 0));
      copies.push(c);
    }
    sel = copies;
  });
}

function dropSelected() {
  if (!sel.length) return;
  edit(() => {
    for (const n of sel) {
      const b = bounds([objOf.get(n)]);
      if (!b.isEmpty()) shiftWorld(n, new THREE.Vector3(0, 0, -b.min.z));
    }
  });
}

function setHoles(v) {
  if (!sel.length) return;
  edit(() => {
    for (const n of sel) {
      const before = asmOf(n), bx = xformOf(n) || {};
      const err = setHole(spec, n, v);
      if (err) return err;
      writeLocal(n, parentAsm(n).invert().multiply(before), bx);
    }
  });
}
const toggleHole = () => setHoles(!sel.every((n) => directHole(spec, n)));

function groupSelected(op = "union") {
  if (!sel.length) return;
  edit(() => {
    const idx = indexTree(spec);
    if (sel.some((n) => idx.get(n).parent === null) && sel.length > 1) return "Parts can't be grouped with other shapes.";
    // Bring everything into the first one's group (keeping it in place), then group.
    const host = idx.get(sel[0]).parent;
    if (host) for (const n of sel.slice(1)) if (indexTree(spec).get(n).parent !== host) relocate(n, host, kids(host).length);
    const g = group(spec, sel, op);
    sel = [g];
  });
}

function ungroupSelected() {
  const n = sel.length === 1 ? sel[0] : null;
  if (!n) return toast("Select one group to ungroup.", true);
  const why = ungroupable(n);
  if (why) return toast(why, true);
  edit(() => {
    const G = localMatrix(n);
    for (const c of n.children) writeLocal(c, G.clone().multiply(localMatrix(c)));
    if (isPartRoot(n)) {
      if (n.children.length !== 1) return "A part's top level holds its shapes together; it can only be ungrouped when it has one child.";
      const part = partOf(n);
      setParts(spec, partEntries(spec).map(([k, v]) => [k, k === part ? n.children[0] : v]));
      sel = [n.children[0]];
      return;
    }
    sel = ungroup(spec, n);
  });
}

function nudge(dx, dy, dz) {
  if (!sel.length) return;
  const s = snapStep() || 1;
  edit(() => sel.forEach((n) => shiftWorld(n, new THREE.Vector3(dx * s, dy * s, dz * s))));
}

// ------------------------------------------------------------ palette
const SHAPE_ICONS = {
  box: '<path d="M4 8l8-4 8 4v8l-8 4-8-4zM4 8l8 4 8-4M12 12v8"/>',
  cylinder: '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/>',
  sphere: '<circle cx="12" cy="12" r="8"/><ellipse cx="12" cy="12" rx="8" ry="3"/>',
  cone: '<path d="M12 3L5 18c0 1.7 3.1 3 7 3s7-1.3 7-3z"/>',
  tube: '<ellipse cx="12" cy="6" rx="7" ry="3"/><ellipse cx="12" cy="6" rx="3.5" ry="1.4"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/>',
  torus: '<ellipse cx="12" cy="12" rx="9" ry="5"/><ellipse cx="12" cy="12" rx="3.5" ry="1.6"/>',
  wedge: '<path d="M4 18h16L4 6zM4 18l3 2h16l-3-2M20 18v2"/>',
  text: '<path d="M5 6h14M12 6v13"/>',
};
const GENERIC = '<rect x="5" y="5" width="14" height="14" rx="3"/><circle cx="12" cy="12" r="3"/>';
const BASIC = ["box", "cylinder", "sphere", "cone", "tube", "torus", "wedge", "text"];
function palette() {
  const mk = (k) => `<button class="shape-btn${PALETTE[k].hole ? " cut" : ""}" draggable="true" data-shape="${k}" title="Click, or drag onto the workplane${PALETTE[k].hole ? " (added as a hole)" : ""}"><svg viewBox="0 0 24 24">${SHAPE_ICONS[k] || GENERIC}</svg>${esc(k.replace(/_/g, " "))}</button>`;
  $("shapes").innerHTML = BASIC.map(mk).join("");
  $("templates").innerHTML = Object.keys(PALETTE).filter((k) => !BASIC.includes(k)).map(mk).join("");
  document.querySelectorAll(".shape-btn").forEach((b) => {
    b.onclick = () => addShape(b.dataset.shape);
    b.ondragstart = (e) => e.dataTransfer.setData("text/x-shape", b.dataset.shape);
  });
}
palette();
canvas.ondragover = (e) => e.preventDefault();
canvas.ondrop = (e) => {
  e.preventDefault();
  const shape = e.dataTransfer.getData("text/x-shape");
  if (!(shape in PALETTE)) return;
  const p = st.ray(e).ray.intersectPlane(workplanePlane(), new THREE.Vector3());
  const s = snapStep();
  if (p && s && isFlat()) { p.x = Math.round(p.x / s) * s; p.y = Math.round(p.y / s) * s; }
  addShape(shape, p || undefined);
};

// ------------------------------------------------------------ workplane
let workplane = new THREE.Matrix4();   // world matrix of the plane new shapes are put on
let wpHolder = null;
const isFlat = () => workplane.equals(new THREE.Matrix4());
const workplaneRot = () => new THREE.Matrix4().extractRotation(workplane);
function workplanePlane() {
  const n = new THREE.Vector3(0, 0, 1).applyMatrix4(workplaneRot());
  return new THREE.Plane().setFromNormalAndCoplanarPoint(n, new THREE.Vector3().setFromMatrixPosition(workplane));
}
function placeWorkplaneGrid() {
  if (wpHolder) { scene.remove(wpHolder); wpHolder.children[0].geometry.dispose(); wpHolder = null; }
  if (isFlat()) return;
  const grid = new THREE.GridHelper(100, 20, 0xff9a3c, 0xffc58a);
  grid.rotation.x = Math.PI / 2;
  grid.material.transparent = true; grid.material.opacity = 0.6;
  wpHolder = new THREE.Group();
  wpHolder.add(grid);
  wpHolder.matrixAutoUpdate = false;
  wpHolder.matrix.copy(workplane);
  scene.add(wpHolder);
}
function setWorkplane(hit) {
  if (!hit) { workplane = new THREE.Matrix4(); placeWorkplaneGrid(); return; }
  const n = hit.face.normal.clone().transformDirection(hit.object.matrixWorld).normalize();
  const q = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 0, 1), n);
  const at = hit.point.clone(), s = snapStep();
  if (s && Math.abs(n.z) > 0.999) { at.x = Math.round(at.x / s) * s; at.y = Math.round(at.y / s) * s; }
  workplane = new THREE.Matrix4().compose(at, q, new THREE.Vector3(1, 1, 1));
  placeWorkplaneGrid();
  toast("Workplane set: new shapes go on this face. Esc puts it back on the build plate.");
}

// ------------------------------------------------------------ measure
let mPoints = [], mLine = null, mFace = null;
function clearMeasure() {
  mPoints = []; mFace = null;
  if (mLine) { scene.remove(mLine); mLine.geometry.dispose(); mLine = null; }
  $("mlabel").hidden = true;
}

// Snap a hit to the nearest corner of the face it hit when that is within 8 px.
function snapHit(hit, ev) {
  const pos = hit.object.geometry.attributes.position;
  if (!hit.face || !pos) return hit.point.clone();
  const r = canvas.getBoundingClientRect();
  const cursor = { x: ev.clientX - r.left, y: ev.clientY - r.top };
  let best = hit.point.clone(), bd = 8;
  for (const i of [hit.face.a, hit.face.b, hit.face.c]) {
    const v = new THREE.Vector3().fromBufferAttribute(pos, i).applyMatrix4(hit.object.matrixWorld);
    const s = st.project(v);
    if (s) { const d = Math.hypot(s.x - cursor.x, s.y - cursor.y); if (d < bd) { bd = d; best = v; } }
  }
  return best;
}

function measureClick(ev) {
  const hit = st.ray(ev).intersectObjects(pickables.filter((m) => m.visible), false)[0];
  const p = hit ? snapHit(hit, ev) : st.ray(ev).ray.intersectPlane(new THREE.Plane(new THREE.Vector3(0, 0, 1), 0), new THREE.Vector3());
  if (!p) return;
  if (ev.shiftKey && hit?.face) {
    const n = hit.face.normal.clone().transformDirection(hit.object.matrixWorld).normalize();
    if (!mFace) { clearMeasure(); mFace = { p: hit.point.clone(), n }; toast("Now shift-click the other face."); return; }
    const d = Math.abs(hit.point.clone().sub(mFace.p).dot(mFace.n));
    const foot = hit.point.clone().addScaledVector(mFace.n, -hit.point.clone().sub(mFace.p).dot(mFace.n));
    drawMeasure(foot, hit.point.clone(), `${r2(d)} mm between faces${Math.abs(n.dot(mFace.n)) < 0.999 ? " (not parallel)" : ""}`);
    mFace = null;
    return;
  }
  if (mPoints.length >= 2) clearMeasure();
  mPoints.push(p);
  if (mPoints.length === 2) {
    const [a, b] = mPoints, d = b.clone().sub(a);
    drawMeasure(a, b, `${r2(d.length())} mm  ·  dx ${r2(Math.abs(d.x))}  dy ${r2(Math.abs(d.y))}  dz ${r2(Math.abs(d.z))}`);
  } else drawMeasure(p, p, "");
}

function drawMeasure(a, b, text) {
  if (mLine) { scene.remove(mLine); mLine.geometry.dispose(); }
  const g = new THREE.BufferGeometry().setFromPoints([a, b]);
  mLine = new THREE.Line(g, new THREE.LineBasicMaterial({ color: 0xffd400, depthTest: false }));
  mLine.renderOrder = 10;
  const dots = new THREE.Points(g, new THREE.PointsMaterial({ color: 0xffd400, size: 7, sizeAttenuation: false, depthTest: false }));
  dots.renderOrder = 10;
  mLine.add(dots);
  mLine.userData.mid = a.clone().add(b).multiplyScalar(0.5);
  scene.add(mLine);
  $("mlabel").textContent = text;
  $("mlabel").hidden = !text;
}

function frameOverlay() {
  comments.updatePins();
  if (!mLine || $("mlabel").hidden) return;
  const s = st.project(mLine.userData.mid);
  if (!s) return;
  $("mlabel").style.left = `${s.x}px`;
  $("mlabel").style.top = `${s.y}px`;
}

function setTool(t) {
  tool = tool === t ? null : t;
  $("measure").classList.toggle("on", tool === "measure");
  $("workplane").classList.toggle("on", tool === "workplane");
  $("commentmode").classList.toggle("on", tool === "comment");
  if (tool !== "measure") clearMeasure();
  comments.setMode(tool === "comment");
  $("modenote").hidden = !tool;
  $("modenote").textContent = tool === "measure" ? "Measure: click two points (corners snap). Shift-click two faces for the distance between them. Esc to stop."
    : tool === "workplane" ? "Workplane: click a face to build on it. Esc puts it back on the build plate."
    : tool === "comment" ? "Comment: click the model where something should change. Esc to stop." : "";
  attachGizmo();
}

// ------------------------------------------------------------ clicks in the view
st.onClick((e) => {
  if (tool === "measure") return measureClick(e);
  if (tool === "comment") return comments.click(e);
  const hit = st.ray(e).intersectObjects(pickables.filter((m) => m.visible), false)[0];
  if (tool === "workplane") { if (hit?.face) { setWorkplane(hit); setTool(null); } return; }
  if (viewResult) return;
  const additive = e.shiftKey || e.metaKey || e.ctrlKey;
  if (!hit) { if (!additive) select(null); return; }
  let node = hit.object.userData.node;
  if (e.altKey) {
    // Alt-click: the group around it (again: one level further up).
    const idx = indexTree(spec);
    if (sel.length === 1 && (sel[0] === node || isAncestor(sel[0], node, idx))) node = idx.get(sel[0])?.parent || sel[0];
    else node = idx.get(node)?.parent || node;
  }
  select(node, additive);
}, () => tc.axis !== null && tc.object);

function isAncestor(a, n, idx) { for (let p = idx.get(n)?.parent; p; p = idx.get(p)?.parent) if (p === a) return true; return false; }

// ------------------------------------------------------------ toolbar + keys
function setMode(m) {
  tc.setMode(m);
  attachGizmo();
  document.querySelectorAll("[data-mode]").forEach((b) => b.classList.toggle("on", b.dataset.mode === m));
}
document.querySelectorAll("[data-mode]").forEach((b) => { b.onclick = () => setMode(b.dataset.mode); });
$("snap").onchange = applySnap;
$("magnet").onclick = () => $("magnet").classList.toggle("on");
$("undo").onclick = undo;
$("redo").onclick = redo;
$("holebtn").onclick = toggleHole;
$("drop").onclick = dropSelected;
$("dup").onclick = duplicateSelected;
$("group").onclick = () => groupSelected();
$("ungroup").onclick = ungroupSelected;
$("del").onclick = removeSelected;
$("measure").onclick = () => setTool("measure");
$("workplane").onclick = () => setTool("workplane");
$("commentmode").onclick = () => setTool("comment");
$("fit").onclick = () => refit("iso");
$("spread").onclick = () => { spread = !spread; spreadAt = null; $("spread").classList.toggle("on", spread); layout(); attachGizmo(); };
$("holes").onclick = () => {
  showHoles = !showHoles;
  try { localStorage.setItem("editor.showHoles", showHoles ? "1" : "0"); } catch {}
  render();
};
$("result").onclick = () => {
  viewResult = !viewResult;
  $("result").classList.toggle("on", viewResult);
  $("modenote").hidden = !viewResult && !tool;
  if (viewResult) $("modenote").textContent = "Result: the built model with holes cut. Click Result again to edit.";
  render();
};
applySnap();

function refreshButtons() {
  $("undo").disabled = !undoStack.length;
  $("redo").disabled = !redoStack.length;
  const any = sel.length > 0 && !viewResult;
  ["holebtn", "drop", "dup", "del", "group"].forEach((b) => { $(b).disabled = !any; });
  $("ungroup").disabled = !(any && sel.length === 1 && isOp(sel[0]));
  $("holebtn").classList.toggle("on", any && sel.every((n) => directHole(spec, n)));
  const multi = spec && partEntries(spec).length > 1;
  $("spread").hidden = !multi;
  $("spread").classList.toggle("on", spread);
  $("holes").classList.toggle("on", showHoles);
}

addEventListener("keydown", (e) => {
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || !spec) return;
  const mod = e.metaKey || e.ctrlKey, k = e.key.toLowerCase();
  if (mod && k === "s") { e.preventDefault(); save(); }
  else if (mod && k === "z") { e.preventDefault(); e.shiftKey ? redo() : undo(); }
  else if (mod && k === "a") { e.preventDefault(); setSelection(pickables.filter((m) => m.visible).map((m) => m.userData.node)); }
  else if (mod && k === "d") { e.preventDefault(); duplicateSelected(); }
  else if (mod && k === "g") { e.preventDefault(); e.shiftKey ? ungroupSelected() : groupSelected(); }
  else if (mod) return;
  else if (k === "w") setMode("translate");
  else if (k === "e") setMode("rotate");
  else if (k === "r") setMode("scale");
  else if (k === "h") toggleHole();
  else if (k === "d") dropSelected();
  else if (k === "m") setTool("measure");
  else if (k === "p") setTool("workplane");
  else if (k === "c") setTool("comment");
  else if (e.key === "ArrowLeft") { e.preventDefault(); nudge(-1, 0, 0); }
  else if (e.key === "ArrowRight") { e.preventDefault(); nudge(1, 0, 0); }
  else if (e.key === "ArrowUp") { e.preventDefault(); e.shiftKey ? nudge(0, 0, 1) : nudge(0, 1, 0); }
  else if (e.key === "ArrowDown") { e.preventDefault(); e.shiftKey ? nudge(0, 0, -1) : nudge(0, -1, 0); }
  else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); removeSelected(); }
  else if (e.key === "Escape") {
    if (tool) setTool(tool);
    else if (mPoints.length || mLine) clearMeasure();
    else if (!isFlat()) setWorkplane(null);
    else select(null);
  }
});

// ------------------------------------------------------------ load / save
function markDirty(v = true) {
  dirty = v;
  $("dirty").textContent = dirty ? "Unsaved changes" : "";
  refreshButtons();
}
addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });

function showInfo() {
  const cur = model.versions.find((v) => v.version === model.current);
  const entries = cur ? Object.entries(cur.parts) : [];
  $("info").innerHTML = `Version ${model.current}` + entries.map(([n, p]) => `<br>${entries.length > 1 ? `${esc(n)}: ` : ""}${esc(fmt(p.extents_mm))}`).join("");
  $("warns").innerHTML = entries.map(([, p]) => warnHtml(p)).join("");
  const first = entries[0]?.[1];
  $("stl").href = first ? `/files/${model.id}/${first.file}` : "#";
  $("stl").download = `${model.id}.stl`;
  $("stl").title = entries.length > 1 ? "The STL of the first part of the last saved version (the viewer has all parts)" : "The STL of the last saved version";
}

async function load() {
  $("loading").hidden = false;
  try {
    model = await api(`/api/models/${encodeURIComponent(id)}`);
  } catch (e) {
    $("loading").textContent = e.message;
    return;
  }
  if (!model.can_edit) {  // someone else's public model: the viewer shows it read-only
    location.replace(`/m/${encodeURIComponent(id)}`);
    return;
  }
  const cur = model.versions.find((v) => v.version === model.current);
  document.title = `${model.name} · editor`;
  $("crumb").textContent = model.name;
  $("viewlink").href = `/m/${encodeURIComponent(id)}`;
  if (cur.kind !== "csg") {
    $("loading").innerHTML = `<p>The editor works on shape recipes. This model is a <b>${esc(cur.kind)}</b>; open it in the <a href="/m/${encodeURIComponent(id)}">viewer</a> and ask Claude to change it.</p>`;
    return;
  }
  if (!partEntries(cur.params).length) {
    $("loading").innerHTML = `<p>This recipe has no shapes. Open it in the <a href="/m/${encodeURIComponent(id)}">viewer</a>.</p>`;
    return;
  }
  const keep = spec ? sel.map((n) => pointerOf(spec, n)) : [];
  spec = clone(cur.params);
  baseVersion = model.current;
  undoStack.length = redoStack.length = 0;
  sel = keep.map((p) => atPointer(spec, p)).filter(Boolean);
  spreadAt = null;
  render();
  showInfo();
  comments.render();
  markDirty(false);
  $("banner").hidden = true;
  $("loading").hidden = true;
  refit(!fitted ? "iso" : false);
}

let saving = false;
async function save() {
  if (saving || !spec) return;
  if (baseVersion !== null) {
    const latest = await api(`/api/models/${encodeURIComponent(id)}`).catch(() => null);
    if (latest && latest.current !== baseVersion && !confirm("A newer version was saved meanwhile (by Claude or another tab). Overwrite it with your edits?")) return;
  }
  saving = true;
  $("save").disabled = true;
  $("save").textContent = "Saving…";
  try {
    await api(`/api/models/${encodeURIComponent(id)}`, { method: "POST", body: { action: "update", params: clone(spec), note: "web editor" } });
    model = await api(`/api/models/${encodeURIComponent(id)}`);
    baseVersion = model.current;
    showInfo();
    renderTree();
    markDirty(false);
    $("banner").hidden = true;
    toast(`Saved as version ${model.current}`);
  } catch (e) {
    toast(`Not saved: ${e.message}`, true);
  } finally {
    saving = false;
    $("save").disabled = false;
    $("save").textContent = "Save";
  }
}
$("save").onclick = save;

$("reload").onclick = load;
$("keep").onclick = () => { $("banner").hidden = true; };
subscribe(async (d) => {
  if (saving) return;
  if (d.id && d.id !== id) return;
  const latest = await api(`/api/models/${encodeURIComponent(id)}`).catch(() => null);
  if (!latest) return;
  if (model) { model.comments = latest.comments; comments.render(); }
  if (latest.current === baseVersion) return;
  if (!dirty) { await load(); toast("Updated to the latest version"); return; }
  $("bannertext").textContent = `Version ${latest.current} was saved elsewhere.`;
  $("banner").hidden = false;
});

load();
