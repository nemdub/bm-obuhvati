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

    def test_bare_zero_dropped(self):
        # "0" is filler (no real house number) — dropped entirely, not a single.
        s = _num("0")
        assert s.singles == [] and s.intervals == [] and s.unknown_tokens == []

    def test_suffixed_zero_kept(self):
        # Only a BARE "0" is filler; "0а" is left as a single (handled elsewhere).
        assert _num("0а").singles == [[0, "А"]]


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

    def test_name_with_only_zero_is_whole(self):
        # "Блендија 0": the filler "0" is dropped, leaving a whole-name claim (whole settlement
        # or street) — not a single matching the nonexistent house 0.
        segs = parse_compact("Блендија 0")
        assert _segtuples(segs) == [("Блендија", True, False, [], [], [], "whole_street")]

    def test_leading_zero_before_number_list(self):
        # A phantom "0" ahead of a real number list is dropped; the list is the coverage.
        segs = parse_compact("Школска 0 1 2")
        assert _segtuples(segs) == [
            ("Школска", False, False, [], [[1, ""], [2, ""]], [], "street_numbers")]


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


class TestGluedHouseNumber:
    """A house number glued to a street name with no space ("Косовска8") is split off by
    _NAME_NUM_GLUE so the number leaves the street name (the Jagodina convention)."""

    def test_glued_number_split_off(self):
        s = parse_coverage("Косовска8")[0]
        assert s.street_raw == "Косовска" and s.singles == [[8, ""]]

    def test_multiword_name_glued_number(self):
        s = parse_coverage("Тихомира Матића12")[0]
        assert s.street_raw == "Тихомира Матића" and s.singles == [[12, ""]]

    def test_two_digit_glued_number(self):
        s = parse_coverage("Школска15")[0]
        assert s.street_raw == "Школска" and s.singles == [[15, ""]]

    def test_numbered_name_reattaches_via_is_street(self):
        # A genuinely numbered street ("Нова 13") keeps the number in the NAME because the
        # register has it (always spaced); the split is undone by parse_compact's is_street.
        reg = {normalize_street("Нова 13")}
        s = parse_coverage("Нова13", is_street=reg.__contains__)[0]
        assert s.street_raw == "Нова 13" and s.singles == []

    def test_block_tag_not_split(self):
        # Housing-estate block tags are 1-2 letters glued to a digit — must stay whole on the
        # number side, never split into "name + number".
        s = parse_coverage("Михаила Пупина С-1")[0]
        assert s.street_raw == "Михаила Пупина" and s.unknown_tokens == ["С-1"]

    def test_house_suffix_letter_not_glued(self):
        # Digit-then-letter (a house suffix "8А") is the other direction — untouched.
        s = parse_coverage("Косовска 8А")[0]
        assert s.street_raw == "Косовска" and s.singles == [[8, "А"]]


class TestGluedNameWords:
    """Two name words glued with no space ("КраљаАлександра") are split by _CAMEL_GLUE on the
    lower->upper case boundary, which marks a word break in Serbian Title-Case names."""

    def test_two_words_split(self):
        assert parse_coverage("КраљаАлександра")[0].street_raw == "Краља Александра"

    def test_three_words_split(self):
        assert parse_coverage("ПутКнезаМихајла")[0].street_raw == "Пут Кнеза Михајла"

    def test_all_caps_tail_not_split(self):
        # "СолунСКА" is a mis-cased "СОЛУНСКА" (one word) — the uppercase tail does not begin a
        # Title-Case word, so it must stay whole.
        assert parse_coverage("СолунСКА")[0].street_raw == "СолунСКА"

    def test_glued_words_and_number(self):
        # Combines with _NAME_NUM_GLUE: word split + house number split off.
        s = parse_coverage("КраљаАлександра72")[0]
        assert s.street_raw == "Краља Александра" and s.singles == [[72, ""]]

    def test_spaced_name_unchanged(self):
        # Correctly spaced text has no lower->upper adjacency — idempotent.
        assert parse_coverage("Краља Александра")[0].street_raw == "Краља Александра"


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


class TestDashDoKraja:
    """A dash used in place of "од … до" ("98-до краја" == "од 98 до краја")."""

    def test_dash_do_kraja_open_ended(self):
        # "98-до краја" -> [98, OPEN_END, even] (98 is even).
        segs = parse_coverage("Лазара Мићуновића 98-до краја")
        assert segs[0].street_raw == "Лазара Мићуновића"
        assert segs[0].intervals == [[98, OPEN_END, "even"]]

    def test_dash_do_kraja_with_space(self):
        # "12- до краја" (space after the dash) parses the same.
        segs = parse_coverage("Прва 12- до краја")
        assert segs[0].intervals == [[12, OPEN_END, "even"]]

    def test_two_dash_do_kraja_split_by_parity(self):
        # "19-до краја и 44-до краја" -> odd from 19 AND even from 44, one street.
        segs = parse_coverage("Лазара Мићуновића 19-до краја и 44-до краја")
        assert len(segs) == 1
        assert segs[0].intervals == [[19, OPEN_END, "odd"], [44, OPEN_END, "even"]]

    def test_plain_number_dash_range_untouched(self):
        # The fix only triggers before "до"; a plain "N-M" range is still a range.
        segs = parse_coverage("Прва 2-44")
        assert segs[0].intervals == [[2, 44, "even"]]


class TestSideOfStreetParity:
    def test_na_parnoj_strani_overrides_to_even(self):
        segs = parse_compact("Прва 2-100 на парној страни")
        assert segs[0].intervals == [[2, 100, "even"]]

    def test_neparna_strana_overrides_to_odd(self):
        segs = parse_compact("Прва 1-99 непарна страна")
        assert segs[0].intervals == [[1, 99, "odd"]]

    def test_standalone_neparna_strana_is_whole_odd_side(self):
        # "Белодримска непарна страна" -> the whole odd side, [1, OPEN_END, odd].
        segs = parse_compact("Белодримска непарна страна")
        assert segs[0].street_raw == "Белодримска"
        assert segs[0].intervals == [[1, OPEN_END, "odd"]] and segs[0].whole is False

    def test_standalone_parni_brojevi_is_whole_even_side(self):
        # "парни бројеви" (no specific numbers) -> the whole even side, [2, OPEN_END, even].
        segs = parse_compact("Љубе Нешића парни бројеви")
        assert segs[0].intervals == [[2, OPEN_END, "even"]]

    def test_dash_separator_before_side_is_dropped(self):
        # "Бањска - непарна страна": the separator dash is stripped from the name.
        segs = parse_compact("Бањска - непарна страна")
        assert segs[0].street_raw == "Бањска"
        assert segs[0].intervals == [[1, OPEN_END, "odd"]]

    def test_side_directive_continues_previous_street(self):
        # "и непарни бројеви" as its own clause qualifies the previous street. The filler "0"
        # house number is dropped (register has no house 0), leaving just the odd-side claim.
        segs = parse_compact("Краља Петра Првог 0 и непарни бројеви")
        assert len(segs) == 1
        s = segs[0]
        assert s.singles == [] and s.intervals == [[1, OPEN_END, "odd"]]

    def test_side_before_range(self):
        # "непарни од 1 до 9 и парни од 14 до 86" -> two parity-split ranges, one street.
        segs = parse_compact("Гаврилова непарни од 1 до 9 и парни од 14 до 86")
        assert len(segs) == 1
        assert segs[0].intervals == [[1, 9, "odd"], [14, 86, "even"]]

    def test_parity_register_street_not_split(self):
        # ПАРНИЧКА / ПАРНИЦА start with the parity stem but are real streets — never split.
        segs = parse_compact("Парничка 5, Парница")
        assert [s.street_raw for s in segs] == ["Парничка", "Парница"]
        assert segs[0].singles == [[5, ""]] and segs[1].whole is True

    def test_users_combined_example(self):
        # The motivating station text: odd side + even-from-98 on one street.
        segs = parse_coverage("Белодримска непарна страна и 98-до краја")
        assert len(segs) == 1 and segs[0].street_raw == "Белодримска"
        assert segs[0].intervals == [[1, OPEN_END, "odd"], [98, OPEN_END, "even"]]


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

    def test_dotted_house_numbers_in_list(self):
        # "Церских јунака 52. и 54.": the trailing dots are punctuation on house numbers,
        # not list ordinals — they parse as singles 52 and 54, one street.
        segs = parse_coverage("Церских јунака 52. и 54.")
        assert len(segs) == 1 and segs[0].street_raw == "Церских јунака"
        assert segs[0].singles == [[52, ""], [54, ""]]

    def test_dotted_house_numbers_do_not_break_ordinal_name(self):
        # An ordinal followed by a WORD ("8. Март") keeps its number in the name.
        segs = parse_coverage("8. Март 1-10")
        assert segs[0].street_raw == "8. Март" and segs[0].intervals == [[1, 10, "all"]]

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


class TestObuhvataPreamble:
    """Smederevska Palanka 'обухвата … улица[:]' prose preambles."""

    def test_podrucje_ulica_stripped(self):
        segs = parse_coverage("обухвата подручје улица: 7. јула, Азањске чете, Алексе Даниловића")
        assert [s.street_raw for s in segs] == ["7. јула", "Азањске чете", "Алексе Даниловића"]

    def test_no_colon_and_glued_digit(self):
        # "обухвата подручје улица28. марта" — no colon, the label glued to the first number.
        segs = parse_coverage("обухвата подручје улица28. марта, Боре Станковића")
        assert [s.street_raw for s in segs] == ["28. марта", "Боре Станковића"]

    def test_settlement_based_preamble(self):
        segs = parse_coverage(
            "обухвата бираче са подручја Месне заједнице Баничина - подручје улица: Босићка, Бркина")
        assert [s.street_raw for s in segs] == ["Босићка", "Бркина"]

    def test_no_collapse_to_structured(self):
        # The bug: "улица" (preamble) + "(од броја …)" flipped the cell to the structured
        # dialect and collapsed it to one segment. The strip keeps it compact.
        segs = parse_coverage(
            "обухвата подручје улица 27. марта, Краља Петра првог (од броја 97 до краја непарна), Ловачка")
        assert [s.street_raw for s in segs] == ["27. марта", "Краља Петра првог", "Ловачка"]
        assert segs[1].intervals == [[97, 100000, "odd"]]


class TestNumberParenUnwrap:
    """Parenthesised number/side directives are unwrapped; alt-names are kept."""

    def test_paren_range_directive_unwrapped(self):
        segs = parse_coverage(
            "Краља Петра првог (од броја 97 до краја непарна, од броја 102 до краја парна страна)")
        assert segs[0].street_raw == "Краља Петра првог"
        assert segs[0].intervals == [[97, 100000, "odd"], [102, 100000, "even"]]

    def test_paren_all_odd_unwrapped(self):
        segs = parse_coverage("Омладинска (сви непарни бројеви)")
        assert segs[0].street_raw == "Омладинска" and segs[0].intervals == [[1, 100000, "odd"]]

    def test_altname_paren_kept(self):
        # No number/side directive inside -> left intact for stage04 alt-name matching.
        for raw in ("Корзо (Бориса Кидрича)", "Елека Бенедека (493. нова)"):
            segs = parse_coverage(raw)
            assert segs[0].street_raw == raw


class TestLjubovijaDirectives:
    """Ljubovija `(и то: …)` number directives with `бројеви кућа` and settlement-assignment
    clarifications — see docs/parsing-matching/02 §2.16."""

    def _one(self, raw, street):
        segs = parse_coverage(raw)
        seg = next(s for s in segs if s.street_raw == street)
        return segs, seg

    def test_comma_before_paren_attaches_ranges(self):
        # Station 12, the reported case: a comma before the paren must not strand the ranges
        # on a phantom "то:" street — they belong to the preceding "Крупањски пут".
        raw = ("Расадничка, Ћуверска, Живановићка, Крупањски пут, (и то: непарни бројеви кућа од "
               "155 – 237, парни бројеви кућа од  152 – 258), Тисићка")
        segs, seg = self._one(raw, "Крупањски пут")
        assert seg.intervals == [[155, 237, "odd"], [152, 258, "even"]]
        names = [s.street_raw for s in segs]
        assert "то:" not in names and "то" not in names
        assert all(not s.unknown_tokens for s in segs)
        assert names == ["Расадничка", "Ћуверска", "Живановићка", "Крупањски пут", "Тисићка"]

    def test_attached_paren_directive(self):
        # Station 2: paren glued to the name, "и то" + "бројеви кућа" dropped.
        _, seg = self._one("Моше Пијаде (и то непарни бројеви кућа од 1 – 39)", "Моше Пијаде")
        assert seg.intervals == [[1, 39, "odd"]] and not seg.unknown_tokens

    def test_only_even_whole_side(self):
        # "и то само парни бројеви кућа" = the whole even side, no range.
        _, seg = self._one("Моше Пијаде (и то само парни бројеви кућа)", "Моше Пијаде")
        assert seg.intervals == [[2, OPEN_END, "even"]] and not seg.whole

    def test_settlement_assignment_clarification_dropped(self):
        # Station 5: the "од којих … припадају насељу X" clarification is redundant and must
        # not inject sub-ranges or unknown tokens; only the two parent ranges survive.
        raw = ("Ужички пут (и то: непарни бројеви кућа од 1 – 105, од којих бројеви од 1 – 59 "
               "припадају насељу Љубовија а бројеви од 61 – 105 припадају насељу Дубоко, парни "
               "бројеви кућа од 2 – 80, од којих бројеви од 2 – 54 припадају насељу Љубовија а "
               "бројеви од 56 -80 припадају насељу Дубоко), Милунке Савић")
        segs, seg = self._one(raw, "Ужички пут")
        assert seg.intervals == [[1, 105, "odd"], [2, 80, "even"]]
        assert [s.street_raw for s in segs] == ["Ужички пут", "Милунке Савић"]
        assert all(not s.unknown_tokens for s in segs)

    def test_chained_continuation_no_phantom_street(self):
        # Station 14: a chained ", а бројеви … припадају насељу" continuation must not become a
        # phantom "а" street, and a bare "насеље X" label inside a directive is dropped.
        raw = ("Зворнички Пут (и то: непарни бројеви кућа од 531 – 701, од којих бројеви од 531 – "
               "541 припадају насељу Селанац, а бројеви од 543-701 припадају насељу Црнча, парни "
               "бројеви кућа од 660 – 818, од којих бројеви од 660 – 674 припадају насељу Селанац, "
               "а бројеви од 676-818 припадају насељу Црнча), Липничка")
        segs, seg = self._one(raw, "Зворнички Пут")
        assert seg.intervals == [[531, 701, "odd"], [660, 818, "even"]]
        names = [s.street_raw for s in segs]
        assert names == ["Зворнички Пут", "Липничка"]
        assert "а" not in names and all(not s.unknown_tokens for s in segs)

    def test_inline_pripadaju_tail_truncated(self):
        # Station 9 "Крупањски пут": "… 1 -7 који припадају насељу Грачаница" — keep the range,
        # cut the inline relative clause.
        raw = ("Крупањски пут (и то: непарни бројеви кућа од 1 -7 који припадају насељу Грачаница, "
               "парни бројеви кућа од 2 – 6 који припадају насељу Лоњин), Заселак Биљићи")
        _, seg = self._one(raw, "Крупањски пут")
        assert seg.intervals == [[1, 7, "odd"], [2, 6, "even"]]
        assert not seg.unknown_tokens

    def test_a_connector_continuation_attaches(self):
        # Station 5 "Буковичка": "непарни … 1 – 7, а парни … 18 – 28" — the "а" connector must
        # not become a phantom street; both ranges land on Буковичка.
        raw = "Буковичка (и то: непарни бројеви кућа од  1 – 7, а парни бројеви кућа од  18 – 28)"
        segs, seg = self._one(raw, "Буковичка")
        assert seg.intervals == [[1, 7, "odd"], [18, 28, "even"]]
        assert [s.street_raw for s in segs] == ["Буковичка"]

    def test_single_house_directive(self):
        # Station 33 "Зобнашка (и то кућа број 41)" — a non-parity single, not a phantom street.
        raw = "Зобнашка (и то кућа број 41), Брезичка"
        segs, seg = self._one(raw, "Зобнашка")
        assert seg.singles == [[41, ""]] and not seg.intervals and not seg.unknown_tokens
        assert [s.street_raw for s in segs] == ["Зобнашка", "Брезичка"]

    def test_dangling_paren_typo(self):
        # Station 24 source typo: an extra ')' closes the paren early, leaving "80-116)".
        raw = ("Шапарска (и то: непарни бројеви кућа од 53 – 95), парни бројеви кућа од 80 – 116), "
               "Миличићка")
        segs, seg = self._one(raw, "Шапарска")
        assert seg.intervals == [[53, 95, "odd"], [80, 116, "even"]]
        assert [s.street_raw for s in segs] == ["Шапарска", "Миличићка"]

    def test_settlement_scope_preamble_stripped(self):
        # Stations 10/15: "у насељу <Name>:" scope marker is stripped, comma separator kept so
        # the preceding street is not glued onto the following one.
        raw = "Заселак Дубоки поток, у насељу Соколац: Брдарска (и то парни бројеви кућа од 68 – 82)"
        segs = parse_coverage(raw)
        names = [s.street_raw for s in segs]
        assert names == ["Заселак Дубоки поток", "Брдарска"]
        assert segs[1].intervals == [[68, 82, "even"]]

    def test_ordinary_directive_paren_unchanged(self):
        # Guard: a non-Ljubovija directive paren (no clarification noise) is untouched.
        segs = parse_coverage("Омладинска (сви непарни бројеви)")
        assert segs[0].street_raw == "Омладинска" and segs[0].intervals == [[1, OPEN_END, "odd"]]
