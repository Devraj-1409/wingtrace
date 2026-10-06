"""SQLite storage of recorded flights, for replay."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .tracks import TrackPoint, decode_track, encode_track

_SCHEMA = """
CREATE TABLE IF NOT EXISTS flights (
    id INTEGER PRIMARY KEY,
    hex TEXT NOT NULL,
    callsign TEXT,
    callsign_key TEXT,
    registration TEXT,
    registration_key TEXT,
    type_code TEXT,
    origin TEXT,
    destination TEXT,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    source TEXT NOT NULL,
    n_points INTEGER NOT NULL,
    track BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS flights_callsign ON flights (callsign_key, start_ts);
CREATE INDEX IF NOT EXISTS flights_registration ON flights (registration_key, start_ts);
CREATE INDEX IF NOT EXISTS flights_hex ON flights (hex, start_ts);
CREATE INDEX IF NOT EXISTS flights_origin ON flights (origin, start_ts);
CREATE INDEX IF NOT EXISTS flights_destination ON flights (destination, start_ts);
CREATE INDEX IF NOT EXISTS flights_start ON flights (start_ts);
CREATE TABLE IF NOT EXISTS imports (
    day TEXT PRIMARY KEY,
    finished_at INTEGER NOT NULL,
    flights INTEGER NOT NULL,
    complete INTEGER NOT NULL
);
"""

_SUMMARY_COLUMNS = "id, hex, callsign, registration, type_code, origin, destination, start_ts, end_ts, source, n_points"


def registration_key(registration: str | None) -> str | None:
    return registration.upper().replace("-", "").replace(" ", "") if registration else None


@dataclass
class FlightRecord:
    hex: str
    callsign: str | None
    registration: str | None
    type_code: str | None
    origin: str | None
    destination: str | None
    start_ts: float
    end_ts: float
    source: str  # "live" (our recorder) or "archive" (adsb.lol daily history)
    points: list[TrackPoint] = field(default_factory=list)
    id: int | None = None
    callsign_key: str | None = None  # normalised callsign used for search
    n_points: int = 0

    def summary_json(self) -> dict:
        return {
            "id": self.id,
            "hex": self.hex,
            "callsign": self.callsign,
            "registration": self.registration,
            "type": self.type_code,
            "origin": self.origin,
            "destination": self.destination,
            "start": int(self.start_ts),
            "end": int(self.end_ts),
            "source": self.source,
            "points": self.n_points or len(self.points),
        }


class FlightStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=60)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _row_values(rec: FlightRecord) -> tuple:
        return (
            rec.hex,
            rec.callsign,
            rec.callsign_key or (rec.callsign.upper() if rec.callsign else None),
            rec.registration,
            registration_key(rec.registration),
            rec.type_code,
            rec.origin,
            rec.destination,
            int(rec.start_ts),
            int(rec.end_ts),
            rec.source,
            len(rec.points),
            encode_track(rec.points),
        )

    def save(self, rec: FlightRecord) -> int:
        """Insert a flight, or update it in place if it already has an id."""
        values = self._row_values(rec)
        with self._lock, self._conn:
            if rec.id is None:
                cur = self._conn.execute(
                    "INSERT INTO flights (hex, callsign, callsign_key, registration, registration_key, type_code,"
                    " origin, destination, start_ts, end_ts, source, n_points, track)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
                rec.id = cur.lastrowid
            else:
                self._conn.execute(
                    "UPDATE flights SET hex=?, callsign=?, callsign_key=?, registration=?, registration_key=?,"
                    " type_code=?, origin=?, destination=?, start_ts=?, end_ts=?, source=?, n_points=?, track=?"
                    " WHERE id=?",
                    (*values, rec.id),
                )
        return rec.id

    def save_archive_batch(self, records: Iterable[FlightRecord]) -> int:
        """Store imported flights, replacing our own coarser live recordings of the same flights."""
        records = list(records)
        with self._lock, self._conn:
            for rec in records:
                self._conn.execute(
                    "DELETE FROM flights WHERE hex = ? AND source = 'live' AND start_ts <= ? AND end_ts >= ?",
                    (rec.hex, int(rec.end_ts), int(rec.start_ts)),
                )
                if rec.id is not None:  # a flight from the previous day, now extended
                    self._conn.execute(
                        "UPDATE flights SET hex=?, callsign=?, callsign_key=?, registration=?, registration_key=?,"
                        " type_code=?, origin=?, destination=?, start_ts=?, end_ts=?, source=?, n_points=?, track=?"
                        " WHERE id=?",
                        (*self._row_values(rec), rec.id),
                    )
                    continue
                self._conn.execute(
                    "DELETE FROM flights WHERE hex = ? AND source = 'archive' AND start_ts = ?",
                    (rec.hex, int(rec.start_ts)),
                )
                cur = self._conn.execute(
                    "INSERT INTO flights (hex, callsign, callsign_key, registration, registration_key, type_code,"
                    " origin, destination, start_ts, end_ts, source, n_points, track)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    self._row_values(rec),
                )
                rec.id = cur.lastrowid
        return len(records)

    def find_continuation(self, hex_code: str, callsign_key: str | None, start_ts: float, max_gap_s: float) -> FlightRecord | None:
        """The stored archive flight that `start_ts` most plausibly continues (same aircraft and callsign)."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_SUMMARY_COLUMNS}, track FROM flights WHERE hex = ? AND source = 'archive'"
                " AND end_ts <= ? AND end_ts >= ? AND (callsign_key IS ? OR callsign_key = ?)"
                " ORDER BY end_ts DESC LIMIT 1",
                (hex_code, int(start_ts), int(start_ts - max_gap_s), callsign_key, callsign_key),
            ).fetchone()
        if row is None:
            return None
        rec = self._summary_from_row(row[:-1])
        rec.points = decode_track(row[-1])
        rec.callsign_key = callsign_key
        return rec

    def recent_live(self, since_ts: float) -> list[FlightRecord]:
        """Flights our recorder was still tracking at `since_ts` (to resume them after a restart)."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_SUMMARY_COLUMNS}, track FROM flights WHERE source = 'live' AND end_ts >= ?",
                (int(since_ts),),
            ).fetchall()
        records = []
        for row in rows:
            rec = self._summary_from_row(row[:-1])
            rec.points = decode_track(row[-1])
            records.append(rec)
        return records

    def get(self, flight_id: int) -> FlightRecord | None:
        with self._lock:
            row = self._conn.execute(f"SELECT {_SUMMARY_COLUMNS}, track FROM flights WHERE id = ?", (flight_id,)).fetchone()
        if row is None:
            return None
        rec = self._summary_from_row(row[:-1])
        rec.points = decode_track(row[-1])
        return rec

    @staticmethod
    def _summary_from_row(row: tuple) -> FlightRecord:
        fid, hex_code, callsign, reg, type_code, origin, dest, start, end, source, n = row
        return FlightRecord(
            hex=hex_code, callsign=callsign, registration=reg, type_code=type_code, origin=origin,
            destination=dest, start_ts=start, end_ts=end, source=source, id=fid, n_points=n,
        )

    def search(
        self,
        *,
        callsigns: Iterable[str] = (),
        registration: str | None = None,
        hex_code: str | None = None,
        airports: Iterable[str] = (),
        start_ts: float | None = None,
        end_ts: float | None = None,
        limit: int = 50,
    ) -> list[FlightRecord]:
        """Flights matching any of the identifiers, at any of the airports, overlapping the time window."""
        where, params = [], []
        ident = []
        callsigns = [c.upper() for c in callsigns if c]
        if callsigns:
            ident.append(f"callsign_key IN ({','.join('?' * len(callsigns))})")
            params += callsigns
        if registration:
            ident.append("registration_key = ?")
            params.append(registration_key(registration))
        if hex_code:
            ident.append("hex = ?")
            params.append(hex_code.lower())
        if ident:
            where.append("(" + " OR ".join(ident) + ")")
        airports = [a.upper() for a in airports if a]
        if airports:
            marks = ",".join("?" * len(airports))
            where.append(f"(origin IN ({marks}) OR destination IN ({marks}))")
            params += airports + airports
        if start_ts is not None:
            where.append("end_ts >= ?")
            params.append(int(start_ts))
        if end_ts is not None:
            where.append("start_ts < ?")
            params.append(int(end_ts))
        sql = f"SELECT {_SUMMARY_COLUMNS} FROM flights"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY start_ts DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._summary_from_row(r) for r in rows]

    def purge_before(self, ts: float) -> int:
        with self._lock, self._conn:
            removed = self._conn.execute("DELETE FROM flights WHERE end_ts < ?", (int(ts),)).rowcount
            cutoff_day = time.strftime("%Y-%m-%d", time.gmtime(ts))
            self._conn.execute("DELETE FROM imports WHERE day < ?", (cutoff_day,))
            return removed

    def mark_imported(self, day: str, flights: int, complete: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO imports VALUES (?, ?, ?, ?)", (day, int(time.time()), flights, int(complete))
            )

    def imported_days(self) -> dict[str, bool]:
        """UTC days with imported history → whether the whole day was imported (False for samples)."""
        with self._lock:
            return {day: bool(complete) for day, complete in self._conn.execute("SELECT day, complete FROM imports ORDER BY day")}

    def stats(self) -> dict:
        with self._lock:
            n, first, last = self._conn.execute("SELECT COUNT(*), MIN(start_ts), MAX(end_ts) FROM flights").fetchone()
            by_source = dict(self._conn.execute("SELECT source, COUNT(*) FROM flights GROUP BY source").fetchall())
        return {"flights": n, "oldest": first, "newest": last, "bySource": by_source}
