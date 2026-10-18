"""Expression-driven shapes for the CSG spec: radial, heightmap, sweep and loft.

They are built directly as triangle meshes (like _thread_rod in templates.py) so the AI can make
organic and parametric geometry without script models. Per-vertex expressions ("=10+2*sin(6*a)")
are parsed once and evaluated vectorized with numpy over the whole vertex grid. The evaluator only
allows arithmetic, comparisons, a if c else b and the whitelisted functions below – no attribute
access, subscripts, lambdas or other calls.
"""
from __future__ import annotations

import ast
import math
from functools import reduce

import manifold3d as mf
import numpy as np

M = mf.Manifold

MAX_TRIANGLES = 2_000_000
MAX_EXPR_NODES = 400

# ---------------------------------------------------------------- vectorized expressions

_D = math.pi / 180


def _hash(ix, iy, iz):
    """Deterministic pseudo random value in [-1, 1] per integer lattice point."""
    h = (ix * 374761393 + iy * 668265263 + iz * 2147483647) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
    return (h ^ (h >> 16)).astype(np.float64) / 0x7FFFFFFF - 1.0


def _noise(x, y=0.0, z=0.0):
    """Smooth value noise in about [-1, 1], one 'bump' per unit."""
    x, y, z = np.broadcast_arrays(*(np.asarray(v, np.float64) for v in (x, y, z)))
    i = [np.floor(v).astype(np.int64) for v in (x, y, z)]
    f = [v - np.floor(v) for v in (x, y, z)]
    s = [w * w * (3 - 2 * w) for w in f]
    out = 0.0
    for dx in (0, 1):
        for dy in (0, 1):
            for dz in (0, 1):
                wgt = ((s[0] if dx else 1 - s[0]) * (s[1] if dy else 1 - s[1])
                       * (s[2] if dz else 1 - s[2]))
                out = out + wgt * _hash(i[0] + dx, i[1] + dy, i[2] + dz)
    return out


def _smoothstep(e0, e1, x):
    t = np.clip((np.asarray(x, np.float64) - e0) / np.where(e1 == e0, 1e-12, np.subtract(e1, e0)), 0, 1)
    return t * t * (3 - 2 * t)


# Trigonometry in degrees, like the rest of the spec.
VFUNCS = {
    "sin": lambda d: np.sin(np.multiply(d, _D)), "cos": lambda d: np.cos(np.multiply(d, _D)),
    "tan": lambda d: np.tan(np.multiply(d, _D)),
    "asin": lambda v: np.degrees(np.arcsin(v)), "acos": lambda v: np.degrees(np.arccos(v)),
    "atan": lambda v: np.degrees(np.arctan(v)), "atan2": lambda y, x: np.degrees(np.arctan2(y, x)),
    "sqrt": np.sqrt, "abs": np.abs, "floor": np.floor, "ceil": np.ceil, "round": np.round,
    "exp": np.exp, "log": np.log, "sign": np.sign, "hypot": np.hypot,
    "min": lambda *a: reduce(np.minimum, a), "max": lambda *a: reduce(np.maximum, a),
    "clamp": lambda v, lo, hi: np.clip(v, lo, hi),
    "lerp": lambda a, b, t: np.add(a, np.multiply(np.subtract(b, a), t)),
    "smoothstep": _smoothstep,
    "noise": _noise,
}
_ARITY = {"atan2": (2, 2), "hypot": (2, 2), "clamp": (3, 3), "lerp": (3, 3), "smoothstep": (3, 3),
          "noise": (1, 3), "min": (1, 32), "max": (1, 32)}
_CONST = {"pi": math.pi}

_BIN = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply, ast.Div: np.true_divide,
        ast.FloorDiv: np.floor_divide, ast.Mod: np.mod}
_CMP = {ast.Lt: np.less, ast.LtE: np.less_equal, ast.Gt: np.greater, ast.GtE: np.greater_equal,
        ast.Eq: np.equal, ast.NotEq: np.not_equal}


def _pow(l, r):
    if np.any(np.abs(r) > 16):
        raise ValueError("exponent too large")
    return np.power(np.asarray(l, np.float64), r)


def compile_expr(expr: str, names) -> callable:
    """Parse expr once and return f(env) that evaluates it with numpy arrays (or numbers).
    names = the variables the expression may use (unknown names fail here, not per vertex)."""
    src = expr[1:] if expr.startswith("=") else expr
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"invalid expression '{src}': {e.msg}") from None
    if sum(1 for _ in ast.walk(tree)) > MAX_EXPR_NODES:
        raise ValueError(f"expression too long: '{src[:60]}…'")

    def c(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float, bool)) \
                and not isinstance(n.value, complex):
            v = float(n.value)
            return lambda e: v
        if isinstance(n, ast.Name):
            k = n.id
            if k in names:
                return lambda e: e[k]
            if k in _CONST:
                v = _CONST[k]
                return lambda e: v
            raise ValueError(f"unknown variable '{k}' in the expression '{src}' "
                             f"(available: {', '.join(sorted(names))})")
        if isinstance(n, ast.BinOp):
            l, r = c(n.left), c(n.right)
            if isinstance(n.op, ast.Pow):
                return lambda e: _pow(l(e), r(e))
            if type(n.op) in _BIN:
                f = _BIN[type(n.op)]
                return lambda e: f(l(e), r(e))
        if isinstance(n, ast.UnaryOp):
            o = c(n.operand)
            if isinstance(n.op, ast.USub):
                return lambda e: np.negative(o(e))
            if isinstance(n.op, ast.UAdd):
                return o
            if isinstance(n.op, ast.Not):
                return lambda e: np.logical_not(o(e))
        if isinstance(n, ast.BoolOp):
            parts = [c(v) for v in n.values]
            f = np.logical_and if isinstance(n.op, ast.And) else np.logical_or
            return lambda e: reduce(f, [p(e) for p in parts])
        if isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]) in _CMP:
            l, r, f = c(n.left), c(n.comparators[0]), _CMP[type(n.ops[0])]
            return lambda e: f(l(e), r(e))
        if isinstance(n, ast.IfExp):
            t, b, o = c(n.test), c(n.body), c(n.orelse)
            return lambda e: np.where(t(e), b(e), o(e))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in VFUNCS \
                and not n.keywords and not any(isinstance(a, ast.Starred) for a in n.args):
            lo, hi = _ARITY.get(n.func.id, (1, 1))
            if not lo <= len(n.args) <= hi:
                raise ValueError(f"{n.func.id}() takes {lo if lo == hi else f'{lo}–{hi}'} "
                                 f"argument(s) in '{src}'")
            f, args = VFUNCS[n.func.id], [c(a) for a in n.args]
            return lambda e: f(*[a(e) for a in args])
        raise ValueError(f"disallowed expression: '{src}' (allowed: arithmetic, comparisons, "
                         f"a if c else b, and {', '.join(VFUNCS)})")

    fn = c(tree.body)

    def run(env):
        with np.errstate(all="ignore"):
            return fn(env)
    return run


def field(value, env: dict, local: dict, what: str, shape=None):
    """Evaluate a number or "=expression" over the local arrays (x, y, z, a, t …) merged with the
    spec variables. Returns a float array broadcast to `shape`; non-finite results are an error."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{what} must be a number or an expression string")
    if isinstance(value, str):
        names = set(env) | set(local)
        out = compile_expr(value, names)({**env, **local})
    else:
        out = float(value)
    out = np.asarray(out, np.float64)
    if shape is not None:
        out = np.broadcast_to(out, shape)
    if not np.all(np.isfinite(out)):
        raise ValueError(f"{what}: the expression gives an invalid value (division by zero, sqrt "
                         f"of a negative number …) somewhere")
    return out


# ---------------------------------------------------------------- helpers

def _int(n, key, default, lo, hi):
    v = n.get(key, default)
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{n.get('shape')}: '{key}' must be a whole number") from None
    if not lo <= v <= hi:
        raise ValueError(f"{n.get('shape')}: '{key}' must be between {lo} and {hi}")
    return v


def _num(n, key, default=None):
    v = n.get(key, default)
    if v is None:
        raise ValueError(f"{n.get('shape')} requires '{key}'")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{n.get('shape')}: '{key}' must be a number")
    return float(v)


def _check_tris(kind, count):
    if count > MAX_TRIANGLES:
        raise ValueError(f"{kind}: too detailed ({count:,} triangles, max {MAX_TRIANGLES:,}) – "
                         f"lower segments/rows/resolution")


def _manifold(kind, V, F) -> M:
    m = M(mf.Mesh(vert_properties=np.ascontiguousarray(V, np.float32),
                  tri_verts=np.ascontiguousarray(F, np.uint32)))
    if m.status() != mf.Error.NoError:
        raise ValueError(f"{kind}: could not build a closed solid ({m.status().name})")
    return m


def _grid_faces(rows, cols, closed_cols=True, closed_rows=False, start=0):
    """Quads between consecutive rows of a rows × cols vertex grid (vertex id = start + i*cols + j)."""
    ni = rows if closed_rows else rows - 1
    nj = cols if closed_cols else cols - 1
    i, j = np.meshgrid(np.arange(ni), np.arange(nj), indexing="ij")
    a = start + i * cols + j
    b = start + i * cols + (j + 1) % cols
    c = start + ((i + 1) % rows) * cols + (j + 1) % cols
    e = start + ((i + 1) % rows) * cols + j
    return np.concatenate([np.stack([a, b, c], -1), np.stack([a, c, e], -1)]).reshape(-1, 3)


# ---------------------------------------------------------------- radial

def radial(n, env):
    """Solid around the z axis whose radius is an expression r(z, a) (a = angle in degrees)."""
    h = _num(n, "h")
    if h <= 0:
        raise ValueError("radial: h must be > 0")
    N = _int(n, "segments", 96, 3, 1024)
    rows = _int(n, "rows", max(2, min(1000, math.ceil(h / 0.4) + 1)), 2, 4000)
    _check_tris("radial", 2 * N * rows)
    z = np.linspace(0.0, h, rows)
    a = np.arange(N) * 360.0 / N
    Z, A = np.meshgrid(z, a, indexing="ij")
    loc = {"z": Z, "a": A, "t": Z / h, "h": h}
    if "r" in n:
        rv = n["r"]
    elif "d" in n:
        d = n["d"]
        rv = f"=({d.lstrip('=')})/2" if isinstance(d, str) else float(d) / 2
    else:
        raise ValueError("radial requires 'r' (radius, number or expression of z, a, t, h)")
    r = field(rv, env, loc, "radial: r", Z.shape)
    if np.min(r) <= 0:
        i = np.unravel_index(np.argmin(r), r.shape)
        raise ValueError(f"radial: the radius must stay > 0 (it is {r[i]:.3g} at z={Z[i]:.3g}, "
                         f"a={A[i]:.3g}°)")
    P = A * _D
    V = np.stack([r * np.cos(P), r * np.sin(P), Z], -1).reshape(-1, 3)
    V = np.vstack([V, [[0, 0, 0], [0, 0, h]]])
    cb, ct, jj = rows * N, rows * N + 1, np.arange(N)
    bottom = np.stack([np.full(N, cb), (jj + 1) % N, jj], -1)
    top = np.stack([np.full(N, ct), (rows - 1) * N + jj, (rows - 1) * N + (jj + 1) % N], -1)
    m = _manifold("radial", V, np.vstack([_grid_faces(rows, N), bottom, top]))
    return m.translate((0, 0, -h / 2)) if n.get("center") else m


# ---------------------------------------------------------------- heightmap

def heightmap(n, env):
    """Solid block over [0, sx] × [0, sy] from z=0 up to the surface z(x, y)."""
    size = n.get("size")
    if not isinstance(size, (list, tuple)) or len(size) != 2:
        raise ValueError("heightmap requires 'size': [sx, sy]")
    sx, sy = float(size[0]), float(size[1])
    if sx <= 0 or sy <= 0:
        raise ValueError("heightmap: size must be > 0")
    res = _num(n, "resolution", max(0.2, max(sx, sy) / 200))
    if res <= 0:
        raise ValueError("heightmap: resolution must be > 0 (grid step in mm)")
    nx, ny = max(2, math.ceil(sx / res) + 1), max(2, math.ceil(sy / res) + 1)
    _check_tris("heightmap", 2 * nx * ny + 4 * (nx + ny))
    if "z" not in n:
        raise ValueError("heightmap requires 'z' (height, expression of x, y, u, v)")
    x, y = np.linspace(0, sx, nx), np.linspace(0, sy, ny)
    Y, X = np.meshgrid(y, x, indexing="ij")                  # vertex id = j*nx + i
    loc = {"x": X, "y": Y, "u": X / sx, "v": Y / sy, "sx": sx, "sy": sy}
    Z = field(n["z"], env, loc, "heightmap: z", X.shape) + float(n.get("base", 0) or 0)
    if np.min(Z) <= 0.01:
        j, i = np.unravel_index(np.argmin(Z), Z.shape)
        raise ValueError(f"heightmap: the surface must stay above z=0 (it is {Z[j, i]:.3g} at "
                         f"x={X[j, i]:.3g}, y={Y[j, i]:.3g}) – add 'base' or raise the expression")
    top_v = np.stack([X, Y, Z], -1).reshape(-1, 3)
    # top surface: quad (i,j),(i+1,j),(i+1,j+1),(i,j+1) counter-clockwise from above → normal up
    i, j = np.meshgrid(np.arange(nx - 1), np.arange(ny - 1), indexing="ij")
    a, b = j * nx + i, j * nx + i + 1
    c, d = b + nx, a + nx
    top = np.concatenate([np.stack([a, b, c], -1), np.stack([a, c, d], -1)]).reshape(-1, 3)
    # boundary ring, counter-clockwise from above
    ring = np.concatenate([np.arange(nx - 1), (np.arange(ny - 1) * nx + nx - 1),
                           (ny - 1) * nx + np.arange(nx - 1, 0, -1), np.arange(ny - 1, 0, -1) * nx])
    R = len(ring)
    base0 = len(top_v)
    bot_v = top_v[ring].copy()
    bot_v[:, 2] = 0
    center = base0 + R
    k = np.arange(R)
    p, q = ring, ring[(k + 1) % R]
    pb, qb = base0 + k, base0 + (k + 1) % R
    walls = np.concatenate([np.stack([pb, qb, q], -1), np.stack([pb, q, p], -1)])
    bottom = np.stack([np.full(R, center), qb, pb], -1)
    V = np.vstack([top_v, bot_v, [[sx / 2, sy / 2, 0]]])
    m = _manifold("heightmap", V, np.vstack([top, walls, bottom]))
    return m.translate((-sx / 2, -sy / 2, 0)) if n.get("center") else m


# ---------------------------------------------------------------- paths

def _pts(v, what, dim_min=2):
    try:
        a = np.asarray(v, np.float64)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a list of [x, y, z] points") from None
    if a.ndim != 2 or a.shape[1] not in (2, 3) or len(a) < dim_min:
        raise ValueError(f"{what} must be a list of at least {dim_min} [x, y, z] points")
    if a.shape[1] == 2:
        a = np.hstack([a, np.zeros((len(a), 1))])
    if not np.all(np.isfinite(a)):
        raise ValueError(f"{what}: invalid coordinates")
    return a


def _catmull(P, steps, closed):
    """Centripetal-free uniform Catmull-Rom through P, `steps` samples per span."""
    n = len(P)
    if closed:
        ext = np.vstack([P[-1:], P, P[:2]])
        spans = n
    else:
        ext = np.vstack([2 * P[0] - P[1], P, 2 * P[-1] - P[-2]])
        spans = n - 1
    out = []
    u = np.linspace(0, 1, steps, endpoint=False)[:, None]
    for s in range(spans):
        p0, p1, p2, p3 = ext[s], ext[s + 1], ext[s + 2], ext[s + 3]
        out.append(0.5 * (2 * p1 + (-p0 + p2) * u + (2 * p0 - 5 * p1 + 4 * p2 - p3) * u ** 2
                          + (-p0 + 3 * p1 - 3 * p2 + p3) * u ** 3))
    if not closed:
        out.append(P[-1:])
    return np.vstack(out)


def _path(p, segs):
    """Path spec → (points (k,3), closed)."""
    if isinstance(p, list):
        p = {"type": "points", "points": p}
    if not isinstance(p, dict):
        raise ValueError("sweep: 'path' must be a list of points or {type: points|spline|arc|helix|bezier, …}")
    kind = p.get("type", "points")
    closed = bool(p.get("closed", False))
    if kind == "points":
        P = _pts(p.get("points"), "sweep path points")
    elif kind == "spline":
        P = _catmull(_pts(p.get("points"), "sweep spline points", 3 if closed else 2),
                     int(p.get("steps", 16)), closed)
    elif kind == "bezier":
        C = _pts(p.get("points"), "sweep bezier points", 4)
        if (len(C) - 1) % 3:
            raise ValueError("sweep: a bezier path needs 3k+1 points (start, c1, c2, end, c1, c2, end …)")
        steps = int(p.get("steps", 24))
        u = np.linspace(0, 1, steps, endpoint=False)[:, None]
        out = []
        for s in range(0, len(C) - 1, 3):
            p0, p1, p2, p3 = C[s:s + 4]
            out.append((1 - u) ** 3 * p0 + 3 * (1 - u) ** 2 * u * p1 + 3 * (1 - u) * u ** 2 * p2 + u ** 3 * p3)
        P = np.vstack(out + [C[-1:]])
    elif kind == "arc":
        r = float(p.get("r", 10))
        deg = float(p.get("degrees", 90))
        start = float(p.get("start", 0))
        if r <= 0 or deg == 0:
            raise ValueError("sweep: arc requires r > 0 and degrees ≠ 0")
        closed = closed or abs(abs(deg) - 360) < 1e-9
        k = max(3, math.ceil(segs * abs(deg) / 360))
        ang = (start + np.linspace(0, deg, k, endpoint=not closed)) * _D
        P = np.stack([r * np.cos(ang), r * np.sin(ang), np.zeros(k)], -1)
    elif kind == "helix":
        r = float(p.get("r", 10))
        pitch = float(p.get("pitch", 5))
        turns = float(p.get("turns", 1))
        if r <= 0 or turns <= 0:
            raise ValueError("sweep: helix requires r > 0 and turns > 0")
        k = max(4, math.ceil(segs * turns) + 1)
        th = np.linspace(0, turns * 2 * math.pi, k) * (-1 if p.get("left") else 1)
        P = np.stack([r * np.cos(th), r * np.sin(th), np.linspace(0, pitch * turns, k)], -1)
        closed = False
    else:
        raise ValueError(f"sweep: unknown path type '{kind}' (points, spline, arc, helix, bezier)")
    # drop repeated points (zero-length segments have no direction)
    keep = np.concatenate([[True], np.linalg.norm(np.diff(P, axis=0), axis=1) > 1e-9])
    P = P[keep]
    if closed and len(P) > 1 and np.linalg.norm(P[0] - P[-1]) < 1e-9:
        P = P[:-1]
    if len(P) < (3 if closed else 2):
        raise ValueError("sweep: the path is too short")
    return P, closed


def _rotate_min(v, a, b):
    """Rotate vector v by the smallest rotation that turns unit vector a into unit vector b."""
    ax = np.cross(a, b)
    s, c = np.linalg.norm(ax), float(np.dot(a, b))
    if s < 1e-12:
        return v
    ax /= s
    return v * c + np.cross(ax, v) * s + ax * np.dot(ax, v) * (1 - c)


def _frames(P, closed):
    """Rotation-minimizing frames for a polyline (exact discrete parallel transport: each segment's
    frame is the previous one turned by the minimal rotation between the segment directions).
    Returns per point: miter-plane normal T, outgoing direction D and that segment's frame X, Y."""
    n = len(P)
    d = (np.roll(P, -1, 0) - P) if closed else np.diff(P, axis=0)
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    if closed:
        d_in, d_out = np.roll(d, 1, 0), d
    else:
        d_in, d_out = np.vstack([d[:1], d]), np.vstack([d, d[-1:]])
    T = d_in + d_out
    tn = np.linalg.norm(T, axis=1, keepdims=True)
    if np.any(tn < 1e-6):
        raise ValueError("sweep: the path folds back on itself")
    T /= tn
    cos_half = np.einsum("ij,ij->i", T, d_out)
    if np.any(cos_half < 0.26):                                # > ~150° turn
        i = int(np.argmin(cos_half))
        raise ValueError(f"sweep: the path turns too sharply at point {i} – round the corner "
                         f"(use type spline/bezier or add points)")
    # initial frame: profile y toward +z (or +y if the path starts vertically), x = y × direction
    up = np.array([0, 0, 1.0]) if abs(d[0, 2]) < 0.99 else np.array([0, 1.0, 0])
    y = up - np.dot(up, d[0]) * d[0]
    Ys = [y / np.linalg.norm(y)]
    for i in range(1, len(d)):
        y = _rotate_min(Ys[-1], d[i - 1], d[i])
        y -= np.dot(y, d[i]) * d[i]
        Ys.append(y / np.linalg.norm(y))
    Y = np.array(Ys)
    if closed:
        # back at the start the frame is rolled by some angle: spread it evenly (no seam)
        yend = _rotate_min(Y[-1], d[-1], d[0])
        ang = math.atan2(np.dot(np.cross(yend, Y[0]), d[0]), np.dot(yend, Y[0]))
        seg = np.linalg.norm(np.roll(P, -1, 0) - P, axis=1)
        th = ang * np.concatenate([[0], np.cumsum(seg)[:-1]]) / seg.sum()
        X = np.cross(Y, d)
        Y = np.cos(th)[:, None] * Y - np.sin(th)[:, None] * X
    else:
        Y = np.vstack([Y, Y[-1:]])
    X = np.cross(Y, d_out)
    return T, d_out, X, Y


def _contours(cs, what):
    polys = [np.asarray(p, np.float64) for p in cs.to_polygons()]
    polys = [p for p in polys if len(p) >= 3]
    if not polys:
        raise ValueError(f"{what}: the profile is empty")
    return polys


def _cap(polys):
    """Triangles (counter-clockwise in the profile plane) over the concatenated contour points."""
    return np.asarray(mf.triangulate(polys), np.int64).reshape(-1, 3)


def _orient_ccw(tris, flat):
    """Make sure cap triangles are counter-clockwise (manifold's triangulate already is, but be safe)."""
    a, b, c = flat[tris[:, 0]], flat[tris[:, 1]], flat[tris[:, 2]]
    area = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    return np.where((area < 0)[:, None], tris[:, [0, 2, 1]], tris)


def _densify(P, closed, segs):
    """Split long straight segments so the path has about `segs` rings over its length."""
    Q = np.vstack([P, P[:1]]) if closed else P
    seg = np.linalg.norm(np.diff(Q, axis=0), axis=1)
    step = seg.sum() / segs
    out = []
    for i in range(len(Q) - 1):
        k = max(1, math.ceil(seg[i] / step - 1e-9))
        out.append(Q[i] + (Q[i + 1] - Q[i]) * (np.arange(k) / k)[:, None])
    if not closed:
        out.append(Q[-1:])
    return np.vstack(out)


def _uses_t(value):
    if not isinstance(value, str):
        return False
    try:
        tree = ast.parse(value.lstrip("="), mode="eval")
    except SyntaxError:
        return False
    return any(isinstance(x, ast.Name) and x.id == "t" for x in ast.walk(tree))


def sweep(n, env, profile_fn):
    """2D profile swept along a 3D path with rotation-minimizing frames, optional twist/taper."""
    if "path" not in n:
        raise ValueError("sweep requires 'path'")
    segs = _int(n, "segments", 64, 3, 1024)
    P, closed = _path(n["path"], segs)
    polys = _contours(profile_fn(n), "sweep")
    if n.get("twist") or n.get("taper", 1) != 1:
        P = _densify(P, closed, segs)                          # twist/taper need rings along straights
    T, D, X, Y = _frames(P, closed)
    k = len(P)
    seg = np.linalg.norm(np.diff(np.vstack([P, P[:1]]) if closed else P, axis=0), axis=1)
    s = np.concatenate([[0], np.cumsum(seg)])[:k] / seg.sum()
    loc = {"t": s, "L": float(seg.sum())}
    tw = field(n.get("twist", 0), env, loc, "sweep: twist", s.shape)
    if not _uses_t(n.get("twist")):                            # a plain value ramps 0 → value
        tw = tw * s
    sc = field(n.get("taper", 1), env, loc, "sweep: taper", s.shape)
    if not _uses_t(n.get("taper")):                            # ramps 1 → value
        sc = 1 + (sc - 1) * s
    if np.min(sc) <= 0:
        raise ValueError("sweep: taper must stay > 0")
    flat = np.vstack(polys)
    C = len(flat)
    _check_tris("sweep", 2 * C * k + 2 * C)
    th = np.radians(tw)
    # profile point (u, v) at path point i: twist, scale, place in the outgoing segment's frame,
    # then project along that segment onto the miter plane (exact mitred corners)
    u = flat[None, :, 0] * sc[:, None]
    v = flat[None, :, 1] * sc[:, None]
    cu, su = np.cos(th)[:, None], np.sin(th)[:, None]
    u, v = u * cu - v * su, u * su + v * cu
    off = u[..., None] * X[:, None, :] + v[..., None] * Y[:, None, :]
    lam = np.einsum("ijk,ik->ij", off, T) / np.einsum("ik,ik->i", D, T)[:, None]
    off -= lam[..., None] * D[:, None, :]
    V = (P[:, None, :] + off).reshape(-1, 3)                 # vertex id = i*C + flat index
    faces, start = [], 0
    for poly in polys:
        m = len(poly)
        idx = np.arange(m)
        ii, jj = np.meshgrid(np.arange(k if closed else k - 1), idx, indexing="ij")
        i2 = (ii + 1) % k
        a, b = ii * C + start + jj, ii * C + start + (jj + 1) % m
        c, e = i2 * C + start + (jj + 1) % m, i2 * C + start + jj
        faces.append(np.concatenate([np.stack([a, b, c], -1), np.stack([a, c, e], -1)]).reshape(-1, 3))
        start += m
    if not closed:
        cap = _orient_ccw(_cap(polys), flat)
        faces.append(cap[:, [0, 2, 1]])                       # start cap faces backwards
        faces.append(cap + (k - 1) * C)
    return _manifold("sweep", V, np.vstack(faces))


# ---------------------------------------------------------------- loft

def _resample(poly, params):
    """Points on the closed polygon at the given arc-length fractions (0..1)."""
    seg = np.linalg.norm(np.roll(poly, -1, 0) - poly, axis=1)
    cum = np.concatenate([[0], np.cumsum(seg)]) / seg.sum()
    closed = np.vstack([poly, poly[:1]])
    return np.stack([np.interp(params, cum, closed[:, 0]), np.interp(params, cum, closed[:, 1])], -1)


def _align(poly):
    """Counter-clockwise, starting at the vertex in the +x direction of the centroid."""
    area = np.sum(poly[:, 0] * np.roll(poly[:, 1], -1) - np.roll(poly[:, 0], -1) * poly[:, 1])
    if area < 0:
        poly = poly[::-1]
    c = poly.mean(axis=0)
    ang = np.mod(np.arctan2(poly[:, 1] - c[1], poly[:, 0] - c[0]), 2 * np.pi)
    sel = np.argmin(np.minimum(ang, 2 * np.pi - ang) + 1e-9 * np.abs(poly[:, 0] - c[0]))
    return np.roll(poly, -sel, 0)


def _fractions(poly):
    seg = np.linalg.norm(np.roll(poly, -1, 0) - poly, axis=1)
    return np.concatenate([[0], np.cumsum(seg)[:-1]]) / seg.sum()


def loft(n, env, profile_fn):
    """Skin between 2+ cross sections at increasing z heights."""
    secs = n.get("sections")
    if not isinstance(secs, list) or len(secs) < 2:
        raise ValueError("loft requires 'sections': [{z, profile}, {z, profile}, …] (at least 2)")
    segs = _int(n, "segments", 96, 8, 2048)
    rings, zs = [], []
    for i, s in enumerate(secs):
        if not isinstance(s, dict) or "z" not in s:
            raise ValueError(f"loft: section {i} needs 'z' and 'profile'")
        polys = _contours(profile_fn(s), f"loft section {i}")
        if len(polys) != 1:
            raise ValueError(f"loft: section {i} must be one closed outline without holes "
                             f"(cut holes afterwards with difference)")
        poly = _align(polys[0])
        off = s.get("offset", [0, 0])
        rot = float(s.get("rotate", 0)) * _D
        c, sn = math.cos(rot), math.sin(rot)
        poly = poly @ np.array([[c, sn], [-sn, c]]) + np.asarray(off, np.float64)[:2]
        rings.append(poly)
        zs.append(float(s["z"]))
    if any(b <= a for a, b in zip(zs, zs[1:])):
        raise ValueError("loft: section z values must increase")
    params = np.unique(np.round(np.concatenate([np.linspace(0, 1, segs, endpoint=False)]
                                               + [_fractions(r) for r in rings]), 9))
    params = params[params < 1]
    R = np.array([_resample(r, params) for r in rings])      # (sections, N, 2)
    Z = np.array(zs)
    div = _int(n, "divisions", 8 if n.get("smooth") else 1, 1, 200)
    if n.get("smooth") and len(R) > 2:
        ext = np.concatenate([2 * R[:1] - R[1:2], R, 2 * R[-1:] - R[-2:-1]])
        u = np.linspace(0, 1, div, endpoint=False)[:, None, None]
        rows, zrows = [], []
        for s in range(len(R) - 1):
            p0, p1, p2, p3 = ext[s], ext[s + 1], ext[s + 2], ext[s + 3]
            rows.append(0.5 * (2 * p1 + (-p0 + p2) * u + (2 * p0 - 5 * p1 + 4 * p2 - p3) * u ** 2
                               + (-p0 + 3 * p1 - 3 * p2 + p3) * u ** 3))
            zrows.append(Z[s] + (Z[s + 1] - Z[s]) * u[:, 0, 0])
        R = np.concatenate(rows + [R[-1:]])
        Z = np.concatenate(zrows + [Z[-1:]])
    elif div > 1:
        u = np.linspace(0, 1, div, endpoint=False)[:, None, None]
        R = np.concatenate([R[s] + (R[s + 1] - R[s]) * u for s in range(len(R) - 1)] + [R[-1:]])
        Z = np.concatenate([Z[s] + (Z[s + 1] - Z[s]) * u[:, 0, 0] for s in range(len(Z) - 1)] + [Z[-1:]])
    rows, N = R.shape[0], R.shape[1]
    _check_tris("loft", 2 * rows * N + 2 * N)
    V = np.concatenate([R, np.broadcast_to(Z[:, None, None], (rows, N, 1))], -1).reshape(-1, 3)
    bot = _orient_ccw(_cap([R[0]]), R[0])
    top = _orient_ccw(_cap([R[-1]]), R[-1])
    F = np.vstack([_grid_faces(rows, N), bot[:, [0, 2, 1]], top + (rows - 1) * N])
    return _manifold("loft", V, F)


SHAPES = {"radial": radial, "heightmap": heightmap, "sweep": sweep, "loft": loft}
# fields that hold per-vertex expressions: they are evaluated here, not by csg.resolve
LAZY = {"radial": ("r", "d"), "heightmap": ("z",), "sweep": ("twist", "taper")}
NEEDS_PROFILE = {"sweep", "loft"}

HELP = """\
Expression shapes (no scripts needed for organic / parametric forms). Per-vertex expressions use
the spec vars plus local variables; trig in degrees; extra functions: asin acos atan atan2 exp log
sign hypot clamp(v,lo,hi) lerp(a,b,t) smoothstep(e0,e1,x) noise(x[,y[,z]]) (smooth, ≈ -1..1 per unit)
  radial    h, r: radius as expression of z, a (angle °), t (=z/h), h; segments=96, rows?, center?
            e.g. vase {"shape":"radial","h":60,"r":"=20+6*sin(t*180)+1.5*cos(8*a)"}
            (radius must stay > 0; a twist is just a phase shift: "=15+2*cos(6*(a+90*t))")
  heightmap size:[sx,sy], z: height as expression of x, y, u (=x/sx), v (=y/sy); base=0 (added to
            z), resolution (grid step mm), center? – solid from z=0 up to the surface (must stay > 0)
  sweep     profile (2D, as for extrude), path, twist?, taper?, segments – the profile's (x, y) is
            placed perpendicular to the path (y toward +z where possible), with rotation-minimizing
            frames and mitred corners. twist (degrees) / taper (profile scale): a value (or expression without t)
            ramps linearly from 0 / 1 to it along the path; an expression using t (0..1 along the
            path; L = path length) is the value at t, e.g. "taper":"=1+0.3*sin(t*360)".
            path: [[x,y,z],…] | {"type":"spline","points":[…],"closed"?} (smooth through points)
            | {"type":"arc","r","degrees","start"?} (xy plane) | {"type":"helix","r","pitch","turns",
            "left"?} | {"type":"bezier","points":[p0,c1,c2,p1,c1,c2,p2…]}; "closed":true joins the
            ends (ring, no caps). Keep bends wider than the profile, or the solid overlaps itself.
  loft      sections:[{"z":0,"profile":{…},"offset"?:[x,y],"rotate"?:deg}, …] (z increasing, one
            outline each), smooth?:true (curved through the sections), divisions?, segments=96
Profiles (extrude, revolve, sweep, loft) also accept {"shape":"spline","points":[[x,y],…]} (smooth
  closed curve), {"op":"union|difference|intersection","profiles":[…]} and "translate":[x,y],
  "rotate":deg on any profile.
"""
