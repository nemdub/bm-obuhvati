"""Tests for stage06_build_sqlite.dump_derived — the incremental D1 import.

A recompute re-derives every derived row but only a few actually change, so the import is
shipped as a per-row delta (UPSERT changed rows, DELETE vanished ones) vs. a local manifest
of what was last successfully shipped, falling back to a full DELETE+reINSERT when no manifest
exists. These tests assert the emitted SQL converges a real (FK-enforced) SQLite DB to the new
build exactly, and that an unchanged build ships nothing.
"""

from __future__ import annotations

import sqlite3

import polars as pl
import pytest

import stage06_build_sqlite as s6

ST_COLS = s6.TABLES["polling_stations"][1]
SG_COLS = s6.TABLES["coverage_segments"][1]

# Non-null defaults so the production schema's NOT NULL constraints hold.
ST_DEF = dict(municipality_id="70017", number=0, name_cyr="", name_lat="", address_cyr="",
              address_lat="", raw_coverage_text="", source_file="f", is_amendment=0)
SG_DEF = dict(station_id=1, settlement_raw="", street_raw="", street_id=None, kind="unknown",
              parsed_json="{}", manual_json=None, manual_locked=0, confidence=0.0,
              needs_review=0, parse_dialect=None, source="base", amendment_note=None,
              review_reason=None)


def _row(cols, defs, **over):
    d = {**defs, **over}
    return tuple(d.get(c, 0 if c == "id" else None) for c in cols)


def _stations(rows):
    return pl.DataFrame([_row(ST_COLS, ST_DEF, **r) for r in rows], schema=ST_COLS, orient="row")


def _segments(rows):
    return pl.DataFrame([_row(SG_COLS, SG_DEF, **r) for r in rows], schema=SG_COLS, orient="row")


def _fresh_db(path):
    con = sqlite3.connect(path)
    for mig in sorted(s6.MIGRATIONS_DIR.glob("*.sql")):
        con.executescript(mig.read_text(encoding="utf-8"))
    con.execute("INSERT OR IGNORE INTO municipalities (id,name_cyr,name_lat,parent_id) "
                "VALUES ('70017','M','M',NULL)")
    con.commit()
    con.close()
    return path


def _apply(path, sqlfile):
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys=ON")  # enforce FK so ordering bugs surface
    con.executescript(sqlfile.read_text(encoding="utf-8"))
    con.commit()
    con.close()


def _state(db):
    con = sqlite3.connect(db)
    st = sorted(con.execute("SELECT id,name_cyr FROM polling_stations").fetchall())
    sg = sorted(con.execute("SELECT id,station_id,street_raw FROM coverage_segments").fetchall())
    con.close()
    return st, sg


def _ship(present, out, state_dir):
    """Emit the dump and promote the pending manifests, as recompute.sh does after a clean
    import. Returns (counts, n_removed, mode)."""
    res = s6.dump_derived(present, out, state_dir)
    for nf in state_dir.glob("*.tsv.new"):
        nf.rename(nf.with_suffix(""))
    return res


@pytest.fixture
def env(tmp_path):
    return tmp_path / "import_derived.sql", tmp_path / "derived_state", tmp_path / "d1.sqlite"


def test_bootstrap_is_full_reload(env):
    out, state_dir, db = env
    present = {
        "polling_stations": _stations([dict(id=1, name_cyr="A"), dict(id=2, name_cyr="B")]),
        "coverage_segments": _segments([dict(id=10, station_id=1, street_raw="X"),
                                        dict(id=11, station_id=2, street_raw="Y")]),
    }
    _, n_removed, mode = _ship(present, out, state_dir)
    assert mode == "full reload" and n_removed == 0
    assert "DELETE FROM coverage_segments;" in out.read_text()  # full wipe, not id-scoped

    _apply(_fresh_db(db), out)
    assert _state(db) == ([(1, "A"), (2, "B")], [(10, 1, "X"), (11, 2, "Y")])


def test_delta_converges_db_to_new_build(env):
    out, state_dir, db = env
    build1 = {
        "polling_stations": _stations([dict(id=1, name_cyr="A"), dict(id=2, name_cyr="B")]),
        "coverage_segments": _segments([dict(id=10, station_id=1, street_raw="X"),
                                        dict(id=11, station_id=2, street_raw="Y")]),
    }
    _ship(build1, out, state_dir)
    _apply(_fresh_db(db), out)

    # change st1 + seg10, add seg12, drop station2 and its seg11.
    build2 = {
        "polling_stations": _stations([dict(id=1, name_cyr="A-NEW")]),
        "coverage_segments": _segments([dict(id=10, station_id=1, street_raw="X-NEW"),
                                        dict(id=12, station_id=1, street_raw="Z")]),
    }
    counts, n_removed, mode = _ship(build2, out, state_dir)
    assert mode == "delta"
    assert n_removed == 2  # station 2 + segment 11
    assert counts == {"polling_stations": 1, "coverage_segments": 2}  # upserts only
    sql = out.read_text()
    assert "ON CONFLICT(id) DO UPDATE" in sql          # changed rows upserted, not full reload
    assert "DELETE FROM coverage_segments;" not in sql  # never a blanket wipe in delta mode

    _apply(db, out)  # apply onto the SAME db the bootstrap populated
    assert _state(db) == ([(1, "A-NEW")], [(10, 1, "X-NEW"), (12, 1, "Z")])


def test_unchanged_build_ships_nothing(env):
    out, state_dir, _ = env
    present = {
        "polling_stations": _stations([dict(id=1, name_cyr="A")]),
        "coverage_segments": _segments([dict(id=10, station_id=1, street_raw="X")]),
    }
    _ship(present, out, state_dir)
    counts, n_removed, mode = _ship(present, out, state_dir)
    assert mode == "delta"
    assert n_removed == 0 and counts == {"polling_stations": 0, "coverage_segments": 0}
    assert out.stat().st_size == 0  # empty file -> recompute.sh's `-s` check skips the import


def test_failed_import_keeps_manifest_so_delta_reemits(env):
    """If the D1 import fails, recompute.sh does NOT promote the .tsv.new manifest, so the
    next run must re-emit the same delta against the last-known-good manifest."""
    out, state_dir, _ = env
    build1 = {"polling_stations": _stations([dict(id=1, name_cyr="A")]),
              "coverage_segments": _segments([dict(id=10, station_id=1, street_raw="X")])}
    _ship(build1, out, state_dir)

    build2 = {"polling_stations": _stations([dict(id=1, name_cyr="A-NEW")]),
              "coverage_segments": _segments([dict(id=10, station_id=1, street_raw="X")])}
    # emit WITHOUT promoting (simulates a failed import: .tsv.new left, .tsv unchanged).
    counts, _, _ = s6.dump_derived(build2, out, state_dir)
    assert counts["polling_stations"] == 1
    # re-run with the same build still sees the change, because the manifest wasn't advanced.
    counts2, _, _ = s6.dump_derived(build2, out, state_dir)
    assert counts2["polling_stations"] == 1
