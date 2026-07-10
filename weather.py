"""
Full path: steeljet_tracker/weather.py

METAR/TAF lookups from aviationweather.gov for the airport an alert resolves
to. Same behavior as the original single-file tracker's weather functions.
"""

import logging

import requests

logger = logging.getLogger("steeljet")

BASE_URL = "https://aviationweather.gov/api/data"


def get_airport_weather(icao, timeout_sec):
    icao = (icao or "").strip().upper()
    if not icao:
        return {"metar": None, "taf": None, "note": "No ICAO code available for weather lookup."}

    metar_url = f"{BASE_URL}/metar?ids={icao}&format=json"
    taf_url = f"{BASE_URL}/taf?ids={icao}&format=json"

    metar_text = None
    taf_text = None
    note = None

    try:
        metar_resp = requests.get(metar_url, timeout=timeout_sec)
        metar_resp.raise_for_status()
        metar_data = metar_resp.json()
        if isinstance(metar_data, list) and metar_data:
            item = metar_data[0]
            metar_text = item.get("rawOb") or item.get("raw_text") or item.get("raw") or item.get("text")
        elif isinstance(metar_data, dict):
            metar_text = (
                metar_data.get("rawOb")
                or metar_data.get("raw_text")
                or metar_data.get("raw")
                or metar_data.get("text")
            )
    except Exception as e:
        note = f"No METAR available: {e.__class__.__name__}"

    try:
        taf_resp = requests.get(taf_url, timeout=timeout_sec)
        taf_resp.raise_for_status()
        taf_data = taf_resp.json()
        if isinstance(taf_data, list) and taf_data:
            item = taf_data[0]
            taf_text = item.get("rawTAF") or item.get("raw_text") or item.get("raw") or item.get("text")
        elif isinstance(taf_data, dict):
            taf_text = (
                taf_data.get("rawTAF")
                or taf_data.get("raw_text")
                or taf_data.get("raw")
                or taf_data.get("text")
            )
    except Exception as e:
        note = f"{note}; No TAF available: {e.__class__.__name__}" if note else f"TAF lookup failed: {e.__class__.__name__}"

    if not metar_text and not taf_text and not note:
        note = "No weather data returned."

    return {"metar": metar_text, "taf": taf_text, "note": note}


def weather_block_text(weather):
    parts = []
    if weather.get("metar"):
        parts.append(f"METAR: {weather['metar']}")
    if weather.get("taf"):
        parts.append(f"TAF: {weather['taf']}")
    if weather.get("note"):
        parts.append(f"Weather note: {weather['note']}")
    return "\n".join(parts) if parts else "Weather data unavailable."
