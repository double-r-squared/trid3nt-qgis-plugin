// Mobile-viewport drive: WA-state landcover (resolution gate path) on the
// local web build. Checks: thinking block streams, gate card visible on the
// small screen, Proceed works, layer row lands, no stuck loading, and the
// spatial-draw top-stack (if present) does not overlap-fail.
// Run: node e2e_mobile_landcover_drive.mjs   (ASCII hyphens; no emojis)

import { chromium, devices } from "playwright";

// DATA-INTEGRITY GUARD (2026-07-12): the trid3nt-local server maps EVERY
// anonymous session to one shared local user, so booting the app RESUMES
// that user's last-active REAL case. Prompting without creating a case
// first mutated real cases (bbox overwrite + layer pollution). Always
// create a brand-new case before sending any prompt.
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


const APP_URL = "http://127.0.0.1:5173/app";
const results = [];
const pass = (id, ev) => { results.push({ id, ok: true, ev }); console.log("PASS", id, ev); };
const fail = (id, ev) => { results.push({ id, ok: false, ev }); console.log("FAIL", id, ev); };

const browser = await chromium.launch({ headless: true, args: ["--disable-gpu", "--disable-dev-shm-usage"] });
browser.on("disconnected", () => console.log("BROWSER DISCONNECTED", new Date().toISOString()));
const ctx = await browser.newContext({ ...devices["iPhone 13"] });
const page = await ctx.newPage();
await page.goto(APP_URL, { waitUntil: "domcontentloaded" });
await page.waitForTimeout(4000);

await createFreshCase(page);
const input = page.locator("textarea, input[placeholder*='Ask'], input[placeholder*='ask']").first();
await input.waitFor({ timeout: 15000 });
await input.fill("Show me landcover over Washington state");
await input.press("Enter");
console.log("prompt sent", new Date().toISOString());

// Phase 1: thinking block during the turn (up to 3 min for cold model)
let sawThinking = false;
let t0 = Date.now();
while (Date.now() - t0 < 180000) {
  if (await page.locator('[data-testid="agent-thinking-block"], [data-testid="agent-thinking-content"]').count() > 0) {
    sawThinking = true;
    break;
  }
  await page.waitForTimeout(1500);
}
sawThinking ? pass("M1_THINKING", `at T+${Math.round((Date.now() - t0) / 1000)}s`) : fail("M1_THINKING", "none in 180s");

// Phase 2: gate card appears and is visible in the mobile viewport
const gateSel = '[data-testid="payload-warning-inline"], [data-testid="resolution-picker-card"], [data-variant="warning"]';
let gateEl = null;
t0 = Date.now();
while (Date.now() - t0 < 300000) {
  const els = page.locator(gateSel);
  if (await els.count() > 0) { gateEl = els.first(); break; }
  await page.waitForTimeout(2000);
}
if (!gateEl) {
  fail("M2_GATE_APPEARS", "no gate card in 300s");
} else {
  const vis = await gateEl.isVisible().catch(() => false);
  const box = await gateEl.boundingBox().catch(() => null);
  const vp = page.viewportSize();
  const inViewport = box && box.x >= -2 && box.x + box.width <= vp.width + 2;
  pass("M2_GATE_APPEARS", `visible=${vis} inViewportX=${inViewport} box=${box ? Math.round(box.width) + "x" + Math.round(box.height) : "none"}`);
  if (!inViewport) fail("M2b_GATE_FITS", `overflows viewport ${vp.width}px`);
  else pass("M2b_GATE_FITS", "");
  await page.screenshot({ path: "/home/nate/Documents/trid3nt-local/docs/proof/47-mobile-gate.png" });
  // Phase 3: tap Proceed
  const proceed = page.getByRole("button", { name: /proceed|confirm/i }).first();
  if (await proceed.count() > 0) {
    await proceed.tap().catch(async () => { await proceed.click(); });
    pass("M3_PROCEED_TAP", "");
  } else {
    fail("M3_PROCEED_TAP", "no Proceed button");
  }
}

// Phase 4: landcover layer row lands + loading resolves (up to 6 min)
let layerOk = false;
t0 = Date.now();
while (Date.now() - t0 < 360000) {
  const rows = await page.locator('[data-testid*="layer"], [class*="LayerRow"], [class*="layer-row"]').count();
  const bodyText = await page.locator("body").innerText().catch(() => "");
  if (rows > 0 && /landcover|land cover/i.test(bodyText)) { layerOk = true; break; }
  await page.waitForTimeout(3000);
}
layerOk ? pass("M4_LAYER_LANDS", `at T+${Math.round((Date.now() - t0) / 1000)}s`) : fail("M4_LAYER_LANDS", "no landcover layer row in 360s");
await page.waitForTimeout(5000);
const stuck = await page.locator("text=/loading/i").count();
stuck === 0 ? pass("M5_NO_STUCK_LOADING", "") : fail("M5_NO_STUCK_LOADING", `loading els=${stuck}`);

// Phase 5: overlap audit of fixed/absolute chrome in the chat area (the
// F15 class): any two visible fixed-position elements overlapping > 60%?
const overlaps = await page.evaluate(() => {
  const els = [...document.querySelectorAll("body *")].filter((e) => {
    const s = getComputedStyle(e);
    if (s.position !== "fixed" && s.position !== "absolute") return false;
    const r = e.getBoundingClientRect();
    return r.width > 60 && r.height > 30 && r.bottom > 0 && r.top < innerHeight && s.visibility !== "hidden" && s.display !== "none";
  });
  const bad = [];
  for (let i = 0; i < els.length; i++)
    for (let j = i + 1; j < els.length; j++) {
      if (els[i].contains(els[j]) || els[j].contains(els[i])) continue;
      const a = els[i].getBoundingClientRect(), b = els[j].getBoundingClientRect();
      const ox = Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left));
      const oy = Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));
      const inter = ox * oy, small = Math.min(a.width * a.height, b.width * b.height);
      const ids = [els[i], els[j]].map((e) => (e.getAttribute("data-testid") || "") + " " + e.className);
      if (ids.some((t) => /grace2-map|maplibregl|scroll-to-bottom-anchor|grace2-chat/.test(t))) continue;
      if (small > 0 && inter / small > 0.6)
        bad.push([els[i].getAttribute("data-testid") || els[i].className.toString().slice(0, 40), els[j].getAttribute("data-testid") || els[j].className.toString().slice(0, 40)]);
    }
  return bad.slice(0, 5);
});
overlaps.length === 0 ? pass("M6_NO_OVERLAPS", "") : fail("M6_NO_OVERLAPS", JSON.stringify(overlaps));

await page.screenshot({ path: "/home/nate/Documents/trid3nt-local/docs/proof/47-mobile-landcover-final.png" });
await browser.close();
const nf = results.filter((r) => !r.ok).length;
console.log(`SUMMARY ${results.length - nf}/${results.length} pass`);
process.exit(nf ? 1 : 0);
