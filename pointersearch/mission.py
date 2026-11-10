"""Follow-up waypoint missions for DJI Pilot 2 (WPML route in a KMZ).

Objects tagged *follow-up* in the review map become a route the aircraft can fly:
climb to transit height, fly to each object, descend to inspection height, point
the gimbal down, take stills on the chosen lenses, climb back, move on.

Heights are relative to the take-off point. The file itself is written by
:mod:`pointersearch.wpml`; always open an exported route in Pilot 2 and check it
on the map before flying it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import FlightConfig, FollowupConfig
from .georef import LocalFrame
from .wpml import ActionGroup, WpmlRoute, WpmlWaypoint, gimbal_rotate, hover, take_photo

MIN_STACK_SEPARATION_M = 1.0   # below this, transit and inspection are treated as one height
PHOTO_OVERHEAD_S = 6.0         # gimbal move + capture + write, per object


class MissionError(ValueError):
    """Raised when a mission cannot be built safely from the given settings."""


@dataclass
class MissionPoint:
    object_id: int
    lat: float
    lon: float
    label: str = ""


@dataclass
class Waypoint:
    lat: float
    lon: float
    height_m: float
    speed_ms: float
    object_id: int | None = None
    inspect: bool = False


@dataclass
class MissionPlan:
    points: list[MissionPoint]
    waypoints: list[Waypoint]
    transit_height_m: float
    inspect_height_m: float
    distance_m: float
    duration_s: float
    home: tuple[float, float] | None
    warnings: list[str] = field(default_factory=list)
    wpml: WpmlRoute | None = None

    def summary(self) -> dict:
        return {
            "objects": len(self.points),
            "waypoints": len(self.waypoints),
            "transit_height_m": self.transit_height_m,
            "inspect_height_m": self.inspect_height_m,
            "distance_m": round(self.distance_m),
            "duration_min": round(self.duration_s / 60.0, 1),
            "home": list(self.home) if self.home else None,
            "order": [{"seq": i + 1, "object_id": p.object_id, "lat": p.lat, "lon": p.lon, "label": p.label}
                      for i, p in enumerate(self.points)],
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# Route ordering
# ---------------------------------------------------------------------------
def order_points(points: list[MissionPoint], home: tuple[float, float] | None) -> list[MissionPoint]:
    """Nearest-neighbour tour from ``home`` improved with 2-opt (tour returns home)."""
    if len(points) < 2:
        return list(points)
    origin = home or (points[0].lat, points[0].lon)
    frame = LocalFrame(*origin)
    xy = [frame.to_ne(p.lat, p.lon) for p in points]
    start = (0.0, 0.0) if home else xy[0]

    def d(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1])

    remaining = list(range(len(points)))
    order: list[int] = []
    cur = start
    while remaining:
        nxt = min(remaining, key=lambda i: d(cur, xy[i]))
        remaining.remove(nxt)
        order.append(nxt)
        cur = xy[nxt]

    def tour_len(o):
        pts = [start] + [xy[i] for i in o] + ([start] if home else [])
        return sum(d(pts[i], pts[i + 1]) for i in range(len(pts) - 1))

    improved = True
    best = tour_len(order)
    while improved and len(order) <= 200:
        improved = False
        for i in range(len(order) - 1):
            for j in range(i + 1, len(order)):
                cand = order[:i] + order[i:j + 1][::-1] + order[j + 1:]
                length = tour_len(cand)
                if length < best - 1e-6:
                    order, best, improved = cand, length, True
    return [points[i] for i in order]


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
def plan_mission(points: list[MissionPoint], cfg: FollowupConfig, flight: FlightConfig | None = None,
                 transit_height_m: float | None = None, inspect_height_m: float | None = None,
                 home: tuple[float, float] | None = None) -> MissionPlan:
    flight = flight or FlightConfig()
    transit = cfg.transit_height_m if transit_height_m is None else float(transit_height_m)
    inspect = cfg.inspect_height_m if inspect_height_m is None else float(inspect_height_m)
    if not points:
        raise MissionError("no objects are tagged for follow-up")
    if inspect <= 0 or transit <= 0:
        raise MissionError("heights must be positive")
    if inspect > transit:
        raise MissionError(f"inspection height ({inspect:g} m) cannot be above transit height ({transit:g} m)")
    if transit > flight.max_height_m:
        raise MissionError(f"transit height ({transit:g} m) exceeds the {flight.max_height_m:g} m ceiling "
                           "(flight.max_height_m)")

    ordered = order_points(points, home)
    stacked = (transit - inspect) >= MIN_STACK_SEPARATION_M
    wps: list[Waypoint] = []
    prev = home
    for p in ordered:
        if stacked:
            a_lat, a_lon = p.lat, p.lon
            if cfg.approach_offset_m > 0 and prev is not None:
                a_lat, a_lon = _offset_towards((p.lat, p.lon), prev, cfg.approach_offset_m)
            wps.append(Waypoint(a_lat, a_lon, transit, cfg.transit_speed_ms, p.object_id))
            wps.append(Waypoint(p.lat, p.lon, inspect, cfg.vertical_speed_ms, p.object_id, inspect=True))
            wps.append(Waypoint(a_lat, a_lon, transit, cfg.transit_speed_ms, p.object_id))
        else:
            wps.append(Waypoint(p.lat, p.lon, inspect, cfg.transit_speed_ms, p.object_id, inspect=True))
        prev = (p.lat, p.lon)

    # Distance and time estimate (take-off climb, route, return).
    frame = LocalFrame(ordered[0].lat, ordered[0].lon)
    horiz = 0.0
    seq = ([home] if home else []) + [(p.lat, p.lon) for p in ordered] + ([home] if home else [])
    for a, b in zip(seq, seq[1:]):
        (n1, e1), (n2, e2) = frame.to_ne(*a), frame.to_ne(*b)
        horiz += math.hypot(n2 - n1, e2 - e1)
    vert_per_obj = 2 * (transit - inspect) if stacked else 0.0
    duration = (horiz / max(cfg.transit_speed_ms, 0.1)
                + len(ordered) * (vert_per_obj / max(cfg.vertical_speed_ms, 0.1) + cfg.hover_s + PHOTO_OVERHEAD_S)
                + 2 * transit / 4.0)            # climb out and descend at ~4 m/s
    distance = horiz + len(ordered) * vert_per_obj

    warnings = []
    if duration / 60.0 > flight.max_flight_min:
        warnings.append(f"estimated {duration / 60:.0f} min exceeds the {flight.max_flight_min:g} min budget; "
                        "split the follow-ups across two flights")
    if home is None:
        warnings.append("no home position given; route order starts at the first object and the "
                        "time estimate excludes the flight out and back")
    warnings.append("heights are relative to the take-off point, not the ground under the aircraft; "
                    "check terrain and obstacles along the route before flying")
    wpml_wps = []
    for i, w in enumerate(wps):
        groups = []
        if w.inspect:
            actions = [gimbal_rotate(cfg.gimbal_pitch_deg, flight.payload_position)]
            if cfg.hover_s > 0:
                actions.append(hover(cfg.hover_s))
            actions.append(take_photo(cfg.photo_lenses, f"PS{w.object_id}", flight.payload_position))
            groups.append(ActionGroup(actions, "reachPoint", None, i, i))
        wpml_wps.append(WpmlWaypoint(w.lat, w.lon, w.height_m, w.speed_ms, groups))
    wpml = WpmlRoute(wpml_wps, transit, cfg.transit_speed_ms, distance, duration,
                     cfg.photo_lenses, cfg.gimbal_pitch_deg)
    return MissionPlan(ordered, wps, transit, inspect, distance, duration, home, warnings, wpml)


def _offset_towards(p: tuple[float, float], towards: tuple[float, float], dist_m: float) -> tuple[float, float]:
    frame = LocalFrame(*p)
    n, e = frame.to_ne(*towards)
    length = math.hypot(n, e)
    if length < 1e-6:
        return p
    k = min(dist_m, length / 2) / length
    return frame.to_latlon(n * k, e * k)


# ---------------------------------------------------------------------------
# From a mission working directory
# ---------------------------------------------------------------------------
def points_from_store(store, status: str = "followup") -> list[MissionPoint]:
    return [MissionPoint(g["id"], g["lat"], g["lon"], "+".join(g["detectors"]))
            for g in store.list_groups(status=status)]


def default_home(store) -> tuple[float, float] | None:
    """Aircraft position in the earliest frame, as a stand-in for the take-off point."""
    for img in store.list_images():
        if img.get("lat") is not None and img.get("lon") is not None:
            return (img["lat"], img["lon"])
    return None
