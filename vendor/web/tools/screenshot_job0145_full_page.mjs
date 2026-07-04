#!/usr/bin/env node
// GRACE-2 — job-0145 full-page evidence screenshot.
// Captures the inline chat cards in context (over the chat panel, against
// the map) so the visual integration is auditable.

import { chromium } from "@playwright/test";
import { mkdir } from "fs/promises";

const OUT_DIR =
  process.argv[2] ?? "reports/inflight/job-0145-web-20260608/evidence";
const BASE_URL = process.env.GRACE2_DEV_URL ?? "http://localhost:5173";

async function main() {
  await mkdir(OUT_DIR, { recursive: true });

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
  });
  const page = await context.newPage();

  await page.addInitScript(() => {
    try {
      localStorage.removeItem("grace2.source_suggestion_suppressed_domains");
      localStorage.removeItem("grace2.mode2_suppressed_domains");
    } catch {}
  });

  page.on("pageerror", (e) => console.warn(`[pageerror] ${e.message}`));
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });

  const authGate = await page.$('[data-testid="grace2-auth-gate-anonymous"]');
  if (authGate) {
    await authGate.click();
  }
  await page.waitForSelector('[data-testid="grace2-app-shell"]', { timeout: 15000 });
  // Let the map paint a couple of frames.
  await page.waitForTimeout(1500);

  // Inject one of each card type.
  await page.evaluate(() => {
    window.__grace2InjectPayloadWarning({
      warning_id: "pw-fullpage-001",
      tool_name: "fetch_nexrad_reflectivity",
      tool_args: { bbox: [-82.5, 26.5, -82.0, 27.0] },
      estimated_mb: 87.3,
      threshold_mb: 25,
      recommendation:
        "Consider narrowing the bounding box to a single county to reduce payload size.",
      alternative_args: { bbox: [-82.2, 26.7, -82.1, 26.8] },
      options: ["proceed", "narrow_scope", "cancel"],
    });
    window.__grace2InjectSourceSuggestion({
      envelope_type: "mode2-candidate",
      candidate: {
        candidate_id: "ss-fullpage-001",
        url: "https://water.weather.gov/ahps/",
        domain: "water.weather.gov",
        domain_tld: "gov",
        confidence: 0.78,
        detected_patterns: ["json-ld", "data-download-link", "openapi-spec-link"],
        title: "NWS AHPS — Advanced Hydrologic Prediction Service",
        suggested_tool_kind: "endpoint",
        snippet: "Stream gage time series + flood forecasts. Download CSV.",
      },
    });
  });

  await page.waitForSelector('[data-testid="inline-chat-card-stack"]', { timeout: 5000 });
  await page.waitForTimeout(400);

  await page.screenshot({
    path: `${OUT_DIR}/inline_chat_cards_in_context.png`,
    fullPage: false,
  });
  console.log(`[screenshot] saved → ${OUT_DIR}/inline_chat_cards_in_context.png`);

  await browser.close();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
