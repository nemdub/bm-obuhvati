"""Tests for common/coverage_parse.py — see docs/parsing-matching/02-coverage-parsing.md."""

import pytest

from common.coverage_parse import (
    OPEN_END,
    Segment,
    interval_parity,
    is_bb_token,
    is_block_token,
    is_broj_token,
    is_house_token,
    is_number_side,
    is_od_token,
    parse_compact,
    parse_coverage,
    parse_number_token,
    parse_structured,
)
from common.normalize import normalize_street


def _num(tok):
    """Classify a single number token in isolation and return the segment."""
    s = Segment("", "", "street_numbers")
    parse_number_token(tok, s)
    return s


# ── Token predicates ─────────────────────────────────────────────────────────
class TestTokenPredicates:
    @pytest.mark.parametrize("tok,expected", [
        ("бб", True), ("ББ", True), ("бб.", True), ("б.б.", True), ("bb", True),
        ("5", False), ("Прва", False),
    ])
    def test_is_bb_token(self, tok, expected):
        assert is_bb_token(tok) is expected

    @pytest.mark.parametrize("tok,expected", [
        ("А-21", True), ("Т-8", True), ("Е1-Е-7/I", True), ("С-1", True),
        ("5", False), ("5а", False), ("Прва", False),
    ])
    def test_is_block_token(self, tok, expected):
        assert is_block_token(tok) is expected

    @pytest.mark.parametrize("tok,expected", [
        ("20", True), ("20а", True), ("190Б", True),
        ("20.", False),   # trailing-period ordinal, NOT a house token
        ("8.", False),
        ("Прва", False),
    ])
    def test_is_house_token(self, tok, expected):
        assert is_house_token(tok) is expected

    def test_is_number_side_union(self):
        assert is_number_side("5") and is_number_side("А-21") and is_number_side("бб")
        assert not is_number_side("Прва")
        assert not is_number_side("20.")   # ordinal


# ── Number-token classification ──────────────────────────────────────────────
class TestParseNumberToken:
    def test_bb_sets_flag(self):
        s = _num("бб")
        assert s.bez_broja and not s.intervals and not s.singles

    @pytest.mark.parametrize("tok,iv", [
        ("17-23", [17, 23, "odd"]),
        ("22-30", [22, 30, "even"]),
        ("1-20", [1, 20, "all"]),
        ("1-23ц", [1, 23, "odd", "", "Ц"]),     # suffixed upper bound
        ("12а-16", [12, 16, "even", "А", ""]),  # suffixed lower bound
        ("2-20-А", [2, 20, "even", "", "А"]),
        ("14-16/1", [14, 16, "even", "", "1"]),
    ])
    def test_ranges(self, tok, iv):
        assert _num(tok).intervals == [iv]

    def test_12_dash_A_is_single_not_range(self):
        # Upper bound must contain digits; "12-А" is single 12 suffix А.
        s = _num("12-А")
        assert s.intervals == [] and s.singles == [[12, "А"]]

    def test_single_with_suffix(self):
        assert _num("190Б").singles == [[190, "Б"]]

    def test_unparseable_goes_to_unknown(self):
        # A block tag routed through the number classifier lands in unknown_tokens.
        assert _num("А-21").unknown_tokens == ["А-21"]


class TestIntervalParity:
    @pytest.mark.parametrize("lo,hi,parity", [
        (17, 23, "odd"), (22, 30, "even"), (1, 20, "all"), (2, 2, "even"), (3, 3, "odd"),
    ])
    def test_parity(self, lo, hi, parity):
        assert interval_parity(lo, hi) == parity


# ── Compact dialect ──────────────────────────────────────────────────────────
def _segtuples(segs):
    return [(s.street_raw, s.whole, s.bez_broja, s.intervals, s.singles,
             s.unknown_tokens, s.kind) for s in segs]


class TestCompactBasics:
    def test_whole_street_default(self):
        segs = parse_compact("Антонија Хаџића")
        assert _segtuples(segs) == [("Антонија Хаџића", True, False, [], [], [], "whole_street")]

    def test_street_with_numbers(self):
        segs = parse_compact("Прва 1-10")
        s = segs[0]
        assert s.street_raw == "Прва" and s.whole is False and s.intervals == [[1, 10, "all"]]

    def test_multiple_streets_split_on_comma(self):
        segs = parse_compact("Прва 1-10, Друга, Трећа 2-8")
        assert [s.street_raw for s in segs] == ["Прва", "Друга", "Трећа"]


class TestCompactContinuation:
    def test_leading_numbers_continue_previous_street(self):
        # "2-22А" continues "Цара Лазара"; "Његошева" is a new whole street.
        segs = parse_compact("Цара Лазара 1-23, 2-22А, Његошева")
        assert _segtuples(segs) == [
            ("Цара Лазара", False, False, [[1, 23, "odd"], [2, 22, "even", "", "А"]], [], [], "street_numbers"),
            ("Његошева", True, False, [], [], [], "whole_street"),
        ]


class TestBezBroja:
    def test_bb_only(self):
        segs = parse_compact("Омладинских бригада бб")
        s = segs[0]
        assert s.street_raw == "Омладинских бригада"
        assert s.bez_broja is True and s.whole is False and not s.intervals

    def test_bb_additive_with_ranges(self):
        segs = parse_compact("Бул. Михаила Пупина 2-6, 3-13 и бб")
        s = segs[0]
        assert s.street_raw == "Бул. Михаила Пупина"
        assert s.intervals == [[2, 6, "even"], [3, 13, "odd"]]
        assert s.bez_broja is True


class TestBlockTokens:
    def test_block_tag_kept_as_unknown_not_glued(self):
        segs = parse_compact("Цара Лазара А-21-А-24")
        s = segs[0]
        assert s.street_raw == "Цара Лазара"
        assert s.unknown_tokens == ["А-21-А-24"]
        assert s.whole is False

    def test_blok_number_is_part_of_name(self):
        # "Блок 112 С-1, Блок 112 С-2": number after Блок is the name; С-N are unknown;
        # repeated same-named fragments merge into one named_block segment.
        segs = parse_compact("Блок 112 С-1, Блок 112 С-2")
        assert len(segs) == 1
        s = segs[0]
        assert s.street_raw == "Блок 112" and s.kind == "named_block"
        assert s.unknown_tokens == ["С-1", "С-2"]


class TestDeo:
    def test_n_deo_kept_in_name_whole(self):
        segs = parse_compact("Угриновачки пут 1 део")
        assert _segtuples(segs) == [("Угриновачки пут 1 део", True, False, [], [], [], "whole_street")]

    def test_n_deo_with_house_number(self):
        segs = parse_compact("Угриновачки пут 1 део 13")
        s = segs[0]
        assert s.street_raw == "Угриновачки пут 1 део" and s.singles == [[13, ""]]

    def test_glued_deo_split_by_parse_coverage(self):
        # "део13" is split by _DEO_GLUE in parse_coverage before tokenizing.
        segs = parse_coverage("Угриновачки пут 1 део13")
        s = segs[0]
        assert s.street_raw == "Угриновачки пут 1 део" and s.singles == [[13, ""]]


class TestCompoundINames:
    def test_compound_name_kept_whole_when_register_has_it(self):
        reg = {"ЗРИЊСКОГ И ФРАНКОПАНА"}
        segs = parse_compact("Зрињског и Франкопана", is_street=reg.__contains__)
        assert [s.street_raw for s in segs] == ["Зрињског и Франкопана"]

    def test_compound_name_split_without_predicate(self):
        segs = parse_compact("Зрињског и Франкопана")
        assert [s.street_raw for s in segs] == ["Зрињског", "Франкопана"]

    def test_genuine_list_stays_split(self):
        # Not a register street -> the "и" is a genuine list connector, two streets.
        reg = {"ЗРИЊСКОГ И ФРАНКОПАНА"}
        segs = parse_compact("Антонија Хаџића и Целовечка", is_street=reg.__contains__)
        assert [s.street_raw for s in segs] == ["Антонија Хаџића", "Целовечка"]

    def test_compound_name_with_paren_and_numbers(self):
        # The merge predicate's name scan stops at "(" / first number, so the compound name
        # is kept whole (not split on "и") even in the old-name-restatement form. The
        # parenthetical itself stays in street_raw here — stripping is stage04's job — but
        # crucially there is ONE segment, not two phantom streets.
        reg = {normalize_street("Трг Јакаба и Комора")}
        segs = parse_compact("Трг Јакаба и Комора (Трг октобарске револуције) 28-30",
                             is_street=reg.__contains__)
        assert len(segs) == 1
        assert segs[0].street_raw == "Трг Јакаба и Комора (Трг октобарске револуције)"
        assert segs[0].intervals == [[28, 30, "even"]]

    def test_merge_does_not_cross_commas(self):
        # The connector merge runs within a comma fragment only.
        reg = {"А И Б"}
        segs = parse_compact("А, и Б", is_street=reg.__contains__)
        assert [s.street_raw for s in segs] == ["А", "Б"]


class TestFragmentMerge:
    def test_repeated_street_merges_to_one_card(self):
        segs = parse_compact("Прва 1, Прва 3, Прва 5")
        assert len(segs) == 1
        assert segs[0].singles == [[1, ""], [3, ""], [5, ""]]


# ── Structured dialect ───────────────────────────────────────────────────────
class TestStructured:
    def test_naselje_ulica_brojevi(self):
        segs = parse_structured("Насеље: Ада Улица: 8. Март бројеви 1, 2, 3")
        s = segs[0]
        assert s.settlement_raw == "Ада" and s.street_raw == "8. Март"
        assert s.singles == [[1, ""], [2, ""], [3, ""]] and s.dialect == "structured"

    def test_no_brojevi_is_whole(self):
        segs = parse_structured("Улица: Његошева")
        assert segs[0].whole is True and segs[0].street_raw == "Његошева"

    def test_trailing_bb_in_structured(self):
        segs = parse_structured("Улица: Омладинских бригада бб")
        s = segs[0]
        assert s.street_raw == "Омладинских бригада"
        assert s.bez_broja is True and s.whole is False

    def test_settlement_carries_across_chunks(self):
        text = "Насеље: Ада Улица: Прва бројеви 1; Улица: Друга бројеви 2"
        segs = parse_structured(text)
        assert [s.settlement_raw for s in segs] == ["Ада", "Ада"]


# ── Dialect detection ────────────────────────────────────────────────────────
class TestDialectDetection:
    def test_structured_needs_ulica_and_brojevi(self):
        assert parse_coverage("Насеље: Ада Улица: 8. Март бројеви 1")[0].dialect == "structured"

    def test_compact_when_no_brojevi(self):
        assert parse_coverage("Алеја маршала Тита 2-10, Цара Лазара 1-23")[0].dialect == "compact"

    def test_empty_text(self):
        assert parse_coverage("") == []
        assert parse_coverage("   ") == []


# ── Number-side grammar: од…до…, до краја, side parity, fillers (§2.12) ───────
class TestBrojOdPredicates:
    @pytest.mark.parametrize("tok,expected", [
        ("бр", True), ("бр.", True), ("број", True), ("броја", True),
        ("бројеви", True), ("broj", True), ("broja", True),
        ("од", False), ("до", False), ("Прва", False),
    ])
    def test_is_broj_token(self, tok, expected):
        assert is_broj_token(tok) is expected

    @pytest.mark.parametrize("tok,expected", [
        ("од", True), ("Од", True), ("од.", True),
        ("до", False), ("бр", False), ("Прва", False),
    ])
    def test_is_od_token(self, tok, expected):
        assert is_od_token(tok) is expected


class TestOdDoRanges:
    def test_od_prefix_before_range(self):
        # "од" ends the name and is dropped; the dash-range parses normally.
        segs = parse_compact("Стевана Чоловића од 1-17")
        s = segs[0]
        assert s.street_raw == "Стевана Чоловића" and s.intervals == [[1, 17, "odd"]]

    def test_od_n_do_m(self):
        segs = parse_compact("Прва од 33 до 117")
        assert segs[0].intervals == [[33, 117, "odd"]]

    def test_od_broja_n_do_m(self):
        # The "броја" label between "од" and the number is skipped.
        segs = parse_compact("Прва од броја 33 до 117")
        assert segs[0].intervals == [[33, 117, "odd"]]

    def test_do_kraja_is_open_ended(self):
        segs = parse_compact("Прва од 5 до краја")
        assert segs[0].intervals == [[5, OPEN_END, "odd"]]
        assert OPEN_END == 100000

    def test_do_kraja_even_lo(self):
        segs = parse_compact("Прва од 4 до краја")
        assert segs[0].intervals == [[4, OPEN_END, "even"]]

    def test_do_is_connector_only_between_numbers(self):
        # A bare "До" with no surrounding numbers is a toponym — stays in the name.
        segs = parse_compact("Добри До")
        assert segs[0].street_raw == "Добри До" and segs[0].whole is True


class TestSideOfStreetParity:
    def test_na_parnoj_strani_overrides_to_even(self):
        segs = parse_compact("Прва 2-100 на парној страни")
        assert segs[0].intervals == [[2, 100, "even"]]

    def test_neparna_strana_overrides_to_odd(self):
        segs = parse_compact("Прва 1-99 непарна страна")
        assert segs[0].intervals == [[1, 99, "odd"]]


class TestBrojLabel:
    def test_broj_label_ends_name_and_is_dropped(self):
        # "Нова 27 бр. 5-9" with no register match for "Нова 27": name is "Нова",
        # 27 is a house single, бр. is dropped, 5-9 is the range.
        segs = parse_compact("Нова 27 бр. 5-9")
        s = segs[0]
        assert s.street_raw == "Нова"
        assert s.singles == [[27, ""]] and s.intervals == [[5, 9, "odd"]]

    def test_numbered_name_then_broj_label(self):
        # When "Нова 27" IS a register street, the number stays in the name and бр. 5-9
        # are the house numbers.
        reg = {normalize_street("Нова 27")}
        segs = parse_compact("Нова 27 бр. 5-9", is_street=reg.__contains__)
        s = segs[0]
        assert s.street_raw == "Нова 27" and s.intervals == [[5, 9, "odd"]]


class TestNumberedStreetNames:
    def test_nova_n_kept_as_name_with_predicate(self):
        reg = {normalize_street(n) for n in ("Нова 4", "Нова 6", "Нова 21")}
        segs = parse_compact("Нова 4, Нова 6, Нова 21", is_street=reg.__contains__)
        assert [s.street_raw for s in segs] == ["Нова 4", "Нова 6", "Нова 21"]

    def test_nova_n_collapses_without_predicate(self):
        # No register predicate -> each number looks like a house, collapsing to one street.
        segs = parse_compact("Нова 4, Нова 6, Нова 21")
        assert [s.street_raw for s in segs] == ["Нова"]
        assert segs[0].singles == [[4, ""], [6, ""], [21, ""]]

    def test_ulica_n_kept_as_name(self):
        reg = {normalize_street("Улица 27")}
        segs = parse_compact("Улица 27", is_street=reg.__contains__)
        assert [s.street_raw for s in segs] == ["Улица 27"]

    def test_bare_number_continuation_not_promoted(self):
        # j>0 guard: a bare number continuing the previous street is never promoted to a
        # street, even when "1" is a register street name elsewhere in the muni.
        reg = {normalize_street("1")}
        segs = parse_compact("Стројковце 0 и 1", is_street=reg.__contains__)
        assert [s.street_raw for s in segs] == ["Стројковце"]


class TestTextPreprocessing:
    @pytest.mark.parametrize("text", ["Прва 2- 100", "Прва 2 - 100", "Прва 2-100"])
    def test_dash_spacing_collapsed(self, text):
        # Spaces around a digit-to-digit dash are collapsed so the range stays one token.
        segs = parse_coverage(text)
        assert segs[0].intervals == [[2, 100, "even"]]

    def test_dash_spacing_does_not_touch_block_tags(self):
        # The dash collapse is digits-only; a block tag "С-1" is untouched (stays unknown).
        segs = parse_coverage("Прва С-1")
        assert segs[0].unknown_tokens == ["С-1"]

    def test_ordinal_glue_split_keeps_ordinal_in_name(self):
        # "7.јула" -> "7. јула": the ordinal stays part of the street name.
        segs = parse_coverage("7.јула 1-10")
        s = segs[0]
        assert s.street_raw == "7. јула" and s.intervals == [[1, 10, "all"]]

    @pytest.mark.parametrize("intro", ["у улицама:", "у улици:"])
    def test_list_preamble_stripped(self, intro):
        # A prose preamble ending "у улиц(и|ама):" is dropped so it doesn't glue onto the
        # first street (Беочин: "...у МЗ Беочин град у улицама: <streets>").
        segs = parse_coverage(f"На овом гласачком месту ... у МЗ Беочин град {intro} Дунавска, Његошева")
        assert [s.street_raw for s in segs] == ["Дунавска", "Његошева"]

    def test_preamble_prevents_false_structured_detection(self):
        # The preamble's "улицама" matches the structured `Улица:` label; with a `број` token
        # in the list the whole coverage was mis-parsed as one structured whole-street blob.
        # Stripping the preamble first keeps it compact -> per-street segments.
        segs = parse_coverage(
            "На овом гласачком месту ... у улицама: Светосавска од броја 6-14, Дунавска")
        assert [s.street_raw for s in segs] == ["Светосавска", "Дунавска"]
        assert segs[0].intervals == [[6, 14, "even"]]

    def test_no_preamble_untouched(self):
        # Normal coverage (no preamble) is unchanged, incl. a street named "... улица".
        segs = parse_coverage("Цара Лазара 1-23, Сутјеска улица")
        assert [s.street_raw for s in segs] == ["Цара Лазара", "Сутјеска улица"]
