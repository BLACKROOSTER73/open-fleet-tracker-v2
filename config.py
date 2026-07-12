"""
Full path: steeljet_tracker/config.py

Configuration loader for SteelJet Tracker.

Reads config.ini (same file/format used by the original single-file
fleet_tracker.py) and exposes every setting as an attribute on a single
Config object, plus a couple of derived values (ICAOS set, AIRCRAFT_TYPES
dict). Section and key names are unchanged from the original script so an
existing config.ini keeps working without any edits.
"""

import configparser
import sys
from pathlib import Path


if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent


class Config:
    def __init__(self, config_path=None):
        self.base_dir = BASE_DIR
        self.config_path = Path(config_path) if config_path else BASE_DIR / "config.ini"

        parser = configparser.ConfigParser()
        if not parser.read(self.config_path):
            raise SystemExit(f"Could not read config file: {self.config_path}")
        self._parser = parser

        # ---- logging ----
        self.log_file = parser.get("logging", "log_file", fallback=str(BASE_DIR / "opensky_alerts.log"))
        self.log_max_bytes = parser.getint("logging", "max_bytes", fallback=5 * 1024 * 1024)
        self.log_backup_count = parser.getint("logging", "backup_count", fallback=5)

        # ---- opensky ----
        self.opensky_user = parser.get("opensky", "user", fallback="")
        self.opensky_pass = parser.get("opensky", "password", fallback="")
        self.opensky_client_id = parser.get("opensky", "client_id", fallback="")
        self.opensky_client_secret = parser.get("opensky", "client_secret", fallback="")

        # ---- discord ----
        self.discord_webhook_url = parser.get("discord", "webhook_url", fallback="")
        self.discord_avatar_url = parser.get(
            "discord", "avatar_url", fallback="https://i.imgur.com/4M34hi2.png"
        )
        self.discord_app_name = parser.get("discord", "app_name", fallback="SteelJet Tracker")

        # ---- smtp ----
        self.smtp_host = parser.get("smtp", "host", fallback="")
        self.smtp_port = parser.getint("smtp", "port", fallback=587)
        self.smtp_user = parser.get("smtp", "user", fallback="")
        self.smtp_password = parser.get("smtp", "password", fallback="")
        self.smtp_to = parser.get("smtp", "to", fallback="")
        self.smtp_from_name = parser.get("smtp", "from_name", fallback="SteelJet Tracker Alerts")

        # ---- tracker ----
        self.poll_seconds = parser.getint("tracker", "poll_seconds", fallback=60)
        self.max_backoff = parser.getint("tracker", "max_backoff", fallback=1800)
        self.airports_csv = Path(parser.get("tracker", "airports_csv", fallback=str(BASE_DIR / "airports.csv")))
        self.runways_csv = Path(parser.get("tracker", "runways_csv", fallback=str(BASE_DIR / "runways.csv")))
        self.airport_lookup_radius_miles = parser.getfloat("tracker", "airport_lookup_radius_miles", fallback=20.0)
        self.min_runway_length_ft = parser.getint("tracker", "min_runway_length_ft", fallback=4000)
        self.altitude_threshold_ft = parser.getfloat("tracker", "altitude_threshold_ft", fallback=1500.0)
        self.altitude_reset_ft = parser.getfloat("tracker", "altitude_reset_ft", fallback=2000.0)
        self.low_alt_descent_threshold_ft = parser.getfloat(
            "tracker", "landing_descent_threshold_ft", fallback=10000.0
        )
        # Same default filename as the original single-file tracker, so
        # upgrading in place does not require any state migration.
        self.state_file = Path(
            parser.get("tracker", "state_file", fallback=str(BASE_DIR / "alert_state.json"))
        )

        icaos_raw = parser.get("tracker", "icaos", fallback="")
        self.icaos = set(icao.strip().lower() for icao in icaos_raw.split(",") if icao.strip())
        if not self.icaos:
            raise SystemExit("No ICAO24 codes loaded; check the 'icaos' line in config.ini")

        # Optional bounding box (min_lat, max_lat, min_lon, max_lon), all in
        # WGS-84 decimal degrees. Leave any of these four blank in config.ini
        # to fall back to an unrestricted/global states query (the original
        # behavior). Filling them in narrows every OpenSky poll to a region,
        # which cuts the API credit cost per call (OpenSky bills by bounding
        # box area: <=25 sq deg = 1 credit, 25-100 = 2, 100-400 = 3, >400 or
        # global = 4 credits -- see https://openskynetwork.github.io/opensky-api/rest.html#limitations).
        # Only use this if the aircraft you track never leave the box you
        # configure, since anything outside it will not be returned at all.
        bbox_raw = (
            parser.get("tracker", "bbox_min_lat", fallback="").strip(),
            parser.get("tracker", "bbox_max_lat", fallback="").strip(),
            parser.get("tracker", "bbox_min_lon", fallback="").strip(),
            parser.get("tracker", "bbox_max_lon", fallback="").strip(),
        )
        if all(bbox_raw):
            self.bbox = tuple(float(v) for v in bbox_raw)
        elif any(bbox_raw):
            raise SystemExit(
                "Partial bounding box in config.ini -- set all four of "
                "bbox_min_lat/bbox_max_lat/bbox_min_lon/bbox_max_lon, or none of them."
            )
        else:
            self.bbox = None

        # ---- alerts ----
        self.landing_recheck_sec = parser.getint("alerts", "landing_recheck_seconds", fallback=300)
        self.landing_alt_margin_ft = parser.getfloat("alerts", "landing_alt_margin_ft", fallback=250.0)
        self.alert_cooldown_sec = parser.getfloat("alerts", "cool_down_seconds", fallback=300.0)
        self.climb_delta_ft = parser.getfloat("alerts", "climb_delta_ft", fallback=250.0)
        self.landing_on_ground_hold_sec = parser.getint("alerts", "landing_on_ground_hold_seconds", fallback=480)
        self.landing_on_ground_min_polls = parser.getint("alerts", "landing_on_ground_min_polls", fallback=2)
        self.landing_no_position_timeout_sec = parser.getint(
            "alerts", "landing_no_position_timeout_seconds", fallback=1080
        )
        self.airborne_tracking_altitude_ft = parser.getfloat(
            "alerts", "airborne_tracking_altitude_ft", fallback=5000.0
        )
        self.followup_delay_sec = parser.getint("alerts", "followup_delay_seconds", fallback=1200)
        # Altitude above which an aircraft counts as "seen airborne" outright.
        self.seen_airborne_altitude_ft = parser.getfloat("alerts", "seen_airborne_altitude_ft", fallback=500.0)

        # ---- quiet-timeout landing confidence scoring ----
        # A candidate going quiet (no fresh position/contact update at all)
        # is not, by itself, reliable evidence of a landing -- OpenSky state
        # vectors are snapshots, and position/velocity fields can already go
        # stale/absent after roughly 15 seconds without a fresh update, while
        # on_ground is not guaranteed to be reported right at touchdown.
        # Once landing_no_position_timeout_sec has elapsed with no updates,
        # score the candidate on multiple weaker signals instead of trusting
        # silence alone; only send a "likely landing" once the score clears
        # landing_quiet_confidence_threshold. If it never clears the
        # threshold, keep waiting until landing_quiet_hard_cap_seconds, then
        # give up and drop the candidate rather than alert on weak evidence.
        self.landing_quiet_confidence_threshold = parser.getint(
            "alerts", "landing_quiet_confidence_threshold", fallback=5
        )
        self.landing_quiet_hard_cap_sec = parser.getint(
            "alerts", "landing_quiet_hard_cap_seconds", fallback=2700
        )
        self.landing_quiet_low_alt_ft = parser.getfloat(
            "alerts", "landing_quiet_low_alt_ft", fallback=1500.0
        )
        self.landing_quiet_current_alt_ft = parser.getfloat(
            "alerts", "landing_quiet_current_alt_ft", fallback=2500.0
        )
        self.landing_quiet_speed_kn = parser.getfloat(
            "alerts", "landing_quiet_speed_kn", fallback=160.0
        )

        # ---- weather ----
        self.weather_timeout_sec = parser.getint("weather", "timeout_seconds", fallback=12)

        # ---- aircraft_types ----
        self.aircraft_types = {}
        if parser.has_section("aircraft_types"):
            for k, v in parser.items("aircraft_types"):
                self.aircraft_types[k.strip().lower()] = v.strip()

        # ---- api: optional local read-only status server, disabled by default ----
        self.api_enabled = parser.getboolean("api", "enabled", fallback=False)
        self.api_host = parser.get("api", "host", fallback="127.0.0.1")
        self.api_port = parser.getint("api", "port", fallback=8787)

        # ---- events: optional append-only event log, disabled by default ----
        self.events_enabled = parser.getboolean("events", "enabled", fallback=False)
        self.events_file = Path(
            parser.get("events", "events_file", fallback=str(BASE_DIR / "events.jsonl"))
        )

    def get_aircraft_type(self, icao24):
        return self.aircraft_types.get((icao24 or "").strip().lower(), "N/A")
