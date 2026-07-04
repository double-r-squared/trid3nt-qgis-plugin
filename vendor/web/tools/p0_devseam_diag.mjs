// GOLD-STANDARD diagnostic: a LOCAL DEV bundle (all seams + [MapView] logs ON)
// pointed at the PROD backend (wss://...cloudfront.net/ws). Anon (no Cognito).
// Sends the real roads prompt to the real agent, captures every [MapView]/[Map]
// log, then reads __grace2GetMap().getStyle() to see EXACTLY whether the two
// published layers got addSource/addLayer'd, plus isStyleLoaded state.
import { chromium } from "playwright";
import fs from "node:fs";

const SITE = "http://127.0.0.1:5180/app";
const OUT = "/home/nate/Documents/GRACE-2/reports/inflight/p0_devseam";
const BUDGET_MS = 10 * 60 * 1000;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const allLogs = [];
const mapLogs = [];
page.on("console", (m) => {
  const t = `${m.type()}: ${m.text()}`;
  allLogs.push(t.slice(0, 400));
  if (/\[MapView\]|\[Map\]/.test(m.text())) mapLogs.push(t.slice(0, 400));
});
page.on("pageerror", (e) => allLogs.push("PAGEERROR: " + String(e).slice(0, 400)));
const tileResp = [];
page.on("response", (r) => { const u = r.url(); if (u.includes("/cog/tiles/")) tileResp.push(`${r.status()} ${u.slice(0,90)}`); });

await page.goto(SITE, { waitUntil: "networkidle", timeout: 60000 });
await page.waitForTimeout(4000);
// anon — chat input should be present without sign-in
let chatInput = await page.locator('[data-testid="chat-input"]').count();
console.log(`[boot] chatInput=${chatInput} url=${page.url().slice(0,50)}`);
if (!chatInput) {
  // maybe an auth wall — try clicking continue-anon / skip if present
  await page.getByRole("button", { name: /continue|skip|anon|explore|got it/i }).first().click().catch(() => {});
  await page.waitForTimeout(3000);
  chatInput = await page.locator('[data-testid="chat-input"]').count();
  console.log(`[boot-retry] chatInput=${chatInput}`);
}
const seam0 = await page.evaluate(() => ({ getMap: typeof window.__grace2GetMap === "function", hasMap: typeof window.__grace2GetMap === "function" ? !!window.__grace2GetMap() : false, inject: typeof window.__grace2InjectMapCommand === "function" }));
console.log(`[seam] getMap=${seam0.getMap} hasMap=${seam0.hasMap} injectMapCmd=${seam0.inject}`);
await page.screenshot({ path: `${OUT}_0_boot.png` });
if (!chatInput) { console.log("[FATAL] no chat input"); fs.writeFileSync(`${OUT}_alllogs.log`, allLogs.join("\n")); await browser.close(); process.exit(2); }

const input = page.locator('[data-testid="chat-input"]');
await input.fill("Add the roads in Fort Myers, Florida as a layer.");
await input.press("Enter");
console.log("[prompt] sent to PROD agent via dev bundle");

const start = Date.now();
let panelIds = [];
while (Date.now() - start < BUDGET_MS) {
  await page.waitForTimeout(4000);
  for (const sel of ['[data-testid="payload-warning-button-proceed"]']) { const b = page.locator(sel); if (await b.count()) await b.first().click().catch(()=>{}); }
  for (const n of [/^Proceed$/i,/^Run$/i,/^Approve$/i]) { const b = page.getByRole("button",{name:n}); if (await b.count()) await b.first().click().catch(()=>{}); }
  panelIds = await page.evaluate(() => Array.from(document.querySelectorAll("[data-layer-id]")).map((r) => r.getAttribute("data-layer-id")));
  const t = Math.round((Date.now()-start)/1000);
  if (t % 40 < 4) console.log(`[t${t}s] panel=${panelIds.length} mapLogs=${mapLogs.length} tiles=${tileResp.length}`);
  if (panelIds.length >= 1 && t > 15) { await page.waitForTimeout(10000); break; }
}

const probe = await page.evaluate((panelIds) => {
  const out = { styleLoaded: null, sources: [], layers: [], matches: {}, err: null };
  const m = window.__grace2GetMap?.();
  if (!m) { out.err = "no map"; return out; }
  try {
    out.styleLoaded = m.isStyleLoaded();
    const s = m.getStyle();
    out.sources = Object.keys(s.sources || {});
    out.layers = (s.layers || []).map((l) => ({ id: l.id, type: l.type, source: l.source, vis: l.layout?.visibility }));
    for (const id of panelIds) {
      let spec = null; try { const ss = s.sources[id]; spec = ss ? { type: ss.type, hasData: ss.data !== undefined, dataFeatures: ss.data?.features?.length, tiles: ss.tiles?.[0]?.slice(0,90) } : null; } catch {}
      out.matches[id] = { src: !!m.getSource(id), layer: !!m.getLayer(id), spec };
    }
  } catch (e) { out.err = String(e); }
  return out;
}, panelIds);

console.log("\n========== __grace2GetMap STYLE PROBE (dev seam, prod data) ==========");
console.log(`styleLoaded=${probe.styleLoaded} err=${probe.err}`);
console.log(`panelIds(${panelIds.length}): ${JSON.stringify(panelIds)}`);
console.log(`sources(${probe.sources.length}): ${JSON.stringify(probe.sources)}`);
console.log(`layers(${probe.layers.length}):`);
for (const l of probe.layers) console.log(`   - ${l.id} [${l.type}] src=${l.source} vis=${l.vis}`);
console.log("per-panel-layer in style:");
for (const [id, v] of Object.entries(probe.matches)) console.log(`   - ${id}: src=${v.src} layer=${v.layer} spec=${JSON.stringify(v.spec)}`);

console.log(`\n========== [MapView]/[Map] LOGS (${mapLogs.length}) ==========`);
for (const l of mapLogs) console.log("   " + l);

console.log(`\n/cog/tiles responses (${tileResp.length}):`);
for (const t of tileResp.slice(0,12)) console.log("   " + t);

console.log("\n========== errors/warnings in console ==========");
for (const l of allLogs.filter((x)=>/^error|^warning|PAGEERROR|abort|fail/i.test(x)).slice(-25)) console.log("   " + l);

await page.screenshot({ path: `${OUT}_final.png` });
fs.writeFileSync(`${OUT}_alllogs.log`, allLogs.join("\n"));
fs.writeFileSync(`${OUT}_probe.json`, JSON.stringify(probe, null, 2));
console.log(`\n[saved] ${OUT}_final.png + ${OUT}_alllogs.log + ${OUT}_probe.json`);
await browser.close();
process.exit(0);
