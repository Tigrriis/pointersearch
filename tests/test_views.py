import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from pointersearch.capture import capture_key, same_moment
from pointersearch.config import Config
from pointersearch.devtools.synthetic import XMP_TEMPLATE, Target, _ground_texture, render_frame, write_jpeg_with_xmp
from pointersearch.geo import LocalFrame
from pointersearch.metadata import ImageMeta
from pointersearch.pipeline import Pipeline
from pointersearch.review.server import create_app
from pointersearch.store import Store
from pointersearch.views import map_pixel, views_for_candidate, views_for_group

LENSES = {"W": ("WideCamera", 24.0, (1600, 1200), False), "Z": ("ZoomCamera", 70.0, (1600, 1200), False),
          "T": ("InfraredCamera", 53.0, (640, 512), True)}
FRAME = LocalFrame(-33.7312, 150.3105)
# an orange, warm pack so every lens (and the thermal detector) can see it
PACK = Target(north=0.0, east=0.0, size_m=1.0, colour_bgr=(10, 110, 245), warm=True, name="pack")


def shoot(folder, seq, cam_ne, lenses="WZT", stamp="20250601063000", yaw=0.0, alt=80.0):
    """Write the files of one shutter press: same sequence number, one file per lens."""
    folder.mkdir(parents=True, exist_ok=True)
    ground = _ground_texture(np.random.default_rng(3), 300.0, 0.1)
    lat, lon = FRAME.to_latlon(*cam_ne)
    paths = {}
    for code in lenses:
        source, f35, (w, h), thermal = LENSES[code]
        img = render_frame(ground, 0.1, (150.0, -150.0), cam_ne, alt, yaw, -90.0, w, h, f35, [PACK], thermal=thermal)
        xmp = XMP_TEMPLATE.format(lat=lat, lon=lon, abs_alt=alt + 400, rel_alt=alt, roll=0.0, yaw=yaw, pitch=-90.0, source=source)
        paths[code] = folder / f"DJI_{stamp}_{seq:04d}_{code}.JPG"
        write_jpeg_with_xmp(paths[code], img, xmp, f35)
    return paths


def meta(source, f35, w, h, yaw=0.0, pitch=-90.0):
    return ImageMeta(path="x.jpg", width=w, height=h, lat=-33.7, lon=150.3, rel_alt=80.0, gimbal_yaw=yaw,
                     gimbal_pitch=pitch, gimbal_roll=0.0, focal_35mm=f35, source=source)


# ---- which files belong together ------------------------------------------------
def test_capture_key_groups_lens_files_of_one_shutter_press():
    a = capture_key("/card/DCIM/DJI_20250101120000_0007_W.JPG")
    assert a == capture_key("/card/DCIM/DJI_20250101120001_0007_T.JPG") == capture_key("/card/DCIM/dji_20250101120000_0007_z.jpg")
    assert a != capture_key("/card/DCIM/DJI_20250101120000_0008_W.JPG")          # next shot
    assert a != capture_key("/card/OTHER/DJI_20250101120000_0007_W.JPG")         # another folder
    assert capture_key("/x/IMG_1234.JPG") is None and capture_key("/x/site_T.JPG") == capture_key("/x/SITE_W.JPG")
    assert same_moment("DJI_20250101120000_0007_W.JPG", "DJI_20250101120002_0007_T.JPG")
    assert not same_moment("DJI_20250101120000_0007_W.JPG", "DJI_20250102093000_0007_T.JPG")   # sequence reused next day


# ---- carrying a pixel from one lens to another -----------------------------------
def test_map_pixel_between_lenses():
    cfg = Config()
    wide, zoom, thermal = meta("wide", 24, 8064, 6048), meta("zoom", 70, 8064, 6048), meta("thermal", 53, 640, 512)
    assert map_pixel(wide, zoom, 4032, 3024, cfg) == pytest.approx((4032, 3024))          # centre stays centre
    assert map_pixel(wide, thermal, 4032, 3024, cfg) == pytest.approx((320, 256))
    # 100 px right of centre in wide is 100 * 70/24 px right of centre in zoom
    x, y = map_pixel(wide, zoom, 4132, 3024, cfg)
    assert x - 4032 == pytest.approx(100 * 70 / 24, rel=1e-6) and y == pytest.approx(3024)
    # and back again
    assert map_pixel(zoom, wide, x, y, cfg) == pytest.approx((4132, 3024))
    # the mapping is the same whichever way the aircraft is facing
    wide_e, th_e = meta("wide", 24, 8064, 6048, yaw=90), meta("thermal", 53, 640, 512, yaw=90)
    assert map_pixel(wide_e, th_e, 4500, 2800, cfg) == pytest.approx(map_pixel(wide, thermal, 4500, 2800, cfg))
    # a lens pointing the other way cannot see it
    assert map_pixel(meta("wide", 24, 8064, 6048, pitch=0), meta("zoom", 70, 8064, 6048, yaw=180, pitch=0), 4032, 3024, cfg) is None
    # alignment offsets shift the result by focal length x tan(angle)
    cfg.lens_align_deg["thermal"] = [1.0, -0.5]
    fx = 53 * np.hypot(640, 512) / 43.266
    assert map_pixel(wide, thermal, 4032, 3024, cfg) == pytest.approx(
        (320 + fx * np.tan(np.radians(1.0)), 256 - fx * np.tan(np.radians(0.5))))


# ---- end to end -----------------------------------------------------------------
@pytest.fixture(scope="module")
def flown(tmp_path_factory):
    root = tmp_path_factory.mktemp("lenses")
    img = root / "DCIM"
    near = shoot(img, 1, (4.0, -6.0))                     # pack 7 m off centre: inside every lens
    far = shoot(img, 2, (20.0, 34.0))                     # 39 m off: wide only
    lone = shoot(img, 3, (-5.0, 3.0), lenses="W", stamp="20250601063100")
    wd = root / "mission"
    wd.mkdir()
    Config().save(wd)
    p = Pipeline(wd)
    assert p.process_path(img) == 7
    return {"wd": wd, "store": p.store, "near": near, "far": far, "lone": lone, "cfg": p.cfg}


def best_in(store, path, detector=None):
    cands = [c for c in store.list_candidates() if c["image_path"] == str(path.resolve())
             and (detector is None or c["detector"] == detector)]
    assert cands, f"no candidates in {path.name}"
    return max(cands, key=lambda c: c["score"])


def test_all_three_lenses_when_object_is_in_every_frame(flown):
    store, cfg = flown["store"], flown["cfg"]
    cand = best_in(store, flown["near"]["W"])
    views = views_for_candidate(store, cfg, cand)
    assert [v["lens"] for v in views] == ["wide", "zoom", "thermal"] and all(v["available"] for v in views)
    assert [v["primary"] for v in views] == [True, False, False]
    assert views[0]["url"] == "files/" + cand["thumb"]
    assert [v["image_name"][-5] for v in views] == ["W", "Z", "T"]

    # the mapped point really is the pack in each lens: orange in zoom, hot in thermal
    zoom = cv2.imread(str(flown["near"]["Z"]))
    b, g, r = (int(v) for v in zoom[int(views[1]["py"]), int(views[1]["px"])])
    assert r > 200 and b < 80
    thermal = cv2.imread(str(flown["near"]["T"]), cv2.IMREAD_GRAYSCALE)
    assert thermal[int(views[2]["py"]), int(views[2]["px"])] > 200

    # found in thermal instead: thermal is primary and the others are derived from it
    tcand = best_in(store, flown["near"]["T"], "thermal")
    tviews = views_for_candidate(store, cfg, tcand)
    assert [(v["lens"], v["primary"]) for v in tviews] == [("wide", False), ("zoom", False), ("thermal", True)]
    wide = cv2.imread(str(flown["near"]["W"]))
    b, g, r = (int(v) for v in wide[int(tviews[0]["py"]), int(tviews[0]["px"])])
    assert r > 200 and b < 80


def test_lenses_that_were_not_looking_at_it_are_marked_unavailable(flown):
    store, cfg = flown["store"], flown["cfg"]
    views = views_for_candidate(store, cfg, best_in(store, flown["far"]["W"]))
    by = {v["lens"]: v for v in views}
    assert by["wide"]["available"] and by["wide"]["primary"]
    assert not by["zoom"]["available"] and not by["thermal"]["available"]
    assert "outside" in by["zoom"]["reason"] and "url" not in by["zoom"]
    # a shot with only one lens file has nothing to switch to
    lone = views_for_candidate(store, cfg, best_in(store, flown["lone"]["W"]))
    assert [v["lens"] for v in lone] == ["wide"]


def test_views_api_and_rendered_closeups(flown):
    c = TestClient(create_app(flown["wd"]))
    groups = c.get("/api/groups").json()
    assert len(groups) == 1                                  # every sighting of the pack is one object
    g = groups[0]
    assert len(g["members"]) >= 4
    # by default the shot shown is one where the most lenses had the object in frame
    body = c.get(f"/api/groups/{g['id']}/views").json()
    views = body["views"]
    assert body["candidate_id"] in g["members"]
    assert {v["lens"] for v in views if v["available"]} == {"wide", "zoom", "thermal"}
    assert all("_0001_" in v["image_name"] for v in views)
    usable = views
    # the reviewer can pick another shot of the same object; there only the wide lens saw it
    far = next(m for m in g["members"] if "_0002_W" in c.get(f"/api/groups/{g['id']}/views", params={"candidate": m}).json()["views"][0]["image_name"])
    other = c.get(f"/api/groups/{g['id']}/views", params={"candidate": far}).json()
    assert other["candidate_id"] == far
    assert [(v["lens"], v["available"]) for v in other["views"]] == [("wide", True), ("zoom", False), ("thermal", False)]
    assert c.get("/api/groups/999999/views").status_code == 404
    assert c.get(f"/api/groups/{g['id']}/views", params={"candidate": 999999}).status_code == 404

    for v in usable:
        r = c.get(v["url"] if v["url"].startswith("api/") else "/" + v["url"]) if not v["primary"] else c.get("/" + v["url"])
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        if not v["primary"]:
            img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
            assert img.shape == (512, 512, 3)
            centre = img[236:276, 236:276].astype(int)
            if v["lens"] == "thermal":
                assert centre.mean() > 150                               # the warm pack fills the centre
            else:
                assert (centre[:, :, 2] > 200).mean() > 0.2              # orange at the centre
    n_files = len(list((flown["wd"] / "views").glob("*.jpg")))
    assert n_files == sum(1 for v in usable if not v["primary"]) == 2     # one close-up per non-primary lens
    for v in usable:
        if not v["primary"]:
            c.get(v["url"])
    assert len(list((flown["wd"] / "views").glob("*.jpg"))) == n_files    # served from the disk cache

    wide_id = next(v["image_id"] for v in views if v["lens"] == "wide")
    assert c.get(f"/api/view/{wide_id}.jpg", params={"px": 5, "py": 5, "win": 200}).status_code == 200   # corner: padded
    corner = cv2.imdecode(np.frombuffer(c.get(f"/api/view/{wide_id}.jpg", params={"px": 5, "py": 5, "win": 200}).content, np.uint8), 1)
    assert corner.shape == (512, 512, 3) and corner[:200, :200].max() == 0
    assert c.get(f"/api/view/{wide_id}.jpg", params={"px": -5, "py": 5}).status_code == 400
    assert c.get(f"/api/view/{wide_id}.jpg", params={"px": 99999, "py": 5}).status_code == 400
    assert c.get("/api/view/999999.jpg", params={"px": 5, "py": 5}).status_code == 404


def test_older_missions_gain_capture_keys_on_open(flown, tmp_path):
    import shutil
    import sqlite3
    wd = tmp_path / "old"
    shutil.copytree(flown["wd"], wd)
    con = sqlite3.connect(wd / "pointersearch.db")
    con.execute("PRAGMA wal_checkpoint(FULL)")
    con.execute("DROP INDEX IF EXISTS idx_images_capture")
    con.execute("ALTER TABLE images DROP COLUMN capture_key")
    con.commit(); con.close()
    store = Store(wd)                                              # opening migrates it
    img = next(i for i in store.list_images() if i["path"].endswith("_0001_W.JPG"))
    assert {i["source"] for i in store.siblings(img)} == {"wide", "zoom", "thermal"}


# ---- "_S" live-view captures ---------------------------------------------------
def shoot_split(folder, seq, cam_ne, stamp="20250601063000"):
    """A split-screen picture as the aircraft saves beside the lens files: thermal (grey with a
    red highlight) beside visible. Deliberately colourful, to tempt the colour detectors."""
    img = np.full((512, 640, 3), 90, np.uint8)
    img += np.random.default_rng(seq).integers(0, 25, img.shape, dtype=np.uint8)
    img[250:262, 312:320] = (20, 20, 235)                      # red isotherm blob, about 2 x 1.4 m on the ground
    lat, lon = FRAME.to_latlon(*cam_ne)
    xmp = XMP_TEMPLATE.format(lat=lat, lon=lon, abs_alt=480, rel_alt=80.0, roll=0.0, yaw=0.0, pitch=-90.0,
                              source="WideCamera")              # metadata can name any lens
    p = folder / f"DJI_{stamp}_{seq:04d}_S.JPG"
    write_jpeg_with_xmp(p, img, xmp, 24.0)
    return p


def test_split_screen_pictures_are_kept_for_viewing_but_not_searched(tmp_path):
    from pointersearch.metadata import read_image_meta
    img = tmp_path / "DCIM"
    files = shoot(img, 1, (4.0, -6.0), lenses="WT")
    s = shoot_split(img, 1, (4.0, -6.0))
    assert read_image_meta(s).source == "split"                # the file name wins over the metadata
    wd = tmp_path / "m"; wd.mkdir(); Config().save(wd)
    p = Pipeline(wd)
    assert p.process_path(img) == 3
    assert not [c for c in p.store.list_candidates() if c["image_path"].endswith("_S.JPG")]
    row = next(i for i in p.store.list_images() if i["path"].endswith("_S.JPG"))
    assert row["source"] == "split" and row["overlay_png"] is None and row["status"] == "ok"
    assert len(p.store.overlays()) == 2                         # not on the mosaic

    views = views_for_candidate(p.store, p.cfg, best_in(p.store, files["W"]))
    assert [v["lens"] for v in views] == ["wide", "thermal", "split"]
    split_view = views[2]
    assert split_view["available"] and split_view["whole"] and split_view["label"] == "Split"
    c = TestClient(create_app(wd))
    r = c.get("/" + split_view["url"])
    pic = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
    assert r.status_code == 200 and pic.shape == (512, 640, 3)  # the whole picture, not a close-up
    assert c.get("/api/summary").json()["version"]

    # "ignore" leaves them out altogether
    wd2 = tmp_path / "m2"; wd2.mkdir()
    cfg = Config(); cfg.split_frames = "ignore"; cfg.save(wd2)
    p2 = Pipeline(wd2)
    assert p2.process_path(img) == 2
    assert [v["lens"] for v in views_for_candidate(p2.store, p2.cfg, best_in(p2.store, files["W"]))] == ["wide", "thermal"]


def test_missions_from_older_versions_are_cleaned_when_opened(tmp_path):
    img = tmp_path / "DCIM"
    shoot(img, 1, (4.0, -6.0), lenses="WT")
    s = shoot_split(img, 1, (4.0, -6.0))
    s2 = shoot_split(img, 2, (30.0, 30.0))
    wd = tmp_path / "m"; wd.mkdir()
    old = Config(); old.split_frames = "process"; old.save(wd)          # how versions before 0.2 behaved
    p = Pipeline(wd)
    p.process_path(img)
    junk = [c for c in p.store.list_candidates() if c["image_path"].endswith("_S.JPG")]
    assert len(junk) >= 2, "the colour detectors should have fired on the red highlight"
    kept = junk[0]
    p.store.set_decision(kept["id"], "accepted")                         # a reviewer's decision
    real_before = len([c for c in p.store.list_candidates() if not c["image_path"].endswith("_S.JPG")])
    assert any(i["overlay_png"] for i in p.store.list_images() if i["path"].endswith("_S.JPG"))
    p.store.close()

    Config().save(wd)                                                     # the updated app's default
    app = create_app(wd)
    assert app.state.cleaned == {"frames": 2, "detections_removed": len(junk) - 1}
    store = app.state.store
    left = [c for c in store.list_candidates() if c["image_path"].endswith("_S.JPG")]
    assert [c["id"] for c in left] == [kept["id"]] and left[0]["status"] == "accepted"
    assert len([c for c in store.list_candidates() if not c["image_path"].endswith("_S.JPG")]) == real_before
    for i in store.list_images():
        if i["path"].endswith("_S.JPG"):
            assert i["source"] == "split" and i["overlay_png"] is None
    assert create_app(wd).state.cleaned["detections_removed"] == 0        # nothing more to do next time


def test_pages_and_scripts_are_always_rechecked_by_the_browser(tmp_path):
    from pointersearch.review.hub import create_hub
    hub = TestClient(create_hub(tmp_path))
    assert hub.get("/").headers["cache-control"] == "no-cache"
    assert hub.get("/static/app.js").headers["cache-control"] == "no-cache"
    url = hub.post("/api/missions", json={"name": "A"}).json()["url"]
    assert hub.get(url).headers["cache-control"] == "no-cache"
    assert hub.get(url + "plan").headers["cache-control"] == "no-cache"
    assert hub.get("/api/app").json()["version"] == hub.get(url + "api/summary").json()["version"]
