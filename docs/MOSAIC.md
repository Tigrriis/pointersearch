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

## Exporting the map as a GeoTIFF

A GeoTIFF is a picture that carries its own position and coordinate system, so
GIS software (QGIS, ArcGIS, Global Mapper, CalTopo desktop) puts it in the right
place with nothing else to set up.

On the review map: **Export…**, then under *Map as a GeoTIFF* choose:

- **Lens.** Colour or thermal. Each is its own file. The thermal file is the
  thermal pictures as the aircraft rendered them (its palette), not temperatures.
- **Coordinate system.**
  - *GDA2020 / MGA zone N* - the Australian standard grid, in metres. Offered in
    Australia. Tasmania is zone 55 (EPSG:7855).
  - *WGS 84 / UTM zone N* - the same grid idea on the GPS datum, in metres.
    Available anywhere.
  - *WGS 84 latitude / longitude* (EPSG:4326) - degrees.
- **Detail.** The ground size of a pixel. The default is the photos' own detail.
  Larger pixels make a smaller file.

Press *Create GeoTIFF*, wait for the progress bar, then *Download*. The file is
also kept in the mission's `exports` folder.

How it is made, and what to expect:

- It is rendered from the **original photos**, not from the low-detail patches
  shown on the review map, so the card or folder must still be connected where
  the photos were added from. If a photo file cannot be found, its low-detail
  patch is used instead and the result says how many.
- Each photo is laid at its lined-up place and blended towards its middle, on a
  north-up grid of square pixels. Outside the photos the file is transparent.
- Very large areas: the pixel size is raised to keep the picture within about
  64 million pixels, and the result says so.
- **Datum.** Positions come from the aircraft's GPS, which is WGS 84. They are
  written into the MGA grid without a datum shift. In 2026 the two differ by
  about half a metre, from continental drift since 2020, which is within this
  map's accuracy.
- **Accuracy.** The same as the map: about a metre where photos lined up, as
  recorded where they did not. The file's description says it is not a surveyed
  orthophoto. Do not measure from it where that matters.

From the command line, both lenses at once:

```bash
pointersearch geotiff missions/<name> --crs mga
```

`--lens colour|thermal|both`, `--crs mga|utm|latlon`, `--gsd-cm 5`, `-o file.tif`.

Checked against GDAL (the library GIS software is built on): the coordinate maths
agrees with it to a ten-thousandth of a millimetre, it reads the coordinate
system, position and transparency from the files, and objects at known
coordinates are found within the map's accuracy of where the file says they are.

## Files and commands

```
missions/<name>/
  overlays/            each photo as first placed
  overlays_aligned/    each photo re-laid at its corrected place
  features/            the distinct points of each photo
  exports/             GeoTIFFs made from the review map
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
