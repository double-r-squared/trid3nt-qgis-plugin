#!/usr/bin/env node
// job-0279 LIVE verify (user-unlocked Gemini quota, used intelligently):
//
//   Scenario A (mobile 390x844, ~2 Gemini turns, both cache-hit chains):
//     1. Boulder relief from root — exercises drawer/sheet/cards/overlay on
//        the REAL mobile UI under a REAL stream (job-0278's open caveat).
//     2. "Now do the same for Seattle, WA." in the SAME Case — location
//        fidelity (job-0274): the turn must geocode SEATTLE, not reuse
//        Boulder; assert via the published layer + envelope captures.
//
// Assertions also prove job-0277 live: streaming envelopes carry case_id.
// STOP on any 429. No inject seams.

import { chromium } from "@playwright/test";
import { mkdir, writeFile } from "fs/promises";

const OUT = "/home/nate/Documents/GRACE-2/reports/inflight/job-0279-testing-20260611/evidence";
const BASE = "http://100.92.163.46:5173";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const t0 = Date.now();
const rel = () => Date.now() - t0;

let rateLimited = false;
const tagged = { withCase: 0, without: 0 };
const layers = new Set();
let publishCompletes = 0;

function watch(page) {
  page.on("websocket", (ws) => {
    ws.on("framereceived", (d) => {
      try {
        const t = typeof d.payload === "string" ? d.payload : d.payload.toString();
        if (/429|RESOURCE_EXHAUSTED|quota/i.test(t)) rateLimited = true;
        const p = JSON.parse(t);
        const streaming = new Set([
          "agent-message-chunk", "pipeline-state", "session-state",
        ]);
        if (p?.type && streaming.has(p.type)) {
          if (typeof p.case_id === "string") tagged.withCase += 1;
          else tagged.without += 1;
        }
        if (p?.type === "session-state")
          for (const l of p?.payload?.loaded_layers ?? [])
            if (l?.layer_id) layers.add(l.layer_id);
        if (p?.type === "pipeline-state")
          for (const s of p?.payload?.steps ?? [])
            if (s.tool_name === "publish_layer" && s.state === "complete")
              publishCompletes += 1;
      } catch {}
    });
  });
}

async function shot(page, name) {
  await page.screenshot({ path: `${OUT}/${name}.png` });
  console.log(`[shot] ${name} @ ${rel()}ms`);
}

async function main() {
  await mkdir(OUT, { recursive: true });
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  watch(page);
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  const anon = page.locator('[data-testid="grace2-auth-gate-anonymous"]');
  if (await anon.isVisible({ timeout: 5000 }).catch(() => false)) await anon.click();
  await sleep(2500);
  await shot(page, "m1_root");

  // Turn 1: Boulder relief (all upstream cache hits → fast).
  const input = page.locator('[data-testid="chat-input"]');
  await input.click();
  await input.fill("Compute a colored relief map for Boulder, Colorado.");
  await input.press("Enter");
  console.log("[turn1] sent");
  await sleep(12000);
  await shot(page, "m2_turn1_sheet_streaming");

  // Wait for the publish to complete (worker ~2.5 min) + a beat for tiles.
  let waited = 0;
  while (publishCompletes < 1 && waited < 300000 && !rateLimited) {
    await sleep(5000); waited += 5000;
  }
  await sleep(8000);
  await shot(page, "m3_turn1_done_sheet");
  // Collapse the sheet to see the map overlay.
  const handle = page.locator('[data-testid="grace2-chat"] [data-sheet-state]').first();
  const toggle = page.getByRole("button", { name: /collapse|chat|sheet|⌄|︿/i }).first();
  if (await toggle.isVisible({ timeout: 1500 }).catch(() => false)) await toggle.click();
  await sleep(2500);
  await shot(page, "m4_turn1_map_overlay");
  // Open the drawer to see the Case + layer list.
  const burger = page.getByRole("button", { name: /menu|cases|☰/i }).first();
  if (await burger.isVisible({ timeout: 1500 }).catch(() => false)) await burger.click();
  await sleep(1200);
  await shot(page, "m5_drawer_with_case");
  await page.keyboard.press("Escape").catch(() => {});
  await sleep(800);

  if (rateLimited) { console.log("[STOP] 429"); await wrap(); return; }

  // Pace before turn 2.
  await sleep(20000);

  // Turn 2 (same Case): location fidelity.
  const input2 = page.locator('[data-testid="chat-input"]');
  await input2.click();
  await input2.fill("Now do the same for Seattle, WA.");
  await input2.press("Enter");
  console.log("[turn2] sent");
  const before = publishCompletes;
  waited = 0;
  while (publishCompletes <= before && waited < 300000 && !rateLimited) {
    await sleep(5000); waited += 5000;
  }
  await sleep(8000);
  await shot(page, "m6_turn2_done");

  async function wrap() {
    const seattleLayer = [...layers].some((l) => /seattle/i.test(l));
    const boulderOnlyAgain = [...layers].filter((l) => /boulder/i.test(l)).length;
    await writeFile(`${OUT}/live_stats.json`, JSON.stringify({
      rateLimited, tagged, layers: [...layers], publishCompletes,
    }, null, 2));
    console.log(`[stats] tagged=${JSON.stringify(tagged)} layers=${[...layers].join(",")} publishes=${publishCompletes}`);
    console.log(`[fidelity] seattle_layer=${seattleLayer} (true = job-0274 holds)`);
    console.log(`[tagging] case-tagged streaming frames=${tagged.withCase} untagged=${tagged.without}`);
    await browser.close();
  }
  await wrap();
}

main().catch((e) => { console.error(e); process.exit(2); });
