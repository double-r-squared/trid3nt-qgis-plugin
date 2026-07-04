// GRACE-2 web — titiler_colormap unit tests (legend frame-truth fix).
//
// Pure tests for the TiTiler rescale + colormap_name parser and the
// colormap_name -> CSS gradient-stops registry. These back the LayerLegend
// FRAME-TRUTH behavior: the legend reads the rescale/colormap embedded in a
// frame layer's XYZ tile-template URL as the source of truth.

import { describe, it, expect } from "vitest";
import {
  getColormapStops,
  isKnownColormap,
  parseTitilerTileStyle,
} from "./titiler_colormap";

const XYZ_TEMPLATE =
  "https://edge.example/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=s3%3A%2F%2Fb%2Fk.tif";

describe("parseTitilerTileStyle — rescale", () => {
  it("parses rescale=0,3.5 into numeric min/max", () => {
    const r = parseTitilerTileStyle(`${XYZ_TEMPLATE}&rescale=0,3.5&colormap_name=blues`);
    expect(r.rescale).toEqual({ min: 0, max: 3.5 });
  });

  it("parses negative + signed bounds (diverging ramps)", () => {
    const r = parseTitilerTileStyle(`${XYZ_TEMPLATE}&rescale=-25,25&colormap_name=rdbu`);
    expect(r.rescale).toEqual({ min: -25, max: 25 });
  });

  it("handles a percent-encoded comma in rescale", () => {
    const r = parseTitilerTileStyle(`${XYZ_TEMPLATE}&rescale=10%2C250`);
    expect(r.rescale).toEqual({ min: 10, max: 250 });
  });

  it("returns null rescale when the param is absent", () => {
    const r = parseTitilerTileStyle(`${XYZ_TEMPLATE}&colormap_name=blues`);
    expect(r.rescale).toBeNull();
  });

  it("returns null rescale when malformed (single value)", () => {
    expect(parseTitilerTileStyle(`${XYZ_TEMPLATE}&rescale=5`).rescale).toBeNull();
  });

  it("returns null rescale when non-numeric", () => {
    expect(parseTitilerTileStyle(`${XYZ_TEMPLATE}&rescale=foo,bar`).rescale).toBeNull();
  });

  it("returns null rescale when degenerate (min >= max)", () => {
    expect(parseTitilerTileStyle(`${XYZ_TEMPLATE}&rescale=5,5`).rescale).toBeNull();
    expect(parseTitilerTileStyle(`${XYZ_TEMPLATE}&rescale=9,2`).rescale).toBeNull();
  });
});

describe("parseTitilerTileStyle — colormap_name", () => {
  it("parses colormap_name and lowercases it", () => {
    expect(parseTitilerTileStyle(`${XYZ_TEMPLATE}&colormap_name=Blues`).colormapName).toBe(
      "blues",
    );
  });

  it("preserves the _r reverse suffix", () => {
    expect(
      parseTitilerTileStyle(`${XYZ_TEMPLATE}&colormap_name=rdylbu_r`).colormapName,
    ).toBe("rdylbu_r");
  });

  it("returns null colormapName when absent", () => {
    expect(parseTitilerTileStyle(`${XYZ_TEMPLATE}&rescale=0,1`).colormapName).toBeNull();
  });
});

describe("parseTitilerTileStyle — defensive / edge", () => {
  it("never throws and returns nulls for null/empty/garbage input", () => {
    expect(parseTitilerTileStyle(null)).toEqual({ rescale: null, colormapName: null });
    expect(parseTitilerTileStyle(undefined)).toEqual({ rescale: null, colormapName: null });
    expect(parseTitilerTileStyle("")).toEqual({ rescale: null, colormapName: null });
    expect(parseTitilerTileStyle("not a url at all")).toEqual({
      rescale: null,
      colormapName: null,
    });
  });

  it("returns nulls for a gs:// pointer (no query params)", () => {
    expect(parseTitilerTileStyle("gs://grace-2/runs/x/depth.cog.tif")).toEqual({
      rescale: null,
      colormapName: null,
    });
  });

  it("returns nulls for a plain QGIS WMS endpoint", () => {
    const r = parseTitilerTileStyle("https://qgis.example/ows/?SERVICE=WMS&LAYERS=depth");
    expect(r.rescale).toBeNull();
    expect(r.colormapName).toBeNull();
  });

  it("parses both params together off a real-shaped TiTiler template", () => {
    const r = parseTitilerTileStyle(`${XYZ_TEMPLATE}&rescale=250,320&colormap_name=rdylbu_r`);
    expect(r.rescale).toEqual({ min: 250, max: 320 });
    expect(r.colormapName).toBe("rdylbu_r");
  });
});

describe("getColormapStops — registry", () => {
  // Every colormap_name the agent emits (publish_layer.py _TITILER_STYLE_REGISTRY
  // + the family/precip fallbacks) must resolve to stops.
  const EMITTED = [
    "ylgnbu",
    "reds",
    "blues",
    "rdylbu_r",
    "viridis",
    "rdbu",
    "ylgn",
    "gray",
    "gray_r",
  ];

  for (const name of EMITTED) {
    it(`resolves '${name}' to non-empty, sorted [0..1] stops`, () => {
      const stops = getColormapStops(name);
      expect(stops).not.toBeNull();
      expect(stops!.length).toBeGreaterThan(1);
      // First stop at position 0, last at 1, monotonic non-decreasing.
      expect(stops![0]!.position).toBe(0);
      expect(stops![stops!.length - 1]!.position).toBe(1);
      for (let i = 1; i < stops!.length; i++) {
        expect(stops![i]!.position).toBeGreaterThanOrEqual(stops![i - 1]!.position);
      }
    });
  }

  it("reverses the ramp for a _r suffix", () => {
    const fwd = getColormapStops("gray")!;
    const rev = getColormapStops("gray_r")!;
    // Same number of stops, colors reversed (black<->white endpoints swap).
    expect(rev.length).toBe(fwd.length);
    expect(rev[0]!.color).toBe(fwd[fwd.length - 1]!.color);
    expect(rev[rev.length - 1]!.color).toBe(fwd[0]!.color);
  });

  it("returns null for an unknown colormap_name", () => {
    expect(getColormapStops("nonexistent_cmap")).toBeNull();
    expect(getColormapStops("turbo")).toBeNull();
  });

  it("returns null for null/empty input", () => {
    expect(getColormapStops(null)).toBeNull();
    expect(getColormapStops(undefined)).toBeNull();
    expect(getColormapStops("")).toBeNull();
  });
});

describe("isKnownColormap", () => {
  it("is true for known ramps and false otherwise", () => {
    expect(isKnownColormap("blues")).toBe(true);
    expect(isKnownColormap("rdylbu_r")).toBe(true);
    expect(isKnownColormap("turbo")).toBe(false);
    expect(isKnownColormap(null)).toBe(false);
  });
});
