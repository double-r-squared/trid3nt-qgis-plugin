// LIVE groundwater contamination via the AWS UI (fresh run) — also the clean
// confirmation of the bbox rectangle on a fresh zoom. Auto-clicks the Proceed gate.
import { chromium } from "playwright";

const SITE = "http://grace2-hazard-web-226996537797.s3-website-us-west-2.amazonaws.com/app";
const PROMPT = "Model a groundwater contamination scenario near Fort Myers, Florida and report the plume metrics.";
const OUT = "/tmp/aws_gw_ui";
const TILE_HOST = "54.185.114.233:8080";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
let tiles = 0;
page.on("response", (r) => { if (r.url().includes(TILE_HOST) && r.url().includes("/cog/tiles/") && r.status() === 200) tiles++; });

await page.goto(SITE, { waitUntil: "networkidle", timeout: 60000 });
const anon = page.getByRole("button", { name: /Continue without saving/i });
try { await anon.waitFor({ timeout: 15000 }); await anon.click(); } catch {}
await page.waitForTimeout(2500);

console.log("[1] sending groundwater prompt");
const input = page.locator('[data-testid="chat-input"]');
await input.waitFor({ timeout: 20000 });
await input.fill(PROMPT);
await input.press("Enter");

const start = Date.now();
let proceeded = false, shot = 1, lastShot = 0, done = false;
const proceedBtn = page.locator('[data-testid="payload-warning-button-proceed"]');
while (Date.now() - start < 420000) {
  await page.waitForTimeout(4000);
  const t = Math.round((Date.now() - start) / 1000);
  if (!proceeded && await proceedBtn.count()) {
    await proceedBtn.first().click();
    proceeded = true;
    console.log(`[gate] clicked Proceed at t=${t}s`);
  }
  if (t - lastShot >= 30) {
    lastShot = t;
    await page.screenshot({ path: `${OUT}_${shot}_t${t}s.png` });
    console.log(`[shot ${shot}] t=${t}s proceeded=${proceeded} tiles=${tiles}`);
    shot++;
  }
  const body = await page.evaluate(() => document.body.innerText);
  if (/plume|concentration|mg\/L|TCE|MODFLOW|groundwater model/i.test(body) && (tiles > 0 || /mg\/L|concentration/i.test(body)) && proceeded && t > 40) {
    done = true; console.log(`[done] plume narration present at t=${t}s; settling`); await page.waitForTimeout(6000); break;
  }
}
await page.screenshot({ path: `${OUT}_final.png` });
console.log(`[result] proceeded=${proceeded} tiles=${tiles} done=${done}`);
await browser.close();
process.exit(done ? 0 : 1);
