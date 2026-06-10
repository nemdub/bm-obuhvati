#!/usr/bin/env python3
"""Stage 04 — resolve streets and match coverage segments to register addresses.

For each segment: resolve street_raw to a register street id (exact normalized match,
else rapidfuzz within the station's municipality/settlement), then select the real
register house numbers it claims (ranges by numeric bound, singles by num+suffix). One
address is linked to at most one station. Finalizes confidence + needs_review.

  reads:  segments_amended.parquet, streets/settlements/addresses/stations parquet
  writes: segments.parquet (final, schema-ready), links.parquet

Usage:
  python3 stage04_match_addresses.py
"""

from __future__ import annotations

import json

import polars as pl
from rapidfuzz import fuzz, process

import config
from common.coverage_parse import interval_parity
from common.normalize import normalize_street

FUZZY_MIN = config.STREET_FUZZY_MIN


def resolve_settlement_from_address(address: str, muni: str, settlements_by_muni) -> str | None:
    """A polling station sits in one settlement, named first in its address
    ('КЕЛЕБИЈА, ПУТ ...'). Use it as the default scope for street resolution."""
    if not address:
        return None
    head = normalize_street(address.split(",")[0])
    if not head:
        return None
    return resolve_settlement(head, muni, settlements_by_muni)


def build_indexes():
    streets = pl.read_parquet(config.STREETS_PARQUET)
    settlements = pl.read_parquet(config.SETTLEMENTS_PARQUET)
    addresses = pl.read_parquet(config.ADDRESSES_PARQUET)
    stations = pl.read_parquet(config.STATIONS_PARQUET)

    sett_to_muni = dict(zip(settlements["id"], settlements["municipality_id"]))
    street_meta: dict[str, dict] = {}
    by_muni_norm: dict[str, dict[str, list[str]]] = {}
    by_sett_norm: dict[str, dict[str, list[str]]] = {}
    for sid, set_id, norm in zip(streets["id"], streets["settlement_id"], streets["name_norm"]):
        muni = sett_to_muni.get(set_id)
        street_meta[sid] = {"settlement_id": set_id, "municipality_id": muni, "name_norm": norm}
        by_muni_norm.setdefault(muni, {}).setdefault(norm, []).append(sid)
        by_sett_norm.setdefault(set_id, {}).setdefault(norm, []).append(sid)

    addr_by_street: dict[str, list[tuple[int, int | None, str]]] = {}
    for aid, st, num, suf in zip(
        addresses["id"], addresses["street_id"], addresses["house_num"], addresses["house_suffix"]
    ):
        addr_by_street.setdefault(st, []).append((aid, num, suf or ""))

    settlements_by_muni: dict[str, list[tuple[str, str]]] = {}
    for sid, muni, name in zip(settlements["id"], settlements["municipality_id"], settlements["name_cyr"]):
        settlements_by_muni.setdefault(muni, []).append((sid, normalize_street(name)))

    station_muni = dict(zip(stations["id"], stations["municipality_id"]))
    station_settlement: dict[int, str | None] = {}
    for sid, muni, addr in zip(stations["id"], stations["municipality_id"], stations["address_cyr"]):
        station_settlement[sid] = resolve_settlement_from_address(addr, muni, settlements_by_muni)
    return (street_meta, by_muni_norm, by_sett_norm, addr_by_street, settlements_by_muni,
            station_muni, station_settlement)


def resolve_settlement(settlement_raw, muni, settlements_by_muni) -> str | None:
    if not settlement_raw:
        return None
    cands = settlements_by_muni.get(muni, [])
    target = normalize_street(settlement_raw)
    for sid, norm in cands:
        if norm == target:
            return sid
    if cands:
        best = process.extractOne(target, [n for _, n in cands], scorer=fuzz.WRatio)
        if best and best[1] >= FUZZY_MIN:
            return cands[best[2]][0]
    return None


def _fuzzy(norm: str, names_map: dict[str, list[str]]) -> tuple[str, float] | None:
    if not names_map:
        return None
    best = process.extractOne(norm, list(names_map.keys()), scorer=fuzz.WRatio)
    if best and best[1] >= FUZZY_MIN:
        return names_map[best[0]][0], float(best[1])
    return None


def resolve_street(street_raw, muni, settlement_id, idx) -> tuple[str | None, str, float]:
    """Resolve a street name to a register street id, scoped to the station's settlement
    first. Falling back to municipality scope (a same-named street in another settlement,
    or genuine cross-settlement coverage) is reported as 'muni_fallback' so it is flagged
    for review. Returns (street_id, method, score)."""
    _, by_muni_norm, by_sett_norm, *_ = idx
    norm = normalize_street(street_raw)

    if settlement_id:
        sett_scope = by_sett_norm.get(settlement_id, {})
        if norm in sett_scope:
            return sett_scope[norm][0], "exact", 100.0
        # Fuzzy is allowed ONLY within the station's own settlement (catches typos in the
        # right place). It is NOT applied municipality-wide, where it invents matches for
        # streets that don't exist (e.g. matching a nonexistent street to an unrelated one
        # in a different settlement).
        hit = _fuzzy(norm, sett_scope)
        if hit:
            return hit[0], "fuzzy", hit[1]

    muni_scope = by_muni_norm.get(muni, {})
    if norm in muni_scope:
        # Exact name in another settlement: genuine cross-settlement coverage OR a
        # same-named street collision. Either way it is flagged for review.
        return muni_scope[norm][0], ("muni_fallback" if settlement_id else "exact"), 100.0
    return None, "none", 0.0


def _iv_parity(iv: list) -> str:
    return iv[2] if len(iv) > 2 else interval_parity(iv[0], iv[1])


def _parity_ok(num: int, parity: str) -> bool:
    return parity == "all" or (parity == "odd" and num % 2 == 1) or (parity == "even" and num % 2 == 0)


# Claim specificity (higher wins). An exact single (number + suffix) beats a bare number
# implying its suffixed variants, which beats a range, which beats a whole street. The
# implied level lets "Пушкинов трг 5" also claim 5а/5б/... unless another station lists
# that exact suffixed address.
SPEC_EXACT_SINGLE = 3
SPEC_IMPLIED_SINGLE = 2
SPEC_INTERVAL = 1
SPEC_WHOLE = 0


def resolve_street_claims(claims: list[dict], rows: list[tuple]) -> tuple[dict[int, dict], set[int], set[int]]:
    """Resolve one street's register houses against all stations' claims on it.

    - A parity-restricted range only claims houses of its own side (odd/even), so two
      stations splitting a street by side never collide.
    - Each house goes to one station; if two stations claim it at the same specificity it
      is a real conflict (flagged), not silently assigned.
    - Parity is then VALIDATED: a range assumed to be one side is 'confirmed' only if the
      complementary-side houses in its span are actually covered by another station;
      otherwise the assumption is unconfirmed and the segment is flagged.

    Returns (assigned: address_id -> winning claim, conflict_seg_ids, parity_unconfirmed_seg_ids).
    """
    assigned: dict[int, dict] = {}
    conflict_seg_ids: set[int] = set()

    for aid, num, suf in rows:
        if num is None:
            continue
        cands: list[tuple[int, dict]] = []
        for c in claims:
            k = c["kind"]
            if k == "whole":
                cands.append((SPEC_WHOLE, c))
            elif k == "interval":
                if c["lo"] <= num <= c["hi"] and _parity_ok(num, c["parity"]):
                    cands.append((SPEC_INTERVAL, c))
            elif c["num"] == num:
                if c["suffix"] == suf:
                    cands.append((SPEC_EXACT_SINGLE, c))          # exact (incl. bare matching bare)
                elif c["suffix"] == "" and suf != "":
                    cands.append((SPEC_IMPLIED_SINGLE, c))        # bare number implies 5а/5б/...
        if not cands:
            continue
        maxspec = max(s for s, _ in cands)
        top = [c for s, c in cands if s == maxspec]
        stations = {c["station_id"] for c in top}
        if len(stations) == 1:
            assigned[aid] = top[0]
        else:
            conflict_seg_ids.update(c["seg_id"] for c in top)

    # Validate parity assumptions against sibling coverage.
    parity_unconfirmed: set[int] = set()
    for c in claims:
        if c["kind"] != "interval" or c["parity"] == "all":
            continue
        comp_even = c["parity"] == "odd"  # complementary side is even
        comp = [
            aid for aid, num, _ in rows
            if num is not None and c["lo"] <= num <= c["hi"] and (num % 2 == 0) == comp_even
        ]
        if not comp:
            continue  # no opposite-parity houses exist -> the split is moot
        covered_by_other = any(
            aid in assigned and assigned[aid]["station_id"] != c["station_id"] for aid in comp
        )
        if not covered_by_other:
            parity_unconfirmed.add(c["seg_id"])

    return assigned, conflict_seg_ids, parity_unconfirmed


def main() -> int:
    config.ensure_artifacts()
    idx = build_indexes()
    street_meta, _bmn, _bsn, addr_by_street, settlements_by_muni, station_muni, station_settlement = idx

    segs = pl.read_parquet(config.SEGMENTS_AMENDED_PARQUET).to_dicts()

    # Pass 1: resolve a register street for every segment.
    seg_recs: list[dict] = []
    claims_by_street: dict[str, list[dict]] = {}
    for s in segs:
        muni = station_muni.get(s["station_id"])
        parsed = json.loads(s["parsed_json"])
        # Scope to the segment's own settlement if labelled, else the station's home
        # settlement (from its address); falls back to municipality inside resolve_street.
        settlement_id = (
            resolve_settlement(s["settlement_raw"], muni, settlements_by_muni)
            or station_settlement.get(s["station_id"])
        )
        street_id, method, score = resolve_street(s["street_raw"], muni, settlement_id, idx)
        rec = {**s, "parsed": parsed, "street_id": street_id, "method": method, "score": score}
        seg_recs.append(rec)
        if not street_id:
            continue
        if parsed.get("whole"):
            claims_by_street.setdefault(street_id, []).append(
                {"seg_id": s["id"], "station_id": s["station_id"], "kind": "whole"})
        else:
            for iv in parsed.get("intervals", []):
                claims_by_street.setdefault(street_id, []).append({
                    "seg_id": s["id"], "station_id": s["station_id"], "kind": "interval",
                    "lo": iv[0], "hi": iv[1], "parity": _iv_parity(iv)})
            for num, sfx in parsed.get("singles", []):
                claims_by_street.setdefault(street_id, []).append({
                    "seg_id": s["id"], "station_id": s["station_id"], "kind": "single",
                    "num": num, "suffix": sfx})

    # Pass 2: resolve each street; collect links + per-segment flags.
    links: list[dict] = []
    matched_seg_ids: set[int] = set()
    conflict_seg_ids: set[int] = set()
    parity_unconfirmed: set[int] = set()
    seg_conf = {r["id"]: round(r["score"] / 100.0, 2) for r in seg_recs}

    for street_id, claims in claims_by_street.items():
        rows = addr_by_street.get(street_id, [])
        assigned, conflicts, unconfirmed = resolve_street_claims(claims, rows)
        conflict_seg_ids |= conflicts
        parity_unconfirmed |= unconfirmed
        for aid, win in assigned.items():
            matched_seg_ids.add(win["seg_id"])
            links.append({
                "station_id": win["station_id"], "address_id": aid, "segment_id": win["seg_id"],
                "match_method": "whole_street" if win["kind"] == "whole" else win["kind"],
                "confidence": seg_conf.get(win["seg_id"], 0.0),
            })

    # Finalize per-segment confidence + needs_review (with reason codes explaining why).
    out_segs: list[dict] = []
    for r in seg_recs:
        parsed, method = r["parsed"], r["method"]
        reasons: list[str] = []
        if r["street_id"] is None:
            conf = 0.2
            reasons.append("street_unresolved")
        elif method == "fuzzy":
            conf = 0.5
            reasons.append("fuzzy")
        elif method == "muni_fallback":
            conf = 0.4
            reasons.append("muni_fallback")
        else:
            conf = 0.95 if r["parse_dialect"] == "structured" else 0.75
        if parsed.get("unknown_tokens"):
            reasons.append("unknown_tokens")
        if r["kind"] == "named_block":
            reasons.append("named_block")
        elif r["kind"] == "unknown":
            reasons.append("unknown_kind")
        if r["source"] == "amendment":
            reasons.append("amendment")
        if not parsed.get("whole") and r["id"] not in matched_seg_ids:
            reasons.append("no_match")
        if r["id"] in conflict_seg_ids:
            reasons.append("conflict")
        if r["id"] in parity_unconfirmed:
            reasons.append("parity_unconfirmed")

        out_segs.append({
            "id": r["id"], "station_id": r["station_id"], "settlement_raw": r["settlement_raw"],
            "street_raw": r["street_raw"], "street_id": r["street_id"], "kind": r["kind"],
            "parsed_json": r["parsed_json"], "manual_json": None, "manual_locked": 0,
            "confidence": conf, "needs_review": int(bool(reasons)),
            "review_reason": ",".join(reasons) or None,
            "parse_dialect": r["parse_dialect"], "source": r["source"],
            "amendment_note": r.get("amendment_note"),
        })

    pl.DataFrame(out_segs, infer_schema_length=None).write_parquet(config.SEGMENTS_PARQUET)
    pl.DataFrame(links, infer_schema_length=None).write_parquet(config.LINKS_PARQUET)

    n_review = sum(x["needs_review"] for x in out_segs)
    n_unres = sum(1 for x in out_segs if x["street_id"] is None)
    print(f"  segments: {len(out_segs):,}  links: {len(links):,}  needs_review: {n_review:,}  "
          f"unresolved_street: {n_unres:,}  conflicts: {len(conflict_seg_ids):,}  "
          f"parity_unconfirmed: {len(parity_unconfirmed):,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
