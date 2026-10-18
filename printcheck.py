"""Print checks (warnings only, never blocking) and the admin-editable settings that drive them.

Every built part is checked for things that make a print fail even though the geometry is valid:
not watertight / inverted, shells floating in mid air, walls thinner than the nozzle can print,
overhangs that need support, and a size that does not fit the printer bed.

The checks are approximate and fast (surface sampling, no ray tracing). Thresholds live in
SCHEMA (defaults) and can be overridden at runtime from the admin UI; overrides are persisted in
<STL_DIR>/settings.json and only differences from the defaults are stored.
"""
from __future__ import annotations

import json
import math
import threading
from pathlib import Path

import numpy as np
import trimesh
from scipy.spatial import cKDTree

# key -> definition. "group" and "label" are only used by the admin UI.
SCHEMA: dict[str, dict] = {
    "enabled": {
        "type": "bool", "default": True, "group": "General", "label": "Run print checks",
        "help": "Master switch. When off, no warnings are produced."},
    "check_watertight": {
        "type": "bool", "default": True, "group": "Mesh integrity", "label": "Check watertightness",
        "help": "Warn when the mesh has holes, open edges or inverted normals (cracks in the slicer)."},
    "check_shells": {
        "type": "bool", "default": True, "group": "Mesh integrity", "label": "Check floating shells",
        "help": "Warn when a piece of a part floats in the air. Pieces on the bed (the floor) or resting on a grounded piece are fine."},
    "check_thin_walls": {
        "type": "bool", "default": True, "group": "Thin walls", "label": "Check thin walls",
        "help": "Warn about wall sections thinner than the minimum wall thickness."},
    "min_wall_mm": {
        "type": "number", "default": 1.2, "min": 0.1, "max": 20, "unit": "mm",
        "group": "Thin walls", "label": "Minimum wall thickness",
        "help": "Roughly 2-3 nozzle widths (0.4 mm nozzle: 1.2 mm)."},
    "thin_wall_min_area_mm2": {
        "type": "number", "default": 2.0, "min": 0, "max": 10000, "unit": "mm²",
        "group": "Thin walls", "label": "Ignore thin areas smaller than",
        "help": "Sharp edges and tips are always thin; only warn when the thin area is at least this big."},
    "check_overhangs": {
        "type": "bool", "default": True, "group": "Overhangs", "label": "Check overhangs",
        "help": "Warn when a large share of the surface needs support material."},
    "max_overhang_angle_deg": {
        "type": "number", "default": 45, "min": 0, "max": 89, "unit": "°",
        "group": "Overhangs", "label": "Maximum overhang angle",
        "help": "Measured from vertical. Downward faces steeper than this count as overhangs."},
    "max_overhang_area_pct": {
        "type": "number", "default": 10, "min": 0, "max": 100, "unit": "%",
        "group": "Overhangs", "label": "Overhang share that triggers a warning",
        "help": "Percent of the total surface area. Faces lying flat on the bed are not counted."},
    "check_build_volume": {
        "type": "bool", "default": True, "group": "Printer bed", "label": "Check build volume",
        "help": "Warn when a part is larger than the printer can print (X and Y may be swapped)."},
    "build_x_mm": {
        "type": "number", "default": 220, "min": 10, "max": 2000, "unit": "mm",
        "group": "Printer bed", "label": "Build volume X"},
    "build_y_mm": {
        "type": "number", "default": 220, "min": 10, "max": 2000, "unit": "mm",
        "group": "Printer bed", "label": "Build volume Y"},
    "build_z_mm": {
        "type": "number", "default": 250, "min": 10, "max": 2000, "unit": "mm",
        "group": "Printer bed", "label": "Build volume Z"},
}

DEFAULTS: dict = {k: v["default"] for k, v in SCHEMA.items()}

MAX_SAMPLES = 60000
MIN_SAMPLES = 2000


# ---------------------------------------------------------------- settings

def validate(values: dict) -> dict:
    """Return a cleaned copy of values (known keys only, correct types, within limits)."""
    if not isinstance(values, dict):
        raise ValueError("settings must be an object")
    out = {}
    for k, v in values.items():
        d = SCHEMA.get(k)
        if d is None:
            raise ValueError(f"unknown setting '{k}'")
        if d["type"] == "bool":
            if not isinstance(v, bool):
                raise ValueError(f"'{k}' must be true or false")
            out[k] = v
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"'{k}' must be a number")
        if not d["min"] <= v <= d["max"]:
            raise ValueError(f"'{k}' must be between {d['min']} and {d['max']}")
        out[k] = float(v) if isinstance(d["default"], float) or v != int(v) else int(v)
    return out


class Settings:
    """Defaults + persisted overrides. Thread-safe; a missing or broken file means defaults."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self.lock = threading.RLock()
        self.overrides: dict = {}
        if self.path and self.path.is_file():
            try:
                self.overrides = validate(json.loads(self.path.read_text()))
            except (ValueError, OSError):
                self.overrides = {}

    def get(self) -> dict:
        with self.lock:
            return {**DEFAULTS, **self.overrides}

    def update(self, values: dict) -> dict:
        clean = validate(values)
        with self.lock:
            merged = {**self.overrides, **clean}
            # a value equal to the default is not an override
            self.overrides = {k: v for k, v in merged.items() if v != DEFAULTS[k]}
            self._save()
            return self.get()

    def reset(self, keys: list[str] | None = None) -> dict:
        with self.lock:
            if keys is None:
                self.overrides = {}
            else:
                for k in keys:
                    if k not in SCHEMA:
                        raise ValueError(f"unknown setting '{k}'")
                    self.overrides.pop(k, None)
            self._save()
            return self.get()

    def _save(self):
        if not self.path:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.overrides, indent=1))
        tmp.replace(self.path)

    def describe(self) -> dict:
        """Everything the admin UI needs: current values, defaults and the schema."""
        return {"values": self.get(), "defaults": dict(DEFAULTS), "schema": SCHEMA}


# ---------------------------------------------------------------- sampling helpers

def _sample_surface(mesh: trimesh.Trimesh, count: int, rng) -> tuple[np.ndarray, np.ndarray]:
    """Area-weighted random surface points and the face normal at each."""
    area = mesh.area_faces
    face = rng.choice(len(area), size=count, p=area / area.sum())
    tri = mesh.triangles[face]
    r1, r2 = rng.random(count), rng.random(count)
    flip = r1 + r2 > 1
    r1, r2 = np.where(flip, 1 - r1, r1), np.where(flip, 1 - r2, r2)
    pts = tri[:, 0] + r1[:, None] * (tri[:, 1] - tri[:, 0]) + r2[:, None] * (tri[:, 2] - tri[:, 0])
    return pts, mesh.face_normals[face]


def _thin_walls(mesh: trimesh.Trimesh, min_wall: float, min_area: float) -> dict | None:
    """Find surface points that have an opposite, inward-facing surface closer than min_wall."""
    total = float(mesh.area)
    spacing = min_wall / 3
    count = int(np.clip(total / spacing ** 2, MIN_SAMPLES, MAX_SAMPLES))
    rng = np.random.default_rng(0)
    pts, nrm = _sample_surface(mesh, count, rng)
    pairs = cKDTree(pts).query_pairs(min_wall, output_type="ndarray")
    if len(pairs) == 0:
        return None
    pairs = np.concatenate([pairs, pairs[:, ::-1]])          # both directions
    i, j = pairs[:, 0], pairs[:, 1]
    v = pts[j] - pts[i]
    dist = np.linalg.norm(v, axis=1)
    ok = dist > 1e-9
    i, j, v, dist = i[ok], j[ok], v[ok], dist[ok]
    inward = -(v * nrm[i]).sum(axis=1) / dist                # q lies inside the material below p
    opposed = (nrm[i] * nrm[j]).sum(axis=1)                  # the two surfaces face away from each other
    hit = (inward > 0.9) & (opposed < -0.8)
    if not hit.any():
        return None
    flagged = np.unique(i[hit])
    area = len(flagged) / count * total
    if area < min_area:
        return None
    thinnest = float(dist[hit].min())
    center = pts[flagged].mean(axis=0)
    return {"area_mm2": round(area, 1), "thinnest_mm": round(thinnest, 2),
            "near_mm": [round(float(c), 1) for c in center]}


GROUND_TOL_MM = 0.05


def _floating_shells(shells: list[trimesh.Trimesh], floor: float) -> list[list[float]]:
    """Centers of the shells that float. The floor (the part's lowest point = the bed) is ground;
    a shell is grounded when it touches the floor, or rests with its underside on a grounded shell."""
    grounded = [sh.bounds[0][2] <= floor + GROUND_TOL_MM for sh in shells]
    changed = True
    while changed and not all(grounded):
        changed = False
        for i, sh in enumerate(shells):
            if grounded[i]:
                continue
            z = sh.bounds[0][2]
            under = _flat_faces(sh, z, up=False)
            for j, base in enumerate(shells):
                if grounded[j] and _overlap_xy(under, _flat_faces(base, z, up=True)):
                    grounded[i] = changed = True
                    break
    return [[round(float(c), 1) for c in sh.bounds.mean(axis=0)]
            for sh, g in zip(shells, grounded) if not g]


def _flat_faces(mesh: trimesh.Trimesh, z: float, up: bool) -> np.ndarray:
    """XY triangles (n, 3, 2) of the faces lying flat at height z, facing up or down."""
    tri = mesh.triangles
    facing = mesh.face_normals[:, 2] > 0.99 if up else mesh.face_normals[:, 2] < -0.99
    at_z = (np.abs(tri[:, :, 2] - z) <= GROUND_TOL_MM).all(axis=1)
    return tri[facing & at_z][:, :, :2]


def _overlap_xy(a: np.ndarray, b: np.ndarray) -> bool:
    """Whether two sets of XY triangles overlap: a corner or center of one lies inside the other.
    Checked both ways, so a wide lid on a thin post and a small cube on a big block both count."""
    if not len(a) or not len(b):
        return False
    return _points_in(_probe_points(a), b) or _points_in(_probe_points(b), a)


def _probe_points(tri: np.ndarray) -> np.ndarray:
    return np.vstack([tri.reshape(-1, 2), tri.mean(axis=1)])


def _points_in(pts: np.ndarray, tri: np.ndarray) -> bool:
    """Whether any point lies inside (or on the edge of) any triangle, vectorised in chunks."""
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    v0, v1 = b - a, c - a
    den = v0[:, 0] * v1[:, 1] - v1[:, 0] * v0[:, 1]
    keep = np.abs(den) > 1e-12                                 # skip degenerate slivers
    a, v0, v1, den = a[keep], v0[keep], v1[keep], den[keep]
    eps = 1e-6
    for chunk in np.array_split(pts, max(1, len(pts) * len(a) // 2_000_000 + 1)):
        d = chunk[:, None, :] - a[None]
        u = (d[..., 0] * v1[:, 1] - v1[:, 0] * d[..., 1]) / den
        v = (v0[:, 0] * d[..., 1] - d[..., 0] * v0[:, 1]) / den
        if ((u >= -eps) & (v >= -eps) & (u + v <= 1 + eps)).any():
            return True
    return False


# ---------------------------------------------------------------- the check

def _warn(code: str, severity: str, message: str, **detail) -> dict:
    w = {"code": code, "severity": severity, "message": message}
    if detail:
        w["detail"] = detail
    return w


def check(mesh: trimesh.Trimesh, settings: dict | None = None) -> list[dict]:
    """Return a list of warnings ({code, severity, message, detail?}); empty when all is well.
    severity: "critical" = the print will most likely fail, "warning" = worth a look."""
    s = {**DEFAULTS, **(settings or {})}
    if not s["enabled"]:
        return []
    out: list[dict] = []
    try:
        watertight = bool(mesh.is_watertight)
        if s["check_watertight"]:
            if not watertight:
                out.append(_warn("not_watertight", "critical",
                                 "not watertight (open edges or holes) - the slicer may produce gaps"))
            elif mesh.volume < 0:
                out.append(_warn("inverted_normals", "critical",
                                 "faces point inward (negative volume) - the slicer may treat it as a hole"))
        if s["check_shells"]:
            shells = mesh.split(only_watertight=False)
            floating = _floating_shells(shells, mesh.bounds[0][2]) if len(shells) > 1 else []
            if floating:
                out.append(_warn("disconnected_shells", "warning",
                                 f"{len(floating)} of {len(shells)} shells float in the air "
                                 f"(not resting on the bed or on another part), around {floating[0]}",
                                 shells=len(shells), floating=len(floating), near_mm=floating))
        if s["check_thin_walls"] and len(mesh.faces) and mesh.area > 0:
            tw = _thin_walls(mesh, float(s["min_wall_mm"]), float(s["thin_wall_min_area_mm2"]))
            if tw:
                out.append(_warn("thin_walls", "warning",
                                 f"walls as thin as {tw['thinnest_mm']} mm (minimum {s['min_wall_mm']} mm) "
                                 f"over about {tw['area_mm2']} mm², around {tw['near_mm']}", **tw))
        if s["check_overhangs"] and len(mesh.faces):
            z0 = mesh.bounds[0][2]
            down = mesh.face_normals[:, 2] < -math.sin(math.radians(s["max_overhang_angle_deg"]))
            on_bed = mesh.triangles[:, :, 2].max(axis=1) < z0 + 0.05
            area = float(mesh.area_faces[down & ~on_bed].sum())
            pct = 100 * area / float(mesh.area) if mesh.area > 0 else 0
            if pct > s["max_overhang_area_pct"]:
                out.append(_warn("overhangs", "warning",
                                 f"{pct:.0f}% of the surface overhangs more than "
                                 f"{s['max_overhang_angle_deg']}° - supports or a different orientation needed",
                                 area_mm2=round(area, 1), percent=round(pct, 1)))
        if s["check_build_volume"] and len(mesh.faces):
            ex, ey, ez = (float(v) for v in mesh.extents)
            bx, by, bz = s["build_x_mm"], s["build_y_mm"], s["build_z_mm"]
            fits_xy = (ex <= bx and ey <= by) or (ex <= by and ey <= bx)
            if not (fits_xy and ez <= bz):
                out.append(_warn("exceeds_build_volume", "warning",
                                 f"{ex:.0f} x {ey:.0f} x {ez:.0f} mm does not fit the build volume "
                                 f"{bx:g} x {by:g} x {bz:g} mm",
                                 extents_mm=[round(ex, 1), round(ey, 1), round(ez, 1)]))
    except Exception as e:  # noqa: BLE001 - a failing check must never block a build
        out.append(_warn("check_failed", "warning", f"the print check itself failed: {e}"))
    return out
