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


@pytest.fixture(scope="module")
def js():
    out = subprocess.run(["node", str(HERE / "spec_roundtrip.mjs")], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@pytest.fixture
def client(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "")
    return TestClient(server.app, follow_redirects=False)


def vol(spec):
    return sum(m.volume for m in csg.build(spec).values())


def test_a_solid_builds_to_its_volume(js):
    assert vol(js["solid"]) == pytest.approx(20 * 20 * 10, rel=1e-6)


def test_a_hole_is_cut_out_of_the_solids(js):
    # 20x20x10 box + a sphere apart from it, minus a d8 bore through the box (8 mm of it is inside)
    import math
    bore = math.pi * 4 ** 2 * 10
    sphere = 4 / 3 * math.pi * 5 ** 3
    assert vol(js["holed"]) == pytest.approx(4000 - bore + sphere, rel=0.05)


def test_the_hole_is_kept_when_read_back(js):
    shapes = [(o["shape"], o["hole"]) for o in js["back"]]
    assert shapes == [("box", False), ("sphere", False), ("cylinder", True)]
    assert js["back"][2]["name"] == "bore" and js["back"][1]["rot"] == [0, 0, 45]


def test_a_recipe_without_solids_is_refused(js):
    assert "at least one solid" in js["noSolid"]


def test_hand_written_corner_anchored_shapes_become_centred(js):
    box, cyl = js["claude"]
    assert box["pos"] == [10, 15, 15] and box["size"] == [10, 20, 30]
    assert cyl["pos"] == [0, 0, 36] and cyl["size"] == [6, 6, 12]


def test_recipes_the_editor_cannot_show_stay_untouched(js):
    assert js["expr"] == ["locked"] and js["template"] == ["locked"]
    assert js["parts"] == "parts"
    assert js["lockedKept"] == {"vars": {"w": 5}, "root": {"op": "hull", "children": [{"shape": "sphere", "d": 4}]}}


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


def csg_ok():
    return {"vars": {}, "root": {"shape": "box", "size": [1, 1, 1]}}


def test_unauthenticated_cannot_create(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    r = TestClient(server.app, follow_redirects=False).post("/api/models", json={"name": "x"})
    assert r.status_code in (401, 302, 303)
