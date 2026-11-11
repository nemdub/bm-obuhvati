-- Station source switched to the bm.itblokada.org export (pipeline/stage02_fetch_bm.py).
-- Carry its identifiers and per-station facts alongside our own integer id, so a station can
-- be cross-referenced with the BM project and located without geocoding:
--   bm_id        BM's stable ULID (GET /api/station/:id)
--   register_id  id in the electoral register (BM's legacy numeric id)
--   voter_count  registered voters in the current roll
--   lat, lon     venue coordinates (WGS84) — also the tie-breaker when several no-street
--                stations share a home settlement (stage04 no-street pass)
-- All nullable: reviewer-added stations and a rollback to the RIK source have none of them.
ALTER TABLE polling_stations ADD COLUMN bm_id TEXT;
ALTER TABLE polling_stations ADD COLUMN register_id TEXT;
ALTER TABLE polling_stations ADD COLUMN voter_count INTEGER;
ALTER TABLE polling_stations ADD COLUMN lat REAL;
ALTER TABLE polling_stations ADD COLUMN lon REAL;
CREATE INDEX idx_polling_stations_bm_id ON polling_stations(bm_id);
