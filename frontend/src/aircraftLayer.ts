import {
  Billboard,
  BillboardCollection,
  Cartesian2,
  Cartesian3,
  Color,
  Ellipsoid,
  EllipsoidalOccluder,
  HorizontalOrigin,
  LabelCollection,
  LabelStyle,
  NearFarScalar,
  VerticalOrigin,
  type Label,
  type Viewer,
} from "cesium";
import { altitudeColor, type AltitudeScale } from "./altitude";
import { destination, KT_TO_MPS } from "./geo";
import { iconFor, sizeFor } from "./icons";
import { FLAG_EMERGENCY, FLAG_ESTIMATED, FLAG_GROUND, type CompactAircraft, type LiveResponse } from "./types";

// How far ahead to dead-reckon from the last position. Cruising aircraft fly
// straight for long stretches; low ones are turning, climbing or landing.
const MAX_EXTRAPOLATE_CRUISE_S = 15 * 60;
const MAX_EXTRAPOLATE_LOW_S = 3 * 60;
// Climbs and descents level off; only continue the vertical rate this long.
const MAX_EXTRAPOLATE_VERTICAL_S = 4 * 60;
const MAX_ALT_FT = 45_000;
const SMOOTHING_S = 1.5;
/**
 * Milliseconds per frame for applying new data and moving icons; the rest waits for the next
 * frames. A fixed time rather than a fixed count, so a phone does less per frame than a laptop.
 */
const FRAME_BUDGET_MS = 5;
const GROUND_VISIBLE_BELOW_M = 400_000;
const ICON_PX = 32;
const EMERGENCY_COLOR = Color.fromCssColorString("#ff3b30");
const SELECTED_COLOR = Color.WHITE;

interface Plane {
  hex: string;
  lat: number;
  lon: number;
  altFt: number | null;
  track: number | null;
  gs: number | null;
  vrate: number | null;
  posTime: number; // server clock, seconds
  callsign: string;
  type: string;
  flags: number;
  category: string;
  billboard: Billboard;
  /** Older than we can reliably predict from: drawn faded, like estimated positions. */
  stale: boolean;
  // Offset blended out after an update, so planes glide instead of jumping.
  corrLat: number;
  corrLon: number;
  corrStart: number;
  shownLat: number;
  shownLon: number;
}

/** Keeps a server-clock estimate so extrapolation works whatever the visitor's clock says. */
export class ServerClock {
  private offset = 0;
  private synced = false;

  sync(serverNow: number): void {
    const local = Date.now() / 1000;
    const sample = serverNow - local;
    this.offset = this.synced ? this.offset * 0.8 + sample * 0.2 : sample;
    this.synced = true;
  }

  now(): number {
    return Date.now() / 1000 + this.offset;
  }
}

export class AircraftLayer {
  readonly clock = new ServerClock();
  private readonly viewer: Viewer;
  private readonly scale: AltitudeScale;
  private readonly billboards: BillboardCollection;
  private readonly labels: LabelCollection;
  private readonly planes = new Map<string, Plane>();
  private selectedHex: string | null = null;
  private selectedLabel: Label | null = null;
  private hoverLabel: Label | null = null;
  private lastUpdate = 0;
  /** Aircraft still to be moved in the current pass. */
  private pending: Plane[] = [];
  /** A /api/live response still being applied, a slice per frame. */
  private incoming: { ac: CompactAircraft[]; serverNow: number; next: number; seen: Set<string> } | null = null;
  /**
   * Hidden icons kept for reuse. Adding or removing even one icon makes Cesium rebuild the
   * whole collection (thousands), so aircraft that leave hand their icon to the next ones.
   */
  private readonly spare: Billboard[] = [];
  private visible = true;
  private readonly scratch = new Cartesian3();
  private readonly color = new Color();

  constructor(viewer: Viewer, scale: AltitudeScale) {
    this.viewer = viewer;
    this.scale = scale;
    this.billboards = viewer.scene.primitives.add(new BillboardCollection({ scene: viewer.scene }));
    this.labels = viewer.scene.primitives.add(new LabelCollection({ scene: viewer.scene }));
    viewer.scene.preRender.addEventListener(() => this.onFrame());
  }

  get count(): number {
    return this.incoming ? Math.max(this.planes.size, this.incoming.ac.length) : this.planes.size;
  }

  setVisible(visible: boolean): void {
    this.visible = visible;
    this.billboards.show = visible;
    this.labels.show = visible;
  }

  /**
   * Apply a /api/live response: add, move and remove aircraft. It's applied a slice per frame
   * (see onFrame): thousands at once froze phones for most of a second.
   */
  update(resp: LiveResponse): void {
    this.clock.sync(resp.now);
    this.incoming = { ac: resp.ac, serverNow: resp.now, next: 0, seen: new Set() };
    // Make room for newcomers in one go (one rebuild) rather than one icon at a time.
    const missing = resp.ac.length - this.planes.size - this.spare.length;
    if (missing > 0) this.growSpare(Math.ceil(missing * 1.1));
  }

  private applyIncoming(deadline: number): void {
    const inc = this.incoming!;
    const now = this.clock.now();
    while (inc.next < inc.ac.length && performance.now() < deadline) {
      const ac = inc.ac[inc.next++];
      inc.seen.add(ac[0]);
      this.upsert(ac, inc.serverNow, now);
    }
    if (inc.next < inc.ac.length) return;
    for (const [hex, plane] of this.planes) {
      if (!inc.seen.has(hex) && hex !== this.selectedHex) this.retire(plane);
    }
    this.incoming = null;
    this.lastUpdate = 0; // start a fresh pass on the next frame
    this.pending = [];
  }

  private growSpare(n: number): void {
    for (let i = 0; i < n; i++) {
      this.spare.push(
        this.billboards.add({
          show: false,
          position: Cartesian3.ZERO,
          image: iconFor(null, ""),
          width: ICON_PX,
          height: ICON_PX,
          alignedAxis: Cartesian3.UNIT_Z,
          scaleByDistance: new NearFarScalar(2.0e5, 1.15, 1.6e7, 0.42),
        }),
      );
    }
  }

  private retire(plane: Plane): void {
    plane.billboard.show = false;
    plane.billboard.id = undefined;
    this.spare.push(plane.billboard);
    this.planes.delete(plane.hex);
  }

  /** Merge a single aircraft (e.g. from the details endpoint of the selected one). */
  updateOne(ac: CompactAircraft, serverNow: number): void {
    this.clock.sync(serverNow);
    this.upsert(ac, serverNow, this.clock.now());
  }

  private upsert(ac: CompactAircraft, serverNow: number, now: number): void {
    const [hex, lat, lon, altFt, track, gs, ageS, callsign, type, flags, category, vrate = null] = ac;
    const posTime = serverNow - ageS;
    let plane = this.planes.get(hex);
    if (!plane) {
      if (!this.spare.length) this.growSpare(1);
      const billboard = this.spare.pop()!;
      billboard.position = Cartesian3.fromDegrees(lon, lat, this.scale.heightM(flags & FLAG_GROUND ? null : altFt), undefined, this.scratch);
      billboard.image = iconFor(category, type);
      billboard.id = hex;
      billboard.show = true;
      plane = {
        hex, lat, lon, altFt, track, gs, vrate, posTime, callsign, type, flags, category, billboard,
        stale: false, corrLat: 0, corrLon: 0, corrStart: 0, shownLat: lat, shownLon: lon,
      };
      this.planes.set(hex, plane);
    } else {
      if (posTime < plane.posTime) return;
      // Remember where it is drawn now, and glide from there to the new estimate.
      const [newLat, newLon] = this.extrapolate({ lat, lon, altFt, track, gs, posTime, flags }, now);
      plane.corrLat = plane.shownLat - newLat;
      plane.corrLon = ((plane.shownLon - newLon + 540) % 360) - 180;
      plane.corrStart = now;
      if (category !== plane.category || type !== plane.type) plane.billboard.image = iconFor(category, type);
      plane.lat = lat;
      plane.lon = lon;
      plane.altFt = altFt;
      plane.track = track;
      plane.gs = gs;
      plane.vrate = vrate;
      plane.posTime = posTime;
      plane.callsign = callsign;
      plane.type = type;
      plane.flags = flags;
      plane.category = category;
    }
    plane.stale = this.isStale(plane, now);
    this.style(plane);
  }

  private extrapolationLimit(p: Pick<Plane, "altFt">): number {
    return (p.altFt ?? 0) >= 10_000 ? MAX_EXTRAPOLATE_CRUISE_S : MAX_EXTRAPOLATE_LOW_S;
  }

  private isStale(p: Plane, now: number): boolean {
    return !(p.flags & FLAG_GROUND) && now - p.posTime > this.extrapolationLimit(p);
  }

  private style(plane: Plane): void {
    const b = plane.billboard;
    const selected = plane.hex === this.selectedHex;
    altitudeColor(this.altOf(plane, this.clock.now()), this.color);
    if (plane.flags & FLAG_EMERGENCY) Color.clone(EMERGENCY_COLOR, this.color);
    if (selected) Color.clone(SELECTED_COLOR, this.color);
    this.color.alpha = plane.flags & FLAG_ESTIMATED || plane.stale ? 0.45 : 1;
    b.color = this.color;
    b.scale = (selected ? 1.5 : 1) * sizeFor(plane.category);
    b.rotation = plane.track === null ? 0 : -(plane.track * Math.PI) / 180;
    // Ground traffic only up close, where it is useful.
    b.disableDepthTestDistance = plane.flags & FLAG_GROUND ? 50_000 : 0;
  }

  private extrapolate(
    p: Pick<Plane, "lat" | "lon" | "altFt" | "track" | "gs" | "posTime" | "flags">,
    now: number,
  ): [number, number] {
    if (p.flags & FLAG_GROUND || p.track === null || !p.gs || p.gs < 40) return [p.lat, p.lon];
    const dt = Math.min(this.extrapolationLimit(p), Math.max(0, now - p.posTime));
    return destination(p.lat, p.lon, p.track, p.gs * KT_TO_MPS * dt);
  }

  /** Current drawn position (degrees and exaggerated height) of an aircraft. */
  positionOf(hex: string): { lat: number; lon: number; height: number; altFt: number | null } | null {
    const p = this.planes.get(hex);
    if (!p) return null;
    const altFt = this.altOf(p, this.clock.now());
    return { lat: p.shownLat, lon: p.shownLon, height: this.scale.heightM(altFt), altFt };
  }

  cartesianOf(hex: string, result = new Cartesian3()): Cartesian3 | null {
    const pos = this.positionOf(hex);
    return pos ? Cartesian3.fromDegrees(pos.lon, pos.lat, pos.height, undefined, result) : null;
  }

  /** Altitude now, continuing a climb or descent for a few minutes (null on the ground). */
  private altOf(p: Plane, now: number): number | null {
    if (p.flags & FLAG_GROUND || p.altFt === null) return p.flags & FLAG_GROUND ? null : p.altFt;
    if (!p.vrate || Math.abs(p.vrate) < 200) return p.altFt;
    const dt = Math.min(MAX_EXTRAPOLATE_VERTICAL_S, Math.max(0, now - p.posTime));
    return Math.min(MAX_ALT_FT, Math.max(0, p.altFt + (p.vrate * dt) / 60));
  }

  private onFrame(): void {
    if (!this.visible) return;
    const t = performance.now();
    const height = this.viewer.camera.positionCartographic.height;
    const now = this.clock.now();
    const occluder = new EllipsoidalOccluder(Ellipsoid.WGS84, this.viewer.camera.positionWC);
    const showGround = height < GROUND_VISIBLE_BELOW_M;

    // Moving thousands of icons in one frame is what makes the globe stutter. From far away an
    // aircraft moves less than a pixel in several seconds, so far views get a pass every few
    // seconds, spread over several frames; close views (few aircraft) update every frame.
    const every = height > 6_000_000 ? 3000 : height > 1_500_000 ? 600 : 0;
    const deadline = t + FRAME_BUDGET_MS;
    if (this.incoming) this.applyIncoming(deadline);
    if (!this.pending.length && t - this.lastUpdate >= every) {
      this.lastUpdate = t;
      this.pending = [...this.planes.values()];
    }
    // Up close (few aircraft) every icon moves every frame; further out, as many as fit the budget.
    while (this.pending.length && (every === 0 || performance.now() < deadline)) {
      this.reposition(this.pending.pop()!, now, occluder, showGround);
    }
    // The selected aircraft always moves smoothly (its label, route line and follow camera use it).
    const selected = this.selectedHex ? this.planes.get(this.selectedHex) : undefined;
    if (selected) this.reposition(selected, now, occluder, showGround);
    this.placeLabel(this.selectedLabel, this.selectedHex);
  }

  private reposition(p: Plane, now: number, occluder: EllipsoidalOccluder, showGround: boolean): void {
    if (this.planes.get(p.hex) !== p) return; // removed since the pass started (its icon may be reused)
    const ground = (p.flags & FLAG_GROUND) !== 0;
    p.billboard.show = showGround || !ground || p.hex === this.selectedHex;
    if (!p.billboard.show) return;
    // Skip aircraft on the far side of the Earth; they're refreshed when they come into view.
    if (p.hex !== this.selectedHex && !occluder.isPointVisible(p.billboard.position)) return;
    let [lat, lon] = this.extrapolate(p, now);
    const k = 1 - (now - p.corrStart) / SMOOTHING_S;
    if (k > 0) {
      lat += p.corrLat * k;
      lon += p.corrLon * k;
    }
    p.shownLat = lat;
    p.shownLon = lon;
    p.billboard.position = Cartesian3.fromDegrees(lon, lat, this.scale.heightM(this.altOf(p, now)), undefined, this.scratch);
    const stale = this.isStale(p, now);
    if (stale !== p.stale) {
      p.stale = stale;
      this.style(p);
    }
  }

  // --- Selection, hover and picking ---

  select(hex: string | null): void {
    const previous = this.selectedHex ? this.planes.get(this.selectedHex) : undefined;
    this.selectedHex = hex;
    if (previous) this.style(previous);
    const plane = hex ? this.planes.get(hex) : undefined;
    if (plane) this.style(plane);
    if (this.selectedLabel) {
      this.labels.remove(this.selectedLabel);
      this.selectedLabel = null;
    }
    if (plane) this.selectedLabel = this.makeLabel(plane.callsign || plane.hex.toUpperCase(), true);
    this.lastUpdate = 0;
  }

  hover(hex: string | null): void {
    if (this.hoverLabel) {
      this.labels.remove(this.hoverLabel);
      this.hoverLabel = null;
    }
    const plane = hex && hex !== this.selectedHex ? this.planes.get(hex) : undefined;
    if (!plane) return;
    const alt = plane.flags & FLAG_GROUND ? "ground" : plane.altFt !== null ? `${Math.round(plane.altFt / 100) * 100} ft` : "";
    this.hoverLabel = this.makeLabel([plane.callsign || plane.hex.toUpperCase(), plane.type, alt].filter(Boolean).join(" · "), false);
    this.placeLabel(this.hoverLabel, plane.hex);
  }

  private makeLabel(text: string, strong: boolean): Label {
    return this.labels.add({
      position: Cartesian3.ZERO,
      text,
      font: `${strong ? 600 : 500} 13px "Inter Variable", "Segoe UI", system-ui, sans-serif`,
      fillColor: Color.WHITE,
      outlineColor: Color.fromCssColorString("#05080d"),
      outlineWidth: 3,
      style: LabelStyle.FILL_AND_OUTLINE,
      showBackground: true,
      backgroundColor: Color.fromCssColorString(strong ? "#141418eb" : "#141418c8"),
      backgroundPadding: new Cartesian2(8, 5),
      horizontalOrigin: HorizontalOrigin.LEFT,
      verticalOrigin: VerticalOrigin.CENTER,
      pixelOffset: new Cartesian2(22, 0),
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    });
  }

  private placeLabel(label: Label | null, hex: string | null): void {
    if (!label || !hex) return;
    const plane = this.planes.get(hex);
    if (!plane) return;
    const position = Cartesian3.fromDegrees(plane.shownLon, plane.shownLat, this.scale.heightM(this.altOf(plane, this.clock.now())));
    label.position = position;
    // Labels ignore depth so terrain never hides them; hide them behind the Earth ourselves.
    label.show = new EllipsoidalOccluder(Ellipsoid.WGS84, this.viewer.camera.positionWC).isPointVisible(position);
  }

  /** The aircraft under a screen position, if any. */
  pick(position: Cartesian2): string | null {
    const picked = this.viewer.scene.pick(position);
    const id = picked?.id;
    return typeof id === "string" && this.planes.has(id) ? id : null;
  }

  has(hex: string): boolean {
    return this.planes.has(hex);
  }
}
