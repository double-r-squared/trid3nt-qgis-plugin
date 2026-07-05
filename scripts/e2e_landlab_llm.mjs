/**
 * e2e_landlab_llm.mjs -- LLM-driven Landlab landslide-susceptibility on qwen3:8b-16k
 *
 * Scenario: send a Landlab landslide susceptibility prompt for a small hilly
 * area near Boulder, CO, then wait up to 25 minutes for the full LLM-driven
 * chain to complete:
 *   model_landslide_scenario -> fetch_dem -> stage_landlab_manifest
 *   -> run_solver('landlab') (local-exec subprocess run_chain.py shim)
 *   -> wait_for_completion -> postprocess_landlab -> susceptibility COG
 *
 * Screenshots -> docs/proof/:
 *   14-landlab-local.png   (pipeline/tool cards visible)
 *   15-landlab-layer.png   (susceptibility layer on the map OR final state)
 *
 * Run:
 *   cd /home/nate/Documents/trid3nt-local
 *   PLAYWRIGHT_BROWSERS_PATH=~/.cache/ms-playwright \
 *     node scripts/e2e_landlab_llm.mjs
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
    console.log("[e2e-landlab] using playwright from:", p);
    break;
  }
}
if (!playwright) {
  console.error("[e2e-landlab] playwright not found in any of:", candidates);
  process.exit(1);
}

const PROOF_DIR = path.resolve(__dirname, "../docs/proof");
fs.mkdirSync(PROOF_DIR, { recursive: true });

const APP_URL = "http://127.0.0.1:5173";
const MC_BIN = path.resolve(__dirname, "../bin/mc");

const BBOX = "[-105.37, 39.998, -105.33, 40.032]";

const LANDLAB_PROMPT =
  "Run a Landlab landslide susceptibility analysis for a small hilly area near " +
  "Boulder, Colorado, around lat 40.015 lon -105.35, 4km box. Use the coarsest " +
  "default resolution and proceed with defaults.";

const NUDGE_TEXT =
  "Call model_landslide_scenario now for Boulder, Colorado. " +
  "Use bbox=" + BBOX + ", analysis=landslide_probability, " +
  "target_resolution_m=30, n_monte_carlo=25. Proceed immediately.";

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function waitFor(fn, timeoutMs, intervalMs = 2000, label = "condition") {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const result = await fn();
      if (result) return result;
    } catch (_) {}
    await sleep(intervalMs);
  }
  throw new Error(`Timeout waiting for: ${label}`);
}

function minioListing() {
  try {
    return execSync(`${MC_BIN} ls local/trid3nt-runs/ --recursive 2>/dev/null`, {
      encoding: "utf8",
      timeout: 10000,
    }).trim();
  } catch (_) {
    return "(mc ls failed)";
  }
}

function runPrefixes(listing) {
  const prefixes = new Set();
  for (const line of listing.split("\n")) {
    const m = line.match(/\s(\S+?)\s*$/);
    if (!m) continue;
    const p = m[1];
    if (p.startsWith("case-manifests/") || p.startsWith("case-views/")) continue;
    const prefix = p.split("/")[0];
    if (prefix && prefix.length > 0) prefixes.add(prefix);
  }
  return prefixes;
}

async function sendChatMessage(page, text) {
  const selectors = [
    '[data-testid="chat-input"] textarea',
    '[data-testid="chat-input-wrapper"] textarea',
    "textarea",
  ];
  let input = null;
  for (const sel of selectors) {
    const el = page.locator(sel).first();
    if (await el.isVisible().catch(() => false)) { input = el; break; }
  }
  if (!input) { console.log("[e2e-landlab] WARN: no chat input found"); return false; }
  await input.click();
  await input.fill(text);
  const actionBtn = page.locator('[data-testid="chat-input-action"]').first();
  if (await actionBtn.isVisible().catch(() => false)) await actionBtn.click();
  else await input.press("Enter");
  console.log("[e2e-landlab] sent:", text.slice(0, 80) + (text.length > 80 ? "..." : ""));
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
        console.log(`[e2e-landlab] clicking confirmation: ${sel}`);
        await el.click().catch(() => {});
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
  const text = await agentMsgs.nth(count - 1).innerText().catch(() => "");
  return /provide.{0,40}more|please.{0,40}(provide|specify)|need.{0,30}(bbox|location)|clarif|cannot.*proceed|missing.*param/i.test(text);
}

async function main() {
  console.log("[e2e-landlab] === LLM-driven Landlab landslide-susceptibility on qwen3:8b-16k ===");
  const { chromium } = playwright;
  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"] });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();

  const preRunListing = minioListing();
  const preRunPrefixes = runPrefixes(preRunListing);
  console.log("[e2e-landlab] pre-run MinIO run prefixes:", [...preRunPrefixes]);

  await context.addInitScript(() => {
    window.localStorage.setItem("grace2_anonymous_accepted", "true");
    window.localStorage.setItem("grace2.anonymous_user_id", "e2e-llm-landlab");
  });

  await page.goto(APP_URL + "/app", { waitUntil: "domcontentloaded", timeout: 30000 });
  await sleep(5000);

  const continueBtn = page.locator(
    'button:has-text("Continue"), button:has-text("anonymous"), button:has-text("Skip"), button:has-text("Use anonymously"), button:has-text("continue without")'
  );
  if (await continueBtn.count().then((c) => c > 0).catch(() => false)) {
    await continueBtn.first().click();
    await sleep(2000);
  }

  try {
    await waitFor(async () => {
      const el = page.locator('[data-testid="chat-input"] textarea, textarea').first();
      return await el.isVisible().catch(() => false);
    }, 30000, 1000, "chat input visible");
  } catch (_) {}

  console.log("[e2e-landlab] sending Landlab prompt ...");
  await sendChatMessage(page, LANDLAB_PROMPT);
  const promptSentAt = Date.now();

  const MAX_WAIT_MS = 25 * 60 * 1000;
  const POLL_MS = 15_000;

  let screenshot14Done = false;
  let screenshot15Done = false;
  let nudgesUsed = 0;
  let lastLayerCount = 0;
  let resultRunId = null;
  let toolSequence = [];
  let outcome = "PENDING";
  let pipelineCardsSeen = false;

  const deadline = Date.now() + MAX_WAIT_MS;
  while (Date.now() < deadline) {
    await sleep(POLL_MS);
    const elapsed = Math.round((Date.now() - promptSentAt) / 1000);

    if (!pipelineCardsSeen) {
      for (const sel of ['[data-testid="pipeline-card-stack"]', '[data-testid="grace2-sheet-tool-strip"]', '[data-testid="resolution-picker-card"]']) {
        if (await page.locator(sel).count().catch(() => 0) > 0) {
          pipelineCardsSeen = true;
          console.log(`[e2e-landlab] pipeline cards detected via ${sel} (+${elapsed}s)`);
          break;
        }
      }
    }

    const confirmClicked = await clickConfirmations(page);
    if (confirmClicked > 0) console.log(`[e2e-landlab] clicked ${confirmClicked} confirmation(s) (+${elapsed}s)`);

    if (pipelineCardsSeen && !screenshot14Done) {
      await page.screenshot({ path: path.join(PROOF_DIR, "14-landlab-local.png") });
      console.log("[e2e-landlab] screenshot: 14-landlab-local.png");
      screenshot14Done = true;
    }

    if (nudgesUsed < 2 && !resultRunId) {
      if (await detectNudgeNeeded(page)) {
        nudgesUsed++;
        console.log(`[e2e-landlab] nudge #${nudgesUsed} (+${elapsed}s)`);
        await sendChatMessage(page, NUDGE_TEXT);
        await sleep(5000);
      }
    }

    const nowPrefixes = runPrefixes(minioListing());
    for (const p of nowPrefixes) {
      if (!preRunPrefixes.has(p)) {
        resultRunId = p;
        console.log(`[e2e-landlab] new MinIO run prefix: ${p} (+${elapsed}s)`);
      }
    }

    const lc = await page.locator('[data-testid="layer-row"], [data-testid="layer-group-row"]').count().catch(() => 0);
    if (lc > lastLayerCount) { console.log(`[e2e-landlab] layers: ${lastLayerCount} -> ${lc}`); lastLayerCount = lc; }

    const pageText = await page.innerText("body").catch(() => "");
    for (const t of ["model_landslide_scenario", "fetch_dem", "stage_landlab_manifest", "run_solver", "postprocess_landlab", "publish_layer"]) {
      if (pageText.includes(t) && !toolSequence.includes(t)) {
        toolSequence.push(t);
        console.log(`[e2e-landlab] tool seen: ${t} (+${elapsed}s)`);
      }
    }

    if (resultRunId != null) {
      await page.screenshot({ path: path.join(PROOF_DIR, "15-landlab-layer.png") });
      console.log("[e2e-landlab] screenshot: 15-landlab-layer.png");
      screenshot15Done = true;
      outcome = "PASS";
      console.log(`[e2e-landlab] SUCCESS at +${elapsed}s (runId=${resultRunId}, layers=${lc})`);
      break;
    }
    if (elapsed % 60 < POLL_MS / 1000 + 1) {
      console.log(`[e2e-landlab] polling... +${elapsed}s | layers=${lc} | runId=${resultRunId} | tools=[${toolSequence.join(",")}]`);
    }
  }

  if (!screenshot14Done) { await page.screenshot({ path: path.join(PROOF_DIR, "14-landlab-local.png") }); }
  if (!screenshot15Done) { await page.screenshot({ path: path.join(PROOF_DIR, "15-landlab-layer.png") }); }
  if (outcome === "PENDING") { outcome = "FAIL"; console.log("[e2e-landlab] 25-minute window expired"); }

  const postRunListing = minioListing();
  const timestamp = new Date().toISOString();
  const artifactEntry = [
    "",
    `=== LLM-driven Landlab landslide-susceptibility (local-exec) on qwen3:8b-16k (${timestamp}) ===`,
    `outcome:        ${outcome}`,
    `run_id:         ${resultRunId || "(none)"}`,
    `nudges_used:    ${nudgesUsed}`,
    `tool_sequence:  ${toolSequence.join(" -> ") || "(none observed)"}`,
    `layer_count:    ${lastLayerCount}`,
    "",
    "Post-run MinIO listing (trid3nt-runs):",
    postRunListing,
    "",
  ].join("\n");

  fs.appendFileSync(path.join(PROOF_DIR, "artifacts.txt"), artifactEntry, "utf8");
  await browser.close();

  console.log("\n=== e2e_landlab_llm.mjs SUMMARY ===");
  console.log("outcome:      ", outcome);
  console.log("run_id:       ", resultRunId || "(none)");
  console.log("tool_sequence:", toolSequence.join(" -> ") || "(none)");
  process.exit(outcome === "PASS" ? 0 : 1);
}

main().catch((err) => { console.error("[e2e-landlab] FATAL:", err); process.exit(1); });
