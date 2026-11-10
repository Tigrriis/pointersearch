"""The app: a start screen over a folder of missions.

Each mission is a sub-folder of the missions root with its own config, database
and imagery. The hub lists them, creates new ones, and serves each mission's
review map and route planner under ``/m/<mission name>/``.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.responses import JSONResponse, RedirectResponse

from .. import __version__
from ..config import Config
from ..saved_routes import list_routes
from ..store import Store
from .server import STATIC_DIR, create_app, require_local

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,59}$")
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


class NewMission(BaseModel):
    name: str


def clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not name:
        raise ValueError("give the mission a name")
    reserved = name.split(".")[0].strip().upper() in WINDOWS_RESERVED
    if not NAME_RE.match(name) or name.endswith(".") or ".." in name or reserved:
        raise ValueError("use letters, numbers, spaces, dashes, dots and underscores (up to 60 characters)")
    return name


def is_mission(path: Path) -> bool:
    return path.is_dir() and ((path / Config.FILENAME).exists() or (path / "pointersearch.db").exists())


class MissionDispatcher:
    """ASGI app mounted at /m: routes /m/<name>/... to that mission's own app."""

    def __init__(self, root: Path, tiles_dir=None):
        self.root = root
        self.tiles_dir = tiles_dir
        self._apps: dict[str, FastAPI] = {}
        self._lock = threading.Lock()

    def get(self, name: str) -> FastAPI | None:
        with self._lock:
            app = self._apps.get(name)
            if app is None:
                path = self.root / name
                if not NAME_RE.match(name) or not is_mission(path):
                    return None
                app = self._apps[name] = create_app(path, self.tiles_dir, name)
            return app

    def store(self, name: str) -> Store | None:
        app = self.get(name)
        return app.state.store if app else None

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return
        root_path = scope.get("root_path", "")
        path = scope["path"]
        new_style = bool(root_path) and path.startswith(root_path)      # Starlette >= 0.33 keeps the full path
        rest = path[len(root_path):] if new_style else path
        name, sep, _ = rest.lstrip("/").partition("/")
        app = self.get(name) if name else None
        if app is None:
            await JSONResponse({"detail": "no such mission"}, status_code=404)(scope, receive, send)
            return
        if not sep:                                                      # /m/name -> /m/name/
            await RedirectResponse(quote(f"{root_path}/{name}/"), status_code=307)(scope, receive, send)
            return
        child = dict(scope)
        child["root_path"] = f"{root_path}/{name}"
        if not new_style:
            child["path"] = rest[len(name) + 1:] or "/"
        await app(child, receive, send)


def mission_card(name: str, path: Path, store: Store | None) -> dict:
    info = {"name": name, "url": "/m/" + quote(name) + "/", "images": 0, "objects": {}, "n_objects": 0, "routes": 0,
            "importing": None}
    try:
        mtime = max((p.stat().st_mtime for p in (path / "pointersearch.db", path / Config.FILENAME) if p.exists()),
                    default=path.stat().st_mtime)
        info["modified"] = datetime.fromtimestamp(mtime).isoformat(timespec="minutes")
        info["modified_ts"] = mtime
    except OSError:
        info["modified"], info["modified_ts"] = None, 0
    if store is not None:
        try:
            s = store.summary()
            info.update(images=s["images_ok"], objects=s["groups"], n_objects=s["n_groups"])
            info["routes"] = len(list_routes(path))
        except Exception:
            pass
    return info


def create_hub(root: str | Path, tiles_dir: str | Path | None = None) -> FastAPI:
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="pointersearch", docs_url=None, redoc_url=None)
    missions = MissionDispatcher(root, tiles_dir)
    app.state.missions = missions

    @app.get("/")
    def home():
        return FileResponse(STATIC_DIR / "home.html")

    @app.get("/api/app")
    def app_info():
        return {"app": "pointersearch", "version": __version__, "root": str(root)}

    @app.get("/api/missions")
    def list_missions():
        out = []
        for p in sorted(root.iterdir()):
            if is_mission(p) and NAME_RE.match(p.name):
                card = mission_card(p.name, p, missions.store(p.name))
                sub = missions.get(p.name)
                job = getattr(sub.state, "ingest", None) if sub else None
                if job is not None and job.status()["state"] in ("running", "watching"):
                    card["importing"] = job.status()["state"]
                out.append(card)
        out.sort(key=lambda m: m["modified_ts"], reverse=True)
        return {"root": str(root), "missions": out}

    @app.post("/api/missions")
    def create_mission(body: NewMission, request: Request):
        require_local(request)
        try:
            name = clean_name(body.name)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        path = root / name
        if any(p.name.lower() == name.lower() for p in root.iterdir()):
            raise HTTPException(409, f"a mission or folder called '{name}' already exists")
        path.mkdir()
        Config().save(path)
        Store(path).close()
        return {"name": name, "url": "/m/" + quote(name) + "/"}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/m", missions)
    return app


def run_app(root: str | Path, host: str = "127.0.0.1", port: int = 8008, tiles_dir=None,
            open_browser: bool = True) -> int:
    """Start the app, or just open the browser if it is already running on that port."""
    import json
    import urllib.request
    import webbrowser

    import uvicorn

    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/"
    try:
        with urllib.request.urlopen(url + "api/app", timeout=1.5) as r:
            if json.load(r).get("app") == "pointersearch":
                print(f"pointersearch is already running at {url}")
                if open_browser:
                    webbrowser.open(url)
                return 0
    except Exception:
        pass
    if open_browser:
        threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(url)), daemon=True).start()
    print(f"pointersearch: {url}\nmissions folder: {Path(root).resolve()}\nKeep this window open. Close it to stop.")
    uvicorn.run(create_hub(root, tiles_dir), host=host, port=port, log_level="warning")
    return 0
