// Make-it-work verification: drive a Fort Myers 100-yr flood end-to-end, auto-confirm
// the gates, wait for the flood-depth layer to render. Screenshots the result.
import { chromium } from "@playwright/test";
import { mkdir } from "fs/promises";
const OUT = "/tmp/claude-1000/-home-nate-Documents-GRACE-2/fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad/shots";
const URL = "https://trid3nt.vercel.app/app", CODE = "trident-demo-4db31803";
const PROMPT = "Run a 100-year flood for downtown Fort Myers, Florida.";
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const log = (...a) => console.log("[flood-verify]", ...a);
await mkdir(OUT, { recursive: true });
const browser = await chromium.launch({ headless: true });
const page = await (await browser.newContext({ viewport: { width: 1500, height: 950 } })).newPage();
page.on("console", m => { const t = m.text(); if (/error|1005|1006|reconnect/i.test(t)) log("console:", t.slice(0,120)); });
await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 60000 });
try { const ci = page.getByTestId("grace2-code-input"); await ci.waitFor({ state:"visible", timeout:25000 }); await ci.fill(CODE); await page.getByTestId("grace2-code-submit").click(); } catch {}
const chat = page.getByTestId("chat-input");
const dl = Date.now()+120000;
while (Date.now()<dl){ if(await chat.isVisible().catch(()=>false))break; const w=page.getByTestId("wake-overlay-rect"); if((await w.count().catch(()=>0))>0&&await w.first().isVisible().catch(()=>false))await w.first().click({timeout:4000}).catch(()=>{}); await sleep(2500); }
await chat.waitFor({ state:"visible", timeout:8000 }); log("connected");
await chat.click(); await chat.fill(PROMPT); await sleep(300); await page.keyboard.press("Enter"); log("prompt sent:", PROMPT);
const t0 = Date.now();
let sawFlood=false, dispatched=false;
while (Date.now()-t0 < 360000){
  await sleep(8000);
  // auto-confirm any gate: resolution picker + generic confirm/proceed/run buttons
  for (const tid of ["resolution-picker-confirm"]) { try { const c=page.getByTestId(tid); if(await c.count()>0&&await c.first().isVisible().catch(()=>false)){ await c.first().click({timeout:4000}); log("confirmed gate:",tid);} } catch {} }
  for (const name of [/confirm/i,/proceed/i,/run (the )?sim/i,/start the run/i,/yes/i]) { try { const b=page.getByRole("button",{name}).first(); if(await b.count()>0&&await b.isVisible().catch(()=>false)){ await b.click({timeout:3000}); log("clicked confirm button:",name.source);} } catch {} }
  // detect dispatch + flood layer
  const txt = (await page.locator("body").innerText().catch(()=>"")||"").toLowerCase();
  if(!dispatched && /(dispatch|batch|simulation|solving|running the (sfincs|sim))/i.test(txt)){ dispatched=true; log(`+${Math.round((Date.now()-t0)/1000)}s sim dispatched (UI mentions it)`); }
  if(/flood|depth|inundation/i.test(txt) && await page.getByTestId("grace2-layer-legend").count().catch(()=>0)>0){ sawFlood=true; }
  await page.screenshot({ path: `${OUT}/flood_t${Math.round((Date.now()-t0)/1000)}.png` }).catch(()=>{});
  log(`+${Math.round((Date.now()-t0)/1000)}s dispatched=${dispatched} floodLayer=${sawFlood}`);
  if(sawFlood) break;
}
await page.screenshot({ path: `${OUT}/flood_FINAL.png` }).catch(()=>{});
log("RESULT dispatched="+dispatched+" floodRendered="+sawFlood);
await browser.close();
