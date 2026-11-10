"""Search routes drawn in the planner: a line to follow, or an area to sweep.

Both become a DJI waypoint route that takes photos at an even spacing along the
whole path. The spacing comes from the ground footprint of the chosen coverage
lens at the flying height and the requested overlap, or can be set directly as a
distance or a time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import CameraDefaults, FlightConfig, RouteConfig
from .georef import LocalFrame
from .wpml import ActionGroup, WpmlRoute, WpmlWaypoint, gimbal_rotate, take_photo

FULL_FRAME_DIAGONAL_MM = 43.266
# Frame shape and stored pixel width per lens (Matrice 4T published figures).
LENS = {
    "wide": {"aspect": (4, 3), "px_wide": 8064, "store": "wide"},
    "thermal": {"aspect": (5, 4), "px_wide": 640, "store": "ir"},
}
MIN_LEG_M = 0.5
TURN_S = 4.0                 # stop, yaw and accelerate at each waypoint
MAX_PREVIEW_PHOTOS = 2000
MAX_SWEEP_LINES = 400        # beyond this the drawing is not a single-flight search area
MAX_ROUTE_KM = 100.0
PERSON_WIDTH_M = 0.5


class RouteError(ValueError):
    """Raised when a route cannot be planned from the drawing and settings."""


def footprint_m(height_m: float, lens: str, camera: CameraDefaults) -> tuple[float, float]:
    """Ground footprint (across-track, along-track) in metres, gimbal straight down,
    frame long side across the direction of flight."""
    if lens not in LENS:
        raise RouteError(f"unknown coverage lens '{lens}' (use wide or thermal)")
    aw, ah = LENS[lens]["aspect"]
    f35 = getattr(camera, lens)
    diag = math.hypot(aw, ah)
    across = height_m * (FULL_FRAME_DIAGONAL_MM * aw / diag) / f35
    along = height_m * (FULL_FRAME_DIAGONAL_MM * ah / diag) / f35
    return across, along


def _dedupe(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for p in pts:
        if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) >= MIN_LEG_M:
            out.append(p)
    return out


def sweep_area(polygon_ne: list[tuple[float, float]], spacing_m: float,
               angle_deg: float | None = None) -> tuple[list[tuple[float, float]], float, float]:
    """Back-and-forth lines covering a polygon. Points are (north, east) metres.

    ``spacing_m`` is the widest acceptable gap between lines. The area is cut into
    equal strips no wider than that, with one line down the middle of each, so the
    outer lines sit half a strip inside the boundary instead of on it.

    ``angle_deg`` is the bearing of the sweep lines, clockwise from north. When
    None the longest edge of the polygon is used, which keeps the number of turns
    low. Returns (path, angle used, spacing used).
    """
    if len(polygon_ne) < 3:
        raise RouteError("an area needs at least three corners")
    if spacing_m <= 0:
        raise RouteError("line spacing must be positive")
    if angle_deg is None:
        best = 0.0
        angle_deg = 0.0
        for a, b in zip(polygon_ne, polygon_ne[1:] + polygon_ne[:1]):
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            if length > best:
                best = length
                angle_deg = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0
    th = math.radians(angle_deg)
    c, s = math.cos(th), math.sin(th)
    # u runs along the sweep lines, v across them.
    uv = [(n * c + e * s, -n * s + e * c) for n, e in polygon_ne]
    vmin, vmax = min(v for _, v in uv), max(v for _, v in uv)
    extent = vmax - vmin
    n_lines = max(1, math.ceil(extent / spacing_m - 1e-9))
    if n_lines > MAX_SWEEP_LINES:
        raise RouteError(f"the area is far too large ({extent / 1000:.0f} km across the sweep lines); "
                         "zoom in and draw the search area at its real size")
    strip = extent / n_lines
    first = vmin + strip / 2.0
    edges = list(zip(uv, uv[1:] + uv[:1]))
    path_uv: list[tuple[float, float]] = []
    flip = False
    for i in range(n_lines):
        v = first + i * strip
        xs = []
        for (pu, pv), (qu, qv) in edges:
            if (pv - v) * (qv - v) < 0:
                xs.append(pu + (v - pv) / (qv - pv) * (qu - pu))
            elif pv == v and qv != v:
                xs.append(pu)
        if len(xs) < 2:
            continue
        u0, u1 = min(xs), max(xs)
        if u1 - u0 < MIN_LEG_M:
            continue
        path_uv += [(u1, v), (u0, v)] if flip else [(u0, v), (u1, v)]
        flip = not flip
    if len(path_uv) < 2:
        raise RouteError("the area is too small for a sweep at this spacing")
    return [(u * c - v * s, u * s + v * c) for u, v in path_uv], angle_deg, strip


def path_length(path_ne: list[tuple[float, float]]) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path_ne, path_ne[1:]))


def photo_positions(path_ne: list[tuple[float, float]], interval_m: float, limit: int = MAX_PREVIEW_PHOTOS):
    """Points every ``interval_m`` along the path, starting at the first waypoint."""
    out = [path_ne[0]]
    carry = 0.0
    for a, b in zip(path_ne, path_ne[1:]):
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        d = interval_m - carry
        while d <= seg and len(out) < limit:
            t = d / seg
            out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
            d += interval_m
        carry = (carry + seg) % interval_m
    return out


@dataclass
class RoutePlan:
    mode: str
    name: str
    path: list[tuple[float, float]]            # (lat, lon) waypoints
    photos: list[tuple[float, float]]          # (lat, lon) preview, may be truncated
    height_m: float
    speed_ms: float
    lens: str
    photo_lenses: str
    interval_m: float
    interval_s: float
    trigger: str
    track_spacing_m: float | None
    sweep_angle_deg: float | None
    footprint_across_m: float
    footprint_along_m: float
    gsd_cm: float
    n_photos: int
    distance_m: float
    duration_s: float
    warnings: list[str] = field(default_factory=list)
    wpml: WpmlRoute | None = None

    def summary(self) -> dict:
        return {
            "mode": self.mode, "name": self.name,
            "path": [list(p) for p in self.path], "photos": [list(p) for p in self.photos],
            "waypoints": len(self.path), "height_m": self.height_m, "speed_ms": self.speed_ms,
            "lens": self.lens, "photo_lenses": self.photo_lenses,
            "interval_m": round(self.interval_m, 1), "interval_s": round(self.interval_s, 1),
            "trigger": self.trigger,
            "track_spacing_m": None if self.track_spacing_m is None else round(self.track_spacing_m, 1),
            "sweep_angle_deg": None if self.sweep_angle_deg is None else round(self.sweep_angle_deg, 1),
            "footprint_m": [round(self.footprint_across_m, 1), round(self.footprint_along_m, 1)],
            "gsd_cm": round(self.gsd_cm, 2),
            "px_across_person": round(PERSON_WIDTH_M * 100.0 / self.gsd_cm, 1),
            "n_photos": self.n_photos, "distance_m": round(self.distance_m),
            "duration_min": round(self.duration_s / 60.0, 1), "warnings": self.warnings,
        }


def plan_route(mode: str, points: list[tuple[float, float]], cfg: RouteConfig, flight: FlightConfig,
               camera: CameraDefaults, *, height_m: float | None = None, speed_ms: float | None = None,
               lens: str | None = None, front_overlap: float | None = None, side_overlap: float | None = None,
               angle_deg: float | None = None, interval_m: float | None = None, interval_s: float | None = None,
               photo_lenses: str | None = None, name: str = "search") -> RoutePlan:
    """Plan a route from a drawing. ``points`` are (lat, lon): the vertices of the
    line (mode "line") or the corners of the search area (mode "area")."""
    height = cfg.height_m if height_m is None else float(height_m)
    speed = cfg.speed_ms if speed_ms is None else float(speed_ms)
    lens = lens or cfg.coverage_lens
    front = cfg.front_overlap if front_overlap is None else float(front_overlap)
    side = cfg.side_overlap if side_overlap is None else float(side_overlap)
    lenses = (photo_lenses or cfg.photo_lenses).replace(" ", "")

    if mode not in ("line", "area"):
        raise RouteError("mode must be line or area")
    if height <= 0:
        raise RouteError("height must be positive")
    if height > flight.max_height_m:
        raise RouteError(f"height ({height:g} m) exceeds the {flight.max_height_m:g} m ceiling (flight.max_height_m)")
    if not 1.0 <= speed <= 15.0:
        raise RouteError("speed must be between 1 and 15 m/s")
    if not (0 <= front < 0.95 and 0 <= side < 0.95):
        raise RouteError("overlap must be between 0 and 95 %")
    if not lenses:
        raise RouteError("choose at least one lens to store")
    if interval_m is not None and interval_s is not None:
        raise RouteError("give a distance interval or a time interval, not both")

    across, along = footprint_m(height, lens, camera)
    frame = LocalFrame(points[0][0], points[0][1]) if points else None
    pts_ne = _dedupe([frame.to_ne(lat, lon) for lat, lon in points]) if frame else []

    track_spacing = None
    used_angle = None
    if mode == "line":
        if len(pts_ne) < 2:
            raise RouteError("draw at least two points for a line route")
        path_ne = pts_ne
    else:
        if len(pts_ne) >= 2 and math.hypot(pts_ne[0][0] - pts_ne[-1][0], pts_ne[0][1] - pts_ne[-1][1]) < MIN_LEG_M:
            pts_ne = pts_ne[:-1]            # closed ring given
        if len(pts_ne) < 3:
            raise RouteError("draw at least three corners for a search area")
        path_ne, used_angle, track_spacing = sweep_area(pts_ne, across * (1.0 - side), angle_deg)

    distance = path_length(path_ne)
    if distance < 1.0:
        raise RouteError("the route is too short")
    if distance > MAX_ROUTE_KM * 1000:
        raise RouteError(f"the route is {distance / 1000:.0f} km long, far beyond one flight; "
                         "zoom in and draw it at its real size, or split it")

    if interval_s is not None:
        if interval_s <= 0:
            raise RouteError("time interval must be positive")
        trigger, param = "multipleTiming", float(interval_s)
        step_m, step_s = interval_s * speed, float(interval_s)
    else:
        step_m = float(interval_m) if interval_m is not None else along * (1.0 - front)
        if step_m <= 0:
            raise RouteError("distance interval must be positive")
        trigger, param = "multipleDistance", step_m
        step_s = step_m / speed

    n_photos = int(distance // step_m) + 1
    duration = distance / speed + TURN_S * max(0, len(path_ne) - 2) + 2 * height / 4.0

    warnings = []
    if step_s < cfg.min_photo_interval_s:
        ok_speed = step_m / cfg.min_photo_interval_s
        warnings.append(f"photos would be {step_s:.1f} s apart, faster than the assumed "
                        f"{cfg.min_photo_interval_s:g} s the camera can sustain; slow to {ok_speed:.1f} m/s "
                        "or widen the spacing")
    if step_m > along:
        warnings.append(f"photo spacing ({step_m:.0f} m) is longer than the {lens} frame ({along:.0f} m): "
                        "there will be gaps between photos along the route")
    store = LENS[lens]["store"]
    if store not in lenses.split(","):
        warnings.append(f"spacing is set for the {lens} lens but '{store}' is not among the stored lenses")
    if duration / 60.0 > flight.max_flight_min:
        warnings.append(f"estimated {duration / 60:.0f} min exceeds the {flight.max_flight_min:g} min budget; "
                        "split the area or route across flights")
    if len(path_ne) > 300:
        warnings.append(f"{len(path_ne)} waypoints is a large route; Pilot 2 may be slow to load it")
    warnings.append("the estimate excludes the flight from take-off to the first waypoint and back")
    warnings.append("heights are relative to the take-off point, not the ground under the aircraft; "
                    "check terrain and obstacles along the route before flying")

    path_ll = [frame.to_latlon(n, e) for n, e in path_ne]
    photos_ll = [frame.to_latlon(n, e) for n, e in photo_positions(path_ne, step_m)]
    last = len(path_ll) - 1
    wps = [WpmlWaypoint(lat, lon, height, speed) for lat, lon in path_ll]
    wps[0].groups = [
        ActionGroup([gimbal_rotate(cfg.gimbal_pitch_deg, flight.payload_position)], "reachPoint", None, 0, 0),
        ActionGroup([take_photo(lenses, "", flight.payload_position)], trigger, param, 0, last),
    ]
    wpml = WpmlRoute(wps, height, speed, distance, duration, lenses, cfg.gimbal_pitch_deg)
    gsd_cm = across / LENS[lens]["px_wide"] * 100.0
    return RoutePlan(mode, name, path_ll, photos_ll, height, speed, lens, lenses, step_m, step_s, trigger,
                     track_spacing, used_angle, across, along, gsd_cm, n_photos, distance, duration, warnings, wpml)
