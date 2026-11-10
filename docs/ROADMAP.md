# Roadmap

## Phase 1 (this release)
- [x] DJI metadata reader, direct georeferencing, per-frame overlays, merged mosaic
- [x] RX colour anomaly, bright colour, thermal hot-spot detectors
- [x] SQLite store, offline Leaflet review UI, GPX/KML/GeoJSON export
- [x] Folder watcher, synthetic demo mission, test suite

## Phase 2: near-real-time ingest
- Self-hosted DJI Cloud API endpoint (MQTT broker + S3-compatible store, e.g.
  Mosquitto + MinIO) on the base laptop so DJI Pilot 2 auto-uploads stills over a
  local hotspot. Live telemetry gives the aircraft track on the review map.
- [x] Candidate de-duplication across overlapping frames (grouped within `group_radius_m`)
- [x] Follow-up tag and DJI Pilot 2 waypoint mission export (transit / inspection heights)
- Verify the exported route on a Matrice 4T: aircraft codes, stacked waypoints, lens names.
- Terrain-aware follow-up heights from a DEM.
- Aircraft track and searched-area coverage layer from frame footprints.

## Phase 3: learned detection
- YOLO-class person/animal detector fine-tuned on aerial SAR datasets (WiSARD,
  HERIDAL, SARD), run on stills alongside the unsupervised detectors.
- Verify the DJI Thermal SDK binding on real R-JPEGs; add temperature-band
  presets for person, dog, and ambient-cold conditions.

## Phase 4: operations
- Multi-reviewer sessions over the hotspot, reviewer attribution per decision.
- Probability-of-detection and coverage reporting per task area for the
  coordinator's debrief.
- Mobile SDK companion app on the RC Plus 2 only if a feature genuinely needs it
  (for example steering the aircraft to a candidate for a tele-lens look).
