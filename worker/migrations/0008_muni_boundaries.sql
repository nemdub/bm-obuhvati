-- Official municipality boundary outlines (simplified), rendered as a reference
-- layer on the review maps. Populated by import_muni_boundaries.sql from stage06.
CREATE TABLE muni_boundaries (
  municipality_id TEXT PRIMARY KEY REFERENCES municipalities(id),
  geojson         TEXT NOT NULL
);
