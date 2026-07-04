#!/usr/bin/env node
// GRACE-2 — job-0235 Case 2 full E2E acceptance (sprint-13 Stage 3, LIVE GATE).
//
// LIVE-DRIVEN ONLY: NO `__grace2Inject*` seams. The ONLY window seam used is
// `__grace2GetMap()` — a READ-ONLY observation getter for the MapLibre map
// (it does not inject any agent envelope; it merely reads map state).
//
// Flow:
//   1. AuthGate -> anonymous -> create a new Case.
//   2. Paste the Case-2 synthetic article (Twin Falls, Idaho TCE spill) into
//      chat with: "Model the groundwater contamination from this spill: <article>"
//   3. Observe ORDER: does a confirmation gate (payload-warning-inline) appear
//      BEFORE any MODFLOW tool/pipeline card dispatches? (Critical assert.)
//   4. If a gate appears -> approve (Proceed).
//   5. Observe MODFLOW run -> plume layer on map -> narration.
//
// All envelopes are tapped off the real WebSocket. Every tool-call name + the
// payload-warning envelope + every agent-message-chunk is timestamped so the
// confirmation-BEFORE-dispatch ordering is provable from the frame log.
//
// Captured at 1440x900. Agent live on :8765, Vite on :5173.

import { chromium } from "@playwright/test";
import { mkdir, writeFile, readFile } from "fs/promises";

const OUT_DIR =
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0235-testing-20260609/evidence";
const BASE_URL = "http://localhost:5173";
const ARTICLE_PATH =
  "/home/nate/Documents/GRACE-2/services/agent/tests/fixtures/case2_news_article.txt";

const findings = {};
const wsFrames = []; // structurally-meaningful inbound frames, timestamped
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
        // Keep the structurally-meaningful envelopes. tool-call-* carry the
        // dispatched tool name; tool-payload-warning is the confirmation gate.
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
          // Pull out the tool name if present.
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
            // keep a compact preview but enough to read params / numbers
            preview: t.slice(0, isChunk ? 200 : 1200),
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

async function chatText(page) {
  // Grab all visible agent message bubbles' text.
  return page.evaluate(() => {
    const nodes = [
      ...document.querySelectorAll(
        "[data-testid='chat-message'],[data-testid='agent-message'],[data-role='assistant']",
      ),
    ];
    // Fallback: grab the whole chat scroll region text.
    const scroll =
      document.querySelector("[data-testid='chat-scroll']") ||
      document.querySelector("[data-testid='chat-messages']");
    return {
      bubbles: nodes.map((n) => n.textContent?.trim()).filter(Boolean),
      scroll_text: scroll ? scroll.textContent : null,
    };
  });
}

async function main() {
  await mkdir(OUT_DIR, { recursive: true });
  const articleRaw = await readFile(ARTICLE_PATH, "utf8");
  // Drop the SYNTHETIC banner lines so the agent sees the article body itself.
  const articleBody = articleRaw
    .split("\n")
    .filter((l) => !l.startsWith("SYNTHETIC FIXTURE") && !l.startsWith("This article is"))
    .join("\n")
    .trim();

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

  console.log("=== job-0235 Case 2 E2E LIVE ===");
  console.log("BASE_URL:", BASE_URL, "OUT:", OUT_DIR);

  try {
    // ---- Step 1: auth -> new case ----------------------------------------
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
    // Ensure we are inside the CaseView (chat input present).
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
    await page.screenshot({ path: `${OUT_DIR}/01_new_case.png`, fullPage: false });
    console.log("[STEP1] new case ready");

    // ---- Step 2: paste article + ask -------------------------------------
    const prompt =
      "Model the groundwater contamination from this spill:\n\n" + articleBody;
    await dismissSaveGate(page);
    const chatInput = page.locator('[data-testid="chat-input"]');
    await chatInput.click();
    await chatInput.fill(prompt);
    findings.prompt_chars = prompt.length;
    const sendT = Date.now();
    await chatInput.press("Enter");
    await page.waitForTimeout(600);
    await dismissSaveGate(page);
    // Capture the thinking indicator opportunistically (first ~few seconds).
    await page.waitForTimeout(1500);
    const thinkingPresent = await page
      .locator('[data-testid="thinking-indicator"]')
      .count();
    if (thinkingPresent > 0) {
      await page.screenshot({
        path: `${OUT_DIR}/02_thinking_indicator.png`,
        fullPage: false,
      });
      console.log("[STEP2] thinking indicator captured");
    }
    console.log("[STEP2] prompt sent, chars:", prompt.length);

    // ---- Step 3: watch for confirmation gate vs MODFLOW dispatch ---------
    // Poll up to 12 min for EITHER the payload-warning gate OR a modflow/run
    // tool card / pipeline. Record which came first (the critical ordering).
    //
    // PATIENCE: the FIRST live session after a restart pays a cold-cache
    // penalty — building the Gemini CachedContent alone takes ~75s before the
    // generation even streams. We therefore DO NOT treat "no running cards +
    // input enabled" as idle until we have seen at least one real agent signal
    // (an agent-message-chunk, a tool-call-start, OR the payload-warning gate).
    // While a `gemini_generate` pipeline-state is the most recent frame, the
    // agent is busy regardless of the input's disabled attribute.
    let gateSeenT = null;
    let modflowDispatchT = null;
    let gateFirst = null;
    let watchHeartbeat = -1;
    const watchT0 = Date.now();
    const WATCH_MS = 720000;
    const seenRealAgentSignal = () =>
      wsFrames.some(
        (f) =>
          f.type === "agent-message-chunk" ||
          f.type === "agent-message" ||
          f.type === "tool-call-start" ||
          f.type === "tool-payload-warning" ||
          f.type === "interaction-request" ||
          f.type === "confirmation-request" ||
          f.type === "error",
      );
    const geminiStillGenerating = () => {
      // Most-recent pipeline-state names gemini_generate AND we have not yet
      // seen a real downstream signal.
      const lastPipeline = [...wsFrames]
        .reverse()
        .find((f) => f.type === "pipeline-state");
      if (!lastPipeline) return false;
      return /gemini_generate|llm_generation/i.test(
        JSON.stringify(lastPipeline.tool_name ?? lastPipeline.preview ?? ""),
      );
    };
    while (Date.now() - watchT0 < WATCH_MS) {
      // Gate present?
      const gate = await page
        .locator('[data-testid="payload-warning-inline"]')
        .count();
      if (gate > 0 && gateSeenT === null) {
        gateSeenT = Date.now();
      }
      // MODFLOW/solver dispatch observed in WS frames (tool-call-start with a
      // modflow/groundwater tool) OR a pipeline card named accordingly.
      const modflowFrame = wsFrames.find(
        (f) =>
          (f.type === "tool-call-start" || f.type === "pipeline-state") &&
          typeof JSON.stringify(f.tool_name) === "string" &&
          /modflow|groundwater/i.test(JSON.stringify(f.tool_name ?? "")),
      );
      const cards = await snapshotCards(page);
      const modflowCard = cards.find((c) =>
        /modflow|groundwater|plume|contaminat/i.test(c.name ?? ""),
      );
      if ((modflowFrame || modflowCard) && modflowDispatchT === null) {
        modflowDispatchT = Date.now();
      }
      // Decide ordering as soon as we have the first of the two.
      if (gateFirst === null && (gateSeenT || modflowDispatchT)) {
        if (gateSeenT && !modflowDispatchT) gateFirst = true;
        else if (modflowDispatchT && !gateSeenT) gateFirst = false;
        else if (gateSeenT && modflowDispatchT)
          gateFirst = gateSeenT <= modflowDispatchT;
      }
      // Stop watching once the gate is up (we then approve), OR once MODFLOW
      // has clearly dispatched without a gate (bypass).
      if (gateSeenT) break;
      if (modflowDispatchT && Date.now() - modflowDispatchT > 8000) {
        // MODFLOW running for >8s and still no gate -> bypass observed.
        break;
      }
      // Also stop if the turn went fully idle with neither (e.g. agent refused
      // / asked a clarifying question / errored). BUT do NOT call it idle while
      // Gemini is still generating (cold-cache build can take >75s with no
      // running pipeline card AND an enabled input), and not until we have seen
      // at least one real downstream agent signal.
      const idle = await page.evaluate(() => {
        const el = document.querySelector('[data-testid="chat-input"]');
        const running = document.querySelectorAll(
          "[data-testid='pipeline-card'][data-state='running']",
        ).length;
        return {
          disabled: el?.disabled ?? null,
          running,
        };
      });
      const generating = geminiStillGenerating();
      const sawSignal = seenRealAgentSignal();
      if (
        !gateSeenT &&
        !modflowDispatchT &&
        !generating &&
        sawSignal &&
        idle.running === 0 &&
        idle.disabled === false &&
        Date.now() - sendT > 25000
      ) {
        // Quiet for a while with nothing more coming -> capture and stop.
        await page.waitForTimeout(4000);
        const stillGate = await page
          .locator('[data-testid="payload-warning-inline"]')
          .count();
        if (stillGate === 0) break;
      }
      // Heartbeat log every ~15s so the monitor shows liveness during long waits.
      if (Math.floor((Date.now() - watchT0) / 15000) !== watchHeartbeat) {
        watchHeartbeat = Math.floor((Date.now() - watchT0) / 15000);
        console.log(
          `[STEP3-wait] t=${Math.round(
            (Date.now() - watchT0) / 1000,
          )}s generating=${generating} sawSignal=${sawSignal} gate=${!!gateSeenT} modflow=${!!modflowDispatchT} frames=${wsFrames.length}`,
        );
      }
      await page.waitForTimeout(1500);
    }

    findings.ordering = {
      gate_seen_rel_ms: gateSeenT ? gateSeenT - t0Global : null,
      modflow_dispatch_rel_ms: modflowDispatchT
        ? modflowDispatchT - t0Global
        : null,
      gate_before_dispatch: gateFirst,
    };

    await page.screenshot({
      path: `${OUT_DIR}/03_after_prompt_state.png`,
      fullPage: false,
    });
    const cardsAtGate = await snapshotCards(page);
    console.log(
      "[STEP3] ordering:",
      JSON.stringify(findings.ordering),
      "cards:",
      JSON.stringify(cardsAtGate),
    );

    // ---- Step 3b: if gate present, capture + approve ---------------------
    const gateCount = await page
      .locator('[data-testid="payload-warning-inline"]')
      .count();
    findings.confirmation_gate_present = gateCount > 0;
    if (gateCount > 0) {
      // Capture the gate text (derived params + demo-aquifer caveat).
      const gateInfo = await page.evaluate(() => {
        const card = document.querySelector(
          '[data-testid="payload-warning-inline"]',
        );
        const tool = document.querySelector(
          '[data-testid="payload-warning-tool"]',
        )?.textContent;
        const rec = document.querySelector(
          '[data-testid="payload-warning-recommendation"]',
        )?.textContent;
        return { full_text: card?.textContent ?? null, tool, recommendation: rec };
      });
      findings.gate_info = gateInfo;
      await page.screenshot({
        path: `${OUT_DIR}/04_confirmation_gate.png`,
        fullPage: false,
      });
      console.log("[STEP3b] GATE present. tool:", gateInfo.tool);
      console.log("[STEP3b] recommendation:", gateInfo.recommendation);

      // Approve (Proceed).
      const proceed = page.locator(
        '[data-testid="payload-warning-button-proceed"]',
      );
      if ((await proceed.count()) > 0) {
        await proceed.click({ timeout: 8000 }).catch(() => {});
        console.log("[STEP3b] Proceed clicked");
      } else {
        console.log("[STEP3b] NO proceed button found on gate");
      }
      await page.waitForTimeout(2000);
    } else {
      console.log(
        "[STEP3b] NO confirmation gate appeared (possible BYPASS or alternate routing)",
      );
    }

    // ---- Step 4: wait for MODFLOW completion + plume layer ---------------
    const baselineMap = await snapshotMap(page);
    const baselineIds = new Set((baselineMap?.layers ?? []).map((l) => l.id));
    let plumeSeen = null;
    const solveT0 = Date.now();
    const SOLVE_MS = 20 * 60 * 1000; // 20 min budget per kickoff
    let lastProgressShot = 0;
    while (Date.now() - solveT0 < SOLVE_MS) {
      const map = await snapshotMap(page);
      const newLayers = (map?.layers ?? []).filter(
        (l) => !baselineIds.has(l.id),
      );
      // raster overlay or any new non-basemap layer
      const overlay = newLayers.find(
        (l) =>
          l.type === "raster" ||
          /plume|modflow|conc|contaminat|gwt/i.test(l.id) ||
          l.type === "fill",
      );
      // Periodic progress screenshots (every ~45s).
      if (Date.now() - lastProgressShot > 45000) {
        lastProgressShot = Date.now();
        await page
          .screenshot({
            path: `${OUT_DIR}/05_progress_${Math.round(
              (Date.now() - solveT0) / 1000,
            )}s.png`,
            fullPage: false,
          })
          .catch(() => {});
      }
      if (overlay) {
        plumeSeen = { layer: overlay, all_new: newLayers, map };
        break;
      }
      // If the agent went idle (no running cards, input enabled) AND there are
      // failed cards -> solver failed; stop.
      const st = await page.evaluate(() => {
        const running = document.querySelectorAll(
          "[data-testid='pipeline-card'][data-state='running']",
        ).length;
        const failed = document.querySelectorAll(
          "[data-testid='pipeline-card'][data-state='failed']",
        ).length;
        const el = document.querySelector('[data-testid="chat-input"]');
        return { running, failed, disabled: el?.disabled ?? null };
      });
      if (st.running === 0 && st.disabled === false && st.failed > 0) {
        console.log("[STEP4] solver/pipeline FAILED card detected, stopping");
        break;
      }
      // Idle with no new layer and no running cards after a grace window.
      if (
        st.running === 0 &&
        st.disabled === false &&
        Date.now() - solveT0 > 30000
      ) {
        // give one more grace pass for the map to settle
        await page.waitForTimeout(5000);
        const map2 = await snapshotMap(page);
        const nl2 = (map2?.layers ?? []).filter((l) => !baselineIds.has(l.id));
        if (
          !nl2.find(
            (l) =>
              l.type === "raster" ||
              l.type === "fill" ||
              /plume|modflow/i.test(l.id),
          )
        ) {
          console.log("[STEP4] idle, no plume layer materialized, stopping");
          break;
        }
      }
      await page.waitForTimeout(3000);
    }

    findings.plume = plumeSeen
      ? {
          layer_id: plumeSeen.layer.id,
          layer_type: plumeSeen.layer.type,
          new_layer_count: plumeSeen.all_new.length,
          map_center: plumeSeen.map.center,
          map_zoom: plumeSeen.map.zoom,
        }
      : { materialized: false };

    // ---- Step 5: open layer panel + capture final state ------------------
    // Try to open the layer panel (toggle).
    const layerToggle = page.locator(
      '[data-testid="grace2-layer-panel-toggle"], [data-testid="layer-panel-toggle"], [aria-label*="layer" i]',
    );
    if ((await layerToggle.count()) > 0) {
      await layerToggle.first().click({ timeout: 4000 }).catch(() => {});
      await page.waitForTimeout(800);
    }
    await page.waitForTimeout(1500);
    await page.screenshot({
      path: `${OUT_DIR}/06_final_plume_map.png`,
      fullPage: false,
    });

    // Capture narration text.
    const narration = await chatText(page);
    findings.narration = {
      bubble_count: narration.bubbles.length,
      scroll_text_tail: (narration.scroll_text ?? "").slice(-2500),
    };

    // Extract any concentration / area numbers from narration for the assert.
    const txt = (narration.scroll_text ?? "").toLowerCase();
    findings.narration_geo = {
      mentions_idaho: /idaho|twin falls|snake river/.test(txt),
      mentions_florida: /florida|fort myers|tampa|miami/.test(txt),
      mentions_concentration:
        /mg\/l|concentration|µg\/l|ug\/l|ppb|ppm/.test(txt),
      mentions_area: /km²|km2|square kilomet|area|extent|plume/.test(txt),
    };

    // Final map center plausibility (Idaho roughly lon -116..-111, lat 42..45).
    const finalMap = await snapshotMap(page);
    findings.final_map = finalMap
      ? {
          center: finalMap.center,
          zoom: finalMap.zoom,
          layer_ids: finalMap.layers.map((l) => l.id),
          in_idaho_bbox:
            finalMap.center.lng > -118 &&
            finalMap.center.lng < -110 &&
            finalMap.center.lat > 41 &&
            finalMap.center.lat < 46,
          in_florida_bbox:
            finalMap.center.lng > -88 &&
            finalMap.center.lng < -79 &&
            finalMap.center.lat > 24 &&
            finalMap.center.lat < 31,
        }
      : null;

    const finalCards = await snapshotCards(page);
    findings.final_cards = finalCards;
    console.log("[STEP5] final cards:", JSON.stringify(finalCards));
    console.log("[STEP5] plume:", JSON.stringify(findings.plume));
    console.log("[STEP5] narration_geo:", JSON.stringify(findings.narration_geo));
    console.log("[STEP5] final_map:", JSON.stringify(findings.final_map));
  } catch (e) {
    findings.fatal_error = String(e && e.stack ? e.stack : e);
    console.error("FATAL:", e);
    await page
      .screenshot({ path: `${OUT_DIR}/99_fatal.png`, fullPage: false })
      .catch(() => {});
  } finally {
    findings.page_errors = errs.slice(0, 50);
    findings.ws_frames = wsFrames;
    await writeFile(
      `${OUT_DIR}/findings.json`,
      JSON.stringify(findings, null, 2),
    );
    await writeFile(
      `${OUT_DIR}/ws_frames.json`,
      JSON.stringify(wsFrames, null, 2),
    );
    await ctx.close().catch(() => {});
    await browser.close();
    console.log("=== COMPLETE — findings.json + ws_frames.json written ===");
  }
}

main().catch((e) => {
  console.error("OUTER FAILURE:", e);
  process.exit(1);
});
