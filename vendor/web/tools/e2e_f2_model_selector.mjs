// E2E: F2 live-verify -- in-chat model selector hot-swap on the TRID3NT local
// build (fix committed 8deff81: GET /api/local-models + Settings-adjacent
// model selector). ASCII hyphens only; no emojis.
//
// Checks:
//   C1  GET /api/local-models lists the real installed Ollama models
//   C2  the header model-selector-button opens a popover listing those models
//   C3  selecting qwen3.5-lowvram:9b-16k persists (button shows new selection)
//   C4  a trivial prompt sent after the switch gets a clean reply (no tool use)
//   C5  server-side proof the SWITCHED model actually served the turn
//       (Ollama /api/ps shows it loaded/recently-used; no "not available"
//       model-selector rejection warning in agent.log for the window)
//   C6  switching BACK to qwen3:8b-16k and repeating C4/C5 also takes effect
//
// Run from web/: node tools/e2e_f2_model_selector.mjs

import { chromium } from "playwright";
import { readFile } from "fs/promises";

const AGENT_HTTP = "http://127.0.0.1:8766";
const APP_URL = "http://127.0.0.1:5173/app";
const OLLAMA_URL = "http://127.0.0.1:11434";
const AGENT_LOG = "/home/nate/Documents/trid3nt-local/logs/agent.log";
const OUT = "/home/nate/Documents/trid3nt-local/docs/proof";

const MODEL_A = "qwen3:8b-16k";           // box default
const MODEL_B = "qwen3.5-lowvram:9b-16k"; // switch target
const PROMPT = "Say READY and nothing else. Do not use any tools.";

const results = [];
function pass(id, evidence) { results.push({ id, ok: true, evidence }); console.log("  PASS", id, "-", evidence); }
function fail(id, evidence) { results.push({ id, ok: false, evidence }); console.log("  FAIL", id, "-", evidence); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function ollamaPs() {
  try {
    const r = await fetch(`${OLLAMA_URL}/api/ps`);
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

// Log lines are stamped in the host's LOCAL time zone (matches `date` on the
// box), format "YYYY-MM-DD HH:MM:SS,mmm LEVEL logger msg". Parse via the
// explicit Date(y,m,d,H,M,S,ms) constructor -- guaranteed local-time
// interpretation, unlike Date.parse() of an ambiguous string.
function parseLogTs(line) {
  const m = line.match(/^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2}),(\d{3})/);
  if (!m) return null;
  const [, y, mo, d, h, mi, s, ms] = m;
  return new Date(+y, +mo - 1, +d, +h, +mi, +s, +ms).getTime();
}

async function logTail(sinceMs, matchRe) {
  // Read the agent log and return lines timestamped >= sinceMs that match matchRe.
  const raw = await readFile(AGENT_LOG, "utf8").catch(() => "");
  const lines = raw.split("\n");
  const out = [];
  for (const line of lines) {
    const t = parseLogTs(line);
    if (t === null || t < sinceMs) continue;
    if (matchRe.test(line)) out.push(line);
  }
  return out;
}

async function main() {
  // C1 -- /api/local-models
  let localModels = null;
  try {
    const r = await fetch(`${AGENT_HTTP}/api/local-models`);
    localModels = await r.json();
    const ids = (localModels.models || []).map((m) => m.id);
    if (ids.includes(MODEL_A) && ids.includes(MODEL_B)) {
      pass("C1_LOCAL_MODELS_API", `models=${JSON.stringify(ids)} default=${localModels.default}`);
    } else {
      fail("C1_LOCAL_MODELS_API", `missing expected ids in ${JSON.stringify(ids)}`);
    }
  } catch (e) {
    fail("C1_LOCAL_MODELS_API", "fetch failed: " + e.message);
    printAndExit();
    return;
  }

  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  const page = await ctx.newPage();
  page.on("console", (m) => { if (m.type() === "error") console.error("[console.error]", m.text().slice(0, 200)); });

  await page.goto(APP_URL, { waitUntil: "domcontentloaded" });
  const chat = page.getByTestId("chat-input");
  await chat.waitFor({ timeout: 20000 }).catch(() => {});
  if (!(await chat.isVisible().catch(() => false))) {
    fail("APP_LOADED", "chat-input not visible after 20s");
    await browser.close();
    printAndExit();
    return;
  }
  pass("APP_LOADED", "chat-input visible");

  // C2 -- open the model selector, assert popover lists the API models
  const modelBtn = page.getByTestId("model-selector-button");
  await modelBtn.waitFor({ timeout: 10000 }).catch(() => {});
  await modelBtn.click({ timeout: 8000 }).catch((e) => fail("C2_POPOVER_OPEN", "click failed: " + e.message));
  const popover = page.getByTestId("model-popover");
  const popoverVisible = await popover.isVisible({ timeout: 5000 }).catch(() => false);
  if (popoverVisible) {
    const optA = await page.getByTestId(`model-option-${MODEL_A}`).count();
    const optB = await page.getByTestId(`model-option-${MODEL_B}`).count();
    if (optA > 0 && optB > 0) pass("C2_POPOVER_OPEN", `popover lists model-option-${MODEL_A} and model-option-${MODEL_B}`);
    else fail("C2_POPOVER_OPEN", `popover missing options: optA=${optA} optB=${optB}`);
  } else {
    fail("C2_POPOVER_OPEN", "model-popover not visible after click");
  }

  await page.screenshot({ path: `${OUT}/58-f2-model-selector.png` }).catch(() => {});
  console.log("screenshot: " + OUT + "/58-f2-model-selector.png");

  // ---- helper: select a model, send the trivial prompt, verify server-side ----
  async function switchAndVerify(targetId, label) {
    // (re)open popover if closed
    if (!(await page.getByTestId("model-popover").isVisible().catch(() => false))) {
      await modelBtn.click({ timeout: 8000 }).catch(() => {});
    }
    await page.getByTestId(`model-option-${targetId}`).click({ timeout: 8000 });
    await sleep(300);
    const activeId = await modelBtn.getAttribute("data-model-id").catch(() => null);
    if (activeId === targetId) pass(`C3_SELECT_${label}`, `button data-model-id=${activeId}`);
    else fail(`C3_SELECT_${label}`, `expected ${targetId} got ${activeId}`);

    const beforePs = await ollamaPs();
    const t0 = new Date();
    await chat.click();
    await chat.fill(PROMPT);
    await sleep(200);
    await page.keyboard.press("Enter");
    console.log(`[${label}] prompt sent at`, t0.toISOString());

    // AgentMessage.tsx wraps the answer in data-testid="agent-message"
    // data-done="true|false"; while streaming it may ALSO contain an
    // agent-thinking-block with an inline <style> tag, so raw .textContent()
    // picks up CSS keyframes text before any real answer exists. Use
    // data-done="true" as the completion signal and .innerText() (which
    // correctly skips the boxless <style> element) to read the answer.
    let replyText = "";
    const deadline = Date.now() + 180000; // 3 min budget (cold Ollama load tolerated)
    while (Date.now() < deadline) {
      await sleep(3000);
      const doneMsg = page.locator('[data-testid="agent-message"][data-done="true"]').last();
      if (await doneMsg.count().catch(() => 0) > 0) {
        const t = ((await doneMsg.innerText().catch(() => "")) || "").trim();
        if (t.length > 0) { replyText = t; break; }
      }
    }
    if (replyText.length > 0 && !/LLM_UNAVAILABLE|unavailable/i.test(replyText)) {
      pass(`C4_REPLY_${label}`, `reply="${replyText.slice(0, 80)}"`);
    } else {
      fail(`C4_REPLY_${label}`, `no clean reply within budget (got "${replyText.slice(0, 80)}")`);
    }

    // C5 -- server-side proof via Ollama /api/ps (ground truth: which model
    // actually executed) + absence of a model-selector rejection warning in
    // agent.log for this window (which would mean the override was dropped).
    // Poll: /api/ps can lag a beat behind the streamed reply finishing
    // (observed: a single 1s-later check can race Ollama's own bookkeeping).
    let afterPs = null;
    let loadedNames = [];
    let targetLoaded = false;
    for (let i = 0; i < 6 && !targetLoaded; i++) {
      await sleep(2000);
      afterPs = await ollamaPs();
      loadedNames = (afterPs?.models || []).map((m) => m.name || m.model);
      targetLoaded = loadedNames.some((n) => n === targetId || n?.startsWith(targetId));
    }
    const rejectLines = await logTail(t0.getTime(), new RegExp(`model selector:.*not available.*requested='?${targetId.replace(/[.:]/g, "\\$&")}'?`, "i"));
    if (targetLoaded && rejectLines.length === 0) {
      pass(`C5_SERVER_MODEL_${label}`, `ollama /api/ps loaded=${JSON.stringify(loadedNames)} no-reject-warning`);
    } else {
      fail(`C5_SERVER_MODEL_${label}`, `loaded=${JSON.stringify(loadedNames)} target=${targetId} rejectLines=${JSON.stringify(rejectLines)}`);
    }
    return { beforePs, afterPs, replyText };
  }

  await switchAndVerify(MODEL_B, "SWITCH_TO_B");
  await switchAndVerify(MODEL_A, "SWITCH_BACK_TO_A");

  await browser.close();
  printAndExit();
}

function printAndExit() {
  console.log("\n=== E2E VERDICT (F2 model selector) ===");
  let allPass = true;
  for (const r of results) {
    const tag = r.ok ? "PASS" : "FAIL";
    console.log(`  ${tag}  ${r.id}: ${r.evidence}`);
    if (!r.ok) allPass = false;
  }
  console.log(allPass ? "\nOVERALL: PASS" : "\nOVERALL: FAIL");
  process.exit(allPass ? 0 : 1);
}

main().catch((e) => { console.error("FATAL", e); process.exit(2); });
