-- Per-station review bookkeeping, owned by the Worker (not the pipeline).
-- dirty=1 means a human changed coverage and the offline Voronoi should be recomputed
-- (the pipeline's --only-dirty mode consumes this); reviewed=1 means a human signed off.
CREATE TABLE station_status (
  station_id  INTEGER PRIMARY KEY REFERENCES polling_stations(id),
  dirty       INTEGER NOT NULL DEFAULT 0,
  reviewed    INTEGER NOT NULL DEFAULT 0,
  updated_at  TEXT
);
