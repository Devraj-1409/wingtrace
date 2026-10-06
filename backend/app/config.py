"""Runtime settings, read from environment variables (see backend/.env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value else default


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    user_agent: str
    # Upstream pacing: seconds between requests. Each client starts at its
    # start interval, slows down on HTTP 429 and speeds up again gradually,
    # never going below its min interval.
    adsblol_min_interval: float
    adsblol_start_interval: float
    adsbfi_enabled: bool
    adsbfi_min_interval: float
    adsbfi_start_interval: float
    global_sweep: bool
    hide_military: bool
    retention_days: int
    # Import yesterday's full flight history from adsb.lol every day (~3 GB download per day).
    auto_import: bool
    client_requests_per_minute: int
    frontend_dist: Path | None
    cors_origins: tuple[str, ...]

    @classmethod
    def from_env(cls) -> Settings:
        data_dir = Path(_env("DATA_DIR", str(BACKEND_DIR / "data")))
        dist = _env("FRONTEND_DIST", "")
        origins = tuple(o.strip() for o in _env("CORS_ORIGINS", "").split(",") if o.strip())
        return cls(
            data_dir=data_dir,
            user_agent=_env("USER_AGENT", "wingtrace/0.1 (non-commercial hobby project)"),
            # adsb.lol sustains roughly one request every 8-10 s for anonymous users.
            adsblol_min_interval=_env_float("ADSBLOL_MIN_INTERVAL", 6.0),
            adsblol_start_interval=_env_float("ADSBLOL_START_INTERVAL", 8.0),
            # adsb.fi publishes a limit of 1 request per second.
            adsbfi_enabled=_env_bool("ADSBFI_ENABLED", True),
            adsbfi_min_interval=_env_float("ADSBFI_MIN_INTERVAL", 1.2),
            adsbfi_start_interval=_env_float("ADSBFI_START_INTERVAL", 1.5),
            global_sweep=_env_bool("GLOBAL_SWEEP", True),
            hide_military=_env_bool("HIDE_MILITARY", True),
            # Flown paths are only shown while a flight is live; 2 days covers the longest flights.
            retention_days=_env_int("RETENTION_DAYS", 2),
            auto_import=_env_bool("AUTO_IMPORT", False),
            client_requests_per_minute=_env_int("CLIENT_REQUESTS_PER_MINUTE", 240),
            frontend_dist=Path(dist) if dist else None,
            cors_origins=origins,
        )

    @property
    def reference_db(self) -> Path:
        return self.data_dir / "reference.sqlite"

    @property
    def flights_db(self) -> Path:
        return self.data_dir / "flights.sqlite"
