"""Top-10 lists of the aircraft in the air right now: longest and shortest routes, fastest, highest."""

from __future__ import annotations

from collections.abc import Iterable

from .geo import haversine_m
from .live import AircraftState
from .refdata import Airport, RefData

TOP_N = 10
MAX_PLAUSIBLE_GS_KT = 750  # faster readings are glitches (no airliner flies that fast, even in a jet stream)
MAX_PLAUSIBLE_ALT_FT = 60_000
MIN_ROUTE_KM = 30


def _airport(a: Airport) -> dict:
    return {"icao": a.icao, "iata": a.iata, "city": a.city or a.name}


def compute_top(states: Iterable[AircraftState], refdata: RefData, now: float) -> dict:
    entries = []
    for s in states:
        if s.on_ground or s.alt_ft is None:
            continue
        entry = {
            "hex": s.hex,
            "callsign": s.callsign,
            "airline": (refdata.airline_for_callsign(s.callsign) or {}).get("name"),
            "type": s.type_code,
            "altFt": s.alt_ft,
            "gs": s.gs,
            "estimated": s.estimated,
            "ageS": round(now - s.pos_ts),
            "route": None,
        }
        match = refdata.match_route(s.callsign, s.lat, s.lon, s.track)
        if match and match.plausible:
            o, d = match.origin, match.destination
            km = haversine_m(o.lat, o.lon, d.lat, d.lon) / 1000
            flown = haversine_m(o.lat, o.lon, s.lat, s.lon) / 1000
            entry["route"] = {
                "origin": _airport(o),
                "destination": _airport(d),
                "km": round(km),
                "progress": round(min(1.0, flown / km), 3) if km else 0,
            }
        entries.append(entry)

    def unique(rows: list[dict]) -> list[dict]:
        """One entry per flight (callsign), in order."""
        seen, out = set(), []
        for row in rows:
            key = row["callsign"] or row["hex"]
            if key not in seen:
                seen.add(key)
                out.append(row)
            if len(out) == TOP_N:
                break
        return out

    routed = [e for e in entries if e["route"] and e["route"]["km"] >= MIN_ROUTE_KM]
    measured = [e for e in entries if not e["estimated"] and e["ageS"] < 300]
    return {
        "longest": unique(sorted(routed, key=lambda e: -e["route"]["km"])),
        "shortest": unique(sorted(routed, key=lambda e: e["route"]["km"])),
        "fastest": unique(sorted(
            (e for e in measured if e["gs"] and e["gs"] < MAX_PLAUSIBLE_GS_KT), key=lambda e: -e["gs"])),
        "highest": unique(sorted(
            (e for e in measured if e["altFt"] < MAX_PLAUSIBLE_ALT_FT), key=lambda e: -e["altFt"])),
        "flightsWithRoutes": len(routed),
        "flights": len(entries),
    }
