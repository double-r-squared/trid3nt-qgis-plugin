import { chromium } from "playwright";
const BASE = "http://127.0.0.1:5192";
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 900, height: 700 } });
const proxyReqs = [];
const proxyResps = [];
page.on("request", (r) => {
  const u = r.url();
  if (u.includes("/qgis-proxy")) proxyReqs.push(u);
});
page.on("response", (r) => {
  const u = r.url();
  if (u.includes("/qgis-proxy")) proxyResps.push({ status: r.status(), ct: r.headers()["content-type"], url: u.slice(0, 90) });
});
await page.goto(`${BASE}/verify-harness/maplibre-proxy.html`, { waitUntil: "load" });
// Give MapLibre time to fetch + render tiles through the proxy.
await page.waitForTimeout(7000);
const info = await page.evaluate(() => ({
  proxyBase: window.__proxyBase,
  tileTemplate: window.__tileTemplate,
  mapLoaded: window.__mapLoaded,
  tilesDrawn: window.__tilesDrawn,
}));
await page.screenshot({ path: "/tmp/panel-0255-proxyON/maplibre-through-proxy.png" });
console.log(JSON.stringify({
  proxyBase: info.proxyBase,
  tileTemplate: info.tileTemplate,
  mapLoaded: info.mapLoaded,
  tilesDrawn: info.tilesDrawn,
  proxyRequestCount: proxyReqs.length,
  sampleProxyReq: proxyReqs[0]?.slice(0, 120),
  proxyResponses: proxyResps.slice(0, 5),
}, null, 2));
await browser.close();
