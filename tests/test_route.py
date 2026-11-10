import io
import math
import zipfile
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from pointersearch.config import CameraDefaults, Config, FlightConfig, RouteConfig
from pointersearch.georef import LocalFrame
from pointersearch.review.server import create_app
from pointersearch.route import RouteError, footprint_m, path_length, photo_positions, plan_route, sweep_area
from pointersearch.wpml import KML_NS, build_kmz

ORIGIN = (-41.5630, 146.9990)
FRAME = LocalFrame(*ORIGIN)
CAM, FLIGHT, ROUTE = CameraDefaults(), FlightConfig(), RouteConfig()


def ll(n, e):
    return FRAME.to_latlon(n, e)


def rect(north=300.0, east=400.0):
    return [ll(0, 0), ll(0, east), ll(north, east), ll(north, 0)]


def plan(mode, pts, **kw):
    return plan_route(mode, pts, ROUTE, FLIGHT, CAM, **kw)


def test_footprints_match_published_lens_geometry():
    across, along = footprint_m(90, "wide", CAM)
    assert across == pytest.approx(129.8, abs=0.5) and along == pytest.approx(97.3, abs=0.5)
    t_across, t_along = footprint_m(90, "thermal", CAM)
    assert t_across == pytest.approx(57.4, abs=0.5) and t_along == pytest.approx(45.9, abs=0.5)
    assert footprint_m(45, "wide", CAM)[0] == pytest.approx(across / 2)
    with pytest.raises(RouteError):
        footprint_m(90, "fisheye", CAM)


def test_photo_positions_are_evenly_spaced_around_corners():
    path = [(0, 0), (0, 100), (50, 100)]
    pts = photo_positions(path, 30)
    assert len(pts) == 6                                   # 0, 30, 60, 90, 120, 150 m along a 150 m path
    assert pts[3] == pytest.approx((0, 90)) and pts[4] == pytest.approx((20, 100))
    assert pts[5] == pytest.approx((50, 100))
    assert len(photo_positions(path, 30, limit=3)) == 3


def test_line_route_follows_the_drawing():
    pts = [ll(0, 0), ll(0, 400), ll(300, 400)]
    p = plan("line", pts)
    assert [c for q in p.path for c in q] == pytest.approx([c for q in pts for c in q], abs=1e-9)
    assert p.distance_m == pytest.approx(700, abs=0.5)
    assert p.trigger == "multipleDistance"
    assert p.interval_m == pytest.approx(97.3 * 0.75, abs=0.5)        # along-track frame less 25 % overlap
    assert p.n_photos == int(700 // p.interval_m) + 1 == len(p.photos)
    assert p.track_spacing_m is None
    s = p.summary()
    assert s["waypoints"] == 3 and s["duration_min"] > 0 and s["gsd_cm"] == pytest.approx(1.61, abs=0.02)


def test_area_sweep_covers_rectangle_with_requested_overlap():
    p = plan("area", rect())
    across = p.footprint_across_m
    assert p.sweep_angle_deg == pytest.approx(90)                    # along the 400 m side
    assert p.track_spacing_m <= across * 0.75 + 1e-6
    ne = [FRAME.to_ne(*q) for q in p.path]
    norths = sorted({round(n, 3) for n, _ in ne})
    assert len(norths) == math.ceil(300 / (across * 0.75)) == len(ne) // 2
    # lines sit half a strip inside the boundary and the swath reaches both edges
    assert norths[0] == pytest.approx(p.track_spacing_m / 2, abs=0.01)
    assert norths[0] - across / 2 <= 0 and norths[-1] + across / 2 >= 300
    assert all(b - a == pytest.approx(p.track_spacing_m, abs=0.01) for a, b in zip(norths, norths[1:]))
    # serpentine: each line runs the full width, alternating direction
    easts = [round(e) for _, e in ne]
    assert easts[:4] == [0, 400, 400, 0]


def test_thermal_spacing_is_tighter_and_sweep_angle_can_be_set():
    wide, thermal = plan("area", rect()), plan("area", rect(), lens="thermal")
    assert thermal.track_spacing_m <= thermal.footprint_across_m * 0.75 + 1e-6
    assert thermal.track_spacing_m < wide.track_spacing_m * 0.6
    assert len(thermal.path) > len(wide.path)
    assert thermal.n_photos > 2 * wide.n_photos and thermal.duration_s > wide.duration_s
    ns = plan("area", rect(), angle_deg=0)                           # lines run north-south
    ne = [FRAME.to_ne(*q) for q in ns.path]
    assert ne[0][1] == pytest.approx(ne[1][1], abs=0.01) and abs(ne[1][0] - ne[0][0]) == pytest.approx(300, abs=0.01)


def test_sweep_handles_triangle_and_closed_ring():
    tri = [(0, 0), (0, 200), (200, 100)]
    path, angle, strip = sweep_area(tri, 40)
    assert len(path) >= 6 and path_length(path) > 0 and strip <= 40
    assert plan("area", rect() + [rect()[0]]).summary()["waypoints"] == plan("area", rect()).summary()["waypoints"]
    with pytest.raises(RouteError, match="too small"):
        sweep_area([(0, 0), (0, 0.2), (0.2, 0.1)], 40)


def test_interval_overrides_and_warnings():
    pts = [ll(0, 0), ll(0, 800)]
    by_time = plan("line", pts, interval_s=5)
    assert by_time.trigger == "multipleTiming" and by_time.interval_m == pytest.approx(40)
    assert plan("line", pts, interval_m=20).n_photos == 41
    fast = plan("line", pts, interval_s=1)
    assert any("faster than" in w for w in fast.warnings)
    gap = plan("line", pts, interval_m=200)
    assert any("gaps between photos" in w for w in gap.warnings)
    no_ir = plan("line", pts, lens="thermal", photo_lenses="wide")
    assert any("'ir' is not among" in w for w in no_ir.warnings)
    long = plan("area", rect(3000, 3000), lens="thermal")
    assert any("budget" in w for w in long.warnings)


@pytest.mark.parametrize("kw,match", [
    ({"height_m": 121}, "ceiling"), ({"height_m": 0}, "positive"), ({"speed_ms": 20}, "speed"),
    ({"front_overlap": 0.99}, "overlap"), ({"interval_m": 10, "interval_s": 2}, "not both"),
    ({"interval_m": -1}, "positive"), ({"photo_lenses": " "}, "at least one lens"),
])
def test_bad_settings_are_refused(kw, match):
    with pytest.raises(RouteError, match=match):
        plan("line", [ll(0, 0), ll(0, 400)], **kw)


def test_too_few_points():
    with pytest.raises(RouteError, match="two points"):
        plan("line", [ll(0, 0)])
    with pytest.raises(RouteError, match="two points"):
        plan("line", [ll(0, 0), ll(0, 0.1)])                  # duplicates collapse
    with pytest.raises(RouteError, match="three corners"):
        plan("area", [ll(0, 0), ll(0, 400)])
    with pytest.raises(RouteError, match="mode"):
        plan("circle", rect())


def test_route_kmz_has_interval_photo_group_over_whole_route():
    p = plan("area", rect(), height_m=120, photo_lenses="wide,zoom,ir")
    kmz = build_kmz(p.wpml, FLIGHT)
    w = f"http://www.dji.com/wpmz/{FLIGHT.wpml_version}"
    with zipfile.ZipFile(io.BytesIO(kmz)) as z:
        way = ET.fromstring(z.read("wpmz/waylines.wpml"))
        tpl = ET.fromstring(z.read("wpmz/template.kml"))
    pms = way.findall(f".//{{{KML_NS}}}Placemark")
    assert len(pms) == len(p.path)
    assert {pm.find(f"{{{w}}}executeHeight").text for pm in pms} == {"120"}
    assert way.find(f".//{{{w}}}takeOffSecurityHeight").text == "120"
    groups = way.findall(f".//{{{w}}}actionGroup")
    assert len(groups) == 2 and all(g in list(pms[0]) for g in groups)      # both hang off waypoint 0
    gimbal, photo = groups
    assert gimbal.find(f".//{{{w}}}actionTriggerType").text == "reachPoint"
    assert gimbal.find(f".//{{{w}}}gimbalPitchRotateAngle").text == "-90"
    assert photo.find(f".//{{{w}}}actionTriggerType").text == "multipleDistance"
    assert float(photo.find(f".//{{{w}}}actionTriggerParam").text) == pytest.approx(p.interval_m, abs=0.01)
    assert photo.find(f"{{{w}}}actionGroupStartIndex").text == "0"
    assert photo.find(f"{{{w}}}actionGroupEndIndex").text == str(len(p.path) - 1)
    assert photo.find(f".//{{{w}}}actionActuatorFunc").text == "takePhoto"
    assert photo.find(f".//{{{w}}}payloadLensIndex").text == "wide,zoom,ir"
    assert tpl.find(f".//{{{w}}}imageFormat").text == "wide,zoom,ir"
    assert len(tpl.findall(f".//{{{KML_NS}}}Placemark")) == len(p.path)

    t = plan("line", [ll(0, 0), ll(0, 400)], interval_s=4)
    with zipfile.ZipFile(io.BytesIO(build_kmz(t.wpml, FLIGHT))) as z:
        way = ET.fromstring(z.read("wpmz/waylines.wpml"))
    trig = way.findall(f".//{{{w}}}actionTrigger")[1]
    assert trig.find(f"{{{w}}}actionTriggerType").text == "multipleTiming"
    assert float(trig.find(f"{{{w}}}actionTriggerParam").text) == 4


def test_planner_api(tmp_path):
    Config().save(tmp_path)
    client = TestClient(create_app(tmp_path))                 # works on an empty mission directory
    assert client.get("/plan").status_code == 200
    assert client.get("/static/plan.js").status_code == 200
    assert client.get("/api/summary").json()["route"]["height_m"] == 90

    body = {"mode": "area", "points": [list(p) for p in rect()], "name": "Oaks Rd / east!", "lens": "thermal"}
    r = client.post("/api/route/plan", json=body)
    assert r.status_code == 200
    s = r.json()
    assert s["mode"] == "area" and s["lens"] == "thermal" and s["n_photos"] > 10
    assert len(s["path"]) == s["waypoints"] and len(s["photos"]) == s["n_photos"]

    r = client.post("/api/route/kmz", json=body)
    assert r.status_code == 200
    assert 'filename="Oaks_Rd___east__area_90m.kmz"' in r.headers["content-disposition"]
    assert sorted(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == ["wpmz/template.kml", "wpmz/waylines.wpml"]

    r = client.post("/api/route/plan", json={**body, "height_m": 150})
    assert r.status_code == 400 and "ceiling" in r.json()["detail"]
    assert client.post("/api/route/kmz", json={"mode": "line", "points": [list(ll(0, 0))]}).status_code == 400
