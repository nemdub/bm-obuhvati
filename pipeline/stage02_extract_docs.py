#!/usr/bin/env python3
"""Stage 02 — extract polling-station tables from the RIK Word documents.

Uses macOS ``textutil`` to read both legacy ``.doc`` (-> txt, linearized table) and
``.docx`` (-> html, table cells preserved). Classifies each file as base / amendment /
military, maps the filename to a register municipality, and emits:

  artifacts/stations.parquet        base polling stations (id, muni, number, name, address, coverage)
  artifacts/amendments_raw.parquet  raw text of amendment docs (parsed later in stage03b)
  artifacts/doc_municipality_map.csv filename -> matched municipality (review low-confidence rows)

Station id is deterministic: int(municipality_id) * 10000 + number, so it is stable
across re-runs (manual coverage edits link to it).

Usage:
  python3 stage02_extract_docs.py
  python3 stage02_extract_docs.py --files "Ada.doc,Bor-glasacka-mesta.docx"   # dev subset
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import unicodedata

import lxml.html
import polars as pl
from rapidfuzz import fuzz, process

import config
from common.transliterate import cyr_to_lat, nfc
from common.normalize import normalize_street

AMENDMENT_RE = re.compile(r"\b(izmena|izmene|dopuna|dopune|ispravka|ispravke)\b", re.IGNORECASE)
MILITARY_RE = re.compile(r"vojsk", re.IGNORECASE)
HEADER_HINT = "НАЗИВ ГЛАСАЧКОГ МЕСТА"
COUNT_RE = re.compile(r"одре[ђd]\w*\s+се\s+(\d+)\s+гласачк")
INT_LINE_RE = re.compile(r"^(\d+)\.?$")  # station number, optionally with a trailing period
# End of the polling-station table: the resolution's closing section ("II ..." with the
# "Ово решење доставити..." boilerplate, signatures, page markers). Without this, the last
# station in a .doc absorbs all trailing text (there is no next number to stop it).
TABLE_END_RE = re.compile(
    r"^(II|III|IV|V|VI)\.?$|^Ово\s+решењ|доставити\s+Републичк|ИЗБОРНА\s+КОМИСИЈА|ПРЕДСЕДНИК",
    re.IGNORECASE,
)
WS_RE = re.compile(r"\s+")


def textutil(path, fmt: str) -> str:
    """Convert a .doc/.docx to txt or html via macOS textutil."""
    res = subprocess.run(
        ["textutil", "-convert", fmt, "-stdout", str(path)],
        capture_output=True, text=True, check=True,
    )
    return res.stdout


def deaccent(s: str) -> str:
    """Drop diacritics so ASCII filenames ('Backa') match register names ('Bačka')."""
    s = s.replace("đ", "d").replace("Đ", "D")
    return "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c))


def clean_filename_to_candidate(name: str) -> str:
    """Reduce a doc filename to a bare municipality candidate string."""
    s = re.sub(r"\.(doc|docx)$", "", name, flags=re.IGNORECASE)
    s = re.sub(r"[-_ ]*glasacka[-_ ]*mesta", " ", s, flags=re.IGNORECASE)
    s = re.sub(
        r"\b(izmena|izmene|izmenama|dopuna|dopune|dopunama|ispravka|ispravke|ispravci|resenja|"
        r"resenje|i|o|odredjivanju)\b", " ", s, flags=re.IGNORECASE,
    )
    s = re.sub(r"^\d+_", "", s)  # strip dedup id prefix from the scraper
    return WS_RE.sub(" ", s).strip()


def collapse(s: str) -> str:
    return WS_RE.sub(" ", nfc(s)).strip()


# ── Table extraction ────────────────────────────────────────────────────────
_LEAD_INT = re.compile(r"^\s*(\d+)")


def rows_from_docx(html: str) -> list[tuple[None, int, str, str, str]]:
    """Parse station rows from textutil HTML. A data row has >=4 cells and a non-empty
    name. The number comes from the first cell when present; some documents render it as
    an auto-numbered list (no text), so we fall back to a running counter."""
    doc = lxml.html.fromstring(html)
    out: list[tuple[int, str, str, str]] = []
    seq = 0
    for tr in doc.xpath("//tr"):
        cells = [collapse(td.text_content()) for td in tr.xpath("./td")]
        if len(cells) < 4 or not cells[1].strip():
            continue
        joined = " ".join(cells).upper()
        if "НАЗИВ ГЛАСАЧКОГ" in joined or "ПОДРУЧЈЕ КОЈЕ" in joined:
            continue  # header row
        seq += 1
        m = _LEAD_INT.match(cells[0])
        num = int(m.group(1)) if m else seq
        coverage = " ".join(c for c in cells[3:] if c).strip()
        out.append((None, num, cells[1], cells[2], coverage))
    return out


def _header_start(lines: list[str]) -> int:
    """Index just after the table header (matches several wordings)."""
    for i, ln in enumerate(lines):
        if HEADER_HINT in ln or ("НАЗИВ" in ln and "МЕСТА" in ln):
            return i + 1
    return 0


SECTION_RE = re.compile(r"ГРАДСКА\s+ОПШТИНА\s+(.+)", re.IGNORECASE)


def rows_from_doc(txt: str, sections: dict[str, str] | None = None
                  ) -> list[tuple[str | None, int, str, str, str]]:
    """Parse station rows from linearized .doc text. Any lone integer line (optionally with
    a trailing period) after the header delimits a station; following lines are name /
    address / coverage. Returns (section_muni_id, number, name, address, coverage).

    `sections` maps a normalized 'ГРАДСКА ОПШТИНА <name>' section header to a municipality
    id; when given (a sectioned city doc, e.g. Niš), each station is tagged with its
    section's opstina so per-section numbering does not collide. Without it section_muni
    is None and the caller uses the document's municipality."""
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    # For sectioned docs start at the first section header (it precedes the table header,
    # which _header_start would otherwise skip past, dropping the first section).
    start = _header_start(lines)
    if sections is not None:
        for i, ln in enumerate(lines):
            sm = SECTION_RE.search(ln)
            if sm and sections.get(normalize_street(sm.group(1))):
                start = i
                break

    out: list[tuple[str | None, int, str, str, str]] = []
    cur: list[str] | None = None
    cur_num = 0
    cur_section: str | None = None

    def flush() -> None:
        if cur is None:
            return
        name = cur[0] if len(cur) >= 1 else ""
        address = cur[1] if len(cur) >= 2 else ""
        coverage = " ".join(cur[2:]).strip()
        out.append((cur_section, cur_num, name, address, coverage))

    for ln in lines[start:]:
        if cur is not None and TABLE_END_RE.search(ln):
            break  # closing section follows the table; only stop once inside it
        if sections is not None:
            sm = SECTION_RE.search(ln)
            if sm:
                muni = sections.get(normalize_street(sm.group(1)))
                if muni:  # a real sub-municipality header (not a venue named "ГРАДСКА ОПШТИНА …")
                    flush()
                    cur = None
                    cur_section = muni
                    continue
        m = INT_LINE_RE.match(ln)
        if m:
            flush()
            cur_num = int(m.group(1))
            cur = []
        elif cur is not None:
            cur.append(ln)
    flush()
    return out


def rows_from_doc_triplets(txt: str) -> list[tuple[None, int, str, str, str]]:
    """Fallback for .doc tables with no number column: group the lines after the header
    into (name, address, coverage) triplets and number them sequentially. Only used when
    the lone-integer parser finds nothing."""
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    start = _header_start(lines)
    body = lines[start:]
    for i, ln in enumerate(body):  # drop the closing section if present
        if TABLE_END_RE.search(ln):
            body = body[:i]
            break
    out: list[tuple[None, int, str, str, str]] = []
    for i in range(0, len(body) - 2, 3):
        out.append((None, i // 3 + 1, body[i], body[i + 1], body[i + 2]))
    return out


# ── Municipality mapping ────────────────────────────────────────────────────
def build_muni_matcher(munis: pl.DataFrame):
    choices = {row["id"]: deaccent(row["name_lat"]).upper() for row in munis.iter_rows(named=True)}
    rev = {v: k for k, v in choices.items()}
    names = list(choices.values())

    def match(candidate: str) -> tuple[str | None, str, float]:
        key = deaccent(candidate).upper()
        if key in rev:
            return rev[key], names[names.index(key)], 100.0
        best = process.extractOne(key, names, scorer=fuzz.WRatio)
        if best is None:
            return None, "", 0.0
        name, score, _ = best
        return rev[name], name, float(score)

    return match


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract polling-station tables from RIK docs.")
    ap.add_argument("--files", help="Comma-separated filenames to process (dev subset). Default: all.")
    args = ap.parse_args()

    config.ensure_artifacts()
    if not config.MUNICIPALITIES_PARQUET.exists():
        sys.exit("Run stage01 first (municipalities.parquet missing).")
    munis = pl.read_parquet(config.MUNICIPALITIES_PARQUET)
    match_muni = build_muni_matcher(munis)

    files = sorted(p for p in config.DOCS_DIR.iterdir() if p.suffix.lower() in (".doc", ".docx"))
    if args.files:
        wanted = {f.strip() for f in args.files.split(",")}
        files = [p for p in files if p.name in wanted]

    station_rows: list[dict] = []
    amend_rows: list[dict] = []
    map_rows: list[dict] = []
    muni_counter: dict[str, int] = {}

    for path in files:
        is_amend = bool(AMENDMENT_RE.search(path.name))
        is_military = bool(MILITARY_RE.search(path.name))
        candidate = clean_filename_to_candidate(path.name)
        override = config.DOC_MUNI_OVERRIDES.get(path.name)
        if override:
            muni_id, muni_name, score = match_muni(override)
        elif is_military:
            muni_id, muni_name, score = None, "(military)", 0.0
        else:
            muni_id, muni_name, score = match_muni(candidate)

        map_rows.append({
            "file": path.name, "candidate": candidate, "kind":
            "amendment" if is_amend else "military" if is_military else "base",
            "municipality_id": muni_id, "matched_name": muni_name, "score": round(score, 1),
        })

        if is_amend:
            amend_rows.append({
                "source_file": path.name, "municipality_id": muni_id,
                "raw_text": textutil(path, "txt"),
            })
            continue
        if is_military or muni_id is None:
            continue  # special docs / unmapped handled separately

        # Sectioned city docs (e.g. Niš) map "ГРАДСКА ОПШТИНА <name>" sections to opstine.
        section_map = {normalize_street(k): v for k, v in config.SECTIONED_DOCS.get(path.name, {}).items()}

        txt = textutil(path, "txt")
        if path.suffix.lower() == ".docx":
            rows = rows_from_docx(textutil(path, "html"))
        else:
            rows = rows_from_doc(txt, section_map or None)
            if not rows:  # number-less table -> triplet fallback
                rows = rows_from_doc_triplets(txt)

        declared = None
        m = COUNT_RE.search(txt)
        if m:
            declared = int(m.group(1))
        if declared is not None and declared != len(rows):
            print(f"  WARN {path.name}: declared {declared} stations, parsed {len(rows)}")

        for section_muni, num, name, address, coverage in rows:
            station_muni = section_muni or muni_id  # section's opstina, else the doc's
            muni_counter[station_muni] = muni_counter.get(station_muni, 0) + 1
            station_rows.append({
                # Stable unique id: municipality + per-municipality running index (the
                # printed number can restart per section, so it alone is not unique).
                "id": int(station_muni) * 100000 + muni_counter[station_muni],
                "municipality_id": station_muni,
                "number": num,
                "name_cyr": name,
                "name_lat": cyr_to_lat(name),
                "address_cyr": address,
                "address_lat": cyr_to_lat(address),
                "raw_coverage_text": coverage,
                "source_file": path.name,
                "is_amendment": 0,
            })

    stations = pl.DataFrame(station_rows) if station_rows else pl.DataFrame()
    stations.write_parquet(config.STATIONS_PARQUET)
    pl.DataFrame(amend_rows).write_parquet(config.AMENDMENTS_RAW_PARQUET)
    pl.DataFrame(map_rows).write_csv(config.DOC_MUNI_MAP)

    print(f"  files: {len(files)}  base stations: {stations.height:,}  amendment docs: {len(amend_rows)}")
    low = [r for r in map_rows if r["kind"] == "base" and r["score"] < 92]
    if low:
        print(f"  {len(low)} low-confidence municipality matches -> review {config.DOC_MUNI_MAP.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
