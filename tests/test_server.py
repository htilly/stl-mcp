import base64
import json
import zipfile

import pytest
import trimesh
from starlette.testclient import TestClient

import server


def _stl_bytes(extents=(10, 10, 10)):
    return trimesh.creation.box(extents=extents).export(file_type="stl")


def _write_box(out_dir, name="box.stl", extents=(10, 20, 30)):
    (out_dir / name).write_bytes(_stl_bytes(extents))
    return name


# ---- helpers

def test_safe_sanitizes_and_truncates():
    assert server._safe("my box!") == "my_box"
    assert len(server._safe("a" * 100)) == 60


def test_safe_rejects_empty():
    with pytest.raises(ValueError, match="invalid name"):
        server._safe("!!!")


def test_url(monkeypatch):
    monkeypatch.setattr(server, "PUBLIC_URL", "https://x.test")
    assert server._url("a.stl") == "https://x.test/files/a.stl"


def test_out_file_blocks_traversal_and_bad_suffix(out_dir):
    (out_dir / "a.txt").write_text("x")
    with pytest.raises(FileNotFoundError):
        server._out_file("../../etc/passwd")
    with pytest.raises(FileNotFoundError):
        server._out_file("a.txt")
    with pytest.raises(FileNotFoundError):
        server._out_file("missing.stl")


def test_check_public_blocks_private(monkeypatch):
    monkeypatch.setattr(server, "ALLOW_PRIVATE_URLS", False)
    with pytest.raises(PermissionError, match="private address"):
        server._check_public("127.0.0.1")
    monkeypatch.setattr(server, "ALLOW_PRIVATE_URLS", True)
    server._check_public("127.0.0.1")


def test_fetch_rejects_non_http(monkeypatch):
    with pytest.raises(ValueError, match="http"):
        server._fetch("file:///etc/passwd")


# ---- tools

def test_create_primitive_sits_on_bed(out_dir):
    info = server.create_primitive("ball", "sphere", diameter=10)
    assert info["file"] == "ball.stl" and (out_dir / "ball.stl").is_file()
    assert info["bounds_mm"][0][2] == 0
    assert info["extents_mm"] == [10.0, 10.0, 10.0]


def test_create_enclosure_returns_two_files(out_dir):
    res = server.create_enclosure("my box", 40, 30, 15)
    assert set(res) == {"body", "lid"}
    assert res["body"]["file"] == "my_box_body.stl"
    assert res["lid"]["file"] == "my_box_lid.stl"
    assert (out_dir / "my_box_body.stl").is_file() and (out_dir / "my_box_lid.stl").is_file()


def test_save_makes_simplified_copy_over_limit(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TINKERCAD_MAX", 20_000)
    info = server._save(trimesh.creation.icosphere(subdivisions=4), "big")
    assert info["tinkercad"]["file"] == "big_tinkercad.stl"
    assert info["tinkercad"]["size_bytes"] <= 20_000
    assert (out_dir / "big_tinkercad.stl").is_file()


def test_file_info(out_dir):
    _write_box(out_dir)
    info = server.file_info("box.stl")
    assert info["watertight"] is True and info["parts"] == 1
    assert sorted(info["extents_mm"]) == [10.0, 20.0, 30.0]
    assert info["volume_cm3"] == 6.0


def test_import_file_base64(out_dir):
    data = base64.b64encode(_stl_bytes()).decode()
    info = server.import_file("imp", data_base64=data)
    assert info["file"] == "imp.stl" and (out_dir / "imp.stl").is_file()


def test_import_file_requires_exactly_one_source():
    with pytest.raises(ValueError, match="either url or data_base64"):
        server.import_file("x")
    with pytest.raises(ValueError, match="either url or data_base64"):
        server.import_file("x", url="http://a", data_base64="AA==")


def test_import_file_size_limit(out_dir, monkeypatch):
    monkeypatch.setattr(server, "MAX_UPLOAD", 10)
    with pytest.raises(ValueError, match="larger than"):
        server.import_file("x", data_base64=base64.b64encode(b"x" * 100).decode())


def test_import_file_from_url(out_dir, monkeypatch):
    monkeypatch.setattr(server, "_fetch", lambda url: (_stl_bytes(), "stl"))
    info = server.import_file("remote", url="https://example.com/m.stl")
    assert info["file"] == "remote.stl"


def test_modify_file_scale_saves_under_new_name(out_dir):
    _write_box(out_dir)
    info = server.modify_file("box.stl", name="big", scale=2)
    assert sorted(info["extents_mm"]) == [20.0, 40.0, 60.0]
    assert (out_dir / "box.stl").is_file() and (out_dir / "big.stl").is_file()


def test_modify_file_overwrites_by_default(out_dir):
    _write_box(out_dir)
    server.modify_file("box.stl", scale=2)
    assert sorted(server.file_info("box.stl")["extents_mm"]) == [20.0, 40.0, 60.0]


def test_modify_file_max_triangles(out_dir):
    (out_dir / "ball.stl").write_bytes(trimesh.creation.icosphere(subdivisions=4).export(file_type="stl"))
    info = server.modify_file("ball.stl", name="small", max_triangles=200)
    assert info["triangles"] <= 200


def test_split_file_orders_largest_first(out_dir):
    a = trimesh.creation.box(extents=(10, 10, 10))
    b = trimesh.creation.icosphere(subdivisions=3)
    b.apply_translation([50, 0, 0])
    (out_dir / "two.stl").write_bytes(trimesh.util.concatenate([a, b]).export(file_type="stl"))
    parts = server.split_file("two.stl")
    assert [p["file"] for p in parts] == ["two_part1.stl", "two_part2.stl"]
    assert parts[0]["triangles"] > parts[1]["triangles"]


def test_export_obj_creates_obj_mtl_zip(out_dir):
    _write_box(out_dir, "a.stl")
    _write_box(out_dir, "b.stl", (5, 5, 5))
    res = server.export_obj("set", [{"file": "a.stl", "color": "blue"}, {"file": "b.stl"}])
    assert res["parts"] == ["a", "b"] and res["triangles"] == 24
    for ext in ("obj", "mtl", "zip"):
        assert (out_dir / f"set.{ext}").is_file()
    with zipfile.ZipFile(out_dir / "set.zip") as z:
        assert sorted(z.namelist()) == ["set.mtl", "set.obj"]


def test_export_obj_simplifies_over_limit(out_dir, monkeypatch):
    (out_dir / "ball.stl").write_bytes(trimesh.creation.icosphere(subdivisions=4).export(file_type="stl"))
    full = server.export_obj("full", [{"file": "ball.stl"}])
    monkeypatch.setattr(server, "TINKERCAD_MAX", full["size_bytes"] // 2)
    small = server.export_obj("small", [{"file": "ball.stl"}])
    assert small["triangles"] < full["triangles"]


def test_list_and_delete_files(out_dir):
    _write_box(out_dir)
    (out_dir / "ignore.txt").write_text("x")
    assert [f["file"] for f in server.list_files()] == ["box.stl"]
    assert server.delete_file("box.stl") == "deleted box.stl"
    assert server.list_files() == []
    with pytest.raises(FileNotFoundError):
        server.delete_file("box.stl")


def test_create_from_script_disabled_by_default(monkeypatch):
    monkeypatch.setattr(server, "ALLOW_SCRIPT", False)
    with pytest.raises(PermissionError, match="disabled"):
        server.create_from_script("s", "result = None")


def test_create_from_script_runs(out_dir, monkeypatch):
    monkeypatch.setattr(server, "ALLOW_SCRIPT", True)
    info = server.create_from_script("s", "result = g.diff(g.blk(20,20,10,0,0,0), [g.cyl(5,12,10,10,-1)])")
    assert info["file"] == "s.stl" and info["watertight"]


def test_create_from_script_reports_errors(out_dir, monkeypatch):
    monkeypatch.setattr(server, "ALLOW_SCRIPT", True)
    with pytest.raises(RuntimeError, match="boom"):
        server.create_from_script("s", "raise Exception('boom')")


def test_tools_are_registered():
    import asyncio
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == {"create_enclosure", "create_primitive", "create_from_script", "import_file",
                     "file_info", "modify_file", "split_file", "export_obj", "list_files",
                     "delete_file"}


# ---- HTTP app

@pytest.fixture
def client(out_dir):
    # Not used as a context manager: the MCP lifespan is not needed for these routes.
    return TestClient(server.app)


def test_health_is_open_even_with_token(client, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    assert client.get("/health").text == "ok"


def test_auth_required_when_token_set(client, out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    _write_box(out_dir)
    assert client.get("/files/box.stl").status_code == 401
    assert client.get("/files/box.stl", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/files/box.stl", headers={"Authorization": "Bearer secret"}).status_code == 200
    assert client.get("/files/box.stl?token=secret").status_code == 200


def test_files_public_skips_auth_for_downloads_only(client, out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    monkeypatch.setattr(server, "FILES_PUBLIC", True)
    _write_box(out_dir)
    assert client.get("/files/box.stl").status_code == 200
    assert client.put("/upload/x.stl", content=_stl_bytes()).status_code == 401


def test_file_download_content_type_and_404(client, out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "")
    _write_box(out_dir)
    r = client.get("/files/box.stl")
    assert r.status_code == 200 and r.headers["content-type"].startswith("model/stl")
    assert client.get("/files/missing.stl").status_code == 404
    (out_dir / "x.txt").write_text("x")
    assert client.get("/files/x.txt").status_code == 404


def test_upload_converts_to_stl(client, out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "")
    obj = trimesh.creation.box().export(file_type="obj")
    r = client.put("/upload/model.obj", content=obj)
    assert r.status_code == 200 and r.json()["file"] == "model.stl"
    assert (out_dir / "model.stl").is_file()


def test_upload_rejects_garbage_and_oversize(client, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "")
    assert client.post("/upload/bad.stl", content=b"not a mesh").status_code == 400
    monkeypatch.setattr(server, "MAX_UPLOAD", 10)
    r = client.put("/upload/big.stl", content=b"x" * 100)
    assert r.status_code == 413 and "larger than" in r.json()["error"]
