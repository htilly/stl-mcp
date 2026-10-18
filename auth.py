"""Users and login: web sessions, passkeys, personal tokens and an OAuth 2.1 authorization server
so MCP clients that cannot send a custom header (claude.ai connectors) can connect.

The OAuth flow: the client registers itself (dynamic client registration), sends the user to
/authorize, the user logs in to the web UI and approves the client on the consent page, and the
client exchanges the code for an access + refresh token tied to that user.

Persistent state lives in Postgres (see migrations/001_users_and_auth.sql); tokens and session ids
are stored only as sha256 hashes. Challenges, pending authorization requests and codes are
short-lived and kept in memory.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from pathlib import Path

from psycopg.types.json import Jsonb

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams,
                                      AuthorizeError, RefreshToken, TokenError,
                                      construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from db import Database

SESSION_TTL = 60 * 60 * 24 * 90
ACCESS_TTL = 60 * 60 * 24
REFRESH_TTL = 60 * 60 * 24 * 90
CODE_TTL = 5 * 60
PENDING_TTL = 10 * 60
CHALLENGE_TTL = 5 * 60
SEEN_EVERY = 60 * 60          # how often last_seen/last_used is written
MAX_CLIENTS = 1000
SCOPE = "stl"
PERSONAL_PREFIX = "stlp_"     # personal tokens are recognizable (and cheap to tell apart)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


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


# ---------------------------------------------------------------- persistent state (Postgres)

_USER = "u.id, u.display_name, u.email, u.role, u.bootstrap, u.disabled, u.created, u.last_login"


class AuthState:
    """Users, sessions, passkeys, OAuth clients/tokens and personal tokens, stored in Postgres.

    Every lookup that turns a credential into a user skips disabled users, so disabling a user
    locks them out everywhere at once."""

    def __init__(self, db: Database):
        self.db = db

    # ---- users

    def user(self, uid: str) -> dict | None:
        return self.db.one(f"SELECT {_USER} FROM users u WHERE u.id = %s", (uid,))

    def create_user(self, display_name: str = "", email: str | None = None,
                    role: str = "member") -> dict:
        return self.db.one(
            f"INSERT INTO users AS u (display_name, email, role) VALUES (%s, %s, %s) RETURNING {_USER}",
            ((display_name or "").strip()[:80], (email or "").strip().lower() or None, role))

    def bootstrap_admin(self, email: str | None = None) -> dict:
        """The admin that MCP_TOKEN logs in as (and that owns data from before users existed)."""
        u = self.db.one(f"SELECT {_USER} FROM users u WHERE u.bootstrap")
        if u:
            return u
        self.db.execute(
            "INSERT INTO users (display_name, email, role, bootstrap) VALUES ('admin', %s, 'admin', true) "
            "ON CONFLICT DO NOTHING", ((email or "").strip().lower() or None,))
        return self.db.one(f"SELECT {_USER} FROM users u WHERE u.bootstrap")

    def touch_login(self, uid: str):
        self.db.execute("UPDATE users SET last_login = %s WHERE id = %s", (time.time(), uid))

    # ---- web sessions (the cookie holds a random id; only its hash is stored)

    def create_session(self, uid: str, ttl: int = SESSION_TTL) -> str:
        raw, now = secrets.token_urlsafe(32), time.time()
        with self.db.tx() as c:
            c.execute("DELETE FROM sessions WHERE expires_at < %s", (now,))
            c.execute("INSERT INTO sessions (hash, user_id, created, expires_at, last_seen) "
                      "VALUES (%s, %s, %s, %s, %s)", (_hash(raw), uid, now, now + ttl, now))
        self.touch_login(uid)
        return raw

    def session_user(self, raw: str) -> dict | None:
        if not raw:
            return None
        now = time.time()
        row = self.db.one(
            f"SELECT {_USER}, s.last_seen AS _seen FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.hash = %s AND s.expires_at > %s AND NOT u.disabled", (_hash(raw), now))
        if not row:
            return None
        if now - row.pop("_seen") > SEEN_EVERY:
            self.db.execute("UPDATE sessions SET last_seen = %s WHERE hash = %s", (now, _hash(raw)))
        return row

    def delete_session(self, raw: str):
        self.db.execute("DELETE FROM sessions WHERE hash = %s", (_hash(raw or ""),))

    def delete_sessions(self, uid: str):
        self.db.execute("DELETE FROM sessions WHERE user_id = %s", (uid,))

    # ---- passkeys

    def passkeys(self, uid: str) -> list[dict]:
        return self.db.all("SELECT * FROM passkeys WHERE user_id = %s ORDER BY created", (uid,))

    def passkey(self, cred_id: str) -> dict | None:
        return self.db.one(
            "SELECT p.* FROM passkeys p JOIN users u ON u.id = p.user_id "
            "WHERE p.id = %s AND NOT u.disabled", (cred_id,))

    def add_passkey(self, uid: str, cred_id: str, public_key: str, sign_count: int,
                    name: str) -> dict:
        return self.db.one(
            "INSERT INTO passkeys (id, user_id, public_key, sign_count, name, created) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET "
            "public_key = EXCLUDED.public_key, sign_count = EXCLUDED.sign_count, name = EXCLUDED.name "
            "WHERE passkeys.user_id = EXCLUDED.user_id RETURNING *",
            (cred_id, uid, public_key, sign_count, (name or "passkey").strip()[:60], time.time()))

    def touch_passkey(self, cred_id: str, sign_count: int):
        self.db.execute("UPDATE passkeys SET sign_count = %s, last_used = %s WHERE id = %s",
                        (sign_count, time.time(), cred_id))

    def delete_passkey(self, cred_id: str, uid: str) -> bool:
        return self.db.execute("DELETE FROM passkeys WHERE id = %s AND user_id = %s",
                               (cred_id, uid)) > 0

    # ---- OAuth clients and tokens

    def client(self, client_id: str) -> dict | None:
        row = self.db.one("SELECT info FROM oauth_clients WHERE client_id = %s", (client_id,))
        return row["info"] if row else None

    def put_client(self, info: dict):
        with self.db.tx() as c:
            c.execute("INSERT INTO oauth_clients (client_id, info, issued_at) VALUES (%s, %s, %s) "
                      "ON CONFLICT (client_id) DO UPDATE SET info = EXCLUDED.info",
                      (info["client_id"], Jsonb(info), info.get("client_id_issued_at") or time.time()))
            # Registration is open to anyone, so cap it: drop the oldest clients without tokens.
            c.execute("DELETE FROM oauth_clients WHERE client_id IN ("
                      " SELECT client_id FROM oauth_clients c WHERE NOT EXISTS"
                      "  (SELECT 1 FROM oauth_tokens t WHERE t.client_id = c.client_id)"
                      " ORDER BY issued_at DESC, client_id DESC OFFSET %s)", (MAX_CLIENTS,))

    def clients(self, uid: str) -> list[dict]:
        """The user's apps that hold a live token (i.e. apps that are actually connected)."""
        rows = self.db.all(
            "SELECT c.client_id, c.info, max(t.issued_at) AS last_authorized FROM oauth_tokens t "
            "JOIN oauth_clients c ON c.client_id = t.client_id "
            "WHERE t.user_id = %s AND t.expires_at > %s GROUP BY c.client_id, c.info "
            "ORDER BY last_authorized DESC", (uid, time.time()))
        return [{"client_id": r["client_id"], "name": r["info"].get("client_name") or r["client_id"],
                 "redirect_uris": r["info"].get("redirect_uris", []),
                 "last_authorized": r["last_authorized"]} for r in rows]

    def revoke_client(self, client_id: str, uid: str) -> bool:
        """Disconnects the app for this user; the registration goes once nobody uses it."""
        with self.db.tx() as c:
            n = c.execute("DELETE FROM oauth_tokens WHERE client_id = %s AND user_id = %s",
                          (client_id, uid)).rowcount
            c.execute("DELETE FROM oauth_clients c WHERE client_id = %s AND NOT EXISTS "
                      "(SELECT 1 FROM oauth_tokens t WHERE t.client_id = c.client_id)", (client_id,))
            return n > 0

    def add_tokens(self, rows: list[dict]):
        with self.db.tx() as c:
            c.execute("DELETE FROM oauth_tokens WHERE expires_at < %s", (time.time(),))
            for t in rows:
                c.execute("INSERT INTO oauth_tokens (hash, kind, client_id, user_id, scopes, resource,"
                          " issued_at, expires_at, pair) VALUES (%(hash)s, %(kind)s, %(client_id)s,"
                          " %(user_id)s, %(scopes)s, %(resource)s, %(issued_at)s, %(expires_at)s,"
                          " %(pair)s)", {**t, "scopes": Jsonb(t["scopes"])})

    def token(self, raw: str, kind: str) -> dict | None:
        """The token row (with user_id) if it is live and its user is not disabled."""
        return self.db.one(
            "SELECT t.* FROM oauth_tokens t JOIN users u ON u.id = t.user_id "
            "WHERE t.hash = %s AND t.kind = %s AND t.expires_at > %s AND NOT u.disabled",
            (_hash(raw or ""), kind, time.time()))

    def revoke_token(self, raw: str):
        """Revokes a token together with its access/refresh partner."""
        with self.db.tx() as c:
            t = c.execute("DELETE FROM oauth_tokens WHERE hash = %s RETURNING pair",
                          (_hash(raw),)).fetchone()
            if t and t["pair"]:
                c.execute("DELETE FROM oauth_tokens WHERE hash = %s", (t["pair"],))

    # ---- personal tokens (for clients that send an Authorization header, e.g. Claude Code)

    def create_personal_token(self, uid: str, name: str) -> tuple[str, dict]:
        raw = PERSONAL_PREFIX + secrets.token_urlsafe(32)
        row = self.db.one("INSERT INTO personal_tokens (hash, user_id, name) VALUES (%s, %s, %s) "
                          "RETURNING id, name, created, last_used",
                          (_hash(raw), uid, (name or "token").strip()[:60]))
        return raw, row

    def personal_tokens(self, uid: str) -> list[dict]:
        return self.db.all("SELECT id, name, created, last_used FROM personal_tokens "
                           "WHERE user_id = %s ORDER BY created", (uid,))

    def delete_personal_token(self, token_id: str, uid: str) -> bool:
        return self.db.execute("DELETE FROM personal_tokens WHERE id = %s AND user_id = %s",
                               (token_id, uid)) > 0

    def personal_token_user(self, raw: str) -> dict | None:
        if not raw.startswith(PERSONAL_PREFIX):
            return None
        row = self.db.one(
            f"SELECT {_USER}, p.last_used AS _used FROM personal_tokens p JOIN users u "
            "ON u.id = p.user_id WHERE p.hash = %s AND NOT u.disabled", (_hash(raw),))
        if row and time.time() - (row.pop("_used") or 0) > SEEN_EVERY:
            self.db.execute("UPDATE personal_tokens SET last_used = %s WHERE hash = %s",
                            (time.time(), _hash(raw)))
        return row

    # ---- data from before users existed

    def import_legacy(self, path: Path, owner: str) -> bool:
        """Moves passkeys, clients and tokens from the old auth.json to `owner`. The file is
        renamed to auth.json.imported afterwards, so this runs once."""
        if not path.is_file():
            return False
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            data = {}
        with self.db.tx() as c:
            for p in data.get("passkeys", []):
                c.execute("INSERT INTO passkeys (id, user_id, public_key, sign_count, name, created,"
                          " last_used) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                          (p["id"], owner, p["public_key"], p.get("sign_count", 0),
                           p.get("name") or "passkey", p.get("created") or time.time(), p.get("last_used")))
            for cid, info in data.get("clients", {}).items():
                c.execute("INSERT INTO oauth_clients (client_id, info, issued_at) VALUES (%s, %s, %s) "
                          "ON CONFLICT DO NOTHING",
                          (cid, Jsonb(info), info.get("client_id_issued_at") or time.time()))
            for h, t in data.get("tokens", {}).items():
                if t.get("expires_at", 0) <= time.time() or t.get("client_id") not in data.get("clients", {}):
                    continue
                c.execute("INSERT INTO oauth_tokens (hash, kind, client_id, user_id, scopes, resource,"
                          " issued_at, expires_at, pair) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                          " ON CONFLICT DO NOTHING",
                          (h, t["kind"], t["client_id"], owner, Jsonb(t.get("scopes") or [SCOPE]),
                           t.get("resource"), t.get("issued_at", 0), t["expires_at"], t.get("pair")))
        path.replace(path.with_name(path.name + ".imported"))
        return True

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
        c = self.state.client(client_id)
        return OAuthClientInformationFull.model_validate(c) if c else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.state.put_client(client_info.model_dump(mode="json", exclude_none=True))

    # ---- authorization

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        req = secrets.token_urlsafe(24)
        self.pending.put(req, {"client_id": client.client_id,
                               "client_name": client.client_name or client.client_id,
                               "params": params.model_dump(mode="json")})
        return f"/oauth/consent?req={req}"

    def pending_request(self, req: str) -> dict | None:
        return self.pending.get(req)

    def approve(self, req: str, user_id: str) -> str:
        """Issues an authorization code for user_id and returns the client's redirect URL."""
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
        self.codes.put(code.code, (code, user_id))
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
        entry = self.codes.get(authorization_code)
        return entry[0] if entry and entry[0].client_id == client.client_id else None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        entry = self.codes.pop(authorization_code.code)  # one-time use
        if not entry:
            raise TokenError("invalid_grant", "authorization code already used")
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource,
                           entry[1])

    # ---- tokens

    def _issue(self, client_id: str, scopes: list[str], resource: str | None,
               user_id: str) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        base = {"client_id": client_id, "user_id": user_id, "scopes": scopes, "resource": resource,
                "issued_at": now}
        self.state.add_tokens([
            {**base, "hash": _hash(access), "kind": "access", "expires_at": now + ACCESS_TTL,
             "pair": _hash(refresh)},
            {**base, "hash": _hash(refresh), "kind": "refresh", "expires_at": now + REFRESH_TTL,
             "pair": _hash(access)}])
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
        t = self.state.token(refresh_token.token, "refresh")
        if not t:
            raise TokenError("invalid_grant", "refresh token is no longer valid")
        self.state.revoke_token(refresh_token.token)  # rotate: the old pair stops working
        return self._issue(client.client_id, scopes, refresh_token.resource, t["user_id"])

    async def load_access_token(self, token: str) -> AccessToken | None:
        t = self.state.token(token, "access")
        if not t:
            return None
        return AccessToken(token=token, client_id=t["client_id"], scopes=t["scopes"],
                           expires_at=int(t["expires_at"]), resource=t.get("resource"))

    def access_token_user(self, token: str) -> dict | None:
        """The user an OAuth access token belongs to, or None."""
        t = self.state.token(token, "access") if token else None
        return self.state.user(t["user_id"]) if t else None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.state.revoke_token(token.token)
