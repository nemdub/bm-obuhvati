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
