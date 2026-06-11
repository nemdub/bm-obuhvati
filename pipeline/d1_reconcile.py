#!/usr/bin/env python3
"""Reconcile remote D1 to the freshly recomputed derived data, writing ONLY what differs.

`--only-dirty` recomputes whole municipalities (Voronoi needs settlement context), but for
the WRITE-BACK almost nothing actually changed: segments change only for the edited
stations, links for those plus any neighbour whose conflict flipped, polygons for stations
sharing a settlement with an edit. Re-importing all ~138k links of the affected munis is
both the expensive part on D1 (targeted deletes over the full 1.9M-row table) and a huge
write bill.

So instead: read the affected stations' CURRENT rows from D1, diff against the desired
parquet rows, and emit a minimal DELETE+INSERT for only the stations that truly differ.
Because the change set is small, the targeted deletes are small (well within what D1 chews
through) and the write count collapses from ~2M to a few thousand.

  reads:  segments/links/polygons/amendments parquet (desired) + remote D1 (current)
  writes: <out>.sql  (marker-chunked partial import; empty if nothing differs)

FK-safe emission (see d1_apply.sh for how chunks are applied):
  1. DELETE amendments for the munis of any seg-changed station   (before segment deletes)
  2. per seg-changed station batch: DELETE links+segments, INSERT segments+links
  3. per link-only-changed station batch: DELETE links, INSERT links
  4. per poly-changed station batch: DELETE polygons, INSERT polygons
  5. INSERT amendments back (after all segments are in place)

Usage:
  python3 d1_reconcile.py --municipalities 80438,80381 [--out artifacts/import_reconcile.sql]
  python3 d1_reconcile.py --municipalities 80438 --prod-sqlite some.sqlite   # offline test
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sqlite3
import sys
import time
from pathlib import Path

import polars as pl

import config
import stage06_build_sqlite as stage06

WORKER_DIR = config.ROOT_DIR / "worker"
DB_NAME = "bm-obuhvati"
READ_BATCH = int(os.environ.get("D1_READ_BATCH", "25"))  # stations per remote read query
READ_RETRIES = 4         # reads are non-destructive; retry transient D1/network blips
CHUNK_MARKER = stage06.CHUNK_MARKER
LINK_BUDGET = stage06.PARTIAL_LINK_BUDGET   # links per write-chunk

# Columns compared / written, per table (subset of stage06.TABLES, minus volatile cols).
SEG_COLS = stage06.TABLES["coverage_segments"][1]
LINK_COLS = stage06.TABLES["station_address_links"][1]
POLY_COLS = stage06.TABLES["polygons"][1]            # incl. computed_at (ignored in diff)
AMD_COLS = stage06.TABLES["amendments"][1]


# ── reading current prod state ────────────────────────────────────────────────
def _d1_read(sql: str) -> list[dict]:
    last = ""
    for attempt in range(1, READ_RETRIES + 1):
        res = subprocess.run(
            ["npx", "wrangler", "d1", "execute", DB_NAME, "--remote", "--json", "--command", sql],
            cwd=str(WORKER_DIR), capture_output=True, text=True,
        )
        if res.returncode == 0:
            data = json.loads(res.stdout)
            return data[0]["results"] if data and "results" in data[0] else []
        last = res.stderr or res.stdout
        if attempt < READ_RETRIES:
            time.sleep(5 * attempt)
    sys.exit(f"d1 read failed after {READ_RETRIES} attempts:\n{last}")


def _sqlite_read(db: Path, sql: str) -> list[dict]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(sql).fetchall()]
    con.close()
    return rows


def read_current(table: str, cols: list[str], station_ids: list[int],
                 prod_sqlite: Path | None) -> list[dict]:
    """Current rows of `table` for `station_ids` from prod (or a local sqlite in tests),
    read in batches to stay under D1's per-query result-size limit."""
    col_list = ", ".join(cols)
    out: list[dict] = []
    for i in range(0, len(station_ids), READ_BATCH):
        ids = ", ".join(str(s) for s in station_ids[i:i + READ_BATCH])
        sql = f"SELECT {col_list} FROM {table} WHERE station_id IN ({ids})"
        out.extend(_sqlite_read(prod_sqlite, sql) if prod_sqlite else _d1_read(sql))
    return out


def read_amendments(muni_ids: list[str], prod_sqlite: Path | None) -> list[dict]:
    if not muni_ids:
        return []
    ids = ", ".join(f"'{m}'" for m in muni_ids)
    sql = f"SELECT {', '.join(AMD_COLS)} FROM amendments WHERE municipality_id IN ({ids})"
    return _sqlite_read(prod_sqlite, sql) if prod_sqlite else _d1_read(sql)


# ── diffing ───────────────────────────────────────────────────────────────────
def _norm(v):
    # Make parquet values and JSON/sqlite values comparable. Key gotcha: id columns are
    # strings in the parquet but come back as ints from D1/sqlite (INTEGER affinity), so
    # coerce digit-strings to int on BOTH sides. Floats are rounded; bools -> int.
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, str) and v.lstrip("-").isdigit():
        return int(v)
    return v


def _sig_by_station(rows: list[dict], cols: list[str], ignore: set[str] | None = None):
    """station_id -> sorted tuple of per-row tuples (an order-independent signature)."""
    keep = [c for c in cols if not (ignore and c in ignore)]
    sig: dict[int, list] = {}
    for r in rows:
        sid = int(r["station_id"])
        sig.setdefault(sid, []).append(tuple(_norm(r[c]) for c in keep))
    return {sid: sorted(v) for sid, v in sig.items()}


def _changed(desired: list[dict], current: list[dict], cols: list[str],
             station_ids: list[int], ignore: set[str] | None = None) -> set[int]:
    d = _sig_by_station(desired, cols, ignore)
    c = _sig_by_station(current, cols, ignore)
    return {sid for sid in station_ids if d.get(sid, []) != c.get(sid, [])}


# ── emission ──────────────────────────────────────────────────────────────────
def _batches(station_ids: list[int], link_count: dict[int, int], budget: int):
    out, cur, n = [], [], 0
    for sid in station_ids:
        cur.append(sid)
        n += link_count.get(sid, 0)
        if n >= budget or len(cur) >= stage06.PARTIAL_MAX_STATIONS:
            out.append(cur)
            cur, n = [], 0
    if cur:
        out.append(cur)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Emit a minimal D1 import by diffing against prod.")
    ap.add_argument("--municipalities", required=True, help="Comma-separated group_rep ids.")
    ap.add_argument("--out", default=str(config.ARTIFACTS_DIR / "import_reconcile.sql"))
    ap.add_argument("--prod-sqlite", help="Read 'current' state from this sqlite instead of remote D1 (testing).")
    args = ap.parse_args()
    munis = {m.strip() for m in args.municipalities.split(",") if m.strip()}
    prod_sqlite = Path(args.prod_sqlite) if args.prod_sqlite else None
    out = Path(args.out)

    affected, muni_ids = stage06.affected_scope(munis)
    affected_set = set(affected)

    # Desired rows (from the freshly recomputed parquets), scoped to affected stations.
    seg_d = pl.read_parquet(config.SEGMENTS_PARQUET).filter(pl.col("station_id").is_in(affected)).to_dicts()
    lnk_d = pl.read_parquet(config.LINKS_PARQUET).filter(pl.col("station_id").is_in(affected)).to_dicts()
    pol_d = pl.read_parquet(config.POLYGONS_PARQUET).filter(pl.col("station_id").is_in(affected)).to_dicts()
    amd_all = pl.read_parquet(config.AMENDMENTS_PARQUET) if config.AMENDMENTS_PARQUET.exists() else None

    # Current rows from prod (or test sqlite).
    seg_c = read_current("coverage_segments", SEG_COLS, affected, prod_sqlite)
    lnk_c = read_current("station_address_links", LINK_COLS, affected, prod_sqlite)
    pol_c = read_current("polygons", POLY_COLS, affected, prod_sqlite)

    seg_changed = _changed(seg_d, seg_c, SEG_COLS, affected)
    link_changed = _changed(lnk_d, lnk_c, LINK_COLS, affected)
    poly_changed = _changed(pol_d, pol_c, POLY_COLS, affected, ignore={"computed_at"})

    core = sorted(seg_changed)                       # segments changed -> redo seg+links
    link_only = sorted(link_changed - seg_changed)   # only links changed -> redo links
    poly_only = sorted(poly_changed - set(core))     # polygon-only changes
    # Amendments must be cleared before any seg-changed segment is deleted (FK), and the
    # cheapest correct scope is the munis of the seg-changed stations.
    sid_to_muni = {int(s): str(m) for s, m in zip(
        pl.read_parquet(config.STATIONS_PARQUET)["id"],
        pl.read_parquet(config.STATIONS_PARQUET)["municipality_id"])}
    amd_munis = sorted({sid_to_muni[s] for s in core if s in sid_to_muni})
    amd_rows = (amd_all.filter(pl.col("municipality_id").is_in(amd_munis)).to_dicts()
                if amd_all is not None and amd_munis else [])

    link_count = {sid: 0 for sid in affected}
    for r in lnk_d:
        link_count[int(r["station_id"])] = link_count.get(int(r["station_id"]), 0) + 1

    # Helper to write INSERTs for a station subset of a desired row list.
    def insert_rows(f, rows: list[dict], cols: list[str], table: str, sids: set[int]):
        sub = [r for r in rows if int(r["station_id"]) in sids]
        if sub:
            stage06.write_inserts(f, pl.DataFrame(sub, infer_schema_length=None).select(cols), table, cols)

    n_writes = 0
    with out.open("w", encoding="utf-8") as f:
        if amd_munis:
            ids = ", ".join(f"'{m}'" for m in amd_munis)
            f.write(f"DELETE FROM amendments WHERE municipality_id IN ({ids});\n")
            f.write(CHUNK_MARKER)
        # seg-changed: redo segments + links + polygons (segments changed -> coverage and
        # hence the polygon may have moved; doing all three keeps it self-contained).
        for batch in _batches(core, link_count, LINK_BUDGET):
            bset = set(batch)
            ids = ", ".join(str(s) for s in batch)
            f.write(f"DELETE FROM station_address_links WHERE station_id IN ({ids});\n")
            f.write(f"DELETE FROM polygons WHERE station_id IN ({ids});\n")
            f.write(f"DELETE FROM coverage_segments WHERE station_id IN ({ids});\n")
            insert_rows(f, seg_d, SEG_COLS, "coverage_segments", bset)
            insert_rows(f, lnk_d, LINK_COLS, "station_address_links", bset)
            insert_rows(f, pol_d, POLY_COLS, "polygons", bset)
            n_writes += sum(link_count[s] for s in batch)
            f.write(CHUNK_MARKER)
        # link-only changed: delete + reinsert links (segments untouched in prod)
        for batch in _batches(link_only, link_count, LINK_BUDGET):
            bset = set(batch)
            ids = ", ".join(str(s) for s in batch)
            f.write(f"DELETE FROM station_address_links WHERE station_id IN ({ids});\n")
            insert_rows(f, lnk_d, LINK_COLS, "station_address_links", bset)
            n_writes += sum(link_count[s] for s in batch)
            f.write(CHUNK_MARKER)
        # polygon-only changed: tiny table, big batches are fine
        for i in range(0, len(poly_only), 400):
            batch = poly_only[i:i + 400]
            bset = set(batch)
            ids = ", ".join(str(s) for s in batch)
            f.write(f"DELETE FROM polygons WHERE station_id IN ({ids});\n")
            insert_rows(f, pol_d, POLY_COLS, "polygons", bset)
            n_writes += len(batch)
            f.write(CHUNK_MARKER)
        if amd_rows:
            stage06.write_inserts(f, pl.DataFrame(amd_rows, infer_schema_length=None).select(AMD_COLS),
                                  "amendments", AMD_COLS)
            f.write(CHUNK_MARKER)

    total_changed = len(core) + len(link_only) + len(poly_only)
    print(f"  reconcile: affected={len(affected)}  seg_changed={len(seg_changed)}  "
          f"link_changed={len(link_changed)}  poly_changed={len(poly_changed)}")
    print(f"  -> {total_changed} station(s) to write (~{n_writes} link/poly rows), "
          f"amendments munis={len(amd_munis)} -> {out}")
    # Signal to the caller whether there's anything to import (exit 0 always; emptiness via file).
    if total_changed == 0 and not amd_rows:
        out.write_text("")  # nothing to do
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
