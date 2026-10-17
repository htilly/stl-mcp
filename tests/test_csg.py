import math

import numpy as np
import pytest

import csg


def bb(m):
    b = m.bounding_box()
    return np.array(b[:3]), np.array(b[3:])


def node_mesh(node, vars_=None):
    return csg.build({"vars": vars_ or {}, "root": node})["model"]


# ---------------------------------------------------------------- expressions

@pytest.mark.parametrize("expr,expected", [
    ("1+2*3", 7),
    ("(1+2)*3", 9),
    ("7//2", 3),
    ("7%4", 3),
    ("2**3", 8),
    ("-3+5", 2),
    ("+4", 4),
    ("10/4", 2.5),
    ("min(3,1,2)", 1),
    ("max(3,1,2)", 3),
    ("abs(-4)", 4),
    ("round(2.6)", 3),
    ("sqrt(16)", 4),
    ("floor(2.9)", 2),
    ("ceil(2.1)", 3),
    ("1 if 2>1 else 0", 1),
    ("1 if 2<1 else 0", 0),
    ("3>=3", True),
    ("3!=3", False),
])
def test_eval_expr_values(expr, expected):
    assert csg.eval_expr(expr, {}) == expected


def test_eval_expr_trig_uses_degrees():
    assert csg.eval_expr("sin(90)", {}) == pytest.approx(1)
    assert csg.eval_expr("cos(60)", {}) == pytest.approx(0.5)
    assert csg.eval_expr("tan(45)", {}) == pytest.approx(1)


def test_eval_expr_constants_and_variables():
    assert csg.eval_expr("pi", {}) == pytest.approx(math.pi)
    assert csg.eval_expr("w*2+wall", {"w": 10, "wall": 1}) == 21


def test_eval_expr_variable_shadows_constant():
    assert csg.eval_expr("pi", {"pi": 3}) == 3


def test_eval_expr_unknown_variable():
    with pytest.raises(ValueError, match="unknown variable 'nope'"):
        csg.eval_expr("nope+1", {})


@pytest.mark.parametrize("expr", [
    "__import__('os').system('true')",
    "open('/etc/passwd')",
    "().__class__",
    "[1,2][0]",
    "'abc'",
    "a.b",
    "lambda: 1",
    "min(x=1)",
    "(lambda: 1)()",
    "1 < 2 < 3",
    "1 and 2",
    "not 1",
    "eval('1')",
    "exec('1')",
])
def test_eval_expr_rejects_disallowed(expr):
    with pytest.raises(ValueError):
        csg.eval_expr(expr, {"a": 1})


def test_eval_expr_rejects_huge_exponent():
    with pytest.raises(ValueError, match="exponent too large"):
        csg.eval_expr("2**1000", {})


def test_eval_expr_syntax_error():
    with pytest.raises(SyntaxError):
        csg.eval_expr("1+", {})


def test_resolve_recurses_but_skips_children():
    env = {"w": 4}
    out = csg.resolve({"size": ["=w", "=w*2", 3], "children": [{"x": "=w"}], "n": "plain"}, env)
    assert out == {"size": [4, 8, 3], "children": [{"x": "=w"}], "n": "plain"}


def test_resolve_leaves_non_expression_values():
    assert csg.resolve("abc", {}) == "abc"
    assert csg.resolve(5, {}) == 5


def test_resolve_vars_in_order():
    assert csg.resolve_vars({"a": 2, "b": "=a*3", "c": "=a+b"}) == {"a": 2, "b": 6, "c": 8}


def test_resolve_vars_forward_reference_fails():
    with pytest.raises(ValueError, match="unknown variable"):
        csg.resolve_vars({"a": "=b", "b": 1})


def test_resolve_vars_empty():
    assert csg.resolve_vars(None) == {}


# ---------------------------------------------------------------- helpers

def test_vec3():
    assert csg._vec3(2) == (2.0, 2.0, 2.0)
    assert csg._vec3([1, 2, 3]) == (1.0, 2.0, 3.0)
    for bad in ([1, 2], "abc", None, [1, 2, 3, 4]):
        with pytest.raises(ValueError, match="must be"):
            csg._vec3(bad, "thing")


def test_segs_default_and_cap():
    assert csg._segs({}) == csg.DEFAULT_SEGMENTS
    assert csg._segs({"segments": 12}) == 12
    assert csg._segs({"segments": 100000}) == 512


def test_radius_prefers_r_then_d():
    assert csg._radius({"r": 3}) == 3
    assert csg._radius({"d": 6}) == 3
    assert csg._radius({"r": 3, "d": 100}) == 3
    with pytest.raises(ValueError, match="requires"):
        csg._radius({"shape": "sphere"})


# ---------------------------------------------------------------- shapes

def test_box_size_and_volume():
    m = csg.build_node({"shape": "box", "size": [10, 20, 30]}, {})
    lo, hi = bb(m)
    assert np.allclose(lo, 0) and np.allclose(hi, [10, 20, 30])
    assert m.volume() == pytest.approx(6000)


def test_cube_alias_and_scalar_size():
    m = csg.build_node({"shape": "cube", "size": 5}, {})
    assert np.allclose(bb(m)[1], 5)


def test_box_centered():
    lo, hi = bb(csg.build_node({"shape": "box", "size": [10, 10, 10], "center": True}, {}))
    assert np.allclose(lo, -5) and np.allclose(hi, 5)


def test_box_rounded_all_has_less_volume():
    plain = csg.build_node({"shape": "box", "size": [20, 20, 20]}, {}).volume()
    rounded = csg.build_node({"shape": "box", "size": [20, 20, 20], "radius": 4}, {})
    assert rounded.volume() < plain
    assert np.allclose(bb(rounded)[1], 20, atol=0.01)


def test_box_rounded_vertical_only_rounds_xy_edges():
    m = csg.build_node({"shape": "box", "size": [20, 20, 10], "radius": 4, "round": "vertical"}, {})
    lo, hi = bb(m)
    assert np.allclose(hi, [20, 20, 10], atol=0.01)
    expected = (20 * 20 - (4 - math.pi) * 16) * 10
    assert m.volume() == pytest.approx(expected, rel=0.01)


def test_box_radius_clamped_to_half_size():
    m = csg.build_node({"shape": "box", "size": [4, 4, 4], "radius": 100}, {})
    assert not m.is_empty()
    assert np.allclose(bb(m)[1], 4, atol=0.01)


def test_cylinder_by_diameter_and_radius():
    a = csg.build_node({"shape": "cylinder", "d": 10, "h": 5}, {})
    b = csg.build_node({"shape": "cylinder", "r": 5, "h": 5}, {})
    assert a.volume() == pytest.approx(b.volume())
    assert a.volume() == pytest.approx(math.pi * 25 * 5, rel=0.01)
    assert bb(a)[1][2] == pytest.approx(5)


def test_cylinder_center_and_taper():
    lo, hi = bb(csg.build_node({"shape": "cylinder", "d": 10, "h": 6, "center": True}, {}))
    assert lo[2] == pytest.approx(-3) and hi[2] == pytest.approx(3)
    tapered = csg.build_node({"shape": "cylinder", "d": 10, "d2": 4, "h": 6}, {})
    straight = csg.build_node({"shape": "cylinder", "d": 10, "h": 6}, {})
    assert tapered.volume() < straight.volume()


def test_cylinder_d1_form():
    m = csg.build_node({"shape": "cylinder", "d1": 8, "h": 2}, {})
    assert bb(m)[1][0] == pytest.approx(4, abs=0.01)


def test_cylinder_requires_dimensions():
    with pytest.raises(ValueError):
        csg.build_node({"shape": "cylinder", "h": 5}, {})
    with pytest.raises(KeyError):
        csg.build_node({"shape": "cylinder", "d": 5}, {})


def test_cone_tapers_to_point():
    m = csg.build_node({"shape": "cone", "d": 10, "h": 6}, {})
    assert m.volume() == pytest.approx(math.pi * 25 * 6 / 3, rel=0.02)


def test_sphere_volume():
    m = csg.build_node({"shape": "sphere", "d": 10}, {})
    assert m.volume() == pytest.approx(4 / 3 * math.pi * 125, rel=0.02)


def test_tube_volume_and_validation():
    m = csg.build_node({"shape": "tube", "d": 10, "inner_d": 6, "h": 4}, {})
    assert m.volume() == pytest.approx(math.pi * (25 - 9) * 4, rel=0.01)
    c = csg.build_node({"shape": "tube", "d": 10, "inner_d": 6, "h": 4, "center": True}, {})
    assert bb(c)[0][2] == pytest.approx(-2)
    with pytest.raises(ValueError, match="inner_d must be smaller"):
        csg.build_node({"shape": "tube", "d": 6, "inner_d": 6, "h": 4}, {})


def test_torus_volume():
    m = csg.build_node({"shape": "torus", "d": 20, "tube_d": 4}, {})
    assert m.volume() == pytest.approx(2 * math.pi ** 2 * 10 * 4, rel=0.03)


def test_wedge_volume():
    m = csg.build_node({"shape": "wedge", "size": [10, 6, 4]}, {})
    assert m.volume() == pytest.approx(10 * 6 * 4 / 2)


def test_extrude_polygon_and_holes():
    pts = [[0, 0], [10, 0], [10, 10], [0, 10]]
    solid = csg.build_node({"shape": "extrude", "h": 3, "profile": {"shape": "polygon", "points": pts}}, {})
    assert solid.volume() == pytest.approx(300)
    holed = csg.build_node({"shape": "extrude", "h": 3, "profile": {
        "shape": "polygon", "points": pts, "holes": [[[2, 2], [4, 2], [4, 4], [2, 4]]]}}, {})
    assert holed.volume() == pytest.approx(300 - 12)


def test_extrude_circle_and_rect_profiles():
    c = csg.build_node({"shape": "extrude", "h": 2, "profile": {"shape": "circle", "d": 10}}, {})
    assert c.volume() == pytest.approx(math.pi * 25 * 2, rel=0.01)
    r = csg.build_node({"shape": "extrude", "h": 2,
                        "profile": {"shape": "rect", "size": [10, 4], "center": True}}, {})
    assert r.volume() == pytest.approx(80)
    assert bb(r)[0][0] == pytest.approx(-5)


def test_extrude_rounded_rect_and_offset():
    rr = csg.build_node({"shape": "extrude", "h": 1,
                         "profile": {"shape": "rect", "size": [10, 10], "radius": 2}}, {})
    assert rr.volume() < 100
    off = csg.build_node({"shape": "extrude", "h": 1,
                          "profile": {"shape": "rect", "size": [10, 10], "offset": 1}}, {})
    assert off.volume() > 100


def test_extrude_scale_top_shrinks():
    pts = [[0, 0], [10, 0], [10, 10], [0, 10]]
    m = csg.build_node({"shape": "extrude", "h": 5, "scale_top": [0.5, 0.5],
                        "profile": {"shape": "polygon", "points": pts}}, {})
    assert 100 * 5 * 0.25 < m.volume() < 500


def test_extrude_polygon_directly_on_node():
    m = csg.build_node({"shape": "extrude", "h": 2, "points": [[0, 0], [4, 0], [0, 4]]}, {})
    assert m.volume() == pytest.approx(16)


def test_polygon_requires_three_points():
    with pytest.raises(ValueError, match="at least 3"):
        csg.build_node({"shape": "extrude", "h": 1, "profile": {"shape": "polygon", "points": [[0, 0], [1, 1]]}}, {})


def test_unknown_profile():
    with pytest.raises(ValueError, match="unknown profile"):
        csg.build_node({"shape": "extrude", "h": 1, "profile": {"shape": "star"}}, {})


def test_revolve_profile_makes_cylinder():
    m = csg.build_node({"shape": "revolve", "profile": {
        "shape": "polygon", "points": [[0, 0], [5, 0], [5, 10], [0, 10]]}}, {})
    assert m.volume() == pytest.approx(math.pi * 25 * 10, rel=0.02)


def test_revolve_partial_degrees_is_smaller():
    spec = {"shape": "revolve", "profile": {"shape": "polygon", "points": [[1, 0], [5, 0], [5, 10], [1, 10]]}}
    full = csg.build_node(spec, {}).volume()
    half = csg.build_node({**spec, "degrees": 180}, {}).volume()
    assert half == pytest.approx(full / 2, rel=0.02)


def test_template_shape_dispatch():
    m = csg.build_node({"shape": "boss", "size": "M3"}, {})
    assert not m.is_empty()


def test_unknown_shape():
    with pytest.raises(ValueError, match="unknown shape 'pyramid'"):
        csg.build_node({"shape": "pyramid"}, {})


# ---------------------------------------------------------------- transforms / order

def test_translate():
    m = csg.build_node({"shape": "box", "size": 2, "translate": [5, 6, 7]}, {})
    assert np.allclose(bb(m)[0], [5, 6, 7])


def test_scale_then_translate():
    m = csg.build_node({"shape": "box", "size": 1, "scale": [2, 3, 4], "translate": [10, 0, 0]}, {})
    lo, hi = bb(m)
    assert np.allclose(lo, [10, 0, 0]) and np.allclose(hi, [12, 3, 4])


def test_rotate_degrees():
    m = csg.build_node({"shape": "box", "size": [10, 2, 2], "rotate": [0, 0, 90]}, {})
    lo, hi = bb(m)
    assert np.allclose(hi - lo, [2, 10, 2], atol=1e-6)


def test_mirror_flips_across_origin():
    m = csg.build_node({"shape": "box", "size": 2, "translate": [1, 0, 0], "mirror": [1, 0, 0]}, {})
    lo, hi = bb(csg.build_node({"shape": "box", "size": 2, "mirror": [1, 0, 0]}, {}))
    assert np.allclose(lo[0], -2) and np.allclose(hi[0], 0)
    assert not m.is_empty()


def test_expressions_in_nodes():
    m = csg.build_node({"shape": "box", "size": ["=w", "=w/2", 3]}, {"w": 10})
    assert np.allclose(bb(m)[1], [10, 5, 3])


def test_linear_array():
    m = csg.build_node({"shape": "box", "size": 2, "array": {"count": [3, 2, 1], "step": [5, 5, 0]}}, {})
    lo, hi = bb(m)
    assert np.allclose(hi, [12, 7, 2])
    assert m.volume() == pytest.approx(6 * 8)


def test_array_scalar_count():
    m = csg.build_node({"shape": "box", "size": 1, "array": {"count": 4, "step": [2, 0, 0]}}, {})
    assert bb(m)[1][0] == pytest.approx(7)


def test_array_too_many_copies():
    with pytest.raises(ValueError, match="too many copies"):
        csg.build_node({"shape": "box", "size": 1, "array": {"count": [20, 20, 6], "step": [2, 2, 2]}}, {})


def test_polar_array_rotates_about_origin_before_translate():
    # polar runs before the node's own translate, so the usual pattern is a group around an offset child
    ring = {"op": "group", "polar": {"count": 4},
            "children": [{"shape": "box", "size": 1, "translate": [10, 0, 0]}]}
    m = csg.build_node(ring, {})
    assert m.volume() == pytest.approx(4)
    lo, hi = bb(m)
    assert hi[0] == pytest.approx(11) and lo[0] == pytest.approx(-11)
    assert hi[1] == pytest.approx(11) and lo[1] == pytest.approx(-11)


def test_polar_array_arc_and_limit():
    arc = {"op": "group", "polar": {"count": 3, "degrees": 90},
           "children": [{"shape": "box", "size": 1, "translate": [10, 0, 0]}]}
    m = csg.build_node(arc, {})
    assert m.volume() == pytest.approx(3)
    assert bb(m)[0][0] == pytest.approx(-1, abs=0.01)          # last copy lies on the +y axis
    with pytest.raises(ValueError, match="too many copies"):
        csg.build_node({"shape": "box", "size": 1, "polar": {"count": 1000}}, {})


# ---------------------------------------------------------------- operations

def _two_boxes():
    return [{"shape": "box", "size": 10}, {"shape": "box", "size": 10, "translate": [5, 0, 0]}]


def test_union():
    m = csg.build_node({"op": "union", "children": _two_boxes()}, {})
    assert m.volume() == pytest.approx(1500)


def test_group_is_union():
    m = csg.build_node({"op": "group", "children": _two_boxes()}, {})
    assert m.volume() == pytest.approx(1500)


def test_difference_first_minus_rest():
    kids = _two_boxes() + [{"shape": "box", "size": 10, "translate": [-5, 0, 0]}]
    m = csg.build_node({"op": "difference", "children": kids}, {})
    assert m.volume() == pytest.approx(0, abs=1e-6) or m.is_empty()


def test_difference_single_child_unchanged():
    m = csg.build_node({"op": "difference", "children": [{"shape": "box", "size": 3}]}, {})
    assert m.volume() == pytest.approx(27)


def test_intersection():
    m = csg.build_node({"op": "intersection", "children": _two_boxes()}, {})
    assert m.volume() == pytest.approx(500)


def test_hull():
    kids = [{"shape": "box", "size": 1}, {"shape": "box", "size": 1, "translate": [9, 0, 0]}]
    m = csg.build_node({"op": "hull", "children": kids}, {})
    assert m.volume() == pytest.approx(10)


def test_minkowski_grows_shape():
    kids = [{"shape": "box", "size": 10}, {"shape": "sphere", "d": 2, "segments": 16}]
    m = csg.build_node({"op": "minkowski", "children": kids}, {})
    lo, hi = bb(m)
    assert lo[0] == pytest.approx(-1, abs=0.05) and hi[0] == pytest.approx(11, abs=0.05)


def test_op_transform_applies_to_result():
    m = csg.build_node({"op": "union", "children": _two_boxes(), "translate": [0, 0, 100]}, {})
    assert bb(m)[0][2] == pytest.approx(100)


def test_unknown_op():
    with pytest.raises(ValueError, match="unknown op 'xor'"):
        csg.build_node({"op": "xor", "children": [{"shape": "box", "size": 1}]}, {})


def test_op_requires_children():
    with pytest.raises(ValueError, match="requires 'children'"):
        csg.build_node({"op": "union"}, {})


def test_node_must_have_shape_or_op():
    with pytest.raises(ValueError, match="'shape' or 'op'"):
        csg.build_node({"translate": [1, 0, 0]}, {})


def test_node_must_be_object():
    with pytest.raises(ValueError, match="must be an object"):
        csg.build_node([1, 2], {})


def test_hidden_node_is_empty_and_skipped_in_ops():
    hidden = {"shape": "box", "size": 10, "hidden": True}
    assert csg.build_node(hidden, {}).is_empty()
    m = csg.build_node({"op": "union", "children": [{"shape": "box", "size": 2}, hidden]}, {})
    assert m.volume() == pytest.approx(8)


def test_tree_depth_limit():
    node = {"shape": "box", "size": 1}
    for _ in range(70):
        node = {"op": "union", "children": [node]}
    with pytest.raises(ValueError, match="too deep"):
        csg.build_node(node, {})


# ---------------------------------------------------------------- build / to_trimesh

def test_build_root_returns_single_model_part():
    parts = csg.build({"root": {"shape": "box", "size": [1, 2, 3]}})
    assert list(parts) == ["model"]
    assert parts["model"].is_watertight
    assert np.allclose(sorted(parts["model"].extents), [1, 2, 3])


def test_build_multiple_parts_with_vars():
    parts = csg.build({"vars": {"w": 6}, "parts": {
        "body": {"shape": "box", "size": ["=w", "=w", 2]},
        "lid": {"shape": "cylinder", "d": "=w", "h": 1}}})
    assert set(parts) == {"body", "lid"}
    assert parts["body"].volume == pytest.approx(72)


def test_to_trimesh_roundtrip():
    mesh = csg.to_trimesh(csg.build_node({"shape": "sphere", "d": 10}, {}))
    assert mesh.is_watertight and len(mesh.faces) > 0


@pytest.mark.parametrize("spec,msg", [
    ([], "spec must be an object"),
    ({}, "requires 'root'"),
    ({"parts": {}}, "'parts' must be"),
    ({"parts": ["a"]}, "'parts' must be"),
])
def test_build_invalid_specs(spec, msg):
    with pytest.raises(ValueError, match=msg):
        csg.build(spec)


def test_build_empty_part():
    spec = {"root": {"op": "intersection", "children": [
        {"shape": "box", "size": 1}, {"shape": "box", "size": 1, "translate": [10, 0, 0]}]}}
    with pytest.raises(ValueError, match="came out empty"):
        csg.build(spec)


def test_spec_help_mentions_templates():
    assert "screw_hole" in csg.SPEC_HELP and "CSG spec" in csg.SPEC_HELP


# ---------------------------------------------------------------- locate

SPEC_LOCATE = {"vars": {"w": 10}, "parts": {
    "body": {"op": "difference", "children": [
        {"shape": "box", "size": ["=w", "=w", "=w"], "name": "block"},
        {"shape": "cylinder", "d": 2, "h": 20, "translate": [5, 5, -5], "name": "hole"}]},
    "lid": {"shape": "box", "size": [10, 10, 2], "translate": [0, 0, 50]}}}


def test_locate_closest_node_first_and_names():
    res = csg.locate(SPEC_LOCATE, "body", [5, 5, 5])
    assert res[0]["pointer"] in ("/parts/body/children/0", "/parts/body/children/1")
    assert {r["name"] for r in res} == {"block", "hole"}
    assert all(r["distance_mm"] == 0 for r in res)
    # ties are broken by smaller bounding volume -> the hole before the block
    assert res[0]["name"] == "hole"


def test_locate_distance():
    res = csg.locate(SPEC_LOCATE, "body", [5, 5, 15])
    block = next(r for r in res if r["name"] == "block")
    assert block["distance_mm"] == pytest.approx(5)


def test_locate_restricts_to_part_and_falls_back_to_first():
    lid = csg.locate(SPEC_LOCATE, "lid", [5, 5, 51])
    assert [r["pointer"] for r in lid] == ["/parts/lid"]
    fallback = csg.locate(SPEC_LOCATE, "missing", [5, 5, 5])
    assert all(r["pointer"].startswith("/parts/body") for r in fallback)


def test_locate_root_spec_and_limit():
    spec = {"root": {"op": "union", "children": [
        {"shape": "box", "size": 1}, {"shape": "box", "size": 1, "translate": [3, 0, 0]},
        {"shape": "box", "size": 1, "translate": [6, 0, 0]}]}}
    res = csg.locate(spec, None, [0, 0, 0], limit=2)
    assert len(res) == 2 and res[0]["pointer"] == "/root/children/0"
    assert "_vol" not in res[0]


def test_locate_applies_ancestor_transforms():
    spec = {"root": {"op": "union", "translate": [100, 0, 0], "children": [{"shape": "box", "size": 1}]}}
    res = csg.locate(spec, None, [100.5, 0.5, 0.5])
    assert res[0]["distance_mm"] == 0


def test_locate_skips_hidden_and_broken_nodes():
    spec = {"root": {"op": "union", "children": [
        {"shape": "box", "size": 1, "hidden": True},
        {"shape": "cylinder"},
        {"shape": "box", "size": 2}]}}
    res = csg.locate(spec, None, [0, 0, 0])
    assert [r["pointer"] for r in res] == ["/root/children/2"]
