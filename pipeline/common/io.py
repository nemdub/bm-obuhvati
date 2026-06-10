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
