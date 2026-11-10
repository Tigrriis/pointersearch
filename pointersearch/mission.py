"""Follow-up waypoint missions for DJI Pilot 2 (WPML route in a KMZ).

Objects tagged *follow-up* in the review map become a route the aircraft can fly:
climb to transit height, fly to each object, descend to inspection height, point
the gimbal down, take stills on the chosen lenses, climb back, move on.

A KMZ holds two files that describe the same route:

* ``wpmz/template.kml``  - what Pilot 2 shows and lets the pilot edit
* ``wpmz/waylines.wpml`` - what the aircraft executes

Heights are relative to the take-off point. The aircraft/payload codes written
into the file come from :class:`pointersearch.config.FollowupConfig`; always
open an exported route in Pilot 2 and check it on the map before flying it.
"""

from __future__ import annotations

import io
import math
import time
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from .config import FollowupConfig
from .georef import LocalFrame

KML_NS = "http://www.opengis.net/kml/2.2"
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
def plan_mission(points: list[MissionPoint], cfg: FollowupConfig,
                 transit_height_m: float | None = None, inspect_height_m: float | None = None,
                 home: tuple[float, float] | None = None) -> MissionPlan:
    transit = cfg.transit_height_m if transit_height_m is None else float(transit_height_m)
    inspect = cfg.inspect_height_m if inspect_height_m is None else float(inspect_height_m)
    if not points:
        raise MissionError("no objects are tagged for follow-up")
    if inspect <= 0 or transit <= 0:
        raise MissionError("heights must be positive")
    if inspect > transit:
        raise MissionError(f"inspection height ({inspect:g} m) cannot be above transit height ({transit:g} m)")
    if transit > cfg.max_height_m:
        raise MissionError(f"transit height ({transit:g} m) exceeds the {cfg.max_height_m:g} m ceiling "
                           "(followup.max_height_m)")

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
    if duration / 60.0 > cfg.max_flight_min:
        warnings.append(f"estimated {duration / 60:.0f} min exceeds the {cfg.max_flight_min:g} min budget; "
                        "split the follow-ups across two flights")
    if home is None:
        warnings.append("no home position given; route order starts at the first object and the "
                        "time estimate excludes the flight out and back")
    warnings.append("heights are relative to the take-off point, not the ground under the aircraft; "
                    "check terrain and obstacles along the route before flying")
    return MissionPlan(ordered, wps, transit, inspect, distance, duration, home, warnings)


def _offset_towards(p: tuple[float, float], towards: tuple[float, float], dist_m: float) -> tuple[float, float]:
    frame = LocalFrame(*p)
    n, e = frame.to_ne(*towards)
    length = math.hypot(n, e)
    if length < 1e-6:
        return p
    k = min(dist_m, length / 2) / length
    return frame.to_latlon(n * k, e * k)


# ---------------------------------------------------------------------------
# WPML writing
# ---------------------------------------------------------------------------
class _X:
    """Tiny helper so element construction reads like the XML it produces."""

    def __init__(self, wpml_ns: str):
        self.wpml_ns = wpml_ns

    def w(self, parent, tag, text=None):
        el = ET.SubElement(parent, f"{{{self.wpml_ns}}}{tag}")
        if text is not None:
            el.text = _fmt(text)
        return el

    def k(self, parent, tag, text=None):
        el = ET.SubElement(parent, f"{{{KML_NS}}}{tag}")
        if text is not None:
            el.text = _fmt(text)
        return el


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        return f"{v:.3f}".rstrip("0").rstrip(".") if abs(v) < 1e6 else f"{v:.0f}"
    return str(v)


def _mission_config(x: _X, doc, cfg: FollowupConfig, transit: float):
    mc = x.w(doc, "missionConfig")
    x.w(mc, "flyToWaylineMode", "safely")
    x.w(mc, "finishAction", cfg.finish_action)
    if cfg.rc_lost_action == "goContinue":
        x.w(mc, "exitOnRCLost", "goContinue")
    else:
        x.w(mc, "exitOnRCLost", "executeLostAction")
        x.w(mc, "executeRCLostAction", cfg.rc_lost_action)
    # Climb straight up to transit height before moving off, and return at that height.
    x.w(mc, "takeOffSecurityHeight", transit)
    x.w(mc, "globalTransitionalSpeed", cfg.transit_speed_ms)
    x.w(mc, "globalRTHHeight", transit)
    di = x.w(mc, "droneInfo")
    x.w(di, "droneEnumValue", cfg.drone_enum)
    x.w(di, "droneSubEnumValue", cfg.drone_sub_enum)
    pi = x.w(mc, "payloadInfo")
    x.w(pi, "payloadEnumValue", cfg.payload_enum)
    x.w(pi, "payloadSubEnumValue", cfg.payload_sub_enum)
    x.w(pi, "payloadPositionIndex", cfg.payload_position)


def _heading_param(x: _X, parent, tag="waypointHeadingParam"):
    hp = x.w(parent, tag)
    x.w(hp, "waypointHeadingMode", "followWayline")
    x.w(hp, "waypointHeadingAngle", 0)
    x.w(hp, "waypointPoiPoint", "0.000000,0.000000,0.000000")
    x.w(hp, "waypointHeadingAngleEnable", 0)
    x.w(hp, "waypointHeadingPathMode", "followBadArc")


def _action_group(x: _X, placemark, index: int, group_id: int, wp: Waypoint, cfg: FollowupConfig):
    ag = x.w(placemark, "actionGroup")
    x.w(ag, "actionGroupId", group_id)
    x.w(ag, "actionGroupStartIndex", index)
    x.w(ag, "actionGroupEndIndex", index)
    x.w(ag, "actionGroupMode", "sequence")
    trig = x.w(ag, "actionTrigger")
    x.w(trig, "actionTriggerType", "reachPoint")
    aid = 0

    def action(func):
        nonlocal aid
        a = x.w(ag, "action")
        x.w(a, "actionId", aid)
        aid += 1
        x.w(a, "actionActuatorFunc", func)
        return x.w(a, "actionActuatorFuncParam")

    g = action("gimbalRotate")
    x.w(g, "gimbalHeadingYawBase", "north")
    x.w(g, "gimbalRotateMode", "absoluteAngle")
    x.w(g, "gimbalPitchRotateEnable", 1)
    x.w(g, "gimbalPitchRotateAngle", cfg.gimbal_pitch_deg)
    x.w(g, "gimbalRollRotateEnable", 0)
    x.w(g, "gimbalRollRotateAngle", 0)
    x.w(g, "gimbalYawRotateEnable", 0)
    x.w(g, "gimbalYawRotateAngle", 0)
    x.w(g, "gimbalRotateTimeEnable", 0)
    x.w(g, "gimbalRotateTime", 0)
    x.w(g, "payloadPositionIndex", cfg.payload_position)
    if cfg.hover_s > 0:
        h = action("hover")
        x.w(h, "hoverTime", cfg.hover_s)
    t = action("takePhoto")
    x.w(t, "payloadPositionIndex", cfg.payload_position)
    x.w(t, "fileSuffix", f"PS{wp.object_id}")
    x.w(t, "payloadLensIndex", cfg.photo_lenses)
    x.w(t, "useGlobalPayloadLensIndex", 0)


def _root(wpml_ns: str):
    ET.register_namespace("", KML_NS)
    ET.register_namespace("wpml", wpml_ns)
    kml = ET.Element(f"{{{KML_NS}}}kml")
    return kml, ET.SubElement(kml, f"{{{KML_NS}}}Document")


def _serialise(root) -> bytes:
    ET.indent(root, space="  ")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


def build_template_kml(plan: MissionPlan, cfg: FollowupConfig, now_ms: int | None = None) -> bytes:
    ns = f"http://www.dji.com/wpmz/{cfg.wpml_version}"
    x = _X(ns)
    kml, doc = _root(ns)
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    x.w(doc, "author", "pointersearch")
    x.w(doc, "createTime", now_ms)
    x.w(doc, "updateTime", now_ms)
    _mission_config(x, doc, cfg, plan.transit_height_m)

    folder = x.k(doc, "Folder")
    x.w(folder, "templateType", "waypoint")
    x.w(folder, "templateId", 0)
    cs = x.w(folder, "waylineCoordinateSysParam")
    x.w(cs, "coordinateMode", "WGS84")
    x.w(cs, "heightMode", "relativeToStartPoint")
    x.w(folder, "autoFlightSpeed", cfg.transit_speed_ms)
    x.w(folder, "globalHeight", plan.transit_height_m)
    x.w(folder, "caliFlightEnable", 0)
    x.w(folder, "gimbalPitchMode", "usePointSetting")
    _heading_param(x, folder, "globalWaypointHeadingParam")
    x.w(folder, "globalWaypointTurnMode", "toPointAndStopWithDiscontinuityCurvature")
    x.w(folder, "globalUseStraightLine", 1)

    group_id = 0
    for i, wp in enumerate(plan.waypoints):
        pm = x.k(folder, "Placemark")
        pt = x.k(pm, "Point")
        x.k(pt, "coordinates", f"{wp.lon:.8f},{wp.lat:.8f}")
        x.w(pm, "index", i)
        x.w(pm, "ellipsoidHeight", wp.height_m)
        x.w(pm, "height", wp.height_m)
        x.w(pm, "useGlobalHeight", 0)
        x.w(pm, "useGlobalSpeed", 0)
        x.w(pm, "waypointSpeed", wp.speed_ms)
        x.w(pm, "useGlobalHeadingParam", 1)
        x.w(pm, "useGlobalTurnParam", 1)
        x.w(pm, "useStraightLine", 1)
        x.w(pm, "gimbalPitchAngle", cfg.gimbal_pitch_deg)
        if wp.inspect:
            _action_group(x, pm, i, group_id, wp, cfg)
            group_id += 1

    pp = x.w(folder, "payloadParam")
    x.w(pp, "payloadPositionIndex", cfg.payload_position)
    x.w(pp, "imageFormat", cfg.photo_lenses)
    return _serialise(kml)


def build_waylines_wpml(plan: MissionPlan, cfg: FollowupConfig) -> bytes:
    ns = f"http://www.dji.com/wpmz/{cfg.wpml_version}"
    x = _X(ns)
    kml, doc = _root(ns)
    _mission_config(x, doc, cfg, plan.transit_height_m)

    folder = x.k(doc, "Folder")
    x.w(folder, "templateId", 0)
    x.w(folder, "executeHeightMode", "relativeToStartPoint")
    x.w(folder, "waylineId", 0)
    x.w(folder, "distance", round(plan.distance_m, 1))
    x.w(folder, "duration", round(plan.duration_s, 1))
    x.w(folder, "autoFlightSpeed", cfg.transit_speed_ms)

    group_id = 0
    for i, wp in enumerate(plan.waypoints):
        pm = x.k(folder, "Placemark")
        pt = x.k(pm, "Point")
        x.k(pt, "coordinates", f"{wp.lon:.8f},{wp.lat:.8f}")
        x.w(pm, "index", i)
        x.w(pm, "executeHeight", wp.height_m)
        x.w(pm, "waypointSpeed", wp.speed_ms)
        _heading_param(x, pm)
        tp = x.w(pm, "waypointTurnParam")
        x.w(tp, "waypointTurnMode", "toPointAndStopWithDiscontinuityCurvature")
        x.w(tp, "waypointTurnDampingDist", 0)
        x.w(pm, "useStraightLine", 1)
        if wp.inspect:
            _action_group(x, pm, i, group_id, wp, cfg)
            group_id += 1
    return _serialise(kml)


def build_kmz(plan: MissionPlan, cfg: FollowupConfig) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("wpmz/template.kml", build_template_kml(plan, cfg))
        z.writestr("wpmz/waylines.wpml", build_waylines_wpml(plan, cfg))
    return buf.getvalue()


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


# ---------------------------------------------------------------------------
# Inspecting a route exported from Pilot 2
# ---------------------------------------------------------------------------
def wpml_info(kmz_path) -> dict:
    """Read the aircraft/payload codes and format version from an existing KMZ, so
    they can be copied into ``followup`` in pointersearch.json."""
    out: dict = {"files": []}
    with zipfile.ZipFile(kmz_path) as z:
        out["files"] = z.namelist()
        name = next((n for n in z.namelist() if n.endswith("waylines.wpml")), None) or \
            next((n for n in z.namelist() if n.endswith("template.kml")), None)
        if name is None:
            raise MissionError("no waylines.wpml or template.kml inside the KMZ")
        data = z.read(name)
    root = ET.fromstring(data)
    wpml_ns = next((el.tag[1:].split("}")[0] for el in root.iter()
                    if el.tag.startswith("{http://www.dji.com/wpmz/")), None)
    if wpml_ns is None:
        raise MissionError("no DJI wpml namespace found")

    def find(tag):
        el = next(root.iter(f"{{{wpml_ns}}}{tag}"), None)
        return el.text.strip() if el is not None and el.text else None

    def to_int(v):
        return int(v) if v is not None and v.lstrip("-").isdigit() else v

    lenses = sorted({el.text.strip() for tag in ("payloadLensIndex", "imageFormat")
                     for el in root.iter(f"{{{wpml_ns}}}{tag}") if el.text})
    out.update({
        "read_from": name,
        "wpml_version": wpml_ns.rsplit("/", 1)[-1],
        "drone_enum": to_int(find("droneEnumValue")),
        "drone_sub_enum": to_int(find("droneSubEnumValue")),
        "payload_enum": to_int(find("payloadEnumValue")),
        "payload_sub_enum": to_int(find("payloadSubEnumValue")),
        "payload_position": to_int(find("payloadPositionIndex")),
        "height_mode": find("executeHeightMode") or find("heightMode"),
        "lens_strings_seen": lenses,
        "actions_seen": sorted({el.text.strip() for el in root.iter(f"{{{wpml_ns}}}actionActuatorFunc") if el.text}),
    })
    return out
