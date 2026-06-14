"""Map the volunteer-drawn coverage files to our canonical municipality register.

The files in ``data/volunteer-polygons/*.geojson`` are named ``<MUNICIPALITY>_2023.geojson``
in Latin script, but the naming is inconsistent with our register: city districts arrive as
``CITY - DISTRICT`` (``NIŠ_-_PALILULA``, ``UŽICE-SEVOJNO``), digraphs are spelled mixed-case
(``VRANjE``), and three districts (Kostolac, Sevojno, Vranjska Banja) have no automated polygons
of their own — the pipeline folds their stations into the parent city.

This script resolves every file to a register municipality (``municipalities.parquet``) and writes
a reviewable mapping table ``artifacts/volunteer-compare/mapping.csv``. That CSV is the contract the
comparison (``compare_volunteer.py``) reads, and the hand-editable seed of a future ingest path.

The resolver is pure (``build_register_index`` + ``resolve``) so it can be unit-tested without the
parquet — see ``tests/test_volunteer_mapping.py``. The matching heuristic is documented in
``docs/parsing-matching/09-volunteer-mapping.md``.

Run: .venv/bin/python pipeline/map_volunteer_polygons.py
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

import config
from common.transliterate import nfc

VOLUNTEER_DIR = config.DATA_DIR / "volunteer-polygons"
OUT_DIR = config.ARTIFACTS_DIR / "volunteer-compare"
MAPPING_CSV = OUT_DIR / "mapping.csv"
R2_POLY_DIR = config.ARTIFACTS_DIR / "r2" / "polygons" / "m"

_PAREN_RE = re.compile(r"\s*\(.*\)\s*")


def _norm(s: str) -> str:
    """Filename/register normal form: NFC, ``_``->space, collapse whitespace, uppercase.

    Python ``str.upper`` folds the Latin digraphs correctly (``VRANjE`` -> ``VRANJE``)."""
    return re.sub(r"\s+", " ", nfc(s).replace("_", " ")).strip().upper()


@dataclass
class RegisterIndex:
    """Lookup over the municipality register, keyed by parenthetical-stripped base name."""

    by_base: dict[str, list[dict]] = field(default_factory=dict)
    parent_of: dict[str, str | None] = field(default_factory=dict)
    has_polygons: set[str] = field(default_factory=set)


def build_register_index(muni_rows: list[dict], polygon_muni_ids: set[str]) -> RegisterIndex:
    """Build the resolver index from municipality rows + the set of muni ids that ship polygons.

    ``muni_rows``: dicts with ``id``, ``name_lat``, ``parent_id``.
    ``polygon_muni_ids``: muni ids (str) that have an R2 polygon file.
    """
    idx = RegisterIndex(has_polygons=set(polygon_muni_ids))
    for r in muni_rows:
        mid = str(r["id"])
        name = _norm(r["name_lat"])
        base = _PAREN_RE.sub("", name).strip()
        paren = None
        mt = re.search(r"\((.*)\)", name)
        if mt:
            paren = mt.group(1).strip()
        idx.by_base.setdefault(base, []).append(
            {"id": mid, "name_lat": r["name_lat"], "paren": paren}
        )
        idx.parent_of[mid] = str(r["parent_id"]) if r["parent_id"] is not None else None
    return idx


def split_prefix(stem: str) -> tuple[str | None, str]:
    """Split a ``CITY - DISTRICT`` stem into ``(prefix, core)``.

    Volunteers use ``_-_`` (Niš, Požarevac) or a bare ``-`` (Užice, Vranje) as the separator.
    Plain municipality files have no separator -> ``(None, stem)``."""
    if "_-_" in stem:
        prefix, core = stem.split("_-_", 1)
        return prefix, core
    if "-" in stem:
        prefix, core = stem.split("-", 1)
        return prefix, core
    return None, stem


def resolve(filename: str, idx: RegisterIndex) -> dict:
    """Resolve a volunteer filename to a register municipality.

    Returns a dict with: muni_id, muni_name_lat, polygon_muni_id, prefix, core, method, status.
    ``polygon_muni_id`` is the muni whose R2 polygon file holds these stations — the resolved
    muni itself, or its parent when it is a child city-district with no polygons of its own.
    ``muni_id``/``polygon_muni_id`` are None when unmatched (status ``unmatched``).
    """
    stem = Path(filename).stem
    stem = re.sub(r"_2023$", "", stem)
    prefix, core = split_prefix(stem)
    core_n = _norm(core)
    prefix_n = _norm(prefix) if prefix else None

    cands = idx.by_base.get(core_n, [])
    chosen: dict | None = None
    method = "exact"
    if len(cands) == 1:
        chosen = cands[0]
    elif len(cands) > 1:
        # Ambiguous base (PALILULA -> Beograd vs Niš): disambiguate by the city prefix's
        # parenthetical; a bare ambiguous name falls back to the (BEOGRAD) candidate.
        method = "ambiguous_resolved"
        if prefix_n:
            for c in cands:
                p = c["paren"]
                if p and (p == prefix_n or prefix_n.startswith(p) or p.startswith(prefix_n)):
                    chosen = c
                    break
        if chosen is None:
            chosen = next((c for c in cands if c["paren"] == "BEOGRAD"), cands[0])

    if chosen is None:
        return {
            "muni_id": None, "muni_name_lat": None, "polygon_muni_id": None,
            "prefix": prefix_n, "core": core_n, "method": "none", "status": "unmatched",
        }

    mid = chosen["id"]
    status = "ok" if method == "exact" else method
    polygon_mid = mid
    if mid not in idx.has_polygons:
        parent = idx.parent_of.get(mid)
        if parent and parent in idx.has_polygons:
            polygon_mid = parent
            status = "child_go_merged_to_parent"
        else:
            status = "no_polygons"
    return {
        "muni_id": mid, "muni_name_lat": chosen["name_lat"], "polygon_muni_id": polygon_mid,
        "prefix": prefix_n, "core": core_n, "method": method, "status": status,
    }


def _feature_stats(path: Path) -> tuple[int, int]:
    """(feature count, features carrying a usable BR_BM)."""
    gj = json.loads(path.read_text())
    feats = gj.get("features", []) or []
    n_brbm = sum(
        1 for f in feats
        if (f.get("properties") or {}).get("BR_BM") not in (None, "")
    )
    return len(feats), n_brbm


def load_register_index() -> RegisterIndex:
    muni = pl.read_parquet(config.MUNICIPALITIES_PARQUET)
    rows = muni.select(["id", "name_lat", "parent_id"]).to_dicts()
    r2_ids = {p.stem for p in R2_POLY_DIR.glob("*.json")}
    return build_register_index(rows, r2_ids)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    idx = load_register_index()

    rows = []
    for path in sorted(VOLUNTEER_DIR.glob("*.geojson")):
        res = resolve(path.name, idx)
        n_feats, n_brbm = _feature_stats(path)
        rows.append({
            "file": path.name,
            "prefix": res["prefix"] or "",
            "core": res["core"],
            "muni_id": res["muni_id"] or "",
            "muni_name_lat": res["muni_name_lat"] or "",
            "polygon_muni_id": res["polygon_muni_id"] or "",
            "n_features": n_feats,
            "n_with_brbm": n_brbm,
            "method": res["method"],
            "status": res["status"],
        })

    cols = ["file", "prefix", "core", "muni_id", "muni_name_lat", "polygon_muni_id",
            "n_features", "n_with_brbm", "method", "status"]
    with MAPPING_CSV.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    print(f"Mapped {len(rows)} volunteer files -> {MAPPING_CSV.relative_to(config.ROOT_DIR)}")
    print("Status counts:", by_status)
    flagged = [r for r in rows if r["status"] not in ("ok",)]
    if flagged:
        print("\nNon-trivial rows (review):")
        for r in flagged:
            print(f"  {r['file']:<36} {r['status']:<26} muni={r['muni_id']} "
                  f"polygons_under={r['polygon_muni_id']} ({r['muni_name_lat']})")
    unmatched = [r for r in rows if r["status"] == "unmatched"]
    if unmatched:
        print(f"\nWARNING: {len(unmatched)} unmatched files — fix the resolver or edit mapping.csv.")


if __name__ == "__main__":
    main()
