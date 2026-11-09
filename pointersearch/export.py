"""Export candidates for ground teams: GPX, KML and GeoJSON."""

from __future__ import annotations

import json
from pathlib import Path
from xml.sax.saxutils import escape

from .store import Store


def _name(c: dict) -> str:
    return f"PS{c['id']:04d} {c['detector']} {c['score']:.1f}"


def _desc(c: dict) -> str:
    bits = [f"detector={c['detector']}", f"score={c['score']:.2f}", f"status={c['status']}",
            f"image={Path(c['image_path']).name}", f"area_m2={c.get('area_m2')}"]
    if c.get("extra", {}).get("peak_temp_c") is not None:
        bits.append(f"peak_temp_c={c['extra']['peak_temp_c']:.1f}")
    if c.get("note"):
        bits.append(f"note={c['note']}")
    return "; ".join(bits)


def to_gpx(cands: list[dict]) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<gpx version="1.1" creator="pointersearch" xmlns="http://www.topografix.com/GPX/1/1">']
    for c in cands:
        out.append(f'  <wpt lat="{c["lat"]:.7f}" lon="{c["lon"]:.7f}">')
        out.append(f"    <name>{escape(_name(c))}</name>")
        out.append(f"    <desc>{escape(_desc(c))}</desc>")
        out.append(f"    <sym>{'Flag, Red' if c['status'] == 'accepted' else 'Flag, Blue'}</sym>")
        out.append("  </wpt>")
    out.append("</gpx>")
    return "\n".join(out) + "\n"


def to_kml(cands: list[dict]) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>pointersearch candidates</name>']
    for c in cands:
        out.append("<Placemark>")
        out.append(f"<name>{escape(_name(c))}</name><description>{escape(_desc(c))}</description>")
        out.append(f"<Point><coordinates>{c['lon']:.7f},{c['lat']:.7f},0</coordinates></Point>")
        out.append("</Placemark>")
    out.append("</Document></kml>")
    return "\n".join(out) + "\n"


def to_geojson(cands: list[dict]) -> str:
    features = []
    for c in cands:
        props = {k: c[k] for k in ("id", "detector", "score", "status", "area_m2", "note", "image_path", "thumb")}
        props.update(c.get("extra") or {})
        features.append({"type": "Feature", "properties": props,
                         "geometry": {"type": "Point", "coordinates": [c["lon"], c["lat"]]}})
    return json.dumps({"type": "FeatureCollection", "features": features}, indent=1)


FORMATS = {"gpx": (to_gpx, "application/gpx+xml"),
           "kml": (to_kml, "application/vnd.google-earth.kml+xml"),
           "geojson": (to_geojson, "application/geo+json")}


def export(store: Store, fmt: str, status: str | None = "accepted") -> tuple[str, str]:
    fn, mime = FORMATS[fmt]
    cands = store.list_candidates(status=None if status in (None, "all") else status)
    return fn(cands), mime
