"""Measurements of a mesh (mm): overall size, and the round holes and bosses (pins, posts,
standoffs) it has, with their diameters, depths and positions.

A round feature is found where it meets a flat face: the outline of each flat region (a facet)
is split into closed loops, and a loop whose points lie on a circle is a circle. The faces on the
other side of the loop say what it is: when they face the circle's axis the surface around it is
hollow (a hole), when they face away it is a solid cylinder (a boss). Circles of the same kind and
size on one axis are the two ends of one feature, which gives its depth and whether a hole goes
through. Holes that end on curved surfaces are not found.
"""
from collections import defaultdict

import numpy as np
import trimesh

MAX_FACES = 400_000      # larger meshes (reliefs, scans) only get their size
MIN_SIDES = 6            # a loop needs this many points to count as round (6: hex nut traps)
ROUND_SIDES = 12         # fewer points than this: reported with "sides" (a polygon, e.g. a hex)
CIRCLE_TOL = 0.02        # largest distance of a loop point from the circle, relative to the radius


def _r(v, n=2):
    if v is None:
        return None
    if isinstance(v, (list, tuple, np.ndarray)):
        return [_r(x, n) for x in v]
    return round(float(v), n) + 0.0  # + 0.0: no "-0.0"


def _loops(edges: np.ndarray) -> list[np.ndarray]:
    """The closed loops in a set of edges: [vertex indices in order]. Edges of vertices that are
    not on exactly one simple loop (two loops touching in a point) are left out."""
    nbr = defaultdict(list)
    for a, b in edges:
        nbr[a].append(b)
        nbr[b].append(a)
    seen, out = set(), []
    for start in nbr:
        if start in seen or len(nbr[start]) != 2:
            continue
        loop, prev, cur, ok = [start], None, start, True
        seen.add(start)
        while True:
            nxt = [v for v in nbr[cur] if v != prev]
            if len(nbr[cur]) != 2 or not nxt:
                ok = False
                break
            prev, cur = cur, nxt[0]
            if cur == start:
                break
            if cur in seen:
                ok = False
                break
            seen.add(cur)
            loop.append(cur)
        if ok and len(loop) >= 3:
            out.append(np.array(loop))
    return out


def _circle(pts: np.ndarray, normal: np.ndarray):
    """(center, radius) when the points (in a plane with this normal) lie on a circle, else None."""
    if len(pts) < MIN_SIDES:
        return None
    u = np.cross(normal, [1.0, 0, 0] if abs(normal[0]) < 0.9 else [0, 1.0, 0])
    u /= np.linalg.norm(u)
    v = np.cross(normal, u)
    o = pts.mean(axis=0)
    x, y = (pts - o) @ u, (pts - o) @ v
    # algebraic circle fit: x² + y² = 2ax + 2by + c
    A = np.column_stack([2 * x, 2 * y, np.ones_like(x)])
    (a, b, c), *_ = np.linalg.lstsq(A, x * x + y * y, rcond=None)
    r = float(np.sqrt(max(c + a * a + b * b, 0)))
    if r <= 1e-6:
        return None
    d = np.hypot(x - a, y - b)
    if np.abs(d - r).max() > CIRCLE_TOL * r + 1e-4:
        return None
    ang = np.sort(np.arctan2(y - b, x - a))
    gaps = np.diff(np.concatenate([ang, [ang[0] + 2 * np.pi]]))
    if gaps.max() > np.pi / 2 + 1e-6:      # an arc, not a full circle
        return None
    return o + a * u + b * v, r


def _edge_faces(mesh: trimesh.Trimesh) -> dict:
    out = defaultdict(list)
    for (a, b), f in zip(mesh.edges_sorted, mesh.edges_face):
        out[(int(a), int(b))].append(int(f))
    return out


def circles(mesh: trimesh.Trimesh) -> list[dict]:
    """Every round loop on a flat face: {center, normal (out of the face), radius, kind, sides}."""
    if len(mesh.faces) == 0 or len(mesh.faces) > MAX_FACES:
        return []
    facets, normals = mesh.facets, mesh.facets_normal
    if not len(facets):
        return []
    in_facet = np.full(len(mesh.faces), -1)
    for i, f in enumerate(facets):
        in_facet[f] = i
    edge_faces = _edge_faces(mesh)
    verts = mesh.vertices
    out = []
    for i, boundary in enumerate(mesh.facets_boundary):
        n = normals[i]
        for loop in _loops(boundary):
            fit = _circle(verts[loop], n)
            if fit is None:
                continue
            center, r = fit
            # the faces across the loop: facing the axis -> hollow (hole), away -> solid (boss)
            votes = 0.0
            for a, b in zip(loop, np.roll(loop, -1)):
                for f in edge_faces.get((min(a, b), max(a, b)), ()):
                    if in_facet[f] == i:
                        continue
                    mid = (verts[a] + verts[b]) / 2 - center
                    radial = mid - n * (mid @ n)
                    votes += float(mesh.face_normals[f] @ radial)
            if votes == 0:
                continue
            out.append({"center": center, "normal": n, "radius": r, "sides": len(loop),
                        "kind": "hole" if votes < 0 else "boss"})
    return out


def _same_axis(a: dict, b: dict) -> bool:
    if a["kind"] != b["kind"]:
        return False
    tol = max(0.02, 0.01 * a["radius"])
    if abs(a["radius"] - b["radius"]) > tol:
        return False
    if np.linalg.norm(np.cross(a["normal"], b["normal"])) > 0.01:
        return False
    d = b["center"] - a["center"]
    return np.linalg.norm(d - a["normal"] * (d @ a["normal"])) <= tol


def features(mesh: trimesh.Trimesh) -> list[dict]:
    """Holes and bosses: the circles grouped by axis. Each has its diameter, axis, center (midway
    between its ends), ends, depth (between the outermost ends; None with a single end) and, for
    holes, through (True when it opens on faces that point away from each other)."""
    groups: list[list[dict]] = []
    for c in circles(mesh):
        for g in groups:
            if _same_axis(g[0], c):
                g.append(c)
                break
        else:
            groups.append([c])
    out = []
    for g in groups:
        axis = g[0]["normal"]
        # a canonical direction: the largest component positive (z up for vertical holes)
        axis = axis if axis[np.argmax(np.abs(axis))] > 0 else -axis
        g.sort(key=lambda c: c["center"] @ axis)
        lo, hi = g[0], g[-1]
        depth = float((hi["center"] - lo["center"]) @ axis) if len(g) > 1 else None
        if depth is not None and depth < 1e-3:   # two loops in one plane, e.g. both sides of a sheet
            depth = None
        f = {"kind": lo["kind"], "diameter": _r(2 * np.mean([c["radius"] for c in g]), 3),
             "axis": _r(axis, 3), "center": _r((lo["center"] + hi["center"]) / 2),
             "ends": [_r(c["center"]) for c in g], "depth": _r(depth)}
        if lo["kind"] == "hole":
            f["through"] = bool(depth is not None and lo["normal"] @ axis < 0 < hi["normal"] @ axis)
        sides = min(c["sides"] for c in g)
        if sides < ROUND_SIDES:
            f["sides"] = sides
            if sides % 2 == 0:  # flat to flat, e.g. the wrench size of a hex nut trap
                f["across_flats"] = _r(f["diameter"] * np.cos(np.pi / sides), 3)
        out.append(f)
    out.sort(key=lambda f: (f["kind"], f["diameter"], f["center"]))
    return out


def summary(feats: list[dict]) -> list[dict]:
    """Features of the same kind, size, depth and through-ness counted together, e.g. 4× Ø3.4
    through. pattern_mm is the size of the rectangle their centers span (for axis-aligned ones),
    e.g. the spacing of four mounting holes."""
    groups: dict[tuple, list[dict]] = {}
    for f in feats:
        key = (f["kind"], f["diameter"], f["depth"], f.get("through"), tuple(f["axis"]), f.get("sides"))
        groups.setdefault(key, []).append(f)
    out = []
    for (kind, dia, depth, through, axis, sides), fs in groups.items():
        s = {"kind": kind, "count": len(fs), "diameter": dia, "depth": depth, "axis": list(axis)}
        if through is not None:
            s["through"] = through
        if sides:
            s["sides"] = sides
        if len(fs) > 1 and max(abs(a) for a in axis) > 0.999:
            ax = int(np.argmax(np.abs(axis)))
            c = np.array([f["center"] for f in fs])
            span = c.max(axis=0) - c.min(axis=0)
            s["pattern_mm"] = {"xyz"[i]: _r(span[i]) for i in range(3) if i != ax}
        out.append(s)
    return out


def describe(f: dict) -> str:
    """'Ø3.4 hole, through' / 'Ø6 boss, 8 deep' ..."""
    d = f"{f['diameter']:g}"
    shape = f"{f['sides']}-sided {f['kind']} Ø{d}" if f.get("sides") else f"Ø{d} {f['kind']}"
    if f.get("across_flats"):
        shape += f" ({f['across_flats']:g} across flats)"
    if f.get("through"):
        return f"{shape}, through ({f['depth']:g} long)"
    if f.get("depth") is not None:
        return f"{shape}, {f['depth']:g} {'deep' if f['kind'] == 'hole' else 'high'}"
    return shape


def measure(mesh: trimesh.Trimesh) -> dict:
    """Size, bounds, volume and round features of one part."""
    b = mesh.bounds if len(mesh.faces) else np.zeros((2, 3))
    out = {"extents_mm": _r(b[1] - b[0]), "bounds_mm": _r(b),
           "volume_cm3": _r(abs(mesh.volume) / 1000 if mesh.is_volume else 0.0),
           "area_cm2": _r(mesh.area / 100), "triangles": len(mesh.faces)}
    if len(mesh.faces) > MAX_FACES:
        out["note"] = f"more than {MAX_FACES} triangles: holes and bosses are not measured"
        out["features"], out["summary"] = [], []
        return out
    feats = features(mesh)
    out["features"], out["summary"] = feats, summary(feats)
    return out
