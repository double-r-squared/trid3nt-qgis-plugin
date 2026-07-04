// Rehydration proof v2: raster Case (user identity) + vector Case (its owner identity).
import { chromium } from "playwright";

const SITE = "http://grace2-hazard-web-226996537797.s3-website-us-west-2.amazonaws.com/app";
const TILE_HOST = "54.185.114.233:8080";

async function openCase(browser, anonId, caseTitle, settleMs) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  let tiles = 0;
  page.on("response", (r) => {
    if (r.url().includes(TILE_HOST) && r.url().includes("/cog/tiles/") && r.status() === 200) tiles++;
  });
  await page.addInitScript((id) => localStorage.setItem("grace2.anonymous_user_id", id), anonId);
  await page.goto(SITE, { waitUntil: "networkidle", timeout: 60000 });
  const anon = page.getByRole("button", { name: /Continue without saving/i });
  try { await anon.waitFor({ timeout: 15000 }); await anon.click(); } catch {}
  await page.waitForTimeout(3000);
  await page.getByText(caseTitle, { exact: false }).first().click();
  await page.waitForTimeout(settleMs);
  return { page, ctx, tiles: () => tiles };
}

const browser = await chromium.launch();

console.log("[A] RASTER rehydrate — user's Seattle hillshade case");
const a = await openCase(browser, "01KTWVKMNWXWFDKH5DQ4G95RPF", "Compute Hillshade Seattle Washington", 10000);
const rasterTiles = a.tiles();
await a.page.screenshot({ path: "/tmp/rehydrate_A_raster.png" });
console.log(`    tiles=${rasterTiles}`);
await a.ctx.close();

console.log("[B] VECTOR rehydrate — Travis boundary case");
const b = await openCase(browser, "01KTWVDH4TA9GFRBQEN6BEQMEE", "Travis Count", 9000);
const panelHasBoundary = await b.page.evaluate(() =>
  document.body.innerText.includes("Admin Boundaries") || document.body.innerText.includes("admin-county"));
await b.page.screenshot({ path: "/tmp/rehydrate_B_vector.png" });
console.log(`    boundary in panel=${panelHasBoundary}`);
await b.ctx.close();
await browser.close();

const pass = rasterTiles >= 3 && panelHasBoundary;
console.log(pass ? "[PASS] raster + vector Cases both rehydrate" :
  `[RESULT] rasterTiles=${rasterTiles} vectorPanel=${panelHasBoundary}`);
process.exit(pass ? 0 : 1);
