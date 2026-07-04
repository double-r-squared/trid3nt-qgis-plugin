// job-0171 diag — drive "Show me weather alerts across America" live.
import { chromium } from "@playwright/test";
import { writeFileSync } from "node:fs";

const OUT_DIR = "/home/nate/Documents/GRACE-2/reports/inflight/job-0171-engine-20260608/evidence";

async function main() {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await ctx.addInitScript(() => {
    try { localStorage.setItem("grace2_anonymous_accepted", "true"); } catch {}
  });
  const page = await ctx.newPage();
  const consoleLines = [];
  page.on("console", (m) => consoleLines.push(`[${m.type()}] ${m.text()}`));
  page.on("pageerror", (e) => consoleLines.push(`[pageerror] ${e.message}`));

  await page.addInitScript(() => {
    const seen = [];
    window.__wsFrames = seen;
    const OrigWS = window.WebSocket;
    function PatchedWS(url, protocols) {
      const ws = protocols ? new OrigWS(url, protocols) : new OrigWS(url);
      const origSend = ws.send.bind(ws);
      ws.send = (data) => {
        try { seen.push({ dir: "out", t: Date.now(), data: String(data).slice(0, 4000) }); } catch {}
        return origSend(data);
      };
      ws.addEventListener("message", (ev) => {
        try { seen.push({ dir: "in", t: Date.now(), data: String(ev.data).slice(0, 16000) }); } catch {}
      });
      return ws;
    }
    PatchedWS.prototype = OrigWS.prototype;
    PatchedWS.OPEN = OrigWS.OPEN; PatchedWS.CLOSED = OrigWS.CLOSED;
    PatchedWS.CONNECTING = OrigWS.CONNECTING; PatchedWS.CLOSING = OrigWS.CLOSING;
    window.WebSocket = PatchedWS;
  });

  await page.goto("http://localhost:5173", { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="grace2-map"]', { timeout: 20000 });
  await page.waitForFunction(() => (window.__wsFrames || []).length > 0, { timeout: 20000 });

  const input = page.locator("textarea").first();
  await input.waitFor({ timeout: 15000 });
  await input.fill("Show me weather alerts across America");
  await input.press("Enter");

  // Alerts query goes through cache fetch+convert; allow up to 60s.
  await page.waitForTimeout(60000);

  await page.screenshot({ path: `${OUT_DIR}/alerts_full_app.png`, fullPage: false });

  const report = await page.evaluate(() => {
    const frames = window.__wsFrames || [];
    const ss = frames.filter((f) => { try { return JSON.parse(f.data).type === "session-state"; } catch { return false; } });
    const lastSs = ss.length ? JSON.parse(ss[ss.length - 1].data).payload : null;
    const layers = lastSs?.loaded_layers ?? [];

    const m = window.__grace2GetMap?.();
    const styleLayers = m ? m.getStyle().layers.map(l => l.id) : [];
    const styleSources = m ? Object.keys(m.getStyle().sources) : [];

    return {
      lastSs_layer_ids: layers.map(l => l.layer_id),
      lastSs_layer_types: layers.map(l => l.layer_type),
      lastSs_layer_uris: layers.map(l => (l.uri || l.source_url || "")),
      mapLayerIds: styleLayers,
      mapSourceIds: styleSources,
    };
  });

  writeFileSync(`${OUT_DIR}/alerts_diag.json`, JSON.stringify({ report, consoleLines: consoleLines.slice(-50) }, null, 2));
  console.log("=== last session-state loaded_layers:", JSON.stringify(report.lastSs_layer_ids));
  console.log("=== uris:", JSON.stringify(report.lastSs_layer_uris));
  console.log("=== map layer ids:", report.mapLayerIds.join(", "));
  console.log("=== console (last 20):");
  for (const l of consoleLines.slice(-20)) console.log("  ", l);

  await browser.close();
}
main().catch((e) => { console.error(e); process.exit(1); });
