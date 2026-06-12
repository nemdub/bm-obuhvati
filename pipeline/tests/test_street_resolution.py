"""Tests for stage04 `resolve_street` — see docs/parsing-matching/05-street-resolution.md.

`resolve_street(street_raw, muni, settlement_id, idx)` walks the resolution ladder and
returns `(street_id, method, score, ambiguous_ids)`. It reads only four slots of the index
tuple, so we build a small synthetic index by hand instead of loading the register parquet:

  idx = (street_meta, by_muni_norm, by_sett_norm, addr_by_street,
         settlements_by_muni, station_muni, station_settlement, sett_to_streets)

`resolve_street` uses idx[1] (by_muni_norm), idx[2] (by_sett_norm), idx[4]
(settlements_by_muni) and idx[7] (sett_to_streets); the rest can be empty placeholders.

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


class TestAlias:
    def test_alias_substitution(self, monkeypatch):
        idx = make_index({"S1": {"st1": "Хероја Пинкија"}}, {"S1": "Прво Село"})
        # Inject an alias (doc "Пинкијева" -> register "Хероја Пинкија") for this muni.
        monkeypatch.setitem(S4._ALIASES, (MUNI, ns("Пинкијева")), ns("Хероја Пинкија"))
        assert S4.resolve_street("Пинкијева", MUNI, "S1", idx)[:2] == ("st1", "alias")


class TestNoMatch:
    def test_unresolvable(self, idx):
        assert S4.resolve_street("Непостојећа Улица", MUNI, "S1", idx)[:2] == (None, "none")
