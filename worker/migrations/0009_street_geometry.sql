-- WGS84 line geometry (GeoJSON) for streets that have no house numbers — the
-- streets a point-based polygon can't cover. Populated by import_street_geometry.sql
-- from stage06. Streets with addresses get coverage from their points instead.
CREATE TABLE street_geometry (
  street_id TEXT PRIMARY KEY REFERENCES streets(id),
  geojson   TEXT NOT NULL
);
