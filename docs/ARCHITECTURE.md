# Architecture

```
SD card / upload folder
        |
        v
  metadata.py      read EXIF + drone-dji XMP (position, altitude, gimbal)
        |
        v
  georef.py        pixel -> ray -> ground (NED), FlatGround or GeoTIFF DEM
        |
        +--> mosaic.py     homography warp of frame onto north-up canvas -> PNG + bounds
        |
        +--> detectors/    rx.py, bright.py (RGB)  |  thermal.py (IR)
        |                        -> Candidate(px, py, score, bbox, area_m2)
        v
  pipeline.py      candidate pixel -> lat/lon, thumbnails, write to store.py (SQLite, WAL)
        |
        v
  review/server.py FastAPI + vendored Leaflet, accept/reject, export.py (GPX/KML/GeoJSON)
```

## Why direct georeferencing instead of a real orthomosaic

A structure-from-motion orthomosaic needs 70-80 % overlap, a complete block of
images, and minutes to hours of compute. A search coordinator needs to know where
a frame was looking within seconds of it landing. Projecting each frame onto the
ground using the aircraft's own position and gimbal angles gives a few metres of
error at 80-120 m AGL on moderate terrain, which is good enough to send a team to
a candidate. If a true orthomosaic is needed later for evidence, the same images
can be run through WebODM or DJI Terra.

Error sources, roughly in order: terrain relief (fixed by supplying a DEM via
`dem_path`), gimbal yaw error (about 1 degree, which is 1.5 m at 80 m), GPS
(1-3 m without RTK, centimetres with the RTK module), and barometric drift in
RelativeAltitude over a long flight.

## Detectors

**RX (Reed-Xiaoli).** Mahalanobis distance of each pixel's colour from the
frame's own colour distribution, computed in CIELAB with lightness down-weighted
so shadows and glare do not dominate. Threshold is the frame median plus
`k_mad` robust standard deviations, floored at `min_score`. Blobs outside
`[min_area_m2, max_area_m2]` are discarded, so the area filter automatically
adapts to the flight altitude through the ground sample distance. This is the
method behind most published wilderness-search tools and the commercial Loc8.

**Bright colour.** HSV saturation and value thresholds with green hues excluded.
It is scene-independent, so it catches a hi-vis vest even when the whole frame is
colourful and RX's statistics are diluted.

**Thermal.** Local background by a wide Gaussian blur, residual in robust sigma
units, intersected with the hottest percentile of the frame, then size-filtered.
Working on the 8-bit rendered JPEG is a limitation: a white-hot palette must be
used and absolute temperature is unknown. If the DJI Thermal SDK library is
available (`thermal.dirp_library`), the R-JPEG is decoded to Celsius and a
20-42 C band is enforced, which removes sun-heated rock and most vehicles.

## Ground sample distance (approximate, from published sensor specs)

| AGL   | Wide 48 MP | Thermal 640 | Person in thermal |
|-------|------------|-------------|-------------------|
| 60 m  | ~1.1 cm/px | ~6 cm/px    | ~8 x 30 px        |
| 100 m | ~1.8 cm/px | ~10 cm/px   | ~5 x 17 px        |
| 120 m | ~2.2 cm/px | ~12 cm/px   | ~4 x 14 px        |

A dog is 3-6 thermal pixels at 100 m; fly 50-60 m or use the tele camera.

## Data layout of a mission directory

```
missions/<name>/
  pointersearch.json   config
  pointersearch.db     SQLite: images, candidates, decisions
  overlays/            one north-up PNG per frame
  thumbs/              candidate crops for the review UI
  mosaic.png/.pgw      output of `merge`
```

Images are referenced by absolute path and never copied, so a mission directory
stays small; keep the SD card contents (or upload folder) alongside it.
