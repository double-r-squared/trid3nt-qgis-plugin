#!/usr/bin/env node
// GRACE-2 — Wave 4.10 Stage 3 job-C1 tools catalog screenshot.
//
// Drives the live agent + web stack:
//   1. Vite dev server on 5173 (or 5177 fallback) serves the React app.
//   2. The app fetches /api/tool-catalog from the agent's HTTP listener
//      (default port 8766) — no mock fetch, no inject seam.
//   3. We open Settings → "View all tools" and screenshot the catalog.
//
// Output: ${OUT_DIR}/tools_catalog.png and /tmp/wave4_10_c1_tools_catalog.png

import { chromium } from "@playwright/test";
import { mkdir, copyFile } from "fs/promises";

const OUT_DIR =
  process.argv[2] ??
  "/home/nate/Documents/GRACE-2/reports/inflight/wave-4-10-c1-tools-catalog-20260609/evidence";
const BASE_URLS = (
  process.env.GRACE2_DEV_URL
    ? [process.env.GRACE2_DEV_URL]
    : ["http://localhost:5173", "http://localhost:5177"]
);

async function pickWorkingBaseUrl(page) {
  for (const url of BASE_URLS) {
    try {
      const resp = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 6000 });
      if (resp && resp.status() < 500) {
        console.log(`[screenshot] using base URL ${url}`);
        return url;
      }
    } catch (err) {
      console.log(`[screenshot] ${url} unreachable: ${err.message}`);
    }
  }
  throw new Error("no Vite dev server reachable on 5173 or 5177");
}

async function main() {
  await mkdir(OUT_DIR, { recursive: true });

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
  });
  const page = await context.newPage();

  // Pre-accept anonymous so the AuthGate doesn't block. (NOT an inject seam
  // for chat state — only a localStorage flag that unblocks the shell render.
  // The catalog fetch still goes through the real HTTP endpoint.)
  await page.addInitScript(() => {
    try {
      localStorage.setItem("grace2_anonymous_accepted", "true");
    } catch {}
  });

  page.on("console", (msg) => {
    if (msg.type() === "error") {
      console.warn(`[screenshot] console.error: ${msg.text()}`);
    }
  });
  page.on("pageerror", (err) =>
    console.warn(`[screenshot] pageerror: ${err.message}`),
  );

  await pickWorkingBaseUrl(page);
  await page.waitForSelector('[data-testid="grace2-app-shell"]', {
    timeout: 15000,
  });
  console.log("[screenshot] app shell mounted");

  // Open Settings popup via the bottom-row pill.
  await page.waitForSelector('[data-testid="grace2-bottom-row-settings"]', {
    timeout: 5000,
  });
  await page.click('[data-testid="grace2-bottom-row-settings"]');
  await page.waitForSelector('[data-testid="grace2-settings-popup"]', {
    timeout: 5000,
  });
  await page.waitForSelector('[data-testid="grace2-settings-open-tools-catalog"]', {
    timeout: 5000,
  });

  // Click "View all tools" → opens ToolsCatalogPopup.
  await page.click('[data-testid="grace2-settings-open-tools-catalog"]');
  await page.waitForSelector('[data-testid="grace2-tools-catalog-popup"]', {
    timeout: 5000,
  });
  // Wait for the catalog to actually load — either ready or error.
  await page.waitForFunction(
    () =>
      !!document.querySelector('[data-testid="grace2-tools-catalog-list"]') ||
      !!document.querySelector('[data-testid="grace2-tools-catalog-error"]'),
    null,
    { timeout: 15000 },
  );

  const errored = await page
    .locator('[data-testid="grace2-tools-catalog-error"]')
    .count();
  if (errored > 0) {
    const errText = await page
      .locator('[data-testid="grace2-tools-catalog-error"]')
      .innerText();
    console.error(`[FAIL] catalog endpoint error: ${errText}`);
    await page.screenshot({
      path: `${OUT_DIR}/tools_catalog_error.png`,
      fullPage: false,
    });
    await browser.close();
    process.exit(1);
  }

  // Count rows for the report.
  const rowCount = await page
    .locator('[data-testid="grace2-tools-catalog-row"]')
    .count();
  const categoryCount = await page
    .locator('[data-testid^="grace2-tools-catalog-category-"]')
    .count();
  console.log(
    `[screenshot] catalog ready — ${rowCount} tools, ${categoryCount} categories`,
  );

  await page.waitForTimeout(300);

  // (1) Initial state — full catalog visible.
  await page.screenshot({
    path: `${OUT_DIR}/tools_catalog.png`,
    fullPage: false,
  });
  console.log("[screenshot] tools_catalog.png saved");
  await copyFile(`${OUT_DIR}/tools_catalog.png`, "/tmp/wave4_10_c1_tools_catalog.png");
  console.log("[screenshot] /tmp/wave4_10_c1_tools_catalog.png copied");

  // (2) Click a category to demonstrate filtering.
  const firstCategory = await page
    .locator('[data-testid^="grace2-tools-catalog-category-"]')
    .first();
  const firstCategoryId = await firstCategory.getAttribute("data-testid");
  if (firstCategoryId) {
    await firstCategory.click();
    await page.waitForTimeout(200);
    await page.screenshot({
      path: `${OUT_DIR}/tools_catalog_filtered.png`,
      fullPage: false,
    });
    console.log(
      `[screenshot] tools_catalog_filtered.png saved (filter: ${firstCategoryId})`,
    );
    // Clear the filter.
    await firstCategory.click();
    await page.waitForTimeout(200);
  }

  // (3) Apply a search filter.
  const searchInput = page.locator('[data-testid="grace2-tools-catalog-search"]');
  await searchInput.fill("flood");
  await page.waitForTimeout(400);
  await page.screenshot({
    path: `${OUT_DIR}/tools_catalog_searched.png`,
    fullPage: false,
  });
  console.log("[screenshot] tools_catalog_searched.png saved (search: 'flood')");
  await searchInput.fill("");
  await page.waitForTimeout(400);

  console.log("[OK] all C1 screenshots saved");
  await browser.close();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
