#!/usr/bin/env python3
"""Stage 05 — build per-station coverage polygons via Voronoi tessellation.

Partitions addresses by settlement, computes a Voronoi diagram over the settlement's
points in UTM (meters), clips cells to the settlement's buffered convex hull, and unions
the cells belonging to each station. Unmatched points get an "unassigned" sentinel so
coverage gaps remain visible. A station spanning settlements is unioned across them.
Output polygons are reprojected to WGS84 GeoJSON.

  reads:  addresses.parquet, links.parquet
  writes: polygons.parquet (station_id, geojson, area_m2, point_count, computed_at)

Usage:
  python3 stage05_voronoi.py
  python3 stage05_voronoi.py --municipalities 80381,70432   # incremental: recompute only
                                                            # these (group_rep) municipalities

With ``--municipalities`` only the settlements touched by those municipalities' stations
are re-tessellated; the FULL point set is still loaded so halo (cross-settlement boundary)
constraints are unchanged, making each recomputed polygon identical to a full run. The new
polygons are merged into the existing complete polygons.parquet (affected stations' rows
replaced, everyone else's kept), so stage06's import stays a correct full reload.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import numpy as np
import polars as pl
import shapely
from pyproj import Transformer
from scipy.spatial import Voronoi, QhullError
from shapely.geometry import MultiPoint, Point, Polygon, mapping
from shapely.ops import transform as shp_transform, unary_union

import config
from common.boundaries import load_muni_boundaries

UNASSIGNED = -1
_TO_WGS84 = Transformer.from_crs(config.UTM_34N, config.WGS84, always_xy=True)


def voronoi_finite_polygons_2d(vor: Voronoi, radius: float) -> tuple[list[list[int]], np.ndarray]:
    """Reconstruct finite Voronoi cells (one per input point, in input order)."""
    new_regions: list[list[int]] = []
    new_vertices = vor.vertices.tolist()
    center = vor.points.mean(axis=0)

    all_ridges: dict[int, list[tuple[int, int, int]]] = {}
    for (p1, p2), (v1, v2) in zip(vor.ridge_points, vor.ridge_vertices):
        all_ridges.setdefault(p1, []).append((p2, v1, v2))
        all_ridges.setdefault(p2, []).append((p1, v1, v2))

    for p1, region_idx in enumerate(vor.point_region):
        vertices = vor.regions[region_idx]
        if all(v >= 0 for v in vertices):
            new_regions.append(vertices)
            continue
        ridges = all_ridges[p1]
        new_region = [v for v in vertices if v >= 0]
        for p2, v1, v2 in ridges:
            if v2 < 0:
                v1, v2 = v2, v1
            if v1 >= 0:
                continue
            t = vor.points[p2] - vor.points[p1]
            t /= np.linalg.norm(t)
            n = np.array([-t[1], t[0]])
            midpoint = vor.points[[p1, p2]].mean(axis=0)
            direction = np.sign(np.dot(midpoint - center, n)) * n
            far_point = vor.vertices[v2] + direction * radius
            new_region.append(len(new_vertices))
            new_vertices.append(far_point.tolist())
        vs = np.asarray([new_vertices[v] for v in new_region])
        c = vs.mean(axis=0)
        angles = np.arctan2(vs[:, 1] - c[1], vs[:, 0] - c[0])
        new_region = [new_region[i] for i in np.argsort(angles)]
        new_regions.append(new_region)
    return new_regions, np.asarray(new_vertices)


def _buffered_by_station(xy: np.ndarray, station_ids: np.ndarray, result: dict[int, list[Polygon]]) -> None:
    """Fallback for tiny/degenerate settlements: each station = its points buffered."""
    for sid in np.unique(station_ids):
        pts = MultiPoint([tuple(p) for p in xy[station_ids == sid]])
        result.setdefault(int(sid), []).append(pts.buffer(config.POLYGON_CLIP_BUFFER_M, quad_segs=2))


def cells_for_settlement(xy: np.ndarray, station_ids: np.ndarray,
                         halo_xy: np.ndarray | None = None) -> dict[int, list[Polygon]]:
    """Return {station_id: [cell polygons]} for one settlement (UTM coords).

    `halo_xy` are nearby points from NEIGHBORING settlements: they participate in the
    tessellation purely as boundary constraints (their cells are discarded), so a cell at
    the settlement edge cannot grow across the boundary into a neighbor's streets — the
    per-settlement partition is otherwise blind to them (e.g. a Zvezdara station's cells
    sprawled into Vračar across Bulevar kralja Aleksandra).

    Each cell is capped to a buffer around ITS OWN generating point, so the polygon hugs
    the addresses instead of sprawling out to the settlement edge. Capping per cell
    (cell ∩ one small octagon) stays cheap."""
    result: dict[int, list[Polygon]] = {}

    # Voronoi needs >=4 non-collinear points; otherwise fall back to buffered points.
    if len(xy) < 4:
        _buffered_by_station(xy, station_ids, result)
        return result

    n_own = len(xy)
    pts = np.vstack([xy, halo_xy]) if halo_xy is not None and len(halo_xy) else xy
    try:
        vor = Voronoi(pts)
    except QhullError:
        _buffered_by_station(xy, station_ids, result)
        return result

    R = config.POLYGON_CLIP_BUFFER_M
    radius = float(np.ptp(pts, axis=0).max()) * 2 + R
    regions, vertices = voronoi_finite_polygons_2d(vor, radius)
    for i in range(n_own):  # halo cells (i >= n_own) are constraints only — discarded
        cap = Point(pts[i]).buffer(R, quad_segs=2)
        poly = Polygon(vertices[regions[i]]).intersection(cap)
        if poly.is_empty:
            continue
        result.setdefault(int(station_ids[i]), []).append(poly)
    return result


def to_wgs84(geom):
    return shp_transform(lambda xs, ys: _TO_WGS84.transform(xs, ys), geom)


def station_clip_geoms(df: pl.DataFrame, boundaries: dict) -> dict[int, object]:
    """{station_id: clip geometry (UTM)} — the union of the official boundaries of the
    municipalities its LINKED addresses lie in (not the station's own municipality, so
    city groups / sectioned docs that legitimately span members stay safe).

    Where the register and the boundary source disagree (e.g. Crveni Krst attributes
    ~660 addresses beyond its polygon), the outlier addresses' own coverage caps are
    unioned back in so clipping never drops a listed address's cell."""
    linked = df.filter(pl.col("station_id") != UNASSIGNED)

    munis_of_station: dict[int, frozenset[str]] = {
        int(sid): frozenset(str(m) for m in ms)
        for sid, ms in linked.group_by("station_id")
        .agg(pl.col("municipality_id").unique()).rows()
    }

    # Outlier caps: linked addresses outside their own municipality polygon.
    outlier_caps: dict[int, list[Polygon]] = {}
    for (mid,), grp in linked.group_by("municipality_id", maintain_order=False):
        boundary = boundaries.get(str(mid))
        if boundary is None:
            continue
        pts = shapely.points(grp["x"].to_numpy(), grp["y"].to_numpy())
        inside = shapely.contains(boundary, pts)
        if inside.all():
            continue
        sids = grp["station_id"].to_numpy()[~inside]
        for sid, p in zip(sids, pts[~inside]):
            outlier_caps.setdefault(int(sid), []).append(
                p.buffer(config.POLYGON_CLIP_BUFFER_M, quad_segs=2)
            )

    # Clip geometry per distinct municipality set (most stations share one muni).
    union_cache: dict[frozenset[str], object] = {}
    clip_geoms: dict[int, object] = {}
    for sid, munis in munis_of_station.items():
        covered = frozenset(m for m in munis if m in boundaries)
        if not covered:
            continue  # no boundary data (until the gradovi layer lands) -> no clipping
        if covered not in union_cache:
            geoms = [boundaries[m] for m in covered]
            u = geoms[0] if len(geoms) == 1 else unary_union(geoms)
            shapely.prepare(u)
            union_cache[covered] = u
        clip = union_cache[covered]
        caps = outlier_caps.get(sid)
        if caps:
            clip = unary_union([clip, *caps])
            shapely.prepare(clip)
        clip_geoms[sid] = clip
    return clip_geoms


def affected_stations(municipalities: set[str]) -> set[int]:
    """Station ids whose group_rep municipality is in ``municipalities`` (the scoping unit
    for incremental recompute: edits to a station only move addresses within its own
    municipality group, so its polygon — and its same-settlement neighbours' — are the
    only ones that can change)."""
    st = pl.read_parquet(config.STATIONS_PARQUET).select("id", "municipality_id")
    return {
        int(sid) for sid, m in zip(st["id"], st["municipality_id"])
        if config.group_rep(str(m)) in municipalities
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-settlement Voronoi -> station polygons.")
    ap.add_argument(
        "--municipalities",
        help="Comma-separated group_rep municipality ids; recompute only their settlements "
             "and merge into the existing polygons.parquet. Default: full rebuild.",
    )
    args = ap.parse_args()
    municipalities = (
        {m.strip() for m in args.municipalities.split(",") if m.strip()}
        if args.municipalities else None
    )

    config.ensure_artifacts()
    addr = pl.read_parquet(config.ADDRESSES_PARQUET).select(
        "id", "settlement_id", "municipality_id", "x", "y"
    )
    links = pl.read_parquet(config.LINKS_PARQUET).select("address_id", "station_id")

    df = addr.join(links, left_on="id", right_on="address_id", how="left").with_columns(
        pl.col("station_id").fill_null(UNASSIGNED)
    )

    # Incremental scope: the stations whose polygons may change, and the settlements that
    # must be re-tessellated to capture them (a station can span several settlements).
    affected: set[int] | None = None
    affected_sett_ids: set | None = None
    if municipalities is not None:
        affected = affected_stations(municipalities)
        aff_df = df.filter(pl.col("station_id").is_in(list(affected)))
        affected_sett_ids = set(aff_df["settlement_id"].unique().to_list())

    boundaries = load_muni_boundaries()
    for g in boundaries.values():
        shapely.prepare(g)
    # Clip geometry is per-station independent, so in scoped mode only build it for the
    # affected stations (skips ~2.5M point-in-polygon checks for everyone else).
    clip_geoms = station_clip_geoms(aff_df if affected is not None else df, boundaries)

    # Global arrays + coarse spatial grid for halo lookup. Bucket size >= the cell cap, so
    # own-bucket + 8 neighbors covers every foreign point a boundary cell could touch.
    X = df["x"].to_numpy()
    Y = df["y"].to_numpy()
    sett_codes, SETT = np.unique(df["settlement_id"].to_numpy(), return_inverse=True)
    B = max(256.0, config.POLYGON_CLIP_BUFFER_M)
    bx = np.floor(X / B).astype(np.int64)
    by = np.floor(Y / B).astype(np.int64)
    bucket_of = bx * 10_000_019 + by
    buckets: dict[int, list[int]] = {}
    for i, k in enumerate(bucket_of):
        buckets.setdefault(int(k), []).append(i)

    idx_by_sett: dict[int, np.ndarray] = {
        int(c): np.flatnonzero(SETT == c) for c in range(len(sett_codes))
    }

    # Accumulate cells per station across settlements.
    station_cells: dict[int, list] = {}
    sids_all = df["station_id"].to_numpy()
    n_compute = 0
    for code, own_idx in idx_by_sett.items():
        if len(own_idx) == 0:
            continue
        # Incremental mode: only re-tessellate settlements touched by affected stations.
        # Every other settlement still contributes its points as halo (above), so the
        # ones we DO compute are identical to a full run.
        if affected_sett_ids is not None and sett_codes[code] not in affected_sett_ids:
            continue
        n_compute += 1
        xy = np.column_stack([X[own_idx], Y[own_idx]])
        sids = sids_all[own_idx]

        # Halo: foreign points in own buckets + their 8 neighbors (boundary constraints).
        halo_xy = None
        if len(own_idx) >= 4:
            cand: list[int] = []
            seen: set[int] = set()
            for b in {(int(bx[i]), int(by[i])) for i in own_idx}:
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        k = (b[0] + dx) * 10_000_019 + (b[1] + dy)
                        if k not in seen:
                            seen.add(k)
                            cand.extend(buckets.get(k, ()))
            cand_arr = np.asarray(cand, dtype=np.int64)
            foreign = cand_arr[SETT[cand_arr] != code]
            if len(foreign):
                halo_xy = np.column_stack([X[foreign], Y[foreign]])

        for sid, cells in cells_for_settlement(xy, sids, halo_xy).items():
            if sid == UNASSIGNED:
                continue
            # Defensive: in scoped mode only ever touch affected stations' polygons, so a
            # stray station in a recomputed settlement can't get a half-recomputed polygon.
            if affected is not None and sid not in affected:
                continue
            station_cells.setdefault(sid, []).extend(cells)
    settlements = sett_codes  # for the summary line

    rows: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()
    n_clipped = 0
    for sid, cells in station_cells.items():
        merged = unary_union(cells).buffer(0)
        if merged.is_empty:
            continue
        clip = clip_geoms.get(sid)
        if clip is not None and not clip.contains(merged):
            clipped = merged.intersection(clip).buffer(0)
            if clipped.is_empty:
                print(f"  WARN station {sid}: clip emptied polygon, keeping unclipped")
            else:
                merged = clipped
                n_clipped += 1
        area = merged.area  # m^2 (UTM)
        # Adaptive simplification: whole-village polygons can exceed the D1 statement
        # budget (~50KB); escalate tolerance until the GeoJSON fits.
        gj = None
        for tol in (config.SIMPLIFY_TOL_M, 15, 40, 100, 250):
            wgs = to_wgs84(merged.simplify(tol, preserve_topology=True))
            gj = json.dumps(mapping(wgs), ensure_ascii=False)
            if len(gj.encode()) <= 45_000:
                break
        rows.append({
            "station_id": sid,
            "geojson": gj,
            "area_m2": round(area, 1),
            "point_count": None,
            "computed_at": now,
        })

    # point_count per station from links.
    counts = dict(links.group_by("station_id").len().rows())
    for r in rows:
        r["point_count"] = int(counts.get(r["station_id"], 0))

    if affected is None:
        pl.DataFrame(rows, infer_schema_length=None).write_parquet(config.POLYGONS_PARQUET)
        n_total = len(rows)
    else:
        # Merge into the existing complete parquet: drop every affected station's old row
        # (so stations whose coverage was emptied vanish) and append the freshly computed
        # ones. Untouched stations are carried over verbatim -> output stays complete and
        # stage06's full delete+reload import remains correct.
        prev = pl.read_parquet(config.POLYGONS_PARQUET)
        kept = prev.filter(~pl.col("station_id").is_in(list(affected)))
        if rows:
            new_rows = pl.DataFrame(rows, infer_schema_length=None).select(prev.columns)
            out = pl.concat([kept, new_rows], how="vertical_relaxed")
        else:
            out = kept
        out.write_parquet(config.POLYGONS_PARQUET)
        n_total = out.height

    # Simplified boundary copy for the review-UI overlay (stage06 ships it to D1).
    brows = [
        {
            "municipality_id": mid,
            "geojson": json.dumps(
                mapping(to_wgs84(g.simplify(config.BOUNDARY_SIMPLIFY_TOL_M, preserve_topology=True))),
                ensure_ascii=False,
            ),
        }
        for mid, g in sorted(boundaries.items())
    ]
    pl.DataFrame(brows).write_parquet(config.MUNI_BOUNDARIES_PARQUET)

    if affected is None:
        print(
            f"  settlements: {len(settlements):,}  station polygons: {len(rows):,}"
            f"  clipped to muni boundary: {n_clipped:,}  boundaries: {len(brows):,}"
        )
    else:
        print(
            f"  [incremental: {len(municipalities)} muni] recomputed {n_compute:,} "
            f"settlements -> {len(rows):,} station polygons; merged into {n_total:,} total"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
