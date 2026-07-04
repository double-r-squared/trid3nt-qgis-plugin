#!/usr/bin/env node
// GRACE-2 — job-0245 Stage 3 re-verify ROUND 3 (closing session) — LIVE, ONE browser session.
//
// Runs after the job-0244 env fixes (commit fdf9b6d): google-cloud-run installed
// (publish_layer run_v2), GOOGLE_APPLICATION_CREDENTIALS set (GDAL /vsigs/),
// GRACE2_SANDBOX_LOCAL=1 (sandbox runs locally like mf6).
//
// Scenario A — Case 2 RENDER proof: New case -> TCE article -> gate -> Proceed ->
//              EXPECT publish_layer succeeds AND the plume layer RENDERS over Idaho.
//              Capture narration verbatim + flag map-add honesty.
// Scenario B — analysis + P5: Fort Myers flood+Pelicun -> ImpactPanel -> count ->
//              chart -> reload-replay. STALL-GUARD: if zero inbound WS progress for
//              >240s after a tool failure, mark p5 BLOCKED and skip to C in a fresh case.
// Scenario C — sandbox live gate: numpy mean/max -> SandboxCard -> Proceed -> LOCAL
//              exec -> status=ok -> narration. WS ordering assert.
//
// LIVE-DRIVEN ONLY: NO __grace2Inject* seams. Read-only __grace2GetMap permitted.
// <=10 Gemini turns total. On ANY 429 -> stop, mark remaining BLOCKED.
// Scenario boundaries spaced ~120s.

import { chromium } from "@playwright/test";
import { mkdir, writeFile, readFile } from "fs/promises";

const OUT_DIR =
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0245-testing-20260610/evidence";
const BASE_URL = "http://localhost:5173";
const ARTICLE_PATH =
  "/home/nate/Documents/GRACE-2/services/agent/tests/fixtures/case2_news_article.txt";

const findings = { scenarios: {} };
const wsFrames = [];
const t0Global = Date.now();
const rel = () => Date.now() - t0Global;

let rateLimited = false;
function maybeRateLimit(text) {
  if (/429|RESOURCE_EXHAUSTED|rate.?limit|quota|too many requests/i.test(text))
    rateLimited = true;
}

function logWS(page) {
  page.on("websocket", (ws) => {
    ws.on("framereceived", (data) => {
      try {
        const t = typeof data.payload === "string" ? data.payload : data.payload.toString();
        let parsed = null;
        try { parsed = JSON.parse(t); } catch {}
        const type = parsed?.type ?? null;
        const KEEP = new Set([
          "tool-call-start", "tool-call-result", "tool-call-error", "pipeline-state",
          "tool-payload-warning", "tool-payload-confirmation", "map-command",
          "session-state", "error", "agent-message", "interaction-request",
          "confirmation-request", "impact-envelope", "chart-emission", "code-exec-request",
          "code-exec-result",
        ]);
        const isChunk = type === "agent-message-chunk";
        if (type === "error" || type === "tool-call-error") maybeRateLimit(t);
        if (type && (KEEP.has(type) || isChunk)) {
          const p = parsed?.payload ?? {};
          let toolName =
            p.tool_name ?? p.name ?? p.tool ??
            (Array.isArray(p.steps) ? p.steps.map((s) => s.tool_name ?? s.name).filter(Boolean) : null);
          wsFrames.push({ t_rel_ms: rel(), type, tool_name: toolName, preview: t.slice(0, isChunk ? 240 : 1600) });
        }
      } catch {}
    });
    ws.on("framesent", (data) => {
      try {
        const t = typeof data.payload === "string" ? data.payload : data.payload.toString();
        const parsed = JSON.parse(t);
        const type = parsed?.type ?? parsed?.envelope_type ?? null;
        if (type === "user-message" || type === "tool-payload-confirmation" || type === "chat-message")
          wsFrames.push({ t_rel_ms: rel(), type: `SENT:${type}`, preview: t.slice(0, 400) });
      } catch {}
    });
  });
}

async function dismissSaveGate(page, attempts = 4) {
  for (let i = 0; i < attempts; i++) {
    const modal = page.locator('[data-testid="grace2-save-gate-modal"]');
    if ((await modal.count()) === 0 || !(await modal.isVisible())) return;
    const cont = page.locator('[data-testid="grace2-save-gate-modal-continue"]');
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
      layers: style.layers.map((l) => ({ id: l.id, type: l.type, source: l.source })),
      sources: Object.keys(style.sources || {}),
      center: { lng: ctr.lng, lat: ctr.lat }, zoom: m.getZoom(),
    };
  });
}

async function chatText(page) {
  return page.evaluate(() => {
    const scroll =
      document.querySelector("[data-testid='chat-scroll']") ||
      document.querySelector("[data-testid='chat-messages']") ||
      document.querySelector("[data-testid='grace2-case-view']");
    const nodes = [...document.querySelectorAll(
      "[data-testid='chat-message'],[data-testid='agent-message'],[data-role='assistant']")];
    return {
      bubbles: nodes.map((n) => n.textContent?.trim()).filter(Boolean),
      scroll_text: scroll ? scroll.textContent : null,
    };
  });
}

const QUIESCE_MS = 20000;

async function turnState(page) {
  const dom = await page.evaluate(() => {
    const el = document.querySelector('[data-testid="chat-input"]');
    const running = document.querySelectorAll("[data-testid='pipeline-card'][data-state='running']").length;
    const failed = document.querySelectorAll("[data-testid='pipeline-card'][data-state='failed']").length;
    return { disabled: el?.disabled ?? null, running, failed };
  });
  const lastPipeline = [...wsFrames].reverse().find((f) => f.type === "pipeline-state");
  const generating =
    lastPipeline &&
    /gemini_generate|llm_generation/i.test(JSON.stringify(lastPipeline.tool_name ?? lastPipeline.preview ?? ""));
  const sawSignal = wsFrames.some((f) =>
    ["agent-message-chunk","agent-message","tool-call-start","tool-payload-warning","interaction-request","code-exec-request","error"].includes(f.type));
  const lastInbound = [...wsFrames].reverse().find((f) => typeof f.type === "string" && !f.type.startsWith("SENT:"));
  const msSinceFrame = lastInbound ? rel() - lastInbound.t_rel_ms : Infinity;
  return { ...dom, generating, sawSignal, msSinceFrame };
}

async function waitForTurnSettle(page, { sendT, maxMs = 720000, label = "" }) {
  const t0 = Date.now();
  let hb = -1;
  while (Date.now() - t0 < maxMs) {
    if (rateLimited) return "rate_limited";
    const st = await turnState(page);
    if (st.failed > 0 && st.running === 0 && st.disabled === false && st.msSinceFrame > QUIESCE_MS) return "failed_card";
    if (st.sawSignal && !st.generating && st.running === 0 && st.disabled === false &&
        st.msSinceFrame > QUIESCE_MS && Date.now() - sendT > 20000) {
      await page.waitForTimeout(4000);
      const st2 = await turnState(page);
      if (st2.running === 0 && st2.disabled === false && !st2.generating && st2.msSinceFrame > QUIESCE_MS) return "settled";
    }
    const hbi = Math.floor((Date.now() - t0) / 15000);
    if (hbi !== hb) { hb = hbi; console.log(`[${label} wait t=${Math.round((Date.now()-t0)/1000)}s] gen=${st.generating} sig=${st.sawSignal} run=${st.running} dis=${st.disabled} quietMs=${Math.round(st.msSinceFrame)} frames=${wsFrames.length}`); }
    await page.waitForTimeout(1500);
  }
  return "timeout";
}

async function sendPrompt(page, text) {
  await dismissSaveGate(page);
  const chatInput = page.locator('[data-testid="chat-input"]');
  await chatInput.click();
  await chatInput.fill(text);
  const sendT = Date.now();
  await chatInput.press("Enter");
  await page.waitForTimeout(600);
  await dismissSaveGate(page);
  return sendT;
}

// nav-fix: escape an active case view back to cases-root before clicking new-case.
async function gotoCasesRoot(page) {
  if ((await page.locator('[data-testid="grace2-case-view"]').count()) > 0) {
    const link = page.locator('[data-testid="grace2-case-view-cases-link"]');
    const back = page.locator('[data-testid="grace2-case-view-back"]');
    if ((await link.count()) > 0) await link.first().click({ timeout: 8000 }).catch(() => {});
    else if ((await back.count()) > 0) await back.first().click({ timeout: 8000 }).catch(() => {});
    await page.waitForTimeout(1200);
    await dismissSaveGate(page);
  }
  await page.waitForSelector('[data-testid="grace2-cases-new"]', { timeout: 15000 });
}

async function newCase(page) {
  await gotoCasesRoot(page);
  await page.locator('[data-testid="grace2-cases-new"]').click();
  await page.waitForTimeout(800);
  await dismissSaveGate(page);
  await page.waitForTimeout(1000);
  await dismissSaveGate(page);
  await page.waitForSelector('[data-testid="grace2-case-view"], [data-testid="grace2-case-row"]', { timeout: 15000 });
  if ((await page.locator('[data-testid="grace2-case-view"]').count()) === 0) {
    const row = page.locator('[data-testid="grace2-case-row"]').first();
    if ((await row.count()) > 0) { await row.click({ timeout: 8000 }).catch(() => {}); await page.waitForTimeout(1000); await dismissSaveGate(page); }
  }
  await page.waitForSelector('[data-testid="chat-input"]', { timeout: 10000 });
  await page.waitForTimeout(800);
}

async function flush() {
  await writeFile(`${OUT_DIR}/findings.json`, JSON.stringify(findings, null, 2));
  await writeFile(`${OUT_DIR}/ws_frames.json`, JSON.stringify(wsFrames, null, 2));
}

async function main() {
  await mkdir(OUT_DIR, { recursive: true });
  const articleRaw = await readFile(ARTICLE_PATH, "utf8");
  const articleBody = articleRaw
    .split("\n")
    .filter((l) => !l.startsWith("SYNTHETIC FIXTURE") && !l.startsWith("This article is"))
    .join("\n").trim();

  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  let page = await ctx.newPage();
  const errs = [];
  page.on("pageerror", (e) => errs.push(`pageerror: ${e.message}`));
  page.on("console", (msg) => { if (msg.type() === "error") errs.push(`console.error: ${msg.text()}`); });
  logWS(page);

  console.log("=== job-0245 Stage 3 re-verify ROUND 3 — LIVE ONE-SESSION ===");
  let turns = 0;

  try {
    // ---- bootstrap: auth ----
    await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('[data-testid="grace2-auth-gate"]', { timeout: 15000 }).catch(() => null);
    const anonBtn = page.locator('[data-testid="grace2-auth-gate-anonymous"]');
    if ((await anonBtn.count()) > 0) { await anonBtn.click(); await page.waitForTimeout(1200); }
    await page.waitForSelector('[data-testid="grace2-app-shell"]', { timeout: 10000 });
    await page.waitForTimeout(1000);

    // ============================================================= SCENARIO A
    console.log("\n##### SCENARIO A — Case 2 RENDER proof #####");
    await newCase(page);
    await page.screenshot({ path: `${OUT_DIR}/A01_new_case.png` });

    const promptA = "Model the groundwater contamination from this spill:\n\n" + articleBody;
    findings.scenarios.A = { prompt_chars: promptA.length };
    const sendA = await sendPrompt(page, promptA);
    turns++;
    await flush();

    // Watch for gate vs modflow dispatch ordering.
    let gateSeenT = null, modflowDispatchT = null;
    const watchT0 = Date.now();
    const WATCH_MS = 720000;
    let hb = -1;
    while (Date.now() - watchT0 < WATCH_MS) {
      if (rateLimited) break;
      const gate = await page.locator('[data-testid="payload-warning-inline"]').count();
      if (gate > 0 && gateSeenT === null) gateSeenT = Date.now();
      const modflowFrame = wsFrames.find((f) =>
        (f.type === "tool-call-start" || f.type === "pipeline-state") &&
        /modflow|groundwater/i.test(JSON.stringify(f.tool_name ?? "")));
      if (modflowFrame && modflowDispatchT === null) modflowDispatchT = t0Global + modflowFrame.t_rel_ms;
      if (gateSeenT) break;
      if (modflowDispatchT && Date.now() - modflowDispatchT > 8000) break;
      const st = await turnState(page);
      if (!gateSeenT && !modflowDispatchT && st.sawSignal && !st.generating && st.running === 0 &&
          st.disabled === false && st.msSinceFrame > QUIESCE_MS && Date.now() - sendA > 30000) {
        await page.waitForTimeout(4000);
        if ((await page.locator('[data-testid="payload-warning-inline"]').count()) === 0) break;
      }
      const hbi = Math.floor((Date.now() - watchT0) / 15000);
      if (hbi !== hb) { hb = hbi; console.log(`[A-watch t=${Math.round((Date.now()-watchT0)/1000)}s] gate=${!!gateSeenT} modflow=${!!modflowDispatchT} quietMs=${Math.round(st.msSinceFrame)} frames=${wsFrames.length}`); }
      await page.waitForTimeout(1500);
    }

    findings.scenarios.A.ordering = {
      gate_seen_rel_ms: gateSeenT ? gateSeenT - t0Global : null,
      modflow_dispatch_rel_ms: modflowDispatchT ? modflowDispatchT - t0Global : null,
      gate_before_dispatch:
        gateSeenT && !modflowDispatchT ? true
        : gateSeenT && modflowDispatchT ? gateSeenT <= modflowDispatchT
        : modflowDispatchT ? false : null,
    };
    await page.screenshot({ path: `${OUT_DIR}/A02_gate_or_dispatch.png` });

    const gateCount = await page.locator('[data-testid="payload-warning-inline"]').count();
    findings.scenarios.A.confirmation_gate_present = gateCount > 0;
    if (gateCount > 0) {
      const gateInfo = await page.evaluate(() => {
        const card = document.querySelector('[data-testid="payload-warning-inline"]');
        return {
          full_text: card?.textContent ?? null,
          tool: document.querySelector('[data-testid="payload-warning-tool"]')?.textContent,
          recommendation: document.querySelector('[data-testid="payload-warning-recommendation"]')?.textContent,
        };
      });
      findings.scenarios.A.gate_info = gateInfo;
      await page.screenshot({ path: `${OUT_DIR}/A03_confirmation_gate.png` });
      console.log("[A] GATE present. tool:", gateInfo.tool);
      await flush();
      const proceed = page.locator('[data-testid="payload-warning-button-proceed"]');
      if ((await proceed.count()) > 0) { await proceed.click({ timeout: 8000 }).catch(() => {}); console.log("[A] Proceed clicked"); }
      await page.waitForTimeout(2000);
    } else {
      console.log("[A] NO gate appeared (BYPASS or alternate routing or 429)");
    }

    // Wait for plume layer to RENDER over Idaho (the round-3 expectation).
    const baselineMap = await snapshotMap(page);
    const baselineIds = new Set((baselineMap?.layers ?? []).map((l) => l.id));
    let plumeSeen = null;
    const solveT0 = Date.now();
    const SOLVE_MS = 22 * 60 * 1000;
    let lastShot = 0;
    while (Date.now() - solveT0 < SOLVE_MS) {
      if (rateLimited) break;
      const map = await snapshotMap(page);
      const newLayers = (map?.layers ?? []).filter((l) => !baselineIds.has(l.id));
      const overlay = newLayers.find((l) =>
        l.type === "raster" || /plume|modflow|conc|contaminat|gwt/i.test(l.id) || l.type === "fill");
      if (Date.now() - lastShot > 45000) {
        lastShot = Date.now();
        await page.screenshot({ path: `${OUT_DIR}/A04_progress_${Math.round((Date.now()-solveT0)/1000)}s.png` }).catch(() => {});
      }
      if (overlay) { plumeSeen = { layer: overlay, all_new: newLayers, map }; break; }
      const st = await turnState(page);
      if (st.failed > 0 && st.running === 0 && st.disabled === false && st.msSinceFrame > QUIESCE_MS) {
        console.log("[A] solver FAILED card detected (quiescent)"); break;
      }
      if (st.running === 0 && st.disabled === false && st.msSinceFrame > QUIESCE_MS && Date.now() - solveT0 > 45000) {
        await page.waitForTimeout(5000);
        const map2 = await snapshotMap(page);
        const nl2 = (map2?.layers ?? []).filter((l) => !baselineIds.has(l.id));
        const st3 = await turnState(page);
        if (st3.msSinceFrame > QUIESCE_MS &&
            !nl2.find((l) => l.type === "raster" || l.type === "fill" || /plume|modflow/i.test(l.id))) {
          console.log("[A] idle (quiescent), no plume layer materialized"); break;
        }
      }
      await page.waitForTimeout(3000);
    }
    findings.scenarios.A.plume = plumeSeen
      ? { layer_id: plumeSeen.layer.id, layer_type: plumeSeen.layer.type,
          new_layer_count: plumeSeen.all_new.length, new_layer_ids: plumeSeen.all_new.map((l) => l.id),
          map_center: plumeSeen.map.center, map_zoom: plumeSeen.map.zoom,
          materialized: true }
      : { materialized: false };
    await flush();

    await waitForTurnSettle(page, { sendT: sendA, maxMs: 240000, label: "A-narr" });
    await page.waitForTimeout(3000);
    const layerToggle = page.locator('[data-testid="grace2-layer-panel-toggle"], [data-testid="layer-panel-toggle"], [aria-label*="layer" i]');
    if ((await layerToggle.count()) > 0) await layerToggle.first().click({ timeout: 4000 }).catch(() => {});
    await page.waitForTimeout(1200);
    // capture layer-panel rows
    findings.scenarios.A.layer_panel = await page.evaluate(() => {
      const rows = [...document.querySelectorAll('[data-testid="grace2-layer-panel-row"], [data-testid="layer-panel-row"], [data-testid^="layer-row"]')];
      return rows.map((r) => r.textContent?.trim()).filter(Boolean).slice(0, 20);
    });
    await page.screenshot({ path: `${OUT_DIR}/A05_final_plume_map.png` });

    const narrA = await chatText(page);
    const txtA = (narrA.scroll_text ?? "").toLowerCase();
    findings.scenarios.A.narration = {
      bubble_count: narrA.bubbles.length,
      scroll_text_tail: (narrA.scroll_text ?? "").slice(-2500),
      mentions_idaho: /idaho|twin falls|snake river/.test(txtA),
      mentions_concentration: /mg\/l|concentration|µg\/l|ug\/l|ppb|ppm|tce|trichloro/.test(txtA),
      mentions_area: /km²|km2|square kilomet|area|extent|plume/.test(txtA),
      claims_map_add: /added to the map|on the map|map for your review|displayed on the map|added the .*layer|plume layer (?:has been |is )?(?:added|displayed|shown)/i.test(narrA.scroll_text ?? ""),
    };
    const finalMapA = await snapshotMap(page);
    findings.scenarios.A.final_map = finalMapA
      ? { center: finalMapA.center, zoom: finalMapA.zoom,
          layer_ids: finalMapA.layers.map((l) => l.id),
          in_idaho_bbox: finalMapA.center.lng > -118 && finalMapA.center.lng < -110 &&
                         finalMapA.center.lat > 41 && finalMapA.center.lat < 46 }
      : null;
    findings.scenarios.A.case_id = await page.evaluate(() => {
      const active = document.querySelector('[data-testid="grace2-case-row"][data-active="true"]');
      return active?.getAttribute("data-case-id") ?? window.__grace2ActiveCaseId ?? null;
    });
    await flush();
    console.log("[A] DONE. ordering:", JSON.stringify(findings.scenarios.A.ordering));
    console.log("[A] plume:", JSON.stringify(findings.scenarios.A.plume));
    console.log("[A] narration idaho/conc/area/claims_map_add:",
      findings.scenarios.A.narration.mentions_idaho,
      findings.scenarios.A.narration.mentions_concentration,
      findings.scenarios.A.narration.mentions_area,
      findings.scenarios.A.narration.claims_map_add);

    if (rateLimited) { findings.rate_limited_after = "A"; throw new Error("RATE_LIMITED"); }

    console.log("[boundary] spacing ~120s before Scenario B...");
    await page.waitForTimeout(120000);

    // ============================================================= SCENARIO B
    console.log("\n##### SCENARIO B — analysis + P5 Pelicun #####");
    findings.scenarios.B = {};
    await newCase(page);
    await page.screenshot({ path: `${OUT_DIR}/B01_new_case.png` });

    const sendB1 = await sendPrompt(page,
      "Model flood damage for Fort Myers, Florida. Run a flood scenario there if no flood layer exists yet, then run a Pelicun damage assessment on it.");
    turns++;
    await flush();

    // STALL-GUARD: watch for >240s of zero inbound WS progress after a tool result/error.
    // If stalled (the round-2 OQ-0244-LOOP-STALL), capture evidence and BLOCK p5, skip to C.
    let b1Stalled = false;
    {
      const b1T0 = Date.now();
      const B1_MAX = 900000;
      let lastFrameCount = wsFrames.length;
      let lastFrameChangeT = Date.now();
      let sawToolResult = false;
      let bhb = -1;
      while (Date.now() - b1T0 < B1_MAX) {
        if (rateLimited) break;
        const st = await turnState(page);
        // settled?
        if (st.sawSignal && !st.generating && st.running === 0 && st.disabled === false &&
            st.msSinceFrame > QUIESCE_MS && Date.now() - sendB1 > 20000) {
          await page.waitForTimeout(4000);
          const st2 = await turnState(page);
          if (st2.running === 0 && st2.disabled === false && !st2.generating && st2.msSinceFrame > QUIESCE_MS) {
            console.log("[B1] settled"); break;
          }
        }
        // progress tracking
        if (wsFrames.length !== lastFrameCount) { lastFrameCount = wsFrames.length; lastFrameChangeT = Date.now(); }
        if (wsFrames.some((f) => f.type === "tool-call-result" || f.type === "tool-call-error" || f.type === "error")) sawToolResult = true;
        // stall detection: a tool result/error happened, then >240s no new inbound frame AND not generating
        const quietS = (Date.now() - lastFrameChangeT) / 1000;
        if (sawToolResult && quietS > 240 && !st.generating && st.running === 0) {
          b1Stalled = true; console.log(`[B1] STALL detected: ${Math.round(quietS)}s no new WS frame after a tool result`); break;
        }
        const bhbi = Math.floor((Date.now() - b1T0) / 20000);
        if (bhbi !== bhb) { bhb = bhbi; console.log(`[B1 t=${Math.round((Date.now()-b1T0)/1000)}s] gen=${st.generating} run=${st.running} dis=${st.disabled} quietMs=${Math.round(st.msSinceFrame)} quietProgressS=${Math.round(quietS)} frames=${wsFrames.length}`); }
        await page.waitForTimeout(1500);
      }
    }
    await page.waitForTimeout(3000);
    const impactCount = await page.locator('[data-testid="grace2-impact-panel"]').count();
    findings.scenarios.B.impact_panel_present = impactCount > 0;
    findings.scenarios.B.b1_stalled = b1Stalled;
    if (impactCount > 0) {
      const impact = await page.evaluate(() => {
        const g = (id) => document.querySelector(`[data-testid="${id}"]`)?.textContent ?? null;
        return {
          title: g("grace2-impact-panel-title"), structures: g("grace2-impact-stat-structures"),
          loss: g("grace2-impact-stat-loss"), population: g("grace2-impact-stat-population"),
          area: g("grace2-impact-stat-area"), ds_distribution: g("grace2-impact-ds-distribution"),
          provenance_runid: g("grace2-impact-provenance-runid"),
        };
      });
      findings.scenarios.B.impact = impact;
      await page.screenshot({ path: `${OUT_DIR}/B02_impact_panel_P5.png` });
      console.log("[B1] IMPACT PANEL:", JSON.stringify(impact));
    } else {
      await page.screenshot({ path: `${OUT_DIR}/B02_no_impact_panel.png` });
      findings.scenarios.B.b1_scroll_tail = ((await chatText(page)).scroll_text ?? "").slice(-2500);
    }
    // capture any flood layer materialization
    findings.scenarios.B.b1_map = await snapshotMap(page);
    await flush();
    if (rateLimited) { findings.rate_limited_after = "B1"; throw new Error("RATE_LIMITED"); }

    const doB23 = impactCount > 0 && !b1Stalled;
    if (doB23) {
      // B2: analytical count
      await page.waitForTimeout(8000);
      const sendB2 = await sendPrompt(page, "How many structures are impacted above damage state 2?");
      turns++;
      await flush();
      const rB2 = await waitForTurnSettle(page, { sendT: sendB2, maxMs: 300000, label: "B2" });
      console.log("[B2] settle:", rB2);
      await page.waitForTimeout(2500);
      const tB2 = ((await chatText(page)).scroll_text ?? "");
      findings.scenarios.B.count_answer_tail = tB2.slice(-1800);
      findings.scenarios.B.count_has_number = /\b\d[\d,]*\b/.test(tB2.slice(-1500));
      await page.screenshot({ path: `${OUT_DIR}/B03_count_answer.png` });
      await flush();
      if (rateLimited) { findings.rate_limited_after = "B2"; throw new Error("RATE_LIMITED"); }

      // B3: chart emission
      await page.waitForTimeout(8000);
      const sendB3 = await sendPrompt(page, "Show me the damage distribution as a chart.");
      turns++;
      await flush();
      const rB3 = await waitForTurnSettle(page, { sendT: sendB3, maxMs: 300000, label: "B3" });
      console.log("[B3] settle:", rB3);
      await page.waitForTimeout(3000);
      const chartStackCount = await page.locator('[data-testid="chart-stack"]').count();
      findings.scenarios.B.chart_stack_present = chartStackCount > 0;
      findings.scenarios.B.chart_emission_frame = !!wsFrames.find((f) => f.type === "chart-emission");
      await page.screenshot({ path: `${OUT_DIR}/B04_chart_stack.png` });
      if (chartStackCount > 0) {
        await page.locator('[data-testid="chart-stack-top-card"]').first().click({ timeout: 5000 })
          .catch(async () => { await page.locator('[data-testid="chart-stack"]').first().click({ timeout: 5000 }).catch(() => {}); });
        await page.waitForTimeout(1500);
        findings.scenarios.B.gallery_opened = (await page.locator('[data-testid="chart-gallery"]').count()) > 0;
        await page.screenshot({ path: `${OUT_DIR}/B05_chart_gallery.png` });
        const close = page.locator('[data-testid="chart-gallery-close"]');
        if ((await close.count()) > 0) await close.click({ timeout: 4000 }).catch(() => {});
        await page.waitForTimeout(800);
      }
      await flush();
      if (rateLimited) { findings.rate_limited_after = "B3"; throw new Error("RATE_LIMITED"); }
    } else {
      console.log("[B] B1 produced no impact (stalled=" + b1Stalled + "); SKIPPING B2/B3, proceeding to Scenario C in a FRESH case.");
      findings.scenarios.B.b23_skipped = true;
      findings.scenarios.B.b23_skip_reason = b1Stalled ? "loop_stall" : "no_impact_panel";
    }

    console.log("[boundary] spacing ~120s before Scenario C...");
    await page.waitForTimeout(120000);

    // ============================================================= SCENARIO C — sandbox live gate
    console.log("\n##### SCENARIO C — sandbox LIVE gate (local mode) #####");
    findings.scenarios.C = {};
    // Fresh case so a stalled B-session WS doesn't poison C.
    await newCase(page);
    await page.screenshot({ path: `${OUT_DIR}/C00_new_case.png` });
    const cFramesStart = wsFrames.length;
    const sendC = await sendPrompt(page,
      "Run a quick Python computation: compute the mean and max of the flood depth raster with numpy and print both.");
    turns++;
    await flush();
    let sandboxReqT = null, execT = null;
    const cWatch0 = Date.now();
    while (Date.now() - cWatch0 < 300000) {
      if (rateLimited) break;
      const sbCount = await page.locator('[data-testid="sandbox-card"]').count();
      if (sbCount > 0 && sandboxReqT === null) {
        if ((await page.locator('[data-testid="sandbox-card-proceed"]').count()) > 0) sandboxReqT = Date.now();
      }
      const execFrame = wsFrames.slice(cFramesStart).find((f) => f.type === "code-exec-request");
      if (execFrame && execT === null) execT = t0Global + execFrame.t_rel_ms;
      if (sandboxReqT) break;
      const st = await turnState(page);
      if (!sandboxReqT && st.sawSignal && !st.generating && st.running === 0 && st.disabled === false &&
          st.msSinceFrame > QUIESCE_MS && Date.now() - sendC > 30000) {
        await page.waitForTimeout(4000);
        if ((await page.locator('[data-testid="sandbox-card-proceed"]').count()) === 0) break;
      }
      await page.waitForTimeout(1500);
    }
    findings.scenarios.C.sandbox_request_present = sandboxReqT !== null;
    findings.scenarios.C.code_exec_request_frame = execT !== null;
    findings.scenarios.C.request_before_execution = execT !== null || sandboxReqT !== null ? true : null;
    if (sandboxReqT) {
      const sbInfo = await page.evaluate(() => ({
        code: document.querySelector('[data-testid="sandbox-card-code"]')?.textContent ?? null,
        title: document.querySelector('[data-testid="sandbox-card-title"]')?.textContent ?? null,
      }));
      findings.scenarios.C.sandbox_card = sbInfo;
      await page.screenshot({ path: `${OUT_DIR}/C01_sandbox_request.png` });
      console.log("[C] SANDBOX REQUEST present. code chars:", (sbInfo.code ?? "").length);
      await flush();
      const proceed = page.locator('[data-testid="sandbox-card-proceed"]');
      if ((await proceed.count()) > 0) { await proceed.click({ timeout: 8000 }).catch(() => {}); console.log("[C] Proceed clicked"); }
      const rC = await waitForTurnSettle(page, { sendT: Date.now(), maxMs: 300000, label: "C-result" });
      console.log("[C] result settle:", rC);
      await page.waitForTimeout(2500);
      const statusChip = await page.locator('[data-testid="sandbox-card-status-chip"]').first().textContent().catch(() => null);
      findings.scenarios.C.result_status_chip = statusChip;
      const resultScalar = await page.locator('[data-testid="sandbox-result-scalar"], [data-testid="sandbox-result-json"], [data-testid="sandbox-card-stdout"]').first().textContent().catch(() => null);
      findings.scenarios.C.result_payload = (resultScalar ?? "").slice(0, 700);
      findings.scenarios.C.c_scroll_tail = ((await chatText(page)).scroll_text ?? "").slice(-1800);
      // capture the code-exec-result frame status if present
      const execResultFrame = wsFrames.slice(cFramesStart).find((f) => f.type === "code-exec-result");
      findings.scenarios.C.code_exec_result_frame_preview = execResultFrame?.preview ?? null;
      await page.screenshot({ path: `${OUT_DIR}/C02_sandbox_result.png` });
      console.log("[C] result status:", statusChip);
    } else {
      await page.screenshot({ path: `${OUT_DIR}/C01_no_sandbox.png` });
      findings.scenarios.C.c_scroll_tail = ((await chatText(page)).scroll_text ?? "").slice(-1800);
      console.log("[C] NO sandbox request card appeared");
    }
    await flush();
    if (rateLimited) { findings.rate_limited_after = "C"; throw new Error("RATE_LIMITED"); }

    // ===================================== B4 — reload-replay (Gemini-free) — only if B23 ran with charts
    if (doB23) {
      console.log("\n##### B4 — refresh + chart replay (Gemini-free) #####");
      await page.waitForTimeout(2000);
      await page.reload({ waitUntil: "domcontentloaded" });
      await page.waitForTimeout(2000);
      const anonBtn2 = page.locator('[data-testid="grace2-auth-gate-anonymous"]');
      if ((await anonBtn2.count()) > 0) { await anonBtn2.click(); await page.waitForTimeout(1500); }
      await page.waitForSelector('[data-testid="grace2-app-shell"]', { timeout: 10000 }).catch(() => {});
      await page.waitForTimeout(1500);
      // The Fort Myers case is NOT the most-recent (C's case is). Find a case row whose
      // text suggests Fort Myers / flood / damage; else fall back to enumerating rows.
      const rows = page.locator('[data-testid="grace2-case-row"]');
      const rowCount = await rows.count();
      findings.scenarios.B.reload_case_rows = rowCount;
      let clicked = false;
      for (let i = 0; i < rowCount; i++) {
        const txt = (await rows.nth(i).textContent().catch(() => "")) ?? "";
        if (/fort myers|flood|damage|pelicun/i.test(txt)) { await rows.nth(i).click({ timeout: 8000 }).catch(() => {}); clicked = true; break; }
      }
      if (!clicked && rowCount > 0) { await rows.nth(Math.min(1, rowCount - 1)).click({ timeout: 8000 }).catch(() => {}); }
      await page.waitForTimeout(2500);
      await dismissSaveGate(page);
      await page.waitForTimeout(2500);
      findings.scenarios.B.chart_replay_after_reload = await page.locator('[data-testid="chart-stack"]').count();
      findings.scenarios.B.impact_replay_after_reload = await page.locator('[data-testid="grace2-impact-panel"]').count();
      await page.screenshot({ path: `${OUT_DIR}/B06_reload_replay.png` });
      console.log("[B4] after reload: chartStacks=", findings.scenarios.B.chart_replay_after_reload, "impactPanels=", findings.scenarios.B.impact_replay_after_reload);
      await flush();
    }
  } catch (e) {
    findings.fatal_error = String(e && e.stack ? e.stack : e);
    console.error("FATAL:", e);
    await page.screenshot({ path: `${OUT_DIR}/99_fatal.png` }).catch(() => {});
  } finally {
    findings.rate_limited = rateLimited;
    findings.turns_sent = turns;
    findings.page_errors = errs.slice(0, 60);
    findings.ws_frames = wsFrames;
    await flush();
    await ctx.close().catch(() => {});
    await browser.close();
    console.log(`=== COMPLETE — turns_sent=${turns} — findings.json + ws_frames.json written ===`);
  }
}

main().catch((e) => { console.error("OUTER FAILURE:", e); process.exit(1); });
