"""FastAPI application. Run with:  uvicorn app.main:create_app --factory"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from . import api
from .config import BACKEND_DIR, Settings
from .feeds import FeedClient
from .services import build_services

log = logging.getLogger("wingtrace")


class RateLimitMiddleware:
    """Per-client limit on /api requests, so one visitor can't use up the shared upstream budget."""

    def __init__(self, app, per_minute: int):
        self.app = app
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = {}
        self._last_prune = time.monotonic()

    @staticmethod
    def _client_ip(scope) -> str:
        client = scope.get("client") or ("?", 0)
        if client[0] in ("127.0.0.1", "::1"):  # behind our own reverse proxy
            for name, value in scope.get("headers", []):
                if name == b"x-forwarded-for":
                    return value.decode().split(",")[0].strip()
        return client[0]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        now = time.monotonic()
        hits = self._hits.setdefault(self._client_ip(scope), deque())
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= self.per_minute:
            body = b'{"detail":"too many requests, slow down"}'
            await send({"type": "http.response.start", "status": 429,
                        "headers": [(b"content-type", b"application/json"), (b"retry-after", b"10")]})
            await send({"type": "http.response.body", "body": body})
            return
        hits.append(now)
        if now - self._last_prune > 300:
            self._last_prune = now
            for ip in [ip for ip, h in self._hits.items() if not h or now - h[-1] > 60]:
                del self._hits[ip]
        return await self.app(scope, receive, send)


def create_app(
    settings: Settings | None = None,
    *,
    sweep_feed: FeedClient | None = None,
    detail_feed: FeedClient | None = None,
    background: bool = True,
) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = build_services(settings, sweep_feed, detail_feed)
        app.state.services = svc
        if not svc.refdata.available:
            log.warning("No reference data (routes, airports). Run: python -m app.refdata_build")
        resumed = await asyncio.to_thread(svc.recorder.restore)
        if resumed:
            log.info("resumed %d flights in progress", resumed)
        for feed in svc.feeds:
            await feed.start()
        tasks = []
        if background:
            tasks = [
                asyncio.create_task(svc.poller.run(), name="poller"),
                asyncio.create_task(svc.recorder.run(), name="recorder"),
                asyncio.create_task(_retention_loop(svc), name="retention"),
            ]
            if settings.auto_import:
                tasks.append(asyncio.create_task(_auto_import_loop(svc), name="auto-import"))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.to_thread(svc.recorder.flush_all)
            for feed in svc.feeds:
                await feed.close()
            svc.store.close()
            svc.refdata.close()

    app = FastAPI(title="WingTrace API", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["GET"])
    app.add_middleware(RateLimitMiddleware, per_minute=settings.client_requests_per_minute)
    app.include_router(api.router)
    if settings.frontend_dist and settings.frontend_dist.is_dir():
        # Production: serve the built frontend from the same origin.
        app.mount("/", StaticFiles(directory=settings.frontend_dist, html=True), name="frontend")
    return app


AUTO_IMPORT_AFTER_UTC_HOUR = 5  # adsb.lol publishes each day's archive around 03:30 UTC the next day


async def _auto_import_loop(svc) -> None:
    """Import yesterday's full-detail history once it has been published (in a separate process)."""
    while True:
        now = datetime.now(timezone.utc)
        yesterday = (now - timedelta(days=1)).date().isoformat()
        done = await asyncio.to_thread(svc.store.imported_days)
        if now.hour >= AUTO_IMPORT_AFTER_UTC_HOUR and not done.get(yesterday):
            log.info("auto-import: importing %s", yesterday)
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "app.importer", "--date", yesterday, cwd=BACKEND_DIR
            )
            code = await proc.wait()
            log.info("auto-import of %s finished with exit code %s", yesterday, code)
            if code != 0:
                await asyncio.sleep(3 * 3600)  # not published yet or a network problem: try again later
                continue
        await asyncio.sleep(1800)


async def _retention_loop(svc) -> None:
    while True:
        cutoff = time.time() - svc.settings.retention_days * 86400
        try:
            removed = await asyncio.to_thread(svc.store.purge_before, cutoff)
            if removed:
                log.info("retention: removed %d flights older than %d days", removed, svc.settings.retention_days)
        except Exception:
            log.exception("retention purge failed")
        await asyncio.sleep(3600)
