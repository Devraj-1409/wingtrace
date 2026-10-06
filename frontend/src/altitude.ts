import { Color, type Viewer } from "cesium";

/**
 * Altitude exaggeration. At true scale a 35,000 ft cruise is ~0.2% of the
 * Earth's radius, so from space every route would lie flat on the surface.
 * "Auto" uses true scale up close and lifts flights progressively as you zoom out.
 */
export type AltitudeMode = "auto" | 1 | 5 | 10 | 20;

const NEAR_M = 150_000;
const FAR_M = 5_000_000;
const AUTO_MAX = 10;

export class AltitudeScale {
  mode: AltitudeMode = "auto";
  private readonly viewer: Viewer;
  private readonly listeners = new Set<() => void>();
  private lastFactor = 1;

  constructor(viewer: Viewer) {
    this.viewer = viewer;
    viewer.scene.postRender.addEventListener(() => {
      const f = this.factor();
      // Tell listeners (route lines) when the factor has changed noticeably.
      if (Math.abs(f - this.lastFactor) / this.lastFactor > 0.08) {
        this.lastFactor = f;
        this.listeners.forEach((l) => l());
      }
    });
  }

  setMode(mode: AltitudeMode): void {
    this.mode = mode;
    this.lastFactor = this.factor();
    this.listeners.forEach((l) => l());
  }

  onChange(listener: () => void): void {
    this.listeners.add(listener);
  }

  factor(): number {
    if (this.mode !== "auto") return this.mode;
    const h = this.viewer.camera.positionCartographic.height;
    const t = (Math.log10(h) - Math.log10(NEAR_M)) / (Math.log10(FAR_M) - Math.log10(NEAR_M));
    return 1 + (AUTO_MAX - 1) * Math.min(1, Math.max(0, t));
  }

  /** Height in metres to draw something flying at `altFt` (ground = 0). */
  heightM(altFt: number | null): number {
    return altFt === null || altFt <= 0 ? 0 : altFt * 0.3048 * this.factor();
  }
}

// Colour by altitude, like most flight trackers: warm low, cool high.
const STOPS: [number, number, number, number][] = [
  // [feet, hue, saturation, lightness]
  [0, 18, 0.92, 0.58],
  [4_000, 36, 0.95, 0.56],
  [10_000, 56, 0.92, 0.52],
  [20_000, 110, 0.65, 0.52],
  [30_000, 186, 0.8, 0.56],
  [40_000, 265, 0.82, 0.7],
];
const GROUND = Color.fromCssColorString("#a3abb5");

export function altitudeColor(altFt: number | null, result = new Color()): Color {
  if (altFt === null) return Color.clone(GROUND, result);
  const a = Math.max(0, altFt);
  let i = 0;
  while (i < STOPS.length - 2 && a > STOPS[i + 1][0]) i++;
  const [f0, h0, s0, l0] = STOPS[i];
  const [f1, h1, s1, l1] = STOPS[i + 1];
  const t = Math.min(1, (a - f0) / (f1 - f0));
  return Color.fromHsl((h0 + (h1 - h0) * t) / 360, s0 + (s1 - s0) * t, l0 + (l1 - l0) * t, 1, result);
}

export function altitudeCss(altFt: number | null): string {
  return altitudeColor(altFt).toCssColorString();
}
