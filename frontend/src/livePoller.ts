import { Math as CesiumMath, type Viewer } from "cesium";
import { api } from "./api";
import type { LiveResponse } from "./types";

/** Above this camera height we ask for the whole world instead of the visible area. */
const GLOBAL_ABOVE_M = 4_000_000;

/**
 * Polls /api/live: the visible area when zoomed in (every 5 s), the whole
 * world when zoomed out (every 20 s), and straight away after the camera moves.
 */
export class LivePoller {
  private readonly viewer: Viewer;
  private readonly onData: (resp: LiveResponse, inView: boolean) => void;
  private readonly onError: (err: unknown) => void;
  private timer: number | undefined;
  private inflight: AbortController | null = null;
  private running = false;
  private moveTimer: number | undefined;
  /** Whether the last request was for the whole world. */
  private lastWasWorld = false;

  constructor(viewer: Viewer, onData: (resp: LiveResponse, inView: boolean) => void, onError: (err: unknown) => void) {
    this.viewer = viewer;
    this.onData = onData;
    this.onError = onError;
    viewer.camera.moveEnd.addEventListener(() => {
      window.clearTimeout(this.moveTimer);
      this.moveTimer = window.setTimeout(() => {
        // Turning the whole globe doesn't change the data needed; only a new area does. (Re-fetching
        // ~10,000 aircraft after every drag made phones stutter.)
        if (this.lastWasWorld && this.bbox() === undefined) return;
        this.pollNow();
      }, 350);
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && this.running) this.pollNow();
    });
  }

  start(): void {
    this.running = true;
    this.pollNow();
  }

  stop(): void {
    this.running = false;
    window.clearTimeout(this.timer);
    this.inflight?.abort();
  }

  pollNow(): void {
    if (!this.running) return;
    window.clearTimeout(this.timer);
    void this.poll();
  }

  private bbox(): [number, number, number, number] | undefined {
    const camera = this.viewer.camera;
    if (camera.positionCartographic.height > GLOBAL_ABOVE_M) return undefined;
    const rect = camera.computeViewRectangle();
    if (!rect) return undefined;
    let west = CesiumMath.toDegrees(rect.west);
    let east = CesiumMath.toDegrees(rect.east);
    let south = CesiumMath.toDegrees(rect.south);
    let north = CesiumMath.toDegrees(rect.north);
    // Pad so aircraft just outside the view are already there when you pan.
    const padLat = (north - south) * 0.15;
    let width = east - west;
    if (width < 0) width += 360;
    if (width > 300) return undefined;
    const padLon = width * 0.15;
    south = Math.max(-90, south - padLat);
    north = Math.min(90, north + padLat);
    west = ((west - padLon + 540) % 360) - 180;
    east = ((east + padLon + 540) % 360) - 180;
    return [west, south, east, north];
  }

  private async poll(): Promise<void> {
    this.inflight?.abort();
    const controller = new AbortController();
    this.inflight = controller;
    const bbox = this.bbox();
    this.lastWasWorld = bbox === undefined;
    // The whole-world view only changes every few minutes (the world sweep), so poll it less.
    let next = bbox ? 5000 : 20000;
    try {
      const resp = await api.live(bbox, controller.signal);
      this.onData(resp, bbox !== undefined);
    } catch (err) {
      if (controller.signal.aborted) return;
      this.onError(err);
      next = 15000;
    } finally {
      if (this.inflight === controller) this.inflight = null;
    }
    if (this.running && !document.hidden) this.timer = window.setTimeout(() => this.pollNow(), next);
  }
}
