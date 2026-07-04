#!/usr/bin/env node
// Probe: can the rail re-enter a Case right now? Captures console errors,
// outbound WS frames, and screenshots. Zero Gemini.
import { chromium } from "@playwright/test";

const OUT = "/tmp/case_reentry";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const consoleErrors = [];
const sent = [];

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
page.on("console", (m) => {
  if (m.type() === "error") consoleErrors.push(m.text().slice(0, 300));
});
page.on("pageerror", (e) => consoleErrors.push(`PAGEERROR: ${String(e).slice(0, 300)}`));
page.on("websocket", (ws) => {
  ws.on("framesent", (d) => {
    try {
      const t = typeof d.payload === "string" ? d.payload : d.payload.toString();
      const p = JSON.parse(t);
      if (p?.type && p.type !== "session-resume") sent.push(`${p.type}:${t.slice(0, 160)}`);
    } catch {}
  });
});
await page.goto("http://localhost:5173", { waitUntil: "domcontentloaded" });
const anon = page.locator('[data-testid="grace2-auth-gate-anonymous"]');
if (await anon.isVisible({ timeout: 4000 }).catch(() => false)) await anon.click();
await sleep(3000);
await page.screenshot({ path: `${OUT}_1_root.png` });

// Click the first Case row in the rail.
const row = page
  .locator('[data-testid="cases-panel-row"], [data-testid^="case-row"]')
  .first();
const rowVisible = await row.isVisible().catch(() => false);
if (rowVisible) {
  await row.click();
} else {
  await page.getByText("Compute Colored Relief", { exact: false }).first().click().catch((e) => consoleErrors.push("click fail: " + e.message));
}
await sleep(4000);
await page.screenshot({ path: `${OUT}_2_after_click.png` });

console.log("SENT FRAMES:", JSON.stringify(sent, null, 1));
console.log("CONSOLE ERRORS:", JSON.stringify(consoleErrors, null, 1));
await browser.close();
