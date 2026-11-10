import json
import math
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from pointersearch.geotiff import (ExportError, crs_choices, export_options, export_ortho, pick_crs, read_georef,
                                   utm_zone, write_geotiff)
from pointersearch.review.server import create_app


# ---- coordinate systems -----------------------------------------------------------
# Reference values from PROJ (GDAL 3.12), which this code agrees with to 0.0001 mm.
PROJ = [
    ("mga", -42.961427, 147.024943, 7855, 502034.334, 5243468.354),      # Lucaston, Tasmania
    ("mga", -33.7312, 150.3105, 7856, 250815.573, 6264398.325),          # Blue Mountains
    ("mga", -12.46, 130.84, 7852, 699986.070, 8621886.607),              # Darwin
    ("mga", -10.5, 149.9, 7855, 817456.488, 8837839.902),                # far from the central meridian
]


@pytest.mark.parametrize("code,lat,lon,epsg,east,north", PROJ)
def test_projection_matches_reference_values(code, lat, lon, epsg, east, north):
    crs = pick_crs(code, lat, lon)
    x, y = crs.forward(lat, lon)
    assert crs.epsg == epsg and float(x) == pytest.approx(east, abs=0.001) and float(y) == pytest.approx(north, abs=0.001)


def test_projection_matches_proj_everywhere():
    rasterio = pytest.importorskip("rasterio")
    from rasterio.warp import transform
    for lat, lon in [(-42.96, 147.02), (-43.6, 146.0), (51.5, -0.12), (64.1, -21.9), (1.3, 103.8), (-0.5, 36.8), (35.7, 139.7)]:
        for crs in crs_choices(lat, lon):
            x, y = crs.forward(lat, lon)
            px, py = transform("EPSG:4326", f"EPSG:{crs.epsg}", [lon], [lat])
            assert math.hypot(float(x) - px[0], float(y) - py[0]) < (1e-6 if crs.projected else 1e-12), crs.label


def test_coordinate_system_choices():
    tas = crs_choices(-42.96, 147.02)
    assert [c.code for c in tas] == ["mga", "utm", "latlon"] and [c.epsg for c in tas] == [7855, 32755, 4326]
    assert tas[0].label == "GDA2020 / MGA zone 55 (EPSG:7855)"
    assert [c.epsg for c in crs_choices(51.5, -0.12)] == [32630, 4326]             # no MGA outside Australia
    assert [c.epsg for c in crs_choices(-36.85, 174.76)] == [32760, 4326]          # nor in New Zealand
    assert utm_zone(147.02) == 55 and utm_zone(-0.12) == 30 and utm_zone(179.9) == 60 and utm_zone(-180) == 1
    assert pick_crs(None, -42.96, 147.02).code == "mga" and pick_crs(None, 51.5, -0.12).code == "utm"
    with pytest.raises(ExportError, match="not available here"):
        pick_crs("mga", 51.5, -0.12)
    x, y = crs_choices(-42.96, 147.02)[2].forward(np.array([-42.96, -42.97]), np.array([147.02, 147.03]))
    assert x.tolist() == [147.02, 147.03] and y.tolist() == [-42.96, -42.97]


def test_written_file_carries_its_position_and_coordinate_system(tmp_path):
    img = np.zeros((40, 60, 4), np.uint8)
    img[..., 3] = 255
    img[:5] = 0                                                                   # a transparent strip
    crs = pick_crs("mga", -42.96, 147.02)
    f = write_geotiff(tmp_path / "a" / "t.tif", img, 502000.0, 5243500.0, 0.25, 0.25, crs, "a test")
    g = read_georef(f)
    assert g == {"width": 60, "height": 40, "pixel": (0.25, 0.25), "x_left": 502000.0, "y_top": 5243500.0,
                 "epsg": 7855, "projected": True, "bands": 4}
    with Image.open(f) as im:
        assert im.mode == "RGBA" and im.tag_v2[270] == "a test" and "MGA zone 55" in im.tag_v2[34737]
    ll = write_geotiff(tmp_path / "ll.tif", img, 147.0, -42.9, 1e-6, 1e-6, pick_crs("latlon", -42.96, 147.02))
    assert read_georef(ll)["epsg"] == 4326 and read_georef(ll)["projected"] is False

    rasterio = pytest.importorskip("rasterio")                                     # and GDAL agrees
    with rasterio.open(f) as ds:
        assert ds.crs.to_epsg() == 7855 and ds.count == 4 and (ds.width, ds.height) == (60, 40)
        assert (ds.transform.a, ds.transform.e, ds.transform.c, ds.transform.f) == (0.25, -0.25, 502000.0, 5243500.0)
        assert [c.name for c in ds.colorinterp] == ["red", "green", "blue", "alpha"]
        assert ds.read(4)[:5].max() == 0 and ds.read(4)[5:].min() == 255
    with rasterio.open(ll) as ds:
        assert ds.crs.to_epsg() == 4326 and ds.transform.c == 147.0


# ---- exporting a mission -------------------------------------------------------------
def _find(path, lat, lon, want, crs_code, radius_m=6.0):
    """Distance in metres between where a target's colour is in the file and where it truly is."""
    g = read_georef(path)
    crs = pick_crs(crs_code, lat, lon)
    x, y = (float(v) for v in crs.forward(lat, lon))
    col, row = (x - g["x_left"]) / g["pixel"][0], (g["y_top"] - y) / g["pixel"][1]
    m_x = 1.0 if crs.projected else 111320.0 * math.cos(math.radians(lat))
    m_y = 1.0 if crs.projected else 111320.0
    r = int(radius_m / (g["pixel"][0] * m_x)) + 1
    with Image.open(path) as im:
        win = np.asarray(im.crop((int(col) - r, int(row) - r, int(col) + r + 1, int(row) + r + 1))).astype(int)
    hit = want(win)
    if not hit.any():
        return None
    yy, xx = np.nonzero(hit)
    return math.hypot((xx.mean() - r) * g["pixel"][0] * m_x, (yy.mean() - r) * g["pixel"][1] * m_y)


BLUE = lambda rgb: (rgb[..., 2] > 150) & (rgb[..., 0] < 90) & (rgb[..., 3] > 0)
ORANGE = lambda rgb: (rgb[..., 0] > 200) & (rgb[..., 2] < 90) & (rgb[..., 1] > 60) & (rgb[..., 3] > 0)
HOT = lambda rgb: (rgb[..., :3].min(axis=-1) > 200) & (rgb[..., 3] > 0)


def test_options_describe_what_can_be_exported(canopy):
    o = export_options(canopy["pipeline"].store)
    assert [l["lens"] for l in o["lenses"]] == ["colour", "thermal"]
    colour, thermal = o["lenses"]
    assert colour["photos"] == thermal["photos"] > 0 and colour["missing_files"] == 0
    # detail follows the lined-up scale: the canopy photos are finer than first recorded
    assert colour["native_gsd_cm"] == pytest.approx(80 / (24 * math.hypot(1200, 900) / 43.266) * 100, rel=0.05)
    assert colour["default_gsd_cm"] >= 5 and thermal["default_gsd_cm"] >= 10
    assert [c["code"] for c in o["crs"]] == ["mga", "utm", "latlon"] and o["crs"][0]["epsg"] == 7856


@pytest.mark.parametrize("crs_code,epsg", [("mga", 7856), ("utm", 32756), ("latlon", 4326)])
def test_colour_export_puts_objects_at_their_true_coordinates(canopy, tmp_path, crs_code, epsg):
    p = canopy["pipeline"]
    info = export_ortho(p.store, canopy["wd"], p.cfg, "colour", crs_code, out_path=tmp_path / "c.tif")
    g = read_georef(info["file"])
    assert info["epsg"] == g["epsg"] == epsg and (g["width"], g["height"]) == (info["width"], info["height"])
    assert not info["coarsened"] and info["missing_files"] == 0 and info["photos"] > 0 and g["bands"] == 4
    targets = {t["name"]: t for t in canopy["manifest"]["targets"]}
    for name, colour in (("blue_jacket", BLUE), ("orange_pack", ORANGE)):
        d = _find(info["file"], targets[name]["lat"], targets[name]["lon"], colour, crs_code)
        assert d is not None and d < 2.5, (name, d)          # within the map's own accuracy
    with Image.open(info["file"]) as im:
        alpha = np.asarray(im)[..., 3]
    assert 0 < (alpha == 0).mean() < 0.6 and (alpha == 255).mean() > 0.4     # see-through outside the photos


def test_thermal_export_and_detail_setting(canopy, tmp_path):
    p = canopy["pipeline"]
    warm = next(t for t in canopy["manifest"]["targets"] if t["warm"])
    info = export_ortho(p.store, canopy["wd"], p.cfg, "thermal", "mga", out_path=tmp_path / "t.tif")
    assert info["gsd_cm"] == pytest.approx(10.0) and read_georef(info["file"])["pixel"] == pytest.approx((0.1, 0.1))
    d = _find(info["file"], warm["lat"], warm["lon"], HOT, "mga")
    assert d is not None and d < 2.5

    coarse = export_ortho(p.store, canopy["wd"], p.cfg, "thermal", "mga", 0.5, tmp_path / "t50.tif")
    assert coarse["gsd_cm"] == 50 and coarse["width"] == pytest.approx(info["width"] / 5, abs=2)
    # the same ground, whatever the pixel size
    assert coarse["extent"][0] == pytest.approx(info["extent"][0]) and coarse["extent"][3] == pytest.approx(info["extent"][3])
    # too big for the allowed size: the pixel size is raised to fit, and the result says so
    capped = export_ortho(p.store, canopy["wd"], p.cfg, "colour", "mga", 0.05, tmp_path / "cap.tif", max_pixels=250_000)
    assert capped["coarsened"] and capped["width"] * capped["height"] <= 250_000 and capped["gsd_cm"] > capped["requested_gsd_cm"]
    with pytest.raises(ExportError, match="colour or thermal"):
        export_ortho(p.store, canopy["wd"], p.cfg, "zoom")
    with pytest.raises(ExportError, match="not available here"):
        export_ortho(p.store, canopy["wd"], p.cfg, "colour", "nad83")


def test_export_without_the_photo_files_uses_the_map_patches(canopy, tmp_path):
    import shutil
    from pointersearch.store import Store
    wd = tmp_path / "moved"
    shutil.copytree(canopy["wd"], wd)
    store = Store(wd)
    store.conn.execute("UPDATE images SET path = '/card/removed/' || id || '.JPG'")       # the card is gone
    store.conn.commit()
    assert export_options(store)["lenses"][0]["missing_files"] > 0
    info = export_ortho(store, wd, canopy["pipeline"].cfg, "colour", "mga", 0.25)
    assert info["missing_files"] == info["low_detail_photos"] == info["photos"] > 0
    blue = next(t for t in canopy["manifest"]["targets"] if t["name"] == "blue_jacket")
    assert _find(info["file"], blue["lat"], blue["lon"], BLUE, "mga") < 2.5              # still in the right place
    assert info["file"].endswith("moved_colour_epsg7856_25cm.tif")


def test_nothing_to_export(tmp_path):
    from pointersearch.config import Config
    from pointersearch.store import Store
    Config().save(tmp_path)
    store = Store(tmp_path)
    assert export_options(store) == {"lenses": [], "crs": [], "photos": 0}
    with pytest.raises(ExportError, match="no colour photos"):
        export_ortho(store, tmp_path, Config(), "colour")


def test_export_from_the_review_map(canopy):
    c = TestClient(create_app(canopy["wd"]))
    assert c.get("/api/geotiff").json()["state"] == "idle"
    assert [l["lens"] for l in c.get("/api/geotiff/options").json()["lenses"]] == ["colour", "thermal"]
    assert c.post("/api/geotiff", json={"lens": "zoom"}).status_code == 400
    assert c.post("/api/geotiff", json={"lens": "thermal", "gsd_cm": 0.01}).status_code == 400

    r = c.post("/api/geotiff", json={"lens": "thermal", "crs": "utm", "gsd_cm": 20})
    assert r.status_code == 200 and r.json()["state"] == "running"
    end = time.time() + 60
    while c.get("/api/geotiff").json()["state"] == "running" and time.time() < end:
        time.sleep(0.1)
    s = c.get("/api/geotiff").json()
    assert s["state"] == "done", s
    res = s["result"]
    assert res["epsg"] == 32756 and res["gsd_cm"] == 20 and res["lens"] == "thermal"
    assert res["url"] == "files/exports/" + res["name"] and res["name"].endswith("_thermal_epsg32756_20cm.tif")
    dl = c.get("/" + res["url"])
    assert dl.status_code == 200 and dl.content[:4] in (b"II*\x00", b"MM\x00*")             # a TIFF

    # a failed request is reported, not left spinning
    c.post("/api/geotiff", json={"lens": "colour", "crs": "nad83"})
    c.app.state.geotiff.join(30)
    s = c.get("/api/geotiff").json()
    assert s["state"] == "error" and "not available here" in s["message"]


def test_gdal_reads_the_exported_map(canopy, tmp_path):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.warp import transform
    p = canopy["pipeline"]
    info = export_ortho(p.store, canopy["wd"], p.cfg, "colour", "mga", out_path=tmp_path / "g.tif")
    blue = next(t for t in canopy["manifest"]["targets"] if t["name"] == "blue_jacket")
    with rasterio.open(info["file"]) as ds:
        assert ds.crs.to_epsg() == 7856 and ds.count == 4 and ds.res == pytest.approx((info["gsd_cm"] / 100,) * 2, rel=0.01)
        x, y = transform("EPSG:4326", ds.crs, [blue["lon"]], [blue["lat"]])
        row, col = ds.index(x[0], y[0])
        r = int(3.0 / ds.res[0])
        win = np.moveaxis(ds.read(window=((row - r, row + r + 1), (col - r, col + r + 1))), 0, -1).astype(int)
        assert BLUE(win).any()                                       # the jacket is within 3 m of where GDAL looks
        assert ds.bounds.left == pytest.approx(info["extent"][0]) and ds.bounds.top == pytest.approx(info["extent"][3])
