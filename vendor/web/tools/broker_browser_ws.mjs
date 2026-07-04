import { chromium } from "@playwright/test";
const ALB = "grace2-agent-broker-872872610.us-west-2.elb.amazonaws.com";
const r = await fetch("https://9ib093sis6.execute-api.us-west-2.amazonaws.com/demo-token", {
  method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ code: "trident-demo-4db31803" }),
});
const { id_token } = await r.json();
const sid = "01CANARY" + Date.now().toString(36).toUpperCase().padStart(18, "0").slice(0,18);
console.log("[browser-ws] token len", id_token.length, "sid", sid);

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
// http origin so ws:// (insecure) to the same host is allowed (no mixed-content block)
await page.goto(`http://${ALB}/healthz`, { waitUntil: "domcontentloaded", timeout: 20000 }).catch((e)=>console.log("goto note:", e.message.slice(0,60)));

const result = await page.evaluate(async ({ alb, sid, token }) => {
  return await new Promise((resolve) => {
    const url = `ws://${alb}/ws?sid=${sid}&st=${encodeURIComponent(token)}`;
    const log = [];
    const t0 = Date.now();
    const el = (e) => Math.round(Date.now() - t0);
    let ws;
    try { ws = new WebSocket(url); } catch (e) { return resolve({ error: "ctor:" + e.message }); }
    log.push(`url_len=${url.length}`);
    ws.onopen = () => { log.push(`open@${el()}ms proto='${ws.protocol}'`);
      // minimal: send an auth-token-ish frame so the agent does not auth-timeout us
      try { ws.send(JSON.stringify({ type: "auth-token", token })); } catch {} };
    ws.onmessage = (m) => { const s = typeof m.data === "string" ? m.data.slice(0,80) : "[binary]"; if (log.length < 30) log.push(`msg@${el()}ms ${s}`); };
    ws.onclose = (c) => { log.push(`CLOSE@${el()}ms code=${c.code} reason='${c.reason}'`); resolve({ held: el() >= 5000, lastMs: el(), closeCode: c.code, log }); };
    ws.onerror = () => log.push(`error@${el()}ms`);
    // hold for 20s then resolve if still open
    setTimeout(() => { const open = ws.readyState === 1; log.push(`held20s open=${open} state=${ws.readyState}`); try { ws.close(); } catch {} resolve({ held: open, lastMs: el(), closeCode: open ? "still-open" : "closed", log }); }, 20000);
  });
}, { alb: ALB, sid, token: id_token });

console.log("[browser-ws] RESULT:", JSON.stringify(result, null, 1));
await browser.close();
