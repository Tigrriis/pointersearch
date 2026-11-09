"""Per-image processing: metadata -> georeference -> overlay -> detect -> store."""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import cv2

from .camera import gsd_m
from .config import Config
from .detectors import BrightColourDetector, RXDetector, ThermalDetector, crop_thumbnail
from .georef import Georeferencer
from .metadata import MetadataError, read_image_meta
from .mosaic import warp_frame
from .store import Store
from .terrain import load_ground

log = logging.getLogger("pointersearch")
IMAGE_SUFFIXES = {".jpg", ".jpeg"}


class Pipeline:
    def __init__(self, workdir: str | Path, cfg: Config | None = None, store: Store | None = None):
        self.workdir = Path(workdir)
        self.cfg = cfg or Config.load(self.workdir)
        self.store = store or Store(self.workdir)
        self.ground = load_ground(self.cfg.dem_path)
        self.overlay_dir = self.workdir / "overlays"
        self.thumb_dir = self.workdir / "thumbs"
        self.rx = RXDetector(self.cfg.rx) if self.cfg.rx.enabled else None
        self.bright = BrightColourDetector(self.cfg.bright) if self.cfg.bright.enabled else None
        self.thermal = ThermalDetector(self.cfg.thermal) if self.cfg.thermal.enabled else None

    # ------------------------------------------------------------------
    def process_path(self, path: str | Path, force: bool = False) -> int:
        """Process a file or directory. Returns number of images processed."""
        path = Path(path)
        files = [path] if path.is_file() else sorted(
            p for p in path.rglob("*") if p.suffix.lower() in IMAGE_SUFFIXES)
        n = 0
        for f in files:
            if self.process_image(f, force=force):
                n += 1
        return n

    def process_image(self, path: Path, force: bool = False) -> bool:
        path = Path(path).resolve()
        if not force and self.store.image_is_current(path):
            return False
        try:
            meta = read_image_meta(path)
        except MetadataError as exc:
            log.warning("skip %s: %s", path.name, exc)
            self.store.upsert_image({"path": str(path), "mtime": path.stat().st_mtime,
                                     "status": "skipped", "error": str(exc)})
            return False
        except Exception as exc:  # unreadable file
            log.warning("skip %s: %s", path.name, exc)
            self.store.upsert_image({"path": str(path), "mtime": path.stat().st_mtime,
                                     "status": "error", "error": repr(exc)})
            return False

        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            self.store.upsert_image({"path": str(path), "mtime": path.stat().st_mtime,
                                     "status": "error", "error": "cv2 could not decode"})
            return False

        geo = Georeferencer(meta, self.cfg.camera, self.ground)
        footprint = geo.footprint_latlon()
        centre_gsd = geo.ground_gsd()

        overlay = None
        if self.cfg.mosaic.enabled and footprint is not None:
            overlay = warp_frame(image, geo, self.cfg.mosaic, self.overlay_dir / f"{path.stem}.png")

        record = {
            "path": str(path), "mtime": path.stat().st_mtime, "source": meta.source,
            "lat": meta.lat, "lon": meta.lon, "rel_alt": meta.rel_alt,
            "gimbal_yaw": meta.gimbal_yaw, "gimbal_pitch": meta.gimbal_pitch, "gimbal_roll": meta.gimbal_roll,
            "width": meta.width, "height": meta.height, "gsd_m": centre_gsd, "captured_at": meta.captured_at,
            "footprint": json.dumps(footprint) if footprint else None,
            "overlay_png": _rel(self.workdir, overlay.png_path) if overlay else None,
            "overlay_bounds": json.dumps(overlay.bounds) if overlay else None,
            "status": "ok", "error": None,
        }
        image_id = self.store.upsert_image(record)

        candidates = []
        if meta.is_thermal:
            if self.thermal:
                candidates += self.thermal.detect(image, centre_gsd, path=path)
        else:
            if self.rx:
                candidates += self.rx.detect(image, centre_gsd)
            if self.bright:
                candidates += self.bright.detect(image, centre_gsd)

        self.thumb_dir.mkdir(parents=True, exist_ok=True)
        thumb_window_px = int(min(self.cfg.thumb_max_px,
                                  max(self.cfg.thumb_min_px, self.cfg.thumb_window_m / max(centre_gsd, 1e-4))))
        rows = []
        for i, c in enumerate(candidates):
            ll = geo.pixel_to_latlon(c.px, c.py)
            if ll is None:
                continue
            ne = geo.pixel_to_ne(c.px, c.py)
            range_m = math.hypot(*ne) if ne else None
            thumb_name = f"{path.stem}_{c.detector}_{i:02d}.jpg"
            cv2.imwrite(str(self.thumb_dir / thumb_name), crop_thumbnail(image, c, thumb_window_px),
                        [cv2.IMWRITE_JPEG_QUALITY, 85])
            rows.append({"detector": c.detector, "score": round(c.score, 3), "px": c.px, "py": c.py,
                         "bbox": list(c.bbox), "area_m2": round(c.area_m2, 3), "lat": ll[0], "lon": ll[1],
                         "thumb": f"thumbs/{thumb_name}", "extra": c.extra, "range_m": range_m})
        self.store.replace_candidates(image_id, rows)
        self.store.regroup(self.cfg.group_radius_m)
        log.info("%s: %s, gsd %.3f m/px, %d candidates", path.name, meta.source, centre_gsd, len(rows))
        return True


def _rel(workdir: Path, p: str) -> str:
    try:
        return str(Path(p).resolve().relative_to(workdir.resolve()))
    except ValueError:
        return p
