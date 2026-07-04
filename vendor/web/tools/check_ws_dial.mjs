import { chromium } from "@playwright/test";
const URL = "https://trid3nt.vercel.app/app", CODE = "trident-demo-4db31803";
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
let dialed = null;
page.on("websocket", (ws) => { if (!dialed) dialed = ws.url(); });
await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 60000 });
try { const ci = page.getByTestId("grace2-code-input"); await ci.waitFor({ state: "visible", timeout: 25000 }); await ci.fill(CODE); await page.getByTestId("grace2-code-submit").click(); } catch {}
// give the app a few seconds to attempt a WS dial
for (let i=0;i<20 && !dialed;i++) await new Promise(r=>setTimeout(r,1500));
await browser.close();
if (!dialed) { console.log("NO_WS_DIAL_OBSERVED"); process.exit(0); }
const hasSt = /[?&]st=/.test(dialed);
const hasSid = /[?&]sid=/.test(dialed);
const red = dialed.replace(/st=[^&]+/, "st=<REDACTED>");
console.log("DIAL_URL:", red);
console.log("has_st=" + hasSt, "has_sid=" + hasSid, "=>", hasSt ? "VERCEL DEPLOYED ?st BUILD" : "OLD BUILD (no ?st) -- wait for Vercel");
