"""Paced, prioritised access to community ADS-B data APIs.

Two free sources are used, each with its own client and pace:
- adsb.lol (ODbL, https://api.adsb.lol): the only one with queries by aircraft
  type, which the world sweep needs. Its rate limit is tight and changes with load.
- adsb.fi (personal, non-commercial use, credit required, 1 request/s,
  https://github.com/adsbfi/opendata): areas and single aircraft.

Every upstream request from the whole server goes through one queue per
source, so the load on these community services stays the same however many
people use the site. The pace adapts: it backs off sharply on HTTP 429 and
speeds up again slowly after successful requests.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

PRIORITY_FOCUS = 0  # aircraft someone has selected, search lookups
PRIORITY_REGION = 1  # areas people are looking at up close
PRIORITY_SWEEP = 2  # background world-wide refresh

# Every Nth request goes to the background sweep when it is waiting, so heavy
# visitor demand can't freeze the zoomed-out world view.
_SWEEP_EVERY = 3
_MAX_ATTEMPTS = 4


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    circle: str
    hex: str
    callsign: str
    registration: str
    type: str | None = None


ADSB_LOL = Provider(
    name="adsb.lol",
    base_url="https://api.adsb.lol",
    type="/v2/type/{code}",
    circle="/v2/point/{lat:.3f}/{lon:.3f}/{radius}",
    hex="/v2/hex/{hex}",
    callsign="/v2/callsign/{callsign}",
    registration="/v2/reg/{registration}",
)

ADSB_FI = Provider(
    name="adsb.fi",
    base_url="https://opendata.adsb.fi/api",
    circle="/v3/lat/{lat:.3f}/lon/{lon:.3f}/dist/{radius}",
    hex="/v2/hex/{hex}",
    callsign="/v2/callsign/{callsign}",
    registration="/v2/registration/{registration}",
)


class UpstreamError(Exception):
    pass


@dataclass(eq=False)
class _Job:
    path: str
    priority: int
    futures: list[asyncio.Future] = field(default_factory=list)
    attempts: int = 0

    @property
    def abandoned(self) -> bool:
        return all(f.done() for f in self.futures)


class FeedClient:
    def __init__(
        self,
        provider: Provider,
        user_agent: str,
        *,
        min_interval: float,
        start_interval: float,
        max_interval: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str | None = None,
    ):
        self.provider = provider
        self._http = httpx.AsyncClient(
            base_url=base_url or provider.base_url,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip"},
            timeout=httpx.Timeout(20.0, connect=10.0),
            transport=transport,
        )
        self._min_interval = min_interval
        self._start_interval = start_interval
        self._max_interval = max_interval
        self._interval = start_interval
        self._queues: tuple[deque[_Job], ...] = (deque(), deque(), deque())
        self._jobs: dict[str, _Job] = {}
        self._wake = asyncio.Event()
        self._next_slot = 0.0
        self._slots_since_sweep = 0
        self._worker: asyncio.Task | None = None
        self.stats = {"requests": 0, "ok": 0, "rateLimited": 0, "errors": 0, "lastOk": None, "lastError": None}

    @property
    def interval(self) -> float:
        return self._interval

    @property
    def supports_type(self) -> bool:
        return self.provider.type is not None

    def status(self) -> dict:
        return {"intervalS": round(self._interval, 2), "queued": [len(q) for q in self._queues], **self.stats}

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name=f"{self.provider.name}-worker")

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        for job in self._jobs.values():
            self._fail(job, UpstreamError("client closed"))
        self._jobs.clear()
        await self._http.aclose()

    async def fetch(self, path: str, priority: int = PRIORITY_SWEEP, timeout: float | None = None) -> dict:
        """Queue a GET and wait for its JSON body. Identical queued requests are shared."""
        future = asyncio.get_running_loop().create_future()
        job = self._jobs.get(path)
        if job is None:
            job = _Job(path, priority)
            self._jobs[path] = job
            self._queues[priority].append(job)
        elif priority < job.priority and job in self._queues[job.priority]:
            self._queues[job.priority].remove(job)
            job.priority = priority
            self._queues[priority].append(job)
        job.futures.append(future)
        self._wake.set()
        if timeout is None:
            return await future
        return await asyncio.wait_for(future, timeout)

    # Endpoints. Each returns the list of aircraft records.

    async def by_type(self, code: str, priority: int = PRIORITY_SWEEP) -> list[dict]:
        if self.provider.type is None:
            raise UpstreamError(f"{self.provider.name} has no aircraft-type query")
        return (await self.fetch(self.provider.type.format(code=code), priority)).get("ac") or []

    async def in_circle(self, lat: float, lon: float, radius_nm: int, priority: int = PRIORITY_REGION) -> list[dict]:
        path = self.provider.circle.format(lat=lat, lon=lon, radius=radius_nm)
        return (await self.fetch(path, priority)).get("ac") or []

    async def by_hex(self, hex_code: str, priority: int = PRIORITY_FOCUS, timeout: float | None = None) -> list[dict]:
        return (await self.fetch(self.provider.hex.format(hex=hex_code), priority, timeout)).get("ac") or []

    async def by_callsign(self, callsign: str, timeout: float | None = None) -> list[dict]:
        path = self.provider.callsign.format(callsign=callsign)
        return (await self.fetch(path, PRIORITY_FOCUS, timeout)).get("ac") or []

    async def by_registration(self, registration: str, timeout: float | None = None) -> list[dict]:
        path = self.provider.registration.format(registration=registration)
        return (await self.fetch(path, PRIORITY_FOCUS, timeout)).get("ac") or []

    # --- Worker ---

    def _pop(self) -> _Job | None:
        focus, region, sweep = self._queues
        for queue in self._queues:  # requests nobody waits for any more cost nothing
            while queue and queue[0].abandoned:
                self._forget(queue.popleft())
        if sweep and (self._slots_since_sweep >= _SWEEP_EVERY - 1 or not (focus or region)):
            self._slots_since_sweep = 0
            return sweep.popleft()
        for queue in (focus, region):
            if queue:
                self._slots_since_sweep += 1
                return queue.popleft()
        return None

    def _forget(self, job: _Job) -> None:
        if self._jobs.get(job.path) is job:
            del self._jobs[job.path]

    async def _run(self) -> None:
        while True:
            if not any(self._queues):
                self._wake.clear()
                await self._wake.wait()
                continue
            delay = self._next_slot - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
                continue
            job = self._pop()
            if job is None:
                continue
            self._next_slot = time.monotonic() + self._interval
            try:
                await self._perform(job)
            except Exception as exc:  # never let one bad response kill the worker
                log.exception("%s request %s failed", self.provider.name, job.path)
                self._finish(job, error=UpstreamError(str(exc)))

    async def _perform(self, job: _Job) -> None:
        self.stats["requests"] += 1
        try:
            resp = await self._http.get(job.path)
        except httpx.HTTPError as exc:
            self._retry_or_fail(job, f"{type(exc).__name__}", slow_down=1.5)
            return
        if resp.status_code == 429:
            self.stats["rateLimited"] += 1
            self._interval = min(self._max_interval, max(self._interval * 2, self._start_interval))
            retry_after = resp.headers.get("Retry-After", "")
            pause = float(retry_after) if retry_after.isdigit() else self._interval
            self._next_slot = time.monotonic() + max(pause, self._interval)
            log.info("%s rate limit hit; interval now %.1fs", self.provider.name, self._interval)
            self._retry_or_fail(job, "HTTP 429", slow_down=1.0)
            return
        if resp.status_code >= 500:
            self._retry_or_fail(job, f"HTTP {resp.status_code}", slow_down=1.5)
            return
        if resp.status_code != 200:
            self._finish(job, error=UpstreamError(f"HTTP {resp.status_code} for {job.path}"))
            return
        try:
            data = resp.json()
        except ValueError:
            self._retry_or_fail(job, "invalid JSON", slow_down=1.0)
            return
        self.stats["ok"] += 1
        self.stats["lastOk"] = time.time()
        self._interval = max(self._min_interval, self._interval * 0.97)
        self._finish(job, result=data)

    def _retry_or_fail(self, job: _Job, reason: str, slow_down: float) -> None:
        self.stats["errors"] += 1
        self.stats["lastError"] = f"{time.strftime('%H:%M:%S')} {reason}"
        if slow_down > 1:
            # Network trouble is not a rate limit: slow down, but only up to a point.
            cap = max(self._interval, min(self._max_interval, self._start_interval * 4))
            self._interval = min(cap, self._interval * slow_down)
        job.attempts += 1
        if job.attempts >= _MAX_ATTEMPTS or job.abandoned:
            self._finish(job, error=UpstreamError(f"{reason} for {job.path}"))
        else:
            self._queues[job.priority].appendleft(job)

    def _finish(self, job: _Job, result: dict | None = None, error: Exception | None = None) -> None:
        self._forget(job)
        if error is not None:
            self._fail(job, error)
            return
        for future in job.futures:
            if not future.done():
                future.set_result(result)

    @staticmethod
    def _fail(job: _Job, error: Exception) -> None:
        for future in job.futures:
            if not future.done():
                future.set_exception(error)
