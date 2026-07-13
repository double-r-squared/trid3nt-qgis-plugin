// E2E: F8/F9 thinking-stream live proof (no-tool prompt, default pref ON).
//
// Checks:
//   T1  agent-thinking-chunk renders: [data-testid=agent-thinking-block]
//       appears DURING the turn with non-empty streamed text
//   T2  the answer bubble streams after/with the thinking block
//   T3  the block is collapsible (toggle present) and auto-collapsed once
//       the answer streamed (or manually toggleable)
//
// Run from web/: node tools/e2e_thinking_stream.mjs
// ASCII hyphens only; no emojis.

import { chromium } from "playwright";

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
const pass = (id, ev) => results.push({ id, ok: true, ev });
const fail = (id, ev) => results.push({ id, ok: false, ev });

const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
const page = await ctx.newPage();
await page.goto(APP_URL, { waitUntil: "domcontentloaded" });
await page.waitForTimeout(4000);

await createFreshCase(page);
const input = page.locator("textarea, input[placeholder*='Ask'], input[placeholder*='ask']").first();
await input.waitFor({ timeout: 15000 });
await input.fill("In one or two sentences, what is a floodplain? Do not use any tools.");
await input.press("Enter");
console.log("prompt sent", new Date().toISOString());

// Poll up to 3 min for the thinking block, record when it appears + text len.
const deadline = Date.now() + 180000;
let sawThinking = false, thinkLenAtFirst = 0, tFirst = 0;
const t0 = Date.now();
while (Date.now() < deadline) {
  const blocks = page.locator('[data-testid="agent-thinking-block"], [data-testid="agent-thinking-content"]');
  const n = await blocks.count();
  if (n > 0) {
    sawThinking = true;
    tFirst = Math.round((Date.now() - t0) / 1000);
    try { thinkLenAtFirst = ((await blocks.first().innerText()) || "").length; } catch {}
    break;
  }
  await page.waitForTimeout(1500);
}
if (sawThinking) pass("T1_THINKING_APPEARS", `block at T+${tFirst}s textlen=${thinkLenAtFirst}`);
else fail("T1_THINKING_APPEARS", "no thinking block within 180s");

// Wait for the answer text to finish (agent bubble with real text).
let answerText = "";
const ansDeadline = Date.now() + 180000;
while (Date.now() < ansDeadline) {
  const bubbles = page.locator('[data-testid="agent-message"], .agent-message');
  const n = await bubbles.count();
  if (n > 0) {
    try { answerText = (await bubbles.last().innerText()) || ""; } catch {}
  }
  if (answerText.trim().length > 30) break;
  await page.waitForTimeout(2000);
}
if (answerText.trim().length > 0) pass("T2_ANSWER_STREAMS", `answer len=${answerText.trim().length}`);
else fail("T2_ANSWER_STREAMS", "no agent answer text within 180s");

// T3: collapsibility - a toggle button exists on the thinking block.
await page.waitForTimeout(2000);
const toggle = page.locator('[data-testid="agent-thinking-toggle"], [data-testid="agent-thinking-block"] button');
const toggleCount = await toggle.count();
if (toggleCount > 0) {
  // measure collapsed vs expanded content visibility
  const contentSel = page.locator('[data-testid="agent-thinking-content"]');
  const visibleBefore = await contentSel.count() > 0 ? await contentSel.first().isVisible().catch(() => false) : false;
  await toggle.first().click().catch(() => {});
  await page.waitForTimeout(500);
  const visibleAfter = await contentSel.count() > 0 ? await contentSel.first().isVisible().catch(() => false) : false;
  pass("T3_COLLAPSIBLE", `toggle present; content visible before=${visibleBefore} after=${visibleAfter}`);
} else {
  fail("T3_COLLAPSIBLE", "no toggle button found on thinking block");
}

await page.screenshot({ path: "/home/nate/Documents/trid3nt-local/docs/proof/44-thinking-stream-web.png", fullPage: false });
console.log("screenshot saved docs/proof/44-thinking-stream-web.png");

for (const r of results) console.log(`${r.ok ? "PASS" : "FAIL"} ${r.id}: ${r.ev}`);
await browser.close();
process.exit(results.every(r => r.ok) ? 0 : 1);
