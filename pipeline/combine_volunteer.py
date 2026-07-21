"""Combine volunteer-drawn coverage polygons with our automated ones — WHOLE-MUNICIPALITY.

Standalone one-off (NOT a pipeline stage). Covers EVERY municipality we have an automated polygon blob
for (all ~165), not just the volunteer ones. Each municipality is emitted from a SINGLE source — either
entirely the hand-drawn volunteer polygons or entirely our automated ones, never mixed. A muni uses
volunteer unless it has no volunteer data, its file is mislocated (``unusable_files``), or it covers too
few of the muni's stations (``VOL_MUNI_MIN_COMPLETENESS``); otherwise it falls back to automated. The
result is written as a per-muni combined GeoJSON (plus one merged national file) for reuse elsewhere.

For volunteer munis we emit the volunteer polygons RAW (their own number/name/address + priority/
voters, no cross-matching to our register stations). For automated munis we emit the automated
polygons with register identity, exactly like ``export_automated_geojson``. Both branches share one
property key set so downstream schema is uniform.

Inputs:  the automated blobs in ``artifacts/prod-r2/…`` (or local ``artifacts/r2/…``) drive the muni
         list; ``artifacts/volunteer-combined/normalized/*.json`` (clean_volunteer_data) supply the
         volunteer polygons where they exist.
Outputs: ``data/volunteer-combined/<slug>_<muni>_combined.geojson`` + ``combined_all.geojson``;
         ``artifacts/volunteer-combined/combine_report.md`` + ``combine_stats.json`` + per-muni overlay.html.

Run: .venv/bin/python pipeline/combine_volunteer.py   [--source prod|local]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import polars as pl
from shapely.geometry import shape
from shapely.ops import unary_union

import clean_volunteer_data as clean
import compare_volunteer as cv
import config
from export_automated_geojson import slug

OUT_DIR = config.VOLUNTEER_COMBINE_DIR
NORM_DIR = config.VOLUNTEER_NORMALIZED_DIR
STATIONS_DIR = config.VOLUNTEER_STATIONS_DIR
EXPORT_DIR = config.VOLUNTEER_COMBINED_OUT
REPORT = OUT_DIR / "combine_report.md"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def load_automated(polygon_muni_id: str, poly_dir: Path):
    """Read a per-muni automated blob -> station rows keyed by number, in UTM + WGS84."""
    data = json.loads((poly_dir / f"{polygon_muni_id}.json").read_text())["stations"]
    auto_utm, auto_wgs, meta = {}, {}, {}
    for s in data:
        n = s["number"]
        g_wgs = cv.clean(shape(json.loads(s["geojson"])))
        auto_wgs[n] = g_wgs
        auto_utm[n] = cv.to_utm(g_wgs)
        meta[n] = s
    return auto_utm, auto_wgs, meta


def load_station_table(polygon_muni_id: str) -> dict[int, dict]:
    """Volunteer STATION table for a muni -> {Broj BM: record}, or {} if none / ambiguous.

    Polygon-independent (built from the Excel), so it still carries priority/voters for municipalities
    whose polygons were dropped. Guard: if two volunteer files in the muni reuse the same Broj BM (a
    merged parent like Požarevac + Kostolac would), the join is ambiguous → return {} rather than guess."""
    path = STATIONS_DIR / f"{polygon_muni_id}.json"
    if not path.exists():
        return {}
    rows = json.loads(path.read_text())
    seen: dict[int, dict] = {}
    for r in rows:
        n = r.get("number")
        if n is None:
            continue
        if n in seen and seen[n]["source_file"] != r["source_file"]:
            return {}  # same Broj BM from two files → can't attribute
        seen[n] = r
    return seen


def load_volunteer_units(polygon_muni_id: str):
    """Read normalized volunteer records -> vol_utm, vol_wgs, rec (metadata) keyed by unit key."""
    path = NORM_DIR / f"{polygon_muni_id}.json"
    if not path.exists():
        return {}, {}, {}
    recs = json.loads(path.read_text())
    vol_utm, vol_wgs, rec = {}, {}, {}
    for i, r in enumerate(recs):
        g_wgs = cv.clean(shape(r["geometry_wgs"]))
        if g_wgs.is_empty:
            continue
        key = f"{r['source_file']}#{r['unit_key']}#{i}"
        vol_wgs[key] = g_wgs
        vol_utm[key] = cv.to_utm(g_wgs)
        rec[key] = r
    return vol_utm, vol_wgs, rec


# ---------------------------------------------------------------------------
# Merge decision
# ---------------------------------------------------------------------------

def unusable_files(vol_utm: dict, rec: dict, auto_utm: dict) -> set[str]:
    """Volunteer files that don't sit on the municipality (mislocated / wrong CRS) -> force automatic.

    A file is unusable if too little of its drawn area overlaps ANY automated polygon, or it is in the
    manual ``VOLUNTEER_UNUSABLE_FILES`` escape hatch."""
    auto_union = unary_union(list(auto_utm.values())) if auto_utm else None
    by_file: dict[str, list] = defaultdict(list)
    for k, g in vol_utm.items():
        by_file[rec[k]["source_file"]].append(g)
    bad = set(config.VOLUNTEER_UNUSABLE_FILES)
    if auto_union is None or auto_union.is_empty:
        # No automated polygons to validate against — can't detect mislocation, so keep whatever
        # the manual escape hatch names.
        return bad
    for fname, geoms in by_file.items():
        vu = unary_union(geoms)
        if vu.is_empty or vu.area == 0:
            bad.add(fname)
            continue
        frac = vu.intersection(auto_union).area / vu.area
        if frac < config.VOL_FILE_MIN_OVERLAP_FRAC:
            bad.add(fname)
    return bad


def decide_source(usable_vol: dict, auto_utm: dict, n_vol_total: int) -> tuple[str, str, float]:
    """Whole-municipality choice: 'volunteer' or 'automatic', with a reason + completeness fraction.

    Volunteer wins unless it has no usable units (no volunteer data, or whole file mislocated) or
    covers too few of the muni's stations (``usable count / automated station count <
    VOL_MUNI_MIN_COMPLETENESS``). ``n_vol_total`` is the raw volunteer unit count before the usability
    filter — 0 means the municipality has no volunteer data at all."""
    n_auto = len(auto_utm)
    n_vol = len(usable_vol)
    completeness = (n_vol / n_auto) if n_auto else (1.0 if n_vol else 0.0)
    if n_vol == 0:
        reason = ("no volunteer data available" if n_vol_total == 0
                  else "no usable volunteer polygons (mislocated file)")
        return "automatic", reason, completeness
    if completeness < config.VOL_MUNI_MIN_COMPLETENESS:
        return "automatic", f"incomplete volunteer coverage ({n_vol}/{n_auto} = {completeness*100:.0f}%)", completeness
    return "volunteer", f"volunteer coverage {n_vol}/{n_auto} = {completeness*100:.0f}%", completeness


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _feature(geom_wgs, props: dict) -> dict:
    return {"type": "Feature", "properties": props, "geometry": geom_wgs.__geo_interface__}


def _vol_props(r: dict, opstina_cyr: str) -> dict:
    """Property set for a raw volunteer polygon (its own metadata; no register cross-match)."""
    n = r.get("number")
    return {
        "station_id": None,
        "BR_BM": n,
        "OPSTINA": opstina_cyr,
        "NAZIV_BM": r.get("name"),
        "ADRESA_BM": r.get("address"),
        "Uparivanje": f"{opstina_cyr}_{n}" if n is not None else None,
        "source": "volunteer",
        "priority": r.get("priority"),
        "voters": r.get("voters"),
        "priority_source": r.get("priority_source", "none"),
        "vol_br_bm": n,
    }


def _auto_props(m: dict, n: int, opstina_cyr: str, st: dict | None = None) -> dict:
    """Property set for an automated polygon (register identity, like export_automated_geojson).

    ``st`` is the volunteer STATION record for this Broj BM, when the municipality has volunteer data
    even though its polygons weren't usable. Geometry and identity stay 100% register — only the
    volunteer ATTRIBUTES (priority/voters) are attached.

    The join is by station number, but the 2023 volunteer numbering has DRIFTED from the current
    register in many municipalities (an inserted/removed station shifts every later Broj BM), so each
    join is CONFIRMED against the address. An unconfirmed pair keeps null priority/voters and is
    marked ``priority_source="unverified"`` — attaching another station's priority would be worse
    than leaving it empty."""
    props = {
        "station_id": m["station_id"],
        "BR_BM": n,
        "OPSTINA": opstina_cyr,
        "NAZIV_BM": m.get("name_cyr"),
        "ADRESA_BM": m.get("address_cyr"),
        "Uparivanje": f"{opstina_cyr}_{n}",
        "source": "automatic",
        "priority": None,
        "voters": None,
        "priority_source": "none",
        "vol_br_bm": None,
    }
    if not st:
        return props
    if not clean.same_address(m.get("address_cyr"), st.get("address")):
        props["priority_source"] = "unverified"
        return props
    props.update({
        "priority": st.get("priority"),
        "voters": st.get("voters"),
        "priority_source": st.get("priority_source", "none"),
        "vol_br_bm": st.get("number"),
    })
    return props


def combine_muni(polygon_muni_id: str, opstina_cyr: str, poly_dir: Path) -> tuple[list[dict], dict]:
    """Produce the combined feature list for one municipality (single source) + a stats dict.

    The whole municipality is emitted from ONE source: all volunteer polygons (raw) or all automated
    polygons, chosen by ``decide_source``. Sources are never mixed within a municipality."""
    auto_utm, auto_wgs, meta = load_automated(polygon_muni_id, poly_dir)
    vol_utm, vol_wgs, rec = load_volunteer_units(polygon_muni_id)

    # A volunteer unit is dropped if its file is mislocated (geometric guard) OR clean_volunteer_data
    # flagged it unusable (e.g. Subotica's geojson uses a wrong numbering scheme vs the Excel/KMZ).
    mislocated = unusable_files(vol_utm, rec, auto_utm)
    def _drop(k):
        return rec[k]["source_file"] in mislocated or not rec[k].get("usable", True)
    usable_vol = {k: g for k, g in vol_utm.items() if not _drop(k)}
    source, reason, completeness = decide_source(usable_vol, auto_utm, len(vol_utm))

    # When volunteer polygons existed but none were usable, report WHY (scheme / mislocation).
    if source == "automatic" and vol_utm and not usable_vol:
        reasons = {rec[k].get("unusable_reason") for k in vol_utm if not rec[k].get("usable", True)}
        reasons.discard(None)
        if mislocated:
            reasons.add("mislocated volunteer file (doesn't overlap the municipality)")
        if reasons:
            reason = "; ".join(sorted(reasons))

    feats: list[dict] = []
    if source == "volunteer":
        for k in sorted(usable_vol):
            feats.append(_feature(vol_wgs[k], _vol_props(rec[k], opstina_cyr)))
    else:
        # The muni uses automated geometry, but if volunteers surveyed it we still have their station
        # table (priority/voters) — attach it by Broj BM (register number == Excel Broj BM).
        st_table = load_station_table(polygon_muni_id)
        for n in sorted(auto_utm):
            feats.append(_feature(auto_wgs[n], _auto_props(meta[n], n, opstina_cyr, st_table.get(n))))
        if st_table and not vol_utm:
            # surveyed by volunteers, but they mapped no coverage polygons here
            reason = "no volunteer polygons (Excel-only); priority/voters attached where verified"

    stats = {
        "polygon_muni_id": polygon_muni_id, "opstina": opstina_cyr,
        "source": source, "reason": reason,
        "n_features": len(feats), "completeness": round(completeness, 3),
        "n_auto_total": len(auto_utm), "n_vol_units": len(vol_utm), "n_vol_usable": len(usable_vol),
        "n_with_priority": sum(1 for f in feats if f["properties"]["priority"] is not None),
        "n_with_voters": sum(1 for f in feats if f["properties"]["voters"] is not None),
        "n_priority_unverified": sum(1 for f in feats
                                     if f["properties"].get("priority_source") == "unverified"),
        "n_vol_stations": len(load_station_table(polygon_muni_id)),
        "unusable_files": sorted(mislocated),
    }
    return feats, stats


_OVERLAY_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>html,body,#map{{height:100%;margin:0}}
.legend{{background:#fff;padding:8px 10px;font:13px sans-serif;line-height:1.6;box-shadow:0 1px 4px rgba(0,0,0,.3);border-radius:4px}}
.legend i{{display:inline-block;width:12px;height:12px;margin-right:6px;vertical-align:middle;opacity:.6}}</style>
</head><body><div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const map = L.map('map');
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
  {{maxZoom:19, attribution:'© OpenStreetMap'}}).addTo(map);
const C = {{volunteer:'#2e7d32', automatic:'#1769aa'}};
function style(f){{ const s=f.properties.source;
  return {{color:C[s]||'#777', weight:2, fillColor:C[s]||'#777',
           fillOpacity: s==='automatic'?0.15:0.30, dashArray: s==='automatic'?'4 3':null}}; }}
fetch('overlay.geojson').then(r=>r.json()).then(gj=>{{
  const layer = L.geoJSON(gj, {{ style,
    onEachFeature:(f,l)=> l.bindPopup(
      `<b>${{f.properties.NAZIV_BM||f.properties.Uparivanje||'—'}}</b><br>`+
      `source: ${{f.properties.source}}<br>BR_BM: ${{f.properties.BR_BM}}<br>`+
      `prioritet: ${{f.properties.priority??'—'}}`)
  }}).addTo(map);
  map.fitBounds(layer.getBounds());
}});
const lg = L.control({{position:'topright'}});
lg.onAdd = ()=>{{ const d=L.DomUtil.create('div','legend');
  d.innerHTML='<i style="background:#2e7d32"></i>volunteer<br>'+
              '<i style="background:#1769aa"></i>automatic'; return d; }};
lg.addTo(map);
</script></body></html>"""


def write_report(all_stats: list[dict]) -> None:
    L: list[str] = []
    L.append("# Volunteer + automated — combine report (whole-municipality source)\n")
    vol_munis = [s for s in all_stats if s["source"] == "volunteer"]
    auto_munis = [s for s in all_stats if s["source"] == "automatic"]
    feat_v = sum(s["n_features"] for s in vol_munis)
    feat_a = sum(s["n_features"] for s in auto_munis)
    L.append(f"Combined **{len(all_stats)}** municipalities — **{len(vol_munis)}** use volunteer data "
             f"({feat_v} polygons), **{len(auto_munis)}** fall back to automated ({feat_a} polygons). "
             f"Each municipality is a single source (never mixed).\n")
    if auto_munis:
        L.append("**Municipalities using automated data (with reason):**")
        for s in sorted(auto_munis, key=lambda s: s["opstina"]):
            L.append(f"- {s['polygon_muni_id']} {s['opstina']} — {s['reason']}")
        L.append("")
    L.append("## Per municipality\n")
    L.append("| muni | opština | source | features | completeness (vol/auto) | reason |")
    L.append("|---|---|---|---|---|---|")
    for s in sorted(all_stats, key=lambda s: (s["source"], s["opstina"])):
        L.append(f"| {s['polygon_muni_id']} | {s['opstina']} | {s['source']} | {s['n_features']} | "
                 f"{s['n_vol_usable']}/{s['n_auto_total']} ({s['completeness']*100:.0f}%) | {s['reason']} |")
    REPORT.write_text("\n".join(L))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["prod", "local"], default="prod",
                    help="automated blobs: 'prod' (fetched, default) or 'local' (fresh stage06 output)")
    args = ap.parse_args()

    prod_dir = config.VOLUNTEER_PROD_POLY_DIR
    local_dir = config.ARTIFACTS_DIR / "r2" / "polygons" / "m"
    poly_dir = prod_dir if (args.source == "prod" and prod_dir.exists()) else local_dir
    print(f"Using automated polygons from: {poly_dir.relative_to(config.ROOT_DIR)}")

    munis = pl.read_parquet(config.MUNICIPALITIES_PARQUET)
    name_by_id = {str(r["id"]): r["name_cyr"] for r in munis.iter_rows(named=True)}
    lat_by_id = {str(r["id"]): r["name_lat"] for r in munis.iter_rows(named=True)}
    # register station count per muni — NOT the same as the polygon count (a station with no matched
    # addresses gets no polygon), so the difference is a real automated-coverage gap worth reporting.
    reg_stations: dict[str, int] = {}
    st_path = config.ARTIFACTS_DIR / "stations.parquet"
    if st_path.exists():
        st = pl.read_parquet(st_path).select(["municipality_id"])
        for r in st.group_by("municipality_id").len().iter_rows(named=True):
            reg_stations[str(r["municipality_id"])] = r["len"]

    # Every municipality we have an automated polygon blob for — volunteer munis resolve to
    # volunteer or automated per decide_source; munis with no volunteer data are whole-automated.
    muni_ids = sorted(p.stem for p in poly_dir.glob("*.json"))

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    all_feats: list[dict] = []
    all_stats: list[dict] = []
    for pid in muni_ids:
        opstina = name_by_id.get(pid, pid)
        feats, stats = combine_muni(pid, opstina, poly_dir)
        out_name = f"{slug(lat_by_id.get(pid, pid))}_{pid}_combined.geojson"
        stats["output_file"] = out_name
        stats["name_lat"] = lat_by_id.get(pid, pid)
        stats["n_register_stations"] = reg_stations.get(pid)
        all_stats.append(stats)
        all_feats.extend(feats)

        fc = {"type": "FeatureCollection", "features": feats}
        (EXPORT_DIR / out_name).write_text(json.dumps(fc, ensure_ascii=False))
        muni_dir = OUT_DIR / "m" / pid
        muni_dir.mkdir(parents=True, exist_ok=True)
        (muni_dir / "overlay.geojson").write_text(json.dumps(fc, ensure_ascii=False))
        (muni_dir / "overlay.html").write_text(_OVERLAY_HTML.format(title=f"{opstina} combined"))
        tag = "VOL " if stats["source"] == "volunteer" else "auto"
        print(f"  {pid} {opstina:<22} {tag} {stats['n_features']:>3} feats  "
              f"({stats['n_vol_usable']}/{stats['n_auto_total']})  {stats['reason']}")

    (EXPORT_DIR / "combined_all.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": all_feats}, ensure_ascii=False))
    write_report(all_stats)
    (OUT_DIR / "combine_stats.json").write_text(json.dumps(all_stats, ensure_ascii=False))
    nv = sum(1 for s in all_stats if s["source"] == "volunteer")
    print(f"\nWrote {len(all_stats)} combined municipalities ({nv} volunteer, {len(all_stats)-nv} "
          f"automatic; {len(all_feats)} features) -> {EXPORT_DIR.relative_to(config.ROOT_DIR)}")
    print(f"  report -> {REPORT.relative_to(config.ROOT_DIR)}")


if __name__ == "__main__":
    main()
