# WingTrace — Architecture & Milestone Plan

> **Updates since this plan:** flight replay was dropped on 2026-10-03 (the site shows live traffic;
> the recorder only draws each live flight's path), and Cesium ion was dropped on 2026-10-06 (see the
> data sources table). `start.cmd` now runs the site; `dev.cmd` is for working on the code.

_Last updated: 2026-10-03_

## Goal

A free, non-commercial website where you can watch any flight **live or replay it afterward**, on a
**realistic 3D globe** that zooms smoothly from the whole planet down to terrain and airports.

What sets it apart from existing trackers:

| | World traffic view | Realistic 3D | Replay |
|---|---|---|---|
| FlightRadar24 | Flat map only | Only following one plane | Flat map, longer history paid |
| Hobby globe sites (WHEREPLANES, FlightOrbit, …) | Globe | Mostly stylized | None found |
| **This project** | **Globe** | **Yes** | **Yes, on the globe, free** |

## Status

| Milestone | Status |
|---|---|
| M0 Setup | ✅ Done: `frontend/` (Vite + TypeScript + CesiumJS), `backend/` (Python 3.12 + FastAPI), `dev.ps1` |
| M1 Realistic globe + live planes | ✅ Done |
| M2 Routes in 3D | ✅ Done |
| M3 Whole-world traffic + gaps | ✅ Done (world refresh is minutes, not seconds: see Limits) |
| M4 Recorder + replay | ✅ Done: live recorder + full-detail daily archive importer |
| M5 Go public | 🟡 Prepared (Dockerfile, Caddy HTTPS, GitHub Pages workflow, [DEPLOY.md](DEPLOY.md)); not deployed. Needs the owner's accounts and the launch checklist in DEPLOY.md |

## Ground rules

- **Free data only, used within its licence.** Every source is credited on the About page.
- **No scraping** of FlightRadar24, FlightAware or any other site that forbids it.
- Each data source sits behind its own adapter (`backend/app/feeds.py`, `refdata_build.py`,
  `importer.py`), so swapping one later touches one file.
- **Privacy:** aircraft on the FAA LADD list or using Privacy ICAO Addresses are never shown;
  military aircraft hidden by default (`HIDE_MILITARY`).
- **Name:** don't use "Flightradar" or other trackers' names or logos. The site is called WingTrace.
- Re-check every licence before launch; terms change.

## Data sources

Licence check 2026-10-03. ✅ OK for a non-commercial hobby site · ⚠️ OK with conditions · ❌ not used.

| Need | Source | Licence / terms | Status |
|---|---|---|---|
| World sweep (positions by aircraft type), daily full history | **adsb.lol** API + `globe_history` releases | ODbL, credit. API terms: free; "contact me" for production use | ✅ (email them before launch) |
| Areas being viewed, selected aircraft | **adsb.fi** open data API | Personal, non-commercial use only; credit + link; 1 request/s | ✅ while non-commercial |
| Routes, airlines, aircraft types | **VRS standing-data** | CC0 | ✅ |
| Airports | **OurAirports** (+ standing-data airports) | Public domain / CC0 | ✅ |
| Day imagery from space, night lights | **NASA Blue Marble / Black Marble** via GIBS | Free with credit | ✅ |
| Satellite imagery, terrain, 3D buildings | **Cesium ion Community** (Bing aerial, Cesium World Terrain, OSM Buildings) | Non-commercial; logo and credits must stay visible | Dropped 2026-10-06 (no ion logo): NASA imagery from space, **Sentinel-2 cloudless (EOX)** up close (CC BY-NC-SA), no terrain or 3D buildings |
| Imagery without a token | **EOX Sentinel-2 cloudless** | 2018+ layers CC BY-NC-SA | ✅ fallback |
| 3D globe | **CesiumJS** | Apache 2.0 | ✅ |
| Route lookup | adsbdb | Route data may not be republished or stored | ❌ |
| Live data, tracks | OpenSky Network | Live services need a written licence | ❌ |
| Google Photorealistic 3D Tiles | Google via Cesium ion | Google's terms forbid mixing with non-Google maps; ~1,000 sessions/month free | ❌ |
| FlightRadar24 / FlightAware websites | — | Terms forbid scraping | ❌ |

## Architecture

```
 Browser (frontend)                        Backend (FastAPI)                         External
┌──────────────────────────┐  /api/...  ┌─────────────────────────────────┐
│ CesiumJS globe           │ ─────────▶ │ API: live, aircraft, search,    │
│  aircraftLayer: icons,   │ ◀───────── │      flights, flights/{id}      │
│   dead reckoning, picking│            ├─────────────────────────────────┤
│  routeLayer: flown path, │            │ Poller ── sweep by type ───────▶│ adsb.lol
│   planned route, airports│            │        ── regions, focus ──────▶│ adsb.fi
│  replay: clock, timeline │            │ (one paced queue per source)    │
│ UI: search, flight card, │            │ LiveStore: privacy filter,      │
│  settings, about         │            │   out-of-coverage estimation    │
└──────────────────────────┘            │ Recorder → SQLite flights       │
                                        │ Importer (daily) ◀──────────────│ GitHub: adsb.lol history
                                        │ RefData: routes, airports       │◀ VRS standing-data, OurAirports
                                        └─────────────────────────────────┘
```

Why a backend: the data APIs block browser requests (no CORS) and have tight rate limits; one shared,
paced queue per source keeps the load constant however many visitors there are; and replay needs
something recording around the clock.

## Limits (measured 2026-10-03)

- **adsb.lol** sustains about one request every 8–10 s for anonymous users and briefly refuses
  connections after bursts. A world sweep (24 common types every cycle + 10 rotating others) therefore
  takes several minutes. Planes are dead-reckoned in between, so they keep moving smoothly.
  Feeding adsb.lol with a home receiver unlocks their faster feeder API.
- **Coverage** is thin over oceans, Russia, much of Africa and parts of Asia (including India, compared
  with FlightRadar24). Cruising aircraft that leave coverage are estimated along their route.
- **Routes** are crowd-sourced by callsign; only shown when the aircraft's position fits.
- **Replay quality**: live recordings have a point every few minutes for most aircraft; imported
  history (next day) has full detail (~180 points per flight after simplification).

## Later ideas

- 3D aircraft models and a cockpit view.
- Airport pages (arrivals, departures, runways from OurAirports).
- Show all traffic at a moment in the past ("time machine"), not just one flight.
- Weather overlay; emergency-squawk alerts.
- A home ADS-B receiver feeding adsb.lol/adsb.fi: better local coverage and feeder API access.
