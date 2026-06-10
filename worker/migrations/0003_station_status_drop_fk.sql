-- Drop the station_status -> polling_stations foreign key.
-- Re-importing derived data deletes + re-inserts polling_stations (station ids are
-- deterministic and stable), but the FK made that delete fail once review rows exist.
-- station_status is Worker-owned bookkeeping; recreate it without the FK, preserving rows.
CREATE TABLE station_status_new (
  station_id  INTEGER PRIMARY KEY,
  dirty       INTEGER NOT NULL DEFAULT 0,
  reviewed    INTEGER NOT NULL DEFAULT 0,
  updated_at  TEXT
);
INSERT INTO station_status_new (station_id, dirty, reviewed, updated_at)
  SELECT station_id, dirty, reviewed, updated_at FROM station_status;
DROP TABLE station_status;
ALTER TABLE station_status_new RENAME TO station_status;
