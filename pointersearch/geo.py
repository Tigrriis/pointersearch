"""Small coordinate helpers with no heavy dependencies (used by the hosted planner)."""

from __future__ import annotations

import math
from dataclasses import dataclass

M_PER_DEG_LAT = 111_320.0


@dataclass
class LocalFrame:
    """Equirectangular local tangent plane centred on (lat0, lon0)."""

    lat0: float
    lon0: float

    def to_ne(self, lat: float, lon: float) -> tuple[float, float]:
        north = (lat - self.lat0) * M_PER_DEG_LAT
        east = (lon - self.lon0) * M_PER_DEG_LAT * math.cos(math.radians(self.lat0))
        return north, east

    def to_latlon(self, north: float, east: float) -> tuple[float, float]:
        lat = self.lat0 + north / M_PER_DEG_LAT
        lon = self.lon0 + east / (M_PER_DEG_LAT * math.cos(math.radians(self.lat0)))
        return lat, lon
