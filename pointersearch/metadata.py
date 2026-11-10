"""Read position and attitude metadata from DJI JPEGs.

DJI aircraft write two things we need into every still:

* standard EXIF (GPS, focal length, timestamp, pixel size), and
* an XMP packet with ``drone-dji:`` fields: relative altitude above take-off,
  gimbal yaw/pitch/roll, flight attitude, laser rangefinder target, image source.

The XMP packet is parsed with a regular expression on the raw file bytes, which is
robust across firmware versions and avoids an XML dependency.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image

_XMP_START = b"<x:xmpmeta"
_XMP_END = b"</x:xmpmeta>"
_DJI_ATTR = re.compile(rb'drone-dji:([A-Za-z0-9_]+)="([^"]*)"')
_DJI_ELEM = re.compile(rb"<drone-dji:([A-Za-z0-9_]+)>([^<]*)</drone-dji:")

# EXIF tag ids
_TAG_FOCAL = 37386
_TAG_FOCAL_35 = 41989
_TAG_DATETIME_ORIGINAL = 36867
_TAG_DATETIME = 306
_IFD_EXIF = 0x8769
_IFD_GPS = 0x8825

# Filename suffix convention used by DJI aircraft:
# DJI_20250101120000_0001_W.JPG  -> W wide, Z zoom/tele, T thermal, V visible (enterprise)
# DJI_20250101120000_0001_D.JPG  -> D default camera (Mini / Air / Mavic consumer lines)
_SUFFIX_SOURCE = {"W": "wide", "V": "wide", "D": "wide", "Z": "zoom", "T": "thermal", "S": "screen"}


class MetadataError(ValueError):
    """Raised when an image lacks the metadata needed to georeference it."""


@dataclass
class ImageMeta:
    path: str
    width: int
    height: int
    lat: float
    lon: float
    rel_alt: float                      # metres above take-off point
    abs_alt: Optional[float] = None     # metres above sea level (EGM96), if present
    gimbal_yaw: float = 0.0             # degrees clockwise from true north
    gimbal_pitch: float = -90.0         # degrees, -90 = straight down
    gimbal_roll: float = 0.0
    focal_mm: Optional[float] = None
    focal_35mm: Optional[float] = None
    source: str = "unknown"             # wide | zoom | tele | thermal | unknown
    captured_at: Optional[str] = None   # ISO 8601
    lrf_distance: Optional[float] = None
    rtk: bool = False
    product: Optional[str] = None       # drone-dji:ProductName, e.g. "M4T", "Mini4 Pro"
    dji: dict = field(default_factory=dict)

    @property
    def is_thermal(self) -> bool:
        return self.source == "thermal"

    def to_dict(self) -> dict:
        return asdict(self)


def read_xmp_dji(data: bytes) -> dict[str, str]:
    """Return all ``drone-dji:`` key/value pairs found in the XMP packet."""
    start = data.find(_XMP_START)
    if start < 0:
        return {}
    end = data.find(_XMP_END, start)
    packet = data[start : end + len(_XMP_END)] if end > 0 else data[start:]
    out: dict[str, str] = {}
    for key, value in _DJI_ATTR.findall(packet):
        out[key.decode()] = value.decode(errors="replace").strip()
    for key, value in _DJI_ELEM.findall(packet):
        out.setdefault(key.decode(), value.decode(errors="replace").strip())
    return out


def _to_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).replace("+", "", 1)) if str(value).startswith("+") else float(value)
    except (TypeError, ValueError):
        return None


def _dms_to_deg(dms, ref) -> Optional[float]:
    try:
        d, m, s = (float(x) for x in dms)
    except (TypeError, ValueError):
        return None
    deg = d + m / 60.0 + s / 3600.0
    if ref in ("S", "W"):
        deg = -deg
    return deg


def _source_from_name(path: Path, dji: dict) -> str:
    src = (dji.get("ImageSource") or "").lower()
    if "infrared" in src or "thermal" in src or "ir" == src:
        return "thermal"
    if "zoom" in src or "tele" in src:
        return "zoom"
    if "wide" in src or "visible" in src:
        return "wide"
    stem = path.stem.upper()
    m = re.search(r"_([WVDZTS])$", stem)
    if m:
        return _SUFFIX_SOURCE.get(m.group(1), "unknown")
    return "unknown"


def read_image_meta(path: str | Path) -> ImageMeta:
    """Read an ImageMeta from a DJI JPEG. Raises MetadataError if ungeoreferenceable."""
    path = Path(path)
    with open(path, "rb") as fh:                       # the XMP packet sits near the start of the file
        data = fh.read(2 * 1024 * 1024)
    dji = read_xmp_dji(data)
    if not dji and path.stat().st_size > len(data):
        dji = read_xmp_dji(path.read_bytes())

    with Image.open(path) as im:
        width, height = im.size
        exif = im.getexif()
        exif_ifd = exif.get_ifd(_IFD_EXIF) if exif else {}
        gps_ifd = exif.get_ifd(_IFD_GPS) if exif else {}

    # Position: prefer XMP (decimal, full precision), fall back to EXIF GPS.
    lat = _to_float(dji.get("GpsLatitude"))
    lon = _to_float(dji.get("GpsLongitude") or dji.get("GpsLongtitude"))  # DJI typo on old fw
    if lat is None or lon is None:
        lat = _dms_to_deg(gps_ifd.get(2), gps_ifd.get(1)) if gps_ifd else None
        lon = _dms_to_deg(gps_ifd.get(4), gps_ifd.get(3)) if gps_ifd else None
    if lat is None or lon is None:
        raise MetadataError(f"{path.name}: no GPS position in XMP or EXIF")

    rel_alt = _to_float(dji.get("RelativeAltitude"))
    abs_alt = _to_float(dji.get("AbsoluteAltitude"))
    if rel_alt is None:
        raise MetadataError(f"{path.name}: no drone-dji:RelativeAltitude")

    focal_mm = _to_float(exif_ifd.get(_TAG_FOCAL)) if exif_ifd else None
    focal_35 = _to_float(exif_ifd.get(_TAG_FOCAL_35)) if exif_ifd else None

    captured = None
    raw_dt = (exif_ifd.get(_TAG_DATETIME_ORIGINAL) if exif_ifd else None) or exif.get(_TAG_DATETIME)
    if raw_dt:
        try:
            captured = datetime.strptime(str(raw_dt), "%Y:%m:%d %H:%M:%S").isoformat()
        except ValueError:
            captured = str(raw_dt)

    return ImageMeta(
        path=str(path),
        width=width,
        height=height,
        lat=lat,
        lon=lon,
        rel_alt=rel_alt,
        abs_alt=abs_alt,
        gimbal_yaw=_to_float(dji.get("GimbalYawDegree")) or 0.0,
        gimbal_pitch=_to_float(dji.get("GimbalPitchDegree")) if dji.get("GimbalPitchDegree") is not None else -90.0,
        gimbal_roll=_to_float(dji.get("GimbalRollDegree")) or 0.0,
        focal_mm=focal_mm,
        focal_35mm=focal_35,
        source=_source_from_name(path, dji),
        captured_at=captured,
        lrf_distance=_to_float(dji.get("LRFTargetDistance")),
        rtk=str(dji.get("RtkFlag", "0")).strip() not in ("", "0"),
        product=dji.get("ProductName") or None,
        dji=dji,
    )
