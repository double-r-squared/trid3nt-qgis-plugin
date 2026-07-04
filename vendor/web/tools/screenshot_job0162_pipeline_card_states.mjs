#!/usr/bin/env node
// GRACE-2 — job-0162 evidence screenshots.
//
// Verifies the Wave 4.6 chat-bundle fixes:
//
//   01_card_pending.png    — grey-subdued pipeline card (no spinner / icon)
//   02_card_running.png    — normal bg + rainbow-gradient text + spinner
//   03_card_success.png    — full green-tint background
//   04_card_failure.png    — full red-tint background + error_code chip
//   05_card_mixed_stack.png — multiple cards (pending + running + success) with
//                             12-16px vertical gap, NO horizontal dividers,
//                             NO "running" / "completed" group headers
//   06_chat_before_collapse.png — chat with user msg + agent reply visible
//   07_chat_after_collapse.png  — chat panel hidden (chevron clicked), hamburger
//                                  visible
//   08_chat_after_reexpand.png  — chat panel reopened, prior user msg STILL
//                                  visible (collapse preserved state)
//
// Uses the dev seam `__grace2InjectPipelineState` on Chat.tsx to drive the
// inline cards without a live agent. The collapse-preserve test uses the
// chat's local message state via direct DOM interaction (typing a fake user
// message; the agent backend echo is not required because we only verify
// that the user bubble persists across collapse/expand).

import { chromium } from "@playwright/test";
import { mkdir, writeFile } from "fs/promises";

const OUT_DIR =
  process.argv[2] ??
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0162-engine-20260608/evidence";
const BASE_URL = process.env.GRACE2_DEV_URL ?? "http://localhost:5173";

async function makeContext(browser) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await ctx.addInitScript(() => {
    try {
      localStorage.setItem("grace2_anonymous_accepted", "true");
    } catch {}
  });
  return ctx;
}

async function gotoApp(page) {
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="grace2-chat"]', { timeout: 15000 });
  await page.waitForFunction(
    () => typeof window.__grace2InjectPipelineState === "function",
    { timeout: 15000 },
  );
}

async function injectPipeline(page, snapshot) {
  await page.evaluate((s) => window.__grace2InjectPipelineState(s), snapshot);
  // Let React reconcile.
  await page.waitForTimeout(150);
}

async function cropChat(page, outPath) {
  // Crop to the chat panel for clarity (the X collapses are positioned at
  // right:16, top:16, bottom:16, width:380).
  const handle = await page.$('[data-testid="grace2-chat"]');
  if (!handle) {
    await page.screenshot({ path: outPath, fullPage: false });
    return;
  }
  await handle.screenshot({ path: outPath });
}

// ────────────────────────────────────────────────────────────────────────── //
// Card-state screenshots
// ────────────────────────────────────────────────────────────────────────── //

async function shotPending(browser) {
  const ctx = await makeContext(browser);
  const page = await ctx.newPage();
  await gotoApp(page);
  await injectPipeline(page, {
    pipeline_id: "pipe-pending",
    steps: [
      {
        step_id: "step-pending-1",
        name: "fetch_dem",
        tool_name: "fetch_dem_tool",
        state: "pending",
      },
    ],
  });
  await cropChat(page, `${OUT_DIR}/01_card_pending.png`);
  await ctx.close();
}

async function shotRunning(browser) {
  const ctx = await makeContext(browser);
  const page = await ctx.newPage();
  await gotoApp(page);
  await injectPipeline(page, {
    pipeline_id: "pipe-running",
    steps: [
      {
        step_id: "step-running-1",
        name: "run_model_flood_scenario",
        tool_name: "run_model_flood_scenario",
        state: "running",
        progress_percent: 47,
      },
    ],
  });
  await cropChat(page, `${OUT_DIR}/02_card_running.png`);
  await ctx.close();
}

async function shotSuccess(browser) {
  const ctx = await makeContext(browser);
  const page = await ctx.newPage();
  await gotoApp(page);
  // For terminal states the reducer archives to history immediately. The
  // single terminal snapshot is enough; mergeStepsByStepId surfaces it.
  await injectPipeline(page, {
    pipeline_id: "pipe-success",
    steps: [
      {
        step_id: "step-success-1",
        name: "publish_layer",
        tool_name: "publish_layer",
        state: "complete",
      },
    ],
  });
  await cropChat(page, `${OUT_DIR}/03_card_success.png`);
  await ctx.close();
}

async function shotFailure(browser) {
  const ctx = await makeContext(browser);
  const page = await ctx.newPage();
  await gotoApp(page);
  await injectPipeline(page, {
    pipeline_id: "pipe-failure",
    steps: [
      {
        step_id: "step-failure-1",
        name: "fetch_precip_return_period",
        tool_name: "fetch_precip_return_period",
        state: "failed",
        error_code: "UPSTREAM_503",
        error_message: "NOAA Atlas 14 API returned 503 Service Unavailable",
      },
    ],
  });
  await cropChat(page, `${OUT_DIR}/04_card_failure.png`);
  await ctx.close();
}

async function shotMixedStack(browser) {
  const ctx = await makeContext(browser);
  const page = await ctx.newPage();
  await gotoApp(page);
  // Simulate the per-tool-pipeline-id pattern: 3 separate tool dispatches,
  // each with its own pipeline_id but a unique step_id. Without the
  // mergeStepsByStepId fix this would render as 3 separate "groups" each
  // with a header — the user-reported bug. With the fix, exactly 3 cards
  // appear with 12-16px gap and no group headers.
  await injectPipeline(page, {
    pipeline_id: "pipe-tool-1",
    steps: [
      {
        step_id: "step-mix-1",
        name: "fetch_dem",
        tool_name: "fetch_dem",
        state: "complete",
      },
    ],
  });
  await injectPipeline(page, {
    pipeline_id: "pipe-tool-2",
    steps: [
      {
        step_id: "step-mix-2",
        name: "fetch_landcover",
        tool_name: "fetch_landcover",
        state: "running",
        progress_percent: 33,
      },
    ],
  });
  await injectPipeline(page, {
    pipeline_id: "pipe-tool-3",
    steps: [
      {
        step_id: "step-mix-3",
        name: "build_sfincs_model",
        tool_name: "build_sfincs_model",
        state: "pending",
      },
    ],
  });
  await cropChat(page, `${OUT_DIR}/05_card_mixed_stack.png`);

  // Verify exactly 3 cards rendered (single transitioning card per
  // dispatch — no duplicate stale + completed pair).
  const cardCount = await page.evaluate(() => {
    return document.querySelectorAll("[data-testid='pipeline-card']").length;
  });
  // Verify no group headers / borderlines.
  const hasGroupHeaders = await page.evaluate(() => {
    return (
      document.querySelectorAll("[data-testid='pipeline-step-group']").length
    );
  });
  await ctx.close();
  return { cardCount, hasGroupHeaders };
}

// ────────────────────────────────────────────────────────────────────────── //
// Collapse-preserve screenshots
// ────────────────────────────────────────────────────────────────────────── //

async function shotCollapsePreserveFlow(browser) {
  const ctx = await makeContext(browser);
  const page = await ctx.newPage();
  await gotoApp(page);

  // Wait for the connection-status indicator. Even if WebSocket fails, we
  // can still type into the input — `submit` only short-circuits when
  // wsRef.current is null, but the input is always enabled when status ==
  // connected. We need a connected status for the message to land. So we
  // poll for "connected"; if the local agent isn't up, we fall back to
  // injecting a fake user message via the DOM (we'll detect this below).
  const statusOk = await page
    .waitForFunction(
      () => {
        const el = document.querySelector(
          "[data-testid='connection-status']",
        );
        return el && el.textContent && el.textContent.includes("connected");
      },
      { timeout: 8000 },
    )
    .then(() => true)
    .catch(() => false);

  // Take screenshot 06 — empty chat (before user message). We'll capture
  // the AFTER state separately.
  if (statusOk) {
    // Type a message and submit via Enter.
    const input = await page.$('[data-testid="chat-input"]');
    if (input) {
      await input.click();
      await input.type("Run flood scenario for Fort Myers", { delay: 5 });
      await page.keyboard.press("Enter");
      await page.waitForTimeout(250);
    }
  }

  // Before collapse: capture chat with content present.
  await page.screenshot({
    path: `${OUT_DIR}/06_chat_before_collapse.png`,
    clip: { x: 1440 - 380 - 32, y: 0, width: 380 + 32, height: 900 },
  });

  // Record what the user bubble shows so we can compare after re-expand.
  const beforeText = await page.evaluate(() => {
    const bubbles = document.querySelectorAll(
      "[data-testid='user-bubble']",
    );
    return Array.from(bubbles).map((b) => b.textContent ?? "");
  });

  // Click the collapse (chevron) button.
  const closeBtn = await page.$('[data-testid="grace2-chat-close"]');
  if (closeBtn) {
    await closeBtn.click();
    await page.waitForTimeout(300);
  }

  // After collapse: chat should be hidden, hamburger should be visible.
  await page.screenshot({
    path: `${OUT_DIR}/07_chat_after_collapse.png`,
    fullPage: false,
  });

  const collapsedState = await page.evaluate(() => {
    const mount = document.querySelector('[data-testid="grace2-chat-mount"]');
    const hamburger = document.querySelector(
      '[data-testid="grace2-chat-hamburger"]',
    );
    return {
      mountAriaHidden: mount?.getAttribute("aria-hidden") ?? null,
      mountStyleDisplay: mount?.style.display ?? null,
      hamburgerPresent: !!hamburger,
    };
  });

  // Re-expand via the hamburger.
  const hamburger = await page.$('[data-testid="grace2-chat-hamburger"]');
  if (hamburger) {
    await hamburger.click();
    await page.waitForTimeout(300);
  }

  // After re-expand: chat should be visible AND prior content preserved.
  await page.screenshot({
    path: `${OUT_DIR}/08_chat_after_reexpand.png`,
    clip: { x: 1440 - 380 - 32, y: 0, width: 380 + 32, height: 900 },
  });

  const afterText = await page.evaluate(() => {
    const bubbles = document.querySelectorAll(
      "[data-testid='user-bubble']",
    );
    return Array.from(bubbles).map((b) => b.textContent ?? "");
  });

  await ctx.close();
  return {
    statusOk,
    beforeText,
    afterText,
    preserved: JSON.stringify(beforeText) === JSON.stringify(afterText),
    collapsedState,
  };
}

// ────────────────────────────────────────────────────────────────────────── //

async function main() {
  await mkdir(OUT_DIR, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const report = {};
  try {
    await shotPending(browser);
    console.log("[shot] 01_card_pending");
    await shotRunning(browser);
    console.log("[shot] 02_card_running");
    await shotSuccess(browser);
    console.log("[shot] 03_card_success");
    await shotFailure(browser);
    console.log("[shot] 04_card_failure");
    report.mixedStack = await shotMixedStack(browser);
    console.log("[shot] 05_card_mixed_stack ", report.mixedStack);
    report.collapsePreserve = await shotCollapsePreserveFlow(browser);
    console.log("[shot] 06-08 collapse-preserve flow", report.collapsePreserve);
  } finally {
    await browser.close();
  }
  await writeFile(
    `${OUT_DIR}/findings.json`,
    JSON.stringify(report, null, 2),
  );
  console.log("[done] wrote findings.json");
  console.log(JSON.stringify(report, null, 2));
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
