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


# Register street names (per municipality group rep) the compact parser consults to
# disambiguate ambiguous comma/space splits. Two cases need register membership:
#   - names containing a literal "и" — keep compound names like "Зрињског и Франкопана"
#     whole instead of splitting on the connector into two phantom streets;
#   - names carrying a number ("НОВА 4", "НОВА 21") — keep a trailing number that is
#     really part of the street name instead of stripping it as a house number (which
#     collapsed "Нова 4, Нова 6, Нова 21, ..." into one "Нова" street + houses 4/6/21).
def build_muni_street_index(streets: pl.DataFrame, settlements: pl.DataFrame) -> dict[str, set[str]]:
    sett_to_muni = dict(zip(settlements["id"], settlements["municipality_id"]))
    muni_and_streets: dict[str, set[str]] = {}
    for set_id, norm in zip(streets["settlement_id"], streets["name_norm"]):
        toks = norm.split()
        if "И" not in toks and not any(t.isdigit() for t in toks):
            continue
        muni = sett_to_muni.get(set_id)
        gmuni = config.group_rep(muni) if muni else muni
        muni_and_streets.setdefault(gmuni, set()).add(norm)
    return muni_and_streets


def segments_for_station(st: dict, muni_and_streets: dict[str, set[str]]) -> list[dict]:
    """Parse one station's raw coverage text into segment row dicts. Segment id is
    deterministic (station_id * 1000 + index) so manual edits keyed to it survive re-runs.
    Shared by stage03 (base parse) and stage03c (re-parse of corrected / added-station text)."""
    muni_streets = muni_and_streets.get(config.group_rep(st["municipality_id"]), set())
    is_street = muni_streets.__contains__
    rows: list[dict] = []
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
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Parse base coverage text into segments.")
    ap.add_argument("--municipality", help="Only this municipality_id (dev subset).")
    args = ap.parse_args()

    config.ensure_artifacts()
    stations = pl.read_parquet(config.STATIONS_PARQUET)
    if args.municipality:
        stations = stations.filter(pl.col("municipality_id") == args.municipality)

    streets = pl.read_parquet(config.STREETS_PARQUET)
    settlements = pl.read_parquet(config.SETTLEMENTS_PARQUET)
    muni_and_streets = build_muni_street_index(streets, settlements)

    rows: list[dict] = []
    for st in stations.iter_rows(named=True):
        rows.extend(segments_for_station(st, muni_and_streets))

    df = pl.DataFrame(rows)
    df.write_parquet(config.SEGMENTS_RAW_PARQUET)

    by_kind = df.group_by("kind").len().sort("kind") if df.height else df
    print(f"  stations: {stations.height:,}  segments: {df.height:,}")
    if df.height:
        print(by_kind)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
