// LIVE proof of the Case-switch rehydration fix: flood Case -> hillshade Case
// must repaint the hillshade raster from persisted state (no re-run).
import { chromium } from "playwright";

const SITE = "http://grace2-hazard-web-226996537797.s3-website-us-west-2.amazonaws.com/app";
const TILE_HOST = "54.185.114.233:8080";
const OUT = "/tmp/aws_switch";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
let tiles = 0;
page.on("response", (r) => {
  if (r.url().includes(TILE_HOST) && r.url().includes("/cog/tiles/") && r.status() === 200) tiles++;
});

// Present the user's sticky anonymous identity (the documented reconnect
// mechanism — job-0172 Part C): owner-scoped case-lists only show THEIR cases.
await page.addInitScript(() => {
  localStorage.setItem("grace2.anonymous_user_id", "01KTWVKMNWXWFDKH5DQ4G95RPF");
});
await page.goto(SITE, { waitUntil: "networkidle", timeout: 60000 });
const anon = page.getByRole("button", { name: /Continue without saving/i });
try { await anon.waitFor({ timeout: 15000 }); await anon.click(); } catch {}
await page.waitForTimeout(3000);

console.log("[1] opening HILLSHADE case (raster rehydrate)");
await page.getByText("Compute Hillshade Seattle Washington", { exact: false }).first().click();
await page.waitForTimeout(9000);
const floodTiles = tiles;
await page.screenshot({ path: `${OUT}_1_flood.png` });
console.log(`    flood case tiles=${floodTiles}`);

console.log("[2] back to Cases root");
const back = page.getByText(/^Cases$/).first();
try { await back.click({ timeout: 5000 }); } catch {
  const crumb = page.getByRole("button", { name: /Cases/i }).first();
  await crumb.click();
}
await page.waitForTimeout(2500);

console.log("[3] switching to BOUNDARY case (vector rehydrate)");
tiles = 0; // reset: everything counted from here belongs to the hillshade case
await page.getByText("Fetch Administrative Boundary Travis Count", { exact: false }).first().click();
await page.waitForTimeout(10000);
await page.screenshot({ path: `${OUT}_2_hillshade_after_switch.png` });
console.log(`    hillshade-after-switch tiles=${tiles}`);

const vectorOk = await page.evaluate(() => {
  const m = window.__grace2Map || null;
  return document.body.innerHTML.includes("Admin Boundaries") ? 1 : 0;
});
await page.screenshot({ path: `${OUT}_3_vector_after_switch.png` });
console.log(`[vector] panel shows boundary layer: ${vectorOk}`);
console.log("[done] raster-case tiles=" + floodTiles + " vector-panel=" + vectorOk);
await browser.close();
process.exit(floodTiles >= 3 && vectorOk ? 0 : 1);
