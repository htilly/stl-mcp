// Scene <-> CSG recipe, for the web editor. Pure functions, no DOM, no three.js.
//
// An editor object is a centred primitive with a bounding size, a rotation and a position:
//   { shape: "box"|"cylinder"|"sphere"|"cone", size: [x, y, z], pos: [x, y, z], rot: [rx, ry, rz] (deg),
//     hole: bool, name: string }
// A recipe the editor cannot represent (templates, expressions, arrays, hull ...) is kept as one
// "locked" object holding the original root node; it is written back untouched.

export const SHAPES = ["box", "cylinder", "sphere", "cone"];
export const DEFAULT_SIZE = { box: [20, 20, 20], cylinder: [20, 20, 20], sphere: [20, 20, 20], cone: [20, 20, 20] };

const SIMPLE_KEYS = new Set(["shape", "size", "d", "r", "h", "center", "translate", "rotate", "scale", "name"]);
const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const isVec = (v) => Array.isArray(v) && v.length === 3 && v.every(isNum);
const vec = (v, dflt) => (v === undefined ? dflt : isVec(v) ? v : null);
const round = (v) => Math.round(v * 1e4) / 1e4;
const zero = (v) => v.every((x) => x === 0);

// One primitive node -> editor object, or null when it is not something the editor can show.
function parseShape(n) {
  if (!n || typeof n !== "object" || !SHAPES.includes(n.shape)) return null;
  if (!Object.keys(n).every((k) => SIMPLE_KEYS.has(k))) return null;
  if ("name" in n && typeof n.name !== "string") return null;
  const t = vec(n.translate, [0, 0, 0]), rot = vec(n.rotate, [0, 0, 0]), sc = vec(n.scale, [1, 1, 1]);
  if (!t || !rot || !sc) return null;
  const centered = n.center === true;
  let size, pos = [...t];
  if (n.shape === "box") {
    if (!isVec(n.size)) return null;
    size = n.size.map((s, i) => s * sc[i]);
  } else {
    const d = isNum(n.d) ? n.d : isNum(n.r) ? n.r * 2 : null;
    if (d === null) return null;
    const h = n.shape === "sphere" ? d : n.h;
    if (!isNum(h)) return null;
    size = [d * sc[0], d * sc[1], h * sc[2]];
  }
  if (!centered && n.shape !== "sphere") {
    // Corner/base-anchored: only equivalent to a centred shape when nothing rotates or scales about it.
    if (!zero(rot) || !sc.every((s) => s === 1)) return null;
    if (n.shape === "box") pos = [t[0] + size[0] / 2, t[1] + size[1] / 2, t[2] + size[2] / 2];
    else pos = [t[0], t[1], t[2] + size[2] / 2];
  }
  if (size.some((s) => !(s > 0))) return null;
  return { shape: n.shape, size: size.map(round), pos: pos.map(round), rot: [...rot], hole: false, name: n.name || "" };
}

export function parse(spec) {
  const vars = spec?.vars ?? {};
  const locked = (root) => ({ vars, objects: [{ shape: "locked", node: root, name: root?.name || "Imported shape", hole: false }] });
  if (!spec || spec.parts || !spec.root) return { vars, objects: [], unsupported: "parts" };
  const root = spec.root;
  const single = parseShape(root);
  if (single) return { vars, objects: [single] };
  if (root.op === "union" && Array.isArray(root.children) && !hasTransform(root)) {
    const kids = root.children.map(parseShape);
    if (kids.length && kids.every(Boolean)) return { vars, objects: kids };
  }
  if (root.op === "difference" && Array.isArray(root.children) && root.children.length > 1 && !hasTransform(root)) {
    const [base, ...cut] = root.children;
    let solids = parseShape(base) ? [parseShape(base)] : null;
    if (!solids && base.op === "union" && Array.isArray(base.children) && !hasTransform(base)) {
      const k = base.children.map(parseShape);
      if (k.length && k.every(Boolean)) solids = k;
    }
    const holes = cut.map(parseShape);
    if (solids && holes.every(Boolean)) {
      holes.forEach((h) => { h.hole = true; });
      return { vars, objects: [...solids, ...holes] };
    }
  }
  return locked(root);
}

const hasTransform = (n) => ["translate", "rotate", "scale", "mirror", "array", "polar", "hidden", "segments"].some((k) => k in n);

function emit(o) {
  const n = { shape: o.shape, center: true };
  if (o.name) n.name = o.name;
  const [x, y, z] = o.size;
  if (o.shape === "box") n.size = [x, y, z];
  else {
    n.d = x;
    if (o.shape === "sphere") {
      if (y !== x || z !== x) n.scale = [1, round(y / x), round(z / x)];
    } else {
      n.h = z;
      if (y !== x) n.scale = [1, round(y / x), 1];
    }
  }
  if (o.rot.some((r) => r !== 0)) n.rotate = o.rot.map(round);
  n.translate = o.pos.map(round);
  return n;
}

// Editor objects -> recipe. Throws when there is nothing solid to build.
export function serialize(objects, vars = {}) {
  const nodeOf = (o) => (o.shape === "locked" ? o.node : emit(o));
  const solids = objects.filter((o) => !o.hole).map(nodeOf);
  const holes = objects.filter((o) => o.hole).map(nodeOf);
  if (!solids.length) throw new Error("Add at least one solid shape.");
  if (objects.length === 1 && objects[0].shape === "locked") return { vars, root: objects[0].node };
  const body = { op: "union", children: solids };
  const root = holes.length ? { op: "difference", children: [body, ...holes] } : body;
  return { vars, root };
}
