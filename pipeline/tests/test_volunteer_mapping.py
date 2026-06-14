"""Tests for the volunteer-file → municipality resolver — see
docs/parsing-matching/09-volunteer-mapping.md.

`map_volunteer_polygons.resolve(filename, idx)` is pure given a `RegisterIndex`, so we build a
small synthetic register by hand (a handful of real municipalities + the three parent/child
city-district pairs) instead of loading municipalities.parquet. The index also needs the set of
muni ids that actually ship polygons, to drive the child-GO → parent fold.
"""

import pytest

from map_volunteer_polygons import build_register_index, resolve, split_prefix

# (id, name_lat, parent_id) — mirrors municipalities.parquet rows for the cases under test.
ROWS = [
    {"id": 89010, "name_lat": "NOVI SAD", "parent_id": None},
    {"id": 80080, "name_lat": "BAČKI PETROVAC", "parent_id": None},
    {"id": 70203, "name_lat": "PALILULA (BEOGRAD)", "parent_id": None},
    {"id": 71323, "name_lat": "PALILULA (NIŠ)", "parent_id": None},
    {"id": 70947, "name_lat": "POŽAREVAC", "parent_id": None},
    {"id": 71340, "name_lat": "KOSTOLAC", "parent_id": 70947},
    {"id": 71145, "name_lat": "UŽICE", "parent_id": None},
    {"id": 71366, "name_lat": "SEVOJNO", "parent_id": 71145},
    {"id": 70432, "name_lat": "VRANJE", "parent_id": None},
    {"id": 71358, "name_lat": "VRANJSKA BANJA", "parent_id": 70432},
]
# Parents ship polygons; the three child districts do NOT (their stations fold into the parent).
POLYGON_IDS = {"89010", "80080", "70203", "71323", "70947", "71145", "70432"}

IDX = build_register_index(ROWS, POLYGON_IDS)


class TestSplitPrefix:
    @pytest.mark.parametrize("stem,prefix,core", [
        ("NOVI_SAD", None, "NOVI_SAD"),                       # plain, no separator
        ("NIŠ_-_PALILULA", "NIŠ", "PALILULA"),                # _-_ separator
        ("POŽAREVAC_-_KOSTOLAC", "POŽAREVAC", "KOSTOLAC"),
        ("UŽICE-SEVOJNO", "UŽICE", "SEVOJNO"),                # bare - separator
        ("VRANjE-VRANjE", "VRANjE", "VRANjE"),                # self-named split
    ])
    def test_split(self, stem, prefix, core):
        assert split_prefix(stem) == (prefix, core)


class TestResolve:
    def _r(self, filename):
        return resolve(filename, IDX)

    @pytest.mark.parametrize("filename,muni_id,polygon_muni_id,status", [
        # plain names
        ("NOVI_SAD_2023.geojson", "89010", "89010", "ok"),
        ("BAČKI_PETROVAC_2023.geojson", "80080", "80080", "ok"),
        # Nj digraph folds via upper(); self-named split resolves to the parent city
        ("VRANjE-VRANjE_2023.geojson", "70432", "70432", "ok"),
        ("UŽICE-UŽICE_2023.geojson", "71145", "71145", "ok"),
        # Palilula collision: prefix picks Niš, bare name falls back to Beograd
        ("NIŠ_-_PALILULA_2023.geojson", "71323", "71323", "ambiguous_resolved"),
        ("PALILULA_2023.geojson", "70203", "70203", "ambiguous_resolved"),
        # child districts fold to the parent's polygon file
        ("POŽAREVAC_-_KOSTOLAC_2023.geojson", "71340", "70947", "child_go_merged_to_parent"),
        ("UŽICE-SEVOJNO_2023.geojson", "71366", "71145", "child_go_merged_to_parent"),
        ("VRANjE-VRANjSKA_BANjA_2023.geojson", "71358", "70432", "child_go_merged_to_parent"),
    ])
    def test_cases(self, filename, muni_id, polygon_muni_id, status):
        res = self._r(filename)
        assert res["muni_id"] == muni_id
        assert res["polygon_muni_id"] == polygon_muni_id
        assert res["status"] == status

    def test_unmatched(self):
        res = self._r("NONEXISTENT_PLACE_2023.geojson")
        assert res["muni_id"] is None
        assert res["polygon_muni_id"] is None
        assert res["status"] == "unmatched"
