import { api } from "./api";
import { cachedPhoto, photoFor } from "./photos";
import { altitudeCss } from "./altitude";
import { airportTime, escapeHtml, fmtAgo, fmtAlt, fmtDuration, fmtInt, fmtKm, fmtSpeed, fmtTrack, fmtVrate, tzLabel } from "./format";
import { haversineM } from "./geo";
import type { AircraftDetails, Airport, SearchResult } from "./types";

const PLANE_SVG =
  '<svg class="route-plane" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5l8 2.5Z"/></svg>';

export interface LivePanelHandlers {
  selectAircraft(hex: string, flyTo: boolean): void;
  selectAirport(airport: Airport): void;
  close(): void;
  toggleFollow(): void;
}

export class LivePanel {
  private readonly body: HTMLElement;
  private readonly input: HTMLInputElement;
  private readonly results: HTMLElement;
  private readonly handlers: LivePanelHandlers;
  private searchTimer: number | undefined;
  private searchAbort: AbortController | null = null;
  private lastResult: SearchResult | null = null;
  private focusIndex = -1;

  constructor(body: HTMLElement, input: HTMLInputElement, results: HTMLElement, handlers: LivePanelHandlers) {
    this.body = body;
    this.input = input;
    this.results = results;
    this.handlers = handlers;

    input.addEventListener("input", () => {
      window.clearTimeout(this.searchTimer);
      this.searchTimer = window.setTimeout(() => void this.search(false), 220);
    });
    input.addEventListener("keydown", (e) => this.onKey(e));
    input.addEventListener("focus", () => {
      if (this.lastResult && input.value.trim().length >= 2) results.hidden = false;
    });
    document.addEventListener("pointerdown", (e) => {
      if (!(e.target as HTMLElement).closest(".search")) results.hidden = true;
    });
    body.addEventListener("click", (e) => {
      const action = (e.target as HTMLElement).closest<HTMLElement>("[data-action]")?.dataset;
      if (!action) return;
      if (action.action === "close") handlers.close();
      if (action.action === "follow") handlers.toggleFollow();
      if (action.action === "share") void this.copyLink(e.target as HTMLElement);
    });
  }

  /** Copy a link that opens this aircraft (the page URL carries #ac=<hex>). */
  private async copyLink(target: HTMLElement): Promise<void> {
    const btn = target.closest<HTMLButtonElement>("button");
    try {
      await navigator.clipboard.writeText(location.href);
      if (btn) btn.textContent = "Link copied";
    } catch {
      if (btn) btn.textContent = "Couldn't copy";
    }
    window.setTimeout(() => btn && (btn.textContent = "Copy link"), 2000);
  }

  // --- Search ---

  private async search(deep: boolean): Promise<void> {
    const q = this.input.value.trim();
    this.searchAbort?.abort();
    if (q.length < 2) {
      this.results.hidden = true;
      this.lastResult = null;
      return;
    }
    const controller = new AbortController();
    this.searchAbort = controller;
    if (deep) this.showResultsMessage("Looking further…");
    try {
      const result = await api.search(q, deep, controller.signal);
      if (controller.signal.aborted) return;
      this.lastResult = result;
      this.renderResults(result, deep);
    } catch {
      if (!controller.signal.aborted) this.showResultsMessage("Search is unavailable right now.");
    }
  }

  private showResultsMessage(text: string): void {
    this.results.innerHTML = `<div class="result-empty">${escapeHtml(text)}</div>`;
    this.results.hidden = false;
  }

  private renderResults(r: SearchResult, deep: boolean): void {
    this.focusIndex = -1;
    if (!r.aircraft.length && !r.airports.length) {
      this.showResultsMessage(deep ? "Nothing found in the air right now." : "No live match. Press Enter to look further.");
      return;
    }
    let html = "";
    if (r.aircraft.length) {
      html += `<div class="result-group">Aircraft</div>`;
      html += r.aircraft
        .map((a) => {
          const route = a.route ? `${escapeHtml(a.route[0])} → ${escapeHtml(a.route[1])}` : "";
          const sub = [route, a.type, a.registration].filter(Boolean).map((s) => escapeHtml(s)).join(" · ");
          return `<button class="result" data-hex="${escapeHtml(a.hex)}">
            <span><div class="result-main">${escapeHtml(a.callsign ?? a.hex.toUpperCase())}</div><div class="result-sub">${sub}</div></span>
            <span class="result-sub">${escapeHtml(fmtAlt(a.altFt, a.onGround))}</span></button>`;
        })
        .join("");
    }
    if (r.airports.length) {
      html += `<div class="result-group">Airports</div>`;
      html += r.airports
        .map(
          (a, i) => `<button class="result" data-airport="${i}">
            <span><div class="result-main">${escapeHtml(a.name)}</div><div class="result-sub">${escapeHtml([a.city, a.country].filter(Boolean).join(", "))}</div></span>
            <span class="result-sub">${escapeHtml(a.iata ?? a.icao)}</span></button>`,
        )
        .join("");
    }
    this.results.innerHTML = html;
    this.results.hidden = false;
    this.results.querySelectorAll<HTMLButtonElement>(".result").forEach((btn) =>
      btn.addEventListener("click", () => this.choose(btn)),
    );
  }

  private choose(btn: HTMLElement): void {
    this.results.hidden = true;
    if (btn.dataset.hex) this.handlers.selectAircraft(btn.dataset.hex, true);
    else if (btn.dataset.airport && this.lastResult) {
      this.handlers.selectAirport(this.lastResult.airports[Number(btn.dataset.airport)]);
    }
    this.input.blur();
  }

  private onKey(e: KeyboardEvent): void {
    const items = [...this.results.querySelectorAll<HTMLElement>(".result")];
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (!items.length) return;
      e.preventDefault();
      this.focusIndex = (this.focusIndex + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      items.forEach((el, i) => el.classList.toggle("focused", i === this.focusIndex));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (this.focusIndex >= 0 && items[this.focusIndex]) this.choose(items[this.focusIndex]);
      else if (items.length === 1) this.choose(items[0]);
      else if (!this.lastResult?.aircraft.length) void this.search(true);
    } else if (e.key === "Escape") {
      this.results.hidden = true;
      this.input.blur();
    }
  }

  // --- Body ---

  renderIdle(info: { count: number; inView: boolean; typicalAgeS: number | null; regional: boolean; updatedAgoS: number | null }): void {
    const freshness = info.inView
      ? "Positions in view refresh every few seconds"
      : info.typicalAgeS !== null
        ? `Positions typically ${fmtDuration(Math.max(60, info.typicalAgeS))} old · zoom in for live`
        : "Filling up the world view…";
    this.body.innerHTML = `
      <div class="card">
        <div class="muted small">${info.inView ? "Aircraft in view" : "Aircraft on the globe"}</div>
        <div class="big-number">${fmtInt(info.count)}</div>
        <div class="muted small">${escapeHtml(freshness)}${
          info.updatedAgoS !== null ? ` · updated ${escapeHtml(fmtAgo(info.updatedAgoS))}` : ""
        }</div>
      </div>
      <div class="section-title">Tips</div>
      <ul class="tips">
        <li>Click any plane to see where it came from and where it's going, in 3D.</li>
        <li>Zoom in to see every aircraft in an area, including small planes and helicopters.</li>
        <li>Hold <b>Ctrl</b> and drag (or use two fingers) to tilt the view.</li>
        <li>Faded planes are estimated: they're out of receiver range, usually over an ocean.</li>
      </ul>`;
  }

  renderLoading(): void {
    this.body.innerHTML = `<div class="card muted">Loading aircraft…</div>`;
  }

  renderMissing(): void {
    this.body.innerHTML = `
      <div class="card">
        <div class="ac-head"><div><div class="ac-callsign">Not tracked</div>
        <div class="ac-airline">This aircraft isn't being received right now.</div></div>
        <button class="close-btn" data-action="close" aria-label="Close">×</button></div>
      </div>`;
  }

  renderAircraft(d: AircraftDetails, following: boolean): void {
    const a = d.aircraft;
    const name = a.callsign ?? a.registration ?? a.hex.toUpperCase();
    const airline = d.airline?.name ?? (a.callsign ? "" : "Private or unidentified");
    const chips = [
      d.flightNumber ? `<span class="chip">${escapeHtml(d.flightNumber)}</span>` : "",
      a.type ? `<span class="chip accent">${escapeHtml(a.type)}</span>` : "",
      a.emergency ? `<span class="chip danger">Squawk ${escapeHtml(a.squawk)}</span>` : "",
    ].join("");

    this.body.innerHTML = `
      <div class="ac-head">
        <div class="ac-title">
          <div class="ac-callsign">${escapeHtml(name)} ${chips}</div>
          <div class="ac-airline">${escapeHtml(airline)}</div>
        </div>
        <button class="close-btn" data-action="close" aria-label="Close">×</button>
      </div>
      <div class="photo" data-photo="${escapeHtml(a.hex)}">${this.photoHtml(a.hex)}</div>
      ${this.sourceStrip(d)}
      ${this.routeCard(d)}
      <div class="actions">
        <button class="btn ${following ? "on" : ""}" data-action="follow">${following ? "Following" : "Follow"}</button>
        <button class="btn primary" data-action="share">Copy link</button>
      </div>
      <div class="stats">
        <div class="stat"><div class="stat-label">Altitude</div><div class="stat-value" style="color:${altitudeCss(a.onGround ? null : a.altFt)}">${escapeHtml(fmtAlt(a.altFt, a.onGround))}</div></div>
        <div class="stat"><div class="stat-label">Ground speed</div><div class="stat-value">${escapeHtml(fmtSpeed(a.gs))}</div></div>
        <div class="stat"><div class="stat-label">Vertical speed</div><div class="stat-value">${escapeHtml(fmtVrate(a.vrate))}</div></div>
        <div class="stat"><div class="stat-label">Track</div><div class="stat-value">${escapeHtml(fmtTrack(a.track))}</div></div>
      </div>
      <div class="card">
        <dl class="kv">
          <dt>Aircraft</dt><dd>${escapeHtml(d.model ?? a.type ?? "Unknown")}</dd>
          <dt>Registration</dt><dd>${escapeHtml(a.registration ?? "—")}</dd>
          <dt>ICAO 24-bit</dt><dd>${escapeHtml(a.hex.toUpperCase())}</dd>
          <dt>Squawk</dt><dd>${escapeHtml(a.squawk ?? "—")}</dd>
        </dl>
      </div>
      <p class="faint small">Live data from adsb.lol and adsb.fi volunteers.</p>`;

    if (cachedPhoto(a.hex) === undefined) {
      void photoFor(a.hex, a.registration).then(() => {
        const slot = this.body.querySelector<HTMLElement>(`[data-photo="${a.hex}"]`);
        if (slot) slot.innerHTML = this.photoHtml(a.hex);
      });
    }
  }

  /** Photo with the credit and link Planespotters asks for; an empty slot collapses. */
  private photoHtml(hex: string): string {
    const photo = cachedPhoto(hex);
    if (!photo) return "";
    return `
      <a href="${escapeHtml(photo.link)}" target="_blank">
        <img src="${escapeHtml(photo.src)}" alt="Photo of this aircraft" width="${photo.width}" height="${photo.height}" />
      </a>
      <a class="photo-credit" href="${escapeHtml(photo.link)}" target="_blank">© ${escapeHtml(photo.photographer)} · Planespotters.net</a>`;
  }

  /** How we know where it is, like FR24's "Tracked via satellite" strip. */
  private sourceStrip(d: AircraftDetails): string {
    const a = d.aircraft;
    let text: string;
    let tone = "";
    if (a.onGround) {
      text = "On the ground";
    } else if (a.estimated) {
      text = "Estimated position: out of receiver range, moving along its route";
      tone = "warn";
    } else if (a.ageS > 120) {
      text = `Last received ${fmtAgo(a.ageS)}`;
      tone = "warn";
    } else {
      const how = a.source === "mlat" ? "multilateration (MLAT)" : a.source === "adsb" ? "ADS-B" : "radar data";
      text = `Tracked via ${how} · ${fmtAgo(a.ageS)}`;
    }
    return `<div class="source-strip ${tone}"><span class="source-dot"></span>${escapeHtml(text)}</div>`;
  }

  private routeCard(d: AircraftDetails): string {
    const a = d.aircraft;
    const r = d.route;
    const dep = d.tookOffFrom;
    if (!r || !r.plausible) {
      const why = r?.conflict
        ? `Route data for this callsign doesn't match: it was ${r.conflict}.`
        : r
          ? "Route data for this callsign doesn't match where the aircraft is."
          : "No route is known for this callsign.";
      const at = d.departedAt && dep ? ` at ${escapeHtml(airportTime(d.departedAt, dep.tz))}` : "";
      const seen = dep
        ? `<div class="small" style="margin-top:6px">Departed <b>${escapeHtml(dep.iata ?? dep.icao)}</b> · ${escapeHtml(dep.city ?? dep.name)}${at}</div>`
        : "";
      return `<div class="card"><div class="muted">Destination unknown</div>${seen}<div class="faint small" style="margin-top:4px">${escapeHtml(why)}</div></div>`;
    }
    const now = Date.now() / 1000;
    const flown = haversineM(r.origin.lat, r.origin.lon, a.lat, a.lon);
    const left = haversineM(a.lat, a.lon, r.destination.lat, r.destination.lon);
    const pct = Math.max(0, Math.min(100, (flown / Math.max(1, flown + left)) * 100));
    // Right after take-off the aircraft is still slow; assume a typical cruise speed for the
    // long part of the trip so the estimate isn't hours too late.
    const speedKt = left > 200_000 ? Math.max(a.gs ?? 0, 450) : a.gs ?? 0;
    const etaS = speedKt > 60 && !a.onGround ? left / (speedKt * 0.514444) : null;
    const end = (ap: Airport, cls: string) => `
      <div class="route-end ${cls}">
        <div class="route-code">${escapeHtml(ap.iata ?? ap.icao)}</div>
        <div class="route-city" title="${escapeHtml(ap.name)}">${escapeHtml(ap.city ?? ap.name)}</div>
        <div class="route-tz">${escapeHtml(tzLabel(ap.tz))}</div>
      </div>`;
    const departed = d.departedAt ? airportTime(d.departedAt, r.origin.tz) : "Not seen";
    const arrival = etaS !== null ? airportTime(now + etaS, r.destination.tz) : "—";
    const since = d.departedAt ? ` · ${escapeHtml(fmtDuration(now - d.departedAt))} ago` : "";
    const until = etaS !== null ? ` · in ${escapeHtml(fmtDuration(etaS))}` : "";
    const source =
      r.confidence === "confirmed"
        ? `✓ Confirmed: seen departing ${escapeHtml(r.origin.iata ?? r.origin.icao)}`
        : "Route from schedule data, matched by callsign";
    return `
      <div class="card route-card">
        <div class="route">${end(r.origin, "")}${PLANE_SVG}${end(r.destination, "dest")}</div>
        <div class="times">
          <div><div class="time-label">Departed</div><div class="time-value">${escapeHtml(departed)}</div></div>
          <div class="dest"><div class="time-label">Estimated arrival</div><div class="time-value">${escapeHtml(arrival)}</div></div>
        </div>
        <div class="progress"><span style="width:${pct.toFixed(1)}%"></span><i style="left:${pct.toFixed(1)}%"></i></div>
        <div class="route-meta">
          <span>${escapeHtml(fmtKm(flown))}${since}</span>
          <span>${escapeHtml(fmtKm(left))}${until}</span>
        </div>
        <div class="route-source ${r.confidence === "confirmed" ? "ok" : ""}">${source}</div>
      </div>`;
  }
}
