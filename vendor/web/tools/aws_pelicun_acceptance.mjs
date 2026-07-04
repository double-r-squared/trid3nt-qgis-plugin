// LIVE acceptance: Pelicun M5.5 (Track A) on the deployed AWS HTTPS site.
// Signs in (mandatory Cognito), runs a flood->damage prompt, auto-approves the
// solver/payload gates, waits through the SFINCS solve, screenshots ImpactPanel.
import { chromium } from "playwright";

const SITE = "https://d125yfbyjrpbre.cloudfront.net/app";
const CF = "d125yfbyjrpbre.cloudfront.net";
const EMAIL = process.env.GRACE2_DEMO_EMAIL;
const PW = process.env.GRACE2_DEMO_PASSWORD;
const PROMPT = "First run a SFINCS pluvial flood simulation for a 100-year storm near Fort Myers, Florida. Once the flood depth layer is ready, assess the building damage and economic losses for that flood (compute the impact envelope).";
const OUT = "/tmp/aws_pelicun";
const BUDGET_MS = 28 * 60 * 1000;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const errors = [];
let tiles = 0;
page.on("pageerror", (e) => errors.push(String(e)));
page.on("response", (r) => { if (r.url().includes("54.185.114.233:8080") && r.url().includes("/cog/tiles/") && r.status() === 200) tiles++; });

// --- sign in (Cognito Hosted UI) ---
await page.goto(SITE, { waitUntil: "networkidle", timeout: 60000 });
await page.waitForTimeout(2000);
await page.getByRole("button", { name: /Sign in|Sign up/i }).first().click().catch(() => {});
await page.waitForTimeout(5000);
const u = page.locator('input[name="username"]:visible, input[type="email"]:visible').first();
await u.waitFor({ timeout: 12000 });
await u.fill(EMAIL);
await page.locator('input[name="password"]:visible, input[type="password"]:visible').first().fill(PW);
await page.locator('input[name="signInSubmitButton"]:visible, input[type="submit"]:visible, button[type="submit"]:visible').first().click().catch(() => {});
for (let i = 0; i < 20; i++) { await page.waitForTimeout(1500); if (page.url().includes(CF) && !/amazoncognito/.test(page.url())) break; }
await page.waitForTimeout(5000);
const booted = await page.locator('[data-testid="chat-input"]').count();
console.log(`[signin] backOnApp=${page.url().includes(CF) && !/amazoncognito/.test(page.url())} chatInput=${booted}`);
if (!booted) { await page.screenshot({ path: `${OUT}_signin_fail.png` }); console.log("[FAIL] sign-in did not reach app"); await browser.close(); process.exit(1); }

// --- send the prompt ---
const input = page.locator('[data-testid="chat-input"]');
await input.fill(PROMPT);
await input.press("Enter");
console.log("[prompt] sent; waiting through SFINCS solve + pelicun (auto-approving gates)");

const start = Date.now();
let done = false, shot = 1, lastShot = 0, gatesClicked = 0;
while (Date.now() - start < BUDGET_MS) {
  await page.waitForTimeout(4000);
  const t = Math.round((Date.now() - start) / 1000);
  // auto-approve any gate that appears (payload warning + solver confirm)
  for (const sel of ['[data-testid="payload-warning-button-proceed"]', '[data-testid="sandbox-card-proceed"]']) {
    const b = page.locator(sel);
    if (await b.count()) { await b.first().click().catch(() => {}); gatesClicked++; console.log(`[gate] clicked ${sel} at t=${t}s`); }
  }
  for (const name of [/Proceed/i, /^Run$/i, /Run anyway/i, /^Approve$/i, /^Confirm$/i]) {
    const b = page.getByRole("button", { name });
    if (await b.count()) { await b.first().click().catch(() => {}); gatesClicked++; console.log(`[gate] clicked btn ${name} at t=${t}s`); }
  }
  if (t - lastShot >= 60) { lastShot = t; await page.screenshot({ path: `${OUT}_${shot}_t${t}s.png` }); console.log(`[shot ${shot}] t=${t}s tiles=${tiles} gates=${gatesClicked}`); shot++; }
  // REAL completion only: the ImpactPanel rendered, or a real $loss figure / "N structures damaged" in narration (NOT tool-card labels). Require t>150 to ride past the flood solve.
  const panel = await page.locator('[data-testid="grace2-impact-panel"], [data-testid="grace2-impact-stat-structures"], [data-testid="grace2-impact-stat-loss"]').count().catch(() => 0);
  const body = await page.evaluate(() => document.body.innerText);
  const realLoss = /\$\s?\d{1,3}(,\d{3})+/.test(body) || /\b\d[\d,]*\s+structures?\s+(were\s+)?(damaged|destroyed|affected)/i.test(body);
  if ((panel > 0 || realLoss) && t > 150) { done = true; console.log(`[done] panel=${panel} realLoss=${realLoss} at t=${t}s; settling`); await page.waitForTimeout(8000); break; }
}
await page.screenshot({ path: `${OUT}_final.png`, fullPage: false });
const body = await page.evaluate(() => document.body.innerText);
console.log(`[result] done=${done} tiles=${tiles} gates=${gatesClicked} errors=${errors.length}`);
console.log("[tail]\n" + body.split("\n").filter(Boolean).slice(-22).join("\n"));
await browser.close();
process.exit(done ? 0 : 1);
