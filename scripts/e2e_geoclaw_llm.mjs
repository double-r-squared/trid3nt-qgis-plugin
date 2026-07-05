/**
 * e2e_geoclaw_llm.mjs -- LLM-driven GeoClaw tsunami inundation on qwen3:8b-16k
 *
 * Scenario: send a small tsunami inundation prompt for Crescent City CA, then
 * wait up to 30 min for the full LLM-driven chain to complete.
 *
 * Screenshots -> docs/proof/:
 *   18-geoclaw-local.png   (pipeline/tool cards visible)
 *   19-geoclaw-layer.png   (depth layer on the map, or -failure.png)
 *
 * Run:
 *   cd /home/nate/Documents/trid3nt-local
 *   PLAYWRIGHT_BROWSERS_PATH=~/.cache/ms-playwright \
 *     node scripts/e2e_geoclaw_llm.mjs
 */

import { createRequire } from "module";
import { fileURLToPath } from "url";
import path from "path";
import fs from "fs";
import { execSync } from "child_process";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const VENDOR_WEB = path.resolve(__dirname, "../vendor/web");
const GRACE2_WEB = "/home/nate/Documents/GRACE-2/web";

let playwright;
const candidates = [
  path.join(VENDOR_WEB, "node_modules/playwright"),
  path.join(GRACE2_WEB, "node_modules/playwright"),
  path.join(VENDOR_WEB, "node_modules/@playwright/test"),
  path.join(GRACE2_WEB, "node_modules/@playwright/test"),
];
for (const p of candidates) {
  if (fs.existsSync(p)) {
    const req = createRequire(import.meta.url);
    playwright = req(p);
    console.log("[e2e-geoclaw] using playwright from:", p);
    break;
  }
}
if (!playwright) {
  console.error("[e2e-geoclaw] playwright not found in any of:", candidates);
  process.exit(1);
}

const PROOF_DIR = path.resolve(__dirname, "../docs/proof");
fs.mkdirSync(PROOF_DIR, { recursive: true });

const APP_URL = "http://127.0.0.1:5173";

const GEOCLAW_PROMPT =
  "Run a small tsunami inundation simulation for Crescent City, California, " +
  "around lat 41.756 lon -124.20, about a 5km coastal box. " +
  "Call run_geoclaw_inundation with bbox=[-124.24, 41.73, -124.16, 41.78], " +
  "scenario=\"tsunami\", sim_duration_s=1800, amr_levels=2, output_frames=6. " +
  "Use the coarsest default resolution. Do NOT ask for confirmation.";

const NUDGE_TEXT =
  "Call run_geoclaw_inundation now with exactly these args: " +
  "bbox=[-124.24, 41.73, -124.16, 41.78], scenario=\"tsunami\", " +
  "sim_duration_s=1800, amr_levels=2, output_frames=6. Proceed immediately.";

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

/** Snapshot of docker ps -a via sg docker. */
function dockerPsAll() {
  try {
    return execSync(
      `sg docker -c 'docker ps -a --format "{{.Names}}\t{{.Status}}\t{{.Image}}" | head -20'`,
      { encoding: "utf8", timeout: 10000 }
    ).trim();
  } catch (e) {
    return "(docker ps failed: " + (e.message || "") + ")";
  }
}

async function sendChatMessage(page, text) {
  const selectors = [
    '[data-testid="chat-input"] textarea',
    '[data-testid="chat-input-wrapper"] textarea',
    "textarea",
    'input[type="text"]',
  ];
  let input = null;
  for (const sel of selectors) {
    const el = page.locator(sel).first();
    if (await el.isVisible().catch(() => false)) {
      input = el;
      break;
    }
  }
  if (!input) {
    console.log("[e2e-geoclaw] WARN: no chat input found");
    return false;
  }
  await input.click();
  await input.fill(text);
  const actionBtn = page.locator('[data-testid="chat-input-action"]').first();
  if (await actionBtn.isVisible().catch(() => false)) {
    await actionBtn.click();
  } else {
    await input.press("Enter");
  }
  console.log("[e2e-geoclaw] sent:", text.slice(0, 80) + (text.length > 80 ? "..." : ""));
  return true;
}

async function clickConfirmations(page) {
  const affirmSelectors = [
    '[data-testid="resolution-picker-confirm"]',
    '[data-testid="sandbox-card-proceed"]',
    'button:has-text("Confirm")',
    'button:has-text("Proceed")',
    'button:has-text("Yes")',
    'button:has-text("Run")',
    'button:has-text("OK")',
    'button:has-text("Coarsest")',
  ];
  let clicked = 0;
  for (const sel of affirmSelectors) {
    const els = page.locator(sel);
    const count = await els.count().catch(() => 0);
    for (let i = 0; i < count; i++) {
      const el = els.nth(i);
      if (await el.isVisible().catch(() => false)) {
        console.log(`[e2e-geoclaw] clicking confirmation: ${sel}`);
        await el.click().catch((e) => console.log(`[e2e-geoclaw] click failed: ${e.message}`));
        await sleep(1000);
        clicked++;
      }
    }
  }
  return clicked;
}

async function detectNudgeNeeded(page) {
  const agentMsgs = page.locator('[data-testid="agent-message"]');
  const count = await agentMsgs.count().catch(() => 0);
  if (count === 0) return false;
  const lastMsg = agentMsgs.nth(count - 1);
  const text = await lastMsg.innerText().catch(() => "");
  return /provide.{0,40}more|please.{0,40}(provide|specify|give)|need.{0,30}(bbox|location)|require.{0,30}(bbox|location)|clarif|cannot.*proceed|missing.*param|which.*(city|area|location)/i.test(text);
}

async function main() {
  console.log("[e2e-geoclaw] === LLM-driven GeoClaw tsunami on qwen3:8b-16k ===");
  const { chromium } = playwright;
  const browser = await chromium.launch({
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });

  // Pre-seed anonymous auth in localStorage so the auth gate is skipped
  await context.addInitScript(() => {
    window.localStorage.setItem("grace2_anonymous_accepted", "true");
    window.localStorage.setItem("grace2.anonymous_user_id", "e2e-geoclaw-llm");
  });

  const page = await context.newPage();
  page.on("console", (msg) => {
    if (msg.type() === "error") {
      console.log("[browser-error]", msg.text().slice(0, 200));
    }
  });

  console.log("[e2e-geoclaw] navigating to", APP_URL + "/app");
  await page.goto(APP_URL + "/app", { waitUntil: "domcontentloaded", timeout: 30000 });
  await sleep(5000);

  // Handle auth gate if still shown
  const continueBtn = page.locator(
    'button:has-text("Continue"), button:has-text("anonymous"), button:has-text("Skip"), ' +
    'button:has-text("Use anonymously"), button:has-text("continue without"), ' +
    'button:has-text("Continue without saving")'
  );
  if (await continueBtn.count().then((c) => c > 0).catch(() => false)) {
    console.log("[e2e-geoclaw] clicking pass-through auth button");
    await continueBtn.first().click();
    await sleep(2000);
  }

  // Wait for chat input
  console.log("[e2e-geoclaw] waiting for chat input...");
  let chatReady = false;
  for (let i = 0; i < 15; i++) {
    const el = page.locator('[data-testid="chat-input"] textarea, [data-testid="chat-input-wrapper"] textarea, textarea').first();
    if (await el.isVisible().catch(() => false)) {
      chatReady = true;
      break;
    }
    await sleep(2000);
  }
  if (!chatReady) {
    console.log("[e2e-geoclaw] WARN: chat input not visible, attempting anyway");
  } else {
    console.log("[e2e-geoclaw] chat input ready");
  }

  // Take initial screenshot
  await page.screenshot({ path: path.join(PROOF_DIR, "18-geoclaw-local.png") });
  console.log("[e2e-geoclaw] initial screenshot: 18-geoclaw-local.png");

  console.log("[e2e-geoclaw] sending GeoClaw tsunami prompt...");
  await sendChatMessage(page, GEOCLAW_PROMPT);
  const promptSentAt = Date.now();

  const MAX_WAIT_MS = 30 * 60 * 1000; // 30 minutes
  const POLL_MS = 15000;
  let nudgesUsed = 0;
  const MAX_NUDGES = 2;
  let pipelineCardsSeen = false;
  let geoclawContainerSeen = false;
  let layerFound = false;
  let screenshot18Updated = false;
  let screenshot19Done = false;
  let outcome = "PENDING";

  console.log("[e2e-geoclaw] entering polling loop (max 30 min)...");
  const deadline = Date.now() + MAX_WAIT_MS;

  while (Date.now() < deadline) {
    await sleep(POLL_MS);
    const elapsed = Math.round((Date.now() - promptSentAt) / 1000);

    // Check pipeline cards
    if (!pipelineCardsSeen) {
      for (const sel of ['[data-testid="pipeline-card-stack"]', '[data-testid="grace2-sheet-tool-strip"]', '[data-testid="resolution-picker-card"]']) {
        if (await page.locator(sel).count().catch(() => 0) > 0) {
          pipelineCardsSeen = true;
          console.log(`[e2e-geoclaw] pipeline cards seen via ${sel} (+${elapsed}s)`);
          break;
        }
      }
    }

    // Confirmations
    await clickConfirmations(page);

    // Docker evidence
    if (!geoclawContainerSeen) {
      const ps = dockerPsAll();
      if (/geoclaw/i.test(ps)) {
        geoclawContainerSeen = true;
        console.log(`[e2e-geoclaw] GeoClaw docker container detected (+${elapsed}s):\n${ps}`);
      }
    }

    // Update screenshot 18 once pipeline/docker seen
    if ((pipelineCardsSeen || geoclawContainerSeen) && !screenshot18Updated) {
      await page.screenshot({ path: path.join(PROOF_DIR, "18-geoclaw-local.png") });
      console.log("[e2e-geoclaw] updated screenshot 18-geoclaw-local.png (pipeline running)");
      screenshot18Updated = true;
    }

    // Check for layer or error
    const pageText = await page.evaluate(() => document.body.innerText).catch(() => "");
    const layerRendered = /geoclaw.*depth|Peak flood|flood.*layer|depth.*peak|geoclaw_depth|inundation.*layer/i.test(pageText);
    const errorSeen = /PostprocessGeoClawError|no.*inundation|no.*fort\.q|error_code.*geoclaw|GeoClawWorkflow/i.test(pageText);

    if (layerRendered || errorSeen) {
      if (!screenshot19Done) {
        const suffix = errorSeen ? "-failure" : "";
        const fname = `19-geoclaw-layer${suffix}.png`;
        await page.screenshot({ path: path.join(PROOF_DIR, fname) });
        console.log(`[e2e-geoclaw] screenshot ${fname} (+${elapsed}s)`);
        screenshot19Done = true;
      }
      layerFound = true;
      outcome = errorSeen ? "ERROR_SURFACE" : "PASS";
      break;
    }

    // Nudge if stalled
    if (nudgesUsed < MAX_NUDGES) {
      const needsNudge = await detectNudgeNeeded(page);
      if (needsNudge) {
        await sendChatMessage(page, NUDGE_TEXT);
        nudgesUsed++;
        console.log(`[e2e-geoclaw] nudge #${nudgesUsed} sent (+${elapsed}s)`);
        await sleep(5000);
      }
    }

    console.log(`[e2e-geoclaw] polling +${elapsed}s pipeline=${pipelineCardsSeen} docker=${geoclawContainerSeen} layer=${layerFound}`);
  }

  // Final screenshots if not taken
  if (!screenshot19Done) {
    await page.screenshot({ path: path.join(PROOF_DIR, "19-geoclaw-layer-timeout.png") });
    console.log("[e2e-geoclaw] timeout screenshot: 19-geoclaw-layer-timeout.png");
    outcome = "TIMEOUT";
  }

  console.log("[e2e-geoclaw] final docker ps:", dockerPsAll());
  console.log("[e2e-geoclaw] outcome:", outcome, "nudges_used:", nudgesUsed, "layer_found:", layerFound);

  await browser.close();
  console.log("[e2e-geoclaw] DONE outcome=" + outcome);
}

main().catch((e) => {
  console.error("[e2e-geoclaw] fatal:", e);
  process.exit(1);
});
