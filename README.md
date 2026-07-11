# Open Fleet Tracker

Modular rewrite of the Open Fleet Tracker. Behavior is 100% preserved from the
original single-file script -- this is a structural refactor only, not a
feature change.

## Run one file, and only one file

You never run more than one process. `fleet_tracker.py` is the sole entry
point; every other `.py` file here is a plain local import used by it. On
your Pelican server, set the startup/process command to:

```
python3 /full/path/to/open-fleet-tracker-v2/fleet_tracker.py
```

There is nothing else to start alongside it -- no second console, no
background worker, no separate API process. The optional `[api]`/`[events]`
config sections below are placeholders/observability only; they do not spin
up a second process.

## Setup

1. `pip install -r requirements.txt` (only installs `pandas` and `requests`
   -- the OpenSky client is vendored, see below, so there's nothing else to
   fetch).
2. Copy `example.config.ini` to `config.ini` in this same directory and fill
   in your OpenSky credentials, Discord webhook, SMTP fallback, and the
   `icaos` list of aircraft to track.
3. Place your `airports.csv` and `runways.csv` (OurAirports format) in this
   directory, or point `[tracker] airports_csv` / `runways_csv` at their
   full paths.
4. Run it: `python3 fleet_tracker.py`

## Migrating from the original single-file script

No migration steps are needed. The state file defaults to `alert_state.json`
in this directory -- exactly the same filename the original script used --
so if you copy your existing `alert_state.json` into this folder, the
tracker picks up right where it left off (landing candidates, airborne
watch list, flight locks, seen-airborne flags, alert cooldowns, all
preserved). If you'd rather keep your state file at its current path,
set `[tracker] state_file = /full/path/to/alert_state.json` in `config.ini`.

## Module layout

| File | Responsibility |
|---|---|
| `fleet_tracker.py` | **The only file you run.** CLI flags, builds all components, runs the poll loop. |
| `config.py` | Loads `config.ini`, exposes every setting as a plain attribute. |
| `logging_setup.py` | One shared `steeljet` logger (rotating file handler + console handler). |
| `state_store.py` | Loads/saves `alert_state.json`; owns the bucket layout. |
| `airports.py` | Airport/runway CSV loading, nearest-airport + nearby-airports lookups. |
| `weather.py` | METAR/TAF lookups from aviationweather.gov. |
| `opensky_client.py` | OpenSky auth (basic or OAuth2 TokenManager), `/states/all` polling with 429 backoff, single-aircraft follow-up lookups. |
| `notifier.py` | Discord webhook (2 retries, `(5, 20)` timeout) with SMTP email fallback. |
| `events.py` | Optional append-only `events.jsonl` debug log (disabled by default). |
| `tracker.py` | All flight-phase/landing-detection logic: seen-airborne gate, flight locks, landing candidates, airborne watch list, alert sending. |
| `opensky_api.py` | Vendored, unmodified official OpenSky Python client. Not written by this project -- see the file header for the upstream source/license. Kept in this directory (not pip-installed) so no `git` access is needed at runtime. |

## CLI flags

Same flags as the original script:

```
python3 fleet_tracker.py --test-discord
python3 fleet_tracker.py --test-email
python3 fleet_tracker.py --test-all
python3 fleet_tracker.py --force-pending-alerts
```

With no flags, it runs the normal continuous poll loop.

## New optional config sections

Both are disabled by default and change nothing about existing behavior:

- `[api]` -- placeholder for a possible future local read-only status
  server. Not implemented; safe to ignore.
- `[events]` -- if you set `enabled = true`, the tracker appends one JSON
  line per notable event (candidate created, alert sent/suppressed, etc.)
  to `events_file`, useful for debugging why an alert did or didn't fire.

## OpenSky rate limits, credits, and the optional bounding box

OpenSky bills every `/states/all` call against a daily credit quota --
anonymous access gets 400 credits/day, a free registered account
(`client_id`/`client_secret` under `[opensky]`) gets 4,000/day, and an
active feeder gets 8,000/day
([official limits](https://openskynetwork.github.io/opensky-api/rest.html#limitations)).
Credit cost per call depends on the geographic area queried:

| Bounding box area | Credits/call |
|---|---|
| <= 25 sq deg | 1 |
| 25 - 100 sq deg | 2 |
| 100 - 400 sq deg | 3 |
| > 400 sq deg, or no bounding box (global) | 4 |

If you poll `get_states()` with no bounding box (the original default),
every call is billed at the 4-credit "global" tier. At `poll_seconds = 60`
that's 1,440 calls/day needed for 24/7 uptime x 4 credits = 5,760
credits/day -- more than even an authenticated 4,000/day account allows,
which is exactly what produces the "OpenSky returned no data" backoff
messages in the log once the daily quota runs out.

`config.ini` supports an optional bounding box to scope every query
(`fetch_states()` and the single-aircraft follow-up lookup both use it):

```ini
[tracker]
bbox_min_lat = 14.5
bbox_max_lat = 75
bbox_min_lon = -170
bbox_max_lon = -50
```

Set all four keys, or leave all four blank to fall back to the original
unrestricted global query -- a partial box raises a startup error. Any
aircraft outside the box will not be detected at all, so only use one if
your tracked aircraft never leave that region.

The example above covers Canada, the continental US + Alaska, Mexico, and
the Bahamas -- but at roughly 7,260 sq degrees, it's still comfortably
over the 400 sq degree cutoff, so it stays in the same 4-credit "global"
tier as no bounding box at all. It's useful for filtering out
irrelevant aircraft, but it does **not** reduce your credit usage. A box
tight enough to actually drop a credit tier would need to be a small
regional area (a few hundred miles across, not a continent).

The lever that actually controls your credit budget is `poll_seconds`. At
the 4-credit tier, an authenticated (4,000/day) account can sustain 24/7
polling no faster than about 86 seconds/poll with zero margin;
`example.config.ini` ships with `poll_seconds = 120` to leave headroom for
occasional follow-up lookups. Only poll faster than that if you've
narrowed the bounding box enough to drop into a cheaper credit tier, or
have a higher-tier OpenSky account.


## Deploying on Pelican (auto-updating from GitHub)

This project is designed to live in a GitHub repo and be deployed as a
custom Pelican egg that (a) auto-installs `pandas`/`requests` on every boot,
and (b) pulls new commits from GitHub whenever you click **Reinstall** in
the panel. `fleet_tracker.py` is still the only thing that ever runs --
Pelican just automates the `git pull` + `pip install` steps that used to be
manual.


### 1. Create a custom egg in the Pelican admin panel

Go to **Admin -> Eggs -> New Egg** (or **Import Egg** if you'd rather build
from a template) and fill in:

| Field | Value |
|---|---|
| **Docker Images** | A Python yolk, e.g. `ghcr.io/parkervcp/yolks:python_3.11` |
| **Startup Command** | `pip install --no-cache-dir -q -r requirements.txt && python3 fleet_tracker.py` |
| **Install Script -> Script Container** | `ghcr.io/parkervcp/installers:debian` |
| **Install Script -> Script Entry** | `bash` |
| **Install Script** | contents of [`pelican/install.sh`](pelican/install.sh) in this repo (pasted directly into the panel's script box) |

Why two different Docker images: the Install Script runs in a separate,
throwaway, root-privileged container just to fetch code (it needs `git`,
which the debian installer image has). The Startup Command runs in the
actual Python runtime container every time the server boots -- that
container has no root and doesn't keep whatever the install container had,
which is exactly why `pip install` is chained onto the front of the startup
command instead of done once during install.

### 2. Add two Variables to the egg

In the egg's **Variables** tab, add:

| Name | Environment Variable | Default value | User viewable/editable |
|---|---|---|---|
| Git Repository | `GIT_REPOSITORY` | `https://github.com/BLACKROOSTER73/open-fleet-tracker-v2.git` | Yes / Yes |
| Git Branch | `GIT_BRANCH` | `main` | Yes / Yes |

The install script reads these as `${GIT_REPOSITORY}` / `${GIT_BRANCH}`. If
your repo is private, put a personal access token directly in the URL
instead of using a separate credential field:
`https://<TOKEN>@github.com/<you>/steeljet-tracker.git`.

### 3. Create the server from that egg

Create a new server, pick your new egg, and set the two variables above to
your real repo URL/branch. On creation, Pelican runs the Install Script,
which clones your repo into the server's data directory and creates
`config.ini` from `example.config.ini` if one doesn't exist yet.

### 4. Upload the files git doesn't track

Over SFTP (the panel gives you SFTP credentials per-server), upload:

- Your filled-in `config.ini` (OpenSky credentials, Discord webhook, SMTP,
  and your `icaos` list) -- overwrite the auto-generated blank one.
- `airports.csv` and `runways.csv` (OurAirports format).
- Your existing `alert_state.json`, if migrating from a previous deployment,
  so alert history/cooldowns carry over.

None of these are ever touched by a later `git pull`/Reinstall, because
they're all listed in `.gitignore`.

### 5. Start the server

Start it from the panel. The Startup Command installs `pandas`/`requests`
(a few seconds, since `opensky_api.py` is vendored and needs no separate
install -- see the Module layout table above) and then runs
`python3 fleet_tracker.py` exactly as it would from a plain terminal.

### 6. Deploying updates later

Whenever you push new commits to GitHub:

1. Stop the server (optional, but avoids restarting mid-write).
2. Click **Reinstall** on the server in the panel. This re-runs the Install
   Script, which does `git fetch` + `git reset --hard origin/<branch>` to
   pull your latest code, without touching `config.ini` or any other
   gitignored file.
3. Start the server again. The Startup Command reinstalls Python packages
   (fast if versions didn't change) and launches the updated
   `fleet_tracker.py`.
