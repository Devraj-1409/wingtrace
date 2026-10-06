import asyncio

import httpx
import pytest

from app.feeds import ADSB_LOL, PRIORITY_FOCUS, PRIORITY_SWEEP, FeedClient, UpstreamError
from app.poller import cell_center, cell_radius_nm, cells_for_bbox
from app.geo import haversine_m


def _client(handler, **kw) -> FeedClient:
    opts = {"min_interval": 0.001, "start_interval": 0.01, "max_interval": 0.2}
    opts.update(kw)
    return FeedClient(ADSB_LOL, "test", transport=httpx.MockTransport(handler), **opts)


@pytest.mark.anyio
async def test_fetch_and_coalesce():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"ac": [{"hex": "abc"}]})

    client = _client(handler)
    await client.start()
    try:
        results = await asyncio.gather(client.by_type("A320"), client.by_type("A320"), client.by_hex("abc"))
        assert results[0] == results[1] == [{"hex": "abc"}]
        assert calls.count("/v2/type/A320") == 1  # identical requests shared
    finally:
        await client.close()


@pytest.mark.anyio
async def test_backs_off_on_429_then_succeeds():
    responses = iter([httpx.Response(429), httpx.Response(200, json={"ac": []})])
    client = _client(lambda r: next(responses))
    await client.start()
    try:
        assert await client.by_type("B738") == []
        assert client.stats["rateLimited"] == 1
        assert client.interval > 0.01
    finally:
        await client.close()


@pytest.mark.anyio
async def test_gives_up_after_repeated_errors():
    client = _client(lambda r: httpx.Response(503))
    await client.start()
    try:
        with pytest.raises(UpstreamError):
            await client.by_type("B738")
    finally:
        await client.close()


@pytest.mark.anyio
async def test_priority_order_and_abandoned_requests_skipped():
    order = []

    def handler(request):
        order.append(request.url.path)
        return httpx.Response(200, json={"ac": []})

    client = _client(handler, min_interval=0.05, start_interval=0.05)
    sweep = asyncio.create_task(client.fetch("/v2/type/A1", PRIORITY_SWEEP))
    abandoned = asyncio.create_task(client.fetch("/v2/hex/gone", PRIORITY_FOCUS))
    focus = asyncio.create_task(client.fetch("/v2/hex/abc", PRIORITY_FOCUS))
    await asyncio.sleep(0)
    abandoned.cancel()
    await client.start()
    try:
        await asyncio.gather(sweep, focus)
        assert order == ["/v2/hex/abc", "/v2/type/A1"]
    finally:
        await client.close()


@pytest.mark.anyio
async def test_focus_falls_back_to_adsblol_when_adsbfi_has_no_position():
    from app.feeds import ADSB_FI
    from app.live import LiveStore
    from app.poller import Poller

    calls = []

    def fi(request):
        calls.append(("fi", request.url.path))
        return httpx.Response(200, json={"ac": []})

    def lol(request):
        calls.append(("lol", request.url.path))
        return httpx.Response(200, json={"ac": [{"hex": "780abc", "lat": 22.3, "lon": 114.1, "alt_baro": 9000,
                                                 "seen_pos": 1, "flight": "CPA1  "}]})

    opts = {"min_interval": 0.001, "start_interval": 0.001}
    detail = FeedClient(ADSB_FI, "test", transport=httpx.MockTransport(fi), **opts)
    sweep = FeedClient(ADSB_LOL, "test", transport=httpx.MockTransport(lol), **opts)
    live = LiveStore(hide_military=True)
    poller = Poller(sweep, detail, live, global_sweep=False)
    await detail.start()
    await sweep.start()
    try:
        await poller._fetch_focus("780abc")
        assert live.get("780abc") is not None
        await poller._fetch_focus("780abc")  # remembered: goes straight to adsb.lol next time
        assert [c[0] for c in calls] == ["fi", "lol", "lol"]
    finally:
        await detail.close()
        await sweep.close()


def test_cells_cover_bbox_and_circles_cover_cells():
    cells = cells_for_bbox((-2.0, 50.0, 2.0, 53.0))
    assert cells and len(cells) <= 9
    for cell in cells:
        lat, lon = cell_center(cell)
        r = cell_radius_nm(cell)
        assert r <= 250
        # the cell corners are inside the query circle
        band = cell[0]
        south = -90 + band * 5
        assert haversine_m(lat, lon, south, lon) / 1852 <= r


def test_cells_antimeridian_and_too_big():
    cells = cells_for_bbox((178.0, 10.0, -178.0, 12.0))
    assert cells is not None and len({c[1] for c in cells}) >= 2
    assert cells_for_bbox((-180.0, -60.0, 180.0, 60.0)) is None
