#!/usr/bin/env node
// GRACE-2 — job-0140 evidence screenshot.
// Boots the dev server, injects a fake payload-warning via the
// window.__grace2InjectPayloadWarning dev seam, screenshots the inline
// card, then clicks "Proceed" to verify the onDecide path.

import { chromium } from "@playwright/test";
import { mkdir } from "fs/promises";

const OUT_DIR =
  process.argv[2] ?? "reports/inflight/job-0140-web-20260608/evidence";
const BASE_URL = process.env.GRACE2_DEV_URL ?? "http://localhost:5174";

async function main() {
  await mkdir(OUT_DIR, { recursive: true });

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
  });
  const page = await context.newPage();

  // Capture WS frames so we can verify tool-payload-confirmation was sent.
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

  // Handle AuthGate — present when Firebase is unconfigured (dev mode).
  const authGate = await page.$('[data-testid="grace2-auth-gate-anonymous"]');
  if (authGate) {
    console.log("[screenshot] AuthGate present — clicking 'Continue anonymously'");
    await authGate.click();
  }

  await page.waitForSelector('[data-testid="grace2-app-shell"]', {
    timeout: 15000,
  });
  console.log("[screenshot] app shell mounted");

  // Baseline — confirm no warning card before injection.
  const preBefore = await page.$('[data-testid="payload-warning-inline"]');
  if (preBefore !== null) {
    console.error("[FAIL] payload-warning-inline visible before injection");
    process.exit(1);
  }
  console.log("[VERIFY] no payload-warning-inline before injection (correct)");

  // Inject a fake payload-warning via the dev seam.
  await page.evaluate(() => {
    window.__grace2InjectPayloadWarning({
      warning_id: "pw-e2e-001",
      tool_name: "fetch_dem",
      tool_args: { bbox: [-82, 26, -81, 27] },
      estimated_mb: 42.5,
      threshold_mb: 25,
      recommendation:
        "Consider narrowing the bounding box to reduce payload size.",
      alternative_args: { bbox: [-81.8, 26.2, -81.2, 26.8] },
      options: ["proceed", "narrow_scope", "cancel"],
    });
  });
  console.log("[screenshot] payload-warning injected");

  // Wait for the inline card.
  await page.waitForSelector('[data-testid="payload-warning-inline"]', {
    timeout: 5000,
  });
  console.log("[VERIFY] payload-warning-inline rendered");

  // Verify key data-testid elements are present and show correct content.
  const toolName = await page.textContent('[data-testid="payload-warning-tool"]');
  if (!toolName?.includes("fetch_dem")) {
    console.error(`[FAIL] expected tool_name fetch_dem, got: ${toolName}`);
    process.exit(1);
  }
  console.log(`[VERIFY] tool_name correct: ${toolName}`);

  const estMb = await page.textContent('[data-testid="payload-warning-estimated-mb"]');
  if (!estMb?.includes("42.5")) {
    console.error(`[FAIL] expected estimated_mb 42.5, got: ${estMb}`);
    process.exit(1);
  }
  console.log(`[VERIFY] estimated_mb correct: ${estMb}`);

  const thrMb = await page.textContent('[data-testid="payload-warning-threshold-mb"]');
  if (!thrMb?.includes("25")) {
    console.error(`[FAIL] expected threshold_mb 25, got: ${thrMb}`);
    process.exit(1);
  }
  console.log(`[VERIFY] threshold_mb correct: ${thrMb}`);

  const rec = await page.textContent('[data-testid="payload-warning-recommendation"]');
  if (!rec?.includes("narrowing")) {
    console.error(`[FAIL] recommendation missing expected text, got: ${rec}`);
    process.exit(1);
  }
  console.log("[VERIFY] recommendation text correct");

  // Verify 3 buttons are present.
  const proceedBtn = await page.$('[data-testid="payload-warning-button-proceed"]');
  const narrowBtn = await page.$('[data-testid="payload-warning-button-narrow_scope"]');
  const cancelBtn = await page.$('[data-testid="payload-warning-button-cancel"]');
  if (!proceedBtn || !narrowBtn || !cancelBtn) {
    console.error("[FAIL] one or more action buttons missing");
    process.exit(1);
  }
  console.log("[VERIFY] all 3 action buttons present");

  // Screenshot 1 — inline card before any decision.
  await page.screenshot({
    path: `${OUT_DIR}/payload_warning_inline_card.png`,
    fullPage: false,
  });
  console.log(`[screenshot] card screenshot saved → ${OUT_DIR}/payload_warning_inline_card.png`);

  // Click Proceed and verify tool-payload-confirmation is sent.
  await page.click('[data-testid="payload-warning-button-proceed"]');
  console.log("[screenshot] Proceed clicked");

  // The App.tsx queue removes the card immediately after onDecide; the 'Sent'
  // footer is local to the component so it may vanish before we can capture
  // it.  We give it a brief grace period but treat it as optional — the WS
  // frame check below is the authoritative verification.
  try {
    await page.waitForSelector('[data-testid="payload-warning-sent"]', {
      timeout: 500,
    });
    const sentText = await page.textContent('[data-testid="payload-warning-sent"]');
    console.log(`[VERIFY] Sent footer shows: ${sentText}`);
  } catch {
    console.log("[VERIFY] Sent footer not captured (component removed from queue — normal; WS frame is authoritative)");
  }

  // Screenshot 2 — after Proceed (card removed from queue = clean state).
  await page.screenshot({
    path: `${OUT_DIR}/payload_warning_after_proceed.png`,
    fullPage: false,
  });
  console.log(`[screenshot] after-proceed screenshot saved → ${OUT_DIR}/payload_warning_after_proceed.png`);

  // Verify WS frame: tool-payload-confirmation with decision=proceed.
  const frames = await page.evaluate(() => window.__grace2CapturedFrames);
  const parsed = frames
    .map((s) => { try { return JSON.parse(s); } catch { return null; } })
    .filter((x) => x !== null);

  const confirmation = parsed.find(
    (e) => e.type === "tool-payload-confirmation",
  );
  if (!confirmation) {
    console.error("[FAIL] no tool-payload-confirmation envelope captured");
    console.error(`captured types: ${parsed.map((e) => e.type).join(", ")}`);
    process.exit(1);
  }
  console.log(
    `[VERIFY] tool-payload-confirmation captured: warning_id=${confirmation.payload.warning_id}, decision=${confirmation.payload.decision}`,
  );
  if (confirmation.payload.decision !== "proceed") {
    console.error(
      `[FAIL] expected decision=proceed, got ${confirmation.payload.decision}`,
    );
    process.exit(1);
  }
  if (confirmation.payload.warning_id !== "pw-e2e-001") {
    console.error(
      `[FAIL] expected warning_id=pw-e2e-001, got ${confirmation.payload.warning_id}`,
    );
    process.exit(1);
  }

  console.log("[OK] all payload-warning seam verifications passed");

  await browser.close();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
