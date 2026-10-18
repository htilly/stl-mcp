"""Two users on one server: neither can see, change or delete the other's models, files,
previews, comments or live updates - over the web API and over MCP. Someone else's model must
look exactly like a model that does not exist (404 / KeyError, never 403)."""
import asyncio
import json
from types import SimpleNamespace

import pytest
import trimesh
from starlette.testclient import TestClient

import server

BOX = {"root": {"shape": "box", "size": [20, 20, 10]}}


def ctx(user):
    """A stand-in for the MCP Context of a request made by user."""
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(state=SimpleNamespace(user=user))))


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def world(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    st = server.oauth.state
    ann, bob = st.create_user("Ann", "ann@example.com"), st.create_user("Bob", "bob@example.com")
    run(server.create_model("anns-box", BOX, ctx=ctx(ann)))
    server.store.add_comment("anns-box", "make it taller")
    (server.Lib(ann).dir / "part.stl").write_bytes(
        trimesh.creation.box(extents=(5, 5, 5)).export(file_type="stl"))

    def client_for(user):
        c = TestClient(server.app, follow_redirects=False)
        c.cookies.set(server.SESSION_COOKIE, st.create_session(user["id"]))
        return c
    return SimpleNamespace(ann=ann, bob=bob, client_for=client_for)


def test_owner_reaches_everything(world):
    c = world.client_for(world.ann)
    assert [m["id"] for m in c.get("/api/models").json()["models"]] == ["anns-box"]
    assert c.get("/api/models/anns-box").status_code == 200
    assert c.get("/api/models/anns-box/preview.png").status_code == 200
    assert c.get("/files/anns-box/v1/model.stl").status_code == 200
    assert c.get(f"/f/{world.ann['id']}/part.stl").status_code == 200
    a = ctx(world.ann)
    assert server.get_model("anns-box", ctx=a)["comments"][0]["text"] == "make it taller"
    assert [f["file"] for f in server.list_files(ctx=a)] == ["part.stl"]
    assert server.file_info("part.stl", ctx=a)["watertight"]


def test_other_user_gets_404_on_every_web_route(world):
    c = world.client_for(world.bob)
    assert c.get("/api/models").json()["models"] == []
    assert c.get("/api/models/anns-box").status_code == 404
    assert c.get("/api/models/anns-box/preview.png").status_code == 404
    assert c.get("/files/anns-box/v1/model.stl").status_code == 404
    assert c.get(f"/f/{world.ann['id']}/part.stl").status_code == 404
    for body in ({"action": "set_current", "version": 1}, {"action": "revert", "version": 1},
                 {"action": "rename", "name": "mine now"}, {"action": "update", "changes": {}},
                 {"action": "comment_add", "text": "hi"}, {"action": "comment_resolve", "id": "c1"},
                 {"action": "comment_delete", "id": "c1"}, {"action": "duplicate", "name": "copy"}):
        assert c.post("/api/models/anns-box", json=body).status_code == 404, body
    assert c.delete("/api/models/anns-box").status_code == 404
    # nothing changed for Ann
    m = server.store.get("anns-box")
    assert m["name"] == "anns-box" and len(m["versions"]) == 1 and len(m["comments"]) == 1
    assert [x["id"] for x in server.store.list()] == ["anns-box"]


def test_other_user_gets_not_found_on_every_mcp_tool(world):
    b = ctx(world.bob)
    assert server.list_models(ctx=b) == [] and server.list_files(ctx=b) == []
    missing = [
        lambda: server.get_model("anns-box", ctx=b),
        lambda: run(server.update_model("anns-box", changes={"x": 1}, ctx=b)),
        lambda: run(server.render_model("anns-box", ctx=b)),
        lambda: run(server.revert_model("anns-box", 1, ctx=b)),
        lambda: run(server.duplicate_model("anns-box", "stolen", ctx=b)),
        lambda: server.delete_model("anns-box", ctx=b),
        lambda: server.reply_to_comment("anns-box", "c1", "hi", ctx=b),
        lambda: server.check_print(model_id="anns-box", ctx=b),
        lambda: server.file_info("anns-box/v1/model.stl", ctx=b),
        lambda: server.export_obj("x", [{"file": "anns-box/v1/model.stl"}], ctx=b),
    ]
    for call in missing:
        with pytest.raises(KeyError, match="does not exist"):
            call()
    for call in (lambda: server.file_info("part.stl", ctx=b), lambda: server.delete_file("part.stl", ctx=b),
                 lambda: server.modify_file("part.stl", scale=2, ctx=b), lambda: server.split_file("part.stl", ctx=b)):
        with pytest.raises(FileNotFoundError):
            call()
    assert (server.Lib(world.ann).dir / "part.stl").is_file()
    assert server.store.get("anns-box")["versions"][0]["version"] == 1


def test_same_names_do_not_collide(world):
    b = ctx(world.bob)
    m = run(server.create_model("anns-box", BOX, ctx=b))
    assert m["model_id"] == "anns-box-2"
    server.import_file("part", data_base64=__import__("base64").b64encode(
        trimesh.creation.box(extents=(1, 1, 1)).export(file_type="stl")).decode(), ctx=b)
    assert server.file_info("part.stl", ctx=b)["extents_mm"] == [1, 1, 1]
    assert server.file_info("part.stl", ctx=ctx(world.ann))["extents_mm"] == [5, 5, 5]


def test_live_updates_follow_only_your_own_library(world):
    bob_c = world.client_for(world.bob)
    before = bob_c.get("/api/models").json()["revision"]
    run(server.update_model("anns-box", changes={}, note="ann edits", ctx=ctx(world.ann)))
    assert bob_c.get("/api/models").json()["revision"] == before
    ann_c = world.client_for(world.ann)
    assert ann_c.get("/api/models").json()["revision"] == server.store.revision


def test_admin_sees_everyone_and_mcp_without_a_user_is_refused(world):
    admin = server._admin()
    c = world.client_for(admin)
    assert [m["id"] for m in c.get("/api/models").json()["models"]] == ["anns-box"]
    assert c.get("/files/anns-box/v1/model.stl").status_code == 200
    assert c.get(f"/f/{world.ann['id']}/part.stl").status_code == 200
    with pytest.raises(PermissionError):
        server.list_models()  # MCP_TOKEN is set, so a call without a request has no user


def test_legacy_models_and_files_become_the_admins(out_dir, db):
    """What startup() does with data from before users existed."""
    st = server.store
    m = st.create("old", "csg", BOX)
    db.execute("DELETE FROM models")
    (st.dir / "old" / "model.json").write_text(json.dumps({k: v for k, v in m.items() if k != "owner"}))
    (out_dir / "loose.stl").write_bytes(trimesh.creation.box().export(file_type="stl"))
    server.startup()
    admin = server._admin()
    assert st.get("old")["owner"] == admin["id"]
    assert (server.Lib(admin).dir / "loose.stl").is_file() and not (out_dir / "loose.stl").exists()
    server.startup()  # idempotent
    assert [x["id"] for x in st.list()] == ["old"]
