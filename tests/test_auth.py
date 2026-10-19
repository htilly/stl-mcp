import asyncio
import base64
import hashlib
import json
import os
import stat
import time
from urllib.parse import parse_qs, urlparse

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.testclient import TestClient

import auth
import server
from auth import AuthState, Expiring, OAuthProvider

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


# ---- Expiring

def test_expiring_pop_is_one_time_and_entries_expire(monkeypatch):
    e = Expiring(ttl=10)
    e.put("a", 1)
    assert e.get("a") == 1 and e.pop("a") == 1 and e.pop("a") is None
    e.put("b", 2)
    later = time.time() + 11
    monkeypatch.setattr(auth.time, "time", lambda: later)
    assert e.get("b") is None and e.pop("b") is None


# ---- AuthState (Postgres)

@pytest.fixture
def st(db):
    return AuthState(db)


def test_migrations_are_idempotent(db):
    assert db.migrate() == []


def test_sessions_create_lookup_expire_and_delete(st):
    u = st.create_user("Joe", "Joe@Example.com")
    assert u["email"] == "joe@example.com" and u["role"] == "member"
    raw = st.create_session(u["id"])
    assert st.session_user(raw)["id"] == u["id"]
    assert st.user(u["id"])["last_login"]
    assert st.session_user("nope") is None and st.session_user("") is None
    expired = st.create_session(u["id"], ttl=-1)
    assert st.session_user(expired) is None
    st.delete_session(raw)
    assert st.session_user(raw) is None


def test_disabled_user_is_locked_out_everywhere(st, db):
    u = st.create_user("Joe")
    raw = st.create_session(u["id"])
    st.add_passkey(u["id"], "cred", "pk", 0, "Mac")
    tok, _ = st.create_personal_token(u["id"], "cli")
    db.execute("UPDATE users SET disabled = true WHERE id = %s", (u["id"],))
    assert st.session_user(raw) is None
    assert st.passkey("cred") is None
    assert st.personal_token_user(tok) is None


def test_bootstrap_admin_is_created_once(st):
    a = st.bootstrap_admin("Boss@Example.com")
    assert a["role"] == "admin" and a["bootstrap"] and a["email"] == "boss@example.com"
    assert st.bootstrap_admin()["id"] == a["id"]


def test_passkeys_add_touch_delete(st):
    u = st.create_user("A")
    st.add_passkey(u["id"], "id1", "pk", 0, "  iPhone  ")
    st.add_passkey(u["id"], "id1", "pk2", 0, "replaced")  # same credential id replaces
    assert [p["name"] for p in st.passkeys(u["id"])] == ["replaced"]
    st.touch_passkey("id1", 7)
    p = st.passkey("id1")
    assert p["sign_count"] == 7 and p["last_used"] and p["user_id"] == u["id"]
    assert st.delete_passkey("id1", u["id"]) and not st.delete_passkey("id1", u["id"])
    assert st.passkey("id1") is None


def test_passkey_of_another_user_is_not_taken_over_or_deleted(st):
    a, b = st.create_user("A"), st.create_user("B")
    st.add_passkey(a["id"], "id1", "pk", 0, "A's")
    assert st.add_passkey(b["id"], "id1", "evil", 0, "B's") is None
    assert not st.delete_passkey("id1", b["id"])
    assert st.passkey("id1")["public_key"] == "pk"


def test_only_token_hashes_are_stored(st, db):
    u = st.create_user("A")
    p = OAuthProvider(st)
    st.put_client({"client_id": "c1", "redirect_uris": [REDIRECT]})
    tok = p._issue("c1", ["stl"], None, u["id"])
    hashes = {r["hash"] for r in db.all("SELECT hash FROM oauth_tokens")}
    assert tok.access_token not in hashes and tok.refresh_token not in hashes
    assert hashlib.sha256(tok.access_token.encode()).hexdigest() in hashes
    assert p.access_token_user(tok.access_token)["id"] == u["id"]


def test_expired_tokens_are_pruned(st, db):
    u = st.create_user("A")
    st.put_client({"client_id": "c", "redirect_uris": [REDIRECT]})
    base = {"client_id": "c", "user_id": u["id"], "scopes": ["stl"], "resource": None,
            "issued_at": 0, "pair": None}
    st.add_tokens([{**base, "hash": "old", "kind": "access", "expires_at": 1}])
    st.add_tokens([{**base, "hash": "new", "kind": "access", "expires_at": time.time() + 60}])
    assert [r["hash"] for r in db.all("SELECT hash FROM oauth_tokens")] == ["new"]


def test_client_registration_is_capped_but_keeps_clients_with_tokens(st, monkeypatch):
    monkeypatch.setattr(auth, "MAX_CLIENTS", 3)
    p = OAuthProvider(st)
    u = st.create_user("A")
    from mcp.shared.auth import OAuthClientInformationFull

    def reg(cid, t):
        asyncio.run(p.register_client(OAuthClientInformationFull(
            client_id=cid, redirect_uris=[REDIRECT], client_id_issued_at=t)))
    reg("keep", 1)
    p._issue("keep", ["stl"], None, u["id"])
    for i in range(2, 7):
        reg(f"c{i}", i)
    clients = {r["client_id"] for r in st.db.all("SELECT client_id FROM oauth_clients")}
    assert "keep" in clients and "c6" in clients and len(clients) == 4  # 3 idle + the one in use


def test_personal_tokens(st):
    u = st.create_user("A")
    raw, row = st.create_personal_token(u["id"], "laptop")
    assert raw.startswith("stlp_") and row["name"] == "laptop"
    assert st.personal_token_user(raw)["id"] == u["id"]
    assert st.personal_token_user("stlp_nope") is None and st.personal_token_user("x") is None
    assert [t["name"] for t in st.personal_tokens(u["id"])] == ["laptop"]
    other = st.create_user("B")
    assert not st.delete_personal_token(row["id"], other["id"])
    assert st.delete_personal_token(row["id"], u["id"])
    assert st.personal_token_user(raw) is None


def test_legacy_auth_json_is_imported_once(st, tmp_path):
    admin = st.bootstrap_admin()
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({
        "passkeys": [{"id": "cred", "public_key": "pk", "sign_count": 3, "name": "Mac",
                      "created": 1.0, "last_used": None}],
        "clients": {"c1": {"client_id": "c1", "redirect_uris": [REDIRECT], "client_name": "Claude"}},
        "tokens": {"h1": {"kind": "access", "client_id": "c1", "scopes": ["stl"], "issued_at": 1,
                          "expires_at": time.time() + 60, "pair": "h2"},
                   "gone": {"kind": "access", "client_id": "c1", "scopes": ["stl"], "issued_at": 1,
                            "expires_at": 1}}}))
    assert st.import_legacy(path, admin["id"])
    assert not path.exists() and (tmp_path / "auth.json.imported").exists()
    assert st.passkey("cred")["user_id"] == admin["id"]
    assert [c["name"] for c in st.clients(admin["id"])] == ["Claude"]
    assert not st.import_legacy(path, admin["id"])  # nothing left to import


# ---- HTTP fixtures

@pytest.fixture
def client(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    return TestClient(server.app, follow_redirects=False)


def login(client, user=None):
    """Logs the client in as user (default: the bootstrap admin); returns the user."""
    user = user or server._admin()
    client.cookies.set(server.SESSION_COOKIE, server.oauth.state.create_session(user["id"]))
    return user


def register_client(client, **extra):
    r = client.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "Claude",
                                       "token_endpoint_auth_method": "none", **extra})
    assert r.status_code == 201, r.text
    return r.json()


def pkce():
    verifier = b64u(os.urandom(32))
    return verifier, b64u(hashlib.sha256(verifier.encode()).digest())


def start_authorize(client, c, challenge, state="xyz"):
    r = client.get("/authorize", params={
        "response_type": "code", "client_id": c["client_id"], "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state})
    assert r.status_code in (302, 307), r.text
    loc = r.headers["location"]
    assert loc.startswith("/oauth/consent?req=")
    return loc


def get_code(client):
    c = register_client(client)
    verifier, challenge = pkce()
    consent = start_authorize(client, c, challenge)
    login(client)
    req = parse_qs(urlparse(consent).query)["req"][0]
    r = client.post("/oauth/consent", data={"req": req, "decision": "allow"})
    assert r.status_code == 303
    q = parse_qs(urlparse(r.headers["location"]).query)
    return c, verifier, q


def exchange(client, c, verifier, code):
    return client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                       "client_id": c["client_id"], "redirect_uri": REDIRECT,
                                       "code_verifier": verifier})


# ---- discovery

def test_metadata_is_public(client):
    r = client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    meta = r.json()
    assert meta["registration_endpoint"].endswith("/register") and "S256" in meta["code_challenge_methods_supported"]
    for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"):
        prm = client.get(path).json()
        assert prm["resource"].endswith("/mcp")


def test_mcp_401_points_to_resource_metadata(client):
    r = client.post("/mcp", json={})
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]
    assert "/.well-known/oauth-protected-resource/mcp" in r.headers["www-authenticate"]


def test_no_www_authenticate_without_oauth(client, monkeypatch):
    monkeypatch.setattr(server, "OAUTH_ROUTES", [])
    r = client.post("/mcp", json={})
    assert r.status_code == 401 and "www-authenticate" not in r.headers
    # and the OAuth endpoints are no longer public
    assert client.post("/register", json={}).status_code == 401


def test_oauth_routes_need_https_or_localhost(monkeypatch):
    monkeypatch.setattr(server, "PUBLIC_URL", "http://stl.example.com")
    assert server._oauth_routes() == []
    monkeypatch.setattr(server, "PUBLIC_URL", "https://stl.example.com")
    assert len(server._oauth_routes()) == 7
    monkeypatch.setattr(server, "OAUTH", False)
    assert server._oauth_routes() == []


# ---- full flow

def test_consent_requires_login(client):
    c = register_client(client)
    consent = start_authorize(client, c, pkce()[1])
    r = client.get(consent, headers={"accept": "text/html"})
    assert r.status_code == 401 and "passkey" in r.text.lower()


def test_consent_page_names_client_and_destination(client):
    c = register_client(client, client_name="<b>Evil</b>")
    consent = start_authorize(client, c, pkce()[1])
    login(client)
    r = client.get(consent)
    assert r.status_code == 200
    assert "&lt;b&gt;Evil&lt;/b&gt;" in r.text and "<b>Evil" not in r.text
    assert "sent back to <strong>https://claude.ai</strong>" in r.text
    assert r.headers["x-frame-options"] == "DENY"


def test_authorization_code_flow_issues_working_tokens(client):
    c, verifier, q = get_code(client)
    assert q["state"] == ["xyz"]
    r = exchange(client, c, verifier, q["code"][0])
    assert r.status_code == 200, r.text
    tok = r.json()
    assert tok["token_type"] == "Bearer" and tok["refresh_token"] and tok["scope"] == "stl"
    client.cookies.clear()
    h = {"Authorization": f"Bearer {tok['access_token']}"}
    assert client.get("/api/models", headers=h).status_code == 200
    assert client.get("/api/models", headers={"Authorization": "Bearer nope"}).status_code == 401
    # the code is one-time use
    assert exchange(client, c, verifier, q["code"][0]).json()["error"] == "invalid_grant"


def test_wrong_pkce_verifier_is_refused(client):
    c, _verifier, q = get_code(client)
    r = exchange(client, c, "wrong" * 10, q["code"][0])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_refresh_rotates_tokens(client):
    c, verifier, q = get_code(client)
    first = exchange(client, c, verifier, q["code"][0]).json()
    client.cookies.clear()
    r = client.post("/token", data={"grant_type": "refresh_token", "client_id": c["client_id"],
                                    "refresh_token": first["refresh_token"]})
    assert r.status_code == 200, r.text
    second = r.json()
    assert second["access_token"] != first["access_token"]
    assert client.get("/api/models", headers={"Authorization": f"Bearer {first['access_token']}"}).status_code == 401
    assert client.get("/api/models", headers={"Authorization": f"Bearer {second['access_token']}"}).status_code == 200
    again = client.post("/token", data={"grant_type": "refresh_token", "client_id": c["client_id"],
                                        "refresh_token": first["refresh_token"]})
    assert again.status_code == 400


def test_deny_redirects_with_access_denied(client):
    c = register_client(client)
    consent = start_authorize(client, c, pkce()[1], state="s1")
    login(client)
    req = parse_qs(urlparse(consent).query)["req"][0]
    r = client.post("/oauth/consent", data={"req": req, "decision": "deny"})
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert r.headers["location"].startswith(REDIRECT)
    assert q["error"] == ["access_denied"] and q["state"] == ["s1"]
    # the request is used up
    assert client.post("/oauth/consent", data={"req": req, "decision": "allow"}).status_code == 400


def test_consent_expired_or_unknown_request(client):
    login(client)
    assert client.get("/oauth/consent?req=nope").status_code == 400
    assert client.post("/oauth/consent", data={"req": "nope", "decision": "allow"}).status_code == 400
    assert client.post("/oauth/consent", data={"req": "nope", "decision": "deny"}).status_code == 400


def test_consent_refuses_cross_origin_post(client):
    c = register_client(client)
    consent = start_authorize(client, c, pkce()[1])
    login(client)
    req = parse_qs(urlparse(consent).query)["req"][0]
    r = client.post("/oauth/consent", data={"req": req, "decision": "allow"},
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/oauth/consent", data={"req": req, "decision": "allow"},
                    headers={"origin": "http://localhost:8765"})
    assert r.status_code == 303


def test_connected_apps_can_be_listed_and_disconnected(client):
    c, verifier, q = get_code(client)
    tok = exchange(client, c, verifier, q["code"][0]).json()
    apps = client.get("/api/auth").json()["clients"]
    assert [a["name"] for a in apps] == ["Claude"]
    assert client.delete(f"/api/auth/clients/{c['client_id']}").json() == {"ok": True}
    assert client.delete(f"/api/auth/clients/{c['client_id']}").status_code == 404
    client.cookies.clear()
    assert client.get("/api/models", headers={"Authorization": f"Bearer {tok['access_token']}"}).status_code == 401


def test_revoke_endpoint(client):
    c, verifier, q = get_code(client)
    tok = exchange(client, c, verifier, q["code"][0]).json()
    client.cookies.clear()
    assert client.post("/revoke", data={"token": tok["access_token"], "client_id": c["client_id"],
                                       "client_secret": ""}).status_code == 200
    assert client.get("/api/models", headers={"Authorization": f"Bearer {tok['access_token']}"}).status_code == 401


# ---- sessions over HTTP

def test_token_query_sets_session_cookie_and_keeps_other_params(client):
    r = client.get("/oauth/consent?req=abc&token=secret")
    assert r.status_code == 303 and r.headers["location"] == "/oauth/consent?req=abc"
    cookie = r.headers["set-cookie"]
    assert "stl_session=" in cookie and "secret" not in cookie.split("stl_token")[0]
    assert "httponly" in cookie.lower()


def test_old_raw_token_cookie_no_longer_logs_in(client):
    client.cookies.set("stl_token", "secret")
    assert client.get("/api/models").status_code == 401


def test_unknown_session_cookie_is_refused(client):
    client.cookies.set(server.SESSION_COOKIE, "made-up")
    assert client.get("/api/models").status_code == 401


def test_logout_revokes_the_session_server_side(client):
    login(client)
    raw = client.cookies[server.SESSION_COOKIE]
    r = client.post("/auth/logout", json={})
    assert r.status_code == 200 and 'stl_session=""' in r.headers["set-cookie"]
    client.cookies.set(server.SESSION_COOKIE, raw)  # a stolen copy of the cookie is dead too
    assert client.get("/api/models").status_code == 401


def test_personal_token_logs_in_as_its_user(client):
    joe = server.oauth.state.create_user("Joe", "joe@example.com")
    raw, _ = server.oauth.state.create_personal_token(joe["id"], "cli")
    server.store.create("joes-box", "csg", {"root": {"shape": "box", "size": [5, 5, 5]}}, owner=joe["id"])
    r = client.get("/api/models", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200 and [m["id"] for m in r.json()["models"]] == ["joes-box"]


def test_admin_pages_need_the_admin_role(client):
    joe = server.oauth.state.create_user("Joe")
    login(client, joe)
    assert client.get("/api/settings").status_code == 403
    assert client.get("/admin").status_code == 403
    login(client)
    assert client.get("/api/settings").status_code == 200


def test_connected_apps_and_passkeys_are_per_user(client):
    c, verifier, q = get_code(client)  # approved by the admin
    exchange(client, c, verifier, q["code"][0])
    joe = login(client, server.oauth.state.create_user("Joe"))
    data = client.get("/api/auth").json()
    assert data["clients"] == [] and data["passkeys"] == [] and data["user"]["id"] == joe["id"]
    assert client.delete(f"/api/auth/clients/{c['client_id']}").status_code == 404


def test_oauth_token_belongs_to_the_user_who_approved(client):
    joe = server.oauth.state.create_user("Joe", "joe@example.com")
    c = register_client(client)
    verifier, challenge = pkce()
    consent = start_authorize(client, c, challenge)
    login(client, joe)
    assert "joe@example.com" in client.get(consent).text  # the consent page names the account
    req = parse_qs(urlparse(consent).query)["req"][0]
    r = client.post("/oauth/consent", data={"req": req, "decision": "allow"})
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    tok = exchange(client, c, verifier, code).json()
    client.cookies.clear()
    server.store.create("joes-box", "csg", {"root": {"shape": "box", "size": [5, 5, 5]}}, owner=joe["id"])

    def models(access):
        r = client.get("/api/models", headers={"Authorization": f"Bearer {access}"})
        return [m["id"] for m in r.json()["models"]]
    assert models(tok["access_token"]) == ["joes-box"]
    # and refreshing keeps the owner
    r = client.post("/token", data={"grant_type": "refresh_token", "client_id": c["client_id"],
                                    "refresh_token": tok["refresh_token"]})
    assert models(r.json()["access_token"]) == ["joes-box"]


def test_auth_api_requires_login_even_with_ui_public(client, monkeypatch):
    monkeypatch.setattr(server, "UI_PUBLIC", True)
    assert client.get("/api/models").status_code == 200
    assert client.get("/api/auth").status_code == 401
    login(client)
    data = client.get("/api/auth").json()
    assert data["login_required"] and data["oauth"] and data["mcp_url"].endswith("/mcp")


def test_token_compare_handles_non_ascii_and_empty():
    assert not server._eq("åäö", "secret")
    assert not server._eq("", "")
    assert server._eq("secret", "secret")


# ---- passkeys, with a software authenticator

class SoftAuthenticator:
    def __init__(self, rp_id="localhost", origin="http://localhost:8765"):
        self.rp_id, self.origin = rp_id, origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.count = 0

    def _client_data(self, typ, challenge):
        return json.dumps({"type": typ, "challenge": challenge, "origin": self.origin,
                           "crossOrigin": False}).encode()

    def _rp_hash(self):
        return hashlib.sha256(self.rp_id.encode()).digest()

    def register(self, options):
        n = self.key.public_key().public_numbers()
        cose = {1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")}
        auth_data = (self._rp_hash() + bytes([0x45]) + self.count.to_bytes(4, "big") + bytes(16)
                     + len(self.cred_id).to_bytes(2, "big") + self.cred_id + cbor2.dumps(cose))
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        cd = self._client_data("webauthn.create", options["challenge"])
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(cd), "attestationObject": b64u(att)},
                "clientExtensionResults": {}}

    def login(self, options, origin=None):
        self.count += 1
        auth_data = self._rp_hash() + bytes([0x05]) + self.count.to_bytes(4, "big")
        cd = json.dumps({"type": "webauthn.get", "challenge": options["challenge"],
                         "origin": origin or self.origin, "crossOrigin": False}).encode()
        sig = self.key.sign(auth_data + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(cd), "authenticatorData": b64u(auth_data),
                             "signature": b64u(sig), "userHandle": b64u(b"stl-mcp-owner")},
                "clientExtensionResults": {}}


def register_passkey(client, dev, name="Mac"):
    o = client.post("/auth/passkey/register/options", json={}).json()
    assert o["options"]["authenticatorSelection"]["residentKey"] == "required"
    r = client.post("/auth/passkey/register/verify",
                    json={"flow": o["flow"], "name": name, "credential": dev.register(o["options"])})
    assert r.status_code == 200, r.text
    return r.json()["passkey"]


def test_passkey_register_requires_login(client):
    assert client.post("/auth/passkey/register/options", json={}).status_code == 401


def test_passkey_register_then_login(client):
    dev = SoftAuthenticator()
    login(client)
    pk = register_passkey(client, dev)
    assert pk["name"] == "Mac" and client.get("/api/auth").json()["passkeys"][0]["id"] == pk["id"]
    client.cookies.clear()
    assert client.get("/api/models").status_code == 401

    o = client.post("/auth/passkey/login/options", json={}).json()
    assert o["options"]["rpId"] == "localhost"
    r = client.post("/auth/passkey/login/verify", json={"flow": o["flow"], "credential": dev.login(o["options"])})
    assert r.status_code == 200, r.text
    assert "stl_session=" in r.headers["set-cookie"]
    assert client.get("/api/models").status_code == 200
    assert client.get("/api/auth").json()["user"]["role"] == "admin"
    assert server.oauth.state.passkey(pk["id"])["sign_count"] == 1


def test_passkey_login_flow_is_one_time(client):
    dev = SoftAuthenticator()
    login(client)
    register_passkey(client, dev)
    client.cookies.clear()
    o = client.post("/auth/passkey/login/options", json={}).json()
    cred = dev.login(o["options"])
    assert client.post("/auth/passkey/login/verify", json={"flow": o["flow"], "credential": cred}).status_code == 200
    client.cookies.clear()
    r = client.post("/auth/passkey/login/verify", json={"flow": o["flow"], "credential": cred})
    assert r.status_code == 400


def test_passkey_login_rejects_wrong_origin_unknown_key_and_register_flow(client):
    dev = SoftAuthenticator()
    login(client)
    register_passkey(client, dev)
    client.cookies.clear()
    o = client.post("/auth/passkey/login/options", json={}).json()
    r = client.post("/auth/passkey/login/verify",
                    json={"flow": o["flow"], "credential": dev.login(o["options"], origin="https://evil.example")})
    assert r.status_code == 400 and "verification failed" in r.json()["error"]
    assert "set-cookie" not in r.headers

    o = client.post("/auth/passkey/login/options", json={}).json()
    stranger = SoftAuthenticator()
    r = client.post("/auth/passkey/login/verify", json={"flow": o["flow"], "credential": stranger.login(o["options"])})
    assert r.status_code == 400 and "unknown passkey" in r.json()["error"]

    # a registration flow id cannot be used to log in
    login(client)
    reg = client.post("/auth/passkey/register/options", json={}).json()
    client.cookies.clear()
    r = client.post("/auth/passkey/login/verify", json={"flow": reg["flow"], "credential": dev.login(reg["options"])})
    assert r.status_code == 400


def test_passkey_register_rejects_bad_attestation_and_excludes_existing(client):
    dev = SoftAuthenticator()
    login(client)
    o = client.post("/auth/passkey/register/options", json={}).json()
    cred = dev.register(o["options"])
    cred["response"]["clientDataJSON"] = b64u(dev._client_data("webauthn.create", "wrong-challenge"))
    r = client.post("/auth/passkey/register/verify", json={"flow": o["flow"], "credential": cred})
    assert r.status_code == 400 and "registration failed" in r.json()["error"]
    assert client.post("/auth/passkey/register/verify", json={"flow": "nope"}).status_code == 400

    pk = register_passkey(client, dev)
    o = client.post("/auth/passkey/register/options", json={}).json()
    assert [c["id"] for c in o["options"]["excludeCredentials"]] == [pk["id"]]


def test_passkey_can_be_removed(client):
    dev = SoftAuthenticator()
    login(client)
    pk = register_passkey(client, dev)
    assert client.delete(f"/api/auth/passkeys/{pk['id']}").json() == {"ok": True}
    assert client.delete(f"/api/auth/passkeys/{pk['id']}").status_code == 404
    client.cookies.clear()
    o = client.post("/auth/passkey/login/options", json={}).json()
    r = client.post("/auth/passkey/login/verify", json={"flow": o["flow"], "credential": dev.login(o["options"])})
    assert r.status_code == 400


def test_passkey_login_then_approve_oauth(client):
    """The whole claude.ai connector flow, logging in with a passkey on the consent page."""
    dev = SoftAuthenticator()
    login(client)
    register_passkey(client, dev)
    client.cookies.clear()

    c = register_client(client)
    verifier, challenge = pkce()
    consent = start_authorize(client, c, challenge)
    assert client.get(consent, headers={"accept": "text/html"}).status_code == 401
    o = client.post("/auth/passkey/login/options", json={}).json()
    client.post("/auth/passkey/login/verify", json={"flow": o["flow"], "credential": dev.login(o["options"])})
    assert client.get(consent).status_code == 200
    req = parse_qs(urlparse(consent).query)["req"][0]
    r = client.post("/oauth/consent", data={"req": req, "decision": "allow"})
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    assert exchange(client, c, verifier, code).status_code == 200


def test_library_page_has_claude_connect_guide(client):
    login(client)
    page = client.get("/").text
    assert 'id="wiz"' in page and 'data-pick="${k}"' in page and "Add custom connector" in page
