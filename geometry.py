"""Geometri-hjälpare (mm). Alla former byggs med trimesh + manifold3d."""
import io
import zipfile

import numpy as np
import trimesh
from trimesh.creation import box as _box, cylinder as _cyl, icosphere, cone as _cone


def blk(sx, sy, sz, x0, y0, z0):
    b = _box(extents=[sx, sy, sz])
    b.apply_translation([x0 + sx / 2, y0 + sy / 2, z0 + sz / 2])
    return b


def cyl(r, h, x, y, z0, sections=48):
    c = _cyl(radius=r, height=h, sections=sections)
    c.apply_translation([x, y, z0 + h / 2])
    return c


def union(ms):
    return trimesh.boolean.union(ms, engine="manifold")


def diff(a, bs):
    if not bs:
        return a
    return trimesh.boolean.difference([a] + list(bs), engine="manifold")


def primitive(shape: str, **p) -> trimesh.Trimesh:
    shape = shape.lower()
    if shape == "box":
        return _box(extents=[p["x"], p["y"], p["z"]])
    if shape == "cylinder":
        return _cyl(radius=p["diameter"] / 2, height=p["height"], sections=64)
    if shape == "sphere":
        return icosphere(subdivisions=4, radius=p["diameter"] / 2)
    if shape == "cone":
        return _cone(radius=p["diameter"] / 2, height=p["height"], sections=64)
    if shape == "tube":
        outer = _cyl(radius=p["diameter"] / 2, height=p["height"], sections=64)
        inner = _cyl(radius=p["inner_diameter"] / 2, height=p["height"] + 1, sections=64)
        return diff(outer, [inner])
    raise ValueError(f"okänd form: {shape} (box, cylinder, sphere, cone, tube)")


WALLS = ("left", "right", "front", "back")


def enclosure(inner_x, inner_y, inner_z, wall=2.4, floor=2.4, lid_t=2.4,
              screws=True, post_d=7.0, pilot_d=2.6, lid_hole_d=3.4, clearance=0.3,
              standoffs=None, cutouts=None, lid_holes=None, lid_slots=None):
    """Returnerar (botten, lock).

    Koordinater: x längs, y på tvären, z uppåt. Invändigt origo = (wall, wall, floor).
    standoffs : [{x, y, hole_d, height=4, outer_d=5}]  (x,y = hålcentrum, inifrån invändigt hörn)
    cutouts   : [{wall: left|right|front|back, pos, width, height, z=0}]
                pos = mitten längs väggen, mätt från väggens vänstra (sett utifrån) ände; z = underkant ovanför golv
    lid_holes : [{x, y, d}]   (invändiga koordinater)
    lid_slots : [{x, y, w, h}] (rektanglar, x,y = nedre vänstra hörn)
    """
    standoffs = standoffs or []
    cutouts = cutouts or []
    lid_holes = lid_holes or []
    lid_slots = lid_slots or []

    ox, oy = inner_x + 2 * wall, inner_y + 2 * wall
    shell = blk(ox, oy, floor + inner_z, 0, 0, 0)
    cavity = blk(inner_x, inner_y, inner_z + 1, wall, wall, floor)
    body = diff(shell, [cavity])

    posts = []
    if screws:
        pr = post_d / 2
        posts = [(wall + pr, wall + pr), (wall + inner_x - pr, wall + pr),
                 (wall + pr, wall + inner_y - pr), (wall + inner_x - pr, wall + inner_y - pr)]
        body = union([body] + [cyl(pr, inner_z, px, py, floor) for px, py in posts])
        body = diff(body, [cyl(pilot_d / 2, inner_z - 2, px, py, floor + 2) for px, py in posts])

    for s in standoffs:
        h = s.get("height", 4.0)
        od = s.get("outer_d", 5.0)
        x, y = wall + s["x"], wall + s["y"]
        body = union([body, cyl(od / 2, h, x, y, floor)])
        body = diff(body, [cyl(s["hole_d"] / 2, h + 0.5, x, y, floor + 0.8)])

    for c in cutouts:
        w_, pos, cw, ch, z = c["wall"], c["pos"], c["width"], c["height"], c.get("z", 0.0)
        z0 = floor + z
        t = wall + 2
        if w_ == "left":
            b = blk(t, cw, ch, -1, wall + pos - cw / 2, z0)
        elif w_ == "right":
            b = blk(t, cw, ch, ox - wall - 1, wall + pos - cw / 2, z0)
        elif w_ == "front":
            b = blk(cw, t, ch, wall + pos - cw / 2, -1, z0)
        elif w_ == "back":
            b = blk(cw, t, ch, wall + pos - cw / 2, oy - wall - 1, z0)
        else:
            raise ValueError(f"wall måste vara en av {WALLS}")
        body = diff(body, [b])

    lid = blk(ox, oy, lid_t, 0, 0, 0)
    lip_t, lip_h = 1.6, 2.4
    lip_outer = blk(inner_x - 2 * clearance, inner_y - 2 * clearance, lip_h,
                    wall + clearance, wall + clearance, lid_t)
    lip_inner = blk(inner_x - 2 * clearance - 2 * lip_t, inner_y - 2 * clearance - 2 * lip_t,
                    lip_h + 1, wall + clearance + lip_t, wall + clearance + lip_t, lid_t - 0.5)
    lid = union([lid, diff(lip_outer, [lip_inner])])
    cuts = []
    for px, py in posts:
        cuts += [cyl(lid_hole_d / 2, lid_t + lip_h + 1, px, py, -0.5),
                 cyl(3.2, 1.6, px, py, lid_t - 1.4)]
    for h in lid_holes:
        cuts.append(cyl(h["d"] / 2, lid_t + lip_h + 1, wall + h["x"], wall + h["y"], -0.5))
    for s in lid_slots:
        cuts.append(blk(s["w"], s["h"], lid_t + 1, wall + s["x"], wall + s["y"], -0.5))
    lid = diff(lid, cuts)
    return body, lid


# ---------------------------------------------------------------- import / ändring / export

IMPORT_FORMATS = ("stl", "obj", "glb", "ply", "off")


def load_mesh(data: bytes, fmt: str | None = None) -> trimesh.Trimesh:
    """Läser en mesh ur bytes. Zip (t.ex. Tinkercads OBJ-export) packas upp automatiskt.
    Scener slås ihop till en mesh; färger/texturer slängs."""
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.rsplit(".", 1)[-1].lower() in IMPORT_FORMATS]
            if not names:
                raise ValueError("zip-filen innehåller ingen stl/obj/glb/ply/off")
            fmt = names[0].rsplit(".", 1)[-1].lower()
            data = z.read(names[0])
    fmt = (fmt or "stl").lower().lstrip(".")
    if fmt not in IMPORT_FORMATS:
        raise ValueError(f"format {fmt} stöds inte ({', '.join(IMPORT_FORMATS)})")
    loaded = trimesh.load(io.BytesIO(data), file_type=fmt)
    if isinstance(loaded, trimesh.Scene):
        meshes = []
        for node in loaded.graph.nodes_geometry:
            transform, gname = loaded.graph[node]
            geo = loaded.geometry[gname]
            if isinstance(geo, trimesh.Trimesh):
                m = trimesh.Trimesh(geo.vertices, geo.faces)
                m.apply_transform(transform)
                meshes.append(m)
        loaded = trimesh.util.concatenate(meshes) if meshes else None
    if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
        raise ValueError("filen innehåller ingen mesh")
    return trimesh.Trimesh(loaded.vertices, loaded.faces)


def repair(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    mesh = mesh.copy()
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.update_faces(mesh.unique_faces())
    mesh.remove_unreferenced_vertices()
    trimesh.repair.fill_holes(mesh)
    trimesh.repair.fix_normals(mesh)
    return mesh


def shape_at(spec: dict) -> trimesh.Trimesh:
    """{shape, x,y,z | diameter, inner_diameter, height, at=[x,y,z], rotate=[rx,ry,rz]}.
    at = formens mittpunkt, cylindrar/koner står längs z innan rotation."""
    m = primitive(spec["shape"], x=spec.get("x", 0), y=spec.get("y", 0), z=spec.get("z", 0),
                  diameter=spec.get("diameter", 0), inner_diameter=spec.get("inner_diameter", 0),
                  height=spec.get("height", 0))
    m.apply_translation(-m.bounds.mean(axis=0))
    if spec.get("rotate"):
        m.apply_transform(_rotation(spec["rotate"]))
    m.apply_translation(spec.get("at", [0, 0, 0]))
    return m


def _rotation(deg) -> np.ndarray:
    rx, ry, rz = np.radians(deg)
    return trimesh.transformations.euler_matrix(rx, ry, rz, "sxyz")


def modify(mesh: trimesh.Trimesh, scale=None, rotate=None, mirror=None, place_on_bed=True,
           center_xy=False, add=None, subtract=None, do_repair=False) -> trimesh.Trimesh:
    """Ordning: reparation, skala, rotera (kring boxens mitt), spegla, placering, add, subtract."""
    mesh = repair(mesh) if do_repair else mesh.copy()
    if scale is not None:
        s = np.broadcast_to(np.asarray(scale, dtype=float), 3)
        if np.any(s <= 0):
            raise ValueError("scale måste vara > 0")
        mesh.apply_transform(np.diag([*s, 1.0]))
    if rotate:
        c = mesh.bounds.mean(axis=0)
        mesh.apply_translation(-c)
        mesh.apply_transform(_rotation(rotate))
        mesh.apply_translation(c)
    for axis in (mirror or ""):
        if axis not in "xyz":
            raise ValueError("mirror får bara innehålla x, y, z")
        c = mesh.bounds.mean(axis=0)
        normal = np.eye(3)["xyz".index(axis)]
        mesh.apply_transform(trimesh.transformations.reflection_matrix(c, normal))
    if center_xy:
        c = mesh.bounds.mean(axis=0)
        mesh.apply_translation([-c[0], -c[1], 0])
    if place_on_bed:
        mesh.apply_translation([0, 0, -mesh.bounds[0][2]])
    if add or subtract:
        if not mesh.is_volume:
            raise ValueError("modellen är inte vattentät; prova repair=true innan add/subtract")
        if add:
            mesh = union([mesh] + [shape_at(s) for s in add])
        if subtract:
            mesh = diff(mesh, [shape_at(s) for s in subtract])
    return mesh


def stl_size(mesh: trimesh.Trimesh) -> int:
    return 84 + 50 * len(mesh.faces)


def simplify_to(mesh: trimesh.Trimesh, max_bytes: int) -> trimesh.Trimesh:
    """Minskar antalet trianglar så att binär STL ryms inom max_bytes."""
    faces = int((max_bytes * 0.98 - 84) // 50)
    return mesh.simplify_quadric_decimation(face_count=faces)


COLORS = {
    "red": "#e8312f", "orange": "#ff8a00", "yellow": "#fcd116", "green": "#22a347",
    "blue": "#1d70b7", "purple": "#7d3c98", "pink": "#f49ac2", "white": "#f2f2f2",
    "gray": "#8c8c8c", "grey": "#8c8c8c", "black": "#222222", "brown": "#8b5a2b",
}
PALETTE = ["red", "blue", "yellow", "green", "orange", "purple", "gray", "pink"]


def _rgb(color: str) -> tuple[float, float, float]:
    h = COLORS.get(color.lower(), color).lstrip("#")
    if not (len(h) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in h)):
        raise ValueError(f"okänd färg: {color} (namn som red/blue eller #rrggbb)")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def to_obj(parts: list[tuple[str, trimesh.Trimesh, str]], mtl_name: str) -> tuple[str, str]:
    """parts: [(namn, mesh, färg)] -> (obj-text, mtl-text). Ett objekt + material per del."""
    obj = [f"mtllib {mtl_name}"]
    mtl = []
    offset = 1
    for i, (pname, mesh, color) in enumerate(parts):
        r, g_, b = _rgb(color or PALETTE[i % len(PALETTE)])
        mat = f"m{i}_{pname}"
        mtl += [f"newmtl {mat}", f"Kd {r:.4f} {g_:.4f} {b:.4f}", "Ka 0 0 0", "d 1", "illum 1", ""]
        obj += [f"o {pname}", f"usemtl {mat}"]
        obj += [f"v {x:.5g} {y:.5g} {z:.5g}" for x, y, z in mesh.vertices]
        obj += [f"f {a + offset} {b_ + offset} {c + offset}" for a, b_, c in mesh.faces]
        offset += len(mesh.vertices)
    return "\n".join(obj) + "\n", "\n".join(mtl)
