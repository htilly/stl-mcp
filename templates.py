"""Ready-made templates for the CSG spec: screw holes, threaded inserts, nuts, screw bosses, snap
hooks, hinges and text. All dimensions in mm.

Convention for *cutting* templates (screw_hole, heat_insert, nut_trap): the axis runs along z, the
surface is at z=0 and the body goes down into the material (plus 0.5 mm overshoot upwards for a clean
boolean operation). Place with translate at the surface point and subtract with "difference".
Set "flip": true to point upwards (hole from below).

Threaded templates (screw, nut, threaded_hole) model a real ISO metric thread (60° trapezoid profile,
right-handed unless "left": true) so printed parts mate with real hardware or with each other.
"""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

import manifold3d as mf
import numpy as np

M = mf.Manifold
CS = mf.CrossSection

FONT_DIR = Path(__file__).parent / "fonts"
FONTS = {"sans": "DejaVuSans.ttf", "bold": "DejaVuSans-Bold.ttf"}
OVERSHOOT = 0.5

# Dimensions for metric screws (mm). clear = through hole (medium), tap = pilot hole for
# self-tapping screw in plastic, head_cs = countersunk head Ø (90°), head_cb/head_h = socket head
# (DIN 912) Ø/height incl. margin, nut_af/nut_h = nut width across flats/height,
# ins_d/ins_l = hole for heat-set insert (Ruthex/CNC Kitchen type) Ø/length, pitch = coarse thread
# pitch, key/key_cs = hex key for socket head (DIN 912) / countersunk and button heads (ISO 10642/7380).
SIZES = {
    "M2":   dict(clear=2.4, tap=1.7, head_cs=4.2,  head_cb=4.0,  head_h=2.0, nut_af=4.0,  nut_h=1.6, ins_d=3.2, ins_l=4.0,  pitch=0.4,  key=1.5, key_cs=1.3),
    "M2.5": dict(clear=2.9, tap=2.1, head_cs=5.2,  head_cb=5.0,  head_h=2.5, nut_af=5.0,  nut_h=2.0, ins_d=3.5, ins_l=5.0,  pitch=0.45, key=2.0, key_cs=1.5),
    "M3":   dict(clear=3.4, tap=2.5, head_cs=6.3,  head_cb=6.0,  head_h=3.0, nut_af=5.5,  nut_h=2.4, ins_d=4.0, ins_l=5.7,  pitch=0.5,  key=2.5, key_cs=2.0),
    "M4":   dict(clear=4.5, tap=3.3, head_cs=8.4,  head_cb=7.6,  head_h=4.0, nut_af=7.0,  nut_h=3.2, ins_d=5.6, ins_l=8.1,  pitch=0.7,  key=3.0, key_cs=2.5),
    "M5":   dict(clear=5.5, tap=4.2, head_cs=10.4, head_cb=9.2,  head_h=5.0, nut_af=8.0,  nut_h=4.0, ins_d=6.4, ins_l=9.5,  pitch=0.8,  key=4.0, key_cs=3.0),
    "M6":   dict(clear=6.6, tap=5.0, head_cs=12.6, head_cb=11.0, head_h=6.0, nut_af=10.0, nut_h=5.0, ins_d=8.0, ins_l=12.7, pitch=1.0,  key=5.0, key_cs=4.0),
    "M8":   dict(clear=9.0, tap=6.8, head_cs=16.6, head_cb=14.0, head_h=8.0, nut_af=13.0, nut_h=6.5, ins_d=9.7, ins_l=12.7, pitch=1.25, key=6.0, key_cs=5.0),
}


def _size(n) -> dict:
    s = str(n.get("size", "M3")).upper().replace(",", ".")
    if s not in SIZES:
        raise ValueError(f"{n['shape']}: unknown size '{s}' (available: {', '.join(SIZES)})")
    return SIZES[s]


def _cyl(h, d, z0=0.0, d2=None, segs=48):
    return M.cylinder(h, d / 2, (d2 if d2 is not None else d) / 2, segs).translate((0, 0, z0))


def _hex(af, h, z0=0.0):
    """Hexagon with width across flats af (flat sides parallel to the x axis)."""
    r = af / math.sqrt(3)
    return CS.circle(r, 6).rotate(30).extrude(h).translate((0, 0, z0))


def _flip(m, n):
    return m.mirror((0, 0, 1)) if n.get("flip") else m


# ---------------------------------------------------------------- cutting templates

def screw_hole(n):
    """Through/blind hole for a screw, with optional countersunk or cylindrical head and nut pocket."""
    s = _size(n)
    h = float(n.get("h", 10))
    fit = n.get("fit", "clearance")
    d = float(n.get("d", s["clear"] if fit == "clearance" else s["tap"]))
    parts = [_cyl(h + OVERSHOOT, d, -h)]
    head = n.get("head", "none")
    if head == "countersunk":
        hd = float(n.get("head_d", s["head_cs"]))
        depth = (hd - d) / 2                                   # 90° countersink
        parts.append(_cyl(depth, d, -depth, d2=hd))
        parts.append(_cyl(OVERSHOOT, hd, 0))
    elif head == "counterbore":
        hd = float(n.get("head_d", s["head_cb"]))
        depth = float(n.get("head_depth", s["head_h"] + 0.4))
        parts.append(_cyl(depth + OVERSHOOT, hd, -depth))
    elif head != "none":
        raise ValueError("screw_hole: head must be none, countersunk or counterbore")
    if n.get("nut"):
        nh = s["nut_h"] + 0.3
        parts.append(_hex(s["nut_af"] + 0.3, nh + OVERSHOOT, -h - OVERSHOOT))
    return _flip(M.batch_boolean(parts, mf.OpType.Add), n)


def heat_insert(n):
    """Hole for a threaded heat-set insert (brass) with a small chamfer at the top edge."""
    s = _size(n)
    d = float(n.get("d", s["ins_d"]))
    depth = float(n.get("h", s["ins_l"] + 1.0))
    parts = [_cyl(depth + OVERSHOOT, d, -depth), _cyl(0.6, d, -0.6, d2=d + 1.0),
             _cyl(OVERSHOOT, d + 1.0, 0)]
    return _flip(M.batch_boolean(parts, mf.OpType.Add), n)


def nut_trap(n):
    """Hexagonal pocket for a nut. slot > 0 extends the pocket in +x so the nut can be slid in from the side."""
    s = _size(n)
    af = s["nut_af"] + float(n.get("clearance", 0.3))
    h = float(n.get("h", s["nut_h"] + 0.3))
    m = _hex(af, h + OVERSHOOT, -h)
    slot = float(n.get("slot", 0))
    if slot > 0:
        m = m + M.cube((slot, af, h + OVERSHOOT)).translate((0, -af / 2, -h))
    if n.get("bolt", True):
        m = m + _cyl(h + OVERSHOOT, s["clear"], -h)
    return _flip(m, n)


# ---------------------------------------------------------------- positive templates

def boss(n):
    """Screw boss (stands on z=0) with a hole for a heat-set insert (for: insert) or self-tapping screw (for: tap)."""
    s = _size(n)
    h = float(n.get("h", 8))
    purpose = n.get("for", "insert")
    if purpose == "insert":
        hole_d, hole_depth = s["ins_d"], s["ins_l"] + 1.0
        od = float(n.get("d", max(hole_d * 2, hole_d + 3.2)))
    elif purpose == "tap":
        hole_d, hole_depth = s["tap"], h - 1.0
        od = float(n.get("d", max(s["tap"] * 2.5, s["tap"] + 3.2)))
    else:
        raise ValueError("boss: for must be insert or tap")
    m = _cyl(h, od)
    if n.get("hole", True):
        depth = min(hole_depth, h - 0.6)
        m = m - _cyl(depth + OVERSHOOT, hole_d, h - depth)
    if n.get("fillet", True):                                  # reinforcing foot
        f = min(od * 0.35, h / 2)
        m = m + _cyl(f, od + 2 * f, 0, d2=od)
    return m


def snap_hook(n):
    """Snap hook (flexing arm with a hook). The arm stands on z=0, flexes in x; the hook points toward +x.
    The width runs along y (0..width)."""
    L = float(n.get("length", 10))
    w = float(n.get("width", 5))
    t = float(n.get("thickness", 1.5))
    y = float(n.get("overhang", 1.0))
    hh = float(n.get("hook_h", max(2 * y, 1.5)))
    if hh >= L:
        raise ValueError("snap_hook: hook_h must be smaller than length")
    # profile in (x, z): arm + hook with a straight locking face and a sloped lead-in
    pts = [(0, 0), (t, 0), (t, L - hh), (t + y, L - hh), (t + y * 0.25, L), (0, L)]
    m = CS([pts]).extrude(w).rotate((90, 0, 0)).translate((0, w, 0))
    if n.get("base", True):                                    # reinforced root
        r = min(t, L / 4)
        root = CS([[(0, 0), (t + r, 0), (t, r)]]).extrude(w).rotate((90, 0, 0)).translate((0, w, 0))
        m = m + root
    return m


def hinge(n):
    """Hinge along x (0..length) with the knuckle axis at z=d/2, y=0. Side a has its leaf toward -y,
    side b toward +y. mode "print": print-in-place (side a carries a pin through side b),
    mode "pin": hole for a separate axle (e.g. 1.75 mm filament → pin_d 1.9)."""
    L = float(n.get("length", 40))
    d = float(n.get("d", 6))
    cnt = int(n.get("knuckles", 5))
    gap = float(n.get("gap", 0.4))
    leaf = float(n.get("leaf", 15))
    t = float(n.get("t", d / 2))
    mode = n.get("mode", "print")
    side = n.get("side", "both")
    pin_d = float(n.get("pin_d", round(d * 0.45, 2) if mode == "print" else 1.9))
    if cnt < 2:
        raise ValueError("hinge: at least 2 knuckles")
    seg = (L - (cnt - 1) * gap) / cnt
    if seg <= gap:
        raise ValueError("hinge: length too short for the number of knuckles")
    r = d / 2
    axis = lambda h, dd, x0: M.cylinder(h, dd / 2, dd / 2, 48).rotate((0, 90, 0)).translate((x0, 0, r))

    def one(which):
        sgn = -1 if which == "a" else 1
        idx = [i for i in range(cnt) if (i % 2 == 0) == (which == "a")]
        parts = []
        for i in idx:
            x0 = i * (seg + gap)
            parts.append(axis(seg, d, x0))
            y0 = 0 if sgn > 0 else -(r + gap + 0.01)
            parts.append(M.cube((seg, r + gap + 0.01, t)).translate((x0, y0, 0)))
        plate_y = r + gap
        parts.append(M.cube((L, leaf, t)).translate((0, plate_y if sgn > 0 else -plate_y - leaf, 0)))
        m = M.batch_boolean(parts, mf.OpType.Add)
        if mode == "pin":
            m = m - axis(L + 2, pin_d, -1)
        elif which == "a":
            m = m + axis(L, pin_d, 0)
        else:
            m = m - axis(L + 2, pin_d + 2 * gap, -1)
        return m

    if mode not in ("print", "pin"):
        raise ValueError("hinge: mode must be print or pin")
    if side in ("a", "b"):
        return one(side)
    if side != "both":
        raise ValueError("hinge: side must be a, b or both")
    return M.batch_boolean([one("a"), one("b")], mf.OpType.Add)


# ---------------------------------------------------------------- threads

THREAD_DEPTH = 5 * math.sqrt(3) / 16          # ISO: radial depth of the basic profile, × pitch


def _thread_dims(n) -> tuple[float, float]:
    """Nominal (major) diameter and pitch: from size, overridable with d / pitch."""
    s = _size(n)
    d = float(n.get("d", float(str(n.get("size", "M3")).upper().replace(",", ".")[1:])))
    pitch = float(n.get("pitch", s["pitch"]))
    if not 0.2 <= pitch <= d / 2:
        raise ValueError(f"{n['shape']}: pitch must be between 0.2 and d/2")
    return d, pitch


def _thread_rod(d, pitch, length, offset=0.0, left=False, segs=None, z0=0.0):
    """Threaded rod along z from z0 to z0+length with an ISO metric profile (crest P/8, root P/4).
    offset grows (+, for cutting internal threads) or shrinks (−, play on an external thread) the
    radius. Built as a mesh whose radius follows the helix, so it costs no boolean operations."""
    R = d / 2 + offset
    r = R - THREAD_DEPTH * pitch
    N = int(segs or max(48, min(128, round(d * 12))))
    per = 16                                                  # rows per pitch
    rows = max(2, int(math.ceil(length / pitch * per)) + 1)
    z = np.linspace(0.0, length, rows)
    phi = np.arange(N) * 2 * np.pi / N
    Z, P = np.meshgrid(z, phi, indexing="ij")
    u = np.mod((Z + z0) / pitch + (1 if left else -1) * P / (2 * np.pi), 1.0)   # phase fixed to z=0
    rad = r + (R - r) * np.interp(u, [0, 1 / 8, 7 / 16, 11 / 16, 1], [1, 1, 0, 0, 1])
    V = np.stack([rad * np.cos(P), rad * np.sin(P), Z + z0], axis=-1).reshape(-1, 3)
    V = np.vstack([V, [[0, 0, z0], [0, 0, z0 + length]]])
    i, j = np.meshgrid(np.arange(rows - 1), np.arange(N), indexing="ij")
    a, b = i * N + j, i * N + (j + 1) % N
    c, e = b + N, a + N
    side = np.concatenate([np.stack([a, b, c], -1), np.stack([a, c, e], -1)]).reshape(-1, 3)
    cb, ct, jj = rows * N, rows * N + 1, np.arange(N)
    bottom = np.stack([np.full(N, cb), (jj + 1) % N, jj], -1)
    top = np.stack([np.full(N, ct), (rows - 1) * N + jj, (rows - 1) * N + (jj + 1) % N], -1)
    F = np.vstack([side, bottom, top]).astype(np.uint32)
    return M(mf.Mesh(vert_properties=np.ascontiguousarray(V, np.float32), tri_verts=F))


def _upside_down(m, n):
    """Like _flip, but rotates instead of mirroring so the thread keeps its handedness."""
    return m.rotate((180, 0, 0)) if n.get("flip") else m


def screw(n):
    """Printable screw with a real thread. The head lies on the build plate (head face at z=0, shaft
    toward +z), which is the strong way to print it. length = nominal length as on a real screw
    (countersunk: incl. the head, other heads: the shaft under the head)."""
    s = _size(n)
    d, pitch = _thread_dims(n)
    length = float(n.get("length", 10))
    head = n.get("head", "socket")
    drive = n.get("drive", "none" if head == "hex" else "hex")
    play = float(n.get("tolerance", 0.1))
    if head == "countersunk":
        dk = float(n.get("head_d", 2 * d))
        k = (dk - d) / 2                                      # 90° under the head
        head_m = _cyl(k, dk, 0, d2=d)
        shaft = length - k
    elif head == "socket":
        dk, k = float(n.get("head_d", 1.5 * d)), float(n.get("head_h", d))
        head_m = _cyl(k, dk)
        shaft = length
    elif head == "button":
        dk, k = float(n.get("head_d", 1.75 * d)), float(n.get("head_h", 0.55 * d))
        head_m = _cyl(k, 0.6 * dk, 0, d2=dk)                  # dome as a cone: prints head-down
        shaft = length
    elif head == "hex":
        dk, k = s["nut_af"], float(n.get("head_h", 0.7 * d))
        head_m = _hex(float(n.get("head_d", dk)), k)
        shaft = length
    elif head == "none":                                      # set screw / threaded rod
        k, head_m, shaft = 0.0, None, length
    else:
        raise ValueError("screw: head must be socket, countersunk, button, hex or none")
    if shaft <= pitch:
        raise ValueError("screw: length too short for the head")
    z0 = k - 0.01
    rod = _thread_rod(d, pitch, k + shaft - z0, -play, bool(n.get("left")), n.get("segments"), z0)
    top = k + shaft                                           # 45° chamfer on the tip (and bottom if headless)
    R, tip_r = d / 2 + 1, d / 2 - THREAD_DEPTH * pitch - play - 0.1
    ch = R - tip_r
    prof = [(0, -1), (R, -1), (R, top - ch), (tip_r, top), (0, top)]
    if head_m is None:
        prof = [(0, 0), (tip_r, 0), (R, ch)] + prof[2:]
    rod = rod ^ CS([prof]).revolve(int(n.get("segments", 64)))
    m = rod if head_m is None else head_m + rod
    depth = float(n.get("drive_depth", {"socket": 0.5, "countersunk": 0.6, "button": 0.6,
                                         "hex": 0.5}.get(head, 0) * k or min(0.5 * d, shaft / 3)))
    if drive == "hex":
        key = float(n.get("key", {"socket": s["key"], "none": d / 2}.get(head, s["key_cs"])))
        m = m - _hex(key + 0.15, depth + OVERSHOOT, -OVERSHOOT)
    elif drive == "slot":
        w = max(0.6, 0.2 * d)
        m = m - M.cube((4 * d, w, depth + OVERSHOOT)).translate((-2 * d, -w / 2, -OVERSHOOT))
    elif drive != "none":
        raise ValueError("screw: drive must be hex, slot or none")
    return m


def nut(n):
    """Printable hex nut with a real internal thread, standing on z=0 and centered on the z axis."""
    s = _size(n)
    d, pitch = _thread_dims(n)
    af = float(n.get("af", s["nut_af"]))
    h = float(n.get("h", s["nut_h"]))
    play = float(n.get("tolerance", 0.15))
    cut = _thread_rod(d, pitch, h + 2, play, bool(n.get("left")), n.get("segments"), -1)
    R = d / 2 + play
    ch = THREAD_DEPTH * pitch + 0.2                           # countersink on both faces
    sinks = [_cyl(ch + 0.01, 2 * (R + 0.2), -0.01, d2=2 * (R + 0.2 - ch)),
             _cyl(ch + 0.01, 2 * (R + 0.2 - ch), h - ch, d2=2 * (R + 0.2))]
    return _hex(af, h) - M.batch_boolean([cut] + sinks, mf.OpType.Add)


def threaded_hole(n):
    """Cutter for a hole with a real internal thread (printed thread instead of a tapped hole or
    insert). Surface at z=0, goes down to −h, with a 45° lead-in at the top."""
    d, pitch = _thread_dims(n)
    h = float(n.get("h", 2 * d))
    play = float(n.get("tolerance", 0.15))
    R = d / 2 + play
    ch = THREAD_DEPTH * pitch + 0.2
    parts = [_thread_rod(d, pitch, h + OVERSHOOT, play, bool(n.get("left")), n.get("segments"), -h),
             _cyl(ch, 2 * (R + 0.2 - ch), -ch, d2=2 * (R + 0.2)), _cyl(OVERSHOOT, 2 * (R + 0.2), 0)]
    return _upside_down(M.batch_boolean(parts, mf.OpType.Add), n)


# ---------------------------------------------------------------- text

@lru_cache(maxsize=4)
def _font(name: str):
    from fontTools.ttLib import TTFont
    if name not in FONTS:
        raise ValueError(f"text: unknown font '{name}' (available: {', '.join(FONTS)})")
    f = TTFont(FONT_DIR / FONTS[name])
    return f, f.getGlyphSet(), f.getBestCmap(), f["hmtx"], f["head"].unitsPerEm


def _glyph_contours(glyphset, gname, steps=6):
    from fontTools.pens.basePen import BasePen

    class Flatten(BasePen):
        def __init__(self, gs):
            super().__init__(gs)
            self.contours, self.cur = [], []

        def _moveTo(self, p):
            self.cur = [p]

        def _lineTo(self, p):
            self.cur.append(p)

        def _qCurveToOne(self, p1, p2):
            p0 = self.cur[-1]
            for i in range(1, steps + 1):
                u = i / steps
                self.cur.append(((1 - u) ** 2 * p0[0] + 2 * (1 - u) * u * p1[0] + u * u * p2[0],
                                 (1 - u) ** 2 * p0[1] + 2 * (1 - u) * u * p1[1] + u * u * p2[1]))

        def _curveToOne(self, p1, p2, p3):
            p0 = self.cur[-1]
            for i in range(1, steps + 1):
                u = i / steps
                a, b, c, e = (1 - u) ** 3, 3 * (1 - u) ** 2 * u, 3 * (1 - u) * u * u, u ** 3
                self.cur.append((a * p0[0] + b * p1[0] + c * p2[0] + e * p3[0],
                                 a * p0[1] + b * p1[1] + c * p2[1] + e * p3[1]))

        def _closePath(self):
            if len(self.cur) >= 3:
                self.contours.append(self.cur)
            self.cur = []

        _endPath = _closePath

    pen = Flatten(glyphset)
    glyphset[gname].draw(pen)
    return pen.contours


def text(n):
    """Text as 3D (extruded along z from 0 to h). size = character height (em) in mm.
    Raised text: union on the surface. Engraved: difference with text that protrudes above the surface."""
    s = str(n.get("text", ""))
    if not s.strip():
        raise ValueError("text: 'text' is missing")
    if len(s) > 200:
        raise ValueError("text: max 200 characters")
    size = float(n.get("size", 10))
    h = float(n.get("h", 1))
    spacing = float(n.get("spacing", 0))
    font, gs, cmap, hmtx, upm = _font(n.get("font", "sans"))
    k = size / upm
    lines = s.split("\n")
    contours, widths = [], []
    for li, line in enumerate(lines):
        x = 0.0
        y = -li * size * float(n.get("line_height", 1.2))
        line_contours = []
        for ch in line:
            gname = cmap.get(ord(ch), ".notdef")
            for c in _glyph_contours(gs, gname):
                line_contours.append([(x + px * k, y + py * k) for px, py in c])
            x += hmtx[gname][0] * k + spacing
        widths.append(x - spacing if line else 0.0)
        contours.append(line_contours)
    align = n.get("halign", "left")
    flat = []
    for line_contours, wdt in zip(contours, widths):
        dx = {"left": 0, "center": -wdt / 2, "right": -wdt}.get(align)
        if dx is None:
            raise ValueError("text: halign must be left, center or right")
        flat += [[(px + dx, py) for px, py in c] for c in line_contours]
    if not flat:
        raise ValueError("text: no visible characters")
    cs = CS(flat, mf.FillRule.NonZero)
    valign = n.get("valign", "baseline")
    if valign != "baseline":
        _, y0, _, y1 = cs.bounds()
        dy = {"bottom": -y0, "center": -(y0 + y1) / 2, "top": -y1}.get(valign)
        if dy is None:
            raise ValueError("text: valign must be baseline, bottom, center or top")
        cs = cs.translate((0, dy))
    return cs.extrude(h)


TEMPLATES = {
    "screw_hole": screw_hole,
    "heat_insert": heat_insert,
    "nut_trap": nut_trap,
    "threaded_hole": threaded_hole,
    "boss": boss,
    "snap_hook": snap_hook,
    "hinge": hinge,
    "screw": screw,
    "nut": nut,
    "text": text,
}

HELP = """\
Templates (use as "shape" in a node; cutting templates are subtracted with "difference"):
  Cutting – surface at z=0, goes downward (−z), "flip": true points upward.
  size: M2, M2.5, M3, M4, M5, M6, M8
  screw_hole  size, h=10, fit: clearance|tap, head: none|countersunk|counterbore, nut?: true
              (nut pocket at the bottom), d?/head_d? for custom dimensions
  heat_insert size, h? – hole for a threaded heat-set insert (brass) with chamfer
  nut_trap    size, h?, slot? (slide in from +x), bolt=true, clearance=0.3
  threaded_hole size, h=2*d, tolerance=0.15 – hole with a printed internal thread (real ISO
              profile) and lead-in, for a real or printed screw. Better from M3 up.
  Positive
  boss        size, h=8, for: insert|tap – screw boss on z=0 with hole and reinforced foot
  snap_hook   length=10, width=5, thickness=1.5, overhang=1, hook_h? – arm on z=0, hook toward +x,
              width along y. The counterpart is a rectangular recess/edge.
  hinge       length=40, d=6, knuckles=5, gap=0.4, leaf=15, t=d/2, mode: print|pin, side: both|a|b,
              pin_d? – leaves on the build plate, axis along x at z=d/2 (side a toward −y, b toward +y)
  screw       size, length=10, head: socket|countersunk|button|hex|none, drive: hex|slot|none,
              tolerance=0.1, left?, pitch?, d?, head_d?, head_h?, key? – printable screw with a real
              thread. Head face on z=0, shaft up along +z (print it like this). length is nominal
              (countersunk incl. head). head none = set screw / threaded rod. Don't build threads yourself.
  nut         size, h?, af?, tolerance=0.15, left? – hex nut with a real thread, on z=0, centered
  text        text, size=10 (mm), h=1, font: sans|bold, halign: left|center|right,
              valign: baseline|bottom|center|top, spacing, line_height, "\\n" for line break
Example: {"op":"difference","children":[<3 mm plate>, {"shape":"screw_hole","size":"M3",
  "head":"countersunk","h":3,"translate":[10,10,3]}]}
"""
