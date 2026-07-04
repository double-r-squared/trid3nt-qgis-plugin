// GRACE-2 web — Playwright config (job-0027).
//
// Used both by `tools/screenshot.mjs` (for the AFK iteration loop —
// `make screenshot` / `make ui-tour`) and by the M3 acceptance suite that
// job-0028 will populate under `tests/m3/playwright/`. Closes job-0016
// OQ-W-3 (Chromium provisioning on a fresh Debian dev box) by making the
// browser install a `make playwright-install` away.
//
// Two projects:
//   - chromium       — Playwright's Chrome-for-Testing build (cf. job-0016 AC4)
//   - firefox-esr    — Playwright's Firefox build (closest to the Firefox-ESR
//                      track on Debian; Playwright tracks the rapid-release
//                      Firefox upstream — see OQ in report.md).
//
// Viewport 1440x900 matches the screenshot CLI default (matches the
// memory-file pattern feedback_playwright_afk_iteration_loop.md), so
// canonical reference captures are consistent with ad-hoc ones.
//
// baseURL is the local Vite dev server (`make run-web` on 5173). Tests
// that target the deployed QGIS Server tiles still go through the dev
// client; the substrate URL travels via VITE_GRACE2_WMS_URL inside the
// client (a job-0025/0026 surface, not a Playwright concern here).

import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "tests/m3/playwright",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? "line" : "list",
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? "http://localhost:5173",
    viewport: { width: 1440, height: 900 },
    ignoreHTTPSErrors: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
    {
      name: "firefox-esr",
      use: { ...devices["Desktop Firefox"], viewport: { width: 1440, height: 900 } },
    },
  ],
});
