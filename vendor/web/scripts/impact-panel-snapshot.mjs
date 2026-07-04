#!/usr/bin/env node
// GRACE-2 — Wave 4.11 P4 ImpactPanel UI snapshot.
//
// Captures the ImpactPanel filled with a representative Fort Myers
// ImpactEnvelope fixture and writes a screenshot to disk.
//
// Per memory `feedback_playwright_must_drive_live_agent`, the inject-seam
// approach (`__grace2InjectImpactEnvelope`) is used here ONLY because this
// is a UI-only snapshot of the panel chrome itself (no agent involvement
// required to render the envelope view). When wired against the real
// agent flow, the panel will surface from the `compute_impact_envelope`
// tool result envelope at runtime.
//
// Usage:
//   node web/scripts/impact-panel-snapshot.mjs [--url=...] [--out=...]
//
// Defaults:
//   url:  http://localhost:5173
//   out:  /tmp/wave4_11_p4_impact_panel.png
//
// Also writes a copy to:
//   reports/inflight/wave-4-11-p4-impact-panel-20260609/evidence/impact_panel.png

import { mkdir, copyFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const PLAYWRIGHT_ENTRY = resolve(
  __dirname,
  "..",
  "node_modules",
  "@playwright",
  "test",
  "index.mjs",
);

const { chromium } = await import(PLAYWRIGHT_ENTRY);

function parseArg(name, fallback) {
  const prefix = `--${name}=`;
  for (const a of process.argv.slice(2)) {
    if (a.startsWith(prefix)) return a.slice(prefix.length);
  }
  return fallback;
}

const URL_ARG = parseArg("url", "http://localhost:5173");
const OUT_ARG = parseArg("out", "/tmp/wave4_11_p4_impact_panel.png");
const EVIDENCE_OUT = resolve(
  __dirname,
  "..",
  "..",
  "reports",
  "inflight",
  "wave-4-11-p4-impact-panel-20260609",
  "evidence",
  "impact_panel.png",
);

// Representative Fort Myers ImpactEnvelope fixture (USACE_NSI path) —
// mirrors the schema in
// packages/contracts/src/grace2_contracts/impact_envelope.py.
const FORT_MYERS_FIXTURE = {
  schema_version: "v1",
  n_structures_total: 12843,
  n_structures_damaged: 3217,
  n_structures_destroyed: 412,
  damage_state_distribution: {
    DS0_none: 9626,
    DS1_slight: 1802,
    DS2_moderate: 712,
    DS3_extensive: 291,
    DS4_complete: 412,
  },
  total_replacement_value_usd: 4_120_000_000,
  damaged_replacement_value_usd: 1_080_000_000,
  expected_loss_usd: 312_500_000,
  loss_percentile_95_usd: 487_200_000,
  population_total: 28_410,
  population_displaced: 6_840,
  population_at_high_risk: 1_220,
  impact_area_km2: 84.6,
  bbox: [-82.05, 26.45, -81.78, 26.72],
  by_occupancy_class: {
    RES1: {
      n_structures: 10_840,
      n_damaged: 2_910,
      n_destroyed: 312,
      expected_loss_usd: 228_000_000,
      loss_percentile_95_usd: 360_000_000,
      population: 24_900,
      population_displaced: 5_910,
    },
    RES3: {
      n_structures: 1_215,
      n_damaged: 184,
      n_destroyed: 56,
      expected_loss_usd: 48_700_000,
      loss_percentile_95_usd: 79_000_000,
      population: 2_840,
      population_displaced: 645,
    },
    COM1: {
      n_structures: 612,
      n_damaged: 99,
      n_destroyed: 32,
      expected_loss_usd: 28_400_000,
      loss_percentile_95_usd: 41_200_000,
      population: 410,
      population_displaced: 162,
    },
    IND1: {
      n_structures: 176,
      n_damaged: 24,
      n_destroyed: 12,
      expected_loss_usd: 7_400_000,
      loss_percentile_95_usd: 7_000_000,
      population: 260,
      population_displaced: 123,
    },
  },
  pelicun_run_id: "01HF2X3YM2N7QYZJ7E0H8WQ5XZ",
  damage_layer_uri:
    "gs://grace2-runs/sessions/fort-myers-2026/damage_assessment_01HF2X3YM2N7.fgb",
  structure_inventory_source: "USACE_NSI",
  flood_layer_uri:
    "gs://grace2-runs/sessions/fort-myers-2026/flood_depth_max_cog.tif",
  fragility_set: "hazus_flood_v6",
  realization_count: 200,
  generated_at: "2026-06-09T14:32:18Z",
};

async function main() {
  await mkdir(dirname(OUT_ARG), { recursive: true });
  await mkdir(dirname(EVIDENCE_OUT), { recursive: true });

  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
  });
  const page = await ctx.newPage();

  page.on("pageerror", (err) => {
    console.error("[pageerror]", err.message);
  });

  console.error(`[snapshot] navigating to ${URL_ARG}`);
  await page.goto(URL_ARG, { waitUntil: "networkidle", timeout: 45_000 });

  // Wait briefly for first paint / MapLibre.
  await page.waitForTimeout(1200);

  // Bypass any AuthGate by accepting anonymous flow if present.
  try {
    const btn = page.locator("[data-testid='grace2-auth-gate-anonymous']");
    if (await btn.isVisible({ timeout: 800 }).catch(() => false)) {
      console.error("[snapshot] clicking AuthGate anonymous CTA");
      await btn.click();
      await page.waitForTimeout(600);
    }
  } catch {
    // Not visible — already past the gate.
  }

  // Verify we're past the AuthGate.
  await page
    .waitForSelector("[data-testid='grace2-app-shell']", { timeout: 5000 })
    .catch(() => {
      console.error("[snapshot] WARN: grace2-app-shell did not surface");
    });

  // Inject the fixture via the dev seam.
  console.error("[snapshot] injecting ImpactEnvelope fixture");
  const injected = await page.evaluate((fx) => {
    // eslint-disable-next-line no-undef
    if (typeof window.__grace2InjectImpactEnvelope !== "function") {
      return false;
    }
    // eslint-disable-next-line no-undef
    window.__grace2InjectImpactEnvelope(fx);
    return true;
  }, FORT_MYERS_FIXTURE);

  if (!injected) {
    console.error(
      "[snapshot] __grace2InjectImpactEnvelope not present — is this a dev build?",
    );
    await browser.close();
    process.exit(1);
  }

  // Wait for the panel to appear and animation to settle.
  await page
    .waitForSelector("[data-testid='grace2-impact-panel']", { timeout: 4000 })
    .catch(() => {
      throw new Error("ImpactPanel did not surface within 4s of injection.");
    });
  await page.waitForTimeout(400);

  console.error(`[snapshot] writing ${OUT_ARG}`);
  await page.screenshot({ path: OUT_ARG, fullPage: false });

  console.error(`[snapshot] writing ${EVIDENCE_OUT}`);
  await copyFile(OUT_ARG, EVIDENCE_OUT);

  await browser.close();
  console.error("[snapshot] done");
}

main().catch((err) => {
  console.error("[snapshot] failed:", err);
  process.exit(1);
});
