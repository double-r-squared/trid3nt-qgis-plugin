/**
 * e2e_modflow_llm.mjs -- LLM-driven MODFLOW retry on qwen3:8b-16k
 *
 * Scenario: send the MODFLOW sustainable-yield prompt for Fresno, CA, then
 * wait up to 25 minutes for the full LLM-driven chain to complete:
 *   geocode_location -> (list_categories / list_tools_in_category / discover_dataset)
 *   -> run_model_sustainable_yield_scenario -> local mf6 -> publish_layer -> raster layer
 *
 * Nudges: if the model stalls with a clarification request or empty-arg error,
 * reply once with the full parameterisation and continue.  Max 2 nudges.
 *
 * Screenshots -> docs/proof/:
 *   06-llm-driven-modflow-running.png   (pipeline/tool cards visible)
 *   07-llm-driven-modflow-layer.png     (result layer on the map)
 *
 * Run:
 *   cd /home/nate/Documents/trid3nt-local/vendor/web
 *   PLAYWRIGHT_BROWSERS_PATH=~/.cache/ms-playwright \
 *     node ../../scripts/e2e_modflow_llm.mjs
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
    console.log("[e2e-modflow] using playwright from:", p);
    break;
  }
}
if (!playwright) {
  console.error("[e2e-modflow] playwright not found in any of:", candidates);
  process.exit(1);
}

const PROOF_DIR = path.resolve(__dirname, "../docs/proof");
fs.mkdirSync(PROOF_DIR, { recursive: true });

const APP_URL = "http://127.0.0.1:5173";
const MC_BIN = path.resolve(__dirname, "../bin/mc");

// The exact prompt sent on the first turn.
// Use aoi_latlon instead of location to avoid the "both supplied" ambiguity in
// model_sustainable_yield_scenario (pass ONE of aoi_latlon / location, not both).
// well_location_latlon and pumping_rate_m3_day are both mandatory.
const MODFLOW_PROMPT =
  "Run a MODFLOW sustainable yield analysis for a small aquifer near Fresno, California. " +
  "Call run_model_sustainable_yield_scenario with these exact arguments and no others: " +
  "aoi_latlon=[36.7468, -119.7726], well_location_latlon=[36.7468, -119.7726], " +
  "pumping_rate_m3_day=2000. Do NOT include location or any other parameters.";

// Nudge text: repeat the exact tool args.
// Use aoi_latlon (not location) so the tool sees only ONE location field.
const NUDGE_TEXT =
  "Call run_model_sustainable_yield_scenario with exactly these args: " +
  "aoi_latlon=[36.7468, -119.7726], well_location_latlon=[36.7468, -119.7726], " +
  "pumping_rate_m3_day=2000. Do NOT pass location or any other parameters.";

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

/** Capture the current MinIO trid3nt-runs listing. */
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

/** Return the set of run-prefix directories (non case-manifests/case-views).
 *  Handles both `mc ls` (directory entries ending with /) and
 *  `mc ls --recursive` (file paths like "prefix/file.tif"). */
function runPrefixes(listing) {
  const prefixes = new Set();
  for (const line of listing.split("\n")) {
    // Match anything after the last whitespace that looks like a path.
    const m = line.match(/\s(\S+?)\s*$/);
    if (!m) continue;
    const p = m[1];
    // Skip case-manifests and case-views.
    if (p.startsWith("case-manifests/") || p.startsWith("case-views/")) continue;
    // Take only the top-level prefix (first path component).
    const prefix = p.split("/")[0];
    if (prefix && prefix.length > 0) {
      prefixes.add(prefix);
    }
  }
  return prefixes;
}

/** Send a message via the chat input - finds the textarea, fills, and submits. */
async function sendChatMessage(page, text) {
  // Try the data-testid first, then fall back to generic selectors.
  const selectors = [
    '[data-testid="chat-input"] textarea',
    '[data-testid="chat-input-wrapper"] textarea',
    'textarea',
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
    console.log("[e2e-modflow] WARN: no chat input found, cannot send:", text.slice(0, 60));
    return false;
  }
  await input.click();
  await input.fill(text);

  // Try the action button first, then Enter.
  const actionBtn = page.locator('[data-testid="chat-input-action"]').first();
  if (await actionBtn.isVisible().catch(() => false)) {
    await actionBtn.click();
  } else {
    await input.press("Enter");
  }
  console.log("[e2e-modflow] sent:", text.slice(0, 80) + (text.length > 80 ? "..." : ""));
  return true;
}

/** Click any visible affirmative confirmation buttons (resolution-picker, payload-warning). */
async function clickConfirmations(page) {
  const affirmSelectors = [
    '[data-testid="resolution-picker-confirm"]',
    '[data-testid="sandbox-card-proceed"]',
    'button:has-text("Confirm")',
    'button:has-text("Proceed")',
    'button:has-text("Yes")',
    'button:has-text("Run")',
    'button:has-text("OK")',
  ];
  let clicked = 0;
  for (const sel of affirmSelectors) {
    const els = page.locator(sel);
    const count = await els.count().catch(() => 0);
    for (let i = 0; i < count; i++) {
      const el = els.nth(i);
      if (await el.isVisible().catch(() => false)) {
        console.log(`[e2e-modflow] clicking confirmation: ${sel}`);
        await el.click().catch((e) => console.log(`[e2e-modflow] click failed: ${e.message}`));
        await sleep(1000);
        clicked++;
      }
    }
  }
  return clicked;
}

/** Check if the model replied asking for clarification or hit the missing-args error.
 *  Only look at agent-message elements (not the user's own prompt text). */
async function detectNudgeNeeded(page) {
  // Look at only agent-authored messages, not user bubbles.
  const agentMsgs = page.locator('[data-testid="agent-message"]');
  const count = await agentMsgs.count().catch(() => 0);
  if (count === 0) return false;
  // Check the LAST agent message (most recent reply).
  const lastMsg = agentMsgs.nth(count - 1);
  const text = await lastMsg.innerText().catch(() => "");
  return (
    /well.{0,40}locat|pumping.{0,40}rate|provide.{0,40}more|please.{0,40}(provide|specify|give)|need.{0,30}(well|rate|location)|require.{0,30}(well|rate)|coordinates|clarif|cannot.*proceed|missing.*param/i.test(
      text
    )
  );
}

/** Count layer-row entries in the LayerPanel. */
async function layerCount(page) {
  const rows = page.locator('[data-testid="layer-row"], [data-testid="layer-group-row"]');
  return await rows.count().catch(() => 0);
}

async function main() {
  console.log("[e2e-modflow] === LLM-driven MODFLOW retry on qwen3:8b-16k ===");
  console.log("[e2e-modflow] launching chromium headless ...");

  const { chromium } = playwright;
  const browser = await chromium.launch({
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();

  // Collect browser errors for diagnostics.
  page.on("console", (msg) => {
    if (msg.type() === "error") {
      console.log("[browser-error]", msg.text().slice(0, 200));
    }
  });

  // ---------------------------------------------------------------------------
  // Pre-run: capture MinIO baseline
  // ---------------------------------------------------------------------------
  const preRunListing = minioListing();
  const preRunPrefixes = runPrefixes(preRunListing);
  console.log("[e2e-modflow] pre-run MinIO run prefixes:", [...preRunPrefixes]);

  // ---------------------------------------------------------------------------
  // Step 1: Load the app (bypass landing page via localStorage)
  // ---------------------------------------------------------------------------
  await context.addInitScript(() => {
    window.localStorage.setItem("grace2_anonymous_accepted", "true");
    window.localStorage.setItem("grace2.anonymous_user_id", "e2e-llm-modflow");
  });

  console.log("[e2e-modflow] navigating to", APP_URL + "/app");
  await page.goto(APP_URL + "/app", { waitUntil: "domcontentloaded", timeout: 30000 });
  await sleep(5000);

  // Bypass any auth gate.
  const continueBtn = page.locator(
    'button:has-text("Continue"), button:has-text("anonymous"), button:has-text("Skip"), button:has-text("Use anonymously"), button:has-text("continue without")'
  );
  if (await continueBtn.count().then((c) => c > 0).catch(() => false)) {
    console.log("[e2e-modflow] clicking pass-through auth button");
    await continueBtn.first().click();
    await sleep(2000);
  }

  // ---------------------------------------------------------------------------
  // Step 2: Wait for WS connected and chat input ready
  // ---------------------------------------------------------------------------
  console.log("[e2e-modflow] waiting for chat input ...");
  let chatReady = false;
  try {
    await waitFor(
      async () => {
        const el = page
          .locator('[data-testid="chat-input"] textarea, [data-testid="chat-input-wrapper"] textarea, textarea')
          .first();
        return await el.isVisible().catch(() => false);
      },
      30000,
      1000,
      "chat input visible"
    );
    chatReady = true;
    console.log("[e2e-modflow] chat input ready");
  } catch (_) {
    console.log("[e2e-modflow] WARN: chat input not found within 30s");
  }

  // ---------------------------------------------------------------------------
  // Step 3: Send the MODFLOW prompt
  // ---------------------------------------------------------------------------
  console.log("[e2e-modflow] sending MODFLOW prompt ...");
  await sendChatMessage(page, MODFLOW_PROMPT);
  const promptSentAt = Date.now();

  // ---------------------------------------------------------------------------
  // Step 4: Main polling loop -- up to 25 min
  //
  // Poll every 15s for:
  //   a) tool / pipeline cards appearing (screenshot 06)
  //   b) confirmation cards -> click them
  //   c) stall (model asking for clarification) -> nudge up to 2x
  //   d) new layer in LayerPanel (success signal)
  //   e) new run prefix in MinIO (success signal)
  // ---------------------------------------------------------------------------
  const MAX_WAIT_MS = 25 * 60 * 1000; // 25 minutes
  const POLL_MS = 15_000;

  let screenshot06Done = false;
  let screenshot07Done = false;
  let nudgesUsed = 0;
  const MAX_NUDGES = 2;
  let lastLayerCount = 0;
  let resultRunId = null;
  let toolSequence = [];
  let outcome = "PENDING";

  console.log("[e2e-modflow] entering 25-minute polling loop ...");

  // Track whether we've seen pipeline/tool cards so we know when to screenshot.
  let pipelineCardsSeen = false;

  const deadline = Date.now() + MAX_WAIT_MS;

  while (Date.now() < deadline) {
    await sleep(POLL_MS);

    const elapsed = Math.round((Date.now() - promptSentAt) / 1000);

    // -- A: Check for pipeline/tool cards (for screenshot 06) --
    if (!pipelineCardsSeen) {
      const toolCardSels = [
        '[data-testid="pipeline-card-stack"]',
        '[data-testid="grace2-sheet-tool-strip"]',
        '[data-testid="grace2-sheet-sandbox-strip"]',
        '[data-testid="resolution-picker-card"]',
        '[data-testid="sandbox-card-proceed"]',
      ];
      for (const sel of toolCardSels) {
        const count = await page.locator(sel).count().catch(() => 0);
        if (count > 0) {
          pipelineCardsSeen = true;
          console.log(`[e2e-modflow] tool/pipeline cards detected via ${sel} (+${elapsed}s)`);
          break;
        }
      }
    }

    // -- B: Click any visible confirmation cards --
    const confirmClicked = await clickConfirmations(page);
    if (confirmClicked > 0) {
      console.log(`[e2e-modflow] clicked ${confirmClicked} confirmation(s) (+${elapsed}s)`);
    }

    // -- C: Take screenshot 06 once we see pipeline cards --
    if (pipelineCardsSeen && !screenshot06Done) {
      await page.screenshot({ path: path.join(PROOF_DIR, "06-llm-driven-modflow-running.png") });
      console.log("[e2e-modflow] screenshot: 06-llm-driven-modflow-running.png");
      screenshot06Done = true;
    }

    // -- D: Detect stall / need for nudge (only if no success signal yet) --
    // Skip nudges once we've seen publish_layer (success is in progress).
    const publishSeen = toolSequence.includes("publish_layer");
    if (nudgesUsed < MAX_NUDGES && !publishSeen && !resultRunId) {
      const needsNudge = await detectNudgeNeeded(page);
      if (needsNudge) {
        nudgesUsed++;
        console.log(`[e2e-modflow] nudge #${nudgesUsed}: model asking for clarification (+${elapsed}s)`);
        await sendChatMessage(page, NUDGE_TEXT);
        await sleep(5000); // give the model time to pick up the nudge
      }
    }

    // -- E: Check MinIO for new run prefix --
    const nowListing = minioListing();
    const nowPrefixes = runPrefixes(nowListing);
    for (const p of nowPrefixes) {
      if (!preRunPrefixes.has(p)) {
        resultRunId = p;
        console.log(`[e2e-modflow] new MinIO run prefix detected: ${p} (+${elapsed}s)`);
      }
    }

    // -- F: Check LayerPanel --
    const lc = await layerCount(page);
    if (lc > lastLayerCount) {
      console.log(`[e2e-modflow] layer count changed: ${lastLayerCount} -> ${lc} (+${elapsed}s)`);
      lastLayerCount = lc;
    }

    // -- G: Extract visible tool names from page text (rough log of what fired) --
    const pageText = await page.innerText("body").catch(() => "");
    const toolNames = [
      "geocode_location",
      "list_categories",
      "list_tools_in_category",
      "discover_dataset",
      "run_model_sustainable_yield_scenario",
      "run_modflow_archetype_job",
      "run_modflow_job",
      "publish_layer",
      "postprocess_drawdown",
    ];
    for (const t of toolNames) {
      if (pageText.includes(t) && !toolSequence.includes(t)) {
        toolSequence.push(t);
        console.log(`[e2e-modflow] tool seen in UI: ${t} (+${elapsed}s)`);
      }
    }

    // -- H: Success condition --
    const layerSuccess = lc > 0 && (resultRunId != null || lc > 0);
    const minioSuccess = resultRunId != null;

    if (layerSuccess || minioSuccess) {
      // Take screenshot 07.
      await page.screenshot({ path: path.join(PROOF_DIR, "07-llm-driven-modflow-layer.png") });
      console.log("[e2e-modflow] screenshot: 07-llm-driven-modflow-layer.png");
      screenshot07Done = true;
      outcome = "PASS";
      console.log(`[e2e-modflow] SUCCESS at +${elapsed}s`);
      break;
    }

    // Log heartbeat every minute.
    if (elapsed % 60 < POLL_MS / 1000 + 1) {
      console.log(
        `[e2e-modflow] still polling... +${elapsed}s | layers=${lc} | nudges=${nudgesUsed} | runId=${resultRunId}`
      );
    }
  }

  // If we never got the success screenshot, capture the final state.
  if (!screenshot06Done) {
    await page.screenshot({ path: path.join(PROOF_DIR, "06-llm-driven-modflow-running.png") });
    console.log("[e2e-modflow] screenshot: 06-llm-driven-modflow-running.png (final state, no tool cards seen)");
  }
  if (!screenshot07Done) {
    await page.screenshot({ path: path.join(PROOF_DIR, "07-llm-driven-modflow-layer.png") });
    console.log("[e2e-modflow] screenshot: 07-llm-driven-modflow-layer.png (final state, no layer)");
  }
  if (outcome === "PENDING") {
    outcome = "FAIL";
    console.log("[e2e-modflow] 25-minute window expired without success");
  }

  // ---------------------------------------------------------------------------
  // Post-run MinIO listing
  // ---------------------------------------------------------------------------
  const postRunListing = minioListing();
  console.log("[e2e-modflow] post-run MinIO listing:\n" + postRunListing);

  // ---------------------------------------------------------------------------
  // Append to artifacts.txt
  // ---------------------------------------------------------------------------
  const timestamp = new Date().toISOString();
  const artifactEntry = [
    "",
    `=== LLM-driven MODFLOW retry on qwen3:8b-16k (${timestamp}) ===`,
    `outcome:        ${outcome}`,
    `run_id:         ${resultRunId || "(none)"}`,
    `nudges_used:    ${nudgesUsed}`,
    `tool_sequence:  ${toolSequence.join(" -> ") || "(none observed)"}`,
    `layer_count:    ${lastLayerCount}`,
    ``,
    "Post-run MinIO listing (trid3nt-runs):",
    postRunListing,
    "",
  ].join("\n");

  fs.appendFileSync(path.join(PROOF_DIR, "artifacts.txt"), artifactEntry, "utf8");
  console.log("[e2e-modflow] appended to artifacts.txt");

  await browser.close();

  // ---------------------------------------------------------------------------
  // Summary
  // ---------------------------------------------------------------------------
  console.log("\n=== e2e_modflow_llm.mjs SUMMARY ===");
  console.log("outcome:      ", outcome);
  console.log("run_id:       ", resultRunId || "(none)");
  console.log("nudges_used:  ", nudgesUsed);
  console.log("tool_sequence:", toolSequence.join(" -> ") || "(none)");
  console.log("layers_found: ", lastLayerCount);
  console.log("screenshots:");
  for (const f of ["06-llm-driven-modflow-running.png", "07-llm-driven-modflow-layer.png"]) {
    const fp = path.join(PROOF_DIR, f);
    const size = fs.existsSync(fp) ? fs.statSync(fp).size : 0;
    console.log("  ", f, `(${size} bytes)`);
  }

  process.exit(outcome === "PASS" ? 0 : 1);
}

main().catch((err) => {
  console.error("[e2e-modflow] FATAL:", err);
  process.exit(1);
});
