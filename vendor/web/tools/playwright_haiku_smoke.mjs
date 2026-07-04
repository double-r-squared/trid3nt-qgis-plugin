#!/usr/bin/env node
// Post-isolation-cutover smoke: log in, pick Haiku, ask a simple question,
// confirm a clean reply through the broker -> isolated agent. Watches for
// connecting-flicker + WS console errors (the artifacts NATE cares about).
import { chromium } from "@playwright/test";
import { mkdir } from "fs/promises";

const OUT = process.env.OUT_DIR ?? "/tmp/claude-1000/-home-nate-Documents-GRACE-2/fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad/shots";
const URL = process.env.URL ?? "https://trid3nt.vercel.app/app";
const CODE = process.env.CODE ?? "trident-demo-4db31803";
const HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
const PROMPT = process.env.PROMPT ?? "What is 2 plus 2? Reply with just the number.";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log("[haiku-smoke]", ...a);

async function main() {
  await mkdir(OUT, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const wsErrs = [];
  const connStates = [];
  page.on("console", (m) => { const t = m.text(); if (/1005|1006|ws.?close|reconnect|LLM_UNAVAILABLE|AccessDenied|error/i.test(t)) wsErrs.push(t.slice(0,180)); });

  log("goto", URL);
  await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 60000 });

  const codeInput = page.getByTestId("grace2-code-input");
  try { await codeInput.waitFor({ state: "visible", timeout: 30000 }); await codeInput.fill(CODE); await page.getByTestId("grace2-code-submit").click(); log("code submitted"); }
  catch { log("no code gate"); }

  // wait for chat input; cold-start of an isolated Fargate agent can take ~60-90s.
  const chat = page.getByTestId("chat-input");
  const deadline = Date.now() + 200000;
  let flickers = 0, lastConn = "";
  while (Date.now() < deadline) {
    if (await chat.isVisible().catch(() => false)) break;
    const wake = page.getByTestId("wake-overlay-rect");
    if ((await wake.count().catch(()=>0)) > 0 && await wake.first().isVisible().catch(()=>false)) await wake.first().click({timeout:4000}).catch(()=>{});
    await sleep(2500);
  }
  await chat.waitFor({ state: "visible", timeout: 8000 });
  log("chat input visible");
  await page.screenshot({ path: `${OUT}/haiku_00_authed.png` }).catch(()=>{});

  // sample connection-status for ~12s to detect flicker (rapid connecting<->connected toggles)
  const cs = page.getByTestId("connection-status");
  for (let i=0;i<24;i++){ const t=(await cs.first().textContent().catch(()=>"")||"").trim().toLowerCase(); if(t&&t!==lastConn){connStates.push(t); if(lastConn&&t!==lastConn)flickers++; lastConn=t;} await sleep(500); }
  log("conn states seen:", JSON.stringify(connStates), "transitions:", flickers);

  // select Haiku
  try { const mb = page.getByTestId("model-selector-button").or(page.getByTestId("chat-input-model")).first(); await mb.click({timeout:8000}); await page.getByTestId(`model-option-${HAIKU}`).click({timeout:8000}); log("Haiku selected"); }
  catch(e){ log("model select skipped:", e.message.slice(0,60)); await page.keyboard.press("Escape").catch(()=>{}); }

  // send the prompt
  await chat.click(); await chat.fill(PROMPT); await sleep(300); await page.keyboard.press("Enter");
  log("prompt sent:", PROMPT); const t0 = Date.now();

  // wait for an agent-message reply (cold agent: allow up to ~150s)
  let replyText = "";
  while (Date.now() - t0 < 150000) {
    await sleep(3000);
    const am = page.getByTestId("agent-message");
    const n = await am.count().catch(()=>0);
    if (n > 0) { replyText = ((await am.last().textContent().catch(()=>""))||"").trim(); if (replyText && replyText.length > 0) { /* let it finish streaming */ await sleep(4000); replyText = ((await am.last().textContent().catch(()=>""))||"").trim(); break; } }
  }
  log(`+${Math.round((Date.now()-t0)/1000)}s reply len=${replyText.length}`);
  await page.screenshot({ path: `${OUT}/haiku_01_reply.png` }).catch(()=>{});

  log("REPLY_TEXT_START"); console.log(replyText.slice(0, 500)); log("REPLY_TEXT_END");
  log("WS_ERRORS:", wsErrs.length ? JSON.stringify(wsErrs.slice(0,8)) : "none");
  const ok = replyText.length > 0 && !/LLM_UNAVAILABLE|AccessDenied|unavailable|failed/i.test(replyText);
  log("VERDICT:", ok ? "PASS (got a clean reply)" : "FAIL", "| flicker-transitions:", flickers);
  await browser.close();
  console.log(`RESULT pass=${ok} replyLen=${replyText.length} flickers=${flickers} wsErrs=${wsErrs.length}`);
  process.exit(ok ? 0 : 1);
}
main().catch((e)=>{ console.error("FATAL", e); process.exit(2); });
