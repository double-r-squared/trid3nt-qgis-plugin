// Drive TWO concurrent isolated sessions through the live app to prove per-user
// isolation: two browser contexts, two demo logins -> two ephemeral users ->
// two independent Fargate agents. Each asks a DISTINCT question; holds both open
// so the orchestrator can observe 2 concurrent agent-session tasks in ECS.
import { chromium } from "@playwright/test";
import { mkdir } from "fs/promises";
const OUT = "/tmp/claude-1000/-home-nate-Documents-GRACE-2/fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad/shots";
const URL = "https://trid3nt.vercel.app/app", CODE = "trident-demo-4db31803";
const HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const SESSIONS = [
  { id: "A", prompt: "What is 7 times 6? Reply with just the number." },
  { id: "B", prompt: "What is the capital of France? One word." },
];
async function drive({ id, prompt }) {
  const log = (...a) => console.log(`[sess-${id}]`, ...a);
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 60000 });
  try { const ci = page.getByTestId("grace2-code-input"); await ci.waitFor({ state: "visible", timeout: 25000 }); await ci.fill(CODE); await page.getByTestId("grace2-code-submit").click(); } catch {}
  const chat = page.getByTestId("chat-input");
  const dl = Date.now() + 180000;
  while (Date.now() < dl) { if (await chat.isVisible().catch(()=>false)) break; const w=page.getByTestId("wake-overlay-rect"); if((await w.count().catch(()=>0))>0&&await w.first().isVisible().catch(()=>false))await w.first().click({timeout:4000}).catch(()=>{}); await sleep(2500); }
  await chat.waitFor({ state: "visible", timeout: 8000 }); log("connected");
  try { const mb=page.getByTestId("model-selector-button").or(page.getByTestId("chat-input-model")).first(); await mb.click({timeout:8000}); await page.getByTestId(`model-option-${HAIKU}`).click({timeout:8000}); } catch { await page.keyboard.press("Escape").catch(()=>{}); }
  await chat.click(); await chat.fill(prompt); await sleep(300); await page.keyboard.press("Enter"); log("sent:", prompt); const t0=Date.now();
  let reply="";
  while (Date.now()-t0 < 150000){ await sleep(3000); const am=page.getByTestId("agent-message"); if(await am.count().catch(()=>0)>0){ reply=((await am.last().textContent().catch(()=>""))||"").trim(); if(reply){ await sleep(4000); reply=((await am.last().textContent().catch(()=>""))||"").trim(); break; } } }
  await page.screenshot({ path: `${OUT}/two_sess_${id}.png` }).catch(()=>{});
  log("REPLY:", JSON.stringify(reply.slice(0,120)));
  log("HOLDING open 60s for ECS observation...");
  await sleep(60000);
  await browser.close();
  return { id, reply };
}
await mkdir(OUT, { recursive: true });
const res = await Promise.all(SESSIONS.map(drive));
console.log("RESULTS:", JSON.stringify(res));
