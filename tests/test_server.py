import asyncio
import base64
import json
import zipfile

import pytest
import trimesh
from starlette.testclient import TestClient

import server

run = asyncio.run

BOX_SPEC = {"vars": {"w": 20}, "root": {"shape": "box", "size": ["=w", 10, 5]}}


def _stl_bytes(extents=(10, 10, 10)):
    return trimesh.creation.box(extents=extents).export(file_type="stl")


def fdir():
    """The admin's standalone files (direct tool calls run as the admin in single-user mode)."""
    return server.Lib(server._admin()).dir


def _write_box(out_dir, name="box.stl", extents=(10, 20, 30)):
    (fdir() / name).write_bytes(_stl_bytes(extents))
    return name


def _new_model(name="demo", spec=None):
    return run(server.create_model(name, spec or BOX_SPEC))


# ---- helpers

def test_safe_sanitizes_and_truncates():
    assert server._safe("my box!") == "my_box"
    assert len(server._safe("a" * 100)) == 60


def test_safe_rejects_empty():
    with pytest.raises(ValueError, match="invalid name"):
        server._safe("!!!")


def test_url(monkeypatch, out_dir):
    monkeypatch.setattr(server, "PUBLIC_URL", "https://x.test")
    admin = server._admin()
    assert server.Lib(admin).url("a.stl") == f"https://x.test/f/{admin['id']}/a.stl"


def test_out_file_blocks_traversal_and_bad_suffix(out_dir):
    lib = server.Lib(server._admin())
    (fdir() / "a.txt").write_text("x")
    with pytest.raises(FileNotFoundError):
        server._out_file(lib, "../../etc/passwd")
    with pytest.raises(FileNotFoundError):
        server._out_file(lib, "a.txt")
    with pytest.raises(FileNotFoundError):
        server._out_file(lib, "missing.stl")


def test_check_public_blocks_private(monkeypatch):
    monkeypatch.setattr(server, "ALLOW_PRIVATE_URLS", False)
    with pytest.raises(PermissionError, match="private address"):
        server._check_public("127.0.0.1")
    monkeypatch.setattr(server, "ALLOW_PRIVATE_URLS", True)
    server._check_public("127.0.0.1")


def test_fetch_rejects_non_http():
    with pytest.raises(ValueError, match="http"):
        server._fetch("file:///etc/passwd")


def test_open_counts_open_comments():
    m = {"comments": [{"status": "open"}, {"status": "resolved"}, {"status": "open"}]}
    assert server._open(m) == 2
    assert server._open({}) == 0


def test_comment_view_drops_empty_and_internal_fields():
    c = {"id": "c1", "text": "hi", "part": None, "replies": [], "author": "user", "point": [1, 2, 3]}
    assert server._comment_view(c) == {"id": "c1", "text": "hi", "point": [1, 2, 3]}


# ---- standalone file tools

def test_save_makes_simplified_copy_over_limit(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TINKERCAD_MAX", 20_000)
    info = server._save(server.Lib(server._admin()), trimesh.creation.icosphere(subdivisions=4), "big")
    assert info["tinkercad"]["file"] == "big_tinkercad.stl"
    assert info["tinkercad"]["size_bytes"] <= 20_000
    assert (fdir() / "big_tinkercad.stl").is_file()


def test_file_info(out_dir):
    _write_box(out_dir)
    info = run(server.file_info("box.stl"))
    assert info["watertight"] is True and info["parts"] == 1
    assert sorted(info["extents_mm"]) == [10.0, 20.0, 30.0]
    assert info["volume_cm3"] == 6.0


def test_import_file_base64(out_dir):
    data = base64.b64encode(_stl_bytes()).decode()
    info = run(server.import_file("imp", data_base64=data))
    assert info["file"] == "imp.stl" and (fdir() / "imp.stl").is_file()


def test_import_file_requires_exactly_one_source():
    with pytest.raises(ValueError, match="either url or data_base64"):
        run(server.import_file("x"))
    with pytest.raises(ValueError, match="either url or data_base64"):
        run(server.import_file("x", url="http://a", data_base64="AA=="))


def test_import_file_size_limit(out_dir, monkeypatch):
    monkeypatch.setattr(server, "MAX_UPLOAD", 10)
    with pytest.raises(ValueError, match="larger than"):
        run(server.import_file("x", data_base64=base64.b64encode(b"x" * 100).decode()))


def test_import_file_from_url(out_dir, monkeypatch):
    monkeypatch.setattr(server, "_fetch", lambda url: (_stl_bytes(), "stl"))
    info = run(server.import_file("remote", url="https://example.com/m.stl"))
    assert info["file"] == "remote.stl"


def test_modify_file_scale_saves_under_new_name(out_dir):
    _write_box(out_dir)
    info = run(server.modify_file("box.stl", name="big", scale=2))
    assert sorted(info["extents_mm"]) == [20.0, 40.0, 60.0]
    assert (fdir() / "box.stl").is_file() and (fdir() / "big.stl").is_file()


def test_modify_file_overwrites_by_default(out_dir):
    _write_box(out_dir)
    run(server.modify_file("box.stl", scale=2))
    assert sorted(run(server.file_info("box.stl"))["extents_mm"]) == [20.0, 40.0, 60.0]


def test_modify_file_max_triangles(out_dir):
    (fdir() / "ball.stl").write_bytes(trimesh.creation.icosphere(subdivisions=4).export(file_type="stl"))
    info = run(server.modify_file("ball.stl", name="small", max_triangles=200))
    assert info["triangles"] <= 200


def test_split_file_orders_largest_first(out_dir):
    a = trimesh.creation.box(extents=(10, 10, 10))
    b = trimesh.creation.icosphere(subdivisions=3)
    b.apply_translation([50, 0, 0])
    (fdir() / "two.stl").write_bytes(trimesh.util.concatenate([a, b]).export(file_type="stl"))
    parts = run(server.split_file("two.stl"))
    assert [p["file"] for p in parts] == ["two_part1.stl", "two_part2.stl"]
    assert parts[0]["triangles"] > parts[1]["triangles"]


def test_export_obj_creates_obj_mtl_zip(out_dir):
    _write_box(out_dir, "a.stl")
    _write_box(out_dir, "b.stl", (5, 5, 5))
    res = run(server.export_obj("set", [{"file": "a.stl", "color": "blue"}, {"file": "b.stl"}]))
    assert res["parts"] == ["a", "b"] and res["triangles"] == 24
    for ext in ("obj", "mtl", "zip"):
        assert (fdir() / f"set.{ext}").is_file()
    with zipfile.ZipFile(fdir() / "set.zip") as z:
        assert sorted(z.namelist()) == ["set.mtl", "set.obj"]


def test_export_obj_simplifies_over_limit(out_dir, monkeypatch):
    (fdir() / "ball.stl").write_bytes(trimesh.creation.icosphere(subdivisions=4).export(file_type="stl"))
    full = run(server.export_obj("full", [{"file": "ball.stl"}]))
    monkeypatch.setattr(server, "TINKERCAD_MAX", full["size_bytes"] // 2)
    small = run(server.export_obj("small", [{"file": "ball.stl"}]))
    assert small["triangles"] < full["triangles"]


def test_list_and_delete_files(out_dir):
    _write_box(out_dir)
    (fdir() / "ignore.txt").write_text("x")
    assert [f["file"] for f in server.list_files()] == ["box.stl"]
    assert server.delete_file("box.stl") == "deleted box.stl"
    assert server.list_files() == []
    with pytest.raises(FileNotFoundError):
        server.delete_file("box.stl")


# ---- model library tools

def test_tools_are_registered():
    names = {t.name for t in run(server.mcp.list_tools())}
    assert names == {
        "create_model", "create_enclosure", "create_from_script", "create_from_openscad", "update_model",
        "get_model", "set_visibility",
        "reply_to_comment", "render_model", "list_models", "revert_model", "duplicate_model",
        "delete_model", "import_file", "file_info", "modify_file", "split_file", "export_obj",
        "list_files", "delete_file", "check_print", "whoami", "lookup_part", "print_guide",
        "import_outline", "create_relief", "create_from_build123d", "measure_model"}


def test_create_model(out_dir):
    s = _new_model("My Demo")
    assert s["model_id"] == "my-demo" and s["version"] == 1 and s["kind"] == "csg"
    assert s["viewer_url"].endswith("/m/my-demo")
    part = s["parts"]["model"]
    assert part["extents_mm"] == [20.0, 10.0, 5.0] and part["watertight"]
    assert part["url"].endswith("/files/my-demo/v1/model.stl")


def test_create_model_rejects_bad_spec(out_dir):
    with pytest.raises(ValueError):
        run(server.create_model("bad", {"nothing": 1}))
    assert server.list_models() == []


def test_create_enclosure_has_body_and_lid(out_dir):
    s = run(server.create_enclosure("my box", 40, 30, 15))
    assert s["kind"] == "enclosure" and set(s["parts"]) == {"body", "lid"}
    assert (out_dir / "models" / "my-box" / "v1" / "body.stl").is_file()
    assert (out_dir / "models" / "my-box" / "v1" / "lid.stl").is_file()


def test_create_from_script_disabled_by_default(out_dir):
    with pytest.raises(PermissionError, match="disabled"):
        run(server.create_from_script("s", "result = None"))


def test_create_from_script_runs(out_dir):
    server.store.allow_script = True
    s = run(server.create_from_script("s", "result = g.blk(2, 3, 4, 0, 0, 0)"))
    assert s["kind"] == "script" and s["parts"]["model"]["extents_mm"] == [2.0, 3.0, 4.0]


def test_create_from_script_reports_errors(out_dir):
    server.store.allow_script = True
    with pytest.raises(RuntimeError, match="boom"):
        run(server.create_from_script("s", "raise Exception('boom')"))


def test_update_model_changes_var_and_creates_version(out_dir):
    _new_model()
    s = run(server.update_model("demo", changes={"w": 40}, note="wider"))
    assert s["version"] == 2 and s["parts"]["model"]["extents_mm"][0] == 40.0
    assert [h["note"] for h in server.get_model("demo")["history"]] == ["created", "wider"]


def test_update_model_json_pointer_and_remove(out_dir):
    spec = {"root": {"op": "union", "children": [
        {"shape": "box", "size": [10, 10, 10]},
        {"shape": "box", "size": [5, 5, 5], "translate": [20, 0, 0]}]}}
    _new_model("two", spec)
    s = run(server.update_model("two", changes={"/root/children/1/size/0": 8}))
    assert s["version"] == 2
    s = run(server.update_model("two", remove=["/root/children/1"]))
    assert s["parts"]["model"]["extents_mm"] == [10.0, 10.0, 10.0]


def test_update_model_rename_without_new_version(out_dir):
    _new_model()
    s = run(server.update_model("demo", name="Renamed", description="d"))
    assert s["version"] == 1 and s["name"] == "Renamed"
    assert server.get_model("demo")["description"] == "d"


def test_update_model_failed_build_saves_nothing(out_dir):
    _new_model()
    with pytest.raises(Exception):
        run(server.update_model("demo", changes={"/root/shape": "nonsense"}))
    assert server.get_model("demo")["version"] == 1


def test_create_with_model_id_rebuilds_existing_model_another_way(out_dir):
    box = run(server.create_enclosure("my box", 40, 30, 15))
    s = run(server.create_model("my box", BOX_SPEC, model_id=box["model_id"]))
    assert s["model_id"] == box["model_id"] and s["version"] == 2 and s["kind"] == "csg"
    assert [m["model_id"] for m in server.list_models()] == [box["model_id"]]
    hist = server.get_model(box["model_id"])["history"]
    assert hist[-1]["note"] == "rebuilt as csg"
    s = run(server.revert_model(box["model_id"], 1))
    assert s["kind"] == "enclosure" and set(s["parts"]) == {"body", "lid"}


def test_update_model_switches_kind_with_params(out_dir):
    box = run(server.create_enclosure("box", 40, 30, 15))
    s = run(server.update_model(box["model_id"], kind="csg", params=BOX_SPEC))
    assert s["kind"] == "csg" and s["version"] == 2
    s = run(server.update_model(box["model_id"], changes={"w": 30}))
    assert s["parts"]["model"]["extents_mm"][0] == 30.0


def test_update_model_kind_needs_params_and_a_known_kind(out_dir):
    _new_model()
    with pytest.raises(ValueError, match="needs params"):
        run(server.update_model("demo", kind="openscad", changes={"w": 3}))
    with pytest.raises(ValueError, match="unknown kind"):
        run(server.update_model("demo", kind="stl", params={}))
    assert server.get_model("demo")["version"] == 1


def test_update_unknown_model(out_dir):
    with pytest.raises(KeyError):
        run(server.update_model("nope", changes={"w": 1}))


def test_get_model_full_summary(out_dir):
    _new_model(spec=BOX_SPEC)
    m = server.get_model("demo")
    assert m["params"] == BOX_SPEC and m["comments"] == [] and m["resolved_comments"] == 0
    assert m["history"] == [{"version": 1, "note": "created"}]


def test_comment_lifecycle_via_tools(out_dir):
    _new_model()
    server.store.add_comment("demo", "make it taller", point=[0, 0, 5], part="model")
    cm = server.get_model("demo")["comments"]
    assert cm[0]["id"] == "c1" and cm[0]["text"] == "make it taller"
    assert server.list_models()[0]["open_comments"] == 1
    r = server.reply_to_comment("demo", "c1", "how tall?")
    assert r["status"] == "open" and r["replies"][-1]["author"] == "claude"
    r = server.reply_to_comment("demo", "c1", "done", resolve=True)
    assert r["status"] == "resolved"
    full = server.get_model("demo")
    assert full["comments"] == [] and full["resolved_comments"] == 1


def test_update_model_resolves_comments(out_dir):
    _new_model()
    server.store.add_comment("demo", "wider please")
    run(server.update_model("demo", changes={"w": 30}, resolves=["c1"]))
    m = server.store.get("demo")
    assert m["comments"][0]["status"] == "resolved"
    assert m["comments"][0]["replies"][0]["text"].startswith("Addressed in v2")


def test_update_model_unknown_comment_does_not_build(out_dir):
    _new_model()
    with pytest.raises(KeyError):
        run(server.update_model("demo", changes={"w": 30}, resolves=["c9"]))
    assert server.get_model("demo")["version"] == 1


def test_list_models_most_recent_first(out_dir):
    _new_model("one")
    _new_model("two")
    run(server.update_model("one", changes={"w": 21}))
    rows = server.list_models()
    assert [r["model_id"] for r in rows] == ["one", "two"]
    assert rows[0]["parts"] == ["model"] and rows[0]["version"] == 2


def test_revert_model_creates_new_version(out_dir):
    _new_model()
    run(server.update_model("demo", changes={"w": 40}))
    s = run(server.revert_model("demo", 1))
    assert s["version"] == 3 and s["parts"]["model"]["extents_mm"][0] == 20.0
    assert server.get_model("demo")["history"][-1]["note"] == "reverted from v1"


def test_duplicate_model(out_dir):
    _new_model()
    s = run(server.duplicate_model("demo", "Variant"))
    assert s["model_id"] == "variant" and s["version"] == 1
    assert {r["model_id"] for r in server.list_models()} == {"demo", "variant"}


def test_delete_model(out_dir):
    _new_model()
    assert server.delete_model("demo") == "deleted demo"
    assert server.list_models() == []
    with pytest.raises(KeyError):
        server.delete_model("demo")


def test_render_model_returns_info_and_png(out_dir):
    _new_model()
    info, image = run(server.render_model("demo", views=["iso", "bogus"]))
    assert json.loads(info)["rendered_version"] == 1
    assert image.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_model_unknown_views_fall_back_to_iso(out_dir):
    _new_model()
    _, image = run(server.render_model("demo", views=["bogus"]))
    assert image.data[:4] == b"\x89PNG"


# ---- HTTP app

@pytest.fixture
def client(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "")
    # Not used as a context manager: the MCP lifespan is not needed for these routes.
    return TestClient(server.app, follow_redirects=False)


def test_health_is_open_even_with_token(client, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    assert client.get("/health").text == "ok"


def test_pages_and_static(client):
    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()
    assert client.get("/m/anything").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_auth_required_when_token_set(client, out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    _write_box(out_dir)
    assert client.get("/files/box.stl").status_code == 401
    assert client.get("/files/box.stl", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/files/box.stl", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_html_request_without_token_gets_login_page(client, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    r = client.get("/", headers={"accept": "text/html"})
    assert r.status_code == 401 and "<html" in r.text.lower()
    assert client.get("/api/models").json() == {"error": "unauthorized"}


def test_token_form_sets_cookie_and_redirects(client, out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    _write_box(out_dir)
    r = client.post("/auth/token", data={"token": "secret", "next": "/files/box.stl"})
    assert r.status_code == 303 and r.headers["location"] == "/files/box.stl"
    assert "stl_session=" in r.headers["set-cookie"]
    assert "stl_token=secret" not in r.headers["set-cookie"]  # the raw token is never a cookie
    client.cookies.set("stl_session", r.cookies["stl_session"])
    assert client.get("/files/box.stl").status_code == 200


def test_files_public_skips_auth_for_downloads_only(client, out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    monkeypatch.setattr(server, "FILES_PUBLIC", True)
    _write_box(out_dir)
    assert client.get("/files/box.stl").status_code == 200
    assert client.get("/api/models").status_code == 401
    assert client.put("/upload/x.stl", content=_stl_bytes()).status_code == 401


def test_ui_public_allows_reads_but_not_writes(client, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    monkeypatch.setattr(server, "UI_PUBLIC", True)
    assert client.get("/").status_code == 200
    assert client.get("/api/models").status_code == 200
    r = client.post("/api/models/x", json={"action": "rename"})
    assert r.status_code == 401
    assert client.delete("/api/models/x").status_code == 401


def test_flat_file_download_content_type_and_404(client, out_dir):
    _write_box(out_dir)
    r = client.get("/files/box.stl")
    assert r.status_code == 200 and r.headers["content-type"].startswith("model/stl")
    assert client.get("/files/missing.stl").status_code == 404
    (fdir() / "x.txt").write_text("x")
    assert client.get("/files/x.txt").status_code == 404


def test_model_file_download(client):
    _new_model()
    r = client.get("/files/demo/v1/model.stl")
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    assert client.get("/files/demo/v9/model.stl").status_code == 404
    assert client.get("/files/demo/v1/../model.json").status_code == 404
    assert client.get("/files/nope/v1/model.stl").status_code == 404


def test_api_models_lists_library(client):
    assert client.get("/api/models").json()["models"] == []
    _new_model()
    data = client.get("/api/models").json()
    m = data["models"][0]
    assert m["id"] == "demo" and m["versions"] == 1 and m["open_comments"] == 0
    assert m["parts"]["model"]["url"] == "/files/demo/v1/model.stl"
    assert data["revision"] == server.store.revision


def test_api_model_get_adds_part_urls(client):
    _new_model()
    m = client.get("/api/models/demo").json()
    assert m["versions"][0]["parts"]["model"]["url"] == "/files/demo/v1/model.stl"
    assert "revision" in m
    assert client.get("/api/models/nope").status_code == 404


def test_api_model_update_and_rename(client):
    _new_model()
    r = client.post("/api/models/demo", json={"action": "update", "changes": {"w": 33}})
    assert r.json() == {"ok": True, "id": "demo"}
    m = client.get("/api/models/demo").json()
    assert m["current"] == 2 and m["versions"][-1]["note"] == "from the web UI"
    client.post("/api/models/demo", json={"action": "rename", "name": "New", "description": "dd"})
    m = client.get("/api/models/demo").json()
    assert m["name"] == "New" and m["description"] == "dd"


def test_api_model_update_error_is_400(client):
    _new_model()
    r = client.post("/api/models/demo", json={"action": "update", "changes": {"/root/shape": "bad"}})
    assert r.status_code == 400 and "error" in r.json()


def test_api_model_set_current_revert_duplicate(client):
    _new_model()
    client.post("/api/models/demo", json={"action": "update", "changes": {"w": 33}})
    client.post("/api/models/demo", json={"action": "set_current", "version": 1})
    assert client.get("/api/models/demo").json()["current"] == 1
    client.post("/api/models/demo", json={"action": "revert", "version": 2})
    assert client.get("/api/models/demo").json()["current"] == 3
    r = client.post("/api/models/demo", json={"action": "duplicate"})
    assert r.json()["id"] == "demo-copy"
    r = client.post("/api/models/demo", json={"action": "duplicate", "name": "Other"})
    assert r.json()["id"] == "other"


def test_api_model_comments(client):
    run(server.create_enclosure("box", 40, 30, 15))
    r = client.post("/api/models/box", json={"action": "comment_add", "text": "hole here",
                                             "point": [10, 10, 10], "normal": [0, 0, 1],
                                             "part": "body", "version": 1})
    assert r.status_code == 200
    c = client.get("/api/models/box").json()["comments"][0]
    assert c["id"] == "c1" and c["inner_xyz"] == [7.6, 7.6, 7.6]
    for action, status in (("comment_resolve", "resolved"), ("comment_reopen", "open")):
        client.post("/api/models/box", json={"action": action, "id": "c1"})
        assert client.get("/api/models/box").json()["comments"][0]["status"] == status
    client.post("/api/models/box", json={"action": "comment_reply", "id": "c1", "text": "ok"})
    assert client.get("/api/models/box").json()["comments"][0]["replies"][0]["author"] == "user"
    client.post("/api/models/box", json={"action": "comment_delete", "id": "c1"})
    assert client.get("/api/models/box").json()["comments"] == []


def test_api_model_empty_comment_is_400(client):
    _new_model()
    r = client.post("/api/models/demo", json={"action": "comment_add", "text": "  "})
    assert r.status_code == 400


def test_api_model_unknown_action_and_delete(client):
    _new_model()
    assert client.post("/api/models/demo", json={"action": "nope"}).status_code == 400
    assert client.delete("/api/models/demo").json() == {"ok": True}
    assert client.delete("/api/models/demo").status_code == 404
    assert client.get("/api/models/demo").status_code == 404


def test_preview_png_is_rendered_then_cached(client, out_dir):
    _new_model()
    r = client.get("/api/models/demo/preview.png")
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert (out_dir / "models" / "demo" / "v1" / "preview-2.png").is_file()
    assert client.get("/api/models/demo/preview.png").content == r.content
    assert client.get("/api/models/nope/preview.png").status_code == 404


def test_api_events_streams_initial_and_changes():
    class Req:
        calls = 0
        state = type("State", (), {"user": None})()  # anonymous: follows the admin's own models

        async def is_disconnected(self):
            return False

    async def go():
        resp = await server.api_events(Req())
        assert resp.media_type == "text/event-stream"
        first = await resp.body_iterator.__anext__()
        server.store._bump("zzz", server._admin()["id"])
        second = await resp.body_iterator.__anext__()
        await resp.body_iterator.aclose()
        return first, second

    first, second = run(go())
    assert json.loads(first.removeprefix("data: ")) == {"revision": server.store.revision - 1, "claude": None, "ok": True}
    assert json.loads(second.removeprefix("data: ")) == {"revision": server.store.revision, "id": "zzz"}


def test_api_events_tell_when_claude_connects(monkeypatch):
    """The Live dot waits (amber) until an AI client reaches /mcp, then turns green."""
    user = server._admin()
    monkeypatch.setattr(server, "mcp_seen", {})

    class Req:
        state = type("State", (), {"user": user})()

        async def is_disconnected(self):
            return False

    async def go():
        resp = await server.api_events(Req())
        first = await resp.body_iterator.__anext__()
        server.mcp_seen[user["id"]] = 1.0  # what the Auth middleware records on a /mcp request
        second = await resp.body_iterator.__anext__()
        await resp.body_iterator.aclose()
        return first, second

    first, second = run(go())
    assert json.loads(first.removeprefix("data: "))["claude"] is False
    assert json.loads(second.removeprefix("data: "))["claude"] is True


def test_health_reports_a_broken_database(client, monkeypatch):
    assert client.get("/health").status_code == 200

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(server.oauth.state.db, "one", boom)
    assert server._healthy() is False
    assert run(server.health(None)).status_code == 503


def test_mcp_request_marks_claude_connected(client, monkeypatch):
    monkeypatch.setattr(server, "mcp_seen", {})
    assert not server._claude_connected(server._admin())
    client.post("/mcp/", json={})
    assert server._claude_connected(server._admin())


def test_upload_converts_to_stl(client, out_dir):
    obj = trimesh.creation.box().export(file_type="obj")
    r = client.put("/upload/model.obj", content=obj)
    assert r.status_code == 200 and r.json()["file"] == "model.stl"
    assert (fdir() / "model.stl").is_file()


def test_upload_rejects_garbage_and_oversize(client, monkeypatch):
    assert client.post("/upload/bad.stl", content=b"not a mesh").status_code == 400
    monkeypatch.setattr(server, "MAX_UPLOAD", 10)
    r = client.put("/upload/big.stl", content=b"x" * 100)
    assert r.status_code == 413 and "larger than" in r.json()["error"]
