import copy
import json
import math

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from pointersearch.align import Correction, Link, residual_m, solve
from pointersearch.config import AlignConfig, Config
from pointersearch.devtools.synthetic import generate_mission
from pointersearch.geo import LocalFrame
from pointersearch.georef import Georeferencer
from pointersearch.metadata import read_image_meta
from pointersearch.mosaic import Overlay, edge_fade, merge_overlays
from pointersearch.pipeline import Pipeline
from pointersearch.review.server import create_app

ORIGIN = (-33.7312, 150.3105)


def _dist_m(a, b):
    return math.hypot((a[0] - b[0]) * 111320, (a[1] - b[1]) * 111320 * math.cos(math.radians(a[0])))


# ---- the solver on its own ------------------------------------------------------
def _grid_links(true_fix, rng, n_pts=30):
    """Photos on a 3 x 3 grid 60 m apart. Each photo's recorded placement is wrong by a known
    amount; links are generated from true ground points seen through those wrong placements."""
    centres = {i: (60.0 * (i // 3), 60.0 * (i % 3)) for i in range(9)}
    def recorded(i, truth):                       # invert the true fix: where photo i *thinks* a point is
        c = Correction(*centres[i], **true_fix[i])
        a, b = 1 + c.ds, c.b
        q = truth - centres[i] - (c.tn, c.te)
        det = a * a + b * b
        return np.column_stack([(a * q[:, 0] + b * q[:, 1]) / det, (-b * q[:, 0] + a * q[:, 1]) / det]) + centres[i]
    links = []
    for i in range(9):
        for j in range(i):
            if abs(i // 3 - j // 3) + abs(i % 3 - j % 3) != 1:
                continue
            mid = (np.array(centres[i]) + centres[j]) / 2
            truth = mid + rng.uniform(-18, 18, (n_pts, 2))
            links.append(Link(i, j, recorded(i, truth).astype(np.float32), recorded(j, truth).astype(np.float32), 100.0, 100.0))
    return centres, links


def test_solver_recovers_known_shift_turn_and_scale():
    rng = np.random.default_rng(0)
    true_fix = {i: {"ds": 0.0, "b": 0.0, "tn": 0.0, "te": 0.0} for i in range(9)}
    true_fix[4] = {"ds": -0.2, "b": math.sin(math.radians(3)), "tn": 4.0, "te": -3.0}     # the middle photo is off
    centres, links = _grid_links(true_fix, rng)
    worst = max(float(np.hypot(*(l.pa - l.pb).T).max()) for l in links)
    assert worst > 5.0                                    # the middle photo disagrees with its neighbours
    corr = solve(centres, links, AlignConfig())
    assert len(corr) == 9 and residual_m(links, corr) < 0.05
    c = corr[4]
    # relative to its neighbours the middle photo gets back its true correction
    rel = lambda k: getattr(c, k) - np.mean([getattr(corr[i], k) for i in (1, 3, 5, 7)])
    assert rel("ds") == pytest.approx(-0.2, abs=0.02) and rel("tn") == pytest.approx(4.0, abs=0.5)
    true_turn = math.degrees(math.atan2(math.sin(math.radians(3)), 0.8))       # turn of a photo also scaled to 80 %
    assert c.turn_deg - np.mean([corr[i].turn_deg for i in (1, 3, 5, 7)]) == pytest.approx(true_turn, abs=0.2)


def test_solver_stays_near_gps_and_ignores_gross_outliers():
    rng = np.random.default_rng(1)
    zero = {i: {"ds": 0.0, "b": 0.0, "tn": 0.0, "te": 0.0} for i in range(9)}
    centres, links = _grid_links(zero, rng)
    corr = solve(centres, links, AlignConfig())
    assert max(c.shift_m for c in corr.values()) < 0.01 and all(abs(c.scale - 1) < 1e-3 for c in corr.values())
    # One wholly false link: two photos that look alike, "matched" 40 m off. Left in, it would
    # be cheapest for the fit to shrink the entire map; it must be recognised and ignored.
    from pointersearch.align import _solve, plausible_links
    false = copy.deepcopy(links)
    false[0].pb += (40.0, -25.0)
    collapsed = _solve(centres, false, AlignConfig())
    assert not collapsed or max(c.shift_m for c in collapsed.values()) > 3.0     # what the bare fit does with it
    assert len(plausible_links(centres, false, AlignConfig())) == len(links) - 1
    corr = solve(centres, false, AlignConfig())
    assert len(corr) == 9 and max(c.shift_m for c in corr.values()) < 0.05
    assert all(abs(c.scale - 1) < 1e-3 for c in corr.values())

    # the same check must not throw away honest links when the surface is high (canopy):
    # every pair then disagrees, but all by the same fraction of the distance between cameras
    canopy_fix = {i: {"ds": -0.27, "b": 0.0, "tn": 0.0, "te": 0.0} for i in range(9)}
    c2, l2 = _grid_links(canopy_fix, np.random.default_rng(2))
    assert len(plausible_links(c2, l2, AlignConfig())) == len(l2)
    assert np.median([c.scale for c in solve(c2, l2, AlignConfig()).values()]) == pytest.approx(0.73, abs=0.02)

    # if matches leave no sensible answer, the photos stay as recorded
    wild = copy.deepcopy(links)
    rng2 = np.random.default_rng(3)
    for l in wild:
        l.pb += rng2.uniform(-60, 60, 2)
    assert all(c.shift_m < 2.5 * AlignConfig().prior_shift_m for c in solve(centres, wild, AlignConfig()).values())
    assert solve({}, [], AlignConfig()) == {}


def test_correction_round_trip_and_serialisation():
    c = Correction(10.0, 20.0, ds=-0.25, b=0.05, tn=2.0, te=-1.0, gain=1.1, links=3)
    pts = np.array([[10.0, 20.0], [40.0, 60.0]])
    out = c.apply(pts)
    assert out[0] == pytest.approx([12.0, 19.0])                       # the centre only shifts
    assert Correction.from_dict(json.loads(json.dumps(c.to_dict()))).apply(pts) == pytest.approx(out)
    assert c.scale == pytest.approx(math.hypot(0.75, 0.05))


# ---- end to end on a mission whose record is wrong -----------------------------------
@pytest.fixture(scope="module")
def canopy(tmp_path_factory):
    """Recorded with 3 m GPS error, 2 degrees heading error, and a surface 30 m above
    take-off level (so every photo is first laid down about 37 % too large)."""
    root = tmp_path_factory.mktemp("canopy")
    man = generate_mission(root / "DCIM", area_n_m=150, area_e_m=200, rgb_size=(1200, 900), detail=True,
                           pose_noise=(3.0, 2.0), alt_bias_m=30.0)
    wd = root / "mission"
    wd.mkdir()
    Config().save(wd)
    p = Pipeline(wd)
    p.process_path(root / "DCIM")
    before = {c["id"]: (c["lat"], c["lon"]) for c in p.store.list_candidates()}
    stats = p.align_mission()
    return {"wd": wd, "pipeline": p, "manifest": man, "stats": stats, "before": before, "images": root / "DCIM"}


def test_photos_line_up_and_land_in_the_right_place(canopy):
    p, man, stats = canopy["pipeline"], canopy["manifest"], canopy["stats"]
    n_wide = sum(1 for i in p.store.list_images() if i["source"] == "wide")
    assert stats["links"] >= n_wide and stats["aligned"] == stats["photos"] == len(man["images"])
    assert stats["before_m"] > 10 and stats["after_m"] < 0.3                    # neighbours now agree

    frame = LocalFrame(*ORIGIN)
    raw_err, fix_err, scales = [], [], []
    for img in p.store.list_images():
        if img["source"] != "wide":
            continue
        meta = read_image_meta(img["path"])
        truth = man["truth"][img["path"].rsplit("/", 1)[-1][:-6]]
        tm = copy.copy(meta)
        tm.lat, tm.lon = frame.to_latlon(truth["north"], truth["east"])
        tm.gimbal_yaw, tm.rel_alt = truth["yaw"], truth["height"]
        true_geo, rec_geo = Georeferencer(tm, p.cfg.camera), Georeferencer(meta, p.cfg.camera)
        mission = LocalFrame(*p.store.origin())
        c = Correction.from_dict(img["align"])
        scales.append(c.scale)
        for px, py in [(0, 0), (meta.width, 0), (meta.width, meta.height), (0, meta.height)]:
            true_ll = true_geo.pixel_to_latlon(px, py)
            rec_ll = rec_geo.pixel_to_latlon(px, py)
            fixed = mission.to_latlon(*c.apply([mission.to_ne(*rec_ll)])[0])
            raw_err.append(_dist_m(rec_ll, true_ll))
            fix_err.append(_dist_m(fixed, true_ll))
    assert np.median(raw_err) > 15                                             # as recorded: tens of metres out
    # Lined up. What remains is the average GPS error of this small block of photos, which no
    # amount of matching between photos can remove; only a surveyed ground point could.
    assert np.median(fix_err) < 2.5 and max(fix_err) < 4.0
    assert np.median(fix_err) < np.median(raw_err) / 8
    assert np.median(scales) == pytest.approx(80 / 110, abs=0.03)              # the canopy height was recovered


def test_detections_move_with_their_photo_and_thermal_follows_wide(canopy):
    p, man = canopy["pipeline"], canopy["manifest"]
    targets = man["targets"]
    cands = p.store.list_candidates()
    moved = [c for c in cands if _dist_m((c["lat"], c["lon"]), canopy["before"][c["id"]]) > 1.0]
    assert len(moved) > len(cands) * 0.8
    assert all(c["lat_raw"] == pytest.approx(canopy["before"][c["id"]][0]) for c in cands)     # the record is kept

    # every planted target is now one object, within about a metre of where it really is
    groups = p.store.list_groups()
    for t in targets:
        near = [g for g in groups if _dist_m((g["lat"], g["lon"]), (t["lat"], t["lon"])) < 2.5]
        assert len(near) == 1, (t["name"], [round(_dist_m((g["lat"], g["lon"]), (t["lat"], t["lon"])), 1) for g in groups])
    raw_miss = min(_dist_m(v, (targets[0]["lat"], targets[0]["lon"])) for v in canopy["before"].values())
    assert raw_miss > 3.0                                                       # before, nothing was that close

    # a thermal photo takes the correction of the wide photo from the same shutter press
    imgs = p.store.list_images()
    wide = next(i for i in imgs if i["source"] == "wide" and i["align"])
    thermal = next(i for i in imgs if i["source"] == "thermal" and i["capture_key"] == wide["capture_key"])
    for k in ("ds", "b", "tn", "te", "cn", "ce"):
        assert thermal["align"][k] == pytest.approx(wide["align"][k])
    assert thermal["align"]["gain"] == 1.0 and thermal["aligned_png"]
    warm = next(t for t in targets if t["warm"])
    hot = [c for c in cands if c["detector"] == "thermal"]
    assert hot and min(_dist_m((c["lat"], c["lon"]), (warm["lat"], warm["lon"])) for c in hot) < 2.0


def test_solving_again_changes_nothing_and_new_photos_join_in(canopy, tmp_path):
    p = canopy["pipeline"]
    files = sorted((canopy["wd"] / "overlays_aligned").glob("*.png"))
    stamp = {f: f.stat().st_mtime_ns for f in files}
    again = p.align_mission()
    assert again["after_m"] == canopy["stats"]["after_m"]
    assert {f: f.stat().st_mtime_ns for f in files} == stamp                    # nothing was re-laid

    # the same mission fed a few photos at a time ends up the same
    wd = tmp_path / "m"; wd.mkdir(); Config().save(wd)
    q = Pipeline(wd)
    wides = sorted(canopy["images"].glob("*_W.JPG"))
    for i in range(0, len(wides), 3):
        for f in wides[i:i + 3]:
            q.process_image(f)
        st = q.align_mission()
    assert st["after_m"] < 0.3 and st["aligned"] == len(wides)


def test_map_api_serves_lined_up_patches_per_lens(canopy):
    c = TestClient(create_app(canopy["wd"]))
    ov = c.get("/api/overlays").json()
    assert {o["source"] for o in ov} == {"wide", "thermal"} and all(o["aligned"] for o in ov)
    assert all(o["png"].startswith("overlays_aligned/") and c.get("/files/" + o["png"]).status_code == 200 for o in ov[:4])
    s = c.get("/api/summary").json()
    assert s["align"]["links"] > 0 and s["align"]["after_m"] < 0.3 and s["stale"]["total"] == 0
    # the footprint drawn on the map is the lined-up one: smaller than as recorded (canopy scale)
    img = next(i for i in c.get("/api/images").json() if i["source"] == "wide")
    raw = next(i for i in canopy["pipeline"].store.list_images() if i["id"] == img["id"])["footprint"]
    side = lambda fp: _dist_m(fp[0], fp[1])
    assert side(img["footprint"]) == pytest.approx(side(raw) * 80 / 110, rel=0.05)


def test_photos_without_matching_detail_are_left_as_recorded(mission):
    # the plain test terrain has nothing for feature matching to hold on to
    from pointersearch.store import Store
    wd = mission["root"] / "work_plain"
    wd.mkdir(); Config().save(wd)
    p = Pipeline(wd)
    p.process_path(mission["images"])
    stats = p.align_mission()
    assert stats["links"] == 0 and stats["aligned"] == 0 and stats["photos"] > 0
    assert all(i["align"] is None and i["aligned_png"] is None for i in p.store.list_images())
    assert all(c["lat"] == c["lat_raw"] for c in p.store.list_candidates())
    assert all(not o["aligned"] for o in p.store.overlays())


# ---- seams and merging ---------------------------------------------------------------
def test_edge_fade_is_opaque_in_the_middle_and_clear_at_the_edge():
    a = edge_fade(100, 200, 0.12)
    assert a.shape == (100, 200) and a[50, 100] == 255 and a[0, 100] < 40 and a[50, 0] < 40
    assert a[6, 100] < a[50, 100] and (edge_fade(50, 50, 0.0) == 255).all()


def test_merge_blends_overlaps_towards_the_nearer_photo(tmp_path):
    def patch(name, colour, west):
        img = np.zeros((100, 100, 4), np.uint8)
        img[:, :, :3] = colour
        img[:, :, 3] = edge_fade(100, 100, 0.3)
        cv2.imwrite(str(tmp_path / name), img)
        return Overlay(str(tmp_path / name), 0.0, west, 0.001, west + 0.001, 100, 100)
    red, blue = patch("a.png", (0, 0, 255), 0.0), patch("b.png", (255, 0, 0), 0.0006)       # 40 % overlap
    out = merge_overlays([red, blue], tmp_path / "m.png")
    m = cv2.imread(str(tmp_path / "m.png"), cv2.IMREAD_UNCHANGED)
    assert out is not None and m.shape[1] == 160 and (tmp_path / "m.pgw").exists()
    row = m[50]
    assert row[30, 2] > 240 and row[30, 0] < 15                 # red photo's own area
    assert row[130, 0] > 240 and row[130, 2] < 15               # blue photo's own area
    assert row[66, 2] > 200 and row[94, 0] > 200                # in the overlap, the nearer middle wins
    assert abs(int(row[80, 0]) - int(row[80, 2])) < 60          # and the join is a blend, not a line
    assert row[80, 3] == 255 and m[50, 0, 3] > 0
    assert merge_overlays([], tmp_path / "none.png") is None
