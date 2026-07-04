#!/usr/bin/env node
// GRACE-2 — job-0231 evidence screenshots (sprint-13: chart inline stacked preview + gallery).
//
// Captures two scenarios:
//
//   01_inline_stacks.png  — chat panel with 4 injected charts:
//                             - Charts A + B share turn_id "T1" → one stack of 2
//                             - Chart C is a singleton (null turn_id)
//                             - Chart D is a singleton (null turn_id)
//                           Shows inline ChartStack cards in the chat scroll.
//
//   02_gallery_open.png   — gallery opened by clicking the stacked turn-T1 card,
//                           showing full-viewport overlay with counter "1 / 2".
//
// Uses the dev seam __grace2InjectChartEmission (wired by App.tsx) to inject
// chart payloads without driving a live agent. Per the kickoff:
// "Playwright SCREENSHOT (UI-only, dev seam PERMITTED — this is a snapshot,
// not live verification)."
//
// vega-embed renders SVG charts; since the dev server is running locally with
// the real vega-embed package, charts render as real SVG after a short settle.

import { chromium } from "@playwright/test";
import { mkdir } from "fs/promises";

const OUT_DIR =
  process.argv[2] ??
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0231-web-20260609/evidence";
const BASE_URL = process.env.GRACE2_DEV_URL ?? "http://localhost:5173";

// ---------------------------------------------------------------------------
// Vega-Lite fixtures from job-0230 evidence
// ---------------------------------------------------------------------------

const HISTOGRAM_SPEC = {
  "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
  title: "Distribution of flood depth",
  data: {
    values: [
      { bin_label: "0–0.36", count: 180565 },
      { bin_label: "0.36–0.71", count: 47680 },
      { bin_label: "0.71–1.06", count: 49686 },
      { bin_label: "1.06–1.41", count: 3862 },
      { bin_label: "1.41–1.76", count: 1508 },
    ],
  },
  mark: { type: "bar", tooltip: true },
  encoding: {
    x: { field: "bin_label", type: "ordinal", title: "depth (m)", sort: null },
    y: { field: "count", type: "quantitative", title: "count" },
  },
  width: "container",
};

const DAMAGE_SPEC = {
  "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
  title: "Damage-state distribution",
  data: {
    values: [
      { damage_state: "DS0 None", ds_index: 0, count: 16 },
      { damage_state: "DS1 Slight", ds_index: 1, count: 3 },
      { damage_state: "DS2 Moderate", ds_index: 2, count: 1 },
      { damage_state: "DS3 Extensive", ds_index: 3, count: 0 },
      { damage_state: "DS4 Complete", ds_index: 4, count: 0 },
    ],
  },
  mark: { type: "bar", tooltip: true },
  encoding: {
    x: { field: "damage_state", type: "ordinal", title: "damage state" },
    y: { field: "count", type: "quantitative", title: "structures" },
    color: { field: "ds_index", type: "ordinal", scale: { scheme: "yellorredd" }, legend: null },
  },
  width: "container",
};

const CHOROPLETH_SPEC = {
  "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
  title: "Repair cost classes",
  data: {
    values: [
      { class_label: "0–9.3k", count: 16 },
      { class_label: "9.3k–58k", count: 4 },
    ],
  },
  mark: { type: "bar", tooltip: true },
  encoding: {
    x: { field: "class_label", type: "ordinal", title: "repair cost class" },
    y: { field: "count", type: "quantitative", title: "feature count" },
    color: { field: "class_label", type: "ordinal", scale: { scheme: "blues" }, legend: null },
  },
  width: "container",
};

const TIME_SERIES_SPEC = {
  "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
  title: "Precipitation time series",
  data: {
    values: [
      { ts: "2026-01-01", value: 12.5 },
      { ts: "2026-01-02", value: 8.3 },
      { ts: "2026-01-03", value: 34.1 },
      { ts: "2026-01-04", value: 22.7 },
    ],
  },
  mark: { type: "line" },
  encoding: {
    x: { field: "ts", type: "temporal", title: "date" },
    y: { field: "value", type: "quantitative", title: "precip (mm)" },
  },
  width: "container",
};

const CHARTS = [
  {
    chart_id: "01KTQPZ9ESAY9R17FS8BTVE0AA",
    vega_lite_spec: HISTOGRAM_SPEC,
    title: "Histogram — flood depth",
    caption: "284,580 values · min 0.01 · mean 0.338 · max 3.52 · 5 bins",
    source_layer_uri: "/tmp/flood_depth_peak.tif",
    created_turn_id: "TURN-001",   // shares a turn with chart-BB
  },
  {
    chart_id: "01KTQPZ9ESAY9R17FS8BTVE0BB",
    vega_lite_spec: DAMAGE_SPEC,
    title: "Damage-state distribution",
    caption: "20 structures · 4 damaged (DS1+) · 0 destroyed (DS4)",
    source_layer_uri: "/tmp/fort_myers_damage.fgb",
    created_turn_id: "TURN-001",   // same turn as chart-AA → renders as a 2-chart stack
  },
  {
    chart_id: "01KTQPZ9ESAY9R17FS8BTVE0CC",
    vega_lite_spec: CHOROPLETH_SPEC,
    title: "Choropleth legend — repair cost",
    caption: "2 quantile classes · 20 features",
    source_layer_uri: "/tmp/fort_myers_damage.fgb",
    created_turn_id: null,         // singleton
  },
  {
    chart_id: "01KTQPZ9ESAY9R17FS8BTVE0DD",
    vega_lite_spec: TIME_SERIES_SPEC,
    title: "Precipitation time series",
    caption: "4 data points",
    source_layer_uri: null,
    created_turn_id: null,         // singleton
  },
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function makeContext(browser) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await ctx.addInitScript(() => {
    try { localStorage.setItem("grace2_anonymous_accepted", "true"); } catch {}
  });
  return ctx;
}

async function gotoApp(page) {
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  // Wait for the chat panel and the dev seam to be available.
  await page.waitForSelector('[data-testid="grace2-chat"]', { timeout: 20000 });
  await page.waitForFunction(
    () => typeof window.__grace2InjectChartEmission === "function",
    { timeout: 20000 },
  );
}

async function injectChart(page, chart) {
  await page.evaluate((c) => window.__grace2InjectChartEmission(c), chart);
  // Allow React state update + vega-embed SVG render to settle.
  await page.waitForTimeout(300);
}

async function cropChat(page, outPath) {
  const chatEl = page.locator('[data-testid="grace2-chat"]');
  const box = await chatEl.boundingBox();
  if (!box) {
    await page.screenshot({ path: outPath, fullPage: false });
    return;
  }
  await page.screenshot({
    path: outPath,
    clip: { x: box.x - 4, y: box.y - 4, width: box.width + 8, height: box.height + 8 },
  });
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  await mkdir(OUT_DIR, { recursive: true });

  const browser = await chromium.launch({ headless: true });
  try {
    // ---- Scenario 1: inline stacks ----------------------------------------
    {
      const ctx = await makeContext(browser);
      const page = await ctx.newPage();
      await gotoApp(page);

      // Clear any residual charts (defensive — fresh session should be clean).
      await page.evaluate(() => {
        if (typeof window.__grace2ClearCharts === "function") {
          window.__grace2ClearCharts();
        }
      });

      // Inject all 4 charts: 2 sharing TURN-001, 2 singletons.
      for (const chart of CHARTS) {
        await injectChart(page, chart);
      }

      // Wait for chart stacks to appear.
      await page.waitForSelector('[data-testid="chart-stack"]', { timeout: 10000 });

      // Wait a little extra for SVG renders.
      await page.waitForTimeout(1000);

      await cropChat(page, `${OUT_DIR}/01_inline_stacks.png`);
      console.log("Saved: 01_inline_stacks.png");

      await ctx.close();
    }

    // ---- Scenario 2: gallery open (click the 2-chart stack) ----------------
    {
      const ctx = await makeContext(browser);
      const page = await ctx.newPage();
      await gotoApp(page);

      // Inject only the 2 stacked charts.
      await injectChart(page, CHARTS[0]);
      await injectChart(page, CHARTS[1]);

      await page.waitForSelector('[data-testid="chart-stack"]', { timeout: 10000 });
      await page.waitForTimeout(800);

      // Click the stack to open gallery.
      await page.locator('[data-testid="chart-stack"]').first().click();

      // Wait for gallery overlay.
      await page.waitForSelector('[data-testid="chart-gallery"]', { timeout: 8000 });
      await page.waitForTimeout(800); // allow gallery chart to embed

      await page.screenshot({
        path: `${OUT_DIR}/02_gallery_open.png`,
        fullPage: false,
      });
      console.log("Saved: 02_gallery_open.png");

      await ctx.close();
    }

    console.log(`\nAll screenshots saved to: ${OUT_DIR}`);
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.error("Screenshot script failed:", err);
  process.exit(1);
});
