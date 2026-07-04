// NO-BEDROCK render diagnostic: sign in, reopen the most recent Case (which has
// published flood/precip layers), capture the session-state loaded_layers the
// CLIENT receives over the WS + whether MapLibre then requests /cog/tiles/.
import { chromium } from "playwright";
const SITE = "https://d125yfbyjrpbre.cloudfront.net/app";
const CF = "d125yfbyjrpbre.cloudfront.net";
const EMAIL = process.env.GRACE2_DEMO_EMAIL, PW = process.env.GRACE2_DEMO_PASSWORD;
const OUT = process.env.DIAG_OUT || "/tmp/aws_render_diag";
const CASE_RE = process.env.DIAG_CASE_RE ? new RegExp(process.env.DIAG_CASE_RE, "i") : null;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const console_errs = [], tileReqs = [];
page.on("console", (m) => { if (m.type() === "error") console_errs.push(m.text().slice(0, 200)); });
page.on("response", (r) => { if (r.url().includes("/cog/tiles/")) tileReqs.push(`${r.status()} ${r.url().slice(0,70)}`); });

// capture session-state loaded_layers from WS frames
const layerSnaps = [];
page.on("websocket", (ws) => {
  ws.on("framereceived", (ev) => {
    const d = typeof ev.payload === "string" ? ev.payload : ev.payload.toString();
    if (d.includes("loaded_layers")) {
      try {
        const j = JSON.parse(d);
        const ll = j?.payload?.loaded_layers ?? j?.loaded_layers;
        if (Array.isArray(ll)) layerSnaps.push(ll.map((l) => ({ id: l.layer_id, t: l.layer_type, uri: (l.uri||"").slice(0,72) })));
      } catch {}
    }
  });
});

await page.goto(SITE, { waitUntil: "networkidle", timeout: 60000 });
await page.waitForTimeout(2000);
await page.getByRole("button", { name: /Sign in|Sign up/i }).first().click().catch(() => {});
await page.waitForTimeout(5000);
const u = page.locator('input[name="username"]:visible, input[type="email"]:visible').first();
await u.waitFor({ timeout: 12000 }); await u.fill(EMAIL);
await page.locator('input[name="password"]:visible, input[type="password"]:visible').first().fill(PW);
await page.locator('input[name="signInSubmitButton"]:visible, input[type="submit"]:visible, button[type="submit"]:visible').first().click().catch(() => {});
for (let i = 0; i < 20; i++) { await page.waitForTimeout(1500); if (page.url().includes(CF) && !/amazoncognito/.test(page.url())) break; }
await page.waitForTimeout(6000);
console.log(`[signin] chatInput=${await page.locator('[data-testid="chat-input"]').count()}`);

// open a Case with a flood layer: click a "SFINCS Pluvial Flood" row by text.
await page.screenshot({ path: `${OUT}_0_root.png` });
let clicked = false;
const caseRes = CASE_RE ? [CASE_RE] : [/First SFINCS Pluvial Flood/i, /SFINCS Pluvial Flood/i, /Pluvial Flood/i, /Check Active Flood/i];
for (const re of caseRes) {
  const item = page.getByText(re).first();
  if (await item.count()) {
    const txt = (await item.innerText().catch(() => "")) || "";
    await item.click().catch(() => {});
    clicked = true; console.log(`[case] clicked '${txt.slice(0,44)}'`);
    break;
  }
}
console.log(`[case] clicked=${clicked}`);
await page.waitForTimeout(12000); // let rehydrate + map add layers + tiles
await page.screenshot({ path: `${OUT}_1_caseopen.png` });
// Camera-only zoom-to-bbox via the app's REAL zoom-to map-command path (does
// NOT fake any layer — the flood layer is already rendered from persisted
// state; this just frames the small flooded area so tiles request at z~10).
if (process.env.DIAG_ZOOM_BBOX) {
  const bbox = JSON.parse(process.env.DIAG_ZOOM_BBOX);
  await page.evaluate((b) => { window.__grace2InjectMapCommand?.({ command: "zoom-to", args: { bbox: b } }); }, bbox);
  await page.waitForTimeout(8000);
  await page.screenshot({ path: `${OUT}_3_zoomed.png` });
} else if (process.env.DIAG_DBLCLICK) {
  // Physically zoom the viewport in onto a screen point (the flood area) by
  // repeated double-clicks — frames a small flood the reopen didn't auto-zoom to.
  const [cx, cy, n] = process.env.DIAG_DBLCLICK.split(",").map(Number);
  for (let i = 0; i < (n || 7); i++) { await page.mouse.dblclick(cx, cy); await page.waitForTimeout(1800); }
  await page.waitForTimeout(5000);
  await page.screenshot({ path: `${OUT}_3_zoomed.png` });
} else {
  // gentle map nudge to force tile requests for the visible viewport
  await page.mouse.move(800, 500); await page.mouse.wheel(0, -200); await page.waitForTimeout(4000);
}
await page.screenshot({ path: `${OUT}_2_afterzoom.png` });

console.log(`[layers] session-state snapshots seen: ${layerSnaps.length}`);
const last = layerSnaps[layerSnaps.length - 1] || [];
console.log(`[layers] last loaded_layers (${last.length}):`);
for (const l of last) console.log(`   - ${l.id} [${l.t}] ${l.uri}`);
console.log(`[tiles] /cog/tiles/ requests: ${tileReqs.length}`);
for (const tr of tileReqs.slice(0, 8)) console.log(`   ${tr}`);
console.log(`[console-errors] ${console_errs.length}`);
for (const e of console_errs.slice(0, 8)) console.log(`   ${e}`);
await browser.close();
