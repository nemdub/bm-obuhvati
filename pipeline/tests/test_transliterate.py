"""Tests for common/transliterate.py — see docs/parsing-matching/01-normalization.md §1.8."""

import pytest

from common.transliterate import cyr_to_lat, lat_to_cyr, nfc


class TestCyrToLat:
    @pytest.mark.parametrize("cyr,lat", [
        ("Београд", "Beograd"),
        ("Нови Сад", "Novi Sad"),
        # Digraphs.
        ("Љубав", "Ljubav"),
        ("Њива", "Njiva"),
        ("Џада", "Džada"),
        ("Љубав Џ Њ", "Ljubav Dž Nj"),
        # Diacritics preserved.
        ("Ћирила", "Ćirila"),
        ("Шума", "Šuma"),
    ])
    def test_basic(self, cyr, lat):
        assert cyr_to_lat(cyr) == lat

    def test_non_cyrillic_passthrough(self):
        assert cyr_to_lat("ABC 123") == "ABC 123"


class TestLatToCyr:
    @pytest.mark.parametrize("lat,cyr", [
        # Returns LOWERCASE Cyrillic (intended for short suffix tokens, caller uppercases).
        ("Ljubav", "љубав"),
        ("Njiva", "њива"),
        ("B", "б"),
        ("190B", "190б"),
    ])
    def test_basic(self, lat, cyr):
        assert lat_to_cyr(lat) == cyr

    def test_digraphs_before_singles(self):
        # 'lj' must be consumed as one Cyrillic letter, not 'l' + 'j'.
        assert lat_to_cyr("lj") == "љ"
        assert lat_to_cyr("nj") == "њ"
        assert lat_to_cyr("dž") == "џ"

    def test_dj_fallback_is_shadowed_by_single_d(self):
        # KNOWN QUIRK: the ASCII fallback pair ("dj" -> "ђ") is listed AFTER the single
        # "d" -> "д" pair, so greedy matching consumes 'd' first and the digraph never
        # fires: "dj" -> "дј", not "ђ". Documented here so a future fix (moving the pair
        # earlier) flips this test deliberately rather than silently.
        assert lat_to_cyr("dj") == "дј"

    def test_unknown_chars_passthrough(self):
        assert lat_to_cyr("123-") == "123-"


class TestNfc:
    def test_composed_equals_decomposed(self):
        # Construct both forms with explicit code points so file-level Unicode
        # normalization can't collapse them: U+0107 vs 'c' + U+0301 (combining acute).
        composed = "\u0107"
        decomposed = "c\u0301"
        assert composed != decomposed             # differ before normalization
        assert nfc(composed) == nfc(decomposed)   # equal after
        assert nfc(decomposed) == composed
