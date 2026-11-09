# pointersearch

Ground-station tools for drone search and rescue imagery, built around the
DJI Matrice 4T. It turns a folder of stills into a georeferenced quick mosaic and
a ranked, reviewable list of candidate objects, entirely offline on a laptop at
the search base.

Use cases: missing persons (alive or deceased), lost dogs and other animals.

## What it does

1. **Ingest** JPEGs from the aircraft's SD card, or watch a folder that photos are
   dropped into as they are taken.
2. **Georeference** every frame directly from the DJI metadata (GPS, relative
   altitude, gimbal attitude). No photogrammetry, a few metres accuracy, under a
   second per image.
3. **Quick mosaic**: each frame is projected onto the ground as a north-up image
   overlay; `merge` writes one PNG with a world file for QGIS or CalTopo.
4. **Detect** candidates in each frame:
   * `rx` - Reed-Xiaoli colour anomaly (what is statistically unusual for this scene)
   * `bright` - saturated, non-vegetation colours (hi-vis, jackets, packs, tents)
   * `thermal` - hot spots of person/animal size in thermal frames
5. **Review** on a local Leaflet map: thumbnails, accept/reject with keyboard,
   filters per detector, live refresh while the aircraft is still flying.
6. **Export** accepted candidates as GPX, KML or GeoJSON for ground teams.

## Install

Python 3.10+ on the base laptop.

```bash
git clone <this repo> && cd pointersearch
python3 -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Try it without an aircraft (generates a synthetic mission with planted targets):

```bash
pointersearch demo demo
```

```bash
pointersearch serve demo
```

Then open http://127.0.0.1:8008/ in a browser.

## Field workflow

```bash
pointersearch init missions/2025-06-01-grose-valley
```

After each battery, pull the SD card and run:

```bash
pointersearch process missions/2025-06-01-grose-valley /media/sdcard/DCIM/DJI_001
```

Or, if photos are arriving in a folder continuously (see
[docs/FIELD_WORKFLOW.md](docs/FIELD_WORKFLOW.md) for the Cloud API / hotspot
setup), leave a watcher running:

```bash
pointersearch watch missions/2025-06-01-grose-valley /srv/uploads
```

In a second terminal, start the review map. Add `--tiles` if you pre-downloaded
XYZ base-map tiles for the area while you still had internet:

```bash
pointersearch serve missions/2025-06-01-grose-valley --tiles ~/tiles/grose
```

Hand accepted points to ground teams:

```bash
pointersearch export missions/2025-06-01-grose-valley --format gpx -o accepted.gpx
```

One georeferenced mosaic for the incident map:

```bash
pointersearch merge missions/2025-06-01-grose-valley
```

## Aircraft settings that matter

* Gimbal pitch -90 (nadir). Frames flatter than 35 degrees are not mosaicked.
* Wide camera stills, timed or distance interval, 20-30 % side overlap is enough.
* Thermal palette **White Hot** so brightness is monotonic with temperature.
* Altitude by target: 60 m for dogs, 80-100 m for people. See the GSD table in
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
* Dawn, dusk or night for thermal. A deceased person at ambient temperature will
  not show in thermal; rely on `rx` and `bright`.

## Configuration

`pointersearch init` writes `pointersearch.json` into the mission directory. All
detector thresholds, object size limits, mosaic resolution and the optional DEM
path live there. See `pointersearch/config.py` for every field and its meaning.

## Tests

```bash
pytest
```

## Status

Phase one. Tested end to end on synthetic imagery only; thresholds need tuning on
real Matrice 4T frames. Radiometric thermal (DJI Thermal SDK) is wired but
unverified against hardware. See [docs/ROADMAP.md](docs/ROADMAP.md).
