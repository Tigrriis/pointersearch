"""Command line interface.

    pointersearch init    missions/2025-06-01-blue-mtns
    pointersearch process missions/2025-06-01-blue-mtns /media/sdcard/DCIM/DJI_001
    pointersearch watch   missions/2025-06-01-blue-mtns /srv/uploads
    pointersearch serve   missions/2025-06-01-blue-mtns --tiles ~/tiles/bluemtns
    pointersearch export  missions/2025-06-01-blue-mtns --format gpx --status accepted
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
    print(f"review UI: http://{a.host}:{a.port}/   (Ctrl-C to stop)")
    serve(a.workdir, host=a.host, port=a.port, tiles_dir=a.tiles, mission_name=a.name)


def cmd_export(a):
    from .export import export
    from .store import Store
    body, _ = export(Store(a.workdir), a.format, a.status)
    if a.output:
        Path(a.output).write_text(body)
        print(f"wrote {a.output}")
    else:
        sys.stdout.write(body)


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


def cmd_demo(a):
    from .devtools.synthetic import generate_mission
    from .pipeline import Pipeline
    wd = Path(a.workdir)
    wd.mkdir(parents=True, exist_ok=True)
    Config().save(wd)
    img_dir = wd / "images"
    manifest = generate_mission(img_dir, with_thermal=not a.no_thermal)
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

    s = sub.add_parser("export", help="export candidates as GPX/KML/GeoJSON")
    s.add_argument("workdir"); s.add_argument("--format", choices=["gpx", "kml", "geojson"], default="gpx")
    s.add_argument("--status", default="accepted", help="accepted | pending | rejected | all")
    s.add_argument("-o", "--output"); s.set_defaults(fn=cmd_export)

    s = sub.add_parser("merge", help="merge all overlays into one georeferenced PNG (+ world file)")
    s.add_argument("workdir"); s.add_argument("-o", "--output"); s.add_argument("--max-px", type=int, default=12000)
    s.set_defaults(fn=cmd_merge)

    s = sub.add_parser("info", help="print metadata, GSD and footprint for images")
    s.add_argument("images", nargs="+")
    s.add_argument("--raw", action="store_true", help="include every drone-dji XMP field as written by the firmware")
    s.set_defaults(fn=cmd_info)

    s = sub.add_parser("demo", help="generate and process a synthetic mission")
    s.add_argument("workdir"); s.add_argument("--no-thermal", action="store_true"); s.set_defaults(fn=cmd_demo)

    a = ap.parse_args(argv)
    _log(a.verbose)
    a.fn(a)


if __name__ == "__main__":
    main()
