"""Configuration for a mission working directory.

All tunables live here so a search coordinator can adjust them in one JSON file
(``pointersearch.json`` inside the working directory) without touching code.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class CameraDefaults:
    """Fallback 35 mm-equivalent focal lengths per lens when EXIF lacks them.

    Values are the published equivalents for the Matrice 4T. They are only used
    when ``FocalLengthIn35mmFilm`` is missing from the image EXIF.
    """

    wide: float = 24.0
    zoom: float = 70.0
    tele: float = 168.0
    thermal: float = 53.0
    unknown: float = 24.0


@dataclass
class RXConfig:
    """Reed-Xiaoli colour anomaly detector."""

    enabled: bool = True
    work_width: int = 1600          # downscale width for scoring
    lum_weight: float = 0.3         # weight of L* channel (0 = colour only)
    k_mad: float = 15.0             # threshold = median + k_mad * MAD of the score map
    min_score: float = 25.0         # absolute floor on Mahalanobis^2
    min_area_m2: float = 0.04       # discard blobs smaller than this
    max_area_m2: float = 12.0       # discard blobs larger than this
    max_candidates: int = 25        # per image, by score


@dataclass
class BrightColourConfig:
    """Saturated / unnatural colour detector (HSV threshold)."""

    enabled: bool = True
    work_width: int = 1600
    min_saturation: float = 0.60    # 0..1 (dry soil and scrub sit around 0.3-0.5)
    min_value: float = 0.35         # 0..1
    exclude_hue_ranges: list[list[int]] = field(
        default_factory=lambda: [[30, 90]]  # OpenCV hue 0..180; greens/yellow-greens
    )
    min_area_m2: float = 0.04
    max_area_m2: float = 12.0
    max_candidates: int = 25


@dataclass
class ThermalConfig:
    """Hot-spot detector for thermal frames."""

    enabled: bool = True
    background_sigma_px: float = 25.0  # size of local background estimate
    k_mad: float = 4.0                 # residual threshold in robust sigmas
    min_percentile: float = 95.0       # blob must also be in the hottest pixels
    min_area_m2: float = 0.05
    max_area_m2: float = 6.0
    max_candidates: int = 25
    # Radiometric mode via the DJI Thermal SDK (libdirp). Optional.
    dirp_library: str | None = None    # path to libdirp.so / libdirp.dll
    temp_min_c: float = 20.0           # radiometric band of interest
    temp_max_c: float = 42.0


@dataclass
class MosaicConfig:
    enabled: bool = True
    gsd_m: float = 0.25                 # output ground sample distance
    min_pitch_deg: float = 35.0         # skip frames looking flatter than this
    max_overlay_px: int = 4000          # cap canvas size per frame


@dataclass
class Config:
    camera: CameraDefaults = field(default_factory=CameraDefaults)
    rx: RXConfig = field(default_factory=RXConfig)
    bright: BrightColourConfig = field(default_factory=BrightColourConfig)
    thermal: ThermalConfig = field(default_factory=ThermalConfig)
    mosaic: MosaicConfig = field(default_factory=MosaicConfig)
    thumb_window_m: float = 4.0          # ground width of candidate thumbnails
    thumb_min_px: int = 160
    thumb_max_px: int = 512
    dem_path: str | None = None          # optional GeoTIFF DEM (needs rasterio)

    FILENAME = "pointersearch.json"

    @classmethod
    def load(cls, workdir: Path) -> "Config":
        path = Path(workdir) / cls.FILENAME
        cfg = cls()
        if path.exists():
            data = json.loads(path.read_text())
            cfg = _merge(cfg, data)
        return cfg

    def save(self, workdir: Path) -> Path:
        path = Path(workdir) / self.FILENAME
        path.write_text(json.dumps(asdict(self), indent=2))
        return path


def _merge(cfg, data: dict):
    """Overlay JSON values onto a dataclass tree, ignoring unknown keys."""
    for key, value in data.items():
        if not hasattr(cfg, key):
            continue
        current = getattr(cfg, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _merge(current, value)
        else:
            setattr(cfg, key, value)
    return cfg
