"""Tests for the OSM (Nominatim) fallback — see docs/parsing-matching/05-street-resolution.md §5.16.

All offline (`OSM_OFFLINE`/monkeypatched), never touches the network:
- `common.osm`: cache hit, cached miss, offline miss (not cached), live-hit caches & flushes,
  and geometry extraction (Polygon as-is, LineString/Point buffered, WGS84→UTM34N).
- `stage04_match_addresses.osm_fallback_pass`: a fake geocoder produces an osm_claim + sets
  method='osm'; a genuine miss leaves the segment untouched and emits nothing.
"""

import json
import math

import pytest
import shapely
import shapely.ops
from shapely.geometry import shape

import config
from common import osm
import stage04_match_addresses as S4


# A small WGS84 area near Sombor (lon ~19.1, lat ~45.77) and its rough UTM-34N footprint,
# used both for OSM geometry fixtures and as the municipality clip boundary.
SOMBOR_LON, SOMBOR_LAT = 19.10, 45.77
SOMBOR_GEOJSON_POLY = {
    "type": "Polygon",
    "coordinates": [[
        [19.08, 45.75], [19.12, 45.75], [19.12, 45.79], [19.08, 45.79], [19.08, 45.75],
    ]],
}


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Point the cache at a throwaway file and reset the module's in-process state."""
    monkeypatch.setattr(config, "OSM_CACHE_JSON", tmp_path / "osm_cache.json")
    monkeypatch.setattr(osm, "_cache", None)
    monkeypatch.setattr(osm, "_cache_dirty", False)
    monkeypatch.delenv("OSM_OFFLINE", raising=False)
    yield


def _sombor_utm_box():
    """Sombor clip boundary in UTM34N — the bbox of the WGS84 fixture, reprojected."""
    g = shapely.ops.transform(
        lambda xs, ys: osm._TO_UTM.transform(xs, ys), shape(SOMBOR_GEOJSON_POLY))
    return g.buffer(0)


# ── geometry extraction ──────────────────────────────────────────────────────

class TestToCoverageGeom:
    def test_polygon_used_as_area(self):
        g = osm.to_coverage_geom({"geojson": SOMBOR_GEOJSON_POLY})
        assert g is not None and g.geom_type in ("Polygon", "MultiPolygon")
        assert g.area > 1_000_000  # ~0.04°×0.04° near 45°N is several km² in UTM meters

    def test_linestring_is_buffered(self):
        line = {"type": "LineString", "coordinates": [[19.10, 45.77], [19.11, 45.77]]}
        g = osm.to_coverage_geom({"geojson": line})
        assert g is not None and g.geom_type in ("Polygon", "MultiPolygon")
        # A ~775 m segment buffered by OSM_STREET_BUFFER_M on each side has a sane area.
        assert g.area > config.OSM_STREET_BUFFER_M ** 2

    def test_point_is_buffered_to_radius(self):
        pt = {"type": "Point", "coordinates": [SOMBOR_LON, SOMBOR_LAT]}
        g = osm.to_coverage_geom({"geojson": pt})
        assert g is not None
        expected = math.pi * config.OSM_POINT_BUFFER_M ** 2
        assert g.area == pytest.approx(expected, rel=0.05)

    def test_missing_geojson_returns_none(self):
        assert osm.to_coverage_geom({"geojson": None}) is None
        assert osm.to_coverage_geom({}) is None


# ── cache behaviour ──────────────────────────────────────────────────────────

_DEFAULT = object()


def _row(cls="place", typ="hamlet", osm_id=1, geojson=_DEFAULT):
    return {"osm_type": "relation", "osm_id": osm_id, "display_name": "X", "class": cls,
            "type": typ, "geojson": SOMBOR_GEOJSON_POLY if geojson is _DEFAULT else geojson}


class TestCache:
    def test_cache_hit_skips_network(self, monkeypatch):
        key = osm.cache_key("settlement", "80381", "Жарковац")
        osm._cache = {key: {"rows": [_row(osm_id=1)]}}
        monkeypatch.setattr(osm, "_request", lambda params: pytest.fail("network hit"))
        got = osm.geocode("settlement", "Жарковац", "80381", "Sombor")
        assert got and got["osm_id"] == 1

    def test_cached_miss_short_circuits(self, monkeypatch):
        key = osm.cache_key("street", "80381", "Нема")
        osm._cache = {key: {"rows": []}}
        monkeypatch.setattr(osm, "_request", lambda params: pytest.fail("network hit"))
        assert osm.geocode("street", "Нема", "80381", "Sombor") is None

    def test_offline_miss_returns_none_and_is_not_cached(self, monkeypatch):
        monkeypatch.setenv("OSM_OFFLINE", "1")
        monkeypatch.setattr(osm, "_request", lambda params: pytest.fail("network hit"))
        assert osm.geocode("settlement", "Жарковац", "80381", "Sombor") is None
        # A later live run must still try the name, so nothing was written.
        assert osm.cache_key("settlement", "80381", "Жарковац") not in osm._load_cache()

    def test_live_hit_is_cached_and_flushed(self, monkeypatch):
        monkeypatch.setattr(osm, "_request",
                            lambda params: [_row("place", "hamlet", 42)])
        got = osm.geocode("settlement", "Жарковац", "80381", "Sombor")
        assert got and got["osm_id"] == 42 and got["type"] == "hamlet"
        osm.flush_cache()
        on_disk = json.loads(config.OSM_CACHE_JSON.read_text(encoding="utf-8"))
        assert osm.cache_key("settlement", "80381", "Жарковац") in on_disk

    def test_request_error_is_not_cached(self, monkeypatch):
        # A 429 / network failure must NOT freeze a false miss — leave it uncached to retry.
        def boom(params):
            raise osm.OsmRequestError("429 Too Many Requests")
        monkeypatch.setattr(osm, "_request", boom)
        assert osm.geocode("settlement", "Жарковац", "80381", "Sombor") is None
        assert osm.cache_key("settlement", "80381", "Жарковац") not in osm._cache

    def test_live_miss_is_cached_as_empty(self, monkeypatch):
        monkeypatch.setattr(osm, "_request", lambda params: [])
        assert osm.geocode("street", "Измишљена", "80381", "Sombor") is None
        # Cached so a recompute doesn't re-query a name OSM doesn't have.
        key = osm.cache_key("street", "80381", "Измишљена")
        assert osm._cache[key]["rows"] == []

    def test_first_acceptable_row_wins(self, monkeypatch):
        # A POI tops the result list; the real hamlet below it is the one we keep.
        monkeypatch.setattr(osm, "_request", lambda params: [
            _row("leisure", "park", 10), _row("place", "hamlet", 11)])
        got = osm.geocode("settlement", "Видовдан", "80381", "Sombor")
        assert got and got["osm_id"] == 11


class TestAcceptanceFilter:
    """Generic descriptors resolve to POIs the bounded search returns — all must be rejected."""

    @pytest.mark.parametrize("cls,typ", [
        ("leisure", "park"), ("leisure", "pitch"), ("amenity", "school"),
        ("landuse", "industrial"), ("landuse", "forest"), ("place", "house"),
        ("highway", "footway"),
    ])
    def test_settlement_rejects_pois(self, cls, typ):
        assert osm._acceptable("settlement", _row(cls, typ)) is False

    @pytest.mark.parametrize("cls,typ", [
        ("place", "hamlet"), ("place", "village"), ("place", "locality"),
        ("boundary", "administrative"),
    ])
    def test_settlement_accepts_real_places(self, cls, typ):
        assert osm._acceptable("settlement", _row(cls, typ)) is True

    def test_street_accepts_highway_only(self):
        assert osm._acceptable("street", _row("highway", "residential")) is True
        assert osm._acceptable("street", _row("place", "hamlet")) is False
        assert osm._acceptable("street", _row("highway", "footway")) is False

    def test_no_geometry_rejected(self):
        assert osm._acceptable("settlement", _row("place", "hamlet", geojson=False)) is False


# ── stage04 integration (fake geocoder) ──────────────────────────────────────

class TestOsmFallbackPass:
    STATION_MUNI = {800: "80381"}
    MUNI_NAME = {"80381": "Sombor"}

    def _bounds(self):
        return {"80381": _sombor_utm_box()}

    def _seg(self, sid, raw, method="none", street_id=None):
        return {"id": sid, "station_id": 800, "street_raw": raw,
                "method": method, "score": 0.0, "amb_ids": [], "street_id": street_id}

    def _run(self, segs, claims_by_street=None, street_meta=None, resolved_by_station=None):
        return S4.osm_fallback_pass(segs, self.STATION_MUNI, self._bounds(), self.MUNI_NAME,
                                    claims_by_street if claims_by_street is not None else {},
                                    street_meta or {}, resolved_by_station or {})

    def test_hit_emits_claim_and_marks_osm(self, monkeypatch):
        monkeypatch.setattr(osm, "geocode", lambda kind, name, mid, mname, vb: (
            {"osm_type": "relation", "osm_id": 7, "geojson": SOMBOR_GEOJSON_POLY}
            if kind == "settlement" else None))
        seg = self._seg(8000000001, "Жарковац")
        claims = self._run([seg])
        assert len(claims) == 1
        c = claims[0]
        assert c["station_id"] == 800 and c["segment_id"] == 8000000001 and c["kind"] == "settlement"
        assert c["wkt"].startswith(("POLYGON", "MULTIPOLYGON"))
        assert seg["method"] == "osm"  # mutated in place

    def test_miss_leaves_segment_unresolved(self, monkeypatch):
        monkeypatch.setattr(osm, "geocode", lambda *a, **k: None)
        seg = self._seg(8000000002, "Непостојећа")
        claims = self._run([seg])
        assert claims == []
        assert seg["method"] == "none"

    def test_geometry_is_clipped_to_municipality(self, monkeypatch):
        # OSM returns an area straddling the muni boundary; the clip trims the outside part.
        outside = {"type": "Polygon", "coordinates": [[
            [19.10, 45.77], [19.30, 45.77], [19.30, 45.90], [19.10, 45.90], [19.10, 45.77]]]}
        monkeypatch.setattr(osm, "geocode", lambda kind, *a, **k: (
            {"osm_type": "relation", "osm_id": 9, "geojson": outside}
            if kind == "settlement" else None))
        seg = self._seg(8000000003, "Жарковац")
        claims = self._run([seg])
        assert len(claims) == 1
        clipped = shapely.from_wkt(claims[0]["wkt"])
        # Clipped geometry must sit inside the muni boundary (small tolerance for rounding).
        assert self._bounds()["80381"].buffer(1.0).contains(clipped)

    def test_weak_fuzzy_hit_overrides_and_pulls_claim(self, monkeypatch):
        # „Жарковац" fuzzy-matched the longer street „БРАНКА РАДИЧЕВИЋА ЖАРКОВАЦ"; on an OSM
        # place hit, that street's claim is pulled and the segment becomes 'osm'.
        monkeypatch.setattr(osm, "geocode", lambda kind, *a, **k: (
            {"osm_type": "relation", "osm_id": 5, "geojson": SOMBOR_GEOJSON_POLY}
            if kind == "settlement" else None))
        seg = self._seg(8000000004, "Жарковац", method="fuzzy", street_id="ST")
        meta = {"ST": {"name_norm": "БРАНКА РАДИЧЕВИЋА ЖАРКОВАЦ"}}
        claims_by_street = {"ST": [{"seg_id": 8000000004}, {"seg_id": 999}]}
        claims = self._run([seg], claims_by_street, meta)
        assert len(claims) == 1 and seg["method"] == "osm" and seg["street_id"] is None
        assert claims_by_street["ST"] == [{"seg_id": 999}]  # only this seg's claim pulled

    def test_weak_fuzzy_miss_keeps_fuzzy_match(self, monkeypatch):
        monkeypatch.setattr(osm, "geocode", lambda *a, **k: None)
        seg = self._seg(8000000005, "Жарковац", method="fuzzy", street_id="ST")
        meta = {"ST": {"name_norm": "БРАНКА РАДИЧЕВИЋА ЖАРКОВАЦ"}}
        claims_by_street = {"ST": [{"seg_id": 8000000005}]}
        claims = self._run([seg], claims_by_street, meta)
        assert claims == [] and seg["method"] == "fuzzy" and seg["street_id"] == "ST"
        assert claims_by_street == {"ST": [{"seg_id": 8000000005}]}  # claim untouched

    def test_far_claim_rejected_when_station_has_anchor(self, monkeypatch):
        # The OSM hit lands on the Sombor polygon, but the station's resolved coverage is ~19 km
        # east — far beyond OSM_MAX_COVERAGE_DIST_M — so the claim is rejected as a mis-geocode.
        monkeypatch.setattr(osm, "geocode", lambda kind, *a, **k: (
            {"osm_type": "relation", "osm_id": 7, "geojson": SOMBOR_GEOJSON_POLY}
            if kind == "settlement" else None))
        far = osm._TO_UTM.transform(19.35, 45.77)  # ~19 km east of the polygon
        seg = self._seg(8000000006, "Маршала Тита")
        claims = self._run([seg], resolved_by_station={800: [far]})
        assert claims == [] and seg["method"] == "none"  # left unresolved, no far polygon

    def test_near_claim_kept_when_station_has_anchor(self, monkeypatch):
        # Same hit, but the station's resolved coverage sits inside the polygon -> kept.
        monkeypatch.setattr(osm, "geocode", lambda kind, *a, **k: (
            {"osm_type": "relation", "osm_id": 7, "geojson": SOMBOR_GEOJSON_POLY}
            if kind == "settlement" else None))
        near = osm._TO_UTM.transform(SOMBOR_LON, SOMBOR_LAT)  # inside the polygon
        seg = self._seg(8000000007, "Жарковац")
        claims = self._run([seg], resolved_by_station={800: [near]})
        assert len(claims) == 1 and seg["method"] == "osm"


class TestWeakSubstringFuzzy:
    """`_weak_substring_fuzzy`: single-token coverage caught as a non-leading word of a street."""

    def test_non_leading_substring_is_weak(self):
        meta = {"ST": {"name_norm": "БРАНКА РАДИЧЕВИЋА ЖАРКОВАЦ"}}
        assert S4._weak_substring_fuzzy(
            {"method": "fuzzy", "street_id": "ST", "street_raw": "Жарковац"}, meta) is True

    def test_leading_word_is_not_weak(self):
        meta = {"ST": {"name_norm": "ЖАРКОВАЦ ГЛАВНА"}}
        assert S4._weak_substring_fuzzy(
            {"method": "fuzzy", "street_id": "ST", "street_raw": "Жарковац"}, meta) is False

    def test_multiword_coverage_is_not_weak(self):
        meta = {"ST": {"name_norm": "ВУКА КARАЏИЋА ЖАРКОВАЦ"}}
        assert S4._weak_substring_fuzzy(
            {"method": "fuzzy", "street_id": "ST", "street_raw": "Вука Караџића"}, meta) is False

    def test_non_fuzzy_method_is_not_weak(self):
        meta = {"ST": {"name_norm": "БРАНКА РАДИЧЕВИЋА ЖАРКОВАЦ"}}
        assert S4._weak_substring_fuzzy(
            {"method": "exact", "street_id": "ST", "street_raw": "Жарковац"}, meta) is False


class TestHasCoverage:
    """_has_coverage gates the OSM fallback: an empty (reviewer-cleared) segment gets no shape."""

    def test_empty_is_false(self):
        assert S4._has_coverage({"whole": False, "intervals": [], "singles": [], "bez_broja": False}) is False

    def test_whole_is_true(self):
        assert S4._has_coverage({"whole": True, "intervals": [], "singles": []}) is True

    def test_intervals_is_true(self):
        assert S4._has_coverage({"whole": False, "intervals": [[1, 9, "odd"]], "singles": []}) is True

    def test_bez_broja_is_true(self):
        assert S4._has_coverage({"whole": False, "intervals": [], "singles": [], "bez_broja": True}) is True


class TestOsmForeignOverlap:
    """_osm_foreign_overlap counts matched addresses inside an OSM claim, split own vs other."""

    def test_counts_split_by_station(self):
        import numpy as np
        from shapely.geometry import box
        from stage05_voronoi import _osm_foreign_overlap, UNASSIGNED
        # points: 2 own (sid 5), 3 other (sid 9), 1 unassigned — all inside the unit box;
        # 1 own point far outside.
        X = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 100.0])
        Y = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 100.0])
        sids = np.array([5, 5, 9, 9, 9, UNASSIGNED, 5])
        B = 1.0
        buckets = {}
        for k in range(len(X)):
            buckets.setdefault(int(np.floor(X[k] / B)) * 10_000_019 + int(np.floor(Y[k] / B)), []).append(k)
        own, other = _osm_foreign_overlap(box(0, 0, 1, 1), 5, X, Y, sids, buckets, B)
        assert (own, other) == (2, 3)  # the far own point and the unassigned point excluded
