"""Thermal hot-spot detector.

Two modes:

* **Relative (default).** Works on the 8-bit thermal JPEG as rendered by the
  aircraft. The local background is estimated with a wide Gaussian blur and
  pixels that stand well above it (in robust sigma units) and sit in the hottest
  percentile of the frame become candidates. Use the *White Hot* palette on the
  aircraft so brightness is monotonic in temperature.

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

        if temps is not None:
            field = temps
            scale = 1.0
        else:
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
            small, scale = downscale(gray, 1280)
            field = small.astype(np.float32)

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
        return cands
