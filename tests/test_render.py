import io

import numpy as np
import pytest
import trimesh
from PIL import Image

import render


def _box(x=10, y=10, z=10, at=(0, 0, 0)):
    m = trimesh.creation.box(extents=(x, y, z))
    m.apply_translation(at)
    return m


def _open(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png))


# ---- layout

def test_layout_spreads_parts_along_x_with_gap():
    parts = {"a": _box(10, 10, 10, (100, 0, 0)), "b": _box(20, 5, 5, (-50, 3, 0))}
    out = render.layout(parts, gap=10)
    (na, a), (nb, b) = out
    assert (na, nb) == ("a", "b")
    assert a.bounds[0][0] == pytest.approx(95)            # first part stays where it is
    assert b.bounds[0][0] == pytest.approx(a.bounds[1][0] + 10)
    assert b.bounds[0][1] == pytest.approx(0.5)           # y/z untouched
    assert parts["a"].bounds[0][0] == pytest.approx(95)   # input meshes not mutated
    assert parts["b"].bounds[0][0] == pytest.approx(-60)


def test_layout_no_spread_or_single_part_keeps_positions():
    parts = {"a": _box(at=(5, 0, 0)), "b": _box(at=(50, 0, 0))}
    out = render.layout(parts, spread=False)
    assert out[1][1].bounds[0][0] == pytest.approx(45)
    single = render.layout({"a": _box(at=(5, 0, 0))})
    assert single[0][1].bounds[0][0] == pytest.approx(0)


# ---- camera

@pytest.mark.parametrize("az,el", [(0, 0), (-45, 30), (0, 90), (-90, 0), (135, 30)])
def test_rot_is_orthonormal(az, el):
    right, up, fwd = render._rot(az, el)
    basis = np.array([right, up, fwd])
    assert np.allclose(basis @ basis.T, np.eye(3), atol=1e-9)


def test_rot_front_view_looks_along_plus_y_with_z_up():
    right, up, fwd = render._rot(0, 0)
    assert np.allclose(fwd, [0, 1, 0]) and np.allclose(right, [1, 0, 0]) and np.allclose(up, [0, 0, 1])


# ---- helpers

def test_split_segments():
    seg = np.array([[[0, 0, 0], [10, 0, 0]], [[0, 0, 0], [1, 0, 0]]], float)
    out = render._split_segments(seg, 2.5)
    assert out.shape == (4 + 1, 2, 3)
    assert out[0][0][0] == 0 and out[3][1][0] == pytest.approx(10)
    assert render._split_segments(np.zeros((0, 2, 3)), 1).shape == (0, 2, 3)


def test_sharp_edges_of_box_are_found():
    m = _box()
    edges = render._sharp_edges(m, np.array([0.0, 1.0, 0.0]))
    assert edges.ndim == 3 and edges.shape[1:] == (2, 3) and len(edges) > 0


def test_subdivided_increases_faces_and_keeps_big_meshes():
    m = _box(50, 50, 50)
    assert len(render._subdivided(m, 10).faces) > len(m.faces)
    assert len(render._subdivided(m, 1000).faces) == len(m.faces)


def test_font_always_returns_something():
    assert render._font(12) is not None


# ---- rendering

def test_render_view_size_and_content():
    img = render.render_view([("a", _box())], -45, 30, size=100, label="iso")
    assert img.size == (100, 100) and img.mode == "RGB"
    arr = np.asarray(img)
    assert (arr != np.array(render.BG)).any(axis=-1).sum() > 100  # something was drawn
    assert tuple(arr[50, 50]) != render.BG                       # model is centered


def test_render_view_empty_when_all_faces_hidden():
    flat = trimesh.Trimesh(vertices=[[0, 0, 0], [1, 0, 0], [0, 0, 1]], faces=[[0, 1, 2]])
    img = render.render_view([("a", flat)], 0, 0, size=50, ss=1)  # edge-on triangle
    assert img.size == (50, 50)


def test_render_png_default_four_views_with_footer():
    png = render.render_png({"model": _box(10, 20, 30)}, size=80)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    img = _open(png)
    assert img.size == (160, 160 + 26)


def test_render_png_without_footer_and_single_view():
    img = _open(render.render_png({"m": _box()}, views=("front",), size=64, footer_text=False))
    assert img.size == (64, 64)


@pytest.mark.parametrize("views,expected", [
    (("iso", "top", "front"), (2 * 50, 2 * 50)),
    (tuple(render.VIEWS), (2 * 50, 3 * 50)),
])
def test_render_png_grid_dimensions(views, expected):
    img = _open(render.render_png({"m": _box()}, views=views, size=50, footer_text=False))
    assert img.size == expected


def test_render_png_multiple_parts_use_distinct_colors():
    parts = {"a": _box(10, 10, 10), "b": _box(10, 10, 10)}
    img = render.render_view(render.layout(parts), 0, 90, size=120, ss=1)
    colors = {tuple(int(v) for v in p) for p in np.asarray(img).reshape(-1, 3)}
    assert any(c[2] > c[0] for c in colors)       # blue-ish first part
    assert any(c[0] > c[2] + 40 for c in colors)  # orange-ish second part


def test_render_png_unknown_view_raises():
    with pytest.raises(KeyError):
        render.render_png({"m": _box()}, views=("sideways",), size=32)
