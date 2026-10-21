import asyncio

import numpy as np
import pytest
import trimesh
from starlette.testclient import TestClient

import geometry as g
import measure
import server

run = asyncio.run


def plate_with_features():
    """40 x 30 x 5 plate: four d3.4 through holes 30 x 20 apart, a d6 blind hole 3 deep, a hex
    pocket 3 deep and a d6 post 8 high on top."""
    plate = g.blk(40, 30, 5, 0, 0, 0)
    holes = [g.cyl(1.7, 20, x, y, -5) for x, y in [(5, 5), (35, 5), (5, 25), (35, 25)]]
    hexp = trimesh.creation.cylinder(radius=3.2, height=3, sections=6)
    hexp.apply_translation([28, 22, 3.5])
    m = g.diff(plate, holes + [g.cyl(3, 3, 20, 15, 2.0001), hexp])
    return g.union([m, g.cyl(3, 8, 20, 4, 5)])


def test_holes_bosses_depths_and_through():
    feats = measure.features(plate_with_features())
    holes = [f for f in feats if f["kind"] == "hole"]
    bosses = [f for f in feats if f["kind"] == "boss"]
    through = [f for f in holes if f["through"]]
    assert len(through) == 4
    assert all(f["diameter"] == 3.4 and f["depth"] == 5 and f["axis"] == [0, 0, 1] for f in through)
    assert sorted(tuple(f["center"][:2]) for f in through) == [(5, 5), (5, 25), (35, 5), (35, 25)]
    blind = [f for f in holes if not f["through"] and "sides" not in f]
    assert len(blind) == 1 and blind[0]["diameter"] == 6 and blind[0]["depth"] == 3
    assert blind[0]["center"][:2] == [20, 15]
    hexes = [f for f in holes if f.get("sides") == 6]
    assert len(hexes) == 1 and hexes[0]["diameter"] == 6.4
    assert hexes[0]["across_flats"] == pytest.approx(6.4 * np.cos(np.pi / 6), abs=1e-3)
    assert len(bosses) == 1 and bosses[0]["diameter"] == 6 and bosses[0]["depth"] == 8


def test_summary_counts_equal_holes_with_their_spacing():
    s = measure.summary(measure.features(plate_with_features()))
    four = [x for x in s if x["count"] == 4]
    assert len(four) == 1
    assert four[0]["diameter"] == 3.4 and four[0]["through"] is True
    assert four[0]["pattern_mm"] == {"x": 30, "y": 20}
    assert sum(x["count"] for x in s) == 7


def test_sideways_tube_has_an_x_axis():
    tube = g.diff(g.cyl(5, 10, 0, 0, 0), [g.cyl(3, 12, 0, 0, -1)])
    tube.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [0, 1, 0]))
    feats = {f["kind"]: f for f in measure.features(tube)}
    assert feats["hole"]["diameter"] == 6 and feats["hole"]["through"] and feats["hole"]["axis"] == [1, 0, 0]
    assert feats["boss"]["diameter"] == 10 and feats["boss"]["depth"] == 10


def test_no_round_features_on_a_box_or_a_sphere():
    assert measure.features(trimesh.creation.box(extents=[10, 20, 30])) == []
    assert measure.features(trimesh.creation.icosphere(subdivisions=3, radius=5)) == []


def test_square_hole_is_not_round():
    m = g.diff(g.blk(20, 20, 4, 0, 0, 0), [g.blk(6, 6, 10, 7, 7, -3)])
    assert measure.features(m) == []


def test_measure_reports_size_and_skips_huge_meshes(monkeypatch):
    r = measure.measure(plate_with_features())
    assert r["extents_mm"] == [40, 30, 13] and r["bounds_mm"][0] == [0, 0, 0]
    assert r["volume_cm3"] > 0 and r["features"] and r["summary"]
    monkeypatch.setattr(measure, "MAX_FACES", 10)
    r = measure.measure(plate_with_features())
    assert r["features"] == [] and "note" in r


def test_describe():
    assert measure.describe({"kind": "hole", "diameter": 3.4, "depth": 5, "through": True}) == "Ø3.4 hole, through (5 long)"
    assert measure.describe({"kind": "boss", "diameter": 6.0, "depth": 8}) == "Ø6 boss, 8 high"
    assert measure.describe({"kind": "hole", "diameter": 6.4, "depth": 3, "through": False, "sides": 6,
                             "across_flats": 5.543}) == "6-sided hole Ø6.4 (5.543 across flats), 3 deep"
    assert measure.describe({"kind": "hole", "diameter": 2, "depth": None, "through": False}) == "Ø2 hole"


def test_loops_skip_open_chains():
    loops = measure._loops(np.array([[0, 1], [1, 2], [2, 0], [5, 6], [6, 7]]))
    assert len(loops) == 1 and sorted(loops[0].tolist()) == [0, 1, 2]


# ---- the MCP tool and the web endpoint

HOLED = {"vars": {"d": 3.4}, "root": {"op": "difference", "children": [
    {"shape": "box", "size": [30, 20, 4]},
    {"shape": "cylinder", "d": "=d", "h": 10, "translate": [8, 10, -3]},
    {"shape": "cylinder", "d": "=d", "h": 10, "translate": [22, 10, -3]}]}}


def test_measure_model_tool(out_dir):
    s = run(server.create_model("Holed", HOLED))
    r = run(server.measure_model(model_id=s["model_id"]))
    assert r["version"] == 1
    part = r["parts"]["model"]
    assert part["extents_mm"] == [30, 20, 4]
    holes = [x for x in part["summary"] if x["kind"] == "hole"]
    assert len(holes) == 1 and holes[0]["count"] == 2 and holes[0]["through"]
    assert holes[0]["diameter"] == pytest.approx(3.4, abs=0.01) and holes[0]["pattern_mm"]["x"] == 14
    with pytest.raises(ValueError):
        run(server.measure_model())
    with pytest.raises(ValueError):
        run(server.measure_model(model_id=s["model_id"], part="nope"))
    run(server.update_model(s["model_id"], {"d": 5}))
    assert run(server.measure_model(model_id=s["model_id"]))["parts"]["model"]["features"][0]["diameter"] == pytest.approx(5, abs=0.01)
    old = run(server.measure_model(model_id=s["model_id"], version=1))
    assert old["version"] == 1 and old["parts"]["model"]["features"][0]["diameter"] == pytest.approx(3.4, abs=0.01)


def test_measure_model_on_a_file(out_dir):
    lib = server.Lib(server._admin())
    (lib.dir / "plate.stl").write_bytes(plate_with_features().export(file_type="stl"))
    r = run(server.measure_model(file="plate.stl"))
    assert r["file"] == "plate.stl" and len(r["parts"]["model"]["features"]) == 7


def test_measure_endpoint(out_dir):
    c = TestClient(server.app)
    s = run(server.create_model("Holed", HOLED))
    r = c.get(f"/api/models/{s['model_id']}/measure")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1 and len(body["parts"]["model"]["features"]) == 2
    assert c.get(f"/api/models/{s['model_id']}/measure?version=7").status_code >= 400
    assert c.get("/api/models/missing/measure").status_code == 404


def test_measure_endpoint_is_private(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    st = server.oauth.state
    ann, bob = st.create_user("Ann", "ann@example.com"), st.create_user("Bob", "bob@example.com")

    def client_for(user):
        c = TestClient(server.app, follow_redirects=False)
        c.cookies.set(server.SESSION_COOKIE, st.create_session(user["id"]))
        return c
    mid = client_for(ann).post("/api/models", json={"name": "Anns"}).json()["id"]
    assert client_for(ann).get(f"/api/models/{mid}/measure").status_code == 200
    assert client_for(bob).get(f"/api/models/{mid}/measure").status_code == 404


# ---- the browser side: web/measure-core.js (in node)

@pytest.fixture(scope="module")
def js():
    import json
    import shutil
    import subprocess
    from pathlib import Path
    if shutil.which("node") is None:
        pytest.skip("node is needed to run web/measure-core.js")
    out = subprocess.run(["node", str(Path(__file__).parent / "measure_core.mjs")],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_js_numbers_and_distances(js):
    assert js["mm"] == ["12.5", "3.4", "0.05", "40", "0", "0.33"]
    assert js["distance"] == {"value": 50, "text": "50 mm", "detail": "dx 30 · dy 40 · dz 0"}
    assert js["straight"]["text"] == "7.25 mm" and js["straight"]["detail"] == ""
    assert js["angle"] == [pytest.approx(90), pytest.approx(180), 0]


def test_js_faces(js):
    assert js["gap"]["parallel"] is True and js["gap"]["value"] == pytest.approx(4.5)
    assert js["corner"]["parallel"] is False and js["corner"]["text"] == "90°"


def test_js_labels_match_the_server_wording(js):
    assert js["labels"] == ["Ø3.4 thru", "Ø6 ↧3", "Ø2", "hex 5.54 AF ↧3", "Ø6 ↥8"]
    assert js["summary"] == ["4× Ø3.4 holes, through, 48 × 28 mm apart", "Ø8 hole, 3 deep",
                             "2× Ø6 bosses, 20 mm apart"]


def test_js_box_dimensions(js):
    dims = {d["axis"]: d for d in js["dims"]}
    assert {a: d["value"] for a, d in dims.items()} == {"x": 60, "y": 40, "z": 5}
    assert dims["x"]["a"] == [0, -4, 0] and dims["x"]["b"] == [60, -4, 0]
    assert dims["z"]["a"] == [64, -4, 0] and dims["z"]["b"] == [64, -4, 5]
    assert js["flat"] == ["x", "y"]


def test_js_label_culling_and_text(js):
    assert js["cull"] == [True, False, False, True, True]
    assert js["kept"] == [[True, True], [0, 12]]   # both shown, the second moved below the first
    assert js["text"] == "1. Distance: 50 mm (dx 30 · dy 40 · dz 0)\n2. Thickness: 2 mm"
    assert js["tools"] == {"distance": 2, "faces": 2, "thickness": 1, "angle": 3}


def test_pages_load_the_measuring_module(out_dir):
    c = TestClient(server.app)
    for path in ("/static/measure.js", "/static/measure-core.js"):
        assert c.get(path).status_code == 200
    for page in ("/m/anything", "/edit/anything"):
        html = c.get(page).text
        assert 'id="dims"' in html and 'id="mshow"' in html
    assert 'id="measuremode"' in c.get("/m/anything").text and 'id="mlist"' in c.get("/m/anything").text


def test_measuring_can_be_shown_and_hidden():
    from pathlib import Path
    web = Path(server.__file__).parent / "web"
    js = (web / "measure.js").read_text()
    assert "setShown" in js and "export function showToggle" in js
    icons = (web / "icons.svg").read_text()
    assert 'id="i-eye"' in icons and 'id="i-eye-off"' in icons
