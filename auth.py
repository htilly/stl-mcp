"""Login for the web UI (passkeys + signed session cookies) and an OAuth 2.1 authorization server
so MCP clients that cannot send a custom header (claude.ai connectors) can connect.

The OAuth flow: the client registers itself (dynamic client registration), sends the user to
/authorize, the user logs in to the web UI (passkey or MCP_TOKEN) and approves the client on the
consent page, and the client exchanges the code for an access + refresh token.

Persistent state lives in <STL_DIR>/auth.json: passkeys, registered clients and tokens (only
sha256 hashes of tokens are stored). Challenges, pending authorization requests and codes are
short-lived and kept in memory.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams,
                                      AuthorizeError, RefreshToken, TokenError,
                                      construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

SESSION_TTL = 60 * 60 * 24 * 90
ACCESS_TTL = 60 * 60 * 24
REFRESH_TTL = 60 * 60 * 24 * 90
CODE_TTL = 5 * 60
PENDING_TTL = 10 * 60
CHALLENGE_TTL = 5 * 60
MAX_CLIENTS = 50
SCOPE = "stl"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------- sessions

def _session_key(secret: str) -> bytes:
    return hmac.new(secret.encode(), b"stl-mcp session", hashlib.sha256).digest()


def make_session(secret: str, ttl: int = SESSION_TTL, now: float | None = None) -> str:
    """A signed, expiring cookie value. Rotating MCP_TOKEN invalidates every session."""
    body = f"s1.{int((now or time.time()) + ttl)}"
    sig = hmac.new(_session_key(secret), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def check_session(secret: str, value: str, now: float | None = None) -> bool:
    try:
        version, exp, sig = value.split(".")
        expires = int(exp)
    except ValueError:
        return False
    if version != "s1" or not secret:
        return False
    good = hmac.new(_session_key(secret), f"s1.{exp}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and expires > (now or time.time())


# ---------------------------------------------------------------- short-lived values

class Expiring:
    """In-memory dict whose entries disappear after ttl seconds; pop() is one-time use."""

    def __init__(self, ttl: int):
        self.ttl = ttl
        self._items: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def put(self, key: str, value) -> str:
        with self._lock:
            now = time.time()
            self._items = {k: v for k, v in self._items.items() if v[0] > now}
            self._items[key] = (now + self.ttl, value)
        return key

    def get(self, key: str):
        with self._lock:
            item = self._items.get(key)
            return item[1] if item and item[0] > time.time() else None

    def pop(self, key: str):
        with self._lock:
            item = self._items.pop(key, None)
            return item[1] if item and item[0] > time.time() else None


# ---------------------------------------------------------------- persistent state

class AuthState:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        data.setdefault("passkeys", [])
        data.setdefault("clients", {})
        data.setdefault("tokens", {})
        return data

    def _write(self, data: dict):
        now = time.time()
        data["tokens"] = {h: t for h, t in data["tokens"].items() if t["expires_at"] > now}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def read(self) -> dict:
        with self._lock:
            return self._read()

    def change(self, fn):
        """fn(data) mutates the state and may return a value; the result is saved atomically."""
        with self._lock:
            data = self._read()
            out = fn(data)
            self._write(data)
            return out

    # ---- passkeys

    def passkeys(self) -> list[dict]:
        return self.read()["passkeys"]

    def passkey(self, cred_id: str) -> dict | None:
        return next((p for p in self.passkeys() if p["id"] == cred_id), None)

    def add_passkey(self, cred_id: str, public_key: str, sign_count: int, name: str) -> dict:
        pk = {"id": cred_id, "public_key": public_key, "sign_count": sign_count,
              "name": (name or "passkey").strip()[:60], "created": time.time(), "last_used": None}

        def fn(d):
            d["passkeys"] = [p for p in d["passkeys"] if p["id"] != cred_id] + [pk]
        self.change(fn)
        return pk

    def touch_passkey(self, cred_id: str, sign_count: int):
        def fn(d):
            for p in d["passkeys"]:
                if p["id"] == cred_id:
                    p["sign_count"], p["last_used"] = sign_count, time.time()
        self.change(fn)

    def delete_passkey(self, cred_id: str) -> bool:
        def fn(d):
            before = len(d["passkeys"])
            d["passkeys"] = [p for p in d["passkeys"] if p["id"] != cred_id]
            return len(d["passkeys"]) != before
        return self.change(fn)

    # ---- clients and tokens

    def clients(self) -> list[dict]:
        """Registered clients that hold a live token (i.e. apps that are actually connected)."""
        d = self.read()
        live: dict[str, dict] = {}
        for t in d["tokens"].values():
            if t["expires_at"] > time.time():
                c = live.setdefault(t["client_id"], {"last_issued": 0})
                c["last_issued"] = max(c["last_issued"], t["issued_at"])
        out = []
        for cid, info in live.items():
            c = d["clients"].get(cid, {})
            out.append({"client_id": cid, "name": c.get("client_name") or cid,
                        "redirect_uris": c.get("redirect_uris", []),
                        "last_authorized": info["last_issued"]})
        return sorted(out, key=lambda c: -c["last_authorized"])

    def revoke_client(self, client_id: str) -> bool:
        def fn(d):
            found = client_id in d["clients"]
            d["clients"].pop(client_id, None)
            d["tokens"] = {h: t for h, t in d["tokens"].items() if t["client_id"] != client_id}
            return found
        return self.change(fn)

    def token(self, raw: str, kind: str) -> dict | None:
        t = self.read()["tokens"].get(_hash(raw))
        if t and t["kind"] == kind and t["expires_at"] > time.time():
            return t
        return None


# ---------------------------------------------------------------- OAuth provider

class OAuthProvider:
    """Implements mcp.server.auth.provider.OAuthAuthorizationServerProvider.

    authorize() parks the request and sends the user to /oauth/consent; the web server calls
    approve()/deny() once the logged-in user has decided."""

    def __init__(self, state: AuthState):
        self.state = state
        self.pending = Expiring(PENDING_TTL)
        self.codes = Expiring(CODE_TTL)

    # ---- clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        c = self.state.read()["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(c) if c else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        def fn(d):
            d["clients"][client_info.client_id] = client_info.model_dump(mode="json", exclude_none=True)
            if len(d["clients"]) > MAX_CLIENTS:
                # Registration is open to anyone, so cap it: drop the oldest clients without tokens.
                in_use = {t["client_id"] for t in d["tokens"].values()}
                idle = sorted((c.get("client_id_issued_at") or 0, cid)
                              for cid, c in d["clients"].items() if cid not in in_use)
                for _, cid in idle[:len(d["clients"]) - MAX_CLIENTS]:
                    del d["clients"][cid]
        self.state.change(fn)

    # ---- authorization

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        req = secrets.token_urlsafe(24)
        self.pending.put(req, {"client_id": client.client_id,
                               "client_name": client.client_name or client.client_id,
                               "params": params.model_dump(mode="json")})
        return f"/oauth/consent?req={req}"

    def pending_request(self, req: str) -> dict | None:
        return self.pending.get(req)

    def approve(self, req: str) -> str:
        """Issues an authorization code and returns the client's redirect URL."""
        p = self.pending.pop(req)
        if not p:
            raise AuthorizeError("invalid_request", "the authorization request has expired")
        params = AuthorizationParams.model_validate(p["params"])
        code = AuthorizationCode(
            code=secrets.token_urlsafe(32), scopes=params.scopes or [SCOPE],
            expires_at=time.time() + CODE_TTL, client_id=p["client_id"],
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource)
        self.codes.put(code.code, code)
        return construct_redirect_uri(str(params.redirect_uri), code=code.code, state=params.state)

    def deny(self, req: str) -> str | None:
        p = self.pending.pop(req)
        if not p:
            return None
        params = AuthorizationParams.model_validate(p["params"])
        return construct_redirect_uri(str(params.redirect_uri), error="access_denied",
                                      error_description="the user denied access", state=params.state)

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        code = self.codes.get(authorization_code)
        return code if code and code.client_id == client.client_id else None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        if not self.codes.pop(authorization_code.code):  # one-time use
            raise TokenError("invalid_grant", "authorization code already used")
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    # ---- tokens

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        base = {"client_id": client_id, "scopes": scopes, "resource": resource, "issued_at": now}

        def fn(d):
            d["tokens"][_hash(access)] = {**base, "kind": "access", "expires_at": now + ACCESS_TTL,
                                          "pair": _hash(refresh)}
            d["tokens"][_hash(refresh)] = {**base, "kind": "refresh", "expires_at": now + REFRESH_TTL,
                                           "pair": _hash(access)}
        self.state.change(fn)
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL, scope=" ".join(scopes),
                          refresh_token=refresh)

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> RefreshToken | None:
        t = self.state.token(refresh_token, "refresh")
        if not t or t["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=t["client_id"], scopes=t["scopes"],
                            expires_at=int(t["expires_at"]), resource=t.get("resource"))

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        await self.revoke_token(refresh_token)  # rotate: the old pair stops working
        return self._issue(client.client_id, scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        t = self.state.token(token, "access")
        if not t:
            return None
        return AccessToken(token=token, client_id=t["client_id"], scopes=t["scopes"],
                           expires_at=int(t["expires_at"]), resource=t.get("resource"))

    def access_token_ok(self, token: str) -> bool:
        return bool(token) and self.state.token(token, "access") is not None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        h = _hash(token.token)

        def fn(d):
            t = d["tokens"].pop(h, None)
            if t:
                d["tokens"].pop(t.get("pair"), None)
        self.state.change(fn)
