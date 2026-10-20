// The CSG recipe, for the web editor. Pure functions, no DOM, no three.js.
//
// The editor works on the recipe itself: every node is the original JSON object, so keys the editor
// doesn't know (templates, arrays, profiles ...) survive a save untouched. Edits only change the keys
// they are about (translate, a size, the order of children ...).
//
// A recipe is {vars, root: node} or {vars, parts: {name: node}}; a node has "shape" or "op" (+ children).

// ------------------------------------------------------------ expressions (mirror csg.eval_expr)

const FUNCS = {
  min: (...a) => { if (!a.length) throw new Error("min() needs arguments"); return Math.min(...a); },
  max: (...a) => { if (!a.length) throw new Error("max() needs arguments"); return Math.max(...a); },
  abs: Math.abs,
  round: (x, n = 0) => roundHalfEven(x, n),
  sqrt: (x) => { if (x < 0) throw new Error("math domain error"); return Math.sqrt(x); },
  sin: (d) => Math.sin((d * Math.PI) / 180),
  cos: (d) => Math.cos((d * Math.PI) / 180),
  tan: (d) => Math.tan((d * Math.PI) / 180),
  floor: Math.floor,
  ceil: Math.ceil,
};
const CONST = { pi: Math.PI };

// Python's round(): decided on the exact value of the float, and exact halves go to the even neighbour
// (round(2.5) == 2; round(2.675, 2) == 2.67 because 2.675 is really 2.67499...).
function roundHalfEven(x, n = 0) {
  if (!Number.isFinite(x)) return x;
  n = Math.trunc(n);
  if (n < 0 || n > 60) return Math.round(x * 10 ** n) / 10 ** n;
  const [int, frac] = Math.abs(x).toFixed(Math.min(100, n + 40)).split(".");
  const keep = int + frac.slice(0, n), rest = frac.slice(n);
  let up = rest[0] > "5" || (rest[0] === "5" && (/[1-9]/.test(rest.slice(1)) || Number(keep.at(-1)) % 2 === 1));
  if (rest[0] < "5") up = false;
  const digits = (BigInt(keep) + (up ? 1n : 0n)).toString().padStart(n + 1, "0");
  const v = Number(n ? `${digits.slice(0, -n)}.${digits.slice(-n)}` : digits);
  return x < 0 ? -v : v;
}

const TOKEN = /\s*(?:(\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)|([A-Za-z_]\w*)|(\*\*|\/\/|==|!=|<=|>=|[-+*/%<>(),]))/y;

function tokenize(src) {
  const out = [];
  TOKEN.lastIndex = 0;
  let i = 0;
  while (i < src.length) {
    if (/^\s*$/.test(src.slice(i))) break;
    TOKEN.lastIndex = i;
    const m = TOKEN.exec(src);
    if (!m) throw new Error(`disallowed expression: '${src}'`);
    if (m[1] !== undefined) out.push({ t: "num", v: parseFloat(m[1]) });
    else if (m[2] !== undefined) out.push({ t: "name", v: m[2] });
    else out.push({ t: "op", v: m[3] });
    i = TOKEN.lastIndex;
  }
  return out;
}

// Same grammar as the Python AST subset csg.eval_expr accepts: arithmetic, one comparison,
// "a if c else b", calls to the whitelisted functions.
export function evalExpr(src, env = {}) {
  const tk = tokenize(src);
  let p = 0;
  const peek = (v) => tk[p] && tk[p].v === v && (tk[p].t === "op" || tk[p].t === "name");
  const take = (v) => { if (!peek(v)) throw new Error(`disallowed expression: '${src}'`); p++; };
  const bad = () => { throw new Error(`disallowed expression: '${src}'`); };

  function test() {
    const body = comparison();
    if (peek("if")) {
      p++;
      const cond = comparison();
      take("else");
      const other = test();
      return cond ? body : other;
    }
    return body;
  }
  function comparison() {
    const l = arith();
    const ops = { "<": (a, b) => a < b, "<=": (a, b) => a <= b, ">": (a, b) => a > b, ">=": (a, b) => a >= b, "==": (a, b) => a === b, "!=": (a, b) => a !== b };
    if (tk[p] && tk[p].t === "op" && tk[p].v in ops) {
      const op = tk[p++].v;
      const r = arith();
      if (tk[p] && tk[p].t === "op" && tk[p].v in ops) bad();   // chained comparisons are refused
      return ops[op](l, r);
    }
    return l;
  }
  function arith() {
    let v = term();
    while (peek("+") || peek("-")) { const op = tk[p++].v; const r = term(); v = op === "+" ? v + r : v - r; }
    return v;
  }
  function term() {
    let v = factor();
    while (peek("*") || peek("/") || peek("//") || peek("%")) {
      const op = tk[p++].v, r = factor();
      if (op !== "*" && r == 0) throw new Error("division by zero");
      if (op === "*") v = v * r;
      else if (op === "/") v = v / r;
      else if (op === "//") v = Math.floor(v / r);
      else v = v - r * Math.floor(v / r);
    }
    return v;
  }
  function factor() {
    if (peek("-")) { p++; return -factor(); }
    if (peek("+")) { p++; return +factor(); }
    return power();
  }
  function power() {
    const base = atom();
    if (peek("**")) {
      p++;
      const e = factor();
      if (Math.abs(e) > 16) throw new Error("exponent too large");
      return base ** e;
    }
    return base;
  }
  function atom() {
    const t = tk[p++];
    if (!t) bad();
    if (t.t === "num") return t.v;
    if (t.t === "op" && t.v === "(") { const v = test(); take(")"); return v; }
    if (t.t === "name") {
      if (peek("(")) {
        if (!(t.v in FUNCS)) bad();
        p++;
        const args = [];
        if (!peek(")")) { args.push(test()); while (peek(",")) { p++; args.push(test()); } }
        take(")");
        return FUNCS[t.v](...args);
      }
      if (t.v === "True") return true;
      if (t.v === "False") return false;
      if (Object.prototype.hasOwnProperty.call(env, t.v)) return env[t.v];
      if (t.v in CONST) return CONST[t.v];
      throw new Error(`unknown variable '${t.v}' in the expression '${src}'`);
    }
    return bad();
  }
  const v = test();
  if (p !== tk.length) bad();
  return v;
}

export const isExpr = (v) => typeof v === "string" && v.startsWith("=");

// Replace "=expr" strings recursively (children are left for their own resolve).
export function resolve(value, env) {
  if (isExpr(value)) return evalExpr(value.slice(1), env);
  if (Array.isArray(value)) return value.map((v) => resolve(v, env));
  if (value && typeof value === "object") {
    const out = {};
    for (const [k, v] of Object.entries(value)) out[k] = k === "children" || k === "child" ? v : resolve(v, env);
    return out;
  }
  return value;
}

// Variables may refer to each other in any order.
export function resolveVars(vars = {}) {
  const env = {}, active = [];
  const lazy = new Proxy({}, {
    has: (_, k) => k in vars || k in env,
    get: (_, k) => get(k),
    getOwnPropertyDescriptor: (_, k) => (k in vars || k in env ? { value: get(k), enumerable: true, configurable: true } : undefined),
  });
  function get(k) {
    if (k in env) return env[k];
    if (active.includes(k)) throw new Error("circular variable reference: " + [...active.slice(active.indexOf(k)), k].join(" -> "));
    active.push(k);
    try { env[k] = resolve(vars[k], lazy); } finally { active.pop(); }
    return env[k];
  }
  Object.keys(vars).forEach(get);
  return env;
}

// ------------------------------------------------------------ the tree

export const OPS = ["union", "difference", "intersection", "hull", "minkowski", "group"];
export const TRANSFORM_KEYS = ["translate", "rotate", "scale", "mirror"];
export const isOp = (n) => !!n && typeof n === "object" && "op" in n;
export const kids = (n) => (isOp(n) && Array.isArray(n.children) ? n.children : []);

// [[name, node]] for both recipe forms; a "root" recipe is the single part "model" (as in csg.build).
export function partEntries(spec) {
  if (spec && spec.parts && typeof spec.parts === "object") return Object.entries(spec.parts);
  if (spec && spec.root) return [["model", spec.root]];
  return [];
}

// Writes parts back: one part named "model" stays a "root" recipe, anything else becomes "parts".
export function setParts(spec, entries) {
  delete spec.root;
  delete spec.parts;
  if (entries.length === 1 && entries[0][0] === "model") spec.root = entries[0][1];
  else spec.parts = Object.fromEntries(entries);
  return spec;
}

// Map of node -> {parent, index, part} (parent null for a part's root).
export function indexTree(spec) {
  const map = new Map();
  const visit = (n, parent, index, part) => {
    if (!n || typeof n !== "object") return;
    map.set(n, { parent, index, part });
    kids(n).forEach((c, i) => visit(c, n, i, part));
  };
  partEntries(spec).forEach(([name, n]) => visit(n, null, 0, name));
  return map;
}

// JSON pointer of a node, and back. Used to keep the selection across undo and reloads.
export function pointerOf(spec, node) {
  const idx = indexTree(spec);
  const path = [];
  let n = node;
  while (n && idx.has(n)) {
    const { parent, index, part } = idx.get(n);
    if (!parent) { path.unshift(spec.parts ? `parts/${part}` : "root"); return "/" + path.join("/"); }
    path.unshift(`children/${index}`);
    n = parent;
  }
  return null;
}

export function atPointer(spec, ptr) {
  if (!ptr) return null;
  let cur = spec;
  for (const key of ptr.split("/").slice(1)) {
    if (cur == null || typeof cur !== "object") return null;
    cur = cur[key];
  }
  return cur && typeof cur === "object" ? cur : null;
}

export function isHole(spec, node, idx = indexTree(spec)) {
  // Cut away by a difference: a later child of one, or anything inside such a child.
  for (let n = node; n && idx.has(n); n = idx.get(n).parent) {
    const { parent, index } = idx.get(n);
    if (parent && parent.op === "difference" && index > 0) return true;
  }
  return false;
}

export const directHole = (spec, node, idx = indexTree(spec)) => {
  const e = idx.get(node);
  return !!e && !!e.parent && e.parent.op === "difference" && e.index > 0;
};

export function label(node) {
  if (node.name) return node.name;
  if (isOp(node)) return node.op;
  return String(node.shape ?? "?").replace(/_/g, " ");
}

function replaceNode(spec, idx, node, repl) {
  const { parent, index, part } = idx.get(node);
  if (parent) parent.children[index] = repl;
  else setParts(spec, partEntries(spec).map(([k, v]) => [k, k === part ? repl : v]));
}

function removeNode(spec, idx, node) {
  const { parent, index } = idx.get(node);
  if (!parent) throw new Error("Delete the part instead.");
  parent.children.splice(index, 1);
}

const MOVE_WITH_WRAPPER = [...TRANSFORM_KEYS, "array", "polar", "hidden"];

// Make a node cut (hole = true) or add (hole = false). Returns an error message or null.
// - A child of a union becomes a later child of the difference that union is the base of; when there is
//   no such difference, the union is wrapped in one (its transform moves to the wrapper, so nothing moves).
// - A later child of a difference moves into the difference's base (wrapped in a union when needed).
export function setHole(spec, node, hole) {
  let idx = indexTree(spec);
  const e = idx.get(node);
  if (!e) return "Not in the recipe.";
  if (directHole(spec, node, idx) === hole) return null;
  if (!e.parent) return hole ? "A whole part can't be a hole. Group it with something first." : null;
  const parent = e.parent;
  if (!hole) {
    // parent is a difference and node is one of its cutters
    parent.children.splice(e.index, 1);
    let base = parent.children[0];
    if (!base || !(base.op === "union" || base.op === "group")) {
      base = { op: "union", children: base ? [base] : [] };
      parent.children[0] = base;
    }
    base.children.push(node);
    return null;
  }
  if (!(parent.op === "union" || parent.op === "group")) return `Children of a ${parent.op} can't be holes. Ungroup or move it first.`;
  if (parent.children.length < 2) return "Nothing left to cut it from: add a solid first.";
  parent.children.splice(e.index, 1);
  const pe = idx.get(parent);
  if (pe.parent && pe.parent.op === "difference" && pe.index === 0) {
    pe.parent.children.push(node);
    return null;
  }
  const wrap = { op: "difference", children: [parent, node] };
  for (const k of MOVE_WITH_WRAPPER) if (k in parent) { wrap[k] = parent[k]; delete parent[k]; }
  idx = indexTree(spec);
  replaceNode(spec, idx, parent, wrap);
  return null;
}

// Put nodes into a new op node in place of the first one. The caller has made their transforms
// relative to the same frame (they must share a parent, which is checked here).
export function group(spec, nodes, op = "union") {
  const idx = indexTree(spec);
  const parents = new Set(nodes.map((n) => idx.get(n)?.parent));
  if (parents.size !== 1 || parents.has(undefined)) throw new Error("Group shapes that sit in the same group.");
  const [parent] = parents;
  const g = { op, children: [] };
  if (!parent) {
    if (nodes.length !== 1) throw new Error("Parts can't be grouped together.");
    g.children.push(nodes[0]);
    replaceNode(spec, idx, nodes[0], g);
    return g;
  }
  const order = [...nodes].sort((a, b) => idx.get(a).index - idx.get(b).index);
  const at = idx.get(order[0]).index;
  for (const n of [...order].reverse()) parent.children.splice(idx.get(n).index, 1);
  g.children.push(...order);
  parent.children.splice(at, 0, g);
  return g;
}

// Replace a union/group by its children. The caller has already folded the group's transform into
// the children (ungroupable() says whether that is possible).
export function ungroup(spec, node) {
  const idx = indexTree(spec);
  const e = idx.get(node);
  if (!e?.parent) throw new Error("A part's top level can't be ungrouped.");
  e.parent.children.splice(e.index, 1, ...node.children);
  return node.children;
}

export function ungroupable(node) {
  if (!(node.op === "union" || node.op === "group")) return `A ${node.op ?? "shape"} can't be ungrouped (it changes the result).`;
  if ("array" in node || "polar" in node) return "This group is repeated (array); it can't be ungrouped.";
  if (node.hidden) return "Show the group before ungrouping it.";
  return null;
}

// Move a node into `target` (an op node) at position `at`, or next to it.
export function moveNode(spec, node, targetParent, at) {
  const idx = indexTree(spec);
  for (let n = targetParent; n; n = idx.get(n)?.parent) if (n === node) throw new Error("Can't move a group into itself.");
  const e = idx.get(node);
  if (!e?.parent) throw new Error("Parts can't be moved; move what is inside them.");
  e.parent.children.splice(e.index, 1);
  if (e.parent === targetParent && e.index < at) at--;
  targetParent.children.splice(Math.max(0, Math.min(at, targetParent.children.length)), 0, node);
}

// Where new shapes of a part go: its top-level union, or the base of its top-level difference.
// Wraps the part's root when it is a lone shape or another op. Returns the container op node.
export function solidsOf(spec, partName) {
  const entries = partEntries(spec);
  const entry = entries.find(([k]) => k === partName) || entries[0];
  if (!entry) throw new Error("No part to add to.");
  const [name, root] = entry;
  if ((root.op === "union" || root.op === "group") && !("array" in root) && !("polar" in root)) return root;
  if (root.op === "difference" && root.children?.length) {
    const base = root.children[0];
    if ((base.op === "union" || base.op === "group") && !("array" in base) && !("polar" in base)) return base;
    const u = { op: "union", children: [base] };
    root.children[0] = u;
    return u;
  }
  const u = { op: "union", children: [root] };
  setParts(spec, entries.map(([k, v]) => [k, k === name ? u : v]));
  return u;
}

// Where new holes of a part go: its top-level difference (made when missing).
export function holesOf(spec, partName) {
  const entries = partEntries(spec);
  const entry = entries.find(([k]) => k === partName) || entries[0];
  const [name, root] = entry;
  if (root.op === "difference" && !("array" in root) && !("polar" in root)) return root;
  const d = { op: "difference", children: [root] };
  setParts(spec, entries.map(([k, v]) => [k, k === name ? d : v]));
  return d;
}

export function uniquePartName(spec, base = "part") {
  const names = new Set(partEntries(spec).map(([k]) => k));
  if (!names.has(base)) return base;
  for (let i = 2; ; i++) if (!names.has(`${base}${i}`)) return `${base}${i}`;
}

// ------------------------------------------------------------ shapes

// Shapes the browser draws itself (as long as they have no rounding and no array).
const CLIENT = new Set(["box", "cube", "cylinder", "cone", "sphere"]);
const num = (v) => typeof v === "number" && Number.isFinite(v);

// Geometry of a resolved primitive in its own frame, or null when the server has to mesh it.
//   {type: "box", size, center} | {type: "cyl", rBottom, rTop, h, center} | {type: "sphere", r}
export function primitive(n) {
  if (!n || !CLIENT.has(n.shape) || "array" in n || "polar" in n) return null;
  const c = n.center === true;
  try {
    if (n.shape === "box" || n.shape === "cube") {
      if (n.radius) return null;
      const s = num(n.size) ? [n.size, n.size, n.size] : n.size;
      if (!Array.isArray(s) || s.length !== 3 || !s.every(num) || s.some((x) => x <= 0)) return null;
      return { type: "box", size: s, center: c ? [0, 0, 0] : s.map((x) => x / 2) };
    }
    if (n.shape === "sphere") {
      const r = radius(n, "d", "r");
      return r > 0 ? { type: "sphere", r } : null;
    }
    const h = n.h;
    const r1 = "d" in n || "r" in n ? radius(n, "d", "r") : radius(n, "d1", "r1");
    const r2 = "d2" in n || "r2" in n ? radius(n, "d2", "r2") : n.shape === "cone" ? 0 : r1;
    if (!num(h) || h <= 0 || !num(r1) || !num(r2) || (r1 <= 0 && r2 <= 0)) return null;
    return { type: "cyl", rBottom: r1, rTop: r2, h, center: [0, 0, c ? 0 : h / 2] };
  } catch {
    return null;
  }
}

function radius(n, d, r) {
  if (r in n) return n[r];
  if (d in n) return n[d] / 2;
  throw new Error("no radius");
}

// Fold a resize into a primitive's own dimensions instead of a "scale" key, when that is exact.
// Returns true when it did.
export function foldScale(node, s) {
  if ("scale" in node || "array" in node || "polar" in node) return false;
  const lit = (k) => !(k in node) || num(node[k]);
  if (node.shape === "box" || node.shape === "cube") {
    if (!Array.isArray(node.size) || !node.size.every(num) || node.radius) return false;
    node.size = node.size.map((v, i) => r4(v * s[i]));
    return true;
  }
  if (node.shape === "cylinder" || node.shape === "cone") {
    if (Math.abs(s[0] - s[1]) > 1e-6 || !["d", "r", "d1", "r1", "d2", "r2", "h"].every(lit)) return false;
    for (const k of ["d", "r", "d1", "r1", "d2", "r2"]) if (k in node) node[k] = r4(node[k] * s[0]);
    if ("h" in node) node.h = r4(node.h * s[2]);
    return true;
  }
  if (node.shape === "sphere") {
    if (Math.abs(s[0] - s[1]) > 1e-6 || Math.abs(s[0] - s[2]) > 1e-6 || !lit("d") || !lit("r")) return false;
    for (const k of ["d", "r"]) if (k in node) node[k] = r4(node[k] * s[0]);
    return true;
  }
  return false;
}

const r4 = (v) => Math.round(v * 1e4) / 1e4;

// What the palette adds. "hole" ones are added as cutters; "rest" is how far the shape reaches below
// its origin (for placing it on top of things).
export const PALETTE = {
  box: { node: { shape: "box", size: [20, 20, 20], center: true }, lift: 10 },
  cylinder: { node: { shape: "cylinder", d: 20, h: 20, center: true }, lift: 10 },
  sphere: { node: { shape: "sphere", d: 20 }, lift: 10 },
  cone: { node: { shape: "cone", d: 20, h: 20, center: true }, lift: 10 },
  tube: { node: { shape: "tube", d: 20, inner_d: 14, h: 20, center: true }, lift: 10 },
  torus: { node: { shape: "torus", d: 20, tube_d: 6 }, lift: 3 },
  wedge: { node: { shape: "wedge", size: [20, 20, 20] }, lift: 0 },
  text: { node: { shape: "text", text: "Text", size: 10, h: 2, halign: "center", valign: "center" }, lift: 0 },
  screw_hole: { node: { shape: "screw_hole", size: "M3", h: 10, head: "countersunk" }, lift: 0, hole: true },
  heat_insert: { node: { shape: "heat_insert", size: "M3" }, lift: 0, hole: true },
  nut_trap: { node: { shape: "nut_trap", size: "M3" }, lift: 0, hole: true },
  threaded_hole: { node: { shape: "threaded_hole", size: "M5", h: 10 }, lift: 0, hole: true },
  boss: { node: { shape: "boss", size: "M3", h: 8 }, lift: 0 },
  snap_hook: { node: { shape: "snap_hook" }, lift: 0 },
  hinge: { node: { shape: "hinge", length: 40 }, lift: 0 },
  screw: { node: { shape: "screw", size: "M5", length: 12 }, lift: 0 },
  nut: { node: { shape: "nut", size: "M5" }, lift: 0 },
};

// Parameters the inspector offers per shape: [key, default, options?]. Keys a node already has are
// always shown, known or not.
const M_SIZES = ["M2", "M2.5", "M3", "M4", "M5", "M6", "M8"];
export const SCHEMA = {
  box: [["size", [20, 20, 20]], ["center", true], ["radius", 2], ["round", "vertical", ["vertical"]]],
  cylinder: [["d", 20], ["h", 20], ["d2", 10], ["center", true]],
  cone: [["d", 20], ["h", 20], ["d2", 0], ["center", true]],
  sphere: [["d", 20]],
  tube: [["d", 20], ["inner_d", 14], ["h", 20], ["center", true]],
  torus: [["d", 20], ["tube_d", 6]],
  wedge: [["size", [20, 20, 20]]],
  extrude: [["h", 10], ["profile", { shape: "rect", size: [10, 10] }], ["twist", 0], ["scale_top", [1, 1]], ["divisions", 0]],
  revolve: [["profile", { shape: "polygon", points: [[5, 0], [10, 0], [10, 10]] }], ["degrees", 360]],
  screw_hole: [["size", "M3", M_SIZES], ["h", 10], ["fit", "clearance", ["clearance", "tap"]], ["head", "none", ["none", "countersunk", "counterbore"]], ["nut", true], ["flip", true], ["d", 3.4], ["head_d", 6]],
  heat_insert: [["size", "M3", M_SIZES], ["h", 5], ["flip", true]],
  nut_trap: [["size", "M3", M_SIZES], ["h", 3], ["slot", 10], ["bolt", true], ["clearance", 0.3], ["flip", true]],
  threaded_hole: [["size", "M5", M_SIZES], ["h", 10], ["tolerance", 0.15], ["left", true], ["flip", true], ["pitch", 0.8]],
  screw: [["size", "M5", M_SIZES], ["length", 12], ["head", "socket", ["socket", "countersunk", "button", "hex", "none"]], ["drive", "hex", ["hex", "slot", "none"]], ["tolerance", 0.1], ["left", true], ["pitch", 0.8], ["head_d", 7.5], ["head_h", 5]],
  nut: [["size", "M5", M_SIZES], ["h", 4], ["af", 8], ["tolerance", 0.15], ["left", true]],
  boss: [["size", "M3", M_SIZES], ["h", 8], ["for", "insert", ["insert", "tap"]]],
  snap_hook: [["length", 10], ["width", 5], ["thickness", 1.5], ["overhang", 1], ["hook_h", 2]],
  hinge: [["length", 40], ["d", 6], ["knuckles", 5], ["gap", 0.4], ["leaf", 15], ["t", 3], ["mode", "print", ["print", "pin"]], ["side", "both", ["both", "a", "b"]], ["pin_d", 2]],
  text: [["text", "Text"], ["size", 10], ["h", 1], ["font", "sans", ["sans", "bold"]], ["halign", "left", ["left", "center", "right"]], ["valign", "baseline", ["baseline", "bottom", "center", "top"]], ["spacing", 1], ["line_height", 1.2]],
};
SCHEMA.cube = SCHEMA.box;
export const COMMON = [["segments", 64], ["array", { count: [2, 1, 1], step: [25, 0, 0] }], ["polar", { count: 6, degrees: 360 }], ["mirror", [1, 0, 0]]];

// Keys the inspector does not list as parameters (they have their own controls, or none: a mesh
// shape's "mesh" is the stored STL it stands for).
export const STRUCTURAL = new Set(["shape", "op", "children", "child", "name", "hidden", "translate", "rotate", "scale", "mesh"]);

// Text typed into a field -> value. "=..." stays an expression; JSON for lists and objects.
export function parseValue(text, prev) {
  const t = text.trim();
  if (t === "") return undefined;
  if (t.startsWith("=")) return t;
  if (typeof prev === "boolean" || t === "true" || t === "false") return t === "true";
  if (/^[[{]/.test(t)) return JSON.parse(t);
  if (typeof prev === "string" && !isExpr(prev)) return t;   // a text or a size like "M3" stays text
  const n = Number(t);
  return Number.isFinite(n) ? n : t;
}

export function formatValue(v) {
  if (v === undefined) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number") return String(r4(v));
  return JSON.stringify(v);
}

export const clone = (v) => JSON.parse(JSON.stringify(v));
