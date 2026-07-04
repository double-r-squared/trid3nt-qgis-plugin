// Live demo drive (dev account): sign in -> new case -> type a prompt -> timed
// screenshots of the result. Reusable spot-test harness for the AFK Playwright loop.
//   GRACE2_DEMO_EMAIL=.. GRACE2_DEMO_PASSWORD=.. PROMPT="..." OUT=/tmp/drive_x node tools/drive_demo.mjs
import { chromium } from "playwright";

const SITE = "https://d125yfbyjrpbre.cloudfront.net/app";
const CF = "d125yfbyjrpbre.cloudfront.net";
const EMAIL = process.env.GRACE2_DEMO_EMAIL;
const PW = process.env.GRACE2_DEMO_PASSWORD;
const PROMPT = process.env.PROMPT || "Hello";
const OUT = process.env.OUT || "/tmp/drive";
const MARKS = (process.env.MARKS || "30,75,125,170").split(",").map((s) => parseInt(s, 10));

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));

async function txt() { return page.evaluate(() => document.body.innerText); }

// ---- sign in ----
await page.goto(SITE, { waitUntil: "networkidle", timeout: 60000 });
await page.waitForTimeout(2000);
await page.getByRole("button", { name: /Sign in|Sign up/i }).first().click().catch(() => {});
await page.waitForTimeout(5000);
if (/amazoncognito\.com/.test(page.url())) {
  await page.locator('input[name="username"]:visible, input[type="email"]:visible').first().fill(EMAIL);
  await page.locator('input[name="password"]:visible, input[type="password"]:visible').first().fill(PW);
  await page.locator('input[name="signInSubmitButton"]:visible, input[type="submit"]:visible, button[type="submit"]:visible').first().click().catch(() => {});
}
for (let i = 0; i < 20; i++) { await page.waitForTimeout(2000); if (page.url().includes(CF) && !/amazoncognito/.test(page.url())) break; }
await page.waitForTimeout(6000);
console.log("[login] backOnApp=", page.url().includes(CF) && !/amazoncognito/.test(page.url()));

// ---- wait for the WS to connect, then open a fresh case ----
await page.waitForSelector('[data-testid="grace2-cases-new"]', { timeout: 30000 }).catch(() => {});
// If the agent box is asleep the app shows a "Wake up" button -- click it + wait.
const wake = page.getByRole("button", { name: /Wake up/i });
if (await wake.count()) {
  console.log("[wake] clicking Wake up + waiting for the box");
  await wake.first().click().catch(() => {});
  for (let i = 0; i < 40; i++) {
    const t = await txt();
    if (!/Wake up/i.test(t) && !/Connecting/i.test(t)) break;
    await page.waitForTimeout(4000);
  }
}
for (let i = 0; i < 25; i++) { if (!/Connecting/i.test(await txt())) break; await page.waitForTimeout(3000); }
await page.screenshot({ path: `${OUT}_0_ready.png` });

// Type into the ROOT composer in the Cases state -- the agent auto-creates the
// case + derives the bounding box. NEVER click New Case (AOI-first onboarding) or
// an existing case row (pinned to its own bbox).
await page.waitForSelector('[data-testid="chat-input"]', { timeout: 45000 });
await page.waitForTimeout(2000);

// ---- send the prompt ----
const input = page.locator('[data-testid="chat-input"]');
await input.fill(PROMPT);
await input.press("Enter");
console.log("[sent]", PROMPT.slice(0, 70), "...");

// ---- timed screenshots ----
let prev = 0;
for (let k = 0; k < MARKS.length; k++) {
  await page.waitForTimeout(Math.max(0, (MARKS[k] - prev) * 1000)); prev = MARKS[k];
  await page.screenshot({ path: `${OUT}_${k + 1}_${MARKS[k]}s.png` }).catch(() => {});
  console.log(`[shot ${k + 1}] ~${MARKS[k]}s body~`, (await txt()).replace(/\s+/g, " ").slice(0, 140));
}
console.log("[errors]", errors.length, errors.slice(0, 3));
await browser.close();
