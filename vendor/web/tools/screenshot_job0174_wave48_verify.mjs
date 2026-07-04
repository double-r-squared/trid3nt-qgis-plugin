#!/usr/bin/env node
// GRACE-2 — job-0174 Wave 4.8 Stage B Playwright verification.
//
// LIVE-DRIVEN ONLY: NO `__grace2Inject*` seams. Every screenshot is the
// result of a real chat-input prompt + real envelopes flowing through
// the WebSocket (pipeline-state, session-state, map-command, error).
//
// 7 screenshots:
//   1_auth_to_case.png            — AuthGate → anonymous → Case "Test 4.8" created
//   2_radar_layer.png             — "Show me radar over America"  → NEXRAD WMS raster overlay rendered
//   3_alerts_overlay.png          — "Show me weather alerts across America" → polygon overlay
//   4_protected_areas_ft_myers.png — "Show me protected areas in Fort Myers"
//                                    (geocode_location → fetch_wdpa → render)
//   5_error_card_red.png          — Trigger error (corrupted prompt) → card transitions to red
//                                    + chat-input back to idle
//   6_map_pan_works.png           — User pans/drags the map → it moves
//   7_case_switch_restore.png     — Switch to a NEW Case (no stale layers) then switch
//                                    BACK to "Test 4.8" (layers + chat restored)
//
// All captured at 1440x900. Agent must already be live on :8765 (Vite on :5173).

import { chromium } from "@playwright/test";
import { mkdir, writeFile } from "fs/promises";

const OUT_DIR =
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0174-testing-20260608/evidence";
const BASE_URL = "http://localhost:5173";

const findings = {};
const wsFrameLog = [];

function logWS(page, label) {
  page.on("websocket", (ws) => {
    ws.on("framereceived", (data) => {
      try {
        const t =
          typeof data.payload === "string"
            ? data.payload
            : data.payload.toString();
        // Track only structurally-meaningful envelopes for our verification.
        if (
          t.includes('"type":"map-command"') ||
          t.includes('"type":"session-state"') ||
          t.includes('"type":"pipeline-state"') ||
          t.includes('"type":"location-resolved"') ||
          t.includes('"type":"error"') ||
          t.includes('"type":"agent-message-chunk"')
        ) {
          wsFrameLog.push({
            t_ms: Date.now(),
            label,
            preview: t.slice(0, 280),
          });
        }
      } catch {}
    });
  });
}

async function dismissSaveGate(page, attempts = 4) {
  // The modal may re-trigger across separate gated actions (new-case, rename,
  // case-select, etc.) so loop a few times until no modal is visible.
  for (let i = 0; i < attempts; i++) {
    const modal = page.locator('[data-testid="grace2-save-gate-modal"]');
    if ((await modal.count()) === 0 || !(await modal.isVisible())) {
      return;
    }
    const cont = page.locator('[data-testid="grace2-save-gate-modal-continue"]');
    if ((await cont.count()) > 0 && (await cont.isVisible())) {
      await cont.click({ timeout: 5000 }).catch(() => {});
      await page.waitForTimeout(500);
    } else {
      // Try ESC as fallback.
      await page.keyboard.press("Escape").catch(() => {});
      await page.waitForTimeout(300);
    }
  }
}

async function sendChatPrompt(page, text) {
  await dismissSaveGate(page);
  const chatInput = page.locator('[data-testid="chat-input"]');
  if ((await chatInput.count()) === 0) {
    throw new Error("chat-input not found");
  }
  await chatInput.click();
  await chatInput.fill(text);
  await chatInput.press("Enter");
  // The chat-send itself may surface a save-gate too.
  await page.waitForTimeout(500);
  await dismissSaveGate(page);
}

// Wait for the chat-input to become idle (i.e. the model + tools are not running).
// Reads `aria-busy` on the chat input, presence of any `running` pipeline card, and
// the `disabled` attribute. Quiet when ALL are false / 0.
async function waitForChatIdle(page, timeoutMs = 360_000) {
  const t0 = Date.now();
  let lastObs = null;
  while (Date.now() - t0 < timeoutMs) {
    const obs = await page.evaluate(() => {
      const el = document.querySelector('[data-testid="chat-input"]');
      const runningCards = document.querySelectorAll(
        "[data-testid='pipeline-card'][data-state='running']",
      );
      return {
        input_disabled: el?.disabled ?? null,
        input_busy: el?.getAttribute("aria-busy"),
        running_count: runningCards.length,
      };
    });
    lastObs = obs;
    // Treat idle as: no running cards AND input is enabled AND not busy.
    const idle =
      obs.running_count === 0 &&
      (obs.input_disabled === false || obs.input_disabled === null) &&
      (!obs.input_busy || obs.input_busy === "false");
    if (idle) {
      return { idle: true, elapsed_ms: Date.now() - t0, observation: obs };
    }
    await page.waitForTimeout(1500);
  }
  return { idle: false, elapsed_ms: Date.now() - t0, observation: lastObs };
}

// Poll until the map state satisfies a predicate (e.g. an overlay layer or zoom level).
async function waitForMapPredicate(page, pred, timeoutMs = 120_000, intervalMs = 1000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const state = await page.evaluate(() => {
      const m = window.__grace2GetMap?.();
      if (!m) return null;
      const style = m.getStyle();
      const ctr = m.getCenter();
      return {
        layers: style.layers.map((l) => ({
          id: l.id,
          type: l.type,
          source: l.source,
          visibility: l.layout?.visibility,
        })),
        sources: Object.keys(style.sources || {}),
        center: { lng: ctr.lng, lat: ctr.lat },
        zoom: m.getZoom(),
      };
    });
    if (state && pred(state)) {
      return { state, elapsed_ms: Date.now() - t0 };
    }
    await page.waitForTimeout(intervalMs);
  }
  return null;
}

// ─────────────────────────────────────────────────────────────────────────────
// SS1 — AuthGate → anonymous → Case "Test 4.8" created
// ─────────────────────────────────────────────────────────────────────────────
async function ss1_auth_to_case(browser) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errs = [];
  page.on("pageerror", (e) => errs.push(`pageerror: ${e.message}`));
  page.on("console", (msg) => {
    if (msg.type() === "error") errs.push(`console.error: ${msg.text()}`);
  });
  logWS(page, "ss1");

  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  await page
    .waitForSelector('[data-testid="grace2-auth-gate"]', { timeout: 15_000 })
    .catch(() => null);

  const anonBtn = page.locator('[data-testid="grace2-auth-gate-anonymous"]');
  if ((await anonBtn.count()) > 0) {
    await anonBtn.click();
    await page.waitForTimeout(1_200);
  }

  await page.waitForSelector('[data-testid="grace2-app-shell"]', { timeout: 10_000 });
  await page.waitForTimeout(1_200);

  // Click "+ New Case" — triggers a SaveGate on anonymous user, then the
  // new case is auto-selected → CaseView mounts (replacing CasesPanel).
  const newBtn = page.locator('[data-testid="grace2-cases-new"]');
  await newBtn.click();
  await page.waitForTimeout(800);
  await dismissSaveGate(page);
  await page.waitForTimeout(1_200);
  await dismissSaveGate(page);

  // Wait for either CaseView OR a case-row.
  await page.waitForSelector(
    '[data-testid="grace2-case-view"], [data-testid="grace2-case-row"]',
    { timeout: 15_000 },
  );

  // Navigate back to the Cases list to rename.
  const backBtn = page.locator('[data-testid="grace2-case-view-back"]');
  if ((await backBtn.count()) > 0) {
    await backBtn.click({ timeout: 5_000 }).catch(() => {});
    await page.waitForTimeout(800);
    await dismissSaveGate(page);
  }

  // Rename it to "Test 4.8".
  await page.waitForSelector('[data-testid="grace2-case-row"]', { timeout: 10_000 });
  await dismissSaveGate(page);
  const renameBtn = page.locator('[data-testid="grace2-case-row-rename"]').first();
  if ((await renameBtn.count()) > 0) {
    await renameBtn.click({ timeout: 8_000 }).catch(() => {});
    await page.waitForTimeout(300);
    await dismissSaveGate(page);
    const renameInput = page
      .locator('[data-testid="grace2-case-row-rename-input"]')
      .first();
    if ((await renameInput.count()) > 0) {
      await renameInput.fill("Test 4.8");
      await renameInput.press("Enter");
      await page.waitForTimeout(800);
      await dismissSaveGate(page);
    }
  }

  // Re-open the Case (click row → CaseView mounts again).
  await page.waitForTimeout(500);
  await dismissSaveGate(page);
  const caseView = await page.$('[data-testid="grace2-case-view"]');
  if (!caseView) {
    const row = page.locator('[data-testid="grace2-case-row"]').first();
    if ((await row.count()) > 0) {
      await row.click({ timeout: 8_000 }).catch(() => {});
      await page.waitForTimeout(800);
      await dismissSaveGate(page);
      await page.waitForTimeout(800);
    }
  }
  await page.waitForTimeout(500);
  await dismissSaveGate(page);

  await page.screenshot({ path: `${OUT_DIR}/1_auth_to_case.png` });

  const info = await page.evaluate(() => {
    const shell = document.querySelector('[data-testid="grace2-app-shell"]');
    const appCaseState = document.querySelector('[data-testid="grace2-app-case-state"]');
    const activeCaseId = appCaseState?.getAttribute("data-active-case-id") ?? null;
    const cases = [...document.querySelectorAll('[data-testid="grace2-case-row"]')].map((r) => ({
      case_id: r.getAttribute("data-case-id"),
      title: r.querySelector('[data-testid="grace2-case-row-title"]')?.textContent ?? null,
      active: r.getAttribute("data-active") === "true",
    }));
    return {
      app_shell_present: !!shell,
      active_case_id: activeCaseId,
      cases,
      case_view_present: !!document.querySelector('[data-testid="grace2-case-view"]'),
    };
  });
  findings.ss1 = { ...info, page_errors: errs };
  console.log("[SS1]", JSON.stringify(info, null, 2));

  return { ctx, page };
}

// ─────────────────────────────────────────────────────────────────────────────
// Helper: send a prompt, then wait for the agent to register an overlay layer
// matching `layerHint` (substring), or any non-basemap layer count to rise.
// Returns full diagnostic.
// ─────────────────────────────────────────────────────────────────────────────
async function sendAndWaitForOverlay(page, prompt, opts = {}) {
  const layerHint = opts.layerHint ?? null;
  const layerTypeHints = opts.layerTypeHints ?? null; // e.g. ['fill', 'raster']
  const timeoutMs = opts.timeoutMs ?? 180_000;

  // Baseline: known layer IDs before submission.
  const baselineArr = await page.evaluate(() => {
    const m = window.__grace2GetMap?.();
    if (!m) return null;
    return m.getStyle().layers.map((l) => l.id);
  });
  const baseline = baselineArr ? new Set(baselineArr) : null;

  await sendChatPrompt(page, prompt);
  const submitT0 = Date.now();

  const settled = await waitForMapPredicate(
    page,
    (state) => {
      if (!baseline) return false;
      const newLayers = state.layers.filter((l) => !baseline.has(l.id));
      if (newLayers.length === 0) return false;
      if (layerHint) {
        return newLayers.some((l) => l.id.toLowerCase().includes(layerHint));
      }
      if (layerTypeHints) {
        return newLayers.some((l) => layerTypeHints.includes(l.type));
      }
      return true;
    },
    timeoutMs,
    1500,
  );

  const final = await page.evaluate(() => {
    const m = window.__grace2GetMap?.();
    if (!m) return null;
    const style = m.getStyle();
    const ctr = m.getCenter();
    return {
      all_layer_ids: style.layers.map((l) => l.id),
      layers: style.layers.map((l) => ({ id: l.id, type: l.type, source: l.source })),
      sources: Object.keys(style.sources || {}),
      center: { lng: ctr.lng, lat: ctr.lat },
      zoom: m.getZoom(),
    };
  });

  const new_layers = (final?.layers ?? []).filter(
    (l) => !baseline || !baseline.has(l.id),
  );

  return {
    prompt,
    submit_t0: submitT0,
    elapsed_ms: settled ? settled.elapsed_ms : Date.now() - submitT0,
    settled_with_overlay: settled !== null,
    new_layers,
    map_center: final?.center,
    map_zoom: final?.zoom,
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// SS2 — NEXRAD radar (raster path)
// ─────────────────────────────────────────────────────────────────────────────
async function ss2_radar(page) {
  const res = await sendAndWaitForOverlay(page, "Show me radar over America", {
    layerTypeHints: ["raster"],
    timeoutMs: 180_000,
  });
  // Let the model finish narrating, so the next prompt isn't queued mid-stream.
  const idle = await waitForChatIdle(page, 120_000);
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${OUT_DIR}/2_radar_layer.png` });
  findings.ss2 = { ...res, chat_idle_after: idle };
  console.log("[SS2]", JSON.stringify({
    elapsed_ms: res.elapsed_ms,
    settled: res.settled_with_overlay,
    new_layers: res.new_layers,
    chat_idle: idle.idle,
  }, null, 2));
}

// ─────────────────────────────────────────────────────────────────────────────
// SS3 — Weather alerts (vector polygon path)
// ─────────────────────────────────────────────────────────────────────────────
async function ss3_alerts(page) {
  const res = await sendAndWaitForOverlay(
    page,
    "Show me weather alerts across America",
    { layerTypeHints: ["fill", "line", "circle"], timeoutMs: 240_000 },
  );
  const idle = await waitForChatIdle(page, 240_000);
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${OUT_DIR}/3_alerts_overlay.png` });

  // Capture pipeline cards + agent's narration to make the honest finding traceable.
  const cards = await page.$$eval("[data-testid='pipeline-card']", (els) =>
    els.map((el) => ({
      state: el.getAttribute("data-state"),
      name: el.querySelector("[data-testid='pipeline-card-name']")?.textContent,
    })),
  );
  findings.ss3 = { ...res, chat_idle_after: idle, pipeline_cards: cards };
  console.log("[SS3]", JSON.stringify({
    elapsed_ms: res.elapsed_ms,
    settled: res.settled_with_overlay,
    new_layers: res.new_layers,
    chat_idle: idle.idle,
    pipeline_cards: cards,
  }, null, 2));
}

// ─────────────────────────────────────────────────────────────────────────────
// SS4 — Protected areas in Fort Myers (geocode + fetch_wdpa + render)
// ─────────────────────────────────────────────────────────────────────────────
async function ss4_protected_areas(page) {
  const res = await sendAndWaitForOverlay(
    page,
    "Show me protected areas in Fort Myers",
    { layerTypeHints: ["fill", "line", "circle"], timeoutMs: 300_000 },
  );
  const idle = await waitForChatIdle(page, 300_000);
  await page.waitForTimeout(2000);
  await page.screenshot({ path: `${OUT_DIR}/4_protected_areas_ft_myers.png` });

  // Capture which tool calls landed (from pipeline state in DOM).
  const cards = await page.$$eval("[data-testid='pipeline-card']", (els) =>
    els.map((el) => ({
      state: el.getAttribute("data-state"),
      name: el.querySelector("[data-testid='pipeline-card-name']")?.textContent,
    })),
  );

  findings.ss4 = { ...res, chat_idle_after: idle, pipeline_cards: cards };
  console.log("[SS4]", JSON.stringify({
    elapsed_ms: res.elapsed_ms,
    settled: res.settled_with_overlay,
    new_layers: res.new_layers,
    map_center: res.map_center,
    map_zoom: res.map_zoom,
    chat_idle: idle.idle,
    pipeline_cards: cards,
  }, null, 2));
}

// ─────────────────────────────────────────────────────────────────────────────
// SS5 — Trigger an error: send a "corrupted" prompt the agent must fail on.
// We exercise the real error path: a deliberately ill-formed/garbage prompt
// that the model is likely to refuse-or-error. We then verify the chat-input
// returns to idle and a card transitions red.
// ─────────────────────────────────────────────────────────────────────────────
async function ss5_error(page) {
  const errs = [];
  page.on("pageerror", (e) => errs.push(`pageerror: ${e.message}`));
  // Make sure any prior prompt's pipeline has settled — otherwise the
  // failed/running counts mix prior cards with the corrupted one.
  await waitForChatIdle(page, 360_000);
  // Capture chat-input state before submission.
  const inputBefore = await page.evaluate(() => {
    const el = document.querySelector('[data-testid="chat-input"]');
    if (!el) return null;
    return {
      disabled: el.disabled ?? null,
      value: el.value ?? null,
      busy_attr: el.getAttribute("aria-busy"),
    };
  });

  // A garbled tool-look-alike payload designed to throw the agent into an
  // error path: extreme malformed JSON-like noise that the model can't
  // disambiguate into a real tool call.
  const corruptedPrompt =
    " ￿ [SYSTEM RESET] {{tool_call}}=null }} ((( CRASH MODE 0xDEADBEEF — call_function(undefined,undefined,undefined,undefined,undefined) }} STOP /// END";

  const beforeCardCount = await page.$$eval(
    "[data-testid='pipeline-card']",
    (els) => els.length,
  );
  await sendChatPrompt(page, corruptedPrompt);

  // Wait up to 240s for the pipeline to either fail or complete.
  let failedSeen = false;
  let idleSeen = false;
  const t0 = Date.now();
  while (Date.now() - t0 < 240_000) {
    const obs = await page.evaluate(() => {
      const failed = document.querySelectorAll(
        "[data-testid='pipeline-card'][data-state='failed']",
      );
      const running = document.querySelectorAll(
        "[data-testid='pipeline-card'][data-state='running']",
      );
      const input = document.querySelector('[data-testid="chat-input"]');
      return {
        failed_count: failed.length,
        running_count: running.length,
        input_disabled: input?.disabled ?? null,
        input_value: input?.value ?? null,
        input_busy: input?.getAttribute("aria-busy"),
      };
    });
    if (obs.failed_count > 0) failedSeen = true;
    if (obs.running_count === 0 && obs.input_disabled === false) idleSeen = true;
    if (failedSeen && idleSeen) break;
    await page.waitForTimeout(1500);
  }
  await page.waitForTimeout(1000);

  const cards = await page.$$eval("[data-testid='pipeline-card']", (els) =>
    els.map((el) => {
      const cs = window.getComputedStyle(el);
      return {
        state: el.getAttribute("data-state"),
        name: el.querySelector("[data-testid='pipeline-card-name']")?.textContent,
        backgroundColor: cs.backgroundColor,
        animationName: cs.animationName,
      };
    }),
  );
  const inputAfter = await page.evaluate(() => {
    const el = document.querySelector('[data-testid="chat-input"]');
    if (!el) return null;
    return {
      disabled: el.disabled ?? null,
      value: el.value ?? null,
      busy_attr: el.getAttribute("aria-busy"),
    };
  });

  await page.screenshot({ path: `${OUT_DIR}/5_error_card_red.png` });

  findings.ss5 = {
    corrupted_prompt_first_chars: corruptedPrompt.slice(0, 80),
    before_card_count: beforeCardCount,
    after_cards: cards,
    failed_seen: failedSeen,
    idle_seen: idleSeen,
    input_before: inputBefore,
    input_after: inputAfter,
    page_errors: errs,
  };
  console.log("[SS5]", JSON.stringify({
    failed_seen: failedSeen,
    idle_seen: idleSeen,
    input_after: inputAfter,
    after_cards_summary: cards.map((c) => ({ name: c.name, state: c.state })),
  }, null, 2));
}

// ─────────────────────────────────────────────────────────────────────────────
// SS6 — User pans / drags the map → it moves.
// ─────────────────────────────────────────────────────────────────────────────
async function ss6_pan(page) {
  await waitForChatIdle(page, 60_000);
  const before = await page.evaluate(() => {
    const m = window.__grace2GetMap?.();
    if (!m) return null;
    const c = m.getCenter();
    return { lng: c.lng, lat: c.lat, zoom: m.getZoom() };
  });

  // Find the map canvas and drag.
  const canvas = page.locator(".maplibregl-canvas").first();
  const box = await canvas.boundingBox();
  if (!box) throw new Error("SS6: map canvas not found");
  const startX = box.x + box.width / 2;
  const startY = box.y + box.height / 2;
  await page.mouse.move(startX, startY);
  await page.mouse.down();
  await page.mouse.move(startX - 250, startY - 150, { steps: 18 });
  await page.mouse.up();
  await page.waitForTimeout(800);

  const after = await page.evaluate(() => {
    const m = window.__grace2GetMap?.();
    if (!m) return null;
    const c = m.getCenter();
    return { lng: c.lng, lat: c.lat, zoom: m.getZoom() };
  });

  await page.screenshot({ path: `${OUT_DIR}/6_map_pan_works.png` });

  const moved =
    before && after && (Math.abs(after.lng - before.lng) > 0.01 || Math.abs(after.lat - before.lat) > 0.01);
  findings.ss6 = { before, after, moved };
  console.log("[SS6]", JSON.stringify(findings.ss6, null, 2));
}

// ─────────────────────────────────────────────────────────────────────────────
// SS7 — Switch to a NEW Case (no stale layers), then switch BACK
//       to "Test 4.8" (layers + chat restored).
// ─────────────────────────────────────────────────────────────────────────────
async function ss7_case_switch(page) {
  // Make sure agent is idle before switching cases — otherwise the save-gate
  // modal can intercept and the case_id never changes.
  await waitForChatIdle(page, 360_000);

  // Layers in "Test 4.8" right now.
  const beforeNewCase = await page.evaluate(() => {
    const m = window.__grace2GetMap?.();
    const layers = m ? m.getStyle().layers.map((l) => l.id) : [];
    const cards = [...document.querySelectorAll('[data-testid="pipeline-card"]')].map((c) => ({
      state: c.getAttribute("data-state"),
      name: c.querySelector('[data-testid="pipeline-card-name"]')?.textContent,
    }));
    const activeCaseId = document
      .querySelector('[data-testid="grace2-app-case-state"]')
      ?.getAttribute("data-active-case-id");
    return { layer_ids: layers, pipeline_cards: cards, active_case_id: activeCaseId };
  });

  // First go back to the Cases list (we're currently in CaseView for Test 4.8).
  const backBtn = page.locator('[data-testid="grace2-case-view-back"]');
  if ((await backBtn.count()) > 0) {
    await backBtn.click({ timeout: 5_000 }).catch(() => {});
    await page.waitForTimeout(800);
    await dismissSaveGate(page);
  }

  // Create a SECOND new Case.
  await dismissSaveGate(page);
  const newBtn = page.locator('[data-testid="grace2-cases-new"]');
  await newBtn.click({ timeout: 8_000 }).catch(() => {});
  await page.waitForTimeout(800);
  await dismissSaveGate(page);
  await page.waitForTimeout(1500);
  await dismissSaveGate(page);

  // The new case is auto-selected and we're back in CaseView. Back out again
  // to rename it.
  const backBtn2 = page.locator('[data-testid="grace2-case-view-back"]');
  if ((await backBtn2.count()) > 0) {
    await backBtn2.click({ timeout: 5_000 }).catch(() => {});
    await page.waitForTimeout(800);
    await dismissSaveGate(page);
  }
  await page.waitForSelector('[data-testid="grace2-case-row"]', { timeout: 10_000 });

  // Rename the most-recent (top) row to "Empty Case B" — its title is the
  // default "Untitled Case", so we pick the first row that ISN'T titled
  // "Test 4.8".
  await dismissSaveGate(page);
  const rowsAll = page.locator('[data-testid="grace2-case-row"]');
  const rowCountAll = await rowsAll.count();
  for (let i = 0; i < rowCountAll; i++) {
    const titleEl = rowsAll.nth(i).locator('[data-testid="grace2-case-row-title"]');
    if ((await titleEl.count()) > 0) {
      const t = (await titleEl.textContent())?.trim();
      if (t !== "Test 4.8") {
        const renameBtn = rowsAll
          .nth(i)
          .locator('[data-testid="grace2-case-row-rename"]');
        if ((await renameBtn.count()) > 0) {
          await renameBtn.click({ timeout: 8_000 }).catch(() => {});
          await page.waitForTimeout(300);
          await dismissSaveGate(page);
          const renameInput = page
            .locator('[data-testid="grace2-case-row-rename-input"]')
            .first();
          if ((await renameInput.count()) > 0) {
            await renameInput.fill("Empty Case B");
            await renameInput.press("Enter");
            await page.waitForTimeout(800);
            await dismissSaveGate(page);
          }
        }
        break;
      }
    }
  }
  await page.waitForTimeout(800);
  await dismissSaveGate(page);

  // Click the "Empty Case B" row to make it active.
  const rowsAfter = page.locator('[data-testid="grace2-case-row"]');
  const rowAfterCount = await rowsAfter.count();
  for (let i = 0; i < rowAfterCount; i++) {
    const titleEl = rowsAfter.nth(i).locator('[data-testid="grace2-case-row-title"]');
    if ((await titleEl.count()) > 0) {
      const t = (await titleEl.textContent())?.trim();
      if (t === "Empty Case B") {
        await rowsAfter.nth(i).click({ timeout: 8_000 }).catch(() => {});
        await page.waitForTimeout(800);
        await dismissSaveGate(page);
        await page.waitForTimeout(1200);
        break;
      }
    }
  }
  await page.waitForTimeout(800);
  await dismissSaveGate(page);

  // Observe the empty-state layers (should be NONE except basemaps).
  const afterSwitchToB = await page.evaluate(() => {
    const m = window.__grace2GetMap?.();
    const layers = m ? m.getStyle().layers.map((l) => l.id) : [];
    const cards = [...document.querySelectorAll('[data-testid="pipeline-card"]')].map((c) => ({
      state: c.getAttribute("data-state"),
      name: c.querySelector('[data-testid="pipeline-card-name"]')?.textContent,
    }));
    const activeCaseId = document
      .querySelector('[data-testid="grace2-app-case-state"]')
      ?.getAttribute("data-active-case-id");
    return { layer_ids: layers, pipeline_cards: cards, active_case_id: activeCaseId };
  });

  // Back-out of Empty Case B's CaseView to the Cases list.
  const backBtn3 = page.locator('[data-testid="grace2-case-view-back"]');
  if ((await backBtn3.count()) > 0) {
    await backBtn3.click({ timeout: 5_000 }).catch(() => {});
    await page.waitForTimeout(800);
    await dismissSaveGate(page);
  }
  await page.waitForSelector('[data-testid="grace2-case-row"]', { timeout: 10_000 });

  // Switch back to "Test 4.8" by clicking its row.
  await dismissSaveGate(page);
  const rows = page.locator('[data-testid="grace2-case-row"]');
  const rowCount = await rows.count();
  let backClicked = false;
  for (let i = 0; i < rowCount; i++) {
    const titleEl = rows.nth(i).locator('[data-testid="grace2-case-row-title"]');
    if ((await titleEl.count()) > 0) {
      const t = (await titleEl.textContent())?.trim();
      if (t === "Test 4.8") {
        await rows.nth(i).click({ timeout: 8_000 }).catch(() => {});
        backClicked = true;
        break;
      }
    }
  }
  await page.waitForTimeout(800);
  await dismissSaveGate(page);
  await page.waitForTimeout(2500);
  await dismissSaveGate(page);

  const afterSwitchBack = await page.evaluate(() => {
    const m = window.__grace2GetMap?.();
    const layers = m ? m.getStyle().layers.map((l) => l.id) : [];
    const cards = [...document.querySelectorAll('[data-testid="pipeline-card"]')].map((c) => ({
      state: c.getAttribute("data-state"),
      name: c.querySelector('[data-testid="pipeline-card-name"]')?.textContent,
    }));
    const activeCaseId = document
      .querySelector('[data-testid="grace2-app-case-state"]')
      ?.getAttribute("data-active-case-id");
    return { layer_ids: layers, pipeline_cards: cards, active_case_id: activeCaseId };
  });

  await page.screenshot({ path: `${OUT_DIR}/7_case_switch_restore.png` });

  const newOverlayBefore = beforeNewCase.layer_ids.filter(
    (id) => !/osm|basemap|background|sky/i.test(id),
  );
  const newOverlayInB = afterSwitchToB.layer_ids.filter(
    (id) => !/osm|basemap|background|sky/i.test(id),
  );
  const newOverlayAfterBack = afterSwitchBack.layer_ids.filter(
    (id) => !/osm|basemap|background|sky/i.test(id),
  );

  findings.ss7 = {
    beforeNewCase: { ...beforeNewCase, overlay_only: newOverlayBefore },
    afterSwitchToB: {
      ...afterSwitchToB,
      overlay_only: newOverlayInB,
      empty_overlay_layers: newOverlayInB.length === 0,
      case_id_changed: afterSwitchToB.active_case_id !== beforeNewCase.active_case_id,
    },
    backClicked,
    afterSwitchBack: {
      ...afterSwitchBack,
      overlay_only: newOverlayAfterBack,
      case_id_restored: afterSwitchBack.active_case_id === beforeNewCase.active_case_id,
      overlay_restored: newOverlayAfterBack.length > 0,
    },
  };
  console.log("[SS7]", JSON.stringify({
    before: { overlay: newOverlayBefore, case: beforeNewCase.active_case_id },
    inB: { overlay: newOverlayInB, case: afterSwitchToB.active_case_id },
    back: { overlay: newOverlayAfterBack, case: afterSwitchBack.active_case_id },
    case_id_restored: afterSwitchBack.active_case_id === beforeNewCase.active_case_id,
  }, null, 2));
}

// ─────────────────────────────────────────────────────────────────────────────
async function main() {
  await mkdir(OUT_DIR, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  console.log("=== job-0174 Wave 4.8 Stage B Playwright LIVE verification ===");
  console.log("BASE_URL:", BASE_URL);
  console.log("OUT_DIR :", OUT_DIR);
  console.log();

  let sharedCtx = null;
  let sharedPage = null;
  try {
    console.log("--- SS1: AuthGate → anonymous → Case 'Test 4.8' ---");
    const { ctx, page } = await ss1_auth_to_case(browser);
    sharedCtx = ctx;
    sharedPage = page;
    console.log();

    console.log("--- SS2: 'Show me radar over America' (raster) ---");
    await ss2_radar(sharedPage);
    console.log();

    console.log("--- SS3: 'Show me weather alerts across America' (vector) ---");
    await ss3_alerts(sharedPage);
    console.log();

    console.log("--- SS4: 'Show me protected areas in Fort Myers' (multi-tool) ---");
    await ss4_protected_areas(sharedPage);
    console.log();

    console.log("--- SS5: corrupted prompt → red card + idle input ---");
    await ss5_error(sharedPage);
    console.log();

    console.log("--- SS6: map pan/drag works ---");
    await ss6_pan(sharedPage);
    console.log();

    console.log("--- SS7: switch to new Case → switch back ---");
    await ss7_case_switch(sharedPage);
    console.log();
  } finally {
    if (sharedCtx) await sharedCtx.close().catch(() => {});
    await browser.close();
  }

  findings.ws_frames = wsFrameLog.slice(-200);
  await writeFile(
    `${OUT_DIR}/findings.json`,
    JSON.stringify(findings, null, 2),
  );
  console.log("=== COMPLETE — findings.json written ===");
}

main().catch((e) => {
  console.error("FAILURE:", e);
  process.exit(1);
});
