#!/usr/bin/env python3
"""Stage 06 — assemble the D1 dataset.

Reads whatever stage artifacts exist and produces:
  artifacts/bm.sqlite        canonical local SQLite (schema from the migration), for inspection
  artifacts/import_*.sql      data-only, batched multi-row INSERTs per table, for `wrangler d1 import`

The heavy ``addresses`` table is split into its own file; the small tables share one.
Schema lives in worker/migrations/0001_init.sql and is applied separately to D1 via
`wrangler d1 migrations apply`; these dumps carry data only.

Usage:
  python3 stage06_build_sqlite.py
  python3 stage06_build_sqlite.py --tables addresses,streets   # subset of tables
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import polars as pl

import config

MIGRATIONS_DIR = config.ROOT_DIR / "worker" / "migrations"
BATCH = 500            # max rows per multi-row INSERT
MAX_STMT_BYTES = 50_000  # flush a statement before it grows past this (D1 statement-size cap)

# table -> (parquet path, ordered columns)
TABLES: dict[str, tuple[Path, list[str]]] = {
    "municipalities": (config.MUNICIPALITIES_PARQUET, ["id", "name_cyr", "name_lat"]),
    "settlements": (config.SETTLEMENTS_PARQUET, ["id", "municipality_id", "name_cyr", "name_lat"]),
    "streets": (config.STREETS_PARQUET, ["id", "settlement_id", "name_cyr", "name_lat", "name_norm"]),
    "addresses": (config.ADDRESSES_PARQUET, [
        "id", "street_id", "settlement_id", "municipality_id",
        "house_num", "house_suffix", "house_raw", "lat", "lon", "x", "y",
    ]),
    "polling_stations": (config.STATIONS_PARQUET, [
        "id", "municipality_id", "number", "name_cyr", "name_lat",
        "address_cyr", "address_lat", "raw_coverage_text", "source_file", "is_amendment",
    ]),
    "coverage_segments": (config.SEGMENTS_PARQUET, [
        "id", "station_id", "settlement_raw", "street_raw", "street_id", "kind",
        "parsed_json", "manual_json", "manual_locked", "confidence", "needs_review",
        "parse_dialect", "source", "amendment_note", "review_reason",
    ]),
    "amendments": (config.AMENDMENTS_PARQUET, [
        "id", "municipality_id", "station_number", "street_raw", "op",
        "old_value", "new_value", "raw_instruction", "source_file", "applied", "target_segment_id",
    ]),
    "station_address_links": (config.LINKS_PARQUET, [
        "station_id", "address_id", "segment_id", "match_method", "confidence",
    ]),
    "polygons": (config.POLYGONS_PARQUET, [
        "station_id", "geojson", "area_m2", "point_count", "computed_at",
    ]),
}

# Three load groups (avoids FK violations without relying on PRAGMA toggles, which
# D1 ignores inside its implicit transaction):
#   REFERENCE — insert-only, loaded once after the migration; never deleted because
#               addresses reference them.
#   addresses — insert-only, the one-time heavy load.
#   DERIVED   — re-runnable each pipeline pass: DELETE child->parent, INSERT parent->child.
REFERENCE = ["municipalities", "settlements", "streets"]
DERIVED_INSERT_ORDER = [
    "polling_stations", "coverage_segments", "amendments", "station_address_links", "polygons",
]
DERIVED_DELETE_ORDER = [
    "station_address_links", "polygons", "amendments", "coverage_segments", "polling_stations",
]


def sql_literal(v: object) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v).replace("'", "''")
    return f"'{s}'"


def write_inserts(f, df: pl.DataFrame, table: str, cols: list[str]) -> int:
    """Batched multi-row INSERTs, flushed by row count OR byte budget (whichever first),
    so wide rows (polygons/segments) don't blow the statement-size cap."""
    rows = df.select(cols).rows()
    col_list = ", ".join(cols)
    header = f"INSERT INTO {table} ({col_list}) VALUES\n"
    buf: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buf, size
        if buf:
            f.write(header + ",\n".join(buf) + ";\n")
            buf = []
            size = 0

    for r in rows:
        tup = "(" + ", ".join(sql_literal(v) for v in r) + ")"
        if buf and (len(buf) >= BATCH or size + len(tup) > MAX_STMT_BYTES):
            flush()
        buf.append(tup)
        size += len(tup) + 2
    flush()
    return len(rows)


def dump_group(present: dict[str, pl.DataFrame], insert_order: list[str],
               delete_order: list[str], out: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    with out.open("w", encoding="utf-8") as f:
        for table in delete_order:
            if table in present:
                f.write(f"DELETE FROM {table};\n")
        for table in insert_order:
            if table in present:
                counts[table] = write_inserts(f, present[table], table, TABLES[table][1])
    return counts


def build_sqlite(present: dict[str, pl.DataFrame]) -> None:
    if config.SQLITE_OUT.exists():
        config.SQLITE_OUT.unlink()
    con = sqlite3.connect(config.SQLITE_OUT)
    # Apply all migrations in order so the canonical SQLite matches the D1 schema.
    for mig in sorted(MIGRATIONS_DIR.glob("*.sql")):
        con.executescript(mig.read_text(encoding="utf-8"))
    for table, df in present.items():
        cols = TABLES[table][1]
        placeholders = ", ".join("?" * len(cols))
        con.executemany(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
            df.select(cols).rows(),
        )
    con.commit()
    con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Assemble the D1 dataset (sqlite + import SQL).")
    ap.add_argument("--tables", help="Comma-separated subset of tables to build. Default: all present.")
    args = ap.parse_args()

    config.ensure_artifacts()
    wanted = set(args.tables.split(",")) if args.tables else set(TABLES)

    present: dict[str, pl.DataFrame] = {}
    for table, (path, _cols) in TABLES.items():
        if table in wanted and path.exists():
            present[table] = pl.read_parquet(path)

    if not present:
        raise SystemExit("No stage artifacts found to build from.")

    # Reference data (insert-only).
    ref_present = {t: present[t] for t in REFERENCE if t in present}
    if ref_present:
        path = config.ARTIFACTS_DIR / "import_reference.sql"
        counts = dump_group(ref_present, REFERENCE, [], path)
        for t, n in counts.items():
            print(f"  {t}: {n:,} rows")
        print(f"  -> {path}")

    # Addresses (insert-only, heavy).
    if "addresses" in present:
        path = config.ARTIFACTS_DIR / "import_addresses.sql"
        counts = dump_group({"addresses": present["addresses"]}, ["addresses"], [], path)
        print(f"  addresses: {counts['addresses']:,} rows -> {path}")

    # Derived data (re-runnable: delete child->parent, insert parent->child).
    derived_present = {t: present[t] for t in DERIVED_INSERT_ORDER if t in present}
    if derived_present:
        path = config.ARTIFACTS_DIR / "import_derived.sql"
        counts = dump_group(derived_present, DERIVED_INSERT_ORDER, DERIVED_DELETE_ORDER, path)
        for t, n in counts.items():
            print(f"  {t}: {n:,} rows")
        print(f"  -> {path}")

    build_sqlite(present)
    print(f"  built {config.SQLITE_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
