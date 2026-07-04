// Post-deploy verification (ZERO Bedrock): confirms the NEW web bundle (rewritten
// auth.ts in passthrough mode) still boots, the anon gate works, and a SAVED case
// rehydrates its layer. Also captures the map for the bbox analysis-extent rectangle.
import { chromium } from "playwright";

const SITE = "http://grace2-hazard-web-226996537797.s3-website-us-west-2.amazonaws.com/app";
const OWNER = "01KTZMJW9T9GRQYC0CVNN50F15";
const CASE_TEXT = "Fetch 10 M Elevation DEM Boulder";
const TILE_HOST = "54.185.114.233:8080";
const OUT = "/tmp/aws_postdeploy";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
let tiles = 0;
page.on("response", (r) => { if (r.url().includes(TILE_HOST) && r.url().includes("/cog/tiles/") && r.status() === 200) tiles++; });

await page.addInitScript((id) => localStorage.setItem("grace2.anonymous_user_id", id), OWNER);
await page.goto(SITE, { waitUntil: "networkidle", timeout: 60000 });

// [1] anon gate (proves AuthGuard passthrough renders the gate, not a crash)
const anon = page.getByRole("button", { name: /Continue without saving/i });
let gateSeen = false;
try { await anon.waitFor({ timeout: 15000 }); gateSeen = true; await anon.click(); } catch {}
await page.waitForTimeout(2500);

// [2] app booted: chat-input present == new bundle + auth.ts passthrough OK
const chatInput = page.locator('[data-testid="chat-input"]');
const booted = await chatInput.count().then((c) => c > 0).catch(() => false);
await page.screenshot({ path: `${OUT}_1_booted.png` });

// [3] wait for WS to connect + case list to load, then open the saved case
let caseOpened = false;
const caseLink = page.getByText(CASE_TEXT, { exact: false }).first();
for (let i = 0; i < 18; i++) { // up to ~36s
  if (await caseLink.count()) break;
  await page.waitForTimeout(2000);
}
try { await caseLink.click({ timeout: 8000 }); caseOpened = true; } catch {}
await page.waitForTimeout(11000);
await page.screenshot({ path: `${OUT}_2_case.png` });

const layerRows = await page.locator('[data-testid="layer-row"], [class*="layer-row"]').count().catch(() => 0);
const body = await page.evaluate(() => document.body.innerText);

console.log(`[boot] anonGateSeen=${gateSeen} chatInputPresent=${booted}`);
console.log(`[case] opened=${caseOpened} layerRows=${layerRows} rehydratedTiles=${tiles}`);
console.log(`[errors] pageerrors=${errors.length}${errors.length ? " :: " + errors.slice(0, 3).join(" | ") : ""}`);
console.log(`[bodyHasDEM] ${/DEM|elevation|Boulder/i.test(body)}`);

const pass = booted && errors.length === 0;
console.log(pass ? "[PASS] new bundle boots + auth passthrough OK (no Bedrock)" : "[REVIEW] see signals");
await browser.close();
process.exit(pass ? 0 : 1);
