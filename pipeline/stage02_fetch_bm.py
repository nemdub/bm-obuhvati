#!/usr/bin/env python3
"""Stage 02 (BM source) — fetch polling stations + covered addresses from bm.itblokada.org.

Replaces ``stage02_extract_docs.py`` (the 2022 RIK Word documents) as the station source. One
call to the bulk export returns every polling station of the current register together with the
verbatim coverage description from the decision closing the voter roll:

  GET https://bm.itblokada.org/api/stations/export?format=json&include=covered_addresses
  header: X-Api-Key: $BM_API_KEY

  reads:  the API (or a saved export via --from-file), municipalities.parquet
  writes: data/bm/export-<date>.json (+ latest.json)   raw snapshot, kept for reproducibility
          artifacts/stations.parquet                     same schema as the RIK stage + BM columns
          artifacts/amendments_raw.parquet               EMPTY (the BM text is already current)
          artifacts/bm_skipped_stations.csv              stations left out, with the reason

Station id = ``int(municipality_id) * 100000 + number``. ``number`` is the printed station number,
unique within a locality (JLS), and every regular locality maps to exactly one municipality, so the
id is unique and — unlike the RIK stage's positional counter — does not shift when a station is
inserted or removed elsewhere in the municipality. The BM ULID (``bm_id``) and the electoral
register id (``register_id``) are carried along for cross-referencing.

Skipped: diaspora and prison stations (``tag``; they have no coverage text) and stations in
municipalities the address register does not contain (Kosovo and Metohija).

Usage:
  BM_API_KEY=... python3 stage02_fetch_bm.py            # fetch + build
  python3 stage02_fetch_bm.py --from-file data/bm/latest.json   # rebuild from a saved export
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

import polars as pl

import config

SKIP_TAGS = {"diaspora", "prison"}

STATION_SCHEMA = {
    "id": pl.Int64, "municipality_id": pl.String, "number": pl.Int64,
    "name_cyr": pl.String, "name_lat": pl.String,
    "address_cyr": pl.String, "address_lat": pl.String,
    "raw_coverage_text": pl.String, "source_file": pl.String, "is_amendment": pl.Int64,
    "section_cyr": pl.String,
    # BM-only columns (ignored by stages that select the legacy set).
    "bm_id": pl.String, "register_id": pl.String, "locality_id": pl.String,
    "voter_count": pl.Int64, "lat": pl.Float64, "lon": pl.Float64,
    "bm_settlement_id": pl.String, "bm_settlement_cyr": pl.String,
}


def station_id(municipality_id: str, number: int) -> int:
    """Deterministic station id: municipality + printed station number."""
    return int(municipality_id) * 100000 + int(number)


def fetch_export(api_key: str, dest: Path) -> None:
    req = urllib.request.Request(config.BM_EXPORT_URL, headers={"X-Api-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp, dest.open("wb") as out:
            shutil.copyfileobj(resp, out)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"BM export failed: HTTP {e.code} {e.reason} (check BM_API_KEY)") from e


def station_rows(results: list[dict], known_munis: set[str], source: str) -> tuple[list[dict], list[dict]]:
    """Map BM export records to station rows. Returns (rows, skipped)."""
    rows: list[dict] = []
    skipped: list[dict] = []

    def skip(r: dict, reason: str) -> None:
        skipped.append({
            "bm_id": r.get("id"), "locality_id": r.get("locality_id"),
            "municipality_id": (r.get("municipality") or {}).get("id"),
            "number": r.get("number"), "name_cyr": r.get("name_cyr"), "reason": reason,
        })

    for r in results:
        if r.get("tag") in SKIP_TAGS:
            skip(r, f"tag:{r['tag']}")
            continue
        muni = (r.get("municipality") or {}).get("id")
        if not muni:
            skip(r, "no_municipality")
            continue
        if muni not in known_munis:
            skip(r, "municipality_not_in_register")
            continue
        if r.get("number") is None:
            skip(r, "no_number")
            continue
        geo = r.get("geo") or {}
        sett = r.get("settlement") or {}
        # `venue_*` is the object name without the address tail and is never empty; `address_*`
        # is "<Settlement>, <street> <no>" (empty for ~2% — stage04 then falls back to the BM
        # settlement / the eponymous town for the station's home settlement).
        rows.append({
            "id": station_id(muni, r["number"]),
            "municipality_id": muni,
            "number": int(r["number"]),
            "name_cyr": (r.get("venue_cyr") or r.get("name_cyr") or "").strip(),
            "name_lat": (r.get("venue_lat") or r.get("name_lat") or "").strip(),
            # "" (not NULL) when unknown: polling_stations.address_* are NOT NULL in D1.
            "address_cyr": (r.get("address_cyr") or "").strip(),
            "address_lat": (r.get("address_lat") or "").strip(),
            "raw_coverage_text": (r.get("covered_addresses") or "").strip(),
            "source_file": source,
            "is_amendment": 0,
            "section_cyr": None,
            "bm_id": r.get("id"),
            "register_id": r.get("register_id"),
            "locality_id": r.get("locality_id"),
            "voter_count": r.get("voter_count"),
            "lat": geo.get("lat"),
            "lon": geo.get("lon"),
            "bm_settlement_id": sett.get("id"),
            "bm_settlement_cyr": sett.get("name_cyr"),
        })
    return rows, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch polling stations from bm.itblokada.org.")
    ap.add_argument("--from-file", type=Path, help="Use a saved export JSON instead of calling the API.")
    args = ap.parse_args()

    config.ensure_artifacts()
    if args.from_file:
        export_path = args.from_file
    else:
        api_key = os.environ.get("BM_API_KEY") or config.read_env_file().get("BM_API_KEY")
        if not api_key:
            print("BM_API_KEY is not set (env var, or BM_API_KEY=... in the repo-root .env)", file=sys.stderr)
            return 2
        config.BM_DIR.mkdir(parents=True, exist_ok=True)
        export_path = config.BM_DIR / f"export-{dt.date.today().isoformat()}.json"
        fetch_export(api_key, export_path)
        shutil.copyfile(export_path, config.BM_LATEST_JSON)
        print(f"  fetched -> {export_path.relative_to(config.ROOT_DIR)}")

    data = json.loads(export_path.read_text(encoding="utf-8"))
    results = data["results"]
    known = set(pl.read_parquet(config.MUNICIPALITIES_PARQUET)["id"].cast(str))
    rows, skipped = station_rows(results, known, f"bm:{export_path.name}")

    stations = pl.DataFrame(rows, schema=STATION_SCHEMA).sort("id")
    dup = stations.filter(pl.col("id").is_duplicated())
    if dup.height:
        print(dup.select("id", "municipality_id", "number", "name_cyr"))
        raise SystemExit(f"{dup.height} duplicate station ids (municipality + number is not unique)")
    stations.write_parquet(config.STATIONS_PARQUET)
    # No amendment documents in this source: the text is the current decision. An empty frame
    # keeps stage03b a pass-through (it still refreshes the *_pristine snapshots).
    pl.DataFrame(schema={"source_file": pl.String, "municipality_id": pl.String, "raw_text": pl.String}
                 ).write_parquet(config.AMENDMENTS_RAW_PARQUET)
    pl.DataFrame(skipped, schema={
        "bm_id": pl.String, "locality_id": pl.String, "municipality_id": pl.String,
        "number": pl.Int64, "name_cyr": pl.String, "reason": pl.String,
    }).write_csv(config.BM_SKIPPED_CSV)

    by_reason: dict[str, int] = {}
    for s in skipped:
        by_reason[s["reason"]] = by_reason.get(s["reason"], 0) + 1
    no_text = stations.filter(pl.col("raw_coverage_text") == "").height
    print(f"  export: {len(results):,} stations  kept: {stations.height:,} "
          f"in {stations['municipality_id'].n_unique()} municipalities  "
          f"without coverage text: {no_text}")
    for reason, n in sorted(by_reason.items()):
        print(f"  skipped {n:>4}  {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
