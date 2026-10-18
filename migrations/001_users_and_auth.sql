-- Users and everything needed to log them in: passkeys, SSO identities, web sessions,
-- OAuth clients/tokens for MCP connectors and personal tokens for header-based clients.
-- Times are unix epoch seconds (double precision), like the rest of the code base.
-- Tokens and session ids are stored as sha256 hex hashes only.

CREATE TABLE users (
    id           text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    display_name text NOT NULL DEFAULT '',
    email        text,
    role         text NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
    bootstrap    boolean NOT NULL DEFAULT false,  -- the admin that MCP_TOKEN logs in as
    disabled     boolean NOT NULL DEFAULT false,
    created      double precision NOT NULL DEFAULT extract(epoch FROM now()),
    last_login   double precision
);
CREATE UNIQUE INDEX users_email ON users (lower(email)) WHERE email IS NOT NULL;
CREATE UNIQUE INDEX users_one_bootstrap ON users (bootstrap) WHERE bootstrap;

CREATE TABLE identities (
    provider text NOT NULL,
    subject  text NOT NULL,
    user_id  text NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    email    text,
    created  double precision NOT NULL DEFAULT extract(epoch FROM now()),
    PRIMARY KEY (provider, subject)
);
CREATE INDEX identities_user ON identities (user_id);

CREATE TABLE passkeys (
    id         text PRIMARY KEY,  -- base64url credential id
    user_id    text NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    public_key text NOT NULL,
    sign_count bigint NOT NULL DEFAULT 0,
    name       text NOT NULL DEFAULT 'passkey',
    created    double precision NOT NULL DEFAULT extract(epoch FROM now()),
    last_used  double precision
);
CREATE INDEX passkeys_user ON passkeys (user_id);

CREATE TABLE sessions (
    hash       text PRIMARY KEY,
    user_id    text NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created    double precision NOT NULL,
    expires_at double precision NOT NULL,
    last_seen  double precision NOT NULL
);
CREATE INDEX sessions_user ON sessions (user_id);

CREATE TABLE oauth_clients (
    client_id text PRIMARY KEY,
    info      jsonb NOT NULL,
    issued_at double precision NOT NULL DEFAULT extract(epoch FROM now())
);

CREATE TABLE oauth_tokens (
    hash       text PRIMARY KEY,
    kind       text NOT NULL CHECK (kind IN ('access', 'refresh')),
    client_id  text NOT NULL REFERENCES oauth_clients (client_id) ON DELETE CASCADE,
    user_id    text NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    scopes     jsonb NOT NULL,
    resource   text,
    issued_at  double precision NOT NULL,
    expires_at double precision NOT NULL,
    pair       text
);
CREATE INDEX oauth_tokens_user ON oauth_tokens (user_id);
CREATE INDEX oauth_tokens_client ON oauth_tokens (client_id);

CREATE TABLE personal_tokens (
    hash      text PRIMARY KEY,
    id        text NOT NULL UNIQUE DEFAULT gen_random_uuid()::text,
    user_id   text NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name      text NOT NULL DEFAULT '',
    created   double precision NOT NULL DEFAULT extract(epoch FROM now()),
    last_used double precision
);
CREATE INDEX personal_tokens_user ON personal_tokens (user_id);
