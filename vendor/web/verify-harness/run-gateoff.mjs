import { chromium } from "playwright";
const BASE = "http://127.0.0.1:5191";
const browser = await chromium.launch();
const page = await browser.newPage();
const client = await page.context().newCDPSession(page);
await client.send("Network.enable");
const frames = [];
client.on("Network.webSocketFrameSent", (p) => { try { frames.push(JSON.parse(p.response.payloadData).type); } catch { frames.push("<nj>"); } });
client.on("Network.webSocketFrameReceived", (p) => { try { frames.push("RECV:" + JSON.parse(p.response.payloadData).type); } catch {} });
// gate-OFF agent on :8907 — anonymous (empty-token) path
await page.goto(`${BASE}/verify-harness/wireorder.html?mode=anon&url=${encodeURIComponent("ws://127.0.0.1:8907")}`, { waitUntil: "load" });
await page.waitForTimeout(2500);
const events = await page.evaluate(() => window.__events);
const sentTypes = await page.evaluate(() => window.__sentTypes);
await page.screenshot({ path: "/tmp/panel-0253b-out/wireorder-gateOFF-anon.png" });
console.log(JSON.stringify({ frames, sentTypes, events }, null, 2));
await browser.close();
