const nf = new Intl.NumberFormat("en-US");

export const fmtInt = (n: number) => nf.format(Math.round(n));

export function fmtAlt(altFt: number | null, onGround = false): string {
  if (onGround) return "On ground";
  if (altFt === null) return "—";
  return `${fmtInt(Math.round(altFt / 25) * 25)} ft`;
}

export const fmtSpeed = (kt: number | null) => (kt === null ? "—" : `${fmtInt(kt)} kt`);

export function fmtVrate(fpm: number | null): string {
  if (fpm === null) return "—";
  if (Math.abs(fpm) < 100) return "Level";
  return `${fpm > 0 ? "▲" : "▼"} ${fmtInt(Math.abs(Math.round(fpm / 50) * 50))} ft/min`;
}

export const fmtTrack = (deg: number | null) => (deg === null ? "—" : `${Math.round(deg).toString().padStart(3, "0")}°`);

export function fmtKm(m: number): string {
  return m < 10_000 ? `${(m / 1000).toFixed(1)} km` : `${fmtInt(m / 1000)} km`;
}

export function fmtDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? `${h} h ${m.toString().padStart(2, "0")} min` : `${m} min`;
}

export function fmtAgo(seconds: number): string {
  if (seconds < 5) return "just now";
  if (seconds < 90) return `${Math.round(seconds)} s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

export function fmtUtc(ts: number, withDate = false): string {
  const d = new Date(ts * 1000);
  const time = d.toISOString().slice(11, 16);
  return withDate ? `${d.toISOString().slice(0, 10)} ${time} UTC` : `${time} UTC`;
}

export function fmtLocal(ts: number, withDate = false): string {
  return new Date(ts * 1000).toLocaleString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    ...(withDate ? { day: "numeric", month: "short" } : {}),
  });
}

/** Clock time at an airport, in its own time zone (falls back to the visitor's). */
export function airportTime(ts: number, tz: string | null | undefined): string {
  try {
    return new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", timeZone: tz ?? undefined });
  } catch {
    return new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }
}

/** "UTC+5:30" style label for an airport's time zone. */
export function tzLabel(tz: string | null | undefined): string {
  if (!tz) return "";
  try {
    const part = new Intl.DateTimeFormat("en-US", { timeZone: tz, timeZoneName: "shortOffset" })
      .formatToParts(new Date())
      .find((p) => p.type === "timeZoneName")?.value;
    return part ? part.replace("GMT", "UTC").replace(/^UTC$/, "UTC+0") : "";
  } catch {
    return "";
  }
}

export function escapeHtml(s: string | null | undefined): string {
  return (s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}
