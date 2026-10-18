"""Sign in with Google, GitHub or Apple (OAuth 2.0 authorization code flow).

Each provider turns a callback into an Identity. The server keeps the flow state (state, nonce,
PKCE verifier) server side and decides which account the identity belongs to.

ID tokens are read without checking their signature: they come straight from the provider's token
endpoint over TLS, which OpenID Connect Core 3.1.3.7 accepts in place of a signature check. The
issuer, audience, expiry and nonce are still checked.

Configuration (a provider is on when its variables are set):
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
  GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET
  APPLE_CLIENT_ID (the Services ID), APPLE_TEAM_ID, APPLE_KEY_ID, and the .p8 key as
  APPLE_PRIVATE_KEY (PEM text) or APPLE_PRIVATE_KEY_FILE (path)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

TIMEOUT = 15


class SSOError(Exception):
    """Shown to the user on the login page."""


@dataclass
class Identity:
    provider: str
    subject: str
    email: str | None          # only set when the provider says it is verified
    name: str


def http() -> httpx.Client:
    """The HTTP client for provider calls (tests replace it with a mock transport)."""
    return httpx.Client(timeout=TIMEOUT)


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, _b64u(hashlib.sha256(verifier.encode()).digest())


def jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as e:
        raise SSOError("the provider sent an unreadable id token") from e


def check_id_token(claims: dict, issuers: set[str], audience: str, nonce: str) -> None:
    aud = claims.get("aud")
    if claims.get("iss") not in issuers:
        raise SSOError("the id token is from an unexpected issuer")
    if audience not in (aud if isinstance(aud, list) else [aud]):
        raise SSOError("the id token is for another app")
    if float(claims.get("exp", 0)) < time.time():
        raise SSOError("the id token has expired - try again")
    if not nonce or claims.get("nonce") != nonce:
        raise SSOError("the sign-in attempt does not match - try again")


def _verified(value) -> bool:
    return value is True or value == "true"


def _post_json(url: str, data: dict, headers: dict | None = None) -> dict:
    with http() as c:
        r = c.post(url, data=data, headers={"Accept": "application/json", **(headers or {})})
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200 or "error" in body:
        raise SSOError(f"the provider refused the sign-in ({body.get('error') or r.status_code})")
    return body


class Provider:
    name = ""
    label = ""
    authorize_url = ""
    scope = ""
    form_post = False   # the callback arrives as a POST (Apple)
    uses_pkce = True

    def __init__(self, client_id: str, client_secret: str = ""):
        self.client_id, self.client_secret = client_id, client_secret

    def authorize(self, redirect_uri: str, state: str, nonce: str, challenge: str) -> str:
        q = {"response_type": "code", "client_id": self.client_id, "redirect_uri": redirect_uri,
             "scope": self.scope, "state": state, **self.extra_params(nonce)}
        if self.uses_pkce:
            q |= {"code_challenge": challenge, "code_challenge_method": "S256"}
        return f"{self.authorize_url}?{urlencode(q)}"

    def extra_params(self, nonce: str) -> dict:
        return {"nonce": nonce}

    def identity(self, code: str, redirect_uri: str, nonce: str, verifier: str,
                 form: dict | None = None) -> Identity:
        raise NotImplementedError


class Google(Provider):
    name, label = "google", "Google"
    authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    scope = "openid email profile"

    def extra_params(self, nonce: str) -> dict:
        return {"nonce": nonce, "prompt": "select_account"}

    def identity(self, code, redirect_uri, nonce, verifier, form=None) -> Identity:
        tok = _post_json(self.token_url, {
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": self.client_id, "client_secret": self.client_secret, "code_verifier": verifier})
        c = jwt_claims(tok.get("id_token", ""))
        check_id_token(c, {"https://accounts.google.com", "accounts.google.com"}, self.client_id, nonce)
        return Identity("google", str(c["sub"]),
                        c.get("email") if _verified(c.get("email_verified")) else None,
                        c.get("name") or c.get("given_name") or "")


class GitHub(Provider):
    name, label = "github", "GitHub"
    authorize_url = "https://github.com/login/oauth/authorize"
    token_url = "https://github.com/login/oauth/access_token"
    api = "https://api.github.com"
    scope = "read:user user:email"

    def extra_params(self, nonce: str) -> dict:
        return {"allow_signup": "true"}

    def identity(self, code, redirect_uri, nonce, verifier, form=None) -> Identity:
        tok = _post_json(self.token_url, {
            "code": code, "redirect_uri": redirect_uri, "client_id": self.client_id,
            "client_secret": self.client_secret, "code_verifier": verifier})
        h = {"Authorization": f"Bearer {tok.get('access_token', '')}",
             "Accept": "application/vnd.github+json"}
        with http() as c:
            user = c.get(f"{self.api}/user", headers=h)
            emails = c.get(f"{self.api}/user/emails", headers=h)
        if user.status_code != 200:
            raise SSOError("GitHub did not share the account")
        u = user.json()
        primary = next((e["email"] for e in (emails.json() if emails.status_code == 200 else [])
                        if e.get("primary") and e.get("verified")), None)
        return Identity("github", str(u["id"]), primary, u.get("name") or u.get("login") or "")


class Apple(Provider):
    name, label = "apple", "Apple"
    authorize_url = "https://appleid.apple.com/auth/authorize"
    token_url = "https://appleid.apple.com/auth/token"
    scope = "name email"
    form_post = True

    def __init__(self, client_id: str, team_id: str, key_id: str, private_key: str):
        super().__init__(client_id)
        self.team_id, self.key_id = team_id, key_id
        self.key = serialization.load_pem_private_key(private_key.encode(), password=None)

    def extra_params(self, nonce: str) -> dict:
        return {"nonce": nonce, "response_mode": "form_post"}

    def client_secret_jwt(self, now: float | None = None) -> str:
        """Apple's client secret: an ES256 JWT signed with the team's key, valid for 5 minutes."""
        now = int(now or time.time())
        header = {"alg": "ES256", "kid": self.key_id}
        claims = {"iss": self.team_id, "iat": now, "exp": now + 300,
                  "aud": "https://appleid.apple.com", "sub": self.client_id}
        signing = f"{_b64u(json.dumps(header).encode())}.{_b64u(json.dumps(claims).encode())}"
        r, s = decode_dss_signature(self.key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
        return f"{signing}.{_b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"

    def identity(self, code, redirect_uri, nonce, verifier, form=None) -> Identity:
        tok = _post_json(self.token_url, {
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": self.client_id, "client_secret": self.client_secret_jwt(),
            "code_verifier": verifier})
        c = jwt_claims(tok.get("id_token", ""))
        check_id_token(c, {"https://appleid.apple.com"}, self.client_id, nonce)
        name = ""
        try:  # Apple sends the name once, on the first sign-in, as a form field
            n = json.loads((form or {}).get("user") or "{}").get("name") or {}
            name = " ".join(p for p in (n.get("firstName"), n.get("lastName")) if p)
        except (ValueError, AttributeError):
            pass
        return Identity("apple", str(c["sub"]),
                        c.get("email") if _verified(c.get("email_verified")) else None, name)


def configured(env=os.environ) -> dict[str, Provider]:
    """The providers whose settings are present, in the order the login page shows them."""
    out: dict[str, Provider] = {}
    if env.get("GOOGLE_CLIENT_ID") and env.get("GOOGLE_CLIENT_SECRET"):
        out["google"] = Google(env["GOOGLE_CLIENT_ID"], env["GOOGLE_CLIENT_SECRET"])
    if env.get("APPLE_CLIENT_ID") and env.get("APPLE_TEAM_ID") and env.get("APPLE_KEY_ID"):
        key = env.get("APPLE_PRIVATE_KEY") or (
            Path(env["APPLE_PRIVATE_KEY_FILE"]).read_text() if env.get("APPLE_PRIVATE_KEY_FILE") else "")
        if key:
            out["apple"] = Apple(env["APPLE_CLIENT_ID"], env["APPLE_TEAM_ID"], env["APPLE_KEY_ID"], key)
    if env.get("GITHUB_CLIENT_ID") and env.get("GITHUB_CLIENT_SECRET"):
        out["github"] = GitHub(env["GITHUB_CLIENT_ID"], env["GITHUB_CLIENT_SECRET"])
    return out
