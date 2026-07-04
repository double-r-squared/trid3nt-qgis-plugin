// Playwright + CDP runner: drives the REAL GraceWs open handler in a real
// chromium against the gate-ON agent and captures the literal on-wire frame
// order via Network.webSocketFrameSent. Gemini-free, no inject seams.
import { chromium } from "playwright";

const BASE = process.env.HARNESS_BASE || "http://127.0.0.1:5191";
const OUTDIR = process.env.OUTDIR || "/tmp/panel-0253b-out";
import { mkdirSync } from "node:fs";
mkdirSync(OUTDIR, { recursive: true });

async function runMode(mode) {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const client = await page.context().newCDPSession(page);
  await client.send("Network.enable");

  const sentFrames = [];
  const closes = [];
  client.on("Network.webSocketFrameSent", (p) => {
    try {
      sentFrames.push(JSON.parse(p.response.payloadData).type);
    } catch {
      sentFrames.push("<non-json>");
    }
  });
  client.on("Network.webSocketFrameReceived", (p) => {
    try {
      const obj = JSON.parse(p.response.payloadData);
      sentFrames.push("RECV:" + obj.type);
    } catch {
      /* ignore */
    }
  });
  client.on("Network.webSocketClosed", () => closes.push("closed"));
  // CDP exposes the close code via the frame error event on some builds; also
  // read it from the page-side handler events.

  const url = `${BASE}/verify-harness/wireorder.html?mode=${mode}&url=${encodeURIComponent("ws://127.0.0.1:8905")}`;
  await page.goto(url, { waitUntil: "load" });
  // Let the open handler + handshake + (rejection|ack) settle.
  await page.waitForTimeout(2500);

  const events = await page.evaluate(() => (window).__events);
  const sentTypesInPage = await page.evaluate(() => (window).__sentTypes);
  await page.screenshot({ path: `${OUTDIR}/wireorder-${mode}.png` });
  await browser.close();
  return { mode, sentFrames, sentTypesInPage, events, closes };
}

const results = {};
for (const mode of ["token", "anon"]) {
  results[mode] = await runMode(mode);
}
console.log(JSON.stringify(results, null, 2));
