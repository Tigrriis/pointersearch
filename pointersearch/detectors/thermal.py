"""Thermal hot-spot detector.

Two modes:

* **Relative (default).** Works on the 8-bit thermal JPEG as rendered by the
  aircraft. The local background is estimated with a wide Gaussian blur and
  pixels that stand well above it (in robust sigma units) and sit in the hottest
  percentile of the frame become candidates. Use a palette where hotter is
  brighter: *White Hot*, or one of the white-hot palettes that paints the hottest
  band red (handled by :func:`heat_field`). Rainbow-style palettes will not work.

* **Radiometric (optional).** If ``thermal.dirp_library`` in the config points at
  DJI's Thermal SDK shared library (``libdirp``), the R-JPEG is decoded to a
  per-pixel temperature matrix and candidates must fall inside a temperature band
  (default 20-42 C, a living person or animal). The ctypes binding follows the
  published ``dirp_api.h`` and has not been exercised against real hardware yet.
"""

from __future__ import annotations

import ctypes
from pathlib import Path

import cv2
import numpy as np

from ..config import ThermalConfig
from .base import Candidate, blobs_from_mask, downscale


class DirpReader:
    """Minimal ctypes wrapper over the DJI Thermal SDK (libdirp)."""

    class _Resolution(ctypes.Structure):
        _fields_ = [("width", ctypes.c_int32), ("height", ctypes.c_int32)]

    def __init__(self, library: str):
        self.lib = ctypes.CDLL(library)
        self.lib.dirp_create_from_rjpeg.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_int32,
                                                     ctypes.POINTER(ctypes.c_void_p)]
        self.lib.dirp_create_from_rjpeg.restype = ctypes.c_int32
        self.lib.dirp_get_rjpeg_resolution.argtypes = [ctypes.c_void_p, ctypes.POINTER(self._Resolution)]
        self.lib.dirp_get_rjpeg_resolution.restype = ctypes.c_int32
        self.lib.dirp_measure.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int32]
        self.lib.dirp_measure.restype = ctypes.c_int32
        self.lib.dirp_destroy.argtypes = [ctypes.c_void_p]
        self.lib.dirp_destroy.restype = ctypes.c_int32

    def temperatures_c(self, rjpeg_path: str | Path) -> np.ndarray | None:
        data = Path(rjpeg_path).read_bytes()
        buf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
        handle = ctypes.c_void_p()
        if self.lib.dirp_create_from_rjpeg(buf, len(data), ctypes.byref(handle)) != 0:
            return None
        try:
            res = self._Resolution()
            if self.lib.dirp_get_rjpeg_resolution(handle, ctypes.byref(res)) != 0:
                return None
            n = res.width * res.height
            out = (ctypes.c_int16 * n)()
            if self.lib.dirp_measure(handle, out, n * 2) != 0:
                return None
            return np.frombuffer(out, dtype=np.int16).reshape(res.height, res.width).astype(np.float32) / 10.0
        finally:
            self.lib.dirp_destroy(handle)


def red_mask(bgr: np.ndarray) -> np.ndarray:
    """Pixels the aircraft's palette painted as its hottest band.

    Measured on Matrice 4T frames the band is a pinkish red (about R 210, G 110, B 110),
    softened further at edges by JPEG compression, so the test is "clearly redder than
    grey" instead of "pure red".
    """
    if bgr.ndim == 2:
        return np.zeros(bgr.shape, bool)
    b, g, r = (bgr[:, :, i].astype(np.int16) for i in range(3))
    return (r > 120) & ((r - np.maximum(g, b)) > 35)


def heat_field(bgr: np.ndarray) -> np.ndarray:
    """Relative heat per pixel from a rendered thermal picture: brighter means hotter.

    Several DJI palettes are white-hot greyscale with the hottest band painted red. A
    plain greyscale conversion turns that red into a mid grey, ranking the hottest
    pixels below merely warm ones, so red pixels are lifted above white.
    """
    if bgr.ndim == 2:
        return bgr.astype(np.float32)
    field = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    b, g, r = (bgr[:, :, i].astype(np.int16) for i in range(3))
    red = red_mask(bgr)
    field[red] = 255.0 + (r - np.maximum(g, b))[red] * 0.2      # hotter than the whitest grey
    return field


class ThermalDetector:
    name = "thermal"

    def __init__(self, cfg: ThermalConfig):
        self.cfg = cfg
        self.dirp = DirpReader(cfg.dirp_library) if cfg.dirp_library else None

    def detect(self, bgr: np.ndarray, gsd_m: float, path: str | Path | None = None) -> list[Candidate]:
        temps = None
        if self.dirp is not None and path is not None:
            try:
                temps = self.dirp.temperatures_c(path)
            except Exception:
                temps = None

        red = None
        if temps is not None:
            field = temps
            scale = 1.0
        else:
            small, scale = downscale(heat_field(bgr), 1280)
            field = small.astype(np.float32)
            if self.cfg.red_hot and bgr.ndim == 3:
                rm = red_mask(bgr).astype(np.uint8)
                if scale != 1.0:
                    rm = cv2.resize(rm, (field.shape[1], field.shape[0]), interpolation=cv2.INTER_NEAREST)
                red = rm

        sigma = self.cfg.background_sigma_px
        background = cv2.GaussianBlur(field, (0, 0), sigma)
        residual = field - background
        mad = float(np.median(np.abs(residual - np.median(residual)))) * 1.4826 + 1e-6
        hot_floor = float(np.percentile(field, self.cfg.min_percentile))
        mask = (residual > self.cfg.k_mad * mad) & (field >= hot_floor)
        if temps is not None:
            mask &= (temps >= self.cfg.temp_min_c) & (temps <= self.cfg.temp_max_c)
        score = residual / mad
        cands = blobs_from_mask(mask, score, scale, gsd_m, self.cfg.min_area_m2,
                                self.cfg.max_area_m2, self.name, self.cfg.max_candidates, open_iters=0)
        if temps is not None:
            for c in cands:
                x, y, w, h = c.bbox
                c.extra["peak_temp_c"] = float(np.max(temps[y:y + h, x:x + w]))

        if red is not None and red.any():
            # The red band is what the aircraft itself marked as hottest. Find those areas
            # on their own, so a red-hot core is kept even when it sits inside a warm region
            # that is too large to be a person or animal and is discarded as a whole.
            red = cv2.morphologyEx(red, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))      # bridge JPEG gaps
            px_area = (gsd_m / scale) ** 2
            min_area = max(self.cfg.red_min_area_m2, self.cfg.red_min_px * px_area)
            hot = blobs_from_mask(red.astype(bool), score, scale, gsd_m, min_area, self.cfg.max_area_m2,
                                  self.name, self.cfg.max_candidates, open_iters=0)
            for c in hot:
                c.extra["red_hot"] = True
            hot.sort(key=lambda c: c.area_m2, reverse=True)     # a body-sized area before a speck

            def holds(c, other):               # other's centre lies inside c's box
                x, y, w, h = c.bbox
                return x <= other.px <= x + w and y <= other.py <= y + h
            # a warm region that merely surrounds a red-hot core is the same thing: keep the core
            cands = hot + [c for c in cands if not any(holds(c, h_) for h_ in hot)]
            cands = cands[:self.cfg.max_candidates]            # red-hot first (largest first), then the rest by score
        return cands
