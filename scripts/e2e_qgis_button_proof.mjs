/** Final proof shots: Export-to-QGIS button on the SFINCS flood case +
 *  debris-flow layer zoomed on the map. Pure UI, no LLM. */
import { createRequire } from "module";
import { fileURLToPath } from "url";
import path from "path";
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const req = createRequire(import.meta.url);
const playwright = req(path.join(__dirname, "../vendor/web/node_modules/playwright"));
const PROOF = path.resolve(__dirname, "../docs/proof");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));


// DATA-INTEGRITY GUARD (2026-07-12): the local server maps EVERY anonymous
// session to one shared local user, so a fresh boot RESUMES that user's
// last-active REAL case. Prompting without creating a case first mutated
// real cases (bbox overwrite + layer pollution). Always create a brand-new
// case before sending any prompt; never select or reuse an existing case.
async function createFreshCase(page) {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const btn = page.locator('[data-testid="grace2-cases-new"]').first();
  if (await btn.isVisible().catch(() => false)) {
    await btn.click().catch(() => {});
  } else {
    const roleBtn = page.getByRole("button", { name: /new case/i }).first();
    if (await roleBtn.count().catch(() => 0)) {
      await roleBtn.click().catch(() => {});
    } else {
      throw new Error("createFreshCase: no new-case button; refusing to prompt into an existing case");
    }
  }
  await wait(2500);
  const gate = page.locator('[data-testid="grace2-save-gate-modal-continue"]').first();
  if (await gate.isVisible().catch(() => false)) {
    await gate.click().catch(() => {});
    await wait(800);
  }
}

async function main() {
  const browser = await playwright.chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
  await page.goto("http://127.0.0.1:5173/app", { waitUntil: "domcontentloaded" });
  await sleep(6000);
  const cont = page.locator('button:has-text("Continue")').first();
  if (await cont.isVisible().catch(() => false)) await cont.click();
  await sleep(3000);

  // DATA-INTEGRITY (2026-07-12): the local server maps all anon sessions to
  // one shared user, so boot resumes a REAL case. Create a fresh case first.
  await createFreshCase(page);
  const input = page.locator('[data-testid="chat-input"] textarea, textarea').first();
  await input.click({ force: true });
  await input.fill("Fetch a digital elevation model and the county boundaries for downtown Tampa, Florida.");
  const actionBtn = page.locator('[data-testid="chat-input-action"]').first();
  if (await actionBtn.isVisible().catch(() => false)) await actionBtn.click();
  else await input.press("Enter");
  console.log("[proof] seed prompt sent");
  const t0 = Date.now();
  while (Date.now() - t0 < 360000) {
    for (const sel of ['[data-testid="resolution-picker-confirm"]', 'button:has-text("Confirm")']) {
      const el = page.locator(sel).first();
      if (await el.isVisible().catch(() => false)) await el.click().catch(() => {});
    }
    const body = await page.textContent("body").catch(() => "");
    if (/elevation/i.test(body || "") && /(count(y|ies)|boundar)/i.test(body || "") &&
        (await page.locator('[data-testid="grace2-case-row-menu-button"]').count().catch(() => 0)) > 0) break;
    await sleep(8000);
  }
  await page.screenshot({ path: path.join(PROOF, "_debug-boot.png") });
  // back to the cases panel via the breadcrumb; the kebab lives on panel rows
  const crumb = page.locator('[data-testid="grace2-case-view-cases-link"], [data-testid="grace2-case-view-back"]').first();
  await crumb.click().catch(() => {});
  await sleep(2500);
  await page.screenshot({ path: path.join(PROOF, "_debug-after-crumb.png") });
  const anyRow = page.locator('[data-testid="grace2-case-row-menu-button"]').first();
  await anyRow.hover().catch(() => {});
  await sleep(800);
  // hover reveals the row's menu button; pick the one in the same row
  const rowKebabs = page.locator('[data-testid="grace2-case-row-menu-button"]');
  const n = await rowKebabs.count();
  console.log("[proof] kebab buttons in DOM:", n);
  if (n > 0) await rowKebabs.first().click({ force: true }).catch((e) => console.log("[proof] kebab click err", String(e).slice(0,80)));
  await sleep(1200);
  await page.screenshot({ path: path.join(PROOF, "32-qgis-export-menu.png") });
  console.log("[proof] 32-qgis-export-menu.png");

  const item = page.locator('text="Export to QGIS"').first();
  if (await item.isVisible().catch(() => false)) {
    await item.click();
    const t = Date.now();
    while (Date.now() - t < 300000) {
      const body = await page.textContent("body").catch(() => "");
      if (/QGIS project ready/i.test(body || "")) break;
      if (/QGIS export failed/i.test(body || "")) break;
      await sleep(3000);
    }
    await sleep(1500);
  }
  await page.screenshot({ path: path.join(PROOF, "33-qgis-export-done.png") });
  console.log("[proof] 33-qgis-export-done.png");

  // ---- B. debris case map, zoomed to the segments bbox ----
  // close the menu, open the debris case
  await page.keyboard.press("Escape").catch(() => {});
  await sleep(800);
  const debrisRow = page.locator("text=/Yes Proceed Now|Post-fire Debris/i").first();
  if (await debrisRow.isVisible().catch(() => false)) {
    await debrisRow.click();
    await sleep(5000);
    // fly the map to the Park Fire bbox via keyboard-less trick: use the
    // map's exposed maplibre instance if present, else zoom buttons
    await page.evaluate(() => {
      const m = window.__grace2_map || window.map || null;
      if (m && m.fitBounds) m.fitBounds([[-121.92, 39.78], [-121.76, 39.92]], { duration: 0 });
    }).catch(() => {});
    await sleep(4000);
    await page.screenshot({ path: path.join(PROOF, "31-debris-flow-map.png") });
    console.log("[proof] 31-debris-flow-map.png (zoomed)");
  }
  await browser.close();
  console.log("[proof] DONE");
}
main().catch((e) => { console.error("[proof] FATAL", e); process.exit(1); });
