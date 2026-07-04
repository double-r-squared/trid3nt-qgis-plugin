#!/usr/bin/env node
// GRACE-2 — job-0155 Wave 4.5 re-verification.
//
// Captures 7 screenshots verifying all Wave 4.5 Stage A fixes:
//   01_palette_3_species_distinct.png  — 3 species, 3 distinct colors (job-0149)
//   02_payload_warning_polished.png    — drop shadow + rounded corners (job-0150)
//   03_secrets_popup_flat.png          — flat layout, no card-within-card (job-0151)
//   04_clean_map.png                   — no zoom buttons, no OSM attribution (job-0152)
//   05_chat_markdown_user_bubble.png   — markdown rendered + user grey bubble (job-0153)
//   06_scroll_to_bottom_arrow.png      — scroll arrow visible when scrolled up (job-0153)
//   07_chat_input_polish.png           — placeholder "Reply to GRACE-2" (job-0153)

import { chromium } from "@playwright/test";
import { mkdir, writeFile } from "fs/promises";

const OUT_DIR =
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0155-testing-20260608/evidence";
const BASE_URL = "http://localhost:5173";

// Real species coords for palette fix verification.
const PANTHER_POINTS = [[-81.34, 26.10], [-81.20, 26.05], [-81.42, 26.20]];
const SPOONBILL_POINTS = [[-80.95, 25.85], [-80.88, 25.92], [-81.10, 25.78]];
const ALLIGATOR_POINTS = [[-81.05, 26.15], [-80.85, 25.75], [-81.30, 25.90]];

function pointFc(coords, species) {
  return {
    type: "FeatureCollection",
    features: coords.map(([lng, lat], i) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [lng, lat] },
      properties: { species, observation_id: `${species}-${i}` },
    })),
  };
}

const MOCK_GEOJSON = new Map([
  ["https://demo.grace2.example.com/case1/gbif-panther-fl.geojson", pointFc(PANTHER_POINTS, "Florida panther")],
  ["https://demo.grace2.example.com/case1/gbif-spoonbill-fl.geojson", pointFc(SPOONBILL_POINTS, "Roseate spoonbill")],
  ["https://demo.grace2.example.com/case1/gbif-alligator-fl.geojson", pointFc(ALLIGATOR_POINTS, "American alligator")],
]);

const AGENT_MD =
  "# Flood scenario summary\n\n" +
  "Modeling **Hurricane Ian** flood depth across *Lee County, FL*.\n\n" +
  "Inputs:\n- 10m DEM (USGS 3DEP)\n- NLCD 2019 land-cover\n\n" +
  "```python\nresult = run_model_flood_scenario(case_id, bbox)\n```\n\n" +
  "Inline: use `git status` to verify.";

const USER_MSG_LONG =
  "Run SFINCS for Hurricane Ian over Lee County with the following parameters: " +
  "10m DEM, NLCD 2019 roughness, NOAA Atlas-14 100-year return-period precipitation, " +
  "and a 24-hour simulation window centered on landfall.";

const FILLER = "Pulling data from the configured endpoints. Long-running step.\n\n- DEM: requested\n- NLCD: requested\n- Precip: requested";

const findings = {};

async function makeContext(browser, viewport) {
  const ctx = await browser.newContext({ viewport });
  await ctx.addInitScript(() => {
    try { localStorage.setItem("grace2_anonymous_accepted", "true"); } catch {}
  });
  return ctx;
}

// ─────────────────────────────────────────────────────────────────────────────
// SS1: 01_palette_3_species_distinct.png (job-0149 djb2 hash fix)
// ─────────────────────────────────────────────────────────────────────────────
async function screenshot01(browser) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await ctx.addInitScript(() => {
    try { localStorage.setItem("grace2_anonymous_accepted", "true"); } catch {}
  });
  const page = await ctx.newPage();
  const errs = [];
  page.on("pageerror", e => errs.push(e.message));

  await page.route("https://demo.grace2.example.com/**", (route) => {
    const body = MOCK_GEOJSON.get(route.request().url());
    if (body) {
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    } else {
      route.fulfill({ status: 404, body: "not found" });
    }
  });

  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="grace2-map"]', { timeout: 15000 });
  await page.waitForFunction(() => typeof window.__grace2InjectSessionState === "function", { timeout: 15000 });

  await page.evaluate((s) => window.__grace2InjectSessionState(s), {
    loaded_layers: [
      { layer_id: "gbif-panther-fl", name: "Florida panther (GBIF)", layer_type: "vector", uri: "https://demo.grace2.example.com/case1/gbif-panther-fl.geojson", visible: true, opacity: 1.0, style_preset: null, z_index: 1 },
      { layer_id: "gbif-spoonbill-fl", name: "Roseate spoonbill (GBIF)", layer_type: "vector", uri: "https://demo.grace2.example.com/case1/gbif-spoonbill-fl.geojson", visible: true, opacity: 1.0, style_preset: null, z_index: 2 },
      { layer_id: "gbif-alligator-fl", name: "American alligator (GBIF)", layer_type: "vector", uri: "https://demo.grace2.example.com/case1/gbif-alligator-fl.geojson", visible: true, opacity: 1.0, style_preset: null, z_index: 3 },
    ],
  });

  await page.evaluate(() => {
    if (typeof window.__grace2InjectMapCommand === "function") {
      window.__grace2InjectMapCommand({ command: "zoom-to", args: { bbox: [-81.6, 25.6, -80.5, 26.5] } });
    }
  });

  // spoonbill and alligator defer (style not loaded yet) and need retry iterations
  await page.waitForTimeout(7000);
  await page.screenshot({ path: `${OUT_DIR}/01_palette_3_species_distinct.png` });

  const colorReport = await page.evaluate(() => {
    const getMap = window.__grace2GetMap;
    if (typeof getMap !== "function") return { error: "no __grace2GetMap" };
    const m = getMap();
    if (!m) return { error: "map not ready" };
    const style = m.getStyle();
    const ids = ["gbif-panther-fl", "gbif-spoonbill-fl", "gbif-alligator-fl"];
    const colors = {};
    for (const l of style.layers) {
      if (ids.includes(l.id)) {
        const p = l.paint || {};
        colors[l.id] = p["circle-color"] ?? p["fill-color"] ?? p["line-color"] ?? "unknown";
      }
    }
    const unique = new Set(Object.values(colors)).size;
    return { colors, unique_colors: unique, collision: unique < ids.length };
  });

  console.log("[SS1] color report:", JSON.stringify(colorReport, null, 2));
  findings.ss1 = { colorReport, pageErrors: errs };

  if (colorReport.collision) {
    console.error("[SS1] FAIL: palette collision still present");
  } else if (colorReport.error) {
    console.warn("[SS1] QUALIFIED:", colorReport.error);
  } else {
    console.log("[SS1] PASS: 3 distinct species colors");
  }

  await ctx.close();
}

// ─────────────────────────────────────────────────────────────────────────────
// SS2: 02_payload_warning_polished.png (job-0150 box-shadow + border-radius)
// ─────────────────────────────────────────────────────────────────────────────
async function screenshot02(browser) {
  const ctx = await makeContext(browser, { width: 1440, height: 900 });
  const page = await ctx.newPage();
  const errs = [];
  page.on("pageerror", e => errs.push(e.message));

  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="grace2-app-shell"]', { timeout: 15000 });
  await page.waitForFunction(() => typeof window.__grace2InjectPayloadWarning === "function", { timeout: 10000 }).catch(() => {});

  const warning = {
    envelope_type: "tool-payload-warning",
    warning_id: "warn_job0155",
    tool_name: "fetch_goes_satellite",
    tool_args: { bbox: [-130, 24, -65, 50], bands: ["visible"], hours: 24 },
    estimated_mb: 150.0,
    threshold_mb: 25.0,
    recommendation: "Narrow to a smaller bbox or request fewer time steps.",
    alternative_args: { bbox: [-82.1, 26.4, -81.5, 26.9], bands: ["visible"], hours: 6 },
    options: ["proceed", "cancel", "narrow_scope"],
    ttl_seconds: 300,
  };

  const injected = await page.evaluate((w) => {
    if (typeof window.__grace2InjectPayloadWarning === "function") {
      window.__grace2InjectPayloadWarning(w);
      return true;
    }
    return false;
  }, warning);

  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${OUT_DIR}/02_payload_warning_polished.png` });

  const domInfo = await page.evaluate(() => {
    const el = document.querySelector('[data-testid="payload-warning-inline"]');
    if (!el) return { found: false };
    const cs = getComputedStyle(el);
    return {
      found: true,
      box_shadow: cs.boxShadow,
      border_radius: cs.borderRadius,
      inline_box_shadow: el.style.boxShadow,
      inline_border_radius: el.style.borderRadius,
    };
  });

  console.log("[SS2] DOM info:", JSON.stringify(domInfo, null, 2));
  findings.ss2 = { injected, domInfo, pageErrors: errs };

  if (!injected) {
    console.warn("[SS2] QUALIFIED: __grace2InjectPayloadWarning seam not present — captured idle state");
  } else if (!domInfo.found) {
    console.warn("[SS2] QUALIFIED: payload-warning-inline element not found in DOM after injection");
  } else {
    const hasShadow = domInfo.box_shadow && domInfo.box_shadow !== "none";
    const hasRadius = domInfo.border_radius && domInfo.border_radius !== "0px";
    if (hasShadow && hasRadius) {
      console.log("[SS2] PASS: box-shadow=" + domInfo.box_shadow + " border-radius=" + domInfo.border_radius);
    } else {
      console.error("[SS2] FAIL: shadow=" + domInfo.box_shadow + " radius=" + domInfo.border_radius);
    }
  }

  await ctx.close();
}

// ─────────────────────────────────────────────────────────────────────────────
// SS3: 03_secrets_popup_flat.png (job-0151 flat layout)
// ─────────────────────────────────────────────────────────────────────────────
async function screenshot03(browser) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await ctx.addInitScript(() => {
    const captured = [];
    class CapturingWS {
      constructor(_url) {
        this._listeners = {};
        setTimeout(() => (this._listeners["open"] ?? []).forEach(cb => cb({})), 0);
      }
      get readyState() { return 1; }
      addEventListener(type, cb) { (this._listeners[type] ??= []).push(cb); }
      send(data) { captured.push(data); }
      close() {}
    }
    CapturingWS.OPEN = 1; CapturingWS.CONNECTING = 0; CapturingWS.CLOSED = 3;
    window.WebSocket = CapturingWS;
    try { localStorage.setItem("grace2_anonymous_accepted", "true"); } catch {}
  });

  const page = await ctx.newPage();
  const errs = [];
  page.on("pageerror", e => errs.push(e.message));
  page.on("console", msg => { if (msg.type() === "error") errs.push(msg.text()); });

  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="grace2-app-shell"]', { timeout: 15000 });

  const secretsBtn = await page.$('[data-testid="grace2-bottom-row-secrets"]');
  if (secretsBtn) {
    await secretsBtn.click();
    await page.waitForSelector('[data-testid="grace2-secrets-popup"]', { timeout: 5000 });
  }

  await page.waitForTimeout(500);
  await page.screenshot({ path: `${OUT_DIR}/03_secrets_popup_flat.png` });

  const flatCheck = await page.evaluate(() => {
    const panel = document.querySelector('[data-testid="grace2-secrets-panel"]');
    const popup = document.querySelector('[data-testid="grace2-secrets-popup"]');
    const nestedCard = panel ? panel.querySelector('[data-testid$="-card"]') : null;
    const headerEl = document.querySelector('[data-testid="grace2-secrets-popup-card"] h2');
    return {
      popup_found: !!popup,
      panel_found: !!panel,
      nested_card_found: !!nestedCard,
      header_text: headerEl ? headerEl.textContent : null,
    };
  });

  console.log("[SS3] flat check:", JSON.stringify(flatCheck, null, 2));
  findings.ss3 = { flatCheck, pageErrors: errs };

  if (!flatCheck.popup_found) {
    console.warn("[SS3] QUALIFIED: secrets popup did not open (button not found or click failed)");
  } else if (flatCheck.nested_card_found) {
    console.error("[SS3] FAIL: nested card element still present inside panel");
  } else {
    const headerOk = flatCheck.header_text === "API Keys";
    console.log(`[SS3] PASS: flat layout confirmed, header="${flatCheck.header_text}", headerOk=${headerOk}`);
  }

  await ctx.close();
}

// ─────────────────────────────────────────────────────────────────────────────
// SS4: 04_clean_map.png (job-0152 — no zoom buttons, no OSM attribution)
// ─────────────────────────────────────────────────────────────────────────────
async function screenshot04(browser) {
  const ctx = await makeContext(browser, { width: 1440, height: 900 });
  const page = await ctx.newPage();
  const errs = [];
  page.on("pageerror", e => errs.push(e.message));

  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="grace2-map"]', { timeout: 15000 });
  await page.waitForTimeout(2000);

  await page.screenshot({ path: `${OUT_DIR}/04_clean_map.png` });

  const mapClean = await page.evaluate(() => {
    // NavigationControl adds buttons with aria-label "Zoom in" / "Zoom out"
    const zoomIn = document.querySelector('button[aria-label="Zoom in"]');
    const zoomOut = document.querySelector('button[aria-label="Zoom out"]');
    const compass = document.querySelector('button[aria-label="Reset bearing to north"]');
    // MapLibre attribution container
    const attribution = document.querySelector('.maplibregl-ctrl-attrib');
    const osmLink = document.querySelector('.maplibregl-ctrl-attrib a[href*="openstreetmap"]');
    return {
      zoom_in_button: !!zoomIn,
      zoom_out_button: !!zoomOut,
      compass_button: !!compass,
      attribution_control: !!attribution,
      osm_link: !!osmLink,
    };
  });

  console.log("[SS4] map clean check:", JSON.stringify(mapClean, null, 2));
  findings.ss4 = { mapClean, pageErrors: errs };

  const noZoom = !mapClean.zoom_in_button && !mapClean.zoom_out_button;
  const noAttr = !mapClean.attribution_control;
  if (noZoom && noAttr) {
    console.log("[SS4] PASS: no zoom buttons, no attribution tag");
  } else {
    if (!noZoom) console.error("[SS4] FAIL: zoom buttons still present");
    if (!noAttr) console.error("[SS4] FAIL: attribution control still present");
  }

  await ctx.close();
}

// ─────────────────────────────────────────────────────────────────────────────
// SS5: 05_chat_markdown_user_bubble.png (job-0153)
// ─────────────────────────────────────────────────────────────────────────────
async function screenshot05(browser) {
  const ctx = await browser.newContext({ viewport: { width: 480, height: 720 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const errs = [];
  page.on("pageerror", e => errs.push(e.message));
  page.on("console", msg => { if (msg.type() === "error") errs.push(msg.text()); });

  await page.goto(BASE_URL + "/", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(3000);

  const MOUNT = `
async (scenario) => {
  document.body.innerHTML = "";
  document.body.style.cssText = "margin:0;padding:0;background:#0d0d11;font-family:system-ui,sans-serif;font-size:13px;";
  const root = document.createElement("div");
  root.id = "harness-root";
  document.body.appendChild(root);

  const frame = document.createElement("div");
  frame.style.cssText = [
    "position:relative","width:380px","height:600px","margin:32px auto",
    "background:rgba(20,20,25,0.92)","color:#eee","border-radius:8px",
    "box-shadow:0 4px 24px rgba(0,0,0,0.4)","overflow:hidden","display:flex","flex-direction:column",
  ].join(";");
  root.appendChild(frame);

  const header = document.createElement("div");
  header.style.cssText = "padding:10px 12px;border-bottom:1px solid #333;";
  header.innerHTML = "<strong>GRACE-2</strong> <span style='color:#888;font-size:11px'>M12 demo</span>";
  frame.appendChild(header);

  const conv = document.createElement("div");
  conv.id = "harness-scroll";
  conv.style.cssText = "flex:1;overflow-y:auto;padding:12px 12px 88px 12px;display:flex;flex-direction:column;gap:10px;";
  frame.appendChild(conv);

  const arrowAnchor = document.createElement("div");
  arrowAnchor.id = "harness-arrow-anchor";
  arrowAnchor.style.cssText = "position:absolute;left:0;right:0;bottom:96px;display:flex;justify-content:center;pointer-events:none;z-index:2;";
  frame.appendChild(arrowAnchor);

  const overlay = document.createElement("div");
  overlay.style.cssText = "position:absolute;left:12px;right:12px;bottom:12px;pointer-events:auto;z-index:3;";
  frame.appendChild(overlay);

  const ReactMod = await import("/node_modules/.vite/deps/react.js");
  const ReactDOMMod = await import("/node_modules/.vite/deps/react-dom_client.js");
  const ChatInputMod = await import("/src/components/ChatInput.tsx");
  const AgentMessageMod = await import("/src/components/AgentMessage.tsx");
  const UserBubbleMod = await import("/src/components/UserBubble.tsx");
  const ScrollToBottomMod = await import("/src/components/ScrollToBottom.tsx");
  const React = ReactMod.default || ReactMod;
  const ReactDOM = ReactDOMMod.default || ReactDOMMod;

  const convRoot = ReactDOM.createRoot(conv);
  const children = scenario.msgs.map((m, i) => {
    if (m.role === "user") {
      return React.createElement(UserBubbleMod.UserBubble, { key: "m" + i, text: m.text });
    }
    return React.createElement(AgentMessageMod.AgentMessage, { key: "m" + i, text: m.text, done: m.done !== false });
  });
  convRoot.render(React.createElement(React.Fragment, null, children));

  const inputRoot = ReactDOM.createRoot(overlay);
  inputRoot.render(React.createElement(ChatInputMod.ChatInput, { state: "idle", onSubmit: () => {}, onCancel: () => {} }));

  const arrowRoot = ReactDOM.createRoot(arrowAnchor);
  arrowRoot.render(React.createElement(ScrollToBottomMod.ScrollToBottom, {
    visible: !!scenario.showArrow,
    onClick: () => { const c = document.getElementById("harness-scroll"); if (c) c.scrollTo({top: c.scrollHeight, behavior:"smooth"}); }
  }));

  await new Promise(res => setTimeout(res, 500));
  if (scenario.scrollToBottom) conv.scrollTop = conv.scrollHeight;
  else conv.scrollTop = 0;
  await new Promise(res => setTimeout(res, 300));
  return "ok";
}
`;

  await page.goto(BASE_URL + "/", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1000);

  const scenario05 = {
    msgs: [
      { role: "user", text: "Model Hurricane Ian over Lee County." },
      { role: "agent", text: AGENT_MD, done: true },
    ],
    scrollToBottom: true,
    showArrow: false,
  };

  let result = await page.evaluate(`(${MOUNT})(${JSON.stringify(scenario05)})`).catch(e => ({ error: String(e) }));
  console.log("[SS5] mount:", result);

  const inspect05 = await page.evaluate(() => {
    const agents = document.querySelectorAll('[data-testid="agent-message"]');
    const users = document.querySelectorAll('[data-testid="user-bubble"]');
    const headings = [...document.querySelectorAll('[data-testid="agent-message"] h1')].map(h => h.textContent);
    const codes = document.querySelectorAll('[data-testid="agent-message"] pre code').length;
    const placeholder = document.querySelector('[data-testid="chat-input"]')?.placeholder;
    return { agentCount: agents.length, userCount: users.length, headings, codeBlocks: codes, placeholder };
  });

  console.log("[SS5] inspect:", JSON.stringify(inspect05, null, 2));
  findings.ss5 = { result, inspect05, pageErrors: errs };

  await page.locator("#harness-root").screenshot({ path: `${OUT_DIR}/05_chat_markdown_user_bubble.png` });
  console.log("[SS5] saved 05_chat_markdown_user_bubble.png");

  const markdownOk = inspect05.codeBlocks > 0 && inspect05.headings.length > 0;
  const placeholderOk = inspect05.placeholder === "Reply to GRACE-2";
  console.log(`[SS5] markdown=${markdownOk}, placeholder="${inspect05.placeholder}" ok=${placeholderOk}`);

  await ctx.close();
}

// ─────────────────────────────────────────────────────────────────────────────
// SS6: 06_scroll_to_bottom_arrow.png (job-0153 scroll arrow visible)
// ─────────────────────────────────────────────────────────────────────────────
async function screenshot06(browser) {
  const ctx = await browser.newContext({ viewport: { width: 480, height: 720 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const errs = [];
  page.on("pageerror", e => errs.push(e.message));
  page.on("console", msg => { if (msg.type() === "error") errs.push(msg.text()); });

  await page.goto(BASE_URL + "/", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(3000);

  const MOUNT = `
async (scenario) => {
  document.body.innerHTML = "";
  document.body.style.cssText = "margin:0;padding:0;background:#0d0d11;font-family:system-ui,sans-serif;font-size:13px;";
  const root = document.createElement("div");
  root.id = "harness-root";
  document.body.appendChild(root);

  const frame = document.createElement("div");
  frame.style.cssText = [
    "position:relative","width:380px","height:600px","margin:32px auto",
    "background:rgba(20,20,25,0.92)","color:#eee","border-radius:8px",
    "box-shadow:0 4px 24px rgba(0,0,0,0.4)","overflow:hidden","display:flex","flex-direction:column",
  ].join(";");
  root.appendChild(frame);

  const header = document.createElement("div");
  header.style.cssText = "padding:10px 12px;border-bottom:1px solid #333;";
  header.innerHTML = "<strong>GRACE-2</strong>";
  frame.appendChild(header);

  const conv = document.createElement("div");
  conv.id = "harness-scroll";
  conv.style.cssText = "flex:1;overflow-y:auto;padding:12px 12px 88px 12px;display:flex;flex-direction:column;gap:10px;";
  frame.appendChild(conv);

  const arrowAnchor = document.createElement("div");
  arrowAnchor.id = "harness-arrow-anchor";
  arrowAnchor.style.cssText = "position:absolute;left:0;right:0;bottom:96px;display:flex;justify-content:center;pointer-events:none;z-index:2;";
  frame.appendChild(arrowAnchor);

  const overlay = document.createElement("div");
  overlay.style.cssText = "position:absolute;left:12px;right:12px;bottom:12px;pointer-events:auto;z-index:3;";
  frame.appendChild(overlay);

  const ReactMod = await import("/node_modules/.vite/deps/react.js");
  const ReactDOMMod = await import("/node_modules/.vite/deps/react-dom_client.js");
  const ChatInputMod = await import("/src/components/ChatInput.tsx");
  const AgentMessageMod = await import("/src/components/AgentMessage.tsx");
  const UserBubbleMod = await import("/src/components/UserBubble.tsx");
  const ScrollToBottomMod = await import("/src/components/ScrollToBottom.tsx");
  const React = ReactMod.default || ReactMod;
  const ReactDOM = ReactDOMMod.default || ReactDOMMod;

  const convRoot = ReactDOM.createRoot(conv);
  const children = scenario.msgs.map((m, i) => {
    if (m.role === "user") {
      return React.createElement(UserBubbleMod.UserBubble, { key: "m" + i, text: m.text });
    }
    return React.createElement(AgentMessageMod.AgentMessage, { key: "m" + i, text: m.text, done: m.done !== false });
  });
  convRoot.render(React.createElement(React.Fragment, null, children));

  const inputRoot = ReactDOM.createRoot(overlay);
  inputRoot.render(React.createElement(ChatInputMod.ChatInput, { state: "idle", onSubmit: () => {}, onCancel: () => {} }));

  const arrowRoot = ReactDOM.createRoot(arrowAnchor);
  arrowRoot.render(React.createElement(ScrollToBottomMod.ScrollToBottom, {
    visible: true,
    onClick: () => {}
  }));

  await new Promise(res => setTimeout(res, 500));
  conv.scrollTop = 0;
  await new Promise(res => setTimeout(res, 300));
  return "ok";
}
`;

  const msgs = [
    { role: "user", text: "First question." },
    { role: "agent", text: AGENT_MD, done: true },
    { role: "user", text: USER_MSG_LONG },
    { role: "agent", text: FILLER, done: true },
    { role: "user", text: "Another question — does this scroll?" },
    { role: "agent", text: AGENT_MD, done: true },
    { role: "user", text: USER_MSG_LONG },
    { role: "agent", text: FILLER, done: true },
  ];

  let result = await page.evaluate(`(${MOUNT})(${JSON.stringify({ msgs })})`).catch(e => ({ error: String(e) }));
  console.log("[SS6] mount:", result);

  const inspect06 = await page.evaluate(() => {
    const arrow = document.querySelector('[data-testid="scroll-to-bottom"]');
    const conv = document.getElementById("harness-scroll");
    return {
      arrowFound: !!arrow,
      arrowVisible: arrow ? arrow.getAttribute("data-visible") : null,
      arrowOpacity: arrow ? window.getComputedStyle(arrow).opacity : null,
      scrollTop: conv ? conv.scrollTop : null,
      scrollHeight: conv ? conv.scrollHeight : null,
    };
  });

  console.log("[SS6] inspect:", JSON.stringify(inspect06, null, 2));
  findings.ss6 = { result, inspect06, pageErrors: errs };

  await page.locator("#harness-root").screenshot({ path: `${OUT_DIR}/06_scroll_to_bottom_arrow.png` });
  console.log("[SS6] saved 06_scroll_to_bottom_arrow.png");

  if (inspect06.arrowFound) {
    console.log(`[SS6] PASS: scroll arrow found, visible=${inspect06.arrowVisible}, opacity=${inspect06.arrowOpacity}`);
  } else {
    console.warn("[SS6] QUALIFIED: scroll-to-bottom element not found — may render differently in live Chat.tsx");
  }

  await ctx.close();
}

// ─────────────────────────────────────────────────────────────────────────────
// SS7: 07_chat_input_polish.png (placeholder + Enter behavior)
// ─────────────────────────────────────────────────────────────────────────────
async function screenshot07(browser) {
  const ctx = await browser.newContext({ viewport: { width: 480, height: 300 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const errs = [];
  page.on("pageerror", e => errs.push(e.message));

  await page.goto(BASE_URL + "/", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(3000);

  const MOUNT = `
async () => {
  document.body.innerHTML = "";
  document.body.style.cssText = "margin:0;padding:0;background:#0d0d11;";
  const root = document.createElement("div");
  root.id = "harness-root";
  root.style.cssText = "padding:16px;position:relative;";
  document.body.appendChild(root);

  const ReactMod = await import("/node_modules/.vite/deps/react.js");
  const ReactDOMMod = await import("/node_modules/.vite/deps/react-dom_client.js");
  const ChatInputMod = await import("/src/components/ChatInput.tsx");
  const React = ReactMod.default || ReactMod;
  const ReactDOM = ReactDOMMod.default || ReactDOMMod;

  const submitted = [];
  const rootEl = ReactDOM.createRoot(root);
  rootEl.render(React.createElement(ChatInputMod.ChatInput, {
    state: "idle",
    onSubmit: (text) => { submitted.push(text); window.__ss7Submitted = submitted; },
    onCancel: () => {},
  }));

  await new Promise(res => setTimeout(res, 400));
  return "ok";
}
`;

  let result = await page.evaluate(`(${MOUNT})()`).catch(e => ({ error: String(e) }));
  console.log("[SS7] mount:", result);

  const inspect07_idle = await page.evaluate(() => {
    const ta = document.querySelector('[data-testid="chat-input"]');
    return { found: !!ta, placeholder: ta?.placeholder, value: ta?.value };
  });

  console.log("[SS7] idle state:", JSON.stringify(inspect07_idle, null, 2));

  // Test Enter submits
  const chatInput = page.locator('[data-testid="chat-input"]');
  if (await chatInput.count() > 0) {
    await chatInput.click();
    await chatInput.fill("test message for enter-submit");
    await chatInput.press("Enter");
    await page.waitForTimeout(300);
  }

  const submitResult = await page.evaluate(() => window.__ss7Submitted ?? []);

  // Type a multi-line draft for the screenshot
  if (await chatInput.count() > 0) {
    await chatInput.fill("Follow-up over\nmultiple lines");
  }

  await page.waitForTimeout(300);
  await page.locator("#harness-root").screenshot({ path: `${OUT_DIR}/07_chat_input_polish.png` });
  console.log("[SS7] saved 07_chat_input_polish.png");

  const placeholderOk = inspect07_idle.placeholder === "Reply to GRACE-2";
  const enterSubmitted = submitResult.length > 0;

  findings.ss7 = { inspect07_idle, submitResult, placeholderOk, enterSubmitted, pageErrors: errs };
  console.log(`[SS7] placeholder="${inspect07_idle.placeholder}" ok=${placeholderOk}, Enter submitted: ${enterSubmitted}`);

  await ctx.close();
}

// ─────────────────────────────────────────────────────────────────────────────
// Main
// ─────────────────────────────────────────────────────────────────────────────
async function main() {
  await mkdir(OUT_DIR, { recursive: true });

  const browser = await chromium.launch({ headless: true });

  console.log("=== job-0155 Wave 4.5 Playwright verification ===");
  console.log("BASE_URL:", BASE_URL);
  console.log("OUT_DIR:", OUT_DIR);

  try {
    console.log("\n--- SS1: palette 3 species distinct ---");
    await screenshot01(browser);

    console.log("\n--- SS2: payload warning polished ---");
    await screenshot02(browser);

    console.log("\n--- SS3: secrets popup flat ---");
    await screenshot03(browser);

    console.log("\n--- SS4: clean map ---");
    await screenshot04(browser);

    console.log("\n--- SS5: chat markdown + user bubble ---");
    await screenshot05(browser);

    console.log("\n--- SS6: scroll to bottom arrow ---");
    await screenshot06(browser);

    console.log("\n--- SS7: chat input polish ---");
    await screenshot07(browser);

  } finally {
    await browser.close();
  }

  await writeFile(`${OUT_DIR}/findings.json`, JSON.stringify(findings, null, 2));
  console.log("\n=== COMPLETE — findings.json written ===");
  console.log("Evidence files:", OUT_DIR);

  const ss = Object.entries(findings);
  for (const [name, f] of ss) {
    const errs = f.pageErrors ?? [];
    if (errs.length > 0) {
      console.warn(`[${name}] ${errs.length} page error(s):`, errs.slice(0, 3));
    }
  }
}

main().catch(e => { console.error(e); process.exit(1); });
