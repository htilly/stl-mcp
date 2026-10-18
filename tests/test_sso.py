"""Sign in with Google, GitHub and Apple, against mocked provider endpoints."""
import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from starlette.testclient import TestClient

import server
import sso
from auth import RateLimiter

APPLE_KEY = ec.generate_private_key(ec.SECP256R1())
APPLE_PEM = APPLE_KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode()


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def id_token(**claims) -> str:
    return f"{b64u(b'{}')}.{b64u(json.dumps(claims).encode())}.sig"


class Fake:
    """The providers' token and API endpoints. `people` maps a code to what the provider says."""

    def __init__(self):
        self.people, self.requests = {}, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        form = parse_qs(request.content.decode()) if request.method == "POST" else {}
        if url in (sso.Google.token_url, sso.Apple.token_url):
            p = self.people.get(form["code"][0])
            if not p:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "at", "id_token": id_token(**p)})
        if url == sso.GitHub.token_url:
            p = self.people.get(form["code"][0])
            return httpx.Response(200, json={"access_token": form["code"][0]} if p else {"error": "bad_verification_code"})
        code = request.headers["authorization"].removeprefix("Bearer ")
        p = self.people[code]
        if url.endswith("/user"):
            return httpx.Response(200, json={"id": p["id"], "login": p["login"], "name": p.get("name")})
        if url.endswith("/user/emails"):
            return httpx.Response(200, json=p.get("emails", []))
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(sso, "http", lambda: httpx.Client(transport=httpx.MockTransport(f.handler)))
    return f


@pytest.fixture
def client(out_dir, monkeypatch, fake):
    monkeypatch.setattr(server, "TOKEN", "secret")
    monkeypatch.setattr(server, "SIGNUP", True)
    monkeypatch.setattr(server, "ADMIN_EMAILS", [])
    monkeypatch.setattr(server, "signups", RateLimiter(5, 3600))
    monkeypatch.setattr(server, "SSO", sso.configured({
        "GOOGLE_CLIENT_ID": "gid", "GOOGLE_CLIENT_SECRET": "gs",
        "GITHUB_CLIENT_ID": "hid", "GITHUB_CLIENT_SECRET": "hs",
        "APPLE_CLIENT_ID": "eu.autosmith.web", "APPLE_TEAM_ID": "TEAM", "APPLE_KEY_ID": "KEY",
        "APPLE_PRIVATE_KEY": APPLE_PEM}))
    return TestClient(server.app, follow_redirects=False)


def start(client, provider, next_="/", link=False):
    r = client.get(f"/auth/sso/{provider}/start", params={"next": next_, **({"link": "1"} if link else {})})
    assert r.status_code == 303, r.text
    return parse_qs(urlparse(r.headers["location"]).query), r.headers["location"]


def google_person(fake, code, sub, email=None, verified=True, name="Joe G", nonce=None, **extra):
    fake.people[code] = {"iss": "https://accounts.google.com", "aud": "gid", "sub": sub,
                         "exp": time.time() + 60, "email": email, "email_verified": verified,
                         "name": name, "nonce": nonce, **extra}


def sign_in_google(client, fake, code="c1", sub="g-1", next_="/", link=False, **person):
    q, _ = start(client, "google", next_, link)
    google_person(fake, code, sub, nonce=q["nonce"][0], **person)
    return client.get("/auth/sso/google/callback", params={"code": code, "state": q["state"][0]})


def me(client):
    return client.get("/api/auth").json()


# ---- discovery and the redirect

def test_login_page_config_lists_configured_providers(client):
    assert [p["id"] for p in client.get("/auth/config").json()["providers"]] == ["google", "apple", "github"]


def test_no_provider_configured_means_none(monkeypatch):
    assert sso.configured({}) == {}
    assert list(sso.configured({"GOOGLE_CLIENT_ID": "x"})) == []  # needs the secret too


def test_start_redirects_with_state_nonce_and_pkce(client):
    q, loc = start(client, "google", "/m/box")
    assert loc.startswith(sso.Google.authorize_url)
    assert q["client_id"] == ["gid"] and q["code_challenge_method"] == ["S256"]
    assert q["redirect_uri"] == ["http://localhost:8765/auth/sso/google/callback"]
    assert q["state"][0] and q["nonce"][0] and q["scope"] == ["openid email profile"]
    assert client.get("/auth/sso/nope/start").status_code == 404


def test_next_is_never_an_open_redirect():
    for bad in ("https://evil.example", "//evil.example", "/\\evil.example", "evil", ""):
        assert server._safe_next(bad) == "/"
    assert server._safe_next("/oauth/consent?req=x") == "/oauth/consent?req=x"


# ---- Google

def test_google_signup_then_login_lands_in_the_same_account(client, fake):
    r = sign_in_google(client, fake, email="Joe@Example.com", next_="/m/x")
    assert r.status_code == 303 and r.headers["location"] == "/m/x"
    first = me(client)["user"]
    assert first["display_name"] == "Joe G" and first["email"] == "joe@example.com" and first["role"] == "member"
    assert [i["provider"] for i in me(client)["identities"]] == ["google"]
    client.cookies.clear()
    sign_in_google(client, fake, code="c2", email="joe@example.com")
    assert me(client)["user"]["id"] == first["id"]


def test_unverified_email_is_not_stored_or_matched(client, fake):
    admin = server._admin()
    server.oauth.state.set_email(admin["id"], "boss@example.com")
    sign_in_google(client, fake, email="boss@example.com", verified=False)
    u = me(client)["user"]
    assert u["id"] != admin["id"] and u["email"] is None and u["role"] == "member"


def test_verified_email_links_to_the_existing_account(client, fake):
    joe = server.oauth.state.create_user("Joe", "joe@example.com")
    sign_in_google(client, fake, email="joe@example.com")
    assert me(client)["user"]["id"] == joe["id"]
    assert server.oauth.state.identity_user("google", "g-1")["id"] == joe["id"]


def test_admin_emails_become_admins(client, fake, monkeypatch):
    monkeypatch.setattr(server, "ADMIN_EMAILS", ["boss@example.com"])
    sign_in_google(client, fake, email="boss@example.com")
    assert me(client)["user"]["role"] == "admin"


def test_bad_id_tokens_are_refused(client, fake):
    for bad in ({"aud": "other-app"}, {"iss": "https://evil.example"}, {"exp": time.time() - 1}):
        client.cookies.clear()
        q, _ = start(client, "google")
        google_person(fake, "bad", "g-9", nonce=q["nonce"][0])
        fake.people["bad"].update(bad)
        r = client.get("/auth/sso/google/callback", params={"code": "bad", "state": q["state"][0]})
        assert "sso_error=" in r.headers["location"] and "set-cookie" not in r.headers
    q, _ = start(client, "google")
    google_person(fake, "n", "g-9", nonce="someone-elses-nonce")
    r = client.get("/auth/sso/google/callback", params={"code": "n", "state": q["state"][0]})
    assert "does+not+match" in r.headers["location"]


def test_state_is_one_time_and_errors_go_back_to_next(client, fake):
    q, _ = start(client, "google", "/m/box")
    google_person(fake, "c1", "g-1", nonce=q["nonce"][0])
    assert client.get("/auth/sso/google/callback", params={"code": "c1", "state": q["state"][0]}).status_code == 303
    client.cookies.clear()
    r = client.get("/auth/sso/google/callback", params={"code": "c1", "state": q["state"][0]})
    assert r.headers["location"].startswith("/?sso_error=") and "set-cookie" not in r.headers
    q, _ = start(client, "google", "/m/box")
    r = client.get("/auth/sso/google/callback", params={"error": "access_denied", "state": q["state"][0]})
    assert r.headers["location"].startswith("/m/box?sso_error=") and "cancelled" in r.headers["location"]
    q, _ = start(client, "github")  # a state from one provider cannot finish another
    r = client.get("/auth/sso/google/callback", params={"code": "c1", "state": q["state"][0]})
    assert "expired" in r.headers["location"]


def test_signup_closed_and_rate_limit(client, fake, monkeypatch):
    monkeypatch.setattr(server, "SIGNUP", False)
    r = sign_in_google(client, fake, email="new@example.com")
    assert "no+account" in r.headers["location"]
    monkeypatch.setattr(server, "SIGNUP", True)
    monkeypatch.setattr(server, "signups", RateLimiter(0, 3600))
    r = sign_in_google(client, fake, code="c2", sub="g-2")
    assert "too+many" in r.headers["location"]


def test_disabled_user_cannot_sign_in(client, fake, db):
    sign_in_google(client, fake)
    db.execute("UPDATE users SET disabled = true")
    client.cookies.clear()
    r = sign_in_google(client, fake, code="c2")
    assert "disabled" in r.headers["location"] and "set-cookie" not in r.headers


# ---- linking and unlinking

def test_link_google_to_a_passkey_account_and_unlink(client, fake):
    joe = server.oauth.state.create_user("Joe")
    server.oauth.state.add_passkey(joe["id"], "cred", "pk", 0, "Mac")
    client.cookies.set(server.SESSION_COOKIE, server.oauth.state.create_session(joe["id"]))
    r = sign_in_google(client, fake, link=True, email="joe@example.com")
    assert r.headers["location"] == "/"
    data = me(client)
    assert data["user"]["id"] == joe["id"] and data["user"]["email"] == "joe@example.com"
    assert [i["provider"] for i in data["identities"]] == ["google"]
    assert client.delete("/api/auth/identities/google").json() == {"ok": True}
    assert client.delete("/api/auth/identities/google").status_code == 404


def test_cannot_link_someone_elses_identity(client, fake):
    sign_in_google(client, fake)  # g-1 now belongs to a new account
    other = server.oauth.state.create_user("Other")
    client.cookies.set(server.SESSION_COOKIE, server.oauth.state.create_session(other["id"]))
    r = sign_in_google(client, fake, code="c2", link=True)
    assert "already+linked" in r.headers["location"]
    assert server.oauth.state.identities(other["id"]) == []


def test_link_needs_a_login(client):
    r = client.get("/auth/sso/google/start", params={"link": "1"})
    assert r.headers["location"] == "/"


def test_the_last_way_in_cannot_be_removed(client, fake):
    sign_in_google(client, fake)
    r = client.delete("/api/auth/identities/google")
    assert r.status_code == 400 and "cannot log in" in r.json()["error"]
    joe = server.oauth.state.create_user("Joe")
    server.oauth.state.add_passkey(joe["id"], "only", "pk", 0, "Mac")
    client.cookies.set(server.SESSION_COOKIE, server.oauth.state.create_session(joe["id"]))
    assert client.delete("/api/auth/passkeys/only").status_code == 400
    server.oauth.state.add_passkey(joe["id"], "second", "pk", 0, "iPhone")
    assert client.delete("/api/auth/passkeys/only").json() == {"ok": True}


# ---- GitHub

def test_github_uses_the_primary_verified_email(client, fake):
    q, loc = start(client, "github")
    assert loc.startswith(sso.GitHub.authorize_url) and q["scope"] == ["read:user user:email"]
    fake.people["gh"] = {"id": 42, "login": "octo", "name": None, "emails": [
        {"email": "old@example.com", "primary": False, "verified": True},
        {"email": "octo@example.com", "primary": True, "verified": True}]}
    r = client.get("/auth/sso/github/callback", params={"code": "gh", "state": q["state"][0]})
    assert r.status_code == 303
    u = me(client)["user"]
    assert u["display_name"] == "octo" and u["email"] == "octo@example.com"
    assert server.oauth.state.identity_user("github", "42")["id"] == u["id"]


def test_github_unverified_primary_gives_no_email(client, fake):
    q, _ = start(client, "github")
    fake.people["gh"] = {"id": 7, "login": "x", "emails": [{"email": "x@example.com", "primary": True, "verified": False}]}
    client.get("/auth/sso/github/callback", params={"code": "gh", "state": q["state"][0]})
    assert me(client)["user"]["email"] is None


# ---- Apple

def test_apple_client_secret_is_a_valid_es256_jwt():
    p = sso.Apple("eu.autosmith.web", "TEAM", "KEY", APPLE_PEM)
    head, body, sig = p.client_secret_jwt(now=1000).split(".")
    pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    assert json.loads(pad(head)) == {"alg": "ES256", "kid": "KEY"}
    assert json.loads(pad(body)) == {"iss": "TEAM", "iat": 1000, "exp": 1300,
                                     "aud": "https://appleid.apple.com", "sub": "eu.autosmith.web"}
    raw = pad(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    APPLE_KEY.public_key().verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))


def test_apple_form_post_callback_with_name_on_first_sign_in(client, fake):
    q, loc = start(client, "apple")
    assert q["response_mode"] == ["form_post"] and q["scope"] == ["name email"]
    fake.people["ap"] = {"iss": "https://appleid.apple.com", "aud": "eu.autosmith.web", "sub": "a.1",
                         "exp": time.time() + 60, "email": "joe@privaterelay.appleid.com",
                         "email_verified": "true", "nonce": q["nonce"][0]}
    user = json.dumps({"name": {"firstName": "Joe", "lastName": "Apple"}})
    r = client.post("/auth/sso/apple/callback", data={"code": "ap", "state": q["state"][0], "user": user})
    assert r.status_code == 303 and r.headers["location"] == "/"
    u = me(client)["user"]
    assert u["display_name"] == "Joe Apple" and u["email"] == "joe@privaterelay.appleid.com"
    token_req = next(x for x in fake.requests if str(x.url) == sso.Apple.token_url)
    sent = parse_qs(token_req.content.decode())
    assert sent["client_id"] == ["eu.autosmith.web"] and sent["client_secret"][0].count(".") == 2


def test_account_dialog_lists_identities_and_providers(client, fake):
    sign_in_google(client, fake)
    data = me(client)
    assert [p["id"] for p in data["providers"]] == ["google", "apple", "github"]
    assert data["identities"][0]["provider"] == "google"
