#!/usr/bin/env python3
"""Stage 03 — parse base coverage text into structured segments.

Reads stations.parquet, parses each station's coverage cell with the dialect-aware
parser, and writes one segment row per street clause to segments_raw.parquet.

Segment id is deterministic (station_id * 1000 + index) so manual edits keyed to it
survive re-runs. Street resolution, confidence and address matching happen in stage04.

Usage:
  python3 stage03_parse_coverage.py
  python3 stage03_parse_coverage.py --municipality 80438   # dev subset
"""

from __future__ import annotations

import argparse
import json

import polars as pl

import config
from common.coverage_parse import parse_coverage


def main() -> int:
    ap = argparse.ArgumentParser(description="Parse base coverage text into segments.")
    ap.add_argument("--municipality", help="Only this municipality_id (dev subset).")
    args = ap.parse_args()

    config.ensure_artifacts()
    stations = pl.read_parquet(config.STATIONS_PARQUET)
    if args.municipality:
        stations = stations.filter(pl.col("municipality_id") == args.municipality)

    # Register street names (per municipality group rep) that contain a literal "и", so the
    # compact parser keeps compound names like "Зрињског и Франкопана" whole instead of
    # splitting them on the connector into two phantom streets.
    streets = pl.read_parquet(config.STREETS_PARQUET)
    settlements = pl.read_parquet(config.SETTLEMENTS_PARQUET)
    sett_to_muni = dict(zip(settlements["id"], settlements["municipality_id"]))
    muni_and_streets: dict[str, set[str]] = {}
    for set_id, norm in zip(streets["settlement_id"], streets["name_norm"]):
        if "И" not in norm.split():
            continue
        muni = sett_to_muni.get(set_id)
        gmuni = config.group_rep(muni) if muni else muni
        muni_and_streets.setdefault(gmuni, set()).add(norm)

    rows: list[dict] = []
    for st in stations.iter_rows(named=True):
        muni_streets = muni_and_streets.get(config.group_rep(st["municipality_id"]), set())
        is_street = muni_streets.__contains__
        for idx, seg in enumerate(parse_coverage(st["raw_coverage_text"], is_street=is_street)):
            rows.append({
                "id": st["id"] * 1000 + idx,
                "station_id": st["id"],
                "settlement_raw": seg.settlement_raw or None,
                "street_raw": seg.street_raw,
                "kind": seg.kind,
                "parsed_json": json.dumps(seg.to_parsed(), ensure_ascii=False),
                "parse_dialect": seg.dialect,
                "source": "base",
            })

    df = pl.DataFrame(rows)
    df.write_parquet(config.SEGMENTS_RAW_PARQUET)

    by_kind = df.group_by("kind").len().sort("kind") if df.height else df
    print(f"  stations: {stations.height:,}  segments: {df.height:,}")
    if df.height:
        print(by_kind)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
