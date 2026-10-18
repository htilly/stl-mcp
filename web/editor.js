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
let selected = null;
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
tc.addEventListener("objectChange", () => { if (selected) { syncFromMesh(selected); fillInspector(); } });
tc.addEventListener("mouseDown", () => pushUndo());
tc.addEventListener("mouseUp", () => { snapSizes(); markDirty(); });
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
  if (!selected || !s || tc.mode !== "scale") return;
  selected.size = selected.size.map((v) => Math.max(s, Math.round(v / s) * s));
  syncToMesh(selected);
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
  const keep = selected ? objects.indexOf(selected) : -1;
  objects.forEach(disposeMesh);
  objects = list;
  objects.forEach(makeMesh);
  if (lockedMeshes) await fillLocked();
  select(keep >= 0 ? objects[keep] ?? null : null);
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
function select(o) {
  selected = o && objects.includes(o) ? o : null;
  tc.detach();
  if (selected && selected.shape !== "locked") tc.attach(selected.mesh);
  objects.forEach((x) => {
    const edges = x.mesh.children[0];
    if (edges) edges.material.color.set(x === selected ? 0xffffff : 0x1b2a41);
  });
  $("nosel").hidden = !!selected;
  $("sel").hidden = !selected || selected.shape === "locked";
  $("lockednote").hidden = !selected || selected.shape !== "locked";
  fillInspector();
  refreshButtons();
}

function fillInspector() {
  if (!selected || selected.shape === "locked") return;
  document.querySelectorAll("#sel input[data-f]").forEach((el) => { el.value = selected[el.dataset.f][+el.dataset.i]; });
  $("name").value = selected.name || "";
  $("hole").checked = selected.hole;
}

document.querySelectorAll("#sel input[data-f]").forEach((el) => {
  el.onchange = () => {
    if (!selected) return;
    pushUndo();
    const v = parseFloat(el.value);
    if (Number.isFinite(v)) selected[el.dataset.f][+el.dataset.i] = el.dataset.f === "size" ? Math.max(0.1, v) : v;
    syncToMesh(selected);
    fillInspector();
    markDirty();
  };
});
$("name").onchange = () => { if (selected) { pushUndo(); selected.name = $("name").value.trim(); markDirty(); } };
$("hole").onchange = () => setHole($("hole").checked);

function setHole(v) {
  if (!selected || selected.shape === "locked") return;
  pushUndo();
  selected.hole = v;
  selected.mesh.material.dispose();
  selected.mesh.material = materialFor(selected);
  fillInspector();
  markDirty();
}

function refreshButtons() {
  $("undo").disabled = !undoStack.length;
  $("redo").disabled = !redoStack.length;
  const editable = selected && selected.shape !== "locked";
  ["holebtn", "drop", "dup"].forEach((b) => { $(b).disabled = !editable; });
  $("del").disabled = !selected;
  $("holebtn").classList.toggle("on", !!editable && selected.hole);
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
  if (!selected) return;
  pushUndo();
  const o = selected;
  select(null);
  objects.splice(objects.indexOf(o), 1);
  disposeMesh(o);
  markDirty();
}

function duplicateSelected() {
  if (!selected || selected.shape === "locked") return;
  pushUndo();
  const { mesh, ...rest } = selected;
  const o = JSON.parse(JSON.stringify(rest));
  const step = snapStep() || 5;
  o.pos[0] += step * 2;
  objects.push(o);
  makeMesh(o);
  select(o);
  markDirty();
}

function dropSelected() {
  if (!selected || selected.shape === "locked") return;
  pushUndo();
  const b = new THREE.Box3().setFromObject(selected.mesh);
  selected.pos[2] = r2(selected.pos[2] - b.min.z);
  syncToMesh(selected);
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
  select(hit ? hit.object.userData.obj : null);
});

// ------------------------------------------------------------ toolbar
function setMode(m) {
  tc.setMode(m);
  document.querySelectorAll("[data-mode]").forEach((b) => b.classList.toggle("on", b.dataset.mode === m));
}
document.querySelectorAll("[data-mode]").forEach((b) => { b.onclick = () => setMode(b.dataset.mode); });
$("snap").onchange = applySnap;
$("undo").onclick = undo;
$("redo").onclick = redo;
$("holebtn").onclick = () => selected && setHole(!selected.hole);
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
  else if (mod && e.key.toLowerCase() === "d") { e.preventDefault(); duplicateSelected(); }
  else if (!mod && e.key === "w") setMode("translate");
  else if (!mod && e.key === "e") setMode("rotate");
  else if (!mod && e.key === "r") setMode("scale");
  else if (!mod && e.key === "h") selected && setHole(!selected.hole);
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
