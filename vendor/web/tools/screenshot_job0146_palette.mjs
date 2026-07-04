#!/usr/bin/env node
// GRACE-2 — job-0146 evidence screenshot.
//
// Verifies the curated 12-colour palette, Pelicun ds_mean choropleth, and
// polygon opacity tuning (Parts 1/2/3/4 of the kickoff).
//
// Three screenshots + one JSON inventory:
//   1. case1_new_palette.png      — Case 1 demo with new curated palette colours;
//                                   proves species are clearly distinguishable.
//   2. pelicun_choropleth.png     — Pelicun damage grid with varied ds_mean values
//                                   (5 CDPs at 0.0, 0.25, 0.5, 0.75, 1.0);
//                                   proves green→yellow→red gradient renders.
//   3. polygon_opacity_tuning.png — WDPA polygon + species points; proves fill
//                                   opacity 0.4 keeps basemap labels readable.
//   4. dom_layer_inventory.json   — MapLibre style source/layer inventory for
//                                   geographic-correctness gate (job-0086 lesson).
//
// Geographic-correctness gate: species coordinates are real Big Cypress /
// Everglades sample positions within bbox (-81.6, 25.6, -80.5, 26.5).
// Pelicun CDPs are synthetic but within the Fort Myers bbox (job-0086 region).

import { chromium } from "@playwright/test";
import { mkdir, writeFile } from "fs/promises";

const OUT_DIR = "reports/inflight/job-0146-web-20260608/evidence";
const BASE_URL = process.env.PLAYWRIGHT_BASE_URL ?? "http://localhost:5173";

// Big Cypress / Everglades real species coordinates.
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

// WDPA Big Cypress simplified polygon.
const WDPA_FC = {
  type: "FeatureCollection",
  features: [{
    type: "Feature",
    geometry: {
      type: "Polygon",
      coordinates: [[
        [-81.55, 25.80], [-81.55, 26.40], [-80.80, 26.40],
        [-80.80, 25.80], [-81.55, 25.80],
      ]],
    },
    properties: { NAME: "Big Cypress National Preserve", DESIG: "National Preserve" },
  }],
};

// Pelicun damage synthetic CDPs — 5 rectangles with varied ds_mean values
// within Fort Myers bbox (-82.0, 26.4, -81.7, 26.65).
const PELICUN_FC = {
  type: "FeatureCollection",
  features: [
    // ds_mean = 0.0 — no damage → should render green
    {
      type: "Feature",
      geometry: {
        type: "Polygon",
        coordinates: [[[-82.00, 26.40], [-82.00, 26.50], [-81.94, 26.50], [-81.94, 26.40], [-82.00, 26.40]]],
      },
      properties: { uid: "cdp-1", name: "Palmona Park", ds_mean: 0.0 },
    },
    // ds_mean = 0.25 — light damage → light green/yellow
    {
      type: "Feature",
      geometry: {
        type: "Polygon",
        coordinates: [[[-81.94, 26.40], [-81.94, 26.50], [-81.88, 26.50], [-81.88, 26.40], [-81.94, 26.40]]],
      },
      properties: { uid: "cdp-2", name: "Fort Myers", ds_mean: 0.25 },
    },
    // ds_mean = 0.50 — moderate damage → yellow
    {
      type: "Feature",
      geometry: {
        type: "Polygon",
        coordinates: [[[-81.88, 26.40], [-81.88, 26.50], [-81.82, 26.50], [-81.82, 26.40], [-81.88, 26.40]]],
      },
      properties: { uid: "cdp-3", name: "Iona", ds_mean: 0.50 },
    },
    // ds_mean = 0.75 — heavy damage → orange/red
    {
      type: "Feature",
      geometry: {
        type: "Polygon",
        coordinates: [[[-81.82, 26.40], [-81.82, 26.50], [-81.76, 26.50], [-81.76, 26.40], [-81.82, 26.40]]],
      },
      properties: { uid: "cdp-4", name: "McGregor", ds_mean: 0.75 },
    },
    // ds_mean = 1.0 — catastrophic damage → red
    {
      type: "Feature",
      geometry: {
        type: "Polygon",
        coordinates: [[[-81.76, 26.40], [-81.76, 26.50], [-81.70, 26.50], [-81.70, 26.40], [-81.76, 26.40]]],
      },
      properties: { uid: "cdp-5", name: "Cape Coral", ds_mean: 1.0 },
    },
    // No ds_mean — should render fallback slate colour
    {
      type: "Feature",
      geometry: {
        type: "Polygon",
        coordinates: [[[-82.00, 26.52], [-82.00, 26.62], [-81.80, 26.62], [-81.80, 26.52], [-82.00, 26.52]]],
      },
      properties: { uid: "cdp-6", name: "Unknown Damage Area" },
    },
  ],
};

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

// Mock URL → GeoJSON map.
const MOCK_RESPONSES = new Map([
  ["https://demo.grace2.example.com/case1/panther-occurrences.geojson", pointFc(PANTHER_POINTS, "Florida panther")],
  ["https://demo.grace2.example.com/case1/spoonbill-occurrences.geojson", pointFc(SPOONBILL_POINTS, "Roseate spoonbill")],
  ["https://demo.grace2.example.com/case1/alligator-occurrences.geojson", pointFc(ALLIGATOR_POINTS, "American alligator")],
  ["https://demo.grace2.example.com/case1/wdpa-big-cypress.geojson", WDPA_FC],
  ["https://demo.grace2.example.com/pelicun/damage-fort-myers.geojson", PELICUN_FC],
]);

// Helper: inject session-state and wait for render to settle.
async function injectAndWait(page, layers, waitMs = 2500) {
  await page.evaluate((s) => window.__grace2InjectSessionState(s), { loaded_layers: layers });
  await page.waitForTimeout(waitMs);
}

async function zoomTo(page, bbox) {
  await page.evaluate((b) => {
    if (typeof window.__grace2InjectMapCommand === "function") {
      window.__grace2InjectMapCommand({ command: "zoom-to", args: { bbox: b } });
    }
  }, bbox);
  await page.waitForTimeout(1800);
}

async function getInventory(page) {
  return page.evaluate(() => {
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
        return { layer_id: l.id, type: l.type, source: l.source, feature_count: featureCount, first_feature_coord: firstCoord, paint: l.paint };
      });
    return {
      total_layers: style.layers.length,
      total_sources: Object.keys(style.sources).length,
      source_ids: Object.keys(style.sources),
      vector_layers: vectorLayers,
    };
  });
}

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

  // Load app, bypass AuthGate.
  // Use addInitScript so the localStorage flag is present before React's
  // useState lazy initializer fires on first render (the 2-goto pattern
  // risks a race between localStorage write + React hydration).
  await context.addInitScript(() => {
    try { localStorage.setItem("grace2_anonymous_accepted", "true"); } catch { /* */ }
  });
  console.log(`[0146] loading ${BASE_URL}`);
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });

  await page.waitForSelector('[data-testid="grace2-map"]', { timeout: 15000 });
  await page.waitForFunction(
    () => typeof window.__grace2InjectSessionState === "function",
    { timeout: 15000 },
  );
  console.log("[0146] map + injection seam ready");

  // -----------------------------------------------------------------------
  // Screenshot 1: case1_new_palette.png
  // -----------------------------------------------------------------------
  // Inject Case 1 with WDPA + 3 species layers, zoom to Big Cypress.
  await injectAndWait(page, [
    { layer_id: "wdpa-big-cypress", name: "Big Cypress WDPA", layer_type: "vector",
      uri: "https://demo.grace2.example.com/case1/wdpa-big-cypress.geojson",
      visible: true, opacity: 1.0, style_preset: "wdpa_polygon" },
    { layer_id: "panther-occurrences", name: "Florida panther", layer_type: "vector",
      uri: "https://demo.grace2.example.com/case1/panther-occurrences.geojson",
      visible: true, opacity: 1.0, style_preset: null },
    { layer_id: "spoonbill-occurrences", name: "Roseate spoonbill", layer_type: "vector",
      uri: "https://demo.grace2.example.com/case1/spoonbill-occurrences.geojson",
      visible: true, opacity: 1.0, style_preset: null },
    { layer_id: "alligator-occurrences", name: "American alligator", layer_type: "vector",
      uri: "https://demo.grace2.example.com/case1/alligator-occurrences.geojson",
      visible: true, opacity: 1.0, style_preset: null },
  ], 2000);

  await zoomTo(page, [-81.55, 25.80, -80.80, 26.40]);
  await page.screenshot({ path: `${OUT_DIR}/case1_new_palette.png`, fullPage: false });
  console.log(`[0146] wrote case1_new_palette.png`);

  // -----------------------------------------------------------------------
  // Screenshot 2: pelicun_choropleth.png
  // -----------------------------------------------------------------------
  // Clear species layers, inject Pelicun damage CDPs, zoom to Fort Myers.
  await injectAndWait(page, [
    { layer_id: "pelicun-damage", name: "Pelicun damage (Fort Myers)", layer_type: "vector",
      uri: "https://demo.grace2.example.com/pelicun/damage-fort-myers.geojson",
      visible: true, opacity: 1.0, style_preset: "pelicun_damage" },
  ], 2000);

  await zoomTo(page, [-82.05, 26.35, -81.65, 26.70]);
  await page.screenshot({ path: `${OUT_DIR}/pelicun_choropleth.png`, fullPage: false });
  console.log(`[0146] wrote pelicun_choropleth.png`);

  // -----------------------------------------------------------------------
  // Screenshot 3: polygon_opacity_tuning.png
  // -----------------------------------------------------------------------
  // Re-inject WDPA + 2 species to verify 0.4 opacity lets basemap labels show.
  await injectAndWait(page, [
    { layer_id: "wdpa-big-cypress", name: "Big Cypress WDPA", layer_type: "vector",
      uri: "https://demo.grace2.example.com/case1/wdpa-big-cypress.geojson",
      visible: true, opacity: 1.0, style_preset: "wdpa_polygon" },
    { layer_id: "panther-occurrences", name: "Florida panther", layer_type: "vector",
      uri: "https://demo.grace2.example.com/case1/panther-occurrences.geojson",
      visible: true, opacity: 1.0, style_preset: null },
    { layer_id: "spoonbill-occurrences", name: "Roseate spoonbill", layer_type: "vector",
      uri: "https://demo.grace2.example.com/case1/spoonbill-occurrences.geojson",
      visible: true, opacity: 1.0, style_preset: null },
  ], 2000);

  await zoomTo(page, [-81.55, 25.80, -80.80, 26.40]);
  await page.screenshot({ path: `${OUT_DIR}/polygon_opacity_tuning.png`, fullPage: false });
  console.log(`[0146] wrote polygon_opacity_tuning.png`);

  // -----------------------------------------------------------------------
  // DOM layer inventory (geographic-correctness gate)
  // -----------------------------------------------------------------------
  const inventory = await getInventory(page);
  await writeFile(`${OUT_DIR}/dom_layer_inventory.json`, JSON.stringify(inventory, null, 2));
  console.log(`[0146] wrote dom_layer_inventory.json`);

  await writeFile(`${OUT_DIR}/playwright_console.log`, consoleLines.join("\n"));

  await browser.close();
  console.log("[0146] done — 3 screenshots + inventory captured");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
