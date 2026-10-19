"""The admin Load page: CPU and memory per online user and per running build job."""
import subprocess
import sys
import threading
import time

import pytest
from starlette.testclient import TestClient

import jobs
import server


@pytest.fixture
def client(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "")
    return TestClient(server.app, follow_redirects=False)


def test_load_page_and_api(client):
    assert "Load" in client.get("/admin/load").text
    d = client.get("/api/admin/load").json()
    assert d["system"]["cpus"] >= 1
    assert d["server"]["pid"] > 0
    assert d["limits"]["workers"] >= 1
    assert isinstance(d["users"], list)


def test_job_counts_its_worker_process_cpu_and_memory():
    job = jobs.start({"id": "u1", "display_name": "Ann"}, "create", None)
    seen = {}

    def burn():
        p = subprocess.Popen([sys.executable, "-c", "x = bytearray(30_000_000)\nwhile True: pass"])
        jobs.add_pid(p.pid)
        try:
            time.sleep(0.4)
            jobs.snapshot()                 # first reading: the baseline
            time.sleep(0.6)
            seen.update(next(j for j in jobs.snapshot()["jobs"] if j["id"] == job.id))
        finally:
            jobs.remove_pid(p.pid)
            p.kill()
            p.wait()
    try:
        t = threading.Thread(target=jobs.run_in, args=(job, burn))
        t.start()
        t.join()
    finally:
        jobs.finish(job)
    assert seen["state"] == "running" and seen["user"] == "u1"
    assert len(seen["processes"]) == 1
    assert seen["cpu"] is not None and seen["cpu"] > 30          # the busy loop
    assert seen["mem_mb"] > 25                                     # the 30 MB bytearray
    assert all(j["id"] != job.id for j in jobs.snapshot()["jobs"])


def test_a_build_shows_up_for_its_user_while_it_runs(client, monkeypatch):
    lib = server._lib(None)
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(5)
        return "ok"

    result = {}
    t = threading.Thread(target=lambda: result.update(r=server.anyio.run(lambda: lib.build(slow, job="test"))))
    t.start()
    try:
        assert started.wait(5)
        d = client.get("/api/admin/load").json()
        me = next(u for u in d["users"] if u["id"] == lib.user["id"])
        assert [j["label"] for j in me["jobs"]] == ["test"]
        assert me["jobs"][0]["state"] == "running"
    finally:
        release.set()
        t.join()
    assert result["r"] == "ok"
    d = client.get("/api/admin/load").json()
    assert all(not u["jobs"] for u in d["users"])


def test_load_api_is_admin_only(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    c = TestClient(server.app, follow_redirects=False)
    assert c.get("/api/admin/load").status_code == 401
