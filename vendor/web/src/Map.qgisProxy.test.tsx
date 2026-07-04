// GRACE-2 web — Map.tsx VITE_QGIS_PROXY_BASE substitution tests (job-0255).
//
// Verifies the env-gated QGIS proxy tile-URL rewrite:
//   - ABSENT (dev/today): applyQgisProxy + buildWmsTileUrl are byte-identical
//     passthroughs — behavior unchanged.
//   - SET (prod): the QGIS host+path is replaced by the proxy base, the
//     original WMS query string preserved.
//
// VITE_QGIS_PROXY_BASE is read at module-eval time, so the "set" branch uses
// vi.stubEnv + a fresh dynamic import (vi.resetModules) to re-evaluate Map.tsx
// with the env present.

import { describe, it, expect, afterEach, vi } from "vitest";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe("applyQgisProxy — env-gated proxy substitution", () => {
  it("is a byte-identical passthrough when VITE_QGIS_PROXY_BASE is absent", async () => {
    // Default import (env not stubbed): no proxy base.
    const { applyQgisProxy, buildWmsTileUrl } = await import("./Map");
    const url =
      "https://qgis.example/ogc/wms?MAP=/mnt/qgs/x.qgs&LAYERS=flood-demo";
    expect(applyQgisProxy(url)).toBe(url);
    // buildWmsTileUrl still produces a URL anchored at the original host.
    expect(buildWmsTileUrl(url)).toContain("https://qgis.example/ogc/wms");
  });

  it("rewrites host+path to the proxy base, preserving the WMS query string", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_QGIS_PROXY_BASE", "https://agent.example/qgis-proxy");
    const { applyQgisProxy } = await import("./Map");
    const url =
      "https://qgis.example/ogc/wms?MAP=/mnt/qgs/x.qgs&LAYERS=flood-demo";
    expect(applyQgisProxy(url)).toBe(
      "https://agent.example/qgis-proxy?MAP=/mnt/qgs/x.qgs&LAYERS=flood-demo",
    );
  });

  it("routes overlay buildWmsTileUrl through the proxy base when set", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_QGIS_PROXY_BASE", "https://agent.example/qgis-proxy");
    const { buildWmsTileUrl } = await import("./Map");
    const url = "https://qgis.example/ogc/wms?MAP=/mnt/qgs/x.qgs&LAYERS=flood";
    const tile = buildWmsTileUrl(url);
    expect(tile).toContain("https://agent.example/qgis-proxy?");
    expect(tile).not.toContain("qgis.example");
    // The per-tile GetMap params are still appended.
    expect(tile).toContain("REQUEST=GetMap");
    expect(tile).toContain("{bbox-epsg-3857}");
  });

  it("handles a base URL with no query string (basemap default)", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_QGIS_PROXY_BASE", "https://agent.example/qgis-proxy");
    const { applyQgisProxy } = await import("./Map");
    expect(applyQgisProxy("https://qgis.example/ogc/wms")).toBe(
      "https://agent.example/qgis-proxy",
    );
  });
});
