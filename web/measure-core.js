// The arithmetic and wording behind measuring in the 3D view (web/measure.js), without three.js so
// tests/measure_core.mjs can run it in node. Points and directions are [x, y, z] arrays in mm.

export const TOOLS = {
  distance: { label: "Distance", points: 2, key: "1",
    hint: "Click two points.", snaps: true },
  faces: { label: "Faces", points: 2, key: "2",
    hint: "Click two faces: the gap between parallel faces, or the angle between the others." },
  thickness: { label: "Thickness", points: 1, key: "3",
    hint: "Click a face: how thick the material is straight behind it." },
  angle: { label: "Angle", points: 3, key: "4",
    hint: "Click three points: the angle at the middle one.", snaps: true },
};

// What a snapped point is, best first: a hole centre wins over a corner, a corner over the middle
// of an edge, that over any point on an edge. px: how close on screen the pointer must be.
export const SNAPS = [
  { kind: "centre", px: 14 }, { kind: "corner", px: 10 }, { kind: "midpoint", px: 10 }, { kind: "edge", px: 8 },
];

// The point of segment a-b nearest to p.
export function closestOnSegment(p, a, b) {
  const ab = sub(b, a), l2 = dot(ab, ab);
  const t = l2 ? Math.max(0, Math.min(1, dot(sub(p, a), ab) / l2)) : 0;
  return [a[0] + ab[0] * t, a[1] + ab[1] * t, a[2] + ab[2] * t];
}

const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const len = (a) => Math.hypot(a[0], a[1], a[2]);

// 12.5 / 3.4 / 0.05 / 40: two decimals at most, no trailing zeros.
export function mm(v) {
  const r = Math.round(v * 100) / 100;
  return (Object.is(r, -0) ? 0 : r).toString();
}

export const deg = (v) => `${Math.round(v * 10) / 10}°`;

export function distance(a, b) {
  const d = sub(b, a);
  const axes = d.map(Math.abs);
  const along = axes.filter((v) => v >= 0.005).length;
  return {
    value: len(d),
    text: `${mm(len(d))} mm`,
    // the per-axis parts only say something when the line is not along one axis
    detail: along > 1 ? `dx ${mm(axes[0])} · dy ${mm(axes[1])} · dz ${mm(axes[2])}` : "",
  };
}

export function angleAt(a, b, c) {
  const u = sub(a, b), v = sub(c, b);
  const lu = len(u), lv = len(v);
  if (!lu || !lv) return 0;
  return Math.acos(Math.max(-1, Math.min(1, dot(u, v) / (lu * lv)))) * 180 / Math.PI;
}

// Two clicked faces (point + outward normal). Parallel faces: the gap between their planes,
// measured from the second point to the first plane. Others: the angle between the faces.
export function faces(p1, n1, p2, n2) {
  const c = dot(n1, n2) / (len(n1) * len(n2));
  const gap = Math.abs(dot(sub(p2, p1), n1) / len(n1));
  if (Math.abs(c) > 0.9995) {
    return { parallel: true, value: gap, text: `${mm(gap)} mm`, detail: "between parallel faces" };
  }
  // the angle the material makes between them: 90° for the faces of a box corner
  const a = 180 - Math.acos(Math.max(-1, Math.min(1, c))) * 180 / Math.PI;
  return { parallel: false, value: a, text: deg(a), detail: "between faces" };
}

// The short label on a hole or boss in the view.
export function featureLabel(f) {
  const size = f.sides && f.across_flats ? `${f.sides === 6 ? "hex" : `${f.sides}-gon`} ${mm(f.across_flats)} AF`
    : `Ø${mm(f.diameter)}`;
  if (f.kind === "hole") {
    if (f.through) return `${size} thru`;
    return f.depth != null ? `${size} ↧${mm(f.depth)}` : size;
  }
  return f.depth != null ? `${size} ↥${mm(f.depth)}` : size;
}

// One line of the summary list: "4× Ø3.4 hole through, 30 × 20 mm apart".
export function summaryLine(s) {
  const what = s.kind === "hole" ? (s.count > 1 ? "holes" : "hole") : (s.count > 1 ? "bosses" : "boss");
  const size = s.sides ? `${s.sides}-sided Ø${mm(s.diameter)}` : `Ø${mm(s.diameter)}`;
  let out = `${s.count > 1 ? `${s.count}× ` : ""}${size} ${what}`;
  if (s.through) out += ", through";
  else if (s.depth != null) out += `, ${mm(s.depth)} ${s.kind === "hole" ? "deep" : "high"}`;
  const p = s.pattern_mm && Object.entries(s.pattern_mm).filter(([, v]) => v > 0.005);
  if (p && p.length) out += `, ${p.map(([, v]) => mm(v)).join(" × ")} mm apart`;
  return out;
}

// Dimension lines for a box [min, max]: width along x under the front edge, depth along y beside
// the right edge and height up the front right edge, each moved `gap` away from the box, with
// extension lines from the box out to it.
export function boxDimensions(min, max, gap) {
  const [x0, y0, z0] = min, [x1, y1, z1] = max;
  const dims = [];
  if (x1 - x0 > 1e-6) dims.push({ axis: "x", value: x1 - x0,
    a: [x0, y0 - gap, z0], b: [x1, y0 - gap, z0], ext: [[[x0, y0, z0], [x0, y0 - gap * 1.25, z0]], [[x1, y0, z0], [x1, y0 - gap * 1.25, z0]]] });
  if (y1 - y0 > 1e-6) dims.push({ axis: "y", value: y1 - y0,
    a: [x1 + gap, y0, z0], b: [x1 + gap, y1, z0], ext: [[[x1, y0, z0], [x1 + gap * 1.25, y0, z0]], [[x1, y1, z0], [x1 + gap * 1.25, y1, z0]]] });
  if (z1 - z0 > 1e-6) dims.push({ axis: "z", value: z1 - z0,
    a: [x1 + gap, y0 - gap, z0], b: [x1 + gap, y0 - gap, z1], ext: [[[x1, y0, z1], [x1 + gap * 1.25, y0 - gap * 1.25, z1]]] });
  return dims;
}

// Which labels to show: rects ({x, y, w, h, keep}, screen px) in priority order; a label is hidden
// when it overlaps one shown before it (or is null: off screen). keep: always shown, moved down
// (r.y is changed) until it is clear of the ones before it.
export function cullOverlaps(rects, pad = 2) {
  const shown = [];
  const overlaps = (r) => shown.find((s) => r.x < s.x + s.w + pad && s.x < r.x + r.w + pad && r.y < s.y + s.h + pad && s.y < r.y + r.h + pad);
  return rects.map((r) => {
    if (!r) return false;
    if (r.keep) {
      for (let i = 0, s; i < 8 && (s = overlaps(r)); i++) r.y = s.y + s.h + pad;
      shown.push(r);
      return true;
    }
    if (overlaps(r)) return false;
    shown.push(r);
    return true;
  });
}

// All measurements as plain text, one per line, to copy or to give Claude.
export function asText(items) {
  return items.map((m, i) => `${i + 1}. ${m.label}: ${m.text}${m.detail ? ` (${m.detail})` : ""}`).join("\n");
}
