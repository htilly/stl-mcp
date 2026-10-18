import * as THREE from "three";
import { OrbitControls } from "/static/vendor/three/OrbitControls.js";
import { TransformControls } from "/static/vendor/three/TransformControls.js";
import { STLLoader } from "/static/vendor/three/STLLoader.js";
import { api, subscribe, esc, warnHtml, fmt } from "/static/common.js";
import { parse, serialize, DEFAULT_SIZE } from "/static/spec.js";

const id = decodeURIComponent(location.pathname.split("/").pop());
const $ = (s) => document.getElementById(s);
const SOLID = 0x4f8cff, HOLE = 0xff9a3c, LOCKED = 0x9aa4b2;

let model = null, baseVersion = null;
let objects = [];           // editor objects (see spec.js); .mesh is the three.js mesh
let vars = {};
let sel = [];               // selected editor objects (shift/⌘-click adds more)
let dirty = false;
const undoStack = [], redoStack = [];

// ------------------------------------------------------------ three.js
const stage = $("stage");
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
stage.prepend(renderer.domElement);
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 100000);
camera.up.set(0, 0, 1);
camera.position.set(90, -110, 80);
const orbit = new OrbitControls(camera, renderer.domElement);
orbit.enableDamping = true;
orbit.dampingFactor = 0.12;
scene.add(new THREE.HemisphereLight(0xffffff, 0x8890a0, 1.6));
const key = new THREE.DirectionalLight(0xffffff, 1.8);
camera.add(key); key.position.set(0.5, 1, 0.4);
scene.add(camera);

const css = () => getComputedStyle(document.documentElement);
function theme() {
  scene.background = new THREE.Color(css().getPropertyValue("--stage").trim());
  if (grid) scene.remove(grid);
  grid = new THREE.GridHelper(400, 40, new THREE.Color(css().getPropertyValue("--stage-grid-major").trim()),
    new THREE.Color(css().getPropertyValue("--stage-grid").trim()));
  grid.rotation.x = Math.PI / 2;
  scene.add(grid);
}
let grid = null;
theme();
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", theme);
const plane = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);   // the workplane, z = 0

function resize() {
  const w = stage.clientWidth, h = stage.clientHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(stage);
renderer.setAnimationLoop(() => { orbit.update(); renderer.render(scene, camera); });

const tc = new TransformControls(camera, renderer.domElement);
tc.addEventListener("dragging-changed", (e) => { orbit.enabled = !e.value; });
// With several shapes selected the gizmo sits on a pivot at their centre; moving or rotating the pivot
// moves/rotates them all as a rigid group (each keeps its offset from the pivot).
const pivot = new THREE.Object3D();
scene.add(pivot);
const rel = new Map();
tc.addEventListener("objectChange", () => {
  if (tc.object === pivot) {
    pivot.updateMatrixWorld();
    sel.forEach((o) => {
      new THREE.Matrix4().multiplyMatrices(pivot.matrixWorld, rel.get(o)).decompose(o.mesh.position, o.mesh.quaternion, o.mesh.scale);
      syncFromMesh(o);
    });
    fillInspector();
  } else if (single()) { syncFromMesh(sel[0]); fillInspector(); }
});
tc.addEventListener("mouseDown", () => {
  pushUndo();
  if (tc.object === pivot) {
    pivot.updateMatrixWorld();
    const inv = pivot.matrixWorld.clone().invert();
    sel.forEach((o) => { o.mesh.updateMatrixWorld(); rel.set(o, inv.clone().multiply(o.mesh.matrixWorld)); });
  }
});
tc.addEventListener("mouseUp", () => { snapSizes(); markDirty(); if (sel.length > 1) placePivot(); });
scene.add(tc.getHelper ? tc.getHelper() : tc);

// ------------------------------------------------------------ objects <-> meshes
function geometryFor(shape) {
  // Unit shapes with the axis along z: the mesh scale is the size in mm.
  if (shape === "box") return new THREE.BoxGeometry(1, 1, 1);
  if (shape === "sphere") return new THREE.SphereGeometry(0.5, 40, 24);
  const g = new THREE.CylinderGeometry(shape === "cone" ? 0 : 0.5, 0.5, 1, 48);
  g.rotateX(Math.PI / 2);
  return g;
}

function materialFor(o) {
  const hole = o.hole, locked = o.shape === "locked";
  return new THREE.MeshStandardMaterial({
    color: locked ? LOCKED : hole ? HOLE : SOLID, roughness: 0.55, metalness: 0.05,
    transparent: hole || locked, opacity: hole ? 0.4 : locked ? 0.85 : 1, depthWrite: !hole,
  });
}

function makeMesh(o) {
  const mesh = new THREE.Mesh(o.shape === "locked" ? new THREE.BufferGeometry() : geometryFor(o.shape), materialFor(o));
  mesh.rotation.order = "ZYX";
  mesh.userData.obj = o;
  mesh.add(new THREE.LineSegments(new THREE.EdgesGeometry(mesh.geometry, 30), new THREE.LineBasicMaterial({ color: 0x1b2a41, transparent: true, opacity: 0.45 })));
  o.mesh = mesh;
  syncToMesh(o);
  scene.add(mesh);
  return mesh;
}

function syncToMesh(o) {
  if (o.shape === "locked") return;
  o.mesh.position.set(...o.pos);
  o.mesh.rotation.set(...o.rot.map((d) => THREE.MathUtils.degToRad(d)), "ZYX");
  o.mesh.scale.set(...o.size);
}

const r2 = (v) => Math.round(v * 100) / 100;
function syncFromMesh(o) {
  o.pos = o.mesh.position.toArray().map(r2);
  o.rot = [o.mesh.rotation.x, o.mesh.rotation.y, o.mesh.rotation.z].map((r) => r2(THREE.MathUtils.radToDeg(r)));
  o.size = o.mesh.scale.toArray().map((s) => Math.max(0.1, r2(s)));
}

function snapStep() { return +$("snap").value; }
function snapSizes() {
  const s = snapStep();
  const o = single();
  if (!o || !s || tc.mode !== "scale") return;
  o.size = o.size.map((v) => Math.max(s, Math.round(v / s) * s));
  syncToMesh(o);
  fillInspector();
}

function applySnap() {
  const s = snapStep();
  tc.setTranslationSnap(s || null);
  tc.setRotationSnap(s ? THREE.MathUtils.degToRad(15) : null);
}

function disposeMesh(o) {
  if (!o.mesh) return;
  scene.remove(o.mesh);
  o.mesh.traverse((c) => { c.geometry?.dispose(); c.material?.dispose?.(); });
  o.mesh = null;
}

function setObjects(list) {
  objects.forEach(disposeMesh);
  objects = list;
  objects.forEach(makeMesh);
}

// ------------------------------------------------------------ undo / redo
const snapshot = () => JSON.stringify(objects.map(({ mesh, ...o }) => o));
function pushUndo() {
  undoStack.push(snapshot());
  if (undoStack.length > 100) undoStack.shift();
  redoStack.length = 0;
  refreshButtons();
}
async function restore(json, lockedMeshes) {
  const list = JSON.parse(json);
  const keep = sel.map((o) => objects.indexOf(o)).filter((i) => i >= 0);
  objects.forEach(disposeMesh);
  objects = list;
  objects.forEach(makeMesh);
  if (lockedMeshes) await fillLocked();
  setSelection(keep.map((i) => objects[i]).filter(Boolean));
  markDirty();
}
function undo() {
  if (!undoStack.length) return;
  redoStack.push(snapshot());
  restore(undoStack.pop(), true);
}
function redo() {
  if (!redoStack.length) return;
  undoStack.push(snapshot());
  restore(redoStack.pop(), true);
}

// ------------------------------------------------------------ selection + inspector
const editable = () => sel.filter((o) => o.shape !== "locked");
const single = () => (sel.length === 1 && sel[0].shape !== "locked" ? sel[0] : null);

function placePivot() {
  const c = new THREE.Vector3();
  sel.forEach((o) => c.add(o.mesh.position));
  pivot.position.copy(c.divideScalar(sel.length));
  pivot.rotation.set(0, 0, 0);
  pivot.scale.set(1, 1, 1);
  pivot.updateMatrixWorld();
}

// The gizmo follows the selection: on the shape itself, or on the pivot for a group (no resizing a group).
function attachGizmo() {
  tc.detach();
  if (single()) tc.attach(sel[0].mesh);
  else if (sel.length > 1 && tc.mode !== "scale") { placePivot(); tc.attach(pivot); }
}

function setSelection(list) {
  sel = list.filter((o, i) => objects.includes(o) && list.indexOf(o) === i);
  attachGizmo();
  objects.forEach((x) => {
    const edges = x.mesh.children[0];
    if (edges) edges.material.color.set(sel.includes(x) ? 0xffffff : 0x1b2a41);
  });
  const lockedOnly = sel.length === 1 && sel[0].shape === "locked";
  $("nosel").hidden = sel.length > 0;
  $("sel").hidden = !sel.length || lockedOnly;
  $("lockednote").hidden = !lockedOnly;
  $("seltitle").textContent = sel.length > 1 ? `Selected (${sel.length})` : "Selected";
  $("multinote").hidden = sel.length < 2;
  fillInspector();
  refreshButtons();
}

// Plain click selects one shape; shift/⌘/ctrl-click adds it to or removes it from the selection.
function select(o, toggle = false) {
  if (!o) return setSelection([]);
  if (!toggle) return setSelection([o]);
  if (o.shape === "locked") return;
  const cur = editable();
  setSelection(cur.includes(o) ? cur.filter((x) => x !== o) : [...cur, o]);
}

function fillInspector() {
  const ed = editable();
  if (!ed.length) return;
  document.querySelectorAll("#sel input[data-f]").forEach((el) => {
    const vals = ed.map((o) => o[el.dataset.f][+el.dataset.i]);
    const same = vals.every((v) => v === vals[0]);
    el.value = same ? vals[0] : "";
    el.placeholder = same ? "" : "—";
  });
  $("name").value = ed.length === 1 ? ed[0].name || "" : "";
  $("name").disabled = ed.length > 1;
  $("name").placeholder = ed.length > 1 ? "Several shapes" : "Name";
  const holes = ed.filter((o) => o.hole).length;
  $("hole").checked = holes === ed.length;
  $("hole").indeterminate = holes > 0 && holes < ed.length;
}

document.querySelectorAll("#sel input[data-f]").forEach((el) => {
  el.onchange = () => {
    const ed = editable();
    const v = parseFloat(el.value);
    if (!ed.length || !Number.isFinite(v)) { fillInspector(); return; }
    pushUndo();
    ed.forEach((o) => {
      o[el.dataset.f][+el.dataset.i] = el.dataset.f === "size" ? Math.max(0.1, v) : v;
      syncToMesh(o);
    });
    if (sel.length > 1) placePivot();
    fillInspector();
    markDirty();
  };
});
$("name").onchange = () => { const o = single(); if (o) { pushUndo(); o.name = $("name").value.trim(); markDirty(); } };
$("hole").onchange = () => setHole($("hole").checked);

function setHole(v) {
  const ed = editable();
  if (!ed.length) return;
  pushUndo();
  ed.forEach((o) => {
    o.hole = v;
    o.mesh.material.dispose();
    o.mesh.material = materialFor(o);
  });
  fillInspector();
  markDirty();
}
const toggleHole = () => setHole(!editable().every((o) => o.hole));

function refreshButtons() {
  $("undo").disabled = !undoStack.length;
  $("redo").disabled = !redoStack.length;
  const ed = editable();
  ["holebtn", "drop", "dup"].forEach((b) => { $(b).disabled = !ed.length; });
  $("del").disabled = !sel.length;
  $("holebtn").classList.toggle("on", !!ed.length && ed.every((o) => o.hole));
}

// ------------------------------------------------------------ add / delete / duplicate
function addShape(shape, at) {
  pushUndo();
  const size = [...DEFAULT_SIZE[shape]];
  const o = { shape, size, pos: [at?.x ?? 0, at?.y ?? 0, size[2] / 2], rot: [0, 0, 0], hole: false, name: "" };
  const s = snapStep();
  if (s) { o.pos[0] = Math.round(o.pos[0] / s) * s; o.pos[1] = Math.round(o.pos[1] / s) * s; }
  objects.push(o);
  makeMesh(o);
  select(o);
  markDirty();
}

function removeSelected() {
  if (!sel.length) return;
  pushUndo();
  const gone = sel;
  select(null);
  gone.forEach((o) => { objects.splice(objects.indexOf(o), 1); disposeMesh(o); });
  markDirty();
}

function duplicateSelected() {
  const ed = editable();
  if (!ed.length) return;
  pushUndo();
  const step = snapStep() || 5;
  const copies = ed.map((src) => {
    const { mesh, ...rest } = src;
    const o = JSON.parse(JSON.stringify(rest));
    o.pos[0] += step * 2;
    objects.push(o);
    makeMesh(o);
    return o;
  });
  setSelection(copies);
  markDirty();
}

function dropSelected() {
  const ed = editable();
  if (!ed.length) return;
  pushUndo();
  ed.forEach((o) => {
    o.mesh.updateMatrixWorld();
    const b = new THREE.Box3().setFromObject(o.mesh);
    o.pos[2] = r2(o.pos[2] - b.min.z);
    syncToMesh(o);
  });
  if (sel.length > 1) placePivot();
  fillInspector();
  markDirty();
}

document.querySelectorAll(".shape-btn").forEach((b) => {
  b.onclick = () => addShape(b.dataset.shape);
  b.ondragstart = (e) => e.dataTransfer.setData("text/plain", b.dataset.shape);
});
const rc = new THREE.Raycaster();
function planePoint(ev) {
  const r = renderer.domElement.getBoundingClientRect();
  rc.setFromCamera(new THREE.Vector2(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1), camera);
  return rc.ray.intersectPlane(plane, new THREE.Vector3());
}
renderer.domElement.ondragover = (e) => e.preventDefault();
renderer.domElement.ondrop = (e) => {
  e.preventDefault();
  const shape = e.dataTransfer.getData("text/plain");
  if (shape in DEFAULT_SIZE) addShape(shape, planePoint(e));
};

// click to select (a click, not an orbit drag or a gizmo drag)
let down = null;
renderer.domElement.addEventListener("pointerdown", (e) => { down = { x: e.clientX, y: e.clientY, over: tc.axis !== null }; });
renderer.domElement.addEventListener("pointerup", (e) => {
  if (!down || down.over || Math.hypot(e.clientX - down.x, e.clientY - down.y) > 4) return;
  const r = renderer.domElement.getBoundingClientRect();
  rc.setFromCamera(new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1), camera);
  const hit = rc.intersectObjects(objects.map((o) => o.mesh), false)[0];
  const additive = e.shiftKey || e.metaKey || e.ctrlKey;
  if (hit) select(hit.object.userData.obj, additive);
  else if (!additive) select(null);
});

// ------------------------------------------------------------ toolbar
function setMode(m) {
  tc.setMode(m);
  attachGizmo();
  document.querySelectorAll("[data-mode]").forEach((b) => b.classList.toggle("on", b.dataset.mode === m));
}
document.querySelectorAll("[data-mode]").forEach((b) => { b.onclick = () => setMode(b.dataset.mode); });
$("snap").onchange = applySnap;
$("undo").onclick = undo;
$("redo").onclick = redo;
$("holebtn").onclick = toggleHole;
$("drop").onclick = dropSelected;
$("dup").onclick = duplicateSelected;
$("del").onclick = removeSelected;
$("fit").onclick = fit;
applySnap();

function fit() {
  const b = new THREE.Box3();
  objects.forEach((o) => b.expandByObject(o.mesh));
  if (b.isEmpty()) return;
  const c = b.getCenter(new THREE.Vector3());
  const r = b.getBoundingSphere(new THREE.Sphere()).radius || 10;
  const d = r / Math.sin((camera.fov * Math.PI) / 360) * 1.3;
  const dir = camera.position.clone().sub(orbit.target).normalize();
  camera.position.copy(c).addScaledVector(dir, d);
  camera.near = d / 100; camera.far = d * 100; camera.updateProjectionMatrix();
  orbit.target.copy(c);
}

addEventListener("keydown", (e) => {
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
  const mod = e.metaKey || e.ctrlKey;
  if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); save(); }
  else if (mod && e.key.toLowerCase() === "z") { e.preventDefault(); e.shiftKey ? redo() : undo(); }
  else if (mod && e.key.toLowerCase() === "a") { e.preventDefault(); setSelection(editable().length === objects.filter((o) => o.shape !== "locked").length ? sel : objects.filter((o) => o.shape !== "locked")); }
  else if (mod && e.key.toLowerCase() === "d") { e.preventDefault(); duplicateSelected(); }
  else if (!mod && e.key === "w") setMode("translate");
  else if (!mod && e.key === "e") setMode("rotate");
  else if (!mod && e.key === "r") setMode("scale");
  else if (!mod && e.key === "h") toggleHole();
  else if (!mod && e.key === "d") dropSelected();
  else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); removeSelected(); }
  else if (e.key === "Escape") select(null);
});

// ------------------------------------------------------------ load / save
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => t.classList.remove("show"), 3500);
}

function markDirty(v = true) {
  dirty = v;
  $("dirty").textContent = dirty ? "Unsaved changes" : "";
  refreshButtons();
}
addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });

async function fillLocked() {
  const lockedObj = objects.find((o) => o.shape === "locked");
  if (!lockedObj) return;
  const cur = model.versions.find((v) => v.version === model.current);
  const part = cur && Object.values(cur.parts)[0];
  if (!part) return;
  const geom = await new STLLoader().loadAsync(part.url);
  lockedObj.mesh.geometry.dispose();
  lockedObj.mesh.geometry = geom;
  lockedObj.mesh.remove(lockedObj.mesh.children[0]);
  lockedObj.mesh.add(new THREE.LineSegments(new THREE.EdgesGeometry(geom, 30), new THREE.LineBasicMaterial({ color: 0x1b2a41, transparent: true, opacity: 0.45 })));
}

function showInfo() {
  const cur = model.versions.find((v) => v.version === model.current);
  const part = cur && Object.values(cur.parts)[0];
  $("info").innerHTML = `Version ${model.current}` + (part ? ` · ${esc(fmt(part.extents_mm))}` : "");
  $("warns").innerHTML = cur ? Object.values(cur.parts).map(warnHtml).join("") : "";
  const f = part && `/files/${model.id}/${part.file}`;
  $("stl").href = f || "#";
  $("stl").download = `${model.id}.stl`;
}

async function load() {
  $("loading").hidden = false;
  try {
    model = await api(`/api/models/${encodeURIComponent(id)}`);
  } catch (e) {
    $("loading").textContent = e.message;
    return;
  }
  const cur = model.versions.find((v) => v.version === model.current);
  document.title = `${model.name} · editor`;
  $("crumb").textContent = model.name;
  $("viewlink").href = `/m/${encodeURIComponent(id)}`;
  if (cur.kind !== "csg") {
    $("loading").innerHTML = `The editor works on shape recipes. This model is a <b>${esc(cur.kind)}</b>; open it in the <a href="/m/${encodeURIComponent(id)}">viewer</a> and ask Claude to change it.`;
    return;
  }
  const parsed = parse(cur.params);
  if (parsed.unsupported) {
    $("loading").innerHTML = `This model has several parts, which the editor doesn't handle yet. Use the <a href="/m/${encodeURIComponent(id)}">viewer</a>.`;
    return;
  }
  vars = parsed.vars;
  baseVersion = model.current;
  undoStack.length = redoStack.length = 0;
  select(null);
  setObjects(parsed.objects);
  await fillLocked();
  showInfo();
  markDirty(false);
  $("banner").hidden = true;
  $("loading").hidden = true;
  fit();
  refreshButtons();
}

let saving = false;
async function save() {
  if (saving) return;
  let spec;
  try { spec = serialize(objects, vars); } catch (e) { toast(e.message); return; }
  if (baseVersion !== null) {
    const latest = await api(`/api/models/${encodeURIComponent(id)}`).catch(() => null);
    if (latest && latest.current !== baseVersion && !confirm("A newer version was saved meanwhile (by Claude or another tab). Overwrite it with your edits?")) return;
  }
  saving = true;
  $("save").disabled = true;
  $("save").textContent = "Saving…";
  try {
    await api(`/api/models/${encodeURIComponent(id)}`, { method: "POST", body: { action: "update", params: spec, note: "web editor" } });
    model = await api(`/api/models/${encodeURIComponent(id)}`);
    baseVersion = model.current;
    showInfo();
    markDirty(false);
    $("banner").hidden = true;
    toast(`Saved as version ${model.current}`);
  } catch (e) {
    toast(`Not saved: ${e.message}`);
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
  if (!latest || latest.current === baseVersion) return;
  if (!dirty) { await load(); toast("Updated to the latest version"); return; }
  $("bannertext").textContent = `Version ${latest.current} was saved elsewhere.`;
  $("banner").hidden = false;
});

load();
