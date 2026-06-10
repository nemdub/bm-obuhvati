-- bm-obuhvati schema
-- Code & docs in English. Names stored in both Cyrillic and Latin (the register has both);
-- UI chrome is transliterated server-side from a single source.

-- ── Administrative hierarchy ────────────────────────────────────────────────
CREATE TABLE municipalities (
  id        TEXT PRIMARY KEY,          -- opstina_maticni_broj
  name_cyr  TEXT NOT NULL,
  name_lat  TEXT NOT NULL
);

CREATE TABLE settlements (
  id               TEXT PRIMARY KEY,   -- naselje_maticni_broj
  municipality_id  TEXT NOT NULL REFERENCES municipalities(id),
  name_cyr         TEXT NOT NULL,
  name_lat         TEXT NOT NULL
);
CREATE INDEX idx_settlements_muni ON settlements(municipality_id);

CREATE TABLE streets (
  id               TEXT PRIMARY KEY,   -- ulica_maticni_broj
  settlement_id    TEXT NOT NULL REFERENCES settlements(id),
  name_cyr         TEXT NOT NULL,
  name_lat         TEXT NOT NULL,
  name_norm        TEXT NOT NULL       -- normalized Cyrillic key for matching
);
CREATE INDEX idx_streets_settlement ON streets(settlement_id);
CREATE INDEX idx_streets_norm ON streets(settlement_id, name_norm);

-- ── Address register (the big table, ~2.48M active rows) ────────────────────
CREATE TABLE addresses (
  id               INTEGER PRIMARY KEY,-- kucni_broj_id
  street_id        TEXT NOT NULL REFERENCES streets(id),
  settlement_id    TEXT NOT NULL REFERENCES settlements(id),
  municipality_id  TEXT NOT NULL REFERENCES municipalities(id),
  house_num        INTEGER,            -- numeric part of the house number
  house_suffix     TEXT NOT NULL DEFAULT '', -- normalized Cyrillic suffix ('' if none)
  house_raw        TEXT NOT NULL,      -- original Cyrillic, e.g. '190Б'
  lat              REAL NOT NULL,      -- WGS84
  lon              REAL NOT NULL,
  x                REAL NOT NULL,      -- UTM Zone 34N (EPSG:32634) easting
  y                REAL NOT NULL       -- UTM Zone 34N northing
);
CREATE INDEX idx_addr_street ON addresses(street_id, house_num);
CREATE INDEX idx_addr_settlement ON addresses(settlement_id);
CREATE INDEX idx_addr_bbox ON addresses(lat, lon);

-- ── Polling stations ────────────────────────────────────────────────────────
CREATE TABLE polling_stations (
  id                 INTEGER PRIMARY KEY,
  municipality_id    TEXT NOT NULL REFERENCES municipalities(id),
  number             INTEGER NOT NULL,        -- Ред.бр within the municipality
  name_cyr           TEXT NOT NULL,
  name_lat           TEXT NOT NULL,
  address_cyr        TEXT NOT NULL,
  address_lat        TEXT NOT NULL,
  raw_coverage_text  TEXT NOT NULL,           -- verbatim coverage cell for the reviewer
  source_file        TEXT NOT NULL,
  is_amendment       INTEGER NOT NULL DEFAULT 0 -- station was touched by an amendment
);
CREATE INDEX idx_ps_muni ON polling_stations(municipality_id, number);

-- ── Parsed coverage segments (re-run safe) ──────────────────────────────────
-- effective value = COALESCE(manual_json, parsed_json).
-- The pipeline only ever writes parsed_json; manual_json/manual_locked are human-owned.
CREATE TABLE coverage_segments (
  id              INTEGER PRIMARY KEY,
  station_id      INTEGER NOT NULL REFERENCES polling_stations(id),
  settlement_raw  TEXT,
  street_raw      TEXT NOT NULL,
  street_id       TEXT REFERENCES streets(id),  -- resolved (nullable)
  kind            TEXT NOT NULL,    -- street_numbers|whole_street|range|named_block|unknown
  parsed_json     TEXT NOT NULL,    -- machine output (overwritten on every re-run)
  manual_json     TEXT,             -- human override (NULL until edited; never overwritten)
  manual_locked   INTEGER NOT NULL DEFAULT 0,
  confidence      REAL NOT NULL DEFAULT 0,
  needs_review    INTEGER NOT NULL DEFAULT 0,
  parse_dialect   TEXT,             -- structured|compact
  source          TEXT NOT NULL DEFAULT 'base', -- base|amendment
  amendment_note  TEXT              -- verbatim amendment instruction, if any
);
CREATE INDEX idx_seg_station ON coverage_segments(station_id);
CREATE INDEX idx_seg_review ON coverage_segments(needs_review);

-- ── Amendment audit (every surgical op parsed from izmena/dopuna/ispravka docs) ──
CREATE TABLE amendments (
  id                INTEGER PRIMARY KEY,
  municipality_id   TEXT NOT NULL REFERENCES municipalities(id),
  station_number    INTEGER NOT NULL,
  street_raw        TEXT,
  op                TEXT NOT NULL,   -- replace_range|add_house|remove_house|fix_street_name|add_street|other
  old_value         TEXT,
  new_value         TEXT,
  raw_instruction   TEXT NOT NULL,   -- verbatim bullet from the amendment doc
  source_file       TEXT NOT NULL,
  applied           INTEGER NOT NULL DEFAULT 0,
  target_segment_id INTEGER REFERENCES coverage_segments(id)
);
CREATE INDEX idx_amend_muni ON amendments(municipality_id, station_number);

-- ── Matched addresses (one address point -> exactly one station) ────────────
CREATE TABLE station_address_links (
  station_id    INTEGER NOT NULL REFERENCES polling_stations(id),
  address_id    INTEGER NOT NULL REFERENCES addresses(id),
  segment_id    INTEGER REFERENCES coverage_segments(id),
  match_method  TEXT NOT NULL,    -- exact|fuzzy|range|whole_street|manual
  confidence    REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (station_id, address_id)
);
CREATE INDEX idx_link_addr ON station_address_links(address_id);
CREATE INDEX idx_link_station ON station_address_links(station_id);

-- ── Coverage polygons (GeoJSON per station, WGS84) ──────────────────────────
CREATE TABLE polygons (
  station_id   INTEGER PRIMARY KEY REFERENCES polling_stations(id),
  geojson      TEXT NOT NULL,      -- Feature geometry
  area_m2      REAL,
  point_count  INTEGER,
  computed_at  TEXT
);
