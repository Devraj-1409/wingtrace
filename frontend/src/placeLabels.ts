import {
  Cartesian2,
  Cartesian3,
  Color,
  DistanceDisplayCondition,
  Ellipsoid,
  EllipsoidalOccluder,
  HorizontalOrigin,
  LabelCollection,
  LabelStyle,
  PointPrimitiveCollection,
  VerticalOrigin,
  type Label,
  type PointPrimitive,
  type Viewer,
} from "cesium";
import places from "./data/places.json";

// Country and city names from Natural Earth (public domain). Drawn by us rather than baked into
// imagery, so they stay readable over NASA's view from space, at night and up close.

type Country = [name: string, lon: number, lat: number, rank: number];
type City = [name: string, lon: number, lat: number, rank: number, capital: number];

const FONT = '"Inter Variable", "Segoe UI", system-ui, sans-serif';
const OUTLINE = Color.fromCssColorString("#05070b").withAlpha(0.9);

/** Camera distance (m) beyond which a city of a given Natural Earth rank is hidden. */
function cityRange(rank: number, capital: boolean): number {
  if (rank <= 1) return 7_000_000;
  if (rank <= 2) return 4_000_000;
  return capital ? 2_500_000 : 1_500_000;
}

interface Place {
  position: Cartesian3;
  label: Label;
  dot?: PointPrimitive;
}

export class PlaceLabels {
  private readonly viewer: Viewer;
  private readonly labels: LabelCollection;
  private readonly dots: PointPrimitiveCollection;
  private readonly places: Place[] = [];
  private visible = true;
  private lastCheck = 0;

  constructor(viewer: Viewer) {
    this.viewer = viewer;
    this.labels = viewer.scene.primitives.add(new LabelCollection({ scene: viewer.scene }));
    this.dots = viewer.scene.primitives.add(new PointPrimitiveCollection());

    for (const [name, lon, lat, rank] of places.countries as Country[]) {
      const position = Cartesian3.fromDegrees(lon, lat);
      const label = this.labels.add({
        position,
        text: name,
        font: `600 ${rank <= 3 ? 12 : 11}px ${FONT}`,
        fillColor: Color.fromCssColorString("#e9ecf2").withAlpha(0.82),
        outlineColor: OUTLINE,
        outlineWidth: 2.5,
        style: LabelStyle.FILL_AND_OUTLINE,
        horizontalOrigin: HorizontalOrigin.CENTER,
        verticalOrigin: VerticalOrigin.CENTER,
        // Countries name the big picture: from space down to a regional view.
        // Only the biggest countries from space; smaller ones appear as you zoom in, so Europe doesn't pile up.
        distanceDisplayCondition: new DistanceDisplayCondition(1_200_000, rank <= 2 ? Infinity : rank <= 4 ? 9_000_000 : 5_000_000),
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      });
      this.places.push({ position, label });
    }

    for (const [name, lon, lat, rank, capital] of places.cities as City[]) {
      // Major cities and capitals only: every label costs drawing time on every frame.
      if (rank > 2 && !capital) continue;
      const position = Cartesian3.fromDegrees(lon, lat);
      const range = new DistanceDisplayCondition(0, cityRange(rank, capital === 1));
      const label = this.labels.add({
        position,
        text: name,
        font: `${rank <= 1 ? 500 : 400} 11px ${FONT}`,
        fillColor: Color.fromCssColorString("#eef1f6"),
        outlineColor: OUTLINE,
        outlineWidth: 3,
        style: LabelStyle.FILL_AND_OUTLINE,
        horizontalOrigin: HorizontalOrigin.LEFT,
        verticalOrigin: VerticalOrigin.CENTER,
        pixelOffset: new Cartesian2(7, 0),
        distanceDisplayCondition: range,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      });
      const dot = this.dots.add({
        position,
        pixelSize: 4,
        color: Color.WHITE.withAlpha(0.9),
        outlineColor: OUTLINE,
        outlineWidth: 1,
        distanceDisplayCondition: range,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      });
      this.places.push({ position, label, dot });
    }

    viewer.scene.preRender.addEventListener(() => this.hideBehindEarth());
  }

  setVisible(on: boolean): void {
    this.visible = on;
    this.labels.show = on;
    this.dots.show = on;
  }

  /** Labels ignore depth (so terrain never hides them); hide the ones on the far side ourselves. */
  private hideBehindEarth(): void {
    if (!this.visible) return;
    const t = performance.now();
    if (t - this.lastCheck < 200) return;
    this.lastCheck = t;
    const occluder = new EllipsoidalOccluder(Ellipsoid.WGS84, this.viewer.camera.positionWC);
    for (const p of this.places) {
      const show = occluder.isPointVisible(p.position);
      p.label.show = show;
      if (p.dot) p.dot.show = show;
    }
  }
}
