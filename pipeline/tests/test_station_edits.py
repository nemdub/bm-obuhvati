"""Tests for stage03c_reconcile_edits.py — see docs/parsing-matching/10-station-edits.md.

Covers the pure `reconcile()` step: pristine stations/segments + the worker-owned edit lists
(text overrides, added stations, tombstones) -> rebuilt canonical parquets. Building tiny
parquets by hand keeps these unit-level (no D1, no full pipeline). The parser itself is
covered by test_coverage_parse; here we only assert the reconcile bookkeeping.
"""

import polars as pl
import pytest

import config
import stage03c_reconcile_edits as R
from common.coverage_parse import parse_coverage
from common.transliterate import cyr_to_lat

STATIONS_SCHEMA = {
    "id": pl.Int64, "municipality_id": pl.Utf8, "number": pl.Int64,
    "name_cyr": pl.Utf8, "name_lat": pl.Utf8, "address_cyr": pl.Utf8, "address_lat": pl.Utf8,
    "raw_coverage_text": pl.Utf8, "source_file": pl.Utf8, "is_amendment": pl.Int64,
}
SEGMENTS_SCHEMA = {
    "id": pl.Int64, "station_id": pl.Int64, "settlement_raw": pl.Utf8, "street_raw": pl.Utf8,
    "kind": pl.Utf8, "parsed_json": pl.Utf8, "parse_dialect": pl.Utf8, "source": pl.Utf8,
    "amendment_note": pl.Utf8,
}

# Two real stations in municipality 80438.
TEXT_A = "Прва, Друга"
TEXT_B = "Трећа"


def _station(sid, num, text):
    return {
        "id": sid, "municipality_id": "80438", "number": num,
        "name_cyr": "БМ", "name_lat": "BM", "address_cyr": "", "address_lat": "",
        "raw_coverage_text": text, "source_file": "x.doc", "is_amendment": 0,
    }


def _segments_for(sid, text):
    rows = []
    for idx, seg in enumerate(parse_coverage(text)):
        rows.append({
            "id": sid * 1000 + idx, "station_id": sid, "settlement_raw": seg.settlement_raw or None,
            "street_raw": seg.street_raw, "kind": seg.kind, "parsed_json": "{}",
            "parse_dialect": seg.dialect, "source": "base", "amendment_note": None,
        })
    return rows


@pytest.fixture
def pristine():
    stations = pl.DataFrame([_station(804380001, 1, TEXT_A), _station(804380002, 2, TEXT_B)],
                            schema=STATIONS_SCHEMA)
    segs = pl.DataFrame(_segments_for(804380001, TEXT_A) + _segments_for(804380002, TEXT_B),
                        schema=SEGMENTS_SCHEMA)
    return stations, segs


def _run(pristine, *, text=None, added=None, removed=None):
    stations, segs = pristine
    return R.reconcile(stations, segs, text or [], added or [], removed or [], {})


class TestNoOp:
    def test_empty_edits_returns_pristine(self, pristine):
        st, sg, stats = _run(pristine)
        assert st.height == pristine[0].height
        assert sorted(st["id"]) == sorted(pristine[0]["id"])
        assert sg.height == pristine[1].height
        assert stats == {"added": 0, "text_overrides": 0, "removed": 0, "reparsed": 0}


class TestAddedStation:
    def test_injects_station_and_segments(self, pristine):
        added = [{"id": 7, "municipality_id": "80438", "number": 99,
                  "name_cyr": "Ново БМ", "address_cyr": "Адреса", "raw_coverage_text": TEXT_A}]
        st, sg, stats = _run(pristine, added=added)
        synth = config.ADDED_STATION_BASE + 7
        row = st.filter(pl.col("id") == synth)
        assert row.height == 1
        assert row["name_lat"][0] == cyr_to_lat("Ново БМ")
        assert row["source_file"][0] == "manual"
        # Its segments are present, keyed station_id*1000+idx, with no amendment note.
        added_segs = sg.filter(pl.col("station_id") == synth)
        assert added_segs.height == len(parse_coverage(TEXT_A))
        assert set(added_segs["id"]) == {synth * 1000 + i for i in range(added_segs.height)}
        assert added_segs["amendment_note"].null_count() == added_segs.height
        assert stats["added"] == 1 and stats["reparsed"] == 1


class TestTextOverride:
    def test_reparses_corrected_text(self, pristine):
        # Station 1 was "Прва, Друга" (2 segments); correct it to a single street.
        text = [{"station_id": 804380001, "raw_coverage_text": "Четврта"}]
        st, sg, stats = _run(pristine, text=text)
        assert st.filter(pl.col("id") == 804380001)["raw_coverage_text"][0] == "Четврта"
        seg1 = sg.filter(pl.col("station_id") == 804380001)
        assert seg1.height == len(parse_coverage("Четврта"))
        # Station 2 untouched.
        assert sg.filter(pl.col("station_id") == 804380002).height == len(parse_coverage(TEXT_B))
        assert stats["text_overrides"] == 1 and stats["reparsed"] == 1

    def test_override_for_unknown_station_ignored(self, pristine):
        text = [{"station_id": 999999, "raw_coverage_text": "Пета"}]
        st, sg, stats = _run(pristine, text=text)
        assert st.height == pristine[0].height
        assert stats["reparsed"] == 0


class TestRemovedStation:
    def test_drops_station_and_segments(self, pristine):
        st, sg, stats = _run(pristine, removed=[{"station_id": 804380002}])
        assert 804380002 not in set(st["id"])
        assert sg.filter(pl.col("station_id") == 804380002).height == 0
        # Station 1 survives.
        assert 804380001 in set(st["id"])
        assert stats["removed"] == 1

    def test_remove_wins_over_text_override(self, pristine):
        # A station both text-corrected and removed should be gone, not re-parsed.
        st, sg, stats = _run(
            pristine,
            text=[{"station_id": 804380001, "raw_coverage_text": "Шеста"}],
            removed=[{"station_id": 804380001}],
        )
        assert 804380001 not in set(st["id"])
        assert sg.filter(pl.col("station_id") == 804380001).height == 0
        assert stats["reparsed"] == 0
