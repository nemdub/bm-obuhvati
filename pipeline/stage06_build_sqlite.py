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
import json
import sqlite3
from pathlib import Path

import polars as pl

import config

MIGRATIONS_DIR = config.ROOT_DIR / "worker" / "migrations"
BATCH = 500            # max rows per multi-row INSERT
MAX_STMT_BYTES = 50_000  # flush a statement before it grows past this (D1 statement-size cap)

# table -> (parquet path, ordered columns)
TABLES: dict[str, tuple[Path, list[str]]] = {
    "municipalities": (config.MUNICIPALITIES_PARQUET, ["id", "name_cyr", "name_lat", "parent_id"]),
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
    "muni_boundaries": (config.MUNI_BOUNDARIES_PARQUET, [
        "municipality_id", "geojson",
    ]),
    "street_geometry": (config.STREET_GEOMETRY_PARQUET, [
        "street_id", "geojson",
    ]),
}

# Three load groups (avoids FK violations without relying on PRAGMA toggles, which
# D1 ignores inside its implicit transaction):
#   REFERENCE — insert-only, loaded once after the migration; never deleted because
#               addresses reference them.
#   addresses — insert-only, the one-time heavy load.
#   DERIVED   — re-runnable each pipeline pass: DELETE child->parent, INSERT parent->child.
REFERENCE = ["municipalities", "settlements", "streets"]
# Two derived tables are built LOCALLY but deliberately NOT shipped to D1:
#   `station_address_links` (~1.9M rows) — stage05 derives Voronoi polygons from it, but the
#       Worker never queries it (coverage points are computed live from `addresses`).
#   `polygons` (~7.4k rows / ~70MB of GeoJSON) — served from R2 instead (per-municipality
#       blobs, see write_r2_blobs); the byte-heavy geometry has no business in D1.
# What remains in the derived D1 dump is small text (segments + stations + amendments), so a
# plain full reload is fast and robust — no chunked-link import, no `--only-dirty` reconcile.
# Both tables are dropped from D1 by migration 0010; build_sqlite tolerates their absence.
DERIVED_INSERT_ORDER = [
    "polling_stations", "coverage_segments", "amendments",
]
DERIVED_DELETE_ORDER = [
    "amendments", "coverage_segments", "polling_stations",
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


def write_inserts(f, df: pl.DataFrame, table: str, cols: list[str], verb: str = "INSERT") -> int:
    """Batched multi-row INSERTs, flushed by row count OR byte budget (whichever first),
    so wide rows (polygons/segments) don't blow the statement-size cap. ``verb`` lets the
    reference group use ``INSERT OR IGNORE`` (idempotent, additive on the live DB)."""
    rows = df.select(cols).rows()
    col_list = ", ".join(cols)
    header = f"{verb} INTO {table} ({col_list}) VALUES\n"
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
               delete_order: list[str], out: Path, verb: str = "INSERT") -> dict[str, int]:
    counts: dict[str, int] = {}
    with out.open("w", encoding="utf-8") as f:
        for table in delete_order:
            if table in present:
                f.write(f"DELETE FROM {table};\n")
        for table in insert_order:
            if table in present:
                counts[table] = write_inserts(f, present[table], table, TABLES[table][1], verb)
    return counts


# Incremental (--municipalities) import: only the derived tables a coverage edit can
# change. INSERT order is parents->children (amendments after coverage_segments, since
# amendments.target_segment_id -> coverage_segments.id; links/polygons after their parents).
DERIVED_PARTIAL_INSERT_ORDER = [
    "coverage_segments", "amendments", "station_address_links", "polygons",
]


def _chunks(xs: list, n: int):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def affected_scope(municipalities: set[str]) -> tuple[list[int], list[str]]:
    """(affected station ids, affected municipality ids) for a group_rep set. A station/
    municipality is affected iff its group_rep is in the set, so both lists are mutually
    consistent: every amendment's target segment belongs to an affected station."""
    st = pl.read_parquet(config.STATIONS_PARQUET).select("id", "municipality_id")
    stations = [int(s) for s, m in zip(st["id"], st["municipality_id"])
                if config.group_rep(str(m)) in municipalities]
    mu = pl.read_parquet(config.MUNICIPALITIES_PARQUET).select("id")
    munis = [str(i) for i in mu["id"] if config.group_rep(str(i)) in municipalities]
    return stations, munis


# A single `wrangler d1 execute` is killed if it exceeds D1's per-execution CPU time
# limit, and DELETEing ~138k rows from the 1.9M-row links table is enough to trip it on its
# own — so the partial import is emitted as per-station BATCHES sized by link count, each
# delimited by a `-- CHUNK` marker that d1_apply.sh runs as its own execute. ~4k links of
# delete + ~4k of insert per chunk stays well under the limit.
PARTIAL_LINK_BUDGET = 2000   # links touched (delete+insert) per chunk
PARTIAL_MAX_STATIONS = 250   # also cap stations/chunk so a sparse batch can't grow an
                             # oversized IN(...) list / huge cumulative delete
CHUNK_MARKER = "-- CHUNK\n"


def _station_batches(station_ids: list[int], link_count: dict[int, int]) -> list[list[int]]:
    """Greedily group stations so each batch's total links <= PARTIAL_LINK_BUDGET (and
    <= PARTIAL_MAX_STATIONS stations), bounding the work in each chunk's d1 execute."""
    batches: list[list[int]] = []
    cur: list[int] = []
    cur_links = 0
    for sid in station_ids:
        cur.append(sid)
        cur_links += link_count.get(sid, 0)
        if cur_links >= PARTIAL_LINK_BUDGET or len(cur) >= PARTIAL_MAX_STATIONS:
            batches.append(cur)
            cur, cur_links = [], 0
    if cur:
        batches.append(cur)
    return batches


def dump_derived_partial(present: dict[str, pl.DataFrame], station_ids: list[int],
                         muni_ids: list[str], out: Path) -> dict[str, int]:
    """Station-scoped DELETE+INSERT for the affected stations only, instead of the full
    delete+reload (which re-ships ~1.9M link rows / 200MB+ every run), emitted as CPU-safe
    `-- CHUNK`-delimited batches.

    FK-safety: links/segments are keyed by station_id and a link only references its own
    station's segment, so each per-station batch is self-contained (delete children, then
    delete + reinsert its segments, then reinsert children). amendments
    (target_segment_id -> coverage_segments.id) are deleted up front — before any segment
    delete — and reinserted in a final chunk, after every batch's segments are back."""
    seg, lnk = present.get("coverage_segments"), present.get("station_address_links")
    pol, amd = present.get("polygons"), present.get("amendments")
    sids = station_ids
    seg_aff = seg.filter(pl.col("station_id").is_in(sids)) if seg is not None else None
    lnk_aff = lnk.filter(pl.col("station_id").is_in(sids)) if lnk is not None else None
    pol_aff = pol.filter(pl.col("station_id").is_in(sids)) if pol is not None else None
    amd_aff = (amd.filter(pl.col("municipality_id").is_in(muni_ids))
               if amd is not None and muni_ids else None)

    link_count: dict[int, int] = {}
    if lnk_aff is not None:
        for s, c in lnk_aff.group_by("station_id").len().iter_rows():
            link_count[int(s)] = c
    batches = _station_batches(sids, link_count)

    counts = {"coverage_segments": 0, "amendments": 0, "station_address_links": 0, "polygons": 0}
    with out.open("w", encoding="utf-8") as f:
        # amendments deleted first (FK: before any coverage_segments delete), own chunk.
        if amd_aff is not None and amd_aff.height:
            for chunk in _chunks(muni_ids, 400):
                ids = ", ".join(f"'{m}'" for m in chunk)
                f.write(f"DELETE FROM amendments WHERE municipality_id IN ({ids});\n")
            f.write(CHUNK_MARKER)
        # one chunk per station batch: delete children -> delete+reinsert its rows.
        for batch in batches:
            ids = ", ".join(str(s) for s in batch)
            if lnk is not None:
                f.write(f"DELETE FROM station_address_links WHERE station_id IN ({ids});\n")
            if pol is not None:
                f.write(f"DELETE FROM polygons WHERE station_id IN ({ids});\n")
            if seg is not None:
                f.write(f"DELETE FROM coverage_segments WHERE station_id IN ({ids});\n")
            for tbl, dfa in (("coverage_segments", seg_aff),
                             ("station_address_links", lnk_aff), ("polygons", pol_aff)):
                if dfa is not None:
                    counts[tbl] += write_inserts(
                        f, dfa.filter(pl.col("station_id").is_in(batch)), tbl, TABLES[tbl][1])
            f.write(CHUNK_MARKER)
        # amendments reinserted last — every affected segment is back in place by now.
        if amd_aff is not None and amd_aff.height:
            counts["amendments"] = write_inserts(f, amd_aff, "amendments", TABLES["amendments"][1])
            f.write(CHUNK_MARKER)
    return counts


def write_muni_meta(df: pl.DataFrame, out: Path) -> int:
    """UPDATE statements syncing municipalities' display fields (name + parent_id) onto the
    existing D1 rows. Municipalities can't be DELETE+reinserted (addresses FK), so renames
    and grouping changes are applied via UPDATE."""
    n = 0
    with out.open("w", encoding="utf-8") as f:
        for r in df.select("id", "name_cyr", "name_lat", "parent_id").iter_rows(named=True):
            f.write(
                f"UPDATE municipalities SET name_cyr={sql_literal(r['name_cyr'])}, "
                f"name_lat={sql_literal(r['name_lat'])}, parent_id={sql_literal(r['parent_id'])} "
                f"WHERE id={sql_literal(r['id'])};\n"
            )
            n += 1
    return n


# Columns each per-municipality polygon blob carries — mirrors the old D1
# `polygons ⋈ polling_stations` row exactly, so the Worker serves them unchanged.
R2_BLOB_COLS = [
    "station_id", "number", "name_cyr", "name_lat", "address_cyr", "address_lat",
    "geojson", "area_m2", "point_count", "computed_at",
]


def write_r2_blobs(polys: pl.DataFrame, stations: pl.DataFrame, out_dir: Path) -> tuple[int, int]:
    """Emit polygons as per-municipality GeoJSON blobs for R2 instead of D1 rows:
    ``polygons/m/<muniId>.json`` = {"stations": [row, ...]} (sorted by number), plus
    ``polygons/summary.json`` = {polygon_count, matched_addresses} for the homepage.

    Keyed by the station's raw ``municipality_id`` — the same grouping the old
    ``allMuniPolygons`` SQL used — so muni pages and the station map resolve identically.
    `geojson` stays a STRING (the Worker JSON.parses it per feature), matching the prior
    D1 contract. Returns (muni_count, polygon_count)."""
    meta = stations.select(
        ["id", "municipality_id", "number", "name_cyr", "name_lat", "address_cyr", "address_lat"]
    )
    df = polys.join(meta, left_on="station_id", right_on="id", how="inner")
    m_dir = out_dir / "polygons" / "m"
    m_dir.mkdir(parents=True, exist_ok=True)

    muni_count = 0
    poly_count = 0
    matched = 0
    for muni_id in df["municipality_id"].unique().to_list():
        sub = df.filter(pl.col("municipality_id") == muni_id).sort("number")
        rows = sub.select(R2_BLOB_COLS).to_dicts()
        (m_dir / f"{muni_id}.json").write_text(
            json.dumps({"stations": rows}, ensure_ascii=False), encoding="utf-8"
        )
        muni_count += 1
        poly_count += len(rows)
        matched += int(sub["point_count"].fill_null(0).sum())

    summary = {"polygon_count": poly_count, "matched_addresses": matched}
    (out_dir / "polygons" / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return muni_count, poly_count


def build_sqlite(present: dict[str, pl.DataFrame]) -> None:
    if config.SQLITE_OUT.exists():
        config.SQLITE_OUT.unlink()
    con = sqlite3.connect(config.SQLITE_OUT)
    # Apply all migrations in order so the canonical SQLite matches the D1 schema.
    for mig in sorted(MIGRATIONS_DIR.glob("*.sql")):
        con.executescript(mig.read_text(encoding="utf-8"))
    # A migration may DROP a table whose parquet we still build locally (polygons -> R2,
    # station_address_links -> stage05-only); skip inserts for tables not in the schema.
    schema_tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, df in present.items():
        if table not in schema_tables:
            continue
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
    ap.add_argument(
        "--municipalities",
        help="Comma-separated group_rep municipality ids. Emit a scoped "
             "import_derived_partial.sql (station-keyed DELETE+INSERT for just these munis) "
             "instead of the full 200MB+ derived reload. Pairs with stage04/05 --municipalities.",
    )
    args = ap.parse_args()
    municipalities = (
        {m.strip() for m in args.municipalities.split(",") if m.strip()}
        if args.municipalities else None
    )

    config.ensure_artifacts()
    wanted = set(args.tables.split(",")) if args.tables else set(TABLES)
    # Partial mode only needs the derived tables a coverage edit can change — skip reading
    # the 78MB addresses parquet etc.
    if municipalities is not None:
        wanted = {"coverage_segments", "amendments", "station_address_links", "polygons"}

    present: dict[str, pl.DataFrame] = {}
    for table, (path, _cols) in TABLES.items():
        if table in wanted and path.exists():
            present[table] = pl.read_parquet(path)

    if not present:
        raise SystemExit("No stage artifacts found to build from.")

    # Incremental import: scoped DELETE+INSERT for the affected stations only.
    if municipalities is not None:
        station_ids, muni_ids = affected_scope(municipalities)
        out = config.ARTIFACTS_DIR / "import_derived_partial.sql"
        counts = dump_derived_partial(present, station_ids, muni_ids, out)
        for t, n in counts.items():
            print(f"  {t}: {n:,} rows (partial)")
        print(f"  -> {out}  ({len(station_ids)} stations, {len(muni_ids)} munis)")
        return 0

    # Reference data (insert-only). INSERT OR IGNORE so re-running against an
    # already-loaded D1 only adds new rows (e.g. newly-registered streets) without
    # PK conflicts; correct for a fresh load too (empty tables).
    ref_present = {t: present[t] for t in REFERENCE if t in present}
    if ref_present:
        path = config.ARTIFACTS_DIR / "import_reference.sql"
        counts = dump_group(ref_present, REFERENCE, [], path, verb="INSERT OR IGNORE")
        for t, n in counts.items():
            print(f"  {t}: {n:,} rows")
        print(f"  -> {path}")

    # Municipality display-meta sync (names + parent_id) for already-loaded D1 rows.
    if "municipalities" in present:
        meta_path = config.ARTIFACTS_DIR / "import_muni_meta.sql"
        n = write_muni_meta(present["municipalities"], meta_path)
        print(f"  muni meta updates: {n} -> {meta_path}")

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

    # Municipality boundaries (re-runnable, rarely changes -> own file).
    if "muni_boundaries" in present:
        path = config.ARTIFACTS_DIR / "import_muni_boundaries.sql"
        counts = dump_group(
            {"muni_boundaries": present["muni_boundaries"]},
            ["muni_boundaries"], ["muni_boundaries"], path,
        )
        print(f"  muni_boundaries: {counts['muni_boundaries']:,} rows -> {path}")

    # Street line geometry for no-house streets (re-runnable, FK-referenced by nothing -> own file).
    if "street_geometry" in present:
        path = config.ARTIFACTS_DIR / "import_street_geometry.sql"
        counts = dump_group(
            {"street_geometry": present["street_geometry"]},
            ["street_geometry"], ["street_geometry"], path,
        )
        print(f"  street_geometry: {counts['street_geometry']:,} rows -> {path}")

    # Polygons -> per-municipality R2 blobs (served from object storage, not D1).
    if "polygons" in present and "polling_stations" in present:
        r2_dir = config.ARTIFACTS_DIR / "r2"
        mc, pc = write_r2_blobs(present["polygons"], present["polling_stations"], r2_dir)
        print(f"  r2 polygon blobs: {mc} munis, {pc:,} polygons -> {r2_dir}/polygons/")

    build_sqlite(present)
    print(f"  built {config.SQLITE_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
