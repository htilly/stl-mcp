"""Geometri-hjälpare (mm). Alla former byggs med trimesh + manifold3d."""
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
