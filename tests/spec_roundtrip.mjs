// Run by tests/test_editor.py: what web/spec.js makes of recipes and of the editor's edits, as JSON.
import {
  evalExpr, resolve, resolveVars, partEntries, setParts, indexTree, pointerOf, atPointer, isHole, setHole,
  group, ungroup, moveNode, solidsOf, holesOf, primitive, foldScale, parseValue, clone,
} from "../web/spec.js";

const EXPRS = JSON.parse(process.argv[2] || "[]");
const tryIt = (f) => { try { return { ok: f() }; } catch (e) { return { err: e.message }; } };
const out = {};

// Expressions: the caller compares these with csg.eval_expr.
const env = { w: 40, wall: 2, n: 3 };
out.exprs = EXPRS.map((x) => tryIt(() => evalExpr(x, env)));
out.vars = resolveVars({ a: "=b*2", b: "=c+1", c: 4 });
out.cycle = tryIt(() => resolveVars({ a: "=b", b: "=a" }));
out.resolved = resolve({ size: ["=w", 5, "=wall*2"], children: [{ d: "=w" }] }, env);

// Parts: a root recipe is the part "model", and writes back as root.
const rootSpec = { vars: {}, root: { shape: "box", size: [1, 1, 1] } };
out.rootParts = partEntries(rootSpec).map(([k]) => k);
out.rootBack = setParts(clone(rootSpec), partEntries(clone(rootSpec)));
const two = { vars: {}, parts: { body: { shape: "box", size: [1, 1, 1] }, lid: { shape: "sphere", d: 2 } } };
out.twoParts = partEntries(two).map(([k]) => k);

// Pointers.
const deep = { vars: {}, parts: { a: { op: "union", children: [{ shape: "box", size: [1, 1, 1] }, { op: "difference", children: [{ shape: "sphere", d: 1 }, { shape: "box", size: [1, 1, 1] }] }] } } };
const cutter = deep.parts.a.children[1].children[1];
out.pointer = pointerOf(deep, cutter);
out.pointerBack = atPointer(deep, out.pointer) === cutter;
out.isHole = [isHole(deep, cutter), isHole(deep, deep.parts.a.children[0])];

// Holes: a 20x20x10 box and a d8 cylinder through it.
const base = () => ({ vars: {}, root: { op: "union", children: [
  { shape: "box", size: [20, 20, 10], center: true, translate: [0, 0, 5] },
  { shape: "cylinder", d: 8, h: 30, center: true, translate: [0, 0, 5], name: "bore" }] } });
const holed = base();
out.holeErr = setHole(holed, holed.root.children[1], true);
out.holed = holed;
const back = clone(holed);
out.solidErr = setHole(back, back.root.children[1], false);
out.solidAgain = back;
// A union with a transform keeps it on the wrapper, so nothing moves.
const moved = base();
moved.root.translate = [100, 0, 0];
setHole(moved, moved.root.children[1], true);
out.movedHoled = moved;
out.partHoleErr = setHole(rootSpec, rootSpec.root, true);

// Group / ungroup / move.
const g = base();
const made = group(g, [...g.root.children], "union");
out.grouped = clone(g);
ungroup(g, made);
out.ungrouped = g;
const m = base();
m.root.children.push({ op: "union", children: [{ shape: "sphere", d: 4, translate: [30, 0, 2] }] });
moveNode(m, m.root.children[0], m.root.children[2], 0);
out.movedInto = m;
out.moveIntoSelf = tryIt(() => moveNode(m, m.root.children[1], m.root.children[1].children[0] || m.root.children[1], 0));

// Where new shapes go.
const lone = { vars: {}, root: { shape: "box", size: [5, 5, 5] } };
solidsOf(lone, "model").children.push({ shape: "sphere", d: 3 });
out.lone = lone;
const cut = { vars: {}, root: { shape: "box", size: [5, 5, 5] } };
holesOf(cut, "model").children.push({ shape: "cylinder", d: 2, h: 10 });
out.cut = cut;

// What the browser draws itself.
out.prims = [
  primitive({ shape: "box", size: [10, 20, 30] }),
  primitive({ shape: "box", size: [10, 20, 30], center: true }),
  primitive({ shape: "cylinder", d: 10, h: 4 }),
  primitive({ shape: "cone", r: 3, h: 4, center: true }),
  primitive({ shape: "sphere", d: 6 }),
  primitive({ shape: "box", size: [1, 1, 1], radius: 0.2 }),
  primitive({ shape: "box", size: [1, 1, 1], array: { count: [2, 1, 1] } }),
  primitive({ shape: "screw_hole", size: "M3" }),
];

// Folding a resize into the dimensions.
const fb = { shape: "box", size: [10, 10, 10] }; out.foldBox = [foldScale(fb, [2, 1, 0.5]), fb];
const fc = { shape: "cylinder", d: 10, h: 4 }; out.foldCyl = [foldScale(fc, [2, 2, 3]), fc];
const fo = { shape: "cylinder", d: 10, h: 4 }; out.foldOval = [foldScale(fo, [2, 1, 1]), fo];
const fe = { shape: "box", size: ["=w", 1, 1] }; out.foldExpr = [foldScale(fe, [2, 1, 1]), fe];

out.values = [parseValue("12", 3), parseValue("=w/2", 3), parseValue("M4", "M3"), parseValue("[1,2]", null),
  parseValue("true", false), parseValue("hello", undefined), parseValue("", 1) ?? null, parseValue("5", "=w")];

console.log(JSON.stringify(out));
