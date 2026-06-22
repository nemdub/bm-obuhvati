-- Place label for stations that share a printed number within one municipality because the
-- RIK document holds a member town's table as a second numbering block (Požarevac→Kostolac,
-- Užice→Sevojno). The stations stay under the city municipality (matching scope unchanged);
-- the Worker uses section_cyr only to group them under a divider and disambiguate exports.
-- NULL for ordinary single-table municipalities.
ALTER TABLE polling_stations ADD COLUMN section_cyr TEXT;
