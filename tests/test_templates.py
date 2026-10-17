import math

import numpy as np
import pytest

import csg
import templates as t


def bb(m):
    b = m.bounding_box()
    return np.array(b[:3]), np.array(b[3:])


def shape(name, **kw):
    return csg.build_node({"shape": name, **kw}, {})


# ---------------------------------------------------------------- sizes

def test_sizes_table_is_complete():
    keys = {"clear", "tap", "head_cs", "head_cb", "head_h", "nut_af", "nut_h", "ins_d", "ins_l"}
    for name, s in t.SIZES.items():
        assert set(s) == keys, name


def test_size_is_case_insensitive_and_accepts_comma():
    assert t._size({"shape": "x", "size": "m2,5"}) is t.SIZES["M2.5"]
    assert t._size({"shape": "x"}) is t.SIZES["M3"]


def test_unknown_size():
    with pytest.raises(ValueError, match="unknown size 'M99'"):
        shape("screw_hole", size="M99")


def test_templates_registry_and_help():
    assert set(t.TEMPLATES) == {"screw_hole", "heat_insert", "nut_trap", "boss", "snap_hook",
                                "hinge", "text"}
    for name in t.TEMPLATES:
        assert name in t.HELP


# ---------------------------------------------------------------- screw_hole

def test_screw_hole_goes_down_with_overshoot():
    m = shape("screw_hole", size="M3", h=10)
    lo, hi = bb(m)
    assert lo[2] == pytest.approx(-10) and hi[2] == pytest.approx(0.5)
    assert hi[0] == pytest.approx(3.4 / 2, abs=0.02)
    assert m.volume() == pytest.approx(math.pi * 1.7 ** 2 * 10.5, rel=0.02)


def test_screw_hole_tap_fit_is_smaller():
    clear = shape("screw_hole", size="M3").volume()
    tap = shape("screw_hole", size="M3", fit="tap").volume()
    assert tap < clear


def test_screw_hole_custom_diameter():
    m = shape("screw_hole", size="M3", d=5)
    assert bb(m)[1][0] == pytest.approx(2.5, abs=0.02)


def test_screw_hole_flip_points_up():
    lo, hi = bb(shape("screw_hole", size="M3", h=10, flip=True))
    assert lo[2] == pytest.approx(-0.5) and hi[2] == pytest.approx(10)


def test_screw_hole_countersunk_is_wider_at_top():
    plain = shape("screw_hole", size="M3", h=10)
    cs = shape("screw_hole", size="M3", h=10, head="countersunk")
    assert cs.volume() > plain.volume()
    assert bb(cs)[1][0] == pytest.approx(6.3 / 2, abs=0.05)


def test_screw_hole_counterbore():
    cb = shape("screw_hole", size="M3", h=10, head="counterbore")
    assert bb(cb)[1][0] == pytest.approx(3.0, abs=0.05)
    assert cb.volume() > shape("screw_hole", size="M3", h=10).volume()


def test_screw_hole_nut_pocket_adds_volume_and_width():
    plain = shape("screw_hole", size="M3", h=10)
    nut = shape("screw_hole", size="M3", h=10, nut=True)
    assert nut.volume() > plain.volume()
    assert bb(nut)[0][2] == pytest.approx(-10.5)


def test_screw_hole_invalid_head():
    with pytest.raises(ValueError, match="head must be"):
        shape("screw_hole", head="round")


def test_screw_hole_cuts_a_plate():
    plate = {"shape": "box", "size": [20, 20, 3]}
    hole = {"shape": "screw_hole", "size": "M3", "h": 3, "head": "countersunk", "translate": [10, 10, 3]}
    m = csg.build_node({"op": "difference", "children": [plate, hole]}, {})
    assert m.volume() < 1200
    assert csg.to_trimesh(m).is_watertight


# ---------------------------------------------------------------- heat_insert

def test_heat_insert_dimensions():
    m = shape("heat_insert", size="M3")
    lo, hi = bb(m)
    assert lo[2] == pytest.approx(-(5.7 + 1.0))
    assert hi[2] == pytest.approx(0.5)
    assert hi[0] == pytest.approx(2.5, abs=0.05)   # chamfer / overshoot widened to d + 1


def test_heat_insert_custom_depth_and_flip():
    m = shape("heat_insert", size="M3", h=3, flip=True)
    lo, hi = bb(m)
    assert hi[2] == pytest.approx(3) and lo[2] == pytest.approx(-0.5)


# ---------------------------------------------------------------- nut_trap

def test_nut_trap_depth_and_hex_width():
    m = shape("nut_trap", size="M3", bolt=False)
    lo, hi = bb(m)
    assert lo[2] == pytest.approx(-(2.4 + 0.3)) and hi[2] == pytest.approx(0.5)
    af = 5.5 + 0.3
    ext = sorted([hi[0] - lo[0], hi[1] - lo[1]])
    assert ext[0] == pytest.approx(af, abs=0.02)                    # across flats
    assert ext[1] == pytest.approx(2 * af / math.sqrt(3), abs=0.05)  # across corners


def test_nut_trap_slot_extends_in_plus_x():
    base = bb(shape("nut_trap", size="M3", bolt=False))[1][0]
    slotted = shape("nut_trap", size="M3", bolt=False, slot=10)
    assert bb(slotted)[1][0] == pytest.approx(10, abs=0.01) and base < 10


def test_nut_trap_bolt_hole_default():
    with_bolt = shape("nut_trap", size="M3").volume()
    without = shape("nut_trap", size="M3", bolt=False).volume()
    assert with_bolt >= without


def test_nut_trap_clearance_and_flip():
    tight = shape("nut_trap", size="M3", bolt=False, clearance=0).volume()
    loose = shape("nut_trap", size="M3", bolt=False, clearance=1).volume()
    assert loose > tight
    assert bb(shape("nut_trap", size="M3", flip=True))[1][2] > 0


# ---------------------------------------------------------------- boss

def test_boss_insert_dimensions():
    m = shape("boss", size="M3", h=8)
    lo, hi = bb(m)
    assert lo[2] == pytest.approx(0) and hi[2] == pytest.approx(8)
    od = max(4.0 * 2, 4.0 + 3.2)
    f = min(od * 0.35, 4)
    assert hi[0] == pytest.approx((od + 2 * f) / 2, abs=0.05)   # reinforcing foot sets the width


def test_boss_hole_and_fillet_options_change_volume():
    full = shape("boss", size="M3", h=8, hole=False, fillet=False)
    holed = shape("boss", size="M3", h=8, fillet=False)
    footed = shape("boss", size="M3", h=8, hole=False)
    assert full.volume() == pytest.approx(math.pi * 4 ** 2 * 8, rel=0.02)
    assert holed.volume() < full.volume() < footed.volume()


def test_boss_tap_uses_pilot_hole():
    insert = shape("boss", size="M3", h=8, fillet=False, **{"for": "insert"})
    tap = shape("boss", size="M3", h=8, fillet=False, **{"for": "tap"})
    assert insert.volume() != tap.volume()


def test_boss_invalid_purpose():
    with pytest.raises(ValueError, match="for must be insert or tap"):
        shape("boss", **{"for": "glue"})


def test_boss_is_valid_solid():
    assert csg.to_trimesh(shape("boss", size="M4", h=10)).is_watertight


# ---------------------------------------------------------------- snap_hook

def test_snap_hook_extents():
    m = shape("snap_hook", length=10, width=5, thickness=1.5, overhang=1.0, base=False)
    lo, hi = bb(m)
    assert np.allclose(lo, [0, 0, 0], atol=1e-6)
    assert hi[0] == pytest.approx(1.5 + 1.0)
    assert hi[1] == pytest.approx(5)
    assert hi[2] == pytest.approx(10)


def test_snap_hook_base_adds_root_reinforcement():
    no_base = shape("snap_hook", base=False).volume()
    with_base = shape("snap_hook").volume()
    assert with_base > no_base


def test_snap_hook_width_scales_volume():
    narrow = shape("snap_hook", width=2).volume()
    wide = shape("snap_hook", width=4).volume()
    assert wide == pytest.approx(2 * narrow, rel=0.01)


def test_snap_hook_hook_height_validation():
    with pytest.raises(ValueError, match="hook_h must be smaller"):
        shape("snap_hook", length=3, hook_h=3)


# ---------------------------------------------------------------- hinge

def test_hinge_print_in_place_extents():
    m = shape("hinge", length=40, d=6, knuckles=5, gap=0.4, leaf=15)
    lo, hi = bb(m)
    assert lo[0] == pytest.approx(0, abs=1e-6) and hi[0] == pytest.approx(40, abs=1e-6)
    assert lo[1] == pytest.approx(-(3 + 0.4 + 15)) and hi[1] == pytest.approx(3 + 0.4 + 15)
    assert lo[2] == pytest.approx(0, abs=1e-6) and hi[2] == pytest.approx(6, abs=0.05)


def test_hinge_sides_are_on_opposite_sides():
    a = bb(shape("hinge", side="a"))
    b = bb(shape("hinge", side="b"))
    assert a[0][1] < 0 and b[1][1] > 0
    assert a[1][1] < b[1][1] + 15


def test_hinge_pin_mode_has_axle_hole():
    pin = shape("hinge", mode="pin", side="a")
    print_a = shape("hinge", mode="print", side="a")
    assert pin.volume() < print_a.volume()


def test_hinge_both_is_union_of_sides():
    both = shape("hinge").volume()
    a = shape("hinge", side="a").volume()
    b = shape("hinge", side="b").volume()
    assert both == pytest.approx(a + b, rel=0.05)


def test_hinge_validation():
    with pytest.raises(ValueError, match="at least 2 knuckles"):
        shape("hinge", knuckles=1)
    with pytest.raises(ValueError, match="too short"):
        shape("hinge", length=2, knuckles=5)
    with pytest.raises(ValueError, match="mode must be"):
        shape("hinge", mode="glue")
    with pytest.raises(ValueError, match="side must be"):
        shape("hinge", side="c")


# ---------------------------------------------------------------- text

def test_text_extrudes_to_height():
    m = shape("text", text="Hi", size=10, h=2)
    lo, hi = bb(m)
    assert lo[2] == pytest.approx(0) and hi[2] == pytest.approx(2)
    assert m.volume() > 0
    assert hi[1] <= 10.5


def test_text_longer_string_is_wider():
    short = bb(shape("text", text="A", size=10))[1][0]
    long = bb(shape("text", text="AAAA", size=10))[1][0]
    assert long > 3 * short * 0.9


def test_text_size_scales_linearly():
    small = bb(shape("text", text="Hi", size=5))
    big = bb(shape("text", text="Hi", size=10))
    assert (big[1][0] - big[0][0]) == pytest.approx(2 * (small[1][0] - small[0][0]), rel=0.05)


def test_text_spacing_widens():
    tight = bb(shape("text", text="AA", size=10))[1][0]
    loose = bb(shape("text", text="AA", size=10, spacing=5))[1][0]
    assert loose == pytest.approx(tight + 5, abs=0.05)


def test_text_halign():
    left = bb(shape("text", text="AAAA", size=10))
    center = bb(shape("text", text="AAAA", size=10, halign="center"))
    right = bb(shape("text", text="AAAA", size=10, halign="right"))
    assert left[0][0] >= -0.01
    assert center[0][0] < 0 < center[1][0]
    assert right[1][0] <= 0.5


def test_text_valign():
    bottom = bb(shape("text", text="Hy", size=10, valign="bottom"))
    top = bb(shape("text", text="Hy", size=10, valign="top"))
    center = bb(shape("text", text="Hy", size=10, valign="center"))
    assert bottom[0][1] == pytest.approx(0, abs=1e-6)
    assert top[1][1] == pytest.approx(0, abs=1e-6)
    assert center[0][1] < 0 < center[1][1]


def test_text_multiline_is_taller():
    one = bb(shape("text", text="A", size=10))
    two = bb(shape("text", text="A\nA", size=10))
    assert (two[1][1] - two[0][1]) > (one[1][1] - one[0][1]) + 10


def test_text_bold_font_has_more_volume():
    sans = shape("text", text="HHH", size=10).volume()
    bold = shape("text", text="HHH", size=10, font="bold").volume()
    assert bold > sans


def test_text_unknown_glyph_uses_notdef():
    assert not shape("text", text="中", size=10).is_empty()


def test_text_validation():
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="missing"):
            shape("text", text=bad)
    with pytest.raises(ValueError, match="max 200"):
        shape("text", text="a" * 201)
    with pytest.raises(ValueError, match="unknown font"):
        shape("text", text="a", font="comic")
    with pytest.raises(ValueError, match="halign"):
        shape("text", text="a", halign="middle")
    with pytest.raises(ValueError, match="valign"):
        shape("text", text="a", valign="middle")


def test_text_with_only_spaces_on_line_has_no_visible_characters():
    with pytest.raises(ValueError):
        shape("text", text=" \n ")


def test_text_engraves_into_plate():
    plate = {"shape": "box", "size": [40, 20, 3]}
    label = {"shape": "text", "text": "Hi", "size": 8, "h": 2, "translate": [3, 5, 2]}
    m = csg.build_node({"op": "difference", "children": [plate, label]}, {})
    assert m.volume() < 2400
