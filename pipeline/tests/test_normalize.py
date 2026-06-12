"""Tests for common/normalize.py — see docs/parsing-matching/01-normalization.md."""

import pytest

from common.normalize import (
    SUFFIX_AZBUKA,
    genitive_variant,
    genitive_variants,
    normalize_house,
    normalize_street,
    normalize_suffix,
    suffix_rank,
)


class TestNormalizeStreet:
    @pytest.mark.parametrize("raw,norm", [
        # Label stripping + uppercase + punctuation drop.
        ("Улица: 8. Март", "8 МАРТ"),
        ("ул. Светог Саве", "СВЕТОГ САВЕ"),
        ("бр. 5 Маја", "5 МАЈА"),
        # Abbreviation expansion.
        ("Ј.Н.А.", "ЈНА"),
        # "ДР" -> "ДОКТОРА" (whole word).
        ("Др Ђорђа Лазића", "ДОКТОРА ЂОРЂА ЛАЗИЋА"),
    ])
    def test_basic(self, raw, norm):
        assert normalize_street(raw) == norm

    def test_idempotent(self):
        once = normalize_street("Краља Петра Првог")
        assert normalize_street(once) == once

    def test_empty(self):
        assert normalize_street("") == ""
        assert normalize_street("   ") == ""


class TestRomanNumerals:
    @pytest.mark.parametrize("raw,norm", [
        ("XII војвођанске", "12 ВОЈВОЂАНСКЕ"),
        ("VIII војвођанска", "8 ВОЈВОЂАНСКА"),
        ("IV", "4"),
        ("VIII", "8"),
    ])
    def test_roman_to_arabic(self, raw, norm):
        assert normalize_street(raw) == norm


class TestSpelledOrdinals:
    @pytest.mark.parametrize("raw,norm", [
        ("Краља Петра Првог", "КРАЉА ПЕТРА 1"),
        ("Краља Петра I", "КРАЉА ПЕТРА 1"),     # Roman path converges
        ("Краља Петра 1", "КРАЉА ПЕТРА 1"),     # already Arabic
        ("Другог", "2"),
        ("Други", "2"),
        # Longest stem wins: ПЕТНАЕСТ (15), not ПЕТ (5).
        ("Петнаестог", "15"),
    ])
    def test_ordinal_to_arabic(self, raw, norm):
        assert normalize_street(raw) == norm

    @pytest.mark.parametrize("raw", [
        "ПЕТ",        # bare cardinal, no inflection -> untouched
        "СЕДАМ",      # cardinal
        "ДРУГОВИ",    # not an ordinal ending
        "ОСМАНЛИЈА",  # unrelated word starting with ОСМ
        "ПРВЕНСТВА",  # unrelated word starting with ПРВ
    ])
    def test_non_ordinals_untouched(self, raw):
        # The word must survive unchanged (it contains no digits afterwards).
        assert normalize_street(raw) == raw.upper()


class TestHomoglyphFolding:
    def test_mixed_script_word_folded(self):
        # Latin 'A' inside an otherwise-Cyrillic word -> folded to all-Cyrillic.
        assert normalize_street("AПАТИН") == "АПАТИН"

    def test_pure_latin_roman_numeral_untouched_by_fold(self):
        # VIII is pure Latin -> not homoglyph-folded; it converts via the Roman path only.
        assert normalize_street("VIII") == "8"

    def test_pure_cyrillic_untouched(self):
        assert normalize_street("ПАТИН") == "ПАТИН"


class TestNormalizeHouse:
    @pytest.mark.parametrize("raw,num,suffix", [
        ("190", 190, ""),
        ("190Б", 190, "Б"),
        ("190-Б", 190, "Б"),
        ("190B", 190, "Б"),     # Latin B folded to Cyrillic Б
        ("0-ББ", 0, "ББ"),
        ("бб", None, "ББ"),     # no leading digit -> num=None
    ])
    def test_parse(self, raw, num, suffix):
        h = normalize_house(raw)
        assert (h.num, h.suffix) == (num, suffix)

    def test_key(self):
        assert normalize_house("12А").key() == (12, "А")


class TestNormalizeSuffix:
    @pytest.mark.parametrize("raw,out", [
        ("-Б", "Б"),
        ("/B", "Б"),
        ("b", "Б"),
        (" а ", "А"),
        ("", ""),
    ])
    def test_basic(self, raw, out):
        assert normalize_suffix(raw) == out


class TestSuffixRank:
    def test_azbuka_order_not_codepoint(self):
        # Д precedes Ц in the Serbian alphabet (Д=5th, Ц=24th), unlike naive ordering.
        assert suffix_rank("Д") < suffix_rank("Ц")

    def test_empty_sorts_first(self):
        assert suffix_rank("") < suffix_rank("А")

    def test_full_alphabet_monotonic(self):
        ranks = [suffix_rank(ch) for ch in SUFFIX_AZBUKA]
        assert ranks == sorted(ranks)

    def test_unknown_char_ranks_after_known(self):
        assert suffix_rank("@") > suffix_rank("Ш")


class TestGenitiveVariants:
    def test_a_stem(self):
        # А<->Е on each word.
        vs = genitive_variants("НИКОЛА ТЕСЛА")
        assert "НИКОЛЕ ТЕСЛЕ" in vs
        assert "НИКОЛА ТЕСЛА" not in vs   # name itself excluded

    def test_o_final_both_styles(self):
        # О-final: Hungarian +А (ДАНКОА) and Serbian -О+А (ДАНКА).
        vs = genitive_variants("ДАНКО")
        assert "ДАНКОА" in vs
        assert "ДАНКА" in vs

    def test_capped(self):
        # Never exceeds the _MAX_GEN_VARIANTS cap (16).
        vs = genitive_variants("НИКОЛА ПЕТРА ЛАЗАРА МАРКА")
        assert len(vs) <= 16

    def test_short_or_numeric_word_not_declined(self):
        # Words < 4 chars or containing digits keep their single option, so a name made
        # only of such words has no declension variants. ("МАЈ" is 3 chars; "8" numeric.)
        assert genitive_variants("8 МАЈ") == []

    def test_genitive_variant_first(self):
        assert genitive_variant("НИКОЛА ТЕСЛА") == genitive_variants("НИКОЛА ТЕСЛА")[0]

    def test_genitive_variant_none_when_no_variants(self):
        assert genitive_variant("8") is None
