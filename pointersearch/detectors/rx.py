"""Reed-Xiaoli (RX) colour anomaly detector.

Scores each pixel by its Mahalanobis distance from the image's own colour
distribution. Anything that is statistically unusual for *this* scene (a blue
jacket on brown scrub, an orange pack in green grass) scores high, with no
training data. This is the method with the longest track record in wilderness
search literature and the basis of the commercial Loc8 product.

Scoring is done in CIELAB with the lightness channel down-weighted so shadows and
sun glint do not dominate.
"""

from __future__ import annotations

import cv2
import numpy as np

from ..config import RXConfig
from .base import Candidate, blobs_from_mask, downscale


def rx_score_map(bgr: np.ndarray, lum_weight: float = 0.3) -> np.ndarray:
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    lab[:, :, 0] *= lum_weight
    X = lab.reshape(-1, 3)
    mu = X.mean(axis=0)
    Xc = X - mu
    cov = (Xc.T @ Xc) / max(1, Xc.shape[0] - 1)
    cov += np.eye(3) * 1e-3
    inv = np.linalg.inv(cov)
    d2 = np.einsum("ij,jk,ik->i", Xc, inv, Xc)
    return d2.reshape(bgr.shape[:2])


class RXDetector:
    name = "rx"

    def __init__(self, cfg: RXConfig):
        self.cfg = cfg

    def detect(self, bgr: np.ndarray, gsd_m: float) -> list[Candidate]:
        small, scale = downscale(bgr, self.cfg.work_width)
        score = rx_score_map(small, self.cfg.lum_weight)
        med = float(np.median(score))
        mad = float(np.median(np.abs(score - med))) * 1.4826
        thr = max(self.cfg.min_score, med + self.cfg.k_mad * mad)
        mask = score > thr
        return blobs_from_mask(mask, score, scale, gsd_m, self.cfg.min_area_m2,
                               self.cfg.max_area_m2, self.name, self.cfg.max_candidates)
