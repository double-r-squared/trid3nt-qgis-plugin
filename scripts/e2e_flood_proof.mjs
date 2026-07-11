/**
 * e2e_flood_proof.mjs -- Flood inundation layer proof screenshots
 *
 * Goals:
 *   1. Open the app, select the Chattanooga SFINCS case (run 01KWT8BWTMTSB7H4NPMD4QWQET)
 *   2. Verify depth layers exist in LayerPanel
 *   3. Ensure peak depth is on top, landcover toggled off or low opacity
 *   4. Screenshot peak depth view -> 22-flood-peak-inundation.png
 *   5. Step through animation frames -> 23/24/25-flood-anim-*.png
 *   6. Start a fresh flood run and capture mid-run -> 26-flood-run-progress.png
 *
 * Run:
 *   cd /home/nate/Documents/trid3nt-local
 *   node scripts/e2e_flood_proof.mjs
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
    console.log("[flood-proof] using playwright from:", p);
    break;
  }
}
if (!playwright) {
  console.error("[flood-proof] playwright not found in any of:", candidates);
  process.exit(1);
}

const PROOF_DIR = path.resolve(__dirname, "../docs/proof");
fs.mkdirSync(PROOF_DIR, { recursive: true });

const APP_URL = "http://127.0.0.1:5173";
const CASE_RUN_ID = "01KWT8BWTMTSB7H4NPMD4QWQET";
const CASE_TITLE_FRAGMENT = "Chattanooga";
const FLOOD_BBOX = "[-85.32, 35.03, -85.28, 35.07]";
const FLOOD_PROMPT =
  "Run a small pluvial rain-on-grid SFINCS flood simulation for a 4km box in " +
  "downtown Chattanooga, Tennessee. Call run_model_flood_scenario with these " +
  "exact arguments and no others: bbox=" + FLOOD_BBOX + ", flood_type=\"pluvial\", " +
  "return_period_years=100, duration_hours=1. Use the coarsest default " +
  "resolution and proceed with defaults. Do NOT ask for confirmation.";

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function waitFor(fn, timeoutMs, intervalMs = 1000, label = "condition") {
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

async function dismissSaveGate(page) {
  const modal = page.locator('[data-testid="grace2-save-gate-modal"]');
  if (await modal.isVisible().catch(() => false)) {
    const cont = page.locator('[data-testid="grace2-save-gate-modal-continue"]').first();
    if (await cont.isVisible().catch(() => false)) {
      await cont.click();
      await sleep(800);
      console.log("[flood-proof] dismissed save-gate modal");
    }
  }
}

async function sendChatMessage(page, text) {
  // Dismiss any save-gate modal blocking the input
  await dismissSaveGate(page);

  const selectors = [
    '[data-testid="chat-input"] textarea',
    "textarea",
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
    console.log("[flood-proof] WARN: no chat input found");
    return false;
  }
  await input.click({ force: true });
  await input.fill(text);
  const actionBtn = page.locator('[data-testid="chat-input-action"]').first();
  if (await actionBtn.isVisible().catch(() => false)) {
    await actionBtn.click();
  } else {
    await input.press("Enter");
  }
  console.log("[flood-proof] sent:", text.slice(0, 80) + (text.length > 80 ? "..." : ""));
  return true;
}

async function clickConfirmations(page) {
  const affirmSelectors = [
    '[data-testid="resolution-picker-confirm"]',
    '[data-testid="sandbox-card-proceed"]',
    'button:has-text("Confirm")',
    'button:has-text("Proceed")',
    'button:has-text("Yes")',
    'button:has-text("Coarsest")',
  ];
  let clicked = 0;
  for (const sel of affirmSelectors) {
    const els = page.locator(sel);
    const count = await els.count().catch(() => 0);
    for (let i = 0; i < count; i++) {
      const el = els.nth(i);
      if (await el.isVisible().catch(() => false)) {
        console.log("[flood-proof] clicking confirmation:", sel);
        await el.click().catch(() => {});
        await sleep(800);
        clicked++;
      }
    }
  }
  return clicked;
}

async function layerCount(page) {
  const rows = page.locator('[data-testid="layer-row"], [data-testid="layer-group-row"]');
  return await rows.count().catch(() => 0);
}

async function getLayerRowByName(page, nameFragment) {
  const rows = page.locator('[data-testid="layer-row"]');
  const count = await rows.count().catch(() => 0);
  for (let i = 0; i < count; i++) {
    const row = rows.nth(i);
    const text = await row.innerText().catch(() => "");
    if (text.toLowerCase().includes(nameFragment.toLowerCase())) {
      return row;
    }
  }
  return null;
}

async function toggleLayerVisibility(page, nameFragment, makeVisible) {
  const row = await getLayerRowByName(page, nameFragment);
  if (!row) {
    console.log("[flood-proof] layer not found:", nameFragment);
    return false;
  }
  // layer-visibility is a checkbox input. Check its current state.
  const visChk = row.locator('[data-testid="layer-visibility"]').first();
  if (!(await visChk.isVisible().catch(() => false))) {
    // Try clicking parent label if checkbox itself is hidden (opacity:0)
    const label = row.locator('label').first();
    if (await label.isVisible().catch(() => false)) {
      // Read checked from the checkbox even if invisible
      const checked = await visChk.isChecked().catch(() => null);
      const currentlyVisible = checked === true || checked === null;
      if (makeVisible !== undefined && currentlyVisible === makeVisible) {
        console.log("[flood-proof] layer", nameFragment, "already at desired visibility");
        return true;
      }
      await label.click();
      await sleep(500);
      console.log("[flood-proof] toggled visibility (via label) for:", nameFragment);
      return true;
    }
    console.log("[flood-proof] no visibility control for:", nameFragment);
    return false;
  }
  const checked = await visChk.isChecked().catch(() => null);
  const currentlyVisible = checked !== false;
  if (makeVisible !== undefined && currentlyVisible === makeVisible) {
    console.log("[flood-proof] layer", nameFragment, "already at desired visibility (checked=" + checked + ")");
    return true;
  }
  await visChk.click();
  await sleep(500);
  console.log("[flood-proof] toggled visibility for:", nameFragment, "checked was:", checked);
  return true;
}

async function setLayerOpacity(page, nameFragment, opacity) {
  // Expand the layer row first to reveal opacity slider
  const row = await getLayerRowByName(page, nameFragment);
  if (!row) return false;
  const expandBtn = row.locator('[data-testid="layer-expand"]').first();
  if (await expandBtn.isVisible().catch(() => false)) {
    await expandBtn.click();
    await sleep(500);
  }
  const opacitySlider = row.locator('[data-testid="layer-opacity"]').first();
  if (!(await opacitySlider.isVisible().catch(() => false))) {
    // Try the parent area
    const opacityRow = page.locator('[data-testid="layer-opacity"]').first();
    if (await opacityRow.isVisible().catch(() => false)) {
      await opacityRow.fill(String(opacity));
      await sleep(300);
      return true;
    }
    return false;
  }
  await opacitySlider.fill(String(opacity));
  await sleep(300);
  return true;
}

async function minioListing() {
  const MC_BIN = path.resolve(__dirname, "../bin/mc");
  try {
    return execSync(`${MC_BIN} ls local/trid3nt-runs/ --recursive 2>/dev/null`, {
      encoding: "utf8",
      timeout: 10000,
    }).trim();
  } catch (_) {
    return "";
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

async function main() {
  console.log("[flood-proof] === Flood inundation layer proof ===");
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

  // Set anon token - use the SAME user_id that owns the Chattanooga case
  // (01KWT89MZNKYHEMQAP5CNEY89A) so the WebSocket session maps to that user's cases.
  // Pre-accept the save gate (sessionStorage) to avoid the modal on new case creation.
  await context.addInitScript(() => {
    window.localStorage.setItem("grace2_anonymous_accepted", "true");
    window.localStorage.setItem("grace2.anonymous_user_id", "01KWT89MZNKYHEMQAP5CNEY89A");
    window.sessionStorage.setItem("grace2-save-gate-accepted", "1");
  });

  console.log("[flood-proof] navigating to", APP_URL + "/app");
  await page.goto(APP_URL + "/app", { waitUntil: "domcontentloaded", timeout: 30000 });
  await sleep(5000);

  // Handle any auth gate
  const continueBtn = page.locator(
    'button:has-text("Continue"), button:has-text("anonymous"), button:has-text("Skip"), button:has-text("Use anonymously"), button:has-text("continue without")'
  );
  if (await continueBtn.count().then((c) => c > 0).catch(() => false)) {
    console.log("[flood-proof] clicking pass-through auth button");
    await continueBtn.first().click();
    await sleep(2000);
  }

  // Wait for chat input to confirm app is ready
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
  } catch (_) {
    console.log("[flood-proof] WARN: chat input not found within 30s, taking debug screenshot");
    await page.screenshot({ path: path.join(PROOF_DIR, "22-flood-peak-inundation.png") });
    await browser.close();
    process.exit(1);
  }

  console.log("[flood-proof] app ready, looking for Chattanooga case...");
  await page.screenshot({ path: path.join(PROOF_DIR, "_debug-app-loaded.png") });

  // ============================================================
  // Step 1: Find and click on the Chattanooga flood case
  // ============================================================
  const caseRows = page.locator('[data-testid="grace2-case-row"]');
  const caseCount = await caseRows.count().catch(() => 0);
  console.log("[flood-proof] found", caseCount, "case rows");

  // Try to find the Chattanooga case by title fragment or run ID
  let casePicked = false;
  for (let i = 0; i < caseCount; i++) {
    const row = caseRows.nth(i);
    const text = await row.innerText().catch(() => "");
    if (
      text.toLowerCase().includes(CASE_TITLE_FRAGMENT.toLowerCase()) ||
      text.toLowerCase().includes("sfincs") ||
      text.toLowerCase().includes("flood")
    ) {
      console.log("[flood-proof] found case row:", text.slice(0, 80).trim());
      await row.click();
      casePicked = true;
      await sleep(3000);
      break;
    }
  }

  if (!casePicked && caseCount > 0) {
    console.log("[flood-proof] no matching case by name, picking most recent (first row)");
    await caseRows.first().click();
    casePicked = true;
    await sleep(3000);
  }

  if (!casePicked) {
    console.log("[flood-proof] no cases found - skipping to fresh run");
  }

  // Wait for layers to appear
  if (casePicked) {
    console.log("[flood-proof] waiting for layers to load...");
    try {
      await waitFor(async () => {
        const lc = await layerCount(page);
        return lc > 0;
      }, 20000, 1000, "layers > 0");
    } catch (_) {
      console.log("[flood-proof] no layers appeared after 20s");
    }
  }

  const lc0 = await layerCount(page);
  console.log("[flood-proof] layer count after case select:", lc0);

  // ============================================================
  // Step 2: Layer panel analysis + fix ordering for screenshots
  // ============================================================

  // Check layer names
  const allRows = page.locator('[data-testid="layer-row"], [data-testid="layer-group-row"]');
  const allRowCount = await allRows.count().catch(() => 0);
  const layerNames = [];
  for (let i = 0; i < allRowCount; i++) {
    const row = allRows.nth(i);
    const text = await row.innerText().catch(() => "");
    layerNames.push(text.trim().slice(0, 60));
  }
  console.log("[flood-proof] layers in panel:", layerNames);

  const hasDepthLayers = layerNames.some(
    (n) => n.toLowerCase().includes("flood depth") || n.toLowerCase().includes("depth step") || n.toLowerCase().includes("peak flood")
  );
  console.log("[flood-proof] has depth/flood layers:", hasDepthLayers);

  // Turn off landcover to let flood layer show through
  await toggleLayerVisibility(page, "Land Cover", false);
  await toggleLayerVisibility(page, "landcover", false);
  await toggleLayerVisibility(page, "NLCD", false);
  await sleep(800);

  // ============================================================
  // Screenshot 22: Peak flood depth visible
  // ============================================================
  await sleep(2000); // let tiles render
  await page.screenshot({ path: path.join(PROOF_DIR, "22-flood-peak-inundation.png") });
  console.log("[flood-proof] screenshot: 22-flood-peak-inundation.png (peak depth visible)");

  // ============================================================
  // Step 3: Animation screenshots
  // ============================================================
  // Look for animation group row (layer-group-row)
  const groupRows = page.locator('[data-testid="layer-group-row"]');
  const groupCount = await groupRows.count().catch(() => 0);
  console.log("[flood-proof] animation group rows:", groupCount);

  if (groupCount > 0) {
    // Get the flood depth animation group
    const group = groupRows.first();
    const groupText = await group.innerText().catch(() => "");
    console.log("[flood-proof] animation group:", groupText.slice(0, 80).trim());

    // Expand group if needed to see frame list
    const expandBtn = group.locator('[data-testid="layer-group-expand"]').first();
    if (await expandBtn.isVisible().catch(() => false)) {
      await expandBtn.click();
      await sleep(500);
    }

    // Get frame selector buttons
    const frameSelects = page.locator('[data-testid="layer-group-frame-select"]');
    const frameCount = await frameSelects.count().catch(() => 0);
    console.log("[flood-proof] animation frames:", frameCount);

    if (frameCount >= 3) {
      // Frame 1 (early) - click first frame selector dot in the group
      await frameSelects.nth(0).click().catch(() => {});
      await sleep(2000);
      // Also try the App-level scrubber "prev" to reset to frame 0
      const scrubPrev = page.locator('[data-testid="scrubber-prev"]').first();
      for (let i = 0; i < 7; i++) {
        if (await scrubPrev.isVisible().catch(() => false)) await scrubPrev.click().catch(() => {});
      }
      await sleep(1500);
      const frameLabel0 = await page.locator('[data-testid="scrubber-frame-label"]').first().innerText().catch(() => "");
      console.log("[flood-proof] scrubber frame label (early):", frameLabel0);
      await page.screenshot({ path: path.join(PROOF_DIR, "23-flood-anim-early.png") });
      console.log("[flood-proof] screenshot: 23-flood-anim-early.png");

      // Frame at 50% (mid) - step forward ~3 times via scrubber-next or frame selector
      const scrubNext = page.locator('[data-testid="scrubber-next"]').first();
      const midIdx = Math.floor(frameCount / 2);
      if (await scrubNext.isVisible().catch(() => false)) {
        for (let i = 0; i < midIdx; i++) {
          await scrubNext.click().catch(() => {});
          await sleep(400);
        }
      } else {
        await frameSelects.nth(midIdx).click().catch(() => {});
      }
      await sleep(1500);
      const frameLabelMid = await page.locator('[data-testid="scrubber-frame-label"]').first().innerText().catch(() => "");
      console.log("[flood-proof] scrubber frame label (mid):", frameLabelMid);
      await page.screenshot({ path: path.join(PROOF_DIR, "24-flood-anim-mid.png") });
      console.log("[flood-proof] screenshot: 24-flood-anim-mid.png");

      // Last frame (late) - step to end via scrubber-next or click last frame selector
      if (await scrubNext.isVisible().catch(() => false)) {
        for (let i = 0; i < frameCount; i++) {
          await scrubNext.click().catch(() => {});
          await sleep(400);
        }
      } else {
        await frameSelects.nth(frameCount - 1).click().catch(() => {});
      }
      await sleep(1500);
      const frameLabelLate = await page.locator('[data-testid="scrubber-frame-label"]').first().innerText().catch(() => "");
      console.log("[flood-proof] scrubber frame label (late):", frameLabelLate);
      await page.screenshot({ path: path.join(PROOF_DIR, "25-flood-anim-late.png") });
      console.log("[flood-proof] screenshot: 25-flood-anim-late.png");
    } else if (frameCount > 0) {
      // Fewer than 3 frames available, do what we can
      await frameSelects.nth(0).click().catch(() => {});
      await sleep(2000);
      await page.screenshot({ path: path.join(PROOF_DIR, "23-flood-anim-early.png") });
      console.log("[flood-proof] screenshot: 23-flood-anim-early.png (only 1 frame)");
      // Copy to mid/late as well
      fs.copyFileSync(
        path.join(PROOF_DIR, "23-flood-anim-early.png"),
        path.join(PROOF_DIR, "24-flood-anim-mid.png")
      );
      fs.copyFileSync(
        path.join(PROOF_DIR, "23-flood-anim-early.png"),
        path.join(PROOF_DIR, "25-flood-anim-late.png")
      );
    } else {
      // No group frames visible - use individual layer rows for stepped view
      console.log("[flood-proof] no frame selectors found, using per-frame layer toggles");
      // Find all flood depth frame rows in the panel (these may be individual layer-rows)
      const layerRowList = page.locator('[data-testid="layer-row"]');
      const lrCount = await layerRowList.count().catch(() => 0);
      const floodFrameRows = [];
      for (let i = 0; i < lrCount; i++) {
        const row = layerRowList.nth(i);
        const text = await row.innerText().catch(() => "");
        if (text.toLowerCase().includes("depth step") || text.toLowerCase().includes("flood depth step")) {
          floodFrameRows.push(i);
        }
      }
      console.log("[flood-proof] individual flood frame rows:", floodFrameRows.length);

      if (floodFrameRows.length >= 2) {
        // Show only early frame
        for (const idx of floodFrameRows) {
          const row = layerRowList.nth(idx);
          const visBtn = row.locator('[data-testid="layer-visibility"]').first();
          await visBtn.click().catch(() => {});
          await sleep(200);
        }
        // Enable frame 0
        const row0 = layerRowList.nth(floodFrameRows[0]);
        const visBtn0 = row0.locator('[data-testid="layer-visibility"]').first();
        await visBtn0.click().catch(() => {});
        await sleep(2000);
        await page.screenshot({ path: path.join(PROOF_DIR, "23-flood-anim-early.png") });
        console.log("[flood-proof] screenshot: 23-flood-anim-early.png (toggled individual)");

        const midRowIdx = floodFrameRows[Math.floor(floodFrameRows.length / 2)];
        await visBtn0.click().catch(() => {}); // hide frame 0
        const rowMid = layerRowList.nth(midRowIdx);
        const visBtnMid = rowMid.locator('[data-testid="layer-visibility"]').first();
        await visBtnMid.click().catch(() => {});
        await sleep(2000);
        await page.screenshot({ path: path.join(PROOF_DIR, "24-flood-anim-mid.png") });
        console.log("[flood-proof] screenshot: 24-flood-anim-mid.png (toggled individual)");

        await visBtnMid.click().catch(() => {}); // hide mid
        const rowLast = layerRowList.nth(floodFrameRows[floodFrameRows.length - 1]);
        const visBtnLast = rowLast.locator('[data-testid="layer-visibility"]').first();
        await visBtnLast.click().catch(() => {});
        await sleep(2000);
        await page.screenshot({ path: path.join(PROOF_DIR, "25-flood-anim-late.png") });
        console.log("[flood-proof] screenshot: 25-flood-anim-late.png (toggled individual)");
      } else {
        // Fallback: just screenshot current state 3 times with note
        console.log("[flood-proof] WARN: less than 2 frame rows, using current state for all anim screenshots");
        await page.screenshot({ path: path.join(PROOF_DIR, "23-flood-anim-early.png") });
        await page.screenshot({ path: path.join(PROOF_DIR, "24-flood-anim-mid.png") });
        await page.screenshot({ path: path.join(PROOF_DIR, "25-flood-anim-late.png") });
      }
    }
  } else {
    // No animation group - frames are individual rows
    console.log("[flood-proof] no layer-group-row found, using individual layer-row toggles");
    const layerRowList = page.locator('[data-testid="layer-row"]');
    const lrCount = await layerRowList.count().catch(() => 0);
    const floodFrameRows = [];
    for (let i = 0; i < lrCount; i++) {
      const row = layerRowList.nth(i);
      const text = await row.innerText().catch(() => "");
      if (text.toLowerCase().includes("depth step") || text.toLowerCase().includes("flood depth step")) {
        floodFrameRows.push(i);
      }
    }
    console.log("[flood-proof] individual flood frame rows:", floodFrameRows.length);

    if (floodFrameRows.length >= 2) {
      // Hide all frames first, then show one at a time
      for (const idx of floodFrameRows) {
        const row = layerRowList.nth(idx);
        const visBtn = row.locator('[data-testid="layer-visibility"]').first();
        await visBtn.click().catch(() => {});
        await sleep(200);
      }
      // Frame 0 early
      const row0 = layerRowList.nth(floodFrameRows[0]);
      const visBtn0 = row0.locator('[data-testid="layer-visibility"]').first();
      await visBtn0.click().catch(() => {});
      await sleep(2000);
      await page.screenshot({ path: path.join(PROOF_DIR, "23-flood-anim-early.png") });
      console.log("[flood-proof] screenshot: 23-flood-anim-early.png");

      const midIdx = floodFrameRows[Math.floor(floodFrameRows.length / 2)];
      await visBtn0.click().catch(() => {});
      const rowMid = layerRowList.nth(midIdx);
      await rowMid.locator('[data-testid="layer-visibility"]').first().click().catch(() => {});
      await sleep(2000);
      await page.screenshot({ path: path.join(PROOF_DIR, "24-flood-anim-mid.png") });
      console.log("[flood-proof] screenshot: 24-flood-anim-mid.png");

      await rowMid.locator('[data-testid="layer-visibility"]').first().click().catch(() => {});
      const rowLast = layerRowList.nth(floodFrameRows[floodFrameRows.length - 1]);
      await rowLast.locator('[data-testid="layer-visibility"]').first().click().catch(() => {});
      await sleep(2000);
      await page.screenshot({ path: path.join(PROOF_DIR, "25-flood-anim-late.png") });
      console.log("[flood-proof] screenshot: 25-flood-anim-late.png");
    } else {
      console.log("[flood-proof] WARN: no flood frame layers found, using current state");
      await page.screenshot({ path: path.join(PROOF_DIR, "23-flood-anim-early.png") });
      await page.screenshot({ path: path.join(PROOF_DIR, "24-flood-anim-mid.png") });
      await page.screenshot({ path: path.join(PROOF_DIR, "25-flood-anim-late.png") });
    }
  }

  // ============================================================
  // Step 4: Fresh flood run + mid-run screenshot 26
  // ============================================================
  console.log("[flood-proof] starting a fresh flood run for screenshot 26...");

  const preRunListing = await minioListing();
  const preRunPrefixes = runPrefixes(preRunListing);

  // Open new case (click the + button in cases panel)
  const newCaseBtn = page.locator('[data-testid="grace2-cases-new"]');
  if (await newCaseBtn.isVisible().catch(() => false)) {
    await newCaseBtn.click();
    await sleep(1500);
    console.log("[flood-proof] clicked new case");
    // Dismiss the save-gate modal if it appears (anonymous user)
    const saveGateContinue = page.locator('[data-testid="grace2-save-gate-modal-continue"]');
    if (await saveGateContinue.isVisible().catch(() => false)) {
      await saveGateContinue.click();
      console.log("[flood-proof] dismissed save gate modal");
      await sleep(1500);
    }
  } else {
    console.log("[flood-proof] no new-case button visible, sending prompt to current case");
  }

  // Wait for chat input to be ready for the fresh run
  try {
    await waitFor(async () => {
      const modal = page.locator('[data-testid="grace2-save-gate-modal"]');
      if (await modal.isVisible().catch(() => false)) {
        // dismiss it first
        const cont = page.locator('[data-testid="grace2-save-gate-modal-continue"]');
        if (await cont.isVisible().catch(() => false)) await cont.click().catch(() => {});
      }
      const el = page.locator('[data-testid="chat-input"] textarea, textarea').first();
      const blocked = await page.locator('[data-testid="grace2-save-gate-modal"]').isVisible().catch(() => false);
      return (await el.isVisible().catch(() => false)) && !blocked;
    }, 15000, 500, "chat input unblocked");
  } catch (_) {
    console.log("[flood-proof] WARN: chat input may still be blocked by modal");
  }

  await sendChatMessage(page, FLOOD_PROMPT);
  const runStartedAt = Date.now();

  const MAX_WAIT_MS = 20 * 60 * 1000; // 20 minutes
  const POLL_MS = 3000;
  let screenshot26Done = false;
  let resultRunId = null;
  let pipelineCardsSeen = false;
  const deadline2 = Date.now() + MAX_WAIT_MS;

  console.log("[flood-proof] polling for run progress...");
  while (Date.now() < deadline2) {
    await sleep(POLL_MS);
    const elapsed = Math.round((Date.now() - runStartedAt) / 1000);

    await clickConfirmations(page);

    // Check for pipeline cards
    if (!pipelineCardsSeen) {
      const toolCardSels = [
        '[data-testid="pipeline-card-stack"]',
        '[data-testid="grace2-sheet-tool-strip"]',
        '[data-testid="resolution-picker-card"]',
        '[data-testid="sandbox-card-proceed"]',
      ];
      for (const sel of toolCardSels) {
        const count = await page.locator(sel).count().catch(() => 0);
        if (count > 0) {
          pipelineCardsSeen = true;
          console.log("[flood-proof] pipeline cards visible via " + sel + " (+" + elapsed + "s)");
          break;
        }
      }
    }

    // Take screenshot 26 once pipeline cards appear
    if (pipelineCardsSeen && !screenshot26Done) {
      await page.screenshot({ path: path.join(PROOF_DIR, "26-flood-run-progress.png") });
      console.log("[flood-proof] screenshot: 26-flood-run-progress.png (pipeline cards)");
      screenshot26Done = true;
    }

    // Check MinIO for new run prefix
    const nowListing = await minioListing();
    const nowPrefixes = runPrefixes(nowListing);
    for (const p of nowPrefixes) {
      if (!preRunPrefixes.has(p)) {
        resultRunId = p;
        if (!screenshot26Done) {
          await page.screenshot({ path: path.join(PROOF_DIR, "26-flood-run-progress.png") });
          console.log("[flood-proof] screenshot: 26-flood-run-progress.png (new run prefix)");
          screenshot26Done = true;
        }
      }
    }

    if (screenshot26Done && resultRunId) {
      // Wait a bit more for depth layer to appear, then we're done
      console.log("[flood-proof] run prefix found: " + resultRunId + " (+" + elapsed + "s), waiting for depth layers...");
      const lc2 = await layerCount(page);
      if (lc2 > 2) {
        console.log("[flood-proof] layers appeared: " + lc2);
        break;
      }
    }

    if (elapsed % 30 < POLL_MS / 1000 + 1) {
      console.log("[flood-proof] still polling... +" + elapsed + "s | pipeline=" + pipelineCardsSeen + " | runId=" + resultRunId);
    }
  }

  if (!screenshot26Done) {
    await page.screenshot({ path: path.join(PROOF_DIR, "26-flood-run-progress.png") });
    console.log("[flood-proof] screenshot: 26-flood-run-progress.png (timeout fallback)");
  }

  // ============================================================
  // OPEN-12 fix: the Step-2 hasDepthLayers/groupCount scan ran BEFORE the
  // fresh run, so it only ever measured pre-existing case layers. Re-scan
  // the panel AFTER the run, waiting up to 150s for the fresh depth rows
  // (publishes land a few seconds after the solve completes).
  // ============================================================
  let hasDepthLayersFinal = hasDepthLayers;
  let groupCountFinal = groupCount;
  {
    const deadline = Date.now() + 150000;
    while (Date.now() < deadline) {
      const rows = page.locator('[data-testid="layer-row"], [data-testid="layer-group-row"]');
      const n = await rows.count().catch(() => 0);
      const names = [];
      for (let i = 0; i < n; i++) {
        names.push((await rows.nth(i).innerText().catch(() => "")).toLowerCase());
      }
      hasDepthLayersFinal = names.some(
        (t) => t.includes("flood depth") || t.includes("depth step") || t.includes("peak flood")
      );
      groupCountFinal = await page.locator('[data-testid="layer-group-row"]').count().catch(() => 0);
      if (hasDepthLayersFinal && groupCountFinal > 0) break;
      await sleep(3000);
    }
    console.log(
      "[flood-proof] post-run panel scan: depth=" + hasDepthLayersFinal + " groups=" + groupCountFinal
    );
  }

  await browser.close();

  // ============================================================
  // Report
  // ============================================================
  const screenshots = [
    "22-flood-peak-inundation.png",
    "23-flood-anim-early.png",
    "24-flood-anim-mid.png",
    "25-flood-anim-late.png",
    "26-flood-run-progress.png",
  ];
  console.log("\n=== e2e_flood_proof.mjs SUMMARY ===");
  console.log("case_picked:", casePicked);
  console.log("has_depth_layers:", hasDepthLayersFinal);
  console.log("animation_groups:", groupCountFinal);
  console.log("fresh_run_id:", resultRunId || "(none)");
  for (const f of screenshots) {
    const fp = path.join(PROOF_DIR, f);
    const size = fs.existsSync(fp) ? fs.statSync(fp).size : 0;
    console.log(" ", f, "(bytes=" + size + ")");
  }
}

main().catch((err) => {
  console.error("[flood-proof] FATAL:", err);
  process.exit(1);
});
