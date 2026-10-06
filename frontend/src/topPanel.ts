import { api, type TopEntry, type TopLists } from "./api";
import { escapeHtml, fmtAlt, fmtInt, fmtSpeed } from "./format";

type ListName = "longest" | "shortest" | "fastest" | "highest";

const LISTS: { name: ListName; label: string; blurb: string }[] = [
  { name: "longest", label: "Longest", blurb: "Longest routes in the air right now" },
  { name: "shortest", label: "Shortest", blurb: "Shortest routes in the air right now" },
  { name: "fastest", label: "Fastest", blurb: "Highest ground speed right now (tailwinds help)" },
  { name: "highest", label: "Highest", blurb: "Highest altitude right now" },
];

const REFRESH_MS = 60_000;

/** The "Top 10" tab: record holders among the flights in the air right now. */
export class TopPanel {
  private readonly root: HTMLElement;
  private readonly onSelect: (hex: string) => void;
  private list: ListName = "longest";
  private data: TopLists | null = null;
  private timer: number | undefined;
  private error = false;

  constructor(root: HTMLElement, onSelect: (hex: string) => void) {
    this.root = root;
    this.onSelect = onSelect;
    root.addEventListener("click", (e) => {
      const target = e.target as HTMLElement;
      const tab = target.closest<HTMLElement>("[data-list]");
      if (tab) {
        this.list = tab.dataset.list as ListName;
        this.render();
        return;
      }
      const row = target.closest<HTMLElement>("[data-hex]");
      if (row) this.onSelect(row.dataset.hex!);
    });
  }

  /** Called when the tab is shown or hidden: refresh while visible only. */
  setActive(active: boolean): void {
    window.clearTimeout(this.timer);
    if (active) void this.load();
  }

  private async load(): Promise<void> {
    if (!this.data) this.render();
    try {
      this.data = await api.top();
      this.error = false;
    } catch {
      this.error = true;
    }
    this.render();
    this.timer = window.setTimeout(() => void this.load(), REFRESH_MS);
  }

  private render(): void {
    const current = LISTS.find((l) => l.name === this.list)!;
    const tabs = LISTS.map(
      (l) => `<button class="${l.name === this.list ? "active" : ""}" data-list="${l.name}">${l.label}</button>`,
    ).join("");
    let body: string;
    if (!this.data) {
      body = `<p class="muted small">${this.error ? "Couldn't load the lists. Retrying…" : "Loading…"}</p>`;
    } else {
      const rows = this.data[this.list];
      body = rows.length
        ? `<div class="list">${rows.map((e, i) => this.row(e, i + 1)).join("")}</div>`
        : `<p class="muted small">Nothing to show yet: the world view is still filling up.</p>`;
    }
    const footnote = this.data
      ? `Out of ${fmtInt(this.data.flights)} flights in the air, ${fmtInt(this.data.flightsWithRoutes)} with a known route. Updated every minute.`
      : "";
    this.root.innerHTML = `
      <h2 class="panel-title">Top 10</h2>
      <p class="lead">${escapeHtml(current.blurb)}</p>
      <div class="segmented top-tabs">${tabs}</div>
      ${body}
      <p class="faint small" style="margin-top:12px">${escapeHtml(footnote)}</p>`;
  }

  private row(e: TopEntry, rank: number): string {
    const r = e.route;
    const name = e.callsign ?? e.hex.toUpperCase();
    const where = r
      ? `${escapeHtml(r.origin.iata ?? r.origin.icao)} → ${escapeHtml(r.destination.iata ?? r.destination.icao)} · ${escapeHtml(r.origin.city)} to ${escapeHtml(r.destination.city)}`
      : escapeHtml([e.airline, e.type].filter(Boolean).join(" · "));
    const value =
      this.list === "fastest" ? fmtSpeed(e.gs) : this.list === "highest" ? fmtAlt(e.altFt) : r ? `${fmtInt(r.km)} km` : "";
    return `
      <button class="list-row top-row" data-hex="${escapeHtml(e.hex)}">
        <span class="rank">${rank}</span>
        <span class="top-main">
          <span class="row-title">${escapeHtml(name)}${e.type ? ` <span class="faint small">${escapeHtml(e.type)}</span>` : ""}</span>
          <span class="row-sub">${where}</span>
          ${r ? `<span class="mini-progress"><span style="width:${Math.round(r.progress * 100)}%"></span></span>` : ""}
        </span>
        <span class="top-value">${escapeHtml(value)}</span>
      </button>`;
  }
}
