"""Open signup (passkey + Turnstile + rate limit), per-user limits, personal tokens and deleting
an account."""
import asyncio
import base64
from types import SimpleNamespace

import pytest
import trimesh
from starlette.testclient import TestClient

import auth
import server
from auth import RateLimiter
from test_auth import SoftAuthenticator

BOX = {"root": {"shape": "box", "size": [20, 20, 10]}}


def ctx(user):
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(state=SimpleNamespace(user=user))))


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    monkeypatch.setattr(server, "SIGNUP", True)
    monkeypatch.setattr(server, "TURNSTILE_SECRET", "")
    monkeypatch.setattr(server, "signups", RateLimiter(5, 3600))
    return TestClient(server.app, follow_redirects=False)


def signup(client, dev, name="Joe", **extra):
    o = client.post("/auth/signup/options", json={"display_name": name, **extra})
    if o.status_code != 200:
        return o
    o = o.json()
    assert o["options"]["authenticatorSelection"]["residentKey"] == "required"
    return client.post("/auth/signup/verify", json={"flow": o["flow"], "name": "Mac",
                                                    "credential": dev.register(o["options"])})


def login_with(client, dev):
    o = client.post("/auth/passkey/login/options", json={}).json()
    return client.post("/auth/passkey/login/verify", json={"flow": o["flow"], "credential": dev.login(o["options"])})


# ---- signup

def test_config_tells_the_login_page_what_is_on(client, monkeypatch):
    monkeypatch.setattr(server, "TURNSTILE_SITE_KEY", "site")
    assert client.get("/auth/config").json() == {"signup": True, "turnstile_site_key": "site",
                                                 "providers": []}
    monkeypatch.setattr(server, "SIGNUP", False)
    assert client.get("/auth/config").json()["signup"] is False
    assert client.get("/auth/config").json()["turnstile_site_key"] == ""


def test_signup_is_closed_by_default(client, monkeypatch):
    monkeypatch.setattr(server, "SIGNUP", False)
    assert client.post("/auth/signup/options", json={"display_name": "Joe"}).status_code == 403
    assert client.post("/auth/signup/verify", json={}).status_code == 403


def test_signup_creates_a_member_with_an_empty_library_and_passkey_login(client):
    dev = SoftAuthenticator()
    r = signup(client, dev, "  Joe  ")
    assert r.status_code == 200, r.text
    assert r.json()["user"]["display_name"] == "Joe" and r.json()["user"]["role"] == "member"
    me = client.get("/api/auth").json()
    assert me["user"]["display_name"] == "Joe" and me["user"]["email"] is None
    assert [p["name"] for p in me["passkeys"]] == ["Mac"]
    assert me["usage"] == {"models": 0, "disk_mb": 0.0, "max_models": 100, "max_disk_mb": 500}
    assert client.get("/api/models").json()["models"] == []
    assert client.get("/api/settings").status_code == 403  # not an admin

    client.post("/auth/logout", json={})
    client.cookies.clear()
    assert login_with(client, dev).status_code == 200
    assert client.get("/api/auth").json()["user"]["id"] == me["user"]["id"]


def test_signup_validates_the_name_and_the_flow(client):
    assert client.post("/auth/signup/options", json={"display_name": " "}).status_code == 400
    assert client.post("/auth/signup/options", json={"display_name": "x" * 81}).status_code == 400
    assert client.post("/auth/signup/verify", json={"flow": "nope"}).status_code == 400
    # a login flow cannot finish a signup
    o = client.post("/auth/passkey/login/options", json={}).json()
    assert client.post("/auth/signup/verify", json={"flow": o["flow"]}).status_code == 400


def test_signup_with_a_passkey_that_belongs_to_someone_else_creates_nothing(client, db):
    dev = SoftAuthenticator()
    assert signup(client, dev).status_code == 200
    client.cookies.clear()
    before = db.one("SELECT count(*) AS n FROM users")["n"]
    r = signup(client, dev, "Copycat")  # same credential id again
    assert r.status_code == 409 and db.one("SELECT count(*) AS n FROM users")["n"] == before


def test_turnstile_is_checked_when_configured(client, monkeypatch):
    monkeypatch.setattr(server, "TURNSTILE_SECRET", "secret-key")
    seen = []
    monkeypatch.setattr(server, "verify_turnstile",
                        lambda secret, token, ip: seen.append((secret, token)) or token == "good")
    assert signup(client, SoftAuthenticator(), turnstile="bad").status_code == 400
    assert signup(client, SoftAuthenticator(), turnstile="good").status_code == 200
    assert seen == [("secret-key", "bad"), ("secret-key", "good")]


def test_verify_turnstile(monkeypatch):
    assert auth.verify_turnstile("", "") is True  # off without a secret
    assert auth.verify_turnstile("s", "") is False
    calls = []

    class Resp:
        def __init__(self, ok):
            self.ok = ok

        def json(self):
            return {"success": self.ok}
    monkeypatch.setattr(auth.httpx, "post", lambda url, data, timeout: calls.append(data) or Resp(data["response"] == "t"))
    assert auth.verify_turnstile("s", "t", "1.2.3.4") is True
    assert auth.verify_turnstile("s", "x") is False
    assert calls[0] == {"secret": "s", "response": "t", "remoteip": "1.2.3.4"}

    def boom(*a, **k):
        raise auth.httpx.ConnectError("down")
    monkeypatch.setattr(auth.httpx, "post", boom)
    assert auth.verify_turnstile("s", "t") is False


def test_signups_are_rate_limited_per_ip(client, monkeypatch):
    monkeypatch.setattr(server, "signups", RateLimiter(1, 3600))
    assert signup(client, SoftAuthenticator(), "A").status_code == 200
    client.cookies.clear()
    r = signup(client, SoftAuthenticator(), "B")
    assert r.status_code == 429 and "too many" in r.json()["error"]


def test_rate_limiter_window(monkeypatch):
    rl = RateLimiter(2, 10)
    assert rl.hit("ip") and rl.hit("ip") and not rl.hit("ip") and not rl.allowed("ip")
    assert rl.allowed("other")
    later = auth.time.time() + 11
    monkeypatch.setattr(auth.time, "time", lambda: later)
    assert rl.allowed("ip")


def test_client_ip_only_trusts_the_proxy_when_told(monkeypatch):
    req = SimpleNamespace(headers={"x-forwarded-for": "6.6.6.6, 1.2.3.4"},
                          client=SimpleNamespace(host="10.0.0.1"))
    monkeypatch.setattr(server, "TRUSTED_PROXY", False)
    assert server._client_ip(req) == "10.0.0.1"
    monkeypatch.setattr(server, "TRUSTED_PROXY", True)
    assert server._client_ip(req) == "1.2.3.4"  # the address the proxy saw, not the client's claim


def test_signup_needs_mcp_token(out_dir, monkeypatch):
    monkeypatch.setattr(server, "SIGNUP", True)
    monkeypatch.setattr(server, "TOKEN", "")
    with pytest.raises(RuntimeError, match="needs MCP_TOKEN"):
        server.startup()


def test_privacy_page_is_public(client):
    r = client.get("/privacy")
    assert r.status_code == 200 and "Privacy" in r.text


# ---- limits

@pytest.fixture
def joe(client):
    return server.oauth.state.create_user("Joe")


def test_model_limit_for_members_only(joe, monkeypatch):
    monkeypatch.setattr(server, "MAX_MODELS_PER_USER", 1)
    run(server.create_model("one", BOX, ctx=ctx(joe)))
    with pytest.raises(server.LimitError, match="limit of 1 models"):
        run(server.create_model("two", BOX, ctx=ctx(joe)))
    with pytest.raises(server.LimitError):
        run(server.duplicate_model("one", "copy", ctx=ctx(joe)))
    run(server.update_model("one", changes={}, ctx=ctx(joe)))  # changing what you have is fine
    admin = server._admin()
    run(server.create_model("a1", BOX, ctx=ctx(admin)))
    run(server.create_model("a2", BOX, ctx=ctx(admin)))


def test_disk_limit_blocks_new_data(joe, client, monkeypatch):
    run(server.create_model("one", BOX, ctx=ctx(joe)))
    monkeypatch.setattr(server, "MAX_DISK_PER_USER", 100)
    stl = base64.b64encode(trimesh.creation.box().export(file_type="stl")).decode()
    for call in (lambda: server.import_file("x", data_base64=stl, ctx=ctx(joe)),
                 lambda: run(server.update_model("one", changes={}, ctx=ctx(joe))),
                 lambda: run(server.create_model("two", BOX, ctx=ctx(joe)))):
        with pytest.raises(server.LimitError, match="storage"):
            call()
    c = TestClient(server.app)
    c.cookies.set(server.SESSION_COOKIE, server.oauth.state.create_session(joe["id"]))
    r = c.put("/upload/x.stl", content=trimesh.creation.box().export(file_type="stl"))
    assert r.status_code == 400 and "storage" in r.json()["error"]
    assert c.get("/api/auth").json()["usage"]["models"] == 1


def test_script_models_are_for_admins(joe, monkeypatch):
    monkeypatch.setattr(server.store, "allow_script", True)
    with pytest.raises(PermissionError, match="admins only"):
        run(server.create_from_script("s", "result = None", ctx=ctx(joe)))


# ---- personal tokens and the account

def logged_in(client, user):
    client.cookies.set(server.SESSION_COOKIE, server.oauth.state.create_session(user["id"]))
    return client


def test_personal_tokens_api(client, joe):
    logged_in(client, joe)
    r = client.post("/api/auth/tokens", json={"name": "Claude Code"}).json()
    assert r["token"].startswith("stlp_") and r["name"] == "Claude Code"
    listed = client.get("/api/auth").json()["tokens"]
    assert [t["name"] for t in listed] == ["Claude Code"] and "token" not in listed[0]
    bare = TestClient(server.app)
    assert bare.get("/api/models", headers={"Authorization": f"Bearer {r['token']}"}).status_code == 200
    assert client.delete(f"/api/auth/tokens/{r['id']}").json() == {"ok": True}
    assert client.delete(f"/api/auth/tokens/{r['id']}").status_code == 404
    assert bare.get("/api/models", headers={"Authorization": f"Bearer {r['token']}"}).status_code == 401


def test_rename(client, joe):
    logged_in(client, joe)
    assert client.patch("/api/auth/me", json={"display_name": " Joseph "}).json()["user"]["display_name"] == "Joseph"
    assert client.patch("/api/auth/me", json={"display_name": ""}).status_code == 400


def test_delete_account_removes_everything(client, joe, db):
    run(server.create_model("mine", BOX, ctx=ctx(joe)))
    stl = base64.b64encode(trimesh.creation.box().export(file_type="stl")).decode()
    server.import_file("part", data_base64=stl, ctx=ctx(joe))
    raw, _ = server.oauth.state.create_personal_token(joe["id"], "cli")
    logged_in(client, joe)
    cookie = client.cookies[server.SESSION_COOKIE]
    assert client.request("DELETE", "/api/auth/me", json={}).status_code == 400  # needs confirmation
    r = client.request("DELETE", "/api/auth/me", json={"confirm": "delete"})
    assert r.json() == {"ok": True, "deleted_models": 1}
    assert db.one("SELECT 1 FROM users WHERE id = %s", (joe["id"],)) is None
    assert server.store.list() == [] and not (server.OUT / "files" / joe["id"]).exists()
    assert not (server.store.dir / "mine").exists()
    c = TestClient(server.app)
    c.cookies.set(server.SESSION_COOKIE, cookie)
    assert c.get("/api/models").status_code == 401
    assert c.get("/api/models", headers={"Authorization": f"Bearer {raw}"}).status_code == 401


def test_bootstrap_admin_cannot_delete_itself(client):
    logged_in(client, server._admin())
    assert client.request("DELETE", "/api/auth/me", json={"confirm": "delete"}).status_code == 403


def test_library_page_hides_admin_link_until_it_knows_the_role(client, joe):
    page = logged_in(client, joe).get("/").text
    assert 'id="adminlink" hidden' in page and '$("adminlink").hidden = u.role !== "admin"' in page
    assert 'id="wizmore"' in page and 'id="tokadd"' in page and 'id="delacct"' in page
