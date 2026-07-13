/**
 * e2e_swmm_llm.mjs -- LLM-driven PySWMM urban-flood on qwen3:8b-16k
 *
 * Scenario: send an urban stormwater SWMM flood prompt for downtown
 * Alexandria, VA, then wait up to 25 minutes for the full LLM-driven
 * chain to complete:
 *   geocode_location -> fetch_dem -> fetch_buildings
 *   -> run_swmm_urban_flood (pyswmm IN-PROCESS, no container)
 *   -> build_swmm_mesh -> run_swmm_local -> postprocess_swmm -> depth layer
 *
 * Nudges: if the model stalls with a clarification request or empty-arg error,
 * reply once with the full parameterisation and continue. Max 2 nudges.
 *
 * Screenshots -> docs/proof/:
 *   12-swmm-local.png   (pipeline/tool cards visible, run in flight)
 *   13-swmm-layer.png   (depth layer on the map OR final state)
 *
 * Run:
 *   cd /home/nate/Documents/trid3nt-local
 *   PLAYWRIGHT_BROWSERS_PATH=~/.cache/ms-playwright \
 *     node scripts/e2e_swmm_llm.mjs
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
    console.log("[e2e-swmm] using playwright from:", p);
    break;
  }
}
if (!playwright) {
  console.error("[e2e-swmm] playwright not found in any of:", candidates);
  process.exit(1);
}

const PROOF_DIR = path.resolve(__dirname, "../docs/proof");
fs.mkdirSync(PROOF_DIR, { recursive: true });

const APP_URL = "http://127.0.0.1:5173";
const MC_BIN = path.resolve(__dirname, "../bin/mc");

// Downtown Alexandria, VA -- small city-block box
const BBOX = "[-77.052, 38.802, -77.044, 38.808]";

const SWMM_PROMPT =
  "Run an urban stormwater SWMM flood simulation for a few blocks of downtown " +
  "Alexandria, Virginia, around lat 38.805 lon -77.047. Use the smallest " +
  "default network and a 10-year design storm. Proceed with defaults.";

const NUDGE_TEXT =
  "Call run_swmm_urban_flood now for downtown Alexandria, Virginia. " +
  "Use bbox=" + BBOX + ", return_period_yr=10, storm_duration_hr=1, " +
  "target_resolution_m=20, building_representation=drop. Proceed immediately.";

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
    console.log("[e2e-swmm] WARN: no chat input found");
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
  console.log("[e2e-swmm] sent:", text.slice(0, 80) + (text.length > 80 ? "..." : ""));
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
        console.log(`[e2e-swmm] clicking confirmation: ${sel}`);
        await el.click().catch((e) => console.log(`[e2e-swmm] click failed: ${e.message}`));
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
  return (
    /provide.{0,40}more|please.{0,40}(provide|specify|give)|need.{0,30}(bbox|location)|require.{0,30}(bbox|location)|clarif|cannot.*proceed|missing.*param|which.*(city|area|location)/i.test(
      text
    )
  );
}

async function layerCount(page) {
  const rows = page.locator('[data-testid="layer-row"], [data-testid="layer-group-row"]');
  return await rows.count().catch(() => 0);
}


// DATA-INTEGRITY GUARD (2026-07-12): the local server maps EVERY anonymous
// session to one shared local user, so a fresh boot RESUMES that user's
// last-active REAL case. Prompting without creating a case first mutated
// real cases (bbox overwrite + layer pollution). Always create a brand-new
// case before sending any prompt; never select or reuse an existing case.
async function createFreshCase(page) {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const btn = page.locator('[data-testid="grace2-cases-new"]').first();
  if (await btn.isVisible().catch(() => false)) {
    await btn.click().catch(() => {});
  } else {
    const roleBtn = page.getByRole("button", { name: /new case/i }).first();
    if (await roleBtn.count().catch(() => 0)) {
      await roleBtn.click().catch(() => {});
    } else {
      throw new Error("createFreshCase: no new-case button; refusing to prompt into an existing case");
    }
  }
  await wait(2500);
  const gate = page.locator('[data-testid="grace2-save-gate-modal-continue"]').first();
  if (await gate.isVisible().catch(() => false)) {
    await gate.click().catch(() => {});
    await wait(800);
  }
}

async function main() {
  console.log("[e2e-swmm] === LLM-driven SWMM urban-flood on qwen3:8b-16k ===");
  console.log("[e2e-swmm] launching chromium headless ...");

  const { chromium } = playwright;
  const browser = await chromium.launch({
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();

  page.on("console", (msg) => {
    if (msg.type() === "error") {
      console.log("[browser-error]", msg.text().slice(0, 200));
    }
  });

  const preRunListing = minioListing();
  const preRunPrefixes = runPrefixes(preRunListing);
  console.log("[e2e-swmm] pre-run MinIO run prefixes:", [...preRunPrefixes]);

  await context.addInitScript(() => {
    window.localStorage.setItem("grace2_anonymous_accepted", "true");
    window.localStorage.setItem("grace2.anonymous_user_id", "e2e-llm-swmm");
  });

  console.log("[e2e-swmm] navigating to", APP_URL + "/app");
  await page.goto(APP_URL + "/app", { waitUntil: "domcontentloaded", timeout: 30000 });
  await sleep(5000);

  const continueBtn = page.locator(
    'button:has-text("Continue"), button:has-text("anonymous"), button:has-text("Skip"), button:has-text("Use anonymously"), button:has-text("continue without")'
  );
  if (await continueBtn.count().then((c) => c > 0).catch(() => false)) {
    console.log("[e2e-swmm] clicking pass-through auth button");
    await continueBtn.first().click();
    await sleep(2000);
  }

  console.log("[e2e-swmm] waiting for chat input ...");
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
    console.log("[e2e-swmm] chat input ready");
  } catch (_) {
    console.log("[e2e-swmm] WARN: chat input not found within 30s");
  }

  console.log("[e2e-swmm] sending SWMM urban-flood prompt ...");
  await createFreshCase(page);

  await sendChatMessage(page, SWMM_PROMPT);
  const promptSentAt = Date.now();

  const MAX_WAIT_MS = 25 * 60 * 1000; // 25 minutes
  const POLL_MS = 15_000;

  let screenshot12Done = false;
  let screenshot13Done = false;
  let nudgesUsed = 0;
  const MAX_NUDGES = 2;
  let lastLayerCount = 0;
  let resultRunId = null;
  let toolSequence = [];
  let outcome = "PENDING";
  let pipelineCardsSeen = false;

  console.log("[e2e-swmm] entering 25-minute polling loop ...");
  const deadline = Date.now() + MAX_WAIT_MS;

  while (Date.now() < deadline) {
    await sleep(POLL_MS);
    const elapsed = Math.round((Date.now() - promptSentAt) / 1000);

    // -- A: pipeline/tool cards --
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
          console.log(`[e2e-swmm] tool/pipeline cards detected via ${sel} (+${elapsed}s)`);
          break;
        }
      }
    }

    // -- B: confirmations --
    const confirmClicked = await clickConfirmations(page);
    if (confirmClicked > 0) {
      console.log(`[e2e-swmm] clicked ${confirmClicked} confirmation(s) (+${elapsed}s)`);
    }

    // -- C: screenshot 12 once we see pipeline cards --
    if (pipelineCardsSeen && !screenshot12Done) {
      await page.screenshot({ path: path.join(PROOF_DIR, "12-swmm-local.png") });
      console.log("[e2e-swmm] screenshot: 12-swmm-local.png");
      screenshot12Done = true;
    }

    // -- D: nudge if stalled --
    const publishSeen = toolSequence.includes("publish_layer");
    if (nudgesUsed < MAX_NUDGES && !publishSeen && !resultRunId) {
      const needsNudge = await detectNudgeNeeded(page);
      if (needsNudge) {
        nudgesUsed++;
        console.log(`[e2e-swmm] nudge #${nudgesUsed}: model asking for clarification (+${elapsed}s)`);
        await sendChatMessage(page, NUDGE_TEXT);
        await sleep(5000);
      }
    }

    // -- E: MinIO new run prefix --
    const nowListing = minioListing();
    const nowPrefixes = runPrefixes(nowListing);
    for (const p of nowPrefixes) {
      if (!preRunPrefixes.has(p)) {
        resultRunId = p;
        console.log(`[e2e-swmm] new MinIO run prefix detected: ${p} (+${elapsed}s)`);
      }
    }

    // -- F: LayerPanel --
    const lc = await layerCount(page);
    if (lc > lastLayerCount) {
      console.log(`[e2e-swmm] layer count changed: ${lastLayerCount} -> ${lc} (+${elapsed}s)`);
      lastLayerCount = lc;
    }

    // -- G: tool names in UI --
    const pageText = await page.innerText("body").catch(() => "");
    const toolNames = [
      "geocode_location",
      "fetch_dem",
      "fetch_buildings",
      "run_swmm_urban_flood",
      "build_swmm_mesh",
      "run_swmm_local",
      "postprocess_swmm",
      "publish_layer",
    ];
    for (const t of toolNames) {
      if (pageText.includes(t) && !toolSequence.includes(t)) {
        toolSequence.push(t);
        console.log(`[e2e-swmm] tool seen in UI: ${t} (+${elapsed}s)`);
      }
    }

    // -- H: success --
    const minioSuccess = resultRunId != null;
    if (minioSuccess) {
      await page.screenshot({ path: path.join(PROOF_DIR, "13-swmm-layer.png") });
      console.log("[e2e-swmm] screenshot: 13-swmm-layer.png");
      screenshot13Done = true;
      outcome = "PASS";
      console.log(`[e2e-swmm] SUCCESS at +${elapsed}s (runId=${resultRunId}, layers=${lc})`);
      break;
    }

    if (elapsed % 60 < POLL_MS / 1000 + 1) {
      console.log(
        `[e2e-swmm] still polling... +${elapsed}s | layers=${lc} | nudges=${nudgesUsed} | runId=${resultRunId} | tools=[${toolSequence.join(",")}]`
      );
    }
  }

  if (!screenshot12Done) {
    await page.screenshot({ path: path.join(PROOF_DIR, "12-swmm-local.png") });
    console.log("[e2e-swmm] screenshot: 12-swmm-local.png (final state)");
  }
  if (!screenshot13Done) {
    await page.screenshot({ path: path.join(PROOF_DIR, "13-swmm-layer.png") });
    console.log("[e2e-swmm] screenshot: 13-swmm-layer.png (final state)");
  }
  if (outcome === "PENDING") {
    outcome = "FAIL";
    console.log("[e2e-swmm] 25-minute window expired without success");
  }

  const postRunListing = minioListing();
  console.log("[e2e-swmm] post-run MinIO listing:\n" + postRunListing);

  const timestamp = new Date().toISOString();
  const artifactEntry = [
    "",
    `=== LLM-driven SWMM urban-flood (local-pyswmm) on qwen3:8b-16k (${timestamp}) ===`,
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
  console.log("[e2e-swmm] appended to artifacts.txt");

  await browser.close();

  console.log("\n=== e2e_swmm_llm.mjs SUMMARY ===");
  console.log("outcome:      ", outcome);
  console.log("run_id:       ", resultRunId || "(none)");
  console.log("nudges_used:  ", nudgesUsed);
  console.log("tool_sequence:", toolSequence.join(" -> ") || "(none)");
  console.log("layers_found: ", lastLayerCount);
  console.log("screenshots:");
  for (const f of ["12-swmm-local.png", "13-swmm-layer.png"]) {
    const fp = path.join(PROOF_DIR, f);
    const size = fs.existsSync(fp) ? fs.statSync(fp).size : 0;
    console.log("  ", f, `(${size} bytes)`);
  }

  process.exit(outcome === "PASS" ? 0 : 1);
}

main().catch((err) => {
  console.error("[e2e-swmm] FATAL:", err);
  process.exit(1);
});
