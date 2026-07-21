"""Tests for stage05 geometry helpers — see stage05_voronoi.py.

`partition_settlement(naselje, xy, sids)` splits a whole-settlement boundary among the
stations that each claim it, by a Voronoi over their own addresses, so the parts tile the
settlement without overlapping (the fix for two stations both drawing the whole village).
"""

import numpy as np
from shapely.geometry import Polygon

import stage05_voronoi as s5


def _square(size=1000.0):
    return Polygon([(0, 0), (size, 0), (size, size), (0, size)])


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
