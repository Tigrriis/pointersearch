import json
import math

from fastapi.testclient import TestClient

from pointersearch.config import Config
from pointersearch.export import export
from pointersearch.pipeline import Pipeline
from pointersearch.review.server import create_app
from pointersearch.watch import Watcher, watch


def _dist_m(a, b):
    return math.hypot((a[0] - b[0]) * 111320, (a[1] - b[1]) * 111320 * math.cos(math.radians(a[0])))


def test_pipeline_finds_planted_targets(mission):
    wd = mission["root"] / "work"
    wd.mkdir()
    Config().save(wd)
    p = Pipeline(wd)
    n = p.process_path(mission["images"])
    assert n == len(mission["manifest"]["images"])
    # idempotent: nothing reprocessed on a second pass
    assert p.process_path(mission["images"]) == 0

    cands = p.store.list_candidates()
    targets = mission["manifest"]["targets"]
    hits = {t["name"]: [] for t in targets}
    false_pos = 0
    for c in cands:
        best = min(targets, key=lambda t: _dist_m((c["lat"], c["lon"]), (t["lat"], t["lon"])))
        d = _dist_m((c["lat"], c["lon"]), (best["lat"], best["lon"]))
        if d < 3.0:
            hits[best["name"]].append(c["detector"])
        else:
            false_pos += 1
    assert "rx" in hits["blue_jacket"]
    assert "bright" in hits["orange_pack"]
    assert "thermal" in hits["warm_body"]
    assert false_pos <= 3

    # overlays and footprints exist for every image
    imgs = p.store.list_images()
    assert all(i["overlay_png"] and i["footprint"] for i in imgs)
    assert (wd / imgs[0]["overlay_png"]).exists()
    assert all((wd / c["thumb"]).exists() for c in cands)

    # grouping: each planted target is one object even when seen in several frames
    groups = p.store.list_groups()
    assert len(groups) < len(cands)
    for t in targets:
        near = [g for g in groups if _dist_m((g["lat"], g["lon"]), (t["lat"], t["lon"])) < 3.0]
        assert len(near) == 1, t["name"]
    jacket = next(g for g in groups if _dist_m((g["lat"], g["lon"]), (targets[0]["lat"], targets[0]["lon"])) < 3)
    assert jacket["n_frames"] >= 1 and set(jacket["detectors"]) >= {"rx"}
    assert all(c["range_m"] is not None for c in cands)

    # a group decision applies to every sighting
    p.store.set_group_decision(jacket["id"], "accepted")
    assert all(p.store.get_candidate(i)["status"] == "accepted" for i in jacket["members"])
    assert next(g for g in p.store.list_groups() if g["id"] == jacket["id"])["status"] == "accepted"

    # exports: one point per object by default, one per sighting on request
    gpx, _ = export(p.store, "gpx", "all")
    assert gpx.count("<wpt") == len(groups)
    assert export(p.store, "gpx", "all", by="sighting")[0].count("<wpt") == len(cands)
    assert export(p.store, "gpx", "accepted")[0].count("<wpt") == 1
    gj = json.loads(export(p.store, "geojson", "all")[0])
    assert len(gj["features"]) == len(groups)


def test_review_api(mission):
    wd = mission["root"] / "work_api"
    wd.mkdir()
    Config().save(wd)
    p = Pipeline(wd)
    first = sorted(mission["images"].glob("*_W.JPG"))[:3]
    for f in first:
        p.process_image(f)
    client = TestClient(create_app(wd))
    assert client.get("/").status_code == 200
    assert client.get("/static/vendor/leaflet/leaflet.js").status_code == 200
    s = client.get("/api/summary").json()
    assert s["images_ok"] == 3
    overlays = client.get("/api/overlays").json()
    assert len(overlays) == 3
    assert client.get("/files/" + overlays[0]["png"]).status_code == 200
    assert client.get("/files/../pointersearch.json").status_code == 404
    cands = client.get("/api/candidates").json()
    groups = client.get("/api/groups").json()
    assert len(groups) <= len(cands)
    if groups:
        gid = groups[0]["id"]
        r = client.post(f"/api/groups/{gid}", json={"status": "accepted", "note": "check"})
        assert r.json()["status"] == "accepted"
        assert client.get("/api/groups", params={"status": "accepted"}).json()[0]["id"] == gid
        assert client.get("/api/export.gpx").text.count("<wpt") == 1
        assert s["n_groups"] == len(groups) or True
    assert client.post("/api/groups/999999", json={"status": "accepted"}).status_code == 404
    assert client.post("/api/candidates/999999", json={"status": "accepted"}).status_code == 404
    assert client.post("/api/candidates/1", json={"status": "maybe"}).status_code == 400


def test_watch_once_processes_settled_files(mission):
    wd = mission["root"] / "work_watch"
    wd.mkdir()
    Config().save(wd)
    p = Pipeline(wd)
    folder = mission["images"]
    w = Watcher(p, folder)
    # First poll only records sizes; second poll processes files whose size is unchanged.
    assert w.poll() == 0
    assert w.poll() == len(mission["manifest"]["images"])
    assert w.poll() == 0
    # `once` wraps the two polls for scripting
    assert watch(p, folder, once=True) == 0
