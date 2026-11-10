# Route planner

Draw a search route on the laptop and export it as a KMZ that DJI Pilot 2 flies,
taking photos at an even spacing along the whole route.

## Open it

Start the app, create or pick a mission on the start screen, and choose
*Plan route*. A new mission opens straight into the planner, so planning works
before any imagery has been collected.

From the command line, `pointersearch plan missions/<name>` opens the planner for
one mission at http://127.0.0.1:8008/plan.

## Base map

- **Online.** With internet, satellite and street maps load automatically.
  Plan at the office or on a phone hotspot before leaving coverage.
- **Offline.** Start with `--tiles <folder>` pointing at pre-downloaded XYZ tiles.
- **Neither.** Imagery already processed into the mission shows as a backdrop,
  and *lat, lon* in the header jumps to a coordinate.

## Two ways to draw

| Mode | Draw | Result |
|---|---|---|
| Line route | points along a track, creek, ridgeline or fence | the aircraft follows the line |
| Search area | the corners of the area | back-and-forth sweep lines covering it |

Click to add a point, drag to move it, right-click to delete it, Ctrl+Z to undo
the last one. Drawing is disabled while zoomed far out, to prevent accidental
country-sized routes; zoom in or use the *lat, lon* box first.

## Saving and editing routes

*Save route* stores the drawing and its settings in the mission. Saved routes are
listed at the top of the panel:

- **Open** loads one back into the planner to change points or settings. Saving
  again under the same name updates it; changing the name saves a copy.
- **KMZ** downloads the file for Pilot 2 without opening the route.
- **✕** deletes it, after a confirmation.

Each route is one small JSON file in `<mission>/routes/`. It records what was
drawn and chosen, not the generated waypoints, so it stays editable and the
waypoints are recalculated every time it is opened or downloaded:

```json
{
  "format": "pointersearch-route", "version": 1,
  "id": "north-block", "name": "North block",
  "mode": "area",
  "points": [[-41.5630, 146.9990], [-41.5630, 147.0038], [-41.5603, 147.0038]],
  "settings": {"height_m": 90, "speed_ms": 8, "lens": "wide",
               "front_overlap": 0.25, "side_overlap": 0.25, "angle_deg": null,
               "interval_m": null, "interval_s": null, "photo_lenses": "wide,ir"},
  "created": "2026-11-10T09:12:00", "updated": "2026-11-10T09:40:00"
}
```

Points are `[latitude, longitude]`. The files can be copied between missions or
edited in a text editor. The unsaved working drawing is also remembered per
mission in the browser.

## Settings

| Setting | Default | Notes |
|---|---|---|
| Height | 90 m | above the take-off point; refused above `flight.max_height_m` (120 m) |
| Speed | 8 m/s | 1 to 15 |
| Space photos for | wide camera | choose thermal when the thermal image must have no gaps; its frame is under half the width |
| Photo overlap | 25 % | between consecutive photos along the route |
| Line overlap | 25 % | between neighbouring sweep lines (area mode) |
| Photo interval | from overlap | or set directly as metres or seconds |
| Sweep direction | along longest side | or a bearing, for example to fly across a slope or into wind |
| Store photos from | wide, thermal | which lenses are saved at each photo |

The panel shows photo count, spacing, route length, a time estimate, the frame
size on the ground, the detail level (centimetres per pixel and how many pixels
wide a person is), and warnings.

Frame size on the ground, gimbal straight down:

| Height | Wide frame | Thermal frame |
|---|---|---|
| 60 m | 87 x 65 m | 38 x 31 m |
| 90 m | 130 x 97 m | 57 x 46 m |
| 120 m | 173 x 130 m | 76 x 61 m |

## What the aircraft does

- Climbs vertically to the route height at take-off, flies to the first waypoint.
- Points the gimbal straight down and starts taking a photo every set distance
  (or time) until the last waypoint.
- Stops and turns at each waypoint, so it flies exactly the drawn line.
- Returns home at the route height.

The photos then go through `pointersearch process` or `watch` as usual.

## Warnings worth acting on

- **Photos too close in time.** The camera needs time between multi-lens stills;
  2 s is assumed (`route.min_photo_interval_s`). Slow down or widen the spacing.
- **Gaps between photos.** The spacing is longer than the frame.
- **Over the time budget.** Split the area; `flight.max_flight_min` is 30.
- **Thermal spacing without thermal stored.** Tick thermal under stored lenses.

## Limits

- Heights are relative to take-off. The planner knows nothing about terrain,
  trees, towers or wires.
- The time estimate excludes the flight from take-off to the first waypoint and
  back. Watch the battery.
- Even-interval photo capture and the Matrice 4T aircraft codes follow DJI's
  published waypoint format but have not been flown from this tool yet. See
  "Not yet verified" in [FOLLOWUP_MISSIONS.md](FOLLOWUP_MISSIONS.md); the same
  `pointersearch wpml-info` check applies.
- Sweep lines on a concave area run straight across any notch in the boundary.
