#!/usr/bin/env python3
"""Stage 03b — parse amendment documents and apply their ops to base segments.

Amendments (izmena/dopuna/ispravka) are surgical prose operations keyed by station
number + street. We parse each bullet into a typed op, apply it to the matching base
segment, and record every op in an audit table. Touched/created segments are tagged
source='amendment' with the verbatim instruction; stage04 force-flags them for review.

  reads:  segments_raw.parquet, amendments_raw.parquet, stations.parquet
  writes: segments_amended.parquet, amendments.parquet, (updates stations.parquet is_amendment)

Usage:
  python3 stage03b_apply_amendments.py
"""

from __future__ import annotations

import json
import re

import polars as pl

import config
from common.coverage_parse import Segment, parse_number_token
from common.normalize import normalize_street

BULLET = re.compile(r"Гласачко место број\s+(\d+)")
Q = r"[„“\"']"  # Serbian/ASCII quotes
RE_FIX = re.compile(rf"назив улице\s*{Q}([^„“\"']+){Q}\s*се исправља.*?гласи\s*:?\s*{Q}([^„“\"']+){Q}")
RE_REPLACE = re.compile(
    rf"у улици\s+(.+?)\s+распон кућних бројева\s+(?:од\s+)?(.+?)\s+мења се.*?гласи\s*:?\s*{Q}([^„“\"']+){Q}"
)
RE_ADD = re.compile(
    r"у улици\s+(.+?)\s+(?:после кућног броја\s+(\S+)\s+)?додаје се\s+(?:кућни\s+)?број\s+(\S+)"
)


def _nums_from(value: str) -> Segment:
    s = Segment("", "", "street_numbers")
    for tok in re.split(r"[,\s]+и\s+|,", value):
        parse_number_token(tok, s)
    return s


def street_matches(a: str, b: str) -> bool:
    na, nb = normalize_street(a), normalize_street(b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def parse_bullet(text: str) -> dict | None:
    """Classify one amendment bullet into a typed op."""
    if (m := RE_FIX.search(text)):
        return {"op": "fix_street_name", "street": m.group(1).strip(),
                "old": m.group(1).strip(), "new": m.group(2).strip()}
    if (m := RE_REPLACE.search(text)):
        return {"op": "replace_range", "street": m.group(1).strip(),
                "old": m.group(2).strip().strip("„“\"'"), "new": m.group(3).strip()}
    if (m := RE_ADD.search(text)):
        return {"op": "add_house", "street": m.group(1).strip(),
                "old": (m.group(2) or "").strip(), "new": m.group(3).strip().strip(".„“\"'")}
    return None


def main() -> int:
    config.ensure_artifacts()
    stations = pl.read_parquet(config.STATIONS_PARQUET)
    # (municipality, printed number) -> station id. Duplicates (cities that restart
    # numbering) keep the first; such amendments are flagged via needs_review anyway.
    station_by_num: dict[tuple[str, int], int] = {}
    for sid, muni_id, num in zip(stations["id"], stations["municipality_id"], stations["number"]):
        station_by_num.setdefault((muni_id, num), sid)

    segs = pl.read_parquet(config.SEGMENTS_RAW_PARQUET).to_dicts()
    for s in segs:
        s["parsed"] = json.loads(s["parsed_json"])
        s["amendment_note"] = None

    by_station: dict[int, list[dict]] = {}
    for s in segs:
        by_station.setdefault(s["station_id"], []).append(s)
    next_seg_id = max((s["id"] for s in segs), default=0) + 1

    amend_audit: list[dict] = []
    touched_stations: set[int] = set()
    amend_id = 1

    if config.AMENDMENTS_RAW_PARQUET.exists():
        amend_docs = pl.read_parquet(config.AMENDMENTS_RAW_PARQUET)
    else:
        amend_docs = pl.DataFrame()

    for doc in amend_docs.iter_rows(named=True):
        muni = doc["municipality_id"]
        if muni is None:
            continue
        text = doc["raw_text"]
        anchors = list(BULLET.finditer(text))
        for k, anc in enumerate(anchors):
            station_number = int(anc.group(1))
            end = anchors[k + 1].start() if k + 1 < len(anchors) else len(text)
            bullet = text[anc.start():end].strip().strip("•").strip()
            instruction = (bullet.split(":", 1)[1] if ":" in bullet else bullet).strip().strip("•").strip()
            op = parse_bullet(instruction)
            station_id = station_by_num.get((muni, station_number))
            if station_id is None:
                continue  # amendment references a station we couldn't extract
            station_segs = by_station.get(station_id, [])

            applied = 0
            target_id = None
            if op:
                target = next((s for s in station_segs if street_matches(s["street_raw"], op["street"])), None)
                if op["op"] == "fix_street_name":
                    target = next((s for s in station_segs if street_matches(s["street_raw"], op["old"])), target)
                    if target:
                        target["street_raw"] = op["new"]
                        applied = 1
                elif op["op"] in ("replace_range", "add_house"):
                    new = _nums_from(op["new"])
                    if target is None:
                        target = {
                            "id": next_seg_id, "station_id": station_id, "settlement_raw": None,
                            "street_raw": op["street"], "kind": new.kind,
                            "parsed": new.to_parsed(), "parse_dialect": "amendment", "source": "amendment",
                            "parsed_json": "", "amendment_note": None,
                        }
                        next_seg_id += 1
                        segs.append(target)
                        station_segs.append(target)
                    else:
                        if op["op"] == "replace_range":
                            old = _nums_from(op["old"])
                            target["parsed"]["intervals"] = [
                                iv for iv in target["parsed"]["intervals"] if iv not in old.intervals
                            ]
                            target["parsed"]["singles"] = [
                                sg for sg in target["parsed"]["singles"] if sg not in old.singles
                            ]
                        target["parsed"]["intervals"].extend(new.intervals)
                        target["parsed"]["singles"].extend(new.singles)
                        target["parsed"]["whole"] = False
                    applied = 1
                if target:
                    target["source"] = "amendment"
                    target["amendment_note"] = instruction
                    target_id = target["id"]

            touched_stations.add(station_id)
            amend_audit.append({
                "id": amend_id, "municipality_id": muni, "station_number": station_number,
                "street_raw": op["street"] if op else None, "op": op["op"] if op else "other",
                "old_value": op.get("old") if op else None, "new_value": op.get("new") if op else None,
                "raw_instruction": instruction, "source_file": doc["source_file"],
                "applied": applied, "target_segment_id": target_id,
            })
            amend_id += 1

    # Re-serialize parsed -> parsed_json, drop the working 'parsed' key.
    for s in segs:
        s["parsed_json"] = json.dumps(s["parsed"], ensure_ascii=False)
        del s["parsed"]

    cols = ["id", "station_id", "settlement_raw", "street_raw", "kind",
            "parsed_json", "parse_dialect", "source", "amendment_note"]
    pl.DataFrame(segs, infer_schema_length=None).select(cols).write_parquet(config.SEGMENTS_AMENDED_PARQUET)
    pl.DataFrame(amend_audit, infer_schema_length=None).write_parquet(config.AMENDMENTS_PARQUET)

    # Flag amended stations.
    stations = pl.read_parquet(config.STATIONS_PARQUET).with_columns(
        pl.when(pl.col("id").is_in(list(touched_stations))).then(1).otherwise(pl.col("is_amendment"))
        .alias("is_amendment")
    )
    stations.write_parquet(config.STATIONS_PARQUET)

    # Refresh the pristine (edit-free) snapshots stage03c rebuilds from each recompute, so a
    # reverted text fix or a restored station recovers without another full re-parse.
    stations.write_parquet(config.STATIONS_PRISTINE_PARQUET)
    pl.read_parquet(config.SEGMENTS_AMENDED_PARQUET).write_parquet(config.SEGMENTS_AMENDED_PRISTINE_PARQUET)

    n_ok = sum(a["applied"] for a in amend_audit)
    print(f"  amendment ops: {len(amend_audit)}  applied: {n_ok}  "
          f"unparsed: {sum(1 for a in amend_audit if a['op']=='other')}  "
          f"stations touched: {len(touched_stations)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
