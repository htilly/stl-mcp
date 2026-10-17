"""Färdiga mallar för CSG-specen: skruvhål, gängade insatser, muttrar, skruvtorn, snäpplås,
gångjärn och text. Alla mått i mm.

Konvention för *skärande* mallar (screw_hole, heat_insert, nut_trap): axeln längs z, ytan
ligger vid z=0 och kroppen går nedåt in i materialet (plus 0.5 mm överskjut uppåt för ren
boolesk operation). Placera med translate på ytans punkt och dra bort med "difference".
Sätt "flip": true för att peka uppåt (hål underifrån).
"""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

import manifold3d as mf

M = mf.Manifold
CS = mf.CrossSection

FONT_DIR = Path(__file__).parent / "fonts"
FONTS = {"sans": "DejaVuSans.ttf", "bold": "DejaVuSans-Bold.ttf"}
OVERSHOOT = 0.5

# Mått för metriska skruvar (mm). clear = genomgående hål (medium), tap = förborrning för
# självgängande skruv i plast, head_cs = försänkt huvud Ø (90°), head_cb/head_h = insexhuvud
# (DIN 912) Ø/höjd inkl. marginal, nut_af/nut_h = mutter nyckelvidd/höjd,
# ins_d/ins_l = hål för värmeinsats (typ Ruthex/CNC Kitchen) Ø/längd.
SIZES = {
    "M2":   dict(clear=2.4, tap=1.7, head_cs=4.2,  head_cb=4.0, head_h=2.0, nut_af=4.0, nut_h=1.6, ins_d=3.2, ins_l=4.0),
    "M2.5": dict(clear=2.9, tap=2.1, head_cs=5.2,  head_cb=5.0, head_h=2.5, nut_af=5.0, nut_h=2.0, ins_d=3.5, ins_l=5.0),
    "M3":   dict(clear=3.4, tap=2.5, head_cs=6.3,  head_cb=6.0, head_h=3.0, nut_af=5.5, nut_h=2.4, ins_d=4.0, ins_l=5.7),
    "M4":   dict(clear=4.5, tap=3.3, head_cs=8.4,  head_cb=7.6, head_h=4.0, nut_af=7.0, nut_h=3.2, ins_d=5.6, ins_l=8.1),
    "M5":   dict(clear=5.5, tap=4.2, head_cs=10.4, head_cb=9.2, head_h=5.0, nut_af=8.0, nut_h=4.0, ins_d=6.4, ins_l=9.5),
}


def _size(n) -> dict:
    s = str(n.get("size", "M3")).upper().replace(",", ".")
    if s not in SIZES:
        raise ValueError(f"{n['shape']}: okänd storlek '{s}' (finns: {', '.join(SIZES)})")
    return SIZES[s]


def _cyl(h, d, z0=0.0, d2=None, segs=48):
    return M.cylinder(h, d / 2, (d2 if d2 is not None else d) / 2, segs).translate((0, 0, z0))


def _hex(af, h, z0=0.0):
    """Sexkant med nyckelvidd af (plana sidor parallella med x-axeln)."""
    r = af / math.sqrt(3)
    return CS.circle(r, 6).rotate(30).extrude(h).translate((0, 0, z0))


def _flip(m, n):
    return m.mirror((0, 0, 1)) if n.get("flip") else m


# ---------------------------------------------------------------- skärande mallar

def screw_hole(n):
    """Genomgående/blint hål för skruv, med valfritt försänkt eller cylindriskt huvud och mutterficka."""
    s = _size(n)
    h = float(n.get("h", 10))
    fit = n.get("fit", "clearance")
    d = float(n.get("d", s["clear"] if fit == "clearance" else s["tap"]))
    parts = [_cyl(h + OVERSHOOT, d, -h)]
    head = n.get("head", "none")
    if head == "countersunk":
        hd = float(n.get("head_d", s["head_cs"]))
        depth = (hd - d) / 2                                   # 90° försänkning
        parts.append(_cyl(depth, d, -depth, d2=hd))
        parts.append(_cyl(OVERSHOOT, hd, 0))
    elif head == "counterbore":
        hd = float(n.get("head_d", s["head_cb"]))
        depth = float(n.get("head_depth", s["head_h"] + 0.4))
        parts.append(_cyl(depth + OVERSHOOT, hd, -depth))
    elif head != "none":
        raise ValueError("screw_hole: head måste vara none, countersunk eller counterbore")
    if n.get("nut"):
        nh = s["nut_h"] + 0.3
        parts.append(_hex(s["nut_af"] + 0.3, nh + OVERSHOOT, -h - OVERSHOOT))
    return _flip(M.batch_boolean(parts, mf.OpType.Add), n)


def heat_insert(n):
    """Hål för gängad värmeinsats (mässing) med liten fasning i överkant."""
    s = _size(n)
    d = float(n.get("d", s["ins_d"]))
    depth = float(n.get("h", s["ins_l"] + 1.0))
    parts = [_cyl(depth + OVERSHOOT, d, -depth), _cyl(0.6, d, -0.6, d2=d + 1.0),
             _cyl(OVERSHOOT, d + 1.0, 0)]
    return _flip(M.batch_boolean(parts, mf.OpType.Add), n)


def nut_trap(n):
    """Sexkantsficka för mutter. slot > 0 förlänger fickan i +x så att muttern kan skjutas in från sidan."""
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


# ---------------------------------------------------------------- positiva mallar

def boss(n):
    """Skruvtorn (står på z=0) med hål för värmeinsats (for: insert) eller självgängande skruv (for: tap)."""
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
        raise ValueError("boss: for måste vara insert eller tap")
    m = _cyl(h, od)
    if n.get("hole", True):
        depth = min(hole_depth, h - 0.6)
        m = m - _cyl(depth + OVERSHOOT, hole_d, h - depth)
    if n.get("fillet", True):                                  # förstärkande fot
        f = min(od * 0.35, h / 2)
        m = m + _cyl(f, od + 2 * f, 0, d2=od)
    return m


def snap_hook(n):
    """Snäpplås (fjädrande arm med hake). Armen står på z=0, böjer i x-led; haken pekar mot +x.
    Bredden ligger längs y (0..width)."""
    L = float(n.get("length", 10))
    w = float(n.get("width", 5))
    t = float(n.get("thickness", 1.5))
    y = float(n.get("overhang", 1.0))
    hh = float(n.get("hook_h", max(2 * y, 1.5)))
    if hh >= L:
        raise ValueError("snap_hook: hook_h måste vara mindre än length")
    # profil i (x, z): arm + hake med rak låsyta och sned infasning
    pts = [(0, 0), (t, 0), (t, L - hh), (t + y, L - hh), (t + y * 0.25, L), (0, L)]
    m = CS([pts]).extrude(w).rotate((90, 0, 0)).translate((0, w, 0))
    if n.get("base", True):                                    # förstärkt rot
        r = min(t, L / 4)
        root = CS([[(0, 0), (t + r, 0), (t, r)]]).extrude(w).rotate((90, 0, 0)).translate((0, w, 0))
        m = m + root
    return m


def hinge(n):
    """Gångjärn längs x (0..length) med knuckle-axel på z=d/2, y=0. Sida a har platta mot -y,
    sida b mot +y. mode "print": print-in-place (sida a bär en tapp genom sida b),
    mode "pin": hål för separat axel (t.ex. 1.75 mm filament → pin_d 1.9)."""
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
        raise ValueError("hinge: minst 2 knuckles")
    seg = (L - (cnt - 1) * gap) / cnt
    if seg <= gap:
        raise ValueError("hinge: för kort length för antalet knuckles")
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
        raise ValueError("hinge: mode måste vara print eller pin")
    if side in ("a", "b"):
        return one(side)
    if side != "both":
        raise ValueError("hinge: side måste vara a, b eller both")
    return M.batch_boolean([one("a"), one("b")], mf.OpType.Add)


# ---------------------------------------------------------------- text

@lru_cache(maxsize=4)
def _font(name: str):
    from fontTools.ttLib import TTFont
    if name not in FONTS:
        raise ValueError(f"text: okänt typsnitt '{name}' (finns: {', '.join(FONTS)})")
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
    """Text som 3D (extruderad längs z från 0 till h). size = teckenhöjd (em) i mm.
    Upphöjd text: union på ytan. Ingraverad: difference med text som sticker ut över ytan."""
    s = str(n.get("text", ""))
    if not s.strip():
        raise ValueError("text: 'text' saknas")
    if len(s) > 200:
        raise ValueError("text: max 200 tecken")
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
            raise ValueError("text: halign måste vara left, center eller right")
        flat += [[(px + dx, py) for px, py in c] for c in line_contours]
    if not flat:
        raise ValueError("text: inga synliga tecken")
    cs = CS(flat, mf.FillRule.NonZero)
    valign = n.get("valign", "baseline")
    if valign != "baseline":
        _, y0, _, y1 = cs.bounds()
        dy = {"bottom": -y0, "center": -(y0 + y1) / 2, "top": -y1}.get(valign)
        if dy is None:
            raise ValueError("text: valign måste vara baseline, bottom, center eller top")
        cs = cs.translate((0, dy))
    return cs.extrude(h)


TEMPLATES = {
    "screw_hole": screw_hole,
    "heat_insert": heat_insert,
    "nut_trap": nut_trap,
    "boss": boss,
    "snap_hook": snap_hook,
    "hinge": hinge,
    "text": text,
}

HELP = """\
Mallar (lägg som "shape" i en nod; skärande mallar dras bort med "difference"):
  Skärande – ytan vid z=0, går nedåt (−z), "flip": true pekar uppåt. size: M2, M2.5, M3, M4, M5
  screw_hole  size, h=10, fit: clearance|tap, head: none|countersunk|counterbore, nut?: true
              (mutterficka i botten), d?/head_d? för egna mått
  heat_insert size, h? – hål för gängad värmeinsats (mässing) med fasning
  nut_trap    size, h?, slot? (skjut in från +x), bolt=true, clearance=0.3
  Positiva
  boss        size, h=8, for: insert|tap – skruvtorn på z=0 med hål och förstärkt fot
  snap_hook   length=10, width=5, thickness=1.5, overhang=1, hook_h? – arm på z=0, hake mot +x,
              bredd längs y. Motstycket är en rektangulär urtagning/kant.
  hinge       length=40, d=6, knuckles=5, gap=0.4, leaf=15, t=d/2, mode: print|pin, side: both|a|b,
              pin_d? – plattor på byggplattan, axel längs x på z=d/2 (sida a mot −y, b mot +y)
  text        text, size=10 (mm), h=1, font: sans|bold, halign: left|center|right,
              valign: baseline|bottom|center|top, spacing, line_height, "\\n" för radbrytning
Exempel: {"op":"difference","children":[<platta 3 mm>, {"shape":"screw_hole","size":"M3",
  "head":"countersunk","h":3,"translate":[10,10,3]}]}
"""
