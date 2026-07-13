// Two-prompt context-window fix proof (2026-07-12), stepwise over CDP.
// Steps (run each as its own foreground command):
//   node tools/e2e_context_fix_two_prompts.mjs boot     -> start headless chromium w/ CDP :9223 (detached)
//   node tools/e2e_context_fix_two_prompts.mjs case     -> open app, create FRESH case
//   node tools/e2e_context_fix_two_prompts.mjs turn1    -> landcover prompt, confirm gate, wait terminal
//   node tools/e2e_context_fix_two_prompts.mjs turn2    -> hillshade prompt, confirm gate, wait terminal, screenshot
//   node tools/e2e_context_fix_two_prompts.mjs shutdown -> close browser
// Turn completion = "gemini loop terminal" line for THIS session in the
// agent log (the honest signal), not UI text sniffing. A turn FAILS on a
// CONTEXT_WINDOW_EXCEEDED line for this session. ASCII hyphens, no emojis.

import { chromium } from "playwright";
import fs from "fs";
import { spawn } from "child_process";

const APP_URL = "http://127.0.0.1:5173/app";
const CDP = "http://127.0.0.1:9223";
const LOG = "/home/nate/Documents/trid3nt-local/logs/agent.log";
const SHOT = "/home/nate/Documents/trid3nt-local/docs/proof/91-context-fix-two-prompts.png";
const STATE = "/tmp/claude-1000/-home-nate-Documents-GRACE-2/fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad/twoPromptState.json";
const log = (...a) => console.log(new Date().toISOString(), ...a);
const step = process.argv[2];

const readState = () => (fs.existsSync(STATE) ? JSON.parse(fs.readFileSync(STATE, "utf8")) : {});
const writeState = (s) => fs.writeFileSync(STATE, JSON.stringify(s));
const logSize = () => fs.statSync(LOG).size;
const logSince = (off) => {
  const fd = fs.openSync(LOG, "r");
  const size = fs.fstatSync(fd).size;
  const len = Math.max(size - off, 0);
  const buf = Buffer.alloc(len);
  if (len > 0) fs.readSync(fd, buf, 0, len, off);
  fs.closeSync(fd);
  return buf.toString("utf8");
};

if (step === "boot") {
  const exe = chromium.executablePath();
  const child = spawn(exe, [
    "--headless=new", "--remote-debugging-port=9223", "--no-first-run",
    "--disable-gpu", "--disable-dev-shm-usage", "--window-size=1440,900",
    "--user-data-dir=/tmp/claude-1000/-home-nate-Documents-GRACE-2/fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad/cdp-profile",
  ], { detached: true, stdio: "ignore" });
  child.unref();
  await new Promise((r) => setTimeout(r, 3000));
  log("chromium CDP up, pid", child.pid);
  writeState({ pid: child.pid });
  process.exit(0);
}

const browser = await chromium.connectOverCDP(CDP);
const ctx = browser.contexts()[0];

if (step === "shutdown") {
  const s = readState();
  await browser.close().catch(() => {});
  if (s.pid) { try { process.kill(s.pid, "SIGTERM"); } catch {} }
  log("browser closed");
  process.exit(0);
}

let page = ctx.pages().find((p) => p.url().includes("5173")) || null;

if (step === "case") {
  page = page || (await ctx.newPage());
  await page.setViewportSize({ width: 1440, height: 900 });
  const off = logSize();
  await page.goto(APP_URL, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(5000);
  const newBtn = page.locator('[data-testid="grace2-cases-new"]');
  await newBtn.waitFor({ timeout: 30000 });
  await newBtn.click();
  const nameInput = page.locator('[data-testid="aoi-name-input"]');
  await nameInput.waitFor({ timeout: 15000 });
  await nameInput.fill("context-fix-proof-2");
  await page.locator('[data-testid="aoi-name-next"]').click();
  const skip = page.locator('[data-testid="aoi-skip"]');
  await skip.waitFor({ timeout: 15000 });
  await skip.click();
  await page.waitForTimeout(4000);
  const created = logSince(off).match(/case-command create session=(\S+) case=(\S+) title='context-fix-proof-2'/);
  if (!created) { log("FAIL: case-create line not found in agent log"); process.exit(1); }
  writeState({ ...readState(), session: created[1], caseId: created[2] });
  log("fresh case created:", created[2], "session:", created[1]);
  process.exit(0);
}

const GATE_SEL = '[data-testid="payload-warning-inline"], [data-testid="resolution-picker-card"], [data-variant="warning"]';
async function clickGateIfAny(p) {
  const proceed = p.getByRole("button", { name: /proceed|confirm|continue|run anyway/i }).first();
  if ((await p.locator(GATE_SEL).count()) > 0 && (await proceed.count()) > 0 && (await proceed.isVisible().catch(() => false))) {
    await proceed.click().catch(() => {});
    log("gate confirmed");
    return true;
  }
  return false;
}

async function runTurn(p, prompt, timeoutMs) {
  const off = logSize();
  const input = p.locator("textarea, input[placeholder*='Ask'], input[placeholder*='ask']").first();
  await input.waitFor({ timeout: 30000 });
  await input.fill(prompt);
  await input.press("Enter");
  log("prompt sent:", JSON.stringify(prompt));
  const t0 = Date.now();
  // confirm the send actually reached the server and capture ITS session id
  // (a page reload mints a fresh WS session, so the case-step id can stale)
  let session = null;
  const needle = "text='" + prompt.replace(/'/g, "") + "'";
  while (Date.now() - t0 < 60000) {
    const m = logSince(off).match(new RegExp("user-message session=(\\S+) research_mode=\\S+ " + needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    if (m) { session = m[1]; break; }
    await p.waitForTimeout(1500);
  }
  if (!session) { log("FAIL: user-message never reached the server"); return false; }
  log("turn session:", session);
  let gates = 0;
  while (Date.now() - t0 < timeoutMs) {
    if (await clickGateIfAny(p)) { gates += 1; await p.waitForTimeout(2000); continue; }
    const tail = logSince(off);
    if (tail.includes("CONTEXT_WINDOW") && tail.includes(session)) {
      log("TURN FAILED: CONTEXT_WINDOW abort in agent log");
      return false;
    }
    if (tail.includes("gemini loop terminal session=" + session)) {
      log("turn terminal at T+" + Math.round((Date.now() - t0) / 1000) + "s gates=" + gates);
      return true;
    }
    await p.waitForTimeout(2500);
  }
  log("TURN TIMEOUT after", Math.round(timeoutMs / 1000), "s");
  return false;
}

if (step === "turn1") {
  if (!page) { log("FAIL: no app page"); process.exit(1); }
  const ok = await runTurn(page, "show me landcover over washington state", 480000);
  process.exit(ok ? 0 : 1);
}

if (step === "turn2") {
  if (!page) { log("FAIL: no app page"); process.exit(1); }
  const ok = await runTurn(page, "now compute a hillshade of the terrain here", 480000);
  await page.waitForTimeout(8000);
  await page.screenshot({ path: SHOT, fullPage: false });
  log("screenshot ->", SHOT);
  process.exit(ok ? 0 : 1);
}

log("unknown step:", step);
process.exit(2);
