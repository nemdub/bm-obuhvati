"""Clean + normalize the three volunteer data families into one per-station record set.

Each municipality in ``data/volunteer-data/`` arrives as up to three files:
  - ``<NAME>_2023.geojson``          (Latin filename)  — hand-drawn coverage polygons
  - ``<NAME_CYR>_biracka_mesta.xlsx`` (Cyrillic filename) — Broj BM, Naziv, Adresa, birači, Prioritet
  - ``<NAME_CYR>_lokacije.kmz``       (Cyrillic filename) — one point per station + priority

This module joins them (by municipality + station number), repairs geometry, rescues station
numbers for the 7 geojson files that carry none, and writes:
  - ``artifacts/volunteer-combined/normalized/<polygon_muni_id>.json`` — normalized unit records
  - ``artifacts/volunteer-combined/data_quality_report.md`` — every gap/issue found

The parsers (``parse_sheet_xml``, ``parse_kml``, ``unit_number``) are pure and unit-tested in
``tests/test_volunteer_combine.py``. Geometry helpers are reused from ``compare_volunteer``.
This is a standalone side tool — NOT a pipeline stage. See docs/parsing-matching/11-volunteer-merge.md.

Run: .venv/bin/python pipeline/clean_volunteer_data.py
"""

from __future__ import annotations

import json
import re
import zipfile
from collections import defaultdict
from pathlib import Path

from lxml import etree
from rapidfuzz import fuzz
from shapely.geometry import Point, shape
from shapely.ops import transform as shp_transform
from shapely.strtree import STRtree

import compare_volunteer as cv
import config
import map_volunteer_polygons as mvp
from common.normalize import _fold_homoglyphs
from common.transliterate import cyr_to_lat, nfc

VOL_DIR = config.VOLUNTEER_DATA_DIR
OUT_DIR = config.VOLUNTEER_COMBINE_DIR
NORM_DIR = config.VOLUNTEER_NORMALIZED_DIR
STATIONS_DIR = config.VOLUNTEER_STATIONS_DIR
DQ_REPORT = OUT_DIR / "data_quality_report.md"

_PUNO_IME_NUM = re.compile(r"^\s*(\d+)")
_DESC_NUM = re.compile(r"\b(?:br|бр)\.?\s*(\d+)", re.IGNORECASE)  # "Biračko mesto br. N" (Latin/Cyrillic)
_STYLE_PRI = re.compile(r"novaPri(\d+)")
_COL_LETTERS = re.compile(r"^([A-Z]+)")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _to_int(v) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(float(str(v).strip().replace(" ", "")))
    except (ValueError, TypeError):
        return None


def unit_number(props: dict) -> int | None:
    """Station number for a volunteer polygon feature: BR_BM, else the leading int of puno_ime.

    ``puno_ime`` is ``"<Broj BM> - <NAME>"``. A few files (e.g. Subotica) instead write ``"<RBR> - "``
    with an EMPTY name — there the leading number is the GLOBAL running index (RBR), not the local
    station number, so we ignore it and leave the unit number-less for the KMZ point-in-polygon rescue
    to fill with the real Broj BM."""
    bm = _to_int(props.get("BR_BM"))
    if bm is not None:
        return bm
    pi = str(props.get("puno_ime") or "")
    m = _PUNO_IME_NUM.match(pi)
    if m and pi[m.end():].lstrip(" -–—").strip():  # a real name follows → trust the number
        return int(m.group(1))
    return None


# geojson priority is written under a few key spellings across the export variants.
_PRIORITY_KEYS = ("prioritet", "Prioritet", "PRIORITET", "priority")


def feature_priority(props: dict) -> int | None:
    """Priority (1-9) written directly on a geojson polygon feature, if any."""
    for k in _PRIORITY_KEYS:
        v = _to_int(props.get(k))
        if v is not None:
            return v
    return None


def raw_feature_number(props: dict) -> int | None:
    """The number a geojson feature *claims* — BR_BM, else the leading int of puno_ime (even when the
    name is empty). Unlike ``unit_number`` this keeps a ``"<RBR> - "`` value, so we can test whether the
    file's numbering matches the authoritative Excel/KMZ set (see ``geojson_scheme_wrong``)."""
    bm = _to_int(props.get("BR_BM"))
    if bm is not None:
        return bm
    m = _PUNO_IME_NUM.match(str(props.get("puno_ime") or ""))
    return int(m.group(1)) if m else None


def geojson_scheme_wrong(raw_numbers: list[int], valid_numbers: set[int]) -> bool:
    """True when the geojson labels its polygons with numbers from a DIFFERENT scheme than the
    authoritative station list (Excel/KMZ ``Broj BM``). Subotica writes the global RBR (1256…) — none
    of which are valid ``Broj BM`` — so its polygon identities can't be trusted. Files with no numbers
    (rely on the KMZ rescue) are not 'wrong' — they're absent — so this returns False for them."""
    if not raw_numbers or not valid_numbers:
        return False
    in_range = sum(1 for n in raw_numbers if n in valid_numbers)
    return in_range / len(raw_numbers) < config.VOL_NUMBER_SCHEME_MIN_MATCH


def select_priority(geojson_pr, kmz_pr, xlsx_pr) -> tuple[int | None, str]:
    """Combine the priority sources: prefer the geojson POLYGON's own value (most specific), then the
    kmz STATION point, then the xlsx table. Returns (priority, source-name)."""
    for src, val in (("geojson", geojson_pr), ("kmz", kmz_pr), ("xlsx", xlsx_pr)):
        if val is not None:
            return val, src
    return None, "none"


# Tokens that carry no identifying information in a Serbian polling-station address.
_ADDR_STOP = {"БР", "БРОЈ", "УЛ", "УЛИЦА", "ББ", "НАСЕЉЕ", "НАСЕЉЕНО", "МЕСТО"}


def address_key(s: str) -> frozenset[str]:
    """Order-insensitive token set for an address.

    The register and the volunteer Excel write the same address differently — "Школска 8, Беочин"
    vs "БЕОЧИН, ШКОЛСКА БР. 8" — so comparing strings (or even sorted strings) gives false negatives.
    Folding to a token SET makes the comparison order-, case- and punctuation-independent;
    ``_fold_homoglyphs`` repairs Latin letters typed inside Cyrillic words (e.g. БОШЊAНЕ)."""
    s = _fold_homoglyphs(nfc(str(s or "")).upper())
    return frozenset(t for t in re.sub(r"[^\w\s]", " ", s).split() if t and t not in _ADDR_STOP)


def same_address(a: str, b: str) -> bool:
    """Do two addresses denote the same place? Used to CONFIRM a by-number station join.

    Exact token set, else a high Jaccard overlap (one side carries an extra word), else a fuzzy
    token-sort ratio (typos / abbreviations). Deliberately strict: a false positive attaches another
    station's priority, which is worse than leaving it null."""
    ta, tb = address_key(a), address_key(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    if len(ta & tb) / len(ta | tb) >= config.VOL_ADDR_JACCARD_MIN:
        return True
    return fuzz.token_sort_ratio(" ".join(sorted(ta)), " ".join(sorted(tb))) >= config.VOL_ADDR_FUZZ_MIN


def station_table(xlsx: dict[int, dict], kmz: list[dict], source_file: str) -> list[dict]:
    """The volunteer STATION list, keyed by the authoritative Excel ``Broj BM`` — independent of any
    polygon. This survives the polygon drops (wrong scheme / RBR), so a municipality that falls back to
    automated coverage can still carry the volunteer priority/voters, joined to the register by number.

    The kmz is only consulted where its point number IS a valid Broj BM (``kmz_by_num.get(n)``) — for
    files whose kmz carries the global RBR (Lučani) the lookup simply misses and the xlsx value wins."""
    kmz_by_num = {p["number"]: p for p in kmz if p["number"] is not None}
    out: list[dict] = []
    for n in sorted(xlsx):
        meta = xlsx[n]
        pt = kmz_by_num.get(n, {})
        priority, priority_source = select_priority(None, pt.get("priority"), meta.get("priority"))
        out.append({
            "number": n,
            "name": meta.get("name") or pt.get("name"),
            "address": meta.get("address") or pt.get("address"),
            "voters": meta.get("voters") if meta.get("voters") is not None else pt.get("voters"),
            "priority": priority,
            "priority_source": priority_source,
            "source_file": source_file,
        })
    return out


def _col_index(ref: str) -> int:
    """Spreadsheet cell ref -> 0-based column index (``B2`` -> 1)."""
    letters = _COL_LETTERS.match(ref or "")
    if not letters:
        return 0
    n = 0
    for ch in letters.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def parse_sheet_xml(xml: bytes) -> list[list[str | None]]:
    """Parse an xlsx worksheet XML into a list of rows (list of cell strings).

    These sheets store strings inline as ``<c t="str"><v>text</v></c>`` (no sharedStrings), so a
    cell value is simply its ``<v>`` text regardless of ``t``. Empty cells become ``None``."""
    root = etree.fromstring(xml)
    rows: list[list[str | None]] = []
    for row in root.iter("{*}row"):
        cells: dict[int, str | None] = {}
        for i, c in enumerate(row.iter("{*}c")):
            ref = c.get("r")
            ci = _col_index(ref) if ref else i
            v = c.find("{*}v")
            cells[ci] = v.text if v is not None else None
        width = (max(cells) + 1) if cells else 0
        rows.append([cells.get(i) for i in range(width)])
    return rows


def parse_kml(kml: bytes) -> list[dict]:
    """Parse KML placemarks -> [{number, point:[lon,lat], priority, name, address, voters}].

    Prefers the structured ``<ExtendedData>`` fields (br_bm/adresa/biraci/prioritet); falls back to
    the description text ("Biračko mesto br. N") and the ``styleUrl`` (#novaPriN) for priority."""
    root = etree.fromstring(kml)
    out: list[dict] = []
    for pm in root.iter("{*}Placemark"):
        ed: dict[str, str | None] = {}
        for data in pm.iter("{*}Data"):
            val = data.find("{*}value")
            ed[data.get("name")] = val.text if val is not None else None

        point = None
        coord_el = pm.find(".//{*}coordinates")
        if coord_el is not None and coord_el.text:
            parts = coord_el.text.strip().split(",")
            if len(parts) >= 2:
                try:
                    point = [float(parts[0]), float(parts[1])]
                except ValueError:
                    point = None

        num = _to_int(ed.get("br_bm"))
        if num is None:
            desc = pm.find("{*}description")
            if desc is not None and desc.text:
                m = _DESC_NUM.search(desc.text)
                if m:
                    num = int(m.group(1))

        pri = _to_int(ed.get("prioritet"))
        if pri is None:
            style = pm.find("{*}styleUrl")
            if style is not None and style.text:
                m = _STYLE_PRI.search(style.text)
                if m:
                    pri = int(m.group(1))

        name_el = pm.find("{*}name")
        out.append({
            "number": num,
            "point": point,
            "priority": pri,
            "name": name_el.text if name_el is not None else None,
            "address": ed.get("adresa"),
            "voters": _to_int(ed.get("biraci")),
        })
    return out


def resolve_xlsx_muni(stem: str, idx) -> dict:
    """Resolve a Cyrillic ``<NAME>_biracka_mesta`` stem to a register municipality.

    Many municipalities have ONLY an Excel (no drawn polygons). The municipality resolver works on the
    Latin geojson naming, so transliterate first and reuse it — that also inherits its handling of
    digraphs, ``CITY - DISTRICT`` splits and the Palilula collision."""
    base = stem.replace("_biracka_mesta", "")
    return mvp.resolve(f"{cyr_to_lat(base)}_2023.geojson", idx)


def companion_key(stem: str) -> str:
    """Normalized join key across the Latin geojson filename and Cyrillic xlsx/kmz filenames.

    Strips the family suffix, transliterates Cyrillic->Latin (identity for the already-Latin geojson
    stem), then applies the same ``_norm`` the municipality resolver uses. So
    ``ARANĐELOVAC_2023`` and ``АРАНЂЕЛОВАЦ_biracka_mesta`` both key to ``ARANĐELOVAC``."""
    for suffix in ("_2023", "_biracka_mesta", "_lokacije"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return mvp._norm(cyr_to_lat(stem))


# ---------------------------------------------------------------------------
# File readers (I/O)
# ---------------------------------------------------------------------------

def stations_from_rows(rows: list[list[str | None]]) -> dict[int, dict]:
    """Map worksheet rows (cols: Broj BM, Naziv, Adresa, birači, Prioritet) -> {number: fields}."""
    out: dict[int, dict] = {}
    for r in rows[1:]:  # skip header
        if not r:
            continue
        num = _to_int(r[0])
        if num is None:
            continue
        out[num] = {
            "name": r[1] if len(r) > 1 else None,
            "address": r[2] if len(r) > 2 else None,
            "voters": _to_int(r[3]) if len(r) > 3 else None,
            "priority": _to_int(r[4]) if len(r) > 4 else None,
        }
    return out


def parse_xlsx(path: Path) -> dict[int, dict]:
    """Read a ``*_biracka_mesta.xlsx`` -> {station number: {name, address, voters, priority}}."""
    with zipfile.ZipFile(path) as z:
        xml = z.read("xl/worksheets/sheet1.xml")
    return stations_from_rows(parse_sheet_xml(xml))


def parse_kmz(path: Path) -> list[dict]:
    """Read a ``*_lokacije.kmz`` (zip of doc.kml) -> list of placemark dicts (see parse_kml)."""
    with zipfile.ZipFile(path) as z:
        name = next((n for n in z.namelist() if n.lower().endswith(".kml")), None)
        if name is None:
            return []
        kml = z.read(name)
    return parse_kml(kml)


def load_geojson_units(path: Path) -> dict[str, dict]:
    """Load one volunteer geojson into station units (dissolving features that share a number).

    Returns {unit_key: {number, geom_utm, geom_wgs}} in both CRSs, geometry repaired, nulls skipped.
    unit_key is ``#bm<n>`` when a number is known (BR_BM or puno_ime), else ``#f<i>``."""
    feats = json.loads(path.read_text()).get("features", []) or []
    is_utm = cv._file_is_utm(feats)
    raw_utm: dict[str, list] = defaultdict(list)
    raw_wgs: dict[str, list] = defaultdict(list)
    numbers: dict[str, int | None] = {}
    priorities: dict[str, list[int]] = defaultdict(list)
    for i, feat in enumerate(feats):
        geom = feat.get("geometry")
        if not geom:
            continue
        g = cv.clean(shape(geom))
        if g.is_empty:
            continue
        g_utm = g if is_utm else cv.to_utm(g)
        g_wgs = cv.clean(shp_transform(cv._to_wgs, g)) if is_utm else g
        props = feat.get("properties") or {}
        num = unit_number(props)
        key = f"#bm{num}" if num is not None else f"#f{i}"
        raw_utm[key].append(g_utm)
        raw_wgs[key].append(g_wgs)
        numbers[key] = num
        pr = feature_priority(props)
        if pr is not None:
            priorities[key].append(pr)
    from shapely.ops import unary_union
    units: dict[str, dict] = {}
    for key in raw_utm:
        gu = raw_utm[key]
        gw = raw_wgs[key]
        # priority written on the polygon(s); if a dissolved unit's polygons disagree take the max.
        prio = max(priorities[key]) if priorities[key] else None
        units[key] = {
            "number": numbers[key],
            "priority": prio,
            "geom_utm": gu[0] if len(gu) == 1 else cv.clean(unary_union(gu)),
            "geom_wgs": gw[0] if len(gw) == 1 else cv.clean(unary_union(gw)),
        }
    return units


def rescue_numbers(units: dict[str, dict], kmz_points: list[dict]) -> int:
    """Assign a station number to number-less units by point-in-polygon of the KMZ points.

    The 7 geojson files with no BR_BM/puno_ime still have a KMZ; each KMZ point carries the station
    number, so the unit that contains it inherits that number. Returns how many units were rescued."""
    numberless = {k: u for k, u in units.items() if u["number"] is None}
    if not numberless:
        return 0
    keys = list(numberless)
    tree = STRtree([numberless[k]["geom_utm"] for k in keys])
    rescued = 0
    for p in kmz_points:
        if p["number"] is None or p["point"] is None:
            continue
        pt = shp_transform(cv._to_utm, Point(p["point"][0], p["point"][1]))
        for j in tree.query(pt, predicate="intersects"):
            k = keys[j]
            if units[k]["number"] is None:
                units[k]["number"] = p["number"]
                rescued += 1
    return rescued


# ---------------------------------------------------------------------------
# Per-file normalization + data-quality collection
# ---------------------------------------------------------------------------

def _find_companions() -> tuple[dict[str, Path], dict[str, Path]]:
    """Index the xlsx and kmz files by companion_key so a geojson file can find its pair."""
    xlsx = {companion_key(p.stem): p for p in VOL_DIR.glob("*_biracka_mesta.xlsx")}
    kmz = {companion_key(p.stem): p for p in VOL_DIR.glob("*_lokacije.kmz")}
    return xlsx, kmz


def normalize_file(gj_path: Path, xlsx_path: Path | None, kmz_path: Path | None,
                   muni_id: str | None, polygon_muni_id: str | None
                   ) -> tuple[list[dict], dict, list[dict]]:
    """Normalize one geojson file (+ companions) -> (unit records, data-quality summary, station table)."""
    units = load_geojson_units(gj_path)
    xlsx = parse_xlsx(xlsx_path) if xlsx_path else {}
    kmz = parse_kmz(kmz_path) if kmz_path else []
    rescued = rescue_numbers(units, kmz) if kmz else 0
    kmz_by_num = {p["number"]: p for p in kmz if p["number"] is not None}

    raw_feats = json.loads(gj_path.read_text()).get("features", []) or []
    null_geoms = sum(1 for f in raw_feats if not f.get("geometry"))
    has_brbm = any((f.get("properties") or {}).get("BR_BM") not in (None, "") for f in raw_feats)

    # The authoritative station numbers are the Excel `Broj BM` (the KMZ point number field is
    # sometimes the global RBR, so it is NOT trusted as ground truth). A volunteer polygon is only
    # usable if we can attribute it to a real station, i.e. its resolved number is a valid Broj BM.
    valid_nums = set(xlsx)
    # File-level: the geojson labels its polygons with a WRONG scheme (Subotica writes the RBR on every
    # feature) — flag the whole file so the muni falls back to automated with a precise reason.
    raw_nums = [n for n in (raw_feature_number(f.get("properties") or {}) for f in raw_feats)
                if n is not None]
    scheme_wrong = geojson_scheme_wrong(raw_nums, valid_nums)
    manual_unusable = gj_path.name in config.VOLUNTEER_UNUSABLE_FILES
    file_reason = ("geojson numbering doesn't match Excel/KMZ (wrong scheme)" if scheme_wrong
                   else "manually flagged unusable" if manual_unusable else None)

    records: list[dict] = []
    prio_src = {"geojson": 0, "kmz": 0, "xlsx": 0, "none": 0}
    n_bad_number = 0
    for key, u in sorted(units.items()):
        num = u["number"]
        meta = xlsx.get(num, {}) if num is not None else {}
        pt = kmz_by_num.get(num, {}) if num is not None else {}
        # Priority is written in different places per file: on the geojson POLYGON, in the kmz for the
        # STATION point, and/or in the xlsx. Use whichever is available (see select_priority).
        priority, priority_source = select_priority(
            u.get("priority"), pt.get("priority"), meta.get("priority"))
        prio_src[priority_source] += 1
        # Per-unit usability: the file must be trustworthy AND this polygon must resolve to a real
        # Broj BM. Number-less polygons and ones carrying a global RBR (Lučani/Kosjerić) are dropped.
        number_valid = num is not None and num in valid_nums
        if not number_valid:
            n_bad_number += 1
        reason = file_reason or (None if number_valid
                                 else "polygon has no valid Broj BM (unresolved / global RBR)")
        records.append({
            "muni_id": muni_id,
            "polygon_muni_id": polygon_muni_id,
            "source_file": gj_path.name,
            "unit_key": key,
            "number": num,
            "name": meta.get("name") or pt.get("name"),
            "address": meta.get("address") or pt.get("address"),
            "voters": meta.get("voters") if meta.get("voters") is not None else pt.get("voters"),
            "priority": priority,
            "priority_source": priority_source,
            "point": pt.get("point"),
            "geometry_wgs": u["geom_wgs"].__geo_interface__,
            "area_m2": u["geom_utm"].area,
            "usable": reason is None,
            "unusable_reason": reason,
        })

    n_units = len(units)
    numberless = sum(1 for u in units.values() if u["number"] is None)
    dq = {
        "file": gj_path.name,
        "muni_id": muni_id,
        "polygon_muni_id": polygon_muni_id,
        "n_geojson_features": len(raw_feats),
        "n_units": n_units,
        "n_null_geoms": null_geoms,
        "has_brbm": has_brbm,
        "n_numberless_units": numberless,
        "n_rescued": rescued,
        "has_xlsx": xlsx_path is not None,
        "has_kmz": kmz_path is not None,
        "n_xlsx_rows": len(xlsx),
        "n_kmz_points": len(kmz),
        "n_no_point": sum(1 for r in records if r["point"] is None),
        "is_utm": cv._file_is_utm(raw_feats),
        "has_geojson": True,
        "n_usable": sum(1 for r in records if r["usable"]),
        "n_bad_number": n_bad_number,
        "scheme_wrong": scheme_wrong,
        "file_reason": file_reason,
        "priority_from_geojson": prio_src["geojson"],
        "priority_from_kmz": prio_src["kmz"],
        "priority_from_xlsx": prio_src["xlsx"],
        "priority_missing": prio_src["none"],
    }
    return records, dq, station_table(xlsx, kmz, gj_path.name)


# ---------------------------------------------------------------------------
# Report + driver
# ---------------------------------------------------------------------------

def write_dq_report(dqs: list[dict]) -> None:
    L: list[str] = []
    L.append("# Volunteer data — data-quality report\n")
    L.append(f"Source: `{VOL_DIR.relative_to(config.ROOT_DIR)}` — {len(dqs)} volunteer files processed.\n")

    # geojson-specific findings only apply to files that HAVE a geojson (many munis are Excel-only)
    geo = [d for d in dqs if d.get("has_geojson")]
    xlsx_only = [d for d in dqs if not d.get("has_geojson") and not d.get("unmapped")]
    null_files = [d for d in geo if d["n_null_geoms"]]
    no_brbm = [d for d in geo if not d["has_brbm"]]
    no_kmz = [d for d in geo if not d["has_kmz"]]
    no_xlsx = [d for d in geo if not d["has_xlsx"]]
    utm_files = [d for d in geo if d["is_utm"]]
    scheme_files = [d for d in geo if d.get("scheme_wrong")]
    bad_num_files = [d for d in geo if d.get("n_bad_number")]

    L.append("## Summary\n")
    L.append(f"- Volunteer files: **{len(geo)}** with drawn polygons (geojson), "
             f"**{len(xlsx_only)}** Excel-only (station list without coverage polygons).")
    L.append(f"- Files with null/empty geometries: **{len(null_files)}** "
             f"({sum(d['n_null_geoms'] for d in null_files)} features).")
    L.append(f"- Files with no BR_BM on any feature: **{len(no_brbm)}** "
             f"(number rescued from KMZ where available).")
    L.append(f"- Municipalities missing a KMZ (no point/priority source): **{len(no_kmz)}**.")
    L.append(f"- Files missing an xlsx companion: **{len(no_xlsx)}**.")
    L.append(f"- Files in UTM34N (reprojected on load): **{len(utm_files)}**.")
    L.append(f"- Files with a wrong geojson numbering scheme (whole file dropped): **{len(scheme_files)}**.")
    L.append(f"- Polygons dropped for no valid Broj BM (unresolved / global RBR): "
             f"**{sum(d['n_bad_number'] for d in dqs)}** across **{len(bad_num_files)}** files.\n")
    if scheme_files:
        L.append("### Wrong numbering scheme (geojson numbers ≠ Excel Broj BM → automated fallback)\n")
        for d in scheme_files:
            L.append(f"- {d['file']} — {d.get('file_reason')}")
        L.append("")
    if bad_num_files:
        L.append("### Polygons without a valid Broj BM (dropped; RBR-numbered or unresolved)\n")
        L.append("| file | dropped | usable | units |")
        L.append("|---|---|---|---|")
        for d in sorted(bad_num_files, key=lambda d: -d["n_bad_number"]):
            L.append(f"| {d['file'].replace('_2023.geojson','')} | {d['n_bad_number']} | "
                     f"{d['n_usable']} | {d['n_units']} |")
        L.append("")

    # Priority is sourced from whichever of geojson-polygon / kmz-station / xlsx has it (in that order).
    pg = sum(d.get("priority_from_geojson", 0) for d in dqs)
    pk = sum(d.get("priority_from_kmz", 0) for d in dqs)
    px = sum(d.get("priority_from_xlsx", 0) for d in dqs)
    pm = sum(d.get("priority_missing", 0) for d in dqs)
    L.append("### Priority source (per volunteer unit)\n")
    L.append(f"- From the **geojson polygon** (`prioritet`): **{pg}**")
    L.append(f"- From the **kmz** station point: **{pk}**")
    L.append(f"- From the **xlsx** table (fallback): **{px}**")
    L.append(f"- **Missing** (no source had it): **{pm}**")
    miss = [d for d in dqs if d.get("priority_missing")]
    if miss:
        L.append("  - units without any priority, by file: "
                 + ", ".join(f"{d['file'].replace('_2023.geojson','')} ({d['priority_missing']})"
                             for d in sorted(miss, key=lambda d: -d["priority_missing"])))
    L.append("")

    if null_files:
        L.append("### Null/empty geometries\n")
        L.append("| file | null features | units kept |")
        L.append("|---|---|---|")
        for d in sorted(null_files, key=lambda d: -d["n_null_geoms"]):
            L.append(f"| {d['file']} | {d['n_null_geoms']} | {d['n_units']} |")
        L.append("")

    if no_brbm:
        L.append("### Files without BR_BM (number rescued from KMZ point-in-polygon)\n")
        L.append("| file | units | number-less after rescue | rescued |")
        L.append("|---|---|---|---|")
        for d in sorted(no_brbm, key=lambda d: d["file"]):
            L.append(f"| {d['file']} | {d['n_units']} | {d['n_numberless_units']} | {d['n_rescued']} |")
        L.append("")

    if no_kmz:
        L.append("### Municipalities missing a KMZ\n")
        L.append(", ".join(sorted(d["file"] for d in no_kmz)) + "\n")

    # count mismatches: geojson units vs xlsx rows vs kmz points
    mism = [d for d in dqs
            if len({d["n_units"], d["n_xlsx_rows"], d["n_kmz_points"]} - {0}) > 1]
    if mism:
        L.append("### Count mismatches (geojson units / xlsx rows / kmz points)\n")
        L.append("| file | units | xlsx rows | kmz points | units w/o point |")
        L.append("|---|---|---|---|---|")
        for d in sorted(mism, key=lambda d: abs(d["n_units"] - d["n_kmz_points"]), reverse=True):
            L.append(f"| {d['file']} | {d['n_units']} | {d['n_xlsx_rows']} | "
                     f"{d['n_kmz_points']} | {d['n_no_point']} |")
        L.append("")

    if utm_files:
        L.append("### UTM34N files (reprojected)\n")
        L.append(", ".join(sorted(d["file"] for d in utm_files)) + "\n")

    DQ_REPORT.write_text("\n".join(L))


def main() -> None:
    NORM_DIR.mkdir(parents=True, exist_ok=True)
    STATIONS_DIR.mkdir(parents=True, exist_ok=True)
    idx = mvp.load_register_index()
    xlsx_idx, kmz_idx = _find_companions()

    by_pid: dict[str, list[dict]] = defaultdict(list)
    stations_by_pid: dict[str, list[dict]] = defaultdict(list)
    dqs: list[dict] = []
    for gj in sorted(VOL_DIR.glob("*_2023.geojson")):
        res = mvp.resolve(gj.name, idx)
        key = companion_key(gj.stem)
        recs, dq, stations = normalize_file(
            gj, xlsx_idx.get(key), kmz_idx.get(key),
            res["muni_id"], res["polygon_muni_id"],
        )
        dqs.append(dq)
        pid = res["polygon_muni_id"] or f"UNMAPPED:{gj.name}"
        by_pid[pid].extend(recs)
        stations_by_pid[pid].extend(stations)

    # Municipalities with an Excel but NO drawn polygons: volunteers surveyed the stations without
    # mapping coverage. They have no polygons to contribute (so they stay `automatic`), but their
    # priority/voters can still be attached to our automated polygons — build their station table.
    gj_keys = {companion_key(p.stem) for p in VOL_DIR.glob("*_2023.geojson")}
    for xp in sorted(VOL_DIR.glob("*_biracka_mesta.xlsx")):
        key = companion_key(xp.stem)
        if key in gj_keys:
            continue
        res = resolve_xlsx_muni(xp.stem, idx)
        pid = res["polygon_muni_id"]
        if not pid:
            dqs.append({"file": xp.name, "muni_id": None, "polygon_muni_id": None,
                        "has_geojson": False, "has_xlsx": True, "has_kmz": False,
                        "n_xlsx_rows": 0, "n_kmz_points": 0, "unmapped": True})
            continue
        xlsx = parse_xlsx(xp)
        kmz_path = kmz_idx.get(key)
        kmz = parse_kmz(kmz_path) if kmz_path else []
        stations_by_pid[pid].extend(station_table(xlsx, kmz, xp.name))
        dqs.append({
            "file": xp.name, "muni_id": res["muni_id"], "polygon_muni_id": pid,
            "has_geojson": False, "has_xlsx": True, "has_kmz": kmz_path is not None,
            "n_xlsx_rows": len(xlsx), "n_kmz_points": len(kmz),
            "n_geojson_features": 0, "n_units": 0, "n_null_geoms": 0, "has_brbm": False,
            "n_numberless_units": 0, "n_rescued": 0, "n_no_point": 0, "is_utm": False,
            "n_usable": 0, "n_bad_number": 0, "scheme_wrong": False, "file_reason": None,
            "priority_from_geojson": 0, "priority_from_kmz": 0,
            "priority_from_xlsx": sum(1 for v in xlsx.values() if v["priority"] is not None),
            "priority_missing": sum(1 for v in xlsx.values() if v["priority"] is None),
        })

    for pid, recs in by_pid.items():
        if pid.startswith("UNMAPPED:"):
            continue
        (NORM_DIR / f"{pid}.json").write_text(json.dumps(recs, ensure_ascii=False))
    for pid, sts in stations_by_pid.items():
        if pid.startswith("UNMAPPED:"):
            continue
        (STATIONS_DIR / f"{pid}.json").write_text(json.dumps(sts, ensure_ascii=False))

    write_dq_report(dqs)
    (OUT_DIR / "data_quality.json").write_text(json.dumps(dqs, ensure_ascii=False))
    total_units = sum(len(r) for r in by_pid.values())
    print(f"Normalized {len(dqs)} volunteer files -> {total_units} station units "
          f"across {len([k for k in by_pid if not k.startswith('UNMAPPED:')])} municipalities.")
    print(f"  records  -> {NORM_DIR.relative_to(config.ROOT_DIR)}/<polygon_muni_id>.json")
    print(f"  stations -> {STATIONS_DIR.relative_to(config.ROOT_DIR)}/<polygon_muni_id>.json "
          f"({sum(len(s) for s in stations_by_pid.values())} volunteer stations)")
    print(f"  report   -> {DQ_REPORT.relative_to(config.ROOT_DIR)}")


if __name__ == "__main__":
    main()
