"""
Full path: steeljet_tracker/state_store.py

Loads and persists the tracker's runtime state (landing candidates, airborne
watch list, alert locks, seen-airborne flags, etc.) to a single JSON file.
Uses the exact same bucket layout as the original alert_state.json, so
upgrading from the single-file tracker to this project does not require any
state migration -- point `tracker.state_file` in config.ini at your existing
alert_state.json and it will load right in.
"""

import json
import logging

logger = logging.getLogger("steeljet")

DEFAULT_BUCKETS = {
    "last_ground": {},
    "last_alt": {},
    "last_altitude_ft": {},
    "recent_alerts": {},
    "last_known_callsign": {},
    "last_known_icao24": {},
    "landing_candidates": {},
    "pending_confirmations": {},
    "active_airborne_tracking": {},
    "flight_alert_locks": {},
    "seen_airborne": {},
}


def _json_safe(obj):
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


class StateStore:
    """Thin wrapper around the persisted state dict.

    `store.data` is the raw dict, kept fully backward compatible with the
    original alert_state.json layout. Domain logic (landing candidates,
    airborne watch, flight locks, seen-airborne, etc.) lives in tracker.py
    and operates on buckets via `store.bucket(name)`.
    """

    def __init__(self, state_file):
        self.state_file = state_file
        self.data = self._load()

    def _load(self):
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                if isinstance(data, dict):
                    for key, default in DEFAULT_BUCKETS.items():
                        data.setdefault(key, dict(default))
                    return data
            except Exception:
                logger.exception("State file unreadable; starting fresh")
        return {key: dict(default) for key, default in DEFAULT_BUCKETS.items()}

    def bucket(self, name):
        return self.data.setdefault(name, {})

    def save(self):
        self.state_file.write_text(json.dumps(_json_safe(self.data), indent=2))
