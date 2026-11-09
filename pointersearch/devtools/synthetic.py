"""Generate a synthetic Matrice 4T-like mission for demos and tests.

Produces JPEGs with DJI-style XMP metadata (position, relative altitude, gimbal
attitude) flown in a lawnmower pattern over a textured "scrub" ground plane with
a few planted targets: a blue jacket, an orange pack, and a warm body in the
thermal frames. Geometry is rendered by projecting the ground plane through the
same camera model the pipeline uses, so detections should land back on the
planted coordinates.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from ..georef import LocalFrame, rotation_cam_to_ned

XMP_TEMPLATE = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="DJI Meta Data"
    xmlns:drone-dji="http://www.dji.com/drone-dji/1.0/"
    drone-dji:GpsLatitude="{lat:.8f}"
    drone-dji:GpsLongitude="{lon:.8f}"
    drone-dji:AbsoluteAltitude="+{abs_alt:.3f}"
    drone-dji:RelativeAltitude="+{rel_alt:.3f}"
    drone-dji:GimbalRollDegree="+{roll:.2f}"
    drone-dji:GimbalYawDegree="{yaw:+.2f}"
    drone-dji:GimbalPitchDegree="{pitch:+.2f}"
    drone-dji:FlightRollDegree="+0.00"
    drone-dji:FlightYawDegree="{yaw:+.2f}"
    drone-dji:FlightPitchDegree="+0.00"
    drone-dji:ImageSource="{source}"
    drone-dji:RtkFlag="0"/>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""


def write_jpeg_with_xmp(path: Path, bgr: np.ndarray, xmp: str, focal_35mm: float, quality: int = 90):
    """Encode BGR to JPEG and insert an APP1 XMP segment plus minimal EXIF."""
    from PIL import Image

    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    im = Image.fromarray(rgb)
    exif = Image.Exif()
    exif[0x8769] = {41989: int(round(focal_35mm)), 36867: "2025:06:01 06:30:00"}
    import io
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, exif=exif.tobytes())
    data = buf.getvalue()
    payload = b"http://ns.adobe.com/xap/1.0/\x00" + xmp.encode("utf-8")
    segment = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    # Insert after SOI (and after an existing APP1 EXIF segment if present).
    pos = 2
    if data[2:4] == b"\xff\xe1":
        pos = 4 + struct.unpack(">H", data[4:6])[0]
    path.write_bytes(data[:pos] + segment + data[pos:])


@dataclass
class Target:
    north: float
    east: float
    size_m: float
    colour_bgr: tuple[int, int, int]
    warm: bool = False
    name: str = "target"


def _ground_texture(rng: np.random.Generator, size_m: float, res_m: float) -> np.ndarray:
    n = int(size_m / res_m)
    base = rng.normal(0, 1, (n // 8 + 1, n // 8 + 1)).astype(np.float32)
    base = cv2.resize(base, (n, n), interpolation=cv2.INTER_CUBIC)
    fine = rng.normal(0, 0.35, (n, n)).astype(np.float32)
    v = cv2.GaussianBlur(base + fine, (0, 0), 1.5)
    v = (v - v.min()) / (v.max() - v.min() + 1e-6)
    # Interpolate between dry brown scrub and olive green.
    brown = np.array([70, 95, 118], np.float32)   # BGR
    green = np.array([55, 105, 70], np.float32)
    img = brown[None, None, :] * (1 - v[..., None]) + green[None, None, :] * v[..., None]
    img += rng.normal(0, 6, img.shape).astype(np.float32)
    return np.clip(img, 0, 255).astype(np.uint8)


def render_frame(ground: np.ndarray, ground_res_m: float, ground_origin_ne: tuple[float, float],
                 cam_ne: tuple[float, float], rel_alt: float, yaw: float, pitch: float,
                 width: int, height: int, focal_35mm: float, targets: list[Target],
                 thermal: bool = False) -> np.ndarray:
    """Render what the camera sees by inverse-mapping the ground plane."""
    f_px = focal_35mm * math.hypot(width, height) / 43.266
    R = rotation_cam_to_ned(yaw, pitch, 0.0)
    xs, ys = np.meshgrid(np.arange(width, dtype=np.float32), np.arange(height, dtype=np.float32))
    v = np.stack([xs - width / 2, ys - height / 2, np.full_like(xs, f_px)], axis=-1)
    d = v @ R.T.astype(np.float32)
    dz = np.where(d[..., 2] > 1e-3, d[..., 2], np.nan)
    t = rel_alt / dz
    north = cam_ne[0] + t * d[..., 0]
    east = cam_ne[1] + t * d[..., 1]
    gy = (ground_origin_ne[0] - north) / ground_res_m   # row 0 = northern edge
    gx = (east - ground_origin_ne[1]) / ground_res_m
    if thermal:
        gray = cv2.cvtColor(ground, cv2.COLOR_BGR2GRAY).astype(np.float32)
        gray = cv2.GaussianBlur(gray, (0, 0), 2) * 0.4 + 70
        src = gray
    else:
        src = ground
    frame = cv2.remap(src, gx.astype(np.float32), gy.astype(np.float32), cv2.INTER_LINEAR,
                      borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
    # Paint targets in ground coordinates -> image via nearest pixel test.
    for tgt in targets:
        inside = (np.abs(north - tgt.north) < tgt.size_m / 2) & (np.abs(east - tgt.east) < tgt.size_m / 2)
        if thermal:
            if tgt.warm:
                frame[inside] = 235
        else:
            frame[inside] = tgt.colour_bgr
    if thermal:
        frame = np.clip(frame + np.random.default_rng(0).normal(0, 2, frame.shape), 0, 255).astype(np.uint8)
        frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    return frame


def generate_mission(out_dir: Path, lat0: float = -33.7312, lon0: float = 150.3105,
                     area_n_m: float = 240.0, area_e_m: float = 320.0, rel_alt: float = 80.0,
                     overlap: float = 0.3, with_thermal: bool = True, seed: int = 7,
                     rgb_size: tuple[int, int] = (1600, 1200), thermal_size: tuple[int, int] = (640, 512),
                     ) -> dict:
    """Write a synthetic mission to ``out_dir``. Returns planted target positions."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    frame = LocalFrame(lat0, lon0)
    pad = 120.0
    size_m = max(area_n_m, area_e_m) + 2 * pad
    res_m = 0.08
    ground = _ground_texture(rng, size_m, res_m)
    origin_ne = (area_n_m / 2 + pad, -area_e_m / 2 - pad)  # NW corner of ground raster

    targets = [
        Target(north=38.0, east=-61.0, size_m=1.4, colour_bgr=(200, 60, 20), name="blue_jacket"),
        Target(north=-52.0, east=95.0, size_m=0.9, colour_bgr=(10, 110, 245), name="orange_pack"),
        Target(north=-10.0, east=20.0, size_m=0.8, colour_bgr=(70, 95, 120), warm=True, name="warm_body"),
    ]

    rgb_f35, th_f35 = 24.0, 53.0
    w, h = rgb_size
    f_px = rgb_f35 * math.hypot(w, h) / 43.266
    foot_e = rel_alt * w / f_px      # ground width of a frame (yaw 0: width spans east)
    foot_n = rel_alt * h / f_px
    step_e = foot_e * (1 - overlap)
    step_n = foot_n * (1 - overlap)
    lines = int(math.ceil(area_n_m / step_n)) + 1
    per_line = int(math.ceil(area_e_m / step_e)) + 1

    idx = 0
    manifest = {"targets": [], "images": []}
    for li in range(lines):
        n = -area_n_m / 2 + li * step_n
        yaw = 0.0 if li % 2 == 0 else 180.0  # fly east on even lines, west on odd
        for j in range(per_line):
            e = -area_e_m / 2 + j * step_e if li % 2 == 0 else area_e_m / 2 - j * step_e
            idx += 1
            lat, lon = frame.to_latlon(n, e)
            stamp = f"DJI_20250601063{idx // 60:01d}{idx % 60:02d}_{idx:04d}"
            img = render_frame(ground, res_m, origin_ne, (n, e), rel_alt, yaw, -90.0, w, h, rgb_f35, targets)
            xmp = XMP_TEMPLATE.format(lat=lat, lon=lon, abs_alt=rel_alt + 410.0, rel_alt=rel_alt, roll=0.0,
                                      yaw=yaw, pitch=-90.0, source="WideCamera")
            p = out_dir / f"{stamp}_W.JPG"
            write_jpeg_with_xmp(p, img, xmp, rgb_f35)
            manifest["images"].append(str(p))
            if with_thermal:
                tw, th_ = thermal_size
                timg = render_frame(ground, res_m, origin_ne, (n, e), rel_alt, yaw, -90.0, tw, th_, th_f35,
                                    targets, thermal=True)
                xmp = XMP_TEMPLATE.format(lat=lat, lon=lon, abs_alt=rel_alt + 410.0, rel_alt=rel_alt, roll=0.0,
                                          yaw=yaw, pitch=-90.0, source="InfraredCamera")
                tp = out_dir / f"{stamp}_T.JPG"
                write_jpeg_with_xmp(tp, timg, xmp, th_f35)
                manifest["images"].append(str(tp))

    for t in targets:
        lat, lon = frame.to_latlon(t.north, t.east)
        manifest["targets"].append({"name": t.name, "lat": lat, "lon": lon, "warm": t.warm, "size_m": t.size_m})
    return manifest
