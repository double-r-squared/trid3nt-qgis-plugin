import { chromium } from "@playwright/test";
const URL = "https://trid3nt.vercel.app/app", CODE = "trident-demo-4db31803";
const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext();
const page = await ctx.newPage();
const dials = [];
page.on("websocket", (ws) => dials.push({ t: Date.now(), url: ws.url() }));
await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 60000 });
try { const ci = page.getByTestId("grace2-code-input"); await ci.waitFor({ state: "visible", timeout: 25000 }); await ci.fill(CODE); await page.getByTestId("grace2-code-submit").click(); } catch {}
// wait up to 40s, collecting all dials (incl. post-auth reconnects)
for (let i=0;i<40;i++){ await new Promise(r=>setTimeout(r,1000)); }
await browser.close();
console.log("total dials:", dials.length);
let anySt=false;
for (const d of dials){ const has=/[?&]st=/.test(d.url); anySt=anySt||has; console.log((has?"[ST] ":"[   ] ")+d.url.replace(/st=[^&]+/,"st=<REDACTED>").slice(0,140)); }
console.log("ANY_ST=" + anySt);
