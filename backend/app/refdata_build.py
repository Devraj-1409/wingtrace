"""Download reference data and build data/reference.sqlite.

Sources (both free to reuse, credited on the site's About page):
- VRS standing-data, CC0 1.0 - routes, airlines, aircraft models, airports
  https://github.com/vradarserver/standing-data
- OurAirports, public domain - airport types, extra airports
  https://ourairports.com/data/

Usage:  python -m app.refdata_build
"""

from __future__ import annotations

import csv
import io
import logging
import os
import sqlite3
import time
import zipfile
from collections import Counter
from pathlib import Path

import httpx

from .config import Settings

log = logging.getLogger("refdata_build")

STANDING_DATA_ZIP = "https://codeload.github.com/vradarserver/standing-data/zip/refs/heads/main"
OURAIRPORTS_CSV = "https://davidmegginson.github.io/ourairports-data/airports.csv"
# Airport time zones (MIT licence): https://github.com/mwgg/Airports
AIRPORT_TIMEZONES_JSON = "https://raw.githubusercontent.com/mwgg/Airports/master/airports.json"

# Display names for common types, where standing-data's rows don't give a clean one.
MODEL_NAMES = {
    "A19N": "Airbus A319neo", "A20N": "Airbus A320neo", "A21N": "Airbus A321neo",
    "A318": "Airbus A318", "A319": "Airbus A319", "A320": "Airbus A320", "A321": "Airbus A321",
    "A306": "Airbus A300-600", "A310": "Airbus A310",
    "A332": "Airbus A330-200", "A333": "Airbus A330-300", "A338": "Airbus A330-800neo",
    "A339": "Airbus A330-900neo", "A343": "Airbus A340-300", "A346": "Airbus A340-600",
    "A359": "Airbus A350-900", "A35K": "Airbus A350-1000", "A388": "Airbus A380-800",
    "BCS1": "Airbus A220-100", "BCS3": "Airbus A220-300", "A3ST": "Airbus Beluga",
    "A400": "Airbus A400M Atlas",
    "B712": "Boeing 717", "B733": "Boeing 737-300", "B734": "Boeing 737-400",
    "B735": "Boeing 737-500", "B736": "Boeing 737-600", "B737": "Boeing 737-700",
    "B738": "Boeing 737-800", "B739": "Boeing 737-900", "B37M": "Boeing 737 MAX 7",
    "B38M": "Boeing 737 MAX 8", "B39M": "Boeing 737 MAX 9", "B3XM": "Boeing 737 MAX 10",
    "B744": "Boeing 747-400", "B748": "Boeing 747-8", "B752": "Boeing 757-200",
    "B753": "Boeing 757-300", "B762": "Boeing 767-200", "B763": "Boeing 767-300",
    "B764": "Boeing 767-400", "B772": "Boeing 777-200", "B77L": "Boeing 777-200LR",
    "B773": "Boeing 777-300", "B77W": "Boeing 777-300ER", "B778": "Boeing 777-8",
    "B779": "Boeing 777-9", "B788": "Boeing 787-8", "B789": "Boeing 787-9", "B78X": "Boeing 787-10",
    "E170": "Embraer 170", "E75L": "Embraer 175", "E75S": "Embraer 175", "E190": "Embraer 190",
    "E195": "Embraer 195", "E290": "Embraer E190-E2", "E295": "Embraer E195-E2",
    "E135": "Embraer ERJ-135", "E145": "Embraer ERJ-145", "E35L": "Embraer Legacy 600",
    "E50P": "Embraer Phenom 100", "E55P": "Embraer Phenom 300",
    "CRJ2": "Bombardier CRJ200", "CRJ7": "Bombardier CRJ700", "CRJ9": "Bombardier CRJ900",
    "CRJX": "Bombardier CRJ1000", "CL30": "Bombardier Challenger 300",
    "CL35": "Bombardier Challenger 350", "CL60": "Bombardier Challenger 600",
    "GLEX": "Bombardier Global Express", "GL5T": "Bombardier Global 5000",
    "GL7T": "Bombardier Global 7500",
    "DH8A": "De Havilland Dash 8-100", "DH8B": "De Havilland Dash 8-200",
    "DH8C": "De Havilland Dash 8-300", "DH8D": "De Havilland Dash 8-400",
    "AT43": "ATR 42-300", "AT45": "ATR 42-500", "AT46": "ATR 42-600",
    "AT72": "ATR 72", "AT75": "ATR 72-500", "AT76": "ATR 72-600",
    "SU95": "Sukhoi Superjet 100", "C919": "COMAC C919", "MD11": "McDonnell Douglas MD-11",
    "B190": "Beechcraft 1900", "BE20": "Beechcraft King Air 200", "B350": "Beechcraft King Air 350",
    "BE9L": "Beechcraft King Air 90", "PC12": "Pilatus PC-12", "PC24": "Pilatus PC-24",
    "C208": "Cessna 208 Caravan", "C150": "Cessna 150", "C152": "Cessna 152",
    "C172": "Cessna 172 Skyhawk", "C182": "Cessna 182 Skylane",
    "C510": "Cessna Citation Mustang", "C525": "Cessna CitationJet", "C25A": "Cessna Citation CJ2",
    "C25B": "Cessna Citation CJ3", "C25C": "Cessna Citation CJ4", "C560": "Cessna Citation V",
    "C56X": "Cessna Citation Excel", "C680": "Cessna Citation Sovereign",
    "C68A": "Cessna Citation Latitude", "C700": "Cessna Citation Longitude",
    "C750": "Cessna Citation X",
    "SR20": "Cirrus SR20", "SR22": "Cirrus SR22", "SF50": "Cirrus Vision Jet",
    "P28A": "Piper PA-28 Cherokee", "DA40": "Diamond DA40", "DA42": "Diamond DA42",
    "GLF4": "Gulfstream IV", "GLF5": "Gulfstream V", "GLF6": "Gulfstream G650",
    "G280": "Gulfstream G280",
    "F2TH": "Dassault Falcon 2000", "F900": "Dassault Falcon 900", "FA7X": "Dassault Falcon 7X",
    "FA8X": "Dassault Falcon 8X", "H25B": "Hawker 800",
    "LJ35": "Learjet 35", "LJ45": "Learjet 45", "LJ60": "Learjet 60", "LJ75": "Learjet 75",
    "EC35": "Airbus H135", "EC45": "Airbus H145", "AS50": "Airbus AS350 Ecureuil",
    "R22": "Robinson R22", "R44": "Robinson R44", "B06": "Bell 206 JetRanger", "B407": "Bell 407",
    "S76": "Sikorsky S-76", "A139": "AgustaWestland AW139", "A109": "AgustaWestland AW109",
    "C130": "Lockheed C-130 Hercules", "C17": "Boeing C-17 Globemaster III",
    "A124": "Antonov An-124 Ruslan",
}

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE airlines (icao TEXT PRIMARY KEY, iata TEXT, name TEXT NOT NULL);
CREATE TABLE airline_iata (iata TEXT PRIMARY KEY, icao TEXT NOT NULL);
CREATE TABLE models (icao TEXT PRIMARY KEY, manufacturer TEXT, model TEXT);
CREATE TABLE airports (
    icao TEXT PRIMARY KEY, iata TEXT, name TEXT NOT NULL, city TEXT, country TEXT,
    lat REAL NOT NULL, lon REAL NOT NULL, elevation_ft INTEGER, kind TEXT, tz TEXT
);
CREATE INDEX airports_iata ON airports (iata);
CREATE INDEX airports_lat ON airports (lat);
CREATE TABLE routes (callsign TEXT PRIMARY KEY, airports TEXT NOT NULL) WITHOUT ROWID;
"""


def _csv_rows(z: zipfile.ZipFile, name: str):
    with z.open(name) as fh:
        yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig"))


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except ValueError:
        return None


def build(
    out_path: Path, standing_data_zip: bytes, ourairports_csv: str, timezones: dict[str, str] | None = None
) -> dict[str, int]:
    timezones = timezones or {}
    z = zipfile.ZipFile(io.BytesIO(standing_data_zip))
    names = z.namelist()

    def files(folder: str) -> list[str]:
        return sorted(n for n in names if f"/{folder}/schema-01/" in n and n.endswith(".csv"))

    if out_path.exists():
        out_path.unlink()
    db = sqlite3.connect(out_path)
    db.executescript(_SCHEMA)

    # Routes first: route counts per airline decide which airline owns a shared IATA code.
    routes: list[tuple[str, str]] = []
    route_count: Counter[str] = Counter()
    for name in files("routes"):
        for row in _csv_rows(z, name):
            callsign, airports = row.get("Callsign"), row.get("AirportCodes")
            if callsign and airports:
                routes.append((callsign.upper(), airports.upper()))
                route_count[(row.get("AirlineCode") or callsign[:3]).upper()] += 1
    db.executemany("INSERT OR REPLACE INTO routes VALUES (?, ?)", routes)

    airlines: dict[str, tuple[str, str | None, str]] = {}
    iata_owner: dict[str, str] = {}
    for name in files("airlines"):
        for row in _csv_rows(z, name):
            icao = (row.get("ICAO") or "").upper()
            iata = (row.get("IATA") or "").upper() or None
            if not icao or not row.get("Name"):
                continue
            airlines[icao] = (icao, iata, row["Name"])
            if iata and route_count[icao] >= route_count[iata_owner.get(iata, "")]:
                iata_owner[iata] = icao
    db.executemany("INSERT INTO airlines VALUES (?, ?, ?)", airlines.values())
    db.executemany("INSERT INTO airline_iata VALUES (?, ?)", iata_owner.items())

    # A type code has many rows (licence builds, military names, variants).
    # Use the most common manufacturer and its shortest model name, unless
    # the type is in MODEL_NAMES.
    model_rows: dict[str, list[tuple[str, str]]] = {}
    for name in files("model-type"):
        for row in _csv_rows(z, name):
            icao = (row.get("ICAO") or "").upper()
            if icao and row.get("IsActive") == "1":
                model_rows.setdefault(icao, []).append((row.get("Manufacturer") or "", row.get("Model") or ""))
    models = []
    for icao, rows in model_rows.items():
        if icao in MODEL_NAMES:
            models.append((icao, "", MODEL_NAMES[icao]))
            continue
        maker = Counter(m for m, _ in rows).most_common(1)[0][0]
        model = min((mdl for m, mdl in rows if m == maker), key=len)
        models.append((icao, maker, model))
    db.executemany("INSERT INTO models VALUES (?, ?, ?)", models)

    # OurAirports: airport types for ranking, plus airports standing-data lacks.
    oa_kind: dict[str, str] = {}
    oa_extra: dict[str, tuple] = {}
    for row in csv.DictReader(io.StringIO(ourairports_csv)):
        kind = row.get("type") or ""
        if kind in ("closed", "balloonport"):
            continue
        codes = {c.upper() for c in (row.get("icao_code"), row.get("ident"), row.get("gps_code")) if c}
        for code in codes:
            oa_kind.setdefault(code, kind)
        icao = (row.get("icao_code") or row.get("ident") or "").upper()
        if len(icao) == 4 and icao.isalpha() and kind in ("large_airport", "medium_airport", "small_airport"):
            oa_extra[icao] = (
                icao,
                (row.get("iata_code") or "").upper() or None,
                row.get("name") or icao,
                row.get("municipality") or None,
                row.get("iso_country") or None,
                float(row["latitude_deg"]),
                float(row["longitude_deg"]),
                _int_or_none(row.get("elevation_ft")),
                kind,
                timezones.get(icao),
            )

    airports: dict[str, tuple] = {}
    for name in files("airports"):
        for row in _csv_rows(z, name):
            icao = (row.get("ICAO") or row.get("Code") or "").upper()
            if not icao or not row.get("Latitude") or not row.get("Longitude"):
                continue
            airports[icao] = (
                icao,
                (row.get("IATA") or "").upper() or None,
                row.get("Name") or icao,
                row.get("Location") or None,
                row.get("CountryISO2") or None,
                float(row["Latitude"]),
                float(row["Longitude"]),
                _int_or_none(row.get("AltitudeFeet")),
                oa_kind.get(icao),
                timezones.get(icao),
            )
    for icao, extra in oa_extra.items():
        airports.setdefault(icao, extra)
    db.executemany("INSERT INTO airports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", airports.values())

    counts = {"routes": len(routes), "airlines": len(airlines), "models": len(models), "airports": len(airports)}
    db.executemany(
        "INSERT INTO meta VALUES (?, ?)",
        [("built_at", str(int(time.time()))), *((k, str(v)) for k, v in counts.items())],
    )
    db.commit()
    db.execute("VACUUM")
    db.close()
    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": settings.user_agent}
    with httpx.Client(timeout=180, follow_redirects=True, headers=headers) as http:
        log.info("Downloading VRS standing-data …")
        sd = http.get(STANDING_DATA_ZIP)
        sd.raise_for_status()
        log.info("Downloading OurAirports …")
        oa = http.get(OURAIRPORTS_CSV)
        oa.raise_for_status()
        log.info("Downloading airport time zones …")
        tz = http.get(AIRPORT_TIMEZONES_JSON)
        tz.raise_for_status()
        timezones = {code.upper(): a["tz"] for code, a in tz.json().items() if a.get("tz")}
    tmp = settings.reference_db.with_suffix(".tmp")
    counts = build(tmp, sd.content, oa.text, timezones)
    os.replace(tmp, settings.reference_db)
    log.info("Wrote %s: %s", settings.reference_db, counts)


if __name__ == "__main__":
    main()
