/** Shapes of the backend API responses (see backend/app/api.py). */

/** [hex, lat, lon, altFt|null, track|null, gs|null, ageS, callsign, type, flags, category, vrate ft/min|null] */
export type CompactAircraft = [
  string, number, number, number | null, number | null, number | null, number, string, string, number, string,
  number | null,
];

export const FLAG_GROUND = 1;
export const FLAG_ESTIMATED = 2;
export const FLAG_MLAT = 4;
export const FLAG_EMERGENCY = 8;

export interface SweepStatus {
  cycles: number;
  lastCycleS: number | null;
  lastCycleEnd: number | null;
  typesDone: number;
}

export interface LiveResponse {
  now: number;
  /** Median age (s) of the positions in this response. */
  typicalAgeS: number | null;
  regional: boolean;
  regionAgeS: number | null;
  sweep: SweepStatus;
  maxExtrapolateS: number;
  ac: CompactAircraft[];
}

export interface Airport {
  icao: string;
  iata: string | null;
  name: string;
  city: string | null;
  country: string | null;
  lat: number;
  lon: number;
  elevationFt: number | null;
  kind: string | null;
  /** IANA time zone, e.g. "Asia/Kolkata". */
  tz: string | null;
}

export interface RouteInfo {
  callsign: string;
  origin: Airport;
  destination: Airport;
  stops: string[];
  plausible: boolean;
  /** "confirmed": we saw it take off from the origin; "schedule": from route data only. */
  confidence: "confirmed" | "schedule";
  /** Why the route was rejected, e.g. "seen taking off from DEL". */
  conflict?: string;
}

export interface Airline {
  icao: string;
  iata: string | null;
  name: string;
}

export interface AircraftState {
  hex: string;
  lat: number;
  lon: number;
  altFt: number | null;
  onGround: boolean;
  gs: number | null;
  track: number | null;
  vrate: number | null;
  callsign: string | null;
  registration: string | null;
  type: string | null;
  squawk: string | null;
  emergency: boolean;
  category: string | null;
  source: "adsb" | "mlat" | "other";
  estimated: boolean;
  ageS: number;
}

/** [unix time s, lat, lon, altFt (null = on the ground), gs kt, track deg] */
export type TrackPoint = [number, number, number, number | null, number | null, number | null];

export interface AircraftDetails {
  now: number;
  aircraft: AircraftState;
  airline: Airline | null;
  /** The flight number passengers know, e.g. "UL605" for callsign ALK605. */
  flightNumber: string | null;
  model: string | null;
  route: RouteInfo | null;
  /** The airport we saw it depart from, when its departure was in our data. */
  tookOffFrom: Airport | null;
  /** When we saw it take off (unix seconds), if we did. */
  departedAt: number | null;
  track: TrackPoint[];
}

export interface SearchResult {
  aircraft: (AircraftState & { route: [string, string] | null })[];
  airports: Airport[];
}

export interface FlightSummary {
  id: number | null;
  hex: string;
  callsign: string | null;
  registration: string | null;
  type: string | null;
  origin: string | null;
  destination: string | null;
  start: number;
  end: number;
  source: "live" | "archive";
  points: number;
}

export interface FlightSearchResult {
  flights: FlightSummary[];
  airports: Record<string, Airport | null>;
}

export interface FlightDetails {
  flight: FlightSummary;
  airline: Airline | null;
  model: string | null;
  origin: Airport | null;
  destination: Airport | null;
  track: TrackPoint[];
}
