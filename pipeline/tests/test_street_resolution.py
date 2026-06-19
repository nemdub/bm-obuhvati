"""Tests for stage04 `resolve_street` — see docs/parsing-matching/05-street-resolution.md.

`resolve_street(street_raw, muni, settlement_id, idx)` walks the resolution ladder and
returns `(street_id, method, score, ambiguous_ids)`. It reads only four slots of the index
tuple, so we build a small synthetic index by hand instead of loading the register parquet:

  idx = (street_meta, by_muni_norm, by_sett_norm, addr_by_street,
         settlements_by_muni, station_muni, station_settlement, sett_to_streets)

`resolve_street` uses idx[1] (by_muni_norm), idx[2] (by_sett_norm), idx[4]
(settlements_by_muni) and idx[7] (sett_to_streets) by index, so an 8-tuple still works even
though `build_indexes` now returns a 9th slot (`station_settlement_inferred`, consumed by the
caller, not `resolve_street`). Whether the scope is an inferred town is passed to
`resolve_street` as the explicit `settlement_inferred` argument (§5.6).

NOTE: register-side alternate keys (declension / sortkey / strip-ulica) are added in
`build_indexes`, NOT here — so these tests exercise the DOC-side variant generation that
`resolve_street` performs itself. The register-side alternates are an integration concern.
"""

import pytest

import stage04_match_addresses as S4
from common.normalize import normalize_street as ns

MUNI = "M1"


def make_index(streets_by_settlement, settlement_names):
    """Build a synthetic 8-tuple index.

    streets_by_settlement: {settlement_id: {street_id: raw_name}}
    settlement_names:      {settlement_id: raw_settlement_name}
    """
    street_meta, by_muni, by_sett, sett_to_streets = {}, {}, {}, {}
    for set_id, names in streets_by_settlement.items():
        for sid, name in names.items():
            norm = ns(name)
            street_meta[sid] = {"settlement_id": set_id, "municipality_id": MUNI, "name_norm": norm}
            by_muni.setdefault(MUNI, {}).setdefault(norm, []).append(sid)
            by_sett.setdefault(set_id, {}).setdefault(norm, []).append(sid)
            sett_to_streets.setdefault(set_id, []).append(sid)
    settlements_by_muni = {MUNI: [(sid, ns(nm)) for sid, nm in settlement_names.items()]}
    return (street_meta, by_muni, by_sett, {}, settlements_by_muni, {}, {}, sett_to_streets)


@pytest.fixture
def idx():
    # One home settlement S1 with several streets; S2 holds a street that S1 lacks.
    return make_index(
        {
            "S1": {
                "st1": "Никола Тесла",
                "st2": "Војни Пут 1",
                "st3": "Војни Пут 2",
                "st4": "Виноградарска",
                "st5": "Угриновачки пут 1 део",
            },
            "S2": {"st6": "Дунавска"},
        },
        {"S1": "Прво Село", "S2": "Друго Село"},
    )


class TestExactAndDeclension:
    def test_exact_settlement_match(self, idx):
        assert S4.resolve_street("Никола Тесла", MUNI, "S1", idx)[:2] == ("st1", "exact")

    def test_declension_doc_side(self, idx):
        # Genitive doc form resolves to the nominative register name — deterministic, exact.
        assert S4.resolve_street("Николе Тесле", MUNI, "S1", idx)[:2] == ("st1", "exact")

    def test_parenthetical_tried_as_exact_alt(self, idx):
        # The parenthetical is stripped for the primary key and tried only as an exact alt.
        assert S4.resolve_street("Корзо (Никола Тесла)", MUNI, "S1", idx)[:2] == ("st1", "exact")


class TestStripUlica:
    def test_doc_side_strip_ulica(self):
        idx = make_index({"S1": {"st1": "Поручничка"}}, {"S1": "Прво Село"})
        assert S4.resolve_street("Поручничка улица", MUNI, "S1", idx)[:2] == ("st1", "exact")


class TestParts:
    def test_base_parts_claims_all_parts(self, idx):
        sid, method, score, amb = S4.resolve_street("Војни Пут", MUNI, "S1", idx)
        assert method == "base_parts"
        assert sid == "st2" and amb == ["st3"]   # both numbered parts claimed

    def test_one_deo_maps_to_base(self):
        # "... 1 део" maps to the plain base name (register part 1 is the base).
        idx = make_index({"S1": {"st1": "Угриновачки пут"}}, {"S1": "Прво Село"})
        assert S4.resolve_street("Угриновачки пут 1 део", MUNI, "S1", idx)[:2] == ("st1", "exact")


class TestFuzzy:
    def test_typo_within_settlement(self, idx):
        # "Виноградска" -> register "Виноградарска" (typo, same settlement).
        assert S4.resolve_street("Виноградска", MUNI, "S1", idx)[:2] == ("st4", "fuzzy")

    def test_digit_guard_rejects_different_number(self):
        # "7 Војвођанске" must NOT fuzzy-match "8 Војвођанске" (different street).
        idx = make_index({"S1": {"st1": "8 Војвођанске"}}, {"S1": "Прво Село"})
        assert S4.resolve_street("7 Војвођанске", MUNI, "S1", idx)[:2] == (None, "none")


class TestMuniScope:
    def test_muni_fallback_single_other_settlement(self, idx):
        # Home settlement S1 lacks "Дунавска"; exactly one other settlement (S2) has it.
        assert S4.resolve_street("Дунавска", MUNI, "S1", idx)[:2] == ("st6", "muni_fallback")

    def test_no_home_settlement_is_plain_exact(self, idx):
        # A station with no resolvable home settlement gets plain 'exact' from muni scope.
        assert S4.resolve_street("Дунавска", MUNI, None, idx)[:2] == ("st6", "exact")

    def test_ambiguous_multiple_settlements(self):
        # Same street name in two settlements, neither the home settlement -> ambiguous.
        idx = make_index(
            {"S1": {"st1": "Прва"}, "S2": {"st2": "Дунавска"}, "S3": {"st3": "Дунавска"}},
            {"S1": "A Село", "S2": "B Село", "S3": "C Село"},
        )
        sid, method, _, amb = S4.resolve_street("Дунавска", MUNI, "S1", idx)
        assert (sid, method) == (None, "ambiguous")
        assert sorted(amb) == ["st2", "st3"]


class TestMuniFuzzy:
    """Strict muni-wide fuzzy (`_fuzzy_muni_unique`), §5.6 — for stations with no home
    settlement, where the settlement-scoped fuzzy (step 8) never runs."""

    def test_no_settlement_muni_fuzzy_unique(self):
        # No home settlement: a one-letter doc typo unique in the muni resolves via the strict
        # muni-wide fuzzy. "Михаила" -> "Михајла" Пупина (WRatio 95.5 >= 93).
        idx = make_index({"S1": {"st1": "Булевар Михајла Пупина"}}, {"S1": "Град"})
        assert S4.resolve_street("Булевар Михаила Пупина", MUNI, None, idx)[:2] == ("st1", "fuzzy")

    def test_muni_fuzzy_non_unique_target_rejected(self):
        # The fuzzy target name exists in TWO settlements (maps to 2 streets) -> not unique,
        # so the uniqueness guard rejects it (picking one would be a coin flip).
        idx = make_index(
            {"S1": {"st1": "Булевар Михајла Пупина"}, "S2": {"st2": "Булевар Михајла Пупина"}},
            {"S1": "Град", "S2": "Село"},
        )
        assert S4.resolve_street("Булевар Михаила Пупина", MUNI, None, idx)[:2] == (None, "none")


class TestInferredTownScope:
    """Home-settlement town inference (§5.1): a no-settlement town station gets the eponymous
    town settlement as scope, but — like a no-settlement station — still gets the muni-wide
    fuzzy last resort for a street the register files under a neighbouring settlement."""

    def test_inferred_town_allows_muni_fuzzy(self):
        # Town scope was INFERRED: a typo'd street absent from the town but uniquely
        # fuzzy-matchable elsewhere in the muni still resolves ("Кикиндски"->"Кикински", 96).
        idx = make_index(
            {"TOWN": {"st1": "Прва"}, "S2": {"st2": "Кикински пут"}},
            {"TOWN": "Град", "S2": "Село"},
        )
        assert S4.resolve_street(
            "Кикиндски пут", MUNI, "TOWN", idx, settlement_inferred=True)[:2] == ("st2", "fuzzy")

    def test_address_resolved_settlement_does_not_muni_fuzzy(self):
        # Same setup but the settlement came from the address (not inferred): the muni-wide
        # fuzzy last resort does NOT run, so the typo stays unresolved.
        idx = make_index(
            {"S1": {"st1": "Прва"}, "S2": {"st2": "Кикински пут"}},
            {"S1": "Прво Село", "S2": "Село"},
        )
        assert S4.resolve_street(
            "Кикиндски пут", MUNI, "S1", idx, settlement_inferred=False)[:2] == (None, "none")


class TestSettlementClaim:
    def test_village_name_claims_all_streets(self):
        idx = make_index(
            {"S1": {"st1": "Прва"}, "S2": {"st2": "Друга", "st3": "Трећа"}},
            {"S1": "Прво Село", "S2": "Бело Село"},
        )
        # "Бело Село" matches no street anywhere -> last-resort settlement claim of all S2 streets.
        sid, method, score, amb = S4.resolve_street("Бело Село", MUNI, "S1", idx)
        assert method == "settlement"
        assert sid == "st2" and amb == ["st3"]

    def test_naseljeno_mesto_prefix_claims_settlement(self):
        # Vladimirci writes every station as "насељено место <village>". Strip the marker and
        # claim the settlement by name (all its streets), even though no street matches.
        idx = make_index(
            {"S1": {"st1": "Прва"}, "S2": {"st2": "Друга", "st3": "Трећа"}},
            {"S1": "Прво Село", "S2": "Белотић"},
        )
        sid, method, score, amb = S4.resolve_street("насељено место Белотић", MUNI, "S1", idx)
        assert method == "settlement"
        assert sid == "st2" and amb == ["st3"]

    def test_naselje_prefix_still_claims_settlement(self):
        # The pre-existing bare "насеље <village>" marker keeps working.
        idx = make_index(
            {"S1": {"st1": "Прва"}, "S2": {"st2": "Друга"}},
            {"S1": "Прво Село", "S2": "Белосавци"},
        )
        assert S4.resolve_street("насеље Белосавци", MUNI, "S1", idx)[:2] == ("st2", "settlement")


class TestLocality:
    """Sub-locality / hamlet (заселак) claim (§5.x): the register encodes a locality with no
    own naselje as a PREFIX on several streets of the parent settlement ('Ранчево' in Sombor →
    'РАНЧЕВО ХИЛАНДАРСКА', 'ЗАСЕЛАК РАНЧЕВО РЕЛИЋИ', …). A single-word coverage claims them all."""

    def test_locality_claims_all_prefixed_streets(self):
        idx = make_index(
            {"S1": {
                "a": "Ранчево Хиландарска",
                "b": "Ранчево Вука Караџића",
                "c": "Заселак Ранчево Релићи",
                "d": "Главна",  # unrelated street, must NOT be claimed
            }},
            {"S1": "Сомбор"},
        )
        sid, method, score, amb = S4.resolve_street("Ранчево", MUNI, "S1", idx)
        assert method == "locality"
        assert sid == "a" and sorted([sid] + amb) == ["a", "b", "c"]

    def test_single_prefixed_street_is_not_a_locality(self):
        # Only ONE 'Ранчево …' street -> not a cluster, so no locality claim.
        idx = make_index({"S1": {"a": "Ранчево Хиландарска", "b": "Главна"}}, {"S1": "Сомбор"})
        assert S4.resolve_street("Ранчево", MUNI, "S1", idx)[1] != "locality"

    def test_generic_prefix_word_is_not_a_locality(self):
        # 'Заселак' (hamlet) is a generic structural word, not a locality name -> no claim.
        idx = make_index(
            {"S1": {"a": "Заселак Криваја", "b": "Заселак Релићи", "c": "Главна"}},
            {"S1": "Сомбор"},
        )
        assert S4.resolve_street("Заселак", MUNI, "S1", idx)[1] != "locality"

    def test_numbered_parts_are_not_locality(self):
        # Numbered parts stay base_parts (numeric remainder excluded from locality).
        idx = make_index({"S1": {"a": "Војни Пут 1", "b": "Војни Пут 2"}}, {"S1": "Сомбор"})
        assert S4.resolve_street("Војни Пут", MUNI, "S1", idx)[1] == "base_parts"


class TestAlias:
    def test_alias_substitution(self, monkeypatch):
        idx = make_index({"S1": {"st1": "Хероја Пинкија"}}, {"S1": "Прво Село"})
        # Inject an alias (doc "Пинкијева" -> register "Хероја Пинкија") for this muni.
        monkeypatch.setitem(S4._ALIASES, (MUNI, ns("Пинкијева")), ns("Хероја Пинкија"))
        assert S4.resolve_street("Пинкијева", MUNI, "S1", idx)[:2] == ("st1", "alias")


class TestNoMatch:
    def test_unresolvable(self, idx):
        assert S4.resolve_street("Непостојећа Улица", MUNI, "S1", idx)[:2] == (None, "none")


class TestSettlementFromAddress:
    """`resolve_settlement_from_address` (§5.1): the home settlement may be the FIRST comma
    token (settlement-first, 'КЕЛЕБИЈА, ПУТ …') or the LAST (settlement-last, Beočin
    'Јована Грчића Миленка 5, Черевић'). First wins; last is the fallback."""

    SBM = {MUNI: [("S1", ns("Черевић")), ("S2", ns("Келебија"))]}

    def test_settlement_first(self):
        assert S4.resolve_settlement_from_address("Келебија, Пут 5", MUNI, self.SBM) == "S2"

    def test_settlement_last(self):
        assert S4.resolve_settlement_from_address(
            "Јована Грчића Миленка 5, Черевић", MUNI, self.SBM) == "S1"

    def test_no_settlement_token(self):
        # A pure town address (street only, no settlement) stays unresolved -> inferred town.
        assert S4.resolve_settlement_from_address("Трг Слободе бб", MUNI, self.SBM) is None


class TestStationAnchor:
    """`_station_anchor` (§5.7): centroid of a station's resolved-street centroids plus an
    adaptive radius clamped to [FLOOR, CAP]. Coords are UTM meters."""

    def test_no_resolved_streets_is_none(self):
        assert S4._station_anchor([]) is None

    def test_single_street_uses_floor_radius(self):
        cx, cy, radius = S4._station_anchor([(1000.0, 2000.0)])
        assert (cx, cy) == (1000.0, 2000.0)              # centroid is the point itself
        assert radius == S4.config.PROXIMITY_RADIUS_FLOOR_M  # extent 0 -> floor

    def test_radius_scales_with_extent(self):
        # centroid (0, 500); extent 500; radius = FACTOR(2) * 500 = 1000 (within [floor, cap]).
        cx, cy, radius = S4._station_anchor([(0.0, 0.0), (0.0, 1000.0)])
        assert (cx, cy) == (0.0, 500.0)
        assert radius == 1000.0

    def test_radius_clamped_to_cap(self):
        # extent 2000 -> 2*2000 = 4000, capped to CAP (3000).
        _, _, radius = S4._station_anchor([(0.0, 0.0), (0.0, 4000.0)])
        assert radius == S4.config.PROXIMITY_RADIUS_CAP_M


class TestNearestUnclaimed:
    """`_nearest_unclaimed` (§5.7): nearest candidate to the anchor among streets already
    filtered to unclaimed + within radius. Disambiguation mode (target None) takes the
    nearest; fuzzy mode keeps only names clearing the cutoff (with the digit guard)."""

    ANCHOR = (0.0, 0.0, 2000.0)

    def test_disambiguation_takes_nearest(self):
        # Two same-named real streets; the nearer one wins (no fuzzy needed).
        cands = [("far", ns("Дунавска"), 100.0, 0.0), ("near", ns("Дунавска"), 50.0, 0.0)]
        assert S4._nearest_unclaimed(self.ANCHOR, cands, None) == ("near", 90.0)

    def test_fuzzy_keeps_only_similar_names(self):
        # The dissimilar candidate is dropped; the matching one resolves even though farther.
        cands = [("other", ns("Зелена"), 10.0, 0.0), ("hit", ns("Виноградарска"), 500.0, 0.0)]
        res = S4._nearest_unclaimed(self.ANCHOR, cands, ns("Виноградарска"))
        assert res is not None and res[0] == "hit"

    def test_fuzzy_digit_guard_rejects_number_mismatch(self):
        # "7 ВОЈВОЂАНСКЕ" must not match "8 ВОЈВОЂАНСКЕ" even when nearby.
        cands = [("st", ns("8 Војвођанске"), 10.0, 0.0)]
        assert S4._nearest_unclaimed(self.ANCHOR, cands, ns("7 Војвођанске")) is None

    def test_empty_pool_returns_none(self):
        assert S4._nearest_unclaimed(self.ANCHOR, [], None) is None

    def test_equidistant_rivals_skipped(self):
        # Two different streets exactly the same distance away -> don't guess.
        cands = [("a", ns("Дунавска"), 100.0, 0.0), ("b", ns("Дунавска"), -100.0, 0.0)]
        assert S4._nearest_unclaimed(self.ANCHOR, cands, None) is None


class TestMarkerScopes:
    """build_marker_scopes: a bare settlement name scopes the streets that follow it."""

    @staticmethod
    def _seg(sid, raw, whole=True, intervals=None, singles=None):
        import json as _j
        p = {"whole": whole, "intervals": intervals or [], "singles": singles or [],
             "bez_broja": False, "unknown_tokens": []}
        return {"id": sid, "station_id": sid // 1000, "street_raw": raw,
                "parsed_json": _j.dumps(p, ensure_ascii=False)}

    def test_leading_settlement_scopes_following_streets(self):
        from stage04_match_addresses import build_marker_scopes
        station_muni = {7003300: "70033"}
        exact = {"70033": {"КОПЉАРЕ": "701521"}}
        segs = [self._seg(7003300000, "Копљаре"),
                self._seg(7003300001, "Карађорђева"),
                self._seg(7003300002, "Косовска")]
        scopes = build_marker_scopes(segs, station_muni, exact)
        # the marker itself is NOT scoped; the two streets after it are scoped to КОПЉАРЕ
        assert 7003300000 not in scopes
        assert scopes[7003300001] == "701521" and scopes[7003300002] == "701521"

    def test_non_settlement_first_segment_no_scope(self):
        from stage04_match_addresses import build_marker_scopes
        station_muni = {7003300: "70033"}
        exact = {"70033": {"КОПЉАРЕ": "701521"}}
        segs = [self._seg(7003300000, "Карађорђева"), self._seg(7003300001, "Косовска")]
        assert build_marker_scopes(segs, station_muni, exact) == {}

    def test_numbered_settlement_name_is_not_a_marker(self):
        # A segment carrying house numbers is a street, never a settlement marker.
        from stage04_match_addresses import build_marker_scopes
        station_muni = {7003300: "70033"}
        exact = {"70033": {"КОПЉАРЕ": "701521"}}
        segs = [self._seg(7003300000, "Копљаре", whole=False, singles=[[5, ""]]),
                self._seg(7003300001, "Карађорђева")]
        assert build_marker_scopes(segs, station_muni, exact) == {}
