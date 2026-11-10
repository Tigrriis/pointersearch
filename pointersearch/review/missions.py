"""Mission folders and request guards shared by the laptop app and the hosted planner.

Nothing here imports the image-processing stack.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import JSONResponse, RedirectResponse

from ..config import Config

STATIC_DIR = Path(__file__).parent / "static"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,59}$")
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
LOCAL_CLIENTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def require_local(request: Request):
    """Actions that touch the host's disk are only for the person at the laptop,
    not for other devices on the hotspot."""
    host = request.client.host if request.client else ""
    if host not in LOCAL_CLIENTS:
        raise HTTPException(403, "this action is only available on the computer running pointersearch")


def allow_all(request: Request):
    """Guard for the hosted app, where every request has already passed the login check."""


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


def mission_url(name: str) -> str:
    return "/m/" + quote(name) + "/"


class MissionDispatcher:
    """ASGI app mounted at /m: routes /m/<name>/... to that mission's own app."""

    def __init__(self, root: Path, factory):
        self.root = root
        self.factory = factory            # (mission folder, mission name) -> ASGI app
        self._apps: dict[str, FastAPI] = {}
        self._lock = threading.Lock()

    def get(self, name: str) -> FastAPI | None:
        with self._lock:
            app = self._apps.get(name)
            if app is None:
                path = self.root / name
                if not NAME_RE.match(name) or not is_mission(path):
                    return None
                app = self._apps[name] = self.factory(path, name)
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
