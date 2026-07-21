"""Tests for the standalone volunteer↔automated combine — see docs/parsing-matching/11-volunteer-merge.md.

Pure-function coverage of the two side scripts:
  - clean_volunteer_data: xlsx worksheet parse, KML placemark parse, station-number sourcing,
    the Cyrillic↔Latin filename join, and KMZ point-in-polygon number rescue.
  - combine_volunteer: the per-pair confidence guard and the per-file usability guard.

Geometry fixtures are small shapely boxes (mirrors test_voronoi.py); no parquet / network / file I/O.
"""

import json

from shapely.geometry import box

import clean_volunteer_data as clean
import combine_volunteer as comb
import compare_volunteer as cv
import config

# ---------------------------------------------------------------------------
# xlsx worksheet parsing
# ---------------------------------------------------------------------------

_SHEET_XML = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetData>
<row r="1"><c r="A1" t="str"><v>Broj BM</v></c><c r="B1" t="str"><v>Naziv</v></c><c r="C1" t="str"><v>Adresa</v></c><c r="D1" t="str"><v>Upisani birači</v></c><c r="E1" t="str"><v>Prioritet (1-9)</v></c></row>
<row r="2"><c r="A2"><v>1</v></c><c r="B2" t="str"><v>ОШ &quot;СВЕТИ САВА&quot;</v></c><c r="C2" t="str"><v>АРАНЂЕЛОВАЦ, НИКОЛЕ ПАШИЋА БР. 66</v></c><c r="D2"><v>1598</v></c><c r="E2"><v>7</v></c></row>
<row r="3"><c r="A3"><v>2</v></c><c r="B3" t="str"><v>ДОМ КУЛТУРЕ</v></c><c r="C3" t="str"><v>БУКОВИК</v></c><c r="D3"><v>842</v></c><c r="E3"><v>3</v></c></row>
</sheetData></worksheet>""".encode("utf-8")


def test_parse_sheet_xml_and_stations():
    rows = clean.parse_sheet_xml(_SHEET_XML)
    assert rows[0][:5] == ["Broj BM", "Naziv", "Adresa", "Upisani birači", "Prioritet (1-9)"]
    stations = clean.stations_from_rows(rows)
    assert set(stations) == {1, 2}
    assert stations[1]["name"] == 'ОШ "СВЕТИ САВА"'
    assert stations[1]["voters"] == 1598
    assert stations[1]["priority"] == 7
    assert stations[2]["address"] == "БУКОВИК"


def test_stations_from_rows_skips_blank_and_nonnumeric():
    rows = [["Broj BM", "Naziv"], [None, "x"], ["", "y"], ["3", "Z"]]
    assert set(clean.stations_from_rows(rows)) == {3}


# ---------------------------------------------------------------------------
# KML placemark parsing
# ---------------------------------------------------------------------------

_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Placemark>
  <name>ОШ X</name>
  <description>Biračko mesto br. 5
Adresa: NEKA, ULICA 1
Broj birača: 900
Prioritet: 4 (od 1 do 9)</description>
  <styleUrl>#novaPri4</styleUrl>
  <Point><coordinates>20.55,44.31,0</coordinates></Point>
  <ExtendedData>
    <Data name="br_bm"><value>5</value></Data>
    <Data name="adresa"><value>NEKA, ULICA 1</value></Data>
    <Data name="biraci"><value>900</value></Data>
    <Data name="prioritet"><value>4</value></Data>
  </ExtendedData>
</Placemark>
<Placemark>
  <name>Y</name>
  <description>Biračko mesto br. 6</description>
  <styleUrl>#novaPri9</styleUrl>
  <Point><coordinates>21.0,44.0,0</coordinates></Point>
</Placemark>
</Document></kml>""".encode("utf-8")


def test_parse_kml_extended_data_and_fallback():
    pms = clean.parse_kml(_KML)
    assert len(pms) == 2
    a = pms[0]
    assert a["number"] == 5 and a["priority"] == 4 and a["voters"] == 900
    assert a["point"] == [20.55, 44.31]
    assert a["address"] == "NEKA, ULICA 1"
    b = pms[1]  # no ExtendedData: number from description, priority from styleUrl
    assert b["number"] == 6 and b["priority"] == 9 and b["point"] == [21.0, 44.0]


# ---------------------------------------------------------------------------
# station-number sourcing + filename join
# ---------------------------------------------------------------------------

def test_unit_number():
    assert clean.unit_number({"BR_BM": 12}) == 12
    assert clean.unit_number({"BR_BM": "12"}) == 12
    assert clean.unit_number({"BR_BM": "", "puno_ime": "7 - OŠ NEKA"}) == 7
    assert clean.unit_number({"puno_ime": ""}) is None
    assert clean.unit_number({}) is None
    # "<RBR> - " with an empty name (Subotica) is the global index, not the station number → ignore
    assert clean.unit_number({"puno_ime": "1258 - "}) is None
    assert clean.unit_number({"puno_ime": "1258 -"}) is None


def test_feature_priority_reads_geojson_polygon():
    assert clean.feature_priority({"prioritet": 8}) == 8
    assert clean.feature_priority({"prioritet": "3"}) == 3
    assert clean.feature_priority({"PRIORITET": 5}) == 5
    assert clean.feature_priority({"BR_BM": 1}) is None  # no priority key
    assert clean.feature_priority({"prioritet": ""}) is None


def test_raw_feature_number_keeps_rbr():
    # unlike unit_number, raw_feature_number keeps the "<RBR> - " value so the scheme can be tested
    assert clean.raw_feature_number({"puno_ime": "1258 - "}) == 1258
    assert clean.raw_feature_number({"BR_BM": 5}) == 5
    assert clean.raw_feature_number({"puno_ime": ""}) is None


def test_geojson_scheme_wrong():
    valid = set(range(1, 103))  # Excel Broj BM 1..102
    # Subotica: geojson numbers are the global RBR (1256..) — none valid → wrong scheme
    assert clean.geojson_scheme_wrong([1256, 1257, 1258, 1300], valid) is True
    # normal file: numbers are 1..N, all valid → fine
    assert clean.geojson_scheme_wrong([1, 2, 3, 50], valid) is False
    # no numbers at all (rely on KMZ rescue) → not "wrong", just absent
    assert clean.geojson_scheme_wrong([], valid) is False
    # mostly-valid (above threshold) → fine
    assert clean.geojson_scheme_wrong([1, 2, 3, 999], valid) is False


def test_select_priority_precedence():
    # geojson polygon wins over kmz and xlsx
    assert clean.select_priority(8, 3, 5) == (8, "geojson")
    # no geojson → kmz station wins over xlsx
    assert clean.select_priority(None, 3, 5) == (3, "kmz")
    # only xlsx available (e.g. a kmz-less muni)
    assert clean.select_priority(None, None, 5) == (5, "xlsx")
    # nothing available
    assert clean.select_priority(None, None, None) == (None, "none")


def test_companion_key_joins_scripts_and_strips_suffix():
    # Latin geojson stem and Cyrillic xlsx/kmz stems collapse to one key.
    assert clean.companion_key("ARANĐELOVAC_2023") == clean.companion_key("АРАНЂЕЛОВАЦ_biracka_mesta")
    assert clean.companion_key("АРАНЂЕЛОВАЦ_lokacije") == clean.companion_key("ARANĐELOVAC_2023")
    # Nj digraph municipality
    assert clean.companion_key("VRANjE-VRANjE_2023") == clean.companion_key("ВРАЊЕ-ВРАЊЕ_biracka_mesta")


# ---------------------------------------------------------------------------
# KMZ point-in-polygon number rescue
# ---------------------------------------------------------------------------

def test_rescue_numbers_assigns_from_kmz_point():
    # Two number-less drawn units near Aranđelovac; a KMZ point inside the first carries number 5.
    u1 = cv.to_utm(box(20.50, 44.30, 20.51, 44.31))
    u2 = cv.to_utm(box(20.60, 44.30, 20.61, 44.31))
    units = {"#f0": {"number": None, "geom_utm": u1},
             "#f1": {"number": None, "geom_utm": u2}}
    kmz = [{"number": 5, "point": [20.505, 44.305]},
           {"number": 6, "point": [20.605, 44.305]}]
    rescued = clean.rescue_numbers(units, kmz)
    assert rescued == 2
    assert units["#f0"]["number"] == 5 and units["#f1"]["number"] == 6


# ---------------------------------------------------------------------------
# address verification (confirms a by-number station join against numbering drift)
# ---------------------------------------------------------------------------

def test_address_key_is_order_and_format_insensitive():
    # the register writes settlement-last, the Excel settlement-first with "БР."
    assert clean.address_key("Школска 8, Беочин") == clean.address_key("БЕОЧИН, ШКОЛСКА БР. 8")


def test_same_address_accepts_format_differences():
    assert clean.same_address("Насеље Дунав 104, Беочин", "БЕОЧИН, НАСЕЉЕ ДУНАВ БР. 104")
    assert clean.same_address("БОРЦИ, УЛ.БОРАЧКА БР 38.", "БОРЦИ, БОРАЧКА БР. 38")
    # homoglyph: register БОШЊAНЕ carries a Latin 'A'
    assert clean.same_address("БОШЊAНЕ, Деспота Стефана Високог бр. 172",
                              "БОШЊАНЕ, ДЕСПОТА СТЕФАНА ВИСОКОГ БР. 172")


def test_same_address_rejects_different_stations():
    # the Rača drift: register BM4 is Viševac, the Excel's BM4 is Veliko Krčmare
    assert not clean.same_address("ВИШЕВАЦ Вожда Карађорђа бр. 88а",
                                  "ВЕЛИКО КРЧМАРЕ, ВОЈВОДЕ ПАВЛА ЦУКИЋА БР. 30")
    # same street, different settlement must NOT pass
    assert not clean.same_address("БОБИШТЕ", "БРАТМИЛОВЦЕ, ТОМЕ КОСТИЋА БР. 1Е")
    assert not clean.same_address("", "БЕОЧИН, ШКОЛСКА БР. 8")


# ---------------------------------------------------------------------------
# volunteer STATION table (polygon-independent; feeds priority/voters onto automated munis)
# ---------------------------------------------------------------------------

def test_station_table_from_xlsx_with_kmz_priority():
    xlsx = {1: {"name": "A", "address": "adr1", "voters": 100, "priority": 5},
            2: {"name": "B", "address": "adr2", "voters": 200, "priority": 3}}
    kmz = [{"number": 1, "priority": 8, "name": "A", "address": None, "voters": None, "point": None}]
    tbl = clean.station_table(xlsx, kmz, "X_2023.geojson")
    assert [r["number"] for r in tbl] == [1, 2]
    # station 1: kmz priority wins over xlsx; station 2: xlsx (no kmz point)
    assert (tbl[0]["priority"], tbl[0]["priority_source"]) == (8, "kmz")
    assert (tbl[1]["priority"], tbl[1]["priority_source"]) == (3, "xlsx")
    assert tbl[0]["voters"] == 100 and tbl[1]["voters"] == 200


def test_station_table_ignores_rbr_numbered_kmz():
    # Lučani pattern: the kmz points are numbered with the global RBR, so they never match a Broj BM
    # and the xlsx priority must win (rather than a wrong point's value being picked up).
    xlsx = {1: {"name": "A", "address": None, "voters": 10, "priority": 4}}
    kmz = [{"number": 5751, "priority": 9, "name": None, "address": None, "voters": None, "point": None}]
    tbl = clean.station_table(xlsx, kmz, "LUČANI_2023.geojson")
    assert (tbl[0]["priority"], tbl[0]["priority_source"]) == (4, "xlsx")


# ---------------------------------------------------------------------------
# combine: whole-municipality source decision
# ---------------------------------------------------------------------------

def _vol(n):  # n usable volunteer units keyed arbitrarily
    return {f"u{i}": box(i, 0, i + 1, 1) for i in range(n)}


def _auto(n):
    return {i: box(i, 0, i + 1, 1) for i in range(n)}


def test_decide_source_complete_volunteer_wins():
    src, reason, comp = comb.decide_source(_vol(60), _auto(60), 60)
    assert src == "volunteer" and comp == 1.0


def test_decide_source_incomplete_falls_back_to_automatic():
    # KNJAŽEVAC pattern: 18 volunteer units for 65 stations = 28% < 70% threshold.
    src, reason, comp = comb.decide_source(_vol(18), _auto(65), 18)
    assert src == "automatic" and comp < config.VOL_MUNI_MIN_COMPLETENESS
    assert "incomplete" in reason


def test_decide_source_mislocated_file_is_automatic():
    # MIONICA pattern: volunteer units existed (n_vol_total>0) but all mislocated → none usable.
    src, reason, _ = comb.decide_source({}, _auto(5), 37)
    assert src == "automatic" and "mislocated" in reason


def test_decide_source_no_volunteer_data_is_automatic():
    # Municipality with no volunteer file at all → whole-automatic, distinct reason.
    src, reason, _ = comb.decide_source({}, _auto(50), 0)
    assert src == "automatic" and "no volunteer data" in reason


def test_decide_source_threshold_boundary():
    # exactly at the threshold counts as volunteer (>=), just below is automatic.
    n_auto = 10
    at = round(config.VOL_MUNI_MIN_COMPLETENESS * n_auto)  # 7
    assert comb.decide_source(_vol(at), _auto(n_auto), at)[0] == "volunteer"
    assert comb.decide_source(_vol(at - 1), _auto(n_auto), at - 1)[0] == "automatic"


# ---------------------------------------------------------------------------
# combine: attaching volunteer priority/voters onto automated polygons
# ---------------------------------------------------------------------------

def test_auto_props_attaches_when_address_confirms():
    meta = {"station_id": 8043800001, "name_cyr": "ОШ", "address_cyr": "СУБОТИЦА, ИВАНА ЗАЈЦА БР. 9"}
    st = {"number": 1, "priority": 5, "voters": 1475, "priority_source": "xlsx",
          "address": "СУБОТИЦА, ИВАНА ЗАЈЦА БР. 9"}
    p = comb._auto_props(meta, 1, "СУБОТИЦА", st)
    # geometry/identity stay register-sourced; only the volunteer attributes are attached
    assert p["source"] == "automatic" and p["station_id"] == 8043800001 and p["NAZIV_BM"] == "ОШ"
    assert p["priority"] == 5 and p["voters"] == 1475 and p["priority_source"] == "xlsx"


def test_auto_props_refuses_when_address_disagrees():
    # numbering drift: the volunteer row numbered 4 is a different station → do NOT attach
    meta = {"station_id": 1, "name_cyr": "ГИМНАЗИЈА", "address_cyr": "ЋУПРИЈА, КАРАЂОРЂЕВА БР. 57"}
    st = {"number": 4, "priority": 9, "voters": 800, "priority_source": "xlsx",
          "address": "ЋУПРИЈА, СВЕТОСАВСКА БР. 18"}
    p = comb._auto_props(meta, 4, "ЋУПРИЈА", st)
    assert p["priority"] is None and p["voters"] is None
    assert p["priority_source"] == "unverified" and p["vol_br_bm"] is None


def test_auto_props_without_volunteer_data_is_null():
    meta = {"station_id": 1, "name_cyr": "X", "address_cyr": "y"}
    p = comb._auto_props(meta, 1, "ADA", None)
    assert p["priority"] is None and p["voters"] is None and p["priority_source"] == "none"


def test_load_station_table_collision_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(comb, "STATIONS_DIR", tmp_path)
    # two volunteer files in one muni reusing Broj BM 1 (merged parent) → ambiguous → refuse to join
    (tmp_path / "70947.json").write_text(json.dumps([
        {"number": 1, "priority": 5, "source_file": "POŽAREVAC.geojson"},
        {"number": 1, "priority": 9, "source_file": "KOSTOLAC.geojson"},
    ]))
    assert comb.load_station_table("70947") == {}
    # single file → fine
    (tmp_path / "80438.json").write_text(json.dumps([
        {"number": 1, "priority": 5, "source_file": "SUBOTICA.geojson"},
    ]))
    assert comb.load_station_table("80438")[1]["priority"] == 5
    # no file at all
    assert comb.load_station_table("99999") == {}


# ---------------------------------------------------------------------------
# combine: per-file usability guard
# ---------------------------------------------------------------------------

def test_unusable_files_flags_mislocated():
    auto = {1: box(0, 0, 100, 100)}
    # good.geojson overlaps the automated cell; bad.geojson sits 10 km away.
    vol_utm = {"good#a": box(0, 0, 100, 100), "bad#a": box(10000, 10000, 10100, 10100)}
    rec = {"good#a": {"source_file": "good.geojson"}, "bad#a": {"source_file": "bad.geojson"}}
    bad = comb.unusable_files(vol_utm, rec, auto)
    assert bad == {"bad.geojson"}


def test_unusable_files_no_automated_keeps_manual_set_only():
    # With no automated polygons to validate against, only the manual escape hatch flags files.
    vol_utm = {"x#a": box(0, 0, 100, 100)}
    rec = {"x#a": {"source_file": "x.geojson"}}
    assert comb.unusable_files(vol_utm, rec, {}) == set(config.VOLUNTEER_UNUSABLE_FILES)
