import {
  BoundingSphere,
  Cartesian3,
  HeadingPitchRange,
  Matrix4,
  ScreenSpaceEventHandler,
  ScreenSpaceEventType,
  Transforms,
} from "cesium";
import "@fontsource-variable/inter";
import "cesium/Build/Cesium/Widgets/widgets.css";
import "./style.css";
import { AircraftLayer } from "./aircraftLayer";
import { AltitudeScale, altitudeCss, type AltitudeMode } from "./altitude";
import { api, ApiError } from "./api";
import { fmtAgo, fmtDuration, fmtInt } from "./format";
import { LivePanel } from "./livePanel";
import { LivePoller } from "./livePoller";
import { PlaceLabels } from "./placeLabels";
import { TopPanel } from "./topPanel";
import { RouteLayer } from "./routeLayer";
import { FLAG_EMERGENCY, FLAG_ESTIMATED, FLAG_GROUND, FLAG_MLAT, type AircraftDetails, type LiveResponse } from "./types";
import { createGlobe, flyHome, zoomBy } from "./viewer";

const $ = <T extends HTMLElement = HTMLElement>(sel: string) => document.querySelector<T>(sel)!;

const { viewer } = createGlobe($("#cesiumContainer"), $("#credits"));
const scale = new AltitudeScale(viewer);
// Place names first, so aircraft and routes are drawn on top of them.
const placeLabels = new PlaceLabels(viewer);
const aircraft = new AircraftLayer(viewer, scale);
const routes = new RouteLayer(viewer, scale);

// ---------- State ----------

let lastLive: LiveResponse | null = null;
let lastLiveAt = 0;
let lastInView = false;
let liveError = false;
let selected: string | null = null;
let selectedDetails: AircraftDetails | null = null;
let detailsTimer: number | undefined;
let detailsAbort: AbortController | null = null;
let following = false;

// ---------- Panels ----------

const panel = new LivePanel($("#live-body"), $<HTMLInputElement>("#search"), $("#search-results"), {
  selectAircraft: (hex, flyTo) => void select(hex, flyTo),
  selectAirport: (airport) => {
    deselect();
    viewer.camera.flyTo({ destination: Cartesian3.fromDegrees(airport.lon, airport.lat - 0.9, 90_000), orientation: { pitch: -0.75, heading: 0, roll: 0 } });
  },
  close: () => deselect(),
  toggleFollow: () => setFollow(!following),
});

// ---------- Phone layout: the panel is a bottom sheet that can be collapsed ----------

const panelEl = $("#panel");

function syncSheetHeight(): void {
  document.documentElement.style.setProperty("--sheet-height", `${panelEl.getBoundingClientRect().height}px`);
}

function setSheetCollapsed(collapsed: boolean): void {
  panelEl.classList.toggle("collapsed", collapsed);
  requestAnimationFrame(syncSheetHeight);
}

$("#sheet-handle").addEventListener("click", () => setSheetCollapsed(!panelEl.classList.contains("collapsed")));
new ResizeObserver(syncSheetHeight).observe(panelEl);

// ---------- Larger screens: the whole panel can be tucked away to the left ----------

const PANEL_KEY = "wingtrace.panelClosed";

function setPanelOpen(open: boolean): void {
  document.body.classList.toggle("panel-closed", !open);
  $("#panel-open").hidden = open;
  try {
    localStorage.setItem(PANEL_KEY, open ? "0" : "1");
  } catch {
    // Storage unavailable (private window): the panel just opens next time.
  }
}

$("#panel-close").addEventListener("click", () => setPanelOpen(false));
$("#panel-open").addEventListener("click", () => setPanelOpen(true));
try {
  if (localStorage.getItem(PANEL_KEY) === "1") setPanelOpen(false);
} catch {
  // As above.
}

const topPanel = new TopPanel($("#top-body"), (hex) => void select(hex, true));

function showTab(name: string): void {
  setSheetCollapsed(false);
  if (document.body.classList.contains("panel-closed")) setPanelOpen(true);
  document.querySelectorAll<HTMLElement>(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll<HTMLElement>(".tab-body").forEach((b) => (b.hidden = b.id !== `tab-${name}`));
  topPanel.setActive(name === "top");
}
document.querySelectorAll<HTMLElement>(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab!)));

// ---------- Live data ----------

const poller = new LivePoller(
  viewer,
  (resp, inView) => {
    lastLive = resp;
    lastLiveAt = Date.now();
    lastInView = inView;
    liveError = false;
    aircraft.update(resp);
    if (!selected) renderIdle();
    updateStatus();
  },
  () => {
    liveError = true;
    updateStatus();
  },
);
poller.start();

function renderIdle(): void {
  panel.renderIdle({
    count: aircraft.count,
    inView: lastInView,
    typicalAgeS: lastLive?.typicalAgeS ?? null,
    regional: lastLive?.regional ?? false,
    updatedAgoS: lastLiveAt ? (Date.now() - lastLiveAt) / 1000 : null,
  });
}

function updateStatus(): void {
  const pill = $("#status");
  const card = $("#status-card");
  $("#status-wrap").hidden = false;
  if (liveError && !lastLive) {
    pill.innerHTML = `<span class="dot off"></span>Offline`;
    card.innerHTML = `<div class="status-title">Can't reach the flight data server</div>
      <p>Aircraft will appear as soon as it's back. If you run WingTrace yourself, check that its window is still open.</p>`;
    return;
  }
  if (!lastLive) {
    pill.innerHTML = `<span class="dot warn"></span>Connecting…`;
    card.innerHTML = `<div class="status-title">Connecting to the flight data server…</div>`;
    return;
  }
  const count = fmtInt(aircraft.count);
  const age = lastLive.typicalAgeS ? fmtDuration(Math.max(60, lastLive.typicalAgeS)) : "a few minutes";
  pill.innerHTML = `<span class="dot ${liveError ? "warn" : ""}"></span><b>Live</b><span class="muted">${count}</span><svg class="chev" viewBox="0 0 24 24" aria-hidden="true"><path d="m7 10 5 5 5-5"/></svg>`;
  // Be upfront about freshness: zoomed in it's seconds, the whole world minutes.
  card.innerHTML = `
    <div class="status-title">${lastInView ? "Live in this area" : "Live around the world"}</div>
    <dl class="status-rows">
      <div><dt>Aircraft${lastInView ? " in view" : ""}</dt><dd>${count}</dd></div>
      <div><dt>Positions</dt><dd>${lastInView ? "every few seconds" : `typically ~${age} old`}</dd></div>
      <div><dt>Last update</dt><dd>${fmtAgo((Date.now() - lastLiveAt) / 1000)}</dd></div>
    </dl>
    <p>${
      lastInView
        ? "Zoomed in, aircraft in view refresh every few seconds."
        : "Free data sources limit how often the whole world can be refreshed. Zoom in for second-by-second positions."
    }${liveError ? " The last update failed; retrying." : ""}</p>`;
}
window.setInterval(updateStatus, 5000);

// ---------- Selection ----------

async function select(hex: string, flyTo = false): Promise<void> {
  if (selected !== hex) {
    setFollow(false);
    routes.clear();
  }
  selected = hex;
  aircraft.select(hex);
  showTab("live");
  setHash(`ac=${hex}`);
  if (!selectedDetails || selectedDetails.aircraft.hex !== hex) panel.renderLoading();
  await refreshDetails(flyTo);
}

function deselect(): void {
  selected = null;
  selectedDetails = null;
  window.clearTimeout(detailsTimer);
  detailsAbort?.abort();
  setFollow(false);
  aircraft.select(null);
  routes.clear();
  renderIdle();
  setHash("");
}

async function refreshDetails(flyTo = false): Promise<void> {
  const hex = selected;
  if (!hex) return;
  window.clearTimeout(detailsTimer);
  detailsAbort?.abort();
  const controller = new AbortController();
  detailsAbort = controller;
  try {
    const d = await api.aircraft(hex, controller.signal);
    if (selected !== hex) return;
    selectedDetails = d;
    const a = d.aircraft;
    const flags = (a.onGround ? FLAG_GROUND : 0) | (a.estimated ? FLAG_ESTIMATED : 0) | (a.source === "mlat" ? FLAG_MLAT : 0) | (a.emergency ? FLAG_EMERGENCY : 0);
    aircraft.updateOne([a.hex, a.lat, a.lon, a.altFt, a.track, a.gs, a.ageS, a.callsign ?? "", a.type ?? "", flags, a.category ?? "", a.vrate], d.now);
    aircraft.select(hex);
    drawSelectedRoute();
    panel.renderAircraft(d, following);
    if (flyTo) flyToAircraft(hex);
  } catch (err) {
    if (controller.signal.aborted) return;
    if (err instanceof ApiError && err.status === 404) {
      panel.renderMissing();
      return;
    }
  }
  if (selected === hex) detailsTimer = window.setTimeout(() => void refreshDetails(), 4000);
}

function drawSelectedRoute(): void {
  const d = selectedDetails;
  if (!d) return;
  const hex = d.aircraft.hex;
  const plausible = d.route?.plausible ?? false;
  routes.show({
    track: d.track,
    current: () => aircraft.positionOf(hex),
    estimated: d.aircraft.estimated,
    origin: plausible ? d.route!.origin : null,
    destination: plausible ? d.route!.destination : null,
    planned: plausible && !d.aircraft.onGround,
  });
}

function flyToAircraft(hex: string): void {
  const target = aircraft.cartesianOf(hex);
  if (!target) return;
  viewer.camera.flyToBoundingSphere(new BoundingSphere(target, 1000), {
    duration: 1.8,
    offset: new HeadingPitchRange(viewer.camera.heading, -Math.PI / 3.2, 700_000),
  });
}

// ---------- Follow camera ----------

function setFollow(on: boolean): void {
  if (on === following) return;
  following = on;
  if (on && selected) {
    const target = aircraft.cartesianOf(selected);
    if (target) {
      viewer.camera.lookAt(target, new HeadingPitchRange(viewer.camera.heading, -Math.PI / 6, 60_000));
    }
  } else {
    viewer.camera.lookAtTransform(Matrix4.IDENTITY);
  }
  if (selectedDetails) panel.renderAircraft(selectedDetails, following);
}

viewer.scene.preRender.addEventListener(() => {
  if (!following || !selected) return;
  const target = aircraft.cartesianOf(selected);
  if (!target) return;
  // Keep the camera's offset (which the user can orbit) relative to the moving aircraft.
  const offset = Cartesian3.clone(viewer.camera.position);
  viewer.camera.lookAtTransform(Transforms.eastNorthUpToFixedFrame(target), offset);
});

// ---------- Mouse ----------

const handler = new ScreenSpaceEventHandler(viewer.scene.canvas);
handler.setInputAction((e: ScreenSpaceEventHandler.PositionedEvent) => {
  const hex = aircraft.pick(e.position);
  if (hex) void select(hex);
  else if (selected && !following) deselect();
}, ScreenSpaceEventType.LEFT_CLICK);

// Hover labels need a "pick", which re-draws the whole scene off-screen, so it is the most
// expensive thing the mouse can trigger. Never pick while dragging the globe, and at most
// every 120 ms once the mouse rests.
let hoverTimer: number | undefined;
let dragging = false;
viewer.scene.canvas.addEventListener("pointerdown", () => {
  dragging = true;
  window.clearTimeout(hoverTimer);
  aircraft.hover(null);
});
window.addEventListener("pointerup", () => (dragging = false));
handler.setInputAction((e: ScreenSpaceEventHandler.MotionEvent) => {
  window.clearTimeout(hoverTimer);
  if (dragging) return;
  const position = e.endPosition.clone();
  hoverTimer = window.setTimeout(() => {
    const hex = aircraft.pick(position);
    aircraft.hover(hex);
    viewer.scene.canvas.style.cursor = hex ? "pointer" : "";
  }, 120);
}, ScreenSpaceEventType.MOUSE_MOVE);

// ---------- Map controls ----------

$("#btn-home").addEventListener("click", () => {
  setFollow(false);
  flyHome(viewer);
});

const settings = $("#settings");
$("#btn-settings").addEventListener("click", (e) => {
  e.stopPropagation();
  settings.hidden = !settings.hidden;
  $("#btn-settings").classList.toggle("active", !settings.hidden);
});
document.addEventListener("pointerdown", (e) => {
  if (!settings.hidden && !(e.target as HTMLElement).closest(".map-controls")) {
    settings.hidden = true;
    $("#btn-settings").classList.remove("active");
  }
});

$("#alt-scale").addEventListener("click", (e) => {
  const btn = (e.target as HTMLElement).closest<HTMLButtonElement>("button[data-value]");
  if (!btn) return;
  const value = btn.dataset.value!;
  scale.setMode(value === "auto" ? "auto" : (Number(value) as AltitudeMode));
  $("#alt-scale").querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
});
$<HTMLInputElement>("#opt-labels").addEventListener("change", (e) => placeLabels.setVisible((e.target as HTMLInputElement).checked));
const statusWrap = $("#status-wrap");
$("#status").addEventListener("click", (e) => {
  e.stopPropagation();
  statusWrap.classList.toggle("open");
});
document.addEventListener("pointerdown", (e) => {
  if (!(e.target as HTMLElement).closest("#status-wrap")) statusWrap.classList.remove("open");
});
$("#btn-zoom-in").addEventListener("click", () => zoomBy(viewer, 0.5));
$("#btn-zoom-out").addEventListener("click", () => zoomBy(viewer, 2));

// ---------- Shareable links: #ac=<hex> selects an aircraft ----------

function openFromHash(): void {
  const hex = new URLSearchParams(location.hash.slice(1)).get("ac");
  if (hex && /^~?[0-9a-f]{6}$/i.test(hex)) void select(hex.toLowerCase(), true);
}

function setHash(value: string): void {
  const url = value ? `#${value}` : location.pathname + location.search;
  if (location.hash.slice(1) !== value) history.replaceState(null, "", url);
}

window.addEventListener("hashchange", openFromHash);
openFromHash();

// ---------- Altitude legend (same colours as the aircraft and routes) ----------

const legendStops = [0, 4000, 10000, 20000, 30000, 40000];
$("#alt-legend").innerHTML = `
  <div class="legend-head"><span>Altitude</span><span>feet</span></div>
  <div class="legend-bar" style="background: linear-gradient(90deg, ${legendStops.map((ft, i) => `${altitudeCss(ft)} ${i * 20}%`).join(", ")})"></div>
  <div class="legend-ticks"><span>Ground</span><span>10k</span><span>20k</span><span>30k</span><span>40k+</span></div>`;

renderIdle();
updateStatus();

// ---------- Loading screen: fades once the globe has drawn its first frame ----------

const removeBootListener = viewer.scene.postRender.addEventListener(() => {
  removeBootListener();
  // The coarse Earth imagery ships with the site, so it's on screen a moment later.
  window.setTimeout(() => {
    const boot = document.getElementById("boot");
    boot?.classList.add("done");
    window.setTimeout(() => boot?.remove(), 500);
  }, 400);
});

// Handles for debugging and automated checks in development builds only.
if (import.meta.env.DEV) {
  Object.assign(window, { __fg: { viewer, aircraft, routes, poller, select, deselect, setFollow } });
}
