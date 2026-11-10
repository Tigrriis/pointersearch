"""The hosted planner application."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__
from ..config import Config
from ..review.missions import (NAME_RE, STATIC_DIR, MissionDispatcher, allow_all, clean_name, is_mission,
                               mission_url)
from ..review.planner_api import add_planner_api, route_defaults
from ..saved_routes import list_routes
from .auth import SESSION_COOKIE, SESSION_DAYS, AuthError, Users

log = logging.getLogger("pointersearch.web")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PUBLIC_PATHS = {"/login", "/healthz", "/api/login", "/api/login-info", "/favicon.ico"}
# what someone who still has to choose their own password may reach
FIRST_LOGIN_PATHS = {"/account", "/api/me", "/api/app", "/api/account/password", "/api/account/name", "/api/logout"}
MISSION_META = "mission.json"


class LoginBody(BaseModel):
    email: str
    password: str


class NameBody(BaseModel):
    name: str


class PasswordBody(BaseModel):
    current: str
    new: str


class NewUser(BaseModel):
    email: str
    name: str
    password: str
    role: str = "member"


class UserPatch(BaseModel):
    name: str | None = None
    role: str | None = None
    active: bool | None = None
    password: str | None = None


class NewMission(BaseModel):
    name: str


def create_planner_app(workdir: Path, name: str) -> FastAPI:
    """One mission's planner: the same page and API as the laptop app, without imagery."""
    cfg = Config.load(workdir)
    app = FastAPI(title="pointersearch planner", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/")
    def index():
        return RedirectResponse("plan")

    @app.get("/api/summary")
    def summary():
        return {"mission": name, "hosted": True, "tiles": False, "images": 0, "images_ok": 0,
                "routes": len(list_routes(workdir)), "route": route_defaults(cfg)}

    @app.get("/api/overlays")
    def overlays():
        return []

    @app.get("/api/images")
    def images():
        return []

    add_planner_api(app, workdir, cfg, allow_all)
    return app


def _is_api(path: str) -> bool:
    return path.startswith("/api/") or "/api/" in path


def create_web_app(data_dir: str | Path, admin_email: str | None = None,
                   admin_password: str | None = None, admin_name: str | None = None) -> FastAPI:
    data_dir = Path(data_dir).resolve()
    root = data_dir / "missions"
    root.mkdir(parents=True, exist_ok=True)
    users = Users(data_dir / "users.db")
    try:
        created = users.bootstrap_admin(admin_email, admin_password, admin_name or "Administrator")
        if created:
            log.warning("created the first administrator login")
    except AuthError as exc:
        log.error("could not create the first administrator: %s", exc)
    if not users.count():
        log.error("no logins exist: set POINTERSEARCH_ADMIN_EMAIL and POINTERSEARCH_ADMIN_PASSWORD and restart")

    app = FastAPI(title="pointersearch", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.users = users
    missions = MissionDispatcher(root, create_planner_app)

    # ---- every request passes through here ---------------------------------
    @app.middleware("http")
    async def gate(request: Request, call_next):
        path = request.url.path
        if request.method not in SAFE_METHODS:
            # changes must come from this site's own pages (the cookie is also SameSite=Lax)
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            response = await call_next(request)
        else:
            user = users.user_for_token(request.cookies.get(SESSION_COOKIE))
            if user is None:
                if _is_api(path):
                    return JSONResponse({"detail": "sign in to continue", "code": "signed_out"}, status_code=401)
                nxt = quote(path + (f"?{request.url.query}" if request.url.query else ""), safe="/")
                return RedirectResponse(f"/login?next={nxt}", status_code=303)
            if user["must_change"] and path not in FIRST_LOGIN_PATHS:
                if _is_api(path):
                    return JSONResponse({"detail": "choose your own password first", "code": "password_change_required"},
                                        status_code=403)
                return RedirectResponse("/account?first=1", status_code=303)
            request.state.user = user
            response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if _is_api(path) or path in ("/", "/account", "/users", "/login"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif "/static/" in path or response.headers.get("content-type", "").startswith("text/html"):
            response.headers.setdefault("Cache-Control", "no-cache")     # an update is never hidden by a stale page
        return response

    def me(request: Request) -> dict:
        return request.state.user

    def admin(request: Request) -> dict:
        user = me(request)
        if user["role"] != "admin":
            raise HTTPException(403, "only an administrator can do that")
        return user

    def brief(user: dict) -> dict:
        return {k: user[k] for k in ("id", "email", "name", "role", "must_change")}

    # ---- pages ----------------------------------------------------------------
    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/login")
    def login_page(request: Request):
        if users.user_for_token(request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse("/", status_code=303)
        return FileResponse(STATIC_DIR / "login.html")

    @app.get("/")
    def home():
        return FileResponse(STATIC_DIR / "home.html")

    @app.get("/account")
    def account_page():
        return FileResponse(STATIC_DIR / "account.html")

    @app.get("/users")
    def users_page(request: Request):
        if me(request)["role"] != "admin":
            return RedirectResponse("/", status_code=303)
        return FileResponse(STATIC_DIR / "users.html")

    # ---- signing in and out -----------------------------------------------------
    @app.get("/api/login-info")
    def login_info():
        return {"setup_needed": users.count() == 0}

    @app.post("/api/login")
    def login(body: LoginBody, request: Request):
        client = request.client.host if request.client else ""
        try:
            token, user = users.login(body.email, body.password, client)
        except AuthError as exc:
            status = 429 if "too many attempts" in str(exc) else 401
            raise HTTPException(status, str(exc))
        resp = JSONResponse({"user": brief(user)})
        resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", path="/")
        return resp

    @app.post("/api/logout")
    def logout(request: Request):
        users.logout(request.cookies.get(SESSION_COOKIE))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @app.get("/api/me")
    def whoami(request: Request):
        return brief(me(request))

    @app.get("/api/app")
    def app_info(request: Request):
        return {"app": "pointersearch", "version": __version__, "hosted": True, "user": brief(me(request))}

    @app.post("/api/account/name")
    def change_name(body: NameBody, request: Request):
        try:
            return brief(users.update(me(request)["id"], name=body.name))
        except AuthError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/account/password")
    def change_password(body: PasswordBody, request: Request):
        try:
            users.change_own_password(me(request)["id"], body.current, body.new,
                                      keep_token=request.cookies.get(SESSION_COOKIE))
        except AuthError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}

    # ---- people (administrators) ------------------------------------------------
    @app.get("/api/users")
    def list_users(request: Request):
        admin(request)
        return users.list()

    @app.post("/api/users")
    def add_user(body: NewUser, request: Request):
        admin(request)
        try:
            return users.create(body.email, body.name, body.password, body.role, must_change=True)
        except AuthError as exc:
            raise HTTPException(400, str(exc))

    @app.patch("/api/users/{user_id}")
    def edit_user(user_id: int, body: UserPatch, request: Request):
        actor = admin(request)
        if user_id == actor["id"] and (body.active is False or (body.role is not None and body.role != "admin")):
            raise HTTPException(400, "you cannot remove your own administrator access; ask another administrator")
        if user_id == actor["id"] and body.password is not None:
            raise HTTPException(400, "change your own password on the account page")
        try:
            return users.update(user_id, name=body.name, role=body.role, active=body.active, password=body.password)
        except AuthError as exc:
            raise HTTPException(404 if "no such person" in str(exc) else 400, str(exc))

    # ---- missions -----------------------------------------------------------------
    def card(path: Path) -> dict:
        routes = list_routes(path)
        meta = {}
        try:
            meta = json.loads((path / MISSION_META).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        stamps = [p.stat().st_mtime for p in [path / Config.FILENAME, *(path / "routes").glob("*.json")] if p.exists()]
        mtime = max(stamps, default=0)
        return {"name": path.name, "url": mission_url(path.name), "routes": len(routes),
                "route_list": [{"id": r["id"], "name": r["name"], "mode": r["mode"], "summary": r.get("summary")}
                               for r in routes],
                "created_by": meta.get("created_by"), "modified_ts": mtime,
                "modified": datetime.fromtimestamp(mtime).isoformat(timespec="minutes") if mtime else None}

    @app.get("/api/missions")
    def list_missions():
        out = [card(p) for p in sorted(root.iterdir()) if is_mission(p) and NAME_RE.match(p.name)]
        out.sort(key=lambda m: m["modified_ts"], reverse=True)
        return {"root": None, "missions": out}

    @app.post("/api/missions")
    def create_mission(body: NewMission, request: Request):
        try:
            name = clean_name(body.name)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if any(p.name.lower() == name.lower() for p in root.iterdir()):
            raise HTTPException(409, f"a mission called '{name}' already exists")
        path = root / name
        path.mkdir()
        Config().save(path)
        (path / MISSION_META).write_text(json.dumps(
            {"created_by": me(request)["name"], "created_at": datetime.now().isoformat(timespec="seconds")}, indent=2),
            encoding="utf-8")
        return {"name": name, "url": mission_url(name)}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/m", missions)
    return app
