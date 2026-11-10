"""Command line interface.

    pointersearch app                                      (start screen in the browser; no other commands needed)
    pointersearch init    missions/2025-06-01-blue-mtns
    pointersearch process missions/2025-06-01-blue-mtns /media/sdcard/DCIM/DJI_001
    pointersearch watch   missions/2025-06-01-blue-mtns /srv/uploads
    pointersearch serve   missions/2025-06-01-blue-mtns --tiles ~/tiles/bluemtns
    pointersearch export  missions/2025-06-01-blue-mtns --format gpx --status accepted
    pointersearch mission missions/2025-06-01-blue-mtns --transit 90 --inspect 60
    pointersearch plan    missions/2025-06-01-blue-mtns
    pointersearch merge   missions/2025-06-01-blue-mtns
    pointersearch demo    demo
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import Config


def _log(verbose: bool):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")


def cmd_init(a):
    wd = Path(a.workdir)
    wd.mkdir(parents=True, exist_ok=True)
    cfg = Config()
    path = cfg.save(wd)
    from .store import Store
    Store(wd).close()
    print(f"initialised {wd}\n  config: {path}\n  edit thresholds there, then run `pointersearch process`")


def cmd_process(a):
    from .pipeline import Pipeline
    p = Pipeline(a.workdir)
    total = 0
    for src in a.sources:
        total += p.process_path(src, force=a.force)
    s = p.store.summary()
    print(f"processed {total} new image(s); {s['images_ok']} images, candidates: {s['candidates']}")


def cmd_watch(a):
    from .pipeline import Pipeline
    from .watch import watch
    p = Pipeline(a.workdir)
    try:
        watch(p, Path(a.folder), interval_s=a.interval, once=a.once)
    except KeyboardInterrupt:
        pass


def cmd_serve(a):
    from .review.server import serve
    print(f"review map:    http://{a.host}:{a.port}/")
    print(f"route planner: http://{a.host}:{a.port}/plan   (Ctrl-C to stop)")
    serve(a.workdir, host=a.host, port=a.port, tiles_dir=a.tiles, mission_name=a.name)


def cmd_app(a):
    """The full app: start screen, missions, planner and review, in the browser."""
    from .review.hub import run_app
    sys.exit(run_app(a.root, host=a.host, port=a.port, tiles_dir=a.tiles, open_browser=not a.no_browser))


def cmd_web(a):
    """The hosted planner, run locally (for trying it out before deploying)."""
    from .web.__main__ import main as web_main
    sys.exit(web_main(["--data", a.data, "--host", a.host, "--port", str(a.port)]))


def cmd_plan(a):
    """Open the route planner. Works before any imagery exists."""
    from .review.server import serve
    wd = Path(a.workdir)
    if not (wd / Config.FILENAME).exists():
        wd.mkdir(parents=True, exist_ok=True)
        Config().save(wd)
        print(f"created {wd}")
    print(f"route planner: http://{a.host}:{a.port}/plan   (Ctrl-C to stop)")
    serve(wd, host=a.host, port=a.port, tiles_dir=a.tiles, mission_name=a.name)


def cmd_export(a):
    from .export import export
    from .store import Store
    body, _ = export(Store(a.workdir), a.format, a.status, by=a.by)
    if a.output:
        Path(a.output).write_text(body)
        print(f"wrote {a.output}")
    else:
        sys.stdout.write(body)


def cmd_mission(a):
    from .mission import MissionError, default_home, plan_mission, points_from_store
    from .store import Store
    from .wpml import build_kmz
    wd = Path(a.workdir)
    cfg = Config.load(wd)
    st = Store(wd)
    st.regroup(cfg.group_radius_m)
    home = None
    if a.home:
        lat, lon = (float(v) for v in a.home.split(","))
        home = (lat, lon)
    else:
        home = default_home(st)
    try:
        plan = plan_mission(points_from_store(st, a.status), cfg.followup, cfg.flight, a.transit, a.inspect, home)
    except MissionError as exc:
        sys.exit(f"cannot build mission: {exc}")
    out = Path(a.output or wd / "followup.kmz")
    out.write_bytes(build_kmz(plan.wpml, cfg.flight))
    s = plan.summary()
    print(f"wrote {out}")
    print(f"  {s['objects']} object(s), {s['waypoints']} waypoints, transit {s['transit_height_m']:g} m / "
          f"inspect {s['inspect_height_m']:g} m (relative to take-off)")
    print(f"  about {s['distance_m']} m and {s['duration_min']} min; lenses {cfg.followup.photo_lenses}")
    for o in s["order"]:
        print(f"   {o['seq']:>2}. G{o['object_id']:<5} {o['lat']:.6f},{o['lon']:.6f}  {o['label']}")
    for w in s["warnings"]:
        print(f"  ! {w}")
    print("import in DJI Pilot 2: Flight Route > Import Route (KMZ/KML), then check it on the map before flying")


def cmd_wpml_info(a):
    from .wpml import WpmlError, wpml_info
    try:
        info = wpml_info(a.kmz)
    except (WpmlError, OSError) as exc:
        sys.exit(f"cannot read {a.kmz}: {exc}")
    print(json.dumps(info, indent=2))
    keys = ("drone_enum", "drone_sub_enum", "payload_enum", "payload_sub_enum", "payload_position", "wpml_version")
    print("\npaste into pointersearch.json to match this aircraft:")
    print(json.dumps({"flight": {k: info[k] for k in keys if info.get(k) is not None}}, indent=2))


def cmd_merge(a):
    from .mosaic import Overlay, merge_overlays
    from .store import Store
    wd = Path(a.workdir)
    st = Store(wd)
    ovs = []
    for o in st.overlays():
        b = o["bounds"]
        import cv2
        img = cv2.imread(str(wd / o["png"]), cv2.IMREAD_UNCHANGED)
        if img is None:
            continue
        ovs.append(Overlay(str(wd / o["png"]), b[0][0], b[0][1], b[1][0], b[1][1], img.shape[1], img.shape[0]))
    out = wd / (a.output or "mosaic.png")
    res = merge_overlays(ovs, out, max_px=a.max_px)
    if res is None:
        print("no overlays to merge")
    else:
        print(f"wrote {out} ({res.width_px}x{res.height_px}) + world file {out.with_suffix('.pgw')}")


def cmd_info(a):
    from .camera import focal_px, gsd_m, hfov_deg
    from .georef import Georeferencer
    from .metadata import read_image_meta
    cfg = Config()
    for f in a.images:
        m = read_image_meta(f)
        g = Georeferencer(m, cfg.camera)
        d = m.to_dict()
        raw = d.pop("dji", {})
        d.update({"focal_px": round(focal_px(m, cfg.camera), 1), "hfov_deg": round(hfov_deg(m, cfg.camera), 1),
                  "gsd_m_nadir": round(gsd_m(m, cfg.camera), 4), "footprint": g.footprint_latlon()})
        if a.raw:
            d["xmp_drone_dji"] = raw
        print(json.dumps(d, indent=2))


def cmd_stats(a):
    """Text summary of a mission for tuning thresholds without screenshots."""
    import statistics
    from collections import Counter, defaultdict
    from .store import Store
    st = Store(a.workdir)
    st.regroup(Config.load(Path(a.workdir)).group_radius_m)
    imgs = st.list_images()
    cands = st.list_candidates()
    print(f"images: {len(imgs)}  ok={sum(i['status']=='ok' for i in imgs)}  "
          f"by source: {dict(Counter(i['source'] for i in imgs))}")
    pitches = [i["gimbal_pitch"] for i in imgs if i.get("gimbal_pitch") is not None]
    alts = [i["rel_alt"] for i in imgs if i.get("rel_alt") is not None]
    gsds = [i["gsd_m"] for i in imgs if i.get("gsd_m")]
    if pitches:
        print(f"gimbal pitch: min {min(pitches):.0f} / median {statistics.median(pitches):.0f} / max {max(pitches):.0f} deg")
    if alts:
        print(f"rel altitude: min {min(alts):.0f} / median {statistics.median(alts):.0f} / max {max(alts):.0f} m")
    if gsds:
        print(f"centre gsd:   median {statistics.median(gsds):.3f} m/px")
    groups = st.list_groups()
    multi = sum(1 for g in groups if g["n_frames"] > 1)
    print(f"overlays: {sum(1 for i in imgs if i['overlay_png'])}   sightings: {len(cands)}  "
          f"({len(cands)/max(1,len(imgs)):.1f} per image)   objects: {len(groups)}  ({multi} seen in >1 frame)")
    print("top objects:")
    for g in groups[:a.top]:
        print(f"   G{g['id']:<5} score {g['score']:>8.1f}  {'+'.join(g['detectors']):<18} "
              f"{g['n_frames']:>3} frames  {g['lat']:.6f},{g['lon']:.6f}  {g['status']}")
    by = defaultdict(list)
    for c in cands:
        by[c["detector"]].append(c)
    for det, cs in sorted(by.items()):
        scores = sorted(c["score"] for c in cs)
        areas = sorted(c["area_m2"] for c in cs if c["area_m2"] is not None)
        q = lambda xs, f: xs[min(len(xs)-1, int(f*len(xs)))] if xs else 0
        status = Counter(c["status"] for c in cs)
        print(f"\n[{det}] n={len(cs)}  score p50={q(scores,.5):.1f} p90={q(scores,.9):.1f} max={scores[-1]:.1f}  "
              f"area m2 p50={q(areas,.5):.2f} p90={q(areas,.9):.2f}  status={dict(status)}")
        for c in cs[:a.top]:
            print(f"   #{c['id']:<5} score {c['score']:>8.1f}  {c['area_m2']:>6.2f} m2  "
                  f"{c['lat']:.6f},{c['lon']:.6f}  {Path(c['image_path']).name}  {c['status']}")


def cmd_demo(a):
    from .devtools.synthetic import generate_mission
    from .pipeline import Pipeline
    wd = Path(a.workdir)
    wd.mkdir(parents=True, exist_ok=True)
    Config().save(wd)
    img_dir = wd / "images"
    manifest = generate_mission(img_dir, with_thermal=not a.no_thermal, with_zoom=a.zoom)
    (wd / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"generated {len(manifest['images'])} synthetic images in {img_dir}")
    p = Pipeline(wd)
    n = p.process_path(img_dir)
    s = p.store.summary()
    print(f"processed {n} images; candidates: {s['candidates']}")
    print("planted targets:")
    for t in manifest["targets"]:
        print(f"  {t['name']:<12} {t['lat']:.6f}, {t['lon']:.6f}")
    print(f"\nnext: pointersearch serve {wd}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pointersearch", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create a mission working directory with a default config")
    s.add_argument("workdir"); s.set_defaults(fn=cmd_init)

    s = sub.add_parser("process", help="process image files/folders into the mission")
    s.add_argument("workdir"); s.add_argument("sources", nargs="+")
    s.add_argument("--force", action="store_true", help="reprocess images already in the database")
    s.set_defaults(fn=cmd_process)

    s = sub.add_parser("watch", help="poll a folder and process new images as they land")
    s.add_argument("workdir"); s.add_argument("folder")
    s.add_argument("--interval", type=float, default=2.0)
    s.add_argument("--once", action="store_true", help="single pass (for scripting)")
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("serve", help="start the local review map")
    s.add_argument("workdir"); s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8008)
    s.add_argument("--tiles", help="folder of XYZ base-map tiles ({z}/{x}/{y}.png) for offline use")
    s.add_argument("--name", help="mission name shown in the UI")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("app", help="start the app with its start screen (create and open missions in the browser)")
    s.add_argument("--root", default="missions", help="folder that holds the missions (default: ./missions)")
    s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8008)
    s.add_argument("--tiles", help="folder of XYZ base-map tiles for offline use")
    s.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    s.set_defaults(fn=cmd_app)

    s = sub.add_parser("web", help="run the hosted planner locally (logins, missions, routes; no image processing)")
    s.add_argument("--data", default="webdata", help="folder for missions and logins (default: ./webdata)")
    s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8010)
    s.set_defaults(fn=cmd_web)

    s = sub.add_parser("plan", help="open the route planner (draw a line or area, download a KMZ)")
    s.add_argument("workdir", nargs="?", default="missions/planning")
    s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8008)
    s.add_argument("--tiles", help="folder of XYZ base-map tiles for offline use")
    s.add_argument("--name"); s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("export", help="export candidates as GPX/KML/GeoJSON")
    s.add_argument("workdir"); s.add_argument("--format", choices=["gpx", "kml", "geojson"], default="gpx")
    s.add_argument("--status", default="accepted", help="accepted | followup | pending | rejected | all, or a comma list")
    s.add_argument("--by", choices=["group", "sighting"], default="group",
                   help="one point per object (group) or per detection (sighting)")
    s.add_argument("-o", "--output"); s.set_defaults(fn=cmd_export)

    s = sub.add_parser("mission", help="write a DJI Pilot 2 waypoint mission (KMZ) over follow-up objects")
    s.add_argument("workdir")
    s.add_argument("--transit", type=float, help="height between objects, m above take-off (default from config: 90)")
    s.add_argument("--inspect", type=float, help="height over each object, m above take-off (default from config: 60)")
    s.add_argument("--status", default="followup", help="which objects to visit, e.g. followup or followup,accepted")
    s.add_argument("--home", help="take-off position 'lat,lon' used to order the route (default: first frame)")
    s.add_argument("-o", "--output")
    s.set_defaults(fn=cmd_mission)

    s = sub.add_parser("wpml-info", help="print aircraft/payload codes from a route exported by DJI Pilot 2")
    s.add_argument("kmz"); s.set_defaults(fn=cmd_wpml_info)

    s = sub.add_parser("merge", help="merge all overlays into one georeferenced PNG (+ world file)")
    s.add_argument("workdir"); s.add_argument("-o", "--output"); s.add_argument("--max-px", type=int, default=12000)
    s.set_defaults(fn=cmd_merge)

    s = sub.add_parser("info", help="print metadata, GSD and footprint for images")
    s.add_argument("images", nargs="+")
    s.add_argument("--raw", action="store_true", help="include every drone-dji XMP field as written by the firmware")
    s.set_defaults(fn=cmd_info)

    s = sub.add_parser("stats", help="print a text summary of images and candidates (for threshold tuning)")
    s.add_argument("workdir"); s.add_argument("--top", type=int, default=8, help="top candidates to list per detector")
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("demo", help="generate and process a synthetic mission")
    s.add_argument("workdir"); s.add_argument("--no-thermal", action="store_true")
    s.add_argument("--zoom", action="store_true", help="also generate zoom-lens frames")
    s.set_defaults(fn=cmd_demo)

    a = ap.parse_args(argv)
    _log(a.verbose)
    a.fn(a)


if __name__ == "__main__":
    main()
