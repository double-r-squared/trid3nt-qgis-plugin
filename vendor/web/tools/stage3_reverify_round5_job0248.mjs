#!/usr/bin/env node
// GRACE-2 — job-0248 Stage 3 FINAL re-verify ROUND 5 — LIVE, ONE browser session.
//
// Runs after the hot-set fix (commit 5026784): code_exec_request joined HOT_SET_TOOLS,
// so the round-4 OutOfAllowedSetError false "cannot run Python" is fixed. Agent already
// restarted on it (:8765, 89 tools) — NOT restarted by this harness.
//
// THREE gating scenarios (overall PASS = R + B(P5) + C all PASS):
//   R  — Case-2 render proof (ZERO Gemini turns): open the ROUND-3 plume case in the UI,
//        screenshot the layer panel + map, assert via __grace2GetMap. WMS GetMap render
//        proof of the plume layer is captured OUT OF BAND (bash) — this scenario just
//        documents the UI rehydration behavior (panel may be empty if the case record's
//        loaded_layer_summaries predates the publish — kickoff-anticipated fallback).
//   B  — P5 + analysis + charts (FRESH Case): EXPLICIT Pelicun prompt naming NSI + the
//        existing Fort Myers flood depth layer -> ImpactPanel headline numbers. If the
//        agent asks ONE clarification, answer "use the NSI inventory" (1 extra turn ok).
//        Then: structure count -> chart -> refresh+replay.
//   C  — sandbox LIVE gate (FRESH Case): numpy [1,5,9,12] -> code-exec-request ->
//        SandboxCard -> Proceed -> LOCAL exec -> status=ok -> narration (mean 6.75, max 12).
//        WS ordering: code-exec-request BEFORE code-exec-result.
//
// LIVE-DRIVEN ONLY: NO __grace2Inject* seams. Read-only __grace2GetMap permitted.
// <=8 Gemini turns total. On ANY 429 -> stop, mark remaining BLOCKED. ~120s between scenarios.

import { chromium } from "@playwright/test";
import { mkdir, writeFile } from "fs/promises";

const OUT_DIR =
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0248-testing-20260610/evidence";
const BASE_URL = "http://localhost:5173";
const ROUND3_CASE_ID = "01KTRNN4P2M2J11SJ79CBSY3MZ"; // round-3 plume case
const PLUME_LAYER = "plume-concentration-01KTRNPCV4NEN0RRQ3H0QMZQY6";

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
          "code-exec-result", "case-open", "case-list",
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
        if (type === "user-message" || type === "tool-payload-confirmation" || type === "chat-message" ||
            type === "create-case" || type === "open-case" || type === "case-command")
          wsFrames.push({ t_rel_ms: rel(), type: `SENT:${type}`, preview: t.slice(0, 400) });
      } catch {}
    });
  });
}

const GW_RE = /twin.?falls|groundwater|trichloro|\btce\b|modflow|contaminat|gwt|snake river/i;
function scanRoute(re, startIdx = 0) {
  const hits = [];
  for (let i = startIdx; i < wsFrames.length; i++) {
    const f = wsFrames[i];
    if (typeof f.type === "string" && f.type.startsWith("SENT:")) continue;
    const hay = JSON.stringify(f.tool_name ?? "") + " " + (f.preview ?? "");
    if (re.test(hay)) hits.push({ idx: i, type: f.type, tool_name: f.tool_name, snippet: (f.preview ?? "").slice(0, 220) });
  }
  return hits;
}
const FLOOD_RE = /flood|sfincs|pelicun|damage|inundat|fort myers|depth|impact|nsi|structure/i;

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
    return { scroll_text: scroll ? scroll.textContent : null };
  });
}

async function layerPanel(page) {
  return page.evaluate(() => {
    const panel =
      document.querySelector("[data-testid='grace2-layer-panel']") ||
      document.querySelector("[data-testid='grace2-case-view-layer-panel-wrap']");
    const emptyMarker = document.querySelector("[data-testid='grace2-case-view-empty-layers']");
    const rows = [...document.querySelectorAll(
      "[data-testid='grace2-layer-row'],[data-testid^='grace2-layer-item'],[data-testid^='grace2-layer-toggle']")];
    return {
      panel_present: !!panel,
      empty_layers_marker: !!emptyMarker,
      panel_text: panel ? (panel.textContent ?? "").slice(0, 800) : null,
      row_count: rows.length,
      rows: rows.map((r) => (r.textContent ?? "").trim()).filter(Boolean).slice(0, 12),
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
  // A terminal pipeline-state still carries tool_name=gemini_generate but has
  // state in {complete,cancelled,failed}. Treat those as NOT generating —
  // otherwise the heuristic latches forever after a text-only terminal turn
  // (round-3/4 artifact). Generating only when the LAST pipeline-state is a
  // gemini/llm step that is NOT in a terminal state.
  let generating = false;
  if (lastPipeline) {
    const pp = lastPipeline.preview ?? "";
    const isGen = /gemini_generate|llm_generation/i.test(JSON.stringify(lastPipeline.tool_name ?? "") + " " + pp);
    const terminalState = /"state"\s*:\s*"(complete|completed|cancelled|canceled|failed|done|success)"/i.test(pp);
    generating = isGen && !terminalState;
  }
  // A terminal agent-message-chunk (done:true) AFTER the last user send is a hard
  // "turn finished narrating" signal — overrides any stale generating latch.
  const lastChunk = [...wsFrames].reverse().find((f) => f.type === "agent-message-chunk");
  if (lastChunk && /"done"\s*:\s*true/i.test(lastChunk.preview ?? "")) generating = false;
  const sawSignal = wsFrames.some((f) =>
    ["agent-message-chunk","agent-message","tool-call-start","tool-payload-warning","interaction-request","code-exec-request","impact-envelope","error"].includes(f.type));
  const lastInbound = [...wsFrames].reverse().find((f) => typeof f.type === "string" && !f.type.startsWith("SENT:"));
  const msSinceFrame = lastInbound ? rel() - lastInbound.t_rel_ms : Infinity;
  return { ...dom, generating, sawSignal, msSinceFrame };
}
async function waitForTurnSettle(page, { sendT, maxMs = 600000, label = "" }) {
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
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  let page = await ctx.newPage();
  const errs = [];
  page.on("pageerror", (e) => errs.push(`pageerror: ${e.message}`));
  page.on("console", (msg) => { if (msg.type() === "error") errs.push(`console.error: ${msg.text()}`); });
  logWS(page);

  console.log("=== job-0248 Stage 3 FINAL re-verify ROUND 5 — LIVE ONE-SESSION (R + B + C) ===");
  let turns = 0;

  try {
    // ---- bootstrap: auth ----
    await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('[data-testid="grace2-auth-gate"]', { timeout: 15000 }).catch(() => null);
    const anonBtn = page.locator('[data-testid="grace2-auth-gate-anonymous"]');
    if ((await anonBtn.count()) > 0) { await anonBtn.click(); await page.waitForTimeout(1200); }
    await page.waitForSelector('[data-testid="grace2-app-shell"]', { timeout: 10000 });
    await page.waitForTimeout(1000);

    // ============================================================= SCENARIO R — Case-2 render (ZERO Gemini)
    console.log("\n##### SCENARIO R — Case-2 render proof (ZERO Gemini turns) #####");
    findings.scenarios.R = {};
    await gotoCasesRoot(page);
    await page.screenshot({ path: `${OUT_DIR}/R00_cases_root.png` });
    // Find the round-3 case row. Anon sessions may not list it (per-session list);
    // capture how many rows are visible + their text. We still document rehydration.
    const rRows = page.locator('[data-testid="grace2-case-row"]');
    const rRowCount = await rRows.count();
    findings.scenarios.R.case_rows_visible = rRowCount;
    const rowTexts = [];
    for (let i = 0; i < rRowCount; i++) rowTexts.push(((await rRows.nth(i).textContent().catch(() => "")) ?? "").trim().slice(0, 80));
    findings.scenarios.R.case_row_texts = rowTexts.slice(0, 25);
    await flush();
    // Try to open the round-3 case by deep-link (set active case via URL hash if app supports it),
    // else open the first/oldest row. We rely on the UI; the authoritative render proof is the
    // out-of-band WMS GetMap (scenarioR_render_proof.json).
    let openedRound3 = false;
    // Attempt deep-link open of the specific case id (read-only navigation, NOT an inject seam).
    await page.goto(`${BASE_URL}/?case=${ROUND3_CASE_ID}`, { waitUntil: "domcontentloaded" }).catch(() => {});
    await page.waitForTimeout(1500);
    const anonBtn2 = page.locator('[data-testid="grace2-auth-gate-anonymous"]');
    if ((await anonBtn2.count()) > 0) { await anonBtn2.click(); await page.waitForTimeout(1200); }
    await page.waitForTimeout(1500);
    await dismissSaveGate(page);
    // Did a case-open frame for the round-3 case arrive?
    const r3Open = wsFrames.find((f) => f.type === "case-open" && (f.preview ?? "").includes(ROUND3_CASE_ID));
    findings.scenarios.R.deeplink_case_open_seen = !!r3Open;
    if (r3Open) findings.scenarios.R.case_open_preview = r3Open.preview;
    const inCaseView = (await page.locator('[data-testid="grace2-case-view"]').count()) > 0;
    findings.scenarios.R.in_case_view_after_deeplink = inCaseView;
    if (!inCaseView && rRowCount > 0) {
      // fall back: open the OLDEST row (round-3 case is older). Rows usually newest-first.
      await gotoCasesRoot(page).catch(() => {});
      const rows2 = page.locator('[data-testid="grace2-case-row"]');
      const n = await rows2.count();
      if (n > 0) { await rows2.nth(n - 1).click({ timeout: 8000 }).catch(() => {}); await page.waitForTimeout(2000); await dismissSaveGate(page); }
    }
    await page.waitForTimeout(2500);
    findings.scenarios.R.layer_panel = await layerPanel(page);
    findings.scenarios.R.map = await snapshotMap(page);
    // assert: does the map carry a plume/qgis overlay + is the view near Idaho?
    const rmap = findings.scenarios.R.map;
    if (rmap) {
      const layerIds = rmap.layers.map((l) => l.id);
      findings.scenarios.R.map_has_qgis_overlay = layerIds.some((id) => /qgis|wms|plume/i.test(id));
      findings.scenarios.R.map_layer_ids = layerIds;
      findings.scenarios.R.map_in_idaho =
        rmap.center.lng > -117.5 && rmap.center.lng < -111 && rmap.center.lat > 42 && rmap.center.lat < 49;
    }
    await page.screenshot({ path: `${OUT_DIR}/R01_case_open.png`, fullPage: false });
    console.log("[R] layer_panel rows:", findings.scenarios.R.layer_panel.row_count,
      "deeplink case-open:", findings.scenarios.R.deeplink_case_open_seen,
      "map center:", rmap ? `${rmap.center.lng.toFixed(2)},${rmap.center.lat.toFixed(2)}` : null);
    await flush();
    if (rateLimited) { findings.rate_limited_after = "R"; throw new Error("RATE_LIMITED"); }

    console.log("[boundary] spacing ~30s before Scenario B...");
    await page.waitForTimeout(30000);

    // ============================================================= SCENARIO B — P5 + analysis + charts
    console.log("\n##### SCENARIO B — P5 + analysis + charts (FRESH Case) #####");
    findings.scenarios.B = {};
    await newCase(page);
    await page.screenshot({ path: `${OUT_DIR}/B01_new_case.png` });

    const bFramesStart = wsFrames.length;
    // Self-sufficient P5 prompt: explicitly model the flood depth FIRST (no
    // "existing layer" trap in a fresh case), then assess damage with Pelicun
    // on the NSI inventory. Names the asset inventory so no clarification is
    // needed. Honors the kickoff P5 intent (NSI + Pelicun -> ImpactPanel).
    const P5_PROMPT = "Model a flood scenario for Fort Myers, Florida, then run a Pelicun damage assessment on the resulting flood depth layer using the USACE NSI building inventory. Show me the impact summary.";
    const sendB1 = await sendPrompt(page, P5_PROMPT);
    turns++;
    findings.scenarios.B.b_frames_start = bFramesStart;
    findings.scenarios.B.prompt = P5_PROMPT;
    await flush();

    // settle / route watch; if clarification arrives, answer once.
    let answeredClarify = false;
    {
      const b1T0 = Date.now();
      const B1_MAX = 720000;
      let bhb = -1;
      while (Date.now() - b1T0 < B1_MAX) {
        if (rateLimited) break;
        const st = await turnState(page);
        // settle?
        if (st.sawSignal && !st.generating && st.running === 0 && st.disabled === false &&
            st.msSinceFrame > QUIESCE_MS && Date.now() - sendB1 > 20000) {
          await page.waitForTimeout(4000);
          const st2 = await turnState(page);
          if (st2.running === 0 && st2.disabled === false && !st2.generating && st2.msSinceFrame > QUIESCE_MS) {
            // Did we get an impact panel? if not and not yet answered a clarification, check chat for a question
            const impactNow = await page.locator('[data-testid="grace2-impact-panel"]').count();
            const tail = ((await chatText(page)).scroll_text ?? "").slice(-1200);
            const asksClarify = /\?\s*$/.test(tail.trim()) || /which|should i|would you like|inventory|footprint|confirm|clarif/i.test(tail.slice(-400));
            if (impactNow === 0 && !answeredClarify && asksClarify) {
              console.log("[B1] agent asked a clarification; answering 'use the NSI inventory'");
              await sendPrompt(page, "Use the NSI inventory.");
              turns++;
              answeredClarify = true;
              await flush();
              await page.waitForTimeout(8000);
              continue; // keep waiting for the impact chain
            }
            console.log("[B1] settled (impactPanel=" + impactNow + ")");
            break;
          }
        }
        const bhbi = Math.floor((Date.now() - b1T0) / 20000);
        if (bhbi !== bhb) { bhb = bhbi; console.log(`[B1 t=${Math.round((Date.now()-b1T0)/1000)}s] gen=${st.generating} run=${st.running} dis=${st.disabled} quietMs=${Math.round(st.msSinceFrame)} frames=${wsFrames.length} answeredClarify=${answeredClarify}`); }
        await page.waitForTimeout(1500);
      }
    }
    await page.waitForTimeout(3000);

    // If a payload-warning gate appeared (large flood/NSI response), Proceed it.
    {
      const gate = page.locator('[data-testid="payload-warning-inline"]');
      if ((await gate.count()) > 0) {
        const proceed = page.locator('[data-testid="payload-warning-button-proceed"]');
        if ((await proceed.count()) > 0) { await proceed.click({ timeout: 8000 }).catch(() => {}); console.log("[B1] payload-warning Proceed clicked"); }
        await page.waitForTimeout(2000);
        await waitForTurnSettle(page, { sendT: sendB1, maxMs: 480000, label: "B1-postgate" });
        await page.waitForTimeout(3000);
      }
    }

    const gwHits = scanRoute(GW_RE, bFramesStart);
    const floodHits = scanRoute(FLOOD_RE, bFramesStart);
    findings.scenarios.B.groundwater_hit_count = gwHits.length;
    findings.scenarios.B.flood_route_hit_count = floodHits.length;
    findings.scenarios.B.flood_route_sample = floodHits.slice(0, 8);
    findings.scenarios.B.clarification_answered = answeredClarify;

    const impactCount = await page.locator('[data-testid="grace2-impact-panel"]').count();
    findings.scenarios.B.impact_panel_present = impactCount > 0;
    findings.scenarios.B.impact_envelope_frame = !!wsFrames.slice(bFramesStart).find((f) => f.type === "impact-envelope");
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
      await page.screenshot({ path: `${OUT_DIR}/B03_impact_panel_P5.png`, fullPage: false });
      console.log("[B1] IMPACT PANEL:", JSON.stringify(impact).slice(0, 400));
    } else {
      await page.screenshot({ path: `${OUT_DIR}/B03_no_impact_panel.png` });
      findings.scenarios.B.b1_scroll_tail = ((await chatText(page)).scroll_text ?? "").slice(-2600);
    }
    findings.scenarios.B.b1_map = await snapshotMap(page);
    await flush();
    if (rateLimited) { findings.rate_limited_after = "B1"; throw new Error("RATE_LIMITED"); }

    const doB23 = impactCount > 0;
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
      findings.scenarios.B.count_has_number = /\b\d[\d,]*\b/.test(tB2.slice(-1200));
      await page.screenshot({ path: `${OUT_DIR}/B04_count_answer.png` });
      await flush();
      if (rateLimited) { findings.rate_limited_after = "B2"; throw new Error("RATE_LIMITED"); }

      // B3: chart emission
      await page.waitForTimeout(8000);
      const b3FramesStart = wsFrames.length;
      const sendB3 = await sendPrompt(page, "Show me the damage distribution as a chart.");
      turns++;
      await flush();
      const rB3 = await waitForTurnSettle(page, { sendT: sendB3, maxMs: 300000, label: "B3" });
      console.log("[B3] settle:", rB3);
      await page.waitForTimeout(3000);
      const chartStackCount = await page.locator('[data-testid="chart-stack"]').count();
      findings.scenarios.B.chart_stack_present = chartStackCount > 0;
      findings.scenarios.B.chart_emission_frame = !!wsFrames.slice(b3FramesStart).find((f) => f.type === "chart-emission");
      await page.screenshot({ path: `${OUT_DIR}/B05_chart_stack.png` });
      if (chartStackCount > 0) {
        await page.locator('[data-testid="chart-stack-top-card"]').first().click({ timeout: 5000 })
          .catch(async () => { await page.locator('[data-testid="chart-stack"]').first().click({ timeout: 5000 }).catch(() => {}); });
        await page.waitForTimeout(1500);
        findings.scenarios.B.gallery_opened = (await page.locator('[data-testid="chart-gallery"]').count()) > 0;
        await page.screenshot({ path: `${OUT_DIR}/B06_chart_gallery.png` });
        const close = page.locator('[data-testid="chart-gallery-close"]');
        if ((await close.count()) > 0) await close.click({ timeout: 4000 }).catch(() => {});
        await page.waitForTimeout(800);
      }
      await flush();
      if (rateLimited) { findings.rate_limited_after = "B3"; throw new Error("RATE_LIMITED"); }
    } else {
      console.log("[B] B1 produced no impact panel; SKIPPING B2/B3, proceeding to C.");
      findings.scenarios.B.b23_skipped = true;
      findings.scenarios.B.b23_skip_reason = "no_impact_panel";
    }

    console.log("[boundary] spacing ~120s before Scenario C...");
    await page.waitForTimeout(120000);

    // ============================================================= SCENARIO C — sandbox LIVE gate
    console.log("\n##### SCENARIO C — sandbox LIVE gate (local mode) #####");
    findings.scenarios.C = {};
    await newCase(page);
    await page.screenshot({ path: `${OUT_DIR}/C00_new_case.png` });
    const cFramesStart = wsFrames.length;
    const sendC = await sendPrompt(page,
      "Run a quick Python computation: compute the mean and max of the numpy array [1, 5, 9, 12] and print both.");
    turns++;
    findings.scenarios.C.c_frames_start = cFramesStart;
    await flush();
    let sandboxReqT = null, execReqFrameIdx = null;
    const cWatch0 = Date.now();
    while (Date.now() - cWatch0 < 300000) {
      if (rateLimited) break;
      const sbCount = await page.locator('[data-testid="sandbox-card"]').count();
      if (sbCount > 0 && sandboxReqT === null) {
        if ((await page.locator('[data-testid="sandbox-card-proceed"]').count()) > 0) sandboxReqT = Date.now();
      }
      if (execReqFrameIdx === null) {
        const idx = wsFrames.findIndex((f, i) => i >= cFramesStart && f.type === "code-exec-request");
        if (idx >= 0) execReqFrameIdx = idx;
      }
      if (sandboxReqT) break;
      const st = await turnState(page);
      if (!sandboxReqT && st.sawSignal && !st.generating && st.running === 0 && st.disabled === false &&
          st.msSinceFrame > QUIESCE_MS && Date.now() - sendC > 30000) {
        await page.waitForTimeout(4000);
        if ((await page.locator('[data-testid="sandbox-card-proceed"]').count()) === 0) break;
      }
      await page.waitForTimeout(1500);
    }
    const cGwHits = scanRoute(GW_RE, cFramesStart);
    findings.scenarios.C.groundwater_hit_count = cGwHits.length;
    findings.scenarios.C.sandbox_request_present = sandboxReqT !== null;
    findings.scenarios.C.code_exec_request_frame = execReqFrameIdx !== null;
    findings.scenarios.C.code_exec_request_idx = execReqFrameIdx;
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
      // expand stdout section if collapsed
      const stToggle = page.locator('[data-testid="sandbox-card-stdout-toggle"]');
      if ((await stToggle.count()) > 0) await stToggle.click({ timeout: 4000 }).catch(() => {});
      await page.waitForTimeout(800);
      const statusChip = await page.locator('[data-testid="sandbox-card-status-chip"]').first().textContent().catch(() => null);
      findings.scenarios.C.result_status_chip = statusChip;
      const stdoutContent = await page.locator('[data-testid="sandbox-card-stdout-content"]').first().textContent().catch(() => null);
      const resultDescriptor = await page.locator('[data-testid="sandbox-card-result-descriptor"], [data-testid="sandbox-result-scalar"], [data-testid="sandbox-result-json"]').first().textContent().catch(() => null);
      findings.scenarios.C.result_stdout = (stdoutContent ?? "").slice(0, 500);
      findings.scenarios.C.result_descriptor = (resultDescriptor ?? "").slice(0, 300);
      findings.scenarios.C.c_scroll_tail = ((await chatText(page)).scroll_text ?? "").slice(-1800);
      const reqIdx = wsFrames.findIndex((f, i) => i >= cFramesStart && f.type === "code-exec-request");
      const resIdx = wsFrames.findIndex((f, i) => i >= cFramesStart && f.type === "code-exec-result");
      findings.scenarios.C.code_exec_result_idx = resIdx;
      findings.scenarios.C.code_exec_result_frame = resIdx >= 0;
      findings.scenarios.C.request_before_execution =
        reqIdx >= 0 && resIdx >= 0 ? reqIdx < resIdx : (reqIdx >= 0 ? true : null);
      findings.scenarios.C.code_exec_result_frame_preview = resIdx >= 0 ? wsFrames[resIdx].preview : null;
      const ct = (findings.scenarios.C.c_scroll_tail ?? "") + " " + (findings.scenarios.C.result_stdout ?? "") + " " + (findings.scenarios.C.result_descriptor ?? "");
      findings.scenarios.C.has_mean_675 = /6\.75|6,75/.test(ct);
      findings.scenarios.C.has_max_12 = /\b12(\.0+)?\b/.test(ct);
      await page.screenshot({ path: `${OUT_DIR}/C02_sandbox_result.png` });
      console.log("[C] result status:", statusChip, "ordering req<res:", findings.scenarios.C.request_before_execution,
        "mean6.75:", findings.scenarios.C.has_mean_675, "max12:", findings.scenarios.C.has_max_12);
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
      const anonBtn3 = page.locator('[data-testid="grace2-auth-gate-anonymous"]');
      if ((await anonBtn3.count()) > 0) { await anonBtn3.click(); await page.waitForTimeout(1500); }
      await page.waitForSelector('[data-testid="grace2-app-shell"]', { timeout: 10000 }).catch(() => {});
      await page.waitForTimeout(1500);
      const rows = page.locator('[data-testid="grace2-case-row"]');
      const rowCount = await rows.count();
      findings.scenarios.B.reload_case_rows = rowCount;
      let clicked = false;
      for (let i = 0; i < rowCount; i++) {
        const txt = (await rows.nth(i).textContent().catch(() => "")) ?? "";
        if (/fort myers|flood|damage|pelicun/i.test(txt)) { await rows.nth(i).click({ timeout: 8000 }).catch(() => {}); clicked = true; break; }
      }
      if (!clicked && rowCount > 0) { await rows.nth(0).click({ timeout: 8000 }).catch(() => {}); }
      await page.waitForTimeout(2500);
      await dismissSaveGate(page);
      await page.waitForTimeout(2500);
      findings.scenarios.B.chart_replay_after_reload = await page.locator('[data-testid="chart-stack"]').count();
      findings.scenarios.B.impact_replay_after_reload = await page.locator('[data-testid="grace2-impact-panel"]').count();
      await page.screenshot({ path: `${OUT_DIR}/B07_reload_replay.png` });
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
    await flush();
    await ctx.close().catch(() => {});
    await browser.close();
    console.log(`=== COMPLETE — turns_sent=${turns} — findings.json + ws_frames.json written ===`);
  }
}

main().catch((e) => { console.error("OUTER FAILURE:", e); process.exit(1); });
