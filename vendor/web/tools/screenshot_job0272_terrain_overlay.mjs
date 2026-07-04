#!/usr/bin/env node
// GRACE-2 — job-0272 LIVE terrain-overlay verification (user-directed).
//
// Drives the REAL app exactly like the user does: anonymous auth -> real
// Boulder colored-relief prompt through the chat input -> real Gemini turn ->
// real publish -> assert the session-state announces the layer -> screenshot
// the map overlay. NO __grace2Inject* seams. ONE Gemini turn. On 429 -> stop.

import { chromium } from "@playwright/test";
import { mkdir, writeFile } from "fs/promises";

const OUT_DIR =
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0272-agent-20260610/evidence";
const BASE_URL = "http://localhost:5173";
const PROMPT = "Compute a colored relief map for Boulder, Colorado.";

const wsFrames = [];
const t0 = Date.now();
const rel = () => Date.now() - t0;
let rateLimited = false;
let layerAnnounced = null; // layer_id from session-state with a WMS uri
let publishDone = false;
let turnTerminal = false;

function logWS(page) {
  page.on("websocket", (ws) => {
    ws.on("framereceived", (data) => {
      try {
        const t =
          typeof data.payload === "string" ? data.payload : data.payload.toString();
        let parsed = null;
        try {
          parsed = JSON.parse(t);
        } catch {}
        const type = parsed?.type ?? null;
        if (!type) return;
        if (/429|RESOURCE_EXHAUSTED|quota/i.test(t)) rateLimited = true;
        if (type === "session-state") {
          const layers = parsed?.payload?.loaded_layers ?? [];
          for (const l of layers) {
            if (
              typeof l?.uri === "string" &&
              l.uri.includes("LAYERS=") &&
              /relief/i.test(l?.layer_id ?? "")
            )
              layerAnnounced = l.layer_id;
          }
        }
        if (type === "pipeline-state") {
          const steps = parsed?.payload?.steps ?? [];
          for (const s of steps)
            if (s.tool_name === "publish_layer" && s.state === "complete")
              publishDone = true;
        }
        if (type === "agent-message" && parsed?.payload?.done === true)
          turnTerminal = true;
        if (type !== "agent-message-chunk")
          wsFrames.push({ t_rel_ms: rel(), type, preview: t.slice(0, 700) });
      } catch {}
    });
  });
}

async function shot(page, name) {
  await page.screenshot({ path: `${OUT_DIR}/${name}.png`, fullPage: false });
  console.log(`[shot] ${name} @ ${rel()}ms`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  await mkdir(OUT_DIR, { recursive: true });
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
  logWS(page);
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });

  // Anonymous auth gate (if present).
  const anonBtn = page.locator('[data-testid="grace2-auth-gate-anonymous"]');
  if (await anonBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
    await anonBtn.click();
  }
  await page.waitForSelector('[data-testid="chat-input"]', { timeout: 15000 });
  await sleep(2500); // basemap settle
  await shot(page, "01_start_root");

  // Real prompt through the real composer.
  const chatInput = page.locator('[data-testid="chat-input"]');
  await chatInput.click();
  await chatInput.fill(PROMPT);
  await chatInput.press("Enter");
  console.log(`[sent] ${PROMPT}`);

  // Wait for snap-to-Boulder + early tool cards.
  await sleep(20000);
  await shot(page, "02_after_snap_tools_running");

  // Wait for publish completion + layer announcement (budget 6 min).
  const deadline = Date.now() + 6 * 60 * 1000;
  while (Date.now() < deadline) {
    if (rateLimited) {
      console.log("[FAIL] rate limited — stopping per quota discipline");
      break;
    }
    if (layerAnnounced && turnTerminal) break;
    await sleep(3000);
  }
  console.log(
    `[state] publishDone=${publishDone} layerAnnounced=${layerAnnounced} terminal=${turnTerminal}`
  );
  await sleep(6000); // WMS tiles fetch + paint
  await shot(page, "03_final_overlay");

  // Layer panel detail (hover region) — best-effort.
  const panel = page.locator('[data-testid="layer-panel"]').first();
  if (await panel.isVisible().catch(() => false)) {
    await panel.hover().catch(() => {});
    await sleep(800);
  }
  await shot(page, "04_layer_panel");

  await writeFile(
    `${OUT_DIR}/ws_frames.json`,
    JSON.stringify({ layerAnnounced, publishDone, turnTerminal, rateLimited, wsFrames }, null, 2)
  );
  const verdict =
    layerAnnounced && publishDone
      ? "PASS"
      : rateLimited
        ? "BLOCKED(429)"
        : "FAIL";
  console.log(`[verdict] ${verdict}`);
  await browser.close();
  process.exit(verdict === "PASS" ? 0 : 1);
}

main().catch((e) => {
  console.error(e);
  process.exit(2);
});
