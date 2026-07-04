#!/usr/bin/env node
// GRACE-2 — job-0250 Stage 3 ROUND 6 — P5-ONLY, LIVE, ONE browser session.
//
// Runs after the HydroMT staging fix (commits df7b4ba + e715dba): gs:// catalog
// inputs are STAGED to local files, so the round-5 cache-hit
// "No such file found: /vsigs/.../dem.tif" failure (OQ-0248-FLOOD-BUILD-VSIGS)
// is reportedly gone. Agent already restarted on the fix (:8765, 89 tools) — NOT
// restarted by this harness.
//
// THE scenario (FRESH Case), per kickoff:
//   P5 — "Run a flood damage assessment for Fort Myers with Pelicun using the NSI
//        building inventory and the existing Fort Myers flood depth layer."
//        If asked ONE clarification -> "use the NSI inventory" (1 extra turn ok).
//        EXPECT chain completes (fresh SFINCS build now works OR Pelicun runs
//        against the existing flood layer; either path OK) -> ImpactPanel slides
//        out with headline numbers. [P5 EVIDENCE]
//        The SFINCS leg may take up to 20 min (cloud solve) — wait, screenshot.
//   B2 — "How many structures are impacted above damage state 2?" -> count
//        consistent with panel.
//   B3 — "Show me the damage distribution as a chart." -> chart-emission ->
//        ChartStack inline -> click -> gallery.
//   B4 — Browser refresh + reselect Case -> chart replay (Gemini-free).
//
// LIVE-DRIVEN ONLY: NO __grace2Inject* seams. Read-only __grace2GetMap permitted.
// <=7 Gemini turns total. On ANY 429 -> stop, mark remaining BLOCKED.

import { chromium } from "@playwright/test";
import { mkdir, writeFile } from "fs/promises";

const OUT_DIR =
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0250-testing-20260610/evidence";
const BASE_URL = "http://localhost:5173";

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

const QUIESCE_MS = 20000;
async function turnState(page) {
  const dom = await page.evaluate(() => {
    const el = document.querySelector('[data-testid="chat-input"]');
    const running = document.querySelectorAll("[data-testid='pipeline-card'][data-state='running']").length;
    const failed = document.querySelectorAll("[data-testid='pipeline-card'][data-state='failed']").length;
    return { disabled: el?.disabled ?? null, running, failed };
  });
  const lastPipeline = [...wsFrames].reverse().find((f) => f.type === "pipeline-state");
  // Round-5 settle heuristic (KEPT): a terminal pipeline-state still carries
  // tool_name=gemini_generate but has state in {complete,cancelled,failed} —
  // treat those as NOT generating, otherwise the heuristic latches forever
  // after a text-only terminal turn (round-3/4 artifact).
  let generating = false;
  if (lastPipeline) {
    const pp = lastPipeline.preview ?? "";
    const isGen = /gemini_generate|llm_generation/i.test(JSON.stringify(lastPipeline.tool_name ?? "") + " " + pp);
    const terminalState = /"state"\s*:\s*"(complete|completed|cancelled|canceled|failed|done|success)"/i.test(pp);
    generating = isGen && !terminalState;
  }
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

async function readImpact(page) {
  return page.evaluate(() => {
    const g = (id) => document.querySelector(`[data-testid="${id}"]`)?.textContent ?? null;
    const dsRows = [...document.querySelectorAll('[data-testid^="grace2-impact-ds-row-"]')]
      .map((r) => (r.textContent ?? "").trim()).filter(Boolean);
    return {
      title: g("grace2-impact-panel-title"), structures: g("grace2-impact-stat-structures"),
      loss: g("grace2-impact-stat-loss"), population: g("grace2-impact-stat-population"),
      area: g("grace2-impact-stat-area"),
      ds_distribution: g("grace2-impact-ds-distribution"), ds_rows: dsRows,
      provenance_runid: g("grace2-impact-provenance-runid"),
    };
  });
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

  console.log("=== job-0250 Stage 3 ROUND 6 — P5-ONLY LIVE (impact -> count -> chart -> replay) ===");
  let turns = 0;

  try {
    // ---- bootstrap: auth ----
    await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('[data-testid="grace2-auth-gate"]', { timeout: 15000 }).catch(() => null);
    const anonBtn = page.locator('[data-testid="grace2-auth-gate-anonymous"]');
    if ((await anonBtn.count()) > 0) { await anonBtn.click(); await page.waitForTimeout(1200); }
    await page.waitForSelector('[data-testid="grace2-app-shell"]', { timeout: 10000 });
    await page.waitForTimeout(1000);

    // ============================================================= SCENARIO P5 — flood+Pelicun -> ImpactPanel
    console.log("\n##### P5 — flood damage assessment (FRESH Case) #####");
    findings.scenarios.B = {};
    await newCase(page);
    await page.screenshot({ path: `${OUT_DIR}/P00_new_case.png` });

    const bFramesStart = wsFrames.length;
    // Kickoff prompt verbatim: names Pelicun + NSI + existing Fort Myers flood depth layer.
    const P5_PROMPT = "Run a flood damage assessment for Fort Myers with Pelicun using the NSI building inventory and the existing Fort Myers flood depth layer.";
    const sendB1 = await sendPrompt(page, P5_PROMPT);
    turns++;
    findings.scenarios.B.b_frames_start = bFramesStart;
    findings.scenarios.B.prompt = P5_PROMPT;
    await flush();

    // settle / route watch; if clarification arrives, answer once.
    // SFINCS cloud solve may take up to ~20 min -> generous max.
    let answeredClarify = false;
    {
      const b1T0 = Date.now();
      const B1_MAX = 1500000; // 25 min ceiling (20-min SFINCS leg + Pelicun + narration)
      let bhb = -1;
      while (Date.now() - b1T0 < B1_MAX) {
        if (rateLimited) break;
        const st = await turnState(page);
        if (st.sawSignal && !st.generating && st.running === 0 && st.disabled === false &&
            st.msSinceFrame > QUIESCE_MS && Date.now() - sendB1 > 20000) {
          await page.waitForTimeout(4000);
          const st2 = await turnState(page);
          if (st2.running === 0 && st2.disabled === false && !st2.generating && st2.msSinceFrame > QUIESCE_MS) {
            const impactNow = await page.locator('[data-testid="grace2-impact-panel"]').count();
            const tail = ((await chatText(page)).scroll_text ?? "").slice(-1200);
            const asksClarify = /\?\s*$/.test(tail.trim()) || /which|should i|would you like|inventory|footprint|confirm|clarif/i.test(tail.slice(-400));
            if (impactNow === 0 && !answeredClarify && asksClarify) {
              console.log("[P5] agent asked a clarification; answering 'use the NSI inventory'");
              await sendPrompt(page, "Use the NSI inventory.");
              turns++;
              answeredClarify = true;
              await flush();
              await page.waitForTimeout(8000);
              continue;
            }
            console.log("[P5] settled (impactPanel=" + impactNow + ")");
            break;
          }
        }
        const bhbi = Math.floor((Date.now() - b1T0) / 20000);
        if (bhbi !== bhb) {
          bhb = bhbi;
          console.log(`[P5 t=${Math.round((Date.now()-b1T0)/1000)}s] gen=${st.generating} run=${st.running} dis=${st.disabled} quietMs=${Math.round(st.msSinceFrame)} frames=${wsFrames.length} answeredClarify=${answeredClarify}`);
          // progress screenshot every ~2 min while the SFINCS leg runs
          if (bhbi % 6 === 0) await page.screenshot({ path: `${OUT_DIR}/P01_progress_t${Math.round((Date.now()-b1T0)/1000)}s.png` }).catch(() => {});
          await flush();
        }
        await page.waitForTimeout(1500);
      }
    }
    await page.waitForTimeout(3000);

    // If a payload-warning gate appeared (large NSI response), Proceed it.
    {
      const gate = page.locator('[data-testid="payload-warning-inline"]');
      if ((await gate.count()) > 0) {
        const proceed = page.locator('[data-testid="payload-warning-button-proceed"]');
        if ((await proceed.count()) > 0) { await proceed.click({ timeout: 8000 }).catch(() => {}); console.log("[P5] payload-warning Proceed clicked"); }
        await page.waitForTimeout(2000);
        await waitForTurnSettle(page, { sendT: sendB1, maxMs: 900000, label: "P5-postgate" });
        await page.waitForTimeout(3000);
      }
    }

    const gwHits = scanRoute(GW_RE, bFramesStart);
    const floodHits = scanRoute(FLOOD_RE, bFramesStart);
    findings.scenarios.B.groundwater_hit_count = gwHits.length;
    findings.scenarios.B.flood_route_hit_count = floodHits.length;
    findings.scenarios.B.flood_route_sample = floodHits.slice(0, 10);
    findings.scenarios.B.clarification_answered = answeredClarify;

    const impactCount = await page.locator('[data-testid="grace2-impact-panel"]').count();
    findings.scenarios.B.impact_panel_present = impactCount > 0;
    findings.scenarios.B.impact_envelope_frame = !!wsFrames.slice(bFramesStart).find((f) => f.type === "impact-envelope");
    // capture the impact-envelope payload preview for cross-checking the count
    const impEnv = wsFrames.slice(bFramesStart).find((f) => f.type === "impact-envelope");
    if (impEnv) findings.scenarios.B.impact_envelope_preview = impEnv.preview;
    if (impactCount > 0) {
      findings.scenarios.B.impact = await readImpact(page);
      await page.screenshot({ path: `${OUT_DIR}/P02_impact_panel.png`, fullPage: false });
      console.log("[P5] IMPACT PANEL:", JSON.stringify(findings.scenarios.B.impact).slice(0, 500));
    } else {
      await page.screenshot({ path: `${OUT_DIR}/P02_no_impact_panel.png` });
      findings.scenarios.B.b1_scroll_tail = ((await chatText(page)).scroll_text ?? "").slice(-3000);
    }
    findings.scenarios.B.b1_map = await snapshotMap(page);
    await flush();
    if (rateLimited) { findings.rate_limited_after = "P5"; throw new Error("RATE_LIMITED"); }

    const doB23 = impactCount > 0;
    if (doB23) {
      // B2: analytical count
      await page.waitForTimeout(8000);
      const sendB2 = await sendPrompt(page, "How many structures are impacted above damage state 2?");
      turns++;
      await flush();
      const rB2 = await waitForTurnSettle(page, { sendT: sendB2, maxMs: 360000, label: "B2" });
      console.log("[B2] settle:", rB2);
      await page.waitForTimeout(2500);
      const tB2 = ((await chatText(page)).scroll_text ?? "");
      findings.scenarios.B.count_answer_tail = tB2.slice(-2200);
      findings.scenarios.B.count_has_number = /\b\d[\d,]*\b/.test(tB2.slice(-1400));
      await page.screenshot({ path: `${OUT_DIR}/P03_count_answer.png` });
      await flush();
      if (rateLimited) { findings.rate_limited_after = "B2"; throw new Error("RATE_LIMITED"); }

      // B3: chart emission
      await page.waitForTimeout(8000);
      const b3FramesStart = wsFrames.length;
      const sendB3 = await sendPrompt(page, "Show me the damage distribution as a chart.");
      turns++;
      await flush();
      const rB3 = await waitForTurnSettle(page, { sendT: sendB3, maxMs: 360000, label: "B3" });
      console.log("[B3] settle:", rB3);
      await page.waitForTimeout(3000);
      const chartStackCount = await page.locator('[data-testid="chart-stack"]').count();
      findings.scenarios.B.chart_stack_present = chartStackCount > 0;
      findings.scenarios.B.chart_emission_frame = !!wsFrames.slice(b3FramesStart).find((f) => f.type === "chart-emission");
      const chartEmit = wsFrames.slice(b3FramesStart).find((f) => f.type === "chart-emission");
      if (chartEmit) findings.scenarios.B.chart_emission_preview = chartEmit.preview;
      await page.screenshot({ path: `${OUT_DIR}/P04_chart_stack.png` });
      if (chartStackCount > 0) {
        await page.locator('[data-testid="chart-stack-top-card"]').first().click({ timeout: 5000 })
          .catch(async () => { await page.locator('[data-testid="chart-stack"]').first().click({ timeout: 5000 }).catch(() => {}); });
        await page.waitForTimeout(1500);
        findings.scenarios.B.gallery_opened = (await page.locator('[data-testid="chart-gallery"]').count()) > 0;
        if (findings.scenarios.B.gallery_opened) {
          findings.scenarios.B.gallery_title = await page.locator('[data-testid="chart-gallery-title"]').first().textContent().catch(() => null);
        }
        await page.screenshot({ path: `${OUT_DIR}/P05_chart_gallery.png` });
        const close = page.locator('[data-testid="chart-gallery-close"]');
        if ((await close.count()) > 0) await close.click({ timeout: 4000 }).catch(() => {});
        await page.waitForTimeout(800);
      }
      await flush();
      if (rateLimited) { findings.rate_limited_after = "B3"; throw new Error("RATE_LIMITED"); }

      // ===================================== B4 — reload-replay (Gemini-free)
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
        if (/fort myers|flood|damage|pelicun|impact/i.test(txt)) { await rows.nth(i).click({ timeout: 8000 }).catch(() => {}); clicked = true; break; }
      }
      if (!clicked && rowCount > 0) { await rows.nth(0).click({ timeout: 8000 }).catch(() => {}); }
      await page.waitForTimeout(2500);
      await dismissSaveGate(page);
      await page.waitForTimeout(2500);
      findings.scenarios.B.chart_replay_after_reload = await page.locator('[data-testid="chart-stack"]').count();
      findings.scenarios.B.impact_replay_after_reload = await page.locator('[data-testid="grace2-impact-panel"]').count();
      // open the replayed gallery for a screenshot
      if (findings.scenarios.B.chart_replay_after_reload > 0) {
        await page.locator('[data-testid="chart-stack-top-card"]').first().click({ timeout: 5000 }).catch(() => {});
        await page.waitForTimeout(1200);
        findings.scenarios.B.replay_gallery_opened = (await page.locator('[data-testid="chart-gallery"]').count()) > 0;
      }
      await page.screenshot({ path: `${OUT_DIR}/P06_reload_replay.png` });
      console.log("[B4] after reload: chartStacks=", findings.scenarios.B.chart_replay_after_reload, "impactPanels=", findings.scenarios.B.impact_replay_after_reload);
      await flush();
    } else {
      console.log("[P5] No impact panel; SKIPPING B2/B3/B4.");
      findings.scenarios.B.b23_skipped = true;
      findings.scenarios.B.b23_skip_reason = "no_impact_panel";
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
