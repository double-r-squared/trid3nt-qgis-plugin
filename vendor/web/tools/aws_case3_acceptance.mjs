// LIVE acceptance: Case 3 (Track C) on the deployed AWS HTTPS site. NWS active
// flood warning -> MRMS accumulated precip over the warning polygon -> SFINCS ->
// flood render. Exercises the Track C s3:// forcing-raster read fix. Targets a
// state with an ACTIVE flood warning (env GRACE2_CASE3_STATE, default Texas).
import { chromium } from "playwright";

const SITE = "https://d125yfbyjrpbre.cloudfront.net/app";
const CF = "d125yfbyjrpbre.cloudfront.net";
const EMAIL = process.env.GRACE2_DEMO_EMAIL;
const PW = process.env.GRACE2_DEMO_PASSWORD;
const STATE = process.env.GRACE2_CASE3_STATE || "Texas";
const PROMPT = `Check for active flood warnings in ${STATE} and model the resulting flooding from the observed MRMS precipitation over the warning area, then show the inundation on the map.`;
const OUT = "/tmp/aws_case3";
const BUDGET_MS = 22 * 60 * 1000;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const errors = [];
let anyTiles = 0, floodTiles = 0;
page.on("pageerror", (e) => errors.push(String(e)));
// Count rendered raster tiles. On the DEPLOYED site tiles come through
// CloudFront (https://<cf>/cog/tiles/...), NOT the raw TiTiler IP — so match by
// path on any host (the old IP-only match left tiles=0 forever). Separate
// FLOOD-DEPTH tiles (the ?url= points at the runs bucket / a flood_depth COG)
// from MRMS-precip + NWS-warning tiles so completion keys on the flood actually
// being painted on the map, not on any earlier layer.
page.on("response", (r) => {
  const url = r.url();
  if (r.status() < 400 && url.includes("/cog/tiles/")) {
    anyTiles++;
    let dec = url; try { dec = decodeURIComponent(url); } catch {}
    if (/flood[_-]?depth|hazard-runs/i.test(dec)) floodTiles++;
  }
});

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
console.log(`[signin] chatInput=${booted}`);
if (!booted) { await page.screenshot({ path: `${OUT}_signin_fail.png` }); await browser.close(); process.exit(1); }

const input = page.locator('[data-testid="chat-input"]');
await input.fill(PROMPT);
await input.press("Enter");
console.log(`[prompt] Case 3 ${STATE}; waiting (auto-approving gates)`);

const start = Date.now();
let done = false, failed = false, shot = 1, lastShot = 0, gates = 0, floodShotTaken = false;
while (Date.now() - start < BUDGET_MS) {
  await page.waitForTimeout(4000);
  const t = Math.round((Date.now() - start) / 1000);
  // auto-approve solver/payload gates
  for (const sel of ['[data-testid="payload-warning-button-proceed"]', '[data-testid="sandbox-card-proceed"]']) {
    const b = page.locator(sel);
    if (await b.count()) { await b.first().click().catch(() => {}); gates++; console.log(`[gate] ${sel} t=${t}s`); }
  }
  for (const name of [/^Proceed$/i, /^Run$/i, /^Approve$/i, /^Confirm$/i, /Run anyway/i]) {
    const b = page.getByRole("button", { name });
    if (await b.count()) { await b.first().click().catch(() => {}); gates++; console.log(`[gate] ${name} t=${t}s`); }
  }
  if (t - lastShot >= 60) { lastShot = t; await page.screenshot({ path: `${OUT}_${shot}_t${t}s.png` }); console.log(`[shot ${shot}] t=${t}s anyTiles=${anyTiles} floodTiles=${floodTiles} gates=${gates}`); shot++; }

  const body = await page.evaluate(() => document.body.innerText);

  // Capture the render moment as soon as the flood-depth raster first paints.
  if (floodTiles >= 1 && !floodShotTaken) {
    floodShotTaken = true; await page.waitForTimeout(2500);
    await page.screenshot({ path: `${OUT}_floodrender_t${t}s.png` });
    console.log(`[flood-render] floodTiles=${floodTiles} t=${t}s`);
  }

  // HARD completion: the flood-depth raster is actually ON THE MAP (>=3 tiles).
  const rendered = floodTiles >= 3 && t > 90;
  // Narration completion: result-with-metrics phrasing ONLY (never in-progress
  // "modeling/SFINCS"), AND tied to at least one rendered flood tile so the
  // warning-area's own km² figure can't trigger a premature finish.
  const resultNarr = floodTiles >= 1 && (
    /(peak|maximum|max)\s+(flood\s+)?depth[^.\n]{0,40}?[\d.]+\s*m\b/i.test(body) ||
    /[\d.]+\s*(km²|km2|square kilomet)[^.\n]{0,30}?(flood|inundat)/i.test(body) ||
    /(flood[-\s]?depth|inundation)[^.\n]{0,50}?(rendered|added to the map|on the map|now (on|visible)|published)/i.test(body)
  );
  // Honest no-active-warning exit (legitimate outcome, not a failure).
  const noWarn = /no active flood warning/i.test(body) && t > 40;
  // Early FAILURE: agent reports the solve did NOT complete — bail fast rather
  // than burn the full budget (exit non-zero so the orchestrator investigates).
  const failNarr = floodTiles === 0 && t > 60 && /(upstream[_\s-]?api[_\s-]?error|did not complete|no flood[- ]?depth layer|solver (failed|error|encountered)|model setup failed)/i.test(body);

  if (rendered || resultNarr) {
    done = true;
    console.log(`[done] rendered=${rendered} resultNarr=${resultNarr} floodTiles=${floodTiles} t=${t}s; settling`);
    await page.waitForTimeout(9000);
    break;
  }
  if (noWarn) { console.log(`[no-warning] honest no-active-warning at t=${t}s`); break; }
  if (failNarr) { failed = true; console.log(`[FAIL-narr] flood solve reported failure at t=${t}s`); await page.waitForTimeout(4000); break; }
}
await page.screenshot({ path: `${OUT}_final.png` });
const body = await page.evaluate(() => document.body.innerText);
console.log(`[result] done=${done} failed=${failed} anyTiles=${anyTiles} floodTiles=${floodTiles} gates=${gates} errors=${errors.length}`);
console.log("[tail]\n" + body.split("\n").filter(Boolean).slice(-24).join("\n"));
await browser.close();
process.exit(done ? 0 : 1);
