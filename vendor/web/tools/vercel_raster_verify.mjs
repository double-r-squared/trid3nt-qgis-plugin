// LIVE raster-render verification on the VERCEL frontend (grace-2.vercel.app).
// Confirms whether COG raster tiles paint (the CORS question) + captures the
// non-raster UI-batch fixes. Decoupled from agent wake: TiTiler (/cog,/tiles)
// and the cold-read case path are always-on, so a blank map here isolates CORS.
//
// Signals captured:
//   - tileOk      : 200 responses from the CloudFront tile host (/cog | /tiles)
//   - tileFailed  : requestfailed on tile URLs (net::ERR_FAILED = CORS block)
//   - corsMsgs    : console errors mentioning CORS / Access-Control / blocked
//   - chatInput   : signed-in app booted
// Creds via env (source /tmp/grace2_e2e_creds.sh first).
import { chromium } from "playwright";

const SITE = "https://grace-2.vercel.app/app";
const TILE_HOST = "d125yfbyjrpbre.cloudfront.net";
const EMAIL = process.env.GRACE2_DEMO_EMAIL;
const PW = process.env.GRACE2_DEMO_PASSWORD;
const OUT = "/tmp/vercel_raster";

const isTile = (u) =>
  u.includes(TILE_HOST) && (u.includes("/cog") || u.includes("/tiles"));

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

const tileOk = [];
const tileFailed = [];
const corsMsgs = [];
const pageErrors = [];

page.on("response", (r) => {
  const u = r.url();
  if (isTile(u) && r.status() === 200) tileOk.push(u.slice(-70));
});
page.on("requestfailed", (req) => {
  const u = req.url();
  if (isTile(u)) tileFailed.push(`${(req.failure()?.errorText) || "?"} ${u.slice(-60)}`);
});
page.on("console", (m) => {
  const t = m.text();
  if (/CORS|Access-Control|has been blocked|ERR_FAILED|cross-origin/i.test(t))
    corsMsgs.push(t.slice(0, 180));
});
page.on("pageerror", (e) => pageErrors.push(String(e).slice(0, 160)));

console.log(`[0] goto ${SITE}`);
await page.goto(SITE, { waitUntil: "networkidle", timeout: 60000 });
await page.waitForTimeout(2500);
await page.screenshot({ path: `${OUT}_1_wall.png` });

console.log("[1] sign in (Hosted UI)");
await page.getByRole("button", { name: /Sign in|Sign up/i }).first().click().catch(() => {});
await page.waitForTimeout(5000);
const onHostedUI = /amazoncognito\.com/.test(page.url());
console.log(`    hostedUI=${onHostedUI} url=${page.url().slice(0, 70)}`);
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
await page.screenshot({ path: `${OUT}_2_signedin.png` });
const chatInput = await page.locator('[data-testid="chat-input"]').count();
console.log(`    backOnApp=${page.url().includes("vercel.app")} chatInput=${chatInput > 0}`);

console.log("[2] open a case (first card with a raster history)");
// log the visible cases so we know what we clicked
const caseText = await page.evaluate(() => document.body.innerText.slice(0, 600));
console.log("    cases-panel head:\n" + caseText.split("\n").filter(Boolean).slice(0, 12).map((l) => "      " + l).join("\n"));
// click the first case row/card
const clicked = await page.evaluate(() => {
  const cands = Array.from(document.querySelectorAll('[data-testid*="case"], button, li, [role="button"]'));
  // pick the first element whose text looks like a case title (Mexico/surge/flood/Beach), else first case-testid
  const byText = cands.find((e) => /mexico|surge|beach|flood|inundation/i.test(e.textContent || "") && (e.textContent || "").length < 80);
  const target = byText || document.querySelector('[data-testid*="case"]');
  if (target) { target.click(); return (target.textContent || "").slice(0, 60); }
  return null;
});
console.log(`    clicked case: ${clicked}`);
await page.waitForTimeout(12000); // allow cold-read snapshot + tile fetches
await page.screenshot({ path: `${OUT}_3_case_map.png`, fullPage: false });

console.log("[3] toggle a raster layer on (force tile fetch) if a layer toggle exists");
await page.evaluate(() => {
  const toggles = Array.from(document.querySelectorAll('input[type="checkbox"]'));
  // turn the first few layer checkboxes ON to force raster paint
  toggles.slice(0, 3).forEach((t) => { if (!t.checked) t.click(); });
}).catch(() => {});
await page.waitForTimeout(9000);
await page.screenshot({ path: `${OUT}_4_layers_on.png` });

console.log("[4] open settings (verify scrubber hidden + legend dock)");
await page.getByRole("button", { name: /settings/i }).first().click().catch(() => {});
await page.waitForTimeout(2500);
await page.screenshot({ path: `${OUT}_5_settings.png` });

console.log("\n==================== RESULT ====================");
console.log(`tileOk(200)   = ${tileOk.length}`);
console.log(`tileFailed    = ${tileFailed.length}`);
console.log(`corsMsgs      = ${corsMsgs.length}`);
console.log(`pageErrors    = ${pageErrors.length}`);
console.log(`chatInput     = ${chatInput > 0}`);
if (tileFailed.length) console.log("\nTILE FAILURES (first 5):\n" + tileFailed.slice(0, 5).map((s) => "  " + s).join("\n"));
if (corsMsgs.length) console.log("\nCORS CONSOLE (first 5):\n" + corsMsgs.slice(0, 5).map((s) => "  " + s).join("\n"));
if (tileOk.length) console.log("\nTILE OK (first 3):\n" + tileOk.slice(0, 3).map((s) => "  " + s).join("\n"));
if (pageErrors.length) console.log("\nPAGE ERRORS (first 3):\n" + pageErrors.slice(0, 3).map((s) => "  " + s).join("\n"));

const rastersRender = tileOk.length >= 3 && tileFailed.length === 0 && corsMsgs.length === 0;
console.log(`\nVERDICT: rasters ${rastersRender ? "RENDER (CORS ok)" : "DO NOT render"}`);
if (!rastersRender && (tileFailed.length || corsMsgs.length))
  console.log("        -> blocked by CORS (run scripts/fix_cloudfront_tiles_cors.py)");
console.log("screenshots: " + OUT + "_{1_wall,2_signedin,3_case_map,4_layers_on,5_settings}.png");
await browser.close();
process.exit(rastersRender ? 0 : 2);
