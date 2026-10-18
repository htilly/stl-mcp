import asyncio

import pytest
import trimesh
from starlette.testclient import TestClient

import printcheck as pc
import server

run = asyncio.run

PLATE = {"root": {"shape": "box", "size": [30, 30, 0.6]}}


@pytest.fixture
def client(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "")
    return TestClient(server.app, follow_redirects=False)


# ---- settings API

def test_admin_page_is_served(client):
    r = client.get("/admin")
    assert r.status_code == 200 and "Print-check settings" in r.text


def test_get_settings_returns_defaults_and_schema(client):
    d = client.get("/api/settings").json()
    assert d["values"] == pc.DEFAULTS == d["defaults"]
    assert set(d["schema"]) == set(pc.DEFAULTS)


def test_save_settings_and_reset(client):
    r = client.post("/api/settings", json={"values": {"min_wall_mm": 0.8, "check_overhangs": False}})
    assert r.status_code == 200 and r.json()["values"]["min_wall_mm"] == 0.8
    assert server.store.settings.get()["check_overhangs"] is False
    r = client.post("/api/settings", json={"reset": ["min_wall_mm"]})
    assert r.json()["values"]["min_wall_mm"] == pc.DEFAULTS["min_wall_mm"]
    assert r.json()["values"]["check_overhangs"] is False
    assert client.post("/api/settings", json={"reset": True}).json()["values"] == pc.DEFAULTS


def test_save_settings_rejects_bad_input(client):
    r = client.post("/api/settings", json={"values": {"min_wall_mm": -3}})
    assert r.status_code == 400 and "between" in r.json()["error"]
    r = client.post("/api/settings", json={"values": {"bogus": 1}})
    assert r.status_code == 400
    assert server.store.settings.get() == pc.DEFAULTS


def test_settings_are_persisted_in_the_data_dir(client, out_dir):
    client.post("/api/settings", json={"values": {"build_z_mm": 180}})
    assert pc.Settings(out_dir / "settings.json").get()["build_z_mm"] == 180


def test_recheck_endpoint(client):
    run(server.create_model("plate", PLATE))
    assert server.store.get("plate")["versions"][0]["parts"]["model"]["warnings"]
    client.post("/api/settings", json={"values": {"min_wall_mm": 0.3}})
    assert client.post("/api/recheck").json() == {"ok": True, "models": 1}
    assert server.store.get("plate")["versions"][0]["parts"]["model"]["warnings"] == []


def test_admin_requires_token_even_when_ui_is_public(client, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    monkeypatch.setattr(server, "UI_PUBLIC", True)
    assert client.get("/api/models").status_code == 200
    for path in ("/admin", "/api/settings"):
        assert client.get(path).status_code == 401
    assert client.post("/api/settings", json={"reset": True}).status_code == 401
    assert client.post("/api/recheck").status_code == 401
    assert client.get("/api/settings", headers={"Authorization": "Bearer secret"}).status_code == 200


# ---- warnings surface in the API and MCP responses

def test_summary_carries_warnings(out_dir):
    s = run(server.create_model("plate", PLATE))
    assert any(w["code"] == "thin_walls" for w in s["parts"]["model"]["warnings"])
    assert any("walls as thin" in m for m in s["print_warnings"])


def test_clean_model_has_no_print_warnings(out_dir):
    s = run(server.create_model("solid", {"root": {"shape": "box", "size": [30, 30, 10]}}))
    assert "print_warnings" not in s and s["parts"]["model"]["warnings"] == []


def test_models_list_counts_warnings(client, out_dir):
    run(server.create_model("plate", PLATE))
    run(server.create_model("solid", {"root": {"shape": "box", "size": [30, 30, 10]}}))
    counts = {m["id"]: m["warnings"] for m in client.get("/api/models").json()["models"]}
    assert counts["plate"] > 0 and counts["solid"] == 0


def test_check_print_tool_for_model_and_file(out_dir):
    run(server.create_model("plate", PLATE))
    r = server.check_print(model_id="plate")
    assert r["ok"] is False and "thin_walls" in {w["code"] for w in r["warnings"]["model"]}
    server.store.settings.update({"min_wall_mm": 0.3})
    assert server.check_print(model_id="plate")["ok"] is True
    (out_dir / "big.stl").write_bytes(trimesh.creation.box((400, 10, 10)).export(file_type="stl"))
    r = server.check_print(file="big.stl")
    assert "exceeds_build_volume" in {w["code"] for w in r["warnings"]["model"]}


def test_check_print_needs_exactly_one_target(out_dir):
    with pytest.raises(ValueError, match="exactly one"):
        server.check_print()
    with pytest.raises(ValueError, match="exactly one"):
        server.check_print(model_id="a", file="b.stl")


def test_uploaded_file_reports_print_warnings(client, out_dir):
    data = trimesh.creation.box((400, 10, 10)).export(file_type="stl")
    r = client.put("/upload/big.stl", content=data)
    assert r.status_code == 200 and any("build volume" in m for m in r.json()["print_warnings"])
