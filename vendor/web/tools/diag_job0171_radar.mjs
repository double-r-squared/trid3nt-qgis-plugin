// job-0171 diagnostic — drive live "Show me radar over America" against the
// running agent and capture full evidence of what reaches Map.tsx.
//
// This connects to the SAME agent the user uses, so symptoms reproduce.

import { chromium } from "@playwright/test";
import { writeFileSync } from "node:fs";

const OUT_DIR = "reports/inflight/job-0171-engine-20260608/evidence";

async function main() {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  // Skip the AuthGate by pre-accepting anonymous (matches existing diag pattern).
  await ctx.addInitScript(() => {
    try { localStorage.setItem("grace2_anonymous_accepted", "true"); } catch {}
  });
  const page = await ctx.newPage();

  // Capture all console + page errors verbatim.
  const consoleLines = [];
  page.on("console", (m) => consoleLines.push(`[${m.type()}] ${m.text()}`));
  page.on("pageerror", (e) => consoleLines.push(`[pageerror] ${e.message}`));

  // Hook ALL WS frames for forensic visibility. Patch WebSocket pre-load.
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
        try { seen.push({ dir: "in", t: Date.now(), data: String(ev.data).slice(0, 8000) }); } catch {}
      });
      return ws;
    }
    PatchedWS.prototype = OrigWS.prototype;
    PatchedWS.OPEN = OrigWS.OPEN;
    PatchedWS.CLOSED = OrigWS.CLOSED;
    PatchedWS.CONNECTING = OrigWS.CONNECTING;
    PatchedWS.CLOSING = OrigWS.CLOSING;
    window.WebSocket = PatchedWS;
  });

  await page.goto("http://localhost:5173", { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="grace2-map"]', { timeout: 20000 });

  // Wait for connected status (Chat panel shows it).
  await page.waitForFunction(() => {
    // Either the live socket is OPEN, or we have at least 1 frame in.
    const frames = window.__wsFrames || [];
    return frames.length > 0;
  }, { timeout: 20000 });

  // Type "Show me radar over America" into the chat input.
  const input = page.locator('textarea[data-testid="grace2-chat-input"], textarea[placeholder*="Ask"], textarea').first();
  await input.waitFor({ timeout: 15000 });
  await input.click();
  await input.fill("Show me radar over America");
  await input.press("Enter");

  // Give the agent time to dispatch the tool (NEXRAD is a sync LayerURI return,
  // so session-state should arrive within a few seconds).
  await page.waitForTimeout(30000);

  // Take screenshot of full app.
  await page.screenshot({ path: `${OUT_DIR}/radar_full_app.png`, fullPage: false });

  // Map screenshot only.
  const mapElem = page.locator('[data-testid="grace2-map"]');
  await mapElem.screenshot({ path: `${OUT_DIR}/radar_map_only.png` }).catch(() => {});

  // Introspect what landed on the map.
  const mapReport = await page.evaluate(() => {
    const getMap = window.__grace2GetMap;
    if (typeof getMap !== "function") return { error: "no __grace2GetMap" };
    const m = getMap();
    if (!m) return { error: "no map" };
    const style = m.getStyle();
    const all = style.layers.map((l) => ({
      id: l.id,
      type: l.type,
      source: l.source,
      visibility: (l.layout && l.layout.visibility) ?? "visible",
    }));
    const allSources = Object.keys(style.sources);
    const sourceInfo = {};
    for (const sid of allSources) {
      const s = style.sources[sid];
      sourceInfo[sid] = {
        type: s.type,
        tiles: s.tiles ? s.tiles.slice(0, 1) : null,
      };
    }
    return {
      layer_count: all.length,
      layers: all,
      sources: sourceInfo,
      styleLoaded: m.isStyleLoaded(),
      center: m.getCenter(),
      zoom: m.getZoom(),
    };
  });

  // Capture WS frames (filtered to interesting types).
  const wsFrames = await page.evaluate(() => {
    const frames = window.__wsFrames || [];
    return frames.map((f) => {
      let type = null;
      let parsed = null;
      try {
        const obj = JSON.parse(f.data);
        type = obj.type;
        // Stash a slim summary.
        if (obj.payload) {
          if (type === "session-state") {
            parsed = {
              loaded_layer_ids: (obj.payload.loaded_layers || []).map((l) => l.layer_id),
              loaded_layer_types: (obj.payload.loaded_layers || []).map((l) => l.layer_type),
              loaded_layer_uris: (obj.payload.loaded_layers || []).map((l) => (l.uri || l.source_url || "").slice(0, 200)),
            };
          } else if (type === "user-message") {
            parsed = { text: obj.payload.text };
          } else if (type === "map-command") {
            parsed = { command: obj.payload.command };
          } else if (type === "pipeline-state") {
            parsed = { steps: (obj.payload.steps || obj.payload.current_pipeline?.steps || []).map((s) => ({ name: s.name, state: s.state })) };
          }
        }
      } catch {}
      return { dir: f.dir, t: f.t, type, parsed, len: f.data.length };
    });
  });

  // Layer-panel snapshot — what does the UI show?
  const layerPanel = await page.evaluate(() => {
    const rows = document.querySelectorAll('[data-testid^="grace2-layer-row-"], [data-testid="grace2-layer-row"]');
    return {
      row_count: rows.length,
      row_texts: Array.from(rows).map((r) => r.textContent?.slice(0, 200) ?? ""),
    };
  });

  const report = {
    timestamp: new Date().toISOString(),
    mapReport,
    layerPanel,
    wsFrameCount: wsFrames.length,
    wsFrames,
    consoleLines: consoleLines.slice(-100),
  };
  writeFileSync(`${OUT_DIR}/radar_diag.json`, JSON.stringify(report, null, 2));

  console.log("=== layer panel rows:", layerPanel.row_count);
  console.log("=== map layers:", mapReport.layer_count);
  console.log("=== map layer ids:", mapReport.layers?.map((l) => l.id).join(", "));
  console.log("=== ws frame count:", wsFrames.length);
  const ssCount = wsFrames.filter((f) => f.type === "session-state").length;
  console.log("=== session-state frames received:", ssCount);
  const lastSs = wsFrames.filter((f) => f.type === "session-state").slice(-1)[0];
  if (lastSs) {
    console.log("=== last session-state loaded_layer_ids:", JSON.stringify(lastSs.parsed?.loaded_layer_ids));
    console.log("=== last session-state loaded_layer_types:", JSON.stringify(lastSs.parsed?.loaded_layer_types));
    console.log("=== last session-state loaded_layer_uris:", JSON.stringify(lastSs.parsed?.loaded_layer_uris));
  }

  await browser.close();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
