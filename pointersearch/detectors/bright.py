"""Saturated / unnatural colour detector.

A simple HSV threshold that complements RX: RX finds what is unusual for the
scene, this finds what is unusual for nature regardless of scene (high-chroma
reds, oranges, blues, magentas). Green and yellow-green hues are excluded by
default because vegetation is saturated too.
"""

from __future__ import annotations

import cv2
import numpy as np

from ..config import BrightColourConfig
from .base import Candidate, blobs_from_mask, downscale


class BrightColourDetector:
    name = "bright"

    def __init__(self, cfg: BrightColourConfig):
        self.cfg = cfg

    def detect(self, bgr: np.ndarray, gsd_m: float) -> list[Candidate]:
        small, scale = downscale(bgr, self.cfg.work_width)
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        h = hsv[:, :, 0]
        s = hsv[:, :, 1].astype(np.float32) / 255.0
        v = hsv[:, :, 2].astype(np.float32) / 255.0
        mask = (s >= self.cfg.min_saturation) & (v >= self.cfg.min_value)
        for lo, hi in self.cfg.exclude_hue_ranges:
            mask &= ~((h >= lo) & (h <= hi))
        score = s * v
        return blobs_from_mask(mask, score, scale, gsd_m, self.cfg.min_area_m2,
                               self.cfg.max_area_m2, self.name, self.cfg.max_candidates)
