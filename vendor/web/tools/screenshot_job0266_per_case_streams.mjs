#!/usr/bin/env node
// GRACE-2 — job-0266 evidence screenshots (PER-CASE CHAT STREAMS).
//
// Dev-seam UI snapshots ONLY (per kickoff: NO Gemini, NO live agent —
// WebSocket is stubbed with a capturing mock so nothing reaches :8765).
// Drives the live Vite client through:
//   __grace2InjectCaseList      (App rail)
//   __grace2InjectCaseOpen      (App → useCases → activeCaseId → Chat prop)
//   __grace2InjectCaseOpenChat  (Chat per-Case stream map, job-0266)
//   __grace2InjectPipelineState (Chat tool card, routed to owning stream)
//
// Captures + asserts:
//   1. per_case_root_clean.png        — root view: rail + clean empty composer;
//                                       rail EXCLUDES deleted/archived rows.
//   2. per_case_case_a_stream.png     — Case A open: its own messages + tool card.
//   3. per_case_case_b_stream.png     — Case B open: DISTINCT stream; A's text absent.
//   4. per_case_case_a_revisit.png    — back to A: buffered stream intact.
//   5. per_case_root_after_nav.png    — breadcrumb back → root clean again.
//   6. rail refresh on a second case-list envelope (assertion only).

import { chromium } from "@playwright/test";
import { mkdir } from "fs/promises";

const OUT_DIR =
  process.argv[2] ??
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0266-web-20260610/evidence";
const BASE_URL = process.env.GRACE2_DEV_URL ?? "http://localhost:5173";

const CASE_A_ID = "01CASEAAAAAAAAAAAAAAAAAAAA";
const CASE_B_ID = "01CASEBBBBBBBBBBBBBBBBBBBB";

const CASE_A = {
  schema_version: "v1",
  case_id: CASE_A_ID,
  title: "Hurricane Ian — Fort Myers flood",
  created_at: "2026-06-09T10:00:00.000Z",
  updated_at: "2026-06-10T11:55:00.000Z",
  status: "active",
  bbox: [-82.0, 26.5, -81.7, 26.8],
  primary_hazard: "flood",
};
const CASE_B = {
  schema_version: "v1",
  case_id: CASE_B_ID,
  title: "NorCal wildfire perimeters",
  created_at: "2026-06-08T10:00:00.000Z",
  updated_at: "2026-06-10T09:00:00.000Z",
  status: "active",
  bbox: [-123.5, 38.0, -122.0, 39.5],
  primary_hazard: "wildfire",
};
const CASE_DELETED = {
  schema_version: "v1",
  case_id: "01CASEDELETEDDDDDDDDDDDDDD",
  title: "Deleted case (must NOT render)",
  created_at: "2026-06-01T10:00:00.000Z",
  updated_at: "2026-06-02T10:00:00.000Z",
  status: "deleted",
};
const CASE_ARCHIVED = {
  schema_version: "v1",
  case_id: "01CASEARCHIVEDAAAAAAAAAAAA",
  title: "Archived case (must NOT render)",
  created_at: "2026-06-01T10:00:00.000Z",
  updated_at: "2026-06-03T10:00:00.000Z",
  status: "archived",
};

function sessionFor(caseSummary, history) {
  return {
    schema_version: "v1",
    case: caseSummary,
    chat_history: history,
    loaded_layers: [],
    pipeline_history: [],
  };
}

const HISTORY_A = [
  {
    message_id: "01MSGA000000000000000000A1",
    case_id: CASE_A_ID,
    role: "user",
    content: "Model flood depth for Fort Myers after Hurricane Ian.",
    created_at: "2026-06-10T11:50:00.000Z",
  },
  {
    message_id: "01MSGA000000000000000000A2",
    case_id: CASE_A_ID,
    role: "agent",
    content:
      "I ran the SFINCS pluvial scenario and published the flood-depth raster to the map.",
    created_at: "2026-06-10T11:54:00.000Z",
  },
];
const HISTORY_B = [
  {
    message_id: "01MSGB000000000000000000B1",
    case_id: CASE_B_ID,
    role: "user",
    content: "Show me active fire perimeters in NorCal.",
    created_at: "2026-06-10T08:58:00.000Z",
  },
  {
    message_id: "01MSGB000000000000000000B2",
    case_id: CASE_B_ID,
    role: "agent",
    content: "I added the NIFC fire perimeters layer for Northern California.",
    created_at: "2026-06-10T08:59:00.000Z",
  },
];

const FAILURES = [];
function check(cond, label) {
  if (cond) {
    console.log(`[VERIFY] ${label} OK`);
  } else {
    console.error(`[FAIL] ${label}`);
    FAILURES.push(label);
  }
}

async function openCase(page, caseSummary, history) {
  await page.evaluate(
    ({ session }) => {
      // Both seams = simulate the real envelope fan-out (App socket via hub
      // + Chat socket native).
      window.__grace2InjectCaseOpen({ envelope_type: "case-open", session_state: session });
      window.__grace2InjectCaseOpenChat({ envelope_type: "case-open", session_state: session });
    },
    { session: sessionFor(caseSummary, history) },
  );
  await page.waitForSelector(
    `[data-testid="grace2-chat"][data-stream-key="${caseSummary.case_id}"]`,
    { timeout: 3000 },
  );
}

async function main() {
  await mkdir(OUT_DIR, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();

  // Stub WebSocket (capturing — NOTHING reaches the live agent on :8765)
  // and pre-accept the anonymous gate.
  await page.addInitScript(() => {
    window.localStorage.setItem("grace2_anonymous_accepted", "true");
    const captured = [];
    class CapturingWS {
      constructor(url) {
        this._url = url;
        this._listeners = {};
        this._ready = 1;
        setTimeout(() => (this._listeners["open"] ?? []).forEach((cb) => cb({})), 0);
      }
      get readyState() { return this._ready; }
      addEventListener(type, cb) { (this._listeners[type] ??= []).push(cb); }
      send(data) { captured.push(data); }
      close() {
        this._ready = 3;
        (this._listeners["close"] ?? []).forEach((cb) => cb({}));
      }
    }
    CapturingWS.OPEN = 1;
    CapturingWS.CONNECTING = 0;
    CapturingWS.CLOSED = 3;
    window.WebSocket = CapturingWS;
    window.__grace2CapturedFrames = captured;
  });

  page.on("pageerror", (err) => console.warn(`[screenshot] pageerror: ${err.message}`));

  console.log(`[screenshot] loading ${BASE_URL}`);
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="grace2-app-shell"]', { timeout: 15000 });
  await page.waitForSelector('[data-testid="grace2-cases-panel"]', { timeout: 5000 });
  await page.waitForTimeout(400);

  // ---- (1) Root view: rail with 2 ACTIVE cases (deleted+archived excluded),
  //          clean empty composer. ------------------------------------------
  await page.evaluate(
    ({ cases }) => window.__grace2InjectCaseList({ envelope_type: "case-list", cases }),
    { cases: [CASE_A, CASE_B, CASE_DELETED, CASE_ARCHIVED] },
  );
  await page.waitForSelector(
    `[data-testid="grace2-case-row"][data-case-id="${CASE_A_ID}"]`,
    { timeout: 3000 },
  );
  const rowIds = await page
    .locator('[data-testid="grace2-case-row"]')
    .evaluateAll((els) => els.map((e) => e.getAttribute("data-case-id")));
  check(rowIds.length === 2, `rail shows exactly 2 rows (got ${rowIds.length})`);
  check(!rowIds.includes(CASE_DELETED.case_id), "deleted case EXCLUDED from rail");
  check(!rowIds.includes(CASE_ARCHIVED.case_id), "archived case EXCLUDED from rail");

  const rootKey = await page
    .locator('[data-testid="grace2-chat"]')
    .getAttribute("data-stream-key");
  check(rootKey === "__root__", `root stream key is __root__ (got ${rootKey})`);
  const rootText = await page.locator('[data-testid="chat-scroll"]').innerText();
  check(/Ask a question/.test(rootText), "root chat is the clean empty composer state");
  await page.screenshot({ path: `${OUT_DIR}/per_case_root_clean.png` });
  console.log("[screenshot] (1) per_case_root_clean.png saved");

  // ---- (2) Open Case A: its stream renders (messages + a tool card). ------
  await openCase(page, CASE_A, HISTORY_A);
  // Tool card routed to A (targetKey adopted from root on first case-open).
  await page.evaluate(() => {
    window.__grace2InjectPipelineState({
      pipeline_id: "pipe-evidence-A",
      steps: [
        {
          step_id: "step-A1",
          name: "run_model_flood_scenario",
          tool_name: "run_model_flood_scenario",
          state: "complete",
        },
      ],
    });
  });
  await page.waitForTimeout(300);
  const aText = await page.locator('[data-testid="chat-scroll"]').innerText();
  check(/Fort Myers/.test(aText), "Case A stream shows its own messages");
  check(/SFINCS pluvial/.test(aText), "Case A agent reply rendered from rehydration");
  await page.screenshot({ path: `${OUT_DIR}/per_case_case_a_stream.png` });
  console.log("[screenshot] (2) per_case_case_a_stream.png saved");

  // ---- (3) Switch to Case B: ENTIRE visible stream swaps. ------------------
  await openCase(page, CASE_B, HISTORY_B);
  await page.waitForTimeout(300);
  const bText = await page.locator('[data-testid="chat-scroll"]').innerText();
  check(/NorCal|fire perimeters/.test(bText), "Case B stream shows its own messages");
  check(!/Fort Myers/.test(bText), "Case A's messages are NOT painted into Case B");
  check(!/run_model_flood_scenario|Modeling/.test(bText), "Case A's tool card not in Case B");
  await page.screenshot({ path: `${OUT_DIR}/per_case_case_b_stream.png` });
  console.log("[screenshot] (3) per_case_case_b_stream.png saved");

  // ---- (4) Back to Case A: buffered stream intact (messages + tool card). -
  await openCase(page, CASE_A, HISTORY_A);
  await page.waitForTimeout(300);
  const aText2 = await page.locator('[data-testid="chat-scroll"]').innerText();
  check(/Fort Myers/.test(aText2), "Case A revisit: messages still there");
  check(!/NorCal/.test(aText2), "Case B's messages are NOT in Case A");
  const aCards = await page.locator('[data-testid="chat-stream"]').innerText();
  check(aCards.length > 0, "Case A revisit: interleaved stream non-empty");
  await page.screenshot({ path: `${OUT_DIR}/per_case_case_a_revisit.png` });
  console.log("[screenshot] (4) per_case_case_a_revisit.png saved");

  // ---- (5) Breadcrumb back → root clears the visible chat. ----------------
  await page.click('[data-testid="grace2-case-view-back"]');
  await page.waitForSelector(
    '[data-testid="grace2-chat"][data-stream-key="__root__"]',
    { timeout: 3000 },
  );
  await page.waitForTimeout(300);
  const rootText2 = await page.locator('[data-testid="chat-scroll"]').innerText();
  check(/Ask a question/.test(rootText2), "root after nav-out is clean (empty composer)");
  check(!/Fort Myers|NorCal/.test(rootText2), "no Case content leaks into root view");
  await page.screenshot({ path: `${OUT_DIR}/per_case_root_after_nav.png` });
  console.log("[screenshot] (5) per_case_root_after_nav.png saved");

  // ---- (6) Rail refreshes on a new case-list envelope. --------------------
  await page.evaluate(
    ({ cases }) => window.__grace2InjectCaseList({ envelope_type: "case-list", cases }),
    { cases: [CASE_A, CASE_DELETED] },
  );
  await page.waitForTimeout(300);
  const rowIds2 = await page
    .locator('[data-testid="grace2-case-row"]')
    .evaluateAll((els) => els.map((e) => e.getAttribute("data-case-id")));
  check(
    rowIds2.length === 1 && rowIds2[0] === CASE_A_ID,
    `rail refreshed on case-list envelope (got ${JSON.stringify(rowIds2)})`,
  );

  await browser.close();
  if (FAILURES.length > 0) {
    console.error(`\n[RESULT] ${FAILURES.length} assertion(s) FAILED`);
    process.exit(1);
  }
  console.log("\n[RESULT] all job-0266 dev-seam assertions PASSED");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
