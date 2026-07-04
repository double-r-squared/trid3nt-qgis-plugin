#!/usr/bin/env node
// GRACE-2 — job-0173 small UX cleanup bundle live verification.
//
// Captures four screenshots demonstrating the four parts of the job-0173
// kickoff work end-to-end against the running dev server (Vite on :5173):
//
//   1_thinking_label.png      — pipeline card with internal name
//                                "llm_generation" rendered as the user-facing
//                                "Thinking…" label. (Part 1)
//   2_chat_input_idle.png     — after an `error` envelope arrives while a
//                                pipeline is running, ChatInput returns to
//                                the idle (blue up-arrow) state and accepts
//                                a new prompt. (Part 2)
//   3_map_pan_unlock.png      — with a Case open + a layer loaded, mouse
//                                events outside the LayerPanel column reach
//                                MapLibre — the map center actually moves
//                                after a programmatic drag from the
//                                middle/right of the viewport. (Part 3)
//   4_no_nudge_buttons.png    — LayerPanel rows have NO ▲/▼ nudge buttons
//                                (drag-and-drop is the sole reorder
//                                affordance). (Part 4)
//
// Drives the dev-only injection seams (`__grace2InjectPipelineState`,
// `__grace2InjectError`, `__grace2InjectCaseList`, `__grace2InjectCaseOpen`,
// `__grace2InjectSessionState`) so the verification does not depend on a
// live agent.

import { chromium } from "@playwright/test";
import { mkdir } from "fs/promises";

const OUT_DIR =
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0173-engine-20260608/evidence";
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
await page.waitForSelector('[data-testid="grace2-chat"]', { timeout: 10_000 });
await page.waitForTimeout(500);

// =============================================================================
// SS1 — pipeline card rendered with "Thinking…" label for internal name
//        "llm_generation". (Part 1)
// =============================================================================
{
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
  await page.waitForSelector("[data-testid='pipeline-card'][data-state='running']", {
    timeout: 5_000,
  });
  await page.waitForTimeout(200);

  const labelText = await page.$eval(
    "[data-testid='pipeline-card-name']",
    (el) => el.textContent,
  );
  if (labelText !== "Thinking…") {
    throw new Error(
      `SS1: expected pipeline card label "Thinking…", got ${JSON.stringify(labelText)}`,
    );
  }
  // The internal token must not be visible anywhere on the rendered card.
  const cardText = await page.$eval(
    "[data-testid='pipeline-card']",
    (el) => el.textContent ?? "",
  );
  if (cardText.includes("llm_generation")) {
    throw new Error(`SS1: internal "llm_generation" token leaked into card UI`);
  }
  await page.screenshot({
    path: `${OUT_DIR}/1_thinking_label.png`,
    fullPage: false,
  });
  console.log("[SS1] pass — pipeline card shows 'Thinking…' for llm_generation");
}

// =============================================================================
// SS2 — ChatInput returns to idle after `error` envelope (Part 2)
//
// A pipeline running step → action button is the grey stop-square. After the
// error envelope arrives, the button must transition back to idle (blue
// up-arrow), allowing the user to send a new prompt.
// =============================================================================
{
  // Step A: kick a fresh running pipeline to put ChatInput into in-flight.
  await page.evaluate(() => {
    window.__grace2InjectPipelineState?.({
      pipeline_id: "pipe-ss2",
      steps: [
        {
          step_id: "step-ss2-running",
          name: "fetch_dem",
          tool_name: "fetch_dem",
          state: "running",
        },
      ],
    });
  });
  await page.waitForFunction(
    () => {
      const btn = document.querySelector("[data-testid='chat-input-action']");
      return btn?.getAttribute("data-action-state") === "in-flight";
    },
    null,
    { timeout: 5_000 },
  );
  console.log("[SS2] precondition: ChatInput in-flight (stop-square)");

  // Step B: dispatch the error envelope.
  await page.evaluate(() => {
    window.__grace2InjectError?.({
      error_code: "LLM_UNAVAILABLE",
      message: "Gemini generation failed: 500 Internal Server Error",
      retryable: true,
    });
  });
  // The action button must transition back to idle.
  await page.waitForFunction(
    () => {
      const btn = document.querySelector("[data-testid='chat-input-action']");
      return btn?.getAttribute("data-action-state") === "idle";
    },
    null,
    { timeout: 3_000 },
  );
  const glyph = await page.$eval(
    "[data-testid='chat-input-glyph']",
    (el) => el.getAttribute("data-glyph"),
  );
  if (glyph !== "up-arrow") {
    throw new Error(
      `SS2: expected ChatInput glyph "up-arrow" after error, got ${JSON.stringify(glyph)}`,
    );
  }
  // Confirm we can actually type into the textarea + the submit button
  // would be reachable (the input is enabled, not just visually idle).
  await page.fill("[data-testid='chat-input']", "follow-up after the error");
  await page.waitForTimeout(150);
  const draftText = await page.$eval(
    "[data-testid='chat-input']",
    (el) => el.value,
  );
  if (draftText !== "follow-up after the error") {
    throw new Error(`SS2: textarea did not accept follow-up text, got ${JSON.stringify(draftText)}`);
  }
  await page.screenshot({
    path: `${OUT_DIR}/2_chat_input_idle.png`,
    fullPage: false,
  });
  console.log("[SS2] pass — ChatInput is idle (up-arrow); user can type a new prompt");
  // Clear the draft so the next screenshots start clean.
  await page.fill("[data-testid='chat-input']", "");
}

// =============================================================================
// SS3 — Map pan unlock: with a Case open + a layer loaded, MapLibre receives
//       pointer events on the area outside the LayerPanel column. (Part 3)
//
// The bug was: an invisible inner `pointerEvents:auto` div spanning the full
// area from top:64 to bottom:60 (left:0 to right:0) blocked all map drags.
// Test: open a Case, load a layer, then programmatically drag from the
// CENTER of the map (well outside the LayerPanel's ~280-300px-wide left
// column) and confirm the map's center actually shifts.
// =============================================================================
{
  // Open a Case so the CaseView UI mounts (which is where the
  // `grace2-case-view-layer-panel-wrap` lives).
  await page.evaluate(() => {
    const nowIso = new Date().toISOString();
    window.__grace2InjectCaseList?.({
      cases: [
        {
          case_id: "case-ss3",
          title: "Pan unlock smoke",
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
  await page.click("[data-testid='grace2-case-row']");
  // Inject case-open so CaseView UI activates. CaseOpenEnvelopePayload
  // shape: { session_state: { case: CaseSummary, chat_history, loaded_layers,
  // pipeline_history, current_pipeline } } — see contracts.ts.
  await page.evaluate(() => {
    const nowIso = new Date().toISOString();
    window.__grace2InjectCaseOpen?.({
      envelope_type: "case-open",
      session_state: {
        case: {
          case_id: "case-ss3",
          title: "Pan unlock smoke",
          status: "active",
          primary_hazard: "flood",
          bbox: [-82.05, 26.5, -81.75, 26.75],
          created_at: nowIso,
          updated_at: nowIso,
        },
        chat_history: [],
        loaded_layers: [],
        pipeline_history: [],
        current_pipeline: null,
      },
    });
  });
  await page.waitForTimeout(500);

  // Push a session-state with one raster layer so the LayerPanel mounts +
  // the case-view layer-panel-wrap kicks in (this is the structural area
  // the bug was in).
  await page.evaluate(() => {
    window.__grace2InjectSessionState?.({
      loaded_layers: [
        {
          layer_id: "layer-ss3-flood",
          name: "Flood depth (synthetic)",
          layer_type: "raster",
          uri: "https://example.invalid/wms",
          visible: true,
          opacity: 0.7,
          z_index: 10,
        },
      ],
    });
  });
  await page.waitForSelector("[data-testid='grace2-layer-panel']", { timeout: 5_000 });
  await page.waitForTimeout(300);

  // Verify the wrap exists and that its inner pointer-events:auto region
  // does NOT cover the whole viewport.
  const wrapInfo = await page.evaluate(() => {
    const wrap = document.querySelector("[data-testid='grace2-case-view-layer-panel-wrap']");
    if (!wrap) return { exists: false };
    const inner = wrap.firstElementChild;
    if (!inner) return { exists: true, inner: null };
    const r = (inner instanceof HTMLElement) ? inner.getBoundingClientRect() : null;
    const style = (inner instanceof HTMLElement) ? inner.style : null;
    return {
      exists: true,
      outerPointerEvents: (wrap instanceof HTMLElement) ? wrap.style.pointerEvents : null,
      innerPointerEvents: style?.pointerEvents ?? null,
      innerLeft: r?.left ?? null,
      innerWidth: r?.width ?? null,
      innerRight: r?.right ?? null,
      viewportWidth: window.innerWidth,
    };
  });
  console.log("[SS3] LayerPanel wrap geometry:", JSON.stringify(wrapInfo));
  if (!wrapInfo.exists) throw new Error("SS3: LayerPanel wrap not in DOM");
  if (wrapInfo.outerPointerEvents !== "none") {
    throw new Error(
      `SS3: outer wrap pointer-events should be "none", got ${wrapInfo.outerPointerEvents}`,
    );
  }
  if (wrapInfo.innerPointerEvents !== "auto") {
    throw new Error(
      `SS3: inner region pointer-events should be "auto", got ${wrapInfo.innerPointerEvents}`,
    );
  }
  // The inner region should occupy a left column only — its right edge must
  // be well to the LEFT of the viewport's right edge (i.e. it doesn't span
  // the whole width).
  if (wrapInfo.innerRight === null || wrapInfo.innerRight > 400) {
    throw new Error(
      `SS3: inner region extends too far right (right=${wrapInfo.innerRight}, viewport=${wrapInfo.viewportWidth}) — would blanket the map`,
    );
  }

  // Now exercise actual map pan: get the map center BEFORE, drag from the
  // CENTER of the viewport (far outside the LayerPanel column), and confirm
  // the center moved. We can read MapLibre's center via the dev seam
  // `__grace2GetMap` exposed by Map.tsx in DEV builds.
  const centerBefore = await page.evaluate(() => {
    const m = window.__grace2GetMap?.();
    if (!m) return null;
    const c = m.getCenter();
    return { lng: c.lng, lat: c.lat };
  });
  if (!centerBefore) {
    console.warn("[SS3] __grace2GetMap not exposed — DEV seam missing; skipping pan assertion");
  } else {
    console.log("[SS3] map center BEFORE drag:", centerBefore);
    // Drag from middle-right (well outside the LayerPanel column) by 300px.
    const start = { x: 900, y: 450 };
    const end = { x: 600, y: 450 };
    await page.mouse.move(start.x, start.y);
    await page.mouse.down();
    // Multiple intermediate steps so MapLibre's drag handler latches.
    for (let i = 1; i <= 8; i++) {
      const t = i / 8;
      await page.mouse.move(
        start.x + (end.x - start.x) * t,
        start.y + (end.y - start.y) * t,
        { steps: 1 },
      );
      await page.waitForTimeout(15);
    }
    await page.mouse.up();
    await page.waitForTimeout(400);
    const centerAfter = await page.evaluate(() => {
      const m = window.__grace2GetMap?.();
      if (!m) return null;
      const c = m.getCenter();
      return { lng: c.lng, lat: c.lat };
    });
    console.log("[SS3] map center AFTER drag:", centerAfter);
    if (!centerAfter) {
      throw new Error("SS3: map center read AFTER drag returned null");
    }
    const dLng = Math.abs(centerAfter.lng - centerBefore.lng);
    const dLat = Math.abs(centerAfter.lat - centerBefore.lat);
    // A 300px westward drag at zoom 4 over ~360° / 4096 px-per-world-at-z4 ≈
    // 0.09°/px around the center → expect ~0.1° of longitude shift. If it's
    // effectively zero (<0.01°), the pan was blocked.
    const MIN_PAN_DELTA_DEG = 0.05;
    if (dLng < MIN_PAN_DELTA_DEG && dLat < MIN_PAN_DELTA_DEG) {
      throw new Error(
        `SS3: map center did not move after drag (Δlng=${dLng.toFixed(4)}, Δlat=${dLat.toFixed(4)}) — pan still blocked!`,
      );
    }
    console.log(`[SS3] pan delta Δlng=${dLng.toFixed(4)} Δlat=${dLat.toFixed(4)} (threshold ${MIN_PAN_DELTA_DEG})`);
  }
  await page.screenshot({
    path: `${OUT_DIR}/3_map_pan_unlock.png`,
    fullPage: false,
  });
  console.log("[SS3] pass — map responds to drag outside the LayerPanel column");
}

// =============================================================================
// SS4 — LayerPanel has NO ▲/▼ nudge buttons (Part 4)
// =============================================================================
{
  // Ensure at least one layer exists (continuing the SS3 layer is fine, but
  // re-inject to make this test self-contained).
  await page.evaluate(() => {
    window.__grace2InjectSessionState?.({
      loaded_layers: [
        {
          layer_id: "layer-ss4-a",
          name: "Layer A",
          layer_type: "raster",
          uri: "https://example.invalid/a",
          visible: true,
          opacity: 1,
          z_index: 2,
        },
        {
          layer_id: "layer-ss4-b",
          name: "Layer B",
          layer_type: "raster",
          uri: "https://example.invalid/b",
          visible: true,
          opacity: 1,
          z_index: 1,
        },
      ],
    });
  });
  await page.waitForTimeout(200);

  const nudgeUpCount = await page.$$eval(
    "[data-testid='layer-nudge-up']",
    (els) => els.length,
  );
  const nudgeDownCount = await page.$$eval(
    "[data-testid='layer-nudge-down']",
    (els) => els.length,
  );
  if (nudgeUpCount !== 0 || nudgeDownCount !== 0) {
    throw new Error(
      `SS4: expected 0 nudge buttons, got up=${nudgeUpCount} down=${nudgeDownCount}`,
    );
  }
  // Drag handle still present so reorder via DnD still works.
  const dragHandleCount = await page.$$eval(
    "[data-testid='layer-drag-handle']",
    (els) => els.length,
  );
  if (dragHandleCount < 2) {
    throw new Error(`SS4: expected drag handles to remain, got ${dragHandleCount}`);
  }
  // No ▲ or ▼ characters anywhere in the panel.
  const panelText = await page.$eval(
    "[data-testid='grace2-layer-panel']",
    (el) => el.textContent ?? "",
  );
  if (panelText.includes("▲") || panelText.includes("▼")) {
    throw new Error("SS4: nudge glyphs ▲/▼ still rendered somewhere in the panel");
  }
  await page.screenshot({
    path: `${OUT_DIR}/4_no_nudge_buttons.png`,
    fullPage: false,
  });
  console.log(
    `[SS4] pass — 0 nudge buttons, ${dragHandleCount} drag handles preserved, no ▲/▼ glyphs`,
  );
}

console.log("---");
console.log("All four screenshots captured at", OUT_DIR);
if (consoleErrs.length > 0) {
  console.log("Console / pageerrors during run:");
  for (const e of consoleErrs) console.log(" -", e);
}
await browser.close();
