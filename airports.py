"""
Full path: steeljet_tracker/airports.py

Airport + runway lookups used to resolve the nearest matching airport (and
nearby alternates) for a given lat/lon. Behavior is identical to the
original single-file tracker's airport matching logic, just isolated into
its own module.
"""

import math
import logging

import pandas as pd

logger = logging.getLogger("steeljet")

VALID_AIRPORT_TYPES = {"large_airport", "medium_airport", "small_airport", "heliport"}


def haversine_miles(lat1, lon1, lat2, lon2):
    r = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class AirportIndex:
    def __init__(self, config):
        self.config = config
        self._df = None

    def load(self):
        if self._df is not None:
            return self._df

        airports_csv = self.config.airports_csv
        runways_csv = self.config.runways_csv

        if not airports_csv.exists():
            logger.warning("Airport CSV not found at %s; airport names will be unavailable", airports_csv)
            self._df = pd.DataFrame()
            return self._df

        airports = pd.read_csv(airports_csv, low_memory=False)
        airports.columns = [c.lower() for c in airports.columns]

        required_cols = {"id", "type", "name", "latitude_deg", "longitude_deg"}
        if not required_cols.issubset(airports.columns):
            logger.warning("Airport CSV missing required columns: %s", required_cols)
            self._df = pd.DataFrame()
            return self._df

        airports = airports[airports["type"].isin(VALID_AIRPORT_TYPES)].copy()

        if runways_csv.exists():
            runways = pd.read_csv(runways_csv, low_memory=False)
            runways.columns = [c.lower() for c in runways.columns]
            if {"airport_ref", "length_ft"}.issubset(runways.columns):
                runways["length_ft"] = pd.to_numeric(runways["length_ft"], errors="coerce")
                runway_max = (
                    runways.groupby("airport_ref", as_index=False)["length_ft"]
                    .max()
                    .rename(columns={"airport_ref": "id", "length_ft": "max_runway_ft"})
                )
                airports = airports.merge(runway_max, on="id", how="left")
                airports = airports[
                    airports["max_runway_ft"].fillna(0) >= self.config.min_runway_length_ft
                ].copy()
            else:
                logger.warning("Runways CSV missing required columns; skipping runway filter")
        else:
            logger.warning("Runways CSV not found at %s; skipping runway filter", runways_csv)

        self._df = airports.reset_index(drop=True)
        logger.info(
            "Loaded %d airports with runway >= %d ft",
            len(self._df),
            self.config.min_runway_length_ft,
        )
        return self._df

    def resolve_airport(self, lat, lon, radius_miles=None):
        df = self.load()
        if df.empty or lat is None or lon is None:
            return None

        radius_miles = radius_miles or self.config.airport_lookup_radius_miles
        best_row = None
        best_dist = float("inf")

        for _, row in df.iterrows():
            alat, alon = row["latitude_deg"], row["longitude_deg"]
            if pd.isna(alat) or pd.isna(alon):
                continue
            dist = haversine_miles(float(lat), float(lon), float(alat), float(alon))
            if dist < best_dist:
                best_dist = dist
                best_row = row

        if best_row is None or best_dist > radius_miles:
            return None

        icao = best_row.get("icao_code", "")
        if pd.isna(icao) or not str(icao).strip():
            icao = best_row.get("ident", "")
        iata = best_row.get("iata_code", "")
        if pd.isna(iata):
            iata = ""

        return {
            "name": str(best_row.get("name", "Unknown Airport")),
            "icao": str(icao) if str(icao).strip() else "",
            "iata": str(iata) if str(iata).strip() else "",
            "distance_miles": best_dist,
            "max_runway_ft": float(best_row.get("max_runway_ft", 0) or 0),
            "lat": float(best_row.get("latitude_deg")),
            "lon": float(best_row.get("longitude_deg")),
        }

    def resolve_nearby_airports(self, lat, lon, limit=3):
        df = self.load()
        if df.empty or lat is None or lon is None:
            return []

        candidates = []
        for _, row in df.iterrows():
            alat, alon = row["latitude_deg"], row["longitude_deg"]
            if pd.isna(alat) or pd.isna(alon):
                continue
            dist = haversine_miles(float(lat), float(lon), float(alat), float(alon))
            icao = row.get("icao_code", "")
            if pd.isna(icao) or not str(icao).strip():
                icao = row.get("ident", "")
            iata = row.get("iata_code", "")
            if pd.isna(iata):
                iata = ""
            candidates.append({
                "name": str(row.get("name", "Unknown Airport")),
                "icao": str(icao) if str(icao).strip() else "",
                "iata": str(iata) if str(iata).strip() else "",
                "distance_miles": dist,
                "max_runway_ft": float(row.get("max_runway_ft", 0) or 0),
            })

        candidates.sort(key=lambda x: x["distance_miles"])
        return candidates[:limit]
