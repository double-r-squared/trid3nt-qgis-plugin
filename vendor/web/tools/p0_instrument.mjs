// DEFINITIVE instrumentation: patch the page BEFORE the bundle runs so we can see
// (1) every WebGL/raster tile the map tries to load (via Image src + fetch),
// (2) whether the published layers' tile templates are EVER requested,
// (3) the map's reconcile behaviour by grabbing the MapLibre Map prototype from
//     the FIRST constructed instance and instrumenting addSource/addLayer/
//     isStyleLoaded + idle/load/error events.
// We grab the prototype by patching Object.prototype briefly is too invasive;
// instead we patch maplibre via the module: the bundle calls `new Map(...)`. We
// hook the prototype lazily the moment ANY object with _container+getStyle shows
// up by scanning requestAnimationFrame-driven. Simpler+robust: override
// CanvasRenderingContext2D? No — MapLibre is WebGL. We instead hook
// HTMLCanvasElement.getContext to capture the map via the canvas, then walk to
// the Map through its painter. Fallback: log all tile URLs (the real signal).
import { chromium } from "playwright";
import fs from "node:fs";

const SITE = "https://d125yfbyjrpbre.cloudfront.net/app";
const CF = "d125yfbyjrpbre.cloudfront.net";
const EMAIL = process.env.GRACE2_DEMO_EMAIL || "grace2-demo@example.com";
const PW = process.env.GRACE2_DEMO_PASSWORD || "Grace2Demo2026";
const OUT = "/home/nate/Documents/GRACE-2/reports/inflight/p0_instr";
const BUDGET_MS = 10 * 60 * 1000;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });

await page.addInitScript(() => {
  const W = window;
  W.__p0 = { tiles: [], mapEvents: [], addSource: [], addLayer: [], styleLoadedCalls: 0 };
  // Capture every Image-based tile load (MapLibre raster tiles use Image()).
  const ImgProto = window.Image;
  // hook the `src` setter on HTMLImageElement
  const desc = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, "src");
  if (desc && desc.set) {
    Object.defineProperty(HTMLImageElement.prototype, "src", {
      configurable: true,
      enumerable: desc.enumerable,
      get() { return desc.get.call(this); },
      set(v) {
        try { if (typeof v === "string" && (v.includes("/cog/tiles/") || v.includes("ogc/wms") || v.includes("{") === false)) W.__p0.tiles.push(v.slice(0, 140)); } catch {}
        return desc.set.call(this, v);
      },
    });
  }
  // Hook the MapLibre Map prototype the moment a canvas gets a webgl context —
  // the Map instance is reachable from the gl context's canvas in many builds,
  // but more reliably we patch the prototype via the first map we can find on
  // each animation frame. We look for a global maplibregl OR scan for a Map.
  const tryInstrument = () => {
    let MapProto = null;
    if (W.maplibregl && W.maplibregl.Map) MapProto = W.maplibregl.Map.prototype;
    // also: any element with a _maplibre style controller
    if (MapProto && !MapProto.__p0patched) {
      MapProto.__p0patched = true;
      const oAdd = MapProto.addSource, oAddL = MapProto.addLayer, oSL = MapProto.isStyleLoaded;
      MapProto.addSource = function (id, src) { try { W.__p0.addSource.push({ id, type: src && src.type, t: Date.now() }); } catch {}; return oAdd.apply(this, arguments); };
      MapProto.addLayer = function (l, b) { try { W.__p0.addLayer.push({ id: l && l.id, type: l && l.type, t: Date.now() }); } catch {}; return oAddL.apply(this, arguments); };
      MapProto.isStyleLoaded = function () { const r = oSL.apply(this, arguments); try { W.__p0.styleLoadedCalls++; if (W.__p0.styleLoadedCalls <= 200) W.__p0.mapEvents.push("isStyleLoaded=" + r + "@" + Date.now()); } catch {}; return r; };
    }
  };
  const iv = setInterval(tryInstrument, 30);
  setTimeout(() => clearInterval(iv), 60000);
});

const consoleMsgs = [];
const tileResp = [];
page.on("console", (m) => consoleMsgs.push(`${m.type()}: ${m.text().slice(0, 240)}`));
page.on("response", (r) => { const u = r.url(); if (u.includes("/cog/tiles/")) tileResp.push(`${r.status()} ${u.slice(0, 90)}`); });

await page.goto(SITE, { waitUntil: "networkidle", timeout: 60000 });
await page.waitForTimeout(2500);
let chatInput = await page.locator('[data-testid="chat-input"]').count();
if (!chatInput) {
  await page.getByRole("button", { name: /Sign in|Sign up/i }).first().click().catch(() => {});
  await page.waitForTimeout(5000);
  if (/amazoncognito/.test(page.url())) {
    const u = page.locator('input[name="username"]:visible, input[type="email"]:visible').first();
    await u.waitFor({ timeout: 12000 }).catch(() => {});
    await u.fill(EMAIL).catch(() => {});
    await page.locator('input[name="password"]:visible, input[type="password"]:visible').first().fill(PW).catch(() => {});
    await page.locator('input[name="signInSubmitButton"]:visible, input[type="submit"]:visible, button[type="submit"]:visible').first().click().catch(() => {});
  }
  for (let i = 0; i < 24; i++) { await page.waitForTimeout(1500); if (page.url().includes(CF) && !/amazoncognito/.test(page.url())) break; }
  await page.waitForTimeout(6000);
  chatInput = await page.locator('[data-testid="chat-input"]').count();
}
console.log(`[signin] chatInput=${chatInput}`);
const instr0 = await page.evaluate(() => ({ patched: !!(window.maplibregl && window.maplibregl.Map && window.maplibregl.Map.prototype.__p0patched), hasGlobal: !!window.maplibregl }));
console.log(`[instr] maplibregl-global=${instr0.hasGlobal} prototype-patched=${instr0.patched}`);

const input = page.locator('[data-testid="chat-input"]');
await input.fill("Add the roads in Fort Myers, Florida as a layer.");
await input.press("Enter");
console.log("[prompt] sent");

const start = Date.now();
let panelIds = [];
while (Date.now() - start < BUDGET_MS) {
  await page.waitForTimeout(4000);
  panelIds = await page.evaluate(() => Array.from(document.querySelectorAll("[data-layer-id]")).map((r) => r.getAttribute("data-layer-id")));
  if (panelIds.length >= 1 && Date.now() - start > 20000) {
    await page.waitForTimeout(6000);
    // Now FORCE the camera to move via the prod-exposed inject seam to provoke
    // the reconcile/idle loop, then wait for tiles.
    await page.evaluate(() => {
      window.__grace2InjectMapCommand && window.__grace2InjectMapCommand({ command: "zoom-to", args: { bbox: [-81.95, 26.5, -81.8, 26.7] } });
    });
    await page.waitForTimeout(9000);
    break;
  }
}

const p0 = await page.evaluate(() => window.__p0);
console.log("\n========== INSTRUMENTATION RESULT ==========");
console.log(`panelIds(${panelIds.length}): ${JSON.stringify(panelIds)}`);
console.log(`addSource calls (${p0.addSource.length}): ${JSON.stringify(p0.addSource)}`);
console.log(`addLayer calls (${p0.addLayer.length}): ${JSON.stringify(p0.addLayer)}`);
console.log(`isStyleLoaded call count: ${p0.styleLoadedCalls}`);
const sl = p0.mapEvents.filter((e) => e.startsWith("isStyleLoaded"));
const trueCount = sl.filter((e) => e.includes("=true")).length;
const falseCount = sl.filter((e) => e.includes("=false")).length;
console.log(`isStyleLoaded sampled: true=${trueCount} false=${falseCount} (first 12: ${sl.slice(0, 12).join(", ")})`);
console.log(`tile/img loads attempted (${p0.tiles.length}); sample:`);
for (const t of p0.tiles.slice(0, 14)) console.log("   " + t);
console.log(`\n/cog/tiles network responses (${tileResp.length}):`);
for (const t of tileResp.slice(0, 12)) console.log("   " + t);

console.log("\n========== console (filtered) ==========");
for (const c of consoleMsgs.filter((x) => /MapView|\[Map\]|vector|inline|source|layer|error|warn|abort/i.test(x)).slice(-30)) console.log("   " + c);

await page.screenshot({ path: `${OUT}_final.png` });
fs.writeFileSync(`${OUT}_p0.json`, JSON.stringify(p0, null, 2));
console.log(`\n[saved] ${OUT}_final.png + ${OUT}_p0.json`);
await browser.close();
process.exit(0);
