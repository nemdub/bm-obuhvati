"""Tests for stage04 claim resolution — see docs/parsing-matching/06-claim-resolution.md.

`resolve_street_claims(claims, rows)` reconciles every station's claims on ONE register
street against that street's real houses. Each house goes to exactly one station; equal
top-specificity across stations is a conflict.

  rows:   list of (address_id, house_num|None, suffix)
  claims: list of dicts {seg_id, station_id, kind, ...kind-specific fields}
  returns (assigned: aid -> winning claim, conflicts: seg_id -> {opposing station_ids},
           parity_unconfirmed: {seg_id})
"""

import stage04_match_addresses as S4


def _whole(seg_id, station_id, kind="whole"):
    return {"seg_id": seg_id, "station_id": station_id, "kind": kind}


def _single(seg_id, station_id, num, suffix=""):
    return {"seg_id": seg_id, "station_id": station_id, "kind": "single", "num": num, "suffix": suffix}


def _interval(seg_id, station_id, lo, hi, parity, losfx="", hisfx=""):
    return {"seg_id": seg_id, "station_id": station_id, "kind": "interval",
            "lo": lo, "hi": hi, "parity": parity, "losfx": losfx, "hisfx": hisfx}


def _bb(seg_id, station_id):
    return {"seg_id": seg_id, "station_id": station_id, "kind": "bez_broja"}


def _winners(assigned):
    return {aid: w["station_id"] for aid, w in assigned.items()}


# ── Specificity ──────────────────────────────────────────────────────────────
class TestSpecificity:
    def test_exact_single_beats_bare_implied(self):
        # Station A claims bare "5" (implies 5а); B claims "5а" exactly -> B wins 5а.
        rows = [(1, 5, ""), (2, 5, "А")]
        claims = [_single(10, 100, 5, ""), _single(20, 200, 5, "А")]
        assigned, conflicts, _ = S4.resolve_street_claims(claims, rows)
        assert _winners(assigned) == {1: 100, 2: 200}
        assert conflicts == {}

    def test_bare_number_implies_suffixed(self):
        # Only A claims "5"; it should also pick up 5а (no competing exact claim).
        rows = [(1, 5, ""), (2, 5, "А")]
        claims = [_single(10, 100, 5, "")]
        assigned, _, _ = S4.resolve_street_claims(claims, rows)
        assert _winners(assigned) == {1: 100, 2: 100}

    def test_interval_beats_whole(self):
        rows = [(1, 7, "")]
        claims = [_interval(10, 100, 1, 10, "all"), _whole(20, 200)]
        assigned, _, _ = S4.resolve_street_claims(claims, rows)
        assert _winners(assigned) == {1: 100}

    def test_whole_beats_settlement(self):
        # sett_whole (spec -1) yields to any street-level claim.
        rows = [(1, 7, "")]
        claims = [_whole(10, 100, kind="sett_whole"), _whole(20, 200, kind="whole")]
        assigned, _, _ = S4.resolve_street_claims(claims, rows)
        assert _winners(assigned) == {1: 200}


# ── Conflicts ────────────────────────────────────────────────────────────────
class TestConflicts:
    def test_two_whole_same_spec_conflict(self):
        rows = [(1, 7, "")]
        claims = [_whole(10, 100), _whole(20, 200)]
        assigned, conflicts, _ = S4.resolve_street_claims(claims, rows)
        assert assigned == {}
        assert conflicts == {10: {200}, 20: {100}}

    def test_disjoint_intervals_no_conflict(self):
        rows = [(1, 3, ""), (2, 12, "")]
        claims = [_interval(10, 100, 1, 10, "all"), _interval(20, 200, 11, 20, "all")]
        assigned, conflicts, _ = S4.resolve_street_claims(claims, rows)
        assert _winners(assigned) == {1: 100, 2: 200}
        assert conflicts == {}


# ── Parity ───────────────────────────────────────────────────────────────────
class TestParity:
    def test_odd_even_split_no_collision(self):
        rows = [(i, i, "") for i in range(17, 24)]  # 17..23
        claims = [_interval(10, 100, 17, 23, "odd"), _interval(20, 200, 17, 23, "even")]
        assigned, conflicts, unconfirmed = S4.resolve_street_claims(claims, rows)
        odd = sorted(aid for aid, w in assigned.items() if w["station_id"] == 100)
        even = sorted(aid for aid, w in assigned.items() if w["station_id"] == 200)
        assert odd == [17, 19, 21, 23] and even == [18, 20, 22]
        assert conflicts == {} and unconfirmed == set()

    def test_parity_unconfirmed_when_complement_uncovered(self):
        # Odd claim 17-19; evens (18) exist but no station covers them -> unconfirmed.
        rows = [(1, 17, ""), (2, 18, ""), (3, 19, "")]
        claims = [_interval(10, 100, 17, 19, "odd")]
        _, _, unconfirmed = S4.resolve_street_claims(claims, rows)
        assert unconfirmed == {10}

    def test_parity_moot_when_no_complement_houses(self):
        # All houses are odd; the even side is empty -> the split is moot, not flagged.
        rows = [(1, 17, ""), (2, 19, "")]
        claims = [_interval(10, 100, 17, 19, "odd")]
        _, _, unconfirmed = S4.resolve_street_claims(claims, rows)
        assert unconfirmed == set()

    def test_all_parity_matches_both_sides(self):
        rows = [(1, 4, ""), (2, 5, "")]
        claims = [_interval(10, 100, 1, 10, "all")]
        assigned, _, _ = S4.resolve_street_claims(claims, rows)
        assert _winners(assigned) == {1: 100, 2: 100}


# ── Suffix-bounded ranges ────────────────────────────────────────────────────
class TestSuffixBounds:
    def test_bounds_ok_upper_suffix(self):
        # "1-23ц": at 23, suffix must be <= Ц. Д < Ц in azbuka -> included; Ш > Ц -> excluded.
        c = {"lo": 1, "hi": 23, "losfx": "", "hisfx": "Ц"}
        assert S4._bounds_ok(23, "Д", c) is True
        assert S4._bounds_ok(23, "Ш", c) is False
        assert S4._bounds_ok(23, "", c) is True    # bare 23 included

    def test_bounds_ok_lower_suffix(self):
        # "12б-16": at 12, suffix must be >= Б. Bare 12 and 12а excluded; 12в included.
        c = {"lo": 12, "hi": 16, "losfx": "Б", "hisfx": ""}
        assert S4._bounds_ok(12, "", c) is False
        assert S4._bounds_ok(12, "А", c) is False
        assert S4._bounds_ok(12, "В", c) is True

    def test_bounds_ok_interior_number_unbounded(self):
        # Suffix bounds only apply at the edges; interior numbers accept any suffix.
        c = {"lo": 1, "hi": 23, "losfx": "А", "hisfx": "Ц"}
        assert S4._bounds_ok(10, "Ш", c) is True

    def test_suffix_bound_applied_in_full_resolution(self):
        rows = [(1, 23, ""), (2, 23, "Д"), (3, 23, "Ш")]
        claims = [_interval(10, 100, 1, 23, "odd", hisfx="Ц")]
        assigned, _, _ = S4.resolve_street_claims(claims, rows)
        # 23 and 23Д included (<= Ц); 23Ш excluded.
        assert _winners(assigned) == {1: 100, 2: 100}


# ── bez_broja / NULL houses ──────────────────────────────────────────────────
class TestBezBroja:
    def test_bb_claims_only_null_houses(self):
        rows = [(1, 10, ""), (2, None, "")]
        claims = [_bb(10, 100)]
        assigned, _, _ = S4.resolve_street_claims(claims, rows)
        assert _winners(assigned) == {2: 100}   # numbered house 10 NOT claimed by bb

    def test_whole_covers_null_house(self):
        rows = [(1, None, "")]
        claims = [_whole(10, 100)]
        assigned, _, _ = S4.resolve_street_claims(claims, rows)
        assert _winners(assigned) == {1: 100}

    def test_bb_outranks_whole_on_null_house(self):
        # On a NULL house, explicit бб (spec 1) beats a generic whole (spec 0).
        rows = [(1, None, "")]
        claims = [_whole(10, 100), _bb(20, 200)]
        assigned, _, _ = S4.resolve_street_claims(claims, rows)
        assert _winners(assigned) == {1: 200}


class TestParityHelpers:
    def test_parity_ok(self):
        assert S4._parity_ok(17, "odd") is True
        assert S4._parity_ok(18, "odd") is False
        assert S4._parity_ok(18, "even") is True
        assert S4._parity_ok(7, "all") is True

    def test_iv_parity_reads_third_element(self):
        assert S4._iv_parity([1, 23, "odd"]) == "odd"
        # Missing third element -> recomputed from bounds.
        assert S4._iv_parity([22, 30]) == "even"
