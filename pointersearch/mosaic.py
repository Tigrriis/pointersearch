"""Quick mosaic by planar projection of each frame onto the ground.

Each image is warped with a homography from its four pixel corners to their
ground positions, producing a north-up PNG with transparency plus its lat/lon
bounds. The review map stacks these as image overlays; ``merge`` paints them all
into a single north-up PNG with an ESRI world file for QGIS / CalTopo import.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .config import MosaicConfig
from .georef import Georeferencer, LocalFrame


@dataclass
class Overlay:
    png_path: str
    south: float
    west: float
    north: float
    east: float
    width_px: int
    height_px: int

    @property
    def bounds(self) -> list[list[float]]:
        return [[self.south, self.west], [self.north, self.east]]


def _latlon_bbox(corners: list[tuple[float, float]]):
    lats = [c[0] for c in corners]
    lons = [c[1] for c in corners]
    return min(lats), min(lons), max(lats), max(lons)


def edge_fade(height: int, width: int, fraction: float) -> np.ndarray:
    """Opacity (0-255) that is full in the middle of a photo and fades out towards its edges.

    The middle of a photo is its most trustworthy part: least lens distortion, and tall
    objects lean least there. Fading the edges lets the photo underneath show through
    where two overlap, in place of a hard line.
    """
    if fraction <= 0:
        return np.full((height, width), 255, np.uint8)
    band = max(1.0, fraction * min(height, width))
    y = np.minimum(np.arange(height), np.arange(height)[::-1]).astype(np.float32)[:, None]
    x = np.minimum(np.arange(width), np.arange(width)[::-1]).astype(np.float32)[None, :]
    ramp = np.clip((np.minimum(y, x) + 1.0) / band, 0.0, 1.0)
    return (ramp * 255).astype(np.uint8)


def warp_frame(image_bgr: np.ndarray, geo: Georeferencer, cfg: MosaicConfig, out_png: Path,
               feather: float = 0.0) -> Overlay | None:
    """Warp one frame to a north-up ground canvas. Returns None if the frame is unusable."""
    if geo.meta.gimbal_pitch > -cfg.min_pitch_deg:
        return None
    corners = geo.footprint_latlon()
    if corners is None:
        return None
    south, west, north, east = _latlon_bbox(corners)
    frame = LocalFrame((south + north) / 2, (west + east) / 2)
    n_min, e_min = frame.to_ne(south, west)
    n_max, e_max = frame.to_ne(north, east)
    width_m, height_m = e_max - e_min, n_max - n_min
    if width_m <= 0 or height_m <= 0:
        return None
    gsd = cfg.gsd_m
    longest = max(width_m, height_m) / gsd
    if longest > cfg.max_overlay_px:
        gsd = max(width_m, height_m) / cfg.max_overlay_px
    W = max(2, int(math.ceil(width_m / gsd)))
    H = max(2, int(math.ceil(height_m / gsd)))

    h_img, w_img = image_bgr.shape[:2]
    # Downscale the source so we never warp far more pixels than the canvas holds.
    scale = min(1.0, (max(W, H) * 1.5) / max(w_img, h_img))
    if scale < 1.0:
        src = cv2.resize(image_bgr, (int(w_img * scale), int(h_img * scale)), interpolation=cv2.INTER_AREA)
    else:
        src = image_bgr
    sh, sw = src.shape[:2]
    src_pts = np.array([[0, 0], [sw, 0], [sw, sh], [0, sh]], dtype=np.float32)
    dst_pts = []
    for lat, lon in corners:
        n, e = frame.to_ne(lat, lon)
        dst_pts.append([(e - e_min) / gsd, (n_max - n) / gsd])
    dst_pts = np.array(dst_pts, dtype=np.float32)
    Hm = cv2.getPerspectiveTransform(src_pts, dst_pts)

    if src.ndim == 2:
        src = cv2.cvtColor(src, cv2.COLOR_GRAY2BGR)
    bgra = cv2.cvtColor(src, cv2.COLOR_BGR2BGRA)
    bgra[:, :, 3] = edge_fade(sh, sw, feather)
    warped = cv2.warpPerspective(bgra, Hm, (W, H), flags=cv2.INTER_LINEAR,
                                 borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_png), warped)
    return Overlay(str(out_png), south, west, north, east, W, H)


def write_world_file(png_path: Path, south: float, west: float, north: float, east: float, W: int, H: int) -> Path:
    """ESRI world file (EPSG:4326) so the PNG opens georeferenced in QGIS."""
    dx = (east - west) / W
    dy = (north - south) / H
    pgw = png_path.with_suffix(".pgw")
    pgw.write_text(f"{dx:.12f}\n0.0\n0.0\n{-dy:.12f}\n{west + dx / 2:.12f}\n{north - dy / 2:.12f}\n")
    png_path.with_suffix(".png.aux.xml").write_text(
        '<PAMDataset><SRS>GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]</SRS></PAMDataset>\n'
    )
    return pgw


def apply_correction(base_png: Path, bounds: list, corr, frame: LocalFrame, out_png: Path) -> Overlay | None:
    """Re-lay a photo's map patch at its corrected place (shift, turn, scale) and brightness.

    ``bounds`` are the patch's [[south, west], [north, east]] as first placed; ``corr`` is
    the photo's :class:`pointersearch.align.Correction` in ``frame`` coordinates.
    """
    img = cv2.imread(str(base_png), cv2.IMREAD_UNCHANGED)
    if img is None or img.ndim != 3 or img.shape[2] != 4:
        return None
    h, w = img.shape[:2]
    (south, west), (north, east) = bounds
    n0, e0 = frame.to_ne(north, west)                      # top-left of the patch
    n1, e1 = frame.to_ne(south, east)                      # bottom-right
    gn, ge = (n0 - n1) / h, (e1 - e0) / w                  # metres per pixel, down and across
    px = np.array([[0, 0], [w, 0], [0, h], [w, h]], np.float32)
    ne = corr.apply(np.column_stack([n0 - px[:, 1] * gn, e0 + px[:, 0] * ge]))
    n_max, n_min, e_min, e_max = ne[:, 0].max(), ne[:, 0].min(), ne[:, 1].min(), ne[:, 1].max()
    g = min(gn, ge) * corr.scale                           # keep the patch's level of detail
    W, H = max(2, int(math.ceil((e_max - e_min) / g))), max(2, int(math.ceil((n_max - n_min) / g)))
    if max(W, H) > 8000:
        return None
    dst = np.column_stack([(ne[:, 1] - e_min) / g, (n_max - ne[:, 0]) / g]).astype(np.float32)
    M = cv2.getAffineTransform(px[:3], dst[:3])
    if abs(corr.gain - 1.0) > 0.01:
        img = img.copy()
        img[:, :, :3] = np.clip(img[:, :, :3].astype(np.float32) * corr.gain, 0, 255).astype(np.uint8)
    out = cv2.warpAffine(img, M, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_png), out)
    s_lat, w_lon = frame.to_latlon(n_min, e_min)
    n_lat, e_lon = frame.to_latlon(n_max, e_max)
    return Overlay(str(out_png), s_lat, w_lon, n_lat, e_lon, W, H)


def merge_overlays(overlays: list[Overlay], out_png: Path, max_px: int = 12000) -> Overlay | None:
    """Blend all map patches into one north-up PNG + world file.

    Where patches overlap, each contributes in proportion to its opacity raised to a
    power, so the photo whose middle is nearest dominates and the join between two
    photos is a short, smooth transition, not a hard line or a double image.
    """
    if not overlays:
        return None
    south = min(o.south for o in overlays)
    west = min(o.west for o in overlays)
    north = max(o.north for o in overlays)
    east = max(o.east for o in overlays)
    dpp_lat = min((o.north - o.south) / o.height_px for o in overlays)
    dpp_lon = min((o.east - o.west) / o.width_px for o in overlays)
    W = int(math.ceil((east - west) / dpp_lon))
    H = int(math.ceil((north - south) / dpp_lat))
    if max(W, H) > max_px:
        s = max_px / max(W, H)
        W, H = max(1, int(W * s)), max(1, int(H * s))
    colour = np.zeros((H, W, 3), dtype=np.float32)
    weight = np.zeros((H, W), dtype=np.float32)
    cover = np.zeros((H, W), dtype=np.uint8)
    for o in overlays:
        img = cv2.imread(o.png_path, cv2.IMREAD_UNCHANGED)
        if img is None or img.ndim != 3 or img.shape[2] != 4:
            continue
        x0 = int(round((o.west - west) / (east - west) * W))
        x1 = int(round((o.east - west) / (east - west) * W))
        y0 = int(round((north - o.north) / (north - south) * H))
        y1 = int(round((north - o.south) / (north - south) * H))
        w, h = max(1, x1 - x0), max(1, y1 - y0)
        tile = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
        th, tw = colour[y0:y0 + h, x0:x0 + w].shape[:2]
        tile = tile[:th, :tw]
        a = tile[:, :, 3].astype(np.float32) / 255.0
        wgt = a ** 3 + 1e-4 * (a > 0)                       # nearest-middle photo dominates; edges never win
        colour[y0:y0 + th, x0:x0 + tw] += tile[:, :, :3].astype(np.float32) * wgt[:, :, None]
        weight[y0:y0 + th, x0:x0 + tw] += wgt
        cover[y0:y0 + th, x0:x0 + tw] = np.maximum(cover[y0:y0 + th, x0:x0 + tw], (a > 0).astype(np.uint8) * 255)
    canvas = np.zeros((H, W, 4), dtype=np.uint8)
    has = weight > 0
    canvas[:, :, :3][has] = np.clip(colour[has] / weight[has][:, None], 0, 255).astype(np.uint8)
    canvas[:, :, 3] = cover
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_png), canvas)
    write_world_file(out_png, south, west, north, east, W, H)
    return Overlay(str(out_png), south, west, north, east, W, H)
