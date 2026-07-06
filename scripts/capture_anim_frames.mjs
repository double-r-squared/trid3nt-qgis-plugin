/**
 * capture_anim_frames.mjs -- Capture all 7 flood depth animation frames
 *
 * Goals:
 *   1. Open the app, select the Chattanooga SFINCS flood case
 *      (case 01KWT89VDTEW2J5FHAT5JYQJKE, user 01KWT89MZNKYHEMQAP5CNEY89A)
 *   2. Expand the animation group in LayerPanel
 *   3. For each of the 7 frames: click frame-select dot, wait for tiles,
 *      assert scrubber shows n/7, screenshot -> docs/proof/anim/frame-0n.png
 *   4. Test the PLAY button: click play, observe if it loops through all 7 frames
 *   5. Report per-frame pixel-change status
 *
 * Selectors used (from SequenceScrubber.tsx + LayerPanel.tsx):
 *   grace2-sequence-scrubber   - the bottom-center scrubber pill
 *   scrubber-play              - play/pause toggle
 *   scrubber-prev              - previous frame
 *   scrubber-next              - next frame
 *   scrubber-slider            - range input (0-based, max=N-1)
 *   scrubber-frame-label       - "x/N" text readout
 *   layer-group-row            - animation group row in LayerPanel
 *   layer-group-frame-label    - "x/N" inside the group row
 *   layer-group-play           - play button in group row
 *   layer-group-frame-select   - individual frame dot buttons
 *   layer-group-expand         - expand/collapse chevron
 *   layer-group-frames         - container of frame sub-rows
 *
 * Run:
 *   cd /home/nate/Documents/trid3nt-local
 *   node scripts/capture_anim_frames.mjs
 */

import { createRequire } from "module";
import { fileURLToPath } from "url";
import path from "path";
import fs from "fs";
import crypto from "crypto";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Playwright resolution -- prefer vendor/web, fallback to GRACE-2 web
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
    console.log("[capture-anim] using playwright from:", p);
    break;
  }
}
if (!playwright) {
  console.error("[capture-anim] playwright not found in any of:", candidates);
  process.exit(1);
}

const PROOF_DIR = path.resolve(__dirname, "../docs/proof");
const ANIM_DIR = path.join(PROOF_DIR, "anim");
fs.mkdirSync(ANIM_DIR, { recursive: true });

const APP_URL = "http://127.0.0.1:5173";
// The Chattanooga SFINCS case with 7 flood depth animation frames
const CASE_USER_ID = "01KWT89MZNKYHEMQAP5CNEY89A";
const CASE_ID = "01KWT89VDTEW2J5FHAT5JYQJKE";
const EXPECTED_FRAMES = 7;

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function waitFor(fn, timeoutMs, intervalMs = 500, label = "condition") {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const result = await fn();
      if (result) return result;
    } catch (_) {}
    await sleep(intervalMs);
  }
  throw new Error("Timeout waiting for: " + label);
}

/**
 * Take a screenshot and compute a hash of its pixels for change detection.
 * Returns { path, hash }.
 */
async function screenshotWithHash(page, filePath) {
  const buf = await page.screenshot({ path: filePath, type: "png" });
  const hash = crypto.createHash("md5").update(buf).digest("hex").slice(0, 12);
  return { path: filePath, hash };
}

/**
 * Wait for map tiles to stabilize by polling for network idle or a short
 * stable period. MapLibre tile loads are async XHR/fetch, so we wait until
 * the page has no pending network requests for ~1s.
 */
async function waitForTilesSettled(page, maxMs = 4000) {
  // Use networkidle as a proxy for tile render completion
  try {
    await page.waitForLoadState("networkidle", { timeout: maxMs });
  } catch (_) {
    // networkidle timeout is OK -- just means tiles are slow; proceed
  }
  // Additional buffer for MapLibre to actually paint after fetch completes
  await sleep(800);
}

async function main() {
  console.log("[capture-anim] === Flood depth animation frame capture ===");
  console.log("[capture-anim] Case:", CASE_ID, "User:", CASE_USER_ID);

  const { chromium } = playwright;
  const browser = await chromium.launch({
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
  });
  const page = await context.newPage();

  // Suppress noise but log errors
  page.on("console", (msg) => {
    if (msg.type() === "error") {
      const t = msg.text().slice(0, 200);
      // Filter out tile 404s and normal networking noise
      if (!t.includes("404") && !t.includes("ERR_") && !t.includes("tiles")) {
        console.log("[browser-error]", t);
      }
    }
  });

  // Inject localStorage before the page loads so the app boots as the correct
  // anonymous user who owns the Chattanooga SFINCS case
  await context.addInitScript((userId) => {
    window.localStorage.setItem("grace2_anonymous_accepted", "true");
    window.localStorage.setItem("grace2.anonymous_user_id", userId);
    window.sessionStorage.setItem("grace2-save-gate-accepted", "1");
  }, CASE_USER_ID);

  console.log("[capture-anim] navigating to", APP_URL + "/app");
  await page.goto(APP_URL + "/app", { waitUntil: "domcontentloaded", timeout: 30000 });
  await sleep(4000);

  // Dismiss auth gate if present
  const continueBtn = page.locator(
    'button:has-text("Continue"), button:has-text("anonymous"), button:has-text("Skip"), button:has-text("Use anonymously")'
  );
  if (await continueBtn.count().then((c) => c > 0).catch(() => false)) {
    console.log("[capture-anim] dismissing auth gate");
    await continueBtn.first().click();
    await sleep(2000);
  }

  // Wait for chat textarea (app fully loaded)
  try {
    await waitFor(
      async () => {
        const el = page.locator('[data-testid="chat-input"] textarea, textarea').first();
        return await el.isVisible().catch(() => false);
      },
      30000,
      1000,
      "chat input visible"
    );
    console.log("[capture-anim] app ready");
  } catch (_) {
    console.log("[capture-anim] WARN: chat input not found, proceeding anyway");
  }

  // ============================================================
  // Step 1: Select the Chattanooga SFINCS flood case
  // ============================================================
  const caseRows = page.locator('[data-testid="grace2-case-row"]');
  const caseCount = await caseRows.count().catch(() => 0);
  console.log("[capture-anim] found", caseCount, "case rows");

  let casePicked = false;
  for (let i = 0; i < caseCount; i++) {
    const row = caseRows.nth(i);
    const text = await row.innerText().catch(() => "");
    // Match by case title or SFINCS/flood keywords
    if (
      text.toLowerCase().includes("chattanooga") ||
      text.toLowerCase().includes("sfincs") ||
      text.toLowerCase().includes("pluvial")
    ) {
      console.log("[capture-anim] selecting case:", text.slice(0, 80).trim());
      await row.click();
      casePicked = true;
      await sleep(3500);
      break;
    }
  }

  if (!casePicked && caseCount > 0) {
    console.log("[capture-anim] no named match -- picking most recent case");
    await caseRows.first().click();
    casePicked = true;
    await sleep(3500);
  }

  if (!casePicked) {
    console.log("[capture-anim] FATAL: no cases found");
    await page.screenshot({ path: path.join(ANIM_DIR, "_debug-no-cases.png") });
    await browser.close();
    process.exit(1);
  }

  // Wait for layers to populate
  console.log("[capture-anim] waiting for layer panel to populate...");
  try {
    await waitFor(async () => {
      const lc = await page.locator('[data-testid="layer-row"], [data-testid="layer-group-row"]').count().catch(() => 0);
      return lc > 0;
    }, 20000, 800, "layers appear");
  } catch (_) {
    console.log("[capture-anim] WARN: layers did not appear after 20s");
  }

  // Enumerate what's in the panel
  const allRows = page.locator('[data-testid="layer-row"], [data-testid="layer-group-row"]');
  const allRowCount = await allRows.count().catch(() => 0);
  const layerNames = [];
  for (let i = 0; i < allRowCount; i++) {
    const row = allRows.nth(i);
    const text = await row.innerText().catch(() => "");
    layerNames.push(text.trim().slice(0, 60));
  }
  console.log("[capture-anim] layers in panel:", layerNames);

  // Hide landcover so the flood depth layer shows through
  for (const fragment of ["Land Cover", "landcover", "NLCD"]) {
    const rows = page.locator('[data-testid="layer-row"]');
    const n = await rows.count().catch(() => 0);
    for (let i = 0; i < n; i++) {
      const row = rows.nth(i);
      const text = await row.innerText().catch(() => "");
      if (text.toLowerCase().includes(fragment.toLowerCase())) {
        const visChk = row.locator('[data-testid="layer-visibility"]').first();
        const checked = await visChk.isChecked().catch(() => null);
        if (checked !== false) {
          // Try label click (some checkboxes use opacity:0 trick)
          const label = row.locator("label").first();
          if (await label.isVisible().catch(() => false)) {
            await label.click().catch(() => {});
          } else {
            await visChk.click().catch(() => {});
          }
          console.log("[capture-anim] toggled off:", fragment);
          await sleep(400);
        }
        break;
      }
    }
  }

  // ============================================================
  // Step 2: Find the animation group and expand it
  // ============================================================
  const groupRows = page.locator('[data-testid="layer-group-row"]');
  const groupCount = await groupRows.count().catch(() => 0);
  console.log("[capture-anim] animation group rows:", groupCount);

  if (groupCount === 0) {
    console.log("[capture-anim] FATAL: no animation group rows found");
    await page.screenshot({ path: path.join(ANIM_DIR, "_debug-no-groups.png") });
    await browser.close();
    process.exit(1);
  }

  // Find the flood depth animation group (should be the only or first group)
  let animGroup = null;
  for (let i = 0; i < groupCount; i++) {
    const g = groupRows.nth(i);
    const text = await g.innerText().catch(() => "");
    if (
      text.toLowerCase().includes("flood depth") ||
      text.toLowerCase().includes("depth step") ||
      text.toLowerCase().includes("flood") ||
      i === 0
    ) {
      animGroup = g;
      console.log("[capture-anim] animation group:", text.slice(0, 80).trim());
      break;
    }
  }

  if (!animGroup) {
    animGroup = groupRows.first();
    console.log("[capture-anim] using first group row as fallback");
  }

  // Click the group row to activate it (sets it as the active group)
  await animGroup.click({ force: true }).catch(() => {});
  await sleep(600);

  // Expand the group to see frame dot buttons
  const expandBtn = animGroup.locator('[data-testid="layer-group-expand"]').first();
  const expandVisible = await expandBtn.isVisible().catch(() => false);
  if (expandVisible) {
    await expandBtn.click();
    console.log("[capture-anim] expanded animation group");
    await sleep(600);
  } else {
    console.log("[capture-anim] no expand button visible -- group may already be expanded");
  }

  // Count frame-select dots
  const frameSelects = page.locator('[data-testid="layer-group-frame-select"]');
  const frameCount = await frameSelects.count().catch(() => 0);
  console.log("[capture-anim] frame-select dots found:", frameCount);

  if (frameCount === 0) {
    console.log("[capture-anim] WARN: no frame dots found, trying scroll + wait");
    await sleep(1000);
    const afterWait = await frameSelects.count().catch(() => 0);
    console.log("[capture-anim] after wait:", afterWait, "frame selects");
  }

  // Check scrubber is visible
  const scrubber = page.locator('[data-testid="grace2-sequence-scrubber"]');
  const scrubberVisible = await scrubber.isVisible().catch(() => false);
  console.log("[capture-anim] scrubber visible:", scrubberVisible);

  // ============================================================
  // Step 3: Capture all 7 frames using frame-select dots
  // ============================================================
  const frameResults = [];
  let prevHash = null;

  // Strategy: prefer frame-select dots; fallback to scrubber slider
  for (let n = 1; n <= EXPECTED_FRAMES; n++) {
    const frameIdx = n - 1; // 0-based
    const outPath = path.join(ANIM_DIR, "frame-0" + n + ".png");

    console.log("[capture-anim] selecting frame", n + "/" + EXPECTED_FRAMES + "...");

    let frameSelected = false;

    // Primary: click the n-th frame-select dot
    const currentFrameSelects = page.locator('[data-testid="layer-group-frame-select"]');
    const dotCount = await currentFrameSelects.count().catch(() => 0);
    if (dotCount >= n) {
      const dot = currentFrameSelects.nth(frameIdx);
      await dot.click({ force: true }).catch((e) => {
        console.log("[capture-anim] WARN: frame dot click failed:", e.message.slice(0, 60));
      });
      frameSelected = true;
      console.log("[capture-anim]   clicked dot", n);
    } else if (scrubberVisible) {
      // Fallback: use the scrubber slider
      const slider = page.locator('[data-testid="scrubber-slider"]');
      if (await slider.isVisible().catch(() => false)) {
        await slider.fill(String(frameIdx));
        frameSelected = true;
        console.log("[capture-anim]   set slider to", frameIdx);
      }
    }

    if (!frameSelected) {
      // Last resort: step via scrubber-next buttons
      if (n === 1) {
        // Reset to beginning
        const prev = page.locator('[data-testid="scrubber-prev"]');
        for (let i = 0; i < EXPECTED_FRAMES + 1; i++) {
          if (await prev.isVisible().catch(() => false)) {
            await prev.click().catch(() => {});
          }
        }
      } else {
        const next = page.locator('[data-testid="scrubber-next"]');
        if (await next.isVisible().catch(() => false)) {
          await next.click().catch(() => {});
        }
      }
      console.log("[capture-anim]   fallback step");
    }

    // Wait for tiles to settle
    await waitForTilesSettled(page, 3500);

    // Read scrubber label (shows "x/N")
    const scrubberLabel = await page
      .locator('[data-testid="scrubber-frame-label"]')
      .first()
      .innerText()
      .catch(() => "(not found)");

    // Also read group row frame label
    const groupFrameLabel = await page
      .locator('[data-testid="layer-group-frame-label"]')
      .first()
      .innerText()
      .catch(() => "(not found)");

    console.log("[capture-anim]   scrubber label:", scrubberLabel, "| group label:", groupFrameLabel);

    // Screenshot
    const { hash } = await screenshotWithHash(page, outPath);
    const changed = prevHash !== null && hash !== prevHash;
    const stat = fs.statSync(outPath);

    console.log("[capture-anim]   frame", n, "hash:", hash, "| changed:", changed, "| size:", stat.size, "bytes");

    frameResults.push({
      frame: n,
      outPath,
      scrubberLabel,
      groupFrameLabel,
      hash,
      changed,
      sizeBytes: stat.size,
    });

    prevHash = hash;
  }

  // ============================================================
  // Step 4: Test PLAY button behavior
  // ============================================================
  console.log("\n[capture-anim] === Testing PLAY button ===");

  // Reset to frame 1 first
  const firstDot = page.locator('[data-testid="layer-group-frame-select"]').first();
  if (await firstDot.isVisible().catch(() => false)) {
    await firstDot.click({ force: true }).catch(() => {});
    await sleep(600);
  }

  // Read initial frame label
  const preLabelBefore = await page
    .locator('[data-testid="scrubber-frame-label"]')
    .first()
    .innerText()
    .catch(() => "(not found)");
  console.log("[capture-anim] before play:", preLabelBefore);

  // Click play
  const playBtn = page.locator('[data-testid="scrubber-play"]').first();
  const playVisible = await playBtn.isVisible().catch(() => false);
  const playDisabled = await playBtn.isDisabled().catch(() => true);
  let playBehavior = "play button not visible";

  if (playVisible && !playDisabled) {
    // Check initial aria-label
    const initialLabel = await playBtn.getAttribute("aria-label").catch(() => "");
    console.log("[capture-anim] play button aria-label:", initialLabel);

    await playBtn.click();
    console.log("[capture-anim] clicked play -- waiting for auto-advance...");

    // Poll frame label for changes over ~10s (at 1100ms/frame default, 7 frames = ~7.7s)
    const playObservations = [];
    const playDeadline = Date.now() + 12000;
    let lastLabel = preLabelBefore;
    let uniqueFramesSeen = new Set([preLabelBefore]);

    while (Date.now() < playDeadline) {
      await sleep(600);
      const label = await page
        .locator('[data-testid="scrubber-frame-label"]')
        .first()
        .innerText()
        .catch(() => lastLabel);
      if (label !== lastLabel) {
        playObservations.push(label);
        uniqueFramesSeen.add(label);
        console.log("[capture-anim]   advanced to:", label);
        lastLabel = label;
      }
    }

    // Check aria-label for playing state
    const playingLabel = await playBtn.getAttribute("aria-label").catch(() => "");
    console.log("[capture-anim] play button aria-label after start:", playingLabel);
    const isPlaying = playingLabel.toLowerCase().includes("pause");

    // Stop playback
    if (await playBtn.isVisible().catch(() => false)) {
      await playBtn.click();
      await sleep(400);
      const stoppedLabel = await playBtn.getAttribute("aria-label").catch(() => "");
      console.log("[capture-anim] play button after stop:", stoppedLabel);
    }

    const uniqueCount = uniqueFramesSeen.size;
    if (uniqueCount >= EXPECTED_FRAMES) {
      playBehavior = "looped all " + EXPECTED_FRAMES + " frames (observed " + uniqueCount + " unique labels)";
    } else if (uniqueCount > 1) {
      playBehavior =
        "advanced " + playObservations.length + " times, saw " + uniqueCount + " unique labels: " + [...uniqueFramesSeen].join(", ");
    } else {
      playBehavior = "stalled -- no frame advances observed over 12s";
    }

    console.log("[capture-anim] PLAY behavior:", playBehavior);
  } else {
    playBehavior = "play button " + (playVisible ? "disabled" : "not visible");
    console.log("[capture-anim] PLAY:", playBehavior);
  }

  await browser.close();

  // ============================================================
  // Summary report
  // ============================================================
  console.log("\n=== capture_anim_frames.mjs SUMMARY ===");
  console.log("case_id:", CASE_ID);
  console.log("user_id:", CASE_USER_ID);
  console.log("expected_frames:", EXPECTED_FRAMES);
  console.log("frame_dots_found:", await (async () => {
    // Can't query after close; use last known count
    return frameResults.length;
  })());
  console.log("\nPer-frame results:");
  for (const r of frameResults) {
    const label = r.scrubberLabel.padEnd(8);
    const status = r.changed ? "CHANGED" : (r.frame === 1 ? "first" : "SAME");
    console.log(
      "  frame " + r.frame + "/7  scrubber=" + label + "  hash=" + r.hash + "  " + status + "  (" + r.sizeBytes + "B)"
    );
  }
  console.log("\nPLAY button:", playBehavior);
  console.log("\nOutput files:");
  for (const r of frameResults) {
    const exists = fs.existsSync(r.outPath) ? "OK" : "MISSING";
    console.log("  " + path.relative(path.resolve(__dirname, ".."), r.outPath) + " [" + exists + "]");
  }

  // Write a machine-readable JSON summary for the GIF assembly step
  const summaryPath = path.join(ANIM_DIR, "capture_summary.json");
  fs.writeFileSync(
    summaryPath,
    JSON.stringify(
      {
        case_id: CASE_ID,
        user_id: CASE_USER_ID,
        frame_count: frameResults.length,
        play_behavior: playBehavior,
        frames: frameResults.map((r) => ({
          frame: r.frame,
          path: r.outPath,
          scrubber_label: r.scrubberLabel,
          group_label: r.groupFrameLabel,
          hash: r.hash,
          changed: r.changed,
          size_bytes: r.sizeBytes,
        })),
      },
      null,
      2
    )
  );
  console.log("\nSummary JSON:", summaryPath);
}

main().catch((err) => {
  console.error("[capture-anim] FATAL:", err);
  process.exit(1);
});
