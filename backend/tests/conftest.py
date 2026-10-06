import csv
import io
import zipfile
from pathlib import Path

import pytest

from app.refdata import RefData
from app.refdata_build import build


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _csv(rows: list[list[str]]) -> str:
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows(rows)
    return out.getvalue()


def make_standing_data_zip() -> bytes:
    """A tiny stand-in for the VRS standing-data repository."""
    files = {
        "standing-data-main/routes/schema-01/B/BAW-all.csv": _csv([
            ["Callsign", "Code", "Number", "AirlineCode", "AirportCodes"],
            ["BAW117", "BAW", "117", "BAW", "EGLL-KJFK"],
            ["BAW15", "BAW", "15", "BAW", "EGLL-WSSS-YSSY"],
        ]),
        "standing-data-main/routes/schema-01/A/AIC-all.csv": _csv([
            ["Callsign", "Code", "Number", "AirlineCode", "AirportCodes"],
            ["AIC101", "AIC", "101", "AIC", "VIDP-KJFK"],
        ]),
        "standing-data-main/airlines/schema-01/airlines.csv": _csv([
            ["Code", "Name", "ICAO", "IATA", "PositioningFlightPattern", "CharterFlightPattern"],
            ["BAW", "British Airways", "BAW", "BA", "", ""],
            ["AIC", "Air India", "AIC", "AI", "", ""],
            ["XBA", "Defunct Airline", "XBA", "BA", "", ""],
        ]),
        "standing-data-main/model-type/schema-01/A.csv": _csv([
            ["ICAO", "Manufacturer", "Model", "Engines", "EngineTypeCode", "EnginePlacementCode", "SpeciesCode", "WakeTurbulenceCode", "IsActive"],
            ["A388", "Airbus", "A-380-800", "4", "J", "", "L", "H", "1"],
            ["ZZZ1", "Acme", "Rocket 2", "1", "J", "", "L", "M", "1"],
            ["ZZZ1", "Acme", "Rocket", "1", "J", "", "L", "M", "1"],
            ["ZZZ1", "Other", "R", "1", "J", "", "L", "M", "1"],
        ]),
        "standing-data-main/airports/schema-01/E/EG.csv": _csv([
            ["Code", "Name", "ICAO", "IATA", "Location", "CountryISO2", "Latitude", "Longitude", "AltitudeFeet"],
            ["EGLL", "London Heathrow Airport", "EGLL", "LHR", "London", "GB", "51.4706", "-0.461941", "83"],
            ["KJFK", "John F Kennedy International Airport", "KJFK", "JFK", "New York", "US", "40.639801", "-73.7789", "13"],
            ["WSSS", "Singapore Changi Airport", "WSSS", "SIN", "Singapore", "SG", "1.35019", "103.994003", "22"],
            ["YSSY", "Sydney Kingsford Smith International Airport", "YSSY", "SYD", "Sydney", "AU", "-33.9461", "151.177002", "21"],
            ["VIDP", "Indira Gandhi International Airport", "VIDP", "DEL", "New Delhi", "IN", "28.5665", "77.103104", "777"],
        ]),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


OURAIRPORTS_CSV = _csv([
    ["id", "ident", "type", "name", "latitude_deg", "longitude_deg", "elevation_ft", "continent", "iso_country",
     "iso_region", "municipality", "scheduled_service", "icao_code", "iata_code", "gps_code", "local_code",
     "home_link", "wikipedia_link", "keywords"],
    ["1", "EGLL", "large_airport", "London Heathrow Airport", "51.4706", "-0.461941", "83", "EU", "GB", "GB-ENG",
     "London", "yes", "EGLL", "LHR", "EGLL", "", "", "", ""],
    ["2", "EGKK", "large_airport", "London Gatwick Airport", "51.148102", "-0.190278", "202", "EU", "GB", "GB-ENG",
     "London", "yes", "EGKK", "LGW", "EGKK", "", "", "", ""],
    ["3", "FAEL", "medium_airport", "Ben Schoeman Airport", "-33.0356", "27.8259", "435", "AF", "ZA", "ZA-EC",
     "East London", "yes", "FAEL", "ELS", "FAEL", "", "", "", ""],
    ["4", "KJFK", "large_airport", "John F Kennedy International Airport", "40.639801", "-73.7789", "13", "NA",
     "US", "US-NY", "New York", "yes", "KJFK", "JFK", "KJFK", "", "", "", ""],
])


@pytest.fixture
def refdata(tmp_path: Path) -> RefData:
    path = tmp_path / "reference.sqlite"
    build(path, make_standing_data_zip(), OURAIRPORTS_CSV, {"EGLL": "Europe/London", "VIDP": "Asia/Kolkata"})
    ref = RefData(path)
    yield ref
    ref.close()


def adsb_record(hex_code: str, lat: float, lon: float, alt=35000, **extra) -> dict:
    """An aircraft record as adsb.lol / adsb.fi return it."""
    rec = {
        "hex": hex_code, "type": "adsb_icao", "flight": extra.pop("flight", "BAW117  "), "r": "G-XLEA", "t": "A388",
        "alt_baro": alt, "gs": 480.0, "track": 280.0, "lat": lat, "lon": lon, "seen_pos": 1.0, "seen": 0.5,
        "category": "A5", "squawk": "1234", "emergency": "none",
    }
    rec.update(extra)
    return rec
