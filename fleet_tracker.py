#!/usr/bin/env python3
"""
Full path: open-fleet-tracker/fleet_tracker.py

SINGLE ENTRY POINT for the fleet tracker.

The display name shown in the console startup line, Discord embeds, and
email alerts is fully configurable via [general] app_name in config.ini
(see config.py / example.config.ini) -- nothing here is hardcoded.

This is the only file you ever run. Every other module in this project
(config.py, logging_setup.py, state_store.py, airports.py, weather.py,
opensky_client.py, notifier.py, events.py, tracker.py) is a local import --
there is nothing else to start, no second process, no second console.
On your Pelican server, point the process command at this one file, e.g.:

    python3 fleet_tracker.py

CLI flags (all preserved from the original single-file script):
    --test-discord           Send a Discord test message and exit
    --test-email             Send an email test message and exit
    --test-all                Send both a Discord and an email test message and exit
    --force-pending-alerts    Immediately send alerts for any pending landing
                               candidates (as "likely landing", unconfirmed),
                               then exit

With no flags, runs the normal continuous poll loop.
"""

import argparse
import sys
import time

from config import Config
from logging_setup import build_logger
from state_store import StateStore
from opensky_client import OpenSkyClient
from adsbfi_client import AdsbFiClient
from airports import AirportIndex
from notifier import Notifier
from events import EventLog
from tracker import Tracker


def build_app(config_path=None):
    config = Config(config_path)
    logger = build_logger(config)
    state = StateStore(config.state_file)
    opensky = OpenSkyClient(config)
    adsbfi = AdsbFiClient(config)
    airports = AirportIndex(config)
    events = EventLog(config)
    notifier = Notifier(config, events=events)
    tracker = Tracker(config, state, opensky, airports, notifier, events=events, adsbfi=adsbfi)
    return config, logger, state, tracker


def parse_args():
    parser = argparse.ArgumentParser(description="Fleet tracker (single entry point)")
    parser.add_argument("--config", default=None, help="Full path to config.ini (defaults to config.ini next to this script)")
    parser.add_argument("--test-discord", action="store_true", help="Send a Discord test message and exit")
    parser.add_argument("--test-email", action="store_true", help="Send an email test message and exit")
    parser.add_argument("--test-all", action="store_true", help="Send both a Discord and an email test message and exit")
    parser.add_argument(
        "--force-pending-alerts",
        action="store_true",
        help="Immediately send alerts for any pending landing candidates, then exit",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    config, logger, state, tracker = build_app(args.config)

    if args.test_all:
        discord_ok = tracker.notifier.send_discord_test()
        email_ok = tracker.notifier.send_email_test()
        logger.info("Test-all results: discord=%s email=%s", discord_ok, email_ok)
        # Matches the original script: both channels must succeed for exit 0.
        sys.exit(0 if (discord_ok and email_ok) else 1)

    if args.test_discord:
        ok = tracker.notifier.send_discord_test()
        logger.info("Discord test result: %s", ok)
        sys.exit(0 if ok else 1)

    if args.test_email:
        ok = tracker.notifier.send_email_test()
        logger.info("Email test result: %s", ok)
        sys.exit(0 if ok else 1)

    if args.force_pending_alerts:
        sent = tracker.force_pending_alerts()
        logger.info("Force-sent %d pending landing alert(s)", sent)
        sys.exit(0)

    logger.info(
        "%s starting up. Tracking %d ICAO24(s). Discord app_name=%s",
        config.app_name,
        len(config.icaos),
        config.discord_app_name,
    )
    logger.info("Config file: %s", config.config_path)
    logger.info("State file: %s", config.state_file)

    while True:
        try:
            tracker.run_periodic_jobs()

            aircraft_list, retry_wait = tracker.opensky.fetch_states()

            # These three early-outs match the original script exactly,
            # including which ones skip the trailing sleep below (via
            # `continue`) versus which ones sleep once and then continue.
            if aircraft_list is None:
                continue
            if retry_wait > 0:
                time.sleep(config.poll_seconds)
                continue

            # Fallback lookup (adsb.fi/adsb.lol) for any tracked ICAO24s
            # OpenSky's poll missed this cycle -- runs even when OpenSky
            # returned an empty list, since that's exactly when a fallback
            # hit matters most. No-ops entirely when [fallback] enabled is
            # false in config.ini.
            aircraft_list = tracker.merge_fallback_aircraft(aircraft_list or [])

            if not aircraft_list:
                time.sleep(config.poll_seconds)
                continue

            logger.info(
                "STATUS tracked=%d visible=%d landing_candidates=%d pending_confirmations=%d airborne_tracking=%s backoff=idle",
                len(config.icaos),
                len(aircraft_list),
                len(state.bucket("landing_candidates")),
                len(state.bucket("pending_confirmations")),
                tracker.airborne_tracking_tail_display(),
            )

            tracker.process_aircraft_list(aircraft_list)

        except KeyboardInterrupt:
            logger.info("Shutting down (KeyboardInterrupt)")
            break
        except Exception as e:
            logger.exception("Unhandled error in main loop: %s", e)

        time.sleep(config.poll_seconds)


if __name__ == "__main__":
    main()
