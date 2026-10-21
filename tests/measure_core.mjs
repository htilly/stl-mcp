// Run by tests/test_measure.py: what web/measure-core.js computes, as JSON.
import * as m from "../web/measure-core.js";

const out = {};
out.mm = [12.5, 3.4, 0.049, 40, -0.001, 1 / 3].map(m.mm);
out.distance = m.distance([0, 0, 0], [30, 40, 0]);
out.straight = m.distance([0, 0, 0], [0, 0, 7.25]);
out.angle = [m.angleAt([1, 0, 0], [0, 0, 0], [0, 1, 0]), m.angleAt([1, 0, 0], [0, 0, 0], [-1, 0, 0]), m.angleAt([0, 0, 0], [0, 0, 0], [1, 0, 0])];
out.gap = m.faces([0, 0, 0], [0, 0, 1], [5, 3, 4.5], [0, 0, -1]);
out.corner = m.faces([0, 0, 0], [1, 0, 0], [0, 0, 0], [0, 1, 0]);
out.labels = [
  { kind: "hole", diameter: 3.4, depth: 5, through: true },
  { kind: "hole", diameter: 6, depth: 3, through: false },
  { kind: "hole", diameter: 2, depth: null, through: false },
  { kind: "hole", diameter: 6.4, depth: 3, through: false, sides: 6, across_flats: 5.543 },
  { kind: "boss", diameter: 6, depth: 8 },
].map(m.featureLabel);
out.summary = [
  { kind: "hole", count: 4, diameter: 3.4, depth: 5, through: true, pattern_mm: { x: 48, y: 28 } },
  { kind: "hole", count: 1, diameter: 8, depth: 3, through: false },
  { kind: "boss", count: 2, diameter: 6, depth: null, pattern_mm: { x: 0, y: 20 } },
].map(m.summaryLine);
out.dims = m.boxDimensions([0, 0, 0], [60, 40, 5], 4);
out.flat = m.boxDimensions([0, 0, 0], [10, 10, 0], 3).map((d) => d.axis);
out.cull = m.cullOverlaps([{ x: 0, y: 0, w: 10, h: 10 }, { x: 5, y: 5, w: 10, h: 10 }, null, { x: 50, y: 0, w: 10, h: 10 },
  { x: 2, y: 2, w: 4, h: 4, keep: true }]);
const kept = [{ x: 0, y: 0, w: 10, h: 10, keep: true }, { x: 2, y: 2, w: 10, h: 10, keep: true }];
out.kept = [m.cullOverlaps(kept), kept.map((r) => r.y)];
out.text = m.asText([{ label: "Distance", text: "50 mm", detail: "dx 30 · dy 40 · dz 0" }, { label: "Thickness", text: "2 mm", detail: "" }]);
out.tools = Object.fromEntries(Object.entries(m.TOOLS).map(([k, t]) => [k, t.points]));
console.log(JSON.stringify(out));
