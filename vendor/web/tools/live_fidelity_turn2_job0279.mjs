#!/usr/bin/env node
// job-0279 part 2: location fidelity (job-0274) — "Now do the same for
// Seattle, WA." inside the Boulder Case must geocode SEATTLE. One Gemini turn.
import { chromium } from "@playwright/test";
import { mkdir } from "fs/promises";

const OUT = "/home/nate/Documents/GRACE-2/reports/inflight/job-0279-testing-20260611/evidence";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let publishDone = false;
let rateLimited = false;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
page.on("websocket", (ws) => {
  ws.on("framereceived", (d) => {
    try {
      const t = typeof d.payload === "string" ? d.payload : d.payload.toString();
      if (/429|RESOURCE_EXHAUSTED/i.test(t)) rateLimited = true;
      const p = JSON.parse(t);
      if (p?.type === "pipeline-state")
        for (const s of p?.payload?.steps ?? [])
          if (s.tool_name === "publish_layer" && s.state === "complete")
            publishDone = true;
    } catch {}
  });
});
await mkdir(OUT, { recursive: true });
await page.goto("http://100.92.163.46:5173", { waitUntil: "domcontentloaded" });
const anon = page.locator('[data-testid="grace2-auth-gate-anonymous"]');
if (await anon.isVisible({ timeout: 4000 }).catch(() => false)) await anon.click();
await sleep(2500);

// Drawer → tap the Boulder Case (drawer closes on case tap).
await page.locator('[data-testid="grace2-mobile-drawer-button"]').click();
await sleep(800);
await page.getByText("Boulder", { exact: false }).first().click();
await sleep(2000);
await page.screenshot({ path: `${OUT}/f1_in_case_replay.png` });

// Send the fidelity prompt.
const input = page.locator('[data-testid="chat-input"]');
await input.click();
await input.fill("Now do the same for Seattle, WA.");
await input.press("Enter");
console.log("[sent] fidelity prompt");
let waited = 0;
while (!publishDone && !rateLimited && waited < 360000) {
  await sleep(5000); waited += 5000;
}
await sleep(8000);
await page.screenshot({ path: `${OUT}/f2_turn2_done.png` });
console.log(`[done] publishDone=${publishDone} rateLimited=${rateLimited}`);
await browser.close();
