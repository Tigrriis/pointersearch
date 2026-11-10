# The hosted planner

pointersearch runs in two places that share the same code:

| | Hosted planner | Laptop app |
|---|---|---|
| Where | Render (or any host), reached from a tablet, phone or PC | the laptop at the search base |
| Needs internet | yes | no |
| Does | logins, missions, route planning, saved routes, KMZ export | everything, including image processing and the review map |
| Start | `python -m pointersearch.web` | `PointerSearch.bat` / `pointersearch app` |
| Installs | FastAPI and Uvicorn only | plus NumPy, OpenCV, Pillow |

No imagery is ever uploaded to the hosted planner. It holds routes and logins.

## Deploying to Render

1. In the Render dashboard choose **New > Blueprint** and pick this repository
   and the branch to deploy. Render reads `render.yaml`.
2. Enter the three values it asks for:
   - `POINTERSEARCH_ADMIN_NAME` - the first administrator's name
   - `POINTERSEARCH_ADMIN_EMAIL` - their sign-in email
   - `POINTERSEARCH_ADMIN_PASSWORD` - a temporary password, at least 10 characters
3. Approve. Render builds the app, attaches a 1 GB disk at `/var/data`, and gives
   it an `https://…onrender.com` address.
4. Open that address, sign in with the email and temporary password, and choose
   your own password when asked.

The administrator values are used once, while no logins exist. After the first
sign-in they can be deleted from the service's environment; changing them later
does not create or reset anything.

What the blueprint sets up: one web service on the Starter plan in the Singapore
region (the closest to Australia), with a persistent disk. The disk is what keeps
missions and logins across deploys; without it everything would be lost on each
update. Both the plan and the disk are paid.

## People and logins

- Everyone has their own login. There is no shared password and no self sign-up.
- An administrator adds people under **People**: name, email, a temporary
  password (the *Suggest* button makes one) and a role.
- Give the temporary password to the person directly. Nothing is emailed.
- At first sign-in they must choose their own password before anything else works.
- **Members** create missions and plan, save, export and delete routes.
  **Administrators** can also add people, reset passwords, change roles and
  disable logins.
- *Reset password* and *Disable* sign that person out on every device at once.
- A forgotten password is fixed by an administrator resetting it. If every
  administrator is locked out, open a shell on the service and run
  `python -m pointersearch.web reset-password you@example.org`.

How it is protected: passwords are stored as salted scrypt hashes; sessions are
random tokens in an HttpOnly, Secure cookie and only their hash is stored; five
wrong passwords lock that login for a growing time; requests from other websites
are refused; nothing but the sign-in page is reachable when signed out.

## Using it

Sign in, create a mission, and plan routes exactly as in
[ROUTE_PLANNER.md](ROUTE_PLANNER.md). The start screen lists each mission's saved
routes with a download button for its KMZ, which is the quick path on a tablet:
download, then copy to the controller.

On a touch screen: tap the map to add a point, drag a point to move it, tap a
point to get a *Delete point* button.

## Moving routes between the hosted planner and the laptop

Routes are small files, and both apps read the same format.

- In either planner, each saved route has a **File** button that downloads
  `<route>.route.json`.
- In either planner, **Import file…** adds one or more of those files to the
  current mission.

So to see the planned routes over the imagery on the laptop's review map:
download the route files from the hosted planner, and import them into the
mission on the laptop. This works with no connection between the two.

## Running it yourself

To try the hosted planner on your own computer:

```bash
pointersearch web
```

It serves on http://127.0.0.1:8010 and keeps its data in `./webdata`. With no
logins yet, create the first one from a shell:

```bash
python -m pointersearch.web --data webdata add-user you@example.org "Your Name" --admin
```

Any host that can run a Python web service and give it a persistent folder will
do; the settings are `PORT`, `POINTERSEARCH_DATA` and the three
`POINTERSEARCH_ADMIN_*` values.

## Limits

- Missions are shared by everyone who can sign in. There are no private missions
  or per-mission permissions yet.
- Two people editing the same saved route at the same moment: the last save wins.
- The Matrice 4T file caveats in [FOLLOWUP_MISSIONS.md](FOLLOWUP_MISSIONS.md)
  apply to KMZ files from here as well.
