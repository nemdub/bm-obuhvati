"""Tests for stage05 geometry helpers — see stage05_voronoi.py.

`partition_settlement(naselje, xy, sids)` splits a whole-settlement boundary among the
stations that each claim it, by a Voronoi over their own addresses, so the parts tile the
settlement without overlapping (the fix for two stations both drawing the whole village).
"""

import numpy as np
from shapely.geometry import MultiPolygon, Point, Polygon

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


class TestDropSmallHoles:
    def _with_hole(self, hole_half):
        # A 1000x1000 square with one centred square hole of half-width `hole_half`.
        return Polygon(_square(1000).exterior.coords, [_ring(500, 500, hole_half)])

    def test_small_hole_removed(self):
        poly = self._with_hole(20)            # hole area 40*40 = 1600 m^2
        out = s5.drop_small_holes(poly, 5000.0)
        assert len(out.interiors) == 0
        assert out.area == 1_000_000          # filled hole -> full square

    def test_large_hole_preserved(self):
        poly = self._with_hole(60)            # hole area 120*120 = 14400 m^2 >= 5000
        out = s5.drop_small_holes(poly, 5000.0)
        assert len(out.interiors) == 1
        assert out.area == poly.area

    def test_multipolygon_each_part_cleaned(self):
        a = Polygon(_square(1000).exterior.coords, [_ring(500, 500, 20)])            # small hole
        b = Polygon([(2000, 0), (3000, 0), (3000, 1000), (2000, 1000)],
                    [_ring(2500, 500, 60)])                                          # large hole
        out = s5.drop_small_holes(MultiPolygon([a, b]), 5000.0)
        assert out.geom_type == "MultiPolygon"
        holes = [len(p.interiors) for p in out.geoms]
        assert sorted(holes) == [0, 1]        # small dropped, large kept

    def test_no_holes_unchanged(self):
        poly = _square(1000)
        out = s5.drop_small_holes(poly, 5000.0)
        assert out.equals(poly)

    def test_small_hole_with_foreign_address_kept(self):
        # A small hole (1600 m^2) that encloses another station's address must be KEPT even
        # though it's below the area threshold — filling it would cover a foreign address.
        poly = self._with_hole(20)
        # keep_ring true for a ring that contains the foreign point (500, 500).
        out = s5.drop_small_holes(poly, 5000.0, keep_ring=lambda r: r.contains(Point(500, 500)))
        assert len(out.interiors) == 1
        # A tiling gap elsewhere (no foreign point) is still dropped.
        out2 = s5.drop_small_holes(poly, 5000.0, keep_ring=lambda r: r.contains(Point(9_999, 9_999)))
        assert len(out2.interiors) == 0


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
