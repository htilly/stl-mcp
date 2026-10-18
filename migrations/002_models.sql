-- The model library's metadata (recipe, version history, comments) moves from model.json files
-- into Postgres, with an owner. The STL and preview files stay on disk under STL_DIR/models/<id>/.
-- doc holds the same JSON document model.json used to; the columns next to it are copies of the
-- fields that are queried on.

CREATE TABLE models (
    id         text PRIMARY KEY,
    owner_id   text REFERENCES users (id) ON DELETE CASCADE,  -- NULL: only admins see it
    name       text NOT NULL,
    visibility text NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'public')),
    updated    double precision NOT NULL,
    doc        jsonb NOT NULL
);
CREATE INDEX models_owner_updated ON models (owner_id, updated DESC);
