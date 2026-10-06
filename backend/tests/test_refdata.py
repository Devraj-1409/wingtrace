import pytest

from app.geo import destination_point, initial_bearing_deg, interpolate_gc


def test_build_counts(refdata):
    meta = refdata.meta()
    assert meta["routes"] == "3"
    assert int(meta["airports"]) >= 5


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("BAW117", "BAW117"),
        ("BA117", "BAW117"),  # IATA code swapped for ICAO (the airline with routes wins the shared code)
        ("BAW0117", "BAW117"),  # leading zeros dropped
        (" baw117 ", "BAW117"),
        ("AI101", "AIC101"),
        ("BAW12AB", "BAW12AB"),
        ("N123AB", "N123AB"),  # looks like a flight number by VRS's rules; simply has no route
        ("BAW12345", None),  # number too long
        ("", None),
        (None, None),
    ],
)
def test_normalize_callsign(refdata, raw, expected):
    assert refdata.normalize_callsign(raw) == expected


def test_airports_by_icao_and_iata(refdata):
    lhr = refdata.airport("LHR")
    assert lhr is not None and lhr.icao == "EGLL" and lhr.kind == "large_airport"
    assert refdata.airport("egll") == lhr
    assert refdata.airport("XXXX") is None


def test_ourairports_adds_missing_airports(refdata):
    assert refdata.airport("EGKK").name == "London Gatwick Airport"


def test_search_airports_prefers_names_starting_with_query(refdata):
    names = [a.icao for a in refdata.search_airports("london")]
    assert names[0] in ("EGLL", "EGKK")
    assert names.index("FAEL") > names.index("EGLL")


def test_model_names(refdata):
    assert refdata.model_name("A388") == "Airbus A380-800"  # curated name
    assert refdata.model_name("ZZZ1") == "Acme Rocket"  # most common maker, shortest model
    assert refdata.model_name("NOPE") is None


def test_airline_for_callsign(refdata):
    assert refdata.airline_for_callsign("BA117")["name"] == "British Airways"
    assert refdata.airline_for_callsign("N123AB") is None


def test_route_plausible_when_on_the_leg(refdata):
    lat, lon = interpolate_gc(51.47, -0.46, 40.64, -73.78, 0.4)
    track = initial_bearing_deg(lat, lon, 40.64, -73.78)
    match = refdata.match_route("BAW117", lat, lon, track)
    assert match.plausible
    assert (match.origin.icao, match.destination.icao) == ("EGLL", "KJFK")


def test_route_implausible_far_away_or_flying_backwards(refdata):
    assert not refdata.match_route("BAW117", 35.0, 139.0, 90.0).plausible  # over Japan
    lat, lon = interpolate_gc(51.47, -0.46, 40.64, -73.78, 0.5)
    backwards = initial_bearing_deg(lat, lon, 51.47, -0.46)
    assert not refdata.match_route("BAW117", lat, lon, backwards).plausible


def test_long_haul_detour_around_closed_airspace_is_accepted(refdata):
    # Delhi → New York, flying ~1,200 km south of the great circle (e.g. avoiding closed airspace).
    lat, lon = interpolate_gc(28.57, 77.10, 40.64, -73.78, 0.3)
    lat, lon = destination_point(lat, lon, 180, 1_200_000)
    track = initial_bearing_deg(lat, lon, 40.64, -73.78)
    match = refdata.match_route("AIC101", lat, lon, track)
    assert match.plausible
    # …but not when flying away from the destination.
    assert not refdata.match_route("AIC101", lat, lon, (track + 180) % 360).plausible


def test_airport_time_zones(refdata):
    assert refdata.airport("DEL").tz == "Asia/Kolkata"
    assert refdata.airport("JFK").tz is None  # not in the time-zone list given to this build


def test_nearest_airport(refdata):
    assert refdata.nearest_airport(51.4775, -0.4614).icao == "EGLL"
    assert refdata.nearest_airport(48.0, -30.0) is None  # mid-Atlantic


def test_multi_leg_route_picks_current_leg(refdata):
    lat, lon = interpolate_gc(1.35, 103.99, -33.95, 151.18, 0.5)  # Singapore → Sydney
    track = initial_bearing_deg(lat, lon, -33.95, 151.18)
    match = refdata.match_route("BAW15", lat, lon, track)
    assert match.plausible
    assert (match.origin.icao, match.destination.icao) == ("WSSS", "YSSY")
    assert [a.icao for a in match.airports] == ["EGLL", "WSSS", "YSSY"]


def test_route_near_origin_on_ground(refdata):
    lat, lon = destination_point(51.4706, -0.4619, 90, 1500)
    assert refdata.match_route("BAW117", lat, lon, None).plausible


def test_unknown_callsign(refdata):
    assert refdata.match_route("BAW9999", 51.0, 0.0) is None
