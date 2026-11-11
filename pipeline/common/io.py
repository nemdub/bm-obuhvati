"""Small IO/geo helpers shared across stages."""

from __future__ import annotations

import numpy as np
from pyproj import Transformer

from config import UTM_34N, WGS84

# always_xy=True -> transform takes (x=easting, y=northing) and returns (lon, lat).
_TO_WGS84 = Transformer.from_crs(UTM_34N, WGS84, always_xy=True)


def utm_to_wgs84(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Vectorized UTM 34N -> WGS84. Returns (lon, lat) arrays."""
    lon, lat = _TO_WGS84.transform(x, y)
    return np.asarray(lon), np.asarray(lat)


_TO_UTM = Transformer.from_crs(WGS84, UTM_34N, always_xy=True)


def wgs84_to_utm(lon: np.ndarray, lat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Vectorized WGS84 -> UTM 34N. Returns (x, y) arrays in meters."""
    x, y = _TO_UTM.transform(lon, lat)
    return np.asarray(x), np.asarray(y)
