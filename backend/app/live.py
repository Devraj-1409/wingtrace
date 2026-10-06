"""Live aircraft state: parsing upstream records, privacy filtering, estimation and snapshots."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

from .geo import KT_TO_MPS, haversine_m, initial_bearing_deg, interpolate_gc
from .refdata import RouteMatch

# readsb database flags (the `dbFlags` field)
DB_FLAG_MILITARY = 1
DB_FLAG_PIA = 4  # Privacy ICAO Address programme
DB_FLAG_LADD = 8  # FAA "Limiting Aircraft Data Displayed" list

# Compact-format flags sent to the browser
FLAG_GROUND = 1
FLAG_ESTIMATED = 2
FLAG_MLAT = 4
FLAG_EMERGENCY = 8

LOST_AFTER_S = 6 * 60  # no position for this long: out of coverage (the world sweep revisits every few minutes)
# Never show a position older than this as if it were current. After it, an aircraft is
# either estimated along a known route (and marked so) or not shown at all.
MAX_UNCONFIRMED_S = 15 * 60
DROP_GROUND_AFTER_S = 15 * 60
MAX_ESTIMATE_S = 14 * 3600  # longest gap we keep estimating along a route (long oceanic crossings)
_EMERGENCY_SQUAWKS = {"7500", "7600", "7700"}


@dataclass(slots=True)
class AircraftState:
    hex: str
    lat: float
    lon: float
    pos_ts: float  # unix time of the position
    alt_ft: int | None  # barometric altitude; None when unknown
    on_ground: bool
    gs: float | None  # ground speed, knots
    track: float | None  # degrees true
    vrate: int | None  # feet per minute
    callsign: str | None
    registration: str | None
    type_code: str | None
    squawk: str | None
    emergency: str | None
    category: str | None
    source: str  # adsb | mlat | other
    db_flags: int = 0
    estimated: bool = False
    refresh_s: float = 0.0  # how often the data source that delivered this revisits it

    @property
    def is_emergency(self) -> bool:
        return self.squawk in _EMERGENCY_SQUAWKS or (self.emergency not in (None, "none"))

    def flags(self) -> int:
        return (
            (FLAG_GROUND if self.on_ground else 0)
            | (FLAG_ESTIMATED if self.estimated else 0)
            | (FLAG_MLAT if self.source == "mlat" else 0)
            | (FLAG_EMERGENCY if self.is_emergency else 0)
        )

    def to_compact(self, now: float) -> list:
        """[hex, lat, lon, alt_ft|null, track|null, gs|null, age_s, callsign, type, flags, category, vrate|null]"""
        return [
            self.hex,
            round(self.lat, 5),
            round(self.lon, 5),
            self.alt_ft,
            None if self.track is None else round(self.track, 1),
            None if self.gs is None else round(self.gs),
            round(max(0.0, now - self.pos_ts), 1),
            self.callsign or "",
            self.type_code or "",
            self.flags(),
            self.category or "",
            self.vrate,
        ]

    def to_json(self, now: float) -> dict:
        return {
            "hex": self.hex,
            "lat": self.lat,
            "lon": self.lon,
            "altFt": self.alt_ft,
            "onGround": self.on_ground,
            "gs": self.gs,
            "track": self.track,
            "vrate": self.vrate,
            "callsign": self.callsign,
            "registration": self.registration,
            "type": self.type_code,
            "squawk": self.squawk,
            "emergency": self.is_emergency,
            "category": self.category,
            "source": self.source,
            "estimated": self.estimated,
            "ageS": round(max(0.0, now - self.pos_ts), 1),
        }


def parse_aircraft(raw: dict, now: float) -> AircraftState | None:
    """Turn an adsb.lol aircraft record into an AircraftState (None if it has no usable position)."""
    hex_code = (raw.get("hex") or "").lower()
    lat, lon, seen_pos = raw.get("lat"), raw.get("lon"), raw.get("seen_pos")
    if lat is None or lon is None:
        last = raw.get("lastPosition") or {}
        lat, lon, seen_pos = last.get("lat"), last.get("lon"), last.get("seen_pos")
    if not hex_code or lat is None or lon is None:
        return None
    alt_baro = raw.get("alt_baro")
    on_ground = alt_baro == "ground"
    if on_ground:
        alt_ft = None
    elif isinstance(alt_baro, (int, float)):
        alt_ft = int(alt_baro)
    elif isinstance(raw.get("alt_geom"), (int, float)):
        alt_ft = int(raw["alt_geom"])
    else:
        alt_ft = None
    kind = raw.get("type") or ""
    source = "mlat" if kind == "mlat" else "adsb" if kind.startswith("adsb") else "other"
    vrate = raw.get("baro_rate")
    if vrate is None:
        vrate = raw.get("geom_rate")
    track = raw.get("track")
    if track is None:
        track = raw.get("true_heading")  # aircraft on the ground often report heading only
    return AircraftState(
        hex=hex_code,
        lat=float(lat),
        lon=float(lon),
        pos_ts=now - float(seen_pos or 0.0),
        alt_ft=alt_ft,
        on_ground=on_ground,
        gs=raw.get("gs"),
        track=track,
        vrate=int(vrate) if isinstance(vrate, (int, float)) else None,
        callsign=(raw.get("flight") or "").strip() or None,
        registration=raw.get("r") or None,
        type_code=raw.get("t") or None,
        squawk=raw.get("squawk"),
        emergency=raw.get("emergency"),
        category=raw.get("category"),
        source=source,
        db_flags=int(raw.get("dbFlags") or 0),
    )


def is_hidden(db_flags: int, hide_military: bool) -> bool:
    """Aircraft whose owners asked not to be tracked publicly (LADD, PIA) are never shown."""
    mask = DB_FLAG_PIA | DB_FLAG_LADD | (DB_FLAG_MILITARY if hide_military else 0)
    return bool(db_flags & mask)


def in_bbox(lat: float, lon: float, bbox: tuple[float, float, float, float]) -> bool:
    west, south, east, north = bbox
    if not south <= lat <= north:
        return False
    if west <= east:
        return west <= lon <= east
    return lon >= west or lon <= east  # box crosses the antimeridian


RouteLookup = Callable[[AircraftState], "RouteMatch | None"]
Observer = Callable[[list[AircraftState]], None]


class LiveStore:
    """Latest known state of every aircraft, fed by the poller."""

    def __init__(self, *, hide_military: bool, route_lookup: RouteLookup | None = None):
        self.hide_military = hide_military
        self._route_lookup = route_lookup
        self._aircraft: dict[str, AircraftState] = {}
        self._observers: list[Observer] = []
        self.last_ingest: float | None = None
        # How often the world sweep revisits an aircraft (updated by the poller).
        self.refresh_s: float = 240.0

    @property
    def lost_after_s(self) -> float:
        """No position for this long means out of coverage, not just "not refreshed yet"."""
        return max(LOST_AFTER_S, 1.5 * self.refresh_s)

    def add_observer(self, observer: Observer) -> None:
        self._observers.append(observer)

    def __len__(self) -> int:
        return len(self._aircraft)

    def ingest(self, records: Iterable[dict], now: float | None = None, refresh_s: float | None = None) -> int:
        """Merge raw upstream records. Returns how many carried a newer position.

        `refresh_s` is how soon the same source will report these aircraft again.
        """
        now = time.time() if now is None else now
        updated: list[AircraftState] = []
        for raw in records:
            state = parse_aircraft(raw, now)
            if state is None:
                continue
            state.refresh_s = self.refresh_s if refresh_s is None else refresh_s
            if is_hidden(state.db_flags, self.hide_military):
                self._aircraft.pop(state.hex, None)
                continue
            known = self._aircraft.get(state.hex)
            if known is not None:
                if state.pos_ts <= known.pos_ts + 0.5:
                    continue
                # Some records lack identity fields; keep what we already know.
                state.callsign = state.callsign or known.callsign
                state.registration = state.registration or known.registration
                state.type_code = state.type_code or known.type_code
                # Records that only carry a position (e.g. readsb's lastPosition when the aircraft
                # is briefly out of range) have no altitude or speed: keep the last known values
                # rather than blanking them (which would also stop the aircraft being moved forward).
                if state.alt_ft is None and not state.on_ground:
                    state.alt_ft = known.alt_ft
                if state.gs is None:
                    state.gs = known.gs
                if state.track is None:
                    state.track = known.track
                if state.vrate is None:
                    state.vrate = known.vrate
            self._aircraft[state.hex] = state
            updated.append(state)
        self.last_ingest = now
        if updated:
            for observer in self._observers:
                observer(updated)
        return len(updated)

    def get(self, hex_code: str, now: float | None = None) -> AircraftState | None:
        state = self._aircraft.get(hex_code.lower())
        if state is None:
            return None
        return self._current(state, time.time() if now is None else now)

    def snapshot(self, bbox: tuple[float, float, float, float] | None = None, now: float | None = None) -> list[AircraftState]:
        now = time.time() if now is None else now
        out = []
        for hex_code, state in list(self._aircraft.items()):
            current = self._current(state, now)
            if current is None:
                del self._aircraft[hex_code]
                continue
            if bbox is None or in_bbox(current.lat, current.lon, bbox):
                out.append(current)
        return out

    def search(self, q: str, limit: int = 10) -> list[AircraftState]:
        q = q.strip().upper().replace(" ", "")
        if len(q) < 2:
            return []
        exact, prefix = [], []
        for state in self._aircraft.values():
            keys = (state.callsign or "", (state.registration or "").replace("-", ""), state.hex.upper())
            if q in keys or q.replace("-", "") in keys:
                exact.append(state)
            elif any(k.startswith(q) for k in keys if k):
                prefix.append(state)
        return (exact + sorted(prefix, key=lambda s: s.callsign or "~"))[:limit]

    def _current(self, state: AircraftState, now: float) -> AircraftState | None:
        """The state to show now: real, estimated along its route, or None to drop it."""
        age = now - state.pos_ts
        if state.on_ground:
            return state if age < DROP_GROUND_AFTER_S else None
        # Until its source is due to report it again, an old position just means "not refreshed yet".
        fresh_for = min(MAX_UNCONFIRMED_S, max(LOST_AFTER_S, 1.5 * (state.refresh_s or self.refresh_s)))
        if age < fresh_for:
            return state
        if age > MAX_ESTIMATE_S:
            return None
        cruising = state.alt_ft is not None and state.alt_ft >= 10_000 and (state.gs or 0) >= 150
        route = self._route_lookup(state) if cruising and self._route_lookup else None
        if route is None or not route.plausible:
            return None
        return estimate_along_route(state, route, now)


def estimate_along_route(state: AircraftState, route: RouteMatch, now: float) -> AircraftState | None:
    """Out of coverage: assume the aircraft keeps its speed along the great circle to its destination."""
    dest = route.destination
    remaining = haversine_m(state.lat, state.lon, dest.lat, dest.lon)
    travelled = (state.gs or 0.0) * KT_TO_MPS * (now - state.pos_ts)
    if travelled >= remaining - 50_000:  # should be descending or landed by now
        return None
    lat, lon = interpolate_gc(state.lat, state.lon, dest.lat, dest.lon, travelled / remaining)
    track = initial_bearing_deg(lat, lon, dest.lat, dest.lon)
    return replace(state, lat=lat, lon=lon, track=track, pos_ts=now, estimated=True)
