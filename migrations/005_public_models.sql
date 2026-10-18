-- Public models: the gallery lists them, most recently changed first. Copies made from someone
-- else's model record it in doc->'copied_from' (counted in the gallery).

CREATE INDEX models_public_updated ON models (updated DESC) WHERE visibility = 'public';
CREATE INDEX models_copied_from ON models ((doc->'copied_from'->>'model_id'))
    WHERE doc ? 'copied_from';
