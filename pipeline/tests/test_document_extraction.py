"""Tests for stage02_extract_docs.py — see docs/parsing-matching/03-document-extraction.md.

These cover the pure text/HTML parsing helpers; the file-glob / textutil shelling and the
parquet writing are integration concerns and not unit-tested here.
"""

import pytest

import stage02_extract_docs as S2

HEADER = "НАЗИВ ГЛАСАЧКОГ МЕСТА"


class TestFilenameCleaning:
    @pytest.mark.parametrize("name,candidate", [
        ("Ada.doc", "Ada"),
        ("Bor-glasacka-mesta.docx", "Bor"),
        ("Backa-Topola.doc", "Backa-Topola"),
        # Amendment connector words AND the scraper's leading "\d+_" id are stripped.
        ("12_Subotica-izmena.doc", "Subotica-"),
    ])
    def test_clean_filename_to_candidate(self, name, candidate):
        assert S2.clean_filename_to_candidate(name) == candidate


class TestDeaccent:
    @pytest.mark.parametrize("raw,out", [
        ("Bačka", "Backa"),
        ("Đorđe", "Dorde"),
        ("Bor", "Bor"),
    ])
    def test_deaccent(self, raw, out):
        assert S2.deaccent(raw) == out


class TestFileClassification:
    @pytest.mark.parametrize("name", [
        "Subotica-izmena.doc", "Ada-dopuna.docx", "Nis-ispravka.doc",
    ])
    def test_amendment_detected(self, name):
        assert S2.AMENDMENT_RE.search(name)

    def test_base_not_amendment(self):
        assert not S2.AMENDMENT_RE.search("Ada.doc")

    def test_military_detected(self):
        assert S2.MILITARY_RE.search("vojska.doc")


class TestRowsFromDoc:
    def test_lone_integer_delimits_stations(self):
        txt = "\n".join([
            HEADER,
            "1", "ОШ Вук", "Ул. Прва 1", "Прва 1-10",
            "2", "Дом", "Друга 2", "Друга бб",
        ])
        rows = S2.rows_from_doc(txt)
        assert rows == [
            (None, 1, "ОШ Вук", "Ул. Прва 1", "Прва 1-10"),
            (None, 2, "Дом", "Друга 2", "Друга бб"),
        ]

    def test_trailing_period_number(self):
        txt = "\n".join([HEADER, "1.", "ОШ Вук", "Адреса", "Покриће"])
        rows = S2.rows_from_doc(txt)
        assert rows[0][1] == 1

    def test_coverage_joins_extra_lines(self):
        txt = "\n".join([HEADER, "1", "ОШ Вук", "Адреса", "Прва 1-10", "Друга 2-8"])
        rows = S2.rows_from_doc(txt)
        assert rows[0][4] == "Прва 1-10 Друга 2-8"

    def test_table_end_trim_drops_boilerplate(self):
        # The closing "II ..." section must not become part of the last station.
        txt = "\n".join([
            HEADER,
            "1", "ОШ Вук", "Ул. Прва 1", "Прва 1-10",
            "II", "Ово решење доставити Републичкој изборној комисији",
        ])
        rows = S2.rows_from_doc(txt)
        assert rows == [(None, 1, "ОШ Вук", "Ул. Прва 1", "Прва 1-10")]

    def test_table_end_only_fires_inside_table(self):
        # The same phrase in the PREAMBLE (before any station) must not abort parsing.
        txt = "\n".join([
            "Ово решење доставити Републичкој изборној комисији",  # preamble noise
            HEADER,
            "1", "ОШ Вук", "Адреса", "Прва 1-10",
        ])
        rows = S2.rows_from_doc(txt)
        assert len(rows) == 1 and rows[0][1] == 1

    def test_dual_script_rows_keep_only_cyrillic(self):
        # Tutin/Prijepolje/Sjenica print every cell twice (Cyrillic then Latin). The
        # Latin restatements must NOT leak into the address (it would steal the Latin
        # name) or the coverage (it would prepend the station's own address + Latin list).
        txt = "\n".join([
            HEADER,
            "1",
            "ЛОКАЛ ХАМЗАГИЋ РЕШАДА", "LOKAL HAMZAGIĆ REŠADA",        # name cyr / lat
            "ТУТИН, БОГОЉУБА ЧУКИЋА ББ", "TUTIN, BOGOLjUBA ČUKIĆA BB",  # address cyr / lat
            "Богољуба Чукића бб, Градац", "Bogoljuba Čukića bb, Gradac",  # coverage cyr / lat
        ])
        rows = S2.rows_from_doc(txt)
        assert rows == [(
            None, 1,
            "ЛОКАЛ ХАМЗАГИЋ РЕШАДА",
            "ТУТИН, БОГОЉУБА ЧУКИЋА ББ",
            "Богољуба Чукића бб, Градац",
        )]

    def test_dual_script_row_with_undoubled_name(self):
        # A Sjenica station whose NAME isn't restated (5 lines, not 6). Pairwise collapse
        # still aligns address/coverage to their Cyrillic side.
        txt = "\n".join([
            HEADER,
            "1",
            "Приватна кућа Неџада Муратовића",   # name (no Latin twin)
            "Врсјенице", "Vrsjenice",            # address cyr / lat
            "Баре, Врсјенице", "Bare, Vrsjenice",  # coverage cyr / lat
        ])
        rows = S2.rows_from_doc(txt)
        assert rows == [(
            None, 1, "Приватна кућа Неџада Муратовића", "Врсјенице", "Баре, Врсјенице",
        )]

    def test_single_script_row_untouched_by_dedupe(self):
        # A normal Cyrillic-only row must pass through unchanged: the line after the name
        # is the Cyrillic address, never the Latin transliteration of the name.
        txt = "\n".join([HEADER, "1", "ОШ Вук", "Ул. Прва 1", "Прва 1-10", "Друга 2-8"])
        rows = S2.rows_from_doc(txt)
        assert rows == [(None, 1, "ОШ Вук", "Ул. Прва 1", "Прва 1-10 Друга 2-8")]


class TestRowsFromDocTriplets:
    def test_groups_into_triplets(self):
        txt = "\n".join([
            HEADER,
            "ОШ Вук", "Прва 1", "Прва 1-10",
            "Дом", "Друга 2", "Друга бб",
        ])
        rows = S2.rows_from_doc_triplets(txt)
        assert rows == [
            (None, 1, "ОШ Вук", "Прва 1", "Прва 1-10"),
            (None, 2, "Дом", "Друга 2", "Друга бб"),
        ]

    def test_sequential_numbering(self):
        txt = "\n".join([HEADER] + ["a", "b", "c"] * 3)
        rows = S2.rows_from_doc_triplets(txt)
        assert [r[1] for r in rows] == [1, 2, 3]


class TestRowsFromDocx:
    def test_basic_table_row(self):
        html = (
            "<table>"
            "<tr><td>Р.б.</td><td>НАЗИВ ГЛАСАЧКОГ МЕСТА</td><td>Адреса</td><td>Подручје</td></tr>"
            "<tr><td>1</td><td>ОШ Вук</td><td>Прва 1</td><td>Прва 1-10</td></tr>"
            "</table>"
        )
        rows = S2.rows_from_docx(html)
        assert rows == [(None, 1, "ОШ Вук", "Прва 1", "Прва 1-10")]

    def test_header_row_skipped(self):
        html = (
            "<table>"
            "<tr><td>1</td><td>НАЗИВ ГЛАСАЧКОГ места</td><td>x</td><td>y</td></tr>"
            "<tr><td>2</td><td>ОШ Вук</td><td>Адр</td><td>Покр</td></tr>"
            "</table>"
        )
        rows = S2.rows_from_docx(html)
        assert [r[2] for r in rows] == ["ОШ Вук"]

    def test_running_seq_when_no_number_cell(self):
        # Empty number cell -> fall back to a running counter.
        html = (
            "<table>"
            "<tr><td></td><td>ОШ A</td><td>a</td><td>c1</td></tr>"
            "<tr><td></td><td>ОШ B</td><td>b</td><td>c2</td></tr>"
            "</table>"
        )
        rows = S2.rows_from_docx(html)
        assert [r[1] for r in rows] == [1, 2]

    def test_multi_cell_coverage_joined(self):
        html = (
            "<table>"
            "<tr><td>1</td><td>ОШ Вук</td><td>Адр</td><td>Прва 1-10</td><td>Друга 2-8</td></tr>"
            "</table>"
        )
        rows = S2.rows_from_docx(html)
        assert rows[0][4] == "Прва 1-10 Друга 2-8"

    def test_row_with_too_few_cells_ignored(self):
        html = "<table><tr><td>1</td><td>ОШ</td><td>Адр</td></tr></table>"
        assert S2.rows_from_docx(html) == []


class TestDeclaredCount:
    def test_count_regex_extracts_number(self):
        m = S2.COUNT_RE.search("одређује се 207 гласачких места")
        assert m and int(m.group(1)) == 207
