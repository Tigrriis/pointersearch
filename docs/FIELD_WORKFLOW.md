# Field workflow

## Before the season

* Install pointersearch on the base laptop and run `pointersearch demo demo` once
  so the dependencies are proven offline.
* Download XYZ base-map tiles for your likely search areas while on internet
  (any tile downloader that writes `{z}/{x}/{y}.png`; zoom 12-17 is plenty).
  Point `pointersearch serve --tiles` at that folder in the field.
* Optionally download a DEM GeoTIFF for the area (Geoscience Australia ELVIS,
  5 m or 1 second SRTM) and set `dem_path` in the mission config. Install
  `pip install rasterio` for DEM support.
* Decide thresholds on real imagery from a training flight. Plant a jacket, a
  hi-vis vest and a dark pack; fly your standard profile; run `process`; adjust
  `rx.k_mad`, `bright.min_saturation` and the area limits until planted items
  rank near the top with a manageable number of false positives.

## On task

1. `pointersearch plan missions/<date-location>`, draw the search area or line,
   download the KMZ and import it in DJI Pilot 2. See
   [ROUTE_PLANNER.md](ROUTE_PLANNER.md).
2. Fly it: nadir gimbal and even-interval photos are in the route. Set the
   thermal palette to White Hot on the aircraft.
3. Ingest, either:
   * **SD card per battery.** `pointersearch process <mission> <DCIM folder>`.
     A 49-minute sortie is a natural batch and this needs no network.
   * **Continuous.** Run `pointersearch watch <mission> <folder>` against a
     folder that photos land in during flight. Two ways to feed it with no
     internet: a laptop Wi-Fi hotspot that the RC Plus 2 joins, with a small
     self-hosted DJI Cloud API server (MQTT + S3-compatible storage) so Pilot 2
     auto-uploads each still; or, simpler, a phone-tethered file sync from the
     RC's media folder. Both are phase-two items (see ROADMAP).
4. `pointersearch serve <mission>` and review on the laptop, or point another
   device on the same hotspot at `--host 0.0.0.0`.
5. Review order: thermal first at dawn/dusk, then `rx` by score, then `bright`.
   Keys: A accept, W follow-up, R reject, U undo, N/P next/previous, V switch
   between the wide, zoom and thermal view of the object, F open the full frame.
6. `pointersearch export <mission> --format gpx -o team_<n>.gpx` and load onto
   the team's GPS or into the incident mapping tool. GeoJSON with `--status all`
   preserves the full review record for the debrief.

7. For anything uncertain, tag it follow-up and export a waypoint route for the
   next battery: `pointersearch mission <mission>` or the Mission button. See
   [FOLLOWUP_MISSIONS.md](FOLLOWUP_MISSIONS.md).

## Target-specific notes

* **Missing person, alive.** Thermal at dawn/dusk/night is the fastest detector.
  Daytime RX for clothing and gear. Expect kangaroos, cattle and sun-warmed rock
  as thermal false positives; the size filter removes most large ones.
* **Deceased.** Thermal contrast is gone once the body reaches ambient. Rely on
  `rx` and `bright` for clothing, and lower `rx.min_area_m2` because the target
  may be partly covered. Fly lower (60-80 m) for more pixels on target.
* **Dogs.** Small and warm. Fly 50-60 m, or tele lens, thermal at dawn/dusk.
  Set `thermal.min_area_m2` to 0.05 and `max_area_m2` to 1.5. Dogs move: re-fly
  the probability area rather than widening it.

## Regulatory reminders (Australia)

Searches are police-led; operate when tasked by the SAR coordinator. Night
operations and beyond-visual-line-of-sight require CASA approval under your
ReOC. Keep the aircraft's flight record exports with the mission directory.
