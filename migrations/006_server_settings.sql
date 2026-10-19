-- Server-wide settings an admin changes while the server runs, one JSON value per key (e.g.
-- 'openscad': {"timeout": 45, "concurrency": 3}). The environment still sets the defaults and the
-- upper bounds; a missing row means the environment's values.

CREATE TABLE server_settings (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated    double precision NOT NULL,
    updated_by text
);
