import json

import numpy as np
import pytest
import trimesh

import geometry as g
import printcheck as pc
from store import Store


def codes(mesh, **settings):
    return {w["code"] for w in pc.check(mesh, settings)}


def box(extents, at=(0, 0, 0)):
    m = trimesh.creation.box(extents=extents)
    m.apply_translation(at)
    return m


# ---- the checks

def test_solid_box_has_no_warnings():
    assert pc.check(box((40, 30, 20))) == []


def test_enclosure_body_has_no_warnings():
    body, _ = g.enclosure(60, 40, 20)
    assert pc.check(body) == []


def test_open_mesh_is_not_watertight():
    b = box((10, 10, 10))
    open_ = trimesh.Trimesh(b.vertices, b.faces[2:], process=False)
    w = [x for x in pc.check(open_) if x["code"] == "not_watertight"]
    assert w and w[0]["severity"] == "critical"


def test_inverted_mesh_is_reported():
    b = box((10, 10, 10))
    b.invert()
    assert "inverted_normals" in codes(b)


def test_separate_shells_on_the_floor_are_grounded():
    two = trimesh.util.concatenate([box((10, 10, 10)), box((10, 10, 10), (30, 0, 0))])
    assert "disconnected_shells" not in codes(two)


def test_shell_in_mid_air_is_flagged():
    two = trimesh.util.concatenate([box((10, 10, 10)), box((10, 10, 10), (30, 0, 20))])
    w = [x for x in pc.check(two) if x["code"] == "disconnected_shells"]
    assert w and w[0]["detail"]["shells"] == 2 and w[0]["detail"]["floating"] == 1
    assert w[0]["detail"]["near_mm"] == [[30.0, 0.0, 20.0]]


def test_shell_resting_on_a_grounded_shell_is_grounded():
    # a wide lid sitting on a narrow post, which stands on the floor
    stack = trimesh.util.concatenate([box((4, 4, 10)), box((20, 20, 2), (0, 0, 6))])
    assert "disconnected_shells" not in codes(stack)
    # and a small cube sitting on top of a big block
    stack = trimesh.util.concatenate([box((20, 20, 10)), box((4, 4, 4), (0, 0, 7))])
    assert "disconnected_shells" not in codes(stack)


def test_thin_plate_is_flagged_with_thickness_and_location():
    w = [x for x in pc.check(box((30, 30, 0.6))) if x["code"] == "thin_walls"]
    assert w
    assert w[0]["detail"]["thinnest_mm"] == pytest.approx(0.6, abs=0.1)
    assert w[0]["detail"]["area_mm2"] > 500


def test_thin_wall_threshold_is_configurable():
    plate = box((30, 30, 0.6))
    assert "thin_walls" in codes(plate)
    assert "thin_walls" not in codes(plate, min_wall_mm=0.4)
    assert "thin_walls" not in codes(plate, check_thin_walls=False)


def test_thin_wall_small_area_is_ignored():
    sliver = box((2, 1, 0.6))     # about 8 mm² of surface, all of it thin
    assert "thin_walls" in codes(sliver)
    assert "thin_walls" not in codes(sliver, thin_wall_min_area_mm2=50)


def test_air_gap_is_not_a_thin_wall():
    # two thick blocks 0.5 mm apart: the gap is air, not material
    blocks = trimesh.util.concatenate([box((20, 20, 10)), box((20, 20, 10), (20.5, 0, 0))])
    assert "thin_walls" not in codes(blocks, check_shells=False)


def test_hollow_box_with_thin_walls():
    body, _ = g.enclosure(60, 40, 20, wall=0.8)
    assert "thin_walls" in codes(body)


def test_overhang_detected_and_flat_bottom_ignored():
    pillar = box((4, 4, 10), (0, 0, 5))
    top = box((40, 40, 3), (0, 0, 11.5))
    t = trimesh.util.concatenate([pillar, top])
    w = [x for x in pc.check(t) if x["code"] == "overhangs"]
    assert w and w[0]["detail"]["percent"] > 10
    assert "overhangs" not in codes(box((40, 30, 20)))   # the flat bottom lies on the bed
    assert "overhangs" not in codes(t, check_overhangs=False)
    assert "overhangs" not in codes(t, max_overhang_area_pct=90)


def test_shallow_angle_is_not_an_overhang():
    cone = trimesh.creation.cone(radius=10, height=20, sections=64)   # walls ~27 degrees from vertical
    assert "overhangs" not in codes(cone)


def test_build_volume_allows_swapped_xy():
    assert "exceeds_build_volume" not in codes(box((300, 100, 10)), build_x_mm=100, build_y_mm=300)
    assert "exceeds_build_volume" in codes(box((300, 100, 10)))
    assert "exceeds_build_volume" in codes(box((10, 10, 300)))
    assert "exceeds_build_volume" not in codes(box((300, 100, 10)), check_build_volume=False)


def test_disabled_returns_nothing():
    assert pc.check(box((300, 100, 0.5)), {"enabled": False}) == []


def test_check_never_raises():
    class Broken(trimesh.Trimesh):
        @property
        def is_watertight(self):
            raise RuntimeError("boom")
    b = box((5, 5, 5))
    w = pc.check(Broken(b.vertices, b.faces))
    assert [x["code"] for x in w] == ["check_failed"]


def test_warning_shape():
    for w in pc.check(box((300, 100, 0.5))):
        assert {"code", "severity", "message"} <= set(w) and w["severity"] in ("critical", "warning")


# ---- settings

def test_defaults_match_schema():
    assert set(pc.DEFAULTS) == set(pc.SCHEMA)
    assert pc.Settings().get() == pc.DEFAULTS


def test_settings_persist_only_overrides(tmp_path):
    f = tmp_path / "settings.json"
    s = pc.Settings(f)
    out = s.update({"min_wall_mm": 0.8, "check_overhangs": False, "build_x_mm": 220})
    assert out["min_wall_mm"] == 0.8 and out["check_overhangs"] is False
    assert json.loads(f.read_text()) == {"min_wall_mm": 0.8, "check_overhangs": False}
    again = pc.Settings(f).get()
    assert again["min_wall_mm"] == 0.8 and again["build_y_mm"] == 220


def test_settings_reset(tmp_path):
    s = pc.Settings(tmp_path / "s.json")
    s.update({"min_wall_mm": 0.8, "build_z_mm": 100})
    assert s.reset(["min_wall_mm"])["min_wall_mm"] == pc.DEFAULTS["min_wall_mm"]
    assert s.get()["build_z_mm"] == 100
    assert s.reset() == pc.DEFAULTS


@pytest.mark.parametrize("bad", [
    {"nope": 1}, {"min_wall_mm": "1"}, {"min_wall_mm": True}, {"min_wall_mm": -1},
    {"min_wall_mm": float("nan")}, {"enabled": 1}, {"max_overhang_angle_deg": 90}, "x"])
def test_settings_reject_bad_values(tmp_path, bad):
    s = pc.Settings(tmp_path / "s.json")
    with pytest.raises(ValueError):
        s.update(bad)
    assert s.get() == pc.DEFAULTS


def test_reset_unknown_key(tmp_path):
    with pytest.raises(ValueError, match="unknown"):
        pc.Settings(tmp_path / "s.json").reset(["nope"])


def test_broken_settings_file_falls_back_to_defaults(tmp_path):
    f = tmp_path / "s.json"
    f.write_text("{not json")
    assert pc.Settings(f).get() == pc.DEFAULTS
    f.write_text('{"min_wall_mm": -5}')
    assert pc.Settings(f).get() == pc.DEFAULTS


def test_describe_has_everything_for_the_ui():
    d = pc.Settings().describe()
    assert set(d) == {"values", "defaults", "schema"}
    assert all("label" in v and "group" in v for v in d["schema"].values())


# ---- store integration

PLATE = {"root": {"shape": "box", "size": [30, 30, 0.6]}}
SOLID = {"root": {"shape": "box", "size": [30, 30, 10]}}


def test_build_stores_warnings_per_part(tmp_path, db):
    st = Store(tmp_path, db)
    m = st.create("plate", "csg", PLATE)
    warnings = Store.current(m)["parts"]["model"]["warnings"]
    assert "thin_walls" in {w["code"] for w in warnings}
    m2 = st.create("solid", "csg", SOLID)
    assert Store.current(m2)["parts"]["model"]["warnings"] == []


def test_build_uses_current_settings(tmp_path, db):
    st = Store(tmp_path, db)
    st.settings.update({"min_wall_mm": 0.3})
    m = st.create("plate", "csg", PLATE)
    assert Store.current(m)["parts"]["model"]["warnings"] == []


def test_warnings_never_block_a_build(tmp_path, db):
    m = Store(tmp_path, db).create("huge", "csg", {"root": {"shape": "box", "size": [400, 400, 400]}})
    codes_ = {w["code"] for w in Store.current(m)["parts"]["model"]["warnings"]}
    assert "exceeds_build_volume" in codes_


def test_recheck_applies_new_settings_without_new_version(tmp_path, db):
    st = Store(tmp_path, db)
    st.create("plate", "csg", PLATE)
    st.settings.update({"min_wall_mm": 0.3})
    rev = st.revision
    assert st.recheck() == 1
    m = st.get("plate")
    assert len(m["versions"]) == 1
    assert Store.current(m)["parts"]["model"]["warnings"] == []
    assert st.revision > rev


def test_recheck_single_model_and_unknown(tmp_path, db):
    st = Store(tmp_path, db)
    st.create("a", "csg", SOLID)
    st.create("b", "csg", SOLID)
    assert st.recheck("a") == 1
    with pytest.raises(KeyError):
        st.recheck("missing")
