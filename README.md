# Open Fleet Tracker

Tracks a list of user-defined aircraft via OpenSky, applies a few independent landing-confirmation checks (ground contact, airport elevation match, or a scored quiet-timeout), and notifies you through Discord (with email as a fallback) once it's confident one has landed.

Modular rewrite of the Open Fleet Tracker. Behavior is 100% preserved from the
original single-file script -- this is a structural refactor only, not a
feature change.

## Run one file, and only one file

You never run more than one process. `fleet_tracker.py` is the sole entry
point; every other `.py` file here is a plain local import used by it. On
your Pelican server, set the startup/process command to:

```
python3 /full/path/to/open-fleet-tracker/fleet_tracker.py
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
| `logging_setup.py` | One shared `open-fleet-tracker` logger (rotating file handler + console handler). |
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

The recurring console `STATUS ...` line's `airborne_tracking=` field now shows a comma-separated list of short tail numbers instead of a bare count -- each tracked aircraft's callsign has its leading letters stripped (e.g. `N6789G` displays as `6789`), falling back to the bare ICAO24 hex if no callsign is known yet, or `none` if nothing is currently being tracked as airborne.

## Landing detection: quiet-timeout confidence scoring

A landing candidate can resolve two ways:

1. **Ground-confirmed** -- OpenSky reports `on_ground` for long enough
   (`landing_on_ground_hold_seconds` / `landing_on_ground_min_polls`). This
   path is unchanged.
2. **Quiet timeout** -- OpenSky stops sending *any* position/contact update
   for the aircraft at all, for `landing_no_position_timeout_seconds`.

The quiet-timeout path used to send a "likely landing" as soon as the
timeout elapsed, with no other evidence. That's too eager: OpenSky state
vectors are snapshots, and individual position/velocity fields can already
go stale/absent after roughly 15 seconds without a fresh update, and
`on_ground` isn't guaranteed to be reported right at touchdown -- so an
aircraft can go quiet mid-flight (coverage gap, handoff between receivers,
etc.) just as easily as it can go quiet because it landed.

Instead, once the quiet timeout elapses, the candidate is scored on
multiple corroborating signals before it's treated as a likely landing:

| Signal | Points |
| --- | --- |
| `on_ground` was seen at any point for this candidate | +3 |
| Lowest tracked altitude is below `landing_quiet_low_alt_ft` (default 1500 ft) | +2 |
| Current (last known) altitude is below `landing_quiet_current_alt_ft` (default 2500 ft) | +2 |
| Last known vertical rate was negative (descending) | +1 |
| Last known speed is below `landing_quiet_speed_kn` (default 160 kn) | +1 |
| Aircraft had previously been marked "seen airborne" | +1 |
| It disappeared after entering candidate state (received at least one fresh update after being created as a candidate), not immediately on creation | +1 |

- **Score >= `landing_quiet_confidence_threshold`** (default 5): sends the
  "likely landing" alert, same as before.
- **Score below threshold**: no alert yet. The candidate keeps waiting and
  is rescored on every tick, so it can still clear the bar later (e.g. once
  altitude/speed data catches up). Once it's been quiet for
  `landing_quiet_hard_cap_seconds` (default 2700s / 45 min) and still hasn't
  cleared the threshold, it's logged as suppressed and dropped rather than
  alerting on weak evidence, and its flight lock is released so a genuine
  future landing for that aircraft isn't permanently blocked.

All of these are tunable under `[alerts]` in `config.ini` -- see
`example.config.ini` for the exact keys and defaults. `landing_no_position_timeout_seconds`
was also bumped from 900s to 1080s (18 min) as part of this change, since a
short whole-aircraft quiet window is itself weak evidence given the 15s
OpenSky staleness window mentioned above.

Source: [OpenSky REST API docs](https://openskynetwork.github.io/opensky-api/rest.html#limitations)
on state vector field staleness and `on_ground` behavior.

### Airport-elevation landing confirmation

On every tick, alongside the `on_ground` ground-hold check, each landing
candidate is also checked against the nearest matching airport's known
field elevation from `airports.csv`:

1. Resolve the nearest airport within `airport_lookup_radius_miles` for the
   candidate's last known lat/lon (same lookup already used to name the
   airport in alert text -- no new dependency).
2. If that airport's `elevation_ft` is within `landing_airport_elevation_margin_ft`
   (default 100 ft) of the candidate's current altitude, count it as a match.
3. Once matched for `landing_airport_elevation_min_polls` (default 2)
   consecutive polls in a row, confirm the landing immediately --
   same `confirmed=True` path as the `on_ground` ground-hold, sent right
   away rather than waiting on the quiet timeout.

This exists because `on_ground` isn't reported reliably by every
transponder/receiver pairing, but altitude is available whenever geo or
baro altitude is, so comparing it to a known field elevation is a solid
independent confirmation signal. It requires `airports.csv` to have an
`elevation_ft` column -- the standard [OurAirports](https://ourairports.com/data/)
schema already includes it, so if you're already using this project's
airport lookups for alert text, no extra download is needed. If the column
is missing, this check is silently skipped (everything else is unaffected).

Caveat: barometric altitude is referenced to standard sea-level pressure
(29.92 inHg), not the airport's actual local pressure, so on days with
unusual pressure it can be off from true elevation by more than a token
amount -- geo altitude (WGS84) doesn't have that specific issue but has its
own small biases. A 100 ft margin already gives some headroom for this;
tighten `landing_airport_elevation_margin_ft` if you want stricter matching,
or loosen it if you see it never triggering at airports whose barometric
readings tend to run further off. Every alert's body now also includes a
"Confirmation reason" line (`on_ground_hold`, `airport_elevation_match`, or
`quiet_timeout_score(...)`) so you can see which path fired.

## Rebranding: one config key controls everything announced to the user

Everything shown to you or your users -- the console startup log line, the
Discord embed username, and the email "From" name -- is driven by a single
`[general] app_name` key in `config.ini` (default: `Open Fleet Tracker`),
not hardcoded anywhere in the code:

```ini
[general]
app_name = My Custom Fleet
```

If you want just one channel to say something different, `[discord] app_name`
and `[smtp] from_name` still work as per-channel overrides -- leave either
blank to fall back to `[general] app_name` (with `" Alerts"` appended for
the email From name). See `example.config.ini` for both.

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

Whenever new updates get pushed to GitHub:

1. Stop the server (optional, but avoids restarting mid-write).
2. Click **Reinstall** on the server in the panel. This re-runs the Install
   Script, which does `git fetch` + `git reset --hard origin/<branch>` to
   pull the latest code, without touching `config.ini` or any other
   gitignored file.
3. Start the server again. The Startup Command reinstalls Python packages
   (fast if versions didn't change) and launches the updated
   `fleet_tracker.py`.
