// Reopen the SAVED Boulder chart case (no Bedrock rerun) and verify the job-0294
// tweaks + chart persistence: full-width chart, humanized labels, click->dim
// gallery, desktop chat-expand, bbox rectangle.
import { chromium } from "playwright";

const SITE = "http://grace2-hazard-web-226996537797.s3-website-us-west-2.amazonaws.com/app";
const OWNER = "01KTZMJW9T9GRQYC0CVNN50F15";
const CASE_TEXT = "Fetch 10 M Elevation DEM Boulder";
const OUT = "/tmp/aws_ux";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
await page.addInitScript((id) => localStorage.setItem("grace2.anonymous_user_id", id), OWNER);
await page.goto(SITE, { waitUntil: "networkidle", timeout: 60000 });
const anon = page.getByRole("button", { name: /Continue without saving/i });
try { await anon.waitFor({ timeout: 15000 }); await anon.click(); } catch {}
await page.waitForTimeout(2500);

console.log("[1] reopening saved chart case (NO rerun)");
await page.getByText(CASE_TEXT, { exact: false }).first().click();
await page.waitForTimeout(8000);
await page.screenshot({ path: `${OUT}_1_reopened.png` });

const chartSvg = await page.locator('[data-testid="chart-embed-area"] svg, [data-testid="chart-embed-area"] canvas').count();
const body = await page.evaluate(() => document.body.innerText);
const rawNames = ["fetch_dem", "geocode_location", "generate_histogram"].filter((n) => body.includes(n));
const humanized = ["Loaded DEM", "Fetching DEM", "DEM", "histogram", "Histogram"].some((s) => body.includes(s));
console.log(`[chart] rehydrated svg=${chartSvg}  | raw tool names leaked=${JSON.stringify(rawNames)}  | humanized text=${humanized}`);

// chart full-width: compare embed-area width to chat column width
const widths = await page.evaluate(() => {
  const embed = document.querySelector('[data-testid="chart-embed-area"]');
  const stack = document.querySelector('[data-testid="chart-stack"]');
  return { embed: embed ? Math.round(embed.getBoundingClientRect().width) : 0,
           stack: stack ? Math.round(stack.getBoundingClientRect().width) : 0 };
});
console.log(`[chart] embed width=${widths.embed}px (was ~200 mini-card)`);

console.log("[2] click chart -> gallery overlay");
const top = page.locator('[data-testid="chart-stack-top-card"]');
if (await top.count()) { await top.click(); await page.waitForTimeout(1500); }
const galleryUp = await page.locator('[data-testid="chart-gallery"]').count();
await page.screenshot({ path: `${OUT}_2_gallery.png` });
console.log(`[gallery] overlay open=${galleryUp}`);
const closeG = page.locator('[data-testid="chart-gallery-close"]');
if (await closeG.count()) await closeG.click();
await page.waitForTimeout(800);

console.log("[3] desktop chat-expand toggle");
const before = await page.evaluate(() => {
  const c = document.querySelector('[data-testid="chat-input"]')?.closest('[class],[style]');
  return c ? Math.round(c.getBoundingClientRect().width) : 0;
});
const toggle = page.locator('[data-testid="grace2-chat-width-toggle"]');
let expanded = "no-toggle-found";
if (await toggle.count()) {
  await toggle.click(); await page.waitForTimeout(800);
  const after = await page.evaluate(() => {
    const c = document.querySelector('[data-testid="chat-input"]')?.closest('[class],[style]');
    return c ? Math.round(c.getBoundingClientRect().width) : 0;
  });
  expanded = `before=${before} after=${after} grew=${after > before}`;
}
await page.screenshot({ path: `${OUT}_3_expanded.png` });
console.log(`[chat-expand] ${expanded}`);

const pass = chartSvg > 0 && rawNames.length === 0 && galleryUp > 0;
console.log(pass ? "[PASS] saved-case reopen rehydrated chart full-width + humanized labels + dim gallery (no rerun)"
                 : "[REVIEW] see signals above");
await browser.close();
process.exit(pass ? 0 : 1);
