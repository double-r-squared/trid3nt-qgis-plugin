// Follow-up: reach the LIVE MapLibre instance WITHOUT the DEV-only __grace2GetMap
// seam (it's dropped in prod). MapLibre stores a back-reference we can walk from
// the canvas. Sign in, send the roads prompt, then introspect getStyle() sources
// + layers + isStyleLoaded() + whether the inline vector source/layer landed.
import { chromium } from "playwright";
import fs from "node:fs";

const SITE = "https://d125yfbyjrpbre.cloudfront.net/app";
const CF = "d125yfbyjrpbre.cloudfront.net";
const EMAIL = process.env.GRACE2_DEMO_EMAIL || "grace2-demo@example.com";
const PW = process.env.GRACE2_DEMO_PASSWORD || "Grace2Demo2026";
const PROMPT = "Add the roads in Fort Myers, Florida as a layer.";
const OUT = "/home/nate/Documents/GRACE-2/reports/inflight/p0_probe";
const BUDGET_MS = 10 * 60 * 1000;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const consoleMsgs = [];
page.on("console", (m) => consoleMsgs.push(`${m.type()}: ${m.text().slice(0, 260)}`));

// expose a hook to grab the map: patch maplibregl.Map.prototype on the page so
// every constructed Map registers itself on window. We inject BEFORE app scripts
// run via addInitScript so the prototype is patched when the bundle creates the map.
await page.addInitScript(() => {
  const w = window;
  w.__p0maps = [];
  const tryPatch = () => {
    const mlib = w.maplibregl;
    if (mlib && mlib.Map && !mlib.Map.__p0patched) {
      mlib.Map.__p0patched = true;
      const orig = mlib.Map;
      // wrap construction: push each instance
      const handler = {
        construct(target, args) {
          const inst = new target(...args);
          w.__p0maps.push(inst);
          return inst;
        },
      };
      try {
        w.maplibregl.Map = new Proxy(orig, handler);
      } catch {}
    }
  };
  // poll until the bundle defines maplibregl (it's imported, may not be global) —
  // if it never becomes global this is a no-op and we fall back to canvas walk.
  const iv = setInterval(tryPatch, 50);
  setTimeout(() => clearInterval(iv), 30000);
});

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

// how many maps did our proxy capture?
const mapsCaptured = await page.evaluate(() => (window.__p0maps || []).length);
console.log(`[probe] proxy-captured maps: ${mapsCaptured} (0 => maplibregl not global; will canvas-walk)`);

const input = page.locator('[data-testid="chat-input"]');
await input.fill(PROMPT);
await input.press("Enter");
console.log(`[prompt] sent`);

const start = Date.now();
let panelIds = [];
while (Date.now() - start < BUDGET_MS) {
  await page.waitForTimeout(4000);
  panelIds = await page.evaluate(() => Array.from(document.querySelectorAll("[data-layer-id]")).map((r) => r.getAttribute("data-layer-id")));
  if (panelIds.length > 0 && Date.now() - start > 20000) { await page.waitForTimeout(9000); break; }
}

const probe = await page.evaluate((panelIds) => {
  // Strategy A: our proxy-captured maps. Strategy B: walk the canvas for a
  // MapLibre back-reference. Strategy C: scan window for any object with getStyle.
  const out = { strategy: null, styleLoaded: null, sources: [], layers: [], matches: {}, err: null };
  let m = null;
  if (window.__p0maps && window.__p0maps.length) { m = window.__p0maps[window.__p0maps.length - 1]; out.strategy = "proxy"; }
  if (!m) {
    const cvs = document.querySelector(".maplibregl-canvas") || document.querySelector("canvas");
    if (cvs) {
      for (const k of Object.keys(cvs)) {
        const v = cvs[k];
        if (v && typeof v.getStyle === "function") { m = v; out.strategy = "canvas-key:" + k; break; }
      }
      // MapLibre keeps the map on the container's parent in some versions
      if (!m) {
        let el = cvs;
        for (let i = 0; i < 6 && el; i++) {
          for (const k of Object.keys(el)) { const v = el[k]; if (v && typeof v.getStyle === "function" && typeof v.getSource === "function") { m = v; out.strategy = "ancestor-key:" + k; break; } }
          if (m) break; el = el.parentElement;
        }
      }
    }
  }
  if (!m) {
    for (const k of Object.keys(window)) {
      try { const v = window[k]; if (v && typeof v.getStyle === "function" && typeof v.getSource === "function") { m = v; out.strategy = "window:" + k; break; } } catch {}
    }
  }
  if (!m) { out.err = "no map instance reachable by any strategy"; return out; }
  try {
    out.styleLoaded = m.isStyleLoaded();
    const s = m.getStyle();
    out.sources = Object.keys(s.sources || {});
    out.layers = (s.layers || []).map((l) => ({ id: l.id, type: l.type, source: l.source, vis: l.layout && l.layout.visibility }));
    for (const id of panelIds) {
      let spec = null; try { const ss = s.sources[id]; spec = ss ? { type: ss.type, hasData: ss.data !== undefined, tiles: ss.tiles && ss.tiles[0] && ss.tiles[0].slice(0, 100) } : null; } catch {}
      out.matches[id] = { src: !!m.getSource(id), layer: !!m.getLayer(id), spec };
    }
  } catch (e) { out.err = String(e); }
  return out;
}, panelIds);

console.log("\n========== SEAM-FREE MAP PROBE ==========");
console.log(`strategy=${probe.strategy} styleLoaded=${probe.styleLoaded} err=${probe.err}`);
console.log(`panelIds(${panelIds.length}): ${JSON.stringify(panelIds)}`);
console.log(`sources(${probe.sources.length}): ${JSON.stringify(probe.sources)}`);
console.log(`layers(${probe.layers.length}):`);
for (const l of probe.layers) console.log(`   - ${l.id} [${l.type}] src=${l.source} vis=${l.vis}`);
console.log("per-panel-layer in style:");
for (const [id, v] of Object.entries(probe.matches)) console.log(`   - ${id}: src=${v.src} layer=${v.layer} spec=${JSON.stringify(v.spec)}`);

console.log("\n========== console (vector/map related) ==========");
for (const c of consoleMsgs.filter((x) => /MapView|\[Map\]|vector|geojson|inline|source|layer|Error|error|warn/i.test(x)).slice(-40)) console.log("   " + c);

await page.screenshot({ path: `${OUT}_final.png` });
fs.writeFileSync(`${OUT}_console_full.log`, consoleMsgs.join("\n"));
console.log(`\n[saved] ${OUT}_final.png  +  ${OUT}_console_full.log`);
await browser.close();
process.exit(0);
