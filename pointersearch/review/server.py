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
from ..saved_routes import list_routes
from ..store import Store
from ..wpml import build_kmz
from .missions import STATIC_DIR, require_local  # noqa: F401  (re-exported)
from .planner_api import add_planner_api, route_defaults


class IngestRequest(BaseModel):
    folder: str
    watch: bool = False


class Decision(BaseModel):
    status: str
    note: str | None = None


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
        s["route"] = route_defaults(cfg)
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

    add_planner_api(app, workdir, cfg, require_local)

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
