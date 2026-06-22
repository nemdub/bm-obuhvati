"""Tests for stage03b_apply_amendments.py — see docs/parsing-matching/04-amendments.md.

Covers the pure bullet-parsing / street-matching helpers. The full apply loop (which reads
parquet) is an integration concern handled elsewhere.
"""

import pytest

import stage03b_apply_amendments as A


class TestStreetMatches:
    @pytest.mark.parametrize("a,b,expected", [
        ("Прва", "Прва", True),
        ("Прва", "Прва улица", True),     # substring tolerance
        ("Прва улица", "Прва", True),     # symmetric
        ("Прва", "Друга", False),
        ("", "Прва", False),              # empty never matches
    ])
    def test_street_matches(self, a, b, expected):
        assert A.street_matches(a, b) is expected


class TestParseBullet:
    def test_fix_street_name(self):
        op = A.parse_bullet('назив улице „Прва" се исправља и гласи: „Прва А"')
        assert op == {"op": "fix_street_name", "street": "Прва", "old": "Прва", "new": "Прва А"}

    def test_replace_range(self):
        op = A.parse_bullet('у улици Прва распон кућних бројева од 1-10 мења се и гласи: „1-20"')
        assert op["op"] == "replace_range"
        assert op["street"] == "Прва" and op["old"] == "1-10" and op["new"] == "1-20"

    def test_add_house(self):
        op = A.parse_bullet("у улици Прва додаје се кућни број 5")
        assert op == {"op": "add_house", "street": "Прва", "old": "", "new": "5"}

    def test_add_house_after_anchor(self):
        op = A.parse_bullet("у улици Прва после кућног броја 3 додаје се кућни број 5")
        assert op["op"] == "add_house" and op["old"] == "3" and op["new"] == "5"

    def test_unrecognized_returns_none(self):
        assert A.parse_bullet("нешто сасвим друго") is None


class TestNumsFrom:
    def test_parses_ranges_and_singles(self):
        seg = A._nums_from("1-10 и 12, 14")
        assert seg.intervals == [[1, 10, "all"]]
        assert seg.singles == [[12, ""], [14, ""]]


class TestBulletAnchor:
    def test_anchor_regex_captures_station_number(self):
        m = A.BULLET.search("Гласачко место број 7 ...")
        assert m and int(m.group(1)) == 7


class TestReplacementParsing:
    def _row(self, num, name, addr="addr", cov="cov"):
        # rows_from_docx output shape: (section, number, name, address, coverage)
        return (None, num, name, addr, cov)

    def test_name_and_address_change(self):
        # Palilula 98: OLD (first) -> NEW (last), name + address differ.
        rows = [
            self._row(98, 'ИСТУРЕНО ОДЕЉЕЊЕ ОШ', "СКЕЛА, ТОВИЛИШТЕ ББ", "Дирекција"),
            self._row(98, 'ОШ "ОЛГА ПЕТРОВ"', "СКЕЛА, СКЕЛА БР. 9", "Дирекција"),
        ]
        ops = A.replacements_from_rows(rows)
        assert len(ops) == 1
        op = ops[0]
        assert op["number"] == 98
        assert op["old_name"] == "ИСТУРЕНО ОДЕЉЕЊЕ ОШ"
        assert op["new_name"] == 'ОШ "ОЛГА ПЕТРОВ"'
        assert op["new_address"] == "СКЕЛА, СКЕЛА БР. 9"

    def test_coverage_only_change(self):
        rows = [
            self._row(84, "ОШ", "addr", "ВУЛОВИЋА 23В"),
            self._row(84, "ОШ", "addr", "ВУЛОВИЋА 23Б"),
        ]
        ops = A.replacements_from_rows(rows)
        assert len(ops) == 1 and ops[0]["new_coverage"] == "ВУЛОВИЋА 23Б"

    def test_identical_reprint_skipped(self):
        rows = [self._row(5, "ОШ", "a", "c"), self._row(5, "ОШ", "a", "c")]
        assert A.replacements_from_rows(rows) == []

    def test_single_record_not_a_replacement(self):
        # Only a NEW row, no OLD pair → not emitted (additions / single-record forms).
        assert A.replacements_from_rows([self._row(7, "ОШ", "a", "c")]) == []

    def test_dual_script_doc_skipped(self):
        # Tutin/Sjenica-style: every name cell restates itself in Latin → whole doc skipped.
        rows = [
            self._row(24, "Приватна кућа Privatna kuća", "Растеновиће Rastenoviće", "x"),
            self._row(24, "Основна школа Osnovna škola", "Тузиње Tuzinje", "x"),
        ]
        assert A.replacements_from_rows(rows) == []


class TestCellIsDualScript:
    @pytest.mark.parametrize("cell,dual", [
        ("Основна школа Osnovna škola", True),
        ("Тузиње Tuzinje", True),
        ("ОШ \"ОЛГА ПЕТРОВ\"", False),       # pure Cyrillic
        ("МЗ МАКИШ", False),
    ])
    def test_cell_is_dual_script(self, cell, dual):
        assert A._cell_is_dual_script(cell) is dual
