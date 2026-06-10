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
import re

import polars as pl
from rapidfuzz import fuzz, process

import config
from common.coverage_parse import interval_parity
from common.normalize import genitive_variants, normalize_street, suffix_rank

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

    # Scope is keyed by the city-group representative so a city's single doc resolves
    # streets/settlements across all its city-municipalities.
    sett_to_muni = dict(zip(settlements["id"], settlements["municipality_id"]))
    street_meta: dict[str, dict] = {}
    by_muni_norm: dict[str, dict[str, list[str]]] = {}
    by_sett_norm: dict[str, dict[str, list[str]]] = {}
    for sid, set_id, norm in zip(streets["id"], streets["settlement_id"], streets["name_norm"]):
        muni = sett_to_muni.get(set_id)
        gmuni = config.group_rep(muni) if muni else muni
        street_meta[sid] = {"settlement_id": set_id, "municipality_id": muni, "name_norm": norm}
        by_muni_norm.setdefault(gmuni, {}).setdefault(norm, []).append(sid)
        sett = by_sett_norm.setdefault(set_id, {})
        sett.setdefault(norm, []).append(sid)
        # Settlement-scoped alternates: declension variants ("НИКОЛА ТЕСЛА" reachable as
        # "НИКОЛЕ ТЕСЛЕ", "ПИШТЕ ДАНКОА" as "ДАНКО ПИШТА") and order-insensitive keys
        # ("ЂЕРЂА ДОЖЕ" reachable as "Дожа Ђерђа"). A literal street name always wins.
        gvs = genitive_variants(norm)
        for altkey in (*gvs, _sortkey(norm), *(_sortkey(g) for g in gvs), _strip_ulica(norm)):
            if altkey and altkey not in sett:
                sett[altkey] = [sid]

    addr_by_street: dict[str, list[tuple[int, int | None, str]]] = {}
    for aid, st, num, suf in zip(
        addresses["id"], addresses["street_id"], addresses["house_num"], addresses["house_suffix"]
    ):
        addr_by_street.setdefault(st, []).append((aid, num, suf or ""))

    settlements_by_muni: dict[str, list[tuple[str, str]]] = {}
    for sid, muni, name in zip(settlements["id"], settlements["municipality_id"], settlements["name_cyr"]):
        settlements_by_muni.setdefault(config.group_rep(muni), []).append((sid, normalize_street(name)))

    # station scope = the group rep of the station's municipality.
    station_muni = {sid: config.group_rep(m) for sid, m in zip(stations["id"], stations["municipality_id"])}
    station_settlement: dict[int, str | None] = {}
    for sid, addr in zip(stations["id"], stations["address_cyr"]):
        station_settlement[sid] = resolve_settlement_from_address(addr, station_muni[sid], settlements_by_muni)
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
        # Unique word-containment: station addresses say "ЗЕМУН, ..." while the register
        # settlement is "БЕОГРАД (ЗЕМУН)" — WRatio length-penalizes that below threshold.
        tw = set(target.split())
        hits = [sid for sid, norm in cands if tw and tw <= set(norm.split())]
        if len(hits) == 1:
            return hits[0]
    return None


def _strip_ulica(norm: str) -> str | None:
    """Drop a standalone 'УЛИЦА' word ("Поручничка улица" <-> register "ПОРУЧНИЧКА").
    Applied symmetrically (also as a register-side alternate, for names like
    "ЗМАЈЕВА УЛИЦА" vs doc "Змајева")."""
    words = norm.split()
    if len(words) > 1 and "УЛИЦА" in words:
        return " ".join(w for w in words if w != "УЛИЦА")
    return None


def _part_streets(primary: str, scope: dict[str, list[str]]) -> list[str]:
    """Register streets that are numbered PARTS of a plain base name: doc "Војни Пут"
    claims register "ВОЈНИ ПУТ 1" + "ВОЈНИ ПУТ 2" (suffix tokens must be digits/ДЕО)."""
    out: list[str] = []
    prefix = primary + " "
    for name, ids in scope.items():
        if name.startswith(prefix):
            rest = name[len(prefix):].split()
            if rest and all(w.isdigit() or w == "ДЕО" for w in rest):
                out.extend(ids)
    return out


def _sortkey(norm: str) -> str | None:
    """Order-insensitive token key — Hungarian names appear in both orders ("Дожа Ђерђа" /
    "Ђерђа Доже"). Single-word names return None (key would equal the name)."""
    w = norm.split()
    return " ".join(sorted(w)) if len(w) > 1 else None


def _token_subset(primary: str, scope: dict[str, list[str]]) -> str | None:
    """Unique settlement street whose name contains ALL of the doc name's words (>=2),
    with the same final word (surname). Returns the street id or None."""
    pt = primary.split()
    if len(pt) < 2:
        return None
    pset = set(pt)
    best: tuple[str, int] | None = None
    tied = False
    for key, ids in scope.items():
        kt = key.split()
        if pset <= set(kt) and kt[-1] == pt[-1] and len(kt) > len(pt):
            extra = len(set(kt) - pset)
            if best is None or extra < best[1]:
                best, tied = (ids[0], extra), False
            elif extra == best[1] and ids[0] != best[0]:
                tied = True
    return None if (best is None or tied) else best[0]


_DIGITS_RE = re.compile(r"\d+")


def _fuzzy(norm: str, names_map: dict[str, list[str]]) -> tuple[str, float] | None:
    if not names_map:
        return None
    best = process.extractOne(norm, list(names_map.keys()), scorer=fuzz.WRatio)
    if best and best[1] >= FUZZY_MIN:
        # Digit guard: names differing in their NUMBERS are different streets even at a
        # high string score ("... 1 ДЕО" vs "... 10 ДЕО", "7 ВОЈВОЂАНСКЕ" vs "8 ...").
        if _DIGITS_RE.findall(norm) != _DIGITS_RE.findall(best[0]):
            return None
        return names_map[best[0]][0], float(best[1])
    return None


_PAREN_RE = re.compile(r"\(([^)]*)\)")
# Normalized alias lookup: (municipality_id, normalized doc name) -> normalized register name.
_ALIASES = {
    (muni, normalize_street(doc)): normalize_street(reg)
    for (muni, doc), reg in config.STREET_ALIASES.items()
}


def resolve_street(street_raw, muni, settlement_id, idx
                   ) -> tuple[str | None, str, float, list[str]]:
    """Resolve a street name to a register street id, scoped to the station's settlement
    first, then municipality. Returns (street_id, method, score, ambiguous_ids).

    Municipality fallback applies ONLY when the exact name exists in exactly one other
    settlement (plausible cross-settlement coverage, flagged 'muni_fallback'). If the
    name exists in SEVERAL other settlements, picking one would be a coin flip (e.g.
    "Николе Тесле" exists in 7 Sombor settlements) — method 'ambiguous' is returned with
    the candidate street ids and nothing is linked.

    Parentheticals are alternate names / provisional designations ("Елека Бенедека
    (493. нова)", "Корзо (Бориса Кидрича)"), NOT part of the street name. They are stripped
    for the primary match key (mashing them in lets a noisy fuzzy match e.g. „493 нова“ to
    „3. нова“) and tried only as an EXACT alternate — never fuzzed."""
    _, by_muni_norm, by_sett_norm, *_ = idx
    m = _PAREN_RE.search(street_raw)
    primary = normalize_street(_PAREN_RE.sub(" ", street_raw)) or normalize_street(street_raw)
    alt = normalize_street(m.group(1)) if m else ""
    # Hand-maintained alias (doc name -> register name), e.g. "Пинкијева" -> "Хероја Пинкија".
    # Alias matches are reported as method 'alias' (flagged for review): they are asserted
    # by us, not by the document, so the reviewer must see and confirm the substitution.
    aliased = _ALIASES.get((muni, primary))
    if aliased:
        primary = aliased
    exact_method = "alias" if aliased else "exact"

    sett_scope = by_sett_norm.get(settlement_id, {}) if settlement_id else {}
    muni_scope = by_muni_norm.get(muni, {})

    if primary in sett_scope:
        return sett_scope[primary][0], exact_method, 100.0, []
    if alt and alt in sett_scope:
        return sett_scope[alt][0], "exact", 100.0, []
    # Genitive/nominative declension variant of the doc name (deterministic morphology,
    # settlement scope only) — "Николе Тесле" finds nominative "НИКОЛА ТЕСЛА" and v.v.
    # Declension variants ("Николе Тесле" finds "НИКОЛА ТЕСЛА"; "Данко Пишта" composes
    # with sortkeys to find "ПИШТЕ ДАНКОА"), then order-insensitive lookups.
    gvs = genitive_variants(primary)
    for g in gvs:
        if g in sett_scope:
            return sett_scope[g][0], exact_method, 100.0, []
    for key in {_sortkey(primary), *(_sortkey(g) for g in gvs)}:
        if key and key in sett_scope:
            return sett_scope[key][0], exact_method, 100.0, []
    # "Part 1" convention: the register's first part of an "N ДЕО" street is the plain
    # base name ("Угриновачки пут 1 део" -> "УГРИНОВАЧКИ ПУТ"; parts start at 2).
    if primary.endswith(" 1 ДЕО"):
        base = primary[: -len(" 1 ДЕО")].strip()
        if base in sett_scope:
            return sett_scope[base][0], exact_method, 100.0, []
        ids = muni_scope.get(base)
        if ids and len(ids) == 1:
            return ids[0], ("muni_fallback" if settlement_id else exact_method), 100.0, []
    # Doc name without the 'улица' word ("Поручничка улица" -> register "ПОРУЧНИЧКА").
    su = _strip_ulica(primary)
    if su:
        if su in sett_scope:
            return sett_scope[su][0], exact_method, 100.0, []
        ids = muni_scope.get(su)
        if ids and len(ids) == 1:
            return ids[0], ("muni_fallback" if settlement_id else exact_method), 100.0, []
    # Plain base name where the register splits the street into numbered parts:
    # "Војни Пут" claims ВОЈНИ ПУТ 1 + ВОЈНИ ПУТ 2. All parts are claimed (the extra ids
    # ride in the 4th slot); flagged via 'base_parts' for review.
    parts = _part_streets(primary, sett_scope) or _part_streets(primary, muni_scope)
    if parts:
        return parts[0], "base_parts", 90.0, parts[1:]
    # Fuzzy ONLY on the primary key, ONLY within the station's own settlement (catches
    # typos in the right place). Never fuzzy municipality-wide (invents matches for
    # nonexistent streets) and never fuzzy the parenthetical alternate.
    hit = _fuzzy(primary, sett_scope)
    if hit:
        return hit[0], "fuzzy", hit[1], []
    # Token-subset within the settlement: every doc word appears in the register name and
    # the last word (surname) matches — "ВУКА КАРАЏИЋА" ⊂ "ВУКА СТЕФАНОВИЋА КАРАЏИЋА".
    # WRatio under-scores these because of the length difference. Flagged like fuzzy.
    sub = _token_subset(primary, sett_scope)
    if sub:
        return sub, "fuzzy", 88.0, []

    # Municipality exact fallback / ambiguity detection.
    for key in ([primary] + ([alt] if alt else [])):
        ids = muni_scope.get(key)
        if not ids:
            continue
        if len(ids) == 1:
            return ids[0], ("muni_fallback" if settlement_id else exact_method), 100.0, []
        return None, "ambiguous", 0.0, ids
    return None, "none", 0.0, []


def _iv_parity(iv: list) -> str:
    return iv[2] if len(iv) > 2 else interval_parity(iv[0], iv[1])


def _parity_ok(num: int, parity: str) -> bool:
    return parity == "all" or (parity == "odd" and num % 2 == 1) or (parity == "even" and num % 2 == 0)


def _bounds_ok(num: int, suf: str, c: dict) -> bool:
    """Suffix-bounded range edges: '12б-16' starts at 12б (12 and 12а excluded);
    '1-23ц' ends at 23ц (23 and 23д included, 23ш excluded). A bound without a
    suffix keeps the historical behavior: all suffixed variants at that number match."""
    if num == c["lo"] and c.get("losfx") and suffix_rank(suf) < suffix_rank(c["losfx"]):
        return False
    if num == c["hi"] and c.get("hisfx") and suffix_rank(suf) > suffix_rank(c["hisfx"]):
        return False
    return True


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

    Returns (assigned: address_id -> winning claim,
             conflicts: seg_id -> set of OPPOSING station_ids,
             parity_unconfirmed_seg_ids).
    """
    assigned: dict[int, dict] = {}
    conflicts: dict[int, set[int]] = {}

    for aid, num, suf in rows:
        if num is None:
            continue
        cands: list[tuple[int, dict]] = []
        for c in claims:
            k = c["kind"]
            if k == "whole":
                cands.append((SPEC_WHOLE, c))
            elif k == "interval":
                if c["lo"] <= num <= c["hi"] and _parity_ok(num, c["parity"]) and _bounds_ok(num, suf, c):
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
            for c in top:
                conflicts.setdefault(c["seg_id"], set()).update(stations - {c["station_id"]})

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

    return assigned, conflicts, parity_unconfirmed


def main() -> int:
    config.ensure_artifacts()
    idx = build_indexes()
    street_meta, _bmn, _bsn, addr_by_street, settlements_by_muni, station_muni, station_settlement = idx

    segs = pl.read_parquet(config.SEGMENTS_AMENDED_PARQUET).to_dicts()

    # Reviewer overrides exported from D1 (fetch_overrides.sh). Manual street assignments
    # and number edits take precedence over machine parsing, so polygons reflect review.
    overrides: dict[int, dict] = {}
    if config.OVERRIDES_JSON.exists():
        overrides = {int(o["segment_id"]): o for o in json.loads(config.OVERRIDES_JSON.read_text())}
        print(f"  reviewer overrides loaded: {len(overrides):,}")

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
        street_id, method, score, amb_ids = resolve_street(s["street_raw"], muni, settlement_id, idx)

        # Apply reviewer override: manual street wins (if it exists in the register) and
        # manual number edits replace the machine parse for claim building.
        ov = overrides.get(s["id"])
        if ov:
            if ov.get("manual_street_id") and ov["manual_street_id"] in street_meta:
                street_id, method, score, amb_ids = ov["manual_street_id"], "manual", 100.0, []
            if ov.get("manual_json"):
                try:
                    mp = json.loads(ov["manual_json"])
                    parsed = {"intervals": mp.get("intervals", []), "singles": mp.get("singles", []),
                              "whole": bool(mp.get("whole")), "unknown_tokens": []}
                except (ValueError, TypeError):
                    pass

        rec = {**s, "parsed": parsed, "street_id": street_id, "method": method, "score": score,
               "amb_ids": amb_ids, "has_paren": bool(_PAREN_RE.search(s["street_raw"]))}
        seg_recs.append(rec)

    # Old-name restatements: documents list a renamed street twice per station — once with
    # current numbers ("Београдски пут 127-166") and once under the old name with the OLD
    # street's numbering ("Београдски пут (Југословенска) 1-31, 2-30"). Mapping the old
    # numbers onto the current street creates phantom claims that conflict with other
    # stations' legitimate ones. If the SAME station also claims the SAME street via a
    # plain (non-parenthetical) segment, the parenthetical segment is a restatement —
    # drop its claims (the plain segment covers the houses).
    plain_pairs = {(r["station_id"], r["street_id"]) for r in seg_recs
                   if r["street_id"] and not r["has_paren"]}
    n_dups = 0
    for r in seg_recs:
        r["old_name_dup"] = bool(
            r["has_paren"] and r["street_id"]
            and (r["station_id"], r["street_id"]) in plain_pairs
        )
        n_dups += r["old_name_dup"]
    if n_dups:
        print(f"  old-name restatement segments (claims dropped): {n_dups:,}")

    for r in seg_recs:
        s, parsed, street_id = r, r["parsed"], r["street_id"]
        if not street_id or r["old_name_dup"]:
            continue
        # 'base_parts': a plain base name claims every numbered part street (extras ride
        # in amb_ids), each with the same parsed numbers.
        targets = [street_id] + (r["amb_ids"] if r["method"] == "base_parts" else [])
        for tid in targets:
            if parsed.get("whole"):
                claims_by_street.setdefault(tid, []).append(
                    {"seg_id": s["id"], "station_id": s["station_id"], "kind": "whole"})
            else:
                for iv in parsed.get("intervals", []):
                    claims_by_street.setdefault(tid, []).append({
                        "seg_id": s["id"], "station_id": s["station_id"], "kind": "interval",
                        "lo": iv[0], "hi": iv[1], "parity": _iv_parity(iv),
                        "losfx": iv[3] if len(iv) > 3 else "", "hisfx": iv[4] if len(iv) > 4 else ""})
                for num, sfx in parsed.get("singles", []):
                    claims_by_street.setdefault(tid, []).append({
                        "seg_id": s["id"], "station_id": s["station_id"], "kind": "single",
                        "num": num, "suffix": sfx})

    # Reviewer-added street claims (streets the document omitted entirely): synthetic
    # segments with deterministic ids (ADDED_SEG_BASE + addition id), claims like any
    # other, and materialized into coverage_segments so links keep FK integrity.
    added_out: list[dict] = []
    if config.ADDITIONS_JSON.exists():
        additions = json.loads(config.ADDITIONS_JSON.read_text())
        n_add = 0
        for a in additions:
            if a["street_id"] not in street_meta:
                continue
            try:
                mp = json.loads(a["manual_json"])
            except (ValueError, TypeError):
                continue
            seg_id = config.ADDED_SEG_BASE + int(a["id"])
            parsed = {"intervals": mp.get("intervals", []), "singles": mp.get("singles", []),
                      "whole": bool(mp.get("whole")), "unknown_tokens": []}
            tid = a["street_id"]
            if parsed["whole"]:
                claims_by_street.setdefault(tid, []).append(
                    {"seg_id": seg_id, "station_id": a["station_id"], "kind": "whole"})
            else:
                for iv in parsed["intervals"]:
                    claims_by_street.setdefault(tid, []).append({
                        "seg_id": seg_id, "station_id": a["station_id"], "kind": "interval",
                        "lo": iv[0], "hi": iv[1], "parity": _iv_parity(iv),
                        "losfx": iv[3] if len(iv) > 3 else "", "hisfx": iv[4] if len(iv) > 4 else ""})
                for num, sfx in parsed["singles"]:
                    claims_by_street.setdefault(tid, []).append({
                        "seg_id": seg_id, "station_id": a["station_id"], "kind": "single",
                        "num": num, "suffix": sfx})
            added_out.append({
                "id": seg_id, "station_id": a["station_id"], "settlement_raw": None,
                "street_raw": street_meta[tid]["name_norm"], "street_id": tid,
                "kind": "manual_added", "parsed_json": json.dumps(parsed, ensure_ascii=False),
                "manual_json": None, "manual_locked": 1, "confidence": 0.9, "needs_review": 0,
                "review_reason": None, "parse_dialect": "manual", "source": "added",
                "amendment_note": None,
            })
            n_add += 1
        if n_add:
            print(f"  reviewer-added street claims: {n_add:,}")

    # Pass 2: resolve each street; collect links + per-segment flags.
    links: list[dict] = []
    matched_seg_ids: set[int] = set()
    conflict_map: dict[int, set[int]] = {}  # seg_id -> opposing station_ids
    parity_unconfirmed: set[int] = set()
    seg_conf = {r["id"]: round(r["score"] / 100.0, 2) for r in seg_recs}
    # Printed station number per station id (for human-readable conflict reasons).
    _st = pl.read_parquet(config.STATIONS_PARQUET)
    station_number = dict(zip(_st["id"], _st["number"]))
    # Settlement names (for human-readable ambiguity reasons).
    _setts = pl.read_parquet(config.SETTLEMENTS_PARQUET)
    sett_names = dict(zip(_setts["id"], _setts["name_cyr"]))

    for street_id, claims in claims_by_street.items():
        rows = addr_by_street.get(street_id, [])
        assigned, conflicts, unconfirmed = resolve_street_claims(claims, rows)
        for seg_id, opponents in conflicts.items():
            conflict_map.setdefault(seg_id, set()).update(opponents)
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
        if r.get("old_name_dup"):
            continue  # old-name restatement of a plain segment — pure duplicate, not shown
        parsed, method = r["parsed"], r["method"]
        reasons: list[str] = []
        if method == "ambiguous":
            # Same-named street in several other settlements — not resolved automatically.
            conf = 0.2
            setts = sorted({
                sett_names.get(street_meta[sid]["settlement_id"], "")
                for sid in r["amb_ids"] if sid in street_meta
            })
            reasons.append("ambiguous:" + "|".join(n for n in setts if n))
        elif r["street_id"] is None:
            conf = 0.2
            reasons.append("street_unresolved")
        elif method == "fuzzy":
            conf = 0.5
            reasons.append("fuzzy")
        elif method == "alias":
            # Hand-maintained substitution — surfaced so the reviewer confirms it once.
            conf = 0.6
            reasons.append("alias")
        elif method == "manual":
            conf = 0.9  # reviewer-assigned street; no flag
        elif method == "base_parts":
            # Plain base name expanded to all numbered part streets — confirmable.
            conf = 0.7
            reasons.append("base_parts")
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
        if not parsed.get("whole") and r["id"] not in matched_seg_ids and not r.get("old_name_dup"):
            reasons.append("no_match")
        if r["id"] in conflict_map:
            # Parameterized code: conflict:<opposing station numbers joined by |>.
            nums = sorted({station_number.get(sid, sid) for sid in conflict_map[r["id"]]})
            reasons.append("conflict:" + "|".join(str(n) for n in nums))
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

    out_segs.extend(added_out)
    pl.DataFrame(out_segs, infer_schema_length=None).write_parquet(config.SEGMENTS_PARQUET)
    pl.DataFrame(links, infer_schema_length=None).write_parquet(config.LINKS_PARQUET)

    n_review = sum(x["needs_review"] for x in out_segs)
    n_unres = sum(1 for x in out_segs if x["street_id"] is None)
    print(f"  segments: {len(out_segs):,}  links: {len(links):,}  needs_review: {n_review:,}  "
          f"unresolved_street: {n_unres:,}  conflicts: {len(conflict_map):,}  "
          f"parity_unconfirmed: {len(parity_unconfirmed):,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
