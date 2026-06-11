-- Move polygons out of D1 and retire the write-only links table.
--
-- `polygons` (~70MB of GeoJSON) is now served from R2 as per-municipality blobs
-- (polygons/m/<muniId>.json + polygons/summary.json); the Worker reads them via the POLY
-- binding, so the D1 table is no longer referenced. `station_address_links` (~1.9M rows)
-- was always write-only — the Worker computes coverage points live from `addresses` — so it
-- only bloated every derived import. Both are still built as LOCAL pipeline artifacts
-- (stage05 derives Voronoi polygons from the links), just never shipped to D1.
--
-- CUTOVER ORDER (so the live app is never without polygons): create the R2 bucket, upload
-- the blobs, deploy the Worker (now reading R2), THEN apply this migration.
DROP TABLE IF EXISTS polygons;
DROP TABLE IF EXISTS station_address_links;
