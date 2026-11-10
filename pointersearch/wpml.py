"""Writer for DJI waypoint routes (WPML inside a KMZ) and a reader for the
aircraft codes in routes exported by DJI Pilot 2.

A KMZ holds two files that describe the same route:

* ``wpmz/template.kml``  - what Pilot 2 shows and lets the pilot edit
* ``wpmz/waylines.wpml`` - what the aircraft executes

Heights are always written relative to the take-off point.
"""

from __future__ import annotations

import io
import time
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from .config import FlightConfig

KML_NS = "http://www.opengis.net/kml/2.2"
STOP_TURN = "toPointAndStopWithDiscontinuityCurvature"


class WpmlError(ValueError):
    """Raised when a route cannot be written or read."""


@dataclass
class ActionGroup:
    """Actions run from waypoint ``start`` to waypoint ``end`` (indices).

    ``trigger``: reachPoint (once, on arrival), multipleDistance (every ``param``
    metres) or multipleTiming (every ``param`` seconds).
    """

    actions: list[tuple[str, dict]]
    trigger: str = "reachPoint"
    param: float | None = None
    start: int = 0
    end: int = 0


@dataclass
class WpmlWaypoint:
    lat: float
    lon: float
    height_m: float
    speed_ms: float
    groups: list[ActionGroup] = field(default_factory=list)


@dataclass
class WpmlRoute:
    waypoints: list[WpmlWaypoint]
    safe_height_m: float            # take-off climb height and return-to-home height
    speed_ms: float
    distance_m: float
    duration_s: float
    image_format: str = "wide,ir"
    gimbal_pitch_deg: float = -90.0


# ---- action constructors ---------------------------------------------------
def gimbal_rotate(pitch_deg: float, payload_position: int = 0) -> tuple[str, dict]:
    return ("gimbalRotate", {
        "gimbalHeadingYawBase": "north", "gimbalRotateMode": "absoluteAngle",
        "gimbalPitchRotateEnable": 1, "gimbalPitchRotateAngle": pitch_deg,
        "gimbalRollRotateEnable": 0, "gimbalRollRotateAngle": 0,
        "gimbalYawRotateEnable": 0, "gimbalYawRotateAngle": 0,
        "gimbalRotateTimeEnable": 0, "gimbalRotateTime": 0,
        "payloadPositionIndex": payload_position,
    })


def hover(seconds: float) -> tuple[str, dict]:
    return ("hover", {"hoverTime": seconds})


def take_photo(lenses: str, suffix: str = "", payload_position: int = 0) -> tuple[str, dict]:
    return ("takePhoto", {
        "payloadPositionIndex": payload_position, "fileSuffix": suffix,
        "payloadLensIndex": lenses, "useGlobalPayloadLensIndex": 0,
    })


# ---- XML helpers -----------------------------------------------------------
class _X:
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


def _root(wpml_ns: str):
    ET.register_namespace("", KML_NS)
    ET.register_namespace("wpml", wpml_ns)
    kml = ET.Element(f"{{{KML_NS}}}kml")
    return kml, ET.SubElement(kml, f"{{{KML_NS}}}Document")


def _serialise(root) -> bytes:
    ET.indent(root, space="  ")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


def _mission_config(x: _X, doc, route: WpmlRoute, flight: FlightConfig):
    mc = x.w(doc, "missionConfig")
    x.w(mc, "flyToWaylineMode", "safely")
    x.w(mc, "finishAction", flight.finish_action)
    if flight.rc_lost_action == "goContinue":
        x.w(mc, "exitOnRCLost", "goContinue")
    else:
        x.w(mc, "exitOnRCLost", "executeLostAction")
        x.w(mc, "executeRCLostAction", flight.rc_lost_action)
    # Climb straight up to the safe height before moving off, and return at that height.
    x.w(mc, "takeOffSecurityHeight", route.safe_height_m)
    x.w(mc, "globalTransitionalSpeed", route.speed_ms)
    x.w(mc, "globalRTHHeight", route.safe_height_m)
    di = x.w(mc, "droneInfo")
    x.w(di, "droneEnumValue", flight.drone_enum)
    x.w(di, "droneSubEnumValue", flight.drone_sub_enum)
    pi = x.w(mc, "payloadInfo")
    x.w(pi, "payloadEnumValue", flight.payload_enum)
    x.w(pi, "payloadSubEnumValue", flight.payload_sub_enum)
    x.w(pi, "payloadPositionIndex", flight.payload_position)


def _heading_param(x: _X, parent, tag="waypointHeadingParam"):
    hp = x.w(parent, tag)
    x.w(hp, "waypointHeadingMode", "followWayline")
    x.w(hp, "waypointHeadingAngle", 0)
    x.w(hp, "waypointPoiPoint", "0.000000,0.000000,0.000000")
    x.w(hp, "waypointHeadingAngleEnable", 0)
    x.w(hp, "waypointHeadingPathMode", "followBadArc")


def _action_groups(x: _X, placemark, wp: WpmlWaypoint, next_group_id: int) -> int:
    for g in wp.groups:
        ag = x.w(placemark, "actionGroup")
        x.w(ag, "actionGroupId", next_group_id)
        next_group_id += 1
        x.w(ag, "actionGroupStartIndex", g.start)
        x.w(ag, "actionGroupEndIndex", g.end)
        x.w(ag, "actionGroupMode", "sequence")
        trig = x.w(ag, "actionTrigger")
        x.w(trig, "actionTriggerType", g.trigger)
        if g.param is not None:
            x.w(trig, "actionTriggerParam", float(g.param))
        for aid, (func, params) in enumerate(g.actions):
            a = x.w(ag, "action")
            x.w(a, "actionId", aid)
            x.w(a, "actionActuatorFunc", func)
            fp = x.w(a, "actionActuatorFuncParam")
            for key, value in params.items():
                x.w(fp, key, value)
    return next_group_id


def _check(route: WpmlRoute):
    if not route.waypoints:
        raise WpmlError("route has no waypoints")
    if len(route.waypoints) > 65535:
        raise WpmlError("too many waypoints for one route")


def build_template_kml(route: WpmlRoute, flight: FlightConfig, now_ms: int | None = None) -> bytes:
    _check(route)
    ns = f"http://www.dji.com/wpmz/{flight.wpml_version}"
    x = _X(ns)
    kml, doc = _root(ns)
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    x.w(doc, "author", "pointersearch")
    x.w(doc, "createTime", now_ms)
    x.w(doc, "updateTime", now_ms)
    _mission_config(x, doc, route, flight)

    folder = x.k(doc, "Folder")
    x.w(folder, "templateType", "waypoint")
    x.w(folder, "templateId", 0)
    cs = x.w(folder, "waylineCoordinateSysParam")
    x.w(cs, "coordinateMode", "WGS84")
    x.w(cs, "heightMode", "relativeToStartPoint")
    x.w(folder, "autoFlightSpeed", route.speed_ms)
    x.w(folder, "globalHeight", route.safe_height_m)
    x.w(folder, "caliFlightEnable", 0)
    x.w(folder, "gimbalPitchMode", "usePointSetting")
    _heading_param(x, folder, "globalWaypointHeadingParam")
    x.w(folder, "globalWaypointTurnMode", STOP_TURN)
    x.w(folder, "globalUseStraightLine", 1)

    gid = 0
    for i, wp in enumerate(route.waypoints):
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
        x.w(pm, "gimbalPitchAngle", route.gimbal_pitch_deg)
        gid = _action_groups(x, pm, wp, gid)

    pp = x.w(folder, "payloadParam")
    x.w(pp, "payloadPositionIndex", flight.payload_position)
    x.w(pp, "imageFormat", route.image_format)
    return _serialise(kml)


def build_waylines_wpml(route: WpmlRoute, flight: FlightConfig) -> bytes:
    _check(route)
    ns = f"http://www.dji.com/wpmz/{flight.wpml_version}"
    x = _X(ns)
    kml, doc = _root(ns)
    _mission_config(x, doc, route, flight)

    folder = x.k(doc, "Folder")
    x.w(folder, "templateId", 0)
    x.w(folder, "executeHeightMode", "relativeToStartPoint")
    x.w(folder, "waylineId", 0)
    x.w(folder, "distance", round(route.distance_m, 1))
    x.w(folder, "duration", round(route.duration_s, 1))
    x.w(folder, "autoFlightSpeed", route.speed_ms)

    gid = 0
    for i, wp in enumerate(route.waypoints):
        pm = x.k(folder, "Placemark")
        pt = x.k(pm, "Point")
        x.k(pt, "coordinates", f"{wp.lon:.8f},{wp.lat:.8f}")
        x.w(pm, "index", i)
        x.w(pm, "executeHeight", wp.height_m)
        x.w(pm, "waypointSpeed", wp.speed_ms)
        _heading_param(x, pm)
        tp = x.w(pm, "waypointTurnParam")
        x.w(tp, "waypointTurnMode", STOP_TURN)
        x.w(tp, "waypointTurnDampingDist", 0)
        x.w(pm, "useStraightLine", 1)
        gid = _action_groups(x, pm, wp, gid)
    return _serialise(kml)


def build_kmz(route: WpmlRoute, flight: FlightConfig) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("wpmz/template.kml", build_template_kml(route, flight))
        z.writestr("wpmz/waylines.wpml", build_waylines_wpml(route, flight))
    return buf.getvalue()


# ---- reading a route exported from Pilot 2 ---------------------------------
def wpml_info(kmz_path) -> dict:
    """Read the aircraft/payload codes and format version from an existing KMZ, so
    they can be copied into ``flight`` in pointersearch.json."""
    out: dict = {"files": []}
    with zipfile.ZipFile(kmz_path) as z:
        out["files"] = z.namelist()
        name = next((n for n in z.namelist() if n.endswith("waylines.wpml")), None) or \
            next((n for n in z.namelist() if n.endswith("template.kml")), None)
        if name is None:
            raise WpmlError("no waylines.wpml or template.kml inside the KMZ")
        data = z.read(name)
    root = ET.fromstring(data)
    wpml_ns = next((el.tag[1:].split("}")[0] for el in root.iter()
                    if el.tag.startswith("{http://www.dji.com/wpmz/")), None)
    if wpml_ns is None:
        raise WpmlError("no DJI wpml namespace found")

    def find(tag):
        el = next(root.iter(f"{{{wpml_ns}}}{tag}"), None)
        return el.text.strip() if el is not None and el.text else None

    def to_int(v):
        return int(v) if v is not None and v.lstrip("-").isdigit() else v

    def seen(*tags):
        return sorted({el.text.strip() for tag in tags for el in root.iter(f"{{{wpml_ns}}}{tag}") if el.text})

    out.update({
        "read_from": name,
        "wpml_version": wpml_ns.rsplit("/", 1)[-1],
        "drone_enum": to_int(find("droneEnumValue")),
        "drone_sub_enum": to_int(find("droneSubEnumValue")),
        "payload_enum": to_int(find("payloadEnumValue")),
        "payload_sub_enum": to_int(find("payloadSubEnumValue")),
        "payload_position": to_int(find("payloadPositionIndex")),
        "height_mode": find("executeHeightMode") or find("heightMode"),
        "lens_strings_seen": seen("payloadLensIndex", "imageFormat"),
        "actions_seen": seen("actionActuatorFunc"),
        "triggers_seen": seen("actionTriggerType"),
        "turn_modes_seen": seen("waypointTurnMode", "globalWaypointTurnMode"),
    })
    return out
