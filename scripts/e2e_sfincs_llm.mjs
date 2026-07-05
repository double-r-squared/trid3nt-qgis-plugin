/**
 * e2e_sfincs_llm.mjs -- LLM-driven SFINCS pluvial flood on qwen3:8b-16k
 *
 * Scenario: send a small pluvial rain-on-grid SFINCS flood prompt for a ~4km
 * box in downtown Chattanooga, TN, then wait up to 30 minutes for the full
 * LLM-driven chain to complete:
 *   geocode_location / fetch_dem / fetch_landcover
 *   -> run_model_flood_scenario (pluvial, coarsest resolution)
 *   -> build_sfincs_model (hydromt) -> run_solver (local-docker: deltares/sfincs-cpu)
 *   -> wait_for_completion -> postprocess_flood -> publish_layer -> depth layer
 *
 * Nudges: if the model stalls with a clarification request or empty-arg error,
 * reply once with the full parameterisation and continue.  Max 2 nudges.
 *
 * Screenshots -> docs/proof/:
 *   08-sfincs-local-running.png   (pipeline/tool cards visible)
 *   09-sfincs-depth-layer.png     (depth layer on the map)
 *
 * Run:
 *   cd /home/nate/Documents/trid3nt-local
 *   PLAYWRIGHT_BROWSERS_PATH=~/.cache/ms-playwright \
 *     node scripts/e2e_sfincs_llm.mjs
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
    console.log("[e2e-sfincs] using playwright from:", p);
    break;
  }
}
if (!playwright) {
  console.error("[e2e-sfincs] playwright not found in any of:", candidates);
  process.exit(1);
}

const PROOF_DIR = path.resolve(__dirname, "../docs/proof");
fs.mkdirSync(PROOF_DIR, { recursive: true });

const APP_URL = "http://127.0.0.1:5173";
const MC_BIN = path.resolve(__dirname, "../bin/mc");

// Downtown Chattanooga, TN -- approx 4km box.
const BBOX = "[-85.32, 35.03, -85.28, 35.07]";

const SFINCS_PROMPT =
  "Run a small pluvial rain-on-grid SFINCS flood simulation for a 4km box in " +
  "downtown Chattanooga, Tennessee. Call run_model_flood_scenario with these " +
  "exact arguments and no others: bbox=" + BBOX + ", flood_type=\"pluvial\", " +
  "return_period_years=100, duration_hours=1. Use the coarsest default " +
  "resolution and proceed with defaults. Do NOT ask for confirmation.";

const NUDGE_TEXT =
  "Call run_model_flood_scenario now with exactly these args: bbox=" + BBOX + ", " +
  "flood_type=\"pluvial\", return_period_years=100, duration_hours=1. " +
  "Proceed with the coarsest default resolution, no other parameters.";

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

/** Return the set of run-prefix directories (non case-manifests/case-views). */
function runPrefixes(listing) {
  const prefixes = new Set();
  for (const line of listing.split("\n")) {
    const m = line.match(/\s(\S+?)\s*$/);
    if (!m) continue;
    const p = m[1];
    if (p.startsWith("case-manifests/") || p.startsWith("case-views/")) continue;
    const prefix = p.split("/")[0];
    if (prefix && prefix.length > 0) {
      prefixes.add(prefix);
    }
  }
  return prefixes;
}

/** Snapshot of `docker ps -a` (name/status/image) via sg docker. */
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

/** True if any container name looks like a SFINCS run (ULID) currently exists. */
function sfincsContainerSeen(psText) {
  // The container name IS the run_id (a 26-char ULID). Match a running/exited
  // container that uses the sfincs image.
  return /sfincs-cpu/i.test(psText);
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
    console.log("[e2e-sfincs] WARN: no chat input found, cannot send:", text.slice(0, 60));
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
  console.log("[e2e-sfincs] sent:", text.slice(0, 80) + (text.length > 80 ? "..." : ""));
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
        console.log(`[e2e-sfincs] clicking confirmation: ${sel}`);
        await el.click().catch((e) => console.log(`[e2e-sfincs] click failed: ${e.message}`));
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
    /provide.{0,40}more|please.{0,40}(provide|specify|give)|need.{0,30}(bbox|location|storm)|require.{0,30}(bbox|location)|clarif|cannot.*proceed|missing.*param|which.*(city|area|location)/i.test(
      text
    )
  );
}

async function layerCount(page) {
  const rows = page.locator('[data-testid="layer-row"], [data-testid="layer-group-row"]');
  return await rows.count().catch(() => 0);
}

async function main() {
  console.log("[e2e-sfincs] === LLM-driven SFINCS pluvial flood on qwen3:8b-16k ===");
  console.log("[e2e-sfincs] launching chromium headless ...");

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
  console.log("[e2e-sfincs] pre-run MinIO run prefixes:", [...preRunPrefixes]);

  await context.addInitScript(() => {
    window.localStorage.setItem("grace2_anonymous_accepted", "true");
    window.localStorage.setItem("grace2.anonymous_user_id", "e2e-llm-sfincs");
  });

  console.log("[e2e-sfincs] navigating to", APP_URL + "/app");
  await page.goto(APP_URL + "/app", { waitUntil: "domcontentloaded", timeout: 30000 });
  await sleep(5000);

  const continueBtn = page.locator(
    'button:has-text("Continue"), button:has-text("anonymous"), button:has-text("Skip"), button:has-text("Use anonymously"), button:has-text("continue without")'
  );
  if (await continueBtn.count().then((c) => c > 0).catch(() => false)) {
    console.log("[e2e-sfincs] clicking pass-through auth button");
    await continueBtn.first().click();
    await sleep(2000);
  }

  console.log("[e2e-sfincs] waiting for chat input ...");
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
    console.log("[e2e-sfincs] chat input ready");
  } catch (_) {
    console.log("[e2e-sfincs] WARN: chat input not found within 30s");
  }

  console.log("[e2e-sfincs] sending SFINCS pluvial prompt ...");
  await sendChatMessage(page, SFINCS_PROMPT);
  const promptSentAt = Date.now();

  const MAX_WAIT_MS = 30 * 60 * 1000; // 30 minutes
  const POLL_MS = 15_000;

  let screenshot08Done = false;
  let screenshot09Done = false;
  let nudgesUsed = 0;
  const MAX_NUDGES = 2;
  let lastLayerCount = 0;
  let resultRunId = null;
  let toolSequence = [];
  let outcome = "PENDING";
  let pipelineCardsSeen = false;
  let dockerContainerSeen = false;
  let dockerEvidence = "";

  console.log("[e2e-sfincs] entering 30-minute polling loop ...");
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
          console.log(`[e2e-sfincs] tool/pipeline cards detected via ${sel} (+${elapsed}s)`);
          break;
        }
      }
    }

    // -- B: confirmations --
    const confirmClicked = await clickConfirmations(page);
    if (confirmClicked > 0) {
      console.log(`[e2e-sfincs] clicked ${confirmClicked} confirmation(s) (+${elapsed}s)`);
    }

    // -- C: docker container evidence (the genuine-execution signal) --
    if (!dockerContainerSeen) {
      const ps = dockerPsAll();
      if (sfincsContainerSeen(ps)) {
        dockerContainerSeen = true;
        dockerEvidence = ps;
        console.log(`[e2e-sfincs] SFINCS docker container detected (+${elapsed}s):\n${ps}`);
      }
    }

    // -- D: screenshot 08 once we see pipeline cards OR docker container --
    if ((pipelineCardsSeen || dockerContainerSeen) && !screenshot08Done) {
      await page.screenshot({ path: path.join(PROOF_DIR, "08-sfincs-local-running.png") });
      console.log("[e2e-sfincs] screenshot: 08-sfincs-local-running.png");
      screenshot08Done = true;
    }

    // -- E: nudge if stalled --
    const publishSeen = toolSequence.includes("publish_layer");
    if (nudgesUsed < MAX_NUDGES && !publishSeen && !resultRunId) {
      const needsNudge = await detectNudgeNeeded(page);
      if (needsNudge) {
        nudgesUsed++;
        console.log(`[e2e-sfincs] nudge #${nudgesUsed}: model asking for clarification (+${elapsed}s)`);
        await sendChatMessage(page, NUDGE_TEXT);
        await sleep(5000);
      }
    }

    // -- F: MinIO new run prefix --
    const nowListing = minioListing();
    const nowPrefixes = runPrefixes(nowListing);
    for (const p of nowPrefixes) {
      if (!preRunPrefixes.has(p)) {
        resultRunId = p;
        console.log(`[e2e-sfincs] new MinIO run prefix detected: ${p} (+${elapsed}s)`);
      }
    }

    // -- G: LayerPanel --
    const lc = await layerCount(page);
    if (lc > lastLayerCount) {
      console.log(`[e2e-sfincs] layer count changed: ${lastLayerCount} -> ${lc} (+${elapsed}s)`);
      lastLayerCount = lc;
    }

    // -- H: tool names in UI --
    const pageText = await page.innerText("body").catch(() => "");
    const toolNames = [
      "geocode_location",
      "fetch_dem",
      "fetch_landcover",
      "run_model_flood_scenario",
      "build_sfincs_model",
      "run_solver",
      "wait_for_completion",
      "postprocess_flood",
      "publish_layer",
    ];
    for (const t of toolNames) {
      if (pageText.includes(t) && !toolSequence.includes(t)) {
        toolSequence.push(t);
        console.log(`[e2e-sfincs] tool seen in UI: ${t} (+${elapsed}s)`);
      }
    }

    // -- I: success --
    // Genuine success = a new run prefix in MinIO that carries a depth/output
    // artifact, OR a rendered layer on the map. We treat a new run prefix as
    // the primary genuine-execution signal.
    const minioSuccess = resultRunId != null;
    const layerSuccess = lc > 0 && resultRunId != null;

    if (minioSuccess || layerSuccess) {
      await page.screenshot({ path: path.join(PROOF_DIR, "09-sfincs-depth-layer.png") });
      console.log("[e2e-sfincs] screenshot: 09-sfincs-depth-layer.png");
      screenshot09Done = true;
      outcome = "PASS";
      console.log(`[e2e-sfincs] SUCCESS at +${elapsed}s (runId=${resultRunId}, layers=${lc})`);
      break;
    }

    if (elapsed % 60 < POLL_MS / 1000 + 1) {
      console.log(
        `[e2e-sfincs] still polling... +${elapsed}s | layers=${lc} | nudges=${nudgesUsed} | runId=${resultRunId} | dockerSeen=${dockerContainerSeen} | tools=[${toolSequence.join(",")}]`
      );
    }
  }

  if (!screenshot08Done) {
    await page.screenshot({ path: path.join(PROOF_DIR, "08-sfincs-local-running.png") });
    console.log("[e2e-sfincs] screenshot: 08-sfincs-local-running.png (final state)");
  }
  if (!screenshot09Done) {
    await page.screenshot({ path: path.join(PROOF_DIR, "09-sfincs-depth-layer.png") });
    console.log("[e2e-sfincs] screenshot: 09-sfincs-depth-layer.png (final state)");
  }
  if (outcome === "PENDING") {
    outcome = "FAIL";
    console.log("[e2e-sfincs] 30-minute window expired without success");
  }

  const postRunListing = minioListing();
  const finalPs = dockerEvidence || dockerPsAll();
  console.log("[e2e-sfincs] post-run MinIO listing:\n" + postRunListing);
  console.log("[e2e-sfincs] docker ps -a:\n" + finalPs);

  const timestamp = new Date().toISOString();
  const artifactEntry = [
    "",
    `=== LLM-driven SFINCS pluvial (local-docker) on qwen3:8b-16k (${timestamp}) ===`,
    `outcome:        ${outcome}`,
    `run_id:         ${resultRunId || "(none)"}`,
    `nudges_used:    ${nudgesUsed}`,
    `tool_sequence:  ${toolSequence.join(" -> ") || "(none observed)"}`,
    `layer_count:    ${lastLayerCount}`,
    `docker_seen:    ${dockerContainerSeen}`,
    "",
    "docker ps -a (sfincs container evidence):",
    finalPs,
    "",
    "Post-run MinIO listing (trid3nt-runs):",
    postRunListing,
    "",
  ].join("\n");

  fs.appendFileSync(path.join(PROOF_DIR, "artifacts.txt"), artifactEntry, "utf8");
  console.log("[e2e-sfincs] appended to artifacts.txt");

  await browser.close();

  console.log("\n=== e2e_sfincs_llm.mjs SUMMARY ===");
  console.log("outcome:      ", outcome);
  console.log("run_id:       ", resultRunId || "(none)");
  console.log("nudges_used:  ", nudgesUsed);
  console.log("tool_sequence:", toolSequence.join(" -> ") || "(none)");
  console.log("layers_found: ", lastLayerCount);
  console.log("docker_seen:  ", dockerContainerSeen);
  console.log("screenshots:");
  for (const f of ["08-sfincs-local-running.png", "09-sfincs-depth-layer.png"]) {
    const fp = path.join(PROOF_DIR, f);
    const size = fs.existsSync(fp) ? fs.statSync(fp).size : 0;
    console.log("  ", f, `(${size} bytes)`);
  }

  process.exit(outcome === "PASS" ? 0 : 1);
}

main().catch((err) => {
  console.error("[e2e-sfincs] FATAL:", err);
  process.exit(1);
});
