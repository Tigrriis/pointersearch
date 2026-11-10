"""Saved routes: the drawing and its settings, kept in the mission folder.

Each route is one JSON file in ``<mission>/routes/``. The file stores what the
person drew (the points) and chose (height, overlap, lenses), not the generated
waypoints, so it can be reopened in the planner and changed later, or edited by
hand. The waypoints are regenerated whenever it is loaded.

    {
      "format": "pointersearch-route", "version": 1,
      "id": "creek-line-north", "name": "Creek line north",
      "mode": "line",                       # line | area
      "points": [[-41.5630, 146.9990], ...],  # [lat, lon]
      "settings": {"height_m": 90, "speed_ms": 8, "lens": "wide",
                   "front_overlap": 0.25, "side_overlap": 0.25, "angle_deg": null,
                   "interval_m": null, "interval_s": null, "photo_lenses": "wide,ir"},
      "created": "...", "updated": "...",
      "summary": {...}                      # informational; recalculated on load
    }
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

FORMAT = "pointersearch-route"
VERSION = 1
SETTING_KEYS = ("height_m", "speed_ms", "lens", "front_overlap", "side_overlap", "angle_deg",
                "interval_m", "interval_s", "photo_lenses")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,49}$")


class RouteFileError(ValueError):
    pass


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:50].strip("-")
    return s or "route"


def routes_dir(workdir: Path) -> Path:
    return Path(workdir) / "routes"


def _path(workdir: Path, route_id: str) -> Path:
    if not _ID_RE.match(route_id):
        raise RouteFileError("bad route id")
    return routes_dir(workdir) / f"{route_id}.json"


def _read(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("format") != FORMAT or not isinstance(d.get("points"), list):
        return None
    d["id"] = path.stem
    d.setdefault("name", path.stem)
    d.setdefault("mode", "line")
    d.setdefault("settings", {})
    return d


def list_routes(workdir: Path) -> list[dict]:
    out = []
    d = routes_dir(workdir)
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            r = _read(p)
            if r is not None and _ID_RE.match(p.stem):
                out.append({k: r.get(k) for k in ("id", "name", "mode", "created", "updated", "summary", "updated_by")}
                           | {"n_points": len(r["points"])})
    out.sort(key=lambda r: r.get("updated") or "", reverse=True)
    return out


def load_route(workdir: Path, route_id: str) -> dict | None:
    p = _path(workdir, route_id)
    return _read(p) if p.is_file() else None


def save_route(workdir: Path, name: str, mode: str, points: list, settings: dict,
               summary: dict | None = None, overwrite: bool = False, by: str | None = None) -> dict:
    name = " ".join(name.split()) or "route"
    route_id = slug(name)
    p = _path(workdir, route_id)
    existing = _read(p) if p.is_file() else None
    if p.exists() and not overwrite:
        raise FileExistsError(route_id)
    now = datetime.now().isoformat(timespec="seconds")
    doc = {
        "format": FORMAT, "version": VERSION, "id": route_id, "name": name, "mode": mode,
        "points": [[float(a), float(b)] for a, b in points],
        "settings": {k: settings.get(k) for k in SETTING_KEYS},
        "created": (existing or {}).get("created") or now, "updated": now,
        "summary": summary or None,
    }
    if by:                                   # who saved it, where the app knows (hosted planner)
        doc["created_by"] = (existing or {}).get("created_by") or by
        doc["updated_by"] = by
    elif existing and existing.get("created_by"):
        doc["created_by"] = existing["created_by"]
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    tmp.replace(p)
    return doc


def delete_route(workdir: Path, route_id: str) -> bool:
    p = _path(workdir, route_id)
    if not p.is_file():
        return False
    p.unlink()
    return True
