import json
import time

import pytest
from fastapi.testclient import TestClient

from pointersearch.config import Config
from pointersearch.georef import LocalFrame
from pointersearch.ingest import IngestError, IngestJob
from pointersearch.review.hub import clean_name, create_hub
from pointersearch.review.server import create_app
from pointersearch.saved_routes import list_routes, load_route, save_route, slug

FRAME = LocalFrame(-41.5630, 146.9990)
AREA = [list(FRAME.to_latlon(n, e)) for n, e in [(0, 0), (0, 400), (300, 400), (300, 0)]]


def wait_for(job_status, states, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        s = job_status()
        if s["state"] in states:
            return s
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {states}: {job_status()}")


# ---- start screen / missions -------------------------------------------------
def test_mission_names():
    assert clean_name("  2026-11-10   Oaks Road ") == "2026-11-10 Oaks Road"
    for bad in ("", "   ", "../etc", "a/b", "a\\b", ".hidden", "x" * 61, "trailing.", "CON:", "con", "NUL.txt", "COM1"):
        with pytest.raises(ValueError):
            clean_name(bad)


def test_create_and_open_missions_from_the_start_screen(tmp_path):
    hub = TestClient(create_hub(tmp_path / "missions"))
    assert hub.get("/").status_code == 200 and "New mission" in hub.get("/").text
    assert hub.get("/api/app").json()["app"] == "pointersearch"
    assert hub.get("/api/missions").json()["missions"] == []

    r = hub.post("/api/missions", json={"name": " 2026-11-10  Oaks Road "})
    assert r.status_code == 200 and r.json() == {"name": "2026-11-10 Oaks Road", "url": "/m/2026-11-10%20Oaks%20Road/"}
    url = r.json()["url"]
    mdir = tmp_path / "missions" / "2026-11-10 Oaks Road"
    assert (mdir / Config.FILENAME).exists() and (mdir / "pointersearch.db").exists()

    assert hub.post("/api/missions", json={"name": "2026-11-10 oaks road"}).status_code == 409   # case-insensitive
    assert hub.post("/api/missions", json={"name": "../escape"}).status_code == 400
    assert hub.post("/api/missions", json={"name": ""}).status_code == 400
    assert not (tmp_path / "escape").exists()

    # the mission's planner, review map and API are served under its own address
    assert "route planner" in hub.get(url + "plan").text
    assert hub.get(url).status_code == 200
    assert hub.get(url + "api/summary").json()["mission"] == "2026-11-10 Oaks Road"
    r = hub.get(url.rstrip("/"), follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == url
    assert hub.get("/m/nope/").status_code == 404 and hub.get("/m/..%2F/").status_code == 404
    assert hub.post(url + "api/route/plan", json={"mode": "area", "points": AREA}).status_code == 200

    hub.post("/api/missions", json={"name": "Second"})
    cards = hub.get("/api/missions").json()["missions"]
    assert {c["name"] for c in cards} == {"2026-11-10 Oaks Road", "Second"}
    assert all(c["images"] == 0 and c["routes"] == 0 and c["importing"] is None for c in cards)
    # folders that are not missions are ignored
    (tmp_path / "missions" / "random folder").mkdir()
    assert len(hub.get("/api/missions").json()["missions"]) == 2


def test_missions_are_isolated(tmp_path):
    hub = TestClient(create_hub(tmp_path))
    a = hub.post("/api/missions", json={"name": "A"}).json()["url"]
    b = hub.post("/api/missions", json={"name": "B"}).json()["url"]
    assert hub.post(a + "api/routes", json={"mode": "area", "points": AREA, "name": "North block"}).status_code == 200
    assert len(hub.get(a + "api/routes").json()) == 1 and hub.get(b + "api/routes").json() == []
    cards = {c["name"]: c for c in hub.get("/api/missions").json()["missions"]}
    assert cards["A"]["routes"] == 1 and cards["B"]["routes"] == 0


def test_host_actions_are_refused_from_other_devices(tmp_path):
    hub = create_hub(tmp_path)
    local = TestClient(hub)
    url = local.post("/api/missions", json={"name": "A"}).json()["url"]
    remote = TestClient(hub, client=("192.168.1.50", 50000))
    assert remote.get("/api/missions").status_code == 200                       # reading is fine
    assert remote.get(url + "api/summary").status_code == 200
    assert remote.post("/api/missions", json={"name": "B"}).status_code == 403
    assert remote.post(url + "api/ingest", json={"folder": str(tmp_path)}).status_code == 403
    assert remote.post(url + "api/browse").status_code == 403
    assert remote.post(url + "api/ingest/stop").status_code == 403


# ---- saved routes ------------------------------------------------------------
def test_saved_route_round_trip_and_edit(tmp_path):
    Config().save(tmp_path)
    c = TestClient(create_app(tmp_path))
    body = {"mode": "area", "points": AREA, "name": "Oaks Rd / east", "lens": "thermal", "height_m": 100,
            "side_overlap": 0.3, "photo_lenses": "wide,ir"}
    r = c.post("/api/routes", json=body)
    assert r.status_code == 200
    doc = r.json()
    assert doc["id"] == "oaks-rd-east" and doc["format"] == "pointersearch-route" and doc["version"] == 1
    assert doc["points"] == AREA and doc["settings"]["height_m"] == 100 and doc["settings"]["lens"] == "thermal"
    assert doc["settings"]["interval_m"] is None and doc["summary"]["n_photos"] > 10

    # it is a plain JSON file in the mission folder
    f = tmp_path / "routes" / "oaks-rd-east.json"
    on_disk = json.loads(f.read_text())
    assert on_disk["name"] == "Oaks Rd / east" and on_disk["mode"] == "area"

    listed = c.get("/api/routes").json()
    assert [x["id"] for x in listed] == ["oaks-rd-east"] and listed[0]["n_points"] == 4
    assert c.get("/api/routes/oaks-rd-east").json()["points"] == AREA
    assert c.get("/api/summary").json()["routes"] == 1

    # saving the same name again needs an explicit overwrite, and keeps the created time
    assert c.post("/api/routes", json=body).status_code == 409
    edited = {**body, "points": AREA[:3], "height_m": 120, "overwrite": True}
    again = c.post("/api/routes", json=edited).json()
    assert again["created"] == doc["created"] and len(again["points"]) == 3 and again["settings"]["height_m"] == 120
    assert len(c.get("/api/routes").json()) == 1

    # editing the file by hand is picked up: the KMZ is regenerated from the file
    on_disk = json.loads(f.read_text())
    on_disk["settings"]["height_m"] = 80
    f.write_text(json.dumps(on_disk))
    r = c.get("/api/routes/oaks-rd-east/kmz")
    assert r.status_code == 200 and "_area_80m.kmz" in r.headers["content-disposition"]

    # a route that cannot be flown is not saved
    assert c.post("/api/routes", json={**body, "name": "too high", "height_m": 150}).status_code == 400
    assert c.post("/api/routes", json={"mode": "line", "points": AREA[:1], "name": "one point"}).status_code == 400
    assert len(c.get("/api/routes").json()) == 1

    assert c.delete("/api/routes/oaks-rd-east").status_code == 200
    assert c.delete("/api/routes/oaks-rd-east").status_code == 404
    assert c.get("/api/routes/..%2F..%2Fpointersearch").status_code == 404
    assert c.get("/api/routes").json() == [] and not f.exists()


def test_saved_routes_with_paths_for_the_review_map(tmp_path):
    Config().save(tmp_path)
    c = TestClient(create_app(tmp_path))
    assert c.get("/api/routes", params={"paths": 1}).json() == []
    c.post("/api/routes", json={"mode": "area", "points": AREA, "name": "North block"})
    c.post("/api/routes", json={"mode": "line", "points": AREA[:2], "name": "Creek line"})
    assert all("path" not in r for r in c.get("/api/routes").json())           # plain listing stays light

    layers = {r["id"]: r for r in c.get("/api/routes", params={"paths": 1}).json()}
    area, line = layers["north-block"], layers["creek-line"]
    assert area["points"] == AREA and len(area["path"]) == area["summary"]["waypoints"] >= 4 and area["error"] is None
    assert len(line["path"]) == 2 and line["mode"] == "line"

    # an edit is reflected (the cache follows the file), and an unflyable file still shows its drawing
    c.post("/api/routes", json={"mode": "line", "points": AREA[:3], "name": "Creek line", "overwrite": True})
    assert len({r["id"]: r for r in c.get("/api/routes", params={"paths": 1}).json()}["creek-line"]["path"]) == 3
    f = tmp_path / "routes" / "north-block.json"
    doc = json.loads(f.read_text()); doc["settings"]["height_m"] = 500; doc["updated"] = "2099-01-01T00:00:00"
    f.write_text(json.dumps(doc))
    bad = {r["id"]: r for r in c.get("/api/routes", params={"paths": 1}).json()}["north-block"]
    assert bad["path"] == [] and bad["points"] == AREA and "ceiling" in bad["error"]


def test_saved_route_files_tolerate_junk(tmp_path):
    assert slug("  Creek line — North #2 ") == "creek-line-north-2" and slug("!!!") == "route"
    save_route(tmp_path, "Good", "line", AREA[:2], {"height_m": 90})
    (tmp_path / "routes" / "broken.json").write_text("{not json")
    (tmp_path / "routes" / "other.json").write_text(json.dumps({"format": "something-else", "points": []}))
    assert [r["id"] for r in list_routes(tmp_path)] == ["good"]
    assert load_route(tmp_path, "broken") is None and load_route(tmp_path, "missing") is None
    with pytest.raises(FileExistsError):
        save_route(tmp_path, "good", "line", AREA[:2], {})


# ---- adding images from the browser -------------------------------------------
def test_ingest_job_processes_a_folder(mission, tmp_path):
    Config().save(tmp_path)
    job = IngestJob(tmp_path)
    assert job.status()["state"] == "idle"
    with pytest.raises(IngestError, match="not found"):
        job.start(str(tmp_path / "missing"))
    with pytest.raises(IngestError, match="enter the folder"):
        job.start("   ")
    f = tmp_path / "a_file.txt"; f.write_text("x")
    with pytest.raises(IngestError, match="not a folder"):
        job.start(str(f))

    n = len(mission["manifest"]["images"])
    job.start(f'"{mission["images"]}"')                       # quotes from "Copy as path" are accepted
    s = wait_for(job.status, {"done", "error"})
    assert s["state"] == "done" and s["total"] == s["done"] == s["added"] == n and "added" in s["message"]
    # a second run finds nothing new
    job.start(str(mission["images"]))
    s = wait_for(job.status, {"done", "error"})
    assert s["added"] == 0 and s["skipped"] == n

    empty = tmp_path / "empty"; empty.mkdir()
    job.start(str(empty))
    assert "no JPEG" in wait_for(job.status, {"done"})["message"]


def test_ingest_api_with_watch_and_stop(mission, tmp_path):
    Config().save(tmp_path)
    c = TestClient(create_app(tmp_path))
    assert c.get("/api/ingest").json()["state"] == "idle"
    assert c.post("/api/ingest", json={"folder": str(tmp_path / "nope")}).status_code == 400

    r = c.post("/api/ingest", json={"folder": str(mission["images"]), "watch": True})
    assert r.status_code == 200 and r.json()["state"] == "running"
    assert c.post("/api/ingest", json={"folder": str(mission["images"])}).status_code == 409   # one at a time
    s = wait_for(lambda: c.get("/api/ingest").json(), {"watching", "error"})
    assert s["state"] == "watching" and s["added"] == len(mission["manifest"]["images"])
    assert c.get("/api/summary").json()["images_ok"] == s["added"]
    assert len(c.get("/api/groups").json()) >= 3              # the review map has objects to show

    c.post("/api/ingest/stop")
    assert wait_for(lambda: c.get("/api/ingest").json(), {"stopped"}, timeout=10)["state"] == "stopped"
    c.app.state.ingest.join(5)
