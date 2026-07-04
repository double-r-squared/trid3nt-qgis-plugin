// FINAL pinpoint: read Style internals to find which Style.loaded() condition is
// false — _updatedSources, imageManager.isLoaded() (sprite), or a stuck tile in a
// sourceCache. Also dump each sourceCache's tile states for fort-myers-roads.
import { chromium } from "playwright";
import fs from "node:fs";

const SITE = "http://127.0.0.1:5180/app";
const OUT = "/home/nate/Documents/GRACE-2/reports/inflight/p0_internals";
const BUDGET_MS = 9 * 60 * 1000;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
await page.goto(SITE, { waitUntil: "networkidle", timeout: 60000 });
await page.waitForTimeout(4000);
if (!(await page.locator('[data-testid="chat-input"]').count())) {
  await page.getByRole("button", { name: /continue|skip|anon|explore|got it/i }).first().click().catch(() => {});
  await page.waitForTimeout(3000);
}
const input = page.locator('[data-testid="chat-input"]');
await input.fill("Add the roads in Fort Myers, Florida as a layer.");
await input.press("Enter");
console.log("[prompt] sent");

const start = Date.now();
let panelIds = [];
while (Date.now() - start < BUDGET_MS) {
  await page.waitForTimeout(4000);
  panelIds = await page.evaluate(() => Array.from(document.querySelectorAll("[data-layer-id]")).map((r) => r.getAttribute("data-layer-id")));
  if (panelIds.length >= 1 && Date.now() - start > 15000) { await page.waitForTimeout(12000); break; }
}

const probe = await page.evaluate(() => {
  const m = window.__grace2GetMap?.();
  if (!m) return { err: "no map" };
  const style = m.style;
  const out = {
    isStyleLoaded: m.isStyleLoaded(),
    style_loaded: style._loaded,
    updatedSources: Object.keys(style._updatedSources || {}),
    imageManagerLoaded: style.imageManager ? style.imageManager.isLoaded() : "n/a",
    changed: style._changed,
    glyphsPending: style.glyphManager ? "(glyphManager present)" : "none",
    sourceCaches: {},
  };
  for (const id in style.sourceCaches) {
    const sc = style.sourceCaches[id];
    const tileStates = {};
    try {
      const tiles = sc._tiles || {};
      for (const k in tiles) {
        const s = tiles[k].state; // loading | loaded | errored | expired | unloaded | reloading
        tileStates[s] = (tileStates[s] || 0) + 1;
      }
    } catch (e) { tileStates.err = String(e); }
    out.sourceCaches[id] = { loaded: sc.loaded(), used: sc.used, tiles: tileStates, tileCount: Object.keys(sc._tiles || {}).length };
  }
  return out;
});

console.log("\n========== STYLE INTERNALS ==========");
console.log(`isStyleLoaded=${probe.isStyleLoaded} style._loaded=${probe.style_loaded} _changed=${probe.changed}`);
console.log(`_updatedSources (pending): ${JSON.stringify(probe.updatedSources)}`);
console.log(`imageManager.isLoaded(): ${probe.imageManagerLoaded}  <-- sprite gate`);
console.log("sourceCaches tile states:");
for (const [id, v] of Object.entries(probe.sourceCaches || {})) {
  console.log(`   - ${id}: loaded=${v.loaded} used=${v.used} tileCount=${v.tileCount} states=${JSON.stringify(v.tiles)}`);
}
await page.screenshot({ path: `${OUT}_final.png` });
fs.writeFileSync(`${OUT}_probe.json`, JSON.stringify(probe, null, 2));
console.log(`\n[saved] ${OUT}_final.png + ${OUT}_probe.json`);
await browser.close();
process.exit(0);
