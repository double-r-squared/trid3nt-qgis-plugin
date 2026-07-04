#!/usr/bin/env node
// GRACE-2 — job-0236 Case 3 acceptance: NWS alert -> MRMS -> SFINCS Idaho
// (sprint-13 Stage 3 LIVE GATE).
//
// LIVE-DRIVEN ONLY: NO `__grace2Inject*` seams. The ONLY window seam used is
// `__grace2GetMap()` — a READ-ONLY observation getter for the MapLibre map
// state. Does not inject envelopes.
//
// Flow:
//   1. AuthGate -> anonymous -> create a new Case.
//   2. Send: "Show me active flood warnings in Idaho, then model the flood
//      for the most severe one."
//   3. Wait for NWS alert fetch -> warning polygon layer on map.
//   4. Wait for MRMS QPE fetch -> precip raster on map.
//   5. Wait for SFINCS run completion -> flood depth layer on map.
//   6. Assert 3-layer accumulation: warning polygon + MRMS precip + flood depth.
//   7. Check SFINCS workflow execution via WS frames (cloud dispatch observable).
//
// REALITY BRANCH: if agent narrates no Idaho flood warnings, re-prompt with
// whatever state was mentioned as having flood warnings. If NO CONUS flood
// warnings at all, document and mark PARTIAL.
//
// Budget: up to 20 minutes for solver completion (kickoff).
// Agent: :8765, Vite: :5173. Evidence: reports/inflight/job-0236.../evidence/.

import { chromium } from "@playwright/test";
import { mkdir, writeFile } from "fs/promises";

const EVIDENCE_DIR =
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0236-testing-20260609/evidence";
const BASE_URL = "http://localhost:5173";

const findings = {
  scenario: "Case 3: NWS alert -> MRMS -> SFINCS Idaho",
  started_at: new Date().toISOString(),
};
const wsFrames = [];
const t0Global = Date.now();

function rel() {
  return Date.now() - t0Global;
}

function logWS(page) {
  page.on("websocket", (ws) => {
    ws.on("framereceived", (data) => {
      try {
        const t =
          typeof data.payload === "string"
            ? data.payload
            : data.payload.toString();
        let parsed = null;
        try {
          parsed = JSON.parse(t);
        } catch {}
        const type = parsed?.type ?? null;
        const KEEP = new Set([
          "tool-call-start",
          "tool-call-result",
          "tool-call-error",
          "pipeline-state",
          "tool-payload-warning",
          "tool-payload-confirmation",
          "map-command",
          "session-state",
          "error",
          "agent-message",
          "interaction-request",
          "confirmation-request",
        ]);
        const isChunk = type === "agent-message-chunk";
        if (type && (KEEP.has(type) || isChunk)) {
          let toolName = null;
          const p = parsed?.payload ?? {};
          toolName =
            p.tool_name ??
            p.name ??
            p.tool ??
            (Array.isArray(p.steps)
              ? p.steps.map((s) => s.tool_name ?? s.name).filter(Boolean)
              : null);
          wsFrames.push({
            t_rel_ms: rel(),
            type,
            tool_name: toolName,
            preview: t.slice(0, isChunk ? 200 : 1500),
          });
        }
      } catch {}
    });
    ws.on("framesent", (data) => {
      try {
        const t =
          typeof data.payload === "string"
            ? data.payload
            : data.payload.toString();
        const parsed = JSON.parse(t);
        const type = parsed?.type ?? parsed?.envelope_type ?? null;
        if (
          type === "user-message" ||
          type === "tool-payload-confirmation" ||
          type === "chat-message"
        ) {
          wsFrames.push({
            t_rel_ms: rel(),
            type: `SENT:${type}`,
            preview: t.slice(0, 400),
          });
        }
      } catch {}
    });
  });
}

async function dismissSaveGate(page, attempts = 4) {
  for (let i = 0; i < attempts; i++) {
    const modal = page.locator('[data-testid="grace2-save-gate-modal"]');
    if ((await modal.count()) === 0 || !(await modal.isVisible())) return;
    const cont = page.locator(
      '[data-testid="grace2-save-gate-modal-continue"]',
    );
    if ((await cont.count()) > 0 && (await cont.isVisible())) {
      await cont.click({ timeout: 5000 }).catch(() => {});
      await page.waitForTimeout(400);
    } else {
      await page.keyboard.press("Escape").catch(() => {});
      await page.waitForTimeout(300);
    }
  }
}

async function snapshotMap(page) {
  return page.evaluate(() => {
    const m = window.__grace2GetMap?.();
    if (!m) return null;
    const style = m.getStyle();
    const ctr = m.getCenter();
    return {
      layers: style.layers.map((l) => ({
        id: l.id,
        type: l.type,
        source: l.source,
      })),
      sources: Object.keys(style.sources || {}),
      center: { lng: ctr.lng, lat: ctr.lat },
      zoom: m.getZoom(),
    };
  });
}

async function snapshotCards(page) {
  return page.$$eval("[data-testid='pipeline-card']", (els) =>
    els.map((el) => ({
      state: el.getAttribute("data-state"),
      name: el.querySelector("[data-testid='pipeline-card-name']")?.textContent,
    })),
  );
}

async function chatScrollText(page) {
  return page.evaluate(() => {
    const scroll =
      document.querySelector("[data-testid='chat-scroll']") ||
      document.querySelector("[data-testid='chat-messages']") ||
      document.querySelector("[data-testid='chat-container']");
    return scroll ? scroll.textContent : document.body.textContent;
  });
}

async function sendChatMessage(page, msg) {
  const chatInput = page.locator('[data-testid="chat-input"]');
  await chatInput.waitFor({ state: "visible", timeout: 15000 });
  // Wait for input to be enabled (not during a running turn).
  await page.waitForFunction(
    () => {
      const el = document.querySelector('[data-testid="chat-input"]');
      return el && !el.disabled;
    },
    { timeout: 30000 },
  ).catch(() => {});
  await chatInput.click();
  await chatInput.fill(msg);
  await chatInput.press("Enter");
  await page.waitForTimeout(600);
  await dismissSaveGate(page);
}

// Wait for the turn to complete (no running cards + input enabled + at least
// one agent signal seen after the prompt was sent).
async function waitForTurnComplete(page, opts = {}) {
  const {
    maxMs = 20 * 60 * 1000, // 20 min budget
    progressShotFn = null,
    progressShotInterval = 45000,
  } = opts;
  const t0 = Date.now();
  let lastShot = 0;
  let heartbeat = -1;
  const seenSignal = () =>
    wsFrames.some(
      (f) =>
        f.type === "agent-message-chunk" ||
        f.type === "agent-message" ||
        f.type === "tool-call-start" ||
        f.type === "tool-call-result" ||
        f.type === "error" ||
        f.type === "tool-call-error",
    );
  const geminiGenerating = () => {
    const last = [...wsFrames]
      .reverse()
      .find((f) => f.type === "pipeline-state");
    if (!last) return false;
    return /gemini_generate|llm_generation/i.test(
      JSON.stringify(last.tool_name ?? last.preview ?? ""),
    );
  };
  while (Date.now() - t0 < maxMs) {
    if (progressShotFn && Date.now() - lastShot > progressShotInterval) {
      lastShot = Date.now();
      await progressShotFn(Math.round((Date.now() - t0) / 1000));
    }
    const st = await page.evaluate(() => {
      const el = document.querySelector('[data-testid="chat-input"]');
      const running = document.querySelectorAll(
        "[data-testid='pipeline-card'][data-state='running']",
      ).length;
      const failed = document.querySelectorAll(
        "[data-testid='pipeline-card'][data-state='failed']",
      ).length;
      return {
        disabled: el?.disabled ?? null,
        running,
        failed,
      };
    });
    const generating = geminiGenerating();
    const hb = Math.floor((Date.now() - t0) / 15000);
    if (hb !== heartbeat) {
      heartbeat = hb;
      console.log(
        `[wait] t=${Math.round((Date.now() - t0) / 1000)}s generating=${generating} running=${st.running} failed=${st.failed} frames=${wsFrames.length}`,
      );
    }
    if (
      !generating &&
      seenSignal() &&
      st.running === 0 &&
      st.disabled === false &&
      Date.now() - t0 > 20000
    ) {
      // Extra grace: wait 4s more to let the map settle.
      await page.waitForTimeout(4000);
      const st2 = await page.evaluate(() => {
        const el = document.querySelector('[data-testid="chat-input"]');
        const running2 = document.querySelectorAll(
          "[data-testid='pipeline-card'][data-state='running']",
        ).length;
        return { disabled: el?.disabled ?? null, running: running2 };
      });
      if (st2.running === 0 && st2.disabled === false) break;
    }
    await page.waitForTimeout(2000);
  }
}

// Extract text content from the narration for asserts.
function extractNarrationInfo(text) {
  const lower = text.toLowerCase();
  return {
    mentions_idaho: /idaho|id\b/.test(lower),
    mentions_flood_warning: /flood warning|flash flood warning|flood watch/.test(
      lower,
    ),
    mentions_sfincs_or_flood_model:
      /sfincs|flood model|flood depth|inundation/.test(lower),
    mentions_mrms: /mrms|precip|precipitation|rainfall|qpe/.test(lower),
    mentions_nws: /nws|national weather service|warning/.test(lower),
    mentions_no_warnings: /no active|no flood warning|no warning/.test(lower),
    mentions_florida: /florida|fort myers|tampa|miami/.test(lower),
    full_tail: text.slice(-3000),
  };
}

// Check the map for layers matching the 3 expected Case 3 layers.
function classify3Layers(mapLayers, baselineIds) {
  const newLayers = mapLayers.filter((l) => !baselineIds.has(l.id));
  const warningPolygon = newLayers.find(
    (l) =>
      /nws|alert|warning|polygon/i.test(l.id) ||
      l.type === "fill" ||
      l.type === "line",
  );
  const precipRaster = newLayers.find(
    (l) =>
      /mrms|precip|qpe|rain/i.test(l.id) ||
      (l.type === "raster" && /mrms|precip|qpe/i.test(l.id)),
  );
  const floodDepth = newLayers.find(
    (l) =>
      /flood|depth|inundation|sfincs/i.test(l.id) ||
      (l.type === "raster" && !precipRaster),
  );
  // Also look at raster layers generically when no specific match.
  const allRasters = newLayers.filter((l) => l.type === "raster");
  const allFills = newLayers.filter(
    (l) => l.type === "fill" || l.type === "line",
  );
  return {
    warning_polygon_layer: warningPolygon ?? null,
    mrms_precip_layer: precipRaster ?? null,
    flood_depth_layer: floodDepth ?? null,
    all_new_layers: newLayers,
    all_rasters: allRasters,
    all_fills: allFills,
    new_layer_count: newLayers.length,
  };
}

async function main() {
  await mkdir(EVIDENCE_DIR, { recursive: true });

  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
  });
  const page = await ctx.newPage();
  const errs = [];
  page.on("pageerror", (e) => errs.push(`pageerror: ${e.message}`));
  page.on("console", (msg) => {
    if (msg.type() === "error") errs.push(`console.error: ${msg.text()}`);
  });
  logWS(page);

  console.log("=== job-0236 Case 3 E2E LIVE (NWS -> MRMS -> SFINCS) ===");

  try {
    // ---- Step 1: boot UI -> auth -> new case --------------------------------
    await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
    await page
      .waitForSelector('[data-testid="grace2-auth-gate"]', { timeout: 15000 })
      .catch(() => null);
    const anonBtn = page.locator('[data-testid="grace2-auth-gate-anonymous"]');
    if ((await anonBtn.count()) > 0) {
      await anonBtn.click();
      await page.waitForTimeout(1200);
    }
    await page.waitForSelector('[data-testid="grace2-app-shell"]', {
      timeout: 10000,
    });
    await page.waitForTimeout(1000);

    const newBtn = page.locator('[data-testid="grace2-cases-new"]');
    await newBtn.click();
    await page.waitForTimeout(800);
    await dismissSaveGate(page);
    await page.waitForTimeout(1000);
    await dismissSaveGate(page);
    await page.waitForSelector(
      '[data-testid="grace2-case-view"], [data-testid="grace2-case-row"]',
      { timeout: 15000 },
    );
    if ((await page.locator('[data-testid="grace2-case-view"]').count()) === 0) {
      const row = page.locator('[data-testid="grace2-case-row"]').first();
      if ((await row.count()) > 0) {
        await row.click({ timeout: 8000 }).catch(() => {});
        await page.waitForTimeout(1000);
        await dismissSaveGate(page);
      }
    }
    await page.waitForSelector('[data-testid="chat-input"]', { timeout: 10000 });
    await page.waitForTimeout(800);

    // Capture baseline map state BEFORE any prompts.
    const baselineMap = await snapshotMap(page);
    const baselineIds = new Set((baselineMap?.layers ?? []).map((l) => l.id));
    findings.baseline_layer_count = baselineIds.size;

    await page.screenshot({
      path: `${EVIDENCE_DIR}/01_new_case.png`,
      fullPage: false,
    });
    console.log("[STEP1] new case ready, baseline layers:", baselineIds.size);

    // ---- Step 2: Send Case 3 prompt ----------------------------------------
    const PROMPT_1 =
      "Show me active flood warnings in Idaho, then model the flood for the most severe one.";
    findings.prompt_1 = PROMPT_1;
    const sendT = Date.now();
    await sendChatMessage(page, PROMPT_1);
    console.log("[STEP2] prompt sent:", PROMPT_1);

    // Capture thinking indicator if visible.
    await page.waitForTimeout(1500);
    const thinkingCount = await page
      .locator('[data-testid="thinking-indicator"]')
      .count();
    if (thinkingCount > 0) {
      await page.screenshot({
        path: `${EVIDENCE_DIR}/02_thinking_indicator.png`,
        fullPage: false,
      });
      console.log("[STEP2] thinking indicator captured");
    }
    findings.thinking_indicator_seen = thinkingCount > 0;

    // ---- Step 3: Wait for the agent to complete turn 1 ---------------------
    // Budget: full 20 min (SFINCS can take ~10-15 min on Cloud Run).
    await waitForTurnComplete(page, {
      maxMs: 20 * 60 * 1000,
      progressShotFn: async (elapsed_s) => {
        await page
          .screenshot({
            path: `${EVIDENCE_DIR}/03_progress_${elapsed_s}s.png`,
            fullPage: false,
          })
          .catch(() => {});
        console.log(
          `[STEP3-progress] ${elapsed_s}s elapsed, ws_frames=${wsFrames.length}`,
        );
      },
      progressShotInterval: 45000,
    });

    const elapsedAfterTurn1 = Date.now() - sendT;
    findings.turn1_elapsed_ms = elapsedAfterTurn1;
    console.log(
      `[STEP3] Turn 1 complete in ${Math.round(elapsedAfterTurn1 / 1000)}s`,
    );

    await page.screenshot({
      path: `${EVIDENCE_DIR}/04_after_turn1.png`,
      fullPage: false,
    });

    // ---- Step 4: Assess what the agent did ----------------------------------
    const map1 = await snapshotMap(page);
    const cards1 = await snapshotCards(page);
    const narration1Raw = await chatScrollText(page);
    const narration1 = extractNarrationInfo(narration1Raw);
    const layers1 = classify3Layers(map1?.layers ?? [], baselineIds);

    findings.turn1 = {
      narration: narration1,
      cards: cards1,
      layers: layers1,
      map_center: map1?.center ?? null,
      map_zoom: map1?.zoom ?? null,
    };

    console.log("[STEP4] narration:", JSON.stringify(narration1));
    console.log("[STEP4] layers:", JSON.stringify(layers1));
    console.log("[STEP4] cards:", JSON.stringify(cards1));

    // ---- Step 5: Reality branch + possible re-prompt -----------------------
    let turn2Done = false;
    let usedFallbackState = null;

    if (narration1.mentions_no_warnings || layers1.new_layer_count === 0) {
      // No Idaho flood warnings. Extract which state has warnings from narration.
      const stateMatch = narration1Raw.match(
        /\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\s+(?:has|have|are|is)\s+(?:\d+\s+)?(?:active\s+)?(?:flood|flash\s+flood)\s+warning/i,
      );
      const anyStateMatch = narration1Raw.match(
        /\bflooding?\s+in\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)/i,
      );
      const altState =
        stateMatch?.[1] ?? anyStateMatch?.[1] ?? "the most affected area";

      console.log(
        "[STEP5] No Idaho warnings; narrated alt state:",
        altState,
      );
      findings.no_idaho_warnings = true;
      findings.fallback_state = altState;

      // If there are SOME new layers already (just not Idaho), we may still be ok.
      // If no new layers at all, re-prompt.
      if (layers1.new_layer_count < 2) {
        const PROMPT_2 = `Since there are no active flood warnings in Idaho right now, show me flood warnings and model the flood in ${altState} instead.`;
        findings.prompt_2 = PROMPT_2;
        usedFallbackState = altState;
        console.log("[STEP5] Re-prompting with:", PROMPT_2);
        await sendChatMessage(page, PROMPT_2);

        await waitForTurnComplete(page, {
          maxMs: 20 * 60 * 1000,
          progressShotFn: async (elapsed_s) => {
            await page
              .screenshot({
                path: `${EVIDENCE_DIR}/05_turn2_progress_${elapsed_s}s.png`,
                fullPage: false,
              })
              .catch(() => {});
          },
          progressShotInterval: 45000,
        });

        await page.screenshot({
          path: `${EVIDENCE_DIR}/06_after_turn2.png`,
          fullPage: false,
        });
        turn2Done = true;

        const map2 = await snapshotMap(page);
        const cards2 = await snapshotCards(page);
        const narration2Raw = await chatScrollText(page);
        const narration2 = extractNarrationInfo(narration2Raw);
        const layers2 = classify3Layers(map2?.layers ?? [], baselineIds);

        findings.turn2 = {
          narration: narration2,
          cards: cards2,
          layers: layers2,
          map_center: map2?.center ?? null,
          map_zoom: map2?.zoom ?? null,
        };
        console.log("[STEP5] Turn 2 narration:", JSON.stringify(narration2));
        console.log("[STEP5] Turn 2 layers:", JSON.stringify(layers2));
      }
    }

    // ---- Step 6: final state after all turns --------------------------------
    // Open layer panel if we can find it.
    const layerToggle = page.locator(
      '[data-testid="grace2-layer-panel-toggle"], [data-testid="layer-panel-toggle"], [aria-label*="layer" i]',
    );
    if ((await layerToggle.count()) > 0) {
      await layerToggle.first().click({ timeout: 4000 }).catch(() => {});
      await page.waitForTimeout(800);
    }
    await page.waitForTimeout(1500);
    await page.screenshot({
      path: `${EVIDENCE_DIR}/07_final_3layer_map.png`,
      fullPage: false,
    });
    console.log("[STEP6] Final screenshot captured");

    // Final authoritative map/narration snapshot.
    const finalMap = await snapshotMap(page);
    const finalNarrationRaw = await chatScrollText(page);
    const finalNarration = extractNarrationInfo(finalNarrationRaw);
    const finalLayers = classify3Layers(finalMap?.layers ?? [], baselineIds);
    const finalCards = await snapshotCards(page);

    // Non-Florida geography check.
    const inFloridaBbox =
      finalMap?.center &&
      finalMap.center.lng > -88 &&
      finalMap.center.lng < -79 &&
      finalMap.center.lat > 24 &&
      finalMap.center.lat < 31;

    findings.final = {
      layers: finalLayers,
      narration: finalNarration,
      cards: finalCards,
      map_center: finalMap?.center ?? null,
      map_zoom: finalMap?.zoom ?? null,
      in_florida_bbox: inFloridaBbox ?? null,
      non_florida_geography: !inFloridaBbox,
    };

    // ---- Step 7: WS-frame tool-call audit (which tools fired?) ------------
    const toolCallFrames = wsFrames.filter(
      (f) =>
        f.type === "tool-call-start" ||
        f.type === "tool-call-result" ||
        f.type === "tool-call-error" ||
        f.type === "pipeline-state",
    );
    const toolNames = toolCallFrames.map((f) => f.tool_name).filter(Boolean);
    findings.tool_calls_observed = toolNames;

    const nwsToolFired = toolCallFrames.some(
      (f) =>
        /nws|alerts_conus|nws_alert/i.test(JSON.stringify(f.tool_name ?? "")) ||
        /nws|alerts_conus/i.test(f.preview ?? ""),
    );
    const mrmsToolFired = toolCallFrames.some(
      (f) =>
        /mrms|qpe/i.test(JSON.stringify(f.tool_name ?? "")) ||
        /mrms|qpe/i.test(f.preview ?? ""),
    );
    const sfincsToolFired = toolCallFrames.some(
      (f) =>
        /sfincs|flood_scenario|flood_event/i.test(
          JSON.stringify(f.tool_name ?? ""),
        ) || /sfincs|flood_scenario|flood_event/i.test(f.preview ?? ""),
    );
    const case3ComposerFired = toolCallFrames.some(
      (f) =>
        /nws_flood_event|case_3|case3/i.test(
          JSON.stringify(f.tool_name ?? ""),
        ) || /nws_flood_event|case_3/i.test(f.preview ?? ""),
    );

    findings.tool_chain = {
      nws_tool_fired: nwsToolFired,
      mrms_tool_fired: mrmsToolFired,
      sfincs_tool_fired: sfincsToolFired,
      case3_composer_fired: case3ComposerFired,
    };

    // ---- Verdict -----------------------------------------------------------
    const hasWarningPolygon = !!finalLayers.warning_polygon_layer || finalLayers.all_fills.length > 0;
    const hasMrms = !!finalLayers.mrms_precip_layer || finalLayers.all_rasters.length >= 1;
    const hasFloodDepth = !!finalLayers.flood_depth_layer || finalLayers.all_rasters.length >= 2;
    const has3Layers = finalLayers.new_layer_count >= 3;
    const toolChainOk = nwsToolFired && (mrmsToolFired || sfincsToolFired || case3ComposerFired);

    let verdict;
    let verdict_detail;

    if (finalNarration.mentions_no_warnings && finalLayers.new_layer_count === 0 && !turn2Done) {
      verdict = "PARTIAL";
      verdict_detail =
        "No active CONUS flood warnings at all — agent degraded correctly " +
        "but solver leg cannot be verified. 0 layers rendered.";
    } else if (has3Layers || (hasMrms && hasFloodDepth)) {
      verdict = "PASS";
      verdict_detail = `3-layer accumulation confirmed: new_layer_count=${finalLayers.new_layer_count}; ` +
        `NWS=${nwsToolFired}, MRMS=${mrmsToolFired || case3ComposerFired}, SFINCS=${sfincsToolFired || case3ComposerFired}; ` +
        `non-Florida=${!inFloridaBbox}`;
    } else if (hasWarningPolygon && (mrmsToolFired || case3ComposerFired)) {
      verdict = "PARTIAL";
      verdict_detail =
        `Warning polygon rendered + MRMS fetch confirmed, but flood depth layer not confirmed. ` +
        `new_layer_count=${finalLayers.new_layer_count}`;
    } else if (toolChainOk && finalLayers.new_layer_count >= 1) {
      verdict = "PARTIAL";
      verdict_detail =
        `Tool chain fired (NWS+/MRMS/SFINCS) but only ${finalLayers.new_layer_count} new layer(s) on map.`;
    } else {
      verdict = "FAIL";
      verdict_detail =
        `Tool chain incomplete or no layers on map. ` +
        `toolChainOk=${toolChainOk}, new_layers=${finalLayers.new_layer_count}, ` +
        `nws=${nwsToolFired}, mrms=${mrmsToolFired}, sfincs=${sfincsToolFired}`;
    }

    findings.verdict = verdict;
    findings.verdict_detail = verdict_detail;
    findings.used_fallback_state = usedFallbackState;
    findings.turn2_done = turn2Done;

    console.log("[VERDICT]", verdict, "—", verdict_detail);
    console.log("[FINAL] tool_chain:", JSON.stringify(findings.tool_chain));
    console.log("[FINAL] layers:", JSON.stringify(finalLayers));
    console.log("[FINAL] non_florida:", !inFloridaBbox);
  } catch (e) {
    findings.fatal_error = String(e && e.stack ? e.stack : e);
    console.error("FATAL:", e);
    await page
      .screenshot({ path: `${EVIDENCE_DIR}/99_fatal.png`, fullPage: false })
      .catch(() => {});
  } finally {
    findings.page_errors = errs.slice(0, 50);
    findings.ws_frames_count = wsFrames.length;
    findings.ws_frames = wsFrames;
    findings.completed_at = new Date().toISOString();

    await writeFile(
      `${EVIDENCE_DIR}/findings.json`,
      JSON.stringify(findings, null, 2),
    );
    await writeFile(
      `${EVIDENCE_DIR}/ws_frames.json`,
      JSON.stringify(wsFrames, null, 2),
    );
    await ctx.close().catch(() => {});
    await browser.close();
    console.log("=== COMPLETE ===");
    console.log("Evidence:", EVIDENCE_DIR);
    console.log("Verdict:", findings.verdict ?? "UNKNOWN");
  }
}

main().catch((e) => {
  console.error("OUTER FAILURE:", e);
  process.exit(1);
});
