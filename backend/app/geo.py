"""Great-circle helpers. Angles are in degrees and distances in metres."""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_008.8
METRES_PER_NM = 1852.0
KT_TO_MPS = METRES_PER_NM / 3600.0


def normalize_lon(lon: float) -> float:
    return (lon + 540.0) % 360.0 - 180.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def destination_point(lat: float, lon: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    d = distance_m / EARTH_RADIUS_M
    b = math.radians(bearing_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), normalize_lon(math.degrees(l2))


def interpolate_gc(lat1: float, lon1: float, lat2: float, lon2: float, f: float) -> tuple[float, float]:
    """Point at fraction `f` (0..1) along the great circle from point 1 to point 2."""
    d = haversine_m(lat1, lon1, lat2, lon2) / EARTH_RADIUS_M
    if d < 1e-9:
        return lat1, lon1
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = math.sin((1 - f) * d) / math.sin(d)
    b = math.sin(f * d) / math.sin(d)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


def _cross_track_rad(lat: float, lon: float, lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float, float]:
    d13 = haversine_m(lat1, lon1, lat, lon) / EARTH_RADIUS_M
    t13 = math.radians(initial_bearing_deg(lat1, lon1, lat, lon))
    t12 = math.radians(initial_bearing_deg(lat1, lon1, lat2, lon2))
    dxt = math.asin(max(-1.0, min(1.0, math.sin(d13) * math.sin(t13 - t12))))
    return d13, t13 - t12, dxt


def cross_track_m(lat: float, lon: float, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance from (lat, lon) to the great circle through points 1 and 2."""
    return abs(_cross_track_rad(lat, lon, lat1, lon1, lat2, lon2)[2]) * EARTH_RADIUS_M


def along_track_m(lat: float, lon: float, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Signed distance from point 1, along the great circle 1→2, to the point closest to (lat, lon)."""
    d13, dt, dxt = _cross_track_rad(lat, lon, lat1, lon1, lat2, lon2)
    cos_dxt = math.cos(dxt)
    if cos_dxt < 1e-12:
        return 0.0
    dat = math.acos(max(-1.0, min(1.0, math.cos(d13) / cos_dxt)))
    return math.copysign(dat * EARTH_RADIUS_M, math.cos(dt))


def angle_diff_deg(a: float, b: float) -> float:
    """Smallest absolute difference between two headings."""
    d = abs(a - b) % 360.0
    return 360.0 - d if d > 180.0 else d
