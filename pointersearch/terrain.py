"""Ground surface models used by direct georeferencing.

``FlatGround`` assumes the terrain under the image is at the take-off elevation,
which is what the aircraft's RelativeAltitude already measures. ``GeoTiffGround``
samples a DEM (for example Geoscience Australia 5 m / 1 s products) and needs the
optional ``rasterio`` dependency.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class Ground(Protocol):
    def elevation(self, lat: float, lon: float) -> float | None:
        """Terrain elevation in metres above sea level, or None if unknown."""


class FlatGround:
    def elevation(self, lat: float, lon: float) -> float | None:  # noqa: D401
        return None


class GeoTiffGround:
    def __init__(self, path: str | Path):
        try:
            import rasterio  # type: ignore
            from rasterio.warp import transform as rio_transform  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("DEM support needs `pip install rasterio`") from exc
        self._rio_transform = rio_transform
        self._ds = rasterio.open(str(path))
        self._band = self._ds.read(1)
        self._nodata = self._ds.nodata

    def elevation(self, lat: float, lon: float) -> float | None:
        xs, ys = self._rio_transform("EPSG:4326", self._ds.crs, [lon], [lat])
        row, col = self._ds.index(xs[0], ys[0])
        if not (0 <= row < self._band.shape[0] and 0 <= col < self._band.shape[1]):
            return None
        value = float(self._band[row, col])
        if self._nodata is not None and value == self._nodata:
            return None
        return value


def load_ground(dem_path: str | None) -> Ground:
    return GeoTiffGround(dem_path) if dem_path else FlatGround()
