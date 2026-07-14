"""
Full path: open-fleet-tracker/tracker.py

The flight-phase state machine: turns raw OpenSky state vectors into
landing candidates and confirmed-landing alerts.

This preserves every rule your single-file tracker had accumulated:
  - an aircraft must be observed airborne (>500 ft) OR caught already
    descending below 10,000 ft before it is eligible for ANY landing alert
    ("seen_airborne" gate)
  - a per-flight alert lock so one flight can only ever create one landing
    candidate, even if it bounces between trigger conditions
  - a landing candidate is confirmed once the aircraft holds on_ground for
    long enough (time or poll-count), or is sent as a best-effort "likely
    landing" if position updates go quiet for too long
  - an active-airborne watch list for aircraft above the watch altitude,
    which can be promoted into a landing candidate once it actually
    descends/lands (peak-altitude drop or negative vertical rate), not on
    a timer alone
  - climb detection so a departing aircraft under the low-altitude
    threshold doesn't get flagged as landing
"""

import logging
import re
import time

import pandas as pd

from weather import get_airport_weather, weather_block_text

logger = logging.getLogger("open-fleet-tracker")


def feet_from_meters(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    return float(val) * 3.28084


def knots_from_mps(val):
    """OpenSky's `velocity` field is ground speed in m/s; convert to knots
    for the quiet-timeout confidence scoring's ~160 kn threshold."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    return float(val) * 1.94384


def tail_number_from_callsign(callsign):
    """Best-effort short "tail number" for console/log display: strips a
    leading alphabetic prefix (a fleet/airline callsign prefix, e.g. the
    "JTZ" in "JTZ316") and keeps the trailing alphanumeric part, so a
    callsign like "JTZ316" displays as "316". Falls back to the callsign
    as-is if it doesn't match that shape, or None if there's no callsign.
    """
    if not callsign:
        return None
    trimmed = str(callsign).strip()
    if not trimmed:
        return None
    match = re.match(r"^[A-Za-z]+(\w*\d\w*)$", trimmed)
    if match:
        return match.group(1)
    return trimmed


def get_altitude_ft(aircraft):
    geo_ft = feet_from_meters(getattr(aircraft, "geo_altitude", None))
    baro_ft = feet_from_meters(getattr(aircraft, "baro_altitude", None))
    if geo_ft is not None:
        return geo_ft, "geo"
    if baro_ft is not None:
        return baro_ft, "baro"
    return None, None


class Tracker:
    def __init__(self, config, state, opensky, airports, notifier, events=None):
        self.config = config
        self.state = state
        self.opensky = opensky
        self.airports = airports
        self.notifier = notifier
        self.events = events

    # ------------------------------------------------------------- climb math
    def is_climbing(self, aircraft, icao24, current_altitude_ft):
        vertical_rate = getattr(aircraft, "vertical_rate", None)
        if vertical_rate is not None and vertical_rate > 0:
            return True

        last_altitude = self.state.bucket("last_altitude_ft").get(icao24)
        if last_altitude is None or current_altitude_ft is None:
            return False

        return (current_altitude_ft - last_altitude) >= self.config.climb_delta_ft

    # -------------------------------------------------------- seen_airborne
    def has_seen_airborne(self, icao24):
        return bool(self.state.bucket("seen_airborne").get(icao24))

    def mark_seen_airborne(self, icao24, now, altitude_ft=None, reason="airborne"):
        bucket = self.state.bucket("seen_airborne")
        existing = bucket.get(icao24)

        if existing is None:
            bucket[icao24] = {"seen_at": now, "altitude_ft": altitude_ft, "reason": reason}
            return

        existing["seen_at"] = now
        if altitude_ft is not None:
            existing["altitude_ft"] = altitude_ft
        if reason:
            existing["reason"] = reason

    def clear_seen_airborne(self, icao24):
        self.state.bucket("seen_airborne").pop(icao24, None)

    # -------------------------------------------------------- flight locks
    def has_flight_alert_lock(self, icao24):
        return bool(self.state.bucket("flight_alert_locks").get(icao24))

    def set_flight_alert_lock(self, icao24, reason):
        self.state.bucket("flight_alert_locks")[icao24] = {"locked_at": time.time(), "reason": reason}

    def clear_flight_alert_lock(self, icao24):
        self.state.bucket("flight_alert_locks").pop(icao24, None)

    # -------------------------------------------------------------- identity
    def update_identity_cache(self, icao24, callsign):
        if icao24:
            self.state.bucket("last_known_icao24")[icao24] = icao24.upper()
        if callsign and callsign.strip():
            self.state.bucket("last_known_callsign")[icao24] = callsign.strip()

    def get_display_callsign(self, icao24, callsign):
        if callsign and callsign.strip():
            return callsign.strip()
        cached = self.state.bucket("last_known_callsign").get(icao24)
        if cached and str(cached).strip():
            return str(cached).strip()
        return icao24.upper()

    # ---------------------------------------------------------- alert cooldown
    def _should_suppress_alert(self, icao24, now):
        recent = self.state.bucket("recent_alerts").get(icao24)
        return recent is not None and (now - recent) < self.config.alert_cooldown_sec

    def _record_alert(self, icao24, now):
        self.state.bucket("recent_alerts")[icao24] = now

    # ----------------------------------------------------- follow-up queue
    def queue_confirmation_followup(self, icao24, item):
        now = time.time()
        self.state.bucket("pending_confirmations")[icao24] = {
            "queued_at": now,
            "send_after": now + self.config.followup_delay_sec,
            "lat": item.get("lat"),
            "lon": item.get("lon"),
            "callsign": item.get("callsign"),
            "velocity": item.get("velocity"),
            "baro_altitude": item.get("baro_altitude"),
            "trigger": item.get("trigger"),
        }

    def get_current_aircraft_state(self, icao24):
        return self.opensky.get_current_aircraft_state(icao24)

    # ----------------------------------------------------- landing candidates
    def clear_landing_candidate(self, icao24):
        self.state.bucket("landing_candidates").pop(icao24, None)

    def upsert_landing_candidate(
        self,
        icao24,
        now,
        altitude_ft,
        altitude_source,
        lat,
        lon,
        callsign,
        velocity,
        baro_altitude,
        trigger,
        on_ground=False,
        last_contact=None,
        time_position=None,
        vertical_rate=None,
    ):
        candidates = self.state.bucket("landing_candidates")
        existing = candidates.get(icao24)

        newest_seen = max([t for t in [last_contact, time_position] if t is not None], default=None)

        if existing is None:
            candidates[icao24] = {
                "first_seen_at": now,
                "last_seen_at": now,
                "recheck_after": now + self.config.landing_recheck_sec,
                "ref_altitude_ft": altitude_ft,
                "current_altitude_ft": altitude_ft,
                "lowest_altitude_ft": altitude_ft,
                "altitude_source": altitude_source,
                "lat": lat,
                "lon": lon,
                "callsign": callsign,
                "velocity": velocity,
                "baro_altitude": baro_altitude,
                "trigger": trigger,
                "on_ground_seen": bool(on_ground),
                "on_ground_first_seen_at": now if on_ground else None,
                "on_ground_poll_count": 1 if on_ground else 0,
                "last_contact": last_contact,
                "time_position": time_position,
                "last_position_or_contact_at": newest_seen,
                "vertical_rate": vertical_rate,
            }
            # Lock the flight the moment it becomes a candidate so it cannot
            # re-enter the pipeline a second time this flight cycle.
            self.set_flight_alert_lock(icao24, trigger)
            if self.events:
                self.events.log("candidate_created", icao24=icao24, trigger=trigger, altitude_ft=altitude_ft)
            return

        existing["last_seen_at"] = now

        if altitude_ft is not None:
            existing["current_altitude_ft"] = altitude_ft
            if existing.get("ref_altitude_ft") is None:
                existing["ref_altitude_ft"] = altitude_ft
            lowest = existing.get("lowest_altitude_ft")
            if lowest is None or altitude_ft < lowest:
                existing["lowest_altitude_ft"] = altitude_ft

        if altitude_source:
            existing["altitude_source"] = altitude_source
        if lat is not None:
            existing["lat"] = lat
        if lon is not None:
            existing["lon"] = lon
        if callsign:
            existing["callsign"] = callsign
        if velocity is not None:
            existing["velocity"] = velocity
        if baro_altitude is not None:
            existing["baro_altitude"] = baro_altitude
        if trigger:
            existing["trigger"] = trigger
        # Track the most recent vertical rate (not just at creation) so the
        # quiet-timeout confidence scorer can see whether the aircraft was
        # still descending right before it went quiet. Only overwrite when
        # we actually got a fresh reading -- preserve the last known value
        # if this field is missing/stale on a given poll.
        if vertical_rate is not None:
            existing["vertical_rate"] = vertical_rate

        existing["last_contact"] = last_contact
        existing["time_position"] = time_position
        if newest_seen is not None:
            existing["last_position_or_contact_at"] = newest_seen

        if on_ground:
            existing["on_ground_seen"] = True
            if existing.get("on_ground_first_seen_at") is None:
                existing["on_ground_first_seen_at"] = now
            existing["on_ground_poll_count"] = existing.get("on_ground_poll_count", 0) + 1
        else:
            existing["on_ground_poll_count"] = 0
            existing["on_ground_first_seen_at"] = None

    def update_landing_candidate_from_poll(self, icao24, now, lat, lon, velocity, baro_altitude,
                                            display_callsign, altitude_ft, altitude_source,
                                            on_ground, last_contact, time_position,
                                            vertical_rate=None):
        """Refresh an existing landing candidate that isn't (re-)triggering this poll."""
        candidate = self.state.bucket("landing_candidates").get(icao24)
        if candidate is None:
            return

        candidate["last_seen_at"] = now
        if last_contact is not None:
            candidate["last_contact"] = last_contact
        if time_position is not None:
            candidate["time_position"] = time_position

        newest_seen = max([t for t in [last_contact, time_position] if t is not None], default=None)
        if newest_seen is not None:
            candidate["last_position_or_contact_at"] = newest_seen

        if lat is not None:
            candidate["lat"] = lat
        if lon is not None:
            candidate["lon"] = lon
        if velocity is not None:
            candidate["velocity"] = velocity
        if baro_altitude is not None:
            candidate["baro_altitude"] = baro_altitude
        if vertical_rate is not None:
            candidate["vertical_rate"] = vertical_rate
        if display_callsign:
            candidate["callsign"] = display_callsign
        if altitude_ft is not None:
            candidate["current_altitude_ft"] = altitude_ft
            lowest = candidate.get("lowest_altitude_ft")
            if lowest is None or altitude_ft < lowest:
                candidate["lowest_altitude_ft"] = altitude_ft
        if altitude_source:
            candidate["altitude_source"] = altitude_source

        if on_ground:
            candidate["on_ground_seen"] = True
            if candidate.get("on_ground_first_seen_at") is None:
                candidate["on_ground_first_seen_at"] = now
            candidate["on_ground_poll_count"] = candidate.get("on_ground_poll_count", 0) + 1

    # ----------------------------------------------------- airborne watch
    def upsert_active_airborne_tracking(
        self,
        icao24,
        now,
        altitude_ft,
        altitude_source,
        lat,
        lon,
        callsign,
        velocity,
        baro_altitude,
        vertical_rate,
        on_ground,
        last_contact=None,
        time_position=None,
    ):
        tracked = self.state.bucket("active_airborne_tracking")
        existing = tracked.get(icao24)

        newest_seen = max([t for t in [last_contact, time_position] if t is not None], default=None)

        if existing is None:
            tracked[icao24] = {
                "first_seen_at": now,
                "last_seen_at": now,
                "last_position_or_contact_at": newest_seen,
                "callsign": callsign,
                "lat": lat,
                "lon": lon,
                "velocity": velocity,
                "baro_altitude": baro_altitude,
                "vertical_rate": vertical_rate,
                "altitude_ft": altitude_ft,
                "peak_altitude_ft": altitude_ft,
                "altitude_source": altitude_source,
                "on_ground": on_ground,
            }
            return

        existing["last_seen_at"] = now
        if newest_seen is not None:
            existing["last_position_or_contact_at"] = newest_seen
        if callsign:
            existing["callsign"] = callsign
        if lat is not None:
            existing["lat"] = lat
        if lon is not None:
            existing["lon"] = lon
        if velocity is not None:
            existing["velocity"] = velocity
        if baro_altitude is not None:
            existing["baro_altitude"] = baro_altitude
        existing["vertical_rate"] = vertical_rate
        if altitude_ft is not None:
            existing["altitude_ft"] = altitude_ft
            peak = existing.get("peak_altitude_ft")
            if peak is None or altitude_ft > peak:
                existing["peak_altitude_ft"] = altitude_ft
        if altitude_source:
            existing["altitude_source"] = altitude_source
        existing["on_ground"] = on_ground

    def remove_active_airborne_tracking(self, icao24):
        self.state.bucket("active_airborne_tracking").pop(icao24, None)

    def should_promote_airborne_watch_to_landing(self, icao24, altitude_ft, vertical_rate, on_ground, aircraft):
        watch = self.state.bucket("active_airborne_tracking").get(icao24)
        if watch is None:
            return False

        if on_ground:
            return True

        if altitude_ft is None:
            return False

        if altitude_ft <= self.config.altitude_threshold_ft and not self.is_climbing(aircraft, icao24, altitude_ft):
            return True

        if altitude_ft <= self.config.airborne_tracking_altitude_ft:
            if vertical_rate is not None and vertical_rate < 0:
                return True

            peak_alt_ft = watch.get("peak_altitude_ft")
            if peak_alt_ft is not None and altitude_ft <= (peak_alt_ft - 500.0):
                return True

        return False

    # --------------------------------------------- quiet-timeout confidence
    def compute_landing_confidence_score(self, icao24, item):
        """Weighted approach-confidence score for a landing candidate that
        has gone quiet (no fresh OpenSky position/contact update at all).

        A bare quiet timeout is not, by itself, reliable evidence of a
        landing: OpenSky state vectors are snapshots and can omit stale
        position/velocity fields after roughly 15 seconds without a fresh
        update, and on_ground is not guaranteed to be reported right at
        touchdown. Instead, silence is combined with corroborating
        approach evidence -- low altitude, recent descent, and low speed
        -- so only a genuinely landing-shaped candidate clears the bar.

        Returns (score, reasons) where reasons is a list of human-readable
        strings for logging.
        """
        score = 0
        reasons = []

        if item.get("on_ground_seen"):
            score += 3
            reasons.append("on_ground_seen(+3)")

        lowest_altitude_ft = item.get("lowest_altitude_ft")
        if lowest_altitude_ft is not None and lowest_altitude_ft < self.config.landing_quiet_low_alt_ft:
            score += 2
            reasons.append(f"lowest_alt_ft={lowest_altitude_ft:.0f}<{self.config.landing_quiet_low_alt_ft:.0f}(+2)")

        current_altitude_ft = item.get("current_altitude_ft")
        if current_altitude_ft is not None and current_altitude_ft < self.config.landing_quiet_current_alt_ft:
            score += 2
            reasons.append(
                f"current_alt_ft={current_altitude_ft:.0f}<{self.config.landing_quiet_current_alt_ft:.0f}(+2)"
            )

        vertical_rate = item.get("vertical_rate")
        if vertical_rate is not None and vertical_rate < 0:
            score += 1
            reasons.append(f"vertical_rate={vertical_rate:.0f}<0(+1)")

        velocity_kn = knots_from_mps(item.get("velocity"))
        if velocity_kn is not None and velocity_kn < self.config.landing_quiet_speed_kn:
            score += 1
            reasons.append(f"speed_kn={velocity_kn:.0f}<{self.config.landing_quiet_speed_kn:.0f}(+1)")

        if self.has_seen_airborne(icao24):
            score += 1
            reasons.append("previously_airborne(+1)")

        first_seen_at = item.get("first_seen_at")
        last_position_or_contact_at = item.get("last_position_or_contact_at")
        if (
            first_seen_at is not None
            and last_position_or_contact_at is not None
            and last_position_or_contact_at > first_seen_at
        ):
            score += 1
            reasons.append("disappeared_after_candidate(+1)")

        return score, reasons

    # --------------------------------------------------------- notification
    def send_landing_alert(self, icao24, item, confirmed=False):
        now = time.time()

        if self._should_suppress_alert(icao24, now):
            logger.info(
                "ALERT suppressed_duplicate type=landing icao24=%s callsign=%s",
                icao24,
                item.get("callsign") or "N/A",
            )
            return

        lat = item.get("lat")
        lon = item.get("lon")
        callsign = (item.get("callsign") or "").strip() or icao24.upper()
        velocity = item.get("velocity")
        baro_altitude = item.get("baro_altitude")
        aircraft_type = self.config.get_aircraft_type(icao24)

        current_altitude_ft = item.get("current_altitude_ft")
        if (
            current_altitude_ft is not None
            and current_altitude_ft > self.config.low_alt_descent_threshold_ft
            and not confirmed
        ):
            logger.info(
                "ALERT blocked_high_altitude_send icao24=%s callsign=%s current_alt=%.0f",
                icao24,
                callsign,
                current_altitude_ft,
            )
            return

        airport = self.airports.resolve_airport(lat, lon)
        airport_name = airport["name"] if airport else "Unknown airport"
        nearby = self.airports.resolve_nearby_airports(lat, lon, limit=3)
        weather = get_airport_weather((airport or {}).get("icao"), self.config.weather_timeout_sec)

        nearby_text = (
            "\n".join(
                f"{i + 1}. {a['name']} ({a['icao'] or 'N/A'}) — {a['distance_miles']:.1f} mi, "
                f"runway {a['max_runway_ft']:.0f} ft"
                for i, a in enumerate(nearby)
            )
            if nearby
            else "No nearby airport matches found."
        )

        status_text = "confirmed landing" if confirmed else "likely landing"
        subject = f"LANDING ALERT: Aircraft {callsign} {status_text} at {airport_name}"
        body = (
            f"{callsign} ({icao24.upper()}) {status_text} at {airport_name}.\n"
            f"Aircraft type: {aircraft_type}\n"
            f"Trigger: {item.get('trigger') or 'unknown'}\n"
            f"Confirmation reason: {item.get('confirmation_reason') or 'unknown'}\n"
            f"Current altitude: {current_altitude_ft if current_altitude_ft is not None else 'N/A'} ft\n"
            f"Lowest tracked altitude: {item.get('lowest_altitude_ft') if item.get('lowest_altitude_ft') is not None else 'N/A'} ft\n"
            f"Altitude source: {item.get('altitude_source') or 'N/A'}\n"
            f"Velocity: {velocity if velocity is not None else 'N/A'} kn\n"
            f"Baro altitude: {baro_altitude if baro_altitude is not None else 'N/A'} ft\n"
            f"Nearby airports:\n{nearby_text}\n\n"
            f"{weather_block_text(weather)}"
        )

        if confirmed:
            channel = self.notifier.send_alert(
                subject,
                body,
                icao24,
                lat=float(lat) if lat is not None else None,
                lon=float(lon) if lon is not None else None,
                callsign=callsign,
                velocity=velocity,
                baro_altitude=baro_altitude,
                airport=airport,
                weather=weather,
                aircraft_type=aircraft_type,
            )

            self._record_alert(icao24, now)
            self.state.bucket("pending_confirmations").pop(icao24, None)
            self.clear_seen_airborne(icao24)
            self.remove_active_airborne_tracking(icao24)

            if self.events:
                self.events.log("landing_confirmed", icao24=icao24, callsign=callsign, channel=channel)

            logger.info(
                "ALERT landing_confirmed_sent icao24=%s callsign=%s airport=%s",
                icao24,
                callsign,
                airport_name,
            )
        else:
            self.queue_confirmation_followup(icao24, item)
            if self.events:
                self.events.log("landing_likely_queued", icao24=icao24, callsign=callsign)
            logger.info(
                "ALERT landing_likely_queued_only icao24=%s callsign=%s airport=%s",
                icao24,
                callsign,
                airport_name,
            )

    # ------------------------------------------------------------ tick jobs
    def process_pending_confirmations(self):
        now = time.time()
        pending = self.state.bucket("pending_confirmations")
        done = []

        for icao24, item in pending.items():
            if now < item.get("send_after", now + self.config.followup_delay_sec):
                continue

            aircraft = self.get_current_aircraft_state(icao24)

            lat = item.get("lat")
            lon = item.get("lon")
            callsign = (item.get("callsign") or "").strip() or icao24.upper()
            velocity = item.get("velocity")
            baro_altitude = item.get("baro_altitude")

            if aircraft is not None:
                fresh_lat = getattr(aircraft, "latitude", None)
                fresh_lon = getattr(aircraft, "longitude", None)
                fresh_callsign = (getattr(aircraft, "callsign", None) or "").strip()
                fresh_velocity = getattr(aircraft, "velocity", None)
                fresh_baro_altitude = feet_from_meters(getattr(aircraft, "baro_altitude", None))

                if fresh_lat is not None:
                    lat = fresh_lat
                if fresh_lon is not None:
                    lon = fresh_lon
                if fresh_callsign:
                    callsign = fresh_callsign
                if fresh_velocity is not None:
                    velocity = fresh_velocity
                if fresh_baro_altitude is not None:
                    baro_altitude = fresh_baro_altitude

            airport = self.airports.resolve_airport(lat, lon)
            airport_name = airport["name"] if airport else "Unknown airport"
            weather = get_airport_weather((airport or {}).get("icao"), self.config.weather_timeout_sec)
            aircraft_type = self.config.get_aircraft_type(icao24)

            subject = f"LANDING CONFIRMED: Aircraft {callsign} at {airport_name}"
            body = (
                f"{callsign} ({icao24.upper()}) landing confirmed at {airport_name}.\n"
                f"Aircraft type: {aircraft_type}\n"
                f"Follow-up delay: {self.config.followup_delay_sec // 60} minutes\n"
                f"Trigger: {item.get('trigger') or 'unknown'}\n"
                f"Original confirmation reason: {item.get('confirmation_reason') or 'unknown'}\n"
                f"Velocity: {velocity if velocity is not None else 'N/A'} kn\n"
                f"Baro altitude: {baro_altitude if baro_altitude is not None else 'N/A'} ft\n\n"
                f"{weather_block_text(weather)}"
            )

            self.notifier.send_alert(
                subject,
                body,
                icao24,
                lat=float(lat) if lat is not None else None,
                lon=float(lon) if lon is not None else None,
                callsign=callsign,
                velocity=velocity,
                baro_altitude=baro_altitude,
                airport=airport,
                weather=weather,
                aircraft_type=aircraft_type,
            )

            logger.info(
                "ALERT landing_confirmed_followup icao24=%s callsign=%s airport=%s",
                icao24,
                callsign,
                airport_name,
            )
            done.append(icao24)

        for icao24 in done:
            pending.pop(icao24, None)

    def process_active_airborne_tracking(self):
        now = time.time()
        tracked = self.state.bucket("active_airborne_tracking")
        done = []

        for icao24, item in tracked.items():
            last_seen = item.get("last_position_or_contact_at") or item.get("last_seen_at") or 0
            if (now - last_seen) >= self.config.landing_no_position_timeout_sec:
                logger.info(
                    "TRACK expired_airborne_watch icao24=%s callsign=%s quiet_for=%.0f",
                    icao24,
                    item.get("callsign") or "N/A",
                    now - last_seen,
                )
                done.append(icao24)

        for icao24 in done:
            tracked.pop(icao24, None)

    def _check_airport_elevation_match(self, icao24, item, now):
        """Returns True once the candidate's current altitude has matched
        (within landing_airport_elevation_margin_ft of) the nearest
        matching airport's field elevation for
        landing_airport_elevation_min_polls consecutive checks in a row.

        Resolves the nearest airport within airport_lookup_radius_miles via
        the existing AirportIndex lookup, so it naturally does nothing if
        the candidate isn't actually near any known airport.
        """
        current_altitude_ft = item.get("current_altitude_ft")
        lat = item.get("lat")
        lon = item.get("lon")

        matched = False
        if current_altitude_ft is not None and lat is not None and lon is not None:
            airport = self.airports.resolve_airport(lat, lon)
            elevation_ft = airport.get("elevation_ft") if airport else None
            if elevation_ft is not None:
                margin = abs(current_altitude_ft - elevation_ft)
                if margin <= self.config.landing_airport_elevation_margin_ft:
                    matched = True
                    item["matched_airport_name"] = airport.get("name")
                    item["matched_airport_elevation_ft"] = elevation_ft

        if not matched:
            item["elevation_match_poll_count"] = 0
            item["elevation_match_first_seen_at"] = None
            return False

        item["elevation_match_poll_count"] = item.get("elevation_match_poll_count", 0) + 1
        if item.get("elevation_match_first_seen_at") is None:
            item["elevation_match_first_seen_at"] = now

        if item["elevation_match_poll_count"] >= self.config.landing_airport_elevation_min_polls:
            logger.info(
                "ALERT airport_elevation_match icao24=%s callsign=%s altitude_ft=%.0f "
                "airport=%s elevation_ft=%.0f polls=%d",
                icao24,
                item.get("callsign") or "N/A",
                current_altitude_ft,
                item.get("matched_airport_name") or "unknown",
                item.get("matched_airport_elevation_ft"),
                item["elevation_match_poll_count"],
            )
            return True

        return False

    def process_landing_candidates(self):
        now = time.time()
        candidates = self.state.bucket("landing_candidates")
        done = []

        for icao24, item in candidates.items():
            current_altitude_ft = item.get("current_altitude_ft")
            ref_altitude_ft = item.get("ref_altitude_ft")
            on_ground_first_seen_at = item.get("on_ground_first_seen_at")
            on_ground_poll_count = item.get("on_ground_poll_count", 0)
            last_position_or_contact_at = item.get("last_position_or_contact_at")

            if on_ground_first_seen_at is not None:
                ground_hold_met = (now - on_ground_first_seen_at) >= self.config.landing_on_ground_hold_sec
                ground_polls_met = on_ground_poll_count >= self.config.landing_on_ground_min_polls

                if ground_hold_met or ground_polls_met:
                    item["confirmation_reason"] = "on_ground_hold"
                    self.send_landing_alert(icao24, item, confirmed=True)
                    done.append(icao24)
                    continue

            # Independent confirmation signal: on_ground isn't always
            # reported reliably by every transponder/receiver pairing, but
            # altitude vs. the nearest matching airport's known field
            # elevation is available whenever geo/baro altitude is. Treat
            # a sustained match as equivalent to being on the ground.
            elevation_match = self._check_airport_elevation_match(icao24, item, now)
            if elevation_match:
                item["confirmation_reason"] = "airport_elevation_match"
                self.send_landing_alert(icao24, item, confirmed=True)
                done.append(icao24)
                continue

            if last_position_or_contact_at is not None:
                quiet_for = now - last_position_or_contact_at
                quiet_timeout_met = quiet_for >= self.config.landing_no_position_timeout_sec
                if quiet_timeout_met:
                    # Silence alone isn't proof of landing -- score the
                    # candidate on corroborating approach evidence (ground
                    # contact, low altitude, descent, low speed) before
                    # treating a quiet timeout as a likely landing.
                    score, reasons = self.compute_landing_confidence_score(icao24, item)
                    threshold = self.config.landing_quiet_confidence_threshold

                    if score >= threshold:
                        logger.info(
                            "ALERT sending_after_position_timeout icao24=%s callsign=%s quiet_for=%.0f "
                            "score=%d/%d reasons=%s",
                            icao24,
                            item.get("callsign") or "N/A",
                            quiet_for,
                            score,
                            threshold,
                            ",".join(reasons) or "none",
                        )
                        item["confirmation_reason"] = f"quiet_timeout_score({score}/{threshold})"
                        self.send_landing_alert(icao24, item, confirmed=False)
                        done.append(icao24)
                        continue

                    hard_cap_met = quiet_for >= self.config.landing_quiet_hard_cap_sec
                    if hard_cap_met:
                        logger.info(
                            "ALERT suppressed_low_confidence_timeout icao24=%s callsign=%s quiet_for=%.0f "
                            "score=%d/%d reasons=%s",
                            icao24,
                            item.get("callsign") or "N/A",
                            quiet_for,
                            score,
                            threshold,
                            ",".join(reasons) or "none",
                        )
                        if self.events:
                            self.events.log(
                                "landing_suppressed_low_confidence",
                                icao24=icao24,
                                score=score,
                                quiet_for=quiet_for,
                            )
                        # Release the flight lock so a genuine future landing
                        # for this aircraft isn't permanently blocked by a
                        # low-confidence quiet gap that never resolved.
                        self.clear_flight_alert_lock(icao24)
                        done.append(icao24)
                        continue

                    logger.info(
                        "ALERT low_confidence_quiet_waiting icao24=%s callsign=%s quiet_for=%.0f "
                        "score=%d/%d reasons=%s",
                        icao24,
                        item.get("callsign") or "N/A",
                        quiet_for,
                        score,
                        threshold,
                        ",".join(reasons) or "none",
                    )
                    continue

            if now < item.get("recheck_after", now + self.config.landing_recheck_sec):
                continue

            if current_altitude_ft is None or ref_altitude_ft is None:
                done.append(icao24)
                continue

            if current_altitude_ft >= (ref_altitude_ft - self.config.landing_alt_margin_ft):
                logger.info(
                    "ALERT suppressed_departure icao24=%s callsign=%s ref_alt=%.0f current_alt=%.0f",
                    icao24,
                    item.get("callsign") or "N/A",
                    ref_altitude_ft,
                    current_altitude_ft,
                )
                done.append(icao24)
                continue

            logger.info(
                "ALERT holding_for_ground_confirmation icao24=%s callsign=%s current_alt=%.0f",
                icao24,
                item.get("callsign") or "N/A",
                current_altitude_ft,
            )

        for icao24 in done:
            candidates.pop(icao24, None)

    def force_pending_alerts(self):
        candidates = self.state.bucket("landing_candidates")
        if not candidates:
            logger.info("No pending landing candidates to force")
            return 0

        sent_count = 0
        for icao24, item in list(candidates.items()):
            try:
                self.send_landing_alert(icao24, item, confirmed=False)
                candidates.pop(icao24, None)
                sent_count += 1
                logger.info("FORCED landing alert sent for %s callsign=%s", icao24, item.get("callsign") or "N/A")
            except Exception as e:
                logger.exception("Failed to force pending alert for %s: %s", icao24, e)

        self.state.save()
        return sent_count

    # ------------------------------------------------------------- main tick
    def process_aircraft_list(self, aircraft_list):
        """Runs the per-aircraft phase logic for one poll cycle.

        Equivalent to the `for aircraft in aircraft_list:` body of the
        original single-file tracker's main(), with the seen_airborne gate,
        flight locks, and airborne-watch promotion all applied.
        """
        last_ground = self.state.bucket("last_ground")
        last_alt = self.state.bucket("last_alt")
        last_altitude_ft_state = self.state.bucket("last_altitude_ft")

        now = time.time()

        for aircraft in aircraft_list:
            icao24 = (aircraft.icao24 or "").lower()
            if icao24 not in self.config.icaos:
                continue

            callsign = (aircraft.callsign or "").strip()
            lat = aircraft.latitude
            lon = aircraft.longitude
            velocity = getattr(aircraft, "velocity", None)
            on_ground = bool(getattr(aircraft, "on_ground", False))
            vertical_rate = getattr(aircraft, "vertical_rate", None)
            last_contact = getattr(aircraft, "last_contact", None)
            time_position = getattr(aircraft, "time_position", None)

            baro_altitude = feet_from_meters(getattr(aircraft, "baro_altitude", None))
            altitude_ft, altitude_source = get_altitude_ft(aircraft)

            self.update_identity_cache(icao24, callsign)
            display_callsign = self.get_display_callsign(icao24, callsign)

            prev_ground = last_ground.get(icao24)
            flight_locked = self.has_flight_alert_lock(icao24)

            if altitude_ft is not None:
                last_altitude_ft_state[icao24] = altitude_ft

            low_alt_descent = (
                altitude_ft is not None
                and altitude_ft <= self.config.low_alt_descent_threshold_ft
                and vertical_rate is not None
                and vertical_rate < 0
            )

            # --- seen_airborne gate: mark eligible before any landing path ---
            if not on_ground and altitude_ft is not None and altitude_ft > self.config.seen_airborne_altitude_ft:
                self.mark_seen_airborne(icao24, now, altitude_ft, reason="airborne_above_500")
            elif not on_ground and low_alt_descent:
                self.mark_seen_airborne(icao24, now, altitude_ft, reason="descent_below_10000")

            seen_airborne = self.has_seen_airborne(icao24)

            # --- airborne watch list for high-altitude aircraft ---
            if (
                altitude_ft is not None
                and altitude_ft >= self.config.airborne_tracking_altitude_ft
                and not on_ground
            ):
                self.upsert_active_airborne_tracking(
                    icao24=icao24,
                    now=now,
                    altitude_ft=altitude_ft,
                    altitude_source=altitude_source,
                    lat=lat,
                    lon=lon,
                    callsign=display_callsign,
                    velocity=velocity,
                    baro_altitude=baro_altitude,
                    vertical_rate=vertical_rate,
                    on_ground=on_ground,
                    last_contact=last_contact,
                    time_position=time_position,
                )

            if prev_ground is None:
                last_ground[icao24] = on_ground
                if altitude_ft is not None:
                    last_alt[icao24] = altitude_ft
                self.state.save()
                continue

            landing_alert = prev_ground is False and on_ground is True

            low_altitude_trigger = (
                altitude_ft is not None
                and altitude_ft <= self.config.altitude_threshold_ft
                and not self.is_climbing(aircraft, icao24, altitude_ft)
            )

            promote_from_airborne_watch = self.should_promote_airborne_watch_to_landing(
                icao24=icao24,
                altitude_ft=altitude_ft,
                vertical_rate=vertical_rate,
                on_ground=on_ground,
                aircraft=aircraft,
            )

            if landing_alert and seen_airborne and not flight_locked:
                self.upsert_landing_candidate(
                    icao24=icao24,
                    now=now,
                    altitude_ft=altitude_ft,
                    altitude_source=altitude_source,
                    lat=lat,
                    lon=lon,
                    callsign=display_callsign,
                    velocity=velocity,
                    baro_altitude=baro_altitude,
                    trigger="on_ground",
                    on_ground=True,
                    last_contact=last_contact,
                    time_position=time_position,
                    vertical_rate=vertical_rate,
                )
                self.remove_active_airborne_tracking(icao24)
                logger.info("ALERT queued_confirmed_landing icao24=%s callsign=%s", icao24, display_callsign)

            elif low_altitude_trigger and seen_airborne and not flight_locked:
                self.upsert_landing_candidate(
                    icao24=icao24,
                    now=now,
                    altitude_ft=altitude_ft,
                    altitude_source=altitude_source,
                    lat=lat,
                    lon=lon,
                    callsign=display_callsign,
                    velocity=velocity,
                    baro_altitude=baro_altitude,
                    trigger="below_1500ft",
                    on_ground=False,
                    last_contact=last_contact,
                    time_position=time_position,
                    vertical_rate=vertical_rate,
                )
                self.remove_active_airborne_tracking(icao24)
                logger.info(
                    "ALERT queued_landing_candidate_1500 icao24=%s callsign=%s alt=%.0f",
                    icao24,
                    display_callsign,
                    altitude_ft,
                )

            elif low_alt_descent and seen_airborne and not flight_locked:
                self.upsert_landing_candidate(
                    icao24=icao24,
                    now=now,
                    altitude_ft=altitude_ft,
                    altitude_source=altitude_source,
                    lat=lat,
                    lon=lon,
                    callsign=display_callsign,
                    velocity=velocity,
                    baro_altitude=baro_altitude,
                    trigger="below_10000ft_descending",
                    on_ground=False,
                    last_contact=last_contact,
                    time_position=time_position,
                    vertical_rate=vertical_rate,
                )
                logger.info(
                    "ALERT queued_landing_candidate_10000 icao24=%s callsign=%s alt=%.0f vr=%s",
                    icao24,
                    display_callsign,
                    altitude_ft,
                    "N/A" if vertical_rate is None else f"{vertical_rate:.0f}",
                )

            elif promote_from_airborne_watch and seen_airborne and not flight_locked:
                self.upsert_landing_candidate(
                    icao24=icao24,
                    now=now,
                    altitude_ft=altitude_ft,
                    altitude_source=altitude_source,
                    lat=lat,
                    lon=lon,
                    callsign=display_callsign,
                    velocity=velocity,
                    baro_altitude=baro_altitude,
                    trigger="airborne_watch_below_5000",
                    on_ground=on_ground,
                    last_contact=last_contact,
                    time_position=time_position,
                    vertical_rate=vertical_rate,
                )
                logger.info(
                    "ALERT promoted_airborne_watch icao24=%s callsign=%s alt=%s vr=%s",
                    icao24,
                    display_callsign,
                    "N/A" if altitude_ft is None else f"{altitude_ft:.0f}",
                    "N/A" if vertical_rate is None else f"{vertical_rate:.0f}",
                )

            else:
                self.update_landing_candidate_from_poll(
                    icao24=icao24,
                    now=now,
                    lat=lat,
                    lon=lon,
                    velocity=velocity,
                    baro_altitude=baro_altitude,
                    display_callsign=display_callsign,
                    altitude_ft=altitude_ft,
                    altitude_source=altitude_source,
                    on_ground=on_ground,
                    last_contact=last_contact,
                    time_position=time_position,
                    vertical_rate=vertical_rate,
                )

            # Release the flight lock once the aircraft has climbed back out
            # of the low-altitude descent band, so a genuine go-around/next
            # flight cycle is eligible again.
            if (
                self.has_flight_alert_lock(icao24)
                and not on_ground
                and altitude_ft is not None
                and altitude_ft >= self.config.low_alt_descent_threshold_ft
            ):
                self.clear_flight_alert_lock(icao24)
                logger.info(
                    "ALERT cleared_flight_lock icao24=%s callsign=%s alt=%.0f",
                    icao24,
                    display_callsign,
                    altitude_ft,
                )

            last_ground[icao24] = on_ground
            if altitude_ft is not None:
                last_alt[icao24] = altitude_ft

            self.state.save()

    def run_periodic_jobs(self):
        """Runs the housekeeping jobs called once per poll loop, before
        fetching fresh states."""
        self.process_pending_confirmations()
        self.process_landing_candidates()
        self.process_active_airborne_tracking()

    def airborne_tracking_tail_display(self):
        """Comma-separated short tail numbers (see tail_number_from_callsign)
        for everything currently in active_airborne_tracking, for the
        console STATUS line. Falls back to the bare icao24 (uppercased)
        when there's no callsign yet, and to "none" when nothing is being
        tracked, so the STATUS line is never blank.
        """
        tracked = self.state.bucket("active_airborne_tracking")
        if not tracked:
            return "none"

        tails = []
        for icao24, item in tracked.items():
            tail = tail_number_from_callsign(item.get("callsign")) or icao24.upper()
            tails.append(tail)
        return ",".join(tails)

    def status_snapshot(self):
        return {
            "tracked_icaos": len(self.config.icaos),
            "landing_candidates": len(self.state.bucket("landing_candidates")),
            "pending_confirmations": len(self.state.bucket("pending_confirmations")),
            "active_airborne_tracking": len(self.state.bucket("active_airborne_tracking")),
            "flight_alert_locks": len(self.state.bucket("flight_alert_locks")),
            "seen_airborne": len(self.state.bucket("seen_airborne")),
        }
