"""Part C of the AI tools: sdf shapes, SVG/DXF outlines and image reliefs."""
import asyncio
import base64
import io
import math

import numpy as np
import pytest

import csg
import server
from server import ELIDED


def run(coro):
    return asyncio.run(coro)


def _model(spec):
    return csg.build(spec)["model"]


# ---- sdf

def test_sdf_primitives_are_accurate_and_closed():
    m = _model({"root": {"shape": "sdf", "sdf": {"sdf": "sphere", "d": 20}}})
    assert m.is_watertight and m.volume == pytest.approx(4 / 3 * math.pi * 1000, rel=0.01)
    m = _model({"root": {"shape": "sdf", "sdf": {"sdf": "cone", "h": 20, "d1": 20, "d2": 6}}})
    assert m.volume == pytest.approx(math.pi * 20 / 3 * (100 + 30 + 9), rel=0.01)
    m = _model({"root": {"shape": "sdf", "sdf": {"sdf": "box", "size": [20, 10, 6], "radius": 1,
                                                  "translate": [10, 5, 3]}}})
    assert m.bounds.round(1).tolist() == [[0, 0, 0], [20, 10, 6]]


def test_sdf_smooth_union_adds_a_fillet():
    kids = [{"sdf": "sphere", "d": 30}, {"sdf": "capsule", "a": [0, 0, 0], "b": [0, 0, 40], "r": 6}]
    plain = _model({"root": {"shape": "sdf", "sdf": {"op": "union", "children": kids}}})
    smooth = _model({"vars": {"k": 5}, "root": {"shape": "sdf", "sdf": {"op": "union", "smooth": "=k",
                                                                         "children": kids}}})
    assert smooth.is_watertight and smooth.volume > plain.volume * 1.01


def test_sdf_shell_and_combining_with_csg():
    shell = _model({"root": {"shape": "sdf", "sdf": {"sdf": "sphere", "d": 30, "shell": 2}}})
    assert shell.volume == pytest.approx(4 / 3 * math.pi * (16 ** 3 - 14 ** 3), rel=0.02)
    moved = _model({"root": {"shape": "sdf", "sdf": {"sdf": "box", "size": [20, 20, 20], "shell": 2,
                                                      "rotate": [0, 0, 45], "translate": [0, 0, 30]}}})
    assert moved.is_watertight and moved.bounds[0][2] == pytest.approx(19, abs=0.3)   # each modifier keeps its own value
    half = _model({"root": {"op": "difference", "children": [
        {"shape": "sdf", "sdf": {"sdf": "sphere", "d": 30}},
        {"shape": "box", "size": [40, 40, 20], "translate": [-20, -20, -20]}]}})
    assert half.is_watertight and half.bounds[0][2] == pytest.approx(0, abs=0.01)


@pytest.mark.parametrize("node, message", [
    ({"sdf": "blob"}, "unknown sdf primitive"),
    ({"op": "intersection", "children": [{"sdf": "sphere", "d": 10, "translate": [50, 0, 0]},
                                         {"sdf": "sphere", "d": 10}]}, "empty"),
    ({"sdf": "box", "size": [10, 10, 10], "radius": 6}, "radius"),
    ({"sdf": "sphere", "d": 10, "shell": -1}, "shell"),
])
def test_sdf_errors(node, message):
    with pytest.raises(ValueError, match=message):
        _model({"root": {"shape": "sdf", "sdf": node}})


def test_sdf_resolution_is_limited():
    with pytest.raises(ValueError, match="too fine"):
        _model({"root": {"shape": "sdf", "resolution": 0.05, "sdf": {"sdf": "sphere", "d": 100}}})


# ---- svg profiles

SQUARE_WITH_HOLE = "M0 0L40 0 40 20 0 20Z M20 5 A5 5 0 1 1 20 15 A5 5 0 1 1 20 5Z"


def test_svg_profile():
    m = _model({"root": {"shape": "extrude", "h": 2, "profile": {"shape": "svg", "path": SQUARE_WITH_HOLE}}})
    assert m.is_watertight and m.volume == pytest.approx((800 - math.pi * 25) * 2, rel=0.002)
    m = _model({"root": {"shape": "extrude", "h": 1, "profile": {"shape": "svg", "path": SQUARE_WITH_HOLE,
                                                                  "size": [80, None], "center": True}}})
    assert m.bounds.round(2).tolist() == [[-40, -20, 0], [40, 20, 1]]
    tri = {"shape": "svg", "path": "M0 0L10 0 10 10Z"}           # the outline starts at (0, 0)
    up = _model({"root": {"shape": "extrude", "h": 1, "profile": tri}})
    down = _model({"root": {"shape": "extrude", "h": 1, "profile": {**tri, "flip_y": True}}})
    assert up.center_mass[1] == pytest.approx(10 / 3) and down.center_mass[1] == pytest.approx(20 / 3)
    with pytest.raises(ValueError, match="invalid SVG path|no closed shape"):
        _model({"root": {"shape": "extrude", "h": 1, "profile": {"shape": "svg", "path": "M0 0 Q"}}})


SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="20mm" viewBox="0 0 40 20">'
       b'<g transform="translate(5,0)"><rect width="30" height="20"/></g><circle cx="20" cy="10" r="5"/>'
       b'<line x1="0" y1="0" x2="5" y2="5" stroke="black"/></svg>')


def _dxf() -> bytes:
    ezdxf = pytest.importorskip("ezdxf")
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 5                          # cm: 5 x 3 cm
    msp = doc.modelspace()
    for a, b in (((0, 0), (5, 0)), ((5, 0), (5, 3)), ((0, 3), (5, 3)), ((0, 3), (0, 0))):
        msp.add_line(a, b)                               # loose lines, one of them reversed
    msp.add_circle((2.5, 1.5), 0.5)
    s = io.StringIO()
    doc.write(s)
    return s.getvalue().encode()


def test_import_outline_svg_and_dxf(out_dir):
    info = run(server.import_outline("logo", data_base64=base64.b64encode(SVG).decode(), format="svg"))
    assert info["size_mm"] == [30.0, 20.0] and info["contours"] == 2      # the open line is left out
    assert info["profile"] == {"shape": "svg", "outline": "logo", "size": [30.0, 20.0]}
    s = run(server.create_model("Logo", {"root": {"shape": "extrude", "h": 2, "profile": {
        "shape": "svg", "outline": "logo", "size": [60, None]}}}))
    part = s["parts"]["model"]
    assert part["extents_mm"] == [60.0, 40.0, 2.0] and part["watertight"]
    assert part["volume_cm3"] == pytest.approx((600 - math.pi * 25) * 8 / 1000, rel=0.01)
    info = run(server.import_outline("plate", data_base64=base64.b64encode(_dxf()).decode(), format="dxf"))
    assert info["size_mm"] == [50.0, 30.0] and info["contours"] == 2


def test_outline_is_kept_in_the_recipe_and_shortened_for_the_ai(out_dir):
    path = "M0 0L" + " ".join(f"{40 + 10 * math.cos(a):.2f} {10 * math.sin(a):.2f}"
                              for a in np.linspace(0, 2 * math.pi, 400)[:-1]) + "Z"
    f = server._outline_file(server.Lib(server._admin()), "ring")
    f.parent.mkdir(exist_ok=True)
    f.write_text(server.json.dumps({"path": path}))
    spec = {"root": {"shape": "extrude", "h": 2, "profile": {"shape": "svg", "outline": "ring"}}}
    s = run(server.create_model("Ring", spec))
    m = server.get_model(s["model_id"])
    shown = m["params"]["root"]["profile"]["path"]
    assert shown.startswith(ELIDED) and len(shown) < 100
    assert server.store.get(s["model_id"])["versions"][-1]["params"]["root"]["profile"]["path"] == path
    f.unlink()                                       # the recipe does not need the file any more
    m["params"]["root"]["h"] = 5                    # the AI sends back what get_model showed it
    s = run(server.update_model(s["model_id"], params=m["params"]))
    assert s["parts"]["model"]["extents_mm"][2] == 5.0
    with pytest.raises(ValueError, match="no outline 'ring'"):      # nothing to fill it from
        run(server.create_model("Copy", m["params"]))
    m["params"]["root"]["profile"] = {"shape": "circle", "d": 10, "note": shown}
    with pytest.raises(ValueError, match="only be sent back in the same place"):
        run(server.create_model("Copy", m["params"]))
    with pytest.raises(ValueError, match="no outline 'nope'"):
        run(server.create_model("X", {"root": {"shape": "extrude", "h": 1, "profile": {
            "shape": "svg", "outline": "nope"}}}))


def test_import_outline_rejects_junk(out_dir):
    with pytest.raises(ValueError, match="could not read the SVG|no closed shapes"):
        run(server.import_outline("x", data_base64=base64.b64encode(b"<svg").decode(), format="svg"))
    with pytest.raises(ValueError, match="format must be svg or dxf"):
        run(server.import_outline("x", data_base64="AA==", format="png"))


# ---- reliefs

def _png(w=120, h=80) -> str:
    from PIL import Image
    ramp = np.tile(np.linspace(0, 200, w), (h, 1))           # dark to light, with some noise
    img = Image.fromarray((ramp + np.random.default_rng(1).uniform(0, 55, (h, w))).astype(np.uint8))
    b = io.BytesIO()
    img.save(b, "PNG")
    return base64.b64encode(b.getvalue()).decode()


def test_lithophane(out_dir):
    s = run(server.create_relief("Photo", data_base64=_png(), pixels=120))
    part = s["parts"]["model"]
    assert s["kind"] == "relief" and part["watertight"]
    x, y, z = part["extents_mm"]
    assert 100 < x < 108 and z == 3.0               # 100 mm picture + 3 mm border, max thickness
    m = server.get_model(s["model_id"])
    assert m["params"]["image"].startswith(ELIDED) and m["params"]["image_size"] == [120, 80]
    s = run(server.update_model(s["model_id"], {"max_thickness": 4, "border": 0}))
    assert s["parts"]["model"]["extents_mm"] == [100.0, pytest.approx(66.39, abs=0.01), 4.0]
    m = server.get_model(s["model_id"])
    m["params"]["width"] = 50
    s = run(server.update_model(s["model_id"], params=m["params"]))
    assert s["parts"]["model"]["extents_mm"][0] == 50.0


def test_relief_mode_and_bad_pictures(out_dir):
    s = run(server.create_relief("Sign", data_base64=_png(), mode="relief", width=40,
                                 options={"base": 1, "height": 3}))
    assert s["parts"]["model"]["extents_mm"][0] == 40.0
    assert s["parts"]["model"]["extents_mm"][2] == pytest.approx(4.0, abs=0.05)
    with pytest.raises(ValueError, match="could not read the picture"):
        run(server.create_relief("Bad", data_base64=base64.b64encode(b"not an image").decode()))
    with pytest.raises(ValueError, match="unknown options"):
        run(server.create_relief("Bad", data_base64=_png(), options={"depth": 2}))
