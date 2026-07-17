"""
Full path: open-fleet-tracker/adsbfi_client.py

Optional fallback data source: adsb.fi, a free, community-run, ADS-B
Exchange v2-schema-compatible API (https://github.com/adsbfi/opendata).
adsb.lol runs the same schema and can be used by pointing [fallback]
base_url at it instead.

This is ONLY ever queried for tracked ICAO24s that OpenSky's own poll did
not return in a given cycle -- e.g. an aircraft sitting in an area with
sparser OpenSky receiver coverage than ADS-B Exchange's. It never replaces
OpenSky as the primary source, and fleet_tracker.py's main loop only calls
into this at all when [fallback] enabled = true in config.ini.

adsb.fi is free for personal, non-commercial use, rate-limited to 1
request/second for this kind of on-demand lookup, and asks that users of
its open data credit adsb.fi with a link back to https://adsb.fi -- see
https://github.com/adsbfi/opendata/blob/main/README.md. Since this client
is only called with a handful of missing ICAOs once per poll cycle
(60-120s by default), it stays well under that limit on its own.

Converts each hit into a plain object exposing the same attributes that
opensky_api.StateVector does (icao24, callsign, latitude, longitude,
baro_altitude, geo_altitude, on_ground, velocity, vertical_rate,
true_track, squawk, time_position, last_contact, ...) -- in the same units
(meters, m/s) OpenSky uses -- so Tracker.process_aircraft_list() and
get_altitude_ft() can consume a merged list without caring which source
any given entry came from.
"""

import logging
import time
from types import SimpleNamespace

import requests

logger = logging.getLogger("open-fleet-tracker")

FEET_PER_METER = 3.28084
MPS_PER_KNOT = 0.514444
MPS_PER_FPM = 0.00508  # feet/minute -> meters/second


def _meters_from_feet(value):
    if value is None:
        return None
    try:
        return float(value) / FEET_PER_METER
    except (TypeError, ValueError):
        return None


def _mps_from_knots(value):
    if value is None:
        return None
    try:
        return float(value) * MPS_PER_KNOT
    except (TypeError, ValueError):
        return None


def _mps_from_fpm(value):
    if value is None:
        return None
    try:
        return float(value) * MPS_PER_FPM
    except (TypeError, ValueError):
        return None


class AdsbFiClient:
    def __init__(self, config):
        self.config = config
        self.base_url = (config.adsbfi_base_url or "https://opendata.adsb.fi/api").rstrip("/")
        self.timeout = config.adsbfi_timeout_seconds

    def fetch_missing(self, icaos):
        """icaos: iterable of lowercase icao24 hex strings that were not
        present in this cycle's OpenSky results. Returns a list of adapter
        objects (StateVector-shaped) for whichever of them adsb.fi
        currently has data for -- an empty list if none are visible there
        either, or if the request fails for any reason. Never raises, so a
        fallback-provider outage can't take down the main poll loop.
        """
        icaos = sorted({icao.strip().lower() for icao in icaos if icao and icao.strip()})
        if not icaos:
            return []

        url = f"{self.base_url}/v2/icao/{','.join(icaos)}"
        try:
            resp = requests.get(url, timeout=self.timeout)
            resp.raise_for_status()
            payload = resp.json()
        except requests.exceptions.RequestException as e:
            logger.warning("adsb.fi fallback request failed: %s", e)
            return []
        except ValueError as e:
            logger.warning("adsb.fi fallback returned unparseable JSON: %s", e)
            return []

        now_ms = payload.get("now")
        now_sec = (now_ms / 1000.0) if now_ms else time.time()

        results = []
        for ac in payload.get("ac", []) or []:
            adapted = self._adapt(ac, now_sec)
            if adapted is not None:
                results.append(adapted)

        if results:
            logger.info(
                "adsb.fi fallback: found %d/%d missing tracked ICAO24(s) [data via https://adsb.fi]: %s",
                len(results),
                len(icaos),
                ",".join(a.icao24 for a in results),
            )
        return results

    @staticmethod
    def _adapt(ac, now_sec):
        icao24 = (ac.get("hex") or "").strip().lower()
        if not icao24:
            return None

        alt_baro_raw = ac.get("alt_baro")
        on_ground = alt_baro_raw == "ground"
        baro_altitude = None if on_ground else _meters_from_feet(alt_baro_raw)
        geo_altitude = _meters_from_feet(ac.get("alt_geom"))

        # adsb.fi/ADSBX v2 report vertical rate as either baro_rate or
        # geom_rate (feet/minute), never both -- OpenSky's StateVector only
        # has a single vertical_rate field, so fold whichever is present.
        vertical_rate_fpm = ac.get("baro_rate")
        if vertical_rate_fpm is None:
            vertical_rate_fpm = ac.get("geom_rate")

        seen = ac.get("seen")
        seen_pos = ac.get("seen_pos")

        return SimpleNamespace(
            icao24=icao24,
            callsign=(ac.get("flight") or "").strip() or None,
            origin_country=None,
            time_position=(now_sec - seen_pos) if seen_pos is not None else None,
            last_contact=(now_sec - seen) if seen is not None else None,
            longitude=ac.get("lon"),
            latitude=ac.get("lat"),
            geo_altitude=geo_altitude,
            on_ground=on_ground,
            velocity=_mps_from_knots(ac.get("gs")),
            true_track=ac.get("track"),
            vertical_rate=_mps_from_fpm(vertical_rate_fpm),
            sensors=None,
            baro_altitude=baro_altitude,
            squawk=ac.get("squawk"),
            spi=bool(ac.get("spi", 0)),
            position_source=None,
            category=None,
        )
