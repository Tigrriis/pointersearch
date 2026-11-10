# The map: how photos are laid down and lined up

## Three ways to make a map from drone photos

| | Placed as recorded | Lined up (what pointersearch does) | True orthophoto |
|---|---|---|---|
| How | each photo projected from the aircraft's GPS, height and gimbal angles | as recorded, then photos correct one another by matching features where they overlap | full photogrammetry: camera positions solved in 3D, a surface model built, every photo re-projected onto it |
| Overlap needed | any | about 20 % or more | 70 to 80 % forward, 60 to 70 % side |
| Time | under a second a photo | under a second a photo | minutes to hours |
| Typical error between neighbouring photos | several metres; tens of metres over forest | well under a metre on open ground | centimetres |
| Tall objects and slopes | lean and stretch | lean and stretch | corrected |
| Works with search flights | yes | yes | no, they overlap too little |

A search flight is spaced to cover ground quickly, around 25 % overlap, so the true
orthophoto method cannot be used on it. The middle column is the best that can be
done with those photos, and it runs as they arrive.

## What "lined up" does

1. **Features.** Distinct points are found in each photo.
2. **Links.** Where two photos overlap, the same points are matched. A robust fit
   keeps only matches that agree with one another.
3. **Solve.** One correction per photo (shift, turn, scale) is solved for all
   photos together, so matched points land on the same spot while each photo stays
   near its recorded GPS position.
4. **Lay down.** Each photo is re-laid at its corrected place, fading out towards
   its edges so that joins are soft, with brightness evened out between photos.

What this fixes:

- **GPS error** of a few metres per photo, relative to its neighbours.
- **Heading error** of a degree or two.
- **Surface height.** The aircraft records its height above the take-off point.
  Over forest the visible surface is the canopy, tens of metres closer, so photos
  come out too large and neighbours disagree by tens of metres. Two photos taken a
  known distance apart only agree at the right scale, so the scale, and with it
  the canopy height, is recovered automatically.
- **Detections move with their photo**, so markers sit on what they mark, and the
  same object seen in two photos merges into one.

Measured on test flights with known truth:

| Errors in the aircraft's record | Placement error as recorded | Lined up |
|---|---|---|
| GPS 3 m, heading 2° | 4.1 m typical, 11.4 m worst | 0.6 m typical, 0.9 m worst |
| Same, with the surface 30 m above take-off level | 26.5 m typical, 36.4 m worst | 0.7 m typical, 1.2 m worst |
| None | 0 | 0.04 m |

What remains after lining up is the average GPS error of the whole block, which no
matching between photos can remove. Only a surveyed ground point or RTK would.

## Thermal

Thermal photos have too little detail to match reliably, and they don't need to
be matched: each is taken at the same instant as a wide photo, through the same
gimbal. So a thermal photo takes the correction of its wide photo. Zoom photos do
the same. A thermal-only flight is matched thermal to thermal, as well as it can be.

On the review map, **map** in the header switches between *colour*, *thermal*,
*colour + zoom* and *none*.

The thermal lens sees under half the width of the wide lens. On a flight spaced
for the wide lens the thermal map therefore has gaps between photos. For a
continuous thermal map, plan the route with *Space photos for: thermal camera*.

## Safeguards

Feature matching can be fooled where everything looks alike. Three checks keep a
bad match from damaging the map; when in doubt the photo stays as recorded:

- A link must agree with what the GPS positions allow. With a raised surface,
  every pair of photos disagrees along the line between the two cameras by the
  same fraction of their distance; a link that disagrees differently is rejected.
- Links the solution cannot satisfy are dropped and the solve is repeated.
- A solution that moves photos much further than GPS error allows is discarded
  entirely.

The header shows *map lined up: N/M photos*. Hover over it for how far apart
matched points were before and after. Photos that could not be matched (water,
uniform ground, no overlap) are placed as recorded.

## Limits

- It is a flat, two-dimensional adjustment. Tall trees still lean away from the
  middle of each photo, and steep terrain is not corrected. Fading the edges of
  each photo hides the worst of this, because the middle of a photo is where
  things lean least.
- Photos must overlap for there to be anything to match. Lines flown with gaps
  between them stay as recorded relative to each other.
- It has been validated on generated test flights with known truth, not yet
  against surveyed points on a real flight.

## Files and commands

```
missions/<name>/
  overlays/            each photo as first placed
  overlays_aligned/    each photo re-laid at its corrected place
  features/            the distinct points of each photo
```

```bash
pointersearch align missions/<name>     # solve again (runs automatically after adding photos)
pointersearch merge missions/<name>     # mosaic.png (colour) and mosaic_thermal.png, each with a world file
```

Settings are under `align` in the mission's `pointersearch.json`. `enabled: false`
turns lining up off. `feather` is how much of each photo's edge fades out.

## When a true orthophoto is needed

For evidence, measurement or a base map of an area you will search repeatedly:

1. Fly it as a mapping flight: 75 % forward and 65 % side overlap, gimbal straight
   down. In the route planner set *Photo overlap* 75 and *Line overlap* 65.
2. Process the photos in a photogrammetry package. [OpenDroneMap](https://opendronemap.org)
   (WebODM) is free and open source; DJI Terra, Pix4D and Metashape are commercial.

Expect three to four times as many photos and flight time for the same area, and
minutes to hours of processing. Dense forest is hard for every package, because
canopy moves in the wind and repeats.
