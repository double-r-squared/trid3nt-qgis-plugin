// PINPOINT: why does map.isStyleLoaded() stay false (so overlays never paint /
// raster tiles never request)? Read per-source loaded() states, style._loaded,
// areTilesLoaded(), and capture map 'error' + 'dataloading'/'idle' events plus
// every basemap tile network status. Dev seam, prod backend, anon.
import { chromium } from "playwright";
import fs from "node:fs";

const SITE = "http://127.0.0.1:5180/app";
const OUT = "/home/nate/Documents/GRACE-2/reports/inflight/p0_styleloaded";
const BUDGET_MS = 9 * 60 * 1000;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const wmsResp = [], cogResp = [], wmsFail = [];
page.on("response", (r) => {
  const u = r.url();
  if (u.includes("ogc/wms")) wmsResp.push(r.status());
  if (u.includes("/cog/tiles/")) cogResp.push(`${r.status()}`);
});
page.on("requestfailed", (r) => { const u = r.url(); if (u.includes("ogc/wms")) wmsFail.push(r.failure()?.errorText || "?"); });

await page.goto(SITE, { waitUntil: "networkidle", timeout: 60000 });
await page.waitForTimeout(4000);
if (!(await page.locator('[data-testid="chat-input"]').count())) {
  await page.getByRole("button", { name: /continue|skip|anon|explore|got it/i }).first().click().catch(() => {});
  await page.waitForTimeout(3000);
}
// install map-event listeners NOW (before layers) via the dev seam
await page.evaluate(() => {
  const m = window.__grace2GetMap?.();
  window.__ev = { errors: [], idleCount: 0, styledata: 0 };
  if (m) {
    m.on("error", (e) => window.__ev.errors.push(String(e?.error?.message || e?.error || e).slice(0, 200)));
    m.on("idle", () => { window.__ev.idleCount++; });
    m.on("styledata", () => { window.__ev.styledata++; });
  }
});

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
  const out = { err: null };
  if (!m) return { err: "no map" };
  try {
    out.isStyleLoaded = m.isStyleLoaded();
    out.areTilesLoaded = typeof m.areTilesLoaded === "function" ? m.areTilesLoaded() : "n/a";
    out.style_loaded = m.style ? m.style._loaded : "n/a";
    const sources = {};
    const st = m.getStyle();
    for (const id of Object.keys(st.sources || {})) {
      let loaded = null;
      try { const s = m.getSource(id); loaded = s && typeof s.loaded === "function" ? s.loaded() : "n/a"; } catch (e) { loaded = "err:" + e; }
      sources[id] = loaded;
    }
    out.sourceLoaded = sources;
    out.events = window.__ev;
    // What does MapLibre think is pending? sample _sourceCaches tile states.
  } catch (e) { out.err = String(e); }
  return out;
});

console.log("\n========== STYLE-LOADED PINPOINT ==========");
console.log(`isStyleLoaded=${probe.isStyleLoaded} areTilesLoaded=${probe.areTilesLoaded} style._loaded=${probe.style_loaded}`);
console.log(`per-source loaded(): ${JSON.stringify(probe.sourceLoaded, null, 0)}`);
console.log(`map events: ${JSON.stringify(probe.events)}`);
console.log(`\nWMS basemap responses: count=${wmsResp.length} statuses=${JSON.stringify([...new Set(wmsResp)])} (sample ${wmsResp.slice(0,8)})`);
console.log(`WMS basemap requestfailed: count=${wmsFail.length} reasons=${JSON.stringify([...new Set(wmsFail)])}`);
console.log(`/cog/tiles responses: count=${cogResp.length} statuses=${JSON.stringify([...new Set(cogResp)])}`);

await page.screenshot({ path: `${OUT}_final.png` });
fs.writeFileSync(`${OUT}_probe.json`, JSON.stringify(probe, null, 2));
console.log(`\n[saved] ${OUT}_final.png + ${OUT}_probe.json`);
await browser.close();
process.exit(0);
