#!/usr/bin/env python3
"""Compare two pipeline runs — by default the pre-BM backup (RIK 2022 docs) vs. the current
artifacts (bm.itblokada.org export) — and write a report for deciding whether to promote.

  reads:  <old>/{stations,segments,links,polygons}.parquet, artifacts/ (same set),
          artifacts/addresses.parquet, artifacts/municipalities.parquet
  writes: artifacts/bm-compare/REPORT.md, artifacts/bm-compare/per_municipality.csv,
          artifacts/bm-compare/station_pairs.csv

Stations are paired by (municipality, printed number) and CONFIRMED by address
(`clean_volunteer_data.same_address`) — numbering drifted between 2022 and the current
register, so a number-only join would compare different stations. Polygon agreement is the
IoU of each confirmed pair (computed on lon/lat; IoU is invariant under the local affine
scaling, so no reprojection is needed).

Usage:
  python3 compare_sources.py                       # newest backups/*-pre-bm vs artifacts/
  python3 compare_sources.py --old ../backups/2026-11-11-pre-bm/artifacts
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

import polars as pl
import shapely
from shapely.geometry import shape

import config
from clean_volunteer_data import same_address

OUT_DIR = config.ARTIFACTS_DIR / "bm-compare"


def load(d: Path) -> dict[str, pl.DataFrame]:
    return {n: pl.read_parquet(d / f"{n}.parquet") for n in ("stations", "segments", "links", "polygons")}


def reason_counts(segs: pl.DataFrame) -> collections.Counter:
    c: collections.Counter = collections.Counter()
    for rr in segs.filter(pl.col("needs_review") == 1)["review_reason"].to_list():
        for code in (rr or "").split(","):
            if code:
                c[code.split(":")[0]] += 1
    return c


def run_stats(r: dict[str, pl.DataFrame]) -> dict:
    st, sg, ln, pg = r["stations"], r["segments"], r["links"], r["polygons"]
    return {
        "stations": st.height,
        "municipalities": st["municipality_id"].n_unique(),
        "segments": sg.height,
        "linked addresses": ln["address_id"].n_unique(),
        "stations with addresses": ln["station_id"].n_unique(),
        "stations with polygon": pg["station_id"].n_unique(),
        "polygon area km2": round(pg["area_m2"].sum() / 1e6, 1),
        "segments needing review": int(sg["needs_review"].sum()),
        "stations needing review": sg.filter(pl.col("needs_review") == 1)["station_id"].n_unique(),
        "unresolved streets": sg.filter(
            pl.col("street_id").is_null() & (pl.col("kind") != "no_street")).height,
    }


def per_muni(r: dict[str, pl.DataFrame]) -> dict[str, dict]:
    st, sg, ln, pg = r["stations"], r["segments"], r["links"], r["polygons"]
    muni_of = dict(zip(st["id"], st["municipality_id"]))
    out: dict[str, dict] = collections.defaultdict(lambda: collections.Counter())
    for m in st["municipality_id"]:
        out[m]["stations"] += 1
    for sid in pg["station_id"].unique():
        if sid in muni_of:
            out[muni_of[sid]]["polygons"] += 1
    for sid, n in ln.group_by("station_id").len().iter_rows():
        if sid in muni_of:
            out[muni_of[sid]]["links"] += n
    for sid, n in sg.filter(pl.col("needs_review") == 1).group_by("station_id").len().iter_rows():
        if sid in muni_of:
            out[muni_of[sid]]["review"] += n
    return out


def pair_stations(old: pl.DataFrame, new: pl.DataFrame) -> list[dict]:
    """(municipality, number) join, confirmed by address."""
    old_by = {(m, n): (i, a) for i, m, n, a in zip(
        old["id"], old["municipality_id"], old["number"], old["address_cyr"])}
    pairs = []
    for i, m, n, a in zip(new["id"], new["municipality_id"], new["number"], new["address_cyr"]):
        hit = old_by.get((m, n))
        if hit is None:
            continue
        pairs.append({"municipality_id": m, "number": n, "old_id": hit[0], "new_id": i,
                      "same_address": bool(a and hit[1] and same_address(a, hit[1]))})
    return pairs


def geoms(pg: pl.DataFrame, ids: set[int]) -> dict[int, object]:
    out = {}
    for sid, gj in zip(pg["station_id"], pg["geojson"]):
        if sid in ids:
            g = shape(json.loads(gj))
            out[sid] = g if g.is_valid else shapely.make_valid(g)
    return out


def md_table(head: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" if i == 0 else "---:" for i in range(len(head))) + "|"]
    out += ["| " + " | ".join(f"{v:,}" if isinstance(v, int) else str(v) for v in r) + " |" for r in rows]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare the previous station source with the current run.")
    ap.add_argument("--old", type=Path, help="Directory with the OLD run's parquets.")
    args = ap.parse_args()
    old_dir = args.old
    if old_dir is None:
        cands = sorted((config.ROOT_DIR / "backups").glob("*-pre-bm/artifacts"))
        if not cands:
            raise SystemExit("no backups/*-pre-bm/artifacts found; pass --old")
        old_dir = cands[-1]

    old, new = load(old_dir), load(config.ARTIFACTS_DIR)
    munis = pl.read_parquet(config.MUNICIPALITIES_PARQUET)
    muni_name = dict(zip(munis["id"].cast(str), munis["name_lat"]))
    addr = pl.read_parquet(config.ADDRESSES_PARQUET, columns=["id", "municipality_id"])
    addr_per_muni = dict(addr.group_by("municipality_id").len().iter_rows())
    n_addr = addr.height

    so, sn = run_stats(old), run_stats(new)
    ro, rn = reason_counts(old["segments"]), reason_counts(new["segments"])
    mo, mn = per_muni(old), per_muni(new)

    pairs = pair_stations(old["stations"], new["stations"])
    confirmed = [p for p in pairs if p["same_address"]]
    go = geoms(old["polygons"], {p["old_id"] for p in confirmed})
    gn = geoms(new["polygons"], {p["new_id"] for p in confirmed})
    for p in confirmed:
        a, b = go.get(p["old_id"]), gn.get(p["new_id"])
        if a is None or b is None:
            p["iou"] = None
            continue
        u = a.union(b).area
        p["iou"] = round(a.intersection(b).area / u, 3) if u else None
    ious = sorted(p["iou"] for p in confirmed if p.get("iou") is not None)

    def q(f: float) -> float:
        return ious[min(len(ious) - 1, int(f * len(ious)))] if ious else float("nan")

    L: list[str] = [
        "# Station source comparison — RIK 2022 docs vs bm.itblokada.org", "",
        f"- old run: `{old_dir}`", f"- new run: `{config.ARTIFACTS_DIR}`",
        f"- address register: {n_addr:,} active addresses", "",
        "## Totals", "",
    ]
    L += md_table(["metric", "old (RIK)", "new (BM)", "change"],
                  [[k, so[k], sn[k], f"{sn[k] - so[k]:+,}" if isinstance(sn[k], int) else f"{sn[k] - so[k]:+.1f}"]
                   for k in so])
    L += ["", f"Share of register addresses explicitly linked to a station: "
              f"old {so['linked addresses'] / n_addr:.1%}, new {sn['linked addresses'] / n_addr:.1%}. "
              "The BM text lists only addresses of registered voters, while the RIK text claimed whole "
              "streets/ranges, so fewer explicit links is expected; unlinked houses are assigned by the "
              "Voronoi step either way.", ""]

    L += ["## Why segments need review", ""]
    L += md_table(["reason", "old", "new"],
                  [[k, ro.get(k, 0), rn.get(k, 0)] for k in sorted(set(ro) | set(rn), key=lambda k: -rn.get(k, 0))])

    ns = new["segments"].filter(pl.col("kind") == "no_street")
    ns_links = new["links"].filter(pl.col("match_method") == "no_street")
    L += ["", "## «НЕМА УЛИЦЕ» (no-street) clauses", "",
          f"- segments: {ns.height:,} on {ns['station_id'].n_unique():,} stations",
          f"- houses assigned through them: {ns_links.height:,} "
          f"({ns_links['station_id'].n_unique():,} stations received at least one)",
          f"- without a home settlement (flagged): "
          f"{ns.filter(pl.col('review_reason').str.contains('no_home_settlement')).height:,}", ""]

    L += ["## Polygon agreement for stations present in both", "",
          f"- paired by municipality + number: {len(pairs):,}; address-confirmed: {len(confirmed):,}",
          f"- with a polygon in both runs: {len(ious):,}"]
    if ious:
        L += [f"- IoU median {q(0.5):.2f}, p25 {q(0.25):.2f}, p10 {q(0.1):.2f}; "
              f"IoU < 0.3: {sum(1 for v in ious if v < 0.3):,}, IoU ≥ 0.7: {sum(1 for v in ious if v >= 0.7):,}"]
    L += [""]

    rows = []
    for m in sorted(set(mo) | set(mn), key=lambda m: muni_name.get(m, m)):
        o, n = mo.get(m, {}), mn.get(m, {})
        mi = [p["iou"] for p in confirmed if p["municipality_id"] == m and p.get("iou") is not None]
        rows.append({
            "municipality_id": m, "name": muni_name.get(m, ""),
            "stations_old": o.get("stations", 0), "stations_new": n.get("stations", 0),
            "polygons_old": o.get("polygons", 0), "polygons_new": n.get("polygons", 0),
            "links_old": o.get("links", 0), "links_new": n.get("links", 0),
            "register_addresses": addr_per_muni.get(m, 0),
            "review_old": o.get("review", 0), "review_new": n.get("review", 0),
            "median_iou": round(sorted(mi)[len(mi) // 2], 2) if mi else None,
        })
    L += ["## Stations without a polygon (new run)", ""]
    have = set(new["polygons"]["station_id"].to_list())
    miss = new["stations"].filter(~pl.col("id").is_in(list(have)))
    L += [f"{miss.height:,} of {new['stations'].height:,}. By municipality: " + ", ".join(
        f"{muni_name.get(m, m)} {n}" for m, n in
        miss.group_by("municipality_id").len().sort("len", descending=True).head(15).iter_rows()) + ".", ""]

    L += ["## Per municipality", ""]
    L += md_table(["municipality", "stations old→new", "polygons old→new", "linked % old→new",
                   "review old→new", "median IoU"],
                  [[f"{r['name']} ({r['municipality_id']})",
                    f"{r['stations_old']}→{r['stations_new']}", f"{r['polygons_old']}→{r['polygons_new']}",
                    (f"{r['links_old'] / r['register_addresses']:.0%}→{r['links_new'] / r['register_addresses']:.0%}"
                     if r["register_addresses"] else "–"),
                    f"{r['review_old']}→{r['review_new']}",
                    "–" if r["median_iou"] is None else r["median_iou"]] for r in rows])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "REPORT.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    pl.DataFrame(rows).write_csv(OUT_DIR / "per_municipality.csv")
    pl.DataFrame(pairs, infer_schema_length=None).write_csv(OUT_DIR / "station_pairs.csv")
    print(f"  wrote {OUT_DIR / 'REPORT.md'}")
    for k in so:
        print(f"  {k:<26} {so[k]:>12,} -> {sn[k]:>12,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
