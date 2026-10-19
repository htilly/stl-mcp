// The 3D stage shared by the viewer and the editor: renderer, z-up camera, orbit controls, lights,
// theme colours from the --stage tokens in style.css, a grid, framing, and pointer picking.
import * as THREE from "three";
import { OrbitControls } from "/static/vendor/three/OrbitControls.js";
import { STLLoader } from "/static/vendor/three/STLLoader.js";

export const VIEWS = { iso: [1, -1.2, 0.9], front: [0, -1, 0.0001], top: [0, -0.0001, 1], right: [1, 0, 0.0001] };

export function createStage(el, { onFrame } = {}) {
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  el.prepend(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 100000);
  camera.up.set(0, 0, 1);
  camera.position.set(90, -110, 80);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.12;
  scene.add(new THREE.HemisphereLight(0xffffff, 0x8890a0, 1.6));
  const key = new THREE.DirectionalLight(0xffffff, 1.8);
  camera.add(key); key.position.set(0.5, 1, 0.4);
  scene.add(camera);

  let grid = null, gridFor = null;
  const colors = () => {
    const css = getComputedStyle(document.documentElement);
    const c = (name) => new THREE.Color(css.getPropertyValue(name).trim());
    return { bg: c("--stage"), major: c("--stage-grid-major"), minor: c("--stage-grid") };
  };

  // A grid on z = 0 under the box (or a fixed 400 mm one), with a step that suits its size.
  function buildGrid(box = gridFor) {
    gridFor = box;
    const col = colors();
    scene.background = col.bg;
    if (grid) { scene.remove(grid); grid.geometry.dispose(); grid = null; }
    let span = 400, step = 10, cx = 0, cy = 0;
    if (box && !box.isEmpty()) {
      const size = box.getSize(new THREE.Vector3());
      span = Math.max(size.x, size.y) * 1.6 + 20;
      step = span > 600 ? 50 : span > 200 ? 20 : 10;
      const c = box.getCenter(new THREE.Vector3());
      cx = Math.round(c.x / step) * step; cy = Math.round(c.y / step) * step;
    }
    const n = Math.ceil(span / step);
    grid = new THREE.GridHelper(n * step, n, col.major, col.minor);
    grid.rotation.x = Math.PI / 2;
    grid.position.set(cx, cy, 0);
    scene.add(grid);
  }
  buildGrid(null);
  // theme.js fires this on every theme change, including the system flipping while on auto
  window.addEventListener("themechange", () => buildGrid());

  function resize() {
    const w = el.clientWidth, h = el.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  new ResizeObserver(resize).observe(el);
  renderer.setAnimationLoop(() => { controls.update(); renderer.render(scene, camera); onFrame?.(); });

  // Frame a box: from a preset direction, or "keep" the current one.
  function frame(box, kind = "keep", margin = 1.15) {
    if (!box || box.isEmpty()) return;
    const c = box.getCenter(new THREE.Vector3());
    const r = box.getBoundingSphere(new THREE.Sphere()).radius || 10;
    const d = r / Math.sin((camera.fov * Math.PI) / 360) * margin;
    const dir = kind === "keep" ? camera.position.clone().sub(controls.target) : new THREE.Vector3(...(VIEWS[kind] || VIEWS.iso));
    camera.position.copy(c).addScaledVector(dir.normalize(), d);
    camera.near = d / 100; camera.far = d * 100; camera.updateProjectionMatrix();
    controls.target.copy(c);
    controls.update();
  }

  const raycaster = new THREE.Raycaster();
  function ray(ev) {
    const r = renderer.domElement.getBoundingClientRect();
    raycaster.setFromCamera(new THREE.Vector2(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1), camera);
    return raycaster;
  }

  // Calls cb(event) for a click on the canvas: a press and release without dragging (orbiting).
  function onClick(cb, skip = () => false) {
    let down = null;
    renderer.domElement.addEventListener("pointerdown", (e) => { down = skip() ? null : { x: e.clientX, y: e.clientY }; });
    renderer.domElement.addEventListener("pointerup", (e) => {
      if (down && Math.hypot(e.clientX - down.x, e.clientY - down.y) <= 4) cb(e);
      down = null;
    });
  }

  // Screen position (CSS px within the canvas) of a world point, or null when behind the camera.
  function project(v) {
    const p = v.clone().project(camera);
    if (p.z > 1) return null;
    const r = renderer.domElement.getBoundingClientRect();
    return { x: (p.x + 1) / 2 * r.width, y: (1 - p.y) / 2 * r.height };
  }

  resize();
  return { THREE, renderer, scene, camera, controls, buildGrid, frame, ray, onClick, project, canvas: renderer.domElement };
}

export const edgesFor = (geom, color = 0x1b2a41, opacity = 0.45, angle = 30) =>
  new THREE.LineSegments(geom.attributes.position ? new THREE.EdgesGeometry(geom, angle) : new THREE.BufferGeometry(),
    new THREE.LineBasicMaterial({ color, transparent: true, opacity }));

const loader = new STLLoader();
export const parseSTL = (buf) => loader.parse(buf);
export const loadSTL = (url) => loader.loadAsync(url);

// Lay objects out side by side along x with a gap (spread), or all at the origin.
export function spreadX(objects, spread, gap = 10) {
  let x = null;
  for (const obj of objects) {
    obj.position.set(0, 0, 0);
    obj.updateMatrixWorld(true);
    if (!spread || objects.length < 2) continue;
    const b = new THREE.Box3().setFromObject(obj);
    if (b.isEmpty()) continue;
    if (x === null) x = b.min.x;
    obj.position.x = x - b.min.x;
    obj.updateMatrixWorld(true);
    x += b.max.x - b.min.x + gap;
  }
}

export function toast(el, msg, err = false, ms) {
  el.textContent = msg;
  el.classList.toggle("err", err);
  el.classList.add("show");
  clearTimeout(el._h);
  el._h = setTimeout(() => el.classList.remove("show"), ms ?? (err ? 6000 : 3000));
}
