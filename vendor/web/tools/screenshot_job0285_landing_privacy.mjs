#!/usr/bin/env node
// GRACE-2 — job-0285 evidence screenshots (landing page + privacy policy).
//
// Captures the new public pages against the LIVE Vite dev server, plus a
// proof shot of the session-passthrough rule (EntryRouter.tsx):
//
//   1. landing_desktop_1440x900.png      — "/" in a FRESH context (no
//      localStorage) → landing hero. Viewport shot.
//   2. landing_desktop_full.png          — same, full-page scroll capture.
//   3. landing_mobile_390x844.png        — "/" fresh context, mobile hero.
//   4. landing_mobile_full.png           — same, full-page.
//   5. privacy_desktop_1440x900.png      — "/privacy", desktop.
//   6. privacy_mobile_390x844.png        — "/privacy", mobile.
//   7. passthrough_root_with_session.png — "/" in a context seeded with
//      grace2_anonymous_accepted (the key every live-verify tool seeds) →
//      must render the APP, not the landing.

import { chromium } from "@playwright/test";
import { mkdir } from "fs/promises";

const OUT_DIR =
  process.argv[2] ??
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0285-web-20260611/evidence";
const BASE_URL = process.env.GRACE2_DEV_URL ?? "http://localhost:5173";

const DESKTOP = { width: 1440, height: 900 };
const MOBILE = { width: 390, height: 844 };

async function freshPage(browser, viewport) {
  const context = await browser.newContext({ viewport });
  return { context, page: await context.newPage() };
}

async function main() {
  await mkdir(OUT_DIR, { recursive: true });
  const browser = await chromium.launch();

  // ── 1-4: landing, fresh visitor (no session keys) ──────────────────────
  for (const [viewport, tag] of [
    [DESKTOP, "desktop_1440x900"],
    [MOBILE, "mobile_390x844"],
  ]) {
    const { context, page } = await freshPage(browser, viewport);
    await page.goto(`${BASE_URL}/`, { waitUntil: "networkidle" });
    const landing = page.getByTestId("grace2-landing");
    if (!(await landing.isVisible())) {
      throw new Error(`FRESH "/" did not render the landing (${tag})`);
    }
    await page.waitForTimeout(500); // let webp imagery decode
    await page.screenshot({ path: `${OUT_DIR}/landing_${tag}.png` });
    await page.screenshot({
      path: `${OUT_DIR}/landing_${tag.split("_")[0]}_full.png`,
      fullPage: true,
    });
    console.log(`[OK] landing ${tag}`);
    await context.close();
  }

  // ── 5-6: privacy policy ────────────────────────────────────────────────
  for (const [viewport, tag] of [
    [DESKTOP, "desktop_1440x900"],
    [MOBILE, "mobile_390x844"],
  ]) {
    const { context, page } = await freshPage(browser, viewport);
    await page.goto(`${BASE_URL}/privacy`, { waitUntil: "networkidle" });
    const privacy = page.getByTestId("grace2-privacy");
    if (!(await privacy.isVisible())) {
      throw new Error(`"/privacy" did not render the policy (${tag})`);
    }
    await page.screenshot({ path: `${OUT_DIR}/privacy_${tag}.png` });
    await page.screenshot({
      path: `${OUT_DIR}/privacy_${tag.split("_")[0]}_full.png`,
      fullPage: true,
    });
    console.log(`[OK] privacy ${tag}`);
    await context.close();
  }

  // ── 7: passthrough proof — seeded session key renders the APP at "/" ──
  {
    const context = await browser.newContext({ viewport: DESKTOP });
    await context.addInitScript(() => {
      try {
        localStorage.setItem("grace2_anonymous_accepted", "true");
      } catch {}
    });
    const page = await context.newPage();
    await page.goto(`${BASE_URL}/`, { waitUntil: "networkidle" });
    await page.waitForTimeout(1500); // lazy App chunk + map paint
    const landingVisible = await page
      .getByTestId("grace2-landing")
      .isVisible()
      .catch(() => false);
    if (landingVisible) {
      throw new Error(
        'PASSTHROUGH BROKEN: "/" rendered the landing despite a session key',
      );
    }
    await page.screenshot({
      path: `${OUT_DIR}/passthrough_root_with_session.png`,
    });
    console.log("[OK] passthrough: '/' + session key → app (not landing)");
    await context.close();
  }

  await browser.close();
  console.log(`[DONE] evidence in ${OUT_DIR}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
