"""
Full path: steeljet_tracker/notifier.py

Delivery layer: Discord webhook (primary) with retry + separate
connect/read timeouts, and SMTP email (fallback). Isolated from tracker
logic so transport failures (timeouts, rate limits) never tangle with
landing-detection state -- a Discord timeout here just triggers the email
fallback, exactly like the original tracker's hardened send_discord().
"""

import logging
import smtplib
import time
from email.message import EmailMessage

import requests

from weather import weather_block_text

logger = logging.getLogger("steeljet")


class Notifier:
    def __init__(self, config, events=None):
        self.config = config
        self.events = events  # optional EventLog, may be None

    # ---------------------------------------------------------------- Discord
    def send_discord(
        self,
        title,
        body,
        icao24,
        lat=None,
        lon=None,
        callsign=None,
        velocity=None,
        baro_altitude=None,
        airport=None,
        weather=None,
        aircraft_type="N/A",
    ):
        discord_url = self.config.discord_webhook_url
        if not discord_url:
            logger.warning("No Discord webhook URL configured; skipping Discord alert")
            return False

        icao24_u = icao24.upper()
        adsb_link = f"[Open map view](https://globe.adsbexchange.com/?icao={icao24_u})"

        summary_parts = []
        if callsign:
            summary_parts.append(callsign)
        summary_parts.append(icao24_u)
        if aircraft_type and aircraft_type != "N/A":
            summary_parts.append(aircraft_type)
        if velocity is not None:
            summary_parts.append(f"{velocity:.2f} kn")
        if baro_altitude is not None:
            summary_parts.append(f"{baro_altitude:.0f} ft")

        summary = " • ".join(summary_parts)
        description = f"{summary}\n{adsb_link}"
        if len(description) > 4096:
            description = description[:4093].rstrip() + "..."

        fields = [
            {"name": "ICAO24", "value": icao24_u, "inline": True},
            {"name": "Callsign", "value": callsign or "N/A", "inline": True},
            {"name": "Aircraft Type", "value": aircraft_type or "N/A", "inline": True},
            {"name": "Velocity", "value": f"{velocity:.2f} kn" if velocity is not None else "N/A", "inline": True},
            {
                "name": "Baro Altitude",
                "value": f"{baro_altitude:.0f} ft" if baro_altitude is not None else "N/A",
                "inline": True,
            },
            {"name": "Status", "value": "Landing Alert", "inline": True},
        ]

        if airport:
            airport_text = airport["name"]
            if airport.get("icao"):
                airport_text += f" ({airport['icao']})"
            if airport.get("iata"):
                airport_text += f" / {airport['iata']}"
            airport_text += f" — {airport['distance_miles']:.1f} mi away"
            airport_text += f"\nLongest runway: {airport.get('max_runway_ft', 0):.0f} ft"
            fields.append({"name": "Nearest Airport", "value": airport_text[:1024], "inline": False})

        if weather:
            weather_text = weather_block_text(weather)
            if len(weather_text) > 1024:
                weather_text = weather_text[:1021].rstrip() + "..."
            fields.append({"name": "Weather", "value": weather_text, "inline": False})

        if lat is not None and lon is not None:
            fields.append({"name": "Latitude / Longitude", "value": f"{lat:.6f}, {lon:.6f}", "inline": True})

        embed = {
            "title": title[:256],
            "description": description,
            "color": 15105570,
            "fields": fields[:25],
        }

        payload = {
            "username": self.config.discord_app_name,
            "avatar_url": self.config.discord_avatar_url,
            "embeds": [embed],
        }

        for attempt in range(1, 3):
            try:
                resp = requests.post(discord_url, json=payload, timeout=(5, 20))
                if resp.status_code == 204:
                    logger.info("Sent rich embed alert to Discord")
                    return True
                logger.error("Discord webhook failed: %d %s", resp.status_code, resp.text)
                return False
            except requests.exceptions.Timeout:
                logger.exception("Discord webhook timed out on attempt %d", attempt)
                if self.events:
                    self.events.log("discord_timeout", icao24=icao24, attempt=attempt)
                time.sleep(2)
            except requests.exceptions.RequestException:
                logger.exception("Discord webhook request failed on attempt %d", attempt)
                time.sleep(2)

        return False

    def send_discord_test(self):
        discord_url = self.config.discord_webhook_url
        if not discord_url:
            logger.warning("No Discord webhook URL configured; cannot send test")
            return False

        payload = {
            "username": self.config.discord_app_name,
            "avatar_url": self.config.discord_avatar_url,
            "embeds": [
                {
                    "title": "SteelJet Tracker Test",
                    "description": "This is a test notification from the tracker.",
                    "color": 3447003,
                    "fields": [{"name": "Status", "value": "Discord webhook test succeeded", "inline": False}],
                }
            ],
        }

        try:
            resp = requests.post(discord_url, json=payload, timeout=(5, 20))
            if resp.status_code == 204:
                logger.info("Discord test notification sent successfully")
                return True
            logger.error("Discord test failed: %d %s", resp.status_code, resp.text)
            return False
        except Exception as e:
            logger.exception("Error sending Discord test: %s", e)
            return False

    # ------------------------------------------------------------------ Email
    def send_email(self, subject, body):
        c = self.config
        if not (c.smtp_host and c.smtp_user and c.smtp_password and c.smtp_to):
            logger.warning("SMTP not fully configured; skipping email fallback")
            return False

        msg = EmailMessage()
        msg["From"] = f"{c.smtp_from_name} <{c.smtp_user}>"
        msg["To"] = c.smtp_to
        msg["Subject"] = subject
        msg.set_content(body)

        try:
            with smtplib.SMTP(c.smtp_host, c.smtp_port, timeout=20) as smtp:
                smtp.starttls()
                smtp.login(c.smtp_user, c.smtp_password)
                smtp.send_message(msg)
            logger.info("Sent email fallback alert")
            return True
        except Exception as e:
            logger.exception("Error sending email fallback: %s", e)
            return False

    def send_email_test(self):
        return self.send_email("SteelJet Tracker Test", "This is a test notification from the tracker.")

    # -------------------------------------------------------------- Combined
    def send_alert(self, subject, body, icao24, **discord_kwargs):
        """Send via Discord first; fall back to email if Discord fails.

        Returns "discord", "email", or None (both channels failed / are
        unconfigured).
        """
        sent = self.send_discord(subject, body, icao24, **discord_kwargs)
        if sent:
            return "discord"
        if self.events:
            self.events.log("email_fallback_sent", icao24=icao24)
        if self.send_email(subject, body):
            return "email"
        return None
