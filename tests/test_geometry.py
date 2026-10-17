import io
import zipfile

import numpy as np
import pytest
import trimesh

import geometry as g


def test_blk_bounds():
    m = g.blk(2, 3, 4, 1, 1, 1)
    assert np.allclose(m.bounds, [[1, 1, 1], [3, 4, 5]])


def test_cyl_bounds():
    m = g.cyl(5, 10, 0, 0, 2)
    assert m.bounds[0][2] == pytest.approx(2)
    assert m.bounds[1][2] == pytest.approx(12)


def test_union_and_diff_volumes():
    a = g.blk(10, 10, 10, 0, 0, 0)
    b = g.blk(10, 10, 10, 5, 0, 0)
    assert g.union([a, b]).volume == pytest.approx(1500)
    assert g.diff(a, [b]).volume == pytest.approx(500)


def test_diff_without_cutters_returns_input():
    a = g.blk(1, 1, 1, 0, 0, 0)
    assert g.diff(a, []) is a


@pytest.mark.parametrize("shape,kwargs", [
    ("box", dict(x=1, y=2, z=3)),
    ("cylinder", dict(diameter=4, height=5)),
    ("sphere", dict(diameter=6)),
    ("cone", dict(diameter=4, height=5)),
    ("tube", dict(diameter=10, inner_diameter=6, height=5)),
])
def test_primitive_is_watertight(shape, kwargs):
    assert g.primitive(shape, **kwargs).is_watertight


def test_primitive_box_extents_and_case_insensitive():
    assert np.allclose(g.primitive("BOX", x=1, y=2, z=3).extents, [1, 2, 3])


def test_tube_volume():
    m = g.primitive("tube", diameter=10, inner_diameter=6, height=5)
    expected = np.pi * (5 ** 2 - 3 ** 2) * 5
    assert m.volume == pytest.approx(expected, rel=0.02)


def test_primitive_unknown_shape():
    with pytest.raises(ValueError, match="unknown shape"):
        g.primitive("pyramid")


def test_enclosure_dimensions():
    body, lid = g.enclosure(80, 50, 20, wall=2.4, floor=2.4, lid_t=2.4)
    assert np.allclose(body.extents, [84.8, 54.8, 22.4])
    assert np.allclose(lid.extents[:2], [84.8, 54.8])
    assert body.is_watertight and lid.is_watertight


def test_enclosure_without_screws_has_less_material():
    with_screws, _ = g.enclosure(80, 50, 20)
    without, _ = g.enclosure(80, 50, 20, screws=False)
    assert without.volume < with_screws.volume


def test_enclosure_standoff_adds_material():
    plain, _ = g.enclosure(80, 50, 20, screws=False)
    with_standoff, _ = g.enclosure(80, 50, 20, screws=False,
                                   standoffs=[{"x": 10, "y": 10, "hole_d": 2.2}])
    assert with_standoff.volume > plain.volume


@pytest.mark.parametrize("wall", g.WALLS)
def test_enclosure_cutout_removes_material(wall):
    plain, _ = g.enclosure(80, 50, 20, screws=False)
    cut, _ = g.enclosure(80, 50, 20, screws=False,
                         cutouts=[{"wall": wall, "pos": 25, "width": 12, "height": 8, "z": 4}])
    assert cut.volume < plain.volume


def test_enclosure_invalid_wall():
    with pytest.raises(ValueError, match="wall must be one of"):
        g.enclosure(80, 50, 20, cutouts=[{"wall": "top", "pos": 1, "width": 1, "height": 1}])


def test_enclosure_lid_holes_and_slots_remove_material():
    _, plain = g.enclosure(80, 50, 20, screws=False)
    _, holed = g.enclosure(80, 50, 20, screws=False, lid_holes=[{"x": 40, "y": 25, "d": 4}],
                           lid_slots=[{"x": 10, "y": 10, "w": 5, "h": 5}])
    assert holed.volume < plain.volume


def _box_bytes(fmt="stl", extents=(10, 20, 30)):
    data = trimesh.creation.box(extents=extents).export(file_type=fmt)
    return data.encode() if isinstance(data, str) else data


@pytest.mark.parametrize("fmt", ["stl", "obj", "ply", "off"])
def test_load_mesh_formats(fmt):
    m = g.load_mesh(_box_bytes(fmt), fmt)
    assert np.allclose(sorted(m.extents), [10, 20, 30])


def test_load_mesh_defaults_to_stl():
    assert len(g.load_mesh(_box_bytes()).faces) == 12


def test_load_mesh_unsupported_format():
    with pytest.raises(ValueError, match="not supported"):
        g.load_mesh(b"x", "step")


def test_load_mesh_unpacks_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("model.obj", _box_bytes("obj"))
        z.writestr("model.mtl", "newmtl a\n")
    assert len(g.load_mesh(buf.getvalue()).faces) == 12


def test_load_mesh_zip_without_model():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "hi")
    with pytest.raises(ValueError, match="contains no"):
        g.load_mesh(buf.getvalue())


def test_load_mesh_glb_scene():
    scene = trimesh.Scene(trimesh.creation.box(extents=(1, 2, 3)))
    m = g.load_mesh(scene.export(file_type="glb"), "glb")
    assert np.allclose(sorted(m.extents), [1, 2, 3])


def test_load_mesh_empty_mesh():
    with pytest.raises(Exception):
        g.load_mesh(b"", "stl")


def test_repair_merges_duplicate_vertices():
    box = trimesh.creation.box()
    soup = trimesh.Trimesh(box.vertices[box.faces].reshape(-1, 3),
                           np.arange(len(box.faces) * 3).reshape(-1, 3), process=False)
    assert not soup.is_watertight
    assert g.repair(soup).is_watertight


def test_shape_at_centers_on_at_point():
    m = g.shape_at({"shape": "box", "x": 2, "y": 2, "z": 2, "at": [10, 20, 30]})
    assert np.allclose(m.bounds.mean(axis=0), [10, 20, 30])


def test_shape_at_rotation():
    m = g.shape_at({"shape": "cylinder", "diameter": 2, "height": 10, "rotate": [90, 0, 0]})
    assert m.extents[1] == pytest.approx(10, abs=0.01)


def test_modify_scale_uniform_and_per_axis():
    m = trimesh.creation.box(extents=(1, 1, 1))
    assert np.allclose(g.modify(m, scale=2).extents, [2, 2, 2])
    assert np.allclose(g.modify(m, scale=[1, 2, 3]).extents, [1, 2, 3])


def test_modify_scale_must_be_positive():
    with pytest.raises(ValueError, match="scale must be > 0"):
        g.modify(trimesh.creation.box(), scale=0)


def test_modify_rotate():
    m = trimesh.creation.box(extents=(1, 2, 3))
    assert np.allclose(g.modify(m, rotate=[0, 0, 90]).extents, [2, 1, 3], atol=1e-6)


def test_modify_mirror_keeps_bounds_and_rejects_bad_axis():
    m = trimesh.creation.box(extents=(1, 2, 3))
    assert np.allclose(g.modify(m, mirror="x").extents, m.extents)
    with pytest.raises(ValueError, match="mirror"):
        g.modify(m, mirror="q")


def test_modify_place_on_bed_and_center_xy():
    m = trimesh.creation.box(extents=(2, 2, 2))
    m.apply_translation([10, 10, 10])
    out = g.modify(m, center_xy=True, place_on_bed=True)
    assert out.bounds[0][2] == pytest.approx(0)
    assert out.bounds.mean(axis=0)[:2] == pytest.approx([0, 0])


def test_modify_does_not_mutate_input():
    m = trimesh.creation.box(extents=(1, 1, 1))
    before = m.bounds.copy()
    g.modify(m, scale=5)
    assert np.allclose(m.bounds, before)


def test_modify_add_and_subtract():
    m = trimesh.creation.box(extents=(20, 20, 20))
    added = g.modify(m, add=[{"shape": "box", "x": 5, "y": 5, "z": 5, "at": [0, 0, 25]}])
    assert added.volume > m.volume
    holed = g.modify(m, subtract=[{"shape": "cylinder", "diameter": 3, "height": 40, "at": [0, 0, 10]}])
    assert holed.volume < m.volume


def test_modify_requires_watertight_for_boolean():
    soup = trimesh.Trimesh(vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], faces=[[0, 1, 2]])
    with pytest.raises(ValueError, match="not watertight"):
        g.modify(soup, subtract=[{"shape": "box", "x": 1, "y": 1, "z": 1}])


def test_stl_size():
    assert g.stl_size(trimesh.creation.box()) == 84 + 50 * 12


def test_simplify_to_fits_budget():
    sphere = trimesh.creation.icosphere(subdivisions=4)
    small = g.simplify_to(sphere, 20_000)
    assert g.stl_size(small) <= 20_000
    assert len(small.faces) < len(sphere.faces)


def test_rgb_names_hex_and_errors():
    assert g._rgb("#ff0000") == (1.0, 0.0, 0.0)
    assert g._rgb("RED") == pytest.approx(g._rgb("#e8312f"))
    with pytest.raises(ValueError, match="unknown color"):
        g._rgb("notacolor")


def test_to_obj_structure():
    box = trimesh.creation.box()
    obj, mtl = g.to_obj([("a", box, "red"), ("b", box, "")], "set.mtl")
    assert obj.startswith("mtllib set.mtl")
    assert obj.count("\no ") == 2 and obj.count("\nf ") == 24
    assert "newmtl m0_a" in mtl and "newmtl m1_b" in mtl
    # second part's face indices are offset past the first part's vertices
    assert f"f {1 + 8} " in obj or any(l.startswith("f 9") or " 9 " in l for l in obj.splitlines())
