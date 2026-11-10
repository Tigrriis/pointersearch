"""The same moment through each lens.

Enterprise DJI aircraft save one file per lens for every shutter press, named
alike except for a suffix (``_W`` wide, ``_Z`` zoom, ``_T`` thermal). For an
object found in one of them this module finds the sibling files, works out where
the object falls in each lens, and renders a matching close-up, but only for
lenses whose frame actually contains the object.

The lenses share one gimbal, so a pixel is carried from one lens to another by
its viewing direction: no terrain or altitude is involved. A small fixed angular
offset per lens (``lens_align_deg`` in the mission config) allows for the lenses
not being perfectly parallel.
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from .camera import focal_px
from .capture import capture_key, same_moment  # noqa: F401  (re-exported)
from .config import Config
from .georef import Georeferencer, rotation_cam_to_ned
from .metadata import ImageMeta, read_image_meta

LENS_ORDER = ["wide", "zoom", "thermal", "unknown"]
LENS_LABEL = {"wide": "Wide", "zoom": "Zoom", "thermal": "Thermal", "unknown": "Photo"}
VIEW_SIZE = 512
@lru_cache(maxsize=512)
def _meta(path: str, mtime: float) -> ImageMeta:
    return read_image_meta(path)


def meta_for(path: str | Path) -> ImageMeta | None:
    p = Path(path)
    try:
        return _meta(str(p), p.stat().st_mtime)
    except Exception:
        return None


def map_pixel(src: ImageMeta, dst: ImageMeta, px: float, py: float, cfg: Config) -> tuple[float, float] | None:
    """Where the direction seen at (px, py) in ``src`` lands in ``dst``.

    Returns None if that direction is behind the destination camera. The result
    may lie outside the destination frame; the caller checks that.
    """
    f1, f2 = focal_px(src, cfg.camera), focal_px(dst, cfg.camera)
    a1 = cfg.lens_align_deg.get(src.source, [0.0, 0.0])
    a2 = cfg.lens_align_deg.get(dst.source, [0.0, 0.0])
    # direction in the source camera frame, with its alignment offset removed
    v = np.array([px - src.width / 2.0 - f1 * math.tan(math.radians(a1[0])),
                  py - src.height / 2.0 - f1 * math.tan(math.radians(a1[1])), f1], dtype=float)
    d = rotation_cam_to_ned(src.gimbal_yaw, src.gimbal_pitch, src.gimbal_roll) @ v
    w = rotation_cam_to_ned(dst.gimbal_yaw, dst.gimbal_pitch, dst.gimbal_roll).T @ d
    if w[2] <= 1e-9:
        return None
    return (dst.width / 2.0 + f2 * w[0] / w[2] + f2 * math.tan(math.radians(a2[0])),
            dst.height / 2.0 + f2 * w[1] / w[2] + f2 * math.tan(math.radians(a2[1])))


def window_px(meta: ImageMeta, cfg: Config) -> int:
    """Side of the square cut from a frame so each lens shows a similar patch of ground."""
    gsd = Georeferencer(meta, cfg.camera).ground_gsd()
    want = cfg.thumb_window_m / max(gsd, 1e-4)
    return int(max(48, min(cfg.thumb_max_px, max(cfg.thumb_min_px, want), min(meta.width, meta.height))))


def views_for_candidate(store, cfg: Config, cand: dict) -> list[dict]:
    """One entry per lens that saw the object at the moment of ``cand``'s frame."""
    primary = store.get_image(cand["image_id"])
    if primary is None:
        return []
    src = meta_for(primary["path"])
    out = []
    bx = cand.get("bbox") or [0, 0, 12, 12]
    for img in store.siblings(primary):
        is_primary = img["id"] == primary["id"]
        lens = img["source"] or "unknown"
        entry = {"lens": lens, "label": LENS_LABEL.get(lens, lens.title()), "image_id": img["id"],
                 "image_name": Path(img["path"]).name, "primary": is_primary, "available": True, "reason": None}
        if is_primary:
            entry.update(px=cand["px"], py=cand["py"], url="files/" + cand["thumb"])
        else:
            if not same_moment(primary["path"], img["path"]) or src is None:
                continue
            dst = meta_for(img["path"])
            if dst is None:
                entry.update(available=False, reason="the photo file was not found; is the card or folder connected?")
                out.append(entry)
                continue
            hit = map_pixel(src, dst, cand["px"], cand["py"], cfg)
            if hit is None or not (0 <= hit[0] < dst.width and 0 <= hit[1] < dst.height):
                entry.update(available=False, reason="the object is outside this lens's frame")
                out.append(entry)
                continue
            scale = focal_px(dst, cfg.camera) / focal_px(src, cfg.camera)
            bw, bh = max(10, round(bx[2] * scale)), max(10, round(bx[3] * scale))
            win = window_px(dst, cfg)
            entry.update(px=round(hit[0], 1), py=round(hit[1], 1),
                         url=f"api/view/{img['id']}.jpg?px={hit[0]:.0f}&py={hit[1]:.0f}&win={win}&bw={bw}&bh={bh}")
        out.append(entry)
    out.sort(key=lambda e: LENS_ORDER.index(e["lens"]) if e["lens"] in LENS_ORDER else 99)
    return out


MAX_SIGHTINGS_CHECKED = 12


def views_for_group(store, cfg: Config, group: dict, candidate_id: int | None = None) -> dict | None:
    """Lens views for one object.

    An object is usually seen in several shots. ``candidate_id`` picks the shot;
    without it the best-scored sighting is used, unless another sighting of the
    same object was in frame for more lenses, in which case that one is preferred
    so the lens switch has something to switch to.
    """
    members = group["members"]
    if candidate_id is not None:
        if candidate_id not in members:
            return None
        cand = store.get_candidate(candidate_id)
        return {"candidate_id": candidate_id, "views": views_for_candidate(store, cfg, cand)} if cand else None
    best_id, best_views, best_n = None, [], -1
    for cid in members[:MAX_SIGHTINGS_CHECKED]:             # members are ordered best score first
        cand = store.get_candidate(cid)
        if cand is None:
            continue
        views = views_for_candidate(store, cfg, cand)
        n = sum(1 for v in views if v["available"])
        if n > best_n:
            best_id, best_views, best_n = cid, views, n
        if n >= 3:
            break
    return {"candidate_id": best_id, "views": best_views}


def render_view(image_path: str | Path, px: float, py: float, win: int, bw: int, bh: int,
                out_path: Path, size: int = VIEW_SIZE) -> bool:
    """Square close-up centred on (px, py) with a marker box. Padded, never stretched,
    when the object is near the edge of the frame."""
    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if image is None:
        return False
    h, w = image.shape[:2]
    half = max(8, int(win) // 2)
    cx, cy = int(round(px)), int(round(py))
    x0, y0, x1, y1 = cx - half, cy - half, cx + half, cy + half
    crop = image[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]
    crop = cv2.copyMakeBorder(crop, max(0, -y0), max(0, y1 - h), max(0, -x0), max(0, x1 - w),
                              cv2.BORDER_CONSTANT, value=(0, 0, 0))
    scale = size / crop.shape[0]
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    view = cv2.resize(crop, (size, size), interpolation=interp)
    c = size // 2
    hw, hh = max(6, int(bw * scale / 2) + 3), max(6, int(bh * scale / 2) + 3)
    cv2.rectangle(view, (c - hw, c - hh), (c + hw, c + hh), (0, 255, 255), 1)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    return bool(cv2.imwrite(str(out_path), view, [cv2.IMWRITE_JPEG_QUALITY, 88]))
