# Follow-up missions

Objects worth a second look can be turned into a waypoint route that DJI Pilot 2
flies on a later sortie.

## Workflow

1. In the review map, select an object and press **W** (or the *Follow-up*
   button). Follow-up is separate from *Accept*: accept means "send a ground
   team", follow-up means "fly back and look closer".
2. Tagged objects are joined by a dashed line in planned visiting order.
3. Press **Mission…**, set the two heights, read the estimate and warnings, and
   download the KMZ. Or from the command line:

   ```bash
   pointersearch mission missions/<name> --transit 90 --inspect 60
   ```

4. Copy the KMZ to the controller and import it in DJI Pilot 2
   (Flight Route, Import Route, KMZ/KML).
5. **Check the route on the Pilot 2 map before flying it.** Confirm the waypoint
   positions, both heights, and the return-to-home height.
6. Process the new photos into the same mission directory. Each follow-up photo
   carries the object id in its filename (`..._PS1234.JPG`).

## What the aircraft does

- Climbs vertically to transit height at the take-off point, then flies level
  to the first object.
- At each object: stops overhead at transit height, descends vertically to
  inspection height, points the gimbal straight down, settles for a few
  seconds, takes one still per configured lens, climbs back to transit height.
- Returns home at transit height when the last object is done.
- On loss of the controller link it returns home (configurable).

## Heights

| | Default | Typical upper setting |
|---|---|---|
| Transit | 90 m | 120 m |
| Inspection | 60 m | 90 m |

Both are **metres above the take-off point**, not above the ground under the
aircraft. If the search area is uphill of the launch site, the real clearance is
smaller by the height difference, so raise both. The export refuses a transit
height above `flight.max_height_m` (120 m) or an inspection height above the
transit height.

Ground coverage of the wide camera over an object, for judging whether the
position error of a few metres is comfortably inside the frame:

| Inspection height | Wide frame on the ground | Ground sample distance |
|---|---|---|
| 60 m | about 85 x 64 m | about 1.1 cm/px |
| 90 m | about 128 x 96 m | about 1.6 cm/px |

## Settings

Under `followup` in the mission's `pointersearch.json`. Rows marked `flight.` sit in
the `flight` section, which the route planner shares:

| Key | Default | Meaning |
|---|---|---|
| `transit_height_m`, `inspect_height_m` | 90, 60 | default heights |
| `transit_speed_ms`, `vertical_speed_ms` | 10, 3 | speeds between and over objects |
| `hover_s` | 3 | settle time before the photo |
| `photo_lenses` | `wide,zoom,ir` | lenses stored at each object |
| `gimbal_pitch_deg` | -90 | gimbal angle for the photo |
| `approach_offset_m` | 0 | see troubleshooting |
| `flight.max_height_m` | 120 | hard ceiling for the export |
| `flight.finish_action` | `goHome` | what happens after the last object |
| `flight.rc_lost_action` | `goBack` | behaviour on loss of controller link |
| `flight.max_flight_min` | 30 | warn when the estimate is longer |

## Not yet verified on the aircraft

The file format follows DJI's published waypoint specification (WPML), but two
things could not be confirmed without a Matrice 4T and Pilot 2:

- **Aircraft and payload codes.** DJI's public table predates the Matrice 4T.
  The defaults (aircraft 99, sub-type 1, payload 89) are the values community
  tools use. To confirm them, create any small waypoint route in Pilot 2 on your
  Matrice 4T, export it, and run:

  ```bash
  pointersearch wpml-info exported_route.kmz
  ```

  It prints the codes that aircraft writes, the lens names it uses, and a
  snippet to paste into the `flight` section of `pointersearch.json`.
- **Stacked waypoints.** The descent is two waypoints at the same position and
  different heights. If Pilot 2 rejects the route with a "waypoints too close"
  style error, set `approach_offset_m` to 3. That moves the transit-height
  waypoints a few metres back along the inbound leg; the photo is still taken
  directly over the object.

## Limits

- Positions come from direct georeferencing: a few metres on nadir frames, more
  on oblique ones. The inspection frame is far larger than that error.
- A moving target (dog, walking person) will not be where it was. Treat its
  waypoint as the place to start a manual look.
- The route is planned in straight lines at transit height with no knowledge of
  terrain, trees, towers or wires. That check is the pilot's.
- The time estimate is rough. Watch the battery, not the estimate.
