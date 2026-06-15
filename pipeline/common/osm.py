"""Cached Nominatim (OpenStreetMap) geocoder — the stage04 last-resort fallback.

When the register can't place a street or settlement at all (e.g. the Sombor hamlet
``Жарковац``, which the register encodes only as suffixes on retired, address-less streets,
while the only ``ЖАРКОВАЦ`` *settlement* polygon sits ~150 km away in Ruma), we geocode the
name against OpenStreetMap **scoped to the station's municipality** and draw the returned
geometry as the coverage.

Every response — hit OR miss — is cached in a committed JSON file (``data/osm_cache.json``)
keyed by ``kind|muni_id|normalized_name``, so a recompute never re-queries a name we've
already looked up and coverage stays reproducible across clean checkouts / CI. Set
``OSM_OFFLINE=1`` to run cache-only (a miss returns ``None`` without touching the network);
tests and reproducible runs use it.

Network access uses the stdlib only (``urllib``); geometry uses shapely/pyproj already in the
project. The public Nominatim service caps usage at 1 req/s and requires a descriptive
User-Agent — both honoured here; point ``NOMINATIM_URL`` at a self-hosted instance to lift
the limit.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import shapely
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform
from pyproj import Transformer

import config
from common.normalize import normalize_street

_TO_UTM = Transformer.from_crs(config.WGS84, config.UTM_34N, always_xy=True)

# In-process cache: loaded once, written back only if new entries were added.
_cache: dict[str, dict] | None = None
_cache_dirty = False
_last_call_ts = 0.0
_pending_writes = 0
_AUTOFLUSH_EVERY = 50  # persist crawl progress periodically so an interruption loses little


def _offline() -> bool:
    return os.environ.get("OSM_OFFLINE", "") not in ("", "0")


def _load_cache() -> dict[str, dict]:
    global _cache
    if _cache is None:
        if config.OSM_CACHE_JSON.exists():
            _cache = json.loads(config.OSM_CACHE_JSON.read_text(encoding="utf-8"))
        else:
            _cache = {}
    return _cache


def flush_cache() -> None:
    """Persist newly-added cache entries (committed file). No-op if nothing changed."""
    global _cache_dirty
    if not _cache_dirty or _cache is None:
        return
    config.OSM_CACHE_JSON.parent.mkdir(parents=True, exist_ok=True)
    config.OSM_CACHE_JSON.write_text(
        json.dumps(_cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _cache_dirty = False


def cache_key(kind: str, muni_id: str, name: str) -> str:
    return f"{kind}|{muni_id}|{normalize_street(name)}"


# OSM class/type acceptance — the bounded muni search will happily return SOME object for a
# generic descriptive token ("ЗГРАДА"=building→amenity=school, "СТАДИОН"=stadium→leisure=pitch,
# "ЦИГЛАНА"=brickworks→landuse=industrial, "1 УПРАВА"→place=house), so we keep only real
# settlements/places and real streets. Applied at read time, so retuning needs no re-query.
_PLACE_TYPES = frozenset({
    "city", "town", "village", "hamlet", "suburb", "neighbourhood", "quarter", "borough",
    "locality", "isolated_dwelling", "farm", "allotments", "city_block",
})
_HIGHWAY_SKIP = frozenset({
    "footway", "path", "cycleway", "steps", "bridleway", "construction", "proposed",
    "bus_stop", "platform", "services", "rest_area", "raceway",
})


def _acceptable(kind: str, row: dict) -> bool:
    cls = row.get("class")
    typ = row.get("type")
    if not row.get("geojson"):
        return False
    if kind == "street":
        return cls == "highway" and typ not in _HIGHWAY_SKIP
    # settlement / place: a named populated place or an administrative area.
    return (cls == "place" and typ in _PLACE_TYPES) or (cls == "boundary" and typ == "administrative")


def _trim(row: dict) -> dict:
    return {
        "osm_type": row.get("osm_type"),
        "osm_id": row.get("osm_id"),
        "display_name": row.get("display_name"),
        "class": row.get("category", row.get("class")),
        "type": row.get("type"),
        "geojson": row.get("geojson"),
    }


class OsmRequestError(Exception):
    """A network/HTTP/parse failure — distinct from a genuine empty result, so the caller can
    decline to cache it (a transient 429 must NOT be frozen as a permanent miss)."""


def _request(params: dict) -> list:
    """One rate-limited Nominatim /search call. Returns the parsed JSON list (``[]`` is a
    genuine no-result); raises ``OsmRequestError`` on a network/HTTP/parse failure."""
    global _last_call_ts
    wait = config.NOMINATIM_RATE_LIMIT_S - (time.monotonic() - _last_call_ts)
    if wait > 0:
        time.sleep(wait)
    url = f"{config.NOMINATIM_URL}/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": config.NOMINATIM_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # network / HTTP (429!) / JSON — do NOT cache, retry next run
        raise OsmRequestError(str(exc)) from exc
    finally:
        _last_call_ts = time.monotonic()
    return data if isinstance(data, list) else []


def geocode(kind: str, name: str, muni_id: str, muni_name: str,
            viewbox: tuple[float, float, float, float] | None = None) -> dict | None:
    """Geocode ``name`` (a ``settlement`` or ``street``) scoped to municipality ``muni_id``.

    Returns the chosen OSM result ``{osm_type, osm_id, display_name, class, type, geojson}``
    or ``None`` (no result). Cached by ``kind|muni_id|normalized_name``; cached misses also
    short-circuit. In offline mode a cache miss returns ``None`` without a network call.

    ``viewbox`` is the municipality bbox in WGS84 ``(min_lon, min_lat, max_lon, max_lat)``;
    when given it bounds the search (``bounded=1``) so we get *this* municipality's place,
    not a same-named one elsewhere in Serbia.
    """
    global _cache_dirty, _pending_writes
    key = cache_key(kind, muni_id, name)
    cache = _load_cache()
    if key not in cache:
        if _offline():
            return None  # do NOT cache: a later live run should still try this name
        # Ask for a few candidates and keep the first that is a real place/street, so a wrong
        # top hit (a POI sharing the token) doesn't mask a genuine match ranked just below it.
        params = {"format": "jsonv2", "polygon_geojson": 1, "limit": 5, "countrycodes": "rs"}
        if kind == "street":
            params["street"] = name
            params["city"] = muni_name
        else:  # settlement / place
            params["q"] = f"{name}, {muni_name}"
        if viewbox is not None:
            params["viewbox"] = ",".join(f"{c:.6f}" for c in viewbox)
            params["bounded"] = 1
        try:
            rows = [_trim(r) for r in _request(params)]
        except OsmRequestError as exc:
            # Transient failure (rate limit / network): leave it UNcached so the next run retries,
            # and return nothing for now rather than freezing a false miss.
            print(f"    [osm] request failed ({exc}); skipping (will retry next run)")
            return None
        cache[key] = {"rows": rows, "queried_at": datetime.now(timezone.utc).isoformat()}
        _cache_dirty = True
        _pending_writes += 1
        if _pending_writes >= _AUTOFLUSH_EVERY:
            flush_cache()
            _pending_writes = 0

    # Acceptance is applied at READ time (on fresh fetches AND cache hits), so the class/type
    # filter can be retuned without re-querying. Returns the first acceptable candidate row.
    for row in cache[key].get("rows", []):
        if _acceptable(kind, row):
            return row
    return None


def to_coverage_geom(result: dict) -> BaseGeometry | None:
    """OSM ``geojson`` (WGS84) → a UTM-34N coverage geometry, validity-fixed.

    Areas (Polygon / MultiPolygon) are used as-is; a street ``LineString`` is buffered by
    ``OSM_STREET_BUFFER_M`` and a bare place ``Point`` by ``OSM_POINT_BUFFER_M`` into a blob
    (both are approximations — that's why every OSM segment is force-flagged for review).
    Returns ``None`` if the geometry is empty/unusable.
    """
    gj = result.get("geojson")
    if not gj:
        return None
    try:
        g = shp_transform(lambda xs, ys: _TO_UTM.transform(xs, ys), shape(gj))
    except Exception:
        return None
    if g.is_empty:
        return None
    gtype = g.geom_type
    # Buffer lines/points BEFORE any validity fix — buffer(0) collapses a non-areal geometry to
    # empty, so the polygon path is the only one that may use it.
    if gtype in ("LineString", "MultiLineString"):
        return g.buffer(config.OSM_STREET_BUFFER_M)
    if gtype in ("Point", "MultiPoint"):
        return g.buffer(config.OSM_POINT_BUFFER_M)
    g = shapely.make_valid(g.buffer(0))  # areas: heal self-intersections from OSM
    return None if g.is_empty else g
