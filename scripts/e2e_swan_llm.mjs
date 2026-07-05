/**
 * e2e_swan_llm.mjs -- LLM-driven SWAN nearshore wave simulation on qwen3:8b-16k
 *
 * Scenario: send a SWAN wave prompt for Huntington Beach CA, then wait up to
 * 20 min for the full LLM-driven chain to complete.
 *
 * Screenshots -> docs/proof/:
 *   20-swan-local.png   (pipeline/tool cards visible)
 *   21-swan-layer.png   (wave height layer on the map, or -failure.png)
 *
 * Run:
 *   cd /home/nate/Documents/trid3nt-local
 *   PLAYWRIGHT_BROWSERS_PATH=~/.cache/ms-playwright \
 *     node scripts/e2e_swan_llm.mjs
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
    console.log("[e2e-swan] using playwright from:", p);
    break;
  }
}
if (!playwright) {
  console.error("[e2e-swan] playwright not found in any of:", candidates);
  process.exit(1);
}

const PROOF_DIR = path.resolve(__dirname, "../docs/proof");
fs.mkdirSync(PROOF_DIR, { recursive: true });

const APP_URL = "http://127.0.0.1:5173";

const SWAN_PROMPT =
  "Run a coastal wave (SWAN) simulation for a small nearshore area around " +
  "lat 33.65 lon -118.0 (Huntington Beach, California). " +
  "Call run_swan_waves with bbox=[-118.05, 33.60, -117.95, 33.70], mode=\"stationary\". " +
  "Use default settings and the coarsest grid. Do NOT ask for confirmation.";

const NUDGE_TEXT =
  "Call run_swan_waves now with exactly these args: " +
  "bbox=[-118.05, 33.60, -117.95, 33.70], mode=\"stationary\". " +
  "Proceed immediately with defaults.";

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
    console.log("[e2e-swan] WARN: no chat input found");
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
  console.log("[e2e-swan] sent:", text.slice(0, 80) + (text.length > 80 ? "..." : ""));
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
        console.log(`[e2e-swan] clicking confirmation: ${sel}`);
        await el.click().catch((e) => console.log(`[e2e-swan] click failed: ${e.message}`));
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
  console.log("[e2e-swan] === LLM-driven SWAN wave simulation on qwen3:8b-16k ===");
  const { chromium } = playwright;
  const browser = await chromium.launch({
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });

  // Pre-seed anonymous auth in localStorage so the auth gate is skipped
  await context.addInitScript(() => {
    window.localStorage.setItem("grace2_anonymous_accepted", "true");
    window.localStorage.setItem("grace2.anonymous_user_id", "e2e-swan-llm");
  });

  const page = await context.newPage();
  page.on("console", (msg) => {
    if (msg.type() === "error") {
      console.log("[browser-error]", msg.text().slice(0, 200));
    }
  });

  console.log("[e2e-swan] navigating to", APP_URL + "/app");
  await page.goto(APP_URL + "/app", { waitUntil: "domcontentloaded", timeout: 30000 });
  await sleep(5000);

  // Handle auth gate if still shown
  const continueBtn = page.locator(
    'button:has-text("Continue"), button:has-text("anonymous"), button:has-text("Skip"), ' +
    'button:has-text("Use anonymously"), button:has-text("continue without"), ' +
    'button:has-text("Continue without saving")'
  );
  if (await continueBtn.count().then((c) => c > 0).catch(() => false)) {
    console.log("[e2e-swan] clicking pass-through auth button");
    await continueBtn.first().click();
    await sleep(2000);
  }

  // Wait for chat input
  console.log("[e2e-swan] waiting for chat input...");
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
    console.log("[e2e-swan] WARN: chat input not visible, attempting anyway");
  } else {
    console.log("[e2e-swan] chat input ready");
  }

  // Take initial screenshot
  await page.screenshot({ path: path.join(PROOF_DIR, "20-swan-local.png") });
  console.log("[e2e-swan] initial screenshot: 20-swan-local.png");

  console.log("[e2e-swan] sending SWAN wave prompt...");
  await sendChatMessage(page, SWAN_PROMPT);
  const promptSentAt = Date.now();

  const MAX_WAIT_MS = 20 * 60 * 1000; // 20 minutes
  const POLL_MS = 15000;
  let nudgesUsed = 0;
  const MAX_NUDGES = 2;
  let pipelineCardsSeen = false;
  let swanContainerSeen = false;
  let layerFound = false;
  let screenshot20Updated = false;
  let screenshot21Done = false;
  let outcome = "PENDING";

  console.log("[e2e-swan] entering polling loop (max 20 min)...");
  const deadline = Date.now() + MAX_WAIT_MS;

  while (Date.now() < deadline) {
    await sleep(POLL_MS);
    const elapsed = Math.round((Date.now() - promptSentAt) / 1000);

    // Check pipeline cards
    if (!pipelineCardsSeen) {
      for (const sel of ['[data-testid="pipeline-card-stack"]', '[data-testid="grace2-sheet-tool-strip"]', '[data-testid="resolution-picker-card"]']) {
        if (await page.locator(sel).count().catch(() => 0) > 0) {
          pipelineCardsSeen = true;
          console.log(`[e2e-swan] pipeline cards seen via ${sel} (+${elapsed}s)`);
          break;
        }
      }
    }

    // Confirmations
    await clickConfirmations(page);

    // Docker evidence
    if (!swanContainerSeen) {
      const ps = dockerPsAll();
      if (/trid3nt-local\/swan/i.test(ps)) {
        swanContainerSeen = true;
        console.log(`[e2e-swan] SWAN docker container detected (+${elapsed}s):\n${ps}`);
      }
    }

    // Update screenshot 20 once pipeline/docker seen
    if ((pipelineCardsSeen || swanContainerSeen) && !screenshot20Updated) {
      await page.screenshot({ path: path.join(PROOF_DIR, "20-swan-local.png") });
      console.log("[e2e-swan] updated screenshot 20-swan-local.png (pipeline running)");
      screenshot20Updated = true;
    }

    // Check for layer or error
    const pageText = await page.evaluate(() => document.body.innerText).catch(() => "");
    const layerRendered = /wave.*height|Peak wave|swan.*layer|Hs.*m|wave_area|WaveField|wave height/i.test(pageText);
    const errorSeen = /PostprocessSwanError|no.*wave|calm.*threshold|error_code.*swan|SwanWorkflow/i.test(pageText);

    if (layerRendered || errorSeen) {
      if (!screenshot21Done) {
        const suffix = errorSeen ? "-failure" : "";
        const fname = `21-swan-layer${suffix}.png`;
        await page.screenshot({ path: path.join(PROOF_DIR, fname) });
        console.log(`[e2e-swan] screenshot ${fname} (+${elapsed}s)`);
        screenshot21Done = true;
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
        console.log(`[e2e-swan] nudge #${nudgesUsed} sent (+${elapsed}s)`);
        await sleep(5000);
      }
    }

    console.log(`[e2e-swan] polling +${elapsed}s pipeline=${pipelineCardsSeen} docker=${swanContainerSeen} layer=${layerFound}`);
  }

  // Final screenshots if not taken
  if (!screenshot21Done) {
    await page.screenshot({ path: path.join(PROOF_DIR, "21-swan-layer-timeout.png") });
    console.log("[e2e-swan] timeout screenshot: 21-swan-layer-timeout.png");
    outcome = "TIMEOUT";
  }

  console.log("[e2e-swan] final docker ps:", dockerPsAll());
  console.log("[e2e-swan] outcome:", outcome, "nudges_used:", nudgesUsed, "layer_found:", layerFound);

  await browser.close();
  console.log("[e2e-swan] DONE outcome=" + outcome);
}

main().catch((e) => {
  console.error("[e2e-swan] fatal:", e);
  process.exit(1);
});
