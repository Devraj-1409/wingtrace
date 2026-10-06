"""Reference data lookups: routes by callsign, airlines, aircraft models and airports.

The database is built by `python -m app.refdata_build` from VRS standing-data
(CC0) and OurAirports (public domain).
"""

from __future__ import annotations

import math
import re
import sqlite3
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .geo import along_track_m, angle_diff_deg, cross_track_m, haversine_m, initial_bearing_deg


@dataclass(frozen=True)
class Airport:
    icao: str
    iata: str | None
    name: str
    city: str | None
    country: str | None
    lat: float
    lon: float
    elevation_ft: int | None
    kind: str | None  # OurAirports type: large_airport, medium_airport, ...
    tz: str | None = None  # IANA time zone, e.g. Asia/Kolkata

    def to_json(self) -> dict:
        return {
            "icao": self.icao,
            "iata": self.iata,
            "name": self.name,
            "city": self.city,
            "country": self.country,
            "lat": self.lat,
            "lon": self.lon,
            "elevationFt": self.elevation_ft,
            "kind": self.kind,
            "tz": self.tz,
        }


@dataclass(frozen=True)
class RouteMatch:
    callsign: str  # normalised callsign the route was found under
    airports: tuple[Airport, ...]  # every stop of the route, in order
    origin: Airport  # the leg the aircraft is believed to be flying
    destination: Airport
    plausible: bool  # False when the aircraft's position doesn't fit the route

    def to_json(self) -> dict:
        return {
            "callsign": self.callsign,
            "origin": self.origin.to_json(),
            "destination": self.destination.to_json(),
            "stops": [a.icao for a in self.airports],
            "plausible": self.plausible,
        }


LONG_HAUL_M = 2_500_000.0
MAX_LONG_HAUL_DETOUR = 1.3

_CALLSIGN_RE = re.compile(r"^([A-Z]{2,3}|[A-Z][0-9]|[0-9][A-Z])(\d[A-Z0-9]*)$")
_NUMBER_RE = re.compile(r"^(\d{1,4}|\d{1,3}[A-Z]|\d{1,2}[A-Z]{2})$")
_AIRPORT_COLUMNS = "icao, iata, name, city, country, lat, lon, elevation_ft, kind, tz"


class RefData:
    """Read-only access to reference.sqlite. Every lookup returns None if the database is missing."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._iata_to_icao: dict[str, str] = {}
        if path.exists():
            self._conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
            self._iata_to_icao = dict(self._query("SELECT iata, icao FROM airline_iata"))
        self.airport = lru_cache(maxsize=20_000)(self._airport)
        self.route_airports = lru_cache(maxsize=50_000)(self._route_airports)
        self.airline = lru_cache(maxsize=5_000)(self._airline)
        self.model_name = lru_cache(maxsize=5_000)(self._model_name)

    @property
    def available(self) -> bool:
        return self._conn is not None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        if self._conn is None:
            return []
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def meta(self) -> dict[str, str]:
        return dict(self._query("SELECT key, value FROM meta"))

    # --- Callsigns and routes ---

    def normalize_callsign(self, callsign: str | None) -> str | None:
        """Normalise like VRS standing-data does: ICAO airline code + number without leading zeros."""
        if not callsign:
            return None
        m = _CALLSIGN_RE.match(callsign.strip().upper().replace(" ", ""))
        if not m:
            return None
        code, number = m.groups()
        if len(code) == 2:
            code = self._iata_to_icao.get(code, code)
        number = number.lstrip("0")
        if not number or number.isalpha():
            number = "0" + number
        if not _NUMBER_RE.match(number):
            return None
        return code + number

    def _route_airports(self, normalized_callsign: str) -> tuple[Airport, ...] | None:
        rows = self._query("SELECT airports FROM routes WHERE callsign = ?", (normalized_callsign,))
        if not rows:
            return None
        airports = tuple(a for a in (self.airport(code) for code in rows[0][0].split("-")) if a is not None)
        return airports if len(airports) >= 2 else None

    def match_route(
        self,
        callsign: str | None,
        lat: float | None = None,
        lon: float | None = None,
        track: float | None = None,
    ) -> RouteMatch | None:
        """Look up a route and pick the leg that fits the aircraft's position.

        Route data is crowd-sourced and keyed only by callsign, so it can be
        stale. A route is `plausible` only if the aircraft is near one of its
        legs and, mid-flight, heading towards that leg's destination.
        """
        norm = self.normalize_callsign(callsign)
        if norm is None:
            return None
        airports = self.route_airports(norm)
        if airports is None:
            return None
        if lat is None or lon is None:
            return RouteMatch(norm, airports, airports[0], airports[-1], plausible=False)
        best: tuple[float, Airport, Airport] | None = None
        for a, b in zip(airports, airports[1:]):
            leg_m = haversine_m(a.lat, a.lon, b.lat, b.lon)
            if leg_m < 1_000:
                continue
            d_a = haversine_m(lat, lon, a.lat, a.lon)
            d_b = haversine_m(lat, lon, b.lat, b.lon)
            xt = cross_track_m(lat, lon, a.lat, a.lon, b.lat, b.lon)
            at = along_track_m(lat, lon, a.lat, a.lon, b.lat, b.lon)
            detour = (d_a + d_b) / leg_m
            near_line = xt <= max(100_000.0, 0.15 * leg_m) and -50_000.0 <= at <= leg_m + 50_000.0
            # Long-haul flights often go far around closed airspace (Russia, Pakistan, Ukraine…):
            # accept positions within a 30% detour of the direct path on long legs.
            on_detour = leg_m >= LONG_HAUL_M and detour <= MAX_LONG_HAUL_DETOUR
            if not (near_line or on_detour):
                continue
            if track is not None and min(d_a, d_b) > 50_000.0:
                if angle_diff_deg(track, initial_bearing_deg(lat, lon, b.lat, b.lon)) > 90.0:
                    continue  # flying away from this leg's destination
            if best is None or detour < best[0]:
                best = (detour, a, b)
        if best is None:
            return RouteMatch(norm, airports, airports[0], airports[-1], plausible=False)
        return RouteMatch(norm, airports, best[1], best[2], plausible=True)

    # --- Airlines, aircraft models, airports ---

    def _airline(self, icao: str) -> dict | None:
        rows = self._query("SELECT icao, iata, name FROM airlines WHERE icao = ?", (icao.upper(),))
        return {"icao": rows[0][0], "iata": rows[0][1], "name": rows[0][2]} if rows else None

    def airline_for_callsign(self, callsign: str | None) -> dict | None:
        norm = self.normalize_callsign(callsign)
        if norm is None or not norm[:3].isalpha():
            return None
        return self.airline(norm[:3])

    def _model_name(self, type_code: str) -> str | None:
        rows = self._query("SELECT manufacturer, model FROM models WHERE icao = ?", (type_code.upper(),))
        if not rows:
            return None
        manufacturer, model = rows[0]
        if manufacturer and model and not model.lower().startswith(manufacturer.lower()):
            return f"{manufacturer} {model}"
        return model or manufacturer

    @staticmethod
    def _airport_from_row(row: tuple) -> Airport:
        return Airport(*row)

    def _airport(self, code: str) -> Airport | None:
        code = code.strip().upper()
        if len(code) == 3:
            rows = self._query(
                f"SELECT {_AIRPORT_COLUMNS} FROM airports WHERE iata = ? ORDER BY {_KIND_RANK} LIMIT 1", (code,)
            )
        else:
            rows = self._query(f"SELECT {_AIRPORT_COLUMNS} FROM airports WHERE icao = ?", (code,))
        return self._airport_from_row(rows[0]) if rows else None

    def nearest_airport(self, lat: float, lon: float, max_km: float = 20.0) -> Airport | None:
        """The airport an aircraft at (lat, lon) is most likely using: the nearest large or medium
        airport within `max_km`, else the nearest small airfield within a few kilometres."""
        dlat = max_km / 111.0
        dlon = dlat / max(0.05, math.cos(math.radians(lat)))
        rows = self._query(
            f"SELECT {_AIRPORT_COLUMNS} FROM airports WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
            " AND (kind IN ('large_airport', 'medium_airport', 'small_airport') OR kind IS NULL)",
            (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
        )
        best: tuple[float, Airport] | None = None
        for airport in map(self._airport_from_row, rows):
            km = haversine_m(lat, lon, airport.lat, airport.lon) / 1000
            limit = max_km if airport.kind in ("large_airport", "medium_airport") else min(max_km, 5.0)
            if km <= limit and (best is None or km < best[0]):
                best = (km, airport)
        return best[1] if best else None

    def search_airports(self, q: str, limit: int = 8) -> list[Airport]:
        q = q.strip()
        if len(q) < 2:
            return []
        exact = self.airport(q) if len(q) in (3, 4) else None
        rows = self._query(
            f"SELECT {_AIRPORT_COLUMNS} FROM airports "
            f"WHERE kind IN ('large_airport', 'medium_airport') AND (name LIKE ? OR city LIKE ?) "
            f"ORDER BY (name LIKE ? OR city LIKE ?) DESC, {_KIND_RANK}, name LIMIT ?",
            (f"%{q}%", f"%{q}%", f"{q}%", f"{q}%", limit),
        )
        results = [exact] if exact else []
        results += [a for a in map(self._airport_from_row, rows) if not exact or a.icao != exact.icao]
        return results[:limit]


_KIND_RANK = (
    "CASE kind WHEN 'large_airport' THEN 0 WHEN 'medium_airport' THEN 1 "
    "WHEN 'small_airport' THEN 2 ELSE 3 END"
)
