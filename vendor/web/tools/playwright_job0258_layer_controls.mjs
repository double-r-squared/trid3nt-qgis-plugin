#!/usr/bin/env node
// GRACE-2 — job-0258 LAYER CONTROLS DEAD: live-DOM evidence run.
//
// Drives the RUNNING Vite dev server (default :5173) with REAL pointer/DOM
// interactions on the LayerPanel and asserts the MapLibre instance actually
// changed — the exact thing that was broken (panel handlers were M3
// local-reducer stubs; nothing reached the map).
//
// Per the job-0258 kickoff this is a DEV-SEAM check: layers are injected via
// the existing __grace2InjectCaseOpen seam (NO chat messages, NO Gemini).
// To guarantee zero traffic to the live agent on :8765 (the user is
// actively demoing), window.WebSocket is stubbed with an inert fake before
// the app loads — every assertion below is client-side wiring only.
//
// Checks:
//   1. opacity slider (real mouse click on the range track) →
//      map.getPaintProperty(fill-opacity) drops          [screenshot pair]
//   2. drag-reorder (real mouse drag on the dnd-kit handle) →
//      map.getStyle().layers order flips (moveLayer path)
//   3. visibility checkbox (real click) →
//      map.getLayoutProperty(visibility) === "none", then back
//
// Output: PNG screenshots + results.json in the job evidence dir.

import { chromium } from "@playwright/test";
import { mkdir, writeFile } from "fs/promises";

const OUT_DIR =
  process.argv[2] ??
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0258-web-20260610/evidence";
const BASE_URL = process.env.GRACE2_DEV_URL ?? "http://localhost:5173";

const results = { checks: [], console_errors: [] };
function check(name, pass, detail) {
  results.checks.push({ name, pass, detail });
  console.log(`${pass ? "PASS" : "FAIL"}  ${name}  ${JSON.stringify(detail)}`);
}

function polygonFc() {
  return {
    type: "FeatureCollection",
    features: [
      {
        type: "Feature",
        properties: { name: "job0258 probe polygon" },
        geometry: {
          type: "Polygon",
          coordinates: [[
            [-105, 31], [-86, 31], [-86, 44], [-105, 44], [-105, 31],
          ]],
        },
      },
    ],
  };
}

function pointsFc() {
  return {
    type: "FeatureCollection",
    features: [-100, -97, -94, -91].map((lon, i) => ({
      type: "Feature",
      properties: { name: `pt-${i}` },
      geometry: { type: "Point", coordinates: [lon, 37.5] },
    })),
  };
}

const now = new Date().toISOString();
const CASE_OPEN = {
  envelope_type: "case-open",
  session_state: {
    schema_version: "v1",
    case: {
      schema_version: "v1",
      case_id: "job0258-probe",
      title: "job-0258 layer-controls probe",
      created_at: now,
      updated_at: now,
      status: "active",
      bbox: [-106, 30, -85, 45],
    },
    chat_history: [],
    pipeline_history: [],
    current_pipeline: null,
    // Array order = MapLibre paint order (bottom→top); z_index = panel order
    // (top-first = z desc). Kept consistent: points bottom, polygon mid,
    // (raster top) so the panel list mirrors the paint stack.
    loaded_layers: [
      {
        layer_id: "job0258-points",
        name: "Probe points (bottom)",
        layer_type: "geojson",
        uri: "inline://job0258-points",
        visible: true,
        opacity: 1,
        z_index: 1,
        inline_geojson: pointsFc(),
      },
      {
        layer_id: "job0258-poly",
        name: "Probe polygon (middle)",
        layer_type: "geojson",
        uri: "inline://job0258-poly",
        visible: true,
        opacity: 1,
        z_index: 2,
        inline_geojson: polygonFc(),
      },
      {
        layer_id: "job0258-raster",
        name: "Probe raster (top)",
        layer_type: "raster",
        uri: "https://grace-2-qgis-server-425352658356.us-central1.run.app/ogc/wms?MAP=/mnt/qgs/grace2-sample.qgs&LAYERS=basemap-osm-conus",
        visible: true,
        opacity: 1,
        z_index: 3,
      },
    ],
  },
};

async function main() {
  await mkdir(OUT_DIR, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });

  await ctx.addInitScript(() => {
    try {
      localStorage.setItem("grace2_anonymous_accepted", "true");
      localStorage.setItem("grace2.leftPanelCollapsed", "false");
      localStorage.setItem("grace2.rightPanelCollapsed", "false");
      // Dark theme → CARTO raster basemap. The deployed QGIS WMS basemap is
      // 500ing as of this probe ("Layer(s) not valid" — pre-existing live
      // issue, flagged in the job-0258 report); CARTO gives the screenshots
      // a real basemap to show the opacity change against.
      localStorage.setItem("grace2.theme", "dark");
    } catch {}
    // Inert WebSocket — guarantees this probe generates ZERO traffic to the
    // live agent on :8765 while the user demos. GraceWs sees a socket that
    // never opens; its reconnect backoff idles harmlessly.
    class FakeWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSING = 2;
      static CLOSED = 3;
      constructor() { this.readyState = 3; }
      addEventListener() {}
      removeEventListener() {}
      send() {}
      close() {}
    }
    window.WebSocket = FakeWebSocket;
  });

  // The deployed QGIS Server WMS is 500ing live ("Layer(s) not valid",
  // ~2.5s per request — curl-verified, pre-existing, out of job-0258 scope).
  // Left unstubbed, the zoom-to camera animation generates dozens of those
  // slow failing tile requests and MapLibre's `idle` never fires inside the
  // probe window. Fulfill them instantly with a 1×1 transparent PNG so the
  // style can settle; the visual basemap is CARTO dark (real network).
  const TRANSPARENT_PNG = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
    "base64",
  );
  // RegExp (not glob) — glob "**host**" silently fails to match full URLs.
  await ctx.route(/grace-2-qgis-server/, (route) =>
    route.fulfill({ status: 200, contentType: "image/png", body: TRANSPARENT_PNG }),
  );

  const page = await ctx.newPage();
  page.on("console", (msg) => {
    if (msg.type() === "error") results.console_errors.push(msg.text());
  });

  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="grace2-app-shell"]', { timeout: 20000 });
  await page.waitForFunction(
    () => typeof window.__grace2InjectCaseOpen === "function" && typeof window.__grace2GetMap === "function",
    { timeout: 20000 },
  );

  // Inject the probe Case (dev seam — no chat, no agent).
  await page.evaluate((env) => window.__grace2InjectCaseOpen(env), CASE_OPEN);

  // Wait for all three layers to land on the live MapLibre instance (vector
  // adds are async + may idle-retry while basemap tiles resolve). NOT gated
  // on isStyleLoaded() — the broken QGIS WMS source can keep it flapping.
  try {
    await page.waitForFunction(
      () => {
        const m = window.__grace2GetMap?.();
        if (!m) return false;
        return (
          !!m.getLayer("job0258-points") &&
          !!m.getLayer("job0258-poly") &&
          !!m.getLayer("job0258-raster")
        );
      },
      { timeout: 30000 },
    );
  } catch (err) {
    // Diagnostic dump before failing — what DID land?
    const diag = await page.evaluate(() => {
      const m = window.__grace2GetMap?.();
      return {
        mapPresent: !!m,
        styleLoaded: m ? m.isStyleLoaded() : null,
        layers: m ? m.getStyle().layers.map((l) => l.id) : null,
        activeCase: document
          .querySelector('[data-testid="grace2-app-case-state"]')
          ?.getAttribute("data-active-case-id"),
        panelMounted: !!document.querySelector('[data-testid="grace2-layer-panel"]'),
      };
    });
    console.error("layer-wait timeout; page state:", JSON.stringify(diag, null, 2));
    throw err;
  }
  await page.waitForSelector('[data-testid="grace2-layer-panel"]', { timeout: 10000 });
  // Let initial tiles/paint settle for a clean "before" frame.
  await page.waitForTimeout(1500);

  const readMapState = () =>
    page.evaluate(() => {
      const m = window.__grace2GetMap();
      const order = m.getStyle().layers.map((l) => l.id);
      return {
        order,
        polyFillOpacity: m.getPaintProperty("job0258-poly", "fill-opacity"),
        rasterOpacity: m.getPaintProperty("job0258-raster", "raster-opacity"),
        polyVisibility: m.getLayoutProperty("job0258-poly", "visibility") ?? "visible",
      };
    });

  const before = await readMapState();
  check(
    "baseline: polygon painted at fill-opacity 0.4 (opacity 1 × POLYGON_FILL_OPACITY)",
    Math.abs((before.polyFillOpacity ?? 0) - 0.4) < 1e-6,
    { polyFillOpacity: before.polyFillOpacity },
  );
  await page.screenshot({ path: `${OUT_DIR}/01_before_opacity.png` });

  // ---- Check 1: opacity slider (REAL mouse click on the range track) ---- //
  const polySlider = page.locator('[data-layer-id="job0258-poly"] [data-testid="layer-opacity"]');
  const sliderBox = await polySlider.boundingBox();
  // Click at ~12% of the track width → value ≈ 0.12.
  await page.mouse.click(sliderBox.x + sliderBox.width * 0.12, sliderBox.y + sliderBox.height / 2);
  await page.waitForTimeout(300);
  let after1 = await readMapState();
  check(
    "opacity slider → map fill-opacity dropped",
    typeof after1.polyFillOpacity === "number" && after1.polyFillOpacity < 0.2,
    { before: before.polyFillOpacity, after: after1.polyFillOpacity },
  );

  // Raster path too (the flood-COG demo case): slide the raster layer down.
  const rasterSlider = page.locator('[data-layer-id="job0258-raster"] [data-testid="layer-opacity"]');
  const rBox = await rasterSlider.boundingBox();
  await page.mouse.click(rBox.x + rBox.width * 0.3, rBox.y + rBox.height / 2);
  await page.waitForTimeout(300);
  after1 = await readMapState();
  check(
    "opacity slider → map raster-opacity dropped (flood-COG path)",
    typeof after1.rasterOpacity === "number" && after1.rasterOpacity < 0.6,
    { before: before.rasterOpacity, after: after1.rasterOpacity },
  );
  await page.screenshot({ path: `${OUT_DIR}/02_after_opacity.png` });

  // ---- Check 2: drag-reorder (REAL mouse drag on the dnd-kit handle) ---- //
  // Panel rows are top-first: [raster, poly, points]. Drag the points row
  // (bottom) onto the raster row (top) → expected new top-first order:
  // [points, raster, poly] → on the map, points paints ABOVE raster.
  const beforeOrder = before.order;
  const srcHandle = page.locator('[data-layer-id="job0258-points"] [data-testid="layer-drag-handle"]');
  const dstHandle = page.locator('[data-layer-id="job0258-raster"] [data-testid="layer-drag-handle"]');
  const src = await srcHandle.boundingBox();
  const dst = await dstHandle.boundingBox();
  await page.mouse.move(src.x + src.width / 2, src.y + src.height / 2);
  await page.mouse.down();
  // dnd-kit PointerSensor activation distance is 4px — move in steps.
  await page.mouse.move(src.x + src.width / 2, src.y - 10, { steps: 5 });
  await page.mouse.move(dst.x + dst.width / 2, dst.y + dst.height / 2 - 6, { steps: 15 });
  await page.waitForTimeout(200);
  await page.mouse.up();
  await page.waitForTimeout(400);

  const after2 = await readMapState();
  const idxPoints = after2.order.indexOf("job0258-points");
  const idxRaster = after2.order.indexOf("job0258-raster");
  check(
    "drag-reorder → MapLibre paint order changed (points now above raster)",
    idxPoints > idxRaster && JSON.stringify(after2.order) !== JSON.stringify(beforeOrder),
    { beforeOrder, afterOrder: after2.order },
  );
  await page.screenshot({ path: `${OUT_DIR}/03_after_reorder.png` });

  // ---- Check 3: visibility checkbox (REAL click) ------------------------ //
  const polyCheckbox = page.locator('[data-layer-id="job0258-poly"] [data-testid="layer-visibility"]');
  await polyCheckbox.click();
  await page.waitForTimeout(300);
  const after3 = await readMapState();
  check(
    "visibility checkbox → map layout visibility none",
    after3.polyVisibility === "none",
    { visibility: after3.polyVisibility },
  );
  await page.screenshot({ path: `${OUT_DIR}/04_after_hide.png` });
  await polyCheckbox.click();
  await page.waitForTimeout(300);
  const after4 = await readMapState();
  check(
    "visibility checkbox → map layout visibility restored",
    after4.polyVisibility === "visible",
    { visibility: after4.polyVisibility },
  );

  await writeFile(`${OUT_DIR}/results.json`, JSON.stringify(results, null, 2));
  await browser.close();

  const failed = results.checks.filter((c) => !c.pass);
  console.log(`\n${results.checks.length - failed.length}/${results.checks.length} checks passed`);
  if (failed.length > 0) process.exit(1);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
