// Pass 4 (post vendor-sync): capture all three states with the CORRECT
// verbatim labels. Running window is ~370-740ms (wire duration_ms), so an
// event-driven wait (case-insensitive) + instant screenshot catches it, with
// a CDP screencast as backup frame source. Then done + persistence shots.
// Run from web/: node tools/compaction_card_proof4.mjs
// ASCII hyphens only; no emojis.

import { chromium } from "playwright";
import fs from "fs";

const APP_URL = "http://127.0.0.1:5173/app";
// DATA-INTEGRITY (2026-07-12): this proof used to hardcode a REAL user case
// (the plume case) and prompt into it. Require an explicit disposable case.
const CASE_ID = process.env.PROOF_CASE_ID;
if (!CASE_ID) {
  console.error("PROOF_CASE_ID env var required (a disposable case id) -- refusing to default to a real user case");
  process.exit(1);
}
const PROOF_DIR = "/home/nate/Documents/trid3nt-local/docs/proof";
const SHOT_RUNNING = PROOF_DIR + "/70-compaction-card-running.png";
const SHOT_DONE = PROOF_DIR + "/71-compaction-card-done.png";
const SHOT_PERSIST = PROOF_DIR + "/72-compaction-card-persisted.png";
const PROMPT = "Say READY and nothing else. Do not call any tools.";
const DONE_RE = /Conversation compacted \(/i;

function log(...a) { console.log(new Date().toISOString(), ...a); }

const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
const page = await ctx.newPage();
await page.goto(APP_URL, { waitUntil: "domcontentloaded" });
await page.waitForTimeout(4000);

async function openCase() {
  const row = page.locator(
    `[data-testid="grace2-case-row"][data-case-id="${CASE_ID}"]`
  );
  await row.waitFor({ timeout: 20000 });
  await row.click();
  await page.waitForTimeout(5000);
}
await openCase();
log("case opened");

// Observer: case-insensitive, epoch timestamps.
await page.evaluate(() => {
  window.__compactLog = { appearedEpoch: null, flippedEpoch: null };
  const check = () => {
    const has = /compacting con/i.test(document.body.innerText);
    const L = window.__compactLog;
    if (has && L.appearedEpoch === null) L.appearedEpoch = Date.now();
    if (!has && L.appearedEpoch !== null && L.flippedEpoch === null) L.flippedEpoch = Date.now();
  };
  const mo = new MutationObserver(check);
  mo.observe(document.body, { childList: true, subtree: true, characterData: true });
  check();
});

// Screencast backup.
const cdp = await ctx.newCDPSession(page);
let frames = [];
cdp.on("Page.screencastFrame", async (ev) => {
  frames.push({ ts: ev.metadata.timestamp, data: ev.data });
  if (frames.length > 900) frames = frames.slice(-900);
  await cdp.send("Page.screencastFrameAck", { sessionId: ev.sessionId }).catch(() => {});
});
await cdp.send("Page.startScreencast", { format: "jpeg", quality: 85, everyNthFrame: 1 });

const baselineDone = await page.getByText(DONE_RE).count();
log("baseline done-cards=" + baselineDone);

const input = page.locator("textarea").first();
await input.waitFor({ timeout: 15000 });
await input.fill(PROMPT);
await input.press("Enter");
log("prompt sent");

let sawRunning = false;
try {
  await page.waitForFunction(
    () => /compacting con/i.test(document.body.innerText),
    undefined,
    { timeout: 120000, polling: 30 }
  );
  await page.screenshot({ path: SHOT_RUNNING });
  sawRunning = true;
  log("RUNNING label detected - instant screenshot saved");
} catch {
  log("running label not detected within 120s");
}

// Done card.
let sawDone = false;
const doneDeadline = Date.now() + 120000;
while (Date.now() < doneDeadline) {
  const d = await page.getByText(DONE_RE).count();
  if (d > baselineDone) {
    sawDone = true;
    const el = page.getByText(DONE_RE).last();
    await el.scrollIntoViewIfNeeded({ timeout: 1500 }).catch(() => {});
    await page.screenshot({ path: SHOT_DONE });
    log("DONE card sighted (count=" + d + ") - screenshot saved");
    break;
  }
  await page.waitForTimeout(300);
}

await page.waitForTimeout(1000);
await cdp.send("Page.stopScreencast");
const winlog = await page.evaluate(() => window.__compactLog);
log("running-label DOM window=" + JSON.stringify(winlog) +
  (winlog.appearedEpoch && winlog.flippedEpoch
    ? " visible ~" + (winlog.flippedEpoch - winlog.appearedEpoch) + "ms"
    : ""));

// If the instant screenshot missed but the DOM had the label, pull the
// real screencast frame from within the window.
if (!sawRunning && winlog.appearedEpoch !== null) {
  const a = winlog.appearedEpoch / 1000 - 0.03;
  const b = (winlog.flippedEpoch ?? Date.now()) / 1000 + 0.03;
  const inWin = frames.filter((f) => f.ts >= a && f.ts <= b);
  log("screencast frames in window=" + inWin.length);
  if (inWin.length > 0) {
    const pick = inWin[Math.floor(inWin.length / 2)];
    fs.writeFileSync(SHOT_RUNNING, Buffer.from(pick.data, "base64"));
    sawRunning = true;
    log("RUNNING frame recovered from screencast");
  }
}

// Let the turn wind down (this case ends in the context-window error card).
const endDeadline = Date.now() + 120000;
while (Date.now() < endDeadline) {
  const streaming = await page.locator('[data-state="running"]').count().catch(() => 0);
  const errCard = await page.getByText(/CONTEXT_WINDOW_EXCEEDED|READY/i).count().catch(() => 0);
  if (streaming === 0 && errCard > 0) break;
  await page.waitForTimeout(1500);
}
await page.waitForTimeout(4000);
log("turn wound down");

// Persistence: back + re-open + verify + shot.
const back = page.locator(
  '[data-testid="grace2-case-view-back"], [data-testid="grace2-case-view-cases-link"]'
).first();
try { await back.waitFor({ timeout: 5000 }); await back.click(); }
catch { await page.goto(APP_URL, { waitUntil: "domcontentloaded" }); }
await page.waitForTimeout(2500);
await openCase();
log("case re-opened");
const persisted = await page.getByText(DONE_RE).count();
let sawPersisted = false;
if (persisted > 0) {
  sawPersisted = true;
  const el = page.getByText(DONE_RE).last();
  await el.scrollIntoViewIfNeeded({ timeout: 1500 }).catch(() => {});
  await page.screenshot({ path: SHOT_PERSIST });
  log("persisted screenshot saved (count=" + persisted + ")");
}

console.log("\n=== RESULT ===");
console.log("sawRunning=" + sawRunning);
console.log("sawDone=" + sawDone);
console.log("sawPersisted=" + sawPersisted + " count=" + persisted);
console.log("window=" + JSON.stringify(winlog));

await browser.close();
process.exit(0);
