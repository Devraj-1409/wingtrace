"""Import a day of full-detail flight history from adsb.lol's open archive.

adsb.lol publishes every aircraft's track for each UTC day as a GitHub release
(github.com/adsblol/globe_history_YYYY, ODbL), about 4 GB, around 03:30 UTC the
next day. The archive is a tar file split into parts. This streams it with HTTP
range requests, jumping over the parts we don't need (heatmaps make up the first
gigabyte), splits each aircraft's trace into flights, simplifies them and
stores them. Nothing but our own database is written to disk.

Usage:
  python -m app.importer                         # yesterday (UTC)
  python -m app.importer --date 2026-10-01
  python -m app.importer --date 2026-10-01 --sample-mb 300   # only ~300 MB of tracks, for testing
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from datetime import date, datetime, timedelta, timezone

import httpx

from .config import Settings
from .live import is_hidden
from .recorder import LOW_ALT_FT, MIN_DURATION_S, NEW_FLIGHT_GAP_S
from .refdata import RefData
from .store import FlightRecord, FlightStore
from .tracks import TrackPoint, simplify_track

log = logging.getLogger("importer")

RELEASES_API = "https://api.github.com/repos/adsblol/globe_history_{year}/releases/tags/{tag}"
_BLOCK = 512
_JUMP_OVER_BYTES = 4 * 1024 * 1024  # skip unwanted members this big with a new range request
_BATCH_TRACES = 40
_MAX_PENDING = 48


# --- Remote archive access ---


class RemoteParts:
    """A file split into consecutive parts on the web, readable as one stream from any offset."""

    def __init__(self, http: httpx.Client, parts: list[tuple[str, int]]):
        self.http = http
        self.parts = parts  # (url, size)
        self.total = sum(size for _, size in parts)
        self.downloaded = 0

    def stream_from(self, offset: int) -> Iterator[bytes]:
        start = 0
        for url, size in self.parts:
            if offset >= start + size:
                start += size
                continue
            inner = max(0, offset - start)
            headers = {"Range": f"bytes={inner}-"} if inner else {}
            with self.http.stream("GET", url, headers=headers) as resp:
                resp.raise_for_status()
                if inner and resp.status_code != 206:
                    raise RuntimeError("server ignored the range request")
                for chunk in resp.iter_bytes(1 << 20):
                    self.downloaded += len(chunk)
                    yield chunk
            start += size


class Reader:
    """Exact-size reads over a chunk iterator, re-positionable by opening a new stream."""

    def __init__(self, remote: RemoteParts):
        self.remote = remote
        self.offset = 0
        self._chunks: Iterator[bytes] = remote.stream_from(0)
        self._buf = bytearray()

    def read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = next(self._chunks, None)
            if chunk is None:
                break
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        self.offset += len(out)
        return out

    def skip(self, n: int) -> None:
        if n - len(self._buf) > _JUMP_OVER_BYTES:
            self.offset += n
            self._buf.clear()
            close = getattr(self._chunks, "close", None)
            if close:
                close()
            self._chunks = self.remote.stream_from(self.offset)
        else:
            self.read(n)


def _tar_field(header: bytes, start: int, length: int) -> str:
    return header[start:start + length].split(b"\0", 1)[0].decode("utf-8", "replace")


def tar_members(reader: Reader, want) -> Iterator[tuple[str, bytes]]:
    """Walk a tar stream, yielding (name, data) for members where want(name) is true."""
    long_name: str | None = None
    while True:
        header = reader.read(_BLOCK)
        if len(header) < _BLOCK or header == b"\0" * _BLOCK:
            return
        size_field = header[124:136]
        if size_field[0] & 0x80:  # GNU base-256 size for huge members
            size = int.from_bytes(size_field[1:], "big")
        else:
            size = int(_tar_field(header, 124, 12).strip() or "0", 8)
        kind = header[156:157]
        padded = (size + _BLOCK - 1) // _BLOCK * _BLOCK
        if kind == b"L":  # GNU long name: the next member's name is the data
            long_name = reader.read(padded)[:size].split(b"\0", 1)[0].decode("utf-8", "replace")
            continue
        if kind in (b"x", b"g"):  # pax headers: only the path is of interest
            data = reader.read(padded)[:size].decode("utf-8", "replace")
            for record in data.splitlines():
                if " path=" in record:
                    long_name = record.split(" path=", 1)[1]
            continue
        prefix = _tar_field(header, 345, 155) if header[257:262] == b"ustar" else ""
        name = long_name or (f"{prefix}/{_tar_field(header, 0, 100)}" if prefix else _tar_field(header, 0, 100))
        long_name = None
        if kind in (b"0", b"\0") and want(name):
            data = reader.read(padded)[:size]
            yield name, data
        else:
            reader.skip(padded)


def release_parts(http: httpx.Client, day: date, variant: str = "prod") -> list[tuple[str, int]]:
    tag = f"v{day:%Y.%m.%d}-planes-readsb-{variant}-0"
    resp = http.get(RELEASES_API.format(year=day.year, tag=tag), headers={"Accept": "application/vnd.github+json"})
    if resp.status_code == 404:
        raise SystemExit(f"No archive for {day} yet ({tag}); adsb.lol publishes each day around 03:30 UTC the next day.")
    resp.raise_for_status()
    assets = sorted(resp.json()["assets"], key=lambda a: a["name"])
    return [(a["browser_download_url"], a["size"]) for a in assets if ".tar" in a["name"]]


# --- Turning traces into flights (runs in worker processes) ---


def flights_from_trace(raw: bytes, hide_military: bool) -> list[dict]:
    """Split one aircraft's day-long readsb trace into flights."""
    d = json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
    if is_hidden(int(d.get("dbFlags") or 0), hide_military):
        return []
    base = float(d["timestamp"])
    legs: list[tuple[list[TrackPoint], Counter]] = []
    points: list[TrackPoint] = []
    callsigns: Counter = Counter()
    last_alt: int | None = None
    for p in d.get("trace", []):
        # [dt, lat, lon, alt|"ground"|null, gs, track, flags, vrate, aircraft|null, ...]
        if p[6] & 2 and points:  # readsb marks the start of a new leg
            legs.append((points, callsigns))
            points, callsigns = [], Counter()
        info = p[8] if len(p) > 8 else None
        if info and info.get("flight"):
            callsigns[info["flight"].strip()] += 1
        if p[1] is None or p[2] is None:
            continue
        if p[3] == "ground":
            alt = None
        elif isinstance(p[3], (int, float)):
            alt = last_alt = int(p[3])
        else:
            alt = last_alt
        points.append((base + p[0], p[1], p[2], alt, p[4], p[5]))
    if points:
        legs.append((points, callsigns))

    out = []
    for leg, names in legs:
        for flight in _split_on_gaps(leg):
            flight = _trim_ground(flight)
            airborne = [p for p in flight if p[3] is not None]
            if len(airborne) < 2 or airborne[-1][0] - airborne[0][0] < MIN_DURATION_S:
                continue
            simplified = simplify_track(flight)
            out.append({
                "hex": d["icao"].lower(),
                "callsign": names.most_common(1)[0][0] if names else None,
                "registration": d.get("r"),
                "type_code": d.get("t"),
                "points": simplified,
            })
    return out


def _split_on_gaps(points: list[TrackPoint]) -> list[list[TrackPoint]]:
    """Also split where the aircraft vanished while low for a long time (readsb sometimes misses a leg)."""
    flights, current = [], []
    for p in points:
        if current:
            last = current[-1]
            low = last[3] is None or p[3] is None or last[3] < LOW_ALT_FT or p[3] < LOW_ALT_FT
            if p[0] - last[0] > NEW_FLIGHT_GAP_S and low:
                flights.append(current)
                current = []
        current.append(p)
    if current:
        flights.append(current)
    return flights


def _trim_ground(points: list[TrackPoint]) -> list[TrackPoint]:
    """Keep only the last ground position before take-off and the first after landing."""
    first = next((i for i, p in enumerate(points) if p[3] is not None), None)
    if first is None:
        return []
    last = max(i for i, p in enumerate(points) if p[3] is not None)
    return points[max(0, first - 1):min(len(points), last + 2)]


def _process_batch(batch: list[bytes], hide_military: bool) -> list[dict]:
    out = []
    for raw in batch:
        try:
            out.extend(flights_from_trace(raw, hide_military))
        except (ValueError, KeyError, IndexError, TypeError, OSError):
            continue  # one malformed trace shouldn't stop the import
    return out


# --- Main ---


MIDNIGHT_WINDOW_S = 3 * 3600  # flights already airborne this soon after 00:00 UTC may continue yesterday's


def _records(flights: list[dict], refdata: RefData, store: FlightStore, day_start: float) -> list[FlightRecord]:
    """Turn traced flights into records, joining flights that cross midnight UTC to yesterday's part."""
    records = []
    for f in flights:
        pts = f["points"]
        key = refdata.normalize_callsign(f["callsign"]) or (f["callsign"] or "").upper() or None
        flight_id = None
        if pts[0][3] is not None and pts[0][0] < day_start + MIDNIGHT_WINDOW_S:
            previous = store.find_continuation(f["hex"], key, pts[0][0], max_gap_s=MIDNIGHT_WINDOW_S)
            if previous and previous.points and previous.points[-1][3] is not None:
                pts = previous.points + [p for p in pts if p[0] > previous.points[-1][0]]
                flight_id = previous.id
        mid = pts[len(pts) // 2]
        route = refdata.match_route(f["callsign"], mid[1], mid[2], mid[5])
        plausible = route is not None and route.plausible
        records.append(FlightRecord(
            hex=f["hex"],
            callsign=f["callsign"],
            registration=f["registration"],
            type_code=f["type_code"],
            origin=route.origin.icao if plausible else None,
            destination=route.destination.icao if plausible else None,
            start_ts=pts[0][0],
            end_ts=pts[-1][0],
            source="archive",
            points=pts,
            id=flight_id,
            callsign_key=key,
        ))
    return records


def run_import(day: date, settings: Settings, sample_mb: int | None = None, workers: int | None = None) -> dict:
    refdata = RefData(settings.reference_db)
    store = FlightStore(settings.flights_db)
    stats = Counter()
    started = time.monotonic()
    workers = workers or max(1, (os.cpu_count() or 2) - 1)
    with httpx.Client(timeout=httpx.Timeout(60, connect=20), follow_redirects=True,
                      headers={"User-Agent": settings.user_agent}) as http:
        parts = release_parts(http, day)
        remote = RemoteParts(http, parts)
        log.info("Archive for %s: %d parts, %.1f GB", day, len(parts), remote.total / 1e9)
        reader = Reader(remote)
        limit = sample_mb * 1_000_000 if sample_mb else None
        pending: set[Future] = set()

        day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()

        def collect(done: set[Future]) -> None:
            for fut in done:
                records = _records(fut.result(), refdata, store, day_start)
                stats["joinedAcrossMidnight"] += sum(1 for r in records if r.id is not None)
                store.save_archive_batch(records)
                stats["flights"] += len(records)

        with ProcessPoolExecutor(max_workers=workers) as pool:
            batch: list[bytes] = []
            trace_bytes = 0
            for name, data in tar_members(reader, lambda n: "trace_full_" in n):
                batch.append(data)
                stats["traces"] += 1
                trace_bytes += len(data)
                if len(batch) >= _BATCH_TRACES:
                    pending.add(pool.submit(_process_batch, batch, settings.hide_military))
                    batch = []
                if len(pending) >= _MAX_PENDING:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    collect(done)
                if stats["traces"] % 5000 == 0:
                    log.info("%d traces, %d flights stored, %.0f MB downloaded, %.0fs",
                             stats["traces"], stats["flights"], remote.downloaded / 1e6, time.monotonic() - started)
                if limit and trace_bytes >= limit:
                    break
            if batch:
                pending.add(pool.submit(_process_batch, batch, settings.hide_military))
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                collect(done)
    stats["seconds"] = round(time.monotonic() - started)
    stats["downloadedMB"] = round(remote.downloaded / 1e6)
    store.mark_imported(day.isoformat(), stats["flights"], complete=sample_mb is None)
    store.close()
    refdata.close()
    log.info("Done: %s", dict(stats))
    return dict(stats)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description="Import a day of flight history from adsb.lol's open archive.")
    parser.add_argument("--date", help="UTC day, YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--sample-mb", type=int, help="stop after this many MB of tracks (for testing)")
    parser.add_argument("--workers", type=int, help="worker processes (default: CPU count - 1)")
    args = parser.parse_args()
    day = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
           else (datetime.now(timezone.utc) - timedelta(days=1)).date())
    run_import(day, Settings.from_env(), sample_mb=args.sample_mb, workers=args.workers)


if __name__ == "__main__":
    main()
