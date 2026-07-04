// LIVE COLD-REPRO lens (box ASLEEP). Drives the Vercel app, signs in, opens a
// case, and OBSERVES the four reported symptoms WITHOUT waking the agent:
//   1) COLD RASTER: count tile 200s from the CloudFront tile host before any wake.
//   2) MEMORY GROWTH: sample JS heap (Chromium performance.memory) every ~10s.
//   3) LAYER REORDER: snapshot the LayerPanel layer-name order every ~10s.
//   4) AUTOPLAY: sample the sequence-scrubber frame indicator over ~30s.
// Do NOT click "Wake up" / send a prompt — stay cold so we measure the cold path.
import { chromium } from "playwright";

const SITE = "https://grace-2.vercel.app/app";
const TILE_HOST = "d125yfbyjrpbre.cloudfront.net";
const EMAIL = process.env.GRACE2_DEMO_EMAIL;
const PW = process.env.GRACE2_DEMO_PASSWORD;
const OUT = "/tmp/cold_repro";

const isTile = (u) =>
  u.includes(TILE_HOST) && (u.includes("/cog") || u.includes("/tiles"));

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

const tileOk = [];
const tileFailed = [];
const corsMsgs = [];
const pageErrors = [];
const wakeUrls = [];

page.on("response", (r) => {
  const u = r.url();
  if (isTile(u) && r.status() === 200) tileOk.push(u.slice(-70));
  if (/case-view-url|case-list/.test(u)) wakeUrls.push(`${r.status()} ${u.slice(0, 90)}`);
});
page.on("requestfailed", (req) => {
  const u = req.url();
  if (isTile(u)) tileFailed.push(`${(req.failure()?.errorText) || "?"} ${u.slice(-60)}`);
});
page.on("console", (m) => {
  const t = m.text();
  if (/CORS|Access-Control|has been blocked|ERR_FAILED|cross-origin/i.test(t))
    corsMsgs.push(t.slice(0, 200));
});
page.on("pageerror", (e) => pageErrors.push(String(e).slice(0, 200)));

console.log(`[0] goto ${SITE}`);
await page.goto(SITE, { waitUntil: "networkidle", timeout: 60000 });
await page.waitForTimeout(2500);

console.log("[1] sign in (Hosted UI)");
await page.getByRole("button", { name: /Sign in|Sign up/i }).first().click().catch(() => {});
await page.waitForTimeout(5000);
const onHostedUI = /amazoncognito\.com/.test(page.url());
if (onHostedUI) {
  const userField = page.locator('input[name="username"]:visible, input[type="email"]:visible').first();
  const pwField = page.locator('input[name="password"]:visible, input[type="password"]:visible').first();
  await userField.waitFor({ timeout: 12000 });
  await userField.fill(EMAIL);
  await pwField.fill(PW);
  await page.locator('input[name="signInSubmitButton"]:visible, input[type="submit"]:visible, button[type="submit"]:visible').first().click().catch(() => {});
  for (let i = 0; i < 20; i++) {
    await page.waitForTimeout(2000);
    if (page.url().includes("vercel.app") && !/amazoncognito/.test(page.url())) break;
  }
}
await page.waitForTimeout(6000);
await page.screenshot({ path: `${OUT}_1_signedin.png` });
const chatInput = await page.locator('[data-testid="chat-input"]').count();
console.log(`    backOnApp=${page.url().includes("vercel.app")} chatInput=${chatInput > 0}`);

// Report what connection state we are in (we want NOT-connected = box asleep).
const connState = await page.evaluate(() => {
  const b = document.body.innerText;
  return {
    wake: /wake up|waking|asleep|sleeping/i.test(b),
    connecting: /connecting|reconnect/i.test(b),
    snippet: b.slice(0, 400),
  };
});
console.log(`    connState wake=${connState.wake} connecting=${connState.connecting}`);

console.log("[2] open the first case (do NOT wake / do NOT prompt)");
const caseText = await page.evaluate(() => document.body.innerText.slice(0, 600));
console.log("    cases-panel head:\n" + caseText.split("\n").filter(Boolean).slice(0, 12).map((l) => "      " + l).join("\n"));
const clicked = await page.evaluate(() => {
  const cands = Array.from(document.querySelectorAll('[data-testid*="case"], button, li, [role="button"]'));
  const byText = cands.find((e) => /mexico|surge|beach|flood|inundation/i.test(e.textContent || "") && (e.textContent || "").length < 120);
  const target = byText || document.querySelector('[data-testid*="case"]');
  if (target) { target.click(); return (target.textContent || "").slice(0, 80); }
  return null;
});
console.log(`    clicked case: ${clicked}`);
await page.waitForTimeout(12000); // cold-read snapshot + tile fetches
await page.screenshot({ path: `${OUT}_2_case_cold.png` });
const tileOkAfterOpen = tileOk.length;
console.log(`    tileOk after open (cold) = ${tileOkAfterOpen}`);

// Helper: snapshot LayerPanel row order + heap + scrubber state.
async function sample(label) {
  return await page.evaluate(() => {
    // performance.memory is Chromium-only.
    const heap = (performance && performance.memory) ? performance.memory.usedJSHeapSize : null;
    // LayerPanel rows: the panel has data-testid grace2-layer-panel; rows carry
    // layer names. Grab text of each row in DOM order.
    const panel = document.querySelector('[data-testid="grace2-layer-panel"]');
    let rows = [];
    if (panel) {
      // Each layer row tends to be a li/div with the layer name text. Collect
      // candidate row elements that have a reasonably short text and look like a name.
      const cand = Array.from(panel.querySelectorAll('li, [data-testid*="layer-row"], [role="listitem"]'));
      rows = cand.map((e) => (e.textContent || "").replace(/\s+/g, " ").trim()).filter((t) => t && t.length < 120);
      if (rows.length === 0) {
        // Fallback: any element with a title-ish span.
        const spans = Array.from(panel.querySelectorAll('span, div'));
        rows = spans.map((e) => (e.textContent || "").replace(/\s+/g, " ").trim())
          .filter((t) => t && t.length > 2 && t.length < 60);
      }
    }
    // Scrubber frame indicator: SequenceScrubber renders a frame label / position.
    const scrub = document.querySelector('[data-testid*="scrubber"], [data-testid*="sequence"]');
    let frame = null;
    if (scrub) frame = (scrub.textContent || "").replace(/\s+/g, " ").trim().slice(0, 80);
    // Also try aria on a range input inside the scrubber.
    const range = scrub ? scrub.querySelector('input[type="range"]') : null;
    const rangeVal = range ? range.value : null;
    // MapLibre canvases present?
    const canvases = document.querySelectorAll('canvas.maplibregl-canvas, canvas').length;
    return { heap, rows, frame, rangeVal, canvases };
  });
}

console.log("[3] SAMPLING over ~70s (heap / layer order / scrubber). NO wake, NO prompt.");
const samples = [];
for (let i = 0; i < 8; i++) {
  const s = await sample(`t${i}`);
  s.t = i * 10;
  s.tileCum = tileOk.length;
  samples.push(s);
  console.log(`    t=${s.t}s heap=${s.heap} rows=${s.rows.length} tileCum=${s.tileCum} frame=${JSON.stringify(s.frame)} range=${s.rangeVal} canvas=${s.canvases}`);
  console.log(`       order: ${JSON.stringify(s.rows.slice(0, 8))}`);
  await page.waitForTimeout(10000);
}
await page.screenshot({ path: `${OUT}_3_after_70s.png` });

// --- Analysis ---------------------------------------------------------------
console.log("\n==================== RESULT ====================");
console.log(`onHostedUI=${onHostedUI} chatInput=${chatInput > 0}`);
console.log(`tileOk(200) total = ${tileOk.length}  (after-open cold = ${tileOkAfterOpen})`);
console.log(`tileFailed = ${tileFailed.length}  corsMsgs = ${corsMsgs.length}  pageErrors = ${pageErrors.length}`);
console.log(`coldEndpoints hit: ${JSON.stringify(wakeUrls.slice(0, 6))}`);

// Heap trend
const heaps = samples.map((s) => s.heap).filter((h) => h != null);
if (heaps.length >= 2) {
  const first = heaps[0], last = heaps[heaps.length - 1];
  const max = Math.max(...heaps), min = Math.min(...heaps);
  let monotonic = true;
  for (let i = 1; i < heaps.length; i++) if (heaps[i] < heaps[i - 1]) monotonic = false;
  console.log(`\nHEAP: first=${(first/1e6).toFixed(1)}MB last=${(last/1e6).toFixed(1)}MB min=${(min/1e6).toFixed(1)}MB max=${(max/1e6).toFixed(1)}MB delta=${((last-first)/1e6).toFixed(1)}MB monotonicUp=${monotonic}`);
  console.log(`HEAP samples(MB): ${heaps.map((h) => (h/1e6).toFixed(1)).join(", ")}`);
} else {
  console.log("\nHEAP: performance.memory unavailable (non-Chromium?) — could not measure.");
}

// Layer order change detection
const orders = samples.map((s) => s.rows.join("|"));
let orderChanges = 0;
const distinctOrders = new Set(orders);
for (let i = 1; i < orders.length; i++) if (orders[i] !== orders[i - 1]) orderChanges++;
console.log(`\nLAYER ORDER: samples=${orders.length} changes-between-consecutive=${orderChanges} distinctOrders=${distinctOrders.size}`);
if (orderChanges > 0) {
  for (let i = 1; i < orders.length; i++) {
    if (orders[i] !== orders[i - 1]) {
      console.log(`  change at t=${samples[i].t}s:`);
      console.log(`    prev: ${JSON.stringify(samples[i-1].rows.slice(0, 8))}`);
      console.log(`    now : ${JSON.stringify(samples[i].rows.slice(0, 8))}`);
    }
  }
}

// Autoplay detection
const frames = samples.map((s) => s.frame);
const ranges = samples.map((s) => s.rangeVal);
let frameChanges = 0;
for (let i = 1; i < frames.length; i++) if (frames[i] !== frames[i - 1]) frameChanges++;
let rangeChanges = 0;
for (let i = 1; i < ranges.length; i++) if (ranges[i] !== ranges[i - 1]) rangeChanges++;
const hasScrubber = frames.some((f) => f != null) || ranges.some((r) => r != null);
console.log(`\nAUTOPLAY: scrubberPresent=${hasScrubber} frameLabelChanges=${frameChanges} rangeValueChanges=${rangeChanges}`);
console.log(`  frames: ${JSON.stringify(frames)}`);
console.log(`  ranges: ${JSON.stringify(ranges)}`);

if (tileFailed.length) console.log("\nTILE FAILURES:\n" + tileFailed.slice(0, 5).map((s) => "  " + s).join("\n"));
if (corsMsgs.length) console.log("\nCORS CONSOLE:\n" + corsMsgs.slice(0, 5).map((s) => "  " + s).join("\n"));
if (pageErrors.length) console.log("\nPAGE ERRORS:\n" + pageErrors.slice(0, 8).map((s) => "  " + s).join("\n"));

console.log("\nscreenshots: " + OUT + "_{1_signedin,2_case_cold,3_after_70s}.png");
await browser.close();
