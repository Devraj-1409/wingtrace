import gzip
import io
import json
import tarfile

from app.importer import Reader, flights_from_trace, tar_members
from app.live import DB_FLAG_LADD

BASE = 1_790_900_000.0


def _trace(points, **top):
    d = {"icao": "c0583a", "r": "C-GHKR", "t": "A333", "dbFlags": 0, "timestamp": BASE, "trace": points}
    d.update(top)
    return gzip.compress(json.dumps(d).encode())


def _pt(dt, lat, lon, alt, flags=0, callsign=None):
    info = {"flight": callsign} if callsign else None
    return [dt, lat, lon, alt, 450.0, 90.0, flags, 0, info, "adsb_icao", None, None, None, None]


def _leg(t0, callsign, n=20, start_flags=0):
    pts = [_pt(t0, 50.0, 0.0, "ground", start_flags, callsign), _pt(t0 + 30, 50.0, 0.01, "ground")]
    pts += [_pt(t0 + 60 + i * 30, 50.0, 0.02 + i * 0.05, 2000 + i * 1000) for i in range(n)]
    pts += [_pt(t0 + 60 + n * 30, 50.0, 1.5, "ground"), _pt(t0 + 90 + n * 30, 50.0, 1.51, "ground")]
    return pts


def test_two_legs_split_on_leg_marker():
    raw = _trace(_leg(0, "ACA840  ") + _leg(20_000, "ACA841  ", start_flags=2))
    flights = flights_from_trace(raw, hide_military=True)
    assert [f["callsign"] for f in flights] == ["ACA840", "ACA841"]
    for f in flights:
        pts = f["points"]
        assert pts[0][3] is None and pts[-1][3] is None  # one ground point at each end
        assert sum(1 for p in pts if p[3] is None) == 2
        assert f["hex"] == "c0583a" and f["type_code"] == "A333" and f["registration"] == "C-GHKR"
    assert flights[0]["points"][0][0] == BASE + 30  # last ground point before take-off


def test_split_on_long_low_gap_without_leg_marker():
    second = [_pt(9000 + i * 30, 51.0, 1.0 + i * 0.05, 3000 + i * 500) for i in range(20)]
    raw = _trace(_leg(0, "ACA840  ") [:-2] + second)  # no landing seen, gap > 30 min while low
    assert len(flights_from_trace(raw, hide_military=True)) == 2


def test_hidden_aircraft_skipped_and_short_hops_dropped():
    assert flights_from_trace(_trace(_leg(0, "X"), dbFlags=DB_FLAG_LADD), hide_military=True) == []
    hop = [_pt(0, 50, 0, 1000), _pt(30, 50, 0.01, 1100)]
    assert flights_from_trace(_trace(hop), hide_military=True) == []


def test_flight_crossing_midnight_is_joined(tmp_path, refdata):
    from app.importer import _records
    from app.store import FlightStore

    store = FlightStore(tmp_path / "flights.sqlite")
    midnight = 1_790_985_600.0  # 2026-10-03 00:00 UTC
    before = [(midnight - 3600 + i * 60, 50.0, -10.0 - i * 0.2, 36000, 480.0, 270.0) for i in range(50)]
    after = [(midnight + 600 + i * 60, 49.0, -25.0 - i * 0.2, 36000, 480.0, 270.0) for i in range(30)]
    flight = {"hex": "c0583a", "callsign": "BAW117", "registration": "G-XLEA", "type_code": "A388"}
    store.save_archive_batch(_records([{**flight, "points": before}], refdata, store, midnight - 86400))
    joined = _records([{**flight, "points": after}], refdata, store, midnight)
    assert joined[0].id is not None and len(joined[0].points) == 80
    store.save_archive_batch(joined)
    found = store.search(hex_code="c0583a")
    assert len(found) == 1 and found[0].start_ts == int(before[0][0]) and found[0].end_ts == int(after[-1][0])
    # A flight that took off after midnight is not joined.
    takeoff = [(midnight + 7200, 51.47, -0.46, None, 0.0, 270.0)] + after
    assert _records([{**flight, "points": takeoff}], refdata, store, midnight)[0].id is None
    store.close()


class _FakeRemote:
    """Serves a byte string like RemoteParts, counting how many bytes were streamed."""

    def __init__(self, data: bytes):
        self.data = data
        self.streams = 0
        self.downloaded = 0

    def stream_from(self, offset: int):
        self.streams += 1
        for i in range(offset, len(self.data), 4096):
            chunk = self.data[i:i + 4096]
            self.downloaded += len(chunk)
            yield chunk


def _tar(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_tar_walker_yields_wanted_members_and_jumps_over_big_ones():
    big = b"\x01" * (6 * 1024 * 1024)
    long_name = "./traces/3a/" + "x" * 120 + "/trace_full_c0583a.json"
    data = _tar({"./heatmap/40.bin.ttf": big, "./traces/3a/trace_full_aaaaaa.json": b"one", long_name: b"two"})
    remote = _FakeRemote(data)
    found = list(tar_members(Reader(remote), lambda n: "trace_full_" in n))
    assert found == [("./traces/3a/trace_full_aaaaaa.json", b"one"), (long_name, b"two")]
    assert remote.streams == 2  # reopened once to jump over the heatmap
    assert remote.downloaded < len(big) / 2
