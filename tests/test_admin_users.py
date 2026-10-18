"""The admin's users list: who is there, and changing role, disabling, logging out and deleting."""
import asyncio
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

import auth
import server

BOX = {"root": {"shape": "box", "size": [20, 20, 10]}}


def ctx(user):
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(state=SimpleNamespace(user=user))))


def as_user(user):
    c = TestClient(server.app, follow_redirects=False)
    c.cookies.set(server.SESSION_COOKIE, server.oauth.state.create_session(user["id"]))
    return c


@pytest.fixture
def world(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    st = server.oauth.state
    admin = server._admin()
    joe = st.create_user("Joe", "joe@example.com")
    st.add_passkey(joe["id"], "cred", "pk", 0, "Mac")
    st.add_identity(joe["id"], "google", "g-1", "joe@example.com")
    asyncio.run(server.create_model("joes-box", BOX, ctx=ctx(joe)))
    return SimpleNamespace(admin=admin, joe=joe, client=as_user(admin))


def post(c, uid, **body):
    return c.post(f"/api/admin/users/{uid}", json=body)


def test_list_shows_every_user_with_counts(world):
    users = {u["display_name"]: u for u in world.client.get("/api/admin/users").json()["users"]}
    joe = users["Joe"]
    assert joe["email"] == "joe@example.com" and joe["role"] == "member" and not joe["disabled"]
    assert joe["models"] == 1 and joe["passkeys"] == 1 and joe["providers"] == ["google"]
    assert joe["disk_mb"] >= 0 and joe["tokens"] == 0 and joe["apps"] == 0
    assert users["admin"]["bootstrap"] and users["admin"]["providers"] == []


def test_members_cannot_reach_the_users_admin(world):
    c = as_user(world.joe)
    assert c.get("/api/admin/users").status_code == 403
    assert post(c, world.admin["id"], action="disable").status_code == 403
    assert c.get("/admin/users").status_code == 403


def test_make_admin_and_back(world):
    assert post(world.client, world.joe["id"], action="role", role="admin").json()["user"]["role"] == "admin"
    assert as_user(world.joe).get("/api/admin/users").status_code == 200
    assert post(world.client, world.joe["id"], action="role", role="member").json()["user"]["role"] == "member"
    assert post(world.client, world.joe["id"], action="role", role="owner").status_code == 400


def test_disable_locks_out_and_enable_lets_back_in(world):
    c = as_user(world.joe)
    raw, _ = server.oauth.state.create_personal_token(world.joe["id"], "cli")
    assert post(world.client, world.joe["id"], action="disable").json()["user"] is not None
    assert c.get("/api/models").status_code == 401
    assert TestClient(server.app).get("/api/models", headers={"Authorization": f"Bearer {raw}"}).status_code == 401
    assert server.oauth.state.passkey("cred") is None
    post(world.client, world.joe["id"], action="enable")
    assert server.oauth.state.passkey("cred") is not None
    assert as_user(world.joe).get("/api/models").status_code == 200


def test_logout_everywhere(world):
    c = as_user(world.joe)
    raw, _ = server.oauth.state.create_personal_token(world.joe["id"], "cli")
    assert post(world.client, world.joe["id"], action="logout").json()["ok"]
    assert c.get("/api/models").status_code == 401
    assert TestClient(server.app).get("/api/models", headers={"Authorization": f"Bearer {raw}"}).status_code == 401
    assert server.oauth.state.user(world.joe["id"])["disabled"] is False


def test_delete_user_with_everything(world, db):
    assert world.client.request("DELETE", f"/api/admin/users/{world.joe['id']}", json={}).status_code == 400
    r = world.client.request("DELETE", f"/api/admin/users/{world.joe['id']}", json={"confirm": "delete"})
    assert r.json() == {"ok": True, "deleted_models": 1}
    assert server.oauth.state.user(world.joe["id"]) is None and server.store.list() == []
    assert db.one("SELECT count(*) AS n FROM identities")["n"] == 0
    assert post(world.client, world.joe["id"], action="disable").status_code == 404


def test_bootstrap_admin_and_yourself_are_protected(world):
    admin_id = world.admin["id"]
    for body in ({"action": "disable"}, {"action": "role", "role": "member"}):
        assert post(world.client, admin_id, **body).status_code == 403
    assert world.client.request("DELETE", f"/api/admin/users/{admin_id}", json={"confirm": "delete"}).status_code == 403
    # a second admin cannot demote, disable or delete themself either
    post(world.client, world.joe["id"], action="role", role="admin")
    joe = as_user(world.joe)
    assert post(joe, world.joe["id"], action="disable").status_code == 403
    assert post(joe, world.joe["id"], action="role", role="member").status_code == 403
    assert post(joe, world.joe["id"], action="logout").status_code == 200  # that one is fine
    assert post(world.client, world.joe["id"], action="nonsense").status_code == 400


def test_users_page_is_an_admin_tab(world):
    page = world.client.get("/admin/users").text
    assert 'href="/admin/users" aria-current="page"' in page and "/api/admin/users" in page
    for other in ("/admin", "/admin/design"):
        assert 'href="/admin/users"' in world.client.get(other).text


def test_login_tracking_shows_country_logins_and_time(world, monkeypatch):
    st = server.oauth.state
    raw = st.create_session(world.joe["id"], country="SE", user_agent="pytest")
    st.create_session(world.joe["id"], country="DE")
    st.db.execute("UPDATE login_events SET at = at - 600 WHERE country = 'SE'")
    st.db.execute("UPDATE sessions SET last_seen = last_seen - 7200 WHERE hash = %s",
                  (auth._hash(raw),))
    joe = {u["display_name"]: u for u in world.client.get("/api/admin/users").json()["users"]}["Joe"]
    assert joe["logins"] == 2 and joe["countries"] == ["DE", "SE"] and joe["seconds"] >= 600
    logins = world.client.get(f"/api/admin/users/{world.joe['id']}/logins").json()["logins"]
    assert [r["country"] for r in logins] == ["DE", "SE"]


def test_country_comes_from_cloudflare_header_only_when_enabled(monkeypatch):
    req = SimpleNamespace(headers={"cf-ipcountry": "se"}, client=None)
    monkeypatch.setattr(server, "GEOIP_DB", "")
    monkeypatch.setattr(server, "CF_COUNTRY", True)
    assert server._country(req) == "SE"
    req.headers["cf-ipcountry"] = "XX"
    assert server._country(req) is None
    monkeypatch.setattr(server, "CF_COUNTRY", False)  # not behind Cloudflare: the header is spoofable
    req.headers["cf-ipcountry"] = "SE"
    assert server._country(req) is None


def test_old_login_events_are_purged(world, monkeypatch):
    st = server.oauth.state
    st.create_session(world.joe["id"], country="SE")
    st.db.execute("UPDATE login_events SET at = at - %s", (auth.LOGIN_HISTORY + 60,))
    st.create_session(world.joe["id"], country="DE")
    assert [r["country"] for r in st.recent_logins(world.joe["id"])] == ["DE"]


def test_login_history_is_admin_only(world):
    assert as_user(world.joe).get(f"/api/admin/users/{world.joe['id']}/logins").status_code == 403
