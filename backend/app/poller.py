"""Background jobs that keep the LiveStore fresh within the data sources' rate limits.

Three kinds of upstream requests, in priority order:
1. Focus: aircraft someone has selected, refreshed every few seconds.
2. Regions: 250 nm circles covering areas people are viewing up close.
3. Sweep: the whole world, one aircraft type at a time (no free API has an
   "everything" endpoint, but a few dozen type queries cover most airliners).

Focus and regions use the detail feed (adsb.fi), the sweep uses adsb.lol.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time

from .feeds import PRIORITY_SWEEP, FeedClient, UpstreamError
from .live import LiveStore

log = logging.getLogger(__name__)

# Queried every sweep cycle: the most common airliner types worldwide.
SWEEP_TYPES_PRIMARY = (
    "A20N", "A320", "B738", "A321", "A21N", "B38M", "A319", "B737", "B739", "B77W",
    "B789", "E75L", "A333", "B788", "CRJ9", "A359", "B772", "AT76", "DH8D", "E190",
    "B763", "BCS3", "CRJ7", "B752",
)
# A slice of these is added to each cycle, so each is visited every few cycles:
# other airliners and freighters, business jets, popular light aircraft.
SWEEP_TYPES_SECONDARY = (
    "A332", "A339", "A35K", "A388", "B744", "B748", "B77L", "B78X", "BCS1", "E75S",
    "E195", "E170", "E290", "E295", "CRJ2", "AT75", "AT72", "B712", "A19N", "MD11",
    "B734", "B39M", "B733", "B753", "B762", "B764", "B773", "A306", "A318", "AT45",
    "AT46", "DH8C", "E145", "SU95", "C919", "B190", "C208", "PC12", "PC24", "BE20",
    "C25B", "C56X", "C68A", "CL35", "CL60", "GLEX", "GL7T", "GLF6", "FA7X", "E55P",
)
SECONDARY_PER_CYCLE = 10
SWEEP_PAUSE_S = 5.0
AREA_REFRESH_GUESS_S = 600.0  # typical time before the area sweep comes back to an area

CELL_LAT_DEG = 5.0
MAX_REGION_CELLS = 9
REGION_TTL_S = 15.0  # refetch a watched region after this long
REGION_DEMAND_S = 30.0  # keep refreshing a region this long after someone last looked at it
MAX_FOCUS = 10
FOCUS_TTL_S = 5.0
FOCUS_DEMAND_S = 30.0

Cell = tuple[int, int]


def _has_position(records: list[dict]) -> bool:
    return any(r.get("lat") is not None or r.get("lastPosition") for r in records)


def _band_count() -> int:
    return int(180 / CELL_LAT_DEG)


def _cells_in_band(band: int) -> int:
    lat_c = -90.0 + (band + 0.5) * CELL_LAT_DEG
    return max(1, int(360.0 * math.cos(math.radians(lat_c)) / CELL_LAT_DEG))


def cell_center(cell: Cell) -> tuple[float, float]:
    band, idx = cell
    step = 360.0 / _cells_in_band(band)
    return -90.0 + (band + 0.5) * CELL_LAT_DEG, -180.0 + (idx + 0.5) * step


def cell_radius_nm(cell: Cell) -> int:
    """Radius of the circle that covers the whole cell (the APIs allow at most 250 nm)."""
    band, _ = cell
    south = -90.0 + band * CELL_LAT_DEG
    north = south + CELL_LAT_DEG
    widest_lat = 0.0 if south < 0.0 < north else min(abs(south), abs(north))
    half_lat_m = CELL_LAT_DEG / 2 * 111_195
    half_lon_m = 360.0 / _cells_in_band(band) / 2 * 111_195 * math.cos(math.radians(widest_lat))
    return min(250, math.ceil(math.hypot(half_lat_m, half_lon_m) / 1852) + 5)


def cells_for_bbox(bbox: tuple[float, float, float, float], max_cells: int = MAX_REGION_CELLS) -> list[Cell] | None:
    """Grid cells covering a lon/lat box, or None if the box needs more than `max_cells`."""
    west, south, east, north = bbox
    first_band = max(0, int((south + 90.0) // CELL_LAT_DEG))
    last_band = min(_band_count() - 1, int((north + 90.0) // CELL_LAT_DEG))
    cells: list[Cell] = []
    for band in range(first_band, last_band + 1):
        n = _cells_in_band(band)
        step = 360.0 / n

        def index(lon: float) -> int:
            return min(n - 1, max(0, int((lon + 180.0) // step)))

        if west <= east:
            indices = range(index(west), index(east) + 1)
        else:
            indices = [*range(index(west), n), *range(0, index(east) + 1)]
        cells.extend((band, i) for i in indices)
        if len(cells) > max_cells:
            return None
    return cells


class Poller:
    def __init__(self, sweep_feed: FeedClient, detail_feed: FeedClient, live: LiveStore, *, global_sweep: bool = True):
        self.sweep_feed = sweep_feed
        self.detail_feed = detail_feed
        self.live = live
        self.global_sweep = global_sweep and sweep_feed.supports_type
        self._kick = asyncio.Event()
        self._region_demand: dict[Cell, float] = {}
        self._region_fetched: dict[Cell, float] = {}
        self._focus_demand: dict[str, float] = {}
        self._focus_fetched: dict[str, float] = {}
        self._focus_via_sweep_feed: set[str] = set()  # selected aircraft the detail feed can't see
        self._tasks: dict[object, asyncio.Task] = {}
        self.sweep_status: dict = {"cycles": 0, "lastCycleS": None, "lastCycleEnd": None, "typesDone": 0}
        self._area_fetched: dict[Cell, float] = {}
        self.area_status: dict = {"cells": 0, "visits": 0, "medianAgeS": None}

    # --- Demand from visitors ---

    def want_region(self, bbox: tuple[float, float, float, float]) -> bool:
        """Ask for detailed data in a box. Returns False when the box is too big for regional detail."""
        cells = cells_for_bbox(bbox)
        if cells is None:
            return False
        now = time.time()
        for cell in cells:
            self._region_demand[cell] = now
        self._kick.set()
        return True

    def want_focus(self, hex_code: str) -> None:
        self._focus_demand[hex_code.lower()] = time.time()
        if len(self._focus_demand) > MAX_FOCUS:
            oldest = sorted(self._focus_demand, key=self._focus_demand.__getitem__)
            for stale in oldest[: len(self._focus_demand) - MAX_FOCUS]:
                self._drop_focus(stale)
        self._kick.set()

    def region_age(self, bbox: tuple[float, float, float, float]) -> float | None:
        """Age in seconds of the oldest regional data covering the box (None if not all fetched yet)."""
        cells = cells_for_bbox(bbox)
        if not cells:
            return None
        fetched = [self._region_fetched.get(c) for c in cells]
        if any(f is None for f in fetched):
            return None
        return time.time() - min(fetched)

    # --- Loops ---

    async def run(self) -> None:
        loops = [self._demand_loop()]
        if self.global_sweep:
            loops.append(self._sweep_loop())
            if self.detail_feed is not self.sweep_feed:
                loops.append(self._area_sweep_loop())
        await asyncio.gather(*loops)

    async def _area_sweep_loop(self) -> None:
        """Visit every area with traffic, one 250 nm circle at a time, on the detail feed's spare capacity.

        The type sweep only finds common airliner types; this finds everything (light aircraft,
        helicopters, rare types). Requests run at sweep priority, so whatever visitors are looking
        at goes first, and the feed's own pacing keeps us under its rate limit.
        """
        await asyncio.sleep(20)  # let the type sweep place the first aircraft
        counts: dict[Cell, int] = {}
        counted_at = 0.0
        while True:
            now = time.time()
            if now - counted_at > 60:
                counts, counted_at = self._traffic_cells(), now
            if not counts:
                await asyncio.sleep(30)
                continue
            # Busy areas come round more often than quiet ones: wait time × √(aircraft there).
            cell = max(counts, key=lambda c: (now - self._area_fetched.get(c, 0)) * math.sqrt(counts[c]))
            lat, lon = cell_center(cell)
            try:
                records = await self.detail_feed.in_circle(lat, lon, cell_radius_nm(cell), PRIORITY_SWEEP)
                self.live.ingest(records, refresh_s=AREA_REFRESH_GUESS_S)
                counts[cell] = max(1, sum(1 for r in records if r.get("lat") is not None))
            except UpstreamError as exc:
                log.info("area %s failed: %s", cell, exc)
            self._area_fetched[cell] = time.time()
            ages = sorted(now - self._area_fetched[c] for c in counts if c in self._area_fetched)
            self.area_status.update(cells=len(counts), visits=self.area_status["visits"] + 1,
                                    medianAgeS=round(ages[len(ages) // 2]) if ages else None)

    def _traffic_cells(self) -> dict[Cell, int]:
        """Grid cells with aircraft we've actually received (not estimates) → how many."""
        counts: dict[Cell, int] = {}
        for state in self.live.snapshot():
            if state.estimated:
                continue  # over an ocean with no receivers: asking would return nothing
            band = min(_band_count() - 1, max(0, int((state.lat + 90.0) // CELL_LAT_DEG)))
            n = _cells_in_band(band)
            cell = (band, min(n - 1, int((state.lon + 180.0) // (360.0 / n))))
            counts[cell] = counts.get(cell, 0) + 1
        return counts

    async def _sweep_loop(self) -> None:
        cycle = 0
        n_slices = math.ceil(len(SWEEP_TYPES_SECONDARY) / SECONDARY_PER_CYCLE)
        while True:
            start = (cycle % n_slices) * SECONDARY_PER_CYCLE
            secondary = SWEEP_TYPES_SECONDARY[start:start + SECONDARY_PER_CYCLE]
            started = time.monotonic()
            for i, type_code in enumerate(SWEEP_TYPES_PRIMARY + secondary, 1):
                # Primary types come round every cycle, secondary ones every n_slices cycles.
                revisit = self.live.refresh_s * (1 if i <= len(SWEEP_TYPES_PRIMARY) else n_slices)
                try:
                    self.live.ingest(await self.sweep_feed.by_type(type_code), refresh_s=revisit)
                except UpstreamError as exc:
                    log.warning("sweep %s failed: %s", type_code, exc)
                self.sweep_status["typesDone"] = i
            cycle += 1
            took = time.monotonic() - started
            self.sweep_status.update(cycles=cycle, lastCycleS=round(took), lastCycleEnd=time.time(), typesDone=0)
            # Positions get as old as the time between visits; tell the store so it
            # doesn't mistake "not refreshed yet" for "out of coverage".
            self.live.refresh_s = max(self.live.refresh_s * 0.5, took + SWEEP_PAUSE_S)
            log.info("world sweep %d done in %ds, %d aircraft known", cycle, took, len(self.live))
            await asyncio.sleep(SWEEP_PAUSE_S)

    # When a source only allows a request every few seconds, refresh less often
    # so focus and region requests don't crowd each other out.
    def _focus_ttl(self, hex_code: str | None = None) -> float:
        feed = self.sweep_feed if hex_code in self._focus_via_sweep_feed else self.detail_feed
        return max(FOCUS_TTL_S, 3 * feed.interval)

    def _region_ttl(self) -> float:
        return max(REGION_TTL_S, 6 * self.detail_feed.interval)

    async def _demand_loop(self) -> None:
        while True:
            now = time.time()
            region_ttl = self._region_ttl()
            for hex_code, wanted in list(self._focus_demand.items()):
                if now - wanted > FOCUS_DEMAND_S:
                    self._drop_focus(hex_code)
                elif now - self._focus_fetched.get(hex_code, 0) > self._focus_ttl(hex_code):
                    self._spawn(("focus", hex_code), self._fetch_focus(hex_code))
            due = [
                cell
                for cell, wanted in self._region_demand.items()
                if now - wanted <= REGION_DEMAND_S and now - self._region_fetched.get(cell, 0) > region_ttl
            ]
            for cell in sorted(due, key=lambda c: self._region_fetched.get(c, 0)):
                self._spawn(("region", cell), self._fetch_region(cell))
            for cell in [c for c, wanted in self._region_demand.items() if now - wanted > REGION_DEMAND_S]:
                del self._region_demand[cell]
                task = self._tasks.get(("region", cell))
                if task:
                    task.cancel()  # nobody is looking any more: don't spend a request on it
            self._kick.clear()
            try:
                await asyncio.wait_for(self._kick.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    def _drop_focus(self, hex_code: str) -> None:
        self._focus_demand.pop(hex_code, None)
        self._focus_via_sweep_feed.discard(hex_code)
        task = self._tasks.get(("focus", hex_code))
        if task:
            task.cancel()

    def _spawn(self, key: object, coro) -> None:
        if key in self._tasks:
            coro.close()
            return
        task = asyncio.create_task(coro)
        self._tasks[key] = task
        task.add_done_callback(lambda t: self._tasks.pop(key, None))

    async def _fetch_focus(self, hex_code: str) -> None:
        try:
            if hex_code not in self._focus_via_sweep_feed:
                records = await self.detail_feed.by_hex(hex_code)
                if _has_position(records) or self.detail_feed is self.sweep_feed:
                    self.live.ingest(records, refresh_s=self._focus_ttl(hex_code))
                    return
                # adsb.fi doesn't see it (its coverage differs, e.g. in parts of Asia): use adsb.lol.
                self._focus_via_sweep_feed.add(hex_code)
            self.live.ingest(await self.sweep_feed.by_hex(hex_code), refresh_s=self._focus_ttl(hex_code))
        except UpstreamError as exc:
            log.info("focus %s failed: %s", hex_code, exc)
        finally:
            self._focus_fetched[hex_code] = time.time()

    async def _fetch_region(self, cell: Cell) -> None:
        lat, lon = cell_center(cell)
        try:
            records = await self.detail_feed.in_circle(lat, lon, cell_radius_nm(cell))
            # Region refreshes stop when nobody is looking; fall back to the sweep's pace.
            self.live.ingest(records, refresh_s=max(self._region_ttl(), self.live.refresh_s))
        except UpstreamError as exc:
            log.info("region %s failed: %s", cell, exc)
        finally:
            self._region_fetched[cell] = time.time()

    def status(self) -> dict:
        return {
            "sweep": self.sweep_status,
            "areaSweep": self.area_status,
            "watchedRegions": len(self._region_demand),
            "focused": len(self._focus_demand),
        }
