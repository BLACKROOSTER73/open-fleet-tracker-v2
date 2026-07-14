"""
Full path: open-fleet-tracker/events.py

Optional append-only event log (events.jsonl), for debugging why an alert
did or did not fire. Disabled by default via config.ini [events] enabled =
false. Purely additive observability -- the tracker behaves identically
whether this is on or off.
"""

import json
import logging
import time

logger = logging.getLogger("open-fleet-tracker")


class EventLog:
    def __init__(self, config):
        self.enabled = config.events_enabled
        self.path = config.events_file

    def log(self, event_type, **fields):
        if not self.enabled:
            return
        try:
            record = {"ts": time.time(), "event": event_type, **fields}
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            logger.exception("Failed to write event log entry")
