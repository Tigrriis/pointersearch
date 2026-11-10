"""Per-image processing: metadata -> georeference -> overlay -> detect -> store."""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import cv2
import numpy as np

from . import align as _align
from .camera import gsd_m
from .config import Config
from .detectors import BrightColourDetector, RXDetector, ThermalDetector, crop_thumbnail
from .georef import Georeferencer
from .metadata import MetadataError, read_image_meta
from .mosaic import apply_correction, warp_frame
from .store import Store
from .terrain import load_ground
from .capture import capture_key, has_wide_sibling
from .geo import LocalFrame

log = logging.getLogger("pointersearch")
# Raised for a lens when its detection changes enough that images already in a mission
# should be searched again. The review map offers to do that.
DETECTOR_VERSION = {"thermal": 3, "wide": 2, "unknown": 2, "zoom": 2}


def detector_version(source: str | None) -> int:
    return DETECTOR_VERSION.get(source or "", 1)
IMAGE_SUFFIXES = {".jpg", ".jpeg"}


class Pipeline:
    def __init__(self, workdir: str | Path, cfg: Config | None = None, store: Store | None = None):
        self.workdir = Path(workdir)
        self.cfg = cfg or Config.load(self.workdir)
        self.store = store or Store(self.workdir)
        self.ground = load_ground(self.cfg.dem_path)
        self.overlay_dir = self.workdir / "overlays"
        self.thumb_dir = self.workdir / "thumbs"
        self.feature_dir = self.workdir / "features"
        self.aligned_dir = self.workdir / "overlays_aligned"
        self._feat_cache: dict[int, _align.Features] = {}
        self._spots: dict[int, tuple] | None = None      # image id -> (source, centre north, east, radius)
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

        split = meta.source == "split" and self.cfg.split_frames != "process"
        if split and self.cfg.split_frames == "ignore":
            self.store.upsert_image({"path": str(path), "mtime": path.stat().st_mtime, "source": "split",
                                     "status": "skipped", "error": "split-screen picture (split_frames = ignore)"})
            return False
        if split:
            # kept so the reviewer can look at it beside the lens frames; not searched, not mapped
            image_id = self.store.upsert_image({
                "path": str(path), "mtime": path.stat().st_mtime, "source": "split",
                "lat": meta.lat, "lon": meta.lon, "rel_alt": meta.rel_alt,
                "gimbal_yaw": meta.gimbal_yaw, "gimbal_pitch": meta.gimbal_pitch, "gimbal_roll": meta.gimbal_roll,
                "width": meta.width, "height": meta.height, "gsd_m": None, "captured_at": meta.captured_at,
                "footprint": None, "overlay_png": None, "overlay_bounds": None, "status": "ok", "error": None,
                "capture_key": capture_key(path) or f"single|{path}"})
            self.store.replace_candidates(image_id, [])
            log.info("%s: split-screen picture, kept for viewing", path.name)
            return True

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
            overlay = warp_frame(image, geo, self.cfg.mosaic, self.overlay_dir / f"{path.stem}.png",
                                 feather=self.cfg.align.feather if self.cfg.align.enabled else 0.0)

        record = {
            "path": str(path), "mtime": path.stat().st_mtime, "source": meta.source,
            "lat": meta.lat, "lon": meta.lon, "rel_alt": meta.rel_alt,
            "gimbal_yaw": meta.gimbal_yaw, "gimbal_pitch": meta.gimbal_pitch, "gimbal_roll": meta.gimbal_roll,
            "width": meta.width, "height": meta.height, "gsd_m": centre_gsd, "captured_at": meta.captured_at,
            "footprint": json.dumps(footprint) if footprint else None,
            "overlay_png": _rel(self.workdir, overlay.png_path) if overlay else None,
            "overlay_bounds": json.dumps(overlay.bounds) if overlay else None,
            "status": "ok", "error": None, "capture_key": capture_key(path) or f"single|{path}",
            "proc_version": detector_version(meta.source),
        }
        image_id = self.store.upsert_image(record)
        self.store.set_alignment(image_id, None, None, None)       # re-solved by align_mission()
        self._link(image_id, path, image, geo, meta, footprint if overlay is not None else None)

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


    # ------------------------------------------------------------------
    # lining photos up with each other (see align.py)
    # ------------------------------------------------------------------
    def _frame(self, lat: float | None = None, lon: float | None = None) -> LocalFrame | None:
        origin = self.store.origin(lat, lon)
        return LocalFrame(*origin) if origin else None

    @staticmethod
    def _spot(frame: LocalFrame, footprint: list) -> tuple[float, float, float]:
        ne = [frame.to_ne(lat, lon) for lat, lon in footprint]
        cn, ce = sum(p[0] for p in ne) / len(ne), sum(p[1] for p in ne) / len(ne)
        return cn, ce, max(math.hypot(p[0] - cn, p[1] - ce) for p in ne)

    def _load_spots(self, frame: LocalFrame) -> dict:
        if self._spots is None:
            self._spots = {}
            for img in self.store.list_images():
                if img["status"] == "ok" and img.get("footprint") and (self.feature_dir / f"{img['id']}.npz").is_file():
                    self._spots[img["id"]] = (img["source"], *self._spot(frame, img["footprint"]))
        return self._spots

    def _features(self, image_id: int):
        f = self._feat_cache.get(image_id)
        if f is None:
            try:
                z = np.load(self.feature_dir / f"{image_id}.npz")
                f = _align.Features(z["ne"], z["desc"], z["lum"])
            except (OSError, KeyError, ValueError):
                return None
            self._feat_cache[image_id] = f
            if len(self._feat_cache) > 60:
                self._feat_cache.pop(next(iter(self._feat_cache)))
        return f

    def _link(self, image_id: int, path: Path, image, geo: Georeferencer, meta, footprint) -> None:
        """Find this photo's distinct points and match them to the photos it overlaps."""
        feat_file = self.feature_dir / f"{image_id}.npz"
        frame = self._frame(meta.lat, meta.lon)
        spots = self._load_spots(frame)
        spots.pop(image_id, None)
        self._feat_cache.pop(image_id, None)
        # Photos are matched within one lens. A thermal or zoom photo that has a wide photo from
        # the same shutter press is not matched at all: it takes that wide photo's correction.
        anchor = footprint is not None and self.cfg.align.enabled and (
            meta.source in ("wide", "unknown") or (meta.source == "thermal" and not has_wide_sibling(path)))
        feats = None
        if anchor:
            def to_ne(px, py):
                ll = geo.pixel_to_latlon(px, py)
                return None if ll is None else frame.to_ne(*ll)
            feats = _align.extract_features(image, to_ne, self.cfg.align)
        if feats is None:
            feat_file.unlink(missing_ok=True)
            self.store.save_links(image_id, [])
            return
        self.feature_dir.mkdir(parents=True, exist_ok=True)
        np.savez(feat_file, ne=feats.ne, desc=feats.desc, lum=feats.lum)
        cn, ce, rad = self._spot(frame, footprint)
        near = sorted(((math.hypot(cn - s[1], ce - s[2]), j) for j, s in spots.items()
                       if s[0] == meta.source and math.hypot(cn - s[1], ce - s[2]) < rad + s[3]))
        links = []
        for dist, j in near[:self.cfg.align.max_neighbours]:
            other = self._features(j)
            if other is None:
                continue
            link = _align.match_pair(image_id, feats, j, other, dist, self.cfg.align)
            if link is not None:
                links.append(link)
        self.store.save_links(image_id, links)
        spots[image_id] = (meta.source, cn, ce, rad)
        self._feat_cache[image_id] = feats

    def align_mission(self) -> dict:
        """Solve a correction for every photo from the stored links, re-lay the map patches
        that moved, and move each photo's detections with it. Cheap enough to run after
        every batch of new photos."""
        stats = {"photos": 0, "aligned": 0, "links": 0, "before_m": None, "after_m": None}
        frame = self._frame()
        if frame is None or not self.cfg.align.enabled:
            return stats
        images = [i for i in self.store.list_images() if i["status"] == "ok"]
        ids = {i["id"] for i in images}
        links = [l for l in self.store.all_links() if l.a in ids and l.b in ids]
        centres = {i["id"]: self._spot(frame, i["footprint"])[:2] for i in images if i.get("footprint")}
        links = [l for l in links if l.a in centres and l.b in centres]
        corr = _align.solve(centres, links, self.cfg.align) if links else {}
        by_shot = {i["capture_key"]: corr[i["id"]] for i in images
                   if i["id"] in corr and i.get("capture_key") and not i["capture_key"].startswith("single|")}
        probe = np.array([[0.0, 0.0], [60.0, 0.0], [0.0, 60.0]])
        for img in images:
            c = corr.get(img["id"])
            if c is None and img.get("capture_key") in by_shot and img["source"] != "split":
                # same instant, same gimbal: the wide photo's correction applies to its siblings,
                # but not its brightness change
                w = by_shot[img["capture_key"]]
                c = _align.Correction(w.cn, w.ce, w.ds, w.b, w.tn, w.te, 1.0, w.links)
            if img.get("overlay_png"):
                stats["photos"] += 1
            if c is None:
                if img.get("align") or img.get("aligned_png"):
                    self.store.set_alignment(img["id"], None, None, None)
                    self.store.move_candidates(img["id"], lambda lat, lon: (lat, lon))
                continue
            old = _align.Correction.from_dict(img["align"]) if img.get("align") else None
            moved = old is None or float(np.abs(old.apply(probe + (c.cn, c.ce)) - c.apply(probe + (c.cn, c.ce))).max()) > 0.15 \
                or abs(old.gain - c.gain) > 0.02
            png, bounds = img.get("aligned_png"), img.get("aligned_bounds")
            if img.get("overlay_png") and (moved or not png or not (self.workdir / png).is_file()):
                ov = apply_correction(self.workdir / img["overlay_png"], img["overlay_bounds"], c, frame,
                                      self.aligned_dir / Path(img["overlay_png"]).name)
                png, bounds = (_rel(self.workdir, ov.png_path), ov.bounds) if ov else (None, None)
            if img.get("overlay_png") and png:
                stats["aligned"] += 1
            if moved or png != img.get("aligned_png"):
                self.store.set_alignment(img["id"], c.to_dict(), png, bounds)
                self.store.move_candidates(img["id"], lambda lat, lon, c=c: frame.to_latlon(
                    *c.apply(np.array([frame.to_ne(lat, lon)]))[0]))
        self.store.conn.commit()
        self.store.regroup(self.cfg.group_radius_m)
        stats["links"] = len(links)
        if links:
            stats["before_m"] = round(_align.residual_m(links, {}), 2)
            stats["after_m"] = round(_align.residual_m(links, corr), 2)
        with self.store.conn:
            self.store.conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('align_stats', ?)", (json.dumps(stats),))
        log.info("map alignment: %s", stats)
        return stats


def _rel(workdir: Path, p: str) -> str:
    try:
        return str(Path(p).resolve().relative_to(workdir.resolve()))
    except ValueError:
        return p
