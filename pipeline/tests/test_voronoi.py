"""Tests for stage05 geometry helpers — see stage05_voronoi.py.

`partition_settlement(naselje, xy, sids)` splits a whole-settlement boundary among the
stations that each claim it, by a Voronoi over their own addresses, so the parts tile the
settlement without overlapping (the fix for two stations both drawing the whole village).
"""

import numpy as np
import shapely
from shapely.geometry import MultiPolygon, Point, Polygon

import config
import stage05_voronoi as s5


def _square(size=1000.0):
    return Polygon([(0, 0), (size, 0), (size, size), (0, size)])


def _ring(cx, cy, r):
    """A square ring (hole) of half-width r centred at (cx, cy)."""
    return [(cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r), (cx - r, cy + r)]


class TestPartitionSettlement:
    def test_two_stations_tile_without_overlap(self):
        naselje = _square(1000.0)
        # Station 10 owns the left half (x<500), station 20 the right half.
        left = [(100, y) for y in (200, 400, 600, 800)]
        right = [(900, y) for y in (200, 400, 600, 800)]
        xy = np.array(left + right, dtype=float)
        sids = np.array([10] * 4 + [20] * 4)
        parts = s5.partition_settlement(naselje, xy, sids)
        assert set(parts) == {10, 20}
        a, b = parts[10], parts[20]
        # Disjoint and together they cover the whole settlement.
        assert a.intersection(b).area < 1.0                       # ~0 overlap
        assert abs(a.union(b).area - naselje.area) < 1.0          # tiles naselje
        # Each station's region sits on its own side (centroid x below/above the midline).
        assert a.centroid.x < 500 < b.centroid.x

    def test_single_claimant_gets_whole_settlement(self):
        naselje = _square()
        xy = np.array([(100, 100), (200, 200), (300, 100), (200, 300)], dtype=float)
        parts = s5.partition_settlement(naselje, xy, np.array([7, 7, 7, 7]))
        assert set(parts) == {7}
        assert abs(parts[7].area - naselje.area) < 1.0

    def test_few_seeds_fallback_still_tiles(self):
        # < 4 seeds exercises the shapely-voronoi fallback path.
        naselje = _square()
        xy = np.array([(250, 500), (750, 500)], dtype=float)
        parts = s5.partition_settlement(naselje, xy, np.array([1, 2]))
        assert set(parts) == {1, 2}
        assert parts[1].intersection(parts[2]).area < 1.0
        assert abs(parts[1].union(parts[2]).area - naselje.area) < 1.0


class TestCloseUnclaimedHoles:
    def _with_hole(self, hole_half):
        # A 1000x1000 square with one centred square hole of half-width `hole_half`.
        return Polygon(_square(1000).exterior.coords, [_ring(500, 500, hole_half)])

    def test_unclaimed_hole_closed_whatever_its_size(self):
        # Size is no longer a reason to keep a hole: both a tiling gap (1600 m^2) and a big
        # unmatched-street cell (160 000 m^2) are closed when nobody else claims them.
        for half, area in ((20, 1_600), (200, 160_000)):
            poly = self._with_hole(half)
            assert Polygon(_ring(500, 500, half)).area == area
            out = s5.close_unclaimed_holes(poly, keep_ring=lambda r: False)
            assert len(out.interiors) == 0
            assert out.area == 1_000_000      # filled hole -> full square

    def test_claimed_hole_kept(self):
        # A hole another station covers stays open — filling it would swallow foreign land.
        poly = self._with_hole(200)
        out = s5.close_unclaimed_holes(poly, keep_ring=lambda r: r.contains(Point(500, 500)))
        assert len(out.interiors) == 1
        assert out.area == poly.area

    def test_multipolygon_each_part_cleaned(self):
        a = Polygon(_square(1000).exterior.coords, [_ring(500, 500, 20)])
        b = Polygon([(2000, 0), (3000, 0), (3000, 1000), (2000, 1000)],
                    [_ring(2500, 500, 60)])
        # Only part b's hole is claimed (it contains the foreign point).
        out = s5.close_unclaimed_holes(MultiPolygon([a, b]),
                                       keep_ring=lambda r: r.contains(Point(2500, 500)))
        assert out.geom_type == "MultiPolygon"
        assert sorted(len(p.interiors) for p in out.geoms) == [0, 1]

    def test_no_holes_unchanged(self):
        poly = _square(1000)
        out = s5.close_unclaimed_holes(poly, keep_ring=lambda r: False)
        assert out.equals(poly)

    def test_no_predicate_closes_everything(self):
        out = s5.close_unclaimed_holes(self._with_hole(200))
        assert len(out.interiors) == 0


class TestHoleIsClaimed:
    AREA = 100_000.0

    def test_foreign_address_claims(self):
        assert s5.hole_is_claimed(self.AREA, 1, 0.0, 0.0)

    def test_empty_hole_is_free(self):
        assert not s5.hole_is_claimed(self.AREA, 0, 0.0, 0.0)

    def test_foreign_polygon_claims_material_share(self):
        frac = config.POLYGON_HOLE_CLAIM_FRAC
        assert s5.hole_is_claimed(self.AREA, 0, self.AREA * frac * 2, 0.0)
        # A sliver below the share is despike fuzz along a shared border, not a claim.
        assert not s5.hole_is_claimed(self.AREA, 0, self.AREA * frac / 2, 0.0)

    def test_foreign_polygon_needs_absolute_floor_too(self):
        # On a small hole the relative share alone is met by a few m^2 of border fuzz; the
        # absolute floor is what rejects it.
        small = 1_000.0
        fuzz = config.POLYGON_HOLE_CLAIM_M2 * 0.8   # > 2% of 1000 m^2, but under the floor
        assert fuzz > config.POLYGON_HOLE_CLAIM_FRAC * small
        assert not s5.hole_is_claimed(small, 0, fuzz, 0.0)
        # Genuinely covered by a neighbour (the whole small hole) -> claimed.
        assert s5.hole_is_claimed(small, 0, small, 0.0)

    def test_foreign_settlement_claims(self):
        frac = config.POLYGON_HOLE_FOREIGN_SETT_FRAC
        # A village this station has no coverage in (all its addresses unmatched, so neither
        # the address nor the polygon check sees anything) must stay open.
        assert s5.hole_is_claimed(self.AREA, 0, 0.0, self.AREA * frac * 2)
        # Clipping the edge of a neighbouring naselje is not a claim.
        assert not s5.hole_is_claimed(self.AREA, 0, 0.0, self.AREA * frac / 2)

    def test_foreign_settlement_needs_a_village_sized_ring(self):
        # A sliver on a naselje border lies ENTIRELY in the next settlement, but it is far too
        # small to be anyone's village — it gets closed like any other speck.
        sliver = config.POLYGON_HOLE_FOREIGN_SETT_M2 / 10
        assert not s5.hole_is_claimed(sliver, 0, 0.0, sliver)
        big = config.POLYGON_HOLE_FOREIGN_SETT_M2 * 10
        assert s5.hole_is_claimed(big, 0, 0.0, big)


class TestResidualShares:
    """The unclaimed land left between independently-tessellated settlements is split by
    nearest matched address, so the shares tile it without ever reaching a foreign address."""

    def test_splits_residual_by_nearest_address(self):
        # Residual strip spanning x in [0,1000]; station 10's address sits left, 20's right.
        residual = Polygon([(0, 0), (1000, 0), (1000, 100), (0, 100)])
        xy = np.array([(100, 50), (100, 60), (900, 50), (900, 60)], dtype=float)
        sids = np.array([10, 10, 20, 20])
        shares = s5.residual_shares(residual, xy, sids)
        assert set(shares) == {10, 20}
        a, b = shares[10], shares[20]
        assert a.intersection(b).area < 1.0                      # disjoint
        assert abs(a.union(b).area - residual.area) < 1.0        # tiles the residual
        assert a.centroid.x < 500 < b.centroid.x                 # each on its own side

    def test_empty_residual_yields_nothing(self):
        xy = np.array([(0, 0), (0, 10), (10, 0), (10, 10)], dtype=float)
        assert s5.residual_shares(Polygon(), xy, np.array([1, 1, 1, 1])) == {}

    def test_too_few_seeds_yields_nothing(self):
        # Under 4 points Voronoi is degenerate; the caller keeps the land unclaimed.
        assert s5.residual_shares(_square(100), np.array([(1.0, 1.0)]), np.array([7])) == {}


class TestAttachOwner:
    """A piece of unclaimed land is only useful to a polygon it MERGES with — otherwise it
    would just become another island, which is what the fill exists to avoid."""

    def _setup(self):
        # Station 1's polygon on the left, station 2's on the right, a gap in between.
        left = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
        right = Polygon([(200, 0), (300, 0), (300, 100), (200, 100)])
        return shapely.STRtree([left, right]), [left, right], [1, 2]

    def test_prefers_the_voronoi_owner_when_it_adjoins(self):
        tree, geoms, ids = self._setup()
        piece = Polygon([(100, 0), (150, 0), (150, 100), (100, 100)])  # touches station 1
        assert s5.attach_owner(piece, 1, tree, geoms, ids) == 1

    def test_reassigns_to_the_adjoining_neighbour(self):
        tree, geoms, ids = self._setup()
        # Nearest address says station 1, but the piece only borders station 2's polygon.
        piece = Polygon([(150, 0), (200, 0), (200, 100), (150, 100)])
        assert s5.attach_owner(piece, 1, tree, geoms, ids) == 2

    def test_longest_shared_border_wins(self):
        tree, geoms, ids = self._setup()
        # Spans the gap: 100 m of border with station 1, 20 m with station 2.
        piece = Polygon([(100, 0), (200, 0), (200, 20), (100, 100)])
        assert s5.attach_owner(piece, 99, tree, geoms, ids) == 1

    def test_orphan_piece_is_left_unclaimed(self):
        tree, geoms, ids = self._setup()
        piece = Polygon([(120, 200), (180, 200), (180, 260), (120, 260)])  # touches nothing
        assert s5.attach_owner(piece, 1, tree, geoms, ids) is None

    def test_point_contact_does_not_merge(self):
        tree, geoms, ids = self._setup()
        # Meets station 1's polygon at the single corner (100,100) — unioning would still
        # leave two islands, so this is not an attachment.
        piece = Polygon([(100, 100), (150, 100), (150, 150)])
        assert s5.attach_owner(piece, 1, tree, geoms, ids) is None


class TestCoveredArea:
    def test_counts_overlapping_geometries_once(self):
        ring = _square(100)                                  # 10 000 m^2
        a = Polygon([(0, 0), (60, 0), (60, 100), (0, 100)])  # left 60%
        b = Polygon([(40, 0), (100, 0), (100, 100), (40, 100)])  # right 60%, overlaps a
        tree = shapely.STRtree([a, b])
        got = s5._covered_area(ring, tree, [a, b], ["x", "y"], skip=lambda k: False)
        assert got == 10_000                                 # union covers the ring exactly once

    def test_skipped_keys_ignored(self):
        ring = _square(100)
        a = _square(100)
        tree = shapely.STRtree([a])
        assert s5._covered_area(ring, tree, [a], ["mine"], skip=lambda k: k == "mine") == 0.0


class TestCellCapNonOverlap:
    """The per-cell cap is intersected with the address's Voronoi cell, so enlarging it can
    never make a cell reach another station's address — cells stay a non-overlapping partition
    regardless of the cap radius."""

    def test_capped_cells_do_not_overlap(self):
        # Two stations, four points each, interleaved so their Voronoi cells share bisectors.
        xy = np.array([(0, 0), (0, 300), (300, 0), (300, 300),        # station 1
                       (600, 0), (600, 300), (900, 0), (900, 300)], dtype=float)  # station 2
        sids = np.array([1, 1, 1, 1, 2, 2, 2, 2])
        cells = s5.cells_for_settlement(xy, sids)
        from shapely.ops import unary_union
        a = unary_union(cells[1]); b = unary_union(cells[2])
        assert a.intersection(b).area < 1.0   # capped cells tile without overlap

    def test_no_capped_cell_contains_foreign_point(self):
        xy = np.array([(0, 0), (0, 300), (300, 0), (300, 300),
                       (600, 0), (600, 300), (900, 0), (900, 300)], dtype=float)
        sids = np.array([1, 1, 1, 1, 2, 2, 2, 2])
        cells = s5.cells_for_settlement(xy, sids)
        from shapely.ops import unary_union
        region = {s: unary_union(c) for s, c in cells.items()}
        for i, (x, y) in enumerate(xy):
            for s, reg in region.items():
                if s != sids[i]:
                    assert not reg.contains(Point(x, y))  # never covers another station's address


class TestTerritoryFill:
    """With a settlement boundary each station fills its full Voronoi territory (clipped to the
    boundary); the regions tile the settlement without overlap and stay inside it."""

    def test_regions_tile_settlement_without_overlap(self):
        boundary = _square(1000.0)
        # station 1 on the left (x<500), station 2 on the right.
        xy = np.array([(100, 200), (100, 800), (300, 500),
                       (700, 500), (900, 200), (900, 800)], dtype=float)
        sids = np.array([1, 1, 1, 2, 2, 2])
        cells = s5.cells_for_settlement(xy, sids, boundary=boundary)
        from shapely.ops import unary_union
        a = unary_union(cells[1]); b = unary_union(cells[2])
        assert a.intersection(b).area < 1.0                 # disjoint
        assert abs(a.union(b).area - boundary.area) < 1.0   # together fill the settlement
        assert boundary.buffer(1e-6).contains(a) and boundary.buffer(1e-6).contains(b)  # inside
        for i, (x, y) in enumerate(xy):                     # no foreign address covered
            other = b if sids[i] == 1 else a
            assert not other.contains(Point(x, y))

    def test_single_station_gets_whole_settlement(self):
        boundary = _square(800.0)
        xy = np.array([(100, 100), (200, 200), (300, 100), (200, 400)], dtype=float)
        cells = s5.cells_for_settlement(xy, np.array([5, 5, 5, 5]), boundary=boundary)
        from shapely.ops import unary_union
        assert abs(unary_union(cells[5]).area - boundary.area) < 1.0

    def test_own_address_outside_boundary_kept(self):
        # An own address outside the naselje boundary (register vs boundary-source disagreement)
        # must still be covered — its tight Voronoi cell is kept so no listed address is dropped.
        boundary = _square(1000.0)
        outside_pt = (1200.0, 500.0)  # 200 m east of the boundary
        # A well-populated interior grid + one outlier, so the outlier's cell is well-formed.
        interior = [(x, y) for x in (200, 500, 800) for y in (200, 500, 800)]
        xy = np.array(interior + [outside_pt], dtype=float)
        sids = np.array([9] * len(xy))
        cells = s5.cells_for_settlement(xy, sids, boundary=boundary)
        from shapely.ops import unary_union
        terr = unary_union(cells[9])
        assert terr.covers(Point(*outside_pt))   # outlier address still covered
        assert terr.covers(Point(500, 500))      # interior address still covered

    def test_few_points_fallback_splits_boundary(self):
        # <4 points exercises the _buffered_by_station boundary-split path.
        boundary = _square(1000.0)
        cells = s5.cells_for_settlement(
            np.array([(250, 500), (750, 500)], dtype=float), np.array([1, 2]), boundary=boundary)
        from shapely.ops import unary_union
        a = unary_union(cells[1]); b = unary_union(cells[2])
        assert a.intersection(b).area < 1.0
        assert abs(a.union(b).area - boundary.area) < 1.0
