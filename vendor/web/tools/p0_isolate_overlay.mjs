// VISUAL proof: do the published OVERLAY layers actually PAINT, independent of the
// basemap? After layers land, hide BOTH basemap layers (qgis-basemap + osm-fallback)
// via the dev seam and screenshot. If road lines remain -> vector overlay paints.
// Then toggle ONLY the vector off to see if the remaining is raster.
import { chromium } from "playwright";

const SITE = "http://127.0.0.1:5180/app";
const OUT = "/home/nate/Documents/GRACE-2/reports/inflight/p0_isolate";
const BUDGET_MS = 9 * 60 * 1000;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
await page.goto(SITE, { waitUntil: "networkidle", timeout: 60000 });
await page.waitForTimeout(4000);
if (!(await page.locator('[data-testid="chat-input"]').count())) {
  await page.getByRole("button", { name: /continue|skip|anon|explore|got it/i }).first().click().catch(() => {});
  await page.waitForTimeout(3000);
}
const input = page.locator('[data-testid="chat-input"]');
await input.fill("Add the roads in Fort Myers, Florida as a layer.");
await input.press("Enter");
console.log("[prompt] sent");

const start = Date.now();
let panelIds = [];
while (Date.now() - start < BUDGET_MS) {
  await page.waitForTimeout(4000);
  panelIds = await page.evaluate(() => Array.from(document.querySelectorAll("[data-layer-id]")).map((r) => r.getAttribute("data-layer-id")));
  if (panelIds.length >= 1 && Date.now() - start > 15000) { await page.waitForTimeout(10000); break; }
}
console.log(`[layers] ${JSON.stringify(panelIds)}`);
await page.screenshot({ path: `${OUT}_1_all.png` });

// Hide basemap layers -> only overlays remain
const r1 = await page.evaluate(() => {
  const m = window.__grace2GetMap?.();
  if (!m) return "no map";
  for (const id of ["qgis-basemap", "osm-fallback-basemap"]) { if (m.getLayer(id)) m.setLayoutProperty(id, "visibility", "none"); }
  m.triggerRepaint();
  return "basemap hidden; overlays remaining: " + m.getStyle().layers.filter(l => m.getLayoutProperty(l.id, "visibility") !== "none").map(l => l.id).join(",");
});
console.log("[isolate] " + r1);
await page.waitForTimeout(4000);
await page.screenshot({ path: `${OUT}_2_overlays_only.png` });

// Now also hide the vector roads -> only the raster (if it paints) remains
const r2 = await page.evaluate((vid) => {
  const m = window.__grace2GetMap?.();
  if (!m || !m.getLayer(vid)) return "no vector layer";
  m.setLayoutProperty(vid, "visibility", "none");
  m.triggerRepaint();
  return "vector hidden";
}, panelIds.find(id => id.startsWith("osm-roads")) || "osm-roads");
console.log("[isolate] " + r2);
await page.waitForTimeout(4000);
await page.screenshot({ path: `${OUT}_3_raster_only.png` });
console.log(`[saved] ${OUT}_1_all.png ${OUT}_2_overlays_only.png ${OUT}_3_raster_only.png`);
await browser.close();
process.exit(0);
