"""HTTP API used by the frontend. All endpoints are read-only GETs under /api."""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Request, Response

from .feeds import UpstreamError
from .services import Services
from .store import FlightRecord
from .top import compute_top
from .tracks import TrackPoint

router = APIRouter(prefix="/api")

_HEX_RE = re.compile(r"^~?[0-9a-f]{6}$")
_REG_RE = re.compile(r"^[A-Z0-9]{1,3}-[A-Z0-9]{2,5}$|^N[0-9][0-9A-Z]{1,4}$")


def _svc(request: Request) -> Services:
    return request.app.state.services


def _json(data: object) -> Response:
    return Response(json.dumps(data, separators=(",", ":")), media_type="application/json")


def parse_bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
    """`west,south,east,north` in degrees; west > east means the box crosses the antimeridian."""
    if not bbox:
        return None
    try:
        west, south, east, north = (float(v) for v in bbox.split(","))
    except ValueError:
        raise HTTPException(400, "bbox must be west,south,east,north") from None
    if not (-180 <= west <= 180 and -180 <= east <= 180 and -90 <= south < north <= 90):
        raise HTTPException(400, "bbox out of range")
    return west, south, east, north


def _track_json(points: list[TrackPoint]) -> list[list]:
    return [
        [int(t), round(lat, 5), round(lon, 5), alt, None if gs is None else round(gs), None if trk is None else round(trk, 1)]
        for t, lat, lon, alt, gs, trk in points
    ]


def _airport_json(svc: Services, code: str | None) -> dict | None:
    airport = svc.refdata.airport(code) if code else None
    return airport.to_json() if airport else None


@router.get("/health")
async def health(request: Request) -> dict:
    svc = _svc(request)
    return {
        "ok": True,
        "aircraft": len(svc.live),
        "upstream": {feed.provider.name: feed.status() for feed in svc.feeds},
        "poller": svc.poller.status(),
        "recorder": {"active": svc.recorder.active_count, **svc.recorder.stats},
        "referenceData": svc.refdata.available,
    }


@router.get("/meta")
async def meta(request: Request) -> dict:
    svc = _svc(request)
    stats = await asyncio.to_thread(svc.store.stats)
    return {
        "retentionDays": svc.settings.retention_days,
        "hideMilitary": svc.settings.hide_military,
        "referenceData": svc.refdata.meta(),
        "recordedFlights": stats,
        "historyDays": await asyncio.to_thread(svc.store.imported_days),
    }


@router.get("/live")
async def live(request: Request, bbox: str | None = None) -> Response:
    """Aircraft positions in a compact array format (see AircraftState.to_compact)."""
    svc = _svc(request)
    box = parse_bbox(bbox)
    regional = svc.poller.want_region(box) if box else False
    now = time.time()
    states = svc.live.snapshot(box, now)
    if box is None:
        # Whole-world view: aircraft on the ground are invisible from that far out, so don't send them.
        states = [s for s in states if not s.on_ground]
    ages = sorted(now - s.pos_ts for s in states if not s.estimated)
    return _json({
        "now": now,
        "typicalAgeS": round(ages[len(ages) // 2]) if ages else None,  # median age of the positions sent
        "regional": regional,
        "regionAgeS": svc.poller.region_age(box) if regional else None,
        "sweep": svc.poller.sweep_status,
        "maxExtrapolateS": round(svc.live.lost_after_s),
        "ac": [s.to_compact(now) for s in states],
    })


@router.get("/aircraft/{hex_code}")
async def aircraft(request: Request, hex_code: str) -> Response:
    """Full details for one aircraft: state, airline, model, route and the track flown so far."""
    svc = _svc(request)
    hex_code = hex_code.lower()
    if not _HEX_RE.match(hex_code):
        raise HTTPException(400, "invalid hex code")
    svc.poller.want_focus(hex_code)
    state = svc.live.get(hex_code)
    if state is None:
        await _lookup_upstream(svc, "by_hex", hex_code)
        state = svc.live.get(hex_code)
    if state is None:
        raise HTTPException(404, "aircraft not currently tracked")
    now = time.time()
    track = svc.recorder.current_track(hex_code)
    route, took_off_from = _checked_route(svc, state, track)
    airline = svc.refdata.airline_for_callsign(state.callsign)
    return _json({
        "now": now,
        "aircraft": state.to_json(now),
        "airline": airline,
        "flightNumber": _flight_number(svc, state.callsign, airline),
        "model": svc.refdata.model_name(state.type_code) if state.type_code else None,
        "route": route,
        "tookOffFrom": took_off_from.to_json() if took_off_from else None,
        # When we saw it leave the ground (first airborne point after a ground or low point).
        "departedAt": _departure_time(track) if took_off_from else None,
        "track": _track_json(track),
    })


def _flight_number(svc: Services, callsign: str | None, airline: dict | None) -> str | None:
    """The flight number passengers know, e.g. callsign ALK605 → UL605."""
    norm = svc.refdata.normalize_callsign(callsign)
    if not norm or not airline or not airline.get("iata") or not norm[:3].isalpha():
        return None
    return airline["iata"] + norm[3:]


def _departure_time(track: list[TrackPoint]) -> int | None:
    for p in track:
        if p[3] is not None:
            return int(p[0])
    return None


def _checked_route(svc: Services, state, track: list[TrackPoint]) -> tuple[dict | None, object | None]:
    """The route from schedule data, cross-checked against where we saw the aircraft take off.

    `confidence` is "confirmed" when the observed departure airport matches the route,
    "schedule" when we didn't see the departure. A route whose origin contradicts the
    observed departure is marked not plausible, so it isn't shown.
    """
    took_off_from = None
    if track:
        first = track[0]
        if first[3] is None or first[3] < 3_000:  # first seen on the ground or just after take-off
            took_off_from = svc.refdata.nearest_airport(first[1], first[2])
    match = svc.refdata.match_route(state.callsign, state.lat, state.lon, state.track)
    if match is None:
        return None, took_off_from
    route = match.to_json()
    route["confidence"] = "schedule"
    if match.plausible and took_off_from is not None:
        if took_off_from.icao == match.origin.icao:
            route["confidence"] = "confirmed"
        else:
            route["plausible"] = False
            route["conflict"] = f"seen taking off from {took_off_from.iata or took_off_from.icao}"
    return route, took_off_from


async def _lookup_upstream(svc: Services, method: str, value: str) -> None:
    """Fetch aircraft we don't know yet: adsb.fi first, then adsb.lol, whose coverage differs."""
    feeds = [svc.detail_feed] if svc.detail_feed is svc.sweep_feed else [svc.detail_feed, svc.sweep_feed]
    for feed, timeout in zip(feeds, (8, 20)):
        try:
            records = await getattr(feed, method)(value, timeout=timeout)
        except (UpstreamError, asyncio.TimeoutError):
            continue
        svc.live.ingest(records)
        if any(r.get("lat") is not None or r.get("lastPosition") for r in records):
            return


@router.get("/search")
async def search(request: Request, q: str = Query(min_length=2, max_length=40), deep: bool = False) -> dict:
    """Live aircraft by callsign / flight number / registration / hex, plus airports.

    With `deep`, aircraft we don't know about yet are looked up upstream (one request).
    """
    svc = _svc(request)
    q = q.strip()
    found = svc.live.search(q)
    norm = svc.refdata.normalize_callsign(q)
    if norm and norm != q.upper():
        found += [s for s in svc.live.search(norm) if s not in found]
    if not found and deep:
        if _REG_RE.match(q.upper()):
            await _lookup_upstream(svc, "by_registration", q.upper())
        elif _HEX_RE.match(q.lower()):
            await _lookup_upstream(svc, "by_hex", q.lower())
        else:
            await _lookup_upstream(svc, "by_callsign", norm or q.upper())
        found = svc.live.search(norm or q)
    now = time.time()
    aircraft = []
    for s in found[:8]:
        route, _ = _checked_route(svc, s, svc.recorder.current_track(s.hex))
        aircraft.append({
            **s.to_json(now),
            "route": [route["origin"]["iata"] or route["origin"]["icao"], route["destination"]["iata"] or route["destination"]["icao"]]
            if route and route["plausible"] else None,
        })
    return {"aircraft": aircraft, "airports": [a.to_json() for a in svc.refdata.search_airports(q, limit=6)]}


_TOP_CACHE_S = 30.0
_top_cache: dict[int, tuple[float, dict]] = {}


@router.get("/top")
async def top(request: Request) -> Response:
    """Top-10 lists of the aircraft in the air right now (recomputed at most every 30 s)."""
    svc = _svc(request)
    now = time.time()
    cached = _top_cache.get(id(svc))
    if cached and now - cached[0] < _TOP_CACHE_S:
        return _json(cached[1])
    states = svc.live.snapshot(now=now)  # taken on the event loop; the ranking runs in a thread
    data = {"now": now, **await asyncio.to_thread(compute_top, states, svc.refdata, now)}
    _top_cache[id(svc)] = (now, data)
    return _json(data)


@router.get("/airports/{code}")
async def airport(request: Request, code: str) -> dict:
    found = _airport_json(_svc(request), code)
    if found is None:
        raise HTTPException(404, "unknown airport")
    return found


# --- Replay ---


def _day_window(date: str | None, retention_days: int) -> tuple[float, float | None]:
    if not date:
        return time.time() - retention_days * 86400, None
    try:
        day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD") from None
    return day.timestamp(), (day + timedelta(days=1)).timestamp()


@router.get("/flights")
async def flights(
    request: Request,
    q: str | None = Query(None, max_length=40),
    airport: str | None = Query(None, max_length=8),
    date: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    """Recorded flights by callsign / flight number / registration / hex and/or airport, optionally on one UTC day."""
    svc = _svc(request)
    if not q and not airport:
        raise HTTPException(400, "give a flight, registration, hex code or airport")
    start, end = _day_window(date, svc.settings.retention_days)
    callsigns, registration, hex_code = [], None, None
    if q:
        q = q.strip()
        callsigns = list({q.upper(), svc.refdata.normalize_callsign(q) or q.upper()})
        registration = q
        hex_code = q.lower() if _HEX_RE.match(q.lower()) else None
    airports = []
    if airport:
        found = svc.refdata.airport(airport)
        if found is None:
            return {"flights": [], "airports": {}}
        airports = [found.icao]
    records = await asyncio.to_thread(
        svc.store.search,
        callsigns=callsigns, registration=registration, hex_code=hex_code,
        airports=airports, start_ts=start, end_ts=end, limit=limit,
    )
    codes = {c for r in records for c in (r.origin, r.destination) if c}
    return {
        "flights": [r.summary_json() for r in records],
        "airports": {c: _airport_json(svc, c) for c in codes},
    }


def _flight_payload(svc: Services, record: FlightRecord) -> dict:
    return {
        "flight": record.summary_json(),
        "airline": svc.refdata.airline_for_callsign(record.callsign),
        "model": svc.refdata.model_name(record.type_code) if record.type_code else None,
        "origin": _airport_json(svc, record.origin),
        "destination": _airport_json(svc, record.destination),
        "track": _track_json(record.points),
    }


@router.get("/flights/live/{hex_code}")
async def live_flight(request: Request, hex_code: str) -> Response:
    """The flight an aircraft is on right now, as recorded so far."""
    svc = _svc(request)
    record = svc.recorder.current_flight(hex_code.lower())
    if record is None:
        raise HTTPException(404, "no recorded track for this aircraft yet")
    return _json(_flight_payload(svc, record))


@router.get("/flights/{flight_id}")
async def flight(request: Request, flight_id: int) -> Response:
    svc = _svc(request)
    record = await asyncio.to_thread(svc.store.get, flight_id)
    if record is None:
        raise HTTPException(404, "unknown flight")
    return _json(_flight_payload(svc, record))
