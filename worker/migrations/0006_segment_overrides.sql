-- Human review edits, separated from pipeline-owned coverage_segments: that table is
-- DELETE+reloaded on every derived re-import, which would wipe in-row manual columns.
-- This table is Worker-owned and survives imports. Effective values:
--   parsed  = COALESCE(overrides.manual_json, segments.parsed_json)
--   street  = COALESCE(overrides.manual_street_id, segments.street_id)
--   review  = segments.needs_review AND NOT overrides.reviewed
CREATE TABLE segment_overrides (
  segment_id        INTEGER PRIMARY KEY,
  manual_json       TEXT,
  manual_street_id  TEXT,
  reviewed          INTEGER NOT NULL DEFAULT 0,
  updated_at        TEXT
);
-- Preserve any existing in-row manual edits.
INSERT INTO segment_overrides (segment_id, manual_json, reviewed, updated_at)
  SELECT id, manual_json, 0, datetime('now') FROM coverage_segments WHERE manual_locked = 1;
