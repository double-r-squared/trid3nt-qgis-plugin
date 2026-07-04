#!/usr/bin/env node
// GRACE-2 — job-0143 evidence screenshots (sprint-12-mega Wave 4).
//
// Captures the new left-rail + Settings/Secrets / save-gate restructure:
//
//   01_cases_root_view.png       — CasesPanel only, no LayerPanel.
//   02_case_active_view.png      — Breadcrumb + LayerPanel only (no Cases-list).
//   03_bottom_row_buttons.png    — [Settings] [Secrets] pills under panel.
//   04_settings_popup.png        — Settings popup with Account / Appearance / About.
//   05_secrets_popup.png         — Secrets popup full-screen.
//   06_anonymous_save_gate.png   — Save-gate disclaimer modal triggered.

import { chromium } from "@playwright/test";
import { mkdir } from "fs/promises";

const OUT_DIR =
  process.argv[2] ??
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0143-web-20260608/evidence";
const BASE_URL = process.env.GRACE2_DEV_URL ?? "http://localhost:5177";

const CASE_FORT_MYERS = {
  schema_version: "v1",
  case_id: "01ABCDEFGHJKMNPQRSTVWX0001",
  title: "Hurricane Ian — Fort Myers",
  created_at: "2026-06-05T10:00:00.000Z",
  updated_at: "2026-06-08T11:55:00.000Z",
  status: "active",
  bbox: [-82.0, 26.5, -81.7, 26.8],
  primary_hazard: "flood",
  layer_summary: ["layer-1"],
  qgs_project_uri: "gs://grace-2-hazard-prod-qgs/case-1.qgs",
};

const CASE_NORCAL_FIRE = {
  schema_version: "v1",
  case_id: "01ABCDEFGHJKMNPQRSTVWX0002",
  title: "NorCal fire 2020",
  created_at: "2026-06-01T10:00:00.000Z",
  updated_at: "2026-06-07T10:00:00.000Z",
  status: "active",
  bbox: [-123.5, 38.0, -122.0, 39.5],
  primary_hazard: "wildfire",
  layer_summary: [],
  qgs_project_uri: null,
};

const FORT_MYERS_LAYER = {
  layer_id: "01LAYER000000000000FORTMY01",
  name: "Hurricane Ian flood depth",
  layer_type: "wms",
  uri: "gs://grace-2-hazard-prod-cog/ian-flood-depth.tif",
  wms_url: "https://grace-2-qgis-server.run.app/ogc/wms?MAP=/mnt/qgs/case-1.qgs&LAYERS=ian-flood",
  attribution: "GRACE-2 SFINCS model run",
  visible: true,
  opacity: 0.85,
  z_index: 10,
  temporal: null,
  style_preset: "flood-depth",
};

async function main() {
  await mkdir(OUT_DIR, { recursive: true });

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
  });
  const page = await context.newPage();

  // CAPTURING mock WebSocket.
  await page.addInitScript(() => {
    const captured = [];
    class CapturingWS {
      constructor(url) {
        this._url = url;
        this._listeners = {};
        this._ready = 1;
        setTimeout(() => {
          (this._listeners["open"] ?? []).forEach((cb) => cb({}));
        }, 0);
      }
      get readyState() { return this._ready; }
      addEventListener(type, cb) {
        (this._listeners[type] ??= []).push(cb);
      }
      send(data) {
        captured.push(data);
      }
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
    // job-0143: anonymous pre-accept so the AuthGate doesn't block the shell.
    try { localStorage.setItem("grace2_anonymous_accepted", "true"); } catch {}
  });

  page.on("pageerror", (err) =>
    console.warn(`[screenshot] pageerror: ${err.message}`),
  );
  page.on("console", (msg) => {
    if (msg.type() === "error") {
      console.warn(`[screenshot] console.error: ${msg.text()}`);
    }
  });

  console.log(`[screenshot] loading ${BASE_URL}`);
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="grace2-app-shell"]', {
    timeout: 15000,
  });
  console.log("[screenshot] app shell mounted");

  await page.waitForSelector('[data-testid="grace2-cases-panel"]', {
    timeout: 5000,
  });
  // Inject the Cases list so the panel has content.
  await page.evaluate(({ cases }) => {
    window.__grace2InjectCaseList({
      envelope_type: "case-list",
      cases,
    });
  }, { cases: [CASE_FORT_MYERS, CASE_NORCAL_FIRE] });
  await page.waitForSelector(
    '[data-testid="grace2-case-row"][data-case-id="01ABCDEFGHJKMNPQRSTVWX0001"]',
    { timeout: 3000 },
  );
  await page.waitForTimeout(300);

  // (1) cases_root_view — CasesPanel visible; no LayerPanel; bottom-row pills.
  // Verify left-rail mode = cases-list.
  const railMode1 = await page.getAttribute(
    '[data-testid="grace2-left-rail"]',
    "data-mode",
  );
  if (railMode1 !== "cases-list") {
    console.error(`[FAIL] expected rail mode "cases-list", got "${railMode1}"`);
    process.exit(1);
  }
  // Verify LayerPanel is NOT rendered.
  const layerPanelCount1 = await page
    .locator('[data-testid="grace2-layer-panel"]')
    .count();
  if (layerPanelCount1 !== 0) {
    console.error(`[FAIL] LayerPanel should be hidden, count=${layerPanelCount1}`);
    process.exit(1);
  }
  // Verify bottom row buttons are visible.
  await page.waitForSelector(
    '[data-testid="grace2-bottom-row-buttons"]',
    { timeout: 2000 },
  );
  await page.screenshot({
    path: `${OUT_DIR}/01_cases_root_view.png`,
    fullPage: false,
  });
  console.log("[screenshot] (1) 01_cases_root_view.png saved");

  // (2) case_active_view — click a Case row, inject case-open with a layer.
  await page.click(
    '[data-testid="grace2-case-row"][data-case-id="01ABCDEFGHJKMNPQRSTVWX0001"]',
  );
  await page.waitForTimeout(100);
  // Inject the rehydration envelope to surface the LayerPanel under the breadcrumb.
  await page.evaluate(({ session_state }) => {
    window.__grace2InjectCaseOpen({
      envelope_type: "case-open",
      session_state,
    });
  }, {
    session_state: {
      case: CASE_FORT_MYERS,
      chat_history: [],
      loaded_layers: [FORT_MYERS_LAYER],
      pipeline_history: [],
      current_pipeline: null,
    },
  });
  await page.waitForSelector(
    '[data-testid="grace2-left-rail"][data-mode="case-view"]',
    { timeout: 3000 },
  );
  await page.waitForSelector(
    '[data-testid="grace2-case-view-breadcrumb"]',
    { timeout: 2000 },
  );
  await page.waitForTimeout(300);
  // Verify the CasesPanel-list is NOT visible.
  const casesPanelCount2 = await page
    .locator('[data-testid="grace2-cases-panel"]')
    .count();
  if (casesPanelCount2 !== 0) {
    console.error(`[FAIL] CasesPanel should be hidden in CaseView mode, count=${casesPanelCount2}`);
    process.exit(1);
  }
  // Verify the LayerPanel IS visible.
  await page.waitForSelector(
    '[data-testid="grace2-layer-panel"]',
    { timeout: 3000 },
  );
  await page.screenshot({
    path: `${OUT_DIR}/02_case_active_view.png`,
    fullPage: false,
  });
  console.log("[screenshot] (2) 02_case_active_view.png saved");

  // (3) bottom_row_buttons — zoomed-in inset shot of the bottom-left pills.
  // Click the breadcrumb arrow to return to Cases-list, then capture the
  // bottom-row area cropped tight.
  await page.click('[data-testid="grace2-case-view-back"]');
  await page.waitForSelector(
    '[data-testid="grace2-left-rail"][data-mode="cases-list"]',
    { timeout: 3000 },
  );
  await page.waitForTimeout(300);
  // Inset crop on the bottom-row pills (left-bottom corner of viewport).
  const pillsBox = await page
    .locator('[data-testid="grace2-bottom-row-buttons"]')
    .boundingBox();
  if (!pillsBox) {
    console.error("[FAIL] bottom-row buttons not found for screenshot");
    process.exit(1);
  }
  await page.screenshot({
    path: `${OUT_DIR}/03_bottom_row_buttons.png`,
    clip: {
      x: 0,
      y: pillsBox.y - 100,
      width: 400,
      height: 140,
    },
  });
  console.log("[screenshot] (3) 03_bottom_row_buttons.png saved");

  // (4) settings_popup — open via the bottom-row Settings button.
  await page.click('[data-testid="grace2-bottom-row-settings"]');
  await page.waitForSelector('[data-testid="grace2-settings-popup"]', {
    timeout: 2000,
  });
  await page.waitForTimeout(200);
  // Verify sections render.
  const settingsCardText = await page
    .locator('[data-testid="grace2-settings-popup-card"]')
    .innerText();
  const cardTextLower = settingsCardText.toLowerCase();
  for (const section of ["account", "appearance", "about"]) {
    if (!cardTextLower.includes(section)) {
      console.error(`[FAIL] Settings missing section: ${section}`);
      console.error(`  saw text: ${settingsCardText}`);
      process.exit(1);
    }
  }
  console.log("[VERIFY] Settings popup contains Account, Appearance, About sections");
  await page.screenshot({
    path: `${OUT_DIR}/04_settings_popup.png`,
    fullPage: false,
  });
  console.log("[screenshot] (4) 04_settings_popup.png saved");
  // Close it before continuing.
  await page.click('[data-testid="grace2-settings-popup-close"]');
  await page.waitForTimeout(200);

  // (5) secrets_popup — open via the bottom-row Secrets button.
  await page.click('[data-testid="grace2-bottom-row-secrets"]');
  await page.waitForSelector('[data-testid="grace2-secrets-popup"]', {
    timeout: 2000,
  });
  await page.waitForSelector('[data-testid="grace2-secrets-panel"]', {
    timeout: 2000,
  });
  await page.waitForTimeout(200);
  await page.screenshot({
    path: `${OUT_DIR}/05_secrets_popup.png`,
    fullPage: false,
  });
  console.log("[screenshot] (5) 05_secrets_popup.png saved");
  await page.click('[data-testid="grace2-secrets-popup-close"]');
  await page.waitForTimeout(200);

  // (6) anonymous_save_gate — click "+ New Case" while anonymous; the gate
  // appears INSTEAD of the action firing.
  await page.click('[data-testid="grace2-cases-new"]');
  await page.waitForSelector('[data-testid="grace2-save-gate-modal"]', {
    timeout: 2000,
  });
  await page.waitForTimeout(200);
  await page.screenshot({
    path: `${OUT_DIR}/06_anonymous_save_gate.png`,
    fullPage: false,
  });
  console.log("[screenshot] (6) 06_anonymous_save_gate.png saved");
  // Verify the save-gate body copy matches the kickoff.
  const gateBody = await page
    .locator('[data-testid="grace2-save-gate-modal-body"]')
    .innerText();
  if (!/sync to your account/i.test(gateBody)) {
    console.error(`[FAIL] save-gate body copy missing expected phrase: "${gateBody}"`);
    process.exit(1);
  }
  console.log(`[VERIFY] save-gate copy OK: "${gateBody}"`);

  // Verify the identity chip is GONE (no [data-testid="grace2-identity-chip"]).
  const chipCount = await page
    .locator('[data-testid="grace2-identity-chip"]')
    .count();
  if (chipCount !== 0) {
    console.error(`[FAIL] identity chip should be DELETED, count=${chipCount}`);
    process.exit(1);
  }
  console.log("[VERIFY] identity chip deleted (top-right is clean)");

  console.log("[OK] all job-0143 verifications passed");
  await browser.close();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
