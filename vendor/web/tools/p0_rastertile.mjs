// Extract the EXACT raster tile template + a concrete substituted tile URL for the
// stuck fort-myers-roads source, and report it. Also capture any /cog request that
// DOES fire (incl. failed) with full URL + status/errorText.
import { chromium } from "playwright";
import fs from "node:fs";

const SITE = "http://127.0.0.1:5180/app";
const OUT = "/home/nate/Documents/GRACE-2/reports/inflight/p0_rastertile";
const BUDGET_MS = 9 * 60 * 1000;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const cogReq = [], cogResp = [], cogFail = [];
page.on("request", (r) => { if (r.url().includes("/cog/")) cogReq.push(r.url()); });
page.on("response", (r) => { if (r.url().includes("/cog/")) cogResp.push(`${r.status()} ${r.url().slice(0,140)}`); });
page.on("requestfailed", (r) => { if (r.url().includes("/cog/")) cogFail.push(`${r.failure()?.errorText} ${r.url().slice(0,140)}`); });

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

const info = await page.evaluate(() => {
  const m = window.__grace2GetMap?.();
  if (!m) return { err: "no map" };
  const out = {};
  const st = m.getStyle();
  out.rasterTemplate = st.sources?.["fort-myers-roads"]?.tiles?.[0] || null;
  // concrete tile url for one stuck tile
  const sc = m.style.sourceCaches?.["fort-myers-roads"];
  out.tileURLs = [];
  if (sc && sc._tiles) {
    for (const k of Object.keys(sc._tiles).slice(0, 3)) {
      const t = sc._tiles[k];
      out.tileURLs.push({ state: t.state, key: k, request: t.request?.url || (t.tileID && JSON.stringify(t.tileID.canonical)) });
    }
  }
  return out;
});

console.log("\n========== RASTER TILE FORENSICS ==========");
console.log(`fort-myers-roads tile template:\n   ${info.rasterTemplate}`);
console.log(`sample stuck tile states: ${JSON.stringify(info.tileURLs, null, 0)}`);
console.log(`\n/cog requests fired (${cogReq.length}):`);
for (const u of cogReq.slice(0, 6)) console.log("   " + u.slice(0, 160));
console.log(`/cog responses (${cogResp.length}):`);
for (const u of cogResp.slice(0, 6)) console.log("   " + u);
console.log(`/cog requestfailed (${cogFail.length}):`);
for (const u of cogFail.slice(0, 6)) console.log("   " + u);

fs.writeFileSync(`${OUT}_info.json`, JSON.stringify({ info, cogReq: cogReq.slice(0,10), cogResp, cogFail }, null, 2));
console.log(`\n[saved] ${OUT}_info.json`);
await browser.close();
process.exit(0);
