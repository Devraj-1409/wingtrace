"""Flight track points: simplification and a compact binary encoding for storage."""

from __future__ import annotations

import struct
import sys
import zlib
from array import array
from collections.abc import Sequence

from .geo import angle_diff_deg

# (unix time s, lat, lon, altitude ft or None when on the ground, ground speed kt, track deg)
TrackPoint = tuple[float, float, float, "int | None", "float | None", "float | None"]

_FORMAT_VERSION = 1
_GROUND = -1_000_000  # altitude sentinel for "on the ground"
_MISSING = -1


def simplify_track(
    points: Sequence[TrackPoint],
    *,
    max_interval_s: float = 120.0,
    max_track_change_deg: float = 4.0,
    max_alt_change_ft: int = 300,
    max_gs_change_kt: float = 30.0,
    gap_s: float = 300.0,
) -> list[TrackPoint]:
    """Drop points that add little shape to the track.

    A point is kept when the aircraft has turned, climbed, sped up or travelled
    long enough since the last kept point, and on either side of a data gap.
    Turns end up sampled every few degrees and straight cruise every couple of
    minutes, which keeps replay faithful at a fraction of the size.
    """
    n = len(points)
    if n <= 2:
        return list(points)
    kept = [points[0]]
    last = points[0]
    for i in range(1, n - 1):
        p = points[i]
        alt, last_alt = p[3], last[3]
        keep = (
            p[0] - last[0] >= max_interval_s
            or points[i + 1][0] - p[0] >= gap_s
            or p[0] - points[i - 1][0] >= gap_s
            or (alt is None) != (last_alt is None)
            or (alt is not None and last_alt is not None and abs(alt - last_alt) >= max_alt_change_ft)
            or (p[5] is not None and last[5] is not None and angle_diff_deg(p[5], last[5]) >= max_track_change_deg)
            or (p[4] is not None and last[4] is not None and abs(p[4] - last[4]) >= max_gs_change_kt)
        )
        if keep:
            kept.append(p)
            last = p
    kept.append(points[-1])
    return kept


def encode_track(points: Sequence[TrackPoint]) -> bytes:
    """Column-wise delta encoding + zlib. Lossy to ~1 m / 1 s / 1 ft / 1 kt / 0.1°."""
    base = int(points[0][0]) if points else 0
    cols = [array("i") for _ in range(6)]
    prev = [0] * 6
    for t, lat, lon, alt, gs, trk in points:
        values = (
            round(t - base),
            round(lat * 1e5),
            round(lon * 1e5),
            _GROUND if alt is None else int(round(alt)),
            _MISSING if gs is None else int(round(gs)),
            _MISSING if trk is None else int(round(trk * 10)) % 3600,
        )
        for c in range(6):
            cols[c].append(values[c] - prev[c])
            prev[c] = values[c]
    if sys.byteorder == "big":
        for col in cols:
            col.byteswap()
    header = struct.pack("<BqI", _FORMAT_VERSION, base, len(points))
    return header + zlib.compress(b"".join(col.tobytes() for col in cols), 6)


def decode_track(blob: bytes) -> list[TrackPoint]:
    version, base, n = struct.unpack_from("<BqI", blob)
    if version != _FORMAT_VERSION:
        raise ValueError(f"unsupported track format version {version}")
    raw = zlib.decompress(blob[struct.calcsize("<BqI"):])
    cols = []
    for c in range(6):
        col = array("i")
        col.frombytes(raw[c * 4 * n:(c + 1) * 4 * n])
        if sys.byteorder == "big":
            col.byteswap()
        cols.append(col)
    out: list[TrackPoint] = []
    acc = [0] * 6
    for i in range(n):
        for c in range(6):
            acc[c] += cols[c][i]
        alt = None if acc[3] == _GROUND else acc[3]
        gs = None if acc[4] == _MISSING else float(acc[4])
        trk = None if acc[5] == _MISSING else acc[5] / 10.0
        out.append((float(base + acc[0]), acc[1] / 1e5, acc[2] / 1e5, alt, gs, trk))
    return out
