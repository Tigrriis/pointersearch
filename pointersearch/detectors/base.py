from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np


@dataclass
class Candidate:
    detector: str
    score: float
    px: float              # centroid x in full-res pixels
    py: float              # centroid y in full-res pixels
    bbox: tuple[int, int, int, int]  # x, y, w, h in full-res pixels
    area_m2: float
    extra: dict = field(default_factory=dict)


def blobs_from_mask(mask: np.ndarray, score_map: np.ndarray, scale: float, gsd_m: float,
                    min_area_m2: float, max_area_m2: float, detector: str,
                    max_candidates: int, open_iters: int = 1) -> list[Candidate]:
    """Connected components of a boolean mask -> candidates in full-res coords.

    ``scale`` is work_width / full_width (the factor the image was downscaled by).
    """
    m = mask.astype(np.uint8)
    if open_iters:
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8), iterations=open_iters)
    n, labels, stats, centroids = cv2.connectedComponentsWithStats(m, connectivity=8)
    px_area_m2 = (gsd_m / scale) ** 2  # ground area of one work-resolution pixel
    out: list[Candidate] = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        area_m2 = float(area) * px_area_m2
        if area_m2 < min_area_m2 or area_m2 > max_area_m2:
            continue
        blob = labels[y:y + h, x:x + w] == i
        score = float(np.max(score_map[y:y + h, x:x + w][blob]))
        cx, cy = centroids[i]
        out.append(Candidate(
            detector=detector,
            score=score,
            px=float(cx / scale),
            py=float(cy / scale),
            bbox=(int(x / scale), int(y / scale), int(max(1, w / scale)), int(max(1, h / scale))),
            area_m2=area_m2,
        ))
    out.sort(key=lambda c: c.score, reverse=True)
    return out[:max_candidates]


def crop_thumbnail(image: np.ndarray, cand: Candidate, window_px: int, out_size: int = 256) -> np.ndarray:
    """Square crop around a candidate with a marker box, resized for the review UI."""
    h, w = image.shape[:2]
    half = max(8, window_px // 2)
    x0 = int(max(0, cand.px - half))
    y0 = int(max(0, cand.py - half))
    x1 = int(min(w, cand.px + half))
    y1 = int(min(h, cand.py + half))
    crop = image[y0:y1, x0:x1].copy()
    if crop.ndim == 2:
        crop = cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR)
    bx, by, bw, bh = cand.bbox
    cv2.rectangle(crop, (bx - x0 - 2, by - y0 - 2), (bx - x0 + bw + 2, by - y0 + bh + 2), (0, 255, 255), 1)
    return cv2.resize(crop, (out_size, out_size), interpolation=cv2.INTER_AREA)


def downscale(image: np.ndarray, work_width: int) -> tuple[np.ndarray, float]:
    h, w = image.shape[:2]
    if w <= work_width:
        return image, 1.0
    scale = work_width / w
    return cv2.resize(image, (work_width, int(round(h * scale))), interpolation=cv2.INTER_AREA), scale
