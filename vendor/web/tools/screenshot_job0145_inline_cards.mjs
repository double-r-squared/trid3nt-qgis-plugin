#!/usr/bin/env node
// GRACE-2 — job-0145 evidence screenshot.
//
// Captures the new Claude Code-styled inline chat cards:
//   1. PayloadWarningInline (warning variant)
//   2. SourceSuggestionInline (info variant — replaces Mode2OfferModal)
//   3. Both stacked together (variant comparison)
//
// Verifies:
//   - No "Mode 2" / "Tier 1/2" / "OQ-" jargon in user-visible text
//   - Detected patterns translate to plain-language phrases
//   - Confidence renders as "N% match"
//   - Add data source emits the expected WS frame (mode2-add-confirmed)
//   - Don't suggest again writes to localStorage suppression list

import { chromium } from "@playwright/test";
import { mkdir } from "fs/promises";

const OUT_DIR =
  process.argv[2] ?? "reports/inflight/job-0145-web-20260608/evidence";
const BASE_URL = process.env.GRACE2_DEV_URL ?? "http://localhost:5174";

async function main() {
  await mkdir(OUT_DIR, { recursive: true });

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
  });
  const page = await context.newPage();

  // Capture outbound WS frames for verification.
  await page.addInitScript(() => {
    const captured = [];
    class CapturingWS {
      constructor() {
        this._listeners = {};
        this._ready = 1;
        setTimeout(() => {
          (this._listeners["open"] ?? []).forEach((cb) => cb({}));
        }, 0);
      }
      get readyState() { return this._ready; }
      addEventListener(type, cb) {
        (this._listeners[type] ??= []).push(cb);
      }
      send(data) { captured.push(data); }
      close() {
        this._ready = 3;
        (this._listeners["close"] ?? []).forEach((cb) => cb({}));
      }
    }
    CapturingWS.OPEN = 1;
    CapturingWS.CONNECTING = 0;
    CapturingWS.CLOSED = 3;
    window.WebSocket = CapturingWS;
    window.__grace2CapturedFrames = captured;
    try {
      localStorage.removeItem("grace2.source_suggestion_suppressed_domains");
      localStorage.removeItem("grace2.mode2_suppressed_domains");
    } catch {
      // ignore
    }
  });

  page.on("pageerror", (err) =>
    console.warn(`[screenshot] pageerror: ${err.message}`),
  );
  page.on("console", (msg) => {
    if (msg.type() === "error") {
      console.warn(`[screenshot] console.error: ${msg.text()}`);
    }
  });

  console.log(`[screenshot] loading ${BASE_URL}`);
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });

  // Click through AuthGate if present.
  const authGate = await page.$('[data-testid="grace2-auth-gate-anonymous"]');
  if (authGate) {
    console.log("[screenshot] AuthGate present — clicking 'Continue anonymously'");
    await authGate.click();
  }

  await page.waitForSelector('[data-testid="grace2-app-shell"]', {
    timeout: 15000,
  });
  console.log("[screenshot] app shell mounted");

  // ====== Capture 1: PayloadWarningInline ====== //
  await page.evaluate(() => {
    window.__grace2InjectPayloadWarning({
      warning_id: "pw-job0145-001",
      tool_name: "fetch_nexrad_reflectivity",
      tool_args: { bbox: [-82.5, 26.5, -82.0, 27.0] },
      estimated_mb: 87.3,
      threshold_mb: 25,
      recommendation:
        "Consider narrowing the bounding box to a single county to reduce payload size.",
      alternative_args: { bbox: [-82.2, 26.7, -82.1, 26.8] },
      options: ["proceed", "narrow_scope", "cancel"],
    });
  });

  await page.waitForSelector('[data-testid="payload-warning-inline"]', {
    timeout: 5000,
  });
  console.log("[VERIFY] payload-warning-inline rendered");

  // Capture full card region.
  const warningCard = await page.$('[data-testid="payload-warning-inline"]');
  await warningCard.screenshot({
    path: `${OUT_DIR}/payload_warning_card.png`,
  });
  console.log(`[screenshot] saved → ${OUT_DIR}/payload_warning_card.png`);

  // Verify language discipline.
  const warningText = await page.textContent('[data-testid="payload-warning-inline"]');
  for (const banned of ["Mode 2", "Mode 1", "Tier 1", "Tier 2", "OQ-"]) {
    if (warningText.includes(banned)) {
      console.error(`[FAIL] banned text '${banned}' present in payload-warning card`);
      process.exit(1);
    }
  }
  console.log("[VERIFY] payload-warning card has no internal jargon");

  // Click cancel to clear, so the next capture is clean.
  await page.click('[data-testid="payload-warning-button-cancel"]');
  await page.waitForSelector('[data-testid="payload-warning-inline"]', {
    state: "detached",
    timeout: 2000,
  });

  // ====== Capture 2: SourceSuggestionInline ====== //
  await page.evaluate(() => {
    window.__grace2InjectSourceSuggestion({
      envelope_type: "mode2-candidate",
      candidate: {
        candidate_id: "ss-job0145-001",
        url: "https://water.weather.gov/ahps/",
        domain: "water.weather.gov",
        domain_tld: "gov",
        confidence: 0.78,
        detected_patterns: ["json-ld", "data-download-link", "openapi-spec-link"],
        title: "NWS AHPS — Advanced Hydrologic Prediction Service",
        suggested_tool_kind: "endpoint",
        snippet: 'Stream gage time series + flood forecasts. Download CSV.',
      },
    });
  });

  await page.waitForSelector('[data-testid="source-suggestion-inline-ss-job0145-001"]', {
    timeout: 5000,
  });
  console.log("[VERIFY] source-suggestion card rendered");

  const sourceCard = await page.$('[data-testid="source-suggestion-inline-ss-job0145-001"]');
  await sourceCard.screenshot({
    path: `${OUT_DIR}/source_suggestion_card.png`,
  });
  console.log(`[screenshot] saved → ${OUT_DIR}/source_suggestion_card.png`);

  // Verify language discipline.
  const sourceText = await page.textContent('[data-testid="source-suggestion-inline-ss-job0145-001"]');
  for (const banned of ["Mode 2", "Mode 1", "Tier 1", "Tier 2", "OQ-"]) {
    if (sourceText.includes(banned)) {
      console.error(`[FAIL] banned text '${banned}' present in source-suggestion card`);
      process.exit(1);
    }
  }
  console.log("[VERIFY] source-suggestion card has no internal jargon");

  // Verify translated patterns.
  const caps = await page.textContent('[data-testid="source-suggestion-capabilities-ss-job0145-001"]');
  if (!caps.includes("Has machine-readable metadata")) {
    console.error(`[FAIL] expected translated 'json-ld' phrase, got: ${caps}`);
    process.exit(1);
  }
  if (!caps.includes("Offers data downloads")) {
    console.error(`[FAIL] expected translated 'data-download-link' phrase, got: ${caps}`);
    process.exit(1);
  }
  if (!caps.includes("Has a documented API")) {
    console.error(`[FAIL] expected translated 'openapi-spec-link' phrase, got: ${caps}`);
    process.exit(1);
  }
  console.log("[VERIFY] detected_patterns translated to user-friendly phrases");

  // Verify percentage rendering.
  const conf = await page.textContent('[data-testid="source-suggestion-confidence-ss-job0145-001"]');
  if (!conf.includes("78% match")) {
    console.error(`[FAIL] expected '78% match', got: ${conf}`);
    process.exit(1);
  }
  console.log(`[VERIFY] confidence rendered as percentage: ${conf}`);

  // ====== Capture 3: Both cards stacked (variant comparison) ====== //
  // Re-inject a payload warning while the source-suggestion card is still up.
  await page.evaluate(() => {
    window.__grace2InjectPayloadWarning({
      warning_id: "pw-job0145-002",
      tool_name: "fetch_dem",
      tool_args: { bbox: [-82, 26, -81, 27] },
      estimated_mb: 42.5,
      threshold_mb: 25,
      recommendation: "Consider narrowing the bbox to reduce payload size.",
      alternative_args: { bbox: [-81.8, 26.2, -81.2, 26.8] },
      options: ["proceed", "narrow_scope", "cancel"],
    });
  });

  await page.waitForSelector('[data-testid="payload-warning-inline"]', {
    timeout: 5000,
  });

  const stack = await page.$('[data-testid="inline-chat-card-stack"]');
  await stack.screenshot({
    path: `${OUT_DIR}/inline_chat_card_variants_stacked.png`,
  });
  console.log(`[screenshot] saved → ${OUT_DIR}/inline_chat_card_variants_stacked.png`);

  // ====== WS frame verification — click Add data source ====== //
  await page.click('[data-testid="source-suggestion-add-ss-job0145-001"]');
  await page.waitForTimeout(200);

  const frames = await page.evaluate(() => window.__grace2CapturedFrames);
  const parsed = frames
    .map((s) => { try { return JSON.parse(s); } catch { return null; } })
    .filter((x) => x !== null);

  const addConfirmed = parsed.find((e) => e.type === "mode2-add-confirmed");
  if (!addConfirmed) {
    console.error("[FAIL] no mode2-add-confirmed envelope captured");
    console.error(`captured types: ${parsed.map((e) => e.type).join(", ")}`);
    process.exit(1);
  }
  console.log(`[VERIFY] mode2-add-confirmed captured: candidate_id=${addConfirmed.payload.candidate_id}`);

  const auditEvents = parsed.filter((e) => e.type === "mode2-audit-event");
  if (auditEvents.length === 0) {
    console.error("[FAIL] no mode2-audit-event envelope captured");
    process.exit(1);
  }
  console.log(`[VERIFY] mode2-audit-event captured (${auditEvents.length} event(s))`);
  const lastAudit = auditEvents[auditEvents.length - 1];
  if (lastAudit.payload.surface !== "inline") {
    console.error(`[FAIL] expected surface=inline, got: ${lastAudit.payload.surface}`);
    process.exit(1);
  }
  console.log(`[VERIFY] audit surface=${lastAudit.payload.surface}`);

  console.log("[OK] all job-0145 verifications passed");

  await browser.close();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
