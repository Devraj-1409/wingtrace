/** Great-circle helpers (degrees, metres). */

export const EARTH_RADIUS_M = 6_371_008.8;
export const KT_TO_MPS = 1852 / 3600;
export const FT_TO_M = 0.3048;

const rad = (d: number) => (d * Math.PI) / 180;
const deg = (r: number) => (r * 180) / Math.PI;

export function normalizeLon(lon: number): number {
  return ((lon + 540) % 360) - 180;
}

export function haversineM(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const dp = rad(lat2 - lat1);
  const dl = rad(lon2 - lon1);
  const a = Math.sin(dp / 2) ** 2 + Math.cos(rad(lat1)) * Math.cos(rad(lat2)) * Math.sin(dl / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(a)));
}

export function bearingDeg(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const p1 = rad(lat1);
  const p2 = rad(lat2);
  const dl = rad(lon2 - lon1);
  const y = Math.sin(dl) * Math.cos(p2);
  const x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
  return (deg(Math.atan2(y, x)) + 360) % 360;
}

/** Point reached from (lat, lon) after `distanceM` on initial bearing `bearing`. */
export function destination(lat: number, lon: number, bearing: number, distanceM: number): [number, number] {
  const d = distanceM / EARTH_RADIUS_M;
  const b = rad(bearing);
  const p1 = rad(lat);
  const sinP1 = Math.sin(p1);
  const cosP1 = Math.cos(p1);
  const sinD = Math.sin(d);
  const cosD = Math.cos(d);
  const p2 = Math.asin(sinP1 * cosD + cosP1 * sinD * Math.cos(b));
  const l2 = rad(lon) + Math.atan2(Math.sin(b) * sinD * cosP1, cosD - sinP1 * Math.sin(p2));
  return [deg(p2), normalizeLon(deg(l2))];
}

/** Point at fraction f (0..1) along the great circle from 1 to 2. */
export function interpolateGc(lat1: number, lon1: number, lat2: number, lon2: number, f: number): [number, number] {
  const d = haversineM(lat1, lon1, lat2, lon2) / EARTH_RADIUS_M;
  if (d < 1e-9) return [lat1, lon1];
  const a = Math.sin((1 - f) * d) / Math.sin(d);
  const b = Math.sin(f * d) / Math.sin(d);
  const p1 = rad(lat1);
  const l1 = rad(lon1);
  const p2 = rad(lat2);
  const l2 = rad(lon2);
  const x = a * Math.cos(p1) * Math.cos(l1) + b * Math.cos(p2) * Math.cos(l2);
  const y = a * Math.cos(p1) * Math.sin(l1) + b * Math.cos(p2) * Math.sin(l2);
  const z = a * Math.sin(p1) + b * Math.sin(p2);
  return [deg(Math.atan2(z, Math.hypot(x, y))), deg(Math.atan2(y, x))];
}
