#!/usr/bin/env node
// GRACE-2 — job-0149 evidence screenshot.
//
// Verifies the palette collision fix: panther + spoonbill + alligator layers
// with the EXACT layer_ids from scenario11_layer_colors.json (job-0148 evidence)
// must now render as 3 DISTINCT colours on the map.
//
// Bug: FNV-1a hash mapped both gbif-panther-fl and gbif-spoonbill-fl → slot 7
// (#4477FF). Fix: replaced with djb2 hash. This script uses the EXACT
// layer_ids from the evidence file to confirm the fix holds in the live app.
//
// Output:
//   1. case1_palette_fix.png        — screenshot with 3 distinctly-coloured species
//   2. dom_layer_inventory.json     — MapLibre paint properties for each layer
//                                     (proves 3 distinct fill/circle-color values)
//   3. layer_colors.json            — extracted colour per layer_id for fast CI check
//
// Geographic-correctness gate (job-0086 lesson): coordinates are real Big
// Cypress / Everglades sample positions within bbox (-81.6, 25.6, -80.5, 26.5).
//
// Usage (assumes `make run-web` or `npm run dev` is up on :5173):
//   node web/tools/screenshot_job0149_palette_fix.mjs

import { chromium } from "@playwright/test";
import { mkdir, writeFile } from "fs/promises";

const OUT_DIR = "reports/inflight/job-0149-web-20260608/evidence";
const BASE_URL = process.env.PLAYWRIGHT_BASE_URL ?? "http://localhost:5173";

// Real Big Cypress / Everglades-area sample coordinates per species.
// Within the kickoff's Big Cypress bbox (-81.6, 25.6, -80.5, 26.5).
const PANTHER_POINTS = [
  [-81.34, 26.10],
  [-81.20, 26.05],
  [-81.42, 26.20],
  [-81.15, 26.30],
  [-81.50, 26.00],
];
const SPOONBILL_POINTS = [
  [-80.95, 25.85],
  [-80.88, 25.92],
  [-81.10, 25.78],
  [-80.80, 25.95],
];
const ALLIGATOR_POINTS = [
  [-81.05, 26.15],
  [-80.85, 25.75],
  [-81.30, 25.90],
  [-81.00, 26.25],
  [-80.95, 26.05],
];

function pointFc(coords, species) {
  return {
    type: "FeatureCollection",
    features: coords.map(([lng, lat], i) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [lng, lat] },
      properties: { species, observation_id: `${species}-${i}` },
    })),
  };
}

// These EXACT layer_ids mirror scenario11_layer_colors.json from job-0148:
// the ones that showed the collision (panther + spoonbill both #4477FF).
const MOCK_RESPONSES = new Map([
  [
    "https://demo.grace2.example.com/case1/gbif-panther-fl.geojson",
    pointFc(PANTHER_POINTS, "Florida panther"),
  ],
  [
    "https://demo.grace2.example.com/case1/gbif-spoonbill-fl.geojson",
    pointFc(SPOONBILL_POINTS, "Roseate spoonbill"),
  ],
  [
    "https://demo.grace2.example.com/case1/gbif-alligator-fl.geojson",
    pointFc(ALLIGATOR_POINTS, "American alligator"),
  ],
]);

async function main() {
  await mkdir(OUT_DIR, { recursive: true });

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();

  const consoleLines = [];
  page.on("console", (msg) => consoleLines.push(`[${msg.type()}] ${msg.text()}`));
  page.on("pageerror", (err) => consoleLines.push(`[pageerror] ${err.message}`));

  // Intercept demo URLs.
  await page.route("https://demo.grace2.example.com/**", (route) => {
    const body = MOCK_RESPONSES.get(route.request().url());
    if (body) {
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    } else {
      route.fulfill({ status: 404, body: "not found" });
    }
  });

  // Bypass AuthGate via addInitScript so localStorage flag is set before React
  // useState lazy initializer fires (avoids race between write and hydration).
  await context.addInitScript(() => {
    try { localStorage.setItem("grace2_anonymous_accepted", "true"); } catch { /* */ }
  });

  console.log(`[0149] loading ${BASE_URL}`);
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });

  await page.waitForSelector('[data-testid="grace2-map"]', { timeout: 15000 });
  await page.waitForFunction(
    () => typeof window.__grace2InjectSessionState === "function",
    { timeout: 15000 },
  );
  console.log("[0149] map + injection seam ready");

  // Inject Case 1 session-state using the EXACT layer_ids from job-0148 evidence.
  // These are the IDs that exhibited the collision in scenario11_layer_colors.json.
  await page.evaluate((s) => window.__grace2InjectSessionState(s), {
    loaded_layers: [
      {
        layer_id: "gbif-panther-fl",
        name: "Florida panther (GBIF)",
        layer_type: "vector",
        uri: "https://demo.grace2.example.com/case1/gbif-panther-fl.geojson",
        visible: true,
        opacity: 1.0,
        style_preset: null,
        z_index: 1,
      },
      {
        layer_id: "gbif-spoonbill-fl",
        name: "Roseate spoonbill (GBIF)",
        layer_type: "vector",
        uri: "https://demo.grace2.example.com/case1/gbif-spoonbill-fl.geojson",
        visible: true,
        opacity: 1.0,
        style_preset: null,
        z_index: 2,
      },
      {
        layer_id: "gbif-alligator-fl",
        name: "American alligator (GBIF)",
        layer_type: "vector",
        uri: "https://demo.grace2.example.com/case1/gbif-alligator-fl.geojson",
        visible: true,
        opacity: 1.0,
        style_preset: null,
        z_index: 3,
      },
    ],
  });
  console.log("[0149] injected session-state with exact job-0148 layer_ids");

  // Zoom to Big Cypress region.
  await page.evaluate(() => {
    if (typeof window.__grace2InjectMapCommand === "function") {
      window.__grace2InjectMapCommand({
        command: "zoom-to",
        args: { bbox: [-81.6, 25.6, -80.5, 26.5] },
      });
    }
  });

  // Wait for async fetches + GeoJSON parse + MapLibre render to settle.
  await page.waitForTimeout(3000);

  // Capture screenshot.
  await page.screenshot({ path: `${OUT_DIR}/case1_palette_fix.png`, fullPage: false });
  console.log(`[0149] wrote case1_palette_fix.png`);

  // Extract the MapLibre style inventory to verify 3 distinct colours.
  const inventory = await page.evaluate(() => {
    const getMap = window.__grace2GetMap;
    if (typeof getMap !== "function") return null;
    const m = getMap();
    if (!m) return null;
    const style = m.getStyle();
    const vectorLayers = style.layers
      .filter((l) => ["circle", "fill", "line", "symbol"].includes(l.type))
      .map((l) => {
        let featureCount = null;
        let firstCoord = null;
        try {
          const src = m.getSource(l.source);
          const data = src && src._data;
          if (data && data.features && data.features.length) {
            featureCount = data.features.length;
            const g = data.features[0].geometry;
            if (g.type === "Point") firstCoord = g.coordinates;
            else if (g.type === "Polygon") firstCoord = g.coordinates[0][0];
          }
        } catch (e) { /* swallow */ }
        return {
          layer_id: l.id,
          type: l.type,
          source: l.source,
          feature_count: featureCount,
          first_feature_coord: firstCoord,
          paint: l.paint,
        };
      });
    return {
      total_layers: style.layers.length,
      total_sources: Object.keys(style.sources).length,
      source_ids: Object.keys(style.sources),
      vector_layers: vectorLayers,
    };
  });

  await writeFile(`${OUT_DIR}/dom_layer_inventory.json`, JSON.stringify(inventory, null, 2));
  console.log(`[0149] wrote dom_layer_inventory.json`);

  // Extract per-layer colours and verify 3 are distinct.
  const layerColors = {};
  const speciesLayerIds = ["gbif-panther-fl", "gbif-spoonbill-fl", "gbif-alligator-fl"];
  if (inventory && inventory.vector_layers) {
    for (const vl of inventory.vector_layers) {
      if (speciesLayerIds.includes(vl.layer_id)) {
        // circle-color for point layers.
        const paint = vl.paint || {};
        layerColors[vl.layer_id] = paint["circle-color"] ?? paint["fill-color"] ?? paint["line-color"] ?? "unknown";
      }
    }
  }

  const uniqueColors = new Set(Object.values(layerColors));
  const collisionDetected = uniqueColors.size < speciesLayerIds.length;

  const colorReport = {
    layer_ids: speciesLayerIds,
    colors: layerColors,
    unique_color_count: uniqueColors.size,
    collision_detected: collisionDetected,
    verdict: collisionDetected
      ? "FAIL — palette collision remains (not all 3 species have distinct colours)"
      : "PASS — all 3 species render with distinct colours",
  };

  await writeFile(`${OUT_DIR}/layer_colors.json`, JSON.stringify(colorReport, null, 2));
  console.log(`[0149] wrote layer_colors.json`);
  console.log(`[0149] colour report:`, JSON.stringify(colorReport, null, 2));

  if (collisionDetected) {
    console.error("[0149] VERIFICATION FAILED: palette collision still present");
    process.exitCode = 1;
  } else {
    console.log("[0149] VERIFICATION PASSED: 3 distinct species colours confirmed");
  }

  await writeFile(`${OUT_DIR}/playwright_console.log`, consoleLines.join("\n"));
  await browser.close();
  console.log("[0149] done");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
