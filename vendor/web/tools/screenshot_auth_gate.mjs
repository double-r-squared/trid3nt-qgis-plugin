#!/usr/bin/env node
// GRACE-2 — job-0138 evidence screenshot.
//
// Drives the live web client against a stub WebSocket and captures the
// AuthGate → main-shell transition the kickoff describes. Captures:
//
//   1. auth_gate_initial.png        — full-screen AuthGate on first load
//                                     (no auth, no anonymous flag).
//   2. auth_gate_why_modal.png      — Why-sign-in modal open over the gate.
//   3. app_shell_after_anonymous.png — main app after clicking
//                                     "Continue without saving".
//   4. auth_gate_after_signout.png  — back to the gate after sign-out from
//                                     the residual identity chip.
//
// Verifies:
//   - The gate is mounted by data-testid on first load.
//   - localStorage flag `grace2_anonymous_accepted` is initially absent.
//   - Clicking the anonymous CTA writes the flag and transitions to the
//     app shell (data-testid="grace2-app-shell" appears, gate disappears).
//   - Reload with the flag set lands directly on the app shell (gate skipped).
//   - The identity chip sign-out button clears the flag and returns the
//     user to the gate.

import { chromium } from "@playwright/test";
import { mkdir } from "fs/promises";

const OUT_DIR =
  process.argv[2] ??
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0138-web-20260608/evidence";
const BASE_URL = process.env.GRACE2_DEV_URL ?? "http://localhost:5179";

async function main() {
  await mkdir(OUT_DIR, { recursive: true });

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
  });
  const page = await context.newPage();

  // Stub WebSocket — never connects, never sends anything; we just want the
  // app to mount and the auth-gate decision to render.
  await page.addInitScript(() => {
    const captured = [];
    class StubWS {
      constructor(url) {
        this._url = url;
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
    StubWS.OPEN = 1; StubWS.CONNECTING = 0; StubWS.CLOSED = 3;
    window.WebSocket = StubWS;
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

  // Clear localStorage to start from a fresh "no auth, no flag" state.
  console.log(`[screenshot] loading ${BASE_URL} (fresh state)`);
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  await page.evaluate(() => {
    try { localStorage.clear(); } catch (_e) { /* noop */ }
  });
  // Reload after the clear so the React tree mounts with the empty state.
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });

  // (1) AuthGate visible — gate, main shell hidden.
  await page.waitForSelector('[data-testid="grace2-auth-gate"]', {
    timeout: 10000,
  });
  console.log("[screenshot] AuthGate mounted on first load");

  // Verify the main app shell is NOT yet rendered.
  const shellCountInitial = await page
    .locator('[data-testid="grace2-app-shell"]')
    .count();
  if (shellCountInitial !== 0) {
    console.error("[FAIL] grace2-app-shell rendered while gate is up");
    process.exit(1);
  }
  console.log("[VERIFY] main shell hidden behind gate (count=0)");

  // Verify localStorage flag is absent.
  const initialFlag = await page.evaluate(() =>
    localStorage.getItem("grace2_anonymous_accepted"),
  );
  if (initialFlag !== null) {
    console.error(`[FAIL] anonymous flag should be null, got: ${initialFlag}`);
    process.exit(1);
  }
  console.log("[VERIFY] localStorage.grace2_anonymous_accepted == null");

  // Verify the wordmark text + both CTAs.
  const wordmark = await page
    .locator('[data-testid="grace2-auth-gate-wordmark"]')
    .innerText();
  if (!/GRACE-2/.test(wordmark)) {
    console.error(`[FAIL] wordmark text wrong: ${wordmark}`);
    process.exit(1);
  }
  console.log(`[VERIFY] wordmark renders: "${wordmark}"`);

  await page.waitForSelector('[data-testid="grace2-auth-gate-google"]');
  await page.waitForSelector('[data-testid="grace2-auth-gate-anonymous"]');
  await page.waitForSelector('[data-testid="grace2-auth-gate-why"]');
  console.log("[VERIFY] all 3 CTAs (google + anonymous + why) render");

  await page.screenshot({
    path: `${OUT_DIR}/auth_gate_initial.png`,
    fullPage: false,
  });
  console.log("[screenshot] (1) auth_gate_initial.png saved");

  // (2) Why-sign-in modal.
  await page.click('[data-testid="grace2-auth-gate-why"]');
  await page.waitForSelector('[data-testid="grace2-auth-gate-why-modal"]', {
    timeout: 3000,
  });
  console.log("[screenshot] Why-modal opened");
  await page.waitForTimeout(150);
  await page.screenshot({
    path: `${OUT_DIR}/auth_gate_why_modal.png`,
    fullPage: false,
  });
  console.log("[screenshot] (2) auth_gate_why_modal.png saved");

  // Close the modal so the subsequent click reaches the anonymous CTA.
  await page.click('[data-testid="grace2-auth-gate-why-close"]');
  await page.waitForSelector('[data-testid="grace2-auth-gate-why-modal"]', {
    state: "detached",
    timeout: 3000,
  });
  console.log("[VERIFY] Why-modal closes cleanly");

  // (3) Click "Continue without saving" — flag must be set, gate dismounts,
  // app shell mounts.
  await page.click('[data-testid="grace2-auth-gate-anonymous"]');
  await page.waitForSelector('[data-testid="grace2-app-shell"]', {
    timeout: 5000,
  });
  const gateAfter = await page
    .locator('[data-testid="grace2-auth-gate"]')
    .count();
  if (gateAfter !== 0) {
    console.error("[FAIL] AuthGate still visible after anonymous accept");
    process.exit(1);
  }
  console.log("[VERIFY] gate dismounts after anonymous accept");

  const flagAfter = await page.evaluate(() =>
    localStorage.getItem("grace2_anonymous_accepted"),
  );
  if (flagAfter !== "true") {
    console.error(
      `[FAIL] localStorage.grace2_anonymous_accepted should be "true", got: ${flagAfter}`,
    );
    process.exit(1);
  }
  console.log(
    `[VERIFY] localStorage.grace2_anonymous_accepted == "${flagAfter}"`,
  );

  // Verify the residual identity chip exists with auth-mode="anonymous".
  await page.waitForSelector('[data-testid="grace2-identity-chip"]', {
    timeout: 3000,
  });
  const chipMode = await page.getAttribute(
    '[data-testid="grace2-identity-chip"]',
    "data-auth-mode",
  );
  if (chipMode !== "anonymous") {
    console.error(
      `[FAIL] identity chip data-auth-mode should be "anonymous", got: ${chipMode}`,
    );
    process.exit(1);
  }
  console.log(`[VERIFY] residual identity chip data-auth-mode="${chipMode}"`);

  await page.waitForTimeout(300);
  await page.screenshot({
    path: `${OUT_DIR}/app_shell_after_anonymous.png`,
    fullPage: false,
  });
  console.log("[screenshot] (3) app_shell_after_anonymous.png saved");

  // (3b) Reload — flag should bypass the gate.
  console.log("[screenshot] reloading to verify flag persistence");
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="grace2-app-shell"]', {
    timeout: 5000,
  });
  const gateAfterReload = await page
    .locator('[data-testid="grace2-auth-gate"]')
    .count();
  if (gateAfterReload !== 0) {
    console.error("[FAIL] AuthGate re-appeared after reload with flag set");
    process.exit(1);
  }
  console.log(
    "[VERIFY] reload bypasses gate (anonymous flag survived localStorage)",
  );

  // (4) Click the sign-out button on the identity chip — gate must reappear.
  await page.click('[data-testid="grace2-identity-chip-signout"]');
  await page.waitForSelector('[data-testid="grace2-auth-gate"]', {
    timeout: 5000,
  });
  const flagAfterSignout = await page.evaluate(() =>
    localStorage.getItem("grace2_anonymous_accepted"),
  );
  if (flagAfterSignout !== null) {
    console.error(
      `[FAIL] anonymous flag should be cleared on sign-out, got: ${flagAfterSignout}`,
    );
    process.exit(1);
  }
  console.log("[VERIFY] sign-out clears anonymous flag");

  const shellAfterSignout = await page
    .locator('[data-testid="grace2-app-shell"]')
    .count();
  if (shellAfterSignout !== 0) {
    console.error("[FAIL] main shell still visible after sign-out");
    process.exit(1);
  }
  console.log("[VERIFY] main shell hidden after sign-out (count=0)");

  await page.waitForTimeout(150);
  await page.screenshot({
    path: `${OUT_DIR}/auth_gate_after_signout.png`,
    fullPage: false,
  });
  console.log("[screenshot] (4) auth_gate_after_signout.png saved");

  console.log("[OK] all auth-gate verifications passed");
  await browser.close();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
