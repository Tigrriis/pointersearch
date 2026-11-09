"""Camera geometry helpers: focal length in pixels and ground sample distance."""

from __future__ import annotations

import math

from .config import CameraDefaults
from .metadata import ImageMeta

FULL_FRAME_DIAGONAL_MM = 43.266  # 36 x 24 mm


def focal_px(meta: ImageMeta, defaults: CameraDefaults) -> float:
    """Focal length in pixels for the full-resolution frame.

    Uses the 35 mm-equivalent focal length when present (it already accounts for
    sensor size), otherwise the per-lens default from config. The diagonal is used
    so 3:2 and 4:3 frames are both handled.
    """
    f35 = meta.focal_35mm
    if not f35 or f35 <= 0:
        f35 = getattr(defaults, meta.source, None) or defaults.unknown
    diag_px = math.hypot(meta.width, meta.height)
    return f35 * diag_px / FULL_FRAME_DIAGONAL_MM


def gsd_m(meta: ImageMeta, defaults: CameraDefaults, height_m: float | None = None) -> float:
    """Ground sample distance (metres per pixel) at nadir for the given height."""
    h = meta.rel_alt if height_m is None else height_m
    return max(h, 0.1) / focal_px(meta, defaults)


def hfov_deg(meta: ImageMeta, defaults: CameraDefaults) -> float:
    return math.degrees(2 * math.atan(meta.width / (2 * focal_px(meta, defaults))))
