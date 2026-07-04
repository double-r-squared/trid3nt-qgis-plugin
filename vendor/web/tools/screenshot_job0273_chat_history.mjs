#!/usr/bin/env node
// GRACE-2 — job-0273 LIVE chat-history verification (user-directed).
//
// Purpose: prove the chat panel actually shows the conversation (user bubble,
// tool cards, narration) during AND after a real turn — including a hard
// reload + Case reopen so the replay path (persistence) is exercised, not
// just live state. NO inject seams. ONE Gemini turn. Honest by construction:
// every agent-message-chunk is logged and the assembled narration is saved.

import { chromium } from "@playwright/test";
import { mkdir, writeFile } from "fs/promises";

const OUT_DIR =
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0273-web-20260610/evidence";
const BASE_URL = "http://localhost:5173";
const PROMPT = "Compute a colored relief map for Boulder, Colorado.";

const t0 = Date.now();
const rel = () => Date.now() - t0;
const frames = [];
let chunkCount = 0;
let narration = "";
let layerAnnounced = null;
let publishDone = false;
let rateLimited = false;

function logWS(page) {
  page.on("websocket", (ws) => {
    ws.on("framereceived", (data) => {
      try {
        const t =
          typeof data.payload === "string" ? data.payload : data.payload.toString();
        let parsed;
        try {
          parsed = JSON.parse(t);
        } catch {
          return;
        }
        const type = parsed?.type;
        if (!type) return;
        if (/429|RESOURCE_EXHAUSTED|quota/i.test(t)) rateLimited = true;
        if (type === "agent-message-chunk") {
          chunkCount += 1;
          narration += parsed?.payload?.delta ?? "";
          return; // counted + assembled, not stored raw
        }
        if (type === "session-state") {
          for (const l of parsed?.payload?.loaded_layers ?? [])
            if (typeof l?.uri === "string" && l.uri.includes("LAYERS=") && /relief/i.test(l?.layer_id ?? ""))
              layerAnnounced = l.layer_id;
        }
        if (type === "pipeline-state") {
          for (const s of parsed?.payload?.steps ?? [])
            if (s.tool_name === "publish_layer" && s.state === "complete")
              publishDone = true;
        }
        frames.push({ t_rel_ms: rel(), type, preview: t.slice(0, 400) });
      } catch {}
    });
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function shotPanel(page, name) {
  await page.screenshot({ path: `${OUT_DIR}/${name}_full.png` });
  // Crop-equivalent: clip the right-side chat panel region at full res.
  await page.screenshot({
    path: `${OUT_DIR}/${name}_chat.png`,
    clip: { x: 1130, y: 0, width: 470, height: 950 },
  });
  console.log(`[shot] ${name} @ ${rel()}ms`);
}

async function main() {
  await mkdir(OUT_DIR, { recursive: true });
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
  logWS(page);
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  const anon = page.locator('[data-testid="grace2-auth-gate-anonymous"]');
  if (await anon.isVisible({ timeout: 5000 }).catch(() => false)) await anon.click();
  await page.waitForSelector('[data-testid="chat-input"]', { timeout: 15000 });
  await sleep(2000);

  const input = page.locator('[data-testid="chat-input"]');
  await input.click();
  await input.fill(PROMPT);
  await input.press("Enter");
  console.log("[sent]", PROMPT);

  await sleep(15000);
  await shotPanel(page, "01_midturn_15s"); // Case view + user bubble + early cards

  // Wait for completion: narration arrived AND layer announced (max 7 min).
  const deadline = Date.now() + 7 * 60 * 1000;
  while (Date.now() < deadline) {
    if (rateLimited) break;
    if (layerAnnounced && chunkCount > 0 && publishDone) break;
    await sleep(3000);
  }
  await sleep(5000);
  await shotPanel(page, "02_turn_complete");

  // THE honesty gate: hard reload, reopen the newest Case, replay from
  // persistence — no live session state can fake this.
  await page.reload({ waitUntil: "domcontentloaded" });
  const anon2 = page.locator('[data-testid="grace2-auth-gate-anonymous"]');
  if (await anon2.isVisible({ timeout: 5000 }).catch(() => false)) await anon2.click();
  await sleep(3000);
  const caseRows = page.locator('[data-testid="cases-panel-row"], [data-testid^="case-row"]');
  const n = await caseRows.count();
  if (n > 0) {
    await caseRows.first().click(); // rail is newest-first per job-0260
  } else {
    // fall back: click any element containing the case title
    await page.getByText("Compute Colored Relief", { exact: false }).first().click().catch(() => {});
  }
  await sleep(4000);
  await shotPanel(page, "03_after_reload_replay");

  await writeFile(
    `${OUT_DIR}/chat_session_stats.json`,
    JSON.stringify(
      { chunkCount, narration, layerAnnounced, publishDone, rateLimited, frames },
      null,
      2
    )
  );
  console.log(
    `[state] chunks=${chunkCount} layer=${layerAnnounced} publishDone=${publishDone} rateLimited=${rateLimited}`
  );
  console.log(`[narration] ${narration.slice(0, 400)}`);
  await browser.close();
}

main().catch((e) => {
  console.error(e);
  process.exit(2);
});
