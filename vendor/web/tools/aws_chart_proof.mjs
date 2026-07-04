// LIVE Playwright proof of CHART EMISSION on AWS (untested-in-PW feature).
// Real prompt -> Bedrock chains fetch_dem -> generate_histogram -> chart envelope
// -> ChartStack vega-embed renders an SVG. No inject seams.
import { chromium } from "playwright";

const SITE = "http://grace2-hazard-web-226996537797.s3-website-us-west-2.amazonaws.com/app";
const PROMPT = "Fetch the 10 m elevation (DEM) for Boulder, Colorado and plot a histogram of the elevation values.";
const OUT = "/tmp/aws_chart";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

await page.goto(SITE, { waitUntil: "networkidle", timeout: 60000 });
const anon = page.getByRole("button", { name: /Continue without saving/i });
try { await anon.waitFor({ timeout: 15000 }); await anon.click(); } catch {}
await page.waitForTimeout(3000);

console.log("[1] sending:", PROMPT);
const input = page.locator('[data-testid="chat-input"]');
await input.waitFor({ timeout: 20000 });
await input.click();
await input.fill(PROMPT);
await input.press("Enter");
await page.screenshot({ path: `${OUT}_1_sent.png` });

const start = Date.now();
let chartUp = false, lastShot = 0, shot = 2;
const stack = page.locator('[data-testid="chart-stack"]');
const svg = page.locator('[data-testid="chart-embed-area"] svg, [data-testid="chart-embed-area"] canvas');
while (Date.now() - start < 360000) {
  await page.waitForTimeout(4000);
  const elapsed = Math.round((Date.now() - start) / 1000);
  if (await stack.count() && await svg.count()) { chartUp = true; }
  if (elapsed - lastShot >= 30) {
    lastShot = elapsed;
    await page.screenshot({ path: `${OUT}_${shot}_t${elapsed}s.png` });
    console.log(`[shot ${shot}] t=${elapsed}s stack=${await stack.count()} svg=${await svg.count()}`);
    shot++;
  }
  if (chartUp) { console.log(`[chart] rendered at t=${elapsed}s; settling`); await page.waitForTimeout(3000); break; }
}
await page.screenshot({ path: `${OUT}_final.png` });
console.log(chartUp
  ? "[PASS] chart emitted + vega-embed rendered an SVG in the live AWS UI"
  : "[FAIL] no chart-stack/SVG appeared");
await browser.close();
process.exit(chartUp ? 0 : 1);
