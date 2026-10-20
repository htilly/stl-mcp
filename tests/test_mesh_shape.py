"""The csg "mesh" shape: an attached STL as one node of a recipe, so an uploaded STL can be moved,
rotated, scaled and combined with shapes - by Claude and in the web editor."""
import asyncio
import base64
import hashlib

import pytest
import trimesh
from starlette.testclient import TestClient

import buildworker
import csg
import server
from store import Store


def run(coro):
    return asyncio.run(coro)


def ctx(user):
    from types import SimpleNamespace
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(state=SimpleNamespace(user=user))))


def stored(mesh_dir, mesh) -> str:
    data = mesh.export(file_type="stl")
    sha = hashlib.sha256(data).hexdigest()
    (mesh_dir / f"{sha}.stl").write_bytes(data)
    return sha


def test_a_mesh_node_builds_moves_and_cuts(tmp_path):
    sha = stored(tmp_path, trimesh.creation.box((20, 20, 10)))
    spec = {"root": {"op": "difference", "children": [
        {"shape": "mesh", "mesh": sha, "translate": [10, 10, 5], "scale": [2, 1, 1]},
        {"shape": "cylinder", "d": 4, "h": 30, "translate": [10, 10, -5]}]}}
    out = buildworker.build("csg", spec, mesh_dir=tmp_path)["model"]
    assert out.is_watertight
    assert out.extents.round(3).tolist() == [40, 20, 10]
    assert out.volume == pytest.approx(40 * 20 * 10 - 3.14159 * 4 * 10, rel=0.01)
    assert buildworker.mesh_node({"shape": "mesh", "mesh": sha}, {}, mesh_dir=tmp_path).extents.tolist() == [20, 20, 10]


@pytest.mark.parametrize("ref", ["", "../../etc/passwd", "0" * 63, "A" * 64])
def test_a_mesh_must_be_an_attached_sha(tmp_path, ref):
    with pytest.raises(ValueError, match="attached mesh"):
        buildworker.build("csg", {"root": {"shape": "mesh", "mesh": ref}}, mesh_dir=tmp_path)


def test_a_missing_or_open_mesh_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(csg, "MESH_DIR", tmp_path)
    with pytest.raises(ValueError, match="not available"):
        csg.build({"root": {"shape": "mesh", "mesh": "0" * 64}})
    box = trimesh.creation.box((5, 5, 5))
    sha = stored(tmp_path, trimesh.Trimesh(box.vertices, box.faces[:-2]))   # one side missing: filled
    assert csg.build({"root": {"shape": "mesh", "mesh": sha}})["model"].is_watertight
    tube = trimesh.creation.cylinder(radius=5, height=10, sections=32)
    side = abs(tube.face_normals[:, 2]) < 0.5                               # no ends: too open to fill
    sha = stored(tmp_path, trimesh.Trimesh(tube.vertices, tube.faces[side]))
    with pytest.raises(ValueError, match="not watertight"):
        csg.build({"root": {"shape": "mesh", "mesh": sha}})


def test_create_model_attaches_mesh_files_by_name(out_dir):
    joe = server.oauth.state.create_user("Joe")
    stl = base64.b64encode(trimesh.creation.box((10, 20, 30)).export(file_type="stl")).decode()
    run(server.import_file("scan", data_base64=stl, ctx=ctx(joe)))
    s = run(server.create_model("Pegged", {"root": {"op": "union", "children": [
        {"shape": "mesh", "mesh": "scan.stl", "translate": [5, 10, 15]},
        {"shape": "cylinder", "d": 3, "h": 10, "translate": [5, 10, 0]}]}}, ctx=ctx(joe)))
    assert s["parts"]["model"]["extents_mm"] == [10.0, 20.0, 30.0]
    sha = server.get_model(s["model_id"], ctx=ctx(joe))["params"]["root"]["children"][0]["mesh"]
    assert csg.MESH_SHA.fullmatch(sha) and (server.store.mesh_dir / f"{sha}.stl").is_file()
    # the recipe keeps its own copy, and a node added later by file name is attached too
    (server.Lib(joe).dir / "scan.stl").unlink()
    run(server.import_file("other", data_base64=stl, ctx=ctx(joe)))
    s = run(server.update_model(s["model_id"], {"/root/children/-": {
        "shape": "mesh", "mesh": "other.stl", "translate": [40, 10, 15]}}, ctx=ctx(joe)))
    assert s["parts"]["model"]["extents_mm"] == [45.0, 20.0, 30.0]


def test_mesh_files_only_from_the_users_own_files(out_dir):
    joe, ann = server.oauth.state.create_user("Joe"), server.oauth.state.create_user("Ann")
    stl = base64.b64encode(trimesh.creation.box().export(file_type="stl")).decode()
    run(server.import_file("secret", data_base64=stl, ctx=ctx(ann)))
    with pytest.raises(FileNotFoundError):
        run(server.create_model("x", {"root": {"shape": "mesh", "mesh": "secret.stl"}}, ctx=ctx(joe)))


def test_the_editor_saves_an_uploaded_stl_as_a_recipe(out_dir, monkeypatch):
    """An uploaded STL (an OpenSCAD import of one mesh) saved from the editor becomes a csg recipe
    with the mesh as a node; the web UI cannot switch to any other kind."""
    monkeypatch.setattr(server, "TOKEN", "")
    client = TestClient(server.app, follow_redirects=False)
    mesh = trimesh.creation.box((10, 10, 10))
    sha = stored(server.store.mesh_dir, mesh)
    monkeypatch.setattr(Store, "build", lambda self, kind, params: (
        buildworker.build(kind, params, mesh_dir=self.mesh_dir) if kind == "csg" else {"model": mesh}))
    m = server.store.create("Scan", "openscad", {"code": 'import("scan.stl");', "meshes": {"scan": sha}})
    assert server._source(Store.current(m)) == "stl"
    spec = {"root": {"op": "union", "children": [
        {"shape": "mesh", "mesh": sha, "name": "scan", "translate": [5, 5, 5]},
        {"shape": "box", "size": [10, 10, 10], "translate": [10, 0, 0]}]}}
    r = client.post(f"/api/models/{m['id']}", json={"action": "update", "params": spec, "kind": "csg"})
    assert r.status_code == 200, r.text
    cur = client.get(f"/api/models/{m['id']}").json()
    v = cur["versions"][-1]
    assert v["kind"] == "csg" and v["parts"]["model"]["extents_mm"] == [20.0, 10.0, 10.0]
    client.post(f"/api/models/{m['id']}", json={"action": "update", "kind": "script",
                                                 "params": {"code": "result = None"}})
    assert client.get(f"/api/models/{m['id']}").json()["versions"][-1]["kind"] == "csg"
