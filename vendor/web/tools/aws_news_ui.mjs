// LIVE news-event ingest via the AWS UI (single Bedrock run, cost-conscious).
// Routes to run_model_news_event_ingest: web_fetch egress from EC2 +
// aggregate_claims_across_sources + geocode + (often) fetch_nws_event vector
// layer. Render signals: TiTiler tiles (if any raster) AND/OR a vector alert
// layer in the LayerPanel AND/OR claim/source narration in chat.
import { chromium } from "playwright";

const SITE = "http://grace2-hazard-web-226996537797.s3-website-us-west-2.amazonaws.com/app";
const PROMPT = "Ingest the current Texas flood event using NWS active alerts and NOAA storm events as the sources, then report the aggregated findings and map the affected area. Use the news event ingest workflow.";
const OUT = "/tmp/aws_news_ui";
const TILE_HOST = "54.185.114.233:8080";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
let tiles = 0;
const tileUrls = [];
page.on("response", (r) => {
  if (r.url().includes(TILE_HOST) && r.url().includes("/cog/tiles/") && r.status() === 200) { tiles++; tileUrls.push(r.url()); }
});

await page.goto(SITE, { waitUntil: "networkidle", timeout: 60000 });
const anon = page.getByRole("button", { name: /Continue without saving/i });
try { await anon.waitFor({ timeout: 15000 }); await anon.click(); } catch {}
await page.waitForTimeout(2500);

console.log("[1] sending news-ingest prompt");
const input = page.locator('[data-testid="chat-input"]');
await input.waitFor({ timeout: 20000 });
await input.fill(PROMPT);
await input.press("Enter");

const start = Date.now();
let proceeded = false, shot = 1, lastShot = 0, done = false;
const proceedBtn = page.locator('[data-testid="payload-warning-button-proceed"]');
while (Date.now() - start < 360000) {
  await page.waitForTimeout(4000);
  const t = Math.round((Date.now() - start) / 1000);
  if (await proceedBtn.count()) { try { await proceedBtn.first().click(); proceeded = true; console.log(`[gate] clicked Proceed at t=${t}s`); } catch {} }
  if (t - lastShot >= 30) {
    lastShot = t;
    await page.screenshot({ path: `${OUT}_${shot}_t${t}s.png` });
    const layers = await page.locator('[data-testid="layer-row"], [class*="layer-row"]').count().catch(() => 0);
    console.log(`[shot ${shot}] t=${t}s tiles=${tiles} layerRows=${layers}`);
    shot++;
  }
  const body = await page.evaluate(() => document.body.innerText);
  // The composer step is "Ingesting the event…" → "Event ingested" on success.
  // Wait until that step resolves either way (or the agent clearly stops).
  const ingestResolved = /Event ingested/i.test(body);
  const ingestFailedCard = /Ingesting the event/i.test(body) && /failed/i.test(body);
  const narration = /aggregat|claim|derived|provenance|review|STOP/i.test(body);
  if ((ingestResolved || (narration && t > 60)) && t > 40) {
    done = true; console.log(`[done] composer resolved at t=${t}s (ingested=${ingestResolved}); settling`); await page.waitForTimeout(7000); break;
  }
}
await page.screenshot({ path: `${OUT}_final.png`, fullPage: false });
const finalBody = await page.evaluate(() => document.body.innerText);
console.log(`[result] proceeded=${proceeded} tiles=${tiles} done=${done}`);
console.log(`[tileUrls] ${tileUrls.slice(0, 3).join("  ")}`);
console.log("[narration-tail]\n" + finalBody.split("\n").filter(Boolean).slice(-25).join("\n"));
await browser.close();
process.exit(done ? 0 : 1);
