"""Export the lined-up map as a GeoTIFF: a picture that GIS software places correctly.

The export is rendered from the original photos (not from the low-detail patches used
on the review map), each laid at its lined-up position and blended towards its middle,
on a north-up grid of square pixels in the chosen coordinate system.

Coordinate systems offered:

* **UTM on WGS 84** (EPSG:326xx / 327xx) - metres, valid anywhere.
* **MGA on GDA2020** (EPSG:78xx) - the Australian standard grid, metres.
* **Latitude / longitude on WGS 84** (EPSG:4326) - degrees.

Lenses: colour (wide), thermal, zoom, and colour with the zoom photos laid on top.

Positions come from the aircraft's GPS, which reports WGS 84. They are written into
the MGA grid without a datum shift: in 2026 the two differ by about half a metre
(continental drift since 2020), which is inside the accuracy of this map.

No GDAL is needed: the georeferencing tags are written directly with Pillow.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, TiffImagePlugin

from .align import Correction
from .config import Config
from .geo import LocalFrame
from .mosaic import edge_fade

# What each export is made of, bottom layer first. Photos within a layer are blended with
# each other; a later layer is laid over the earlier one wherever it has photos.
WIDE = ("wide", "unknown")
LENS_LAYERS = {"colour": [WIDE], "thermal": [("thermal",)], "zoom": [("zoom",)], "colour+zoom": [WIDE, ("zoom",)]}
LENS_LABEL = {"colour": "Colour", "thermal": "Thermal", "zoom": "Zoom", "colour+zoom": "Colour with zoom on top"}
DEFAULT_GSD_M = {"colour": 0.05, "thermal": 0.10, "zoom": 0.02, "colour+zoom": 0.05}
LENS_SOURCES = {name: tuple(s for layer in layers for s in layer) for name, layers in LENS_LAYERS.items()}


def _native_gsd(images: list[dict], fallback: float) -> float:
    """Typical ground size of a pixel in these photos, at their lined-up scale."""
    vals = [i["gsd_m"] * (Correction.from_dict(i["align"]).scale if i.get("align") else 1.0)
            for i in images if i.get("gsd_m")]
    return float(np.median(vals)) if vals else fallback
MAX_PIXELS = 64_000_000


class ExportError(ValueError):
    """Raised when there is nothing to export or the request cannot be met."""


# ---------------------------------------------------------------------------
# Coordinate systems
# ---------------------------------------------------------------------------
_ELLIPSOID = {"WGS84": (6378137.0, 1 / 298.257223563), "GDA2020": (6378137.0, 1 / 298.257222101)}   # GRS80


@dataclass(frozen=True)
class Crs:
    code: str                 # utm | mga | latlon
    epsg: int
    name: str
    projected: bool
    zone: int = 0
    south: bool = False
    datum: str = "WGS84"

    def forward(self, lat, lon):
        """(lat, lon) in degrees -> (x east, y north) in this system's units. Accepts arrays."""
        lat, lon = np.asarray(lat, dtype=np.float64), np.asarray(lon, dtype=np.float64)
        if not self.projected:
            return lon, lat
        a, f = _ELLIPSOID[self.datum]
        return transverse_mercator(lat, lon, self.zone * 6 - 183, a, f, false_northing=1e7 if self.south else 0.0)

    @property
    def label(self) -> str:
        return f"{self.name} (EPSG:{self.epsg})"


def utm_zone(lon: float) -> int:
    return int(math.floor((lon + 180.0) / 6.0)) % 60 + 1


def crs_choices(lat: float, lon: float) -> list[Crs]:
    """The coordinate systems that make sense for a place, most useful first."""
    zone, south = utm_zone(lon), lat < 0
    out = [Crs("utm", (32700 if south else 32600) + zone, f"WGS 84 / UTM zone {zone}{'S' if south else 'N'}",
               True, zone, south, "WGS84")]
    if south and 46 <= zone <= 59 and -60 < lat < -8 and 93 < lon < 174:        # Australia and its territories
        out.insert(0, Crs("mga", 7800 + zone, f"GDA2020 / MGA zone {zone}", True, zone, True, "GDA2020"))
    out.append(Crs("latlon", 4326, "WGS 84 latitude / longitude", False))
    return out


def pick_crs(code: str | None, lat: float, lon: float) -> Crs:
    choices = crs_choices(lat, lon)
    if not code:
        return choices[0]
    for c in choices:
        if c.code == code:
            return c
    raise ExportError(f"coordinate system '{code}' is not available here; choose from "
                      + ", ".join(c.code for c in choices))


def transverse_mercator(lat, lon, lon0: float, a: float, f: float, k0: float = 0.9996,
                        false_easting: float = 500000.0, false_northing: float = 0.0):
    """Forward transverse Mercator by the Krüger series (accurate to well under a millimetre
    inside a UTM zone)."""
    n = f / (2 - f)
    A = a / (1 + n) * (1 + n ** 2 / 4 + n ** 4 / 64 + n ** 6 / 256)
    alpha = (n / 2 - 2 * n ** 2 / 3 + 5 * n ** 3 / 16 + 41 * n ** 4 / 180,
             13 * n ** 2 / 48 - 3 * n ** 3 / 5 + 557 * n ** 4 / 1440,
             61 * n ** 3 / 240 - 103 * n ** 4 / 140,
             49561 * n ** 4 / 161280)
    phi, lam = np.radians(lat), np.radians(np.asarray(lon) - lon0)
    e = math.sqrt(f * (2 - f))
    s = np.sin(phi)
    t = np.sinh(np.arctanh(s) - e * np.arctanh(e * s))
    xi = np.arctan2(t, np.cos(lam))
    eta = np.arctanh(np.sin(lam) / np.sqrt(1 + t * t))
    x, y = eta.copy(), xi.copy()
    for j, al in enumerate(alpha, start=1):
        x = x + al * np.cos(2 * j * xi) * np.sinh(2 * j * eta)
        y = y + al * np.sin(2 * j * xi) * np.cosh(2 * j * eta)
    return false_easting + k0 * A * x, false_northing + k0 * A * y


# ---------------------------------------------------------------------------
# Writing the file
# ---------------------------------------------------------------------------
def _geokeys(crs: Crs) -> tuple[tuple[int, ...], str]:
    citation = crs.name + "|"
    keys = [(1024, 0, 1, 1 if crs.projected else 2),          # model: projected / geographic
            (1025, 0, 1, 1),                                   # a pixel is an area
            (1026, 34737, len(citation), 0)]                   # name, for people
    if crs.projected:
        keys += [(3072, 0, 1, crs.epsg), (3076, 0, 1, 9001)]   # projected system, metres
    else:
        keys += [(2048, 0, 1, crs.epsg), (2054, 0, 1, 9102)]   # geographic system, degrees
    keys.sort()
    flat = (1, 1, 0, len(keys)) + tuple(v for k in keys for v in k)
    return flat, citation


def write_geotiff(path: str | Path, rgba: np.ndarray, x_left: float, y_top: float,
                  pixel_x: float, pixel_y: float, crs: Crs, description: str = "") -> Path:
    """Write an RGBA picture (uint8, north up) whose top-left corner is at (x_left, y_top)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    keys, citation = _geokeys(crs)
    ifd = TiffImagePlugin.ImageFileDirectory_v2()
    ifd[33550] = (float(pixel_x), float(pixel_y), 0.0); ifd.tagtype[33550] = 12        # pixel size
    ifd[33922] = (0.0, 0.0, 0.0, float(x_left), float(y_top), 0.0); ifd.tagtype[33922] = 12   # tie point
    ifd[34735] = keys; ifd.tagtype[34735] = 3
    ifd[34737] = citation; ifd.tagtype[34737] = 2
    ifd[305] = "pointersearch"; ifd.tagtype[305] = 2
    if description:
        ifd[270] = description; ifd.tagtype[270] = 2
    Image.fromarray(rgba, "RGBA").save(path, format="TIFF", compression="tiff_lzw", tiffinfo=ifd)
    return path


def read_georef(path: str | Path) -> dict:
    """The georeferencing written by :func:`write_geotiff`, read back without GDAL."""
    with Image.open(path) as im:
        tags = im.tag_v2
        scale, tie, keys = tags[33550], tags[33922], tags[34735]
        entries = {keys[i]: keys[i + 3] for i in range(4, len(keys), 4)}
        return {"width": im.width, "height": im.height, "pixel": (scale[0], scale[1]),
                "x_left": tie[3], "y_top": tie[4], "epsg": entries.get(3072) or entries.get(2048),
                "projected": entries.get(1024) == 1, "bands": len(im.getbands())}


# ---------------------------------------------------------------------------
# Rendering the map
# ---------------------------------------------------------------------------
def _corners_latlon(img: dict, frame: LocalFrame | None) -> np.ndarray | None:
    """Where the photo's four corners (TL, TR, BR, BL) fall on the ground, lined up if it was."""
    fp = img.get("footprint")
    if not fp:
        return None
    if frame is not None and img.get("align"):
        c = Correction.from_dict(img["align"])
        ne = c.apply([frame.to_ne(lat, lon) for lat, lon in fp])
        return np.array([frame.to_latlon(n, e) for n, e in ne])
    return np.array(fp, dtype=np.float64)


def export_options(store) -> dict:
    """What can be exported from a mission, for the export dialog."""
    images = [i for i in store.list_images() if i["status"] == "ok" and i.get("overlay_png") and i.get("footprint")]
    if not images:
        return {"lenses": [], "crs": [], "photos": 0}
    lat = float(np.mean([i["lat"] for i in images])); lon = float(np.mean([i["lon"] for i in images]))
    lenses = []
    for name, layers in LENS_LAYERS.items():
        per_layer = [[i for i in images if i["source"] in layer] for layer in layers]
        if not all(per_layer):                               # every layer needs photos
            continue
        mine = [i for layer in per_layer for i in layer]
        base = _native_gsd(per_layer[0], DEFAULT_GSD_M[name])            # the bottom layer sets the default detail
        finest = min(_native_gsd(layer, DEFAULT_GSD_M[name]) for layer in per_layer)
        lenses.append({"lens": name, "label": LENS_LABEL[name], "photos": len(mine),
                       "native_gsd_cm": round(finest * 100, 1),
                       "default_gsd_cm": round(max(DEFAULT_GSD_M[name], base) * 100, 1),
                       "missing_files": sum(1 for i in mine if not Path(i["path"]).is_file())})
    return {"lenses": lenses, "photos": len(images),
            "crs": [{"code": c.code, "label": c.label, "epsg": c.epsg} for c in crs_choices(lat, lon)]}


def export_ortho(store, workdir: Path, cfg: Config, lens: str = "colour", crs_code: str | None = None,
                 gsd_m: float | None = None, out_path: Path | None = None, progress=None,
                 max_pixels: int = MAX_PIXELS) -> dict:
    """Render one lens of the lined-up map to a GeoTIFF. Returns a description of what was written.

    ``gsd_m`` is the ground size of a pixel in metres (ignored for latitude/longitude,
    where it sets the pixel size at the middle of the map). If the area is too large for
    ``max_pixels`` the pixel size is increased to fit, and the result says so.
    """
    if lens not in LENS_LAYERS:
        raise ExportError("lens must be one of " + ", ".join(LENS_LAYERS))
    workdir = Path(workdir)
    origin = store.origin()
    frame = LocalFrame(*origin) if origin else None
    layers = LENS_LAYERS[lens]
    photos = []                                          # (photo, corners, layer), bottom layer first
    for depth, sources in enumerate(layers):
        found = 0
        for img in store.list_images():                  # capture order
            if img["status"] != "ok" or img["source"] not in sources or not img.get("overlay_png"):
                continue
            corners = _corners_latlon(img, frame)
            if corners is not None:
                photos.append((img, corners, depth))
                found += 1
        if not found:
            what = "zoom" if sources == ("zoom",) else "thermal" if sources == ("thermal",) else "colour"
            raise ExportError(f"there are no {what} photos on the map to export")

    all_ll = np.vstack([c for _, c, _ in photos])
    crs = pick_crs(crs_code, float(all_ll[:, 0].mean()), float(all_ll[:, 1].mean()))
    base_native = _native_gsd([i for i, _, d in photos if d == 0], DEFAULT_GSD_M[lens])
    finest = min(_native_gsd([i for i, _, d in photos if d == k], DEFAULT_GSD_M[lens]) for k in range(len(layers)))
    want = float(gsd_m) if gsd_m else max(DEFAULT_GSD_M[lens], base_native)
    if want <= 0:
        raise ExportError("pixel size must be positive")
    want = max(want, finest * 0.5)                         # no point inventing detail far beyond the photos

    xs, ys = crs.forward(all_ll[:, 0], all_ll[:, 1])
    x_min, x_max, y_min, y_max = float(xs.min()), float(xs.max()), float(ys.min()), float(ys.max())
    lat_mid = float(all_ll[:, 0].mean())
    m_per_x = 1.0 if crs.projected else 111_320.0 * math.cos(math.radians(lat_mid))     # metres per unit of x
    m_per_y = 1.0 if crs.projected else 111_320.0
    used = want
    while True:
        px, py = used / m_per_x, used / m_per_y
        W, H = int(math.ceil((x_max - x_min) / px)) + 1, int(math.ceil((y_max - y_min) / py)) + 1
        if W * H <= max_pixels:
            break
        used *= math.sqrt(W * H / max_pixels) * 1.01
    canvas = np.zeros((H, W, 3), np.uint8)
    wsum = np.zeros((H, W), np.float32)

    def to_px(ll: np.ndarray) -> np.ndarray:               # (lat, lon) -> canvas pixel (x right, y down)
        x, y = crs.forward(ll[:, 0], ll[:, 1])
        return np.column_stack([(x - x_min) / px, (y_max - y) / py]).astype(np.float32)

    missing = low_detail = 0
    for n, (img, corners, depth) in enumerate(photos):
        if progress:
            progress(n, len(photos), Path(img["path"]).name)
        dst = to_px(corners)
        src_img = cv2.imread(img["path"], cv2.IMREAD_COLOR) if Path(img["path"]).is_file() else None
        if src_img is not None:
            h, w = src_img.shape[:2]
            # shrink first with proper averaging, so the warp never skips over source pixels
            side = max(np.hypot(*(dst[1] - dst[0])), np.hypot(*(dst[2] - dst[1])), 1.0)
            k = min(1.0, 1.25 * side / max(w, h))
            if k < 1.0:
                src_img = cv2.resize(src_img, (max(2, int(w * k)), max(2, int(h * k))), interpolation=cv2.INTER_AREA)
            h, w = src_img.shape[:2]
            alpha = edge_fade(h, w, cfg.align.feather if cfg.align.enabled else 0.0)
            gain = Correction.from_dict(img["align"]).gain if img.get("align") else 1.0
            if abs(gain - 1.0) > 0.01:
                src_img = np.clip(src_img.astype(np.float32) * gain, 0, 255).astype(np.uint8)
            src_pts = np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float32)
        else:
            # the photo file is not reachable: fall back to its low-detail map patch
            missing += 1
            patch_rel = img.get("aligned_png") or img.get("overlay_png")
            bounds = img.get("aligned_bounds") if img.get("aligned_png") else img.get("overlay_bounds")
            patch = cv2.imread(str(workdir / patch_rel), cv2.IMREAD_UNCHANGED) if patch_rel else None
            if patch is None or patch.ndim != 3 or patch.shape[2] != 4 or not bounds:
                continue
            low_detail += 1
            src_img, alpha = patch[:, :, :3], patch[:, :, 3]
            h, w = alpha.shape
            (s, wst), (nth, est) = bounds
            dst = to_px(np.array([[nth, wst], [nth, est], [s, est], [s, wst]]))
            src_pts = np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float32)

        x0, y0 = np.floor(dst.min(axis=0)).astype(int)
        x1, y1 = np.ceil(dst.max(axis=0)).astype(int) + 1
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
        if x1 <= x0 or y1 <= y0:
            continue
        M = cv2.getPerspectiveTransform(src_pts, (dst - (x0, y0)).astype(np.float32))
        size = (x1 - x0, y1 - y0)
        tile = cv2.warpPerspective(src_img, M, size, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
        a = cv2.warpPerspective(alpha, M, size, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
        a = a.astype(np.float32) / 255.0
        region, ws = canvas[y0:y1, x0:x1], wsum[y0:y1, x0:x1]
        if depth == 0:
            # bottom layer: photos share the ground between them; the one whose middle is nearest dominates
            wgt = a ** 3 + 1e-4 * (a > 0)
            total = ws + wgt
            hit = total > 0
            blend = (region.astype(np.float32) * ws[:, :, None] + tile.astype(np.float32) * wgt[:, :, None])
            region[hit] = np.clip(blend[hit] / total[hit][:, None], 0, 255).astype(np.uint8)
            ws[:] = total
        else:
            # a layer on top (zoom over colour): laid over whatever is beneath, fading in at its edges
            hit = a > 0
            over = tile.astype(np.float32) * a[:, :, None] + region.astype(np.float32) * (1.0 - a[:, :, None])
            bare = hit & (ws <= 0)                         # nothing beneath: show the photo itself
            over[bare] = tile[bare]
            region[hit] = np.clip(over[hit], 0, 255).astype(np.uint8)
            ws[hit] = np.maximum(ws[hit], a[hit])
    if progress:
        progress(len(photos), len(photos), "writing the file")
    if not (wsum > 0).any():
        raise ExportError("none of the photos or their map patches could be read")

    rgba = np.dstack([canvas[:, :, ::-1], np.where(wsum > 0, 255, 0).astype(np.uint8)])
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in workdir.name)
    out_path = Path(out_path) if out_path else workdir / "exports" / \
        f"{safe}_{lens.replace('+', '-')}_epsg{crs.epsg}_{used * 100:.0f}cm.tif"
    description = (f"pointersearch {LENS_LABEL[lens].lower()} map of '{workdir.name}', {len(photos)} photos, about {used * 100:.1f} cm per pixel. "
                   "Photos placed from aircraft GPS and lined up with each other; not a surveyed orthophoto.")
    write_geotiff(out_path, rgba, x_min, y_max, px, py, crs, description)
    return {"file": str(out_path), "name": out_path.name, "lens": lens, "crs": crs.label, "epsg": crs.epsg,
            "width": W, "height": H, "gsd_cm": round(used * 100, 1), "requested_gsd_cm": round(want * 100, 1),
            "coarsened": used > want * 1.02, "photos": len(photos), "missing_files": missing,
            "layers": [sum(1 for _, _, d in photos if d == k) for k in range(len(layers))],
            "low_detail_photos": low_detail, "size_mb": round(out_path.stat().st_size / 1e6, 1),
            "extent": [x_min, y_min, x_min + W * px, y_max]}


# ---------------------------------------------------------------------------
# Running an export in the background, for the app
# ---------------------------------------------------------------------------
class ExportJob:
    """One export at a time per mission, with progress the review map can show."""

    def __init__(self, workdir: str | Path):
        import threading
        self.workdir = Path(workdir)
        self._lock = threading.Lock()
        self._thread = None
        self._s = {"state": "idle", "done": 0, "total": 0, "current": None, "message": None, "result": None}

    def status(self) -> dict:
        with self._lock:
            return dict(self._s)

    def start(self, lens: str, crs_code: str | None, gsd_m: float | None) -> dict:
        import threading
        if lens not in LENS_LAYERS:
            raise ExportError("lens must be one of " + ", ".join(LENS_LAYERS))
        if gsd_m is not None and not (0.005 <= gsd_m <= 5.0):
            raise ExportError("pixel size must be between 0.5 cm and 5 m")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise ExportError("an export is already running for this mission")
            self._s = {"state": "running", "done": 0, "total": 0, "current": None, "message": None, "result": None}
            self._thread = threading.Thread(target=self._run, args=(lens, crs_code, gsd_m), daemon=True,
                                            name=f"export-{self.workdir.name}")
            self._thread.start()
        return self.status()

    def join(self, timeout: float | None = None):
        if self._thread is not None:
            self._thread.join(timeout)

    def _set(self, **kw):
        with self._lock:
            self._s.update(kw)

    def _run(self, lens, crs_code, gsd_m):
        from .store import Store
        store = None
        try:
            store = Store(self.workdir)
            cfg = Config.load(self.workdir)
            info = export_ortho(store, self.workdir, cfg, lens, crs_code, gsd_m,
                                progress=lambda d, t, name: self._set(done=d, total=t, current=name))
            info["url"] = "files/exports/" + info["name"]
            self._set(state="done", current=None, result=info)
        except ExportError as exc:
            self._set(state="error", current=None, message=str(exc))
        except MemoryError:
            self._set(state="error", current=None, message="not enough memory for a picture this large; choose a larger pixel size")
        except Exception as exc:                                   # never leave the dialog spinning
            self._set(state="error", current=None, message=str(exc) or exc.__class__.__name__)
        finally:
            if store is not None:
                store.close()
