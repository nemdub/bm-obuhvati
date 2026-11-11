#!/usr/bin/env python3
"""Build the D1 import for a STATION-SOURCE SWITCH (see docs/parsing-matching/13-bm-source.md).

The normal imports cannot do this: `import_derived.sql` is a per-row delta against the last
shipped manifest, but a new source reuses station/segment id VALUES for different stations, and
the reviewer tables are keyed to the old ids. `import_reference.sql` / `import_addresses.sql`
are insert-only, so they cannot update or remove register rows either.

This writes an ordered set of files to artifacts/promote/, each applied with d1_apply.sh:

  01_clear.sql      reviewer tables, then every station-keyed table (per municipality, so no
                    single DELETE is large), street_geometry
  02_register.sql   register update: delete vanished addresses -> upsert municipalities,
                    settlements, streets IN FULL -> upsert changed/new addresses (delta vs the
                    previous run's parquets) -> delete vanished streets (FK-safe in that order)
  03_derived.sql    polling_stations, then coverage_segments (full insert)

followed by the regular full-replace files stage06 already wrote: import_station_settlements,
import_street_geometry, import_muni_boundaries, import_muni_meta.

Usage:
  python3 promote_source_switch.py --old ../backups/2026-11-11-pre-bm/artifacts
"""

from __future__ import annotations

import argparse
from pathlib import Path

import polars as pl

import config
import stage06_build_sqlite as s6

OUT_DIR = config.ARTIFACTS_DIR / "promote"
REVIEW_TABLES = [  # Worker-owned, keyed to the old ids (children of nothing we keep)
    "segment_overrides", "station_added_segments", "station_text_overrides",
    "added_stations", "removed_stations", "station_status",
]
ID_CHUNK = 500


def _changed(old: pl.DataFrame, new: pl.DataFrame, cols: list[str]) -> tuple[pl.DataFrame, list[str]]:
    """(rows to upsert = new or modified, ids to delete = vanished), comparing as strings."""
    o = old.select(cols).with_columns(pl.col("id").cast(pl.String))
    n = new.select(cols).with_columns(pl.col("id").cast(pl.String))
    gone = o.join(n.select("id"), on="id", how="anti")["id"].to_list()
    both = n.join(o, on="id", how="left", suffix="_o")
    diff = pl.lit(False)
    for c in cols:
        if c == "id":
            continue
        a, b = pl.col(c), pl.col(c + "_o")
        if n.schema[c] in (pl.Float64, pl.Float32):
            diff = diff | b.is_null() | ((a - b).abs() > 1e-6)
        else:
            diff = diff | (a.cast(pl.String).fill_null("\0") != b.cast(pl.String).fill_null("\0"))
    ids = both.filter(diff)["id"].to_list()
    return new.filter(pl.col("id").cast(pl.String).is_in(ids)), gone


def _delete_ids(f, table: str, ids: list[str], quote: bool) -> None:
    for i in range(0, len(ids), ID_CHUNK):
        part = ids[i:i + ID_CHUNK]
        vals = ", ".join(s6.sql_literal(v) if quote else str(v) for v in part)
        f.write(f"DELETE FROM {table} WHERE id IN ({vals});\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the D1 import for a station-source switch.")
    ap.add_argument("--old", type=Path, required=True,
                    help="Directory with the parquets production was last loaded from.")
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    def cur(name: str) -> pl.DataFrame:
        return pl.read_parquet(config.ARTIFACTS_DIR / f"{name}.parquet")

    def old(name: str) -> pl.DataFrame:
        return pl.read_parquet(args.old / f"{name}.parquet")

    # ── 01: clear ────────────────────────────────────────────────────────────
    munis = sorted(set(old("municipalities")["id"].cast(str)) | set(cur("municipalities")["id"].cast(str)))
    with (OUT_DIR / "01_clear.sql").open("w", encoding="utf-8") as f:
        for t in REVIEW_TABLES:
            f.write(f"DELETE FROM {t};\n")
        f.write("DELETE FROM station_settlements;\nDELETE FROM amendments;\nDELETE FROM street_geometry;\n")
        # Station ids are int(muni)*100000 + n under both sources, so a municipality's rows are
        # one contiguous id range — 168 small deletes instead of one over the whole table.
        for m in munis:
            lo, hi = int(m) * 100000, int(m) * 100000 + 99999
            f.write(f"DELETE FROM coverage_segments WHERE station_id BETWEEN {lo} AND {hi};\n")
        f.write("DELETE FROM coverage_segments;\n")   # leftovers (reviewer-added stations)
        for m in munis:
            lo, hi = int(m) * 100000, int(m) * 100000 + 99999
            f.write(f"DELETE FROM polling_stations WHERE id BETWEEN {lo} AND {hi};\n")
        f.write("DELETE FROM polling_stations;\n")

    # ── 02: register delta ───────────────────────────────────────────────────
    report: dict[str, tuple[int, int]] = {}
    with (OUT_DIR / "02_register.sql").open("w", encoding="utf-8") as f:
        deltas = {}
        for t in ("municipalities", "settlements", "streets", "addresses"):
            cols = s6.TABLES[t][1]
            deltas[t] = _changed(old(t), cur(t), cols)
            if t != "addresses":
                # The small reference tables are upserted IN FULL: production rows can differ
                # from the previous parquet (the reference import is insert-only, so e.g.
                # streets.name_norm in D1 predates later normalization fixes — 1,660 rows on
                # 2026-11-11). Only the 2.5M-row addresses table is shipped as a delta.
                deltas[t] = (cur(t), deltas[t][1])
            report[t] = (deltas[t][0].height, len(deltas[t][1]))
        _delete_ids(f, "addresses", deltas["addresses"][1], quote=True)
        for t in ("municipalities", "settlements", "streets", "addresses"):   # parents -> children
            cols = s6.TABLES[t][1]
            if deltas[t][0].height:
                s6.write_inserts(f, deltas[t][0], t, cols, on_conflict=s6._upsert_suffix(cols))
        for t in ("streets", "settlements", "municipalities"):               # children -> parents
            _delete_ids(f, t, deltas[t][1], quote=True)

    # ── 03: derived (full insert, parents -> children) ───────────────────────
    stations = cur("stations")
    stations = stations.with_columns(
        [pl.lit(None).alias(c) for c in s6.TABLES["polling_stations"][1] if c not in stations.columns])
    segments = s6.strip_discarded_osm_reason(cur("segments"))
    with (OUT_DIR / "03_derived.sql").open("w", encoding="utf-8") as f:
        n_st = s6.write_inserts(f, stations, "polling_stations", s6.TABLES["polling_stations"][1])
        n_sg = s6.write_inserts(f, segments, "coverage_segments", s6.TABLES["coverage_segments"][1])

    print(f"  -> {OUT_DIR}")
    for t, (up, gone) in report.items():
        print(f"  register {t:<15} upsert {up:>6,}  delete {gone:>6,}")
    print(f"  derived  polling_stations {n_st:,}  coverage_segments {n_sg:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
