// Final persistence proof: FRESH browser context, open the case cold (no
// prompt, no live turn), verify the completed compaction card(s) render from
// persisted history, screenshot settled state.
// Run from web/: node tools/compaction_persist_proof.mjs
// ASCII hyphens only; no emojis.

import { chromium } from "playwright";

const APP_URL = "http://127.0.0.1:5173/app";
// DATA-INTEGRITY (2026-07-12): this proof used to hardcode a REAL user case
// (the plume case) and prompt into it. Require an explicit disposable case.
const CASE_ID = process.env.PROOF_CASE_ID;
if (!CASE_ID) {
  console.error("PROOF_CASE_ID env var required (a disposable case id) -- refusing to default to a real user case");
  process.exit(1);
}
const SHOT = "/home/nate/Documents/trid3nt-local/docs/proof/72-compaction-card-persisted.png";
const DONE_RE = /Conversation compacted \(/i;

function log(...a) { console.log(new Date().toISOString(), ...a); }

const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
const page = await ctx.newPage();
await page.goto(APP_URL, { waitUntil: "domcontentloaded" });
await page.waitForTimeout(4000);
const row = page.locator(
  `[data-testid="grace2-case-row"][data-case-id="${CASE_ID}"]`
);
await row.waitFor({ timeout: 20000 });
await row.click();
await page.waitForTimeout(6000);
log("case opened cold");

const running = await page.locator('[data-state="running"]').count().catch(() => 0);
const done = await page.getByText(DONE_RE).count();
log("running-els=" + running + " done-cards=" + done);
if (done > 0 && running === 0) {
  const el = page.getByText(DONE_RE).last();
  await el.scrollIntoViewIfNeeded({ timeout: 1500 }).catch(() => {});
  await page.waitForTimeout(500);
  await page.screenshot({ path: SHOT });
  log("persisted settled screenshot saved");
  console.log("RESULT persisted=true count=" + done + " running=" + running);
} else {
  console.log("RESULT persisted=" + (done > 0) + " count=" + done + " running=" + running + " (not settled)");
}
await browser.close();
process.exit(0);
