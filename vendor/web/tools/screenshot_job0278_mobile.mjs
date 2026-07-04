#!/usr/bin/env node
// GRACE-2 — job-0278 evidence screenshots (mobile-friendly UI, static shell).
//
// Captures the mobile (390x844, iPhone-portrait) shell against the LIVE Vite
// dev server. Per kickoff constraints: page load + drawer/sheet toggling
// ONLY — no chat prompts are sent, no inject seams are used. The real agent
// WS connection is allowed to open (read-only envelopes like case-list may
// arrive; that's fine and representative of what the phone will show).
//
//   1. mobile_root_collapsed_sheet.png — root view: map + ☰ drawer button +
//      collapsed bottom sheet (handle + composer).
//   2. mobile_drawer_open.png          — slide-in drawer over backdrop
//      (CasesPanel + Settings/Secrets pills in the footer).
//   3. mobile_sheet_expanded.png       — chat bottom sheet expanded to 70vh.
//   4. desktop_root_regression.png     — 1440x900 control shot: desktop
//      layout unchanged (no drawer button, side chat panel present).

import { chromium } from "@playwright/test";
import { mkdir } from "fs/promises";

const OUT_DIR =
  process.argv[2] ??
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0278-web-20260611/evidence";
const BASE_URL = process.env.GRACE2_DEV_URL ?? "http://localhost:5173";

async function newPage(browser, viewport) {
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  // Skip the AuthGate the same way a returning anonymous user does.
  await page.addInitScript(() => {
    try {
      localStorage.setItem("grace2_anonymous_accepted", "true");
    } catch {
      /* non-fatal */
    }
  });
  return { context, page };
}

async function main() {
  await mkdir(OUT_DIR, { recursive: true });
  const browser = await chromium.launch({ headless: true });

  // ---- Mobile (iPhone 12-ish portrait) ---------------------------------- //
  const { context: mctx, page } = await newPage(browser, {
    width: 390,
    height: 844,
  });
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="grace2-app-shell"]', {
    timeout: 15000,
  });
  // Mobile shell markers.
  await page.waitForSelector('[data-testid="grace2-mobile-drawer-button"]', {
    timeout: 10000,
  });
  await page.waitForSelector(
    '[data-testid="grace2-chat"][data-sheet-state="collapsed"]',
    { timeout: 10000 },
  );
  // Let the basemap tiles settle for a representative shot.
  await page.waitForTimeout(3500);

  // 1. Root + collapsed sheet.
  await page.screenshot({
    path: `${OUT_DIR}/mobile_root_collapsed_sheet.png`,
  });
  console.log("captured mobile_root_collapsed_sheet.png");

  // Sanity: desktop chrome absent on mobile.
  for (const tid of [
    "grace2-left-rail",
    "grace2-chat-hamburger",
    "grace2-layers-hamburger",
  ]) {
    const n = await page.locator(`[data-testid="${tid}"]`).count();
    if (n !== 0) throw new Error(`desktop element ${tid} leaked into mobile`);
  }

  // 2. Drawer open (☰ tap), backdrop visible.
  await page.click('[data-testid="grace2-mobile-drawer-button"]');
  await page.waitForSelector('[data-testid="grace2-mobile-drawer"]', {
    timeout: 5000,
  });
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${OUT_DIR}/mobile_drawer_open.png` });
  console.log("captured mobile_drawer_open.png");

  // Backdrop tap closes the drawer.
  await page
    .locator('[data-testid="grace2-mobile-drawer-backdrop"]')
    .click({ position: { x: 370, y: 420 } });
  await page.waitForSelector('[data-testid="grace2-mobile-drawer"]', {
    state: "detached",
    timeout: 5000,
  });

  // 3. Sheet expanded (handle tap) — NO prompt is typed or sent.
  await page.click('[data-testid="grace2-chat-sheet-toggle"]');
  await page.waitForSelector(
    '[data-testid="grace2-chat"][data-sheet-state="expanded"]',
    { timeout: 5000 },
  );
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${OUT_DIR}/mobile_sheet_expanded.png` });
  console.log("captured mobile_sheet_expanded.png");

  // Collapse again to verify the round trip.
  await page.click('[data-testid="grace2-chat-sheet-toggle"]');
  await page.waitForSelector(
    '[data-testid="grace2-chat"][data-sheet-state="collapsed"]',
    { timeout: 5000 },
  );
  await mctx.close();

  // ---- Desktop control (regression guard) ------------------------------- //
  const { context: dctx, page: dpage } = await newPage(browser, {
    width: 1440,
    height: 900,
  });
  await dpage.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  await dpage.waitForSelector('[data-testid="grace2-app-shell"]', {
    timeout: 15000,
  });
  await dpage.waitForTimeout(3500);
  // Desktop must NOT show mobile chrome.
  for (const tid of [
    "grace2-mobile-drawer-button",
    "grace2-chat-sheet-toggle",
    "grace2-mobile-drawer",
  ]) {
    const n = await dpage.locator(`[data-testid="${tid}"]`).count();
    if (n !== 0) throw new Error(`mobile element ${tid} leaked into desktop`);
  }
  await dpage.screenshot({ path: `${OUT_DIR}/desktop_root_regression.png` });
  console.log("captured desktop_root_regression.png");
  await dctx.close();

  await browser.close();
  console.log("OK — all captures complete");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
