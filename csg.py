"""Deklarativ CSG: ett JSON-träd av former, transformer och booleska operationer -> mesh (mm).

Byggs med manifold3d. Formatet är tänkt att vara lätt för Claude att skriva OCH att justera
i efterhand (ändra en variabel eller ett enskilt värde via JSON-pekare).

Exempel:
{
  "vars": {"w": 40, "wall": 2},
  "root": {"op": "difference", "children": [
      {"shape": "box", "size": ["=w", "=w", 20], "radius": 3},
      {"shape": "cylinder", "d": "=w-2*wall", "h": 30, "translate": ["=w/2", "=w/2", "=wall"]}
  ]}
}

Strängar som börjar med "=" räknas ut som uttryck med variablerna i "vars".
"""
from __future__ import annotations

import ast
import math
import operator

import manifold3d as mf
import numpy as np
import trimesh

M = mf.Manifold
CS = mf.CrossSection

DEFAULT_SEGMENTS = 64

# ---------------------------------------------------------------- uttryck

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
        ast.Pow: operator.pow}
_UN = {ast.USub: operator.neg, ast.UAdd: operator.pos}
_CMP = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
        ast.Eq: operator.eq, ast.NotEq: operator.ne}
_FUNCS = {"min": min, "max": max, "abs": abs, "round": round, "sqrt": math.sqrt,
          "sin": lambda d: math.sin(math.radians(d)), "cos": lambda d: math.cos(math.radians(d)),
          "tan": lambda d: math.tan(math.radians(d)), "floor": math.floor, "ceil": math.ceil}
_CONST = {"pi": math.pi}


def eval_expr(expr: str, env: dict):
    """Säker aritmetik: + - * / // % **, jämförelser, a if c else b, min/max/sqrt/sin(grader)..."""
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float, bool)):
            return n.value
        if isinstance(n, ast.Name):
            if n.id in env:
                return env[n.id]
            if n.id in _CONST:
                return _CONST[n.id]
            raise ValueError(f"okänd variabel '{n.id}' i uttrycket '{expr}'")
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            l, r = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(r) > 16:
                raise ValueError("för stor exponent")
            return _BIN[type(n.op)](l, r)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _UN:
            return _UN[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]) in _CMP:
            return _CMP[type(n.ops[0])](ev(n.left), ev(n.comparators[0]))
        if isinstance(n, ast.IfExp):
            return ev(n.body) if ev(n.test) else ev(n.orelse)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS \
                and not n.keywords:
            return _FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise ValueError(f"otillåtet uttryck: '{expr}'")
    return ev(ast.parse(expr, mode="eval"))


def resolve(value, env: dict):
    """Ersätt alla "=uttryck" rekursivt."""
    if isinstance(value, str) and value.startswith("="):
        return eval_expr(value[1:], env)
    if isinstance(value, list):
        return [resolve(v, env) for v in value]
    if isinstance(value, dict):
        return {k: (v if k in ("children", "child") else resolve(v, env)) for k, v in value.items()}
    return value


def resolve_vars(vars_: dict) -> dict:
    """Variabler får referera till tidigare variabler (i ordning)."""
    env: dict = {}
    for k, v in (vars_ or {}).items():
        env[k] = resolve(v, env)
    return env


# ---------------------------------------------------------------- former

def _vec3(v, name="vektor"):
    if isinstance(v, (int, float)):
        return (float(v),) * 3
    if not isinstance(v, (list, tuple)) or len(v) != 3:
        raise ValueError(f"{name} måste vara [x, y, z]")
    return tuple(float(x) for x in v)


def _segs(n):
    s = int(n.get("segments", 0) or 0)
    return min(s, 512) if s > 0 else DEFAULT_SEGMENTS


def _radius(n, key_d="d", key_r="r"):
    if key_r in n:
        return float(n[key_r])
    if key_d in n:
        return float(n[key_d]) / 2
    raise ValueError(f"{n.get('shape')} kräver '{key_d}' (diameter) eller '{key_r}' (radie)")


def _rounded_box(size, r, segs):
    x, y, z = size
    r = min(r, x / 2, y / 2, z / 2)
    if r <= 0:
        return M.cube(size)
    sph = M.sphere(r, max(16, segs // 2))
    corners = [sph.translate((cx, cy, cz))
               for cx in (r, x - r) for cy in (r, y - r) for cz in (r, z - r)]
    return M.batch_hull(corners)


def _rounded_rect_box(size, r, segs):
    """Box med rundade vertikala kanter (bara i xy) – vanligast för utskrifter."""
    x, y, z = size
    r = min(r, x / 2, y / 2)
    if r <= 0:
        return M.cube(size)
    cs = CS.square((x - 2 * r, y - 2 * r)).translate((r, r)).offset(r, mf.JoinType.Round,
                                                                        circular_segments=segs)
    return cs.extrude(z)


def _polygon(n):
    pts = n.get("points")
    if not pts or len(pts) < 3:
        raise ValueError("polygon kräver 'points': [[x,y], ...] (minst 3)")
    contours = [pts] + list(n.get("holes", []))
    return CS(contours, mf.FillRule.EvenOdd)


def _profile(n):
    """2D-profil för extrude/revolve: polygon, circle, rect eller text-fri enkel form."""
    p = n.get("profile")
    if p is None:
        return _polygon(n)
    kind = p.get("shape", "polygon")
    segs = _segs(p)
    if kind == "polygon":
        cs = _polygon(p)
    elif kind == "circle":
        cs = CS.circle(_radius(p), segs)
    elif kind == "rect":
        sx, sy = p["size"]
        cs = CS.square((sx, sy), bool(p.get("center", False)))
        if p.get("radius"):
            r = float(p["radius"])
            cs = cs.offset(-r, mf.JoinType.Miter).offset(r, mf.JoinType.Round, circular_segments=segs)
    else:
        raise ValueError(f"okänd profil '{kind}' (polygon, circle, rect)")
    if p.get("offset"):
        cs = cs.offset(float(p["offset"]), mf.JoinType.Round, circular_segments=segs)
    return cs


def _shape(n) -> M:
    kind = n["shape"]
    segs = _segs(n)
    center = bool(n.get("center", False))
    if kind in ("box", "cube"):
        size = _vec3(n.get("size"), "size")
        r = float(n.get("radius", 0) or 0)
        if r > 0:
            m = (_rounded_rect_box if n.get("round") == "vertical" else _rounded_box)(size, r, segs)
        else:
            m = M.cube(size)
        if center:
            m = m.translate(tuple(-s / 2 for s in size))
        return m
    if kind == "cylinder":
        h = float(n["h"])
        r1 = _radius(n) if ("d" in n or "r" in n) else _radius(n, "d1", "r1")
        r2 = _radius(n, "d2", "r2") if ("d2" in n or "r2" in n) else r1
        return M.cylinder(h, r1, r2, segs, center)
    if kind == "cone":
        h = float(n["h"])
        r1 = _radius(n) if ("d" in n or "r" in n) else _radius(n, "d1", "r1")
        r2 = _radius(n, "d2", "r2") if ("d2" in n or "r2" in n) else 0.0
        return M.cylinder(h, r1, r2, segs, center)
    if kind == "sphere":
        return M.sphere(_radius(n), segs)
    if kind == "tube":
        h = float(n["h"])
        ro = _radius(n)
        ri = _radius(n, "inner_d", "inner_r")
        if ri >= ro:
            raise ValueError("tube: inner_d måste vara mindre än d")
        m = M.cylinder(h, ro, ro, segs) - M.cylinder(h + 2, ri, ri, segs).translate((0, 0, -1))
        return m.translate((0, 0, -h / 2)) if center else m
    if kind == "torus":
        R = _radius(n, "d", "r")           # huvudradie (till rörets mitt)
        rt = _radius(n, "tube_d", "tube_r")
        return CS.circle(rt, max(16, segs // 2)).translate((R, 0)).revolve(segs)
    if kind == "wedge":
        x, y, z = _vec3(n["size"], "size")
        return CS([[(0, 0), (y, 0), (0, z)]]).extrude(x).rotate((90, 0, 90))
    if kind == "extrude":
        cs = _profile(n)
        return cs.extrude(float(n["h"]), int(n.get("divisions", 0)), float(n.get("twist", 0)),
                          tuple(n.get("scale_top", (1.0, 1.0))))
    if kind == "revolve":
        return _profile(n).revolve(segs, float(n.get("degrees", 360)))
    raise ValueError(f"okänd form '{kind}'. Tillgängliga: box, cylinder, cone, sphere, tube, torus, "
                     "wedge, extrude, revolve")


def _transform(m: M, n: dict) -> M:
    # ordning: scale -> rotate -> mirror -> translate (som man brukar tänka)
    if "scale" in n:
        m = m.scale(_vec3(n["scale"], "scale"))
    if "rotate" in n:
        m = m.rotate(_vec3(n["rotate"], "rotate"))
    if "mirror" in n:
        m = m.mirror(_vec3(n["mirror"], "mirror"))
    if "translate" in n:
        m = m.translate(_vec3(n["translate"], "translate"))
    return m


def _array(m: M, n: dict) -> M:
    """Upprepning: "array": {"count": [nx, ny, nz], "step": [dx, dy, dz]} eller
    "polar": {"count": n, "degrees": 360}."""
    if "array" in n:
        a = n["array"]
        cnt = a.get("count", [1, 1, 1])
        cnt = [int(c) for c in (cnt if isinstance(cnt, list) else [cnt, 1, 1])]
        step = _vec3(a.get("step", [0, 0, 0]), "array.step")
        if cnt[0] * cnt[1] * cnt[2] > 2000:
            raise ValueError("array: för många kopior (max 2000)")
        m = M.batch_boolean([m.translate((i * step[0], j * step[1], k * step[2]))
                             for i in range(cnt[0]) for j in range(cnt[1]) for k in range(cnt[2])],
                            mf.OpType.Add)
    if "polar" in n:
        p = n["polar"]
        c = int(p["count"])
        if c > 720:
            raise ValueError("polar: för många kopior (max 720)")
        deg = float(p.get("degrees", 360))
        stepd = deg / c if abs(deg - 360) < 1e-9 else (deg / (c - 1) if c > 1 else 0)
        m = M.batch_boolean([m.rotate((0, 0, i * stepd)) for i in range(c)], mf.OpType.Add)
    return m


def build_node(node, env: dict, depth: int = 0) -> M:
    if depth > 64:
        raise ValueError("för djupt träd")
    if not isinstance(node, dict):
        raise ValueError(f"nod måste vara ett objekt, fick {type(node).__name__}")
    if node.get("hidden"):
        return M()
    n = resolve(node, env)
    if "shape" in n:
        m = _shape(n)
    elif "op" in n:
        op = n["op"]
        kids = [build_node(c, env, depth + 1) for c in node.get("children", [])]
        if not kids:
            raise ValueError(f"op '{op}' kräver 'children'")
        if op in ("union", "group"):
            m = M.batch_boolean(kids, mf.OpType.Add)
        elif op == "difference":
            m = kids[0] - M.batch_boolean(kids[1:], mf.OpType.Add) if len(kids) > 1 else kids[0]
        elif op == "intersection":
            m = M.batch_boolean(kids, mf.OpType.Intersect)
        elif op == "hull":
            m = M.batch_hull(kids)
        elif op == "minkowski":
            m = kids[0]
            for k in kids[1:]:
                m = m.minkowski_sum(k)
        else:
            raise ValueError(f"okänd op '{op}' (union, difference, intersection, hull, minkowski)")
    else:
        raise ValueError("nod måste ha 'shape' eller 'op'")
    m = _array(m, n)
    return _transform(m, n)


def to_trimesh(m: M) -> trimesh.Trimesh:
    mesh = m.to_mesh()
    verts = np.asarray(mesh.vert_properties)[:, :3]
    faces = np.asarray(mesh.tri_verts)
    return trimesh.Trimesh(vertices=verts, faces=faces, process=False)


def build(spec: dict) -> dict[str, trimesh.Trimesh]:
    """spec: {"vars": {...}, "root": node} eller {"vars": {...}, "parts": {"namn": node, ...}}.
    Returnerar {delnamn: trimesh}."""
    if not isinstance(spec, dict):
        raise ValueError("spec måste vara ett objekt")
    env = resolve_vars(spec.get("vars", {}))
    if "parts" in spec:
        parts = spec["parts"]
        if not isinstance(parts, dict) or not parts:
            raise ValueError("'parts' måste vara {namn: nod}")
    elif "root" in spec:
        parts = {"model": spec["root"]}
    else:
        raise ValueError("spec kräver 'root' (en nod) eller 'parts' ({namn: nod})")
    out = {}
    for name, node in parts.items():
        m = build_node(node, env)
        if m.is_empty():
            raise ValueError(f"del '{name}' blev tom – kontrollera mått och booleska operationer")
        out[name] = to_trimesh(m)
    return out


SPEC_HELP = """\
CSG-spec (mm, z uppåt, byggplatta z=0):
{"vars": {"w": 40}, "root": <nod>}            – en del
{"vars": {...}, "parts": {"botten": <nod>, "lock": <nod>}}   – flera delar (en STL per del)

Former ("shape"):
  box      size:[x,y,z]  radius?  round?:"all"|"vertical"  center?
  cylinder h, d|r  (d2|r2 för avsmalnande)  center?
  cone     h, d|r  (d2|r2, default 0)
  sphere   d|r
  tube     h, d, inner_d
  torus    d (huvuddiameter), tube_d
  wedge    size:[x,y,z]  (rät triangel-prisma)
  extrude  h, profile, twist?, scale_top?:[sx,sy]
  revolve  profile (i xz-planet: x=radie), degrees?
  profile: {"shape":"polygon","points":[[x,y],...],"holes":[[...]]} | {"shape":"circle","d":..}
           | {"shape":"rect","size":[x,y],"radius"?, "center"?}   (+ "offset"?)
Operationer ("op") med "children":[...]: union, difference (första minus resten), intersection,
  hull, minkowski
Alla noder: translate:[x,y,z], rotate:[rx,ry,rz] (grader), scale, mirror:[nx,ny,nz],
  array:{count:[nx,ny,nz], step:[dx,dy,dz]}, polar:{count, degrees?}, segments, hidden:true,
  name:"valfri etikett"
Värden kan vara uttryck: "=w/2+wall", "=max(a,b)", "=sqrt(x)", "=cos(30)" (grader).
"""
