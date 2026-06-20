"""Shared paths and constants for the bm-obuhvati pipeline.

All stages import from here. Stage outputs land in ``artifacts/`` (gitignored).
"""

from __future__ import annotations

from pathlib import Path

# ── Paths ───────────────────────────────────────────────────────────────────
PIPELINE_DIR = Path(__file__).resolve().parent
ROOT_DIR = PIPELINE_DIR.parent
DATA_DIR = ROOT_DIR / "data"
ARTIFACTS_DIR = PIPELINE_DIR / "artifacts"

REGISTER_CSV = DATA_DIR / "kucni_broj.csv"
ULICA_CSV = DATA_DIR / "ulica.csv"   # official street register (authoritative street list + line geometry)
NASELJE_CSV = DATA_DIR / "naselje.csv"   # official settlement (naselje) boundary polygons (UTM34N WKT)
DOCS_DIR = DATA_DIR / "polling_stations_2022"
OPSTINE_GEOJSON = DATA_DIR / "opstine.geojson"   # official municipality boundaries (WGS84)
GRADOVI_GEOJSON = DATA_DIR / "gradovi.geojson"   # companion city layer (optional; fills munis missing above)

# Stage outputs
ADDRESSES_PARQUET = ARTIFACTS_DIR / "addresses.parquet"
MUNICIPALITIES_PARQUET = ARTIFACTS_DIR / "municipalities.parquet"
SETTLEMENTS_PARQUET = ARTIFACTS_DIR / "settlements.parquet"
STREETS_PARQUET = ARTIFACTS_DIR / "streets.parquet"
STREET_GEOMETRY_PARQUET = ARTIFACTS_DIR / "street_geometry.parquet"  # WGS84 line geometry for no-house streets
SETTLEMENT_GEOMETRY_PARQUET = ARTIFACTS_DIR / "settlement_geometry.parquet"  # UTM34N settlement boundary polygons (WKT)
STATION_SETT_CLAIMS_PARQUET = ARTIFACTS_DIR / "station_settlement_claims.parquet"  # station_id -> claimed settlement_id (whole-settlement coverage)
STATIONS_PARQUET = ARTIFACTS_DIR / "stations.parquet"
AMENDMENTS_RAW_PARQUET = ARTIFACTS_DIR / "amendments_raw.parquet"  # raw amendment doc text
SEGMENTS_RAW_PARQUET = ARTIFACTS_DIR / "segments_raw.parquet"        # stage03 output
SEGMENTS_AMENDED_PARQUET = ARTIFACTS_DIR / "segments_amended.parquet" # stage03b output
SEGMENTS_PARQUET = ARTIFACTS_DIR / "segments.parquet"                 # stage04 final output
AMENDMENTS_PARQUET = ARTIFACTS_DIR / "amendments.parquet"
LINKS_PARQUET = ARTIFACTS_DIR / "links.parquet"
POLYGONS_PARQUET = ARTIFACTS_DIR / "polygons.parquet"
MUNI_BOUNDARIES_PARQUET = ARTIFACTS_DIR / "muni_boundaries.parquet"  # simplified boundaries for the UI
DOC_MUNI_MAP = ARTIFACTS_DIR / "doc_municipality_map.csv"  # filename -> municipality (review me)
OVERRIDES_JSON = ARTIFACTS_DIR / "overrides.json"  # reviewer edits exported from D1 (fetch_overrides.sh)
ADDITIONS_JSON = ARTIFACTS_DIR / "additions.json"  # reviewer-added street claims (fetch_overrides.sh)
DIRTY_SNAPSHOT_JSON = ARTIFACTS_DIR / "dirty_snapshot.json"  # station_status dirty rows at fetch time
CLEAR_DIRTY_SQL = ARTIFACTS_DIR / "clear_dirty.sql"  # race-safe dirty=0 UPDATEs (post-import)
ADDED_SEG_BASE = 9_000_000_000_000  # synthetic segment-id base (shared with the Worker)

# Station-level reviewer edits (worker-owned; exported by fetch_overrides.sh, applied by
# stage03c_reconcile_edits.py). See docs/parsing-matching/10-station-edits.md.
TEXT_OVERRIDES_JSON = ARTIFACTS_DIR / "text_overrides.json"      # corrected raw coverage text
ADDED_STATIONS_JSON = ARTIFACTS_DIR / "added_stations.json"      # brand-new stations
REMOVED_STATIONS_JSON = ARTIFACTS_DIR / "removed_stations.json"  # tombstoned stations
# Pristine (edit-free) snapshots stage03c rebuilds the canonical parquets from each recompute,
# so reverting a text fix or restoring a removed station recovers without a full re-parse.
# Refreshed by stage03b at the end of a full rebuild (and bootstrapped by stage03c if missing).
STATIONS_PRISTINE_PARQUET = ARTIFACTS_DIR / "stations_pristine.parquet"
SEGMENTS_AMENDED_PRISTINE_PARQUET = ARTIFACTS_DIR / "segments_amended_pristine.parquet"
ADDED_STATION_BASE = 9_500_000_000_000  # synthetic station-id base (shared with the Worker)

SQLITE_OUT = ARTIFACTS_DIR / "bm.sqlite"

# ── Coordinate reference systems ────────────────────────────────────────────
UTM_34N = 32634  # register native CRS (meters)
WGS84 = 4326     # output CRS for mapping (lat/lon)

# ── Voronoi tuning ──────────────────────────────────────────────────────────
HULL_BUFFER_M = 200.0       # settlement boundary = buffered convex hull of its points
SIMPLIFY_TOL_M = 5.0        # polygon simplification tolerance (meters)
# Each station polygon is clipped to this buffer around its OWN addresses, so it hugs the
# listed addresses instead of sprawling across empty land out to the settlement hull.
# Large enough to bridge addresses across a street / along a block, small enough to trim
# parks and edges.
POLYGON_CLIP_BUFFER_M = 180.0
# Morphological-close radius applied to each merged station polygon before simplification.
# The per-cell octagons touch through pinch points, so unary_union threads needle-thin
# zero-width slits into the boundary that render as spikes shooting toward the centre
# ("intestines"). A small dilate-then-erode heals them; the simplify pass below removes the
# buffer's rounding, so the net polygon has FEWER vertices and is valid. Keep well under the
# smallest real coverage feature so it never bridges distinct parts or fills real concavities.
POLYGON_DESPIKE_M = 3.0
BOUNDARY_SIMPLIFY_TOL_M = 20.0  # simplification of municipality boundaries for the UI overlay

# ── Matching tuning ─────────────────────────────────────────────────────────
STREET_FUZZY_MIN = 90       # rapidfuzz score below which a street match needs review
STREET_FUZZY_MUNI_MIN = 93  # stricter cutoff for the muni-wide fuzzy fallback used only by
                            # stations with no home settlement (Belgrade/Niš city-munis)

# Proximity pass (stage04): a polling station covers a contiguous neighbourhood, so a
# street the lexical ladder can't resolve is almost always physically near the streets the
# station ALREADY covers — and one no other station has claimed. The search radius is
# adaptive: it scales with how spread-out the station's own matched addresses are.
PROXIMITY_RADIUS_FACTOR = 2.0       # search radius = 2× the station's own coverage extent
PROXIMITY_RADIUS_FLOOR_M = 400.0    # but never tighter than this (dense city blocks)
PROXIMITY_RADIUS_CAP_M = 3000.0     # nor wider than this (sprawling rural stations)
STREET_FUZZY_PROX_MIN = 90          # name-similarity cutoff for the proximity fuzzy fallback

# ── OSM (Nominatim) fallback (stage04) ──────────────────────────────────────
# Last resort, after the proximity pass: a street/settlement the register can't place at all
# (e.g. the Sombor hamlet "Жарковац", which the register encodes only as suffixes on retired,
# address-less streets) is geocoded against OpenStreetMap, scoped to the station's
# municipality, and its geometry is drawn as the coverage. Responses are cached on disk and
# committed (data/osm_cache.json) so a recompute never re-queries the same name. See
# pipeline/common/osm.py and docs/parsing-matching/05-street-resolution.md.
OSM_CACHE_JSON = DATA_DIR / "osm_cache.json"            # committed Nominatim response cache
OSM_CLAIMS_PARQUET = ARTIFACTS_DIR / "osm_claims.parquet"  # station_id -> OSM coverage geometry (UTM WKT)
OSM_REJECTED_PARQUET = ARTIFACTS_DIR / "osm_rejected.parquet"  # segment ids whose OSM estimate stage05 discarded
# Public Nominatim by default; point NOMINATIM_URL at a self-hosted instance to avoid the
# public service's 1 req/s policy. A descriptive User-Agent is required by that policy.
NOMINATIM_URL = "https://nominatim.openstreetmap.org"
NOMINATIM_USER_AGENT = "bm-obuhvati/1.0 (https://github.com/dubravac-nemanja/bm-obuhvati; polling-station coverage)"
NOMINATIM_RATE_LIMIT_S = 1.0        # min seconds between live calls (public policy: <=1 req/s)
OSM_STREET_BUFFER_M = 40.0          # half-width buffer for a geocoded street LineString
OSM_POINT_BUFFER_M = 300.0          # radius buffer for a geocoded place node with no area
# Geographic sanity check on a geocoded OSM claim: a common street name (e.g. "Маршала Тита")
# resolves to a same-named place elsewhere in the municipality, dropping a polygon far from the
# station's real coverage. Reject an OSM claim that sits farther than this from EVERY resolved-
# street centroid the station already has. Stations with no resolved coverage have no anchor and
# are exempt (OSM is then the only signal). ~3 km mirrors PROXIMITY_RADIUS_CAP_M.
OSM_MAX_COVERAGE_DIST_M = 3000.0
# A geocoded OSM claim draws a whole street/area; when the register can't place the doc street
# (e.g. a town street absent from the register), the OSM line is drawn in full and can run over a
# neighbouring street whose addresses belong to OTHER stations — violating one-address-one-station
# and looking like wrong coverage. stage05 rejects an OSM claim whose footprint contains at least
# this many matched addresses belonging to OTHER stations AND more of them than the claiming
# station's own (a legitimate register-gap claim sits on addresses the register lacks, so few/no
# foreign matched points fall inside it).
OSM_FOREIGN_REJECT_MIN = 10

# ── Manual overrides for doc filename -> municipality (Latin register name). ──
# Auto-matching in stage02 handles most files; add corrections here when the
# fuzzy match is wrong. Keys are the on-disk filenames; values are the register
# opstina_ime_lat to bind to.
# ── Street aliases ──────────────────────────────────────────────────────────
# Documents sometimes use a different (older/colloquial) name than the register's
# official one — too different for safe fuzzy matching. Hand-maintained:
# (municipality_id, doc street name) -> register street name. Both sides are
# normalize_street()-ed at lookup, so any spelling/case works here.
STREET_ALIASES: dict[tuple[str, str], str] = {
    ("80381", "Пинкијева"): "Хероја Пинкија",  # Sombor
    # "Нушићева" = "Бранислава Нушића". CAUTION: aliases replace the name BEFORE lookup,
    # municipality-wide — in munis where НУШИЋЕВА is also a real register street this
    # hijacks correctly-matching stations (verified: broke 4 Požarevac stations). Only
    # safe where the alias target is the sole plausible street for the affected station.
    ("70785", "Нушићева"): "Бранислава Нушића",  # Majdanpek
}

DOC_MUNI_OVERRIDES: dict[str, str] = {
    # "Palilula" is ambiguous (Belgrade + Niš both have a Палилула); the fuzzy match is
    # unstable. These two docs are Belgrade Palilula; Niš's Palilula comes from the Niš
    # sectioned doc. Without this, the Belgrade stations leak into Niš Palilula.
    "Palilula-glasacka-mesta.doc": "PALILULA (BEOGRAD)",
    "Palilula.docx": "PALILULA (BEOGRAD)",
}


# ── City municipality groups ────────────────────────────────────────────────
# The address register splits big cities into city-municipalities (each its own
# opstina id), but RIK publishes ONE polling-station document per city. Group the
# members under a representative so street/address matching spans the whole city and
# the members don't show as separate 0-station entries. `rep` is the opstina the
# document mapped to; `name_*` overrides the rep's display name where needed.
# Scope-merge groups: NON-sectioned city docs where the member town has no stations of its
# own (numbering is continuous). The member's addresses are matched by the rep's stations,
# and the member is hidden in the UI (parent_id).
CITY_GROUPS: list[dict] = [
    {"rep": "70432", "members": ["71358"]},  # Vranje + Vranjska Banja
    {"rep": "70947", "members": ["71340"]},  # Požarevac + Kostolac
    {"rep": "71145", "members": ["71366"]},  # Užice + Sevojno
]

# Sectioned docs: one document covering several city-municipalities, each in its own
# "ГРАДСКА ОПШТИНА <name>" section with numbering restarting per section. Each station is
# assigned to its section's opstina (so numbers don't collide); the members are nested in
# the UI under the city (see the Worker's CITY_DISPLAY), not hidden.
SECTIONED_DOCS: dict[str, dict[str, str]] = {
    "Nis-glasacka-mesta.doc": {  # City of Niš — 5 city-municipalities
        "МЕДИЈАНА": "71331",
        "ПАЛИЛУЛА": "71323",
        "ПАНТЕЛЕЈ": "71307",
        "ЦРВЕНИ КРСТ": "71315",
        "НИШКА БАЊА": "71285",
    },
}

_MEMBER_TO_REP: dict[str, str] = {}
_REP_NAME: dict[str, tuple[str, str]] = {}
for _g in CITY_GROUPS:
    for _m in _g["members"]:
        _MEMBER_TO_REP[_m] = _g["rep"]
    if "name_cyr" in _g:
        _REP_NAME[_g["rep"]] = (_g["name_cyr"], _g["name_lat"])


def group_rep(municipality_id: str) -> str:
    """Representative opstina id for matching scope (members -> rep, others -> self)."""
    return _MEMBER_TO_REP.get(municipality_id, municipality_id)


def parent_of(municipality_id: str) -> str | None:
    """Rep id for a grouped member (so it can be hidden in the UI), else None."""
    return _MEMBER_TO_REP.get(municipality_id)


def rep_name(municipality_id: str) -> tuple[str, str] | None:
    return _REP_NAME.get(municipality_id)


def ensure_artifacts() -> None:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
