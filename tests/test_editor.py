"""The web editor: its routes, and that the recipes it writes build the geometry it shows."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import csg
import server

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run web/spec.js")
HERE = Path(__file__).parent


EXPRS = ["1+2", "w/3", "w//3", "-7%3", "7%-3", "2**3", "min(1, w, 3)", "max(w, n*20)", "round(2.5)",
         "round(3.5)", "round(2.675, 2)", "sin(30)", "cos(60)*w", "1 if w > 10 else 2", "(w - 2*wall)/2",
         "sqrt(-1)", "foo+1", "a < b < c", "pi*2", "abs(-wall)", "w > 3", "floor(-2.5)", "ceil(2.1)", "2**20",
         "1/0", "w if n == 3 else 0", "__import__('os')", "w.real"]
ENV = {"w": 40, "wall": 2, "n": 3}


@pytest.fixture(scope="module")
def js():
    out = subprocess.run(["node", str(HERE / "spec_roundtrip.mjs"), json.dumps(EXPRS)],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@pytest.fixture
def client(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "")
    return TestClient(server.app, follow_redirects=False)


def vol(spec):
    return sum(m.volume for m in csg.build(spec).values())


def test_expressions_match_the_server(js):
    for expr, got in zip(EXPRS, js["exprs"]):
        try:
            want = csg.eval_expr(expr, ENV)
        except Exception:  # noqa: BLE001
            assert "err" in got, f"{expr}: the editor evaluates what the server refuses"
            continue
        assert "ok" in got, f"{expr}: the editor refuses what the server evaluates ({got})"
        assert got["ok"] == pytest.approx(want), expr


def test_vars_resolve_in_any_order_and_cycles_are_caught(js):
    assert js["vars"] == {"a": 10, "b": 5, "c": 4}
    assert "circular" in js["cycle"]["err"]
    assert js["resolved"] == {"size": [40, 5, 4], "children": [{"d": "=w"}]}


def test_a_root_recipe_is_one_part_and_stays_a_root(js):
    assert js["rootParts"] == ["model"] and js["twoParts"] == ["body", "lid"]
    assert js["rootBack"] == {"vars": {}, "root": {"shape": "box", "size": [1, 1, 1]}}


def test_pointers_find_nodes_and_holes_are_known(js):
    assert js["pointer"] == "/parts/a/children/1/children/1" and js["pointerBack"]
    assert js["isHole"] == [True, False]


def test_making_a_hole_cuts_and_making_it_solid_adds_again(js):
    import math
    bore = math.pi * 4 ** 2 * 10
    assert js["holeErr"] is None and js["solidErr"] is None
    assert vol(js["holed"]) == pytest.approx(4000 - bore, rel=0.02)
    assert js["holed"]["root"]["children"][1]["name"] == "bore"
    assert vol(js["solidAgain"]) == pytest.approx(vol({"vars": {}, "root": {"op": "union", "children": [
        {"shape": "box", "size": [20, 20, 10], "center": True, "translate": [0, 0, 5]},
        {"shape": "cylinder", "d": 8, "h": 30, "center": True, "translate": [0, 0, 5]}]}}), rel=1e-6)


def test_a_hole_in_a_moved_group_stays_where_it_was(js):
    spec = js["movedHoled"]
    assert spec["root"]["op"] == "difference" and spec["root"]["translate"] == [100, 0, 0]
    assert "translate" not in spec["root"]["children"][0]
    bounds = csg.build(spec)["model"].bounds
    assert bounds[0][0] == pytest.approx(90) and bounds[1][0] == pytest.approx(110)
    assert "part" in js["partHoleErr"]


def test_group_ungroup_and_move(js):
    assert js["grouped"]["root"]["children"][0]["op"] == "union"
    assert len(js["grouped"]["root"]["children"]) == 1
    assert js["ungrouped"] == {"vars": {}, "root": {"op": "union", "children": [
        {"shape": "box", "size": [20, 20, 10], "center": True, "translate": [0, 0, 5]},
        {"shape": "cylinder", "d": 8, "h": 30, "center": True, "translate": [0, 0, 5], "name": "bore"}]}}
    moved = js["movedInto"]["root"]["children"]
    assert len(moved) == 2 and moved[1]["children"][0]["shape"] == "box"
    assert "itself" in js["moveIntoSelf"]["err"]


def test_new_shapes_and_holes_find_their_place(js):
    assert js["lone"]["root"]["op"] == "union" and len(js["lone"]["root"]["children"]) == 2
    assert js["cut"]["root"]["op"] == "difference"
    assert vol(js["cut"]) < 125


def test_the_browser_draws_plain_primitives_only(js):
    box, cbox, cyl, cone, sphere, rounded, arrayed, template = js["prims"]
    assert box == {"type": "box", "size": [10, 20, 30], "center": [5, 10, 15]}
    assert cbox["center"] == [0, 0, 0]
    assert cyl == {"type": "cyl", "rBottom": 5, "rTop": 5, "h": 4, "center": [0, 0, 2]}
    assert cone == {"type": "cyl", "rBottom": 3, "rTop": 0, "h": 4, "center": [0, 0, 0]}
    assert sphere == {"type": "sphere", "r": 3}
    assert rounded is None and arrayed is None and template is None


def test_a_resize_becomes_dimensions_when_exact(js):
    assert js["foldBox"] == [True, {"shape": "box", "size": [20, 10, 5]}]
    assert js["foldCyl"] == [True, {"shape": "cylinder", "d": 20, "h": 12}]
    assert js["foldOval"][0] is False and js["foldExpr"][0] is False


def test_typed_values(js):
    assert js["values"] == [12, "=w/2", "M4", [1, 2], True, "hello", None, 5]


def test_editor_page_is_served(client):
    r = client.get("/edit/anything")
    assert r.status_code == 200 and "Export STL" in r.text
    assert client.get("/static/editor.js").status_code == 200
    assert client.get("/static/spec.js").status_code == 200


def test_new_blank_model_builds_and_is_editable(client):
    r = client.post("/api/models", json={"name": "Hand made"})
    assert r.status_code == 200
    mid = r.json()["id"]
    m = client.get(f"/api/models/{mid}").json()
    cur = m["versions"][-1]
    assert cur["kind"] == "csg" and cur["parts"]["model"]["extents_mm"] == [20, 20, 20]
    assert m["name"] == "Hand made"


def test_saving_from_the_editor_makes_a_new_version(client, js):
    mid = client.post("/api/models", json={"name": "Hand made"}).json()["id"]
    r = client.post(f"/api/models/{mid}", json={"action": "update", "params": js["holed"], "note": "web editor"})
    assert r.status_code == 200
    m = client.get(f"/api/models/{mid}").json()
    assert m["current"] == 2 and m["versions"][-1]["params"] == js["holed"]


def test_a_multi_part_recipe_saves_as_parts(client):
    mid = client.post("/api/models", json={"name": "Two"}).json()["id"]
    spec = {"vars": {"w": 10}, "parts": {"body": {"shape": "box", "size": ["=w", 10, 10]},
                                         "lid": {"shape": "cylinder", "d": 8, "h": 2}}}
    assert client.post(f"/api/models/{mid}", json={"action": "update", "params": spec}).status_code == 200
    assert set(client.get(f"/api/models/{mid}").json()["versions"][-1]["parts"]) == {"body", "lid"}


def test_mesh_previews_nodes_the_browser_cannot_draw(client):
    import io
    import trimesh
    mid = client.post("/api/models", json={"name": "Mesh"}).json()["id"]
    for node in ({"shape": "screw_hole", "size": "M3", "h": 10, "head": "countersunk"},
                 {"op": "hull", "children": [{"shape": "sphere", "d": "=w"}, {"shape": "box", "size": [1, 1, 1], "translate": [20, 0, 0]}]}):
        r = client.post(f"/api/models/{mid}/mesh", json={"node": node, "vars": {"w": 6}})
        assert r.status_code == 200, r.text
        mesh = trimesh.load(io.BytesIO(r.content), file_type="stl")
        assert mesh.volume > 0
    assert client.get(f"/api/models/{mid}/mesh").status_code in (404, 405)
    r = client.post(f"/api/models/{mid}/mesh", json={"node": {"shape": "nope"}})
    assert r.status_code == 400 and "unknown shape" in r.json()["error"]
    r = client.post(f"/api/models/{mid}/mesh", json={"node": {"shape": "box", "size": ["=zz", 1, 1]}})
    assert r.status_code == 400
    assert client.post("/api/models/missing/mesh", json={"node": {"shape": "sphere", "d": 1}}).status_code == 404


def test_a_failing_build_saves_nothing(client):
    mid = client.post("/api/models", json={"name": "Hand made"}).json()["id"]
    r = client.post(f"/api/models/{mid}", json={"action": "update", "params": {"vars": {}, "root": {"op": "union", "children": []}}})
    assert r.status_code >= 400
    assert client.get(f"/api/models/{mid}").json()["current"] == 1


def test_users_only_create_models_for_themselves(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    st = server.oauth.state
    ann, bob = st.create_user("Ann", "ann@example.com"), st.create_user("Bob", "bob@example.com")

    def client_for(user):
        c = TestClient(server.app, follow_redirects=False)
        c.cookies.set(server.SESSION_COOKIE, st.create_session(user["id"]))
        return c
    mid = client_for(ann).post("/api/models", json={"name": "Anns"}).json()["id"]
    assert client_for(ann).get(f"/api/models/{mid}").status_code == 200
    assert client_for(bob).get(f"/api/models/{mid}").status_code == 404
    assert client_for(bob).post(f"/api/models/{mid}", json={"action": "update", "params": csg_ok()}).status_code == 404
    assert client_for(bob).get("/api/models").json()["models"] == []
    assert client_for(bob).post(f"/api/models/{mid}/mesh", json={"node": {"shape": "sphere", "d": 1}}).status_code == 404


def csg_ok():
    return {"vars": {}, "root": {"shape": "box", "size": [1, 1, 1]}}


def test_unauthenticated_cannot_create(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    r = TestClient(server.app, follow_redirects=False).post("/api/models", json={"name": "x"})
    assert r.status_code in (401, 302, 303)
