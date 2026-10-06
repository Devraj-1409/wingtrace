"""Turns live position updates into recorded flights, for replay."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .geo import KT_TO_MPS, haversine_m
from .live import AircraftState
from .refdata import RefData
from .store import FlightRecord, FlightStore
from .tracks import TrackPoint, simplify_track

log = logging.getLogger(__name__)

LOW_ALT_FT = 10_000
NEW_FLIGHT_GAP_S = 30 * 60  # a gap this long while low (or implausible speed) starts a new flight
MAX_GAP_S = 12 * 3600
CLOSE_LOW_AFTER_S = 30 * 60  # unseen this long after being low: flight over
CLOSE_HIGH_AFTER_S = 12 * 3600  # unseen at cruise: may be crossing an ocean, keep it open
FLUSH_EVERY_S = 120.0
MIN_DURATION_S = 120.0
MAX_POINTS_IN_MEMORY = 4000


@dataclass
class _Active:
    hex: str
    callsign: str | None
    registration: str | None
    type_code: str | None
    points: list[TrackPoint] = field(default_factory=list)
    flight_id: int | None = None
    landed: bool = False
    dirty: bool = True
    last_flush: float = 0.0


class Recorder:
    def __init__(self, store: FlightStore, refdata: RefData):
        self.store = store
        self.refdata = refdata
        self._active: dict[str, _Active] = {}
        self._parked: dict[str, TrackPoint] = {}  # last ground position of aircraft not flying
        self._closed: list[_Active] = []
        self.stats = {"saved": 0, "closed": 0}

    # --- Fed by LiveStore (synchronous and cheap) ---

    def observe(self, states: list[AircraftState]) -> None:
        for s in states:
            if s.estimated:
                continue
            point: TrackPoint = (s.pos_ts, s.lat, s.lon, None if s.on_ground else s.alt_ft, s.gs, s.track)
            flight = self._active.get(s.hex)
            if flight is not None and point[0] <= flight.points[-1][0]:
                continue
            if flight is not None and self._is_new_flight(flight, s, point):
                self._close(flight)
                flight = None
            if flight is None:
                if s.on_ground:
                    self._parked[s.hex] = point
                    continue
                flight = _Active(s.hex, s.callsign, s.registration, s.type_code)
                parked = self._parked.pop(s.hex, None)
                if parked is not None and point[0] - parked[0] < 20 * 60:
                    flight.points.append(parked)  # where it took off from
                self._active[s.hex] = flight
            elif s.on_ground:
                if flight.landed:
                    continue  # taxiing after landing: keep only the touchdown point
                flight.landed = True
            flight.points.append(point)
            flight.dirty = True
            flight.callsign = flight.callsign or s.callsign
            flight.registration = flight.registration or s.registration
            flight.type_code = flight.type_code or s.type_code
            if len(flight.points) > MAX_POINTS_IN_MEMORY:
                flight.points = simplify_track(flight.points)

    def _is_new_flight(self, flight: _Active, s: AircraftState, point: TrackPoint) -> bool:
        last = flight.points[-1]
        gap = point[0] - last[0]
        if flight.landed and not s.on_ground:
            return True
        if gap > MAX_GAP_S:
            return True
        if s.callsign and flight.callsign and s.callsign != flight.callsign:
            return True
        if gap > NEW_FLIGHT_GAP_S:
            if last[3] is None or point[3] is None or last[3] < LOW_ALT_FT or point[3] < LOW_ALT_FT:
                return True
            speed_kt = haversine_m(last[1], last[2], point[1], point[2]) / gap / KT_TO_MPS
            return not 150 <= speed_kt <= 700
        return False

    def _close(self, flight: _Active) -> None:
        self._active.pop(flight.hex, None)
        self._closed.append(flight)
        self.stats["closed"] += 1

    def restore(self, now: float | None = None) -> int:
        """After a restart, pick up the flights that were in progress, so their paths aren't lost."""
        now = time.time() if now is None else now
        restored = 0
        for rec in self.store.recent_live(now - CLOSE_LOW_AFTER_S):
            if rec.hex in self._active or len(rec.points) < 2:
                continue
            last = rec.points[-1]
            self._active[rec.hex] = _Active(
                rec.hex, rec.callsign, rec.registration, rec.type_code, list(rec.points),
                flight_id=rec.id, landed=last[3] is None, dirty=False, last_flush=now,
            )
            restored += 1
        return restored

    # --- Queries ---

    def current_track(self, hex_code: str) -> list[TrackPoint]:
        flight = self._active.get(hex_code.lower())
        return list(flight.points) if flight else []

    def current_flight(self, hex_code: str) -> FlightRecord | None:
        flight = self._active.get(hex_code.lower())
        return self._to_record(flight, simplify=False) if flight and len(flight.points) >= 2 else None

    @property
    def active_count(self) -> int:
        return len(self._active)

    # --- Persistence ---

    def _to_record(self, flight: _Active, simplify: bool = True) -> FlightRecord:
        points = simplify_track(flight.points) if simplify else list(flight.points)
        last = flight.points[-1]
        route = self.refdata.match_route(flight.callsign, last[1], last[2], last[5])
        plausible = route is not None and route.plausible
        return FlightRecord(
            hex=flight.hex,
            callsign=flight.callsign,
            registration=flight.registration,
            type_code=flight.type_code,
            origin=route.origin.icao if plausible else None,
            destination=route.destination.icao if plausible else None,
            start_ts=points[0][0],
            end_ts=points[-1][0],
            source="live",
            points=points,
            id=flight.flight_id,
            callsign_key=self.refdata.normalize_callsign(flight.callsign) or flight.callsign,
        )

    @staticmethod
    def _worth_saving(flight: _Active) -> bool:
        airborne = [p for p in flight.points if p[3] is not None]
        return len(airborne) >= 2 and airborne[-1][0] - airborne[0][0] >= MIN_DURATION_S

    def _collect(self, now: float, everything: bool = False) -> list[tuple[_Active, FlightRecord]]:
        for flight in list(self._active.values()):
            last = flight.points[-1]
            unseen = now - last[0]
            high = last[3] is not None and last[3] >= LOW_ALT_FT
            if unseen > (CLOSE_HIGH_AFTER_S if high else CLOSE_LOW_AFTER_S):
                self._close(flight)
        batch = [(f, self._to_record(f)) for f in self._closed if self._worth_saving(f)]
        self._closed.clear()
        for flight in self._active.values():
            if flight.dirty and (everything or now - flight.last_flush >= FLUSH_EVERY_S) and self._worth_saving(flight):
                batch.append((flight, self._to_record(flight)))
                flight.dirty = False
                flight.last_flush = now
        for hex_code in [h for h, p in self._parked.items() if now - p[0] > 3600]:
            del self._parked[hex_code]
        return batch

    def _save(self, batch: list[tuple[_Active, FlightRecord]]) -> None:
        for flight, record in batch:
            flight.flight_id = self.store.save(record)
            self.stats["saved"] += 1

    async def run(self, interval: float = 30.0) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                batch = self._collect(time.time())
                if batch:
                    await asyncio.to_thread(self._save, batch)
            except Exception:
                log.exception("recorder flush failed")

    def flush_all(self) -> None:
        self._save(self._collect(time.time(), everything=True))
