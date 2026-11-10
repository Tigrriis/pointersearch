"""Route planner endpoints for one mission: plan, export KMZ, save, open, import.

Shared by the laptop app and the hosted planner. Imports nothing from the
image-processing stack.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from ..config import Config
from ..route import RouteError, plan_route
from ..saved_routes import (FORMAT, SETTING_KEYS, RouteFileError, delete_route, list_routes, load_route,
                            save_route)
from ..wpml import build_kmz
from .missions import STATIC_DIR


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


class RouteImport(BaseModel):
    route: dict                             # a saved route document (the JSON file's content)
    overwrite: bool = False


SUMMARY_KEYS = ("waypoints", "n_photos", "distance_m", "duration_min", "height_m", "interval_m", "track_spacing_m")


def route_defaults(cfg: Config) -> dict:
    return {"height_m": cfg.route.height_m, "speed_ms": cfg.route.speed_ms,
            "coverage_lens": cfg.route.coverage_lens, "front_overlap": cfg.route.front_overlap,
            "side_overlap": cfg.route.side_overlap, "photo_lenses": cfg.route.photo_lenses,
            "max_height_m": cfg.flight.max_height_m}


def _who(request: Request) -> str | None:
    user = getattr(request.state, "user", None)
    return (user or {}).get("name") or (user or {}).get("email") if isinstance(user, dict) else None


def add_planner_api(app: FastAPI, workdir: Path, cfg: Config, guard: Callable[[Request], None]):
    """Register the planner page and its API on ``app``. ``guard`` protects deletion."""
    workdir = Path(workdir)

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

    def _request_from_doc(doc: dict) -> RouteRequest:
        st = doc.get("settings") or {}
        try:
            return RouteRequest(mode=doc.get("mode", "line"), points=doc.get("points") or [],
                                name=str(doc.get("name") or "route"),
                                **{k: st.get(k) for k in SETTING_KEYS})
        except Exception:
            raise HTTPException(400, "the route file has invalid settings")

    def _kmz_response(req: RouteRequest):
        plan = _route(req)
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in req.name.strip())[:40] or "search"
        fname = f"{safe}_{plan.mode}_{plan.height_m:g}m.kmz"
        return Response(build_kmz(plan.wpml, cfg.flight), media_type="application/vnd.google-earth.kmz",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    @app.post("/api/route/plan")
    def route_plan(req: RouteRequest):
        """Waypoints, photo positions, spacing, time estimate and warnings for a drawing."""
        return _route(req).summary()

    @app.post("/api/route/kmz")
    def route_kmz(req: RouteRequest):
        return _kmz_response(req)

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
        out = dict(meta, points=r.get("points") or [], path=[], error=None)
        try:
            plan = _route(_request_from_doc(r))
            out["path"] = [list(p) for p in plan.path]
            s = plan.summary()
            out["summary"] = {k: s[k] for k in SUMMARY_KEYS}
        except HTTPException as exc:
            out["error"] = str(exc.detail)
        _path_cache[meta["id"]] = (key, out)
        return out

    @app.get("/api/routes")
    def routes_list(paths: bool = False):
        """Saved routes. With ``paths=1`` each also carries its flight path for map display."""
        routes = list_routes(workdir)
        return [_route_layer(r) for r in routes] if paths else routes

    def _save(req: RouteRequest, request: Request):
        plan = _route(req)                                   # only save something that can be flown
        s = plan.summary()
        try:
            return save_route(workdir, req.name, req.mode, req.points, req.model_dump(),
                              {k: s[k] for k in SUMMARY_KEYS}, req.overwrite, by=_who(request))
        except FileExistsError:
            raise HTTPException(409, f"a route called '{' '.join(req.name.split())}' is already saved in this mission")

    @app.post("/api/routes")
    def routes_save(req: RouteRequest, request: Request):
        """Save the drawing and settings into the mission so it can be reopened and edited."""
        return _save(req, request)

    @app.post("/api/routes/import")
    def routes_import(body: RouteImport, request: Request):
        """Add a route from a route file (downloaded from another mission, the hosted
        planner or the laptop app). The route is re-validated before it is stored."""
        doc = body.route
        if not isinstance(doc, dict) or doc.get("format") != FORMAT or not isinstance(doc.get("points"), list):
            raise HTTPException(400, "that is not a pointersearch route file")
        req = _request_from_doc(doc)
        req.overwrite = body.overwrite
        return _save(req, request)

    def _get(route_id: str) -> dict:
        try:
            r = load_route(workdir, route_id)
        except RouteFileError:
            r = None
        if r is None:
            raise HTTPException(404, "no such saved route")
        return r

    @app.get("/api/routes/{route_id}")
    def routes_get(route_id: str, download: bool = False):
        r = _get(route_id)
        if not download:
            return r
        return Response(json.dumps(r, indent=2), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{route_id}.route.json"'})

    @app.delete("/api/routes/{route_id}")
    def routes_delete(route_id: str, request: Request):
        guard(request)
        try:
            ok = delete_route(workdir, route_id)
        except RouteFileError:
            ok = False
        if not ok:
            raise HTTPException(404, "no such saved route")
        return {"deleted": route_id}

    @app.get("/api/routes/{route_id}/kmz")
    def routes_kmz(route_id: str):
        return _kmz_response(_request_from_doc(_get(route_id)))
