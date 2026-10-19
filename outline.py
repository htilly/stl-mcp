"""2D outlines from SVG and DXF: the "svg" profile of the CSG spec and the import_outline tool.

A recipe keeps an outline as an SVG path string ("M0 0L40 0 40 20Z ...") in mm with y up, so it
stays small, readable and self-contained (rebuilds and copies need no files). import_outline turns
an SVG or DXF file into such a path: every closed shape, transforms applied, curves flattened,
moved so its bounding box starts at (0, 0). Parsing runs in a build worker (buildworker.py), not in
the server.
"""
from __future__ import annotations

import io
import math

import numpy as np

MAX_POINTS = 50_000          # per outline, after flattening
MAX_PATH = 400_000           # characters of a path string
MM_PER_PX = 25.4 / 96        # SVG user units are CSS pixels


def _fmt(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def to_path(contours: list[np.ndarray]) -> str:
    """Contours (closed, mm, y up) as a compact SVG path string."""
    out = []
    for c in contours:
        pts = [f"{_fmt(x)} {_fmt(y)}" for x, y in c]
        out.append(f"M{pts[0]}L{' '.join(pts[1:])}Z")
    return "".join(out)


def _sample(seg, tol: float) -> np.ndarray:
    """Points along one curved segment, at most tol apart (excluding its start). Spacing, not
    chord error: 0.3 mm keeps a 5 mm radius within 0.01 mm."""
    try:
        length = seg.length(error=1e-4)
    except Exception:  # noqa: BLE001 – a degenerate segment
        length = 0
    n = min(400, max(2, math.ceil(length / max(tol, 1e-3))))
    pts = seg.npoint(np.linspace(0, 1, n + 1)[1:])
    return np.asarray(pts, np.float64).reshape(-1, 2)


def parse_path(d: str, tol: float = 0.3) -> list[np.ndarray]:
    """An SVG path string as closed contours (each an n×2 array). Open subpaths are closed."""
    from svgelements import Close, Line, Move, Path
    if not isinstance(d, str) or not d.strip():
        raise ValueError("the outline path is empty")
    if len(d) > MAX_PATH:
        raise ValueError(f"the outline path is too long (max {MAX_PATH:,} characters)")
    try:
        path = Path(d)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"invalid SVG path: {e}") from None
    contours, cur, total = [], [], 0
    for seg in path:
        if isinstance(seg, Move):
            if len(cur) >= 3:
                contours.append(np.array(cur))
            cur = [(seg.end.x, seg.end.y)]
        elif isinstance(seg, Close):
            if len(cur) >= 3:
                contours.append(np.array(cur))
            cur = [(seg.end.x, seg.end.y)] if seg.end is not None else []
        elif isinstance(seg, Line):
            if not cur and seg.start is not None:
                cur = [(seg.start.x, seg.start.y)]
            cur.append((seg.end.x, seg.end.y))
        else:
            if not cur and seg.start is not None:
                cur = [(seg.start.x, seg.start.y)]
            cur.extend(map(tuple, _sample(seg, tol)))
        total += 1
        if len(cur) + sum(len(c) for c in contours) > MAX_POINTS:
            raise ValueError(f"the outline has too many points (max {MAX_POINTS:,}) - raise the tolerance")
    if len(cur) >= 3:
        contours.append(np.array(cur))
    out = []
    for c in contours:
        if np.allclose(c[0], c[-1]):
            c = c[:-1]
        if len(c) >= 3 and np.all(np.isfinite(c)):
            out.append(c)
    if not out:
        raise ValueError("the outline has no closed shape with an area")
    return out


def profile(p: dict, cs_class, fill_rules) -> object:
    """The CSG profile {"shape": "svg", "path": "...", "size"?, "scale"?, "flip_y"?, "fill"?,
    "tolerance"?, "center"?} as a manifold3d CrossSection."""
    tol = float(p.get("tolerance", 0.3))
    if not 0.02 <= tol <= 10:
        raise ValueError("svg profile: tolerance (point spacing on curves) must be 0.02-10 mm")
    contours = parse_path(p.get("path"), tol)
    if p.get("flip_y"):                       # a path copied from an SVG file has y pointing down
        contours = [c * [1, -1] for c in contours]
    allp = np.vstack(contours)
    lo, hi = allp.min(0), allp.max(0)
    ext = hi - lo
    if "size" in p:
        size = p["size"]
        if not isinstance(size, (list, tuple)) or len(size) != 2:
            raise ValueError("svg profile: size must be [width, height] (one may be null)")
        f = [None if s is None else float(s) / e if e > 0 else None for s, e in zip(size, ext)]
        f = [v for v in f if v]
        if not f:
            raise ValueError("svg profile: give a width or height in size")
        s = min(f)                            # fits inside the size, keeping the proportions
        if any(v <= 0 for v in f):
            raise ValueError("svg profile: size must be > 0")
    else:
        s = float(p.get("scale", 1))
        if s <= 0:
            raise ValueError("svg profile: scale must be > 0")
    origin = (lo + hi) / 2 if p.get("center") else lo
    contours = [((c - origin) * s).tolist() for c in contours]
    fill = p.get("fill", "evenodd")
    if fill not in fill_rules:
        raise ValueError("svg profile: fill must be 'evenodd' (holes inside shapes) or 'nonzero'")
    return cs_class(contours, fill_rules[fill])


# ---------------------------------------------------------------- files

def _svg_file(data: bytes, tol: float) -> list[np.ndarray]:
    from svgelements import SVG, Path, Shape, Text
    try:
        doc = SVG.parse(io.BytesIO(data), reify=True, ppi=96)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"could not read the SVG file: {e}") from None
    contours = []
    for el in doc.elements():
        if isinstance(el, Text):
            raise ValueError("the SVG contains text: convert it to paths first (Inkscape: Path > "
                             "Object to Path)")
        if isinstance(el, Shape):
            d = Path(el).d()
            if d:
                try:
                    contours += parse_path(d, tol / MM_PER_PX)
                except ValueError:
                    continue                  # an open line or a shape without area
    if not contours:
        raise ValueError("the SVG has no closed shapes (only lines or text?)")
    return [c * [MM_PER_PX, -MM_PER_PX] for c in contours]     # px -> mm, y up


_DXF_UNITS = {0: 1.0, 1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0}


def _dxf_file(data: bytes, tol: float) -> list[np.ndarray]:
    import ezdxf
    from ezdxf import path as dxfpath
    try:
        doc = ezdxf.read(io.StringIO(data.decode("utf-8", "replace")))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"could not read the DXF file: {e}") from None
    k = _DXF_UNITS.get(doc.header.get("$INSUNITS", 0), 1.0)
    closed, open_ = [], []
    for e in doc.modelspace():
        if e.dxftype() not in ("LINE", "ARC", "CIRCLE", "ELLIPSE", "LWPOLYLINE", "POLYLINE", "SPLINE"):
            continue
        try:
            pts = np.array([(v.x, v.y) for v in dxfpath.make_path(e).flattening(tol / k)])
        except Exception:  # noqa: BLE001 – an entity ezdxf cannot turn into a path
            continue
        if len(pts) < 2:
            continue
        (closed if len(pts) >= 3 and np.allclose(pts[0], pts[-1], atol=1e-6) else open_).append(pts)
    closed += _chain(open_, tol / k)
    if not closed:
        raise ValueError("the DXF has no closed outlines (lines must meet end to end)")
    return [c * k for c in closed]


def _chain(pieces: list[np.ndarray], tol: float) -> list[np.ndarray]:
    """Joins open pieces (lines, arcs, open polylines) whose ends meet into closed loops."""
    loops, pieces = [], list(pieces)
    while pieces:
        cur = pieces.pop()
        for _ in range(10_000):
            if len(cur) >= 3 and np.linalg.norm(cur[0] - cur[-1]) <= tol:
                loops.append(cur)
                break
            for i, p in enumerate(pieces):
                if np.linalg.norm(p[0] - cur[-1]) <= tol:
                    cur = np.vstack([cur, p[1:]])
                elif np.linalg.norm(p[-1] - cur[-1]) <= tol:
                    cur = np.vstack([cur, p[::-1][1:]])
                else:
                    continue
                pieces.pop(i)
                break
            else:
                break                         # a dead end: not part of a closed outline
    return loops


def import_file(data: bytes, fmt: str, tol: float = 0.3) -> dict:
    """An SVG or DXF file as {"path", "size", "contours", "points"} (mm, y up, starting at 0, 0)."""
    fmt = fmt.lower().lstrip(".")
    if fmt == "svg":
        contours = _svg_file(data, tol)
    elif fmt == "dxf":
        contours = _dxf_file(data, tol)
    else:
        raise ValueError("outlines can be imported from svg or dxf files")
    contours = [c[:-1] if np.allclose(c[0], c[-1]) else c for c in contours]
    contours = [c for c in contours if len(c) >= 3]
    n = sum(len(c) for c in contours)
    if n > MAX_POINTS:
        raise ValueError(f"the outline has too many points ({n:,}, max {MAX_POINTS:,})")
    lo = np.vstack(contours).min(0)
    contours = [c - lo for c in contours]
    size = np.vstack(contours).max(0)
    path = to_path(contours)
    if len(path) > MAX_PATH:
        raise ValueError("the outline is too detailed - simplify it first")
    return {"path": path, "size": [round(float(size[0]), 2), round(float(size[1]), 2)],
            "contours": len(contours), "points": n}


HELP = """\
  svg profile: {"shape":"svg","path":"M0 0L40 0 40 20 0 20Z ...", "size"?:[w,h] (fit inside, either
           may be null) | "scale"?, "center"?, "fill"?:"evenodd" (default: inner loops are holes) |
           "nonzero", "flip_y"? (for a path copied straight from an SVG file, y down), "tolerance"?: 0.3
           (point spacing on curves, mm)}
           Path in mm, y up; import_outline turns an SVG/DXF file (logo, laser-cut part) into one.
           e.g. {"shape":"extrude","h":2,"profile":{"shape":"svg","path":"…","size":[60,null]}}
"""
