"""Wiring of the backend's long-lived components."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .feeds import ADSB_FI, ADSB_LOL, FeedClient
from .live import AircraftState, LiveStore
from .poller import Poller
from .recorder import Recorder
from .refdata import RefData
from .store import FlightStore


@dataclass
class Services:
    settings: Settings
    refdata: RefData
    store: FlightStore
    live: LiveStore
    recorder: Recorder
    sweep_feed: FeedClient  # world sweep by aircraft type (adsb.lol)
    detail_feed: FeedClient  # areas and single aircraft (adsb.fi, or adsb.lol if disabled)
    poller: Poller

    @property
    def feeds(self) -> list[FeedClient]:
        return [self.sweep_feed] if self.detail_feed is self.sweep_feed else [self.sweep_feed, self.detail_feed]


def build_services(
    settings: Settings,
    sweep_feed: FeedClient | None = None,
    detail_feed: FeedClient | None = None,
) -> Services:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    refdata = RefData(settings.reference_db)
    store = FlightStore(settings.flights_db)

    def route_lookup(state: AircraftState):
        return refdata.match_route(state.callsign, state.lat, state.lon, state.track)

    live = LiveStore(hide_military=settings.hide_military, route_lookup=route_lookup)
    recorder = Recorder(store, refdata)
    live.add_observer(recorder.observe)
    sweep_feed = sweep_feed or FeedClient(
        ADSB_LOL,
        settings.user_agent,
        min_interval=settings.adsblol_min_interval,
        start_interval=settings.adsblol_start_interval,
    )
    if detail_feed is None:
        detail_feed = (
            FeedClient(
                ADSB_FI,
                settings.user_agent,
                min_interval=settings.adsbfi_min_interval,
                start_interval=settings.adsbfi_start_interval,
            )
            if settings.adsbfi_enabled
            else sweep_feed
        )
    poller = Poller(sweep_feed, detail_feed, live, global_sweep=settings.global_sweep)
    return Services(settings, refdata, store, live, recorder, sweep_feed, detail_feed, poller)
