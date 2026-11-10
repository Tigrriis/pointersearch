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
```
