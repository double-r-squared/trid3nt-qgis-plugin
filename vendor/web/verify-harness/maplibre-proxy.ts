// MapLibre harness driving the REAL applyQgisProxy rewrite (job-0255) against a
// live throwaway proxy. VITE_QGIS_PROXY_BASE is set at vite-serve time; the
// production applyQgisProxy() rewrites the QGIS WMS URL through /qgis-proxy so
// MapLibre's tile requests hit the proxy, which streams real QGIS tiles.
import maplibregl from "maplibre-gl";
import { applyQgisProxy } from "../src/Map";

// Reconstruct the basemap WMS base exactly as Map.tsx does, then run it through
// the production applyQgisProxy (which reads import.meta.env.VITE_QGIS_PROXY_BASE).
const DEFAULT_WMS_URL =
  "https://grace-2-qgis-server-425352658356.us-central1.run.app/ogc/wms?MAP=/mnt/qgs/grace2-sample.qgs";
const WMS_BASE_URL = applyQgisProxy(DEFAULT_WMS_URL);
const WMS_TILE_TEMPLATE = `${WMS_BASE_URL}&SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=basemap-osm-conus&CRS=EPSG:3857&FORMAT=image/png&TRANSPARENT=true&BBOX={bbox-epsg-3857}&WIDTH=256&HEIGHT=256&STYLES=`;

(window as unknown as { __tileTemplate: string }).__tileTemplate = WMS_TILE_TEMPLATE;
(window as unknown as { __proxyBase: string | undefined }).__proxyBase =
  import.meta.env.VITE_QGIS_PROXY_BASE as string | undefined;

const map = new maplibregl.Map({
  container: "map",
  style: {
    version: 8,
    sources: {
      "qgis-wms": {
        type: "raster",
        tiles: [WMS_TILE_TEMPLATE],
        tileSize: 256,
      },
    },
    layers: [{ id: "qgis-wms", type: "raster", source: "qgis-wms" }],
  },
  // Fort Myers / Gulf region — matches where QGIS has data.
  center: [-81.87, 26.64],
  zoom: 7,
});

(window as unknown as { __mapLoaded: boolean }).__mapLoaded = false;
map.on("load", () => {
  (window as unknown as { __mapLoaded: boolean }).__mapLoaded = true;
});
(window as unknown as { __tilesDrawn: number }).__tilesDrawn = 0;
map.on("data", (e) => {
  if ((e as { tile?: unknown }).tile) {
    (window as unknown as { __tilesDrawn: number }).__tilesDrawn += 1;
  }
});
