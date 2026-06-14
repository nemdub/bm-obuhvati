#!/usr/bin/env python3
"""Stage 03c — apply station-level reviewer edits before the incremental recompute.

Consumes the worker-owned edit exports (written by fetch_overrides.sh):

  text_overrides.json    [{"station_id": int, "raw_coverage_text": str}, ...]
  added_stations.json     [{"id": int, "municipality_id": str, "number": int|null,
                            "name_cyr": str, "address_cyr": str|null, "raw_coverage_text": str}, ...]
  removed_stations.json   [{"station_id": int}, ...]

and rebuilds the canonical stations.parquet + segments_amended.parquet (the inputs stage04
reads) from the PRISTINE snapshots, applying the current edits:

  * text override  -> replace a station's raw_coverage_text and re-parse its segments
  * added station  -> inject a station row (id = ADDED_STATION_BASE + id) + its parsed segments
  * removed station-> drop the station and its segments (stage06's delta then DELETEs it in D1)

Because it always rebuilds from pristine, reverting a text fix or restoring a removed station
recovers automatically on the next run — the edit simply isn't applied. Re-uses stage03's
parser (segments_for_station) so corrected/new text is parsed identically to base extraction.

No-op-safe: with no edits it just rewrites canonical == pristine. The pristine snapshots are
refreshed by stage03b at the end of a full rebuild, and bootstrapped here from the canonical
parquets if they don't exist yet.

Usage:
  python3 stage03c_reconcile_edits.py
"""

from __future__ import annotations

import json

import polars as pl

import config
from common.transliterate import cyr_to_lat
from stage03_parse_coverage import build_muni_street_index, segments_for_station


def _load_json(path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text()) or []
    except json.JSONDecodeError:
        return []


def _ensure_pristine() -> None:
    """Bootstrap pristine snapshots from the canonical parquets if they don't exist yet
    (first run after deploying this feature; thereafter stage03b keeps them fresh)."""
    if not config.STATIONS_PRISTINE_PARQUET.exists():
        pl.read_parquet(config.STATIONS_PARQUET).write_parquet(config.STATIONS_PRISTINE_PARQUET)
    if not config.SEGMENTS_AMENDED_PRISTINE_PARQUET.exists():
        pl.read_parquet(config.SEGMENTS_AMENDED_PARQUET).write_parquet(config.SEGMENTS_AMENDED_PRISTINE_PARQUET)


def _added_station_row(a: dict) -> dict:
    name = a.get("name_cyr") or ""
    addr = a.get("address_cyr") or ""
    return {
        "id": config.ADDED_STATION_BASE + int(a["id"]),
        "municipality_id": str(a["municipality_id"]),
        "number": int(a["number"]) if a.get("number") is not None else None,
        "name_cyr": name,
        "name_lat": cyr_to_lat(name),
        "address_cyr": addr,
        "address_lat": cyr_to_lat(addr),
        "raw_coverage_text": a.get("raw_coverage_text") or "",
        "source_file": "manual",
        "is_amendment": 0,
    }


def reconcile(
    pristine_stations: pl.DataFrame,
    pristine_segments: pl.DataFrame,
    text_overrides: list[dict],
    added_stations: list[dict],
    removed_stations: list[dict],
    muni_and_streets: dict[str, set[str]],
) -> tuple[pl.DataFrame, pl.DataFrame, dict]:
    """Pure reconcile: pristine + edits -> (stations, segments, stats). Tested directly."""
    by_id: dict[int, dict] = {r["id"]: r for r in pristine_stations.to_dicts()}

    # Added stations (upsert by synthetic id — idempotent across runs).
    for a in added_stations:
        row = _added_station_row(a)
        by_id[row["id"]] = row

    # Text overrides on existing stations.
    for o in text_overrides:
        sid = int(o["station_id"])
        if sid in by_id:
            by_id[sid]["raw_coverage_text"] = o.get("raw_coverage_text") or ""

    removed_ids = {int(r["station_id"]) for r in removed_stations}
    reparse_ids = {config.ADDED_STATION_BASE + int(a["id"]) for a in added_stations}
    reparse_ids |= {int(o["station_id"]) for o in text_overrides if int(o["station_id"]) in by_id}
    reparse_ids -= removed_ids

    # Drop removed stations from the station set.
    for sid in removed_ids:
        by_id.pop(sid, None)

    # Segments: keep pristine rows except those of re-parsed or removed stations; then re-emit
    # parsed segments for the re-parse set from the (edited) station rows.
    drop_seg_for = reparse_ids | removed_ids
    seg_rows = [s for s in pristine_segments.to_dicts() if s["station_id"] not in drop_seg_for]
    for sid in reparse_ids:
        st = by_id.get(sid)
        if st is None:
            continue
        for seg in segments_for_station(st, muni_and_streets):
            seg["amendment_note"] = None
            seg_rows.append(seg)

    out_stations = pl.DataFrame(list(by_id.values()), schema=pristine_stations.schema)
    out_segments = pl.DataFrame(seg_rows, schema=pristine_segments.schema)
    stats = {
        "added": len(added_stations),
        "text_overrides": sum(1 for o in text_overrides if int(o["station_id"]) in reparse_ids),
        "removed": len(removed_ids),
        "reparsed": len(reparse_ids),
    }
    return out_stations, out_segments, stats


def main() -> int:
    config.ensure_artifacts()

    text_overrides = _load_json(config.TEXT_OVERRIDES_JSON)
    added_stations = _load_json(config.ADDED_STATIONS_JSON)
    removed_stations = _load_json(config.REMOVED_STATIONS_JSON)

    _ensure_pristine()
    pristine_stations = pl.read_parquet(config.STATIONS_PRISTINE_PARQUET)
    pristine_segments = pl.read_parquet(config.SEGMENTS_AMENDED_PRISTINE_PARQUET)

    if not (text_overrides or added_stations or removed_stations):
        # No edits: canonical == pristine. Rewrite so a just-reverted edit is undone.
        pristine_stations.write_parquet(config.STATIONS_PARQUET)
        pristine_segments.write_parquet(config.SEGMENTS_AMENDED_PARQUET)
        print("  no station-level edits; canonical reset to pristine")
        return 0

    streets = pl.read_parquet(config.STREETS_PARQUET)
    settlements = pl.read_parquet(config.SETTLEMENTS_PARQUET)
    muni_and_streets = build_muni_street_index(streets, settlements)

    out_stations, out_segments, stats = reconcile(
        pristine_stations, pristine_segments,
        text_overrides, added_stations, removed_stations, muni_and_streets,
    )
    out_stations.write_parquet(config.STATIONS_PARQUET)
    out_segments.write_parquet(config.SEGMENTS_AMENDED_PARQUET)

    print(f"  station edits — added: {stats['added']}  text fixes: {stats['text_overrides']}  "
          f"removed: {stats['removed']}  (re-parsed {stats['reparsed']} station(s))")
    print(f"  stations: {out_stations.height:,}  segments: {out_segments.height:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
