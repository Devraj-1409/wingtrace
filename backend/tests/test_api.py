import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.feeds import ADSB_FI, ADSB_LOL, FeedClient
from app.geo import initial_bearing_deg, interpolate_gc
from app.main import create_app
from app.refdata_build import build
from app.store import FlightRecord
from tests.conftest import OURAIRPORTS_CSV, adsb_record, make_standing_data_zip


def _settings(tmp_path, **overrides) -> Settings:
    values = dict(
        data_dir=tmp_path,
        user_agent="test",
        adsblol_min_interval=0.001,
        adsblol_start_interval=0.001,
        adsbfi_enabled=True,
        adsbfi_min_interval=0.001,
        adsbfi_start_interval=0.001,
        global_sweep=False,
        hide_military=True,
        retention_days=14,
        auto_import=False,
        client_requests_per_minute=1000,
        frontend_dist=None,
        cors_origins=(),
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def upstream_calls():
    return []


@pytest.fixture
def client(tmp_path, upstream_calls):
    build(tmp_path / "reference.sqlite", make_standing_data_zip(), OURAIRPORTS_CSV)

    def handler(request: httpx.Request):
        upstream_calls.append(str(request.url))
        if "/hex/aaaaaa" in request.url.path:
            return httpx.Response(200, json={"ac": [adsb_record("aaaaaa", 30.0, 40.0, flight="AIC101  ")]})
        return httpx.Response(200, json={"ac": []})

    transport = httpx.MockTransport(handler)
    app = create_app(
        _settings(tmp_path),
        sweep_feed=FeedClient(ADSB_LOL, "test", min_interval=0.001, start_interval=0.001, transport=transport),
        detail_feed=FeedClient(ADSB_FI, "test", min_interval=0.001, start_interval=0.001, transport=transport),
        background=False,
    )
    with TestClient(app) as c:
        yield c


def _ingest(client, records):
    client.app.state.services.live.ingest(records)


def _on_route(f=0.4):
    lat, lon = interpolate_gc(51.47, -0.46, 40.64, -73.78, f)
    return lat, lon, initial_bearing_deg(lat, lon, 40.64, -73.78)


def test_health(client):
    body = client.get("/api/health").json()
    assert body["ok"] and body["referenceData"]
    assert set(body["upstream"]) == {"adsb.lol", "adsb.fi"}


def test_live_compact_format_and_bbox(client):
    lat, lon, trk = _on_route()
    _ingest(client, [adsb_record("abc001", lat, lon, track=trk), adsb_record("abc002", -33.9, 151.2)])
    body = client.get("/api/live").json()
    assert {a[0] for a in body["ac"]} == {"abc001", "abc002"}
    row = next(a for a in body["ac"] if a[0] == "abc001")
    assert row[3] == 35000 and row[7] == "BAW117" and row[8] == "A388" and len(row) == 12
    box = f"{lon - 2},{lat - 2},{lon + 2},{lat + 2}"
    body = client.get(f"/api/live?bbox={box}").json()
    assert [a[0] for a in body["ac"]] == ["abc001"]
    assert body["regional"] is True
    assert client.get("/api/live?bbox=1,2,3").status_code == 400


def test_aircraft_details_with_route(client):
    lat, lon, trk = _on_route()
    _ingest(client, [adsb_record("abc001", lat, lon, track=trk)])
    body = client.get("/api/aircraft/abc001").json()
    assert body["aircraft"]["callsign"] == "BAW117"
    assert body["airline"]["name"] == "British Airways"
    assert body["model"] == "Airbus A380-800"
    assert body["route"]["plausible"] is True
    assert body["route"]["origin"]["iata"] == "LHR" and body["route"]["destination"]["iata"] == "JFK"
    assert len(body["track"]) == 1


def _fly(client, hex_code, points, callsign="BAW117  "):
    """Feed positions (lat, lon, alt) one minute apart, ending now."""
    now = time.time()
    live = client.app.state.services.live
    for i, (lat, lon, alt) in enumerate(points):
        trk = initial_bearing_deg(lat, lon, 40.64, -73.78)
        live.ingest([adsb_record(hex_code, lat, lon, alt=alt, track=trk, flight=callsign, seen_pos=0)],
                    now=now - (len(points) - i) * 60)


def test_route_confirmed_by_observed_take_off(client):
    lat, lon, _ = _on_route(0.3)
    _fly(client, "abc010", [(51.4706, -0.4619, "ground"), (51.47, -0.55, 2000), (lat, lon, 35000)])
    body = client.get("/api/aircraft/abc010").json()
    assert body["tookOffFrom"]["icao"] == "EGLL"
    assert body["route"]["plausible"] and body["route"]["confidence"] == "confirmed"
    assert body["flightNumber"] == "BA117" and body["departedAt"] is not None


def test_route_contradicted_by_observed_take_off_is_hidden(client):
    lat, lon, _ = _on_route(0.3)
    # Took off from Delhi, yet the schedule data for this callsign says London → New York.
    _fly(client, "abc011", [(28.5665, 77.1031, "ground"), (lat, lon, 35000)])
    body = client.get("/api/aircraft/abc011").json()
    assert body["tookOffFrom"]["icao"] == "VIDP"
    assert body["route"]["plausible"] is False and "DEL" in body["route"]["conflict"]
    assert client.get("/api/search?q=BAW117").json()["aircraft"][0]["route"] is None


def test_route_without_observed_take_off_is_from_schedule(client):
    lat, lon, trk = _on_route()
    _ingest(client, [adsb_record("abc012", lat, lon, track=trk)])
    assert client.get("/api/aircraft/abc012").json()["route"]["confidence"] == "schedule"


def test_aircraft_unknown_is_looked_up_upstream(client, upstream_calls):
    body = client.get("/api/aircraft/aaaaaa").json()
    assert body["aircraft"]["callsign"] == "AIC101"
    assert any("opendata.adsb.fi" in u and "/hex/aaaaaa" in u for u in upstream_calls)
    assert client.get("/api/aircraft/bbbbbb").status_code == 404
    assert client.get("/api/aircraft/not-a-hex").status_code == 400


def test_hidden_aircraft_never_served(client):
    _ingest(client, [adsb_record("abc009", 51.0, 0.0, dbFlags=8)])
    assert all(a[0] != "abc009" for a in client.get("/api/live").json()["ac"])


def test_search(client):
    lat, lon, trk = _on_route()
    _ingest(client, [adsb_record("abc001", lat, lon, track=trk)])
    body = client.get("/api/search?q=BA117").json()
    assert [a["hex"] for a in body["aircraft"]] == ["abc001"]
    assert body["aircraft"][0]["route"] == ["LHR", "JFK"]
    assert body["airports"] == [] or isinstance(body["airports"], list)
    assert client.get("/api/search?q=LHR").json()["airports"][0]["icao"] == "EGLL"


def test_flights_search_and_replay(client):
    store = client.app.state.services.store
    t0 = time.time() - 7200
    points = [(t0 + i * 60, 51.0 + i * 0.1, -1.0 - i * 0.3, 35000, 480.0, 280.0) for i in range(30)]
    store.save_archive_batch([FlightRecord(
        hex="abc001", callsign="BAW117", registration="G-XLEA", type_code="A388", origin="EGLL", destination="KJFK",
        start_ts=points[0][0], end_ts=points[-1][0], source="archive", points=points, callsign_key="BAW117",
    )])
    day = time.strftime("%Y-%m-%d", time.gmtime(t0))
    for query in ("q=BA117", "q=G-XLEA", "q=gxlea", "q=abc001", "airport=LHR", "airport=KJFK"):
        body = client.get(f"/api/flights?{query}").json()
        assert len(body["flights"]) == 1, query
    assert client.get(f"/api/flights?q=BAW117&date={day}").json()["flights"]
    assert client.get("/api/flights?q=BAW117&date=2001-01-01").json()["flights"] == []
    assert client.get("/api/flights").status_code == 400
    flight_id = client.get("/api/flights?q=BAW117").json()["flights"][0]["id"]
    detail = client.get(f"/api/flights/{flight_id}").json()
    assert len(detail["track"]) == 30 and detail["origin"]["iata"] == "LHR"
    assert client.get("/api/flights/999999").status_code == 404


def test_live_flight_replay(client):
    lat, lon, trk = _on_route()
    services = client.app.state.services
    now = time.time()
    for i in range(3):
        services.live.ingest([adsb_record("abc001", lat, lon - i * 0.1, track=trk, seen_pos=0)], now=now + i * 30)
    body = client.get("/api/flights/live/abc001").json()
    assert len(body["track"]) == 3 and body["flight"]["id"] is None
    assert client.get("/api/flights/live/ffffff").status_code == 404


def test_top_lists(client):
    lhr_jfk = _on_route(0.4)
    del_jfk_lat, del_jfk_lon = interpolate_gc(28.57, 77.10, 40.64, -73.78, 0.4)
    del_jfk_trk = initial_bearing_deg(del_jfk_lat, del_jfk_lon, 40.64, -73.78)
    _ingest(client, [
        adsb_record("abc020", lhr_jfk[0], lhr_jfk[1], track=lhr_jfk[2], gs=520.0, alt=39000),
        adsb_record("abc021", del_jfk_lat, del_jfk_lon, track=del_jfk_trk, flight="AIC101  ", gs=480.0, alt=41000),
        adsb_record("abc022", 51.0, 0.0, flight="N123AB", gs=1900.0, alt=35000),  # speed glitch
        adsb_record("abc023", 51.47, -0.46, alt="ground"),
    ])
    body = client.get("/api/top").json()
    assert [e["callsign"] for e in body["longest"]] == ["AIC101", "BAW117"]  # Delhi–New York is longer
    assert [e["callsign"] for e in body["shortest"]] == ["BAW117", "AIC101"]
    assert body["longest"][0]["route"]["origin"]["iata"] == "DEL" and body["longest"][0]["route"]["km"] > 11_000
    assert [e["hex"] for e in body["fastest"]] == ["abc020", "abc021"]  # the 1,900 kt glitch is ignored
    assert body["highest"][0]["hex"] == "abc021"
    assert all(e["hex"] != "abc023" for lst in ("longest", "fastest", "highest") for e in body[lst])


def test_meta_lists_imported_days(client):
    client.app.state.services.store.mark_imported("2026-10-01", 5478, complete=False)
    body = client.get("/api/meta").json()
    assert body["historyDays"] == {"2026-10-01": False}
    assert body["retentionDays"] == 14


def test_recorder_resumes_flights_after_restart(tmp_path):
    build(tmp_path / "reference.sqlite", make_standing_data_zip(), OURAIRPORTS_CSV)
    settings = _settings(tmp_path)
    now = time.time()
    with TestClient(create_app(settings, background=False)) as c:
        live = c.app.state.services.live
        for i in range(5):
            live.ingest([adsb_record("abc050", 51.0 + i * 0.1, -1.0, seen_pos=0)], now=now - 300 + i * 60)
        assert len(c.get("/api/aircraft/abc050").json()["track"]) == 5
    # Shutdown flushed the flight; a new server picks it up again.
    with TestClient(create_app(settings, background=False)) as c:
        assert len(c.app.state.services.recorder.current_track("abc050")) >= 2


def test_rate_limit(tmp_path):
    app = create_app(_settings(tmp_path, client_requests_per_minute=3), background=False)
    with TestClient(app) as c:
        codes = [c.get("/api/health").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]
