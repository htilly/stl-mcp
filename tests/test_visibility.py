"""Private and public models: a model is private by default; its owner can make it public, which
lists it in the gallery and lets every logged-in user read, download and copy it - but not change
it, comment on it or see its comments. Making it private again hides it like before."""
import asyncio
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

import server

BOX = {"root": {"shape": "box", "size": [20, 20, 10]}}


def ctx(user):
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

    def client_for(user):
        c = TestClient(server.app, follow_redirects=False)
        c.cookies.set(server.SESSION_COOKIE, st.create_session(user["id"]))
        return c
    return SimpleNamespace(ann=ann, bob=bob, client_for=client_for,
                           annc=client_for(ann), bobc=client_for(bob))


def publish(world):
    r = world.annc.post("/api/models/anns-box", json={"action": "set_visibility", "visibility": "public"})
    assert r.status_code == 200, r.text


def test_models_are_private_by_default_and_show_it(world):
    assert server.store.get("anns-box")["visibility"] == "private"
    assert world.annc.get("/api/models").json()["models"][0]["visibility"] == "private"
    m = world.annc.get("/api/models/anns-box").json()
    assert m["visibility"] == "private" and m["can_edit"] and m["mine"]
    a = ctx(world.ann)
    assert server.get_model("anns-box", ctx=a)["visibility"] == "private"
    assert server.list_models(ctx=a)[0]["visibility"] == "private"
    assert world.annc.get("/api/gallery").json()["models"] == []
    assert world.bobc.get("/api/models/anns-box").status_code == 404


def test_owner_publishes_and_it_appears_in_the_gallery(world):
    publish(world)
    assert world.annc.get("/api/models").json()["models"][0]["visibility"] == "public"
    for c, mine in ((world.annc, True), (world.bobc, False)):
        g = c.get("/api/gallery").json()["models"]
        assert [(x["id"], x["owner_name"], x["mine"], x["copies"]) for x in g] == [("anns-box", "Ann", mine, 0)]
        assert "owner" not in g[0]  # user ids never reach other users
        assert g[0]["source"] == "csg"
    assert world.annc.get("/api/models").json()["models"][0]["source"] == "csg"
    assert world.annc.get("/api/models/anns-box").json()["versions"][0]["source"] == "csg"
    assert world.bobc.get("/gallery", headers={"accept": "text/html"}).status_code == 200


def test_public_model_is_readable_but_not_writable_by_others(world):
    publish(world)
    c = world.bobc
    m = c.get("/api/models/anns-box").json()
    assert m["visibility"] == "public" and not m["can_edit"] and not m["mine"]
    assert m["owner_name"] == "Ann" and "owner" not in m
    assert m["comments"] == []  # comments stay between Ann and her Claude
    assert c.get("/api/models/anns-box/preview.png").status_code == 200
    assert c.get("/files/anns-box/v1/model.stl").status_code == 200
    assert c.get("/api/models").json()["models"] == []  # not in Bob's own library
    for body in ({"action": "set_current", "version": 1}, {"action": "revert", "version": 1},
                 {"action": "rename", "name": "mine now"}, {"action": "update", "changes": {}},
                 {"action": "comment_add", "text": "hi"}, {"action": "comment_resolve", "id": "c1"},
                 {"action": "comment_delete", "id": "c1"},
                 {"action": "set_visibility", "visibility": "private"}):
        assert c.post("/api/models/anns-box", json=body).status_code == 404, body
    assert c.delete("/api/models/anns-box").status_code == 404
    m = server.store.get("anns-box")
    assert m["name"] == "anns-box" and len(m["versions"]) == 1 and len(m["comments"]) == 1
    assert m["visibility"] == "public"


def test_public_model_over_mcp(world):
    publish(world)
    b = ctx(world.bob)
    got = server.get_model("anns-box", ctx=b)
    assert got["visibility"] == "public" and got["comments"] == [] and "read_only" in got
    assert "read_only" not in server.get_model("anns-box", ctx=ctx(world.ann))
    assert server.check_print(model_id="anns-box", ctx=b)["warnings"]
    assert server.list_models(ctx=b) == []
    for call in (lambda: run(server.update_model("anns-box", changes={"x": 1}, ctx=b)),
                 lambda: server.delete_model("anns-box", ctx=b),
                 lambda: server.reply_to_comment("anns-box", "c1", "hi", ctx=b),
                 lambda: server.set_visibility("anns-box", "private", ctx=b)):
        with pytest.raises(KeyError, match="does not exist"):
            call()


def test_copy_is_an_independent_private_model_with_attribution(world):
    publish(world)
    r = world.bobc.post("/api/models/anns-box", json={"action": "duplicate", "name": "Bobs box"})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    copy = world.bobc.get(f"/api/models/{cid}").json()
    assert copy["visibility"] == "private" and copy["can_edit"] and copy["mine"]
    assert len(copy["versions"]) == 1 and not copy.get("comments")
    assert copy["copied_from"] == {"model_id": "anns-box", "name": "anns-box", "version": 1,
                                   "owner_name": "Ann"}
    assert world.annc.get("/api/gallery").json()["models"][0]["copies"] == 1
    # Bob changes his copy; Ann's original is untouched, and stays so when she unpublishes
    assert world.bobc.post(f"/api/models/{cid}", json={"action": "rename", "name": "B"}).status_code == 200
    assert server.store.get("anns-box")["name"] == "anns-box"
    world.annc.post("/api/models/anns-box", json={"action": "set_visibility", "visibility": "private"})
    assert world.bobc.get(f"/api/models/{cid}").status_code == 200
    # the MCP tool copies too
    m = run(server.duplicate_model(cid, "again", ctx=ctx(world.bob)))
    assert m["visibility"] == "private"


def test_unpublishing_hides_it_again(world):
    publish(world)
    world.annc.post("/api/models/anns-box", json={"action": "set_visibility", "visibility": "private"})
    assert world.bobc.get("/api/gallery").json()["models"] == []
    assert world.bobc.get("/api/models/anns-box").status_code == 404
    assert world.bobc.get("/files/anns-box/v1/model.stl").status_code == 404
    assert world.bobc.post("/api/models/anns-box", json={"action": "duplicate", "name": "x"}).status_code == 404


def test_set_visibility_tool_and_validation(world):
    a = ctx(world.ann)
    r = server.set_visibility("anns-box", "public", ctx=a)
    assert r["visibility"] == "public" and r["gallery_url"].endswith("/gallery")
    m = server.store.get("anns-box")
    assert m["published"] and len(m["versions"]) == 1  # no new version
    with pytest.raises(ValueError, match="visibility"):
        server.set_visibility("anns-box", "friends", ctx=a)
    bad = world.annc.post("/api/models/anns-box", json={"action": "set_visibility", "visibility": "x"})
    assert bad.status_code == 400
    # visibility survives later edits of the model
    run(server.update_model("anns-box", changes={"/root/size": [30, 20, 10]}, ctx=a))
    assert server.store.get("anns-box")["visibility"] == "public"
    server.set_visibility("anns-box", "private", ctx=a)
    assert server.store.get("anns-box")["published"] is None


def test_admin_can_unpublish_anyones_model(world):
    publish(world)
    admin = world.client_for(server._admin())
    r = admin.post("/api/models/anns-box", json={"action": "set_visibility", "visibility": "private"})
    assert r.status_code == 200
    assert world.bobc.get("/api/gallery").json()["models"] == []


def test_gallery_needs_a_login(world):
    publish(world)
    anon = TestClient(server.app, follow_redirects=False)
    assert anon.get("/api/gallery").status_code == 401
    assert anon.get("/api/models/anns-box").status_code == 401


def test_gallery_names_the_maker_and_filters_by_them(world):
    """Each gallery card names who made it with a maker key to filter on: the same for all of one
    user's models, different per user, and never the user id itself."""
    publish(world)
    b = ctx(world.bob)
    run(server.create_model("bobs-cup", BOX, ctx=b))
    run(server.create_model("bobs-lid", BOX, ctx=b))
    server.set_visibility("bobs-cup", "public", ctx=b)
    server.set_visibility("bobs-lid", "public", ctx=b)
    g = {m["id"]: m for m in world.annc.get("/api/gallery").json()["models"]}
    assert g["bobs-cup"]["owner_name"] == g["bobs-lid"]["owner_name"] == "Bob"
    assert g["bobs-cup"]["maker"] == g["bobs-lid"]["maker"] != g["anns-box"]["maker"]
    keys = {m["maker"] for m in g.values()}
    assert all(k and world.bob["id"] not in k and world.ann["id"] not in k for k in keys)
    # the viewer links the same maker key
    assert world.annc.get("/api/models/bobs-cup").json()["maker"] == g["bobs-cup"]["maker"]
    page = world.annc.get(f"/gallery?by={g['bobs-cup']['maker']}", headers={"accept": "text/html"})
    assert page.status_code == 200 and 'data-nav' in page.text


def test_gallery_source_tells_a_plain_stl_from_openscad():
    mesh = {"base": "0" * 64}
    src = lambda kind, **p: server._source({"kind": kind, "params": p})
    assert src("openscad", code='import("base.stl");', meshes=mesh) == "stl"
    assert src("openscad", code=' import ( "base.stl" ) \n', meshes=mesh) == "stl"
    assert src("openscad", code='difference() { import("base.stl"); cube(3); }', meshes=mesh) == "openscad"
    assert src("openscad", code="cube(3);") == "openscad"
    assert src("enclosure") == "enclosure"
