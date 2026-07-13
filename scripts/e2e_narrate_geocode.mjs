// TRID3NT Local -- narration + geolocation showcase capture.
// Sends a prompt that makes the local model (qwen3:8b-16k) geocode a city and
// narrate, then screenshots mid-stream and at turn end.
// Run: node scripts/e2e_narrate_geocode.mjs
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const req = createRequire(import.meta.url);
let playwright;
for (const p of [
  path.resolve(__dirname, "../vendor/web/node_modules/playwright"),
  "/home/nate/Documents/GRACE-2/web/node_modules/playwright",
  "/home/nate/Documents/GRACE-2/web/node_modules/@playwright/test",
]) {
  if (fs.existsSync(p)) { playwright = req(p); break; }
}
if (!playwright) { console.error("playwright not found"); process.exit(1); }

const PROOF = path.resolve(__dirname, "../docs/proof");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const PROMPT =
  "Geocode Chattanooga, Tennessee and zoom the map there. Then briefly narrate what makes this area prone to river flooding.";


// DATA-INTEGRITY GUARD (2026-07-12): the local server maps EVERY anonymous
// session to one shared local user, so a fresh boot RESUMES that user's
// last-active REAL case. Prompting without creating a case first mutated
// real cases (bbox overwrite + layer pollution). Always create a brand-new
// case before sending any prompt; never select or reuse an existing case.
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

async function main() {
  const { chromium } = playwright;
  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"] });
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await ctx.addInitScript(() => {
    window.localStorage.setItem("grace2_anonymous_accepted", "true");
    window.localStorage.setItem("grace2.anonymous_user_id", "e2e-narrate-demo");
  });
  const page = await ctx.newPage();
  await page.goto("http://127.0.0.1:5173/app", { waitUntil: "domcontentloaded", timeout: 30000 });
  await sleep(6000);

  const cont = page.locator('button:has-text("Continue"), button:has-text("Skip"), button:has-text("anonymous")');
  if (await cont.count()) { await cont.first().click(); await sleep(2000); }

  await createFreshCase(page);

  const input = page.locator('textarea, [contenteditable="true"], [role="textbox"]').first();
  await input.waitFor({ timeout: 30000 });
  await input.click();
  await input.fill(PROMPT);
  const send = page.locator('button[type="submit"], button[aria-label*="send"]').first();
  (await send.isVisible().catch(() => false)) ? await send.click() : await input.press("Enter");
  console.log("[demo] prompt sent:", PROMPT);

  // Baseline AFTER the prompt is in the DOM -- new-assistant-text detection
  // must not match words from the user's own message.
  await sleep(3000);
  const baseline = (await page.innerText("body").catch(() => "")).length;
  const t0 = Date.now();
  let midShot = false;
  let lastLen = baseline;
  let stableSince = Date.now();
  while (Date.now() - t0 < 240000) {
    const body = await page.innerText("body").catch(() => "");
    const grown = body.length - baseline;
    if (!midShot && grown > 150) {
      await sleep(2500); // let more narration stream in
      await page.screenshot({ path: path.join(PROOF, "10-narrate-geocode-mid.png") });
      console.log("[demo] mid-turn screenshot at +" + grown + " chars of assistant output");
      midShot = true;
    }
    if (body.length !== lastLen) { lastLen = body.length; stableSince = Date.now(); }
    // Turn end: substantial output present AND no text growth for 15s
    if (midShot && grown > 300 && Date.now() - stableSince > 15000) break;
    await sleep(2000);
  }

  // Known 8B quirk: it may ask WHICH location instead of extracting it.
  // Reply like a user would, then wait for the real geocode + narration turn.
  const bodyNow = await page.innerText("body").catch(() => "");
  if (/provide|specify|clarif|which location|could you/i.test(bodyNow.slice(-600))) {
    console.log("[demo] model asked for clarification -- replying like a user");
    await input.click();
    await input.fill("Chattanooga, Tennessee");
    await input.press("Enter");
    const base2 = (await page.innerText("body").catch(() => "")).length;
    let last2 = base2, stable2 = Date.now();
    const t1 = Date.now();
    let mid2 = false;
    while (Date.now() - t1 < 240000) {
      const b = await page.innerText("body").catch(() => "");
      if (!mid2 && b.length - base2 > 150) {
        await sleep(2500);
        await page.screenshot({ path: path.join(PROOF, "10-narrate-geocode-mid.png") });
        console.log("[demo] mid-turn screenshot (follow-up turn)");
        mid2 = true;
      }
      if (b.length !== last2) { last2 = b.length; stable2 = Date.now(); }
      if (mid2 && b.length - base2 > 300 && Date.now() - stable2 > 15000) break;
      await sleep(2000);
    }
  }
  await sleep(1000);
  await page.screenshot({ path: path.join(PROOF, "11-narrate-geocode-final.png") });
  console.log("[demo] final screenshot (narration + map position)");
  await browser.close();
}
main().catch((e) => { console.error(e); process.exit(1); });
