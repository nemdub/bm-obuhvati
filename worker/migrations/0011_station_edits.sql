-- Station-level reviewer edits (worker-owned; survive derived re-imports, consumed by the
-- pipeline reconcile step stage03c). Mirror segment_overrides / station_added_segments.

-- Corrected raw coverage text for an existing station. The pipeline re-parses it into fresh
-- segments on the next recompute; because segment ids are positional (station_id*1000+idx),
-- the worker purges this station's stale segment_overrides when the text is set.
CREATE TABLE station_text_overrides (
  station_id        INTEGER PRIMARY KEY,
  raw_coverage_text TEXT NOT NULL,
  updated_at        TEXT
);

-- Brand-new stations the RIK document dropped entirely. Coverage is entered as raw text and
-- parsed by the reconcile step. Text stored Cyrillic; Latin derived on display, as with
-- polling_stations.raw_coverage_text. Station id = ADDED_STATION_BASE + id.
CREATE TABLE added_stations (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  municipality_id   TEXT NOT NULL,
  number            INTEGER,                          -- printed Ред.бр; null => auto-assign
  name_cyr          TEXT NOT NULL,
  address_cyr       TEXT,
  raw_coverage_text TEXT NOT NULL DEFAULT '',
  created_at        TEXT
);

-- Tombstones: hide a station from the UI and DELETE it from derived D1 on recompute.
-- Reversible (un-remove = delete this row).
CREATE TABLE removed_stations (
  station_id        INTEGER PRIMARY KEY,
  reason            TEXT,
  removed_at        TEXT
);
