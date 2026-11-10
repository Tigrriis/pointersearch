import io
import zipfile
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from pointersearch.config import Config, FlightConfig, FollowupConfig
from pointersearch.mission import MissionError, MissionPoint, order_points, plan_mission, points_from_store
from pointersearch.wpml import KML_NS, build_kmz, wpml_info
from pointersearch.pipeline import Pipeline
from pointersearch.review.server import create_app

HOME = (-41.5625, 146.9985)
PTS = [MissionPoint(1, -41.5630, 146.9990, "rx"),
       MissionPoint(2, -41.5640, 147.0005, "rx+bright"),
       MissionPoint(3, -41.5633, 146.9996, "thermal")]


def _wpml(cfg):
    return f"http://www.dji.com/wpmz/{cfg.wpml_version}"


def _parse(kmz: bytes, name: str):
    with zipfile.ZipFile(io.BytesIO(kmz)) as z:
        return ET.fromstring(z.read(name))


def test_default_heights_and_three_waypoints_per_object():
    cfg = FollowupConfig()
    plan = plan_mission(PTS, cfg, home=HOME)
    assert (plan.transit_height_m, plan.inspect_height_m) == (90.0, 60.0)
    assert len(plan.waypoints) == 9
    heights = [w.height_m for w in plan.waypoints]
    assert heights == [90, 60, 90] * 3
    assert [w.inspect for w in plan.waypoints] == [False, True, False] * 3
    # the descent is vertical: transit and inspection waypoints share a position
    a, b, c = plan.waypoints[:3]
    assert (a.lat, a.lon) == (b.lat, b.lon) == (c.lat, c.lon)
    assert plan.duration_s > 0 and plan.distance_m > 0


def test_height_overrides_and_limits():
    cfg = FollowupConfig()
    plan = plan_mission(PTS, cfg, None, 120, 90, HOME)
    assert {w.height_m for w in plan.waypoints} == {120.0, 90.0}
    with pytest.raises(MissionError, match="ceiling"):
        plan_mission(PTS, cfg, None, 121, 90, HOME)
    with pytest.raises(MissionError, match="above transit"):
        plan_mission(PTS, cfg, None, 60, 90, HOME)
    with pytest.raises(MissionError, match="positive"):
        plan_mission(PTS, cfg, None, 90, 0, HOME)
    assert plan_mission(PTS, cfg, FlightConfig(max_height_m=150), 140, 90, HOME).transit_height_m == 140
    with pytest.raises(MissionError, match="no objects"):
        plan_mission([], cfg, home=HOME)


def test_equal_heights_give_one_waypoint_per_object():
    plan = plan_mission(PTS, FollowupConfig(), None, 60, 60, HOME)
    assert len(plan.waypoints) == 3 and all(w.inspect for w in plan.waypoints)


def test_approach_offset_separates_stacked_waypoints():
    plan = plan_mission(PTS, FollowupConfig(approach_offset_m=3.0), home=HOME)
    a, b, _ = plan.waypoints[:3]
    assert (a.lat, a.lon) != (b.lat, b.lon)
    assert (b.lat, b.lon) == (plan.points[0].lat, plan.points[0].lon)   # photo is still over the object


def test_route_order_is_shorter_than_input_order():
    import math
    pts = [MissionPoint(i, HOME[0] + dy * 1e-3, HOME[1] + dx * 1e-3) for i, (dy, dx) in
           enumerate([(1, 1), (-3, 4), (1, 2), (-3, 3), (2, 1), (-2, 4)])]

    def length(seq):
        path = [HOME] + [(p.lat, p.lon) for p in seq] + [HOME]
        return sum(math.dist(a, b) for a, b in zip(path, path[1:]))

    ordered = order_points(pts, HOME)
    assert sorted(p.object_id for p in ordered) == list(range(6))
    assert length(ordered) < length(pts)


def test_kmz_structure_and_contents():
    cfg = FlightConfig()
    plan = plan_mission(PTS, FollowupConfig(), cfg, home=HOME)
    kmz = build_kmz(plan.wpml, cfg)
    with zipfile.ZipFile(io.BytesIO(kmz)) as z:
        assert sorted(z.namelist()) == ["wpmz/template.kml", "wpmz/waylines.wpml"]
    w = _wpml(cfg)
    way = _parse(kmz, "wpmz/waylines.wpml")
    assert way.tag == f"{{{KML_NS}}}kml"

    def text(root, tag):
        return root.find(f".//{{{w}}}{tag}").text

    assert text(way, "executeHeightMode") == "relativeToStartPoint"
    assert text(way, "takeOffSecurityHeight") == "90" and text(way, "globalRTHHeight") == "90"
    assert (text(way, "droneEnumValue"), text(way, "droneSubEnumValue"), text(way, "payloadEnumValue")) == ("99", "1", "89")
    assert text(way, "exitOnRCLost") == "executeLostAction" and text(way, "executeRCLostAction") == "goBack"

    pms = way.findall(f".//{{{KML_NS}}}Placemark")
    assert [pm.find(f"{{{w}}}index").text for pm in pms] == [str(i) for i in range(9)]
    assert [pm.find(f"{{{w}}}executeHeight").text for pm in pms] == ["90", "60", "90"] * 3
    lon, lat = (float(v) for v in pms[1].find(f"{{{KML_NS}}}Point/{{{KML_NS}}}coordinates").text.split(","))
    assert (lat, lon) == pytest.approx((plan.points[0].lat, plan.points[0].lon))

    groups = way.findall(f".//{{{w}}}actionGroup")
    assert len(groups) == 3
    assert [g.find(f"{{{w}}}actionGroupId").text for g in groups] == ["0", "1", "2"]
    for pm in pms:
        has = pm.find(f"{{{w}}}actionGroup") is not None
        assert has == (pm.find(f"{{{w}}}executeHeight").text == "60")
    g = groups[0]
    assert g.find(f"{{{w}}}actionGroupStartIndex").text == g.find(f"{{{w}}}actionGroupEndIndex").text == "1"
    assert [a.text for a in g.iter(f"{{{w}}}actionActuatorFunc")] == ["gimbalRotate", "hover", "takePhoto"]
    assert g.find(f".//{{{w}}}gimbalPitchRotateAngle").text == "-90"
    assert g.find(f".//{{{w}}}payloadLensIndex").text == "wide,zoom,ir"
    assert g.find(f".//{{{w}}}fileSuffix").text == f"PS{plan.points[0].object_id}"

    tpl = _parse(kmz, "wpmz/template.kml")
    assert text(tpl, "templateType") == "waypoint"
    assert text(tpl, "heightMode") == "relativeToStartPoint"
    assert text(tpl, "imageFormat") == "wide,zoom,ir"
    tpms = tpl.findall(f".//{{{KML_NS}}}Placemark")
    assert [pm.find(f"{{{w}}}height").text for pm in tpms] == ["90", "60", "90"] * 3
    assert len(tpl.findall(f".//{{{w}}}actionGroup")) == 3


def test_wpml_info_round_trip(tmp_path):
    cfg = FlightConfig(drone_enum=77, drone_sub_enum=1, payload_enum=67, wpml_version="1.0.2")
    f = tmp_path / "route.kmz"
    f.write_bytes(build_kmz(plan_mission(PTS, FollowupConfig(), cfg, home=HOME).wpml, cfg))
    info = wpml_info(f)
    assert (info["drone_enum"], info["drone_sub_enum"], info["payload_enum"]) == (77, 1, 67)
    assert info["wpml_version"] == "1.0.2" and info["height_mode"] == "relativeToStartPoint"
    assert "takePhoto" in info["actions_seen"] and "wide,zoom,ir" in info["lens_strings_seen"]


def test_followup_status_and_mission_api(mission):
    wd = mission["root"] / "work_mission"
    wd.mkdir()
    Config().save(wd)
    p = Pipeline(wd)
    p.process_path(mission["images"])
    client = TestClient(create_app(wd))

    # nothing tagged yet
    r = client.get("/api/mission")
    assert r.status_code == 400 and "no objects" in r.json()["detail"]

    groups = client.get("/api/groups").json()
    assert len(groups) >= 3
    for g in groups[:2]:
        assert client.post(f"/api/groups/{g['id']}", json={"status": "followup"}).json()["status"] == "followup"
    client.post(f"/api/groups/{groups[2]['id']}", json={"status": "accepted"})
    assert client.get("/api/summary").json()["groups"]["followup"] == 2
    assert len(points_from_store(p.store)) == 2
    assert len(points_from_store(p.store, "followup,accepted")) == 3

    prev = client.get("/api/mission").json()
    assert prev["objects"] == 2 and prev["waypoints"] == 6
    assert (prev["transit_height_m"], prev["inspect_height_m"]) == (90, 60)
    assert {o["object_id"] for o in prev["order"]} == {groups[0]["id"], groups[1]["id"]}

    assert client.get("/api/mission", params={"transit": 120, "inspect": 90}).json()["transit_height_m"] == 120
    assert client.get("/api/mission", params={"transit": 130}).status_code == 400
    assert client.get("/api/mission", params={"status": "followup,accepted"}).json()["objects"] == 3

    r = client.get("/api/mission.kmz", params={"transit": 120, "inspect": 90})
    assert r.status_code == 200 and "followup_2pts_120-90m.kmz" in r.headers["content-disposition"]
    way = _parse(r.content, "wpmz/waylines.wpml")
    w = _wpml(FlightConfig())
    assert [e.text for e in way.iter(f"{{{w}}}executeHeight")] == ["120", "90", "120"] * 2

    # follow-ups export as waypoints for a map too, and are not in the accepted export
    assert client.get("/api/export.gpx", params={"status": "followup"}).text.count("<wpt") == 2
    assert client.get("/api/export.gpx").text.count("<wpt") == 1
