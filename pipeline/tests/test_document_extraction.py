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

    def test_double_period_number_still_delimits(self):
        # Pančevo bug: station 23 was typed "23.." (two periods) and got merged into 22.
        # Any number of trailing periods must still start a new station.
        txt = "\n".join([
            HEADER,
            "22", "Дом 22", "Друга 2", "Друга 2-8",
            "23..", "ОШ Вук", "Прва 1", "Прва 1-10",
        ])
        rows = S2.rows_from_doc(txt)
        assert rows == [
            (None, 22, "Дом 22", "Друга 2", "Друга 2-8"),
            (None, 23, "ОШ Вук", "Прва 1", "Прва 1-10"),
        ]

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

    def test_quoted_name_continuation_merged(self):
        # A venue name wrapping onto a fully-quoted second line ('ДЕЧИЈИ ВРТИЋ' /
        # '``ДУШКО РАДОВИЋ``') merges into the name — NOT read as the address (which would
        # shove the real address into the coverage and mis-claim the whole town).
        txt = "\n".join([
            HEADER, "1",
            "ДЕЧИЈИ ВРТИЋ", "``ДУШКО РАДОВИЋ``",
            "ПОЖАРЕВАЦ, ПОЖАРЕВАЧКИ ОДРЕД ББ",
            "Алексе Галибарде, Боре Станковића",
        ])
        rows = S2.rows_from_doc(txt)
        assert rows == [(
            None, 1,
            "ДЕЧИЈИ ВРТИЋ ``ДУШКО РАДОВИЋ``",
            "ПОЖАРЕВАЦ, ПОЖАРЕВАЧКИ ОДРЕД ББ",
            "Алексе Галибарде, Боре Станковића",
        )]

    def test_quoted_address_not_merged(self):
        # A line that STARTS with a quote but carries address text after the closing quote
        # ('"КРАЉЕВИЦА" ББ, ЗАЈЕЧАР') is the address, not a name fragment — left in place.
        txt = "\n".join([
            HEADER, "1",
            'ВРТИЋ "ЂУРЂЕВАК"', '"КРАЉЕВИЦА" ББ, ЗАЈЕЧАР',
            "Љубе Нешића, Краљевица",
        ])
        rows = S2.rows_from_doc(txt)
        assert rows[0][2:] == ('ВРТИЋ "ЂУРЂЕВАК"', '"КРАЉЕВИЦА" ББ, ЗАЈЕЧАР', "Љубе Нешића, Краљевица")

    def test_multiline_coverage_not_treated_as_name(self):
        # A NON-quoted line after the name is the address; remaining lines are coverage —
        # a multi-line coverage row must stay correct (name=1 line, addr=1 line).
        txt = "\n".join([
            HEADER, "1",
            "ОСНОВНА ШКОЛА", "ЛУЧИЦА",
            "15. октобра 37-265", "2-58, Сеоско сокаче",
        ])
        rows = S2.rows_from_doc(txt)
        assert rows[0][2:] == ("ОСНОВНА ШКОЛА", "ЛУЧИЦА", "15. октобра 37-265 2-58, Сеоско сокаче")


class TestIsDualScriptDoc:
    """`_is_dual_script_doc` gates whether the HTML table may replace the txt parse for a
    .doc: it must NOT for dual-script docs, whose HTML cells still carry both scripts."""

    def _rows(self, *names):  # minimal (section, num, name, addr, cov) tuples
        return [(None, i + 1, nm, "addr", "cov") for i, nm in enumerate(names)]

    def test_doubled_name_cells_detected(self):
        txt = self._rows("ЛОКАЛ ХАМЗАГИЋ РЕШАДА", "ЛОКАЛ СМАИЛОВИЋ РАМИЗА")
        html = self._rows(
            "ЛОКАЛ ХАМЗАГИЋ РЕШАДА LOKAL HAMZAGIĆ REŠADA",
            "ЛОКАЛ СМАИЛОВИЋ РАМИЗА LOKAL SMAILOVIĆ RAMIZA",
        )
        assert S2._is_dual_script_doc(txt, html) is True

    def test_single_script_doc_not_flagged(self):
        # Identical name cells (no Latin twin) → safe to use the HTML columns.
        rows = self._rows("ОСНОВНА ШКОЛА", "МЕСНА ЗАЈЕДНИЦА")
        assert S2._is_dual_script_doc(rows, rows) is False


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


class TestSpecialAndOverrideDetection:
    @pytest.mark.parametrize("name,is_special", [
        ("resenje o odredjivanju glasackih mesta -inostranstvo.doc", True),
        ("Resenje o odredjivanju glasackih mesta u zavodima.doc", True),
        ("Resenje o odredjivanju glasackih mesta za vojsku.docx", False),  # caught by MILITARY_RE
        ("Pozarevac-glasacka-mesta.doc", False),
    ])
    def test_special_re(self, name, is_special):
        assert bool(S2.SPECIAL_RE.search(name)) is is_special

    @pytest.mark.parametrize("text,is_override", [
        ("... одређује се измена ... уместо:\n98\n...", True),         # Palilula block
        ("- редни број гласачког места 12 уместо:\n12", True),         # Čukarica per-station
        ("Стари назив гласачког места:\n59", True),                    # Aleksinac
        ("ред. бр. треба да стоји:", True),                            # Čukarica NEW marker
        ("одређује се 68 гласачких места на територији Града", False), # ordinary base preamble
    ])
    def test_override_body_re(self, text, is_override):
        assert bool(S2.OVERRIDE_BODY_RE.search(text)) is is_override


class TestSectionLabels:
    NAMES = {"70947": "ПОЖАРЕВАЦ", "71340": "КОСТОЛАЦ"}

    def _rows(self, numbers):
        # rows are (section_muni, number, name, address, coverage)
        return [(None, n, f"BM{n}", "addr", "cov") for n in numbers]

    def test_number_reset_splits_into_city_then_member(self, monkeypatch):
        # 1..3 (city) then 1..2 (Kostolac) → first block ПОЖАРЕВАЦ, second КОСТОЛАЦ.
        rows = self._rows([1, 2, 3, 1, 2])
        labels = S2.section_labels_for_rows(rows, "70947", self.NAMES)
        assert labels == ["ПОЖАРЕВАЦ", "ПОЖАРЕВАЦ", "ПОЖАРЕВАЦ", "КОСТОЛАЦ", "КОСТОЛАЦ"]

    def test_no_reset_single_table_unlabelled(self):
        # Continuous numbering (Vranje-style) → no member sub-table → no labels.
        rows = self._rows([1, 2, 3, 4])
        assert S2.section_labels_for_rows(rows, "70947", self.NAMES) == [None, None, None, None]

    def test_non_rep_muni_never_labelled(self):
        rows = self._rows([1, 2, 1])
        assert S2.section_labels_for_rows(rows, "80365", self.NAMES) == [None, None, None]
