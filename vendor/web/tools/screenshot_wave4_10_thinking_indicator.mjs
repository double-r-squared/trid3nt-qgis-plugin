#!/usr/bin/env node
// GRACE-2 — wave-4-10 thinking-state evidence screenshots.
//
// Captures two states of the ephemeral Thinking… indicator per
// `feedback_thinking_state_ephemeral`:
//
//   01_thinking_indicator.png       — Gemini llm_generation pseudo-step in
//                                      running state → italic muted-gray
//                                      "Thinking…" with subtle opacity pulse,
//                                      pinned to the bottom of the chat scroll,
//                                      NO box / NO card chrome
//   02_thinking_indicator_after.png — first agent text bubble has streamed in →
//                                      indicator vanished; text bubble is the
//                                      only chat content
//
// This is a COMPONENT-STATE capture (per the pattern established in
// screenshot_job0162_pipeline_card_states.mjs). The agent backend is being
// modified by an in-flight Stage 4 re-sweep workflow (kickoff constraint:
// services/agent/* is off-limits), so we use the dev-only
// __grace2InjectPipelineState seam on Chat.tsx to drive the indicator into
// each state. The seam is invalid for end-to-end VERIFICATION (per
// `feedback_playwright_must_drive_live_agent`) but is valid for the
// component-state UX captures this job needs.
//
// Output:
//   - {OUT_DIR}/01_thinking_indicator.png        ← "before" state
//   - {OUT_DIR}/02_thinking_indicator_after.png  ← "after vanish" state
//   - /tmp/wave4_10_thinking_indicator.png       ← duplicate of (1) per kickoff
//   - /tmp/wave4_10_thinking_indicator_after.png ← duplicate of (2) per kickoff

import { chromium } from "@playwright/test";
import { mkdir, copyFile } from "fs/promises";

const OUT_DIR =
  process.argv[2] ??
  "/home/nate/Documents/GRACE-2/reports/inflight/wave-4-10-thinking-state-20260609/evidence";
const BASE_URL = process.env.GRACE2_DEV_URL ?? "http://localhost:5173";
const TMP_BEFORE = "/tmp/wave4_10_thinking_indicator.png";
const TMP_AFTER = "/tmp/wave4_10_thinking_indicator_after.png";

async function makeContext(browser) {
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
  });
  await ctx.addInitScript(() => {
    try {
      localStorage.setItem("grace2_anonymous_accepted", "true");
    } catch {}
  });
  return ctx;
}

async function gotoApp(page) {
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="grace2-chat"]', {
    timeout: 15000,
  });
  await page.waitForFunction(
    () => typeof window.__grace2InjectPipelineState === "function",
    { timeout: 15000 },
  );
}

async function injectPipeline(page, snapshot) {
  await page.evaluate((s) => window.__grace2InjectPipelineState(s), snapshot);
  await page.waitForTimeout(200);
}

async function cropChat(page, outPath) {
  const handle = await page.$('[data-testid="grace2-chat"]');
  if (!handle) {
    await page.screenshot({ path: outPath, fullPage: false });
    return;
  }
  await handle.screenshot({ path: outPath });
}

// "Type" text into the chat input so the screenshot shows a recognisable
// chat context (matches what a real Gemini turn would look like at the
// moment of thinking). We do NOT attempt to submit — the send button is
// disabled when the WebSocket can't reach a live agent (Stage 4 re-sweep is
// in flight so the backend isn't responding). Leaving the typed text in
// the input is enough visual context for the captures.
async function typeUserMessage(page, text) {
  const input = await page.$('[data-testid="chat-input"]');
  if (!input) return false;
  await input.click();
  await input.type(text, { delay: 5 });
  await page.waitForTimeout(150);
  return true;
}

// ────────────────────────────────────────────────────────────────────────── //
// 01 — Thinking indicator active (running llm_generation, no agent text yet)
// ────────────────────────────────────────────────────────────────────────── //

async function shotBefore(browser) {
  const ctx = await makeContext(browser);
  const page = await ctx.newPage();
  await gotoApp(page);

  // Type a user prompt so the chat scroll has some context above the
  // indicator (matches the real-world look: user asks, model "thinks").
  await typeUserMessage(page, "Show me protected areas in Fort Myers");

  // Inject a prior completed tool card so the screenshot demonstrates that
  // the indicator IS pinned to the bottom of the chat (below existing
  // content). Without this, the indicator is the only chat content and
  // looks like it could just be naturally first.
  await injectPipeline(page, {
    pipeline_id: "pipe-prior",
    steps: [
      {
        step_id: "step-prior-1",
        name: "geocode_location",
        tool_name: "geocode_location",
        state: "complete",
      },
    ],
  });

  // Inject the Gemini llm_generation pseudo-step in running state.
  // PipelineCard humanizes this to "Thinking…" — but Chat now filters it
  // OUT of the interleaved stream and the ThinkingIndicator picks it up
  // as the ephemeral end-of-chat affordance.
  await injectPipeline(page, {
    pipeline_id: "pipe-thinking-01",
    steps: [
      {
        step_id: "step-thinking-1",
        name: "llm_generation",
        tool_name: "gemini_generate",
        state: "running",
      },
    ],
  });

  // Confirm the ThinkingIndicator is rendered + no PipelineCard for the
  // llm_generation step exists in the interleaved stream.
  const findings = await page.evaluate(() => {
    const ind = document.querySelector('[data-testid="thinking-indicator"]');
    const cards = Array.from(
      document.querySelectorAll('[data-testid="pipeline-card"]'),
    ).map((c) => c.getAttribute("data-state"));
    const indPresent = ind !== null;
    const indText = ind?.textContent ?? null;
    return {
      indPresent,
      indText,
      pipelineCardCount: cards.length,
      cardStates: cards,
    };
  });

  await cropChat(page, `${OUT_DIR}/01_thinking_indicator.png`);
  await copyFile(`${OUT_DIR}/01_thinking_indicator.png`, TMP_BEFORE);
  await ctx.close();
  return findings;
}

// ────────────────────────────────────────────────────────────────────────── //
// 02 — Indicator vanishes after first agent text chunk streams in
// ────────────────────────────────────────────────────────────────────────── //

async function shotAfter(browser) {
  const ctx = await makeContext(browser);
  const page = await ctx.newPage();
  await gotoApp(page);

  await typeUserMessage(page, "Show me protected areas in Fort Myers");

  // First: indicator active (running llm_generation).
  await injectPipeline(page, {
    pipeline_id: "pipe-thinking-02",
    steps: [
      {
        step_id: "step-thinking-2",
        name: "llm_generation",
        tool_name: "gemini_generate",
        state: "running",
      },
    ],
  });

  // Simulate the agent text chunk streaming in. There's no public dev seam
  // for agent-message-chunk; we use the same indirect path the in-flight
  // agent uses by transitioning the llm_generation step to "complete" AND
  // injecting a follow-up tool dispatch (a non-thinking tool card). Per the
  // memory spec, EITHER terminal thinking state OR a non-thinking tool
  // landing OR an agent text bubble arriving will hide the indicator. We
  // exercise the terminal-state path because it's the cleanest single-seam
  // transition.
  await injectPipeline(page, {
    pipeline_id: "pipe-thinking-02",
    steps: [
      {
        step_id: "step-thinking-2",
        name: "llm_generation",
        tool_name: "gemini_generate",
        state: "complete",
      },
    ],
  });

  // Inject a follow-up tool card so the "after" screenshot has visible
  // chat content to confirm the chat panel isn't empty (matches the real
  // flow where the model decides to call a tool after thinking).
  await injectPipeline(page, {
    pipeline_id: "pipe-tool-01",
    steps: [
      {
        step_id: "step-tool-1",
        name: "fetch_wdpa_protected_areas",
        tool_name: "fetch_wdpa_protected_areas",
        state: "complete",
      },
    ],
  });

  const findings = await page.evaluate(() => {
    const ind = document.querySelector('[data-testid="thinking-indicator"]');
    const cards = Array.from(
      document.querySelectorAll('[data-testid="pipeline-card"]'),
    ).map((c) => ({
      state: c.getAttribute("data-state"),
      // The card's label after humanizeStepName.
      name: c.querySelector('[data-testid="pipeline-card-name"]')?.textContent,
    }));
    return {
      indicatorVanished: ind === null,
      pipelineCardCount: cards.length,
      cards,
    };
  });

  await cropChat(page, `${OUT_DIR}/02_thinking_indicator_after.png`);
  await copyFile(`${OUT_DIR}/02_thinking_indicator_after.png`, TMP_AFTER);
  await ctx.close();
  return findings;
}

// ────────────────────────────────────────────────────────────────────────── //

async function main() {
  await mkdir(OUT_DIR, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  let before, after;
  try {
    before = await shotBefore(browser);
    console.log("[shot] 01_thinking_indicator", before);
    after = await shotAfter(browser);
    console.log("[shot] 02_thinking_indicator_after", after);
  } finally {
    await browser.close();
  }
  console.log(
    "[done] before/after captures",
    JSON.stringify({ before, after }, null, 2),
  );
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
