import {
  ArcType,
  CallbackProperty,
  Cartesian2,
  Cartesian3,
  Color,
  Ellipsoid,
  EllipsoidalOccluder,
  Entity,
  GeometryInstance,
  HorizontalOrigin,
  LabelStyle,
  Material,
  PolylineColorAppearance,
  PolylineGeometry,
  PolylineMaterialAppearance,
  Primitive,
  VerticalOrigin,
  type Viewer,
} from "cesium";
import { altitudeColor, type AltitudeScale } from "./altitude";
import { haversineM, interpolateGc } from "./geo";
import type { Airport, TrackPoint } from "./types";

/** Gaps longer than this in a recorded track (no data, e.g. over an ocean) are bridged along the great circle. */
export const GAP_S = 10 * 60;
/** Opacity of flown stretches we didn't receive and filled in (before tracking began, ocean gaps). */
const INFERRED_ALPHA = 0.55;
const LINE_WIDTH = 3.5;

type Point = [lat: number, lon: number, altFt: number | null];

export interface RouteDrawing {
  track: TrackPoint[];
  /** Live aircraft: where it is drawn now. A short line follows it from the last recorded point. */
  current?: () => { lat: number; lon: number; altFt: number | null } | null;
  /** The current position is estimated (out of receiver range): draw the line to it dashed. */
  estimated?: boolean;
  origin?: Airport | null;
  destination?: Airport | null;
  /** Draw the dashed planned remainder from the aircraft to `destination`. */
  planned?: boolean;
}

/**
 * Draws one flight: the flown path in 3D, coloured by altitude, the planned
 * remainder as a dashed great circle with a simple descent profile, and the
 * origin and destination airports. Everything is built synchronously and only
 * rebuilt when something actually changed, so nothing flickers.
 */
export class RouteLayer {
  private readonly viewer: Viewer;
  private readonly scale: AltitudeScale;
  private primitives: Primitive[] = [];
  private entities: Entity[] = [];
  private airports: { entity: Entity; position: Cartesian3 }[] = [];
  private drawing: RouteDrawing | null = null;
  private signature = "";

  constructor(viewer: Viewer, scale: AltitudeScale) {
    this.viewer = viewer;
    this.scale = scale;
    scale.onChange(() => {
      if (this.drawing) this.render(true);
    });
    viewer.scene.preRender.addEventListener(() => this.hideOccluded());
  }

  show(drawing: RouteDrawing): void {
    this.drawing = drawing;
    this.render(false);
  }

  clear(): void {
    this.drawing = null;
    this.signature = "";
    this.removeAll();
  }

  private removeAll(): void {
    for (const p of this.primitives) this.viewer.scene.primitives.remove(p);
    for (const e of this.entities) this.viewer.entities.remove(e);
    this.primitives = [];
    this.entities = [];
    this.airports = [];
  }

  private position(lat: number, lon: number, altFt: number | null, result?: Cartesian3): Cartesian3 {
    return Cartesian3.fromDegrees(lon, lat, this.scale.heightM(altFt), undefined, result);
  }

  private render(force: boolean): void {
    const d = this.drawing;
    if (!d) return;
    const last = d.track[d.track.length - 1];
    const now = d.current?.() ?? null;
    // Rebuild only when the data changed; the planned path is refreshed at most every ~10 s.
    const signature = [
      d.track.length, last?.[0], d.origin?.icao, d.destination?.icao, d.planned, d.estimated,
      now ? Math.floor(Date.now() / 10_000) : 0,
    ].join("|");
    if (!force && signature === this.signature) return;
    this.signature = signature;
    this.removeAll();

    // The part already flown: solid, coloured by altitude. Stretches we didn't receive (before we
    // started tracking it, or gaps over oceans) follow the great circle and are slightly faded.
    // Only the part still to fly is dashed.
    const received: GeometryInstance[] = [];
    const inferred: GeometryInstance[] = [];
    let segment: Point[] = [];
    const flush = () => {
      if (segment.length >= 2) received.push(this.colouredLine(segment));
      segment = [];
    };
    d.track.forEach((p, i) => {
      if (i > 0 && p[0] - d.track[i - 1][0] > GAP_S) {
        flush();
        const a = d.track[i - 1];
        inferred.push(this.colouredLine(gapPath([a[1], a[2], a[3]], [p[1], p[2], p[3]]), INFERRED_ALPHA));
      }
      segment.push([p[1], p[2], p[3]]);
    });
    flush();

    // Before we first saw it (tracking often starts mid-air): from the origin airport, climbing
    // like a real departure.
    const first: Point | null = d.track.length ? [d.track[0][1], d.track[0][2], d.track[0][3]] : now ? [now.lat, now.lon, now.altFt] : null;
    if (d.origin && first && first[2] !== null && haversineM(d.origin.lat, d.origin.lon, first[0], first[1]) > 30_000) {
      inferred.push(this.colouredLine(untrackedDeparture(d.origin, first), INFERRED_ALPHA));
    }

    if (received.length) {
      this.addPrimitive(new Primitive({
        geometryInstances: received,
        appearance: new PolylineColorAppearance({ translucent: false }),
        asynchronous: false,
      }));
    }
    if (inferred.length) {
      this.addPrimitive(new Primitive({
        geometryInstances: inferred,
        appearance: new PolylineColorAppearance({ translucent: true }),
        asynchronous: false,
      }));
    }

    if (d.current && last) {
      // A live line from the last recorded point to the moving aircraft (faded if its position
      // is estimated because no receiver is in range).
      const from = this.position(last[1], last[2], last[3]);
      const scratch = new Cartesian3();
      this.entities.push(this.viewer.entities.add({
        polyline: {
          positions: new CallbackProperty(() => {
            const c = d.current?.();
            return c ? [from, this.position(c.lat, c.lon, c.altFt, scratch)] : [from, from];
          }, false),
          width: LINE_WIDTH,
          material: altitudeColor(last[3]).withAlpha(d.estimated ? INFERRED_ALPHA : 1),
          arcType: ArcType.GEODESIC,
        },
      }));
    }

    if (d.planned && now && d.destination) {
      this.addDashed([plannedPath(now, d.destination)], Color.WHITE.withAlpha(0.75));
    }
    if (d.origin) this.airport(d.origin, "#9fb3c8");
    if (d.destination) this.airport(d.destination, "#ffffff");
  }

  private colouredLine(points: Point[], alpha = 1): GeometryInstance {
    return new GeometryInstance({
      geometry: new PolylineGeometry({
        positions: points.map(([lat, lon, alt]) => this.position(lat, lon, alt)),
        colors: points.map(([, , alt]) => altitudeColor(alt).withAlpha(alpha)),
        colorsPerVertex: true,
        width: LINE_WIDTH,
        arcType: ArcType.GEODESIC,
        vertexFormat: PolylineColorAppearance.VERTEX_FORMAT,
      }),
    });
  }

  private addDashed(paths: Point[][], color: Color): void {
    this.addPrimitive(new Primitive({
      geometryInstances: paths.map(
        (path) => new GeometryInstance({
          geometry: new PolylineGeometry({
            positions: path.map(([lat, lon, alt]) => this.position(lat, lon, alt)),
            width: 2.5,
            arcType: ArcType.GEODESIC,
            vertexFormat: PolylineMaterialAppearance.VERTEX_FORMAT,
          }),
        }),
      ),
      appearance: new PolylineMaterialAppearance({
        material: Material.fromType(Material.PolylineDashType, { color, dashLength: 16 }),
      }),
      asynchronous: false,
    }));
  }

  private addPrimitive(p: Primitive): void {
    this.primitives.push(this.viewer.scene.primitives.add(p));
  }

  private airport(a: Airport, color: string): void {
    const position = Cartesian3.fromDegrees(a.lon, a.lat, 0);
    const entity = this.viewer.entities.add({
      position,
      point: {
        pixelSize: 9,
        color: Color.fromCssColorString(color),
        outlineColor: Color.fromCssColorString("#0b1220"),
        outlineWidth: 2.5,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      label: {
        text: a.iata ?? a.icao,
        font: '600 13px "Inter Variable", "Segoe UI", system-ui, sans-serif',
        fillColor: Color.WHITE,
        outlineColor: Color.fromCssColorString("#05080d"),
        outlineWidth: 3,
        style: LabelStyle.FILL_AND_OUTLINE,
        // Below the dot, so it doesn't collide with the aircraft's label beside it.
        horizontalOrigin: HorizontalOrigin.CENTER,
        verticalOrigin: VerticalOrigin.TOP,
        pixelOffset: new Cartesian2(0, 9),
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
    });
    this.entities.push(entity);
    this.airports.push({ entity, position });
  }

  /** Airport markers ignore depth (so terrain never hides them); hide them behind the Earth ourselves. */
  private hideOccluded(): void {
    if (!this.airports.length) return;
    const occluder = new EllipsoidalOccluder(Ellipsoid.WGS84, this.viewer.camera.positionWC);
    for (const a of this.airports) a.entity.show = occluder.isPointVisible(a.position);
  }
}

/** Straight great-circle line across a data gap, altitude interpolated. */
function gapPath(a: Point, b: Point): Point[] {
  const n = Math.max(2, Math.min(64, Math.ceil(haversineM(a[0], a[1], b[0], b[1]) / 100_000)));
  return Array.from({ length: n + 1 }, (_, i) => {
    const f = i / n;
    const [lat, lon] = interpolateGc(a[0], a[1], b[0], b[1], f);
    const alt = a[2] === null || b[2] === null ? (a[2] ?? b[2]) : a[2] + (b[2] - a[2]) * f;
    return [lat, lon, alt] as Point;
  });
}

/** Great circle from the origin airport to where we first saw the aircraft, climbing ~3 nm per 1,000 ft. */
function untrackedDeparture(from: Airport, to: Point): Point[] {
  const total = haversineM(from.lat, from.lon, to[0], to[1]);
  const groundFt = from.elevationFt ?? 0;
  const targetFt = to[2] ?? groundFt;
  const climbM = Math.max(1, ((targetFt - groundFt) / 1000) * 3 * 1852);
  const n = Math.max(2, Math.min(96, Math.ceil(total / 50_000)));
  const out: Point[] = [];
  for (let i = 0; i <= n; i++) {
    const f = i / n;
    const [lat, lon] = interpolateGc(from.lat, from.lon, to[0], to[1], f);
    out.push([lat, lon, groundFt + (targetFt - groundFt) * Math.min(1, (total * f) / climbM)]);
  }
  return out;
}

/** Great circle to the destination: hold altitude, then descend ~3 nm per 1,000 ft. */
function plannedPath(from: { lat: number; lon: number; altFt: number | null }, to: Airport): Point[] {
  const total = haversineM(from.lat, from.lon, to.lat, to.lon);
  const alt = from.altFt ?? 0;
  const groundFt = to.elevationFt ?? 0;
  const descentM = Math.max(0, (alt - groundFt) / 1000) * 3 * 1852;
  const n = Math.max(2, Math.min(96, Math.ceil(total / 50_000)));
  const out: Point[] = [];
  for (let i = 0; i <= n; i++) {
    const f = i / n;
    const [lat, lon] = interpolateGc(from.lat, from.lon, to.lat, to.lon, f);
    const remaining = total * (1 - f);
    const altFt = remaining >= descentM || descentM === 0 ? alt : groundFt + (alt - groundFt) * (remaining / descentM);
    out.push([lat, lon, altFt]);
  }
  return out;
}
