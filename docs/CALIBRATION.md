# Calibration checklist (pending)

Run on a real mission with the review map open, list sorted by score.

1. **Score floor.** Scroll the list until thumbnails stop being real objects and
   become ground texture, shadow edges or glint. Enter that score in *min RX*.
   Record the score and how many objects remain. That value becomes the
   default `rx.min_score` for that terrain type.
2. **Frame floor.** Set *min frames* to 2. Note anything you care about that
   disappears. Moving people will; static objects should not.
3. **Junk taxonomy.** For junk above the floor, note what it is in a few words
   each (roof glint, shadow edge, flagging tape, wet patch). Each has a
   different fix: lightness weighting, area limits, hue exclusions.
4. **Bright detector.** Were the orange markers in open grass anything real?
   If flowers or dry patches, raise `bright.min_saturation`.

Record results per terrain type (worksite, pasture, dry scrub, bush) in this
file so defaults can be chosen per mission.

## Notes from the first Matrice 4T flight (Lucaston)

- `_S` files are split-screen pictures (thermal left, visible right, black bars
  above and below). Before 0.2.0 they were searched as colour photos and produced
  false objects; now they are view-only.
- Thermal frames are 1280 x 1024 and use a white-hot palette with the hottest
  band in red. Before 0.2.0 red was read as mid grey, so the hottest spots were
  under-ranked. Daytime frames light up sun-warmed dead branches; expect many
  thermal candidates in daylight.

## Results

_None yet._
