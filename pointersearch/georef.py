"""Direct georeferencing: map image pixels to ground coordinates.

No structure-from-motion is involved. Each pixel defines a ray from the camera
position, rotated by the gimbal attitude recorded in the image metadata, and the
ray is intersected with the ground. Accuracy is a few metres at typical search
altitudes with flat terrain, which is sufficient for tasking ground teams.

Conventions (matching DJI metadata):
* gimbal yaw: degrees clockwise from true north
* gimbal pitch: degrees, 0 = horizon, -90 = straight down
* gimbal roll: degrees about the optical axis
* local frame: NED (north, east, down) metres from the camera position
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .camera import focal_px
from .config import CameraDefaults
from .metadata import ImageMeta
from .terrain import FlatGround, Ground

M_PER_DEG_LAT = 111_320.0


@dataclass
class LocalFrame:
    """Equirectangular local tangent plane centred on (lat0, lon0)."""

    lat0: float
    lon0: float

    def to_ne(self, lat: float, lon: float) -> tuple[float, float]:
        north = (lat - self.lat0) * M_PER_DEG_LAT
        east = (lon - self.lon0) * M_PER_DEG_LAT * math.cos(math.radians(self.lat0))
        return north, east

    def to_latlon(self, north: float, east: float) -> tuple[float, float]:
        lat = self.lat0 + north / M_PER_DEG_LAT
        lon = self.lon0 + east / (M_PER_DEG_LAT * math.cos(math.radians(self.lat0)))
        return lat, lon


def rotation_cam_to_ned(yaw_deg: float, pitch_deg: float, roll_deg: float) -> np.ndarray:
    """3x3 matrix taking camera-frame vectors (x right, y down, z forward) to NED."""
    y, p, r = (math.radians(a) for a in (yaw_deg, pitch_deg, roll_deg))
    # camera (x right, y down, z forward) -> body NED at zero attitude:
    # forward (z_c) = north, right (x_c) = east, down (y_c) = down
    c2b = np.array([[0.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    rx = np.array([[1, 0, 0], [0, math.cos(r), -math.sin(r)], [0, math.sin(r), math.cos(r)]])
    ry = np.array([[math.cos(p), 0, math.sin(p)], [0, 1, 0], [-math.sin(p), 0, math.cos(p)]])
    rz = np.array([[math.cos(y), -math.sin(y), 0], [math.sin(y), math.cos(y), 0], [0, 0, 1]])
    return rz @ ry @ rx @ c2b


class Georeferencer:
    def __init__(self, meta: ImageMeta, camera: CameraDefaults, ground: Ground | None = None):
        self.meta = meta
        self.f_px = focal_px(meta, camera)
        self.cx = meta.width / 2.0
        self.cy = meta.height / 2.0
        self.R = rotation_cam_to_ned(meta.gimbal_yaw, meta.gimbal_pitch, meta.gimbal_roll)
        self.frame = LocalFrame(meta.lat, meta.lon)
        self.ground = ground or FlatGround()
        self._takeoff_elev = None
        if meta.abs_alt is not None:
            self._takeoff_elev = meta.abs_alt - meta.rel_alt

    def ray_ned(self, px: float, py: float) -> np.ndarray:
        v = np.array([px - self.cx, py - self.cy, self.f_px], dtype=float)
        v /= np.linalg.norm(v)
        return self.R @ v

    def pixel_to_ne(self, px: float, py: float) -> tuple[float, float] | None:
        """Ground offset (north, east) in metres from the camera, or None if above horizon."""
        d = self.ray_ned(px, py)
        if d[2] <= 1e-6:
            return None
        h = self.meta.rel_alt
        # Terrain refinement: iterate height above actual ground under the ray.
        if self._takeoff_elev is not None and not isinstance(self.ground, FlatGround):
            cam_elev = self.meta.abs_alt
            for _ in range(3):
                t = h / d[2]
                lat, lon = self.frame.to_latlon(t * d[0], t * d[1])
                g = self.ground.elevation(lat, lon)
                if g is None:
                    break
                h = cam_elev - g
                if h <= 1.0:
                    h = 1.0
                    break
        t = h / d[2]
        return float(t * d[0]), float(t * d[1])

    def pixel_to_latlon(self, px: float, py: float) -> tuple[float, float] | None:
        ne = self.pixel_to_ne(px, py)
        return None if ne is None else self.frame.to_latlon(*ne)

    def footprint_latlon(self) -> list[tuple[float, float]] | None:
        """Corners (TL, TR, BR, BL) of the image on the ground as (lat, lon)."""
        w, h = self.meta.width, self.meta.height
        corners = [(0, 0), (w, 0), (w, h), (0, h)]
        out = []
        for px, py in corners:
            ll = self.pixel_to_latlon(px, py)
            if ll is None:
                return None
            out.append(ll)
        return out

    def ground_gsd(self, px: float | None = None, py: float | None = None) -> float:
        """Approximate metres per pixel at a pixel (defaults to image centre)."""
        px = self.cx if px is None else px
        py = self.cy if py is None else py
        a = self.pixel_to_ne(px, py)
        b = self.pixel_to_ne(px + 1, py)
        if a is None or b is None:
            return self.meta.rel_alt / self.f_px
        return math.hypot(b[0] - a[0], b[1] - a[1])
