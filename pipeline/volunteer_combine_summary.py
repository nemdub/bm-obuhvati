"""Generate the per-municipality data report for the volunteer↔automated combine.

Joins the machine-readable artifacts the other scripts produce —
  - ``artifacts/volunteer-compare/mapping.csv``          (file → municipality, map_volunteer_polygons)
  - ``artifacts/volunteer-combined/data_quality.json``   (per-file cleanup findings, clean_volunteer_data)
  - ``artifacts/volunteer-combined/combine_stats.json``  (per-muni outcome, combine_volunteer)
— into ``data/volunteer-combined/SUMMARY.md``: for every municipality the polygon **source**
(automatic vs volunteer), whether **priority** and **voter counts** are available, and any
**discrepancies**; plus sections documenting the merged municipalities (Sevojno→Užice …), the data
mapping issues (Subotica …), and the automated coverage gaps.

Run AFTER clean_volunteer_data.py + combine_volunteer.py:
  .venv/bin/python pipeline/volunteer_combine_summary.py
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict

import config
import map_volunteer_polygons as mvp

OUT_DIR = config.VOLUNTEER_COMBINE_DIR
SUMMARY = config.VOLUNTEER_COMBINED_OUT / "SUMMARY.md"


def _short(fname: str) -> str:
    return fname.replace("_2023.geojson", "")


def cleanup_flags(all_dqs: list[dict]) -> str:
    """Compact per-municipality cleanup notes aggregated across its volunteer file(s).

    Only files that actually have a geojson can have geometry/number findings — Excel-only
    municipalities are skipped so they aren't falsely reported as "no BR_BM"."""
    dqs = [d for d in all_dqs if d.get("has_geojson")]
    if not dqs:
        return "Excel only (no drawn polygons)" if all_dqs else "clean"
    flags: list[str] = []
    nulls = sum(d["n_null_geoms"] for d in dqs)
    if nulls:
        flags.append(f"{nulls} null geom dropped")
    for d in dqs:
        if not d["has_brbm"]:
            got = d["n_rescued"]
            miss = d["n_numberless_units"]
            flags.append(f"no BR_BM → {got} numbers rescued from KMZ"
                         + (f", {miss} still unnumbered" if miss else ""))
    if any(not d["has_kmz"] for d in dqs):
        flags.append("no KMZ (no points/priority)")
    if any(d["is_utm"] for d in dqs):
        flags.append("UTM34N → reprojected")
    for d in dqs:
        counts = {d["n_units"], d["n_xlsx_rows"], d["n_kmz_points"]} - {0}
        if len(counts) > 1:
            flags.append(f"count mismatch (geo {d['n_units']}/xlsx {d['n_xlsx_rows']}/kmz {d['n_kmz_points']})")
    return "; ".join(flags) if flags else "clean"


def discrepancies(s: dict, dqs: list[dict], children: list[dict]) -> list[str]:
    """Short per-municipality discrepancy flags for the report table."""
    out: list[str] = []
    reg, feats = s.get("n_register_stations"), s["n_features"]
    if reg and feats < reg:
        out.append(f"**{reg - feats} of {reg} stations have no polygon**")
    if children:
        out.append("absorbs " + ", ".join(f"{c['muni_name_lat']} ({c['muni_id']})" for c in children))
    unver = s.get("n_priority_unverified") or 0
    if unver:
        out.append(f"**priority unverified for {unver}** (2023 numbering drifted)")
    if any(d.get("scheme_wrong") for d in dqs):
        out.append("volunteer geojson numbered with global RBR → dropped")
    dropped = sum(d.get("n_bad_number", 0) for d in dqs)
    if dropped and not any(d.get("scheme_wrong") for d in dqs):
        out.append(f"{dropped} volunteer polygons without a valid Broj BM → dropped")
    if s["unusable_files"]:
        out.append("volunteer file mislocated")
    if dqs:
        # volunteer station count = Excel rows (works for merged parents too, where the station-table
        # join is refused as ambiguous and n_vol_stations is 0)
        vol_st = sum(d.get("n_xlsx_rows", 0) for d in dqs)
        if reg and vol_st and vol_st != reg:
            out.append(f"volunteer lists {vol_st} stations vs register {reg}")
        flags = cleanup_flags(dqs)
        if flags != "clean":
            out.append(flags)
    return out


def main() -> None:
    with mvp.MAPPING_CSV.open(newline="") as fh:
        mapping = [r for r in csv.DictReader(fh) if r["polygon_muni_id"]]
    files_by_pid: dict[str, list[dict]] = defaultdict(list)
    for r in mapping:
        files_by_pid[r["polygon_muni_id"]].append(r)
    # municipalities absorbed into a parent (they get no row of their own)
    children_by_pid: dict[str, list[dict]] = defaultdict(list)
    for r in mapping:
        if r["status"] == "child_go_merged_to_parent":
            children_by_pid[r["polygon_muni_id"]].append(r)

    dq = json.loads((OUT_DIR / "data_quality.json").read_text())
    dq_by_pid: dict[str, list[dict]] = defaultdict(list)
    for d in dq:
        if d["polygon_muni_id"]:
            dq_by_pid[d["polygon_muni_id"]].append(d)

    stats = {s["polygon_muni_id"]: s for s in json.loads((OUT_DIR / "combine_stats.json").read_text())}
    rows = sorted(stats.values(), key=lambda s: s.get("name_lat", s["polygon_muni_id"]))
    vol_rows = [s for s in rows if s["source"] == "volunteer"]
    auto_rows = [s for s in rows if s["source"] == "automatic"]
    # "surveyed" = volunteers produced files for this muni (NOT n_vol_stations, which is 0 for the
    # merged parents where the station-table join is refused as ambiguous).
    surveyed = [s for s in rows if s["polygon_muni_id"] in dq_by_pid]
    no_vol = [s for s in rows if s["polygon_muni_id"] not in dq_by_pid]

    L: list[str] = []
    L.append("# Combined coverage — per-municipality data report\n")
    L.append("For every municipality: which data the coverage **polygons** come from (volunteer vs "
             "automatic), whether **priority** and **voter counts** are available, and any "
             "**discrepancies**. Each municipality is a single polygon source — never mixed. "
             "Generated by `pipeline/volunteer_combine_summary.py`; regenerate after re-running the "
             "combine.\n")

    L.append("## How it was produced\n")
    L.append("1. `pipeline/map_volunteer_polygons.py` — resolves each `data/volunteer-data/<NAME>_2023"
             ".geojson` to a register municipality (`mapping.csv`).")
    L.append("2. `pipeline/fetch_prod_polygons.sh` — pulls the production automatic polygons from R2.")
    L.append("3. `pipeline/clean_volunteer_data.py` — reads geojson + `*_biracka_mesta.xlsx` "
             "(priority/voters) + `*_lokacije.kmz` (points), repairs geometry, resolves station "
             "numbers → normalized records + a volunteer **station table** + `data_quality_report.md`.")
    L.append("4. `pipeline/combine_volunteer.py` — picks ONE source per municipality and writes "
             "`data/volunteer-combined/<slug>_<muni>_combined.geojson`.\n")

    L.append("## Totals\n")
    L.append(f"- **{len(rows)} municipalities** in the dataset (every muni with automatic polygons). "
             f"The register has 168; **3 are merged into a parent** (see below), so 165 rows.")
    L.append(f"- Polygon source: **{len(vol_rows)} volunteer** "
             f"({sum(s['n_features'] for s in vol_rows)} polygons) · **{len(auto_rows)} automatic** "
             f"({sum(s['n_features'] for s in auto_rows)} polygons).")
    L.append(f"- **Priority available** for **{sum(s['n_with_priority'] for s in rows)}** of "
             f"{sum(s['n_features'] for s in rows)} polygons, across **"
             f"{sum(1 for s in rows if s['n_with_priority'])}** municipalities.")
    L.append(f"- **Voter counts available** for **{sum(s['n_with_voters'] for s in rows)}** polygons, "
             f"across **{sum(1 for s in rows if s['n_with_voters'])}** municipalities.")
    nopri = [s for s in rows if not s["n_with_priority"]]
    L.append(f"- **{len(surveyed)}** municipalities were surveyed by volunteers"
             + (f"; **{len(no_vol)}** have no volunteer data at all" if no_vol else " (all of them)")
             + (f". **{len(nopri)}** end up with no priority at all: "
                + ", ".join(f"{s.get('name_lat','')}" for s in nopri) if nopri else ".") + "\n")
    L.append("> Priority/voters come from the volunteer Excel/KMZ. Most municipalities were surveyed "
             "with a station list (Excel) but **without** drawn coverage polygons — those keep our "
             "automatic polygons and get the priority/voters attached by station number, with the "
             "geometry staying 100% automatic. Each such attachment is confirmed against the station "
             "address (see *numbering drift* below); unconfirmed ones are left empty rather than "
             "guessed.\n")

    L.append("## Per municipality\n")
    L.append("**source** = where the polygons come from. **stations** = polygons emitted / stations in "
             "the register. **priority** / **voters** = polygons carrying that field. A dash means the "
             "municipality has no volunteer data, so the field cannot exist.\n")
    L.append("| muni | municipality | source | stations | priority | voters | discrepancies |")
    L.append("|---|---|---|---|---|---|---|")
    for s in rows:
        pid = s["polygon_muni_id"]
        dqs = dq_by_pid.get(pid, [])
        n, reg = s["n_features"], s.get("n_register_stations")
        src = "**automatic**" if s["source"] == "automatic" else "volunteer"
        pri = f"{s['n_with_priority']}/{n}" if dqs else "—"   # "—" = no volunteer data, field can't exist
        vot = f"{s['n_with_voters']}/{n}" if dqs else "—"
        disc = discrepancies(s, dqs, children_by_pid.get(pid, []))
        L.append(f"| {pid} | {s.get('name_lat','')} | {src} | {n}/{reg if reg else '?'} | {pri} | "
                 f"{vot} | {'; '.join(disc) if disc else '—'} |")
    L.append("")

    # ── merged municipalities ────────────────────────────────────────────────
    L.append("## Merged municipalities\n")
    L.append("Three register municipalities are **city districts that have no polling stations of their "
             "own** — the RIK document numbers them continuously with the parent city, so the pipeline "
             "files their stations (and polygons) under the parent. They therefore have **no row of "
             "their own** in this dataset; their stations appear under the parent municipality.\n")
    L.append("| child muni | name | merged into | volunteer file feeding the parent |")
    L.append("|---|---|---|---|")
    for pid, cs in sorted(children_by_pid.items()):
        for c in cs:
            L.append(f"| {c['muni_id']} | {c['muni_name_lat']} | {pid} "
                     f"{stats[pid].get('name_lat','') if pid in stats else ''} | {_short(c['file'])} |")
    L.append("")
    L.append("Consequence: e.g. **Sevojno's** stations are inside `užice_71145_combined.geojson`, not a "
             "Sevojno file — a consumer looking for Sevojno must look under Užice. The parent's "
             "`OPSTINA` property says the parent's name for all of them.\n")
    amb = [r for r in mapping if r["status"] == "ambiguous_resolved"]
    if amb:
        L.append("For contrast, these are **name collisions**, not merges — each keeps its own row:\n")
        for r in amb:
            L.append(f"- `{_short(r['file'])}` → {r['muni_name_lat']} ({r['muni_id']})")
        L.append("")

    # ── mapping issues ───────────────────────────────────────────────────────
    L.append("## Data mapping issues (volunteer polygons that could not be trusted)\n")
    L.append("The authoritative station numbers are the Excel `Broj BM` (1..N per municipality). A "
             "volunteer polygon is only used when it resolves to one of those. These municipalities "
             "failed that test and fall back to automatic polygons:\n")
    L.append("| muni | issue | effect |")
    L.append("|---|---|---|")
    for s in sorted(auto_rows, key=lambda s: s.get("name_lat", "")):
        dqs = dq_by_pid.get(s["polygon_muni_id"], [])
        if not dqs:
            continue  # no volunteer data — not a mapping issue
        L.append(f"| {s.get('name_lat','')} ({s['polygon_muni_id']}) | {s['reason']} | "
                 f"all {s['n_features']} polygons automatic; priority/voters still attached "
                 f"({s['n_with_priority']}) |")
    partial = [s for s in vol_rows if sum(d.get("n_bad_number", 0) for d in dq_by_pid.get(s["polygon_muni_id"], []))]
    if partial:
        L.append("")
        L.append("Partially affected — kept as volunteer, but the un-attributable polygons were dropped:\n")
        L.append("| muni | polygons dropped | kept |")
        L.append("|---|---|---|")
        for s in sorted(partial, key=lambda s: s.get("name_lat", "")):
            dropped = sum(d.get("n_bad_number", 0) for d in dq_by_pid[s["polygon_muni_id"]])
            L.append(f"| {s.get('name_lat','')} ({s['polygon_muni_id']}) | {dropped} | {s['n_features']} |")
    L.append("")
    L.append("**Subotica** is the clearest case: its geojson labels every polygon with the *global* "
             "`RBR` (1256…1357) instead of the local `Broj BM` (1–102), so the polygons cannot be "
             "matched to stations at all. **Lučani/Kosjerić** have the same problem one level down — "
             "their geojson carries no number and their KMZ points are numbered with the RBR (5751…). "
             "In both cases the Excel is intact, so priority/voters are still available.\n")

    # ── volunteer numbering drift ────────────────────────────────────────────
    drift = [s for s in rows if (s.get("n_priority_unverified") or 0)]
    L.append("## Volunteer Excel numbering drift\n")
    L.append("Priority/voters are joined to our automated polygons by station number (`Broj BM`), but "
             "the 2023 volunteer Excel has **drifted** from the current register in many municipalities "
             "— a station inserted or removed shifts every later number. (Ćuprija has an extra station "
             "at #4, so register #5 is the Excel's #6; Rača lists 27 stations against 22 in the "
             "register.) Station counts matching is *not* proof: Leskovac is 194 vs 194 and still "
             "drifts by station ~100.\n")
    L.append("Every join is therefore confirmed against the **address** (order-insensitive, so "
             "\"Школска 8, Беочин\" = \"БЕОЧИН, ШКОЛСКА БР. 8\"). Unconfirmed stations keep **null** "
             "priority/voters and are marked `priority_source=\"unverified\"` — attaching another "
             "station's priority would be worse than leaving it empty.\n")
    L.append(f"**{sum(s.get('n_priority_unverified') or 0 for s in rows)}** stations across "
             f"**{len(drift)}** municipalities could not be verified.\n")
    L.append("| muni | source | priority attached | unverified |")
    L.append("|---|---|---|---|")
    for s in sorted(drift, key=lambda s: -(s.get("n_priority_unverified") or 0))[:25]:
        L.append(f"| {s.get('name_lat','')} ({s['polygon_muni_id']}) | {s['source']} | "
                 f"{s['n_with_priority']}/{s['n_features']} | **{s['n_priority_unverified']}** |")
    L.append("")
    L.append(f"_(top 25 of {len(drift)} by unverified count)_\n")

    # ── automatic coverage gaps ──────────────────────────────────────────────
    gaps = [s for s in rows if s.get("n_register_stations") and s["n_features"] < s["n_register_stations"]]
    L.append("## Coverage gaps (register stations with no polygon)\n")
    L.append("Stations that exist in the register but have no polygon in this dataset, for two "
             "different reasons depending on the source:\n")
    L.append("- **automatic** municipalities — our pipeline only builds a polygon for a station whose "
             "coverage text matched addresses in the register; unmatched stations get none "
             "(e.g. MIONICA has just 5 polygons for 38 stations).")
    L.append("- **volunteer** municipalities — volunteers didn't draw every station, and/or polygons "
             "were dropped because they carried no valid `Broj BM`.\n")
    gap_auto = [s for s in gaps if s["source"] == "automatic"]
    gap_vol = [s for s in gaps if s["source"] == "volunteer"]
    L.append(f"**{len(gaps)}** municipalities are affected "
             f"({sum(s['n_register_stations'] - s['n_features'] for s in gaps)} stations missing) — "
             f"{len(gap_auto)} automatic, {len(gap_vol)} volunteer.\n")
    L.append("| muni | source | polygons | register stations | missing |")
    L.append("|---|---|---|---|---|")
    for s in sorted(gaps, key=lambda s: -(s["n_register_stations"] - s["n_features"]))[:25]:
        L.append(f"| {s.get('name_lat','')} ({s['polygon_muni_id']}) | {s['source']} | "
                 f"{s['n_features']} | {s['n_register_stations']} | "
                 f"**{s['n_register_stations'] - s['n_features']}** |")
    L.append("")
    L.append(f"_(top 25 of {len(gaps)} by missing count)_\n")

    # ── other data-quality issues ────────────────────────────────────────────
    L.append("## Other volunteer data-quality issues\n")
    no_kmz = [d for d in dq if not d["has_kmz"]]
    utm = [d for d in dq if d["is_utm"]]
    nulls = [d for d in dq if d["n_null_geoms"]]
    L.append(f"- **Missing KMZ** ({len(no_kmz)} municipalities — no station points; priority/voters come "
             f"from the Excel only): {', '.join(sorted(_short(d['file']) for d in no_kmz))}")
    L.append(f"- **UTM34N geojson** (reprojected on load): "
             f"{', '.join(sorted(_short(d['file']) for d in utm)) or 'none'}")
    L.append(f"- **Null/empty geometries dropped**: {sum(d['n_null_geoms'] for d in nulls)} features across "
             f"{len(nulls)} files ({', '.join(sorted(_short(d['file']) for d in nulls))})")
    L.append("\nFull per-file detail: `pipeline/artifacts/volunteer-combined/data_quality_report.md`.\n")

    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY.write_text("\n".join(L))
    print(f"Wrote per-municipality data report ({len(rows)} munis) -> "
          f"{SUMMARY.relative_to(config.ROOT_DIR)}")


if __name__ == "__main__":
    main()
