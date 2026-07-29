#!/usr/bin/env python3
"""Stage 05 — build per-station coverage polygons via Voronoi tessellation.

Partitions addresses by settlement, computes a Voronoi diagram over the settlement's MATCHED
points in UTM (meters), and gives each station its FULL Voronoi territory within the settlement:
the station's cells are unioned and clipped to the official naselje boundary. Voronoi cells tile
space, so the per-station regions are disjoint and NEVER overlap another station (the project's
one-address-one-station invariant), while filling the village into contiguous simple shapes
instead of many tiny per-address circles. A station spanning settlements is unioned across them.

Land still unclaimed after that — settlements are tessellated independently, so whatever inside
naselje A is nearer an address in naselje B is dropped by both — is then split between the
municipality's stations by nearest matched address (`residual_shares`), each piece going to a
polygon it actually merges with (`attach_owner`). That closes the corridors which cut a village
station into islands and grows every polygon out to the municipality boundary.

After the union + despike, interior holes are CLOSED (``close_unclaimed_holes``) for cleaner
volunteer hand-editing — unless another station claims the enclosed land (``hole_is_claimed``:
a foreign matched address inside, a foreign polygon covering it, or a settlement this station
has no coverage in), so filling can never cover a foreign address or overlap a neighbour.
Output is reprojected to WGS84 GeoJSON.

  reads:  addresses.parquet, links.parquet, station_settlement_claims.parquet, osm_claims.parquet
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
import time
from datetime import datetime, timezone

import numpy as np
import polars as pl
import shapely
from pyproj import Transformer
from scipy.spatial import Voronoi, QhullError
from shapely.geometry import MultiPoint, MultiPolygon, Point, Polygon, mapping, shape
from shapely.ops import transform as shp_transform, unary_union

import config
from common.boundaries import load_muni_boundaries, load_settlement_boundaries

UNASSIGNED = -1
_TO_WGS84 = Transformer.from_crs(config.UTM_34N, config.WGS84, always_xy=True)
_TO_UTM = Transformer.from_crs(config.WGS84, config.UTM_34N, always_xy=True)


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


def _buffered_by_station(xy: np.ndarray, station_ids: np.ndarray, result: dict[int, list[Polygon]],
                         boundary=None) -> None:
    """Fallback for tiny/degenerate settlements (<4 points or a Qhull failure).

    With a settlement `boundary` the few points split it (so the station(s) fill the village
    without overlapping): a lone station gets the whole boundary, otherwise a shapely Voronoi
    over the points partitions it. Without a boundary each station is just its buffered points."""
    if boundary is not None and not boundary.is_empty and len(xy):
        stations = np.unique(station_ids)
        if len(stations) == 1:
            result.setdefault(int(stations[0]), []).append(boundary)
            return
        try:
            mp = MultiPoint([tuple(p) for p in xy])
            cells = list(shapely.voronoi_polygons(mp, extend_to=boundary.envelope).geoms)
            shapely.prepare(boundary)
            for cell in cells:
                for i in range(len(xy)):
                    if cell.intersects(Point(xy[i])):
                        part = cell.intersection(boundary)
                        if not part.is_empty:
                            result.setdefault(int(station_ids[i]), []).append(part)
                        break
            return
        except Exception:
            pass  # fall through to plain buffers
    for sid in np.unique(station_ids):
        pts = MultiPoint([tuple(p) for p in xy[station_ids == sid]])
        result.setdefault(int(sid), []).append(pts.buffer(config.POLYGON_CELL_CAP_M, quad_segs=2))


def cells_for_settlement(xy: np.ndarray, station_ids: np.ndarray,
                         halo_xy: np.ndarray | None = None, boundary=None) -> dict[int, list[Polygon]]:
    """Return {station_id: [region polygons]} for one settlement (UTM coords).

    With a settlement `boundary` (the official naselje polygon), each station is given its FULL
    Voronoi territory within the settlement: the station's Voronoi cells are unioned and clipped
    to the boundary. Because Voronoi cells tile space, the per-station regions are disjoint and
    together fill the settlement — contiguous simple shapes (few parts) that NEVER overlap
    another station. The boundary bounds the growth to the village (over-covering empty land
    inside it, not sprawling muni-wide). `halo_xy` (foreign points from OTHER settlements that
    fall within this boundary's bbox) still participates as Voronoi constraints — its cells are
    discarded — so where two naselje boundaries overlap, a territory stops at the bisector before
    reaching a neighbouring station's address rather than covering it.

    Without a boundary (should not happen — naselje geometry covers 100% of address settlements —
    but kept as a defensive fallback) it reverts to the older behaviour: each cell capped to an
    octagon around its own point, with `halo_xy` constraining edge cells."""
    result: dict[int, list[Polygon]] = {}

    # Voronoi needs >=4 non-collinear points; otherwise fall back to buffered/split points.
    if len(xy) < 4:
        _buffered_by_station(xy, station_ids, result, boundary)
        return result

    use_boundary = boundary is not None and not boundary.is_empty
    pts = np.vstack([xy, halo_xy]) if halo_xy is not None and len(halo_xy) else xy
    try:
        vor = Voronoi(pts)
    except QhullError:
        _buffered_by_station(xy, station_ids, result, boundary)
        return result

    n_own = len(xy)
    if use_boundary:
        # Territory fill: union each OWN station's Voronoi cells (halo cells i>=n_own are
        # constraints only — discarded), then clip to the settlement boundary ONCE per station
        # (cheap vs per-cell). Interior unions sit inside the boundary (a prepared `contains`
        # check skips the intersection); only edge territories are actually clipped.
        radius = float(np.ptp(pts, axis=0).max()) * 2 + 1000.0
        regions, vertices = voronoi_finite_polygons_2d(vor, radius)
        shapely.prepare(boundary)
        # Split own points by whether they fall inside the naselje boundary. In-boundary cells
        # fill the settlement (clipped to it); the ~0.15% of addresses OUTSIDE their boundary
        # (register coords vs boundary source disagree) keep their tight Voronoi cell (cell ∩
        # cap) instead — a raw cap there isn't Voronoi-constrained and would overlap neighbours.
        # Every piece is a subset of that address's Voronoi cell, so the partition never overlaps
        # and no listed address loses coverage.
        inside = shapely.contains(boundary, shapely.points(xy[:, 0], xy[:, 1]))
        by_in: dict[int, list[Polygon]] = {}
        by_out: dict[int, list[Polygon]] = {}
        for i in range(n_own):
            sid = int(station_ids[i])
            cell = Polygon(vertices[regions[i]])
            if inside[i]:
                by_in.setdefault(sid, []).append(cell)
            else:
                cap = Point(xy[i]).buffer(config.POLYGON_CELL_CAP_M, quad_segs=2)
                piece = cell.intersection(cap)
                if not piece.is_empty:
                    by_out.setdefault(sid, []).append(piece)
        for sid in set(by_in) | set(by_out):
            geoms: list[Polygon] = []
            ins = by_in.get(sid)
            if ins:
                terr = unary_union(ins)
                if not boundary.contains(terr):
                    terr = terr.intersection(boundary)
                if not terr.is_empty:
                    geoms.append(terr)
            geoms.extend(by_out.get(sid, ()))
            if geoms:
                result.setdefault(sid, []).append(
                    geoms[0] if len(geoms) == 1 else unary_union(geoms)
                )
        return result

    R = config.POLYGON_CELL_CAP_M
    radius = float(np.ptp(pts, axis=0).max()) * 2 + R
    regions, vertices = voronoi_finite_polygons_2d(vor, radius)
    for i in range(n_own):  # halo cells (i >= n_own) are constraints only — discarded
        cap = Point(pts[i]).buffer(R, quad_segs=2)
        poly = Polygon(vertices[regions[i]]).intersection(cap)
        if poly.is_empty:
            continue
        result.setdefault(int(station_ids[i]), []).append(poly)
    return result


def partition_settlement(naselje, xy: np.ndarray, sids: np.ndarray) -> dict[int, object]:
    """Split a whole-settlement boundary among the stations that EACH claim the whole
    settlement, so together they rebuild it without overlapping.

    When two+ polling stations claim the same settlement but also list individual streets,
    giving each the full naselje boundary makes their polygons overlap across the whole
    village. Instead, partition the naselje by the stations' OWN coverage: a Voronoi over
    their won addresses (`xy`/`sids`), each address's cell assigned to its station, unioned
    per station and clipped to the naselje. Every point of the naselje goes to the station
    whose nearest listed address is closest — including the streets neither listed explicitly
    (they fall to the geographically nearest claimant). Returns {station_id: portion}."""
    stations = np.unique(sids)
    if len(stations) == 1:
        return {int(stations[0]): naselje}
    nx0, ny0, nx1, ny1 = naselje.bounds
    radius = max(float(np.ptp(xy, axis=0).max()) * 2.0,
                 float(np.hypot(nx1 - nx0, ny1 - ny0))) + 1000.0
    cells_by_station: dict[int, list] = {}
    if len(xy) >= 4:
        try:
            vor = Voronoi(xy)
            regions, vertices = voronoi_finite_polygons_2d(vor, radius)
            for i in range(len(xy)):
                cells_by_station.setdefault(int(sids[i]), []).append(Polygon(vertices[regions[i]]))
        except QhullError:
            cells_by_station = {}
    if not cells_by_station:
        # Few / collinear seeds: fall back to shapely's Voronoi (handles n>=2), matching each
        # cell to the seed it contains.
        mp = MultiPoint([tuple(p) for p in xy])
        try:
            cells = list(shapely.voronoi_polygons(mp, extend_to=naselje.envelope).geoms)
        except Exception:
            return {}
        for cell in cells:
            for i in range(len(xy)):
                if cell.intersects(Point(xy[i])):
                    cells_by_station.setdefault(int(sids[i]), []).append(cell)
                    break
    out: dict[int, object] = {}
    for sid, cells in cells_by_station.items():
        region = unary_union(cells).buffer(0).intersection(naselje).buffer(0)
        if not region.is_empty:
            out[sid] = region
    return out


def residual_shares(residual, xy: np.ndarray, sids: np.ndarray) -> dict[int, object]:
    """Split a municipality's UNCLAIMED land between its stations by nearest matched address.

    {station_id: geometry}. `residual` is the part of the municipality no station's polygon
    covers; `xy`/`sids` are the municipality's matched addresses and their owners. A Voronoi over
    those addresses assigns every point of the residual to the station owning the address nearest
    to it, so the shares tile the residual and — the invariant again — a share can never contain
    another station's matched address, because each Voronoi cell holds exactly one address.

    Why any residual exists at all: settlements are tessellated INDEPENDENTLY and each is clipped
    to its own naselje boundary, so land inside naselje A that happens to be nearer an address in
    naselje B is dropped by both (A never claims it, B's cell is clipped away). It shows up as the
    empty corridors that cut a village station into islands and the bays along the municipality
    edge. Filling it is what grows a polygon out to the municipality boundary."""
    # Sub-metre slivers along shared naselje borders dominate the part count and are invisible
    # on the map; dropping them up front is what keeps this pass affordable.
    parts = [p for p in _parts(residual) if p.area >= config.POLYGON_MIN_FILL_PIECE_M2]
    if not parts or len(xy) < 4:
        return {}
    try:
        vor = Voronoi(xy)
    except QhullError:
        return {}
    radius = float(np.ptp(xy, axis=0).max()) * 2 + 1000.0
    regions, vertices = voronoi_finite_polygons_2d(vor, radius)
    # Degenerate regions (co-located addresses) are skipped, so keep the owner alongside the cell.
    cells: list[Polygon] = []
    owners: list[int] = []
    for i, region in enumerate(regions):
        if len(region) < 3:
            continue
        cell = Polygon(vertices[region])
        if cell.is_empty:
            continue
        cells.append(cell)
        owners.append(int(sids[i]))
    if not cells:
        return {}

    # Union each station's cells BEFORE clipping to the residual. (A ∪ B) ∩ R == (A ∩ R) ∪ (B ∩ R),
    # so the result is identical — but it turns tens of thousands of cell×residual intersections
    # into a few dozen region×residual ones. Doing it per cell made single municipalities take
    # minutes (70645: 460 s).
    cells_by_station: dict[int, list] = {}
    for cell, sid in zip(cells, owners):
        cells_by_station.setdefault(sid, []).append(cell)

    tree = shapely.STRtree(parts)
    out: dict[int, object] = {}
    for sid, own_cells in cells_by_station.items():
        region = own_cells[0] if len(own_cells) == 1 else unary_union(own_cells)
        got: list[Polygon] = []
        for k in tree.query(region, predicate="intersects"):
            try:
                piece = region.intersection(parts[k])
            except Exception:  # pragma: no cover - defensive against GEOS edge cases
                continue
            # An intersection can come back as a collection with edge-contact lines; keep areas.
            for p in getattr(piece, "geoms", [piece]):
                if p.geom_type == "Polygon" and not p.is_empty:
                    got.append(p)
        if got:
            out[sid] = got[0] if len(got) == 1 else unary_union(got)
    return out


def attach_owner(piece: Polygon, preferred: int, tree, geoms: list, ids: list) -> int | None:
    """Which station should absorb this piece of unclaimed land — or None to leave it alone.

    `preferred` is the Voronoi owner (the station with the nearest matched address). A piece is
    only useful to a station whose polygon it MERGES with: one that merely lands nearby becomes
    yet another island, which is the opposite of what the fill is for. So the piece goes to
    `preferred` when it shares a border with that station's polygon, otherwise to the adjoining
    station with the longest shared border, and nowhere if it adjoins nothing.

    Reassigning is address-safe: a piece is a subset of ONE Voronoi cell, so the only matched
    address it can contain is that cell's own — `preferred`'s. Callers therefore only reassign
    pieces that contain no matched address at all, which leaves the invariant untouched."""
    best_sid: int | None = None
    best_len = 0.0
    for k in tree.query(piece, predicate="intersects"):
        try:
            shared = piece.intersection(geoms[k])
        except Exception:  # pragma: no cover - defensive against GEOS edge cases
            continue
        length = shared.length
        if length <= 0:
            continue  # touching at a single point doesn't merge
        if ids[k] == preferred:
            return preferred
        if length > best_len:
            best_sid, best_len = ids[k], length
    return best_sid


def to_wgs84(geom):
    return shp_transform(lambda xs, ys: _TO_WGS84.transform(xs, ys), geom)


def to_utm(geom):
    return shp_transform(lambda xs, ys: _TO_UTM.transform(xs, ys), geom)


def previous_polygons(exclude: set[int]) -> tuple[list[int], list]:
    """(station_ids, UTM geometries) of the already-published polygons of stations NOT in
    ``exclude`` — read back from polygons.parquet and reprojected.

    Only used in incremental (`--municipalities`) mode: the hole-closing pass must see every
    station that could own a hole, and a neighbour outside the recompute scope is only on disk.
    Without it a hole would be filled merely because its owner wasn't recomputed."""
    if not config.POLYGONS_PARQUET.exists():
        return [], []
    prev = pl.read_parquet(config.POLYGONS_PARQUET).filter(
        ~pl.col("station_id").is_in(list(exclude))
    )
    ids: list[int] = []
    geoms: list = []
    for sid, gj in zip(prev["station_id"].to_list(), prev["geojson"].to_list()):
        if not gj:
            continue
        try:
            g = shapely.make_valid(to_utm(shape(json.loads(gj))))
        except Exception:
            continue
        if not g.is_empty:
            ids.append(int(sid))
            geoms.append(g)
    return ids, geoms


def _parts(geom) -> list:
    """The Polygon members of a (Multi)Polygon; [] for empty/other geometries."""
    if geom.is_empty:
        return []
    return list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]


def close_unclaimed_holes(geom, keep_ring=None):
    """Close every interior ring except the ones ``keep_ring(ring_polygon)`` marks as claimed.

    A station's territory is riddled with holes: the tiny gaps the cells leave where they don't
    tile perfectly, and — much larger — the discarded Voronoi cells of UNASSIGNED addresses (a
    street we failed to match punches its whole cell out of the surrounding station). Both are
    clutter for the volunteer editors, and land no other station covers belongs to the station
    that surrounds it, so the default is to fill.

    ``keep_ring`` is the whole safety story (see `hole_is_claimed` for the criteria the caller
    applies): filling a ring another station covers would break the one-address-one-station
    invariant or create polygon overlap. Dropping an unclaimed ring only adds land nobody else
    has, so it is overlap-neutral by construction."""
    def fix(p: Polygon) -> Polygon:
        keep = [r for r in p.interiors if keep_ring is not None and keep_ring(Polygon(r))]
        return Polygon(p.exterior, keep) if len(keep) != len(p.interiors) else p

    parts = _parts(geom)
    if not parts:
        return geom
    fixed = [fix(p) for p in parts]
    return fixed[0] if len(fixed) == 1 and geom.geom_type == "Polygon" else MultiPolygon(fixed)


def hole_is_claimed(ring_area: float, foreign_addresses: int,
                    foreign_polygon_area: float, foreign_settlement_area: float) -> bool:
    """Does another polling station own the land inside this hole? (see `config` for the
    thresholds). Three independent claims, any of which keeps the hole open:

    1. it encloses another station's MATCHED ADDRESS — filling it would cover a foreign address;
    2. another station's POLYGON covers a material share of it — address-less coverage
       (whole-settlement or OSM claims) is real coverage too, and filling would overlap it. The
       absolute floor keeps despike fuzz along a shared border from reading as a claim;
    3. a material share of a big enough ring lies in a SETTLEMENT this station has no coverage
       in — a village nobody was matched to (its addresses are all unassigned, so 1. and 2. see
       nothing) is a matching gap that belongs to whichever station RIK actually listed it for.
       The area floor matters: naselje borders are lined with slivers that lie in the next
       settlement by definition, and without it every one of them survives as a speck."""
    if foreign_addresses > 0:
        return True
    if (foreign_polygon_area > config.POLYGON_HOLE_CLAIM_FRAC * ring_area
            and foreign_polygon_area > config.POLYGON_HOLE_CLAIM_M2):
        return True
    return (ring_area >= config.POLYGON_HOLE_FOREIGN_SETT_M2
            and foreign_settlement_area > config.POLYGON_HOLE_FOREIGN_SETT_FRAC * ring_area)


def _covered_area(ring: Polygon, tree, geoms: list, keys: list, skip) -> float:
    """Area of ``ring`` covered by the indexed geometries whose key ``skip(key)`` rejects.
    Overlapping members are counted once (they are unioned first)."""
    hits = [geoms[k] for k in tree.query(ring, predicate="intersects") if not skip(keys[k])]
    if not hits:
        return 0.0
    try:
        return unary_union(hits).intersection(ring).area
    except Exception:  # pragma: no cover - defensive against GEOS edge cases
        return 0.0


def _osm_foreign_overlap(geom, sid, X, Y, sids_all, buckets, B) -> tuple[int, int]:
    """Count matched addresses inside an OSM claim geometry, split (own, other) by owning
    station. Uses the coarse point grid (`buckets`, cell size `B`) to gather candidates within
    the geom's bbox, then an exact contains test. Unassigned points (no station) are ignored."""
    minx, miny, maxx, maxy = geom.bounds
    i0, i1 = int(np.floor(minx / B)), int(np.floor(maxx / B))
    j0, j1 = int(np.floor(miny / B)), int(np.floor(maxy / B))
    cand: list[int] = []
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            cand.extend(buckets.get(i * 10_000_019 + j, ()))
    if not cand:
        return 0, 0
    shapely.prepare(geom)
    own = other = 0
    for k in cand:
        s = sids_all[k]
        if s == UNASSIGNED:
            continue
        if geom.contains(Point(X[k], Y[k])):
            if s == sid:
                own += 1
            else:
                other += 1
    return own, other


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

    # Settlements claimed by 2+ stations are CONTESTED: rather than drawing the full naselje
    # boundary for each (which overlaps their polygons across the whole village), the boundary
    # is partitioned among them (see partition_settlement). Detected from the FULL claim set so
    # a contested settlement is recognised even when only one claimant is in an incremental
    # scope — and there, ALL its co-claimants are pulled into `affected` so the shared partition
    # is recomputed consistently for every owner.
    claimants_by_sett: dict[str, list[int]] = {}
    if config.STATION_SETT_CLAIMS_PARQUET.exists():
        _sc = pl.read_parquet(config.STATION_SETT_CLAIMS_PARQUET)
        for s, sid in zip(_sc["settlement_id"].to_list(), _sc["station_id"].to_list()):
            claimants_by_sett.setdefault(s, []).append(int(sid))
    contested_setts = {s for s, sts in claimants_by_sett.items() if len(set(sts)) >= 2}
    if affected is not None and contested_setts:
        extra: set[int] = set()
        for s in contested_setts:
            cl = set(claimants_by_sett[s])
            if cl & affected:
                extra |= cl
        if extra - affected:
            affected |= extra
            aff_df = df.filter(pl.col("station_id").is_in(list(affected)))
            affected_sett_ids |= set(aff_df["settlement_id"].unique().to_list())

    boundaries = load_muni_boundaries()
    for g in boundaries.values():
        shapely.prepare(g)
    # Clip geometry is per-station independent, so in scoped mode only build it for the
    # affected stations (skips ~2.5M point-in-polygon checks for everyone else).
    clip_geoms = station_clip_geoms(aff_df if affected is not None else df, boundaries)

    # Global arrays + coarse spatial grid for halo lookup. Bucket size >= the cell cap, so
    # own-bucket + 8 neighbors covers every foreign point a boundary cell could touch — MUST
    # track POLYGON_CELL_CAP_M (a cap larger than the bucket would let cells overshoot the halo
    # constraints and spill across settlement boundaries into neighbours' addresses).
    X = df["x"].to_numpy()
    Y = df["y"].to_numpy()
    sett_codes, SETT = np.unique(df["settlement_id"].to_numpy(), return_inverse=True)
    B = max(256.0, config.POLYGON_CELL_CAP_M)
    bx = np.floor(X / B).astype(np.int64)
    by = np.floor(Y / B).astype(np.int64)
    bucket_of = bx * 10_000_019 + by
    buckets: dict[int, list[int]] = {}
    for i, k in enumerate(bucket_of):
        buckets.setdefault(int(k), []).append(i)

    # Only MATCHED addresses seed the tessellation. An UNMATCHED address (its street didn't
    # resolve) used to seed a Voronoi cell that was then discarded, punching that whole cell out
    # of the station around it — which is exactly where the "unclaimed" land came from: the gaps
    # that split a village station into islands, the bays along its edge, and most of the holes
    # (Subotica: 267 of the municipality's 1 007 km² belonged to nobody, 100% of it inside
    # settlements that DO have matched addresses). Ignoring them lets the matched cells tile the
    # settlement, so each station fills its village out to the boundary and its islands join up.
    # Non-overlap is untouched: this is still a Voronoi over matched points, so a cell can never
    # reach another station's matched address — the same argument as the territory fill itself.
    sids_all = df["station_id"].to_numpy()
    seed_mask = sids_all != UNASSIGNED
    seed_idx = np.flatnonzero(seed_mask)

    # Spatial index over the seed points, used to gather the halo (foreign points within a
    # settlement's boundary bbox) that constrains territory cells so they don't cross into a
    # neighbouring settlement's addresses where naselje boundaries overlap. Indices are mapped
    # back to global positions via `seed_idx`.
    pt_tree = shapely.STRtree(shapely.points(X[seed_idx], Y[seed_idx]))

    idx_by_sett: dict[int, np.ndarray] = {
        int(c): np.flatnonzero((SETT == c) & seed_mask) for c in range(len(sett_codes))
    }

    # Official settlement (naselje) boundaries. Each station's Voronoi cells are clipped to its
    # settlement's boundary so it fills its FULL territory within the village (contiguous, few
    # parts) without overlapping neighbours (Voronoi tiles) or sprawling muni-wide. 100% of
    # address settlements have a boundary; the rare miss falls back to the octagon cap + halo.
    # Loaded once and reused for the whole-settlement claim geometry below.
    sett_boundaries = load_settlement_boundaries()

    # Accumulate cells per station across settlements. `station_setts` records WHICH settlements
    # each station has coverage in (cells here, whole-settlement claims below) — the hole-closing
    # pass needs it to tell "empty land inside my own village" (fill) from "a village I don't
    # cover at all, enclosed by my territory" (keep the hole; it belongs to whoever RIK listed).
    station_cells: dict[int, list] = {}
    station_setts: dict[int, set[str]] = {}
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
        boundary = sett_boundaries.get(str(sett_codes[code]))

        # Halo = foreign (other-settlement) points that constrain this settlement's edge cells so
        # a territory can't cross the Voronoi bisector into a neighbour's addresses. In boundary
        # mode gather every foreign point within the boundary's bbox (naselje boundaries can
        # overlap, so the constraint must cover the whole settlement extent, not just 8 buckets).
        # In the no-boundary fallback use the cheaper own-bucket + 8-neighbour gather.
        halo_xy = None
        if boundary is not None:
            # Query region = the boundary bbox UNIONed with the own-points bbox, expanded by the
            # cap. The own-points span matters because ~0.15% of addresses sit outside the
            # boundary; their outlier caps must also be constrained by nearby foreign points.
            bx0, by0, bx1, by1 = boundary.bounds
            m = config.POLYGON_CELL_CAP_M
            qbox = shapely.box(
                min(bx0, float(xy[:, 0].min())) - m, min(by0, float(xy[:, 1].min())) - m,
                max(bx1, float(xy[:, 0].max())) + m, max(by1, float(xy[:, 1].max())) + m,
            )
            cand_arr = seed_idx[pt_tree.query(qbox)]
            if len(cand_arr):
                foreign = cand_arr[SETT[cand_arr] != code]
                if len(foreign):
                    halo_xy = np.column_stack([X[foreign], Y[foreign]])
        elif len(own_idx) >= 4:
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
            cand_arr = cand_arr[seed_mask[cand_arr]]  # unmatched points don't constrain either
            foreign = cand_arr[SETT[cand_arr] != code]
            if len(foreign):
                halo_xy = np.column_stack([X[foreign], Y[foreign]])

        for sid, cells in cells_for_settlement(xy, sids, halo_xy, boundary).items():
            # Defensive: in scoped mode only ever touch affected stations' polygons, so a
            # stray station in a recomputed settlement can't get a half-recomputed polygon.
            if affected is not None and sid not in affected:
                continue
            station_cells.setdefault(sid, []).extend(cells)
            station_setts.setdefault(sid, set()).add(str(sett_codes[code]))
    settlements = sett_codes  # for the summary line

    # Whole-settlement coverage: a station that claims a whole settlement gets the official
    # naselje boundary as its coverage polygon (unioned with any point-Voronoi cells it also
    # has), instead of the ragged point-cloud shape. Boundaries come from data/naselje.csv
    # via stage01's settlement_geometry.parquet (UTM, same CRS as the cells). For a CONTESTED
    # settlement (claimed by 2+ stations) the boundary is partitioned between the claimants by
    # their own addresses, so together they rebuild the village without overlapping.
    claim_geom: dict[int, object] = {}
    if config.STATION_SETT_CLAIMS_PARQUET.exists():
        sc = pl.read_parquet(config.STATION_SETT_CLAIMS_PARQUET)
        if affected is not None:
            sc = sc.filter(pl.col("station_id").is_in(list(affected)))
        sbound = sett_boundaries  # reuse the boundaries already loaded for cell clipping
        # Per contested settlement, the portion each claimant owns (Voronoi over its won
        # addresses, clipped to the naselje). Built once from the FULL claimant set + full df.
        partitions: dict[str, dict[int, object]] = {}
        n_split = 0
        for s in contested_setts:
            nb = sbound.get(s)
            if nb is None:
                continue
            seeds = df.filter((pl.col("settlement_id") == s)
                              & pl.col("station_id").is_in(claimants_by_sett[s]))
            if seeds.is_empty():
                continue
            partitions[s] = partition_settlement(
                nb, np.column_stack([seeds["x"].to_numpy(), seeds["y"].to_numpy()]),
                seeds["station_id"].to_numpy())
            n_split += 1
        if n_split:
            print(f"  contested settlements split between stations: {n_split:,}")
        for (sid,), grp in sc.group_by("station_id"):
            sid = int(sid)
            geoms = []
            for s in grp["settlement_id"].to_list():
                station_setts.setdefault(sid, set()).add(str(s))
                if s in partitions:
                    # Contested + splittable: only this claimant's share (None if it has no
                    # addresses here, in which case it draws nothing and the co-claimant owns it).
                    part = partitions[s].get(sid)
                    if part is not None and not part.is_empty:
                        geoms.append(part)
                elif s in sbound:
                    # Uncontested, or contested but unsplittable (no addresses to seed the
                    # partition) — fall back to the full naselje boundary.
                    geoms.append(sbound[s])
            if geoms:
                claim_geom[sid] = geoms[0] if len(geoms) == 1 else unary_union(geoms)

    # OSM-fallback coverage: a segment whose street/settlement the register can't place was
    # geocoded against OpenStreetMap in stage04 (data/osm_claims.parquet, UTM WKT, already
    # clipped to the municipality). Union it into the station's claim geometry exactly like a
    # whole-settlement claim, so it merges with any point-Voronoi cells the station also has.
    # REJECTED when the geocoded shape runs over another station's coverage (see
    # `_osm_foreign_overlap` / OSM_FOREIGN_REJECT_MIN) — a whole-street OSM line drawn through a
    # neighbouring registered street would otherwise cover addresses that belong to other stations.
    # Per-segment surviving OSM geometry (WGS84 GeoJSON), kept so the review UI can draw it
    # distinctly and zoom to it — an OSM claim has no addresses, so the segment's polygon is the
    # only thing to focus on. {station_id: [(segment_id, geojson_str), ...]}.
    osm_by_station: dict[int, list[tuple[int, str]]] = {}
    osm_rejected_ids: list[int] = []  # segment ids whose OSM estimate was discarded as overlapping
    n_osm_rejected = 0
    if config.OSM_CLAIMS_PARQUET.exists():
        oc = pl.read_parquet(config.OSM_CLAIMS_PARQUET)
        if affected is not None:
            oc = oc.filter(pl.col("station_id").is_in(list(affected)))
        for (sid,), grp in oc.group_by("station_id"):
            sid = int(sid)
            geoms = []
            for seg_id, w in zip(grp["segment_id"].to_list(), grp["wkt"].to_list()):
                try:
                    g = shapely.make_valid(shapely.from_wkt(w))
                except Exception:
                    continue
                if g.is_empty:
                    continue
                own, other = _osm_foreign_overlap(g, sid, X, Y, sids_all, buckets, B)
                if other >= config.OSM_FOREIGN_REJECT_MIN and other > own:
                    n_osm_rejected += 1
                    osm_rejected_ids.append(int(seg_id))
                    continue
                geoms.append(g)
                osm_by_station.setdefault(sid, []).append((int(seg_id), json.dumps(
                    mapping(to_wgs84(g.simplify(config.SIMPLIFY_TOL_M, preserve_topology=True))),
                    ensure_ascii=False)))
            if geoms:
                geoms += [claim_geom[sid]] if sid in claim_geom else []
                claim_geom[sid] = geoms[0] if len(geoms) == 1 else unary_union(geoms)
    if n_osm_rejected:
        print(f"  OSM claims rejected (overlap other stations' coverage): {n_osm_rejected:,}")

    # ── Pass 1: merge each station's cells + claims, clip to the municipality, despike ──────
    merged_by_sid: dict[int, object] = {}
    n_clipped = 0
    n_sett = 0
    for sid in set(station_cells) | set(claim_geom):
        parts = list(station_cells.get(sid, []))
        cg = claim_geom.get(sid)
        if cg is not None:
            parts.append(cg)
            n_sett += 1
        merged = unary_union(parts).buffer(0)
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
        # Despike: the per-cell octagons touch through pinch points, so unary_union (and the
        # clip intersection) thread needle-thin spikes into the boundary that shoot toward the
        # centre — inward zero-width slits AND outward needles, and they make the polygon
        # invalid. A morphological close-then-open (dilate, erode past, dilate back) heals both
        # kinds and repairs validity; the simplify pass below removes the rounding so the result
        # has fewer vertices, not more. Radius stays well under the smallest real coverage
        # feature, so it never bridges distinct parts or fills real concavities.
        r = config.POLYGON_DESPIKE_M
        smoothed = (
            merged.buffer(r, quad_segs=2)
            .buffer(-2 * r, quad_segs=2)
            .buffer(r, quad_segs=2)
        )
        if not smoothed.is_empty:
            merged = smoothed
        merged_by_sid[sid] = merged.buffer(0)

    # Stations left untouched by an incremental scope still own land and still claim holes, but
    # their shapes only exist on disk — load them so both passes below see the full picture.
    prev_ids, prev_geoms = previous_polygons(exclude=affected) if affected is not None else ([], [])

    # ── Pass 1.5: hand each station the unclaimed land nearest to its addresses ─────────────
    # Settlements are tessellated independently and clipped to their own naselje boundary, so
    # land inside one naselje that is nearer an address in the next one is dropped by both — the
    # corridors that cut a village station into islands and the bays along the municipality
    # edge. Split that leftover by nearest matched address (see `residual_shares`): the polygons
    # then tile the municipality, islands join their main body, and each grows out to the
    # municipality boundary. EXCLUDED: a settlement with no matched address at all (its village
    # is somebody else's coverage gap, not the neighbours' land — the same rule hole-closing
    # applies to Боранци), so those stay visibly uncovered.
    matched_df = df.filter(pl.col("station_id") != UNASSIGNED)
    sett_with_matches = set(matched_df["settlement_id"].unique().to_list())
    setts_by_muni = {
        str(m): [str(s) for s in ss]
        for m, ss in df.group_by("municipality_id").agg(pl.col("settlement_id").unique()).rows()
    }
    fill_munis = matched_df if affected is None else matched_df.filter(
        pl.col("municipality_id").is_in(
            matched_df.filter(pl.col("station_id").is_in(list(affected)))["municipality_id"]
            .unique().to_list()
        )
    )
    n_fill_stations = 0
    fill_area = 0.0
    fill_ids = list(merged_by_sid) + prev_ids
    fill_geoms = [merged_by_sid[s] for s in merged_by_sid] + prev_geoms
    fill_tree = shapely.STRtree(fill_geoms)
    n_fill_munis = len(fill_munis["municipality_id"].unique())
    t_fill = time.monotonic()
    for i_muni, ((mid,), grp) in enumerate(
            fill_munis.group_by("municipality_id", maintain_order=True), start=1):
        mb = boundaries.get(str(mid))
        if mb is None:
            continue  # no official boundary -> nothing to grow into
        t_muni = time.monotonic()
        covered = [fill_geoms[k] for k in fill_tree.query(mb, predicate="intersects")]
        residual = mb.difference(unary_union(covered)) if covered else mb
        empty = [sett_boundaries[s] for s in setts_by_muni.get(str(mid), ())
                 if s not in sett_with_matches and s in sett_boundaries]
        if empty:
            residual = residual.difference(unary_union(empty))
        if residual.is_empty:
            continue
        shares = residual_shares(
            residual,
            np.column_stack([grp["x"].to_numpy(), grp["y"].to_numpy()]),
            grp["station_id"].to_numpy(),
        )
        # Route each piece to a polygon it actually MERGES with (see `attach_owner`), so filling
        # never leaves a station a new island. A piece holding a matched address always stays
        # with its Voronoi owner — that address is by construction the owner's (one cell, one
        # address) — even when that means keeping it detached. The address lookup runs only for
        # the few pieces that would otherwise move or be dropped.
        pieces: dict[int, list] = {}
        for sid, share in shares.items():
            for piece in _parts(share):
                if piece.area < config.POLYGON_MIN_FILL_PIECE_M2:
                    continue
                owner = attach_owner(piece, sid, fill_tree, fill_geoms, fill_ids)
                if owner != sid and len(pt_tree.query(piece, predicate="contains")) > 0:
                    owner = sid  # holds its own address: never hand it to someone else
                if owner is None:
                    continue  # adjoins nothing it could merge into — leave it unclaimed
                pieces.setdefault(owner, []).append(piece)
        for sid, geoms in pieces.items():
            if affected is not None and sid not in affected:
                continue  # out of scope: its share waits for that municipality's own recompute
            share = geoms[0] if len(geoms) == 1 else unary_union(geoms)
            base = merged_by_sid.get(sid)
            merged_by_sid[sid] = (unary_union([base, share]) if base is not None else share).buffer(0)
            n_fill_stations += 1
            fill_area += share.area
        print(f"    [{i_muni}/{n_fill_munis}] muni {mid}: filled in {time.monotonic() - t_muni:,.1f}s"
              f" (total {time.monotonic() - t_fill:,.0f}s)", flush=True)
    if n_fill_stations:
        print(f"  residual fill: {fill_area / 1e6:,.0f} km² of unclaimed land -> "
              f"{n_fill_stations:,} station polygons")

    # ── Hole closing: indexes over everything that can CLAIM a hole ────────────────────────
    # Other stations' polygons (this run's, plus the out-of-scope ones loaded above) and the
    # official naselje boundaries, so a hole lying in a settlement this station doesn't cover
    # stays open.
    poly_ids = list(merged_by_sid) + prev_ids
    poly_geoms = [merged_by_sid[s] for s in merged_by_sid] + prev_geoms
    poly_tree = shapely.STRtree(poly_geoms)
    sett_ids = list(sett_boundaries)
    sett_geoms = [sett_boundaries[s] for s in sett_ids]
    sett_tree = shapely.STRtree(sett_geoms)

    def drifted_over_foreign(simplified, merged, sid: int) -> bool:
        """Did simplifying push this polygon's boundary over another station's matched address?

        Only the land the simplified shape GAINED can do that, and that is a thin band along the
        boundary — so this checks the difference, not the whole polygon, which keeps it cheap
        enough to run for every station."""
        try:
            gained = simplified.difference(merged)
        except Exception:  # pragma: no cover - defensive against GEOS edge cases
            return False
        if gained.is_empty:
            return False
        return any(sids_all[seed_idx[i]] != sid
                   for i in pt_tree.query(gained, predicate="contains"))

    def keep_hole(ring: Polygon, sid: int, own_setts: frozenset) -> bool:
        """Is this hole claimed by another station (see `hole_is_claimed`)? Foreign addresses
        come from the coarse point grid (`_osm_foreign_overlap` returns (own, other))."""
        return hole_is_claimed(
            ring.area,
            _osm_foreign_overlap(ring, sid, X, Y, sids_all, buckets, B)[1],
            _covered_area(ring, poly_tree, poly_geoms, poly_ids, skip=lambda s: s == sid),
            _covered_area(ring, sett_tree, sett_geoms, sett_ids, skip=own_setts.__contains__),
        )

    # ── Pass 2: close unclaimed holes, simplify, emit ──────────────────────────────────────
    rows: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()
    n_holes_closed = 0
    n_holes_kept = 0
    n_drift = 0        # stations where the simplify tolerance drifted over a foreign address
    n_drift_fixed = 0  # ...of which a finer tolerance fixed within the size budget
    for sid, merged in merged_by_sid.items():
        # Close the holes no other station covers — the tiny gaps the cells leave plus the much
        # larger discarded cells of UNASSIGNED addresses (an unmatched street punches its whole
        # Voronoi cell out of the station that surrounds it). Land nobody else has belongs to
        # the surrounding station, and filling it can't create overlap.
        own_setts = frozenset(station_setts.get(sid, ()))
        before = sum(len(p.interiors) for p in _parts(merged))
        merged = close_unclaimed_holes(
            merged, keep_ring=lambda ring, _s=sid, _o=own_setts: keep_hole(ring, _s, _o)
        )
        kept = sum(len(p.interiors) for p in _parts(merged))
        n_holes_closed += before - kept
        n_holes_kept += kept
        area = merged.area  # m^2 (UTM)

        def as_geojson(geom):
            """WGS84 GeoJSON of a UTM geometry. Simplify + the nonlinear reprojection can leave
            a self-intersection near the many-holed boundaries the generalize step produces, so
            the result is validity-repaired (buffer(0) is topology-only, CRS-agnostic) — keeping
            the output 100% OGC-valid."""
            wgs = to_wgs84(geom)
            if not wgs.is_valid:
                repaired = wgs.buffer(0)
                if not repaired.is_empty:
                    wgs = repaired
            return json.dumps(mapping(wgs), ensure_ascii=False)

        # Adaptive simplification: whole-village polygons can exceed the D1 statement budget
        # (~50KB); escalate the tolerance until the GeoJSON fits.
        #
        # Simplification moves the boundary by up to `tol`, and since the territory fill makes
        # the polygons TILE their municipality, they now share long borders with neighbours —
        # so a neighbour's address sitting within `tol` of a shared border can drift INSIDE.
        # Measured: 5 m tolerance swallowed 37 such addresses nationwide, all within 3.4 m of the
        # border. When that happens, back off to a finer tolerance; the size budget is the hard
        # constraint, so a station that cannot be both clean and small keeps the small version.
        gj = None
        for tol in (config.SIMPLIFY_TOL_M, 15, 40, 100, 250):
            simplified = merged.simplify(tol, preserve_topology=True)
            gj = as_geojson(simplified)
            if len(gj.encode()) > 45_000:
                continue
            if not drifted_over_foreign(simplified, merged, sid):
                break
            n_drift += 1
            for finer in (2.0, 0.5, 0.0):
                alt = merged if finer == 0.0 else merged.simplify(finer, preserve_topology=True)
                gj_alt = as_geojson(alt)
                if len(gj_alt.encode()) <= 45_000 and not drifted_over_foreign(alt, merged, sid):
                    gj = gj_alt
                    n_drift_fixed += 1
                    break
            break
        osm_segs = osm_by_station.get(sid)
        rows.append({
            "station_id": sid,
            "geojson": gj,
            "area_m2": round(area, 1),
            "point_count": None,
            "computed_at": now,
            # Per-segment OSM claim geometry for the UI ({segment_id, geojson}); null when none.
            "osm": json.dumps([{"segment_id": s, "geojson": json.loads(g)} for s, g in osm_segs],
                              ensure_ascii=False) if osm_segs else None,
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

    # Segment ids whose OSM estimate was discarded (overlapped other stations) — stage06 strips
    # the 'osm_fallback' review reason from these so they don't masquerade as drawn coverage.
    # Merged like polygons: in scoped mode, replace only the affected stations' rows.
    rej_df = pl.DataFrame({"segment_id": osm_rejected_ids}, schema={"segment_id": pl.Int64})
    if affected is None:
        rej_df.write_parquet(config.OSM_REJECTED_PARQUET)
    elif config.OSM_REJECTED_PARQUET.exists():
        prev = pl.read_parquet(config.OSM_REJECTED_PARQUET)
        kept = prev.filter(~(pl.col("segment_id") // 1000).is_in(list(affected)))
        pl.concat([kept, rej_df], how="vertical_relaxed").write_parquet(config.OSM_REJECTED_PARQUET)
    else:
        rej_df.write_parquet(config.OSM_REJECTED_PARQUET)

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
            f"  settlement-claim polygons: {n_sett:,}"
        )
    else:
        print(
            f"  [incremental: {len(municipalities)} muni] recomputed {n_compute:,} "
            f"settlements -> {len(rows):,} station polygons; merged into {n_total:,} total"
        )
    print(f"  holes closed (unclaimed land): {n_holes_closed:,}  kept (another station's): "
          f"{n_holes_kept:,}")
    if n_drift:
        print(f"  simplify drifted over a neighbour's address: {n_drift:,} station(s), "
              f"{n_drift_fixed:,} fixed by a finer tolerance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
