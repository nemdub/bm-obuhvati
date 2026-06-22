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
from common.transliterate import cyr_to_lat
from stage02_extract_docs import rows_from_docx, textutil, collapse, _is_lat_restatement
from stage03_parse_coverage import build_muni_street_index, segments_for_station

_LAT_CH = re.compile(r"[A-Za-zČĆŽŠĐčćžšđ]")
_CYR_CH = re.compile(r"[А-Яа-яЂЈЉЊЋЏђјљњћџ]")

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


# ── `уместо / одређује се` whole-station replacements ────────────────────────
# The dominant amendment format (27 docs, e.g. Palilula/Čukarica/Aleksinac) reprints a
# station's full row twice: an OLD table (`уместо:` / `Стари назив` / `… N уместо:`) then a
# NEW table (`одређује се:` / `Треба да стоји:` / `Нови назив`), keyed by the printed number.
# We read the doc's Word table via HTML (clean <td> columns — robust to names that wrap onto
# several lines, which the linearized txt mis-splits). In document order the FIRST data row
# for a number is the OLD record, the SECOND is the NEW one. A replacement is emitted only
# when a field actually changed, so ordinary (non-replacement) amendment tables are ignored.


def _changed(a: str, b: str) -> bool:
    return collapse(a) != collapse(b)


def _cell_is_dual_script(cell: str) -> bool:
    """True for a `Назив Naziv` cell that restates its Cyrillic content in Latin — the HTML
    table keeps both scripts (Tutin / Sjenica / Prijepolje). The txt base parser de-dups these;
    here we just detect them so a replacement isn't built from doubled text."""
    toks = cell.split()
    for i in range(1, len(toks)):
        if _LAT_CH.search(toks[i]) and not _CYR_CH.search(toks[i]):
            cyr, lat = " ".join(toks[:i]), " ".join(toks[i:])
            if _CYR_CH.search(cyr) and _is_lat_restatement(cyr, lat):
                return True
    return False


def replacements_from_rows(rows: list) -> list[dict]:
    """Pure core of `parse_replacement_doc`: pair each station number's OLD (first) and NEW
    (last) table row and emit an op when a field changed. `rows` is rows_from_docx output:
    (section, number, name, address, coverage)."""
    # Dual-script docs (Tutin/Sjenica/Prijepolje) carry both scripts per HTML cell; skip them
    # so a replacement isn't built from doubled text (base coverage stays loaded — no regression).
    names = [name for *_, name, _, _ in rows if name.strip()]
    if names and sum(_cell_is_dual_script(n) for n in names) * 2 >= len(names):
        return []
    by_num: dict[int, list[tuple[str, str, str]]] = {}
    for _, num, name, address, coverage in rows:
        by_num.setdefault(num, []).append((name, address, coverage))
    ops: list[dict] = []
    for num, recs in by_num.items():
        if len(recs) < 2:
            continue  # no old/new pair → not a replacement (additions, reprints, bullets)
        (o_name, o_addr, o_cov), (n_name, n_addr, n_cov) = recs[0], recs[-1]
        if not (_changed(o_name, n_name) or _changed(o_addr, n_addr) or _changed(o_cov, n_cov)):
            continue  # identical reprint
        ops.append({
            "number": num,
            "old_name": o_name, "old_address": o_addr, "old_coverage": o_cov,
            "new_name": n_name, "new_address": n_addr, "new_coverage": n_cov,
        })
    return ops


def parse_replacement_doc(path) -> list[dict]:
    """Parse `уместо/одређује се` whole-station replacements from an amendment doc's Word
    table (read as HTML for clean columns). Returns ops {number, old_*, new_*}."""
    try:
        rows = rows_from_docx(textutil(path, "html"))
    except Exception:
        return []
    return replacements_from_rows(rows)


def main() -> int:
    config.ensure_artifacts()
    stations = pl.read_parquet(config.STATIONS_PARQUET)
    # (municipality, printed number) -> station id. Duplicates (cities that restart
    # numbering) keep the first; such amendments are flagged via needs_review anyway.
    station_by_num: dict[tuple[str, int], int] = {}
    for sid, muni_id, num in zip(stations["id"], stations["municipality_id"], stations["number"]):
        station_by_num.setdefault((muni_id, num), sid)
    # Per-station base metadata, for `replace_station` ops to compare against and patch.
    station_meta: dict[int, dict] = {
        r["id"]: r for r in stations.select(
            "id", "municipality_id", "name_cyr", "address_cyr", "raw_coverage_text"
        ).iter_rows(named=True)
    }
    # Register street index for re-parsing a replaced station's new coverage text.
    muni_and_streets = build_muni_street_index(
        pl.read_parquet(config.STREETS_PARQUET), pl.read_parquet(config.SETTLEMENTS_PARQUET)
    )

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
    # `replace_station` outputs: name/address patches and re-parsed coverage segments.
    name_over: dict[int, dict] = {}
    replaced_ids: set[int] = set()
    replacement_segs: list[dict] = []
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

        # `уместо/одређује се` whole-station replacements (name / address / coverage).
        for rep in parse_replacement_doc(config.DOCS_DIR / doc["source_file"]):
            station_id = station_by_num.get((muni, rep["number"]))
            if station_id is None:
                continue
            meta = station_meta[station_id]
            patch: dict = {"o_name_cyr": None, "o_name_lat": None,
                           "o_address_cyr": None, "o_address_lat": None}
            if rep["new_name"] and _changed(rep["new_name"], meta["name_cyr"]):
                patch["o_name_cyr"] = rep["new_name"]
                patch["o_name_lat"] = cyr_to_lat(rep["new_name"])
            if rep["new_address"] and _changed(rep["new_address"], meta["address_cyr"]):
                patch["o_address_cyr"] = rep["new_address"]
                patch["o_address_lat"] = cyr_to_lat(rep["new_address"])
            if any(v is not None for v in patch.values()):
                name_over[station_id] = patch

            target_id = None
            if rep["new_coverage"] and _changed(rep["new_coverage"], meta["raw_coverage_text"] or ""):
                # Re-parse the corrected coverage and replace this station's base segments.
                new_segs = segments_for_station(
                    {"id": station_id, "municipality_id": meta["municipality_id"],
                     "raw_coverage_text": rep["new_coverage"]},
                    muni_and_streets,
                )
                for s in new_segs:
                    s["source"] = "amendment"
                    s["amendment_note"] = "уместо/одређује се"
                    s["parsed"] = json.loads(s["parsed_json"])
                replaced_ids.add(station_id)
                replacement_segs.extend(new_segs)
                target_id = new_segs[0]["id"] if new_segs else None

            touched_stations.add(station_id)
            amend_audit.append({
                "id": amend_id, "municipality_id": muni, "station_number": rep["number"],
                "street_raw": None, "op": "replace_station",
                "old_value": f"{rep['old_name']} | {rep['old_address']}",
                "new_value": f"{rep['new_name']} | {rep['new_address']}",
                "raw_instruction": "уместо/одређује се", "source_file": doc["source_file"],
                "applied": 1, "target_segment_id": target_id,
            })
            amend_id += 1

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

    # Swap in re-parsed coverage for `replace_station` stations (drop their base segments).
    if replaced_ids:
        segs = [s for s in segs if s["station_id"] not in replaced_ids]
    segs.extend(replacement_segs)

    # Re-serialize parsed -> parsed_json, drop the working 'parsed' key.
    for s in segs:
        s["parsed_json"] = json.dumps(s["parsed"], ensure_ascii=False)
        del s["parsed"]

    cols = ["id", "station_id", "settlement_raw", "street_raw", "kind",
            "parsed_json", "parse_dialect", "source", "amendment_note"]
    pl.DataFrame(segs, infer_schema_length=None).select(cols).write_parquet(config.SEGMENTS_AMENDED_PARQUET)
    pl.DataFrame(amend_audit, infer_schema_length=None).write_parquet(config.AMENDMENTS_PARQUET)

    # Flag amended stations and apply `replace_station` name/address patches.
    stations = pl.read_parquet(config.STATIONS_PARQUET).with_columns(
        pl.when(pl.col("id").is_in(list(touched_stations))).then(1).otherwise(pl.col("is_amendment"))
        .alias("is_amendment")
    )
    if name_over:
        ov = pl.DataFrame([{"id": sid, **patch} for sid, patch in name_over.items()])
        stations = (
            stations.join(ov, on="id", how="left")
            .with_columns(
                pl.coalesce("o_name_cyr", "name_cyr").alias("name_cyr"),
                pl.coalesce("o_name_lat", "name_lat").alias("name_lat"),
                pl.coalesce("o_address_cyr", "address_cyr").alias("address_cyr"),
                pl.coalesce("o_address_lat", "address_lat").alias("address_lat"),
            )
            .drop("o_name_cyr", "o_name_lat", "o_address_cyr", "o_address_lat")
        )
    stations.write_parquet(config.STATIONS_PARQUET)

    # Refresh the pristine (edit-free) snapshots stage03c rebuilds from each recompute, so a
    # reverted text fix or a restored station recovers without another full re-parse.
    stations.write_parquet(config.STATIONS_PRISTINE_PARQUET)
    pl.read_parquet(config.SEGMENTS_AMENDED_PARQUET).write_parquet(config.SEGMENTS_AMENDED_PRISTINE_PARQUET)

    n_ok = sum(a["applied"] for a in amend_audit)
    n_repl = sum(1 for a in amend_audit if a["op"] == "replace_station")
    print(f"  amendment ops: {len(amend_audit)}  applied: {n_ok}  "
          f"replace_station: {n_repl}  "
          f"unparsed: {sum(1 for a in amend_audit if a['op']=='other')}  "
          f"stations touched: {len(touched_stations)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
