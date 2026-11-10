"""Local review server: a Leaflet map of overlays and candidates with accept/reject.

Runs entirely offline. Base map tiles are optional: point ``--tiles`` at a folder
of XYZ tiles (``{z}/{x}/{y}.png``) pre-downloaded while you still had internet.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import Config
from ..export import FORMATS, export
from ..ingest import IngestError, IngestJob, pick_folder
from ..mission import MissionError, default_home, plan_mission, points_from_store
from ..route import RouteError, plan_route
from ..saved_routes import RouteFileError, delete_route, list_routes, load_route, save_route
from ..store import Store
from ..wpml import build_kmz

STATIC_DIR = Path(__file__).parent / "static"
LOCAL_CLIENTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def require_local(request: Request):
    """Actions that touch the host's disk are only for the person at the laptop,
    not for other devices on the hotspot."""
    host = request.client.host if request.client else ""
    if host not in LOCAL_CLIENTS:
        raise HTTPException(403, "this action is only available on the computer running pointersearch")


class IngestRequest(BaseModel):
    folder: str
    watch: bool = False


class Decision(BaseModel):
    status: str
    note: str | None = None


class RouteRequest(BaseModel):
    mode: str = "line"                      # line | area
    points: list[list[float]]               # [[lat, lon], ...]
    name: str = "search"
    height_m: float | None = None
    speed_ms: float | None = None
    lens: str | None = None                 # coverage lens: wide | thermal
    front_overlap: float | None = None      # 0..1
    side_overlap: float | None = None       # 0..1
    angle_deg: float | None = None          # sweep direction; None = along the longest edge
    interval_m: float | None = None         # override: photo every N metres
    interval_s: float | None = None         # override: photo every N seconds
    photo_lenses: str | None = None         # e.g. "wide,ir"
    overwrite: bool = False                 # saving only: replace a saved route of the same name


def create_app(workdir: str | Path, tiles_dir: str | Path | None = None, mission_name: str | None = None) -> FastAPI:
    workdir = Path(workdir).resolve()
    store = Store(workdir)
    cfg = Config.load(workdir)
    store.regroup(cfg.group_radius_m)   # missions processed before grouping existed
    app = FastAPI(title="pointersearch review", docs_url=None, redoc_url=None)
    app.state.store = store
    ingest = IngestJob(workdir)
    app.state.ingest = ingest

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/summary")
    def summary():
        s = store.summary()
        s["mission"] = mission_name or workdir.name
        s["routes"] = len(list_routes(workdir))
        s["followup"] = {"transit_height_m": cfg.followup.transit_height_m,
                         "inspect_height_m": cfg.followup.inspect_height_m,
                         "max_height_m": cfg.flight.max_height_m}
        s["route"] = {"height_m": cfg.route.height_m, "speed_ms": cfg.route.speed_ms,
                      "coverage_lens": cfg.route.coverage_lens, "front_overlap": cfg.route.front_overlap,
                      "side_overlap": cfg.route.side_overlap, "photo_lenses": cfg.route.photo_lenses,
                      "max_height_m": cfg.flight.max_height_m}
        s["tiles"] = tiles_dir is not None
        return s

    @app.get("/api/images")
    def images():
        return store.list_images()

    @app.get("/api/overlays")
    def overlays():
        return store.overlays()

    @app.get("/api/candidates")
    def candidates(status: str | None = None, detector: str | None = None, since_id: int = 0):
        return store.list_candidates(status=status, detector=detector, since_id=since_id)

    @app.post("/api/candidates/{cid}")
    def decide(cid: int, d: Decision):
        try:
            ok = store.set_decision(cid, d.status, d.note)
        except ValueError:
            raise HTTPException(400, "status must be pending|accepted|followup|rejected")
        if not ok:
            raise HTTPException(404, "no such candidate")
        return store.get_candidate(cid)

    @app.get("/api/groups")
    def groups(status: str | None = None):
        return store.list_groups(status=status)

    @app.post("/api/groups/{gid}")
    def decide_group(gid: int, d: Decision):
        try:
            n = store.set_group_decision(gid, d.status, d.note)
        except ValueError:
            raise HTTPException(400, "status must be pending|accepted|followup|rejected")
        if not n:
            raise HTTPException(404, "no such group")
        g = next((g for g in store.list_groups() if g["id"] == gid), None)
        return g or {"id": gid, "status": d.status, "updated": n}

    @app.get("/api/export.{fmt}")
    def export_route(fmt: str, status: str = "accepted", by: str = "group"):
        if fmt not in FORMATS:
            raise HTTPException(404, "format must be one of " + ", ".join(FORMATS))
        body, mime = export(store, fmt, status, by=by)
        return Response(body, media_type=mime,
                        headers={"Content-Disposition": f'attachment; filename="candidates_{status}.{fmt}"'})

    def _plan(transit, inspect, status, home):
        home_ll = None
        if home:
            try:
                lat, lon = (float(v) for v in home.split(","))
                home_ll = (lat, lon)
            except ValueError:
                raise HTTPException(400, "home must be 'lat,lon'")
        else:
            home_ll = default_home(store)
        try:
            return plan_mission(points_from_store(store, status), cfg.followup, cfg.flight,
                                transit, inspect, home_ll)
        except MissionError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/mission")
    def mission_preview(transit: float | None = None, inspect: float | None = None,
                        status: str = "followup", home: str | None = None):
        """Route order, distance, time estimate and warnings, without writing a file."""
        d = _plan(transit, inspect, status, home).summary()
        d["defaults"] = {"transit_height_m": cfg.followup.transit_height_m,
                         "inspect_height_m": cfg.followup.inspect_height_m,
                         "max_height_m": cfg.flight.max_height_m}
        return d

    @app.get("/api/mission.kmz")
    def mission_kmz(transit: float | None = None, inspect: float | None = None,
                    status: str = "followup", home: str | None = None):
        plan = _plan(transit, inspect, status, home)
        name = f"followup_{len(plan.points)}pts_{plan.transit_height_m:g}-{plan.inspect_height_m:g}m.kmz"
        return Response(build_kmz(plan.wpml, cfg.flight), media_type="application/vnd.google-earth.kmz",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ---- adding images ---------------------------------------------------
    @app.get("/api/ingest")
    def ingest_status():
        return ingest.status()

    @app.post("/api/ingest")
    def ingest_start(body: IngestRequest, request: Request):
        require_local(request)
        try:
            return ingest.start(body.folder, body.watch)
        except IngestError as exc:
            running = "already running" in str(exc)
            raise HTTPException(409 if running else 400, str(exc))

    @app.post("/api/ingest/stop")
    def ingest_stop(request: Request):
        require_local(request)
        return ingest.stop()

    @app.post("/api/browse")
    def browse(request: Request):
        """Open the operating system's folder dialog on this computer."""
        require_local(request)
        folder = pick_folder()
        if folder is None:
            raise HTTPException(501, "no folder dialog is available here; paste the folder path instead")
        return {"folder": folder}

    # ---- search route planner -------------------------------------------
    @app.get("/plan")
    def plan_page():
        return FileResponse(STATIC_DIR / "plan.html")

    def _route(req: RouteRequest):
        try:
            return plan_route(
                req.mode, [(p[0], p[1]) for p in req.points if len(p) >= 2], cfg.route, cfg.flight, cfg.camera,
                height_m=req.height_m, speed_ms=req.speed_ms, lens=req.lens,
                front_overlap=req.front_overlap, side_overlap=req.side_overlap, angle_deg=req.angle_deg,
                interval_m=req.interval_m, interval_s=req.interval_s, photo_lenses=req.photo_lenses,
                name=req.name)
        except RouteError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/route/plan")
    def route_plan(req: RouteRequest):
        """Waypoints, photo positions, spacing, time estimate and warnings for a drawing."""
        return _route(req).summary()

    @app.post("/api/route/kmz")
    def route_kmz(req: RouteRequest):
        plan = _route(req)
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in req.name.strip())[:40] or "search"
        fname = f"{safe}_{plan.mode}_{plan.height_m:g}m.kmz"
        return Response(build_kmz(plan.wpml, cfg.flight), media_type="application/vnd.google-earth.kmz",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    # ---- saved routes ------------------------------------------------------
    _path_cache: dict[str, tuple] = {}

    def _route_layer(meta: dict) -> dict:
        """A saved route with its flight path, for drawing on a map. Cached until the file changes."""
        try:
            st_ = (workdir / "routes" / f"{meta['id']}.json").stat()
            key = (st_.st_mtime_ns, st_.st_size)
        except OSError:
            key = (meta.get("updated"), meta.get("n_points"))
        hit = _path_cache.get(meta["id"])
        if hit and hit[0] == key:
            return hit[1]
        r = load_route(workdir, meta["id"]) or {}
        st = r.get("settings") or {}
        out = dict(meta, points=r.get("points") or [], path=[], error=None)
        try:
            req = RouteRequest(mode=r.get("mode", "line"), points=out["points"], name=meta["name"],
                               **{k: st.get(k) for k in ("height_m", "speed_ms", "lens", "front_overlap",
                                                         "side_overlap", "angle_deg", "interval_m", "interval_s",
                                                         "photo_lenses")})
            plan = _route(req)
            out["path"] = [list(p) for p in plan.path]
            s = plan.summary()
            out["summary"] = {k: s[k] for k in ("waypoints", "n_photos", "distance_m", "duration_min", "height_m",
                                                "interval_m", "track_spacing_m")}
        except HTTPException as exc:
            out["error"] = str(exc.detail)
        except Exception:
            out["error"] = "the saved route file has invalid settings"
        _path_cache[meta["id"]] = (key, out)
        return out

    @app.get("/api/routes")
    def routes_list(paths: bool = False):
        """Saved routes. With ``paths=1`` each also carries its flight path for map display."""
        routes = list_routes(workdir)
        return [_route_layer(r) for r in routes] if paths else routes

    @app.post("/api/routes")
    def routes_save(req: RouteRequest):
        """Save the drawing and settings into the mission so it can be reopened and edited."""
        plan = _route(req)                                   # only save something that can be flown
        s = plan.summary()
        summary = {k: s[k] for k in ("waypoints", "n_photos", "distance_m", "duration_min", "height_m",
                                     "interval_m", "track_spacing_m")}
        try:
            return save_route(workdir, req.name, req.mode, req.points, req.model_dump(), summary, req.overwrite)
        except FileExistsError:
            raise HTTPException(409, f"a route called '{' '.join(req.name.split())}' is already saved in this mission")

    @app.get("/api/routes/{route_id}")
    def routes_get(route_id: str):
        try:
            r = load_route(workdir, route_id)
        except RouteFileError:
            r = None
        if r is None:
            raise HTTPException(404, "no such saved route")
        return r

    @app.delete("/api/routes/{route_id}")
    def routes_delete(route_id: str, request: Request):
        require_local(request)
        try:
            ok = delete_route(workdir, route_id)
        except RouteFileError:
            ok = False
        if not ok:
            raise HTTPException(404, "no such saved route")
        return {"deleted": route_id}

    @app.get("/api/routes/{route_id}/kmz")
    def routes_kmz(route_id: str):
        r = routes_get(route_id)
        st = r.get("settings") or {}
        try:
            req = RouteRequest(mode=r["mode"], points=r["points"], name=r["name"],
                               **{k: st.get(k) for k in ("height_m", "speed_ms", "lens", "front_overlap",
                                                         "side_overlap", "angle_deg", "interval_m", "interval_s",
                                                         "photo_lenses")})
        except Exception:
            raise HTTPException(400, "the saved route file has invalid settings")
        return route_kmz(req)

    @app.get("/files/{rel_path:path}")
    def files(rel_path: str):
        # Only serve from inside the working directory.
        target = (workdir / rel_path).resolve()
        if workdir not in target.parents or not target.is_file():
            raise HTTPException(404)
        return FileResponse(target)

    @app.get("/image/{image_id}")
    def full_image(image_id: int):
        row = next((i for i in store.list_images() if i["id"] == image_id), None)
        if not row or not Path(row["path"]).is_file():
            raise HTTPException(404)
        return FileResponse(row["path"])

    if tiles_dir is not None:
        tiles_dir = Path(tiles_dir).resolve()

        @app.get("/tiles/{z}/{x}/{y}.png")
        def tile(z: int, x: int, y: int):
            for ext in ("png", "jpg", "jpeg", "webp"):
                p = tiles_dir / str(z) / str(x) / f"{y}.{ext}"
                if p.is_file():
                    return FileResponse(p)
            return Response(status_code=204)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def serve(workdir: str | Path, host: str = "127.0.0.1", port: int = 8008, tiles_dir=None, mission_name=None):
    import uvicorn

    uvicorn.run(create_app(workdir, tiles_dir, mission_name), host=host, port=port, log_level="warning")
