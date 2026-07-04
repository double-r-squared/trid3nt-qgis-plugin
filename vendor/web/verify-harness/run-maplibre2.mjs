import { chromium } from "playwright";
const BASE = "http://127.0.0.1:5192";
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 900, height: 700 } });
const proxyReqs = [];
const proxyResps = [];
page.on("request", (r) => { if (r.url().includes("/qgis-proxy")) proxyReqs.push(r.url()); });
page.on("response", (r) => {
  if (r.url().includes("/qgis-proxy")) proxyResps.push({ status: r.status(), ct: r.headers()["content-type"] });
});
await page.goto(`${BASE}/verify-harness/maplibre-proxy.html`, { waitUntil: "load" });
// QGIS Cloud Run cold tiles take ~25-47s each; wait generously for a few to land.
await page.waitForTimeout(70000);
const info = await page.evaluate(() => ({
  mapLoaded: window.__mapLoaded, tilesDrawn: window.__tilesDrawn, proxyBase: window.__proxyBase,
}));
await page.screenshot({ path: "/tmp/panel-0255-proxyON/maplibre-through-proxy.png" });
console.log(JSON.stringify({
  proxyBase: info.proxyBase, mapLoaded: info.mapLoaded, tilesDrawn: info.tilesDrawn,
  proxyRequestCount: proxyReqs.length,
  proxyResponseCount: proxyResps.length,
  okResponses: proxyResps.filter(r => r.status === 200 && r.ct === "image/png").length,
  sampleResp: proxyResps.slice(0, 3),
}, null, 2));
await browser.close();
