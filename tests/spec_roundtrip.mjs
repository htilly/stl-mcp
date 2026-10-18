// Run by tests/test_editor.py: prints the editor's recipe for some scenes, and what it makes of recipes.
import { parse, serialize } from "../web/spec.js";

const box = { shape: "box", size: [20, 20, 10], pos: [0, 0, 5], rot: [0, 0, 0], hole: false, name: "" };
const hole = { shape: "cylinder", size: [8, 8, 30], pos: [0, 0, 5], rot: [0, 0, 0], hole: true, name: "bore" };
const tilted = { shape: "sphere", size: [10, 10, 10], pos: [30, 0, 5], rot: [0, 0, 45], hole: false, name: "" };
const out = {
  solid: serialize([box]),
  holed: serialize([box, hole, tilted]),
  noSolid: (() => { try { serialize([hole]); } catch (e) { return e.message; } })(),
  // parse back what serialize wrote
  back: parse(serialize([box, hole, tilted])).objects.map(({ shape, size, pos, rot, hole, name }) => ({ shape, size, pos, rot, hole, name })),
  // a hand-written recipe: corner-anchored box and base-anchored cylinder
  claude: parse({ vars: {}, root: { op: "union", children: [
    { shape: "box", size: [10, 20, 30], translate: [5, 5, 0] },
    { shape: "cylinder", d: 6, h: 12, translate: [0, 0, 30] }] } }).objects.map(({ shape, size, pos }) => ({ shape, size, pos })),
  // things the editor must leave alone
  expr: parse({ vars: { w: 5 }, root: { shape: "box", size: ["=w", 5, 5] } }).objects.map((o) => o.shape),
  template: parse({ root: { shape: "screw_hole", d: 3 } }).objects.map((o) => o.shape),
  parts: parse({ parts: { a: { shape: "box", size: [1, 1, 1] } } }).unsupported,
  lockedKept: serialize(parse({ vars: { w: 5 }, root: { op: "hull", children: [{ shape: "sphere", d: 4 }] } }).objects,
    { w: 5 }),
};
console.log(JSON.stringify(out));
