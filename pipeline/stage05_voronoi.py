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
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import numpy as np
import polars as pl
from pyproj import Transformer
from scipy.spatial import Voronoi, QhullError
from shapely.geometry import MultiPoint, Point, Polygon, mapping
from shapely.ops import transform as shp_transform, unary_union

import config

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


def cells_for_settlement(xy: np.ndarray, station_ids: np.ndarray) -> dict[int, list[Polygon]]:
    """Return {station_id: [cell polygons]} for one settlement (UTM coords).

    Each Voronoi cell is capped to a buffer around ITS OWN generating point, so the
    polygon hugs the addresses instead of sprawling out to the settlement edge. Capping
    per cell (cell ∩ one small octagon) stays cheap; clipping to a settlement-wide
    point-cloud boundary does not (that geometry has thousands of vertices)."""
    result: dict[int, list[Polygon]] = {}

    # Voronoi needs >=4 non-collinear points; otherwise fall back to buffered points.
    if len(xy) < 4:
        _buffered_by_station(xy, station_ids, result)
        return result
    try:
        vor = Voronoi(xy)
    except QhullError:
        _buffered_by_station(xy, station_ids, result)
        return result

    R = config.POLYGON_CLIP_BUFFER_M
    radius = float(np.ptp(xy, axis=0).max()) * 2 + R
    regions, vertices = voronoi_finite_polygons_2d(vor, radius)
    for i, region in enumerate(regions):
        cap = Point(xy[i]).buffer(R, quad_segs=2)
        poly = Polygon(vertices[region]).intersection(cap)
        if poly.is_empty:
            continue
        result.setdefault(int(station_ids[i]), []).append(poly)
    return result


def to_wgs84(geom):
    return shp_transform(lambda xs, ys: _TO_WGS84.transform(xs, ys), geom)


def main() -> int:
    config.ensure_artifacts()
    addr = pl.read_parquet(config.ADDRESSES_PARQUET).select("id", "settlement_id", "x", "y")
    links = pl.read_parquet(config.LINKS_PARQUET).select("address_id", "station_id")

    df = addr.join(links, left_on="id", right_on="address_id", how="left").with_columns(
        pl.col("station_id").fill_null(UNASSIGNED)
    )

    # Accumulate cells per station across settlements.
    station_cells: dict[int, list] = {}
    settlements = df["settlement_id"].unique().to_list()
    for set_id in settlements:
        sub = df.filter(pl.col("settlement_id") == set_id)
        xy = np.column_stack([sub["x"].to_numpy(), sub["y"].to_numpy()])
        sids = sub["station_id"].to_numpy()
        if len(xy) == 0:
            continue
        for sid, cells in cells_for_settlement(xy, sids).items():
            if sid == UNASSIGNED:
                continue
            station_cells.setdefault(sid, []).extend(cells)

    rows: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()
    for sid, cells in station_cells.items():
        merged = unary_union(cells).buffer(0)
        if merged.is_empty:
            continue
        area = merged.area  # m^2 (UTM)
        wgs = to_wgs84(merged.simplify(config.SIMPLIFY_TOL_M, preserve_topology=True))
        rows.append({
            "station_id": sid,
            "geojson": json.dumps(mapping(wgs), ensure_ascii=False),
            "area_m2": round(area, 1),
            "point_count": None,
            "computed_at": now,
        })

    # point_count per station from links.
    counts = dict(links.group_by("station_id").len().rows())
    for r in rows:
        r["point_count"] = int(counts.get(r["station_id"], 0))

    pl.DataFrame(rows, infer_schema_length=None).write_parquet(config.POLYGONS_PARQUET)
    print(f"  settlements: {len(settlements):,}  station polygons: {len(rows):,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
