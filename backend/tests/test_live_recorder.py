import pytest

from app.geo import destination_point, interpolate_gc
from app.live import (
    DB_FLAG_LADD,
    DB_FLAG_MILITARY,
    DB_FLAG_PIA,
    FLAG_ESTIMATED,
    FLAG_GROUND,
    LiveStore,
    in_bbox,
    parse_aircraft,
)
from app.recorder import Recorder
from app.store import FlightStore
from tests.conftest import adsb_record

NOW = 1_791_000_000.0


def test_parse_aircraft_basic_and_ground():
    s = parse_aircraft(adsb_record("ABC123", 51.0, -1.0), NOW)
    assert s.hex == "abc123" and s.callsign == "BAW117" and s.alt_ft == 35000 and not s.on_ground
    assert s.pos_ts == pytest.approx(NOW - 1.0)
    g = parse_aircraft(adsb_record("abc124", 51.0, -1.0, alt="ground", track=None, true_heading=90.0), NOW)
    assert g.on_ground and g.alt_ft is None and g.track == 90.0


def test_parse_aircraft_uses_last_position_and_rejects_missing():
    rec = adsb_record("abc125", 0, 0)
    del rec["lat"], rec["lon"]
    rec["lastPosition"] = {"lat": 10.0, "lon": 20.0, "seen_pos": 30.0}
    s = parse_aircraft(rec, NOW)
    assert (s.lat, s.lon, s.pos_ts) == (10.0, 20.0, NOW - 30.0)
    del rec["lastPosition"]
    assert parse_aircraft(rec, NOW) is None


@pytest.mark.parametrize("flags, hide_military, hidden", [
    (DB_FLAG_LADD, False, True),
    (DB_FLAG_PIA, False, True),
    (DB_FLAG_MILITARY, True, True),
    (DB_FLAG_MILITARY, False, False),
    (0, True, False),
])
def test_privacy_filter(flags, hide_military, hidden):
    store = LiveStore(hide_military=hide_military)
    store.ingest([adsb_record("abc126", 51.0, -1.0, dbFlags=flags)], now=NOW)
    assert (store.get("abc126", now=NOW) is None) == hidden


def test_ingest_keeps_newest_and_identity():
    store = LiveStore(hide_military=True)
    store.ingest([adsb_record("abc127", 51.0, -1.0, seen_pos=1.0)], now=NOW)
    older = adsb_record("abc127", 52.0, -2.0, seen_pos=1.0, flight="", r=None)
    assert store.ingest([older], now=NOW - 10) == 0  # older position ignored
    store.ingest([older], now=NOW + 10)
    s = store.get("abc127", now=NOW + 10)
    assert s.lat == 52.0 and s.callsign == "BAW117" and s.registration == "G-XLEA"


def test_position_only_record_keeps_altitude_and_speed():
    store = LiveStore(hide_military=True)
    store.ingest([adsb_record("abc132", 51.0, -1.0, alt=36000, gs=480.0, track=270.0, baro_rate=0)], now=NOW)
    # Out of range: the source only has a last position, no altitude/speed/track.
    rec = {"hex": "abc132", "lastPosition": {"lat": 51.0, "lon": -1.5, "seen_pos": 5.0}, "flight": "BAW117  "}
    store.ingest([rec], now=NOW + 120)
    s = store.get("abc132", now=NOW + 120)
    assert s.lon == -1.5 and s.alt_ft == 36000 and s.gs == 480.0 and s.track == 270.0


def test_bbox_and_antimeridian():
    assert in_bbox(10, 20, (0, 0, 30, 30))
    assert not in_bbox(10, 40, (0, 0, 30, 30))
    assert in_bbox(10, 179, (170, 0, -170, 30)) and in_bbox(10, -175, (170, 0, -170, 30))
    assert not in_bbox(10, 0, (170, 0, -170, 30))


def test_lost_aircraft_estimated_along_route_then_dropped(refdata):
    store = LiveStore(hide_military=True, route_lookup=lambda s: refdata.match_route(s.callsign, s.lat, s.lon, s.track))
    store.refresh_s = 60
    lat, lon = interpolate_gc(51.47, -0.46, 40.64, -73.78, 0.3)
    store.ingest([adsb_record("abc128", lat, lon, track=285.0, gs=480.0, seen_pos=0)], now=NOW)
    later = NOW + 3600
    est = store.get("abc128", now=later)
    assert est is not None and est.estimated
    assert est.lon < lon  # moved west along the route
    assert est.to_compact(later)[9] & FLAG_ESTIMATED
    assert store.get("abc128", now=NOW + 12 * 3600) is None  # would have landed long ago


def test_lost_low_aircraft_without_route_dropped():
    store = LiveStore(hide_military=True)
    store.refresh_s = 60
    store.ingest([adsb_record("abc129", 51.0, -1.0, alt=3000, flight="N123AB")], now=NOW)
    assert store.get("abc129", now=NOW + 300) is not None
    assert store.get("abc129", now=NOW + 3600) is None


def test_old_positions_never_shown_as_current():
    """A slow-to-refresh source must not keep a frozen position on screen for long."""
    store = LiveStore(hide_military=True)
    store.ingest([adsb_record("abc140", 51.0, -1.0, alt=36000, flight="N123AB")], now=NOW, refresh_s=40 * 60)
    assert store.get("abc140", now=NOW + 10 * 60) is not None
    assert store.get("abc140", now=NOW + 16 * 60) is None  # no route to estimate along: hidden


def test_ground_flag_in_compact_format():
    store = LiveStore(hide_military=True)
    store.ingest([adsb_record("abc130", 51.47, -0.46, alt="ground")], now=NOW)
    assert store.snapshot(now=NOW)[0].to_compact(NOW)[9] & FLAG_GROUND


def test_search_by_callsign_registration_hex():
    store = LiveStore(hide_military=True)
    store.ingest([adsb_record("abc131", 51.0, -1.0)], now=NOW)
    assert [s.hex for s in store.search("BAW117")] == ["abc131"]
    assert [s.hex for s in store.search("gxlea")] == ["abc131"]
    assert [s.hex for s in store.search("ABC131")] == ["abc131"]
    assert [s.hex for s in store.search("BAW1")] == ["abc131"]  # prefix
    assert store.search("Z") == []


# --- Recorder ---


def _feed(recorder, store, positions, hex_code="def001", **extra):
    """positions: list of (t, lat, lon, alt)"""
    for t, lat, lon, alt in positions:
        store.ingest([adsb_record(hex_code, lat, lon, alt=alt, seen_pos=0, **extra)], now=t)


@pytest.fixture
def recording(tmp_path, refdata):
    flights = FlightStore(tmp_path / "flights.sqlite")
    recorder = Recorder(flights, refdata)
    live = LiveStore(hide_military=True)
    live.add_observer(recorder.observe)
    yield flights, recorder, live
    flights.close()


def _flight_positions(t0, n=40, dt=60, alt=35000):
    out = []
    for i in range(n):
        lat, lon = interpolate_gc(51.47, -0.46, 40.64, -73.78, 0.1 + i * 0.01)
        out.append((t0 + i * dt, lat, lon, alt))
    return out


def test_recorder_saves_flight_with_route(recording):
    flights, recorder, live = recording
    _feed(recorder, live, _flight_positions(NOW))
    recorder.flush_all()
    found = flights.search(callsigns=["BAW117"])
    assert len(found) == 1
    rec = flights.get(found[0].id)
    assert (rec.origin, rec.destination, rec.source) == ("EGLL", "KJFK", "live")
    assert len(rec.points) >= 2 and rec.end_ts > rec.start_ts


def test_recorder_splits_after_landing(recording):
    flights, recorder, live = recording
    first = _flight_positions(NOW, n=10)
    _feed(recorder, live, first)
    _feed(recorder, live, [(NOW + 700, 40.64, -73.78, "ground")])
    second = [(NOW + 5000 + i * 60, 40.64 + i * 0.05, -73.78, 3000 + i * 1000) for i in range(10)]
    _feed(recorder, live, second, flight="BAW118  ")
    recorder.flush_all()
    assert len(flights.search(hex_code="def001")) == 2


def test_recorder_keeps_ocean_gap_as_one_flight(recording):
    flights, recorder, live = recording
    a = _flight_positions(NOW, n=5)
    t_gap = a[-1][0] + 3 * 3600
    lat, lon = interpolate_gc(51.47, -0.46, 40.64, -73.78, 0.9)
    _feed(recorder, live, a + [(t_gap, lat, lon, 36000), (t_gap + 60, lat - 0.05, lon - 0.1, 36000)])
    recorder.flush_all()
    assert len(flights.search(hex_code="def001")) == 1


def test_recorder_splits_after_low_gap(recording):
    flights, recorder, live = recording
    _feed(recorder, live, [(NOW + i * 60, 51.0 + i * 0.02, -1.0, 2000 + i * 500) for i in range(6)])
    _feed(recorder, live, [(NOW + 7200 + i * 60, 51.0, -1.0 + i * 0.05, 3000) for i in range(6)])
    recorder.flush_all()
    assert len(flights.search(hex_code="def001")) == 2


def test_recorder_ignores_short_hops(recording):
    flights, recorder, live = recording
    _feed(recorder, live, [(NOW, 51.0, -1.0, 2000), (NOW + 30, 51.01, -1.0, 2100)])
    recorder.flush_all()
    assert flights.search(hex_code="def001") == []


def test_current_flight_for_replay(recording):
    _, recorder, live = recording
    _feed(recorder, live, _flight_positions(NOW, n=5))
    rec = recorder.current_flight("def001")
    assert rec is not None and len(rec.points) == 5 and rec.id is None
