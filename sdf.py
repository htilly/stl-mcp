"""Signed-distance shapes for the CSG spec: shape "sdf".

A small tree of distance-field primitives and operations, evaluated with numpy over a grid and
turned into a mesh with marching cubes. What it adds over plain CSG is blending: smooth unions
and differences that melt shapes into each other with a fillet of radius `smooth`, even shells,
rounding and twisting of whole shapes - grips, figures, organic transitions. The result is an
ordinary solid, so it can be combined with every other node.

    {"shape": "sdf", "resolution": 0.5, "sdf":
      {"op": "union", "smooth": 4, "children": [
        {"sdf": "sphere", "d": 30},
        {"sdf": "capsule", "a": [0, 0, 0], "b": [0, 0, 40], "r": 6}]}}

Distances are negative inside. Primitives are centred on the origin; every node takes
translate / rotate / scale (uniform) and the modifiers shell, round and twist.
"""
from __future__ import annotations

import math

import manifold3d as mf
import numpy as np

M = mf.Manifold

MAX_POINTS = 12_000_000     # grid points; about 230³
MAX_NODES = 200
CHUNK = 1_000_000           # points evaluated at once (bounds the memory)
PRIMITIVES = ("sphere", "box", "cylinder", "capsule", "torus", "cone", "ellipsoid")
OPS = ("union", "difference", "intersection")


def _num(n, key, what, default=None):
    v = n.get(key, default)
    if v is None:
        raise ValueError(f"sdf {what} requires '{key}'")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"sdf {what}: '{key}' must be a number")
    return float(v)


def _vec(v, what, key):
    if isinstance(v, bool) or not isinstance(v, (list, tuple)) or len(v) != 3 \
            or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
        raise ValueError(f"sdf {what}: '{key}' must be [x, y, z]")
    return np.array(v, np.float64)


def _radius(n, what):
    if "r" in n:
        r = _num(n, "r", what)
    elif "d" in n:
        r = _num(n, "d", what) / 2
    else:
        raise ValueError(f"sdf {what} requires 'r' or 'd'")
    if r <= 0:
        raise ValueError(f"sdf {what}: the radius must be > 0")
    return r


def _rot(deg) -> np.ndarray:
    """Rotation about x, then y, then z (degrees), like the CSG spec's rotate."""
    ax, ay, az = (math.radians(a) for a in deg)
    cx, sx, cy, sy, cz, sz = math.cos(ax), math.sin(ax), math.cos(ay), math.sin(ay), math.cos(az), math.sin(az)
    rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return rz @ ry @ rx


def _smin(a, b, k):
    """Polynomial smooth minimum: a fillet of about radius k where the two surfaces meet."""
    h = np.clip(0.5 + 0.5 * (b - a) / k, 0, 1)
    return b + (a - b) * h - k * h * (1 - h)


def _smax(a, b, k):
    return -_smin(-a, -b, k)


# ---------------------------------------------------------------- compiling the tree

class Node:
    """f(P) -> distances for points P (n×3), and the bounds (lo, hi) the shape lies within."""

    def __init__(self, f, lo, hi):
        self.f, self.lo, self.hi = f, np.asarray(lo, np.float64), np.asarray(hi, np.float64)


def _primitive(n) -> Node:
    kind = n["sdf"]
    what = kind
    if kind == "sphere":
        r = _radius(n, what)
        return Node(lambda P: np.linalg.norm(P, axis=1) - r, [-r] * 3, [r] * 3)
    if kind == "ellipsoid":
        s = _vec(n.get("size"), what, "size") / 2          # full size, like box
        if np.any(s <= 0):
            raise ValueError("sdf ellipsoid: size must be > 0")

        def ellipsoid(P):                                   # a good approximation (Quilez)
            k0 = np.linalg.norm(P / s, axis=1)
            k1 = np.linalg.norm(P / (s * s), axis=1)
            return k0 * (k0 - 1) / np.maximum(k1, 1e-12)
        return Node(ellipsoid, -s, s)
    if kind == "box":
        b = _vec(n.get("size"), what, "size") / 2
        if np.any(b <= 0):
            raise ValueError("sdf box: size must be > 0")
        r = _num(n, "radius", what, 0)
        if r < 0 or r >= b.min():
            raise ValueError("sdf box: radius must be >= 0 and smaller than half the smallest side")
        inner = b - r

        def box(P):
            q = np.abs(P) - inner
            return (np.linalg.norm(np.maximum(q, 0), axis=1) + np.minimum(q.max(axis=1), 0)) - r
        return Node(box, -b, b)
    if kind == "cylinder":
        r, h = _radius(n, what), _num(n, "h", what)
        if h <= 0:
            raise ValueError("sdf cylinder: h must be > 0")

        def cylinder(P):
            d = np.stack([np.hypot(P[:, 0], P[:, 1]) - r, np.abs(P[:, 2]) - h / 2], 1)
            return np.minimum(d.max(axis=1), 0) + np.linalg.norm(np.maximum(d, 0), axis=1)
        return Node(cylinder, [-r, -r, -h / 2], [r, r, h / 2])
    if kind == "cone":                                        # capped cone along z, centred
        h = _num(n, "h", what)
        r1 = n["r1"] if "r1" in n else n.get("d1", 0) / 2
        r2 = n["r2"] if "r2" in n else n.get("d2", 0) / 2
        r1, r2 = _num({"r1": r1}, "r1", what), _num({"r2": r2}, "r2", what)
        if h <= 0 or r1 < 0 or r2 < 0 or r1 + r2 <= 0:
            raise ValueError("sdf cone: h > 0, r1/d1 (bottom) and r2/d2 (top) >= 0, not both 0")
        hh = h / 2

        def cone(P):                                          # Quilez' capped cone
            qx, qy = np.hypot(P[:, 0], P[:, 1]), P[:, 2]
            k2 = np.array([r2 - r1, 2 * hh])
            cax = qx - np.minimum(qx, np.where(qy < 0, r1, r2))
            cay = np.abs(qy) - hh
            t = np.clip(((r2 - qx) * k2[0] + (hh - qy) * k2[1]) / k2.dot(k2), 0, 1)
            cbx, cby = qx - r2 + k2[0] * t, qy - hh + k2[1] * t
            s = np.where((cbx < 0) & (cay < 0), -1.0, 1.0)
            return s * np.sqrt(np.minimum(cax * cax + cay * cay, cbx * cbx + cby * cby))
        rm = max(r1, r2)
        return Node(cone, [-rm, -rm, -hh], [rm, rm, hh])
    if kind == "capsule":
        a, b, r = _vec(n.get("a"), what, "a"), _vec(n.get("b"), what, "b"), _num(n, "r", what)
        if r <= 0:
            raise ValueError("sdf capsule: r must be > 0")
        ba = b - a
        L = ba.dot(ba)

        def capsule(P):
            pa = P - a
            h = np.clip(pa @ ba / L, 0, 1) if L > 0 else np.zeros(len(P))
            return np.linalg.norm(pa - h[:, None] * ba, axis=1) - r
        return Node(capsule, np.minimum(a, b) - r, np.maximum(a, b) + r)
    if kind == "torus":
        R = _radius(n, what)
        t = _num(n, "tube_r", what) if "tube_r" in n else _num(n, "tube_d", what) / 2
        if t <= 0 or t >= R:
            raise ValueError("sdf torus: 0 < tube_r < r")
        return Node(lambda P: np.hypot(np.hypot(P[:, 0], P[:, 1]) - R, P[:, 2]) - t,
                    [-R - t, -R - t, -t], [R + t, R + t, t])
    raise ValueError(f"unknown sdf primitive '{kind}' ({', '.join(PRIMITIVES)})")


def _op(n, kids: list[Node]) -> Node:
    op, k = n["op"], _num(n, "smooth", f"op {n['op']}", 0)
    if k < 0:
        raise ValueError("sdf: smooth must be >= 0")
    if op == "union":
        def f(P):
            d = kids[0].f(P)
            for c in kids[1:]:
                d = _smin(d, c.f(P), k) if k else np.minimum(d, c.f(P))
            return d
        lo = np.min([c.lo for c in kids], 0) - k
        hi = np.max([c.hi for c in kids], 0) + k
    elif op == "intersection":
        def f(P):
            d = kids[0].f(P)
            for c in kids[1:]:
                d = _smax(d, c.f(P), k) if k else np.maximum(d, c.f(P))
            return d
        lo, hi = np.max([c.lo for c in kids], 0), np.min([c.hi for c in kids], 0)
    elif op == "difference":
        def f(P):
            d = kids[0].f(P)
            for c in kids[1:]:
                d = _smax(d, -c.f(P), k) if k else np.maximum(d, -c.f(P))
            return d
        lo, hi = kids[0].lo - k, kids[0].hi + k
    else:
        raise ValueError(f"unknown sdf op '{op}' ({', '.join(OPS)})")
    return Node(f, lo, hi)


def _corners(lo, hi):
    return np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])


def _modify(n, node: Node) -> Node:
    """Modifiers, in this order: twist, shell, round, scale, rotate, translate."""
    f, lo, hi = node.f, node.lo, node.hi
    if "twist" in n:                     # degrees per mm along z, about the z axis
        tw = math.radians(_num(n, "twist", "twist"))
        inner = f

        def f(P, inner=inner, tw=tw):
            a = -tw * P[:, 2]
            c, s = np.cos(a), np.sin(a)
            Q = np.stack([c * P[:, 0] - s * P[:, 1], s * P[:, 0] + c * P[:, 1], P[:, 2]], 1)
            return inner(Q) / math.sqrt(1 + (tw * np.abs(Q[:, :2]).max()) ** 2 if len(Q) else 1)
        R = np.abs(_corners(lo, hi)[:, :2]).max() * math.sqrt(2)
        lo, hi = np.array([-R, -R, lo[2]]), np.array([R, R, hi[2]])
    if "shell" in n:                     # an even wall of this thickness around the surface
        t = _num(n, "shell", "shell")
        if t <= 0:
            raise ValueError("sdf: shell must be > 0 (the wall thickness)")
        inner = f
        f = lambda P, inner=inner, t=t: np.abs(inner(P)) - t / 2      # noqa: E731
        lo, hi = lo - t / 2, hi + t / 2
    if "round" in n:                     # grows the shape by r, rounding its edges
        r = _num(n, "round", "round")
        inner = f
        f = lambda P, inner=inner, r=r: inner(P) - r                  # noqa: E731
        lo, hi = lo - max(r, 0), hi + max(r, 0)
    if "scale" in n:
        s = _num(n, "scale", "scale")
        if s <= 0:
            raise ValueError("sdf: scale must be a number > 0 (uniform)")
        inner = f
        f = lambda P, inner=inner, s=s: inner(P / s) * s             # noqa: E731
        lo, hi = lo * s, hi * s
    if "rotate" in n:
        R = _rot(_vec(n["rotate"], "rotate", "rotate"))
        inner = f
        f = lambda P, inner=inner, R=R: inner(P @ R)                 # P @ R = R⁻¹ applied to rows
        c = _corners(lo, hi) @ R.T
        lo, hi = c.min(0), c.max(0)
    if "translate" in n:
        t = _vec(n["translate"], "translate", "translate")
        inner = f
        f = lambda P, inner=inner, t=t: inner(P - t)                 # noqa: E731
        lo, hi = lo + t, hi + t
    return Node(f, lo, hi)


def compile_tree(node, resolve, depth=0, count=None) -> Node:
    """The node tree as one distance function. resolve(dict) evaluates "=expr" values."""
    count = count if count is not None else [0]
    count[0] += 1
    if count[0] > MAX_NODES or depth > 32:
        raise ValueError(f"the sdf tree is too big (max {MAX_NODES} nodes, 32 deep)")
    if not isinstance(node, dict):
        raise ValueError("sdf nodes must be objects")
    n = resolve(node)
    if "op" in n:
        kids = n.get("children")
        if not isinstance(kids, list) or not kids:
            raise ValueError(f"sdf op '{n['op']}' requires 'children'")
        out = _op(n, [compile_tree(c, resolve, depth + 1, count) for c in kids])
    elif "sdf" in n:
        out = _primitive(n)
    else:
        raise ValueError(f"an sdf node needs 'sdf' ({', '.join(PRIMITIVES)}) or 'op' ({', '.join(OPS)})")
    return _modify(n, out)


# ---------------------------------------------------------------- meshing

def mesh(tree: Node, resolution: float | None = None) -> M:
    from skimage.measure import marching_cubes

    lo, hi = tree.lo, tree.hi
    if np.any(hi <= lo) or not np.all(np.isfinite([lo, hi])):
        raise ValueError("sdf: the shape is empty (an intersection or difference left nothing)")
    ext = hi - lo
    if resolution is None:                # fine enough for prints, within the point budget
        resolution = max(ext.max() / 150, (np.prod(ext) / MAX_POINTS * 1.5) ** (1 / 3), 0.2)
    if resolution <= 0:
        raise ValueError("sdf: resolution must be > 0 (the grid step in mm)")
    pad = 2 * resolution
    lo, hi = lo - pad, hi + pad
    shape = np.ceil((hi - lo) / resolution).astype(int) + 1
    if np.prod(shape.astype(np.float64)) > MAX_POINTS:
        raise ValueError(f"sdf: too fine ({int(np.prod(shape.astype(np.float64))):,} grid points, "
                         f"max {MAX_POINTS:,}) - raise 'resolution' (now {resolution:.3g} mm)")
    axes = [lo[i] + np.arange(shape[i]) * resolution for i in range(3)]
    vol = np.empty(shape, np.float32)
    plane = shape[1] * shape[2]
    X, Y = np.meshgrid(axes[1], axes[2], indexing="ij")
    yz = np.stack([X.ravel(), Y.ravel()], 1)
    step = max(1, CHUNK // plane)
    with np.errstate(all="ignore"):
        for i in range(0, shape[0], step):
            xs = axes[0][i:i + step]
            P = np.empty((len(xs) * plane, 3))
            P[:, 0] = np.repeat(xs, plane)
            P[:, 1:] = np.tile(yz, (len(xs), 1))
            vol[i:i + len(xs)] = tree.f(P).reshape(len(xs), shape[1], shape[2])
    if not np.all(np.isfinite(vol)):
        raise ValueError("sdf: the distance is not a number somewhere (check the sizes)")
    # the outermost layer is outside by construction, so the surface is always closed
    vol[0], vol[-1], vol[:, 0], vol[:, -1], vol[:, :, 0], vol[:, :, -1] = (resolution,) * 6
    if vol.min() >= 0:
        raise ValueError("sdf: the shape is empty (or thinner than the resolution - lower it)")
    V, F, _, _ = marching_cubes(vol, 0.0, spacing=(resolution,) * 3, allow_degenerate=False)
    V += lo
    m = M(mf.Mesh(vert_properties=np.ascontiguousarray(V, np.float32),
                  tri_verts=np.ascontiguousarray(F[:, ::-1], np.uint32)))   # outward normals
    if m.status() != mf.Error.NoError:
        raise ValueError(f"sdf: could not build a closed solid ({m.status().name}) - try another "
                         "resolution")
    if m.volume() < 0:
        m = M(mf.Mesh(vert_properties=np.ascontiguousarray(V, np.float32),
                      tri_verts=np.ascontiguousarray(F, np.uint32)))
    return m


def build(n: dict, resolve) -> M:
    """The CSG node {"shape": "sdf", "sdf": {tree}, "resolution"?} as a manifold."""
    if "sdf" not in n or not isinstance(n["sdf"], dict):
        raise ValueError("shape sdf requires 'sdf': {a tree of sdf nodes}")
    res = n.get("resolution")
    if res is not None and (isinstance(res, bool) or not isinstance(res, (int, float))):
        raise ValueError("sdf: resolution must be a number (mm)")
    return mesh(compile_tree(n["sdf"], resolve), None if res is None else float(res))


HELP = """\
SDF shape (blends and organic forms; an ordinary solid afterwards, combine it with anything):
  {"shape":"sdf","sdf":{tree},"resolution"?: grid step mm (default: size/150, at least 0.2;
    finer = slower; details thinner than about 2 steps disappear)}
  tree nodes: primitives, centred on the origin: {"sdf":"sphere","d"|"r"}, {"sdf":"box","size":[x,y,z],
    "radius"?}, {"sdf":"cylinder","d"|"r","h"} (along z), {"sdf":"cone","h","d1"|"r1","d2"|"r2"},
    {"sdf":"capsule","a":[x,y,z],"b":[x,y,z],"r"}, {"sdf":"torus","r","tube_r"}, {"sdf":"ellipsoid","size":[x,y,z]}
  ops: {"op":"union|difference|intersection","children":[…],"smooth"?: k} – smooth = blend radius in
    mm (melts shapes together with a fillet; for difference/intersection a rounded inside edge)
  on any node: translate [x,y,z], rotate [x,y,z] (deg), scale (uniform number), shell (wall
    thickness: hollow, even wall), round (grow by r and round the edges), twist (deg per mm about z)
  e.g. a grip: {"shape":"sdf","sdf":{"op":"union","smooth":6,"children":[
    {"sdf":"capsule","a":[0,0,0],"b":[0,0,90],"r":14},{"sdf":"torus","r":18,"tube_r":4,"translate":[0,0,25]}]}}
"""
