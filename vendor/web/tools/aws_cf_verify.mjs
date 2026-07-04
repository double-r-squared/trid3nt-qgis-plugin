// Verify the HTTPS CloudFront cutover (ZERO Bedrock): the real-browser WSS
// handshake through CloudFront completes (case list loads + saved case opens
// from DynamoDB), 0 page errors, no mixed-content. Counts any CF tile loads.
import { chromium } from "playwright";

const SITE = "https://d125yfbyjrpbre.cloudfront.net/app";
const CF = "d125yfbyjrpbre.cloudfront.net";
const OWNER = "01KTZMJW9T9GRQYC0CVNN50F15";
const CASE_TEXT = "Fetch 10 M Elevation DEM Boulder";
const OUT = "/tmp/aws_cf";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const errors = [], mixed = [];
let wsOpened = false, cfTiles = 0;
page.on("pageerror", (e) => errors.push(String(e)));
page.on("console", (m) => { const t = m.text(); if (/mixed content|insecure|blocked:mixed/i.test(t)) mixed.push(t); });
page.on("websocket", (ws) => { if (ws.url().includes(CF)) wsOpened = true; });
page.on("response", (r) => { if (r.url().includes(CF) && r.url().includes("/cog/") && r.status() === 200) cfTiles++; });

await page.addInitScript((id) => localStorage.setItem("grace2.anonymous_user_id", id), OWNER);
await page.goto(SITE, { waitUntil: "networkidle", timeout: 60000 });
const anon = page.getByRole("button", { name: /Continue without saving/i });
let gateSeen = false;
try { await anon.waitFor({ timeout: 15000 }); gateSeen = true; await anon.click(); } catch {}
await page.waitForTimeout(2500);
const booted = await page.locator('[data-testid="chat-input"]').count().then((c) => c > 0).catch(() => false);
await page.screenshot({ path: `${OUT}_1_booted.png` });

// open saved case (proves WSS handshake completed: the case list arrives over the socket)
let caseOpened = false;
const caseLink = page.getByText(CASE_TEXT, { exact: false }).first();
for (let i = 0; i < 18; i++) { if (await caseLink.count()) break; await page.waitForTimeout(2000); }
try { await caseLink.click({ timeout: 8000 }); caseOpened = true; } catch {}
await page.waitForTimeout(10000);
await page.screenshot({ path: `${OUT}_2_case.png` });
const body = await page.evaluate(() => document.body.innerText);

console.log(`[scheme] site=https via CloudFront`);
console.log(`[boot] gate=${gateSeen} chatInput=${booted}`);
console.log(`[wss] websocketToCloudFront=${wsOpened}  caseListLoaded+opened=${caseOpened}  bodyHasDEM=${/DEM|Boulder|elevation/i.test(body)}`);
console.log(`[tiles] cfTiles=${cfTiles}`);
console.log(`[errors] pageerrors=${errors.length} mixedContent=${mixed.length}${errors.length ? " :: " + errors.slice(0,2).join(" | ") : ""}`);
const pass = booted && wsOpened && caseOpened && errors.length === 0 && mixed.length === 0;
console.log(pass ? "[PASS] HTTPS+WSS cutover live: boots, WSS connects through CloudFront, saved case opens from DynamoDB, no mixed content"
               : "[REVIEW] see signals");
await browser.close();
process.exit(pass ? 0 : 1);
