# WingTrace

Live air traffic on a realistic 3D globe. A free, non-commercial hobby project.

- **Live**: thousands of aircraft on a CesiumJS globe: NASA's Blue Marble under a sunlit atmosphere by
  day, city lights by night and Sentinel-2 satellite imagery up close. Aircraft are coloured by
  altitude and move smoothly between updates.
- **Routes in 3D**: click a plane to see where it came from and where it's going: the path flown so
  far (coloured by altitude), the rest of the route (dashed), progress and an arrival estimate.
- **Top 10**: the longest, shortest, fastest and highest flights in the air right now.
- **Honest about gaps**: aircraft out of receiver range (usually over oceans) keep moving along their
  route and are drawn faded as *estimated*.
- **Shareable links**: `#ac=<hex>` opens an aircraft.

## Run it (Windows)

Needs **Python 3.12** and **Node.js 20+**.

Double-click **`start.cmd`** (or run `.\start.cmd`). The first run installs everything, which takes a
few minutes. After that it starts in seconds and opens **http://localhost:8000** in your browser. Keep
its window open while you use WingTrace; close it to stop.

`start.cmd` builds the website (only when something changed) and runs one server that serves both the
website and the flight data, the same way the real site works.

### Working on the code

`dev.cmd` opens two windows instead: the backend on http://127.0.0.1:8000 and a live-reloading website
on http://localhost:5173 (much slower to load, but code changes appear instantly).

Manual setup:

```powershell
cd backend
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m app.refdata_build          # routes, airlines, aircraft types, airports
.venv\Scripts\python -m uvicorn app.main:create_app --factory --port 8000

cd ..\frontend
npm install
npm run dev
```

## Configuration

Backend settings are environment variables, all optional: see `backend/.env.example`.
Frontend settings: `frontend/.env.example`.

## Tests

```powershell
cd backend;  .venv\Scripts\python -m pytest
cd frontend; npm run build      # type-check + production build
```

## How it works

```
Browser (CesiumJS, TypeScript)  ──/api──▶  Backend (Python, FastAPI)  ──▶  adsb.lol   world sweep by aircraft type
  aircraft, routes, top 10                   one paced queue per source ──▶  adsb.fi    areas people view, selected aircraft
                                             live store + estimation
                                             flight paths → SQLite
```

- The free APIs don't allow browser requests and have tight rate limits, so every request goes through
  the backend. Each source has one shared, self-pacing queue (it slows down on HTTP 429), so the load on
  these community services is the same however many people use the site.
- adsb.lol has no "all aircraft" endpoint; the backend sweeps the world one aircraft type at a time, plus
  an area sweep for everything else. With the free sources' limits, the zoomed-out world view is
  typically a minute or two old; zoomed-in areas and selected aircraft refresh every few seconds.
- Routes come from callsigns; a route is shown only if the aircraft's position fits it.

| Path | What |
|---|---|
| `backend/app/feeds.py` | Paced, prioritised clients for adsb.lol and adsb.fi |
| `backend/app/poller.py` | World sweep, area sweep and focus refreshes |
| `backend/app/live.py` | Live aircraft state, privacy filter, out-of-coverage estimation |
| `backend/app/recorder.py` | Builds each live flight's path |
| `backend/app/refdata*.py` | Routes, airlines, aircraft types, airports |
| `frontend/src/viewer.ts` | The globe: imagery, lighting, camera |
| `frontend/src/aircraftLayer.ts` | Aircraft icons, dead reckoning, picking |
| `frontend/src/routeLayer.ts` | Flown path, planned route, airports |

## Data and licences

| Data | Source | Licence / terms |
|---|---|---|
| Live positions | [adsb.lol](https://adsb.lol) | ODbL; credit required |
| Live positions | [adsb.fi](https://adsb.fi) | Personal, non-commercial use; credit and link required |
| Routes, airlines, aircraft types | [VRS standing data](https://github.com/vradarserver/standing-data) | CC0 |
| Airports | [OurAirports](https://ourairports.com/data/) | Public domain |
| Airport time zones | [mwgg/Airports](https://github.com/mwgg/Airports) | MIT |
| Place names | [Natural Earth](https://www.naturalearthdata.com) | Public domain |
| Earth by day and night | NASA Blue Marble / Black Marble via [GIBS](https://earthdata.nasa.gov/gibs) | Public domain (credit appreciated) |
| Satellite imagery up close | [Sentinel-2 cloudless](https://s2maps.eu) by EOX | CC BY-NC-SA 4.0: non-commercial, credit on the map |
| 3D globe | [CesiumJS](https://cesium.com/platform/cesiumjs/) | Apache 2.0 |
| Typeface | [Inter](https://rsms.me/inter/) | SIL Open Font License |

Aircraft on the FAA LADD list or using Privacy ICAO Addresses are never shown; military aircraft are
hidden by default. Not for navigation or any safety-related use.

Putting it online: see [docs/DEPLOY.md](docs/DEPLOY.md).
