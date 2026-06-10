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
DOC_MUNI_MAP = ARTIFACTS_DIR / "doc_municipality_map.csv"  # filename -> municipality (review me)
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

# ── Matching tuning ─────────────────────────────────────────────────────────
STREET_FUZZY_MIN = 90       # rapidfuzz score below which a street match needs review

# ── Manual overrides for doc filename -> municipality (Latin register name). ──
# Auto-matching in stage02 handles most files; add corrections here when the
# fuzzy match is wrong. Keys are the on-disk filenames; values are the register
# opstina_ime_lat to bind to.
DOC_MUNI_OVERRIDES: dict[str, str] = {}


def ensure_artifacts() -> None:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
