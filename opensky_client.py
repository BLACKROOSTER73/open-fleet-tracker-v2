"""
Full path: steeljet_tracker/opensky_client.py

Thin wrapper around the OpenSky python client: builds the authenticated
`OpenSkyApi` instance, polls `/states/all`, and applies the same 429 backoff
and follow-up single-aircraft lookup behavior as the original tracker.
"""

import logging
import random
import time

import requests
from opensky_api import OpenSkyApi, TokenManager

logger = logging.getLogger("steeljet")


class OpenSkyClient:
    def __init__(self, config):
        self.config = config
        if config.opensky_client_id and config.opensky_client_secret:
            tm = TokenManager(
                client_id=config.opensky_client_id,
                client_secret=config.opensky_client_secret,
            )
            logger.info("Using OpenSky TokenManager with client_id/client_secret")
        else:
            tm = None
            logger.info("No client_id/client_secret; will use basic auth (if user/pass given)")

        self.api = (
            OpenSkyApi(token_manager=tm)
            if tm
            else OpenSkyApi(config.opensky_user or None, config.opensky_pass or None)
        )
        self._consecutive_429 = 0

    def _parse_retry_wait(self, resp):
        x_retry = resp.headers.get("X-Rate-Limit-Retry-After-Seconds")
        if x_retry:
            try:
                return max(30, int(x_retry))
            except ValueError:
                pass
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(30, int(retry_after))
            except ValueError:
                pass
        return self.config.poll_seconds

    def fetch_states(self):
        """Returns (aircraft_list, retry_wait). aircraft_list is None on error."""
        while True:
            try:
                states = self.api.get_states()
                self._consecutive_429 = 0
                return states.states, 0
            except requests.exceptions.HTTPError as e:
                resp = getattr(e, "response", None)
                if resp is not None and resp.status_code == 429:
                    retry_seconds = self._parse_retry_wait(resp)
                    self._consecutive_429 += 1
                    backoff = min(
                        self.config.max_backoff,
                        max(retry_seconds, self.config.poll_seconds) * (2 ** (self._consecutive_429 - 1)),
                    )
                    backoff += random.uniform(0, 10)
                    logger.warning("429 from OpenSky; backing off for %.0f seconds", backoff)
                    time.sleep(backoff)
                    continue

                logger.exception("HTTP error while fetching states: %s", e)
                return None, 0
            except requests.exceptions.ConnectTimeout:
                logger.warning("OpenSky connect timeout; retrying after %d seconds", self.config.poll_seconds)
                time.sleep(self.config.poll_seconds)
                return None, 0
            except requests.exceptions.ReadTimeout:
                logger.warning("OpenSky read timeout; retrying after %d seconds", self.config.poll_seconds)
                time.sleep(self.config.poll_seconds)
                return None, 0
            except requests.exceptions.RequestException as e:
                logger.exception("Request error while fetching states: %s", e)
                return None, 0
            except Exception as e:
                logger.exception("Failed to fetch states: %s", e)
                return None, 0

    def get_current_aircraft_state(self, icao24):
        try:
            states = self.api.get_states(icao24=icao24)
            if not states or not states.states:
                return None
            for aircraft in states.states:
                if (aircraft.icao24 or "").lower() == icao24.lower():
                    return aircraft
        except Exception as e:
            logger.exception("Failed follow-up lookup for %s: %s", icao24, e)
        return None
