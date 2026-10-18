-- One row per web login, kept after the session itself is gone, for the admin's usage view.
-- The IP address is never stored: the country is looked up at login and only that is kept.
-- last_seen is bumped while the session is used, so last_seen - at is the time spent logged in.

CREATE TABLE login_events (
    id         bigserial PRIMARY KEY,
    user_id    text NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    session    text,  -- sessions.hash while the session lives (no foreign key: sessions get purged)
    at         double precision NOT NULL,
    last_seen  double precision NOT NULL,
    country    text,  -- ISO 3166-1 alpha-2, null when unknown
    user_agent text NOT NULL DEFAULT ''
);
CREATE INDEX login_events_user ON login_events (user_id, at DESC);
CREATE INDEX login_events_session ON login_events (session);
