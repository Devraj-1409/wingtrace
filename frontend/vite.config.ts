import { defineConfig } from "vite";
import { viteStaticCopy } from "vite-plugin-static-copy";

// CesiumJS loads workers, images and widget styles at runtime from a static
// folder. We copy them to /cesiumStatic and point CESIUM_BASE_URL there
// (see index.html).
const cesiumSource = "node_modules/cesium/Build/Cesium";
const cesiumStaticDir = "cesiumStatic";

export default defineConfig({
  // Relative asset paths, so the build also works from a sub-path (e.g. GitHub Pages /<repo>/).
  base: "./",
  plugins: [
    viteStaticCopy({
      targets: ["Workers", "ThirdParty", "Assets", "Widgets"].map((dir) => ({
        src: `${cesiumSource}/${dir}/**/*`,
        dest: cesiumStaticDir,
        // Strip "node_modules/cesium/Build/Cesium" so files land in cesiumStatic/<dir>/...
        rename: { stripBase: 4 },
      })),
    }),
  ],
  server: {
    // Always the same address (fail rather than quietly move to another port).
    port: 5173,
    strictPort: true,
    // The backend (FastAPI) runs on :8000 in development.
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
