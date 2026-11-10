# Using the app

Everything can be done from the browser. The only thing to start is the app
itself.

## Starting and stopping

- **Windows:** double-click `PointerSearch.bat`. A small window opens and stays
  open; your browser opens the start screen. Close that window to stop the app.
  The first run installs what it needs and takes a few minutes with internet.
- **Otherwise:** run `pointersearch app`.

Starting it a second time just opens the browser on the copy already running.
The address is http://127.0.0.1:8008/.

Options, for a shortcut or the command line: `--root <folder>` to keep missions
somewhere else (default is a `missions` folder beside the app), `--tiles
<folder>` for offline base maps, `--port`, `--no-browser`.

## Start screen

- **New mission.** Type a name and press *Create mission*. It opens the route
  planner, because a new search usually starts with a plan.
- **Missions.** One card per mission with its saved routes, images and objects
  still to review, most recently changed first. Each has *Plan route*, *Add
  images* and *Review map*.

A mission is a folder inside the missions folder. Backing up or moving a mission
is copying that folder. Photos are not copied into it; it remembers where they
are, so keep the photo folder in place.

## Which version is running

The version number is shown in small text beside the name at the top of every
page, for example `v0.2.0`. After updating, close the app's window and start it
again. If there is no version number, you are looking at a copy from before
0.2.0.

## Adding images

On the review map press *Add images…*, choose the folder that holds the photos
(for example the DCIM folder on the SD card), and press *Add images*.

- *Browse…* opens the normal folder window on the computer running the app. If
  that is not available, paste the folder path.
- Progress shows in the window and in the header. The map fills in as images
  are processed, and you can start reviewing straight away.
- *Keep watching this folder* carries on after the first pass and picks up new
  photos as they land, until you press *Stop*.
- Adding the same folder again only processes photos that are new.
- Tick *Search again images that are already in the mission* after an update
  that changes how images are searched. Accept, follow-up and reject decisions
  are kept; undecided detections are replaced.

## Looking at an object through each lens

The Matrice 4T saves a wide, a zoom and a thermal file for every shutter press.
When you select an object on the review map, tabs above its picture switch
between those lenses for the same moment:

- **Wide / Zoom / Thermal.** The close-up is centred on the object in each lens,
  with a box around it. The dot marks the lens the object was found in.
- A lens is crossed out when its frame did not contain the object, which is
  common for the zoom lens because its view is much narrower. Hover for the reason.
- **Split.** If the aircraft also saved its split-screen picture (the `_S` file,
  thermal and visible side by side), it gets a tab too and is shown whole. It is
  for looking at only: it is not searched for objects and not put on the mosaic,
  because it is two views in one picture.
- If a shot has only one lens file, a note under the tabs' place says so.
- **V** steps through the available lenses. Your choice is remembered, so if you
  prefer thermal, each object opens in thermal where it is available.
- **F** and *full image* open the whole frame of the lens being shown.
- An object is usually seen in several shots. The small pictures under the main
  one are those shots; tap one to look at it, with its own lens tabs. By default
  the shot shown is one where the most lenses had the object in frame.

For this to work the wide, zoom and thermal files need to be in the same folder
when the images are added, with their original names. Close-ups are made the
first time you look and kept in the mission's `views` folder.

If the box sits consistently to one side of the object in the thermal or zoom
view, the lenses are not perfectly parallel. Set `lens_align_deg` in the
mission's `pointersearch.json`, in degrees right and down, for example
`"thermal": [0.4, -0.2]`.

## Other devices on the same network

Started with `--host 0.0.0.0`, the review map and planner can be opened from a
tablet or second laptop on the same hotspot. Creating missions, adding images
and deleting saved routes are only allowed from the computer running the app.

## Where things live

```
missions/
  2026-11-10 Oaks Road/
    pointersearch.json     settings for this mission
    pointersearch.db       images, objects, review decisions
    routes/*.json          saved routes (editable)
    overlays/ thumbs/      generated imagery
    views/                 close-ups through other lenses, made on demand
```
