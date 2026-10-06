import {
  CameraEventType,
  Cartesian3,
  Color,
  Credit,
  CreditDisplay,
  ImageryLayer,
  ImageryProvider,
  KeyboardEventModifier,
  Rectangle,
  ScreenSpaceEventType,
  SingleTileImageryProvider,
  UrlTemplateImageryProvider,
  Viewer,
  WebMercatorProjection,
} from "cesium";

/** Camera heights (m) between which Blue Marble fades out, revealing the sharper imagery. */
const BLUE_MARBLE_FADE: [number, number] = [1_500_000, 4_000_000];
/** Camera heights (m) between which the city lights fade in. */
const NIGHT_LIGHTS_FADE: [number, number] = [2_000_000, 4_000_000];
/**
 * The coarsest NASA tiles (zoom 0–2, under 1 MB) ship with the site in public/earth/, so the
 * whole globe appears as soon as the page loads; sharper tiles then stream in from NASA.
 */
const BUNDLED_MAX_LEVEL = 2;
/**
 * NASA's tiles have no data for the last sliver (up to ~0.13°) before the date line, which
 * shows as a thin dark line down the globe at 180°. That many degrees get filled in from
 * the neighbouring pixels.
 */
const DATE_LINE_GAP_DEG = 0.15;

// Satellite imagery for zoomed-in views: free, no account (CC BY-NC-SA 4.0, non-commercial use).
function eoxSentinel2(): ImageryLayer {
  return new ImageryLayer(
    new UrlTemplateImageryProvider({
      url: "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2024_3857/default/GoogleMapsCompatible/{z}/{y}/{x}.jpg",
      maximumLevel: 14,
      credit: new Credit(
        '<a href="https://s2maps.eu" target="_blank" rel="noopener">Sentinel-2 cloudless</a> by EOX IT Services GmbH ' +
          "(Contains modified Copernicus Sentinel data 2024)",
        true, // on the map itself (only while this imagery is in view), as its licence asks
      ),
    }),
  );
}

// NASA imagery is public domain (credited on the About tab), and GIBS serves it with CORS.
const GIBS = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best";

function nasaLayer(name: string, remote: string, ext: string): ImageryLayer {
  const provider = new UrlTemplateImageryProvider({ url: `${remote}/{z}/{y}/{x}.${ext}`, maximumLevel: 8 });
  const bundled = new URL(`earth/${name}/`, document.baseURI).href;
  const requestRemote = provider.requestImage.bind(provider);
  provider.requestImage = (x, y, level, request) => {
    const image =
      level <= BUNDLED_MAX_LEVEL
        ? (ImageryProvider.loadImage(provider, `${bundled}${level}/${y}/${x}.${ext}`) as ReturnType<typeof requestRemote>)
        : requestRemote(x, y, level, request);
    const west = x === 0;
    const east = x === (1 << level) - 1;
    return image && (west || east) ? image.then((tile) => fillDateLine(tile, level, west, east)) : image;
  };
  return new ImageryLayer(provider);
}

type Tile = Awaited<NonNullable<ReturnType<ImageryProvider["requestImage"]>>>;

/** Covers the no-data sliver along the date line by stretching the nearest good pixel column over it. */
async function fillDateLine(tile: Tile, level: number, west: boolean, east: boolean): Promise<Tile> {
  const drawable = tile instanceof HTMLImageElement || tile instanceof HTMLCanvasElement || tile instanceof ImageBitmap;
  if (!drawable) return tile;
  const { width, height } = tile;
  const columns = Math.min(width / 4, Math.ceil((DATE_LINE_GAP_DEG / 360) * width * 2 ** level) + 1);
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  if (!ctx) return tile;
  ctx.drawImage(tile, 0, 0);
  if (west) ctx.drawImage(canvas, columns, 0, 1, height, 0, 0, columns, height);
  if (east) ctx.drawImage(canvas, width - 1 - columns, 0, 1, height, width - columns, 0, columns, height);
  // Cesium loads tiles as bitmaps that are already upside down (WebGL can't flip bitmaps while
  // uploading them). Hand one back in the same form, or the tile would be flipped twice.
  return tile instanceof ImageBitmap ? createImageBitmap(canvas) : canvas;
}

// Cloud-free true-colour Earth with shaded relief and ocean depths: beautiful from
// space, but only ~500 m per pixel, so it fades into satellite imagery as you zoom in.
const nasaBlueMarble = () =>
  nasaLayer("blue-marble", `${GIBS}/BlueMarble_ShadedRelief_Bathymetry/default/2004-08-01/GoogleMapsCompatible_Level8`, "jpeg");

// City lights (NASA Black Marble).
const nasaBlackMarble = () =>
  nasaLayer("night-lights", `${GIBS}/VIIRS_Black_Marble/default/2016-01-01/GoogleMapsCompatible_Level8`, "png");

/**
 * Thin NASA strips (shipped with the site, from the same Blue Marble and Black Marble
 * imagery) covering 84–90° north and south, shown only beyond ~85° where web map imagery stops
 * (so the night strips don't darken the overlap twice).
 */
function polarCaps(kind: "day" | "night"): ImageryLayer[] {
  const edge = WebMercatorProjection.MaximumLatitude;
  const caps: [string, number, number, Rectangle][] = [
    ["north", 84, 90, Rectangle.fromRadians(-Math.PI, edge, Math.PI, Math.PI / 2)],
    ["south", -90, -84, Rectangle.fromRadians(-Math.PI, -Math.PI / 2, Math.PI, -edge)],
  ];
  return caps.map(([pole, south, north, shown]) => {
    const url = new URL(`earth/pole-${kind}-${pole}.jpg`, document.baseURI).href;
    return ImageryLayer.fromProviderAsync(
      SingleTileImageryProvider.fromUrl(url, { rectangle: Rectangle.fromDegrees(-180, south, 180, north) }),
      { rectangle: shown },
    );
  });
}

export interface Globe {
  viewer: Viewer;
}

export function createGlobe(container: HTMLElement, creditContainer: HTMLElement): Globe {
  // Nothing here comes from Cesium ion, so there's no ion logo to show (it's only required
  // alongside ion content). CesiumJS itself is open source (Apache 2.0, credited on About).
  CreditDisplay.cesiumCredit = undefined as unknown as Credit;

  const base = eoxSentinel2();
  const viewer = new Viewer(container, {
    baseLayer: base,
    creditContainer,
    animation: false,
    timeline: false,
    baseLayerPicker: false,
    geocoder: false,
    homeButton: false,
    sceneModePicker: false,
    navigationHelpButton: false,
    fullscreenButton: false,
    infoBox: false,
    selectionIndicator: false,
    // Edges are smoothed by the much cheaper FXAA filter below instead.
    msaaSamples: 1,
  });
  const { scene } = viewer;

  // Web map imagery stops at about 85° north and south, leaving holes at the poles.
  for (const layer of polarCaps("day")) viewer.imageryLayers.add(layer, 0);

  // Smoothness first: a cheap edge-smoothing filter instead of 4× multisampling, and at most
  // 1.25 device pixels per CSS pixel (sharper than Cesium's default on high-DPI laptop screens,
  // without the cost of full resolution).
  scene.postProcessStages.fxaa.enabled = true;
  viewer.useBrowserRecommendedResolution = false;
  viewer.resolutionScale = Math.min(1, 1.25 / (window.devicePixelRatio || 1));
  scene.globe.tileCacheSize = 300;

  // From space it looks like a photo of the Earth: NASA's colours as they are, the real sun
  // lighting a soft blue atmosphere, and city lights on the night side.
  scene.backgroundColor = Color.BLACK;
  // Shown for a moment where imagery hasn't loaded yet (Cesium's default is bright blue).
  scene.globe.baseColor = Color.BLACK;

  // Zoomed-in satellite map, toned down a little so the coloured aircraft stand out.
  base.brightness = 0.95;
  base.saturation = 0.9;

  // NASA Blue Marble from space, fading into the sharper satellite map below ~4,000 km.
  const blueMarble = nasaBlueMarble();
  viewer.imageryLayers.add(blueMarble);

  // City lights on the night side only.
  const night = nasaBlackMarble();
  const nightCaps = polarCaps("night");
  for (const layer of [...nightCaps, night]) {
    layer.dayAlpha = 0.0;
    layer.brightness = 1.8;
    viewer.imageryLayers.add(layer);
  }

  const fade = (h: number, [from, to]: [number, number]) => Math.min(1, Math.max(0, (h - from) / (to - from)));
  scene.preRender.addEventListener(() => {
    const h = viewer.camera.positionCartographic.height;
    blueMarble.alpha = fade(h, BLUE_MARBLE_FADE);
    blueMarble.show = blueMarble.alpha > 0.01;
    // Hidden completely under Blue Marble from far out: don't download it there.
    base.show = blueMarble.alpha < 0.99;
    // NASA's night lights are ~500 m per pixel: lovely from space, a blurry glare up close.
    // Below ~2,000 km the globe is evenly lit anyway, so show the sharp satellite map instead.
    night.nightAlpha = fade(h, NIGHT_LIGHTS_FADE);
    night.show = night.nightAlpha > 0.01;
    for (const cap of nightCaps) {
      cap.nightAlpha = night.nightAlpha;
      cap.show = night.show;
    }
  });

  const { globe } = scene;
  globe.enableLighting = true;
  // The atmosphere (the blue haze over the day side and the soft day/night edge) is lit
  // by the real sun rather than from the camera.
  globe.showGroundAtmosphere = true;
  globe.dynamicAtmosphereLighting = true;
  globe.dynamicAtmosphereLightingFromSun = true;
  // Day/night shading from space; below ~2,000 km everything is evenly lit so airports
  // stay readable at night. Cesium measures these from the Earth's centre.
  const R = 6_371_000;
  globe.lightingFadeOutDistance = R + 2_000_000;
  globe.lightingFadeInDistance = R + 6_000_000;
  scene.highDynamicRange = false;
  // The sun (and so the day/night line) follows real time.
  viewer.clock.shouldAnimate = true;

  // Double-click is for zooming, not Cesium's default "track entity".
  viewer.cesiumWidget.screenSpaceEventHandler.removeInputAction(ScreenSpaceEventType.LEFT_DOUBLE_CLICK);

  // Touchpad pinch arrives as Ctrl + mouse wheel: zoom the globe, not the web page.
  scene.screenSpaceCameraController.zoomEventTypes = [
    CameraEventType.RIGHT_DRAG,
    CameraEventType.WHEEL,
    CameraEventType.PINCH,
    { eventType: CameraEventType.WHEEL, modifier: KeyboardEventModifier.CTRL },
  ];
  scene.canvas.addEventListener("wheel", (e) => e.ctrlKey && e.preventDefault(), { passive: false });

  flyHome(viewer, 0);
  return { viewer };
}

/** Smoothly zoom towards (factor < 1) or away from (factor > 1) what the camera looks at. */
export function zoomBy(viewer: Viewer, factor: number): void {
  const camera = viewer.camera;
  const height = camera.positionCartographic.height;
  const total = Math.abs(height * (1 - factor));
  const steps = 12;
  let i = 0;
  const step = () => {
    if (factor < 1) camera.zoomIn(total / steps);
    else camera.zoomOut(total / steps);
    if (++i < steps) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/** A whole-globe view centred near the visitor (longitude guessed from their time zone). */
export function flyHome(viewer: Viewer, duration = 1.5): void {
  const lon = Math.max(-170, Math.min(170, (-new Date().getTimezoneOffset() / 60) * 15));
  const destination = Cartesian3.fromDegrees(lon, 22, 17_000_000);
  if (duration === 0) viewer.camera.setView({ destination });
  else viewer.camera.flyTo({ destination, duration });
}
