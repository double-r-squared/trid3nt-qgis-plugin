// Mid-case model hot-swap acceptance (NATE 2026-07-11):
//   ONE case. Turn 1 (model A = qwen3:8b-16k): fetch a DEM. HOT SWAP to
//   model B (qwen3.5-lowvram:9b-16k) via the header selector. Turn 2
//   (model B): process turn 1's data (hillshade via the case-state handle).
//   Ground truth per turn = Ollama /api/ps (which model actually executed).
//   Honest screenshots at each meaningful moment.
// Run from web/: node tools/e2e_midcase_swap.mjs
// ASCII hyphens; no emojis.

import { chromium } from "playwright";

const APP_URL = "http://127.0.0.1:5173/app";
const OLLAMA = "http://127.0.0.1:11434";
const MODEL_A = "qwen3:8b-16k";
const MODEL_B = "qwen3.5-lowvram:9b-16k";
const PROOF = "/home/nate/Documents/trid3nt-local/docs/proof";
const results = [];
const pass = (id, ev) => { results.push({ id, ok: true }); console.log("PASS", id, ev || ""); };
const fail = (id, ev) => { results.push({ id, ok: false }); console.log("FAIL", id, ev || ""); };

async function loadedModels() {
  const r = await fetch(`${OLLAMA}/api/ps`).then((x) => x.json()).catch(() => ({ models: [] }));
  return (r.models || []).map((m) => m.name);
}

// Full /api/ps objects (name + expires_at) for residency-timeline evidence.
async function psFull() {
  const r = await fetch(`${OLLAMA}/api/ps`).then((x) => x.json()).catch(() => ({ models: [] }));
  return (r.models || []).map((m) => ({ name: m.name, expires_at: m.expires_at }));
}

// Poll /api/ps until stop() is called; return the observation timeline.
// Rationale (strengthened T2 evidence): a single post-budget ps snapshot is
// weak - model B's default 5-minute keepalive can expire inside the turn-2
// wait budget, and dual residency (A still loaded + B newly loaded) is
// legitimate. The strong assertion is "B became resident only AFTER the swap
// turn started", proven by polling DURING the turn.
function startPsTimeline(intervalMs = 5000) {
  const timeline = [];
  let live = true;
  const loop = (async () => {
    while (live) {
      timeline.push({ t: new Date().toISOString(), models: await psFull() });
      await new Promise((r) => setTimeout(r, intervalMs));
    }
  })();
  return { timeline, stop: async () => { live = false; await loop; } };
}

async function waitTurnDone(page, budgetMs, matcher) {
  const t0 = Date.now();
  while (Date.now() - t0 < budgetMs) {
    const rows = page.locator('[data-testid="layer-row"], [data-testid="layer-group-row"]');
    const n = await rows.count().catch(() => 0);
    const names = [];
    for (let i = 0; i < n; i++) names.push((await rows.nth(i).innerText().catch(() => "")).toLowerCase());
    if (names.some(matcher)) return { ok: true, sec: Math.round((Date.now() - t0) / 1000) };
    await page.waitForTimeout(3000);
  }
  return { ok: false, sec: Math.round(budgetMs / 1000) };
}

// DRIVER HARDENING (2026-07-11): on this 16GB box the headless chromium can
// die mid-turn-2 while the swapped 9b model cold-loads (memory pressure).
// Recovery = relaunch the browser, reopen the app, click back into the run's
// case, keep polling. Driver-side only; no product code touched.
async function launchPage() {
  const b = await chromium.launch({ headless: true, args: ["--disable-gpu", "--disable-dev-shm-usage"] });
  const p = await (await b.newContext({ viewport: { width: 1400, height: 900 } })).newPage();
  await p.goto(APP_URL, { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(4000);
  return { b, p };
}

async function recoverPage() {
  console.log("DRIVER-NOTE: browser died mid-wait; relaunching and reopening the case", new Date().toISOString());
  try { await browser.close(); } catch {}
  const fresh = await launchPage();
  browser = fresh.b;
  page = fresh.p;
  // reopen the run's case (newest matching row first in the list)
  const row = page.locator('[data-testid="grace2-case-row"]', { hasText: /call fetch_dem/i }).first();
  if (await row.count().catch(() => 0)) {
    await row.click().catch(() => {});
    await page.waitForTimeout(5000);
    console.log("DRIVER-NOTE: case reopened after recovery");
  } else {
    console.log("DRIVER-NOTE: no matching case row found after recovery");
  }
}

// waitTurnDone that survives a browser crash: on page-closed errors it
// relaunches + reopens the case and keeps polling within the same budget.
async function waitTurnDoneResilient(budgetMs, matcher) {
  const t0 = Date.now();
  let recoveries = 0;
  while (Date.now() - t0 < budgetMs) {
    try {
      const rows = page.locator('[data-testid="layer-row"], [data-testid="layer-group-row"]');
      const n = await rows.count();
      const names = [];
      for (let i = 0; i < n; i++) names.push((await rows.nth(i).innerText().catch(() => "")).toLowerCase());
      if (names.some(matcher)) return { ok: true, sec: Math.round((Date.now() - t0) / 1000), recoveries };
      await page.waitForTimeout(3000);
    } catch (e) {
      if (recoveries >= 4) return { ok: false, sec: Math.round((Date.now() - t0) / 1000), recoveries, crashed: true };
      recoveries += 1;
      await new Promise((r) => setTimeout(r, 10000));
      try { await recoverPage(); } catch (e2) { console.log("DRIVER-NOTE: recovery attempt failed:", String(e2).slice(0, 120)); }
    }
  }
  return { ok: false, sec: Math.round(budgetMs / 1000), recoveries };
}

let { b: browser, p: page } = await launchPage();

// fresh case for a clean slate
const newBtn = page.getByRole("button", { name: /new case/i }).first();
if (await newBtn.count().catch(() => 0)) { await newBtn.click().catch(() => {}); await page.waitForTimeout(3000); }

// ensure model A selected
const modelBtn = page.getByTestId("model-selector-button");
await modelBtn.click();
await page.waitForTimeout(500);
await page.getByTestId(`model-option-${MODEL_A}`).click().catch(() => {});
await page.waitForTimeout(500);

// TURN 1 (model A): fetch DEM
const input = page.locator("textarea, input[placeholder*='Ask'], input[placeholder*='ask']").first();
await input.fill("Call fetch_dem with exactly these args: bbox=[-85.32, 35.03, -85.28, 35.07], source=\"3dep\", resolution_m=30. Then publish the result with publish_layer. Do not use any other tools.");
await input.press("Enter");
console.log("turn 1 sent (model A)", new Date().toISOString());
// gate may fire (fetch_dem is gated): auto-confirm
const gateWatch = (async () => {
  for (let i = 0; i < 120; i++) {
    const btn = page.getByRole("button", { name: /proceed|confirm/i }).first();
    if (await btn.count().catch(() => 0) && await btn.isVisible().catch(() => false)) {
      await btn.click().catch(() => {});
      console.log("gate confirmed");
      return;
    }
    await page.waitForTimeout(2000);
  }
})();
const t1 = await waitTurnDone(page, 420000, (t) => t.includes("dem"));
await gateWatch;
t1.ok ? pass("T1_DEM_LANDS", `DEM layer at T+${t1.sec}s`) : fail("T1_DEM_LANDS", "no DEM layer in budget");
const psA = await loadedModels();
psA.includes(MODEL_A) ? pass("T1_RAN_ON_A", `ollama ps: ${psA.join(",")}`) : fail("T1_RAN_ON_A", `ollama ps: ${psA.join(",")}`);
await page.screenshot({ path: `${PROOF}/60-midcase-t1-dem-modelA.png` });

// HOT SWAP mid-case to model B
await modelBtn.click();
await page.waitForTimeout(500);
await page.screenshot({ path: `${PROOF}/61-midcase-swap-popover.png` });
await page.getByTestId(`model-option-${MODEL_B}`).click();
await page.waitForTimeout(800);
pass("SWAP_CLICKED", `selected ${MODEL_B} mid-case`);

// UI truth: the selector button (icon-only) stamps the active model id on
// data-model-id - assert it now reports model B.
const selModelId = await page.getByTestId("model-selector-button").getAttribute("data-model-id").catch(() => null);
selModelId === MODEL_B
  ? pass("SWAP_UI_SHOWS_B", `selector data-model-id='${selModelId}'`)
  : fail("SWAP_UI_SHOWS_B", `selector data-model-id='${selModelId}'`);

// Pre-turn-2 residency baseline: B must NOT be loaded yet.
const psPre = await psFull();
console.log("ps before turn 2:", JSON.stringify(psPre));
const bLoadedPre = psPre.some((m) => m.name === MODEL_B);

// TURN 2 (model B): process turn 1's data via the case-state handle.
// Poll /api/ps DURING the turn so B's load is observed even if its keepalive
// expires before the wait budget ends.
const psWatch = startPsTimeline(5000);
await input.fill("Call compute_hillshade with dem_uri set to the DEM layer handle already in this case (from the case state - do not construct a uri). Then publish the hillshade with publish_layer.");
await input.press("Enter");
const turn2SentAt = new Date().toISOString();
console.log("turn 2 sent (model B)", turn2SentAt);
const t2 = await waitTurnDoneResilient(600000, (t) => t.includes("hillshade"));
await psWatch.stop();
t2.ok
  ? pass("T2_HILLSHADE_LANDS", `hillshade layer at T+${t2.sec}s (recoveries=${t2.recoveries})`)
  : fail("T2_HILLSHADE_LANDS", `no hillshade layer in budget (recoveries=${t2.recoveries}${t2.crashed ? ", gave up after repeated crashes" : ""})`);
// Honesty evidence: dump the visible chat text so the final reply (honest
// error vs fabricated success) is captured even without the screenshot.
try {
  const chatText = await page.locator('[data-testid="chat-messages"], [class*="chat"]').first().innerText({ timeout: 5000 });
  console.log("CHAT-TEXT-BEGIN\n" + chatText.slice(-2500) + "\nCHAT-TEXT-END");
} catch { console.log("DRIVER-NOTE: could not read chat text"); }
const bSightings = psWatch.timeline.filter((o) => o.models.some((m) => m.name === MODEL_B));
console.log("ps timeline during turn 2 (" + psWatch.timeline.length + " samples):");
for (const o of psWatch.timeline) console.log(" ", o.t, JSON.stringify(o.models));
if (!bLoadedPre && bSightings.length > 0) {
  const first = bSightings[0];
  const bEntry = first.models.find((m) => m.name === MODEL_B);
  pass(
    "T2_RAN_ON_B",
    `B not loaded pre-turn-2; B first resident at ${first.t} (after turn-2 send ${turn2SentAt}); expires_at=${bEntry.expires_at}; sightings=${bSightings.length}/${psWatch.timeline.length}`
  );
} else if (bLoadedPre) {
  fail("T2_RAN_ON_B", "ambiguous: B was already loaded BEFORE turn 2 was sent");
} else {
  fail("T2_RAN_ON_B", `B never observed in ${psWatch.timeline.length} ps samples during turn 2`);
}
await page.screenshot({ path: `${PROOF}/62-midcase-t2-hillshade-modelB.png`, fullPage: false }).catch((e) => console.log("DRIVER-NOTE: t2 screenshot failed:", String(e).slice(0, 120)));

// restore default model for NATE (re-resolve from the CURRENT page - the
// original modelBtn locator may belong to a crashed, recovered-away page)
try {
  await page.getByTestId("model-selector-button").click();
  await page.waitForTimeout(500);
  await page.getByTestId(`model-option-${MODEL_A}`).click();
} catch (e) { console.log("DRIVER-NOTE: model-A restore skipped:", String(e).slice(0, 120)); }

await browser.close().catch(() => {});
const nf = results.filter((r) => !r.ok).length;
console.log(`SUMMARY ${results.length - nf}/${results.length} pass`);
process.exit(nf ? 1 : 0);
