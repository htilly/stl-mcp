"""Simple software rendering (PIL) so Claude can *see* the model without a GPU.

Orthographic views, flat shading, painter's algorithm (triangle sorting by depth).
"""
from __future__ import annotations

import io
import math
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image, ImageDraw, ImageFont

PART_COLORS = [(79, 140, 255), (255, 154, 60), (90, 200, 120), (230, 90, 120), (170, 120, 255),
               (60, 200, 210)]
BG = (246, 247, 249)

# (name, azimuth, elevation) – azimuth 0 = looking along +y (from the front), z up
VIEWS = {"iso": ("iso", -45, 30), "front": ("front", 0, 0), "top": ("top", 0, 90),
         "right": ("right", -90, 0), "back_iso": ("iso back", 135, 30)}


def layout(parts: dict[str, trimesh.Trimesh], spread: bool = True, gap: float = 10.0):
    """Lay the parts out side by side along x (as in the web view). Returns [(name, mesh)]."""
    out = []
    x = None
    for name, m in parts.items():
        m = m.copy()
        if spread and len(parts) > 1:
            if x is None:
                x = m.bounds[0][0]
            m.apply_translation([x - m.bounds[0][0], 0, 0])
            x = m.bounds[1][0] + gap
        out.append((name, m))
    return out


def _subdivided(m: trimesh.Trimesh, max_edge: float) -> trimesh.Trimesh:
    """Subdivide large triangles so the depth sorting (painter's algorithm) is correct."""
    if len(m.faces) > 150_000:
        return m
    try:
        v, f = trimesh.remesh.subdivide_to_size(m.vertices, m.faces, max_edge, max_iter=8)
    except ValueError:
        return m
    return m if len(f) > 250_000 else trimesh.Trimesh(v, f, process=False)


def _sharp_edges(m: trimesh.Trimesh, fwd, deg: float = 25.0):
    """Visible sharp edges and silhouette edges as an (n, 2, 3) array of endpoints."""
    adj, edges = m.face_adjacency, m.face_adjacency_edges
    facing = m.face_normals @ fwd < -1e-6
    f1, f2 = facing[adj[:, 0]], facing[adj[:, 1]]
    keep = (m.face_adjacency_angles > math.radians(deg)) & (f1 | f2)
    return m.vertices[edges[keep]]


def _split_segments(seg, max_len):
    """Split long edge segments so they sort correctly against the subdivided triangles."""
    if len(seg) == 0:
        return seg
    a, b = seg[:, 0], seg[:, 1]
    n = np.maximum(1, np.ceil(np.linalg.norm(b - a, axis=1) / max_len).astype(int))
    rep = np.repeat(np.arange(len(seg)), n)
    k = np.concatenate([np.arange(c) for c in n])
    t0 = (k / n[rep])[:, None]
    t1 = ((k + 1) / n[rep])[:, None]
    d = (b - a)[rep]
    return np.stack([a[rep] + d * t0, a[rep] + d * t1], 1)


def _rot(az, el):
    a, e = math.radians(az), math.radians(el)
    # camera right vector, up vector and view direction (into the screen) in world coordinates
    fwd = np.array([math.sin(a) * math.cos(e), math.cos(a) * math.cos(e), -math.sin(e)])
    right = np.array([math.cos(a), -math.sin(a), 0.0])
    up = np.cross(right, fwd)
    return right, up, fwd


def _font(size):
    bundled = str(Path(__file__).parent / "fonts" / "DejaVuSans.ttf")
    for f in (bundled, "DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            pass
    return ImageFont.load_default()


def render_view(meshes, az, el, size=420, label="", ss=2):
    W = size * ss
    img = Image.new("RGB", (W, W), BG)
    draw = ImageDraw.Draw(img)
    right, up, fwd = _rot(az, el)
    allv = np.vstack([m.vertices for _, m in meshes])
    center = (allv.min(0) + allv.max(0)) / 2
    pts2 = np.stack([(allv - center) @ right, (allv - center) @ up], 1)
    span = max(np.ptp(pts2[:, 0]), np.ptp(pts2[:, 1]), 1e-6)
    scale = W * 0.82 / span
    light = -fwd + up * 0.6 + right * 0.3
    light /= np.linalg.norm(light)

    max_edge = span / 30
    depths, polys, colors = [], [], []
    for i, (_, m) in enumerate(meshes):
        seg = _split_segments(_sharp_edges(m, fwd), max_edge) - center
        sm = _subdivided(m, max_edge)
        tri = sm.vertices[sm.faces] - center           # (n,3,3)
        n = sm.face_normals
        facing = n @ fwd < -1e-6                       # discard back-facing (and edge-on) triangles
        tri, n = tri[facing], n[facing]
        shade = 0.35 + 0.65 * np.clip(n @ light, 0, 1)
        base = np.array(PART_COLORS[i % len(PART_COLORS)])
        cols = (base[None, :] * shade[:, None]).clip(0, 255).astype(int)
        depths += [tri.mean(1) @ fwd, seg.mean(1) @ fwd - max_edge * 0.05]  # edges slightly closer
        polys += [np.stack([tri @ right, -(tri @ up)], -1).reshape(-1, 6),
                  np.stack([seg @ right, -(seg @ up)], -1).reshape(-1, 4)]
        colors += [cols, None]
    if not depths:
        return img
    items = []                                          # (depth, type, index, part)
    for j, d in enumerate(depths):
        items.append(np.stack([d, np.full(len(d), j), np.arange(len(d))], 1))
    items = np.concatenate(items)
    items = items[np.argsort(-items[:, 0], kind="stable")]  # farthest first
    polys_px = [(p * scale + W / 2).tolist() for p in polys]
    cols_l = [c.tolist() if c is not None else None for c in colors]
    poly, line = draw.polygon, draw.line
    ecol = (35, 40, 55)
    for _, j, k in items.tolist():
        j, k = int(j), int(k)
        if j % 2 == 0:
            c = tuple(cols_l[j][k])
            poly(polys_px[j][k], fill=c, outline=c)
        else:
            line(polys_px[j][k], fill=ecol, width=ss)

    # small axis indicator
    o = np.array([34, size - 34]) * ss * 1.0      # inset so arrows pointing left/down stay in frame
    for vec, col, txt in (((1, 0, 0), (220, 60, 60), "x"), ((0, 1, 0), (60, 170, 60), "y"),
                          ((0, 0, 1), (60, 90, 230), "z")):
        d = np.array([np.dot(vec, right), -np.dot(vec, up)])
        if np.linalg.norm(d) < 0.15:
            continue
        e = o + d * 18 * ss
        draw.line([*o, *e], fill=col, width=2 * ss)
        draw.text(tuple(e + d * 4 * ss - np.array([3, 6]) * ss), txt, fill=col, font=_font(11 * ss))
    if label:
        draw.text((8 * ss, 6 * ss), label, fill=(60, 60, 70), font=_font(13 * ss))
    return img.resize((size, size), Image.LANCZOS)


def render_png(parts: dict[str, trimesh.Trimesh], views=("iso", "front", "top", "right"),
               size=420, spread=True, footer_text=True) -> bytes:
    meshes = layout(parts, spread)
    tiles = [render_view(meshes, VIEWS[v][1], VIEWS[v][2], size, VIEWS[v][0] if footer_text else "")
             for v in views]
    cols = 2 if len(tiles) > 1 else 1
    rows = math.ceil(len(tiles) / cols)
    allv = np.vstack([m.vertices for _, m in meshes])
    ext = allv.max(0) - allv.min(0)
    footer = 26 if footer_text else 0
    sheet = Image.new("RGB", (cols * size, rows * size + footer), (255, 255, 255))
    for i, t in enumerate(tiles):
        sheet.paste(t, ((i % cols) * size, (i // cols) * size))
    d = ImageDraw.Draw(sheet)
    if footer_text:
        names = ", ".join(n for n, _ in meshes)
        d.text((8, rows * size + 5),
               f"{ext[0]:.1f} × {ext[1]:.1f} × {ext[2]:.1f} mm   parts: {names}",
               fill=(40, 40, 50), font=_font(13))
    buf = io.BytesIO()
    sheet.save(buf, "PNG", optimize=True)
    return buf.getvalue()
