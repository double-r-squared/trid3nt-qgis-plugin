// P0 LIVE REPRO: published layers appear in Layers panel but DO NOT render on map.
// Signs in, captures EVERY console msg + EVERY failed/blocked request + every WS
// session-state frame (to see the render face the client receives), sends a cheap
// roads prompt (geocode + fetch_roads_osm + publish_layer), then inspects the LIVE
// MapLibre style via window.__grace2GetMap() to determine WHY the layer_id has no
// rendered source/layer. Root-cause classification printed at the end.
import { chromium } from "playwright";
import fs from "node:fs";

const SITE = "https://d125yfbyjrpbre.cloudfront.net/app";
const CF = "d125yfbyjrpbre.cloudfront.net";
const EMAIL = process.env.GRACE2_DEMO_EMAIL || "grace2-demo@example.com";
const PW = process.env.GRACE2_DEMO_PASSWORD || "Grace2Demo2026";
const PROMPT = process.env.P0_PROMPT || "Add the roads in Fort Myers, Florida as a layer.";
const OUT = process.env.P0_OUT || "/home/nate/Documents/GRACE-2/reports/inflight/p0_render";
const BUDGET_MS = 12 * 60 * 1000;

fs.mkdirSync(OUT.substring(0, OUT.lastIndexOf("/")), { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });

// ---- capture EVERYTHING ----
const consoleMsgs = []; // {type, text}
const mapViewLogs = []; // any [MapView]/[Map] log
const pageErrors = [];
const failedReqs = []; // request failed (DNS/CORS/blocked)
const httpErrs = []; // >=400 responses
const tileReqs = []; // /cog/tiles
const sessionFrames = []; // parsed loaded_layers snapshots from WS
const rawLayerFrames = []; // full raw frame text containing layer render faces

page.on("console", (m) => {
  const t = m.text();
  consoleMsgs.push({ type: m.type(), text: t.slice(0, 500) });
  if (/\[Map(View)?\]/.test(t)) mapViewLogs.push(`${m.type()}: ${t.slice(0, 300)}`);
});
page.on("pageerror", (e) => pageErrors.push(String(e && e.stack ? e.stack : e).slice(0, 800)));
page.on("requestfailed", (r) => {
  failedReqs.push(`${r.failure()?.errorText || "?"} ${r.method()} ${r.url().slice(0, 120)}`);
});
page.on("response", (r) => {
  const url = r.url();
  const st = r.status();
  if (url.includes("/cog/tiles/")) tileReqs.push(`${st} ${url.slice(0, 110)}`);
  if (st >= 400 && (url.includes(CF) || url.includes("amazonaws") || url.includes("/cog/"))) {
    httpErrs.push(`${st} ${r.request().method()} ${url.slice(0, 120)}`);
  }
});
page.on("websocket", (ws) => {
  ws.on("framereceived", (ev) => {
    const d = typeof ev.payload === "string" ? ev.payload : ev.payload?.toString?.() || "";
    if (!d.includes("loaded_layers") && !d.includes("layer_id")) return;
    try {
      const j = JSON.parse(d);
      const ll = j?.payload?.loaded_layers ?? j?.loaded_layers ?? j?.payload?.session_state?.loaded_layers;
      if (Array.isArray(ll)) {
        sessionFrames.push(
          ll.map((l) => ({
            id: l.layer_id,
            type: l.layer_type,
            uri: (l.uri || l.source_url || "").slice(0, 90),
            has_inline: l.inline_geojson !== undefined && l.inline_geojson !== null,
            inline_n: l.inline_geojson?.features?.length,
            wms_url: l.wms_url ? String(l.wms_url).slice(0, 60) : undefined,
            keys: Object.keys(l),
          })),
        );
        // keep the raw frame for the LAST snapshot so we can dump the full render face
        rawLayerFrames.push(d.slice(0, 4000));
      }
    } catch {}
  });
});

const log = (...a) => console.log(...a);

// ---- sign in ----
await page.goto(SITE, { waitUntil: "networkidle", timeout: 60000 });
await page.waitForTimeout(2500);
let chatInput = await page.locator('[data-testid="chat-input"]').count();
if (!chatInput) {
  await page.getByRole("button", { name: /Sign in|Sign up/i }).first().click().catch(() => {});
  await page.waitForTimeout(5000);
  if (/amazoncognito/.test(page.url())) {
    const u = page.locator('input[name="username"]:visible, input[type="email"]:visible').first();
    await u.waitFor({ timeout: 12000 }).catch(() => {});
    await u.fill(EMAIL).catch(() => {});
    await page.locator('input[name="password"]:visible, input[type="password"]:visible').first().fill(PW).catch(() => {});
    await page
      .locator('input[name="signInSubmitButton"]:visible, input[type="submit"]:visible, button[type="submit"]:visible')
      .first()
      .click()
      .catch(() => {});
  }
  for (let i = 0; i < 24; i++) {
    await page.waitForTimeout(1500);
    if (page.url().includes(CF) && !/amazoncognito/.test(page.url())) break;
  }
  await page.waitForTimeout(6000);
  chatInput = await page.locator('[data-testid="chat-input"]').count();
}
log(`[signin] chatInput=${chatInput} url=${page.url().slice(0, 70)}`);
await page.screenshot({ path: `${OUT}_0_signedin.png` });
if (!chatInput) {
  log("[FATAL] no chat input after sign-in; aborting");
  await browser.close();
  process.exit(2);
}

// confirm the map seam exists
const seam = await page.evaluate(() => ({
  getMap: typeof window.__grace2GetMap === "function",
  hasMap: typeof window.__grace2GetMap === "function" ? !!window.__grace2GetMap() : false,
}));
log(`[seam] __grace2GetMap=${seam.getMap} hasMapInstance=${seam.hasMap}`);

// ---- send the cheap roads prompt ----
const input = page.locator('[data-testid="chat-input"]');
await input.fill(PROMPT);
await input.press("Enter");
log(`[prompt] sent: "${PROMPT}"`);

// ---- wait for the layer to land in the panel ----
const start = Date.now();
let panelLayerIds = [];
let done = false;
let shot = 1;
let lastShot = 0;
while (Date.now() - start < BUDGET_MS) {
  await page.waitForTimeout(4000);
  const t = Math.round((Date.now() - start) / 1000);

  // auto-approve any payload / gate dialogs
  for (const sel of ['[data-testid="payload-warning-button-proceed"]', '[data-testid="sandbox-card-proceed"]']) {
    const b = page.locator(sel);
    if (await b.count()) await b.first().click().catch(() => {});
  }
  for (const name of [/^Proceed$/i, /^Run$/i, /^Approve$/i, /^Confirm$/i]) {
    const b = page.getByRole("button", { name });
    if (await b.count()) await b.first().click().catch(() => {});
  }

  // read layer-panel row ids
  panelLayerIds = await page.evaluate(() => {
    const rows = Array.from(document.querySelectorAll("[data-layer-id]"));
    return rows.map((r) => r.getAttribute("data-layer-id"));
  });

  if (t - lastShot >= 45) {
    lastShot = t;
    await page.screenshot({ path: `${OUT}_t${t}s.png` });
    log(`[t${t}s] panelLayers=${panelLayerIds.length} tileReqs=${tileReqs.length} sessFrames=${sessionFrames.length}`);
    shot++;
  }

  if (panelLayerIds.length > 0 && t > 20) {
    // give the map a couple more idle cycles to attempt the add, then stop
    await page.waitForTimeout(8000);
    done = true;
    break;
  }
  const body = await page.evaluate(() => document.body.innerText);
  if (/i (was )?unable|couldn'?t|failed to|no roads|error/i.test(body) && t > 60 && panelLayerIds.length === 0) {
    log(`[narration-fail] agent reported failure at t=${t}s`);
    break;
  }
}

await page.waitForTimeout(2000);
await page.screenshot({ path: `${OUT}_FINAL_map.png` });

// ---- INSPECT THE LIVE MAP STYLE ----
const probe = await page.evaluate((panelIds) => {
  const out = { panelIds, styleLoaded: null, sources: [], layers: [], matches: {}, addError: null };
  const m = window.__grace2GetMap?.();
  if (!m) {
    out.addError = "no map instance from __grace2GetMap()";
    return out;
  }
  try {
    out.styleLoaded = m.isStyleLoaded();
    const style = m.getStyle();
    out.sources = Object.keys(style.sources || {});
    out.layers = (style.layers || []).map((l) => ({ id: l.id, type: l.type, source: l.source }));
    for (const id of panelIds) {
      const srcExists = !!m.getSource(id);
      const layerExists = !!m.getLayer(id);
      let sourceSpec = null;
      try {
        const s = m.getStyle().sources[id];
        sourceSpec = s ? { type: s.type, tiles: s.tiles?.[0]?.slice(0, 120), dataType: typeof s.data } : null;
      } catch {}
      out.matches[id] = { srcExists, layerExists, sourceSpec };
    }
  } catch (e) {
    out.addError = String(e);
  }
  return out;
}, panelLayerIds);

log("\n========== LIVE MAP STYLE PROBE ==========");
log(`styleLoaded=${probe.styleLoaded} addError=${probe.addError}`);
log(`panel layer ids (${panelLayerIds.length}): ${JSON.stringify(panelLayerIds)}`);
log(`map sources (${probe.sources.length}): ${JSON.stringify(probe.sources)}`);
log(`map layers (${probe.layers.length}):`);
for (const l of probe.layers) log(`   - ${l.id} [${l.type}] src=${l.source}`);
log("per-panel-layer presence in style:");
for (const [id, v] of Object.entries(probe.matches)) {
  log(`   - ${id}: sourceInStyle=${v.srcExists} layerInStyle=${v.layerExists} spec=${JSON.stringify(v.sourceSpec)}`);
}

log("\n========== SESSION-STATE RENDER FACE (last WS frame) ==========");
const lastSess = sessionFrames[sessionFrames.length - 1] || [];
log(`session-state snapshots seen: ${sessionFrames.length}; last has ${lastSess.length} layers`);
for (const l of lastSess) {
  log(
    `   - ${l.id} [${l.type}] inline=${l.has_inline}${l.inline_n != null ? "(" + l.inline_n + " feats)" : ""} ` +
      `wms_url=${l.wms_url || "-"} uri=${l.uri || "-"}`,
  );
  log(`       wire keys: ${JSON.stringify(l.keys)}`);
}

log("\n========== [MapView] CLIENT RENDER LOGS ==========");
for (const ml of mapViewLogs.slice(-40)) log(`   ${ml}`);

log("\n========== CONSOLE ERRORS / WARNINGS ==========");
const errsWarns = consoleMsgs.filter((m) => m.type === "error" || m.type === "warning");
log(`total console errors+warnings: ${errsWarns.length}`);
for (const e of errsWarns.slice(-30)) log(`   [${e.type}] ${e.text}`);

log("\n========== PAGE (uncaught) ERRORS ==========");
for (const e of pageErrors.slice(-10)) log(`   ${e}`);

log("\n========== FAILED / BLOCKED REQUESTS ==========");
for (const f of failedReqs.slice(-20)) log(`   ${f}`);
log("\n========== HTTP >=400 (CF / s3 / cog) ==========");
for (const h of httpErrs.slice(-20)) log(`   ${h}`);

log("\n========== /cog/tiles REQUESTS ==========");
log(`total: ${tileReqs.length}`);
for (const tr of tileReqs.slice(0, 12)) log(`   ${tr}`);

// dump full last raw frame for render-face forensics
fs.writeFileSync(`${OUT}_last_ws_frame.json`, rawLayerFrames[rawLayerFrames.length - 1] || "(none)");
log(`\n[saved] last raw WS layer frame -> ${OUT}_last_ws_frame.json`);
log(`[saved] final map screenshot -> ${OUT}_FINAL_map.png`);

// ---- ROOT CAUSE CLASSIFICATION ----
log("\n========== ROOT-CAUSE CLASSIFICATION ==========");
const anyPanel = panelLayerIds.length > 0;
const anyMatch = Object.values(probe.matches).find((v) => v.srcExists || v.layerExists);
const faceMissing = lastSess.length > 0 && lastSess.every((l) => !l.has_inline && !l.wms_url && !l.uri);
if (!anyPanel) {
  log("INCONCLUSIVE: no layer reached the Layers panel (agent may not have published). See narration tail.");
} else if (probe.addError) {
  log(`(e) MAP UNAVAILABLE / exception probing style: ${probe.addError}`);
} else if (faceMissing) {
  log("(d) session-state layer(s) LACK a render face (no inline_geojson / wms_url / uri).");
} else if (!anyMatch) {
  log("(b) source/layer NEVER added to the MapLibre style (panel has id, style does not). Check [MapView] logs + console errors above.");
} else if (tileReqs.length > 0 && tileReqs.every((t) => t.startsWith("4") || t.startsWith("5"))) {
  log("(c) source added but ALL tile requests failed. See /cog/tiles statuses above.");
} else {
  log("(a/other) source+layer present in style. If still not visible, check opacity/visibility/bounds or paint. See probe above.");
}

const body = await page.evaluate(() => document.body.innerText);
log("\n[narration tail]\n" + body.split("\n").filter(Boolean).slice(-16).join("\n"));

await browser.close();
process.exit(0);
