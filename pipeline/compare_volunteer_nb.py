"""One-off comparison: volunteer-mapped coverage vs the automated pipeline (Novi Beograd).

Compares the hand-drawn volunteer polygons in data/volunteer-polygons/novi-beograd.geojson
against the automated Voronoi/clip output for municipality 70181, on three axes:
  1. per-station shape agreement (IoU, asymmetric coverage, area, centroid distance)
  2. per-address accuracy (do automated assignments fall inside the human polygons?)
  3. a visual overlay (overlay.geojson + standalone Leaflet overlay.html)

Automated polygons are read from the per-municipality R2 serving file
`pipeline/artifacts/r2/polygons/m/<muni>.json` (the format introduced when polygons moved
from D1 to R2). That file's `stations` list already bundles station metadata + geojson, so no
parquet join is needed for the polygons. Per-address accuracy still reads links/addresses parquet.

All area / containment math is done in UTM34N (EPSG:32634), the register-native metric CRS.
Outputs land in pipeline/artifacts/volunteer-compare/. Read-only against existing artifacts.

Run: .venv/bin/python pipeline/compare_volunteer_nb.py
"""

from __future__ import annotations

import json
import statistics as st
from pathlib import Path

import polars as pl
from pyproj import Transformer
from shapely import STRtree
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform
from shapely.ops import unary_union
from shapely.validation import make_valid

MUNI_ID = "70181"          # Novi Beograd (register / municipalities.parquet id, stored as String)
MUNI_NAME = "НОВИ БЕОГРАД"
STATION_BASE = 70181 * 100_000  # station_id = base + station number

PIPELINE_DIR = Path(__file__).resolve().parent
ROOT_DIR = PIPELINE_DIR.parent
ARTIFACTS = PIPELINE_DIR / "artifacts"
OUT_DIR = ARTIFACTS / "volunteer-compare"
VOLUNTEER_GEOJSON = ROOT_DIR / "data" / "volunteer-polygons" / "novi-beograd.geojson"
AUTO_R2_JSON = ARTIFACTS / "r2" / "polygons" / "m" / f"{MUNI_ID}.json"

_to_utm = Transformer.from_crs(4326, 32634, always_xy=True).transform


def clean(geom: BaseGeometry) -> BaseGeometry:
    """make_valid, then keep only polygonal parts (drop stray line/point slivers a
    GeometryCollection repair can introduce — they break some external GeoJSON tools)."""
    g = make_valid(geom)
    if g.geom_type == "GeometryCollection":
        polys = [p for p in g.geoms if p.geom_type in ("Polygon", "MultiPolygon")]
        g = unary_union(polys) if polys else g
    return g


def to_utm(geom: BaseGeometry) -> BaseGeometry:
    """Reproject a WGS84 shapely geometry to UTM34N and repair self-touching rings."""
    return clean(shp_transform(_to_utm, geom))


# ---------------------------------------------------------------------------
# Load inputs
# ---------------------------------------------------------------------------

def load_automated():
    """Read the R2 per-muni polygon file → (auto_utm, auto_wgs, meta by number).

    meta[number] = {"station_id", "name_cyr", "address_cyr"}.
    """
    data = json.loads(AUTO_R2_JSON.read_text())["stations"]
    auto_utm: dict[int, BaseGeometry] = {}
    auto_wgs: dict[int, BaseGeometry] = {}
    meta: dict[int, dict] = {}
    for s in data:
        n = s["number"]
        g_wgs = clean(shape(json.loads(s["geojson"])))
        auto_wgs[n] = g_wgs
        auto_utm[n] = to_utm(g_wgs)
        meta[n] = {
            "station_id": s["station_id"],
            "name_cyr": s.get("name_cyr"),
            "address_cyr": s.get("address_cyr"),
        }
    return auto_utm, auto_wgs, meta


def load_volunteer():
    """Return (vol_utm by number, vol_wgs by number). Dissolves multi-feature stations."""
    gj = json.loads(VOLUNTEER_GEOJSON.read_text())
    raw: dict[int, list[BaseGeometry]] = {}
    for feat in gj["features"]:
        n = feat["properties"]["BR_BM"]
        raw.setdefault(n, []).append(clean(shape(feat["geometry"])))
    vol_wgs = {n: (g[0] if len(g) == 1 else clean(unary_union(g))) for n, g in raw.items()}
    vol_utm = {n: to_utm(g) for n, g in vol_wgs.items()}
    return vol_utm, vol_wgs


# ---------------------------------------------------------------------------
# 1. Per-station shape comparison
# ---------------------------------------------------------------------------

def cause_tag(iou: float, ratio: float, cov_vol: float, cov_auto: float, cdist: float) -> str:
    if cov_vol >= 0.7 and ratio < 0.6:
        return "auto_much_smaller (volunteer over-draws empty land)"
    if cov_auto >= 0.7 and ratio > 1.6:
        return "auto_much_larger (automated spills beyond volunteer)"
    if cdist > 400:
        return "low_overlap (centroids diverge — boundary disagreement)"
    if iou < 0.2:
        return "low_overlap (little shared area)"
    return "partial_shift"


def compare_shapes(auto_utm, vol_utm):
    common = sorted(set(auto_utm) & set(vol_utm))
    rows = []
    for n in common:
        a, v = auto_utm[n], vol_utm[n]
        inter = a.intersection(v).area
        union = a.union(v).area
        iou = inter / union if union else 0.0
        cov_vol = inter / v.area if v.area else 0.0
        cov_auto = inter / a.area if a.area else 0.0
        ratio = a.area / v.area if v.area else 0.0
        cdist = a.centroid.distance(v.centroid)
        rows.append({
            "number": n,
            "iou": iou,
            "auto_km2": a.area / 1e6,
            "vol_km2": v.area / 1e6,
            "area_ratio": ratio,
            "coverage_of_volunteer": cov_vol,
            "coverage_of_auto": cov_auto,
            "centroid_dist_m": cdist,
            "cause": cause_tag(iou, ratio, cov_vol, cov_auto, cdist),
        })
    return rows


# ---------------------------------------------------------------------------
# 2. Per-address accuracy
# ---------------------------------------------------------------------------

def compare_addresses(vol_utm):
    addrs = pl.read_parquet(ARTIFACTS / "addresses.parquet").filter(
        pl.col("municipality_id") == MUNI_ID
    ).select(["id", "x", "y"])
    # NB station links: station_id in the 70181xxxxx range
    nb_links = pl.read_parquet(ARTIFACTS / "links.parquet").filter(
        (pl.col("station_id") >= STATION_BASE) & (pl.col("station_id") < STATION_BASE + 100_000)
    )

    addr_xy = {r["id"]: (r["x"], r["y"]) for r in addrs.iter_rows(named=True)}

    # spatial index over volunteer polygons
    vnums = list(vol_utm)
    vgeoms = [vol_utm[n] for n in vnums]
    tree = STRtree(vgeoms)

    # map each NB address -> list of volunteer polygon numbers containing it
    contained: dict[str, list[int]] = {}
    for aid, (x, y) in addr_xy.items():
        hits = tree.query(Point(x, y), predicate="intersects")
        contained[aid] = [vnums[i] for i in hits]

    # automated link per address (NB stations only)
    auto_link_num: dict[str, int] = {
        r["address_id"]: r["station_id"] - STATION_BASE
        for r in nb_links.iter_rows(named=True)
    }

    # --- containment rate: auto-assigned address inside its own volunteer polygon ---
    cont_total = cont_inside = 0
    for aid, n in auto_link_num.items():
        if n not in vol_utm or aid not in addr_xy:
            continue
        cont_total += 1
        cont_inside += int(n in contained.get(aid, []))

    # --- assignment agreement: auto link number vs volunteer-polygon number ---
    agree = disagree = no_vol = 0
    confusion: dict[tuple[int, int], int] = {}
    for aid, n in auto_link_num.items():
        if aid not in addr_xy:
            continue
        vols = contained.get(aid, [])
        if not vols:
            no_vol += 1
            continue
        if n in vols:
            agree += 1
        else:
            disagree += 1
            confusion[(n, vols[0])] = confusion.get((n, vols[0]), 0) + 1

    # --- unlinked NB addresses that volunteers nonetheless covered ---
    unlinked = [aid for aid in addr_xy if aid not in auto_link_num]
    unlinked_in_vol = sum(1 for aid in unlinked if contained.get(aid))

    return {
        "nb_addresses": len(addr_xy),
        "auto_linked": len(auto_link_num),
        "containment_total": cont_total,
        "containment_inside": cont_inside,
        "containment_rate": cont_inside / cont_total if cont_total else 0.0,
        "agree": agree,
        "disagree": disagree,
        "no_vol_polygon": no_vol,
        "agreement_rate": agree / (agree + disagree) if (agree + disagree) else 0.0,
        "confusion_top": sorted(confusion.items(), key=lambda kv: -kv[1])[:10],
        "unlinked": len(unlinked),
        "unlinked_in_vol": unlinked_in_vol,
    }


# ---------------------------------------------------------------------------
# 3. Visual overlay
# ---------------------------------------------------------------------------

def write_overlay(auto_wgs, vol_wgs, meta, iou_by_num):
    feats = []
    for n, g in sorted(auto_wgs.items()):
        feats.append({
            "type": "Feature",
            "geometry": g.__geo_interface__,
            "properties": {"source": "auto", "number": n,
                            "station_id": meta.get(n, {}).get("station_id"),
                            "iou": round(iou_by_num.get(n, 0.0), 3)},
        })
    for n, g in sorted(vol_wgs.items()):
        feats.append({
            "type": "Feature",
            "geometry": g.__geo_interface__,
            "properties": {"source": "volunteer", "number": n,
                            "iou": round(iou_by_num.get(n, 0.0), 3)},
        })
    fc = {"type": "FeatureCollection", "features": feats}
    (OUT_DIR / "overlay.geojson").write_text(json.dumps(fc))

    html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Novi Beograd: automated vs volunteer coverage</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>html,body,#map{height:100%;margin:0}
.legend{background:#fff;padding:8px 10px;font:13px sans-serif;line-height:1.5;box-shadow:0 1px 4px rgba(0,0,0,.3);border-radius:4px}
.legend i{display:inline-block;width:12px;height:12px;margin-right:6px;vertical-align:middle;opacity:.6}</style>
</head><body><div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const map = L.map('map');
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom:19, attribution:'© OpenStreetMap'}).addTo(map);
function style(src){ return src==='auto'
  ? {color:'#1769aa', weight:2, fillColor:'#2196f3', fillOpacity:.20}
  : {color:'#b71c1c', weight:2, fillColor:'#f44336', fillOpacity:.18, dashArray:'4 3'}; }
fetch('overlay.geojson').then(r=>r.json()).then(gj=>{
  const layer = L.geoJSON(gj, {
    style: f => style(f.properties.source),
    onEachFeature: (f,l)=> l.bindPopup(
      `<b>BM ${f.properties.number}</b><br>${f.properties.source}<br>IoU: ${f.properties.iou}`)
  }).addTo(map);
  map.fitBounds(layer.getBounds());
});
const lg = L.control({position:'topright'});
lg.onAdd = ()=>{ const d=L.DomUtil.create('div','legend');
  d.innerHTML='<i style="background:#2196f3"></i>automated<br><i style="background:#f44336"></i>volunteer'; return d; };
lg.addTo(map);
</script></body></html>"""
    (OUT_DIR / "overlay.html").write_text(html)


# ---------------------------------------------------------------------------
# 4. Report
# ---------------------------------------------------------------------------

def diagnose_missing(missing_auto):
    """For numbers with no automated polygon, count their automated links (resolved addresses)."""
    if not missing_auto:
        return {}
    links = pl.read_parquet(ARTIFACTS / "links.parquet")
    return {n: links.filter(pl.col("station_id") == STATION_BASE + n).height for n in missing_auto}


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    auto_utm, auto_wgs, meta = load_automated()
    vol_utm, vol_wgs = load_volunteer()

    rows = compare_shapes(auto_utm, vol_utm)
    iou_by_num = {r["number"]: r["iou"] for r in rows}

    missing_auto = sorted(set(vol_utm) - set(auto_utm))
    missing_vol = sorted(set(auto_utm) - set(vol_utm))
    missing_links = diagnose_missing(missing_auto)

    addr = compare_addresses(vol_utm)
    write_overlay(auto_wgs, vol_wgs, meta, iou_by_num)

    ious = [r["iou"] for r in rows]
    hist = {
        "<0.2": sum(1 for x in ious if x < 0.2),
        "0.2-0.5": sum(1 for x in ious if 0.2 <= x < 0.5),
        "0.5-0.8": sum(1 for x in ious if 0.5 <= x < 0.8),
        ">=0.8": sum(1 for x in ious if x >= 0.8),
    }
    tot_auto = sum(r["auto_km2"] for r in rows)
    tot_vol = sum(r["vol_km2"] for r in rows)
    worst = sorted(rows, key=lambda r: r["iou"])[:15]

    L = []
    L.append(f"# Volunteer vs automated coverage — {MUNI_NAME} (muni {MUNI_ID})\n")
    L.append(f"_Automated source: {AUTO_R2_JSON.relative_to(ROOT_DIR)} (R2 serving format)._\n")
    L.append("## Aggregate\n")
    L.append(f"- Volunteer stations: **{len(vol_utm)}**, automated polygons: **{len(auto_utm)}**, "
             f"compared pairs: **{len(rows)}**")
    L.append(f"- IoU: mean **{st.mean(ious):.3f}**, median **{st.median(ious):.3f}**, "
             f"min {min(ious):.3f}, max {max(ious):.3f}")
    L.append(f"- IoU histogram: {hist}")
    L.append(f"- Total area: automated **{tot_auto:.2f} km²**, volunteer **{tot_vol:.2f} km²** "
             f"(ratio {tot_auto/tot_vol:.2f})\n")

    L.append("## Missing on each side\n")
    if missing_auto:
        parts = ", ".join(f"BM {n} ({missing_links.get(n,0)} automated links)" for n in missing_auto)
        L.append(f"- Volunteer polygon but **no automated polygon**: {parts}")
        L.append("  - 0 links → station's coverage was fully unresolved (no addresses matched); "
                 ">0 links → addresses matched but polygon build dropped it.")
    else:
        L.append("- Volunteer polygon but no automated polygon: none")
    L.append(f"- Automated polygon but no volunteer polygon: "
             f"{missing_vol if missing_vol else 'none'}\n")

    L.append("## Worst 15 by IoU\n")
    L.append("| BM | IoU | auto km² | vol km² | ratio | cov(vol) | cov(auto) | centroid Δm | likely cause |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for r in worst:
        L.append(f"| {r['number']} | {r['iou']:.3f} | {r['auto_km2']:.3f} | {r['vol_km2']:.3f} | "
                 f"{r['area_ratio']:.2f} | {r['coverage_of_volunteer']:.2f} | "
                 f"{r['coverage_of_auto']:.2f} | {r['centroid_dist_m']:.0f} | {r['cause']} |")
    L.append("")

    L.append("## Per-address accuracy\n")
    L.append(f"- NB register addresses: **{addr['nb_addresses']}**; "
             f"automated-linked to an NB station: **{addr['auto_linked']}** "
             f"({addr['auto_linked']/addr['nb_addresses']*100:.1f}%)")
    L.append(f"- **Containment rate**: of {addr['containment_total']} auto-assigned addresses "
             f"(where the station has a volunteer polygon), **{addr['containment_rate']*100:.1f}%** "
             f"fall inside that same volunteer polygon ({addr['containment_inside']}/{addr['containment_total']}).")
    L.append(f"- **Assignment agreement**: among addresses inside ≥1 volunteer polygon, "
             f"automated link matches the containing polygon **{addr['agreement_rate']*100:.1f}%** "
             f"of the time (agree {addr['agree']}, disagree {addr['disagree']}, "
             f"no volunteer polygon over point {addr['no_vol_polygon']}).")
    if addr["confusion_top"]:
        cz = ", ".join(f"auto {a}→vol {b} ({c})" for (a, b), c in addr["confusion_top"])
        L.append(f"  - Top off-by pairs: {cz}")
    L.append(f"- Automated **left unlinked**: {addr['unlinked']} NB addresses; of those "
             f"**{addr['unlinked_in_vol']}** fall inside a volunteer polygon "
             f"(coverage the volunteers filled but the pipeline missed).\n")

    report = "\n".join(L)
    (OUT_DIR / "report.md").write_text(report)

    print(report)
    print(f"\nWrote: {OUT_DIR/'report.md'}, {OUT_DIR/'overlay.geojson'}, {OUT_DIR/'overlay.html'}")


if __name__ == "__main__":
    main()
