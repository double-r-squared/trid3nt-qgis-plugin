#!/usr/bin/env node
// GRACE-2 — job-0166 UX cleanup live verification.
//
// Captures three screenshots demonstrating the three Wave 4.6 follow-up fixes
// work live end-to-end against the running dev server (Vite on :5173):
//
//   1_pipeline_failure_red.png    — A `running` pipeline card transitions to a
//                                    red, no-animation `failed` card after an
//                                    `error` envelope arrives (LLM_UNAVAILABLE).
//                                    Demonstrates Part 1 of the kickoff.
//   2_font_consistency.png        — CasesPanel + ConfirmationDialog rendered
//                                    side-by-side with the chat panel; all
//                                    surfaces use the same system-ui sans-serif
//                                    font (no Times-New-Roman serif anywhere).
//                                    Demonstrates Part 2.
//   3_single_llm_card.png         — A single transitioning `llm_generation`
//                                    card after an out-of-order sequence that
//                                    would previously have rendered as two
//                                    cards (stale blue + green completed).
//                                    Demonstrates Part 3.
//
// Drives the dev-only injection seams (`__grace2InjectPipelineState`,
// `__grace2InjectError`, `__grace2InjectCaseList`) so the verification does
// not depend on a live agent failure path.

import { chromium } from "@playwright/test";
import { mkdir } from "fs/promises";

const OUT_DIR =
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0166-web-20260608/evidence";
const BASE_URL = "http://localhost:5173";

await mkdir(OUT_DIR, { recursive: true });

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
// Skip auth-gate.
await ctx.addInitScript(() => {
  try {
    localStorage.setItem("grace2_anonymous_accepted", "true");
  } catch {}
});
const page = await ctx.newPage();
const consoleErrs = [];
page.on("pageerror", (e) => consoleErrs.push(`pageerror: ${e.message}`));
page.on("console", (msg) => {
  if (msg.type() === "error") consoleErrs.push(`console.error: ${msg.text()}`);
});

await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
// Wait for the app to mount; tolerate a couple seconds for the WS
// connection to negotiate / fail without dragging the script down.
await page.waitForSelector('[data-testid="grace2-chat"]', { timeout: 10_000 });
await page.waitForTimeout(500);

// =============================================================================
// SS1 — pipeline failure transitions to red on `error` envelope arrival.
// =============================================================================
{
  // Step A: inject a `running` llm_generation pipeline-state.
  await page.evaluate(() => {
    window.__grace2InjectPipelineState?.({
      pipeline_id: "pipe-ss1",
      steps: [
        {
          step_id: "step-llm-ss1",
          name: "llm_generation",
          tool_name: "gemini_generate",
          state: "running",
        },
      ],
    });
  });
  await page.waitForSelector(
    "[data-testid='pipeline-card'][data-state='running']",
    { timeout: 5_000 },
  );
  // Brief pause so the rainbow gradient is visibly mid-cycle for inspection.
  await page.waitForTimeout(300);

  // Step B: dispatch the `error` envelope.
  await page.evaluate(() => {
    window.__grace2InjectError?.({
      error_code: "LLM_UNAVAILABLE",
      message: "Gemini generation failed: 500 Internal Server Error",
      retryable: true,
    });
  });
  // The card must transition to `failed` (RED, no spinner).
  await page.waitForSelector(
    "[data-testid='pipeline-card'][data-state='failed']",
    { timeout: 3_000 },
  );
  // Sanity: there should be NO running card left.
  const runningCount = await page.$$eval(
    "[data-testid='pipeline-card'][data-state='running']",
    (els) => els.length,
  );
  if (runningCount !== 0) {
    throw new Error(`SS1: ${runningCount} running cards still on screen after error`);
  }
  // Sanity: there should be NO spinner indicator.
  const spinnerCount = await page.$$eval(
    "[data-testid='pipeline-card-indicator']",
    (els) => els.length,
  );
  if (spinnerCount !== 0) {
    throw new Error(`SS1: spinner still visible (count=${spinnerCount})`);
  }
  // Sanity: the failed card carries the error_code chip.
  const errChip = await page.$eval(
    "[data-testid='pipeline-card-error']",
    (el) => el.textContent,
  );
  if (!errChip?.includes("LLM_UNAVAILABLE")) {
    throw new Error(`SS1: error chip text was ${JSON.stringify(errChip)}`);
  }
  await page.screenshot({
    path: `${OUT_DIR}/1_pipeline_failure_red.png`,
    fullPage: false,
  });
  console.log("[SS1] pass — running→failed RED card, no spinner, LLM_UNAVAILABLE chip");
}

// =============================================================================
// SS2 — font consistency: Cases panel + ConfirmationDialog vs chat
//
// We inject a fake case-list so the CasesPanel renders rows, then click the
// row's delete button so the ConfirmationDialog appears. The Chat panel is
// already on screen. All three surfaces should render in the same
// system-ui sans-serif stack.
// =============================================================================
{
  // Reset the pipeline state to keep the chat panel uncluttered for SS2.
  await page.evaluate(() => {
    window.__grace2InjectPipelineState?.({
      pipeline_id: "pipe-ss2-reset",
      steps: [],
    });
  });

  // Inject a case-list with one row.
  await page.evaluate(() => {
    const nowIso = new Date().toISOString();
    window.__grace2InjectCaseList?.({
      cases: [
        {
          case_id: "case-ss2",
          title: "Fort Myers flood study",
          status: "active",
          primary_hazard: "flood",
          bbox: [-82.05, 26.5, -81.75, 26.75],
          created_at: nowIso,
          updated_at: nowIso,
        },
      ],
    });
  });
  await page.waitForSelector("[data-testid='grace2-case-row']", { timeout: 5_000 });

  // Open the delete confirmation modal.
  await page.click("[data-testid='grace2-case-row-delete']");
  await page.waitForSelector("[data-testid='grace2-case-delete-dialog']", {
    timeout: 5_000,
  });

  // Spot-check the resolved font-family on each surface — none should be a
  // serif. happy-dom does not resolve `getComputedStyle` like a real browser,
  // but Playwright/Chromium does.
  const fonts = await page.evaluate(() => {
    function fam(el) {
      if (!el) return null;
      return window.getComputedStyle(el).fontFamily;
    }
    return {
      chatHeader: fam(document.querySelector("[data-testid='grace2-chat']")),
      caseRow: fam(document.querySelector("[data-testid='grace2-case-row']")),
      modalText: fam(
        document.querySelector("[data-testid='grace2-case-delete-dialog-message']"),
      ),
      newCaseBtn: fam(document.querySelector("[data-testid='grace2-cases-new']")),
      cancelBtn: fam(
        document.querySelector("[data-testid='grace2-case-delete-dialog-cancel']"),
      ),
      confirmBtn: fam(
        document.querySelector("[data-testid='grace2-case-delete-dialog-confirm']"),
      ),
    };
  });
  console.log("[SS2] resolved fontFamily:", JSON.stringify(fonts, null, 2));
  for (const [k, v] of Object.entries(fonts)) {
    if (v === null) {
      throw new Error(`SS2: ${k} element not found`);
    }
    if (/serif/i.test(v) && !/sans-serif/i.test(v)) {
      throw new Error(`SS2: ${k} resolved to a serif font: ${v}`);
    }
    if (/Times/i.test(v) || /Georgia/i.test(v)) {
      throw new Error(`SS2: ${k} resolved to a Times/Georgia serif font: ${v}`);
    }
  }
  await page.screenshot({
    path: `${OUT_DIR}/2_font_consistency.png`,
    fullPage: false,
  });
  console.log("[SS2] pass — all surfaces resolved to sans-serif stack");

  // Dismiss the modal so SS3 starts clean.
  await page.click("[data-testid='grace2-case-delete-dialog-cancel']");
  await page.waitForTimeout(200);
}

// =============================================================================
// SS3 — single transitioning llm_generation card
//
// Simulates the failure mode the kickoff describes: two different step_ids
// emitted with the same (name="llm_generation", tool_name="gemini_generate")
// — historically rendered as a stale running card stacked above a green
// completed one. With the Part 3 merge-by-(name,tool_name) fix in place, the
// user must see ONE card in the latest state.
// =============================================================================
{
  // Stale running snapshot — different pipeline_id + step_id from the next.
  await page.evaluate(() => {
    window.__grace2InjectPipelineState?.({
      pipeline_id: "pipe-stale",
      steps: [
        {
          step_id: "step-llm-stale",
          name: "llm_generation",
          tool_name: "gemini_generate",
          state: "running",
        },
      ],
    });
  });
  await page.waitForTimeout(150);
  // Terminal complete snapshot — same name/tool_name, different step_id.
  await page.evaluate(() => {
    window.__grace2InjectPipelineState?.({
      pipeline_id: "pipe-terminal",
      steps: [
        {
          step_id: "step-llm-terminal",
          name: "llm_generation",
          tool_name: "gemini_generate",
          state: "complete",
        },
      ],
    });
  });
  // Allow one render tick.
  await page.waitForTimeout(300);
  const cards = await page.$$eval(
    "[data-testid='pipeline-card']",
    (els) => els.map((el) => ({
      state: el.getAttribute("data-state"),
      name: el.querySelector("[data-testid='pipeline-card-name']")?.textContent,
    })),
  );
  console.log("[SS3] pipeline cards on screen:", JSON.stringify(cards));
  const llmCards = cards.filter((c) => c.name === "llm_generation");
  if (llmCards.length !== 1) {
    throw new Error(
      `SS3: expected exactly ONE llm_generation card, got ${llmCards.length}`,
    );
  }
  if (llmCards[0].state !== "complete") {
    throw new Error(
      `SS3: expected llm_generation card state=complete, got ${llmCards[0].state}`,
    );
  }
  await page.screenshot({
    path: `${OUT_DIR}/3_single_llm_card.png`,
    fullPage: false,
  });
  console.log("[SS3] pass — exactly one llm_generation card, state=complete");
}

console.log("---");
console.log("All three screenshots captured at", OUT_DIR);
if (consoleErrs.length > 0) {
  console.log("Console / pageerrors during run:");
  for (const e of consoleErrs) console.log(" -", e);
}
await browser.close();
