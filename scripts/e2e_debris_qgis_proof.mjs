/**
 * e2e_debris_qgis_proof.mjs -- proof screenshots for the two new tools:
 *   1. model_debris_flow driven by the LOCAL LLM over the 2024 Park Fire scar
 *      -> 30-debris-flow-chat.png / 31-debris-flow-map.png
 *   2. the user-driven "Export to QGIS" kebab item on that case
 *      -> 32-qgis-export-menu.png / 33-qgis-export-done.png
 *
 * Run: node scripts/e2e_debris_qgis_proof.mjs
 */

import { createRequire } from "module";
import { fileURLToPath } from "url";
import path from "path";
import fs from "fs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const VENDOR_WEB = path.resolve(__dirname, "../vendor/web");
const PROOF_DIR = path.resolve(__dirname, "../docs/proof");
const APP_URL = "http://127.0.0.1:5173";
const req = createRequire(import.meta.url);
const playwright = req(path.join(VENDOR_WEB, "node_modules/playwright"));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function sendPrompt(page, text) {
  const input = page.locator('[data-testid="chat-input"] textarea, textarea').first();
  await input.click({ force: true });
  await input.fill(text);
  const actionBtn = page.locator('[data-testid="chat-input-action"]').first();
  if (await actionBtn.isVisible().catch(() => false)) await actionBtn.click();
  else await input.press("Enter");
  console.log("[proof] sent:", text.slice(0, 70));
}

async function clickAnyConfirm(page) {
  for (const sel of [
    '[data-testid="resolution-picker-confirm"]',
    'button:has-text("Confirm")',
    'button:has-text("Proceed")',
    'button:has-text("Run")',
  ]) {
    const el = page.locator(sel).first();
    if (await el.isVisible().catch(() => false)) {
      await el.click().catch(() => {});
      console.log("[proof] clicked confirmation:", sel);
      return true;
    }
  }
  return false;
}

async function main() {
  fs.mkdirSync(PROOF_DIR, { recursive: true });
  const browser = await playwright.chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
  page.on("console", (m) => { if (m.type() === "error") console.log("[page-err]", m.text().slice(0, 120)); });

  await page.goto(APP_URL + "/app", { waitUntil: "domcontentloaded", timeout: 30000 });
  await sleep(6000);
  const cont = page.locator('button:has-text("Continue")').first();
  if (await cont.isVisible().catch(() => false)) await cont.click();
  await sleep(4000);

  // open the existing debris case when present (retry flow), else stay on the fresh case
  const priorRow = page.locator('text=/Post-fire Debris/i').first();
  if (await priorRow.isVisible().catch(() => false)) { await priorRow.click(); await sleep(3000); }

  // ---- 1. debris flow, LLM-driven ----
  await sendPrompt(
    page,
    "Yes, proceed now: call the model_debris_flow tool with " +
    "bbox = [-121.90, 39.80, -121.78, 39.90] and rainfall_intensity_mm_h = 24. " +
    "Do not ask again."
  );

  // watch up to 12 min: confirm any gates, stop when a debris layer or a final reply lands
  const t0 = Date.now();
  let sawTool = false;
  while (Date.now() - t0 < 12 * 60 * 1000) {
    await clickAnyConfirm(page);
    const body = await page.textContent("body").catch(() => "");
    if (!sawTool && /debris/i.test(body || "")) {
      sawTool = true;
      console.log("[proof] debris activity visible in UI");
    }
    if (/hazard class|debris-flow (segments|hazard)|High hazard/i.test(body || "")) break;
    // layer panel entry is the strongest completion signal
    const layer = page.locator('text=/debris/i').first();
    if (sawTool && (await layer.count().catch(() => 0)) > 1) break;
    await sleep(10000);
  }
  await sleep(3000);
  await page.screenshot({ path: path.join(PROOF_DIR, "30-debris-flow-chat.png") });
  console.log("[proof] 30-debris-flow-chat.png");

  // map view: try zoom-to-layer via the layer row if present, else raw map
  const zoomBtn = page.locator('[title*="Zoom"], [data-testid*="zoom-to"]').first();
  if (await zoomBtn.isVisible().catch(() => false)) { await zoomBtn.click().catch(() => {}); await sleep(2500); }
  await page.screenshot({ path: path.join(PROOF_DIR, "31-debris-flow-map.png") });
  console.log("[proof] 31-debris-flow-map.png");

  // ---- 2. Export to QGIS via the kebab menu ----
  // open the cases panel (breadcrumb/back), then the row kebab
  for (const sel of ['[data-testid="cases-button"]', 'button:has-text("Cases")', '[aria-label="Cases"]']) {
    const el = page.locator(sel).first();
    if (await el.isVisible().catch(() => false)) { await el.click().catch(() => {}); break; }
  }
  await sleep(2000);
  const caseRow = page.locator('text=/Post-fire Debris/i').first();
  if (await caseRow.isVisible().catch(() => false)) await caseRow.hover().catch(() => {});
  await sleep(800);
  const kebab = page.locator('[data-testid="grace2-case-row-menu-button"]').first();
  if (await kebab.isVisible().catch(() => false)) {
    await kebab.click();
    await sleep(1200);
  } else {
    console.log("[proof] WARN: case-row menu button not visible");
  }
  await page.screenshot({ path: path.join(PROOF_DIR, "32-qgis-export-menu.png") });
  console.log("[proof] 32-qgis-export-menu.png");

  const exportItem = page.locator('text="Export to QGIS"').first();
  if (await exportItem.isVisible().catch(() => false)) {
    await exportItem.click();
    // wait for the ready line (export downloads the qgz too)
    const t1 = Date.now();
    while (Date.now() - t1 < 120000) {
      const body = await page.textContent("body").catch(() => "");
      if (/QGIS project ready/i.test(body || "")) break;
      if (/error|failed/i.test(body || "") && /qgis/i.test(body || "")) break;
      await sleep(3000);
    }
    await sleep(1500);
  } else {
    console.log("[proof] WARN: Export to QGIS item not visible");
  }
  await page.screenshot({ path: path.join(PROOF_DIR, "33-qgis-export-done.png") });
  console.log("[proof] 33-qgis-export-done.png");

  await browser.close();
  console.log("[proof] DONE");
}

main().catch((e) => { console.error("[proof] FATAL", e); process.exit(1); });
