import math

import pytest

from app.geo import (
    along_track_m,
    angle_diff_deg,
    cross_track_m,
    destination_point,
    haversine_m,
    initial_bearing_deg,
    interpolate_gc,
    normalize_lon,
)
from app.tracks import decode_track, encode_track, simplify_track

LHR = (51.4706, -0.461941)
JFK = (40.639801, -73.7789)


def test_haversine_lhr_jfk():
    assert haversine_m(*LHR, *JFK) == pytest.approx(5_540_000, rel=0.01)


def test_destination_point_round_trip():
    lat, lon = destination_point(*LHR, initial_bearing_deg(*LHR, *JFK), haversine_m(*LHR, *JFK))
    assert (lat, lon) == pytest.approx(JFK, abs=1e-6)


def test_interpolate_gc_endpoints_and_midpoint():
    assert interpolate_gc(*LHR, *JFK, 0.0) == pytest.approx(LHR, abs=1e-9)
    assert interpolate_gc(*LHR, *JFK, 1.0) == pytest.approx(JFK, abs=1e-9)
    mid = interpolate_gc(*LHR, *JFK, 0.5)
    assert haversine_m(*LHR, *mid) == pytest.approx(haversine_m(*mid, *JFK), rel=1e-6)
    assert mid[0] > 51.5  # the great circle bends north of both ends


def test_cross_and_along_track():
    mid = interpolate_gc(*LHR, *JFK, 0.5)
    assert cross_track_m(*mid, *LHR, *JFK) < 1
    assert along_track_m(*mid, *LHR, *JFK) == pytest.approx(haversine_m(*LHR, *JFK) / 2, rel=1e-6)
    off = destination_point(*mid, initial_bearing_deg(*mid, *JFK) + 90, 100_000)
    assert cross_track_m(*off, *LHR, *JFK) == pytest.approx(100_000, rel=0.01)
    assert along_track_m(52.0, 10.0, *LHR, *JFK) < 0  # east of London: "behind" the start


def test_angles():
    assert normalize_lon(190) == -170
    assert angle_diff_deg(350, 10) == 20
    assert angle_diff_deg(10, 350) == 20


def _straight_track(n=200, dt=8.0):
    pts = []
    lat, lon = LHR
    for i in range(n):
        lat, lon = destination_point(lat, lon, 270.0, 480 * 0.514444 * dt)
        pts.append((1_790_000_000 + i * dt, lat, lon, 35000, 480.0, 270.0))
    return pts


def test_simplify_keeps_shape_and_drops_redundant_points():
    track = _straight_track()
    simple = simplify_track(track)
    assert simple[0] == track[0] and simple[-1] == track[-1]
    assert len(simple) < len(track) / 8
    gaps = [b[0] - a[0] for a, b in zip(simple, simple[1:])]
    assert max(gaps) <= 130  # at least one point every ~2 minutes


def test_simplify_keeps_turns_climbs_and_gap_edges():
    track = _straight_track(50)
    t0 = track[-1][0]
    turn = [(t0 + 8 * (i + 1), 50.0 + i * 0.01, -10.0, 35000 + i * 200, 470.0, 270.0 + i * 3) for i in range(20)]
    after_gap = [(turn[-1][0] + 3600, 45.0, -40.0, 36000, 480.0, 300.0), (turn[-1][0] + 3610, 45.0, -40.1, 36000, 480.0, 300.0)]
    simple = simplify_track(track + turn + after_gap)
    kept_turn = [p for p in simple if p in turn]
    assert len(kept_turn) >= 10  # every few degrees of turn / few hundred feet of climb
    assert turn[-1] in simple and after_gap[0] in simple  # both sides of the data gap


def test_track_encoding_round_trip():
    track = _straight_track(30) + [(1_790_001_000.4, 40.6398, -73.7789, None, 12.0, None)]
    decoded = decode_track(encode_track(track))
    assert len(decoded) == len(track)
    for a, b in zip(track, decoded):
        assert b[0] == pytest.approx(a[0], abs=1)
        assert b[1] == pytest.approx(a[1], abs=1e-5) and b[2] == pytest.approx(a[2], abs=1e-5)
        assert b[3] == a[3]
        assert b[4] == pytest.approx(a[4], abs=0.5)
        assert (b[5] is None) == (a[5] is None)
        if a[5] is not None:
            assert b[5] == pytest.approx(a[5], abs=0.05)


def test_track_encoding_is_compact():
    track = _straight_track(300)
    assert len(encode_track(track)) < 300 * 10


def test_empty_track_encodes():
    assert decode_track(encode_track([])) == []
    assert math.isfinite(len(encode_track([])))
