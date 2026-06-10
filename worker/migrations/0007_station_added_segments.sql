-- Reviewer-added street claims, for streets the RIK document omitted entirely
-- (e.g. Zemun "Штурмова" — never mentioned in any coverage text). Worker-owned;
-- survives derived re-imports. The pipeline ingests these as synthetic segments
-- (id = 9e12 + this id) so links/polygons include them.
CREATE TABLE station_added_segments (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  station_id   INTEGER NOT NULL,
  street_id    TEXT NOT NULL,
  manual_json  TEXT NOT NULL,        -- {intervals, singles, whole}
  created_at   TEXT
);
CREATE INDEX idx_added_station ON station_added_segments(station_id);
