-- The set of settlements (villages) a polling station's coverage spans, as assumed by the
-- matcher: role 'home' is the address-derived settlement, 'spanned' is every other settlement
-- the station covers (a street resolved there, an explicit document label/marker, or a
-- whole-settlement claim). stage04 derives it and uses the whole set as the matching scope
-- (a street in a spanned village resolves cleanly instead of via the flagged muni fallback);
-- the Worker shows it on the station page. Re-runnable: import_station_settlements.sql does a
-- full DELETE+reINSERT each pipeline pass.
CREATE TABLE station_settlements (
  station_id    INTEGER NOT NULL REFERENCES polling_stations(id),
  settlement_id TEXT    NOT NULL REFERENCES settlements(id),
  role          TEXT    NOT NULL,   -- 'home' | 'spanned'
  PRIMARY KEY (station_id, settlement_id)
);
CREATE INDEX idx_station_settlements_station ON station_settlements(station_id);
