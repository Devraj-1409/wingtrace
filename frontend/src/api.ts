import type {
  AircraftDetails,
  FlightDetails,
  FlightSearchResult,
  LiveResponse,
  SearchResult,
} from "./types";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** Where the backend lives. Empty: same origin (development, or backend serving the site). */
const API_BASE = (import.meta.env.VITE_API_BASE ?? "").replace(/\/$/, "");

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(API_BASE + path, { signal });
  if (!res.ok) {
    let message = res.statusText;
    try {
      message = (await res.json()).detail ?? message;
    } catch {
      // not JSON
    }
    throw new ApiError(res.status, message);
  }
  return res.json() as Promise<T>;
}

function query(params: Record<string, string | number | boolean | undefined | null>): string {
  const q = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") q.set(key, String(value));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

export const api = {
  live: (bbox?: [number, number, number, number], signal?: AbortSignal) =>
    getJson<LiveResponse>(`/api/live${query({ bbox: bbox?.map((v) => v.toFixed(3)).join(",") })}`, signal),
  aircraft: (hex: string, signal?: AbortSignal) =>
    getJson<AircraftDetails>(`/api/aircraft/${encodeURIComponent(hex)}`, signal),
  search: (q: string, deep = false, signal?: AbortSignal) =>
    getJson<SearchResult>(`/api/search${query({ q, deep: deep || undefined })}`, signal),
  flights: (params: { q?: string; airport?: string; date?: string }, signal?: AbortSignal) =>
    getJson<FlightSearchResult>(`/api/flights${query(params)}`, signal),
  flight: (id: number, signal?: AbortSignal) => getJson<FlightDetails>(`/api/flights/${id}`, signal),
  liveFlight: (hex: string, signal?: AbortSignal) =>
    getJson<FlightDetails>(`/api/flights/live/${encodeURIComponent(hex)}`, signal),
  meta: (signal?: AbortSignal) => getJson<Meta>("/api/meta", signal),
  top: (signal?: AbortSignal) => getJson<TopLists>("/api/top", signal),
};

export interface TopEntry {
  hex: string;
  callsign: string | null;
  airline: string | null;
  type: string | null;
  altFt: number | null;
  gs: number | null;
  estimated: boolean;
  route: {
    origin: { icao: string; iata: string | null; city: string };
    destination: { icao: string; iata: string | null; city: string };
    km: number;
    progress: number;
  } | null;
}

export interface TopLists {
  now: number;
  longest: TopEntry[];
  shortest: TopEntry[];
  fastest: TopEntry[];
  highest: TopEntry[];
  flights: number;
  flightsWithRoutes: number;
}

export interface Meta {
  retentionDays: number;
  hideMilitary: boolean;
  /** UTC days with full-detail history imported → whether the whole day was imported. */
  historyDays: Record<string, boolean>;
  recordedFlights: { flights: number; oldest: number | null; newest: number | null };
}
