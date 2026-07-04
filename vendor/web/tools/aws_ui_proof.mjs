// LIVE Playwright proof: real prompt -> AWS agent (Bedrock) -> raster paints on the map.
// No inject seams (standing rule). Logs every TiTiler tile request as render evidence.
import { chromium } from "playwright";

const SITE = "http://grace2-hazard-web-226996537797.s3-website-us-west-2.amazonaws.com/app";
const PROMPT = "Compute a hillshade for Boulder, Colorado and show it on the map.";
const OUT = "/tmp/aws_proof";
const TILE_HOST = "54.185.114.233:8080";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

let tileOk = 0, tileErr = 0;
page.on("response", (r) => {
  if (r.url().includes(TILE_HOST) && r.url().includes("/cog/tiles/")) {
    if (r.status() === 200) tileOk++; else tileErr++;
  }
});

console.log("[1] loading", SITE);
await page.goto(SITE, { waitUntil: "networkidle", timeout: 60000 });
await page.waitForTimeout(4000);
await page.screenshot({ path: `${OUT}_1_loaded.png` });

// pass the auth gate: wait for either the chat input or the anonymous CTA
const anon = page.getByRole("button", { name: /Continue without saving/i });
try {
  await anon.waitFor({ timeout: 15000 });
  await anon.first().click();
  console.log("[gate] continued anonymously");
  await page.waitForTimeout(2000);
} catch { console.log("[gate] no auth gate shown"); }

console.log("[2] sending real prompt:", PROMPT);
const input = page.locator('[data-testid="chat-input"]');
await input.waitFor({ timeout: 20000 });
await input.click();
await input.fill(PROMPT);
await input.press("Enter");
await page.screenshot({ path: `${OUT}_2_sent.png` });

// watch the run: screenshot every 30s up to 7 min; stop early once tiles flow
const start = Date.now();
let lastShot = 0, shot = 3;
while (Date.now() - start < 420000) {
  await page.waitForTimeout(5000);
  const elapsed = Math.round((Date.now() - start) / 1000);
  if (elapsed - lastShot >= 30) {
    lastShot = elapsed;
    await page.screenshot({ path: `${OUT}_${shot}_t${elapsed}s.png` });
    console.log(`[shot ${shot}] t=${elapsed}s tiles ok=${tileOk} err=${tileErr}`);
    shot++;
  }
  if (tileOk >= 4) {
    console.log(`[tiles] ${tileOk} tile responses 200 — overlay is painting; settling 8s`);
    await page.waitForTimeout(8000);
    break;
  }
}
await page.screenshot({ path: `${OUT}_final.png`, fullPage: false });
console.log(`[done] tiles ok=${tileOk} err=${tileErr}`);
console.log(tileOk > 0 ? "[PASS] raster tiles served from AWS TiTiler into the live UI"
                       : "[FAIL] no tile requests observed");
await browser.close();
process.exit(tileOk > 0 ? 0 : 1);
