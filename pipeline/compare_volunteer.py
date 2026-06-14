"""Compare the volunteer-drawn coverage polygons against our automated (Voronoi + review) output.

Driven off ``artifacts/volunteer-compare/mapping.csv`` (produced by ``map_volunteer_polygons.py``).
Volunteer files are grouped by ``polygon_muni_id`` — so the three child city-districts merge with
their parent city's file — and each group is compared against the parent muni's R2 polygon file.

Station alignment is **geometry-based** (greedy max-overlap), so it works uniformly across all files,
including the seven that lack a usable ``BR_BM`` station number. Where ``BR_BM`` exists we additionally
record whether the geometric match agrees with the number, as a cross-check.

Per group we compute:
  1. per-station shape agreement on matched pairs (IoU, asymmetric coverage, area, centroid distance)
  2. per-address accuracy (do automated assignments land the address inside the matched human polygon?)
  3. a visual overlay (overlay.geojson + standalone Leaflet overlay.html)

plus an aggregate ranking of all municipalities in ``summary.md``. All area / containment math is in
UTM34N (EPSG:32634), the register-native metric CRS. Read-only against existing artifacts.

Run: .venv/bin/python pipeline/compare_volunteer.py
"""

from __future__ import annotations

import csv
import json
import statistics as st
import warnings
from collections import defaultdict
from pathlib import Path

import polars as pl
from pyproj import Transformer
from shapely import STRtree
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform
from shapely.ops import unary_union
from shapely.validation import make_valid

import config

OUT_DIR = config.ARTIFACTS_DIR / "volunteer-compare"
MAPPING_CSV = OUT_DIR / "mapping.csv"
VOLUNTEER_DIR = config.DATA_DIR / "volunteer-polygons"
R2_POLY_DIR = config.ARTIFACTS_DIR / "r2" / "polygons" / "m"
STATION_BASE_MULT = 100_000  # station_id = int(muni_id) * STATION_BASE_MULT + station number

_to_utm = Transformer.from_crs(4326, 32634, always_xy=True).transform
_to_wgs = Transformer.from_crs(32634, 4326, always_xy=True).transform

# Most volunteer files are WGS84 lat/lon, but a few were exported in UTM34N (projected metres).
# make_valid emits a benign RuntimeWarning on the self-touching rings some hand-drawn polygons
# have; clean() handles them, so quiet the noise.
warnings.filterwarnings("ignore", message="invalid value encountered in make_valid")


# ---------------------------------------------------------------------------
# Geometry helpers (lifted from the original compare_volunteer_nb.py)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def load_mapping() -> list[dict]:
    with MAPPING_CSV.open(newline="") as fh:
        return list(csv.DictReader(fh))


def load_automated(polygon_muni_id: str):
    """Read the R2 per-muni polygon file → (auto_utm, auto_wgs, meta) keyed by station number."""
    path = R2_POLY_DIR / f"{polygon_muni_id}.json"
    data = json.loads(path.read_text())["stations"]
    auto_utm: dict[int, BaseGeometry] = {}
    auto_wgs: dict[int, BaseGeometry] = {}
    meta: dict[int, dict] = {}
    for s in data:
        n = s["number"]
        g_wgs = clean(shape(json.loads(s["geojson"])))
        auto_wgs[n] = g_wgs
        auto_utm[n] = to_utm(g_wgs)
        meta[n] = {"station_id": s["station_id"], "name_cyr": s.get("name_cyr"),
                   "address_cyr": s.get("address_cyr")}
    return auto_utm, auto_wgs, meta


def _first_coord(geom: dict):
    cc = geom["coordinates"]
    while isinstance(cc, list):
        if cc and isinstance(cc[0], (int, float)):
            return cc
        cc = cc[0]
    return None


def _file_is_utm(features: list[dict]) -> bool:
    """A coordinate magnitude > 200 cannot be a lat/lon degree → the file is projected (UTM34N)."""
    for feat in features:
        g = feat.get("geometry")
        if g:
            c = _first_coord(g)
            if c is not None:
                return abs(c[0]) > 200
    return False


def load_volunteer(files: list[str]):
    """Load one group's volunteer features into units, in both UTM34N and WGS84.

    A unit dissolves features sharing a (file, BR_BM) key; features with no usable BR_BM each
    stand alone (one polygon per polling station, as drawn). Null/empty geometries are skipped.
    Coordinate system is detected per file (most are WGS84; a few are UTM34N). Returns
    (vol_utm, vol_wgs, vol_brbm) keyed by a stable unit id."""
    raw_utm: dict[str, list[BaseGeometry]] = defaultdict(list)
    raw_wgs: dict[str, list[BaseGeometry]] = defaultdict(list)
    brbm: dict[str, int | None] = {}
    for fname in files:
        feats = json.loads((VOLUNTEER_DIR / fname).read_text()).get("features", []) or []
        is_utm = _file_is_utm(feats)
        for i, feat in enumerate(feats):
            geom = feat.get("geometry")
            if not geom:
                continue
            g = clean(shape(geom))
            if g.is_empty:
                continue
            g_utm = g if is_utm else to_utm(g)
            g_wgs = clean(shp_transform(_to_wgs, g)) if is_utm else g
            props = feat.get("properties") or {}
            bm = props.get("BR_BM")
            bm = bm if bm not in (None, "") else None
            key = f"{fname}#bm{bm}" if bm is not None else f"{fname}#f{i}"
            raw_utm[key].append(g_utm)
            raw_wgs[key].append(g_wgs)
            brbm[key] = bm
    vol_utm = {k: (g[0] if len(g) == 1 else clean(unary_union(g))) for k, g in raw_utm.items()}
    vol_wgs = {k: (g[0] if len(g) == 1 else clean(unary_union(g))) for k, g in raw_wgs.items()}
    return vol_utm, vol_wgs, brbm


# ---------------------------------------------------------------------------
# 1. Geometry-based matching + per-pair shape metrics
# ---------------------------------------------------------------------------

def match_geometric(auto_utm: dict[int, BaseGeometry], vol_utm: dict[str, BaseGeometry]):
    """Greedy max-overlap pairing of automated stations to volunteer units.

    Returns (matches, unmatched_auto, unmatched_vol) where matches maps auto number -> vol key.
    Candidate pairs come from an STRtree intersection query; we sort all overlapping pairs by
    intersection area (desc) and assign each side at most once."""
    vkeys = list(vol_utm)
    vgeoms = [vol_utm[k] for k in vkeys]
    tree = STRtree(vgeoms)
    pairs: list[tuple[float, int, str]] = []
    for n, ag in auto_utm.items():
        for j in tree.query(ag, predicate="intersects"):
            vk = vkeys[j]
            inter = ag.intersection(vol_utm[vk]).area
            if inter > 0:
                pairs.append((inter, n, vk))
    pairs.sort(key=lambda p: -p[0])
    matches: dict[int, str] = {}
    used_vol: set[str] = set()
    for _, n, vk in pairs:
        if n in matches or vk in used_vol:
            continue
        matches[n] = vk
        used_vol.add(vk)
    unmatched_auto = sorted(set(auto_utm) - set(matches))
    unmatched_vol = [k for k in vkeys if k not in used_vol]
    return matches, unmatched_auto, unmatched_vol


def shape_rows(auto_utm, vol_utm, vol_brbm, matches) -> list[dict]:
    rows = []
    for n, vk in sorted(matches.items()):
        a, v = auto_utm[n], vol_utm[vk]
        inter = a.intersection(v).area
        union = a.union(v).area
        iou = inter / union if union else 0.0
        cov_vol = inter / v.area if v.area else 0.0
        cov_auto = inter / a.area if a.area else 0.0
        ratio = a.area / v.area if v.area else 0.0
        cdist = a.centroid.distance(v.centroid)
        bm = vol_brbm.get(vk)
        rows.append({
            "auto_number": n, "vol_key": vk, "vol_brbm": bm,
            "number_agrees": (bm == n) if bm is not None else None,
            "iou": iou, "auto_km2": a.area / 1e6, "vol_km2": v.area / 1e6,
            "area_ratio": ratio, "coverage_of_volunteer": cov_vol, "coverage_of_auto": cov_auto,
            "centroid_dist_m": cdist, "cause": cause_tag(iou, ratio, cov_vol, cov_auto, cdist),
        })
    return rows


# ---------------------------------------------------------------------------
# 2. Per-address accuracy (number-agnostic, via the geometric matching)
# ---------------------------------------------------------------------------

def address_accuracy(polygon_muni_id: str, group_muni_ids: set[str], vol_utm, matches):
    """Do automated assignments land addresses inside the matched human polygon?

    containment: of addresses auto-linked to a station whose polygon is matched to a volunteer
      unit, the fraction whose point falls inside that matched unit.
    agreement: of addresses that fall inside some volunteer unit and are auto-linked, the fraction
      where the containing unit is the one matched to the address's automated station.
    """
    muni_ids = group_muni_ids | {polygon_muni_id}
    addrs = pl.read_parquet(config.ADDRESSES_PARQUET).filter(
        pl.col("municipality_id").is_in(list(muni_ids))
    ).select(["id", "x", "y"])
    base = int(polygon_muni_id) * STATION_BASE_MULT
    links = pl.read_parquet(config.LINKS_PARQUET).filter(
        (pl.col("station_id") >= base) & (pl.col("station_id") < base + STATION_BASE_MULT)
    )
    auto_num = {r["address_id"]: r["station_id"] - base for r in links.iter_rows(named=True)}

    vkeys = list(vol_utm)
    tree = STRtree([vol_utm[k] for k in vkeys])

    cont_total = cont_inside = 0
    agree = disagree = no_vol = 0
    n_addr = n_linked = 0
    for r in addrs.iter_rows(named=True):
        n_addr += 1
        aid = r["id"]
        n = auto_num.get(aid)
        if n is None:
            continue
        n_linked += 1
        pt = Point(r["x"], r["y"])
        contained = {vkeys[i] for i in tree.query(pt, predicate="intersects")}
        vmatch = matches.get(n)
        if vmatch is not None:
            cont_total += 1
            cont_inside += int(vmatch in contained)
        if contained:
            if vmatch is not None and vmatch in contained:
                agree += 1
            else:
                disagree += 1
        else:
            no_vol += 1
    return {
        "addresses": n_addr, "auto_linked": n_linked,
        "containment_total": cont_total, "containment_inside": cont_inside,
        "containment_rate": cont_inside / cont_total if cont_total else 0.0,
        "agree": agree, "disagree": disagree, "no_vol_polygon": no_vol,
        "agreement_rate": agree / (agree + disagree) if (agree + disagree) else 0.0,
    }


# ---------------------------------------------------------------------------
# 3. Overlay
# ---------------------------------------------------------------------------

_OVERLAY_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>html,body,#map{{height:100%;margin:0}}
.legend{{background:#fff;padding:8px 10px;font:13px sans-serif;line-height:1.5;box-shadow:0 1px 4px rgba(0,0,0,.3);border-radius:4px}}
.legend i{{display:inline-block;width:12px;height:12px;margin-right:6px;vertical-align:middle;opacity:.6}}</style>
</head><body><div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const map = L.map('map');
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
  {{maxZoom:19, attribution:'© OpenStreetMap'}}).addTo(map);
function style(src){{ return src==='auto'
  ? {{color:'#1769aa', weight:2, fillColor:'#2196f3', fillOpacity:.20}}
  : {{color:'#b71c1c', weight:2, fillColor:'#f44336', fillOpacity:.18, dashArray:'4 3'}}; }}
fetch('overlay.geojson').then(r=>r.json()).then(gj=>{{
  const layer = L.geoJSON(gj, {{
    style: f => style(f.properties.source),
    onEachFeature: (f,l)=> l.bindPopup(
      `<b>${{f.properties.label}}</b><br>${{f.properties.source}}<br>IoU: ${{f.properties.iou}}`)
  }}).addTo(map);
  map.fitBounds(layer.getBounds());
}});
const lg = L.control({{position:'topright'}});
lg.onAdd = ()=>{{ const d=L.DomUtil.create('div','legend');
  d.innerHTML='<i style="background:#2196f3"></i>automated<br><i style="background:#f44336"></i>volunteer'; return d; }};
lg.addTo(map);
</script></body></html>"""


def write_overlay(out_dir: Path, title: str, auto_wgs, vol_wgs, vol_brbm, matches, iou_by_num):
    vol_match_iou = {vk: iou_by_num.get(n, 0.0) for n, vk in matches.items()}
    feats = []
    for n, g in sorted(auto_wgs.items()):
        feats.append({"type": "Feature", "geometry": g.__geo_interface__,
                      "properties": {"source": "auto", "label": f"auto BM {n}",
                                     "iou": round(iou_by_num.get(n, 0.0), 3)}})
    for vk, g in sorted(vol_wgs.items()):
        bm = vol_brbm.get(vk)
        feats.append({"type": "Feature", "geometry": g.__geo_interface__,
                      "properties": {"source": "volunteer",
                                     "label": f"vol {'BM '+str(bm) if bm is not None else vk}",
                                     "iou": round(vol_match_iou.get(vk, 0.0), 3)}})
    (out_dir / "overlay.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}))
    (out_dir / "overlay.html").write_text(_OVERLAY_HTML.format(title=title))


# ---------------------------------------------------------------------------
# Per-muni driver + reporting
# ---------------------------------------------------------------------------

def iou_hist(ious: list[float]) -> dict:
    return {
        "<0.2": sum(1 for x in ious if x < 0.2),
        "0.2-0.5": sum(1 for x in ious if 0.2 <= x < 0.5),
        "0.5-0.8": sum(1 for x in ious if 0.5 <= x < 0.8),
        ">=0.8": sum(1 for x in ious if x >= 0.8),
    }


def write_muni_report(out_dir: Path, name: str, polygon_muni_id: str, files: list[str],
                      rows, n_auto, n_vol, unmatched_auto, unmatched_vol, addr) -> dict:
    ious = [r["iou"] for r in rows]
    tot_auto = sum(r["auto_km2"] for r in rows)
    tot_vol = sum(r["vol_km2"] for r in rows)
    worst = sorted(rows, key=lambda r: r["iou"])[:15]
    n_brbm_checked = [r for r in rows if r["number_agrees"] is not None]
    n_brbm_agree = sum(1 for r in n_brbm_checked if r["number_agrees"])

    L = []
    L.append(f"# Volunteer vs automated coverage — {name} (polygons under muni {polygon_muni_id})\n")
    L.append(f"_Volunteer files: {', '.join(files)}_\n")
    L.append("## Aggregate\n")
    L.append(f"- Volunteer units: **{n_vol}**, automated polygons: **{n_auto}**, "
             f"matched pairs: **{len(rows)}** (geometry-based max-overlap)")
    if ious:
        L.append(f"- IoU over matched pairs: mean **{st.mean(ious):.3f}**, median "
                 f"**{st.median(ious):.3f}**, min {min(ious):.3f}, max {max(ious):.3f}")
        L.append(f"- IoU histogram: {iou_hist(ious)}")
        L.append(f"- Total matched area: automated **{tot_auto:.2f} km²**, volunteer "
                 f"**{tot_vol:.2f} km²** (ratio {tot_auto/tot_vol:.2f})" if tot_vol else
                 f"- Total matched area: automated **{tot_auto:.2f} km²**")
    if n_brbm_checked:
        L.append(f"- BR_BM cross-check: of {len(n_brbm_checked)} matched pairs carrying a BR_BM, "
                 f"the geometric match agrees with the number **{n_brbm_agree}** times "
                 f"({n_brbm_agree/len(n_brbm_checked)*100:.0f}%).")
    L.append("")
    L.append("## Unmatched\n")
    L.append(f"- Automated polygons with no overlapping volunteer unit: "
             f"{[f'BM {n}' for n in unmatched_auto] if unmatched_auto else 'none'}")
    L.append(f"- Volunteer units with no overlapping automated polygon: "
             f"{len(unmatched_vol)}{'' if not unmatched_vol else ' ('+', '.join(unmatched_vol[:10])+('…' if len(unmatched_vol)>10 else '')+')'}\n")

    L.append("## Worst 15 matched pairs by IoU\n")
    L.append("| auto BM | vol | IoU | auto km² | vol km² | ratio | cov(vol) | cov(auto) | centroid Δm | likely cause |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in worst:
        vlab = f"BM {r['vol_brbm']}" if r["vol_brbm"] is not None else r["vol_key"].split("#")[-1]
        L.append(f"| {r['auto_number']} | {vlab} | {r['iou']:.3f} | {r['auto_km2']:.3f} | "
                 f"{r['vol_km2']:.3f} | {r['area_ratio']:.2f} | {r['coverage_of_volunteer']:.2f} | "
                 f"{r['coverage_of_auto']:.2f} | {r['centroid_dist_m']:.0f} | {r['cause']} |")
    L.append("")

    L.append("## Per-address accuracy\n")
    L.append(f"- Register addresses in group: **{addr['addresses']}**; automated-linked: "
             f"**{addr['auto_linked']}** "
             f"({addr['auto_linked']/addr['addresses']*100:.1f}%)" if addr["addresses"] else
             "- No register addresses found.")
    if addr["addresses"]:
        L.append(f"- **Containment rate**: of {addr['containment_total']} auto-assigned addresses "
                 f"(station matched to a volunteer unit), **{addr['containment_rate']*100:.1f}%** "
                 f"fall inside that matched unit ({addr['containment_inside']}/{addr['containment_total']}).")
        L.append(f"- **Assignment agreement**: among addresses inside ≥1 volunteer unit, the "
                 f"automated station's matched unit is the containing unit **"
                 f"{addr['agreement_rate']*100:.1f}%** of the time (agree {addr['agree']}, "
                 f"disagree {addr['disagree']}, point outside any volunteer unit {addr['no_vol_polygon']}).")
    L.append("")

    (out_dir / "report.md").write_text("\n".join(L))
    return {
        "polygon_muni_id": polygon_muni_id, "name": name, "files": files,
        "n_auto": n_auto, "n_vol": n_vol, "matched": len(rows),
        "iou_mean": st.mean(ious) if ious else 0.0,
        "iou_median": st.median(ious) if ious else 0.0,
        "area_ratio": (tot_auto / tot_vol) if tot_vol else 0.0,
        "containment_rate": addr["containment_rate"], "agreement_rate": addr["agreement_rate"],
        "unmatched_auto": len(unmatched_auto), "unmatched_vol": len(unmatched_vol),
        "brbm_agree_pct": (n_brbm_agree / len(n_brbm_checked)) if n_brbm_checked else None,
    }


def coverage_supplement_section(groups_summary: list[dict]) -> list[str]:
    """How much area would automated polygons add when supplementing the volunteer set?

    Two granularities: whole municipalities volunteers never mapped (the large gain), and
    spatial gaps within already-covered municipalities (small — volunteers tend to over-draw)."""
    poly = pl.read_parquet(config.POLYGONS_PARQUET).with_columns(
        (pl.col("station_id") // STATION_BASE_MULT).alias("muni"))
    area_by_muni = {str(r["muni"]): r["a"] / 1e6 for r in
                    poly.group_by("muni").agg(pl.col("area_m2").sum().alias("a")).iter_rows(named=True)}
    total_auto = sum(area_by_muni.values())
    covered = {g["polygon_muni_id"] for g in groups_summary}
    covered_auto = sum(area_by_muni.get(m, 0.0) for m in covered)
    uncovered = [m for m in area_by_muni if m not in covered]
    uncovered_auto = total_auto - covered_auto

    # within covered munis: automated area not already under a volunteer unit
    auto_only = [(g["auto_union_km2"] - g["overlap_km2"], g) for g in groups_summary]
    # near-zero overlap == unusable volunteer file (mislocated / wrong CRS): automated effectively
    # supplies the whole municipality there, so separate it from genuine within-muni infill.
    broken = [(a, g) for a, g in auto_only
              if g["auto_union_km2"] and g["overlap_km2"] < 0.05 * g["auto_union_km2"]]
    genuine_gap = sum(a for a, g in auto_only) - sum(a for a, _ in broken)

    L = ["## Coverage supplement (automated on top of volunteer)\n"]
    L.append(f"Automated polygons span **{len(area_by_muni)}** municipalities "
             f"(**{total_auto:,.0f} km²**); volunteers cover **{len(covered)}** of them.\n")
    L.append("**By municipality (the large gain):**")
    L.append(f"- Volunteer-covered: {len(covered)} munis, {covered_auto:,.0f} km² "
             f"({covered_auto/total_auto*100:.0f}% of automated area).")
    L.append(f"- **No volunteer data: {len(uncovered)} munis, {uncovered_auto:,.0f} km² "
             f"({uncovered_auto/total_auto*100:.0f}%)** — supplementing adds all of this.\n")
    L.append("**Within already-covered munis (small — volunteers over-draw):**")
    L.append(f"- Volunteer union {sum(g['vol_union_km2'] for g in groups_summary):,.0f} km² vs "
             f"automated union {sum(g['auto_union_km2'] for g in groups_summary):,.0f} km² "
             f"(overlap {sum(g['overlap_km2'] for g in groups_summary):,.0f} km²).")
    L.append(f"- Genuine automated-only infill: **~{genuine_gap:,.0f} km²**; plus "
             f"{sum(a for a, _ in broken):,.0f} km² from {len(broken)} unusable volunteer files "
             f"({', '.join(g['name'] for _, g in broken)}) where automated supplies the whole muni.\n")
    L.append("Net: the supplement is overwhelmingly **breadth** — the "
             f"{uncovered_auto:,.0f} km² of unmapped municipalities — not infill of covered ones.\n")
    L.append("### Municipalities with no volunteer coverage (largest automated area first)\n")
    L.append("| muni | area km² |")
    L.append("|---|---|")
    names = {str(r["id"]): r["name_lat"]
             for r in pl.read_parquet(config.MUNICIPALITIES_PARQUET).iter_rows(named=True)}
    for m in sorted(uncovered, key=lambda m: -area_by_muni[m]):
        L.append(f"| {m} {names.get(m, '')} | {area_by_muni[m]:,.0f} |")
    L.append("")
    return L


def write_summary(groups_summary: list[dict]):
    ranked = sorted(groups_summary, key=lambda g: g["iou_mean"])
    L = []
    L.append("# Volunteer vs automated coverage — aggregate\n")
    L.append(f"Compared **{len(groups_summary)}** municipalities. Mapping: "
             f"`mapping.csv`. Per-muni detail: `m/<id>/report.md` + `overlay.html`.\n")
    all_ious = [g["iou_mean"] for g in groups_summary]
    L.append(f"- Mean of per-muni mean IoU: **{st.mean(all_ious):.3f}** "
             f"(median {st.median(all_ious):.3f})")
    L.append(f"- Municipalities with mean IoU < 0.4: "
             f"**{sum(1 for g in groups_summary if g['iou_mean'] < 0.4)}**\n")
    L += coverage_supplement_section(groups_summary)
    L.append("## Ranked by mean IoU (worst first)\n")
    L.append("| muni | name | mean IoU | median | matched/auto/vol | area ratio | containment | agreement | BR_BM✓ |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for g in ranked:
        bm = f"{g['brbm_agree_pct']*100:.0f}%" if g["brbm_agree_pct"] is not None else "—"
        L.append(f"| {g['polygon_muni_id']} | {g['name']} | {g['iou_mean']:.3f} | "
                 f"{g['iou_median']:.3f} | {g['matched']}/{g['n_auto']}/{g['n_vol']} | "
                 f"{g['area_ratio']:.2f} | {g['containment_rate']*100:.0f}% | "
                 f"{g['agreement_rate']*100:.0f}% | {bm} |")
    (OUT_DIR / "summary.md").write_text("\n".join(L))


def muni_name(polygon_muni_id: str) -> str:
    muni = pl.read_parquet(config.MUNICIPALITIES_PARQUET).filter(
        pl.col("id") == int(polygon_muni_id))
    return muni["name_lat"][0] if muni.height else polygon_muni_id


def main() -> None:
    mapping = load_mapping()
    # group volunteer files by polygon_muni_id (skip unmatched / no-polygon rows)
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in mapping:
        if r["polygon_muni_id"]:
            groups[r["polygon_muni_id"]].append(r)

    names = pl.read_parquet(config.MUNICIPALITIES_PARQUET)
    name_by_id = {str(r["id"]): r["name_lat"] for r in names.iter_rows(named=True)}

    summary = []
    for polygon_muni_id, rs in sorted(groups.items()):
        files = [r["file"] for r in rs]
        group_muni_ids = {r["muni_id"] for r in rs if r["muni_id"]}
        name = name_by_id.get(polygon_muni_id, polygon_muni_id)

        auto_utm, auto_wgs, _meta = load_automated(polygon_muni_id)
        vol_utm, vol_wgs, vol_brbm = load_volunteer(files)
        matches, unmatched_auto, unmatched_vol = match_geometric(auto_utm, vol_utm)
        rows = shape_rows(auto_utm, vol_utm, vol_brbm, matches)
        iou_by_num = {r["auto_number"]: r["iou"] for r in rows}
        addr = address_accuracy(polygon_muni_id, group_muni_ids, vol_utm, matches)

        muni_dir = OUT_DIR / "m" / polygon_muni_id
        muni_dir.mkdir(parents=True, exist_ok=True)
        write_overlay(muni_dir, f"{name}: automated vs volunteer coverage",
                      auto_wgs, vol_wgs, vol_brbm, matches, iou_by_num)
        g = write_muni_report(muni_dir, name, polygon_muni_id, files, rows,
                              len(auto_utm), len(vol_utm), unmatched_auto, unmatched_vol, addr)
        # Union areas (for the coverage-supplement aggregate). Automated cells tessellate a
        # settlement so they barely overlap, but volunteer units can; union both to be safe.
        auto_u = unary_union(list(auto_utm.values())) if auto_utm else None
        vol_u = unary_union(list(vol_utm.values())) if vol_utm else None
        g["auto_union_km2"] = (auto_u.area / 1e6) if auto_u else 0.0
        g["vol_union_km2"] = (vol_u.area / 1e6) if vol_u else 0.0
        g["overlap_km2"] = (auto_u.intersection(vol_u).area / 1e6) if (auto_u and vol_u) else 0.0
        summary.append(g)
        print(f"  {polygon_muni_id} {name:<24} matched {g['matched']:>3}/{g['n_auto']:<3} "
              f"mean IoU {g['iou_mean']:.3f}  containment {g['containment_rate']*100:.0f}%")

    write_summary(summary)
    print(f"\nWrote {len(summary)} municipality reports + {OUT_DIR/'summary.md'}")


if __name__ == "__main__":
    main()
