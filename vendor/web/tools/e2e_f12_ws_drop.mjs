// E2E: F12 live-verify -- a client WS drop mid-turn must not kill the turn or
// misreport LLM_UNAVAILABLE (fix c93a809). ASCII hyphens only; no emojis.
//
// Checks:
//   C1  a tool-producing turn is visibly running (pipeline card) before drop
//   C2  hard-dropping the transport mid-turn (context offline) does not
//       surface an LLM_UNAVAILABLE / error toast in chat
//   C3  agent.log shows the transport-drop backstop fired (NOT a model
//       failure) and/or the turn was DETACHED (kept running), not cancelled
//   C4  after reconnect, the produced layer eventually appears in the panel
//       (replay/persistence backstop)
//   C5  no LLM_UNAVAILABLE / error text visible in the chat transcript at the end
//
// Run from web/: node tools/e2e_f12_ws_drop.mjs

import { chromium } from "playwright";
import { readFile } from "fs/promises";

const APP_URL = "http://127.0.0.1:5173/app";
const AGENT_LOG = "/home/nate/Documents/trid3nt-local/logs/agent.log";
const OUT = "/home/nate/Documents/trid3nt-local/docs/proof";

// fetch_usgs_earthquakes real signature: bbox (west,south,east,north) EPSG:4326,
// start_date/end_date (defaults to last ~30 days when both omitted),
// min_magnitude (default 2.5). NOT "days=" -- that kwarg does not exist.
//
// bbox = Southern California (Ridgecrest / Salton Sea corridor) -- verified
// live against the USGS FDSN endpoint (19 M>=2.5 events in the trailing 30
// days as of this drive) so the tool call is guaranteed non-empty, unlike an
// arbitrary Bay Area bbox which can legitimately return zero events and mask
// the WS-drop signal behind an unrelated no-data tool error.
//
// A first attempt also chained a `publish_layer` follow-up call, but the
// local small model fires BOTH tool calls in the SAME round (it does not
// wait for fetch_usgs_earthquakes's real result before calling publish_layer
// -- it hallucinates a placeholder handle), so publish_layer harmlessly
// errors ("not an s3:// COG"). That is fine (F32 benign-vector-noop territory
// for a well-formed handle, and just a normal tool_dispatch error for a
// hallucinated one) but adds noise; vector fetch tools already auto-render
// inline without publish_layer, so the follow-up call is dropped here to
// keep the prompt's tool-use fully deterministic.
const PROMPT =
  "Call the tool fetch_usgs_earthquakes with exactly these arguments: " +
  "bbox=[-118.5, 33.0, -115.0, 36.0]. Do not pass any other arguments to it " +
  "(the default time window and magnitude floor apply). Do not call any " +
  "other tool.";

const results = [];
function pass(id, evidence) { results.push({ id, ok: true, evidence }); console.log("  PASS", id, "-", evidence); }
function fail(id, evidence) { results.push({ id, ok: false, evidence }); console.log("  FAIL", id, "-", evidence); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function parseLogTs(line) {
  const m = line.match(/^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2}),(\d{3})/);
  if (!m) return null;
  const [, y, mo, d, h, mi, s, ms] = m;
  return new Date(+y, +mo - 1, +d, +h, +mi, +s, +ms).getTime();
}

async function logTail(sinceMs, matchRe) {
  const raw = await readFile(AGENT_LOG, "utf8").catch(() => "");
  const lines = raw.split("\n");
  const out = [];
  for (const line of lines) {
    const t = parseLogTs(line);
    if (t === null || t < sinceMs) continue;
    if (matchRe.test(line)) out.push(line);
  }
  return out;
}

async function main() {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  const page = await ctx.newPage();
  const consoleErrs = [];
  page.on("console", (m) => {
    const t = m.text();
    if (m.type() === "error") consoleErrs.push(t.slice(0, 200));
    if (/LLM_UNAVAILABLE/i.test(t)) console.log("[console]", t.slice(0, 200));
  });

  await page.goto(APP_URL, { waitUntil: "domcontentloaded" });
  const chat = page.getByTestId("chat-input");
  await chat.waitFor({ timeout: 20000 }).catch(() => {});
  if (!(await chat.isVisible().catch(() => false))) {
    fail("APP_LOADED", "chat-input not visible after 20s");
    await browser.close();
    printAndExit();
    return;
  }
  pass("APP_LOADED", "chat-input visible");

  // Scope the later log-evidence search to THIS session id -- the local dev
  // box's WS churns some on its own (Vite/React-StrictMode double-mount
  // reconnects, other already-open tabs polling case-list), so an unscoped
  // "any DETACHED line after tDrop" search can both miss (the real event
  // logged slightly BEFORE our explicit reload lands, mid-navigation) and
  // false-positive (someone else's session). Filtering on our session_id
  // eliminates that ambiguity.
  const sessionId = await page.evaluate(() => localStorage.getItem("grace2.session_id")).catch(() => null);
  console.log("session_id:", sessionId);

  const layerCountBefore = await page.getByTestId("layer-row").count().catch(() => 0);
  console.log("layer rows before turn:", layerCountBefore);

  const tSend = Date.now();
  await chat.click();
  await chat.fill(PROMPT);
  await sleep(200);
  await page.keyboard.press("Enter");
  console.log("prompt sent at", new Date(tSend).toISOString());

  // C1 -- wait for the turn to be visibly running (a pipeline/tool card).
  let running = false;
  const runDeadline = Date.now() + 60000;
  while (Date.now() < runDeadline) {
    const n = await page.getByTestId("pipeline-card").count().catch(() => 0);
    if (n > 0) { running = true; break; }
    await sleep(1500);
  }
  if (running) pass("C1_TURN_RUNNING", "pipeline-card visible before drop");
  else fail("C1_TURN_RUNNING", "no pipeline-card appeared within 60s; dropping anyway");

  // -- hard-drop the transport mid-turn --
  // First attempt used context.setOffline(true) for 10s then false. Evidence
  // from that run: the client's own reconnect logic re-established a NEW
  // socket (server logged "session-resume rebound") within ~1s of restoring
  // network, but NEITHER server-side close-detection log line ("client
  // websocket closed mid-turn" / "connection closed with in-flight turn ...
  // DETACHED") appeared in the following 20s -- the OLD socket's handler
  // apparently never got a chance to observe ConnectionClosed within that
  // window (no send was attempted on the dead socket during the gap, and the
  // `websockets` library's own ping/pong liveness check has a longer period
  // than we waited). A page reload is a strictly harder drop: navigating away
  // destroys the WebSocket object outright, which the server's async read
  // loop observes as an immediate close -- this is the mechanism the task
  // description flagged as the more reliable one, so it is used here instead.
  const tDrop = Date.now();
  console.log("dropping transport (page reload) at", new Date(tDrop).toISOString());
  await page.reload({ waitUntil: "domcontentloaded" }).catch((e) => console.log("reload during drop:", e.message));
  console.log("reload issued at", new Date().toISOString());

  // Give the server a moment to observe the close and log it.
  await sleep(6000);

  // C2 -- no LLM_UNAVAILABLE / error toast visible in chat immediately after drop.
  const wsErrorVisible = await page.getByTestId("ws-error").isVisible().catch(() => false);
  const wsErrorText = wsErrorVisible ? (await page.getByTestId("ws-error").textContent().catch(() => "")) : "";
  if (!wsErrorVisible || !/LLM_UNAVAILABLE/i.test(wsErrorText)) {
    pass("C2_NO_LLM_UNAVAILABLE_TOAST", `ws-error visible=${wsErrorVisible} text="${(wsErrorText || "").slice(0, 100)}"`);
  } else {
    fail("C2_NO_LLM_UNAVAILABLE_TOAST", `ws-error shows LLM_UNAVAILABLE: "${wsErrorText.slice(0, 150)}"`);
  }

  // C3 -- agent.log evidence: the transport-drop backstop and/or DETACHED line
  // for OUR session, scoped to the whole turn lifetime (tSend, not tDrop --
  // see the sessionId comment above for why). Poll briefly since log writes
  // can lag the socket-close observation slightly.
  const sessScopeRe = sessionId ? new RegExp(`session=${sessionId}\\b`) : /./;
  let dropLines = [];
  let detachLines = [];
  const logDeadline = Date.now() + 20000;
  while (Date.now() < logDeadline) {
    const dropAll = await logTail(tSend, /client websocket closed mid-turn \(transport drop, not a model failure\)/i);
    const detachAll = await logTail(tSend, /connection closed with in-flight turn.*DETACHED \(kept running\), not cancelled/i);
    dropLines = dropAll.filter((l) => sessScopeRe.test(l));
    detachLines = detachAll.filter((l) => sessScopeRe.test(l));
    if (dropLines.length > 0 || detachLines.length > 0) break;
    await sleep(2000);
  }
  if (dropLines.length > 0 || detachLines.length > 0) {
    pass("C3_LOG_DETACHED_NOT_CANCELLED", `session=${sessionId} drop-lines=${dropLines.length} detach-lines=${detachLines.length}; sample=${(dropLines[0] || detachLines[0] || "").slice(0, 200)}`);
  } else {
    fail("C3_LOG_DETACHED_NOT_CANCELLED", `no transport-drop backstop or DETACHED line found for session=${sessionId} in agent.log after tSend`);
  }
  // Sanity: no raw model-failure exception logged anywhere in this window.
  // (logger.exception("model stream failed: %s", exc) does not carry a
  // session= field, so this check is intentionally global, not session-scoped.)
  const modelFailLines = await logTail(tSend, /model stream failed:/i);
  if (modelFailLines.length === 0) pass("C3b_NO_MODEL_FAILURE_LOGGED", "no 'model stream failed' lines in window");
  else fail("C3b_NO_MODEL_FAILURE_LOGGED", `found: ${modelFailLines[0].slice(0, 160)}`);

  // -- reconnect: the drop WAS the reload (durable session_id in localStorage
  // resumes the same session + replays state); just confirm the chat input
  // came back up on the reloaded page. --
  await chat.waitFor({ timeout: 20000 }).catch(() => {});
  const reconnected = await chat.isVisible().catch(() => false);
  if (reconnected) pass("RECONNECTED", "chat-input visible after reload");
  else fail("RECONNECTED", "chat-input not visible after reload");

  // C4 -- the layer eventually appears (replay/persistence backstop).
  let layerFound = false;
  const layerDeadline = Date.now() + 240000; // 4 min
  while (Date.now() < layerDeadline) {
    const n = await page.getByTestId("layer-row").count().catch(() => 0);
    if (n > layerCountBefore) { layerFound = true; break; }
    await sleep(5000);
  }
  const layerCountAfter = await page.getByTestId("layer-row").count().catch(() => 0);
  if (layerFound) pass("C4_LAYER_APPEARS", `layer rows before=${layerCountBefore} after=${layerCountAfter}`);
  else fail("C4_LAYER_APPEARS", `no new layer row within 4min (before=${layerCountBefore} after=${layerCountAfter})`);

  // C5 -- final chat transcript has no LLM_UNAVAILABLE / error text.
  const bodyText = (await page.locator("body").innerText().catch(() => "")) || "";
  if (!/LLM_UNAVAILABLE/i.test(bodyText)) {
    pass("C5_NO_ERROR_IN_TRANSCRIPT", "no LLM_UNAVAILABLE text anywhere on page");
  } else {
    fail("C5_NO_ERROR_IN_TRANSCRIPT", "LLM_UNAVAILABLE text found on page");
  }

  await page.screenshot({ path: `${OUT}/59-f12-ws-drop.png`, fullPage: false }).catch(() => {});
  console.log("screenshot: " + OUT + "/59-f12-ws-drop.png");
  console.log("console errors seen:", consoleErrs.length ? JSON.stringify(consoleErrs.slice(0, 5)) : "none");

  await browser.close();
  printAndExit();
}

function printAndExit() {
  console.log("\n=== E2E VERDICT (F12 WS drop) ===");
  let allPass = true;
  for (const r of results) {
    const tag = r.ok ? "PASS" : "FAIL";
    console.log(`  ${tag}  ${r.id}: ${r.evidence}`);
    if (!r.ok) allPass = false;
  }
  console.log(allPass ? "\nOVERALL: PASS" : "\nOVERALL: FAIL");
  process.exit(allPass ? 0 : 1);
}

main().catch((e) => { console.error("FATAL", e); process.exit(2); });
