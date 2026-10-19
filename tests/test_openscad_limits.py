"""OpenSCAD limits an admin changes on the Load page: capped by the environment, saved in the
database, in effect for the next build without a restart."""
import threading
import time

import pytest
from starlette.testclient import TestClient

import openscad
import server
from openscad import OpenSCADError, Slots


def as_user(user):
    c = TestClient(server.app, follow_redirects=False)
    c.cookies.set(server.SESSION_COOKIE, server.oauth.state.create_session(user["id"]))
    return c


@pytest.fixture
def scad(monkeypatch):
    """Caps of 60 s / 3 builds, limits back at the caps, fresh slots; all restored afterwards."""
    monkeypatch.setattr(openscad, "TIMEOUT_CAP", 60.0)
    monkeypatch.setattr(openscad, "CONCURRENCY_CAP", 3)
    monkeypatch.setattr(openscad, "CPU_SECONDS_ENV", 0)
    monkeypatch.setattr(openscad, "TIMEOUT", 60.0)
    monkeypatch.setattr(openscad, "CONCURRENCY", 3)
    monkeypatch.setattr(openscad, "_slots", Slots(3))
    return openscad


@pytest.fixture
def admin(out_dir, monkeypatch, scad):
    monkeypatch.setattr(server, "TOKEN", "secret")
    return as_user(server._admin())


# ---------------------------------------------------------------- openscad.configure

def test_configure_changes_limits_and_cpu_follows_the_timeout(scad):
    scad.configure(30, 2)
    assert (scad.TIMEOUT, scad.CONCURRENCY, scad._slots.size) == (30, 2, 2)
    assert scad.cpu_seconds() == 120
    scad.configure()                                   # None = the environment's values
    assert (scad.TIMEOUT, scad.CONCURRENCY, scad.cpu_seconds()) == (60, 3, 240)


def test_explicit_cpu_seconds_do_not_follow_the_timeout(scad, monkeypatch):
    monkeypatch.setattr(scad, "CPU_SECONDS_ENV", 500)
    scad.configure(10, 1)
    assert scad.cpu_seconds() == 500


@pytest.mark.parametrize("timeout, concurrency", [
    (61, 1), (1, 1), (30, 4), (30, 0), (30, 1.5), ("30", 1), (True, 1), (float("nan"), 1)])
def test_configure_refuses_values_outside_the_caps(scad, timeout, concurrency):
    with pytest.raises(ValueError):
        scad.configure(timeout, concurrency)
    assert (scad.TIMEOUT, scad.CONCURRENCY) == (60, 3)     # nothing changed


def test_clamp_cuts_saved_values_down_to_lowered_caps(scad):
    scad.configure(600, 8, clamp=True)
    assert (scad.TIMEOUT, scad.CONCURRENCY) == (60, 3)


def test_build_uses_the_timeout_in_effect_when_it_starts(scad, monkeypatch):
    seen = []
    monkeypatch.setattr(scad, "available", lambda: True)
    monkeypatch.setattr(scad, "_build", lambda code, ov, parts, timeout, meshes=None: seen.append(timeout) or {})
    scad.build({"code": "cube(1);"})
    scad.configure(20, 3)
    scad.build({"code": "cube(1);"})
    assert seen == [60, 20]


# ---------------------------------------------------------------- resizable slots

def test_slots_grow_lets_waiting_builds_start():
    s = Slots(1)
    assert s.acquire(0)
    got = threading.Event()
    t = threading.Thread(target=lambda: s.acquire(5) and got.set())
    t.start()
    time.sleep(0.05)
    assert not got.is_set()
    s.resize(2)                                        # wakes the waiter without a release
    assert got.wait(2)
    t.join()
    assert s.used == 2


def test_slots_shrink_keeps_running_builds_and_waits_for_them():
    s = Slots(3)
    assert all(s.acquire(0) for _ in range(3))
    s.resize(1)
    assert s.used == 3                                 # nobody loses a slot
    assert not s.acquire(0.01)
    s.release()
    s.release()
    assert not s.acquire(0.01)                         # 1 running = the new size
    s.release()
    assert s.acquire(0) and s.used == 1


def test_slots_refuse_a_release_too_many():
    s = Slots(2)
    s.acquire(0)
    s.release()
    with pytest.raises(ValueError):
        s.release()
    assert s.used == 0


def test_resizing_while_builds_wait(scad, monkeypatch):
    """Many builds through build() while the size grows and shrinks again: never more at once
    than the largest size, and every slot is given back."""
    monkeypatch.setattr(scad, "available", lambda: True)
    lock, now, peak = threading.Lock(), [0], [0]

    def fake(*_):
        with lock:
            now[0] += 1
            peak[0] = max(peak[0], now[0])
        time.sleep(0.02)
        with lock:
            now[0] -= 1
        return {}
    monkeypatch.setattr(scad, "_build", fake)
    scad.configure(60, 1)
    threads = [threading.Thread(target=scad.build, args=({"code": "cube(1);"},)) for _ in range(12)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    scad.configure(60, 3)
    time.sleep(0.05)
    scad.configure(60, 2)
    for t in threads:
        t.join()
    assert peak[0] <= 3 and scad._slots.used == 0


# ---------------------------------------------------------------- the admin endpoint

def test_get_shows_effective_values_caps_and_cpu(admin):
    o = admin.get("/api/admin/limits").json()["openscad"]
    assert (o["timeout"], o["timeout_cap"], o["concurrency"], o["concurrency_cap"]) == (60, 60, 3, 3)
    assert o["cpu_seconds"] == 240 and o["changed"] == {"timeout": False, "concurrency": False}


def test_admin_lowers_limits_live_and_they_are_saved(admin, db, scad):
    r = admin.post("/api/admin/limits", json={"timeout": 30, "concurrency": 2})
    assert r.status_code == 200, r.text
    o = r.json()["openscad"]
    assert (o["timeout"], o["concurrency"], o["cpu_seconds"]) == (30, 2, 120)
    assert o["changed"] == {"timeout": True, "concurrency": True} and o["updated_by"]
    assert (scad.TIMEOUT, scad._slots.size) == (30, 2)            # in effect, no restart
    row = db.one("SELECT value FROM server_settings WHERE key = 'openscad'")
    assert row["value"] == {"timeout": 30, "concurrency": 2}


def test_partial_post_and_reset_to_the_cap(admin, db, scad):
    admin.post("/api/admin/limits", json={"timeout": 30, "concurrency": 2})
    o = admin.post("/api/admin/limits", json={"concurrency": None}).json()["openscad"]
    assert (o["timeout"], o["concurrency"]) == (30, 3)
    o = admin.post("/api/admin/limits", json={"timeout": None}).json()["openscad"]
    assert (o["timeout"], o["concurrency"]) == (60, 3)
    assert db.one("SELECT 1 AS x FROM server_settings WHERE key = 'openscad'") is None


@pytest.mark.parametrize("body", [{"timeout": 61}, {"concurrency": 4}, {"timeout": 2},
                                  {"concurrency": 0}, {"timeout": "x"}, {"memory": 9000}, [1]])
def test_values_above_the_env_caps_are_refused(admin, db, scad, body):
    r = admin.post("/api/admin/limits", json=body)
    assert r.status_code == 400 and r.json()["error"]
    assert (scad.TIMEOUT, scad.CONCURRENCY) == (60, 3)
    assert db.one("SELECT 1 AS x FROM server_settings") is None


def test_members_cannot_read_or_change_limits(admin, scad):
    joe = server.oauth.state.create_user("Joe", "joe@example.com")
    c = as_user(joe)
    assert c.get("/api/admin/limits").status_code == 403
    assert c.post("/api/admin/limits", json={"timeout": 10}).status_code == 403
    anon = TestClient(server.app, follow_redirects=False)
    assert anon.post("/api/admin/limits", json={"timeout": 10}).status_code in (302, 401, 403)
    assert scad.TIMEOUT == 60


def test_saved_limits_apply_at_startup_within_the_caps(admin, scad, monkeypatch):
    admin.post("/api/admin/limits", json={"timeout": 30, "concurrency": 2})
    scad.configure()                                   # as after a restart
    server._apply_openscad_limits()
    assert (scad.TIMEOUT, scad.CONCURRENCY) == (30, 2)
    monkeypatch.setattr(scad, "TIMEOUT_CAP", 20.0)     # the environment was lowered since
    monkeypatch.setattr(scad, "CONCURRENCY_CAP", 1)
    server._apply_openscad_limits()
    assert (scad.TIMEOUT, scad.CONCURRENCY) == (20, 1)


def test_load_page_has_the_controls(admin):
    html = admin.get("/admin/load").text
    assert 'id="scad-timeout"' in html and 'id="scad-concurrency"' in html and "/api/admin/limits" in html
