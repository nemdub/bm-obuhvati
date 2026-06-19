#!/usr/bin/env python3
"""Stage 04 — resolve streets and match coverage segments to register addresses.

For each segment: resolve street_raw to a register street id (exact normalized match,
else rapidfuzz within the station's municipality/settlement), then select the real
register house numbers it claims (ranges by numeric bound, singles by num+suffix). One
address is linked to at most one station. Finalizes confidence + needs_review.

A geographic proximity pass then fills segments the lexical ladder left unresolved
('none'/'ambiguous'): for a street near the station's already-matched coverage that no
other station has claimed, take the nearest same-named (ambiguous) or fuzzy-close (none)
register street. Flagged 'proximity' for review. Incremental --municipalities mode loads
all segments of the affected group_rep munis and proximity is muni-scoped, so its
'already claimed' snapshot and per-station anchors stay complete within scope.

A final OSM (Nominatim) fallback geocodes the few segments still unresolved against
OpenStreetMap, scoped to the station's municipality, and emits the returned geometry as the
coverage (method 'osm', always flagged for review). Responses are cached and committed
(data/osm_cache.json) so a recompute never re-queries; OSM_OFFLINE=1 runs cache-only.

  reads:  segments_amended.parquet, streets/settlements/addresses/stations parquet
  writes: segments.parquet (final, schema-ready), links.parquet, osm_claims.parquet

Usage:
  python3 stage04_match_addresses.py
  python3 stage04_match_addresses.py --municipalities 80381,70432   # incremental: re-match
                                                                   # only these (group_rep) munis

With ``--municipalities`` only segments whose station belongs to those group_rep
municipalities are re-matched (segment ids come from segments_amended.parquet and are
preserved, so reviewer overrides stay attached). The results are merged into the existing
complete segments.parquet / links.parquet — every claimant of a given street shares a
municipality, so conflict resolution stays identical to a full run.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict

import numpy as np
import polars as pl
from rapidfuzz import fuzz, process
from rapidfuzz.distance import DamerauLevenshtein
from scipy.spatial import cKDTree

import config
from common import osm
from common.boundaries import load_muni_boundaries
from common.coverage_parse import interval_parity
from common.normalize import genitive_variants, normalize_street, suffix_rank

FUZZY_MIN = config.STREET_FUZZY_MIN
# Min name length for the single-edit (Damerau ≤1) settlement match — below this a single edit
# can flip identity (БОР/БАР), above it it is overwhelmingly a declension or typo.
SETT_EDIT_MIN_LEN = 6


def resolve_settlement_from_address(address: str, muni: str, settlements_by_muni) -> str | None:
    """A polling station sits in one settlement, named in its address. RIK docs use BOTH
    orders: settlement-first ('КЕЛЕБИЈА, ПУТ ...') and settlement-last
    ('Јована Грчића Миленка 5, Черевић' — Beočin). Try the first comma token, then the last;
    first wins (settlement-first is the common form, and a street rarely resolves), so a
    settlement-last station gets its real home settlement instead of falling back to the
    eponymous town (which scoped its streets muni-wide → bogus muni_fallback + conflicts)."""
    if not address:
        return None
    parts = [p for p in address.split(",") if p.strip()]
    for tok in ([parts[0], parts[-1]] if len(parts) > 1 else parts[:1]):
        head = normalize_street(tok)
        if head:
            sid = resolve_settlement(head, muni, settlements_by_muni)
            if sid:
                return sid
    return None


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
    # Running x/y sums per street → centroid, for the proximity pass. Coords are UTM 34N
    # meters, so a plain mean and Euclidean distance are correct (no haversine needed).
    _xy_sum: dict[str, list[float]] = {}
    for aid, st, num, suf, x, y in zip(
        addresses["id"], addresses["street_id"], addresses["house_num"], addresses["house_suffix"],
        addresses["x"], addresses["y"]
    ):
        addr_by_street.setdefault(st, []).append((aid, num, suf or ""))
        acc = _xy_sum.setdefault(st, [0.0, 0.0, 0.0])
        acc[0] += x
        acc[1] += y
        acc[2] += 1
    street_centroid: dict[str, tuple[float, float]] = {
        st: (sx / n, sy / n) for st, (sx, sy, n) in _xy_sum.items() if n
    }

    settlements_by_muni: dict[str, list[tuple[str, str]]] = {}
    for sid, muni, name in zip(settlements["id"], settlements["municipality_id"], settlements["name_cyr"]):
        settlements_by_muni.setdefault(config.group_rep(muni), []).append((sid, normalize_street(name)))

    # Eponymous town settlement per municipality: the settlement whose name matches the
    # municipality's (Ваљево muni -> Ваљево town; Нови Београд -> "Београд (Нови Београд)"
    # via the word-containment fallback in resolve_settlement). Stations in the town list
    # streets without a settlement prefix in their address, so their home settlement can't
    # be derived from the address — it defaults here. Verified: a no-settlement station is
    # a town station (rural stations name their village as the address).
    municipalities = pl.read_parquet(config.MUNICIPALITIES_PARQUET)
    muni_name = dict(zip(municipalities["id"].cast(str), municipalities["name_cyr"]))
    town_settlement: dict[str, str | None] = {
        gmuni: (resolve_settlement(muni_name[gmuni], gmuni, settlements_by_muni)
                if gmuni in muni_name else None)
        for gmuni in settlements_by_muni
    }

    # station scope = the group rep of the station's municipality.
    station_muni = {sid: config.group_rep(m) for sid, m in zip(stations["id"], stations["municipality_id"])}
    station_settlement: dict[int, str | None] = {}
    # Stations whose settlement was INFERRED from the town fallback (address had no settlement
    # prefix). They are genuinely in the town, but the town doc may reference a peri-urban
    # street the register files under a neighbouring settlement — so, like a no-settlement
    # station, they still get the strict municipality-wide fuzzy last resort.
    station_settlement_inferred: set[int] = set()
    for sid, addr in zip(stations["id"], stations["address_cyr"]):
        from_addr = resolve_settlement_from_address(addr, station_muni[sid], settlements_by_muni)
        station_settlement[sid] = from_addr or town_settlement.get(station_muni[sid])
        if from_addr is None and station_settlement[sid] is not None:
            station_settlement_inferred.add(sid)
    # settlement id -> all its street ids (for village-name coverage claims).
    sett_to_streets: dict[str, list[str]] = {}
    for sid, meta in street_meta.items():
        sett_to_streets.setdefault(meta["settlement_id"], []).append(sid)

    return (street_meta, by_muni_norm, by_sett_norm, addr_by_street, settlements_by_muni,
            station_muni, station_settlement, sett_to_streets, station_settlement_inferred,
            street_centroid)


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
        # Near-miss declension / typo: a station address often uses a DECLINED settlement name
        # ("КОПЉАРИ" for register "КОПЉАРЕ", "ВЕНЧАНИ"/"ВЕНЧАНЕ") or a single mistyped letter
        # ("ШАИНИВАЦ"/"ШАИНОВАЦ", "НАФРЉЕ"/"НАДРЉЕ") that WRatio scores ~85 — below FUZZY_MIN.
        # A SINGLE edit (Damerau, so transpositions count as one) is a typo/inflection; TWO
        # edits already separate genuinely different places ("ДОЊА" vs "ГОРЊА ГРАБОВИЦА" = 2,
        # "ТОПОЛА ВАРОШ" vs "ВАРОШИЦА" = 3). Accept distance ≤ 1 only, and only when it is the
        # UNIQUE such settlement and both names are ≥ SETT_EDIT_MIN_LEN chars (a single edit on
        # a short name can flip identity). Nationwide: 9 stations, 0 false positives.
        if len(target) >= SETT_EDIT_MIN_LEN:
            close = [sid for sid, norm in cands
                     if abs(len(norm) - len(target)) <= 1 and DamerauLevenshtein.distance(target, norm) <= 1]
            if len(close) == 1:
                return close[0]
        # Unique word-containment: station addresses say "ЗЕМУН, ..." while the register
        # settlement is "БЕОГРАД (ЗЕМУН)" — WRatio length-penalizes that below threshold.
        tw = set(target.split())
        hits = [sid for sid, norm in cands if tw and tw <= set(norm.split())]
        if len(hits) == 1:
            return hits[0]
    return None


def build_marker_scopes(segs, station_muni, sett_exact_by_muni) -> dict[int, str]:
    """Coverage settlement markers -> {segment_id: marker settlement_id in effect}.

    A rural compact list names a settlement then lists its streets ("Копљаре, Бранислава
    Нушића, …"). Walking each station's segments in id (document) order, a WHOLE-street segment
    whose normalized name is EXACTLY a settlement of the muni becomes the current marker; every
    LATER segment records it. The marker segment itself gets the scope in effect before it (so it
    still resolves as a settlement claim, not a street within itself). The caller applies a
    marker only over an inferred-town home (see 5.1.2), so address-resolved stations are
    untouched."""
    by_station: dict[int, list[dict]] = defaultdict(list)
    for s in segs:
        by_station[s["station_id"]].append(s)
    out: dict[int, str] = {}
    for st, rows in by_station.items():
        exact = sett_exact_by_muni.get(station_muni.get(st), {})
        cur: str | None = None
        for s in sorted(rows, key=lambda x: x["id"]):
            if cur:
                out[s["id"]] = cur
            p = json.loads(s["parsed_json"])
            if p.get("whole") or not (p.get("intervals") or p.get("singles")):
                msid = exact.get(normalize_street(s["street_raw"] or ""))
                if msid:
                    cur = msid
    return out


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


# Generic structural / common-noun words that are NOT locality names: they lead many
# unrelated register streets, so a bare coverage of one of them must not sweep them all up
# ("Заселак" → every ЗАСЕЛАК… street; "Насеље" → every НАСЕЉЕ… street; "Пут" → every road).
_LOCALITY_STOP = {"ЗАСЕЛАК", "НАСЕЉЕ", "НАСЕЉЕНО", "САЛАШ", "ПОТЕС", "МАХАЛА",
                  "ПУТ", "БЛОК", "ТРГ", "КРАЈ", "СОКАК", "УЛИЦА", "ДЕО", "НОВА", "СТАРА"}


def _locality_streets(primary: str, scope: dict[str, list[str]], street_meta: dict[str, dict]
                      ) -> list[str]:
    """Sub-locality / hamlet (заселак) claim. The register has no separate naselje for some
    localities — it encodes them as a PREFIX on several street names within the parent
    settlement: doc "Ранчево" (Sombor) → "РАНЧЕВО ХИЛАНДАРСКА", "РАНЧЕВО ВУКА КАРАЏИЋА",
    "ЗАСЕЛАК РАНЧЕВО РЕЛИЋИ", … A single-word coverage that is the locality token of two or
    more such streets claims them all.

    The locality token is the street's FIRST word, or the word right after a leading
    "ЗАСЕЛАК". The remainder must be a NAME (not all-numeric — numbered parts are
    `_part_streets`' job, e.g. "ВОЈНИ ПУТ 1/2"). Guards against false localities:
      - `primary` is single-word and non-numeric (a locality name isn't "8");
      - only CANONICAL street names are considered (a key whose street's `name_norm` equals
        the key) — `scope` also holds declension/sortkey ALT keys pointing at the same or an
        unrelated street, which would otherwise fake a cluster ("ДОЊА БРДА МАЛА" surfaces
        under the sortkey "БРДА …"; "НИКОЛЕ ЛУЊЕВИЦЕ" under "ЛУЊЕВИЦА …");
      - a real locality has >=2 DISTINCT street ids (deduped here)."""
    if not primary or " " in primary or primary.isdigit() or primary in _LOCALITY_STOP:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for name, ids in scope.items():
        toks = name.split()
        if len(toks) >= 2 and toks[0] == primary:
            rest = toks[1:]
        elif len(toks) >= 3 and toks[0] == "ЗАСЕЛАК" and toks[1] == primary:
            rest = toks[2:]
        else:
            continue
        if not rest or all(w.isdigit() or w == "ДЕО" for w in rest):
            continue
        for i in ids:
            if i not in seen and street_meta.get(i, {}).get("name_norm") == name:  # canonical only
                seen.add(i)
                out.append(i)
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


# Title abbreviations the register spells out ("ПРОФ" doc form vs "ПРОФЕСОРА" register form).
# "ДР"->"ДОКТОРА" is already expanded in normalize on both sides; "ПРОФ" can't be (the register
# itself stores some streets abbreviated as "ПРОФ.", so expanding would split that population),
# so it is handled here as a token equivalence in the abbreviation match instead.
_TITLE_ABBREV = {"ПРОФ": "ПРОФЕСОРА"}


def _abbrev_token_match(d: str, r: str) -> bool:
    """One doc token `d` matches one register token `r` under abbreviation: a single-letter
    initial matches by first letter; a known title abbreviation matches its spelled-out form;
    otherwise the tokens must be equal."""
    if len(d) == 1 and d.isalpha():
        return r.startswith(d)
    return d == r or _TITLE_ABBREV.get(d) == r or _TITLE_ABBREV.get(r) == d


def _initial_abbrev_match(primary: str, scope: dict[str, list[str]],
                          street_meta: dict[str, dict]) -> str | None:
    """A doc street that abbreviates a word — a given name to its initial ("М.Пупина" -> "М
    ПУПИНА" for register "МИХАЈЛА ПУПИНА"; "Др В.Војиновића" -> "ДОКТОРА В ВОЈИНОВИЋА" for "ДР
    ВЛАДИМИРА ВОЈИНОВИЋА") or a title ("Проф Војислава Бабића" -> "ПРОФЕСОРА ВОЈИСЛАВА
    БАБИЋА"). Matched POSITIONALLY against the settlement's streets: an initial matches a
    register word by first letter, a title abbreviation matches its spelled-out form, every
    other token matches EXACTLY, same token count. Matched against each street's CANONICAL name
    (not the declension/sortkey alt keys, whose reordering would let an initial match the wrong
    word). Returns the register street id only when it is UNIQUE (two given names sharing an
    initial and surname — "МИХАЈЛА ПУПИНА" vs "МИЛАНА ПУПИНА" — are an unresolvable coin flip)."""
    dt = primary.split()
    has_abbrev = any((len(t) == 1 and t.isalpha()) or t in _TITLE_ABBREV for t in dt)
    if len(dt) < 2 or not has_abbrev:
        return None
    if not any(len(t) > 1 and t not in _TITLE_ABBREV for t in dt):  # need a full anchor (surname)
        return None
    ids_in_scope = {i for lst in scope.values() for i in lst}
    found: set[str] = set()
    for sid in ids_in_scope:
        rt = street_meta[sid]["name_norm"].split()
        if len(rt) == len(dt) and all(_abbrev_token_match(d, r) for d, r in zip(dt, rt)):
            found.add(sid)
            if len(found) > 1:
                return None
    return next(iter(found)) if len(found) == 1 else None


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


def _fuzzy_muni_unique(norm: str, names_map: dict[str, list[str]],
                       threshold: float) -> tuple[str, float] | None:
    """Municipality-wide fuzzy, used ONLY for stations with no resolvable home settlement
    (Belgrade/Niš city-municipalities, whose addresses carry no settlement head, so the
    settlement-scoped fuzzy never runs). Far stricter than settlement fuzzy: it fires only
    when a SINGLE register name clears `threshold` and that name maps to a SINGLE street.
    The uniqueness requirement is what keeps this from reintroducing the invented matches
    that retired the blanket muni-wide fuzzy (a typo'd street that doesn't exist would, at
    most, near-miss one real name — so it stays unresolved unless that lone candidate is
    unambiguous and a reviewer confirms it)."""
    if not names_map:
        return None
    nd = _DIGITS_RE.findall(norm)
    hits = [
        m for m in process.extract(norm, list(names_map.keys()), scorer=fuzz.WRatio,
                                   score_cutoff=threshold, limit=5)
        if _DIGITS_RE.findall(m[0]) == nd  # digit guard, as in _fuzzy
    ]
    if len(hits) != 1:
        return None
    name, score, _ = hits[0]
    ids = names_map[name]
    if len(ids) != 1:  # same name in multiple settlements of the muni — ambiguous, skip
        return None
    return ids[0], float(score)


def _station_anchor(resolved_xy: list[tuple[float, float]]
                    ) -> tuple[float, float, float] | None:
    """Centroid of a station's resolved-street centroids + an adaptive search radius.

    `resolved_xy` are the UTM centroids of the streets the station ALREADY matched. The
    radius scales with the station's own coverage extent (max distance from the centroid to
    any resolved street), clamped to [FLOOR, CAP]: tight in dense cities, wider in sparse
    villages. Returns (cx, cy, radius), or None when the station has nothing to anchor on —
    such stations are skipped (no sibling coverage to judge proximity against)."""
    if not resolved_xy:
        return None
    cx = sum(x for x, _ in resolved_xy) / len(resolved_xy)
    cy = sum(y for _, y in resolved_xy) / len(resolved_xy)
    extent = max((math.hypot(x - cx, y - cy) for x, y in resolved_xy), default=0.0)
    radius = min(max(config.PROXIMITY_RADIUS_FACTOR * extent,
                     config.PROXIMITY_RADIUS_FLOOR_M), config.PROXIMITY_RADIUS_CAP_M)
    return cx, cy, radius


def _nearest_unclaimed(anchor: tuple[float, float, float],
                       candidates: list[tuple[str, str, float, float]],
                       target_norm: str | None) -> tuple[str, float] | None:
    """Pick the nearest register street to `anchor` among `candidates`
    = [(street_id, name_norm, x, y)], all pre-filtered to UNCLAIMED streets WITHIN radius.

    Two modes:
      - disambiguation (`target_norm is None`): candidates are already the right name (the
        caller restricted them to the segment's same-named `amb_ids`) — just take the
        nearest.
      - fuzzy fallback (`target_norm` given): keep candidates whose name clears
        STREET_FUZZY_PROX_MIN, reusing the digit guard from `_fuzzy`; then take the nearest.

    If the two best candidates are exactly equidistant but different streets, skip — the
    same don't-guess caution as `_fuzzy_muni_unique`. Returns (street_id, score) or None."""
    cx, cy, _ = anchor
    scored: list[tuple[float, str, float]] = []
    for sid, name, x, y in candidates:
        if target_norm is not None:
            r = fuzz.WRatio(target_norm, name)
            if r < config.STREET_FUZZY_PROX_MIN:
                continue
            if _DIGITS_RE.findall(target_norm) != _DIGITS_RE.findall(name):
                continue
            score = float(r)
        else:
            score = 90.0
        scored.append((math.hypot(x - cx, y - cy), sid, score))
    if not scored:
        return None
    scored.sort()
    if len(scored) >= 2 and scored[0][0] == scored[1][0] and scored[0][1] != scored[1][1]:
        return None  # genuinely equidistant rivals — don't guess
    return scored[0][1], scored[0][2]


def _emit_claims(claims_by_street: dict[str, list[dict]], seg_id: int, station_id: int,
                 parsed: dict, targets: list[str], whole_kind: str) -> None:
    """Append a segment's parsed claims (whole / intervals / singles / bez_broja) to
    `claims_by_street` for each target register street. Shared by the pass-1 claim build,
    the reviewer-added claims, and the proximity pass."""
    for tid in targets:
        if parsed.get("whole"):
            claims_by_street.setdefault(tid, []).append(
                {"seg_id": seg_id, "station_id": station_id, "kind": whole_kind})
        else:
            for iv in parsed.get("intervals", []):
                claims_by_street.setdefault(tid, []).append({
                    "seg_id": seg_id, "station_id": station_id, "kind": "interval",
                    "lo": iv[0], "hi": iv[1], "parity": _iv_parity(iv),
                    "losfx": iv[3] if len(iv) > 3 else "", "hisfx": iv[4] if len(iv) > 4 else ""})
            for num, sfx in parsed.get("singles", []):
                claims_by_street.setdefault(tid, []).append({
                    "seg_id": seg_id, "station_id": station_id, "kind": "single",
                    "num": num, "suffix": sfx})
        # "бб" is additive: claims the street's no-number houses alongside any ranges.
        if parsed.get("bez_broja"):
            claims_by_street.setdefault(tid, []).append(
                {"seg_id": seg_id, "station_id": station_id, "kind": "bez_broja"})


_PAREN_RE = re.compile(r"\(([^)]*)\)")
# Normalized alias lookup: (municipality_id, normalized doc name) -> normalized register name.
_ALIASES = {
    (muni, normalize_street(doc)): normalize_street(reg)
    for (muni, doc), reg in config.STREET_ALIASES.items()
}


# Explicit "this clause names a whole SETTLEMENT, not a street" markers, in normalized form.
# A coverage like "насељено место Белотић" (every Vladimirci station writes this way) or
# "насеље Белосавци" claims the settlement by name. Longest prefix first so "НАСЕЉЕНО МЕСТО"
# is stripped in full rather than leaving a stray "НО МЕСТО …" behind a bare "НАСЕЉЕ" match.
_SETTLEMENT_PREFIXES = ("НАСЕЉЕНО МЕСТО ", "НАСЕЉЕ ")


def _strip_settlement_prefix(name: str) -> str:
    for p in _SETTLEMENT_PREFIXES:
        if name.startswith(p):
            return name[len(p):].strip()
    return name


def _has_coverage(parsed: dict) -> bool:
    """True when a parsed segment claims any coverage (whole street, бб, or specific numbers).
    An empty claim (e.g. a reviewer who cleared the segment) gets no OSM shape."""
    return bool(parsed.get("whole") or parsed.get("intervals")
                or parsed.get("singles") or parsed.get("bez_broja"))


def _weak_substring_fuzzy(rec, street_meta) -> bool:
    """A 'fuzzy' match that is really a single-word coverage caught as a NON-leading substring
    of a longer register street — the Sombor „Жарковац" → „БРАНКА РАДИЧЕВИЋА ЖАРКОВАЦ" trap:
    the hamlet token only appears as the street's last word, so WRatio's partial ratio links
    one street of a whole hamlet. These are routed to the OSM fallback (which prefers a real
    place polygon), but only on a hit — on a miss the fuzzy match is kept."""
    if rec["method"] != "fuzzy" or not rec["street_id"]:
        return False
    primary = _strip_settlement_prefix(normalize_street(_PAREN_RE.sub(" ", rec["street_raw"])))
    if not primary or " " in primary:  # single-token coverage only
        return False
    toks = street_meta.get(rec["street_id"], {}).get("name_norm", "").split()
    return len(toks) > 1 and primary in toks and toks[0] != primary


def resolve_street(street_raw, muni, settlement_id, idx, settlement_inferred=False
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
    # Sub-locality / hamlet (заселак): a single-word coverage that prefixes >=2 streets in the
    # home settlement ("Ранчево" -> all "РАНЧЕВО …" streets) claims them all. Before fuzzy so
    # it doesn't get hijacked into matching just one of the cluster's streets.
    loc = _locality_streets(primary, sett_scope, idx[0])
    if len(loc) >= 2:
        return loc[0], "locality", 80.0, loc[1:]
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
    # Initial-abbreviated given name ("М.Пупина" -> "МИХАЈЛА ПУПИНА"): a single-letter token
    # matches a register word by first letter, the rest (surname, titles) match exactly. An
    # inference, so settlement scope only and flagged for review.
    abv = _initial_abbrev_match(primary, sett_scope, idx[0])
    if abv:
        return abv, "abbrev", 70.0, []

    # Municipality exact fallback / ambiguity detection.
    for key in ([primary] + ([alt] if alt else [])):
        ids = muni_scope.get(key)
        if not ids:
            continue
        if len(ids) == 1:
            return ids[0], ("muni_fallback" if settlement_id else exact_method), 100.0, []
        return None, "ambiguous", 0.0, ids
    # Municipality-wide fuzzy — for stations with no home settlement OR an inferred town
    # scope (Belgrade/Niš/Valjevo-style town stations, whose addresses carry no settlement
    # head, so the settlement-scoped fuzzy above never finds a street the register files
    # under a neighbouring settlement; a one-letter doc typo like "Михаила"->"Михајла" Пупина
    # would otherwise fall through to no_match). Stricter cutoff + single-candidate guard;
    # flagged 'fuzzy' so the reviewer sees the doc->register name discrepancy and confirms.
    if not settlement_id or settlement_inferred:
        hit = _fuzzy_muni_unique(primary, muni_scope, config.STREET_FUZZY_MUNI_MIN)
        if hit:
            return hit[0], "fuzzy", hit[1], []
    # Village-name coverage: some stations name a whole SETTLEMENT instead of streets, either
    # bare ("Белосавци" in Topola) or with an explicit marker ("насељено место Белотић" — the
    # whole of Vladimirci; "насеље Белосавци"). If the (de-prefixed) name matches a settlement
    # in the municipality, LAST RESORT — only when no street matches anywhere in the
    # municipality (otherwise it hijacks cross-settlement street matches); claim every street in
    # it (extra ids ride in the 4th slot, method 'settlement').
    setts_by_muni, sett_to_streets = idx[4], idx[7]
    sett_key = _strip_settlement_prefix(primary)
    for s_id, s_norm in setts_by_muni.get(muni, []):
        if s_norm == sett_key:
            streets = sett_to_streets.get(s_id, [])
            if streets:
                return streets[0], "settlement", 85.0, streets[1:]

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


def _interval_spec(num: int, suf: str, c: dict) -> float:
    """Specificity of an interval match for one house. A suffixed house matched only
    because a bare range bound implies all suffixes (no losfx/hisfx pinning that edge)
    is demoted, so '2-60' yields 60а to a station that lists '60а-80'. The house has
    already passed _bounds_ok, so a suffix sitting at an edge with a suffix bound set is
    explicitly covered → full spec; interior and bare-bound suffixed houses are implied."""
    if suf and not ((num == c["lo"] and c.get("losfx")) or (num == c["hi"] and c.get("hisfx"))):
        return SPEC_INTERVAL_IMPLIED_SUFFIX
    return SPEC_INTERVAL


# Claim specificity (higher wins). An exact single (number + suffix) beats a bare number
# implying its suffixed variants, which beats a range, which beats a whole street. The
# implied level lets "Пушкинов трг 5" also claim 5а/5б/... unless another station lists
# that exact suffixed address.
SPEC_EXACT_SINGLE = 3
SPEC_IMPLIED_SINGLE = 2
SPEC_INTERVAL = 1
# A suffixed house matched ONLY because a bare range bound implies all its suffixes
# (e.g. "2-60" reaching 60а) yields to a claim that names that suffix explicitly — an
# exact single (spec 3) or a suffix-bounded range edge like "60а-80" (spec SPEC_INTERVAL).
SPEC_INTERVAL_IMPLIED_SUFFIX = 0.5
# bez_broja ("бб"): claims a street's no-number (house_num IS NULL) houses. It only ever
# competes with whole/sett_whole for those NULL-house addresses (intervals/singles need a
# number), so an explicit "бб" outranks a generic whole-street claim there.
SPEC_BEZ_BROJA = 1
SPEC_WHOLE = 0
SPEC_SETT_WHOLE = -1  # village-name claim: yields to ANY street-level claim


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
        cands: list[tuple[int, dict]] = []
        for c in claims:
            k = c["kind"]
            # whole / sett_whole cover every house including the no-number ones.
            if k == "whole":
                cands.append((SPEC_WHOLE, c))
            elif k == "sett_whole":
                cands.append((SPEC_SETT_WHOLE, c))
            elif k == "bez_broja":
                # "бб" claims ONLY the no-number houses (house_num IS NULL).
                if num is None:
                    cands.append((SPEC_BEZ_BROJA, c))
            elif num is None:
                continue  # interval / single need a house number
            elif k == "interval":
                if c["lo"] <= num <= c["hi"] and _parity_ok(num, c["parity"]) and _bounds_ok(num, suf, c):
                    cands.append((_interval_spec(num, suf, c), c))
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


def osm_fallback_pass(pending, station_muni, muni_bounds, muni_name,
                      claims_by_street, street_meta, resolved_by_station) -> list[dict]:
    """Geocode unresolved (or weakly-matched) segments against OpenStreetMap, emit geometry.

    Two kinds of segment fall here:
    - 'none' / 'ambiguous' — a street or settlement the register cannot place at all (a hamlet
      stored only as suffixes on retired, address-less streets, or a locality with no naselje);
    - a weak-substring 'fuzzy' match (`_weak_substring_fuzzy`) — a single-word hamlet caught as
      a non-leading substring of a longer street (the Sombor „Жарковац" → „БРАНКА РАДИЧЕВИЋА
      ЖАРКОВАЦ" trap). These prefer a real OSM place polygon; on a HIT their (wrong) register
      claim is pulled, on a MISS the fuzzy match is kept untouched.

    For each we geocode the name SCOPED to the station's municipality (its bbox bounds the
    search, so we get this muni's place, not a same-named one ~150 km away in Ruma), clip the
    returned geometry to the municipality boundary, and return it as an OSM coverage claim that
    stage05 draws directly (like a whole-settlement claim). Matched seg recs become method='osm'
    in place; responses are cached by the osm module (committed data/osm_cache.json).
    """
    import shapely
    from shapely import wkt as shapely_wkt
    from shapely.ops import transform as shp_transform
    from pyproj import Transformer

    to_wgs = Transformer.from_crs(config.UTM_34N, config.WGS84, always_xy=True)
    viewbox_cache: dict[str, tuple[float, float, float, float] | None] = {}

    def _viewbox(mid: str):
        # Municipality bbox in WGS84 (lon/lat) for the Nominatim viewbox; computed once.
        if mid not in viewbox_cache:
            b = muni_bounds.get(mid)
            if b is None:
                viewbox_cache[mid] = None
            else:
                wgs = shp_transform(lambda xs, ys: to_wgs.transform(xs, ys), b)
                viewbox_cache[mid] = wgs.bounds  # (min_lon, min_lat, max_lon, max_lat)
        return viewbox_cache[mid]

    osm_claims: list[dict] = []
    n_far = 0
    for r in pending:
        mid = station_muni.get(r["station_id"])
        if mid is None or mid not in muni_name:
            continue
        name = _strip_settlement_prefix(normalize_street(_PAREN_RE.sub(" ", r["street_raw"])))
        if not name:
            continue
        # Never geocode a query with no letters (a bare number like "54" left by a mis-parsed
        # house number): Nominatim resolves it to an unrelated numbered admin relation far from
        # the station (e.g. a 9 km² area over the town centre). A place name has letters.
        if not any(ch.isalpha() for ch in name):
            continue
        vb = _viewbox(mid)
        # A weak-substring fuzzy match means the doc word is really a PLACE the register lacks —
        # only accept a place/settlement here (a street query would just find another street).
        weak_fuzzy = _weak_substring_fuzzy(r, street_meta)
        kinds = ("settlement",) if weak_fuzzy else ("settlement", "street")
        result = kind = None
        for k in kinds:
            hit = osm.geocode(k, name, mid, muni_name[mid], vb)
            if hit:
                result, kind = hit, k
                break
        if result is None:
            continue  # miss: 'none'/'ambiguous' stay unresolved, weak-fuzzy keeps its match
        geom = osm.to_coverage_geom(result)
        if geom is None or geom.is_empty:
            continue
        clip = muni_bounds.get(mid)
        if clip is not None:
            geom = shapely.make_valid(geom.intersection(clip).buffer(0))
            if geom.is_empty:
                continue
        # Geographic sanity: a common street name often geocodes to a same-named place far from
        # this station's real coverage (a polygon over another town's centre). Reject the claim
        # when it sits farther than OSM_MAX_COVERAGE_DIST_M from every resolved-street centroid
        # the station already has. Stations with no resolved coverage have no anchor -> exempt.
        anchor_pts = resolved_by_station.get(r["station_id"])
        if anchor_pts:
            d = min(geom.distance(shapely.Point(px, py)) for px, py in anchor_pts)
            if d > config.OSM_MAX_COVERAGE_DIST_M:
                n_far += 1
                continue
        # Hit: if this was a (wrong) fuzzy street match, pull its already-emitted claims so the
        # OSM geometry — not the substring street's addresses — becomes the coverage.
        old_sid = r["street_id"]
        if old_sid and old_sid in claims_by_street:
            kept = [c for c in claims_by_street[old_sid] if c["seg_id"] != r["id"]]
            if kept:
                claims_by_street[old_sid] = kept
            else:
                del claims_by_street[old_sid]
        osm_claims.append({
            "station_id": r["station_id"], "segment_id": r["id"], "kind": kind,
            "query": name, "osm_type": str(result.get("osm_type")),
            "osm_id": str(result.get("osm_id")), "wkt": shapely_wkt.dumps(geom),
        })
        # Resolved by OSM: no register street/address links, the geometry is the coverage.
        r["method"], r["score"], r["amb_ids"], r["street_id"] = "osm", 50.0, [], None
    if n_far:
        print(f"  OSM claims rejected as far from coverage: {n_far:,}")
    return osm_claims


def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve streets and match coverage to addresses.")
    ap.add_argument(
        "--municipalities",
        help="Comma-separated group_rep municipality ids; re-match only their segments and "
             "merge into the existing segments/links parquet. Default: full rebuild.",
    )
    args = ap.parse_args()
    municipalities = (
        {m.strip() for m in args.municipalities.split(",") if m.strip()}
        if args.municipalities else None
    )

    config.ensure_artifacts()
    idx = build_indexes()
    (street_meta, _bmn, _bsn, addr_by_street, settlements_by_muni,
     station_muni, station_settlement, _sett_streets, station_settlement_inferred,
     street_centroid) = idx

    segs = pl.read_parquet(config.SEGMENTS_AMENDED_PARQUET).to_dicts()
    # Incremental scope: keep only segments whose station is in an affected municipality.
    # station_muni already maps each station to its group_rep, so membership is exact.
    if municipalities is not None:
        segs = [s for s in segs if station_muni.get(s["station_id"]) in municipalities]
        print(f"  [incremental: {len(municipalities)} muni] re-matching {len(segs):,} segments")

    # Reviewer overrides exported from D1 (fetch_overrides.sh). Manual street assignments
    # and number edits take precedence over machine parsing, so polygons reflect review.
    overrides: dict[int, dict] = {}
    if config.OVERRIDES_JSON.exists():
        overrides = {int(o["segment_id"]): o for o in json.loads(config.OVERRIDES_JSON.read_text())}
        print(f"  reviewer overrides loaded: {len(overrides):,}")

    # Coverage settlement markers: in rural docs the compact list NAMES a settlement, then
    # lists its streets ("Копљаре, Бранислава Нушића, Карађорђева, …"). Like "Насеље:" in the
    # structured dialect, that bare settlement name scopes the streets that FOLLOW it to that
    # settlement. Build, per segment, the marker settlement in effect (order-based, segment ids
    # are positional). A marker is a whole-street segment whose name is EXACTLY a settlement of
    # the municipality. Used only to override an INFERRED home (the eponymous-town fallback when
    # the address settlement didn't resolve) — without it the village's streets, whose names
    # also exist in the muni's town, fall back muni-wide and mis-match the town.
    sett_exact_by_muni: dict[str, dict[str, str]] = {}
    for _muni, _cands in settlements_by_muni.items():
        d: dict[str, str] = {}
        for _sid, _norm in _cands:
            d.setdefault(_norm, _sid)
        sett_exact_by_muni[_muni] = d
    seg_marker_sett = build_marker_scopes(segs, station_muni, sett_exact_by_muni)

    # Pass 1: resolve a register street for every segment.
    seg_recs: list[dict] = []
    claims_by_street: dict[str, list[dict]] = {}
    for s in segs:
        muni = station_muni.get(s["station_id"])
        parsed = json.loads(s["parsed_json"])
        # Scope to the segment's own settlement if labelled, else the station's home
        # settlement (from its address); falls back to municipality inside resolve_street.
        seg_sett = resolve_settlement(s["settlement_raw"], muni, settlements_by_muni)
        home_inferred = s["station_id"] in station_settlement_inferred
        # A coverage settlement marker beats the eponymous-town guess (but never a real address
        # settlement): it is an explicit in-document scope, so the streets resolve in the named
        # village instead of mis-matching the same-named town streets.
        marker_sett = seg_marker_sett.get(s["id"]) if home_inferred else None
        settlement_id = seg_sett or marker_sett or station_settlement.get(s["station_id"])
        # The scope is an INFERRED town only when neither the segment, a marker, nor the address
        # pinned a settlement — that's when the muni-wide fuzzy last resort still applies.
        settlement_inferred = seg_sett is None and marker_sett is None and home_inferred
        street_id, method, score, amb_ids = resolve_street(
            s["street_raw"], muni, settlement_id, idx, settlement_inferred)

        # Apply reviewer override: manual street wins (if it exists in the register) and
        # manual number edits replace the machine parse for claim building. A "sett:<id>"
        # pick means the reviewer chose a whole settlement (village / city area): anchor
        # on its first street and ride the rest in amb_ids, like document village claims.
        ov = overrides.get(s["id"])
        if ov:
            ov_sid = ov.get("manual_street_id")
            if ov_sid == "none":
                # Reviewer confirmed the street does not exist in the register: drop any
                # machine match so no links/polygon are built, and treat it as resolved.
                street_id, method, score, amb_ids = None, "manual_none", 100.0, []
            elif ov_sid and ov_sid.startswith("sett:"):
                sett_streets = _sett_streets.get(ov_sid[len("sett:"):], [])
                if sett_streets:
                    street_id, method, score, amb_ids = (
                        sett_streets[0], "manual_settlement", 100.0, sett_streets[1:])
            elif ov_sid and ov_sid in street_meta:
                street_id, method, score, amb_ids = ov_sid, "manual", 100.0, []
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
        targets = [street_id] + (
            r["amb_ids"] if r["method"] in ("base_parts", "locality", "settlement", "manual_settlement") else [])
        whole_kind = "sett_whole" if r["method"] in ("settlement", "manual_settlement") else "whole"
        _emit_claims(claims_by_street, s["id"], s["station_id"], parsed, targets, whole_kind)

    # Settlement names (for added-claim labels and human-readable reasons below).
    _setts = pl.read_parquet(config.SETTLEMENTS_PARQUET)
    sett_names = dict(zip(_setts["id"], _setts["name_cyr"]))

    # Reviewer-added street claims (streets the document omitted entirely): synthetic
    # segments with deterministic ids (ADDED_SEG_BASE + addition id), claims like any
    # other, and materialized into coverage_segments so links keep FK integrity.
    # A "sett:<id>" street_id is a whole-settlement claim from the area-aware picker.
    # Whole-settlement coverage claims (station -> settlement). stage05 uses the official
    # naselje boundary as the coverage polygon for these instead of the point-Voronoi shape.
    sett_claims: list[dict] = []
    added_out: list[dict] = []
    if config.ADDITIONS_JSON.exists():
        additions = json.loads(config.ADDITIONS_JSON.read_text())
        if municipalities is not None:
            additions = [a for a in additions
                         if station_muni.get(a["station_id"]) in municipalities]
        n_add = 0
        for a in additions:
            sett_id = (a["street_id"][len("sett:"):]
                       if str(a["street_id"]).startswith("sett:") else None)
            if sett_id:
                targets = _sett_streets.get(sett_id, [])
                if not targets:
                    continue
            elif a["street_id"] in street_meta:
                targets = [a["street_id"]]
            else:
                continue
            try:
                mp = json.loads(a["manual_json"])
            except (ValueError, TypeError):
                continue
            seg_id = config.ADDED_SEG_BASE + int(a["id"])
            parsed = {"intervals": mp.get("intervals", []), "singles": mp.get("singles", []),
                      "whole": bool(mp.get("whole")), "bez_broja": bool(mp.get("bez_broja")),
                      "unknown_tokens": []}
            # Settlement claims yield to any street-level claim, like document ones.
            whole_kind = "sett_whole" if sett_id else "whole"
            _emit_claims(claims_by_street, seg_id, a["station_id"], parsed, targets, whole_kind)
            if sett_id:
                sett_claims.append({"station_id": a["station_id"], "settlement_id": sett_id})
            sname = sett_names.get(sett_id, sett_id) if sett_id else None
            added_out.append({
                "id": seg_id, "station_id": a["station_id"], "settlement_raw": None,
                "street_raw": sname or street_meta[targets[0]]["name_norm"],
                "street_id": targets[0],
                "kind": "manual_added", "parsed_json": json.dumps(parsed, ensure_ascii=False),
                "manual_json": None, "manual_locked": 1, "confidence": 0.9, "needs_review": 0,
                # The settlement_claim marker makes the UI title the card by the area
                # name (street_raw) instead of the anchor street's register name.
                "review_reason": f"settlement_claim:{sname}" if sname else None,
                "parse_dialect": "manual", "source": "added",
                "amendment_note": None,
            })
            n_add += 1
        if n_add:
            print(f"  reviewer-added street claims: {n_add:,}")

    # Proximity pass: a polling station covers a contiguous neighbourhood, so a street the
    # lexical ladder left unresolved is almost always physically near the streets the
    # station ALREADY matched — and one no other station has claimed. We anchor on the
    # station's resolved-street centroids, search a radius adapted to its own coverage
    # extent, and take the nearest UNCLAIMED register street that is either same-named (the
    # 'ambiguous' case) or fuzzy-close (the 'none' case). Runs AFTER pass 1 + reviewer/added
    # claims so `claimed` is complete; new matches feed pass 2 like any other claim. Every
    # one is flagged 'proximity' for review. (Incremental mode loads all segments of the
    # affected group_rep munis, and proximity is muni-scoped, so the snapshot is complete.)
    claimed = set(claims_by_street.keys())
    resolved_by_station: dict[int, list[tuple[float, float]]] = {}
    for r in seg_recs:
        if not r["street_id"] or r["old_name_dup"]:
            continue
        extra = r["amb_ids"] if r["method"] in ("base_parts", "locality", "settlement", "manual_settlement") else []
        for tid in (r["street_id"], *extra):
            c = street_centroid.get(tid)
            if c:
                resolved_by_station.setdefault(r["station_id"], []).append(c)

    # Candidate pool per group_rep muni: unclaimed register streets that have a centroid.
    cand_by_muni: dict[str, list[tuple[str, str, float, float]]] = defaultdict(list)
    for sid, meta in street_meta.items():
        if sid in claimed:
            continue
        c = street_centroid.get(sid)
        if c is None:
            continue
        gmuni = config.group_rep(meta["municipality_id"]) if meta["municipality_id"] else None
        cand_by_muni[gmuni].append((sid, meta["name_norm"], c[0], c[1]))
    trees: dict[str, tuple[cKDTree, list]] = {
        gmuni: (cKDTree(np.array([[x, y] for *_, x, y in cands])), cands)
        for gmuni, cands in cand_by_muni.items()
    }

    newly_claimed: set[str] = set()
    n_prox = n_disamb = 0
    for r in seg_recs:
        if r["method"] not in ("none", "ambiguous"):
            continue
        anchor = _station_anchor(resolved_by_station.get(r["station_id"], []))
        if anchor is None:
            continue
        entry = trees.get(station_muni.get(r["station_id"]))
        if entry is None:
            continue
        tree, cands = entry
        near = tree.query_ball_point([anchor[0], anchor[1]], anchor[2])
        if not near:
            continue
        if r["method"] == "ambiguous":
            amb = set(r["amb_ids"])
            pool = [cands[i] for i in near if cands[i][0] in amb and cands[i][0] not in newly_claimed]
            hit = _nearest_unclaimed(anchor, pool, None)
        else:
            target = normalize_street(_PAREN_RE.sub(" ", r["street_raw"])) or normalize_street(r["street_raw"])
            pool = [cands[i] for i in near if cands[i][0] not in newly_claimed]
            hit = _nearest_unclaimed(anchor, pool, target)
        if not hit:
            continue
        sid, score = hit
        n_disamb += r["method"] == "ambiguous"
        r["street_id"], r["method"], r["score"], r["amb_ids"] = sid, "proximity", score, []
        newly_claimed.add(sid)
        _emit_claims(claims_by_street, r["id"], r["station_id"], r["parsed"], [sid], "whole")
        n_prox += 1
    if n_prox:
        print(f"  proximity matches: {n_prox:,} ({n_disamb:,} disambiguated)")

    # OSM (Nominatim) fallback: geocode the few still-unresolved segments against OpenStreetMap,
    # scoped to the station's municipality, and emit the returned geometry as the coverage (see
    # osm_fallback_pass). Skipped when offline with an empty cache (no network and nothing to
    # replay), so the offline tests never touch it.
    osm_claims: list[dict] = []
    # The OSM claim draws the whole geocoded street/area — meaningful only when the segment
    # actually claims coverage. A segment with EMPTY effective coverage (no whole/intervals/
    # singles/бб) claims nothing, so it gets no OSM shape. This also lets a reviewer suppress an
    # OSM polygon by clearing the segment's coverage (the intuitive action), not only via the
    # "doesn't exist" button (which sets method 'manual_none', already excluded here).
    osm_pending = [r for r in seg_recs
                   if (r["method"] in ("none", "ambiguous") or _weak_substring_fuzzy(r, street_meta))
                   and _has_coverage(r["parsed"])]
    if osm_pending and not (osm._offline() and not config.OSM_CACHE_JSON.exists()):
        muni_bounds = load_muni_boundaries()  # {muni_id: polygon (UTM34N)}
        m = pl.read_parquet(config.MUNICIPALITIES_PARQUET)
        muni_name = dict(zip(m["id"], m["name_lat"]))
        osm_claims = osm_fallback_pass(osm_pending, station_muni, muni_bounds, muni_name,
                                       claims_by_street, street_meta, resolved_by_station)
        osm.flush_cache()
        if osm_claims:
            print(f"  OSM fallback matches: {len(osm_claims):,}")

    # Pass 2: resolve each street; collect links + per-segment flags.
    links: list[dict] = []
    matched_seg_ids: set[int] = set()
    conflict_map: dict[int, set[int]] = {}  # seg_id -> opposing station_ids
    parity_unconfirmed: set[int] = set()
    seg_conf = {r["id"]: round(r["score"] / 100.0, 2) for r in seg_recs}
    # Printed station number per station id (for human-readable conflict reasons).
    _st = pl.read_parquet(config.STATIONS_PARQUET)
    station_number = dict(zip(_st["id"], _st["number"]))

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
        if method == "manual_none":
            # Reviewer confirmed the street doesn't exist: resolved, no street, no flag.
            out_segs.append({
                "id": r["id"], "station_id": r["station_id"], "settlement_raw": r["settlement_raw"],
                "street_raw": r["street_raw"], "street_id": None, "kind": r["kind"],
                "parsed_json": r["parsed_json"], "manual_json": None, "manual_locked": 1,
                "confidence": 0.9, "needs_review": 0, "review_reason": None,
                "parse_dialect": r["parse_dialect"], "source": r["source"],
                "amendment_note": r.get("amendment_note"),
            })
            continue
        reasons: list[str] = []
        if method == "ambiguous":
            # Same-named street in several other settlements — not resolved automatically.
            conf = 0.2
            setts = sorted({
                sett_names.get(street_meta[sid]["settlement_id"], "")
                for sid in r["amb_ids"] if sid in street_meta
            })
            reasons.append("ambiguous:" + "|".join(n for n in setts if n))
        elif method == "osm":
            # Geocoded against OpenStreetMap — no register street; the OSM geometry is drawn as
            # the coverage in stage05. Always surfaced so the reviewer confirms the place/extent.
            conf = 0.5
            reasons.append("osm_fallback")
        elif r["street_id"] is None:
            conf = 0.2
            reasons.append("street_unresolved")
        elif method == "fuzzy":
            conf = 0.5
            reasons.append("fuzzy")
        elif method == "proximity":
            # Resolved by geographic proximity to the station's other coverage — always
            # surfaced so the reviewer confirms the cross-settlement / same-name pick.
            conf = 0.5
            reasons.append("proximity")
        elif method == "alias":
            # Hand-maintained substitution — surfaced so the reviewer confirms it once.
            conf = 0.6
            reasons.append("alias")
        elif method == "abbrev":
            # Initial-abbreviated given name expanded to a settlement street — confirmable.
            conf = 0.6
            reasons.append("abbrev")
        elif method in ("manual", "manual_settlement"):
            conf = 0.9  # reviewer-assigned street/settlement; no flag
            if method == "manual_settlement" and r["street_id"] in street_meta:
                sett_claims.append({"station_id": r["station_id"],
                                    "settlement_id": street_meta[r["street_id"]]["settlement_id"]})
        elif method == "base_parts":
            # Plain base name expanded to all numbered part streets — confirmable.
            conf = 0.7
            reasons.append("base_parts")
        elif method == "locality":
            # Single-word coverage expanded to all streets of a register sub-locality/hamlet
            # (заселак prefix) — confirmable.
            conf = 0.7
            reasons.append("locality")
        elif method == "settlement":
            # Village-name coverage: the whole settlement is claimed — confirmable.
            conf = 0.8
            claim_sett = street_meta[r["street_id"]]["settlement_id"]
            sett_claims.append({"station_id": r["station_id"], "settlement_id": claim_sett})
            sname = sett_names.get(claim_sett, "")
            reasons.append("settlement_claim:" + sname if sname else "settlement_claim")
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
        if (not parsed.get("whole") and r["id"] not in matched_seg_ids
                and not r.get("old_name_dup") and method != "osm"):
            reasons.append("no_match")
        if r["id"] in conflict_map:
            # Parameterized code: conflict:<opposing station numbers joined by |>.
            nums = sorted({station_number.get(sid, sid) for sid in conflict_map[r["id"]]})
            reasons.append("conflict:" + "|".join(str(n) for n in nums))
        if r["id"] in parity_unconfirmed:
            reasons.append("parity_unconfirmed")

        # parity_unconfirmed is informational only: the inferred odd/even side has proven
        # correct in the vast majority of cases, so it no longer triggers review on its own.
        # It stays in review_reason (shown as context when the segment is flagged for some
        # OTHER reason, and the per-range parity dropdown remains available either way), but
        # a segment flagged ONLY for parity is treated as resolved.
        flagging = [x for x in reasons if x != "parity_unconfirmed"]

        out_segs.append({
            "id": r["id"], "station_id": r["station_id"], "settlement_raw": r["settlement_raw"],
            "street_raw": r["street_raw"], "street_id": r["street_id"], "kind": r["kind"],
            "parsed_json": r["parsed_json"], "manual_json": None, "manual_locked": 0,
            "confidence": conf, "needs_review": int(bool(flagging)),
            "review_reason": ",".join(reasons) or None,
            "parse_dialect": r["parse_dialect"], "source": r["source"],
            "amendment_note": r.get("amendment_note"),
        })

    out_segs.extend(added_out)

    sett_claims_df = pl.DataFrame(
        sett_claims, schema={"station_id": pl.Int64, "settlement_id": pl.String}
    ).unique()
    osm_claims_df = pl.DataFrame(osm_claims, schema={
        "station_id": pl.Int64, "segment_id": pl.Int64, "kind": pl.String,
        "query": pl.String, "osm_type": pl.String, "osm_id": pl.String, "wkt": pl.String,
    })

    if municipalities is None:
        pl.DataFrame(out_segs, infer_schema_length=None).write_parquet(config.SEGMENTS_PARQUET)
        pl.DataFrame(links, infer_schema_length=None).write_parquet(config.LINKS_PARQUET)
        sett_claims_df.write_parquet(config.STATION_SETT_CLAIMS_PARQUET)
        osm_claims_df.write_parquet(config.OSM_CLAIMS_PARQUET)
    else:
        # Merge into the complete parquets: drop every affected station's old segments and
        # links, then append the freshly matched ones (segment ids preserved). Untouched
        # stations carry over verbatim, so the output stays complete for stage06.
        affected = [sid for sid, rep in station_muni.items() if rep in municipalities]
        prev_segs = pl.read_parquet(config.SEGMENTS_PARQUET)
        prev_links = pl.read_parquet(config.LINKS_PARQUET)
        seg_parts = [prev_segs.filter(~pl.col("station_id").is_in(affected))]
        if out_segs:
            seg_parts.append(
                pl.DataFrame(out_segs, infer_schema_length=None).select(prev_segs.columns))
        link_parts = [prev_links.filter(~pl.col("station_id").is_in(affected))]
        if links:
            link_parts.append(
                pl.DataFrame(links, infer_schema_length=None).select(prev_links.columns))
        pl.concat(seg_parts, how="vertical_relaxed").write_parquet(config.SEGMENTS_PARQUET)
        pl.concat(link_parts, how="vertical_relaxed").write_parquet(config.LINKS_PARQUET)
        # Settlement-claim map: drop affected stations' rows, append the fresh ones.
        if config.STATION_SETT_CLAIMS_PARQUET.exists():
            prev_sc = pl.read_parquet(config.STATION_SETT_CLAIMS_PARQUET)
            sc_parts = [prev_sc.filter(~pl.col("station_id").is_in(affected))]
            if sett_claims_df.height:
                sc_parts.append(sett_claims_df.select(prev_sc.columns))
            pl.concat(sc_parts, how="vertical_relaxed").unique().write_parquet(
                config.STATION_SETT_CLAIMS_PARQUET)
        else:
            sett_claims_df.write_parquet(config.STATION_SETT_CLAIMS_PARQUET)
        # OSM-claim geometry: drop affected stations' rows, append the fresh ones.
        if config.OSM_CLAIMS_PARQUET.exists():
            prev_osm = pl.read_parquet(config.OSM_CLAIMS_PARQUET)
            osm_parts = [prev_osm.filter(~pl.col("station_id").is_in(affected))]
            if osm_claims_df.height:
                osm_parts.append(osm_claims_df.select(prev_osm.columns))
            pl.concat(osm_parts, how="vertical_relaxed").write_parquet(config.OSM_CLAIMS_PARQUET)
        else:
            osm_claims_df.write_parquet(config.OSM_CLAIMS_PARQUET)

    n_review = sum(x["needs_review"] for x in out_segs)
    n_unres = sum(1 for x in out_segs if x["street_id"] is None)
    scope = "" if municipalities is None else "[incremental] recomputed "
    print(f"  {scope}segments: {len(out_segs):,}  links: {len(links):,}  needs_review: {n_review:,}  "
          f"unresolved_street: {n_unres:,}  conflicts: {len(conflict_map):,}  "
          f"parity_unconfirmed: {len(parity_unconfirmed):,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
