"""Tests for the bm.itblokada.org station source — see docs/parsing-matching/13-bm-source.md.

Covers the BM coverage dialect (`parse_bm`), the station-id scheme and record mapping
(`stage02_fetch_bm`), the house-number evidence pick and the no-street assignment (stage04).
"""

import stage02_fetch_bm as S2
import stage04_match_addresses as S4
from common.coverage_parse import is_bm_text, parse_bm, parse_coverage


# ── BM dialect ───────────────────────────────────────────────────────────────
class TestBmDialect:
    def test_detects_bm_text(self):
        assert is_bm_text("Улица 8.МАРТА бројеви 1, 2, ; Улица БЕЧЕЈСКИ ПУТ бројеви 1;")
        assert not is_bm_text("Насеље: АДА Улица: ЛЕЊИНОВА бројеви 1-9")
        assert not is_bm_text("Лењинова 1-9, Маршала Тита")

    def test_explicit_numbers_and_letter_suffixes(self):
        (seg,) = parse_bm("Улица БЕЧЕЈСКИ ПУТ бројеви 1, 1-А, 1-Б, 3, 4, ;")
        assert (seg.kind, seg.street_raw, seg.dialect) == ("street_numbers", "БЕЧЕЈСКИ ПУТ", "bm")
        assert seg.singles == [[1, ""], [1, "А"], [1, "Б"], [3, ""], [4, ""]]
        assert seg.intervals == [] and not seg.whole

    def test_number_dash_number_is_one_house_not_a_range(self):
        # "23-2" / "6/2" = house 23 sub-number 2; the register has letter suffixes only, so the
        # numeric sub-part is dropped. The RIK dialects would read "23-2" as a range.
        (seg,) = parse_bm("Улица ЛОЛЕ РИБАРА бројеви 23-2, 6/2, 7-А/3, 148-1;")
        assert seg.singles == [[23, ""], [6, ""], [7, "А"], [148, ""]]
        assert seg.intervals == []

    def test_duplicates_collapse(self):
        (seg,) = parse_bm("Улица НОВА бројеви 5, 5/1, 5/2, 5-А;")
        assert seg.singles == [[5, ""], [5, "А"]]

    def test_zero_is_dropped_and_lone_zero_is_whole_street(self):
        a, b = parse_bm("Улица ЖЕЛЕЗНИЧКА бројеви 0, 1, 2; Улица ЗАСЕЛАК ЛИВАДА бројеви 0;")
        assert a.singles == [[1, ""], [2, ""]] and not a.whole
        assert (b.kind, b.whole, b.singles) == ("whole_street", True, [])

    def test_no_street_clause(self):
        a, b = parse_bm("Улица НЕМА УЛИЦЕ бројеви 0, ; Улица НЕМА УЛИЦЕ бројеви 5, 12;")
        assert (a.kind, a.whole) == ("no_street", True)
        assert (b.kind, b.singles) == ("no_street", [[5, ""], [12, ""]])

    def test_ordinal_glued_to_name_is_split(self):
        (seg,) = parse_bm("Улица 8.МАРТА бројеви 1;")
        assert seg.street_raw == "8. МАРТА"

    def test_unparseable_clause_is_kept_for_review(self):
        (seg,) = parse_bm("нешто без структуре;")
        assert seg.kind == "unknown" and seg.unknown_tokens

    def test_parse_coverage_routes_only_when_bm_source(self):
        text = "Улица ЛОЛЕ РИБАРА бројеви 23-2;"
        assert parse_coverage(text, bm=True)[0].dialect == "bm"
        legacy = parse_coverage(text)[0]
        assert legacy.dialect == "structured" and legacy.intervals  # RIK reading: a range


# ── Station records ──────────────────────────────────────────────────────────
def _rec(**kw):
    base = {
        "id": "01M0AQSK4A3EXN1Y4ZGA6MX9P4", "register_id": "4246", "number": 1,
        "name_cyr": '"Прецизни Лив" - Ада, Молски пут 4', "name_lat": "x",
        "venue_cyr": '"Прецизни Лив"', "venue_lat": '"Precizni Liv"',
        "address_cyr": "Ада, Молски пут 4", "address_lat": "Ada, Molski put 4",
        "locality_id": "1", "tag": None, "municipality": {"id": "80012"},
        "settlement": {"id": "800015", "name_cyr": "АДА"},
        "geo": {"lat": 45.78524, "lon": 20.1355}, "voter_count": 715,
        "covered_addresses": "Улица 8.МАРТА бројеви 1, 2;",
    }
    base.update(kw)
    return base


class TestStationRows:
    def test_id_is_municipality_plus_printed_number(self):
        assert S2.station_id("80012", 1) == 8001200001
        assert S2.station_id("70254", 113) == 7025400113

    def test_maps_fields(self):
        rows, skipped = S2.station_rows([_rec()], {"80012"}, "bm:test.json")
        assert not skipped
        (r,) = rows
        assert r["id"] == 8001200001 and r["municipality_id"] == "80012" and r["number"] == 1
        assert r["name_cyr"] == '"Прецизни Лив"' and r["address_cyr"] == "Ада, Молски пут 4"
        assert r["raw_coverage_text"] == "Улица 8.МАРТА бројеви 1, 2;"
        assert (r["bm_id"], r["register_id"], r["voter_count"]) == ("01M0AQSK4A3EXN1Y4ZGA6MX9P4", "4246", 715)
        assert (r["lat"], r["lon"], r["bm_settlement_id"]) == (45.78524, 20.1355, "800015")
        assert r["is_amendment"] == 0 and r["source_file"] == "bm:test.json"

    def test_skips_special_groups_and_unknown_municipalities(self):
        recs = [
            _rec(tag="diaspora", municipality=None, covered_addresses=None),
            _rec(tag="prison", covered_addresses=None),
            _rec(municipality={"id": "90018"}),
            _rec(number=2),
        ]
        rows, skipped = S2.station_rows(recs, {"80012"}, "bm:test.json")
        assert [r["number"] for r in rows] == [2]
        assert [s["reason"] for s in skipped] == [
            "tag:diaspora", "tag:prison", "municipality_not_in_register"]

    def test_missing_address_is_empty_not_null(self):
        # polling_stations.address_* are NOT NULL in D1.
        (r,), _ = S2.station_rows([_rec(address_cyr=None, address_lat=None)], {"80012"}, "s")
        assert r["address_cyr"] == "" and r["address_lat"] == ""


# ── House-number evidence (stage04 pass 1.6) ─────────────────────────────────
def _nums(table):
    return lambda sid: table.get(sid, set())


class TestNumberEvidence:
    HOUSES = {"town": {1, 2, 3, 4, 5, 6, 7, 8}, "village": {40, 42, 44, 46}, "other": {1, 3}}

    def test_keeps_a_good_lexical_match(self):
        assert S4.number_evidence_pick({1, 2, 3}, "town", "exact", ["town", "village"],
                                       _nums(self.HOUSES)) is None

    def test_reresolves_a_match_with_no_listed_house(self):
        # Home-settlement street has none of the listed numbers; the same-named village street has all.
        assert S4.number_evidence_pick({40, 42, 44}, "town", "exact", ["town", "village"],
                                       _nums(self.HOUSES)) == "village"

    def test_resolves_ambiguous_and_confirms_muni_fallback(self):
        assert S4.number_evidence_pick({40, 46}, None, "ambiguous", ["town", "village"],
                                       _nums(self.HOUSES)) == "village"
        assert S4.number_evidence_pick({40, 46}, "village", "muni_fallback", ["village"],
                                       _nums(self.HOUSES)) == "village"

    def test_similar_candidates_stay_unresolved(self):
        # Both streets carry the listed numbers — numbers cannot tell them apart.
        assert S4.number_evidence_pick({1, 3}, None, "ambiguous", ["town", "other"],
                                       _nums(self.HOUSES)) is None

    def test_needs_half_of_the_list(self):
        assert S4.number_evidence_pick({40, 90, 91, 92, 93}, None, "none", ["village"],
                                       _nums(self.HOUSES)) is None

    def test_single_listed_house_is_enough_when_unique(self):
        assert S4.number_evidence_pick({44}, None, "ambiguous", ["town", "village"],
                                       _nums(self.HOUSES)) == "village"

    def test_no_candidates(self):
        assert S4.number_evidence_pick({1}, None, "none", [], _nums(self.HOUSES)) is None


class TestHomeSettlementPreference:
    """Pančevo #20 (Качарево): the text says "4. ОКТОБАР", Качарево's register street is
    "4. ОКТОБРА", and Банатски Брестовац has a street spelled exactly "4. ОКТОБАР". Low house
    numbers exist on both, so the exact-name street of the OTHER village looked confirmed."""
    HOUSES = {"brestovac": set(range(1, 32)), "kacarevo": set(range(1, 45))}
    LISTED = set(range(1, 45))

    def test_home_near_spelling_beats_exact_name_elsewhere(self):
        pick = S4.number_evidence_pick(self.LISTED, "brestovac", "muni_fallback",
                                       ["brestovac", "kacarevo"], _nums(self.HOUSES), home={"kacarevo"})
        assert pick == "kacarevo"

    def test_without_home_candidate_the_margin_rule_still_blocks(self):
        # Same numbers, no home information: 31 vs 44 hits is not a 2x margin -> undecided.
        assert S4.number_evidence_pick(self.LISTED, "brestovac", "muni_fallback",
                                       ["brestovac", "kacarevo"], _nums(self.HOUSES)) is None

    def test_elsewhere_needs_2x_over_the_home_candidate(self):
        houses = {"home": {1, 2, 3}, "away": {1, 2, 3, 4, 5}}
        assert S4.number_evidence_pick({1, 2, 3, 4, 5}, None, "ambiguous", ["home", "away"],
                                       _nums(houses), home={"home"}) is None
        houses = {"home": {1}, "away": {1, 2, 3, 4, 5, 6}}
        assert S4.number_evidence_pick({1, 2, 3, 4, 5, 6}, None, "ambiguous", ["home", "away"],
                                       _nums(houses), home={"home"}) == "away"

    def test_home_candidate_must_still_clear_the_thresholds(self):
        houses = {"home": {1}, "away": set()}
        assert S4.number_evidence_pick({1, 50, 51, 52}, "away", "muni_fallback", ["home", "away"],
                                       _nums(houses), home={"home"}) is None

    def test_near_name_streets_digit_guard_and_similarity(self):
        scope = {"4 ОКТОБРА": ["a"], "6 ОКТОБРА": ["b"], "4 ОКТОБАР": ["c"], "ЦАРА ДУШАНА": ["d"]}
        assert S4.near_name_streets("4 ОКТОБАР", scope) == ["a"]   # not itself, not "6 …", not unrelated

    def test_same_word_forms(self):
        assert S4.same_word_forms("4 ОКТОБАР", "4 ОКТОБРА")
        assert S4.same_word_forms("4 ЈУЛИ", "4 ЈУЛА")
        assert S4.same_word_forms("КОПАОНИЧКИ ОДРЕД", "КОПАОНИЧКОГ ОДРЕДА")
        assert not S4.same_word_forms("МИЛОША ОБРЕНОВИЋА", "МИЛАНА ОБРЕНОВИЋА")
        assert not S4.same_word_forms("ИЛИЈЕ АЛЕКСИЋА", "МИЈЕ АЛЕКСИЋА")
        assert not S4.same_word_forms("ВИНОГРАДСКА", "ВИНОГРАДАРСКА")
        assert not S4.same_word_forms("НОВА", "НОВА 2")


# ── No-street assignment (stage04 no-street pass) ────────────────────────────
class TestAssignNoStreet:
    def test_sole_claimant_takes_every_free_house(self):
        out = S4.assign_no_street({"V": {10: 1000}}, {"V": [(1, 0.0, 0.0), (2, 5.0, 5.0)]}, {})
        assert sorted(out) == [(10, 1, 1000), (10, 2, 1000)]

    def test_shared_settlement_splits_by_nearest_station(self):
        out = S4.assign_no_street(
            {"V": {10: 1000, 20: 2000}},
            {"V": [(1, 1.0, 0.0), (2, 99.0, 0.0), (3, 40.0, 0.0)]},
            {10: (0.0, 0.0), 20: (100.0, 0.0)})
        assert sorted(out) == [(10, 1, 1000), (10, 3, 1000), (20, 2, 2000)]

    def test_unlocated_co_claimants_take_nothing(self):
        assert S4.assign_no_street({"V": {10: 1000, 20: 2000}}, {"V": [(1, 0.0, 0.0)]}, {}) == []

    def test_settlement_without_free_houses(self):
        assert S4.assign_no_street({"V": {10: 1000}}, {}, {10: (0.0, 0.0)}) == []


# ── Register consistency (stage01) ───────────────────────────────────────────
class TestHouseOnlyStreets:
    def test_adds_streets_missing_from_the_street_register(self):
        import polars as pl
        import stage01_load_register as S1
        streets = pl.DataFrame({"id": ["s1"], "settlement_id": ["n1"], "name_cyr": ["ГЛАВНА"],
                                "name_lat": ["GLAVNA"], "name_norm": ["ГЛАВНА"]})
        addr = pl.DataFrame({
            "street_id": ["s1", "s2", "s2"], "settlement_id": ["n1", "n2", "n2"],
            "ulica_ime": ["ГЛАВНА", "Нова улица", "Нова улица"],
            "ulica_ime_lat": ["GLAVNA", "Nova ulica", "Nova ulica"],
        })
        out = S1.add_house_only_streets(streets, addr)
        assert out["id"].to_list() == ["s1", "s2"]
        added = out.row(1, named=True)
        assert added["settlement_id"] == "n2" and added["name_cyr"] == "Нова улица"
        assert added["name_norm"]  # normalized like every other street

    def test_noop_when_register_is_consistent(self):
        import polars as pl
        import stage01_load_register as S1
        streets = pl.DataFrame({"id": ["s1"], "settlement_id": ["n1"], "name_cyr": ["ГЛАВНА"],
                                "name_lat": ["GLAVNA"], "name_norm": ["ГЛАВНА"]})
        addr = pl.DataFrame({"street_id": ["s1"], "settlement_id": ["n1"],
                             "ulica_ime": ["ГЛАВНА"], "ulica_ime_lat": ["GLAVNA"]})
        assert S1.add_house_only_streets(streets, addr).height == 1
