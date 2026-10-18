import math

import manifold3d as mf
import numpy as np
import pytest

import csg
import fnshapes


def solid(node, vars_=None):
    """Build a node; assert it is a valid closed manifold and return it."""
    env = csg.resolve_vars(vars_ or {})
    m = csg.build_node(node, env)
    assert m.status() == mf.Error.NoError
    assert not m.is_empty()
    assert m.volume() > 0
    assert m.genus() >= 0
    return m


def mesh(node, vars_=None):
    t = csg.build({"vars": vars_ or {}, "root": node})["model"]
    assert t.is_watertight and t.is_winding_consistent and t.volume > 0
    return t


def bounds(m):
    b = m.bounding_box()
    return np.array(b[:3]), np.array(b[3:])


# ---------------------------------------------------------------- vectorized expressions

def test_compile_expr_vectorized_and_degrees():
    f = fnshapes.compile_expr("=2*sin(a)+x**2", {"a", "x"})
    a = np.array([0.0, 90.0, 270.0])
    np.testing.assert_allclose(f({"a": a, "x": 3.0}), [9, 11, 7])


@pytest.mark.parametrize("expr,expected", [
    ("atan2(1, 1)", 45), ("asin(1)", 90), ("acos(1)", 0), ("atan(1)", 45),
    ("clamp(5, 0, 2)", 2), ("lerp(10, 20, 0.25)", 12.5), ("smoothstep(0, 1, 0.5)", 0.5),
    ("hypot(3, 4)", 5), ("sign(-3)", -1), ("exp(0)", 1), ("log(1)", 0),
    ("1 if 2 > 1 and 3 > 2 else 0", 1), ("1 if not 2 > 1 else 0", 0), ("max(1, 5, 3)", 5),
])
def test_vector_functions(expr, expected):
    assert float(fnshapes.compile_expr(expr, set())({})) == pytest.approx(expected)


def test_conditional_is_elementwise():
    f = fnshapes.compile_expr("1 if x > 0 else -1", {"x"})
    np.testing.assert_array_equal(f({"x": np.array([-2.0, 3.0])}), [-1, 1])


def test_noise_is_smooth_bounded_and_deterministic():
    x = np.linspace(0, 10, 2001)
    v = fnshapes._noise(x, 0.3, 0.7)
    assert np.all(np.abs(v) <= 1.0)
    assert np.max(np.abs(np.diff(v))) < 0.02                  # continuous
    np.testing.assert_array_equal(v, fnshapes._noise(x, 0.3, 0.7))
    assert np.std(v) > 0.05


@pytest.mark.parametrize("expr", [
    "__import__('os')", "x.real", "x[0]", "(lambda: 1)()", "open('f')", "sin(x=1)",
    "[1, 2]", "'a'", "{1: 2}", "sin(*x)", "2**100", "f'{x}'",
])
def test_compile_expr_rejects_unsafe(expr):
    with pytest.raises(ValueError):
        f = fnshapes.compile_expr(expr, {"x"})
        f({"x": np.array([1.0])})


def test_compile_expr_errors_are_clear():
    with pytest.raises(ValueError, match="unknown variable 'q'.*available: a, z"):
        fnshapes.compile_expr("q+1", {"a", "z"})
    with pytest.raises(ValueError, match="takes 2"):
        fnshapes.compile_expr("atan2(1)", set())
    with pytest.raises(ValueError, match="invalid expression"):
        fnshapes.compile_expr("1+", set())
    with pytest.raises(ValueError, match="too long"):
        fnshapes.compile_expr("+".join(["1"] * 500), set())


def test_scalar_expressions_get_the_new_functions():
    assert csg.eval_expr("atan2(0, 1)", {}) == 0
    assert csg.eval_expr("clamp(7, 0, 5)", {}) == 5
    assert csg.eval_expr("min(3, 1)", {}) == 1                # existing ones untouched


# ---------------------------------------------------------------- radial

@pytest.mark.parametrize("r", [
    5, "=10+2*sin(6*a)*z/h", "=8+3*sin(t*180)+cos(8*a)", "=w+noise(a/30, z/5)",
    "=6+2*cos(5*(a+90*t))", "=3 if z < 5 else 6",
])
@pytest.mark.parametrize("segments,rows", [(3, 2), (24, None), (96, 50)])
def test_radial_is_valid(r, segments, rows):
    node = {"shape": "radial", "h": 20, "r": r, "segments": segments}
    if rows:
        node["rows"] = rows
    m = solid(node, {"w": 7})
    lo, hi = bounds(m)
    assert lo[2] == pytest.approx(0) and hi[2] == pytest.approx(20)


def test_radial_constant_matches_cylinder_volume():
    m = solid({"shape": "radial", "h": 10, "d": "=2*w", "segments": 512}, {"w": 5})
    assert m.volume() == pytest.approx(math.pi * 25 * 10, rel=1e-3)


def test_radial_center_and_booleans():
    m = solid({"op": "difference", "children": [
        {"shape": "radial", "h": 30, "r": "=15+3*sin(t*180)", "center": True},
        {"shape": "cylinder", "d": 20, "h": 40, "center": True}]})
    lo, hi = bounds(m)
    assert lo[2] == pytest.approx(-15) and hi[2] == pytest.approx(15)
    assert m.genus() == 1


def test_radial_errors():
    with pytest.raises(ValueError, match="radius must stay > 0.*z=10"):
        mesh({"shape": "radial", "h": 10, "r": "=5-z"})
    with pytest.raises(ValueError, match="invalid value"):
        mesh({"shape": "radial", "h": 10, "r": "=sqrt(z-5)"})
    with pytest.raises(ValueError, match="requires 'r'"):
        mesh({"shape": "radial", "h": 10})
    with pytest.raises(ValueError, match="too detailed"):
        mesh({"shape": "radial", "h": 10, "r": 5, "segments": 1024, "rows": 4000})
    with pytest.raises(ValueError, match="segments"):
        mesh({"shape": "radial", "h": 10, "r": 5, "segments": 2})


def test_radial_expression_survives_update_of_vars():
    """The per-vertex expression is kept raw (not resolved by csg.resolve) but sees the spec vars."""
    a = mesh({"shape": "radial", "h": 10, "r": "=w+z/10"}, {"w": 4})
    b = mesh({"shape": "radial", "h": 10, "r": "=w+z/10"}, {"w": 6})
    assert b.volume > a.volume


# ---------------------------------------------------------------- heightmap

@pytest.mark.parametrize("z,base", [
    (3, 0), ("=2+x/10", 0), ("=sin(x*20)*cos(y*20)", 2), ("=2*noise(x/5, y/5)", 3),
    ("=3+2*u*v", 0), ("=5-hypot(x-sx/2, y-sy/2)/5", 1),
])
@pytest.mark.parametrize("res", [None, 0.7, 3])
def test_heightmap_is_valid(z, base, res):
    node = {"shape": "heightmap", "size": [20, 13], "z": z, "base": base}
    if res:
        node["resolution"] = res
    m = solid(node)
    lo, hi = bounds(m)
    assert np.allclose(lo[:2], 0) and np.allclose(hi[:2], [20, 13])
    assert lo[2] == pytest.approx(0)


def test_heightmap_volume_and_center():
    m = solid({"shape": "heightmap", "size": [10, 20], "z": "=2+x/10", "center": True})
    assert m.volume() == pytest.approx(500, rel=1e-6)
    lo, hi = bounds(m)
    assert np.allclose(lo[:2], [-5, -10])


def test_heightmap_errors():
    with pytest.raises(ValueError, match="above z=0.*add 'base'"):
        mesh({"shape": "heightmap", "size": [10, 10], "z": "=sin(x*36)"})
    with pytest.raises(ValueError, match="too detailed"):
        mesh({"shape": "heightmap", "size": [1000, 1000], "resolution": 0.1, "z": 1})
    with pytest.raises(ValueError, match="size"):
        mesh({"shape": "heightmap", "size": [10], "z": 1})
    with pytest.raises(ValueError, match="requires 'z'"):
        mesh({"shape": "heightmap", "size": [10, 10]})


# ---------------------------------------------------------------- sweep

RING = {"op": "difference", "profiles": [{"shape": "circle", "d": 6}, {"shape": "circle", "d": 3}]}
PATHS = [
    [[0, 0, 0], [20, 0, 0], [20, 20, 0], [20, 20, 20]],
    [[0, 0], [10, 0], [15, 8]],
    {"type": "spline", "points": [[0, 0, 0], [10, 5, 3], [20, 0, 8], [30, 10, 0]]},
    {"type": "spline", "points": [[0, 0, 0], [20, 5, 3], [25, 25, 8], [0, 20, 4]], "closed": True},
    {"type": "arc", "r": 15, "degrees": 120, "start": 30},
    {"type": "arc", "r": 15, "degrees": 360},
    {"type": "helix", "r": 10, "pitch": 8, "turns": 2.5},
    {"type": "helix", "r": 10, "pitch": 8, "turns": 1, "left": True},
    {"type": "bezier", "points": [[0, 0, 0], [0, 0, 20], [20, 0, 20], [20, 0, 40],
                                  [20, 0, 50], [30, 0, 50], [40, 0, 50]]},
    [[0, 0, 0], [0, 0, 10], [10, 0, 10]],                    # starts vertically
]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("profile", [
    {"shape": "circle", "d": 3}, {"shape": "rect", "size": [4, 2], "center": True}, RING,
    {"shape": "polygon", "points": [[-1, -1], [2, 0], [-1, 1]]},
])
@pytest.mark.parametrize("extra", [{}, {"twist": 90, "taper": 1.5},
                                   {"twist": "=360*t", "taper": "=1+0.3*sin(t*360)"}])
def test_sweep_is_valid(path, profile, extra):
    solid({"shape": "sweep", "profile": profile, "path": path, **extra})


def test_sweep_polyline_has_exact_mitred_volume():
    m = solid({"shape": "sweep", "profile": {"shape": "rect", "size": [4, 2], "center": True},
               "path": [[0, 0, 0], [20, 0, 0], [20, 20, 0], [20, 20, 20]]})
    assert m.volume() == pytest.approx(8 * 60, rel=1e-9)


def test_sweep_torus_and_helix_volumes():
    tor = solid({"shape": "sweep", "profile": {"shape": "circle", "d": 3, "segments": 128},
                 "path": {"type": "arc", "r": 15, "degrees": 360}, "segments": 256})
    assert tor.volume() == pytest.approx(math.pi * 1.5 ** 2 * 2 * math.pi * 15, rel=2e-3)
    assert tor.genus() == 1
    hel = solid({"shape": "sweep", "profile": {"shape": "circle", "d": 2, "segments": 128},
                 "path": {"type": "helix", "r": 10, "pitch": 5, "turns": 2}, "segments": 256})
    length = 2 * math.hypot(2 * math.pi * 10, 5)
    assert hel.volume() == pytest.approx(math.pi * length, rel=2e-3)
    lo, hi = bounds(hel)
    assert hi[2] == pytest.approx(10 + 1, abs=0.05)


def test_sweep_twist_and_taper_change_the_shape():
    base = {"shape": "sweep", "profile": {"shape": "rect", "size": [4, 2], "center": True},
            "path": [[0, 0, 0], [0, 0, 30]]}
    plain = solid(base)
    tapered = solid({**base, "taper": 2})
    assert tapered.volume() == pytest.approx(8 * 30 * (1 + 1 + 1 / 3), rel=1e-3)  # ∫(1+t)² dt
    twisted = solid({**base, "twist": "=total"}, {"total": 90})
    lo, hi = bounds(twisted)
    assert hi[0] - lo[0] > bounds(plain)[1][0] - bounds(plain)[0][0] - 1e-6
    assert twisted.volume() == pytest.approx(plain.volume(), rel=0.02)


def test_sweep_ring_hole_goes_through():
    m = solid({"shape": "sweep", "profile": RING, "path": {"type": "arc", "r": 20, "degrees": 90}})
    assert m.genus() == 1


def test_sweep_errors():
    circ = {"shape": "circle", "d": 1}
    with pytest.raises(ValueError, match="turns too sharply"):
        mesh({"shape": "sweep", "profile": circ, "path": [[0, 0, 0], [10, 0, 0], [0, 0.1, 0]]})
    with pytest.raises(ValueError, match="folds back"):
        mesh({"shape": "sweep", "profile": circ, "path": [[0, 0, 0], [10, 0, 0], [0, 0, 0]]})
    with pytest.raises(ValueError, match="3k\\+1"):
        mesh({"shape": "sweep", "profile": circ, "path": {"type": "bezier", "points": [[0, 0]] * 5}})
    with pytest.raises(ValueError, match="unknown path type"):
        mesh({"shape": "sweep", "profile": circ, "path": {"type": "zigzag"}})
    with pytest.raises(ValueError, match="requires 'path'"):
        mesh({"shape": "sweep", "profile": circ})
    with pytest.raises(ValueError, match="taper must stay > 0"):
        mesh({"shape": "sweep", "profile": circ, "path": [[0, 0, 0], [0, 0, 5]], "taper": "=1-2*t"})
    with pytest.raises(ValueError, match="too short"):
        mesh({"shape": "sweep", "profile": circ, "path": [[0, 0, 0], [0, 0, 0]]})


# ---------------------------------------------------------------- loft

SECTIONS = [
    [{"z": 0, "profile": {"shape": "rect", "size": [20, 20], "center": True}},
     {"z": 15, "profile": {"shape": "circle", "d": 12}}],
    [{"z": 0, "profile": {"shape": "rect", "size": [20, 20], "center": True}},
     {"z": 15, "profile": {"shape": "circle", "d": 12}},
     {"z": 30, "profile": {"shape": "circle", "d": 20}, "rotate": 10}],
    [{"z": 0, "profile": {"shape": "circle", "d": 20}},
     {"z": 10, "profile": {"shape": "spline", "points": [[0, 0], [10, 2], [12, 10], [2, 12]]},
      "offset": [-6, -6]},
     {"z": 12, "profile": {"shape": "polygon", "points": [[-4, -4], [4, -4], [0, 5]]}}],
    [{"z": 0, "profile": {"shape": "rect", "size": [30, 10], "center": True, "radius": 3}},
     {"z": 5, "profile": {"shape": "circle", "d": 8, "segments": 6}, "rotate": 45},
     {"z": 20, "profile": {"shape": "rect", "size": [10, 10]}, "offset": [-5, -5]}],
]


@pytest.mark.parametrize("sections", SECTIONS)
@pytest.mark.parametrize("opts", [{}, {"smooth": True}, {"divisions": 4, "segments": 16},
                                  {"smooth": True, "divisions": 20, "segments": 200}])
def test_loft_is_valid(sections, opts):
    m = solid({"shape": "loft", "sections": sections, **opts})
    lo, hi = bounds(m)
    assert lo[2] == pytest.approx(sections[0]["z"]) and hi[2] == pytest.approx(sections[-1]["z"])


def test_loft_prism_volume_and_corners_kept():
    m = solid({"shape": "loft", "sections": [
        {"z": 0, "profile": {"shape": "rect", "size": [10, 10]}},
        {"z": 10, "profile": {"shape": "rect", "size": [10, 10]}, "offset": [5, 0]}]})
    assert m.volume() == pytest.approx(1000)
    frustum = solid({"shape": "loft", "sections": [
        {"z": 0, "profile": {"shape": "rect", "size": [20, 20], "center": True}},
        {"z": 10, "profile": {"shape": "rect", "size": [10, 10], "center": True}}]})
    assert frustum.volume() == pytest.approx(10 / 3 * (400 + 100 + 200))


def test_loft_errors():
    sq = {"shape": "rect", "size": [5, 5]}
    with pytest.raises(ValueError, match="at least 2"):
        mesh({"shape": "loft", "sections": [{"z": 0, "profile": sq}]})
    with pytest.raises(ValueError, match="increase"):
        mesh({"shape": "loft", "sections": [{"z": 5, "profile": sq}, {"z": 0, "profile": sq}]})
    with pytest.raises(ValueError, match="without holes"):
        mesh({"shape": "loft", "sections": [{"z": 0, "profile": RING}, {"z": 5, "profile": sq}]})
    with pytest.raises(ValueError, match="needs 'z'"):
        mesh({"shape": "loft", "sections": [{"profile": sq}, {"z": 5, "profile": sq}]})


def test_loft_sections_resolve_spec_vars():
    m = mesh({"shape": "loft", "sections": [
        {"z": 0, "profile": {"shape": "circle", "d": "=d"}},
        {"z": "=h", "profile": {"shape": "circle", "d": "=d/2"}}]}, {"d": 20, "h": 12})
    assert m.bounds[1][2] == pytest.approx(12)


# ---------------------------------------------------------------- profiles

def test_profile_booleans_and_transforms():
    m = mesh({"shape": "extrude", "h": 3, "profile": {"op": "difference", "profiles": [
        {"shape": "rect", "size": [20, 10], "center": True},
        {"shape": "circle", "d": 4, "translate": [5, 0]},
        {"shape": "circle", "d": 4, "translate": [-5, 0]}]}})
    assert m.volume == pytest.approx((200 - 2 * math.pi * 4) * 3, rel=0.01)
    rot = mesh({"shape": "extrude", "h": 1, "profile": {"shape": "rect", "size": [10, 2],
                                                         "center": True, "rotate": 90}})
    assert rot.extents[1] == pytest.approx(10)
    hull = mesh({"shape": "extrude", "h": 1, "profile": {"op": "hull", "profiles": [
        {"shape": "circle", "d": 2}, {"shape": "circle", "d": 2, "translate": [10, 0]}]}})
    assert hull.extents[0] == pytest.approx(12, abs=0.01)


def test_spline_profile_passes_through_points():
    m = mesh({"shape": "extrude", "h": 1, "profile": {"shape": "spline",
              "points": [[0, 0], [10, 0], [10, 10], [0, 10]]}})
    assert m.bounds[1][0] >= 10 - 1e-6 and m.bounds[0][0] <= 1e-6


def test_profile_errors():
    with pytest.raises(ValueError, match="requires 'profiles'"):
        mesh({"shape": "extrude", "h": 1, "profile": {"op": "union"}})
    with pytest.raises(ValueError, match="unknown profile op"):
        mesh({"shape": "extrude", "h": 1, "profile": {"op": "xor", "profiles": [{"shape": "circle", "d": 1}]}})
    with pytest.raises(ValueError, match="spline profile"):
        mesh({"shape": "extrude", "h": 1, "profile": {"shape": "spline", "points": [[0, 0], [1, 1]]}})


# ---------------------------------------------------------------- integration

def test_help_mentions_new_shapes():
    for word in ("radial", "heightmap", "sweep", "loft", "noise", "spline"):
        assert word in csg.SPEC_HELP


def test_locate_works_with_expression_shapes():
    spec = {"vars": {}, "root": {"op": "union", "children": [
        {"shape": "radial", "h": 10, "r": "=5+sin(a*4)"},
        {"shape": "heightmap", "size": [5, 5], "z": "=1+x/5", "translate": [30, 0, 0]}]}}
    hits = csg.locate(spec, None, [32, 2, 1])
    assert hits[0]["pointer"] == "/root/children/1"


def test_unknown_shape_lists_new_shapes():
    with pytest.raises(ValueError, match="radial, heightmap, sweep, loft"):
        mesh({"shape": "blob"})
