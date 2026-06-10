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
DOCS_DIR = DATA_DIR / "polling_stations_2022"
OPSTINE_GEOJSON = DATA_DIR / "opstine.geojson"   # official municipality boundaries (WGS84)
GRADOVI_GEOJSON = DATA_DIR / "gradovi.geojson"   # companion city layer (optional; fills munis missing above)

# Stage outputs
ADDRESSES_PARQUET = ARTIFACTS_DIR / "addresses.parquet"
MUNICIPALITIES_PARQUET = ARTIFACTS_DIR / "municipalities.parquet"
SETTLEMENTS_PARQUET = ARTIFACTS_DIR / "settlements.parquet"
STREETS_PARQUET = ARTIFACTS_DIR / "streets.parquet"
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
ADDED_SEG_BASE = 9_000_000_000_000  # synthetic segment-id base (shared with the Worker)
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
BOUNDARY_SIMPLIFY_TOL_M = 20.0  # simplification of municipality boundaries for the UI overlay

# ── Matching tuning ─────────────────────────────────────────────────────────
STREET_FUZZY_MIN = 90       # rapidfuzz score below which a street match needs review

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
