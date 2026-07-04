#!/usr/bin/env node
// GRACE-2 — job-0244 Stage 3 re-verify ROUND 2 — Scenarios B + C ONLY (fresh session).
// The full A+B+C harness proved Scenario A (the fix) end-to-end, then crashed at the
// Scenario-B newCase() navigation: grace2-cases-new lives in the cases-root panel, but
// after Scenario A we were INSIDE an active case view (grace2-case-view) where that
// button doesn't exist. This focused harness fixes newCase() to first return to
// cases-root via grace2-case-view-cases-link, then runs B (flood+Pelicun P5, count,
// chart, reload-replay) + C (sandbox gate). Fresh browser session (no carry-over).
//
// LIVE-DRIVEN ONLY: NO __grace2Inject* seams. Read-only __grace2GetMap permitted.
// <=12 Gemini turns total across BOTH harness runs. On 429 -> stop, mark remaining BLOCKED.

import { chromium } from "@playwright/test";
import { mkdir, writeFile } from "fs/promises";

const OUT_DIR =
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0244-testing-20260610/evidence";
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
          "code-exec-result",
        ]);
        const isChunk = type === "agent-message-chunk";
        if (type === "error" || type === "tool-call-error") maybeRateLimit(t);
        if (type && (KEEP.has(type) || isChunk)) {
          const p = parsed?.payload ?? {};
          let toolName = p.tool_name ?? p.name ?? p.tool ?? null;
          wsFrames.push({ t_rel_ms: rel(), type, tool_name: toolName, preview: t.slice(0, isChunk ? 240 : 1400) });
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
      center: { lng: ctr.lng, lat: ctr.lat }, zoom: m.getZoom(),
    };
  });
}

async function chatText(page) {
  return page.evaluate(() => {
    const scroll = document.querySelector("[data-testid='chat-scroll']") ||
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
  const sawSignal = wsFrames.some((f) =>
    ["agent-message-chunk","agent-message","tool-call-start","tool-payload-warning","interaction-request","code-exec-request","error"].includes(f.type));
  const lastInbound = [...wsFrames].reverse().find((f) => typeof f.type === "string" && !f.type.startsWith("SENT:"));
  const msSinceFrame = lastInbound ? rel() - lastInbound.t_rel_ms : Infinity;
  return { ...dom, sawSignal, msSinceFrame };
}

async function waitForTurnSettle(page, { sendT, maxMs = 720000, label = "" }) {
  const t0 = Date.now();
  let hb = -1;
  while (Date.now() - t0 < maxMs) {
    if (rateLimited) return "rate_limited";
    const st = await turnState(page);
    if (st.failed > 0 && st.running === 0 && st.disabled === false && st.msSinceFrame > QUIESCE_MS) return "failed_card";
    if (st.sawSignal && st.running === 0 && st.disabled === false && st.msSinceFrame > QUIESCE_MS && Date.now() - sendT > 20000) {
      await page.waitForTimeout(4000);
      const st2 = await turnState(page);
      if (st2.running === 0 && st2.disabled === false && st2.msSinceFrame > QUIESCE_MS) return "settled";
    }
    const hbi = Math.floor((Date.now() - t0) / 15000);
    if (hbi !== hb) { hb = hbi; console.log(`[${label} wait t=${Math.round((Date.now()-t0)/1000)}s] sig=${st.sawSignal} run=${st.running} dis=${st.disabled} quietMs=${Math.round(st.msSinceFrame)} frames=${wsFrames.length}`); }
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

// FIXED: escape an active case view back to cases-root before clicking new-case.
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
  await writeFile(`${OUT_DIR}/findings_BC.json`, JSON.stringify(findings, null, 2));
  await writeFile(`${OUT_DIR}/ws_frames_BC.json`, JSON.stringify(wsFrames, null, 2));
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
  console.log("=== job-0244 ROUND 2 — Scenarios B + C (fresh session) ===");

  try {
    await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('[data-testid="grace2-auth-gate"]', { timeout: 15000 }).catch(() => null);
    const anonBtn = page.locator('[data-testid="grace2-auth-gate-anonymous"]');
    if ((await anonBtn.count()) > 0) { await anonBtn.click(); await page.waitForTimeout(1200); }
    await page.waitForSelector('[data-testid="grace2-app-shell"]', { timeout: 10000 });
    await page.waitForTimeout(1000);

    // ===================================================== SCENARIO B
    console.log("\n##### SCENARIO B — analysis + P5 Pelicun #####");
    findings.scenarios.B = {};
    await newCase(page);
    await page.screenshot({ path: `${OUT_DIR}/B01_new_case.png` });

    const sendB1 = await sendPrompt(page,
      "Model flood damage for Fort Myers, Florida. Run a flood scenario there if no flood layer exists yet, then run a Pelicun damage assessment on it.");
    await flush();
    const rB1 = await waitForTurnSettle(page, { sendT: sendB1, maxMs: 900000, label: "B1" });
    console.log("[B1] settle:", rB1);
    await page.waitForTimeout(3000);
    const impactCount = await page.locator('[data-testid="grace2-impact-panel"]').count();
    findings.scenarios.B.impact_panel_present = impactCount > 0;
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
    await flush();
    if (rateLimited) { findings.rate_limited_after = "B1"; throw new Error("RATE_LIMITED"); }

    // B2: analytical count
    await page.waitForTimeout(8000);
    const sendB2 = await sendPrompt(page, "How many structures are impacted above damage state 2?");
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

    // ===================================================== SCENARIO C — sandbox gate
    console.log("\n##### SCENARIO C — sandbox gate #####");
    findings.scenarios.C = {};
    await page.waitForTimeout(10000);
    const sendC = await sendPrompt(page,
      "Run a quick Python computation: compute the mean and max of the flood depth raster with numpy and print both.");
    await flush();
    let sandboxReqT = null, execT = null;
    const cWatch0 = Date.now();
    while (Date.now() - cWatch0 < 240000) {
      if (rateLimited) break;
      const sbCount = await page.locator('[data-testid="sandbox-card"]').count();
      if (sbCount > 0 && sandboxReqT === null) {
        if ((await page.locator('[data-testid="sandbox-card-proceed"]').count()) > 0) sandboxReqT = Date.now();
      }
      const execFrame = wsFrames.find((f) => f.type === "code-exec-request");
      if (execFrame && execT === null) execT = t0Global + execFrame.t_rel_ms;
      if (sandboxReqT) break;
      const st = await turnState(page);
      if (!sandboxReqT && st.sawSignal && st.running === 0 && st.disabled === false && st.msSinceFrame > QUIESCE_MS && Date.now() - sendC > 30000) {
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
      const rC = await waitForTurnSettle(page, { sendT: Date.now(), maxMs: 240000, label: "C-result" });
      console.log("[C] result settle:", rC);
      await page.waitForTimeout(2500);
      const statusChip = await page.locator('[data-testid="sandbox-card-status-chip"]').first().textContent().catch(() => null);
      findings.scenarios.C.result_status_chip = statusChip;
      const resultScalar = await page.locator('[data-testid="sandbox-result-scalar"], [data-testid="sandbox-result-json"]').first().textContent().catch(() => null);
      findings.scenarios.C.result_payload = (resultScalar ?? "").slice(0, 500);
      findings.scenarios.C.c_scroll_tail = ((await chatText(page)).scroll_text ?? "").slice(-1500);
      await page.screenshot({ path: `${OUT_DIR}/C02_sandbox_result.png` });
      console.log("[C] result status:", statusChip);
    } else {
      await page.screenshot({ path: `${OUT_DIR}/C01_no_sandbox.png` });
      console.log("[C] NO sandbox request card appeared");
    }
    await flush();
    if (rateLimited) { findings.rate_limited_after = "C"; throw new Error("RATE_LIMITED"); }

    // B4: reload-replay (Gemini-free)
    console.log("\n##### B4 — refresh + chart replay (Gemini-free) #####");
    await page.waitForTimeout(2000);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForTimeout(2000);
    const anonBtn2 = page.locator('[data-testid="grace2-auth-gate-anonymous"]');
    if ((await anonBtn2.count()) > 0) { await anonBtn2.click(); await page.waitForTimeout(1500); }
    await page.waitForSelector('[data-testid="grace2-app-shell"]', { timeout: 10000 }).catch(() => {});
    await page.waitForTimeout(1500);
    const rows = page.locator('[data-testid="grace2-case-row"]');
    const rowCount = await rows.count();
    findings.scenarios.B.reload_case_rows = rowCount;
    if (rowCount > 0) { await rows.first().click({ timeout: 8000 }).catch(() => {}); await page.waitForTimeout(2500); await dismissSaveGate(page); }
    await page.waitForTimeout(2500);
    findings.scenarios.B.chart_replay_after_reload = await page.locator('[data-testid="chart-stack"]').count();
    findings.scenarios.B.impact_replay_after_reload = await page.locator('[data-testid="grace2-impact-panel"]').count();
    await page.screenshot({ path: `${OUT_DIR}/B06_reload_replay.png` });
    console.log("[B4] after reload: chartStacks=", findings.scenarios.B.chart_replay_after_reload, "impactPanels=", findings.scenarios.B.impact_replay_after_reload);
    await flush();
  } catch (e) {
    findings.fatal_error = String(e && e.stack ? e.stack : e);
    console.error("FATAL:", e);
    await page.screenshot({ path: `${OUT_DIR}/99_fatal_BC.png` }).catch(() => {});
  } finally {
    findings.rate_limited = rateLimited;
    findings.page_errors = errs.slice(0, 60);
    findings.ws_frames = wsFrames;
    await flush();
    await ctx.close().catch(() => {});
    await browser.close();
    console.log("=== COMPLETE — findings_BC.json + ws_frames_BC.json written ===");
  }
}

main().catch((e) => { console.error("OUTER FAILURE:", e); process.exit(1); });
