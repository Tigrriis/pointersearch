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
5. **Group** sightings of the same object across overlapping frames, so the
   reviewer sees one marker per object with "seen in N frames".
6. **Review** on a local Leaflet map: thumbnails, accept/reject with keyboard,
   filters per detector, live refresh while the aircraft is still flying. Switch
   each object between the wide, zoom and thermal frames of the same moment.
7. **Export** accepted objects as GPX, KML or GeoJSON for ground teams.
8. **Plan search routes**: draw a line or a search area on the map and export a
   route for DJI Pilot 2 that takes photos at an even spacing
   (see [docs/ROUTE_PLANNER.md](docs/ROUTE_PLANNER.md)).
9. **Follow-up missions**: tag objects for a second look and export a waypoint
   route for DJI Pilot 2 that revisits each one at a lower height
   (see [docs/FOLLOWUP_MISSIONS.md](docs/FOLLOWUP_MISSIONS.md)).

## Two parts

- **Hosted planner** - logins, missions, route planning and KMZ export, reached
  from a tablet or any browser. Deployed to Render from `render.yaml`. See
  [docs/WEB_APP.md](docs/WEB_APP.md).
- **Laptop app** - image processing, the review map and follow-up missions, fully
  offline at the search base. It also contains the planner, for days with no
  coverage.

Routes move between the two as small route files (File / Import file… in the
planner).

## Start the laptop app

**Windows:** double-click `PointerSearch.bat` in this folder. The first run sets
itself up (needs internet once); after that it opens the start screen in your
browser. Keep its window open while you work and close it to stop.

**Any system**, after installing as below:

```bash
pointersearch app
```

From the start screen you create a mission, plan and save routes, add the images
from a flight, and review candidates, with no further commands. See
[docs/APP.md](docs/APP.md). The individual commands further down still work for
scripting.

## Install

Python 3.10+ on the base laptop.

Linux / macOS:

```bash
git clone https://github.com/Tigrriis/pointersearch && cd pointersearch
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

Windows (PowerShell, one line at a time; `&&` is not supported):

```powershell
git clone https://github.com/Tigrriis/pointersearch
cd pointersearch
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

If `Activate.ps1` is blocked, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`
first, or skip activation and call `.venv\Scripts\pointersearch.exe` directly.

Try it without an aircraft (generates a synthetic mission with planted targets):

```bash
pointersearch demo demo
```

```bash
pointersearch serve demo
```

Then open http://127.0.0.1:8008/ in a browser.

## Field workflow

Plan the search route, download the KMZ and import it in DJI Pilot 2:

```bash
pointersearch plan missions/2025-06-01-grose-valley
```

After each battery, pull the SD card and run:

```bash
pointersearch process missions/2025-06-01-grose-valley /media/sdcard/DCIM/DJI_001   # Windows: E:\DCIM\DJI_001
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

A waypoint route back over the objects tagged follow-up (heights in metres above take-off):

```bash
pointersearch mission missions/2025-06-01-grose-valley --transit 90 --inspect 60
```

One georeferenced mosaic for the incident map:

```bash
pointersearch merge missions/2025-06-01-grose-valley
```

## Aircraft settings that matter

* Gimbal pitch -90 (nadir). Frames flatter than 35 degrees are not mosaicked.
* Wide camera stills, timed or distance interval, 20-30 % side overlap is enough.
* Thermal palette where hotter is brighter: **White Hot**, or a white-hot palette
  that paints the hottest band red. Rainbow-style palettes will not work.
* The split-screen `_S` pictures are kept for viewing but not searched.
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
