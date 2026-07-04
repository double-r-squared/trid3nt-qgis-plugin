// GRACE-2 web  -  LayerLegend unit tests.
//
// Covers the interactive AOI-snapping legend (NATE overlay-layout spec
// 2026-06-17), built on top of the original content contract:
//   CONTENT (preserved): one colorbar "key" per continuous-raster layer with a
//     known style_preset; title + min/max range labels; hides when nothing
//     eligible.
//   INTERACTION (new): each key is its own card (data-testid
//     "grace2-layer-legend-key"); keys snap COUNTER-CLOCKWISE to the AOI sides
//     (bottom, right, top, left) when an AOI rect (anchor + barWidth) is
//     present; keys are draggable + resizable; compact/flatten + hide toggles.
//
// The wrapper element (data-testid "grace2-layer-legend") is now a full-bleed,
// click-through container; the positioned/sized card is the KEY element.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import {
  LayerLegend,
  LEGEND_Z_INDEX,
  MOBILE_LEGEND_PILL_BOTTOM_CSS,
  MOBILE_LEGEND_PILL_CLEARANCE_PX,
  DESKTOP_LEGEND_PILL_BOTTOM_PX,
  MOBILE_SHEET_DOCK_GAP_PX,
  MOBILE_LEGEND_MAX_WIDTH_CSS,
  MOBILE_LEGEND_VIEWPORT_MARGIN_PX,
  mobileLegendMaxHeightCss,
  sheetTopDockBottomPx,
  legendBandDockBottomPx,
  MOBILE_LEGEND_SCRUBBER_FOOTPRINT_PX,
  LEGEND_BAND_DOCK_GAP_PX,
  MobileLegendToggle,
  legendHasContent,
  LS_DESKTOP_LEGEND_DOCK,
  DESKTOP_DOCK_BOTTOM_SNAP_BAND_PX,
  DESKTOP_DOCK_BBOX_GAP_PX,
  readDesktopDockMode,
  writeDesktopDockMode,
  desktopDockModeForDrop,
} from "./components/LayerLegend";
// MOBILE ONE-ROW BAND DOCK (NATE 2026-06-27) - the legend's new mobile dock math
// composes on top of the SequenceScrubber's chat-clearance gap (20), so the band
// row clears the scrubber. Import the canonical value so the two stay in lockstep.
import { SCRUBBER_SHEET_DOCK_GAP_PX } from "./components/SequenceScrubber";
import { ProjectLayerSummary } from "./contracts";
import { getStylePreset } from "./lib/style-presets";
// ITEM 5 (NATE 2026-06-22)  -  the legend reads the shared AnimationController to
// know whether the SCRUBBER is showing (so it rails to the right of the bbox,
// vertically). Reset the process-global controller before every test so a group
// set by one test never bleeds into another's snap geometry.
import {
  AnimationController,
  setAnimationController,
  getAnimationController,
} from "./lib/animation_controller";

// LANE D (NATE's DECISION): the AOI-snap / drag / resize / scale / CCW legend
// behavior is now MOBILE-ONLY (on desktop the legend is a static bottom-center
// docked strip - see the "desktop docked legend" block at the end). So the snap
// pipeline tests below run in MOBILE mode: stub window.matchMedia so useIsMobile
// reports mobile by default. Desktop-specific tests (the #157 pill block and the
// desktop-dock block) override matchMedia per-test/per-block. Restored each test.
let _matchMediaOriginal: typeof window.matchMedia | undefined;
function stubMatchMedia(mobile: boolean): void {
  window.matchMedia = ((query: string) => ({
    matches: query.includes("max-width") ? mobile : false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}
beforeEach(() => {
  setAnimationController(new AnimationController());
  _matchMediaOriginal = window.matchMedia;
  stubMatchMedia(true); // default: mobile (the snap pipeline is mobile-only now)
});
afterEach(() => {
  window.matchMedia = _matchMediaOriginal as typeof window.matchMedia;
});

function makeLayer(overrides: Partial<ProjectLayerSummary> = {}): ProjectLayerSummary {
  return {
    layer_id: "layer-001",
    name: "Test layer",
    layer_type: "raster",
    uri: "gs://grace-2/runs/test/depth.cog.tif",
    visible: true,
    opacity: 1,
    z_index: 1,
    style_preset: "continuous_flood_depth",
    ...overrides,
  };
}

describe("LayerLegend  -  content contract (preserved)", () => {
  it("renders a key when a raster layer with a known preset is loaded", () => {
    render(<LayerLegend layers={[makeLayer()]} />);
    expect(screen.getByTestId("grace2-layer-legend")).toBeInTheDocument();
    expect(screen.getByTestId("grace2-layer-legend-key")).toBeInTheDocument();
  });

  it("shows the correct title for continuous_flood_depth", () => {
    render(<LayerLegend layers={[makeLayer()]} />);
    expect(screen.getByTestId("layer-legend-title")).toHaveTextContent(
      "Max flood depth (m)",
    );
  });

  it("shows min and max labels", () => {
    render(<LayerLegend layers={[makeLayer()]} />);
    expect(screen.getByTestId("layer-legend-min-label")).toHaveTextContent("0 m");
    expect(screen.getByTestId("layer-legend-max-label")).toHaveTextContent("3.5 m");
  });

  it("renders the gradient bar", () => {
    render(<LayerLegend layers={[makeLayer()]} />);
    expect(screen.getByTestId("layer-legend-bar")).toBeInTheDocument();
  });

  it("hides when no layers are loaded", () => {
    const { container } = render(<LayerLegend layers={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("hides when the raster layer has no style_preset", () => {
    const { container } = render(
      <LayerLegend layers={[makeLayer({ style_preset: null })]} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("hides when the style_preset is unknown", () => {
    const { container } = render(
      <LayerLegend layers={[makeLayer({ style_preset: "unknown_preset_xyz" })]} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("hides when layers contain only vector layers (no raster)", () => {
    const { container } = render(
      <LayerLegend
        layers={[makeLayer({ layer_type: "vector", style_preset: "continuous_flood_depth" })]}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("an anchor does not override the hide-when-no-preset behavior", () => {
    const { container } = render(
      <LayerLegend
        layers={[makeLayer({ style_preset: null })]}
        anchor={{ left: 100, top: 200 }}
        barWidth={200}
      />,
    );
    expect(container.firstChild).toBeNull();
  });
});

describe("LayerLegend  -  one key per eligible raster layer", () => {
  it("renders one key for each continuous-raster layer with a known preset", () => {
    const layers: ProjectLayerSummary[] = [
      makeLayer({ layer_id: "a", style_preset: "continuous_flood_depth", z_index: 3 }),
      makeLayer({ layer_id: "b", style_preset: "continuous_flood_depth", z_index: 2 }),
      makeLayer({ layer_id: "c", style_preset: null, z_index: 1 }),
    ];
    render(<LayerLegend layers={layers} />);
    expect(screen.getAllByTestId("grace2-layer-legend-key")).toHaveLength(2);
  });

  it("skips vector + unknown-preset layers when building keys", () => {
    const layers: ProjectLayerSummary[] = [
      makeLayer({ layer_id: "vec", layer_type: "vector" }),
      makeLayer({ layer_id: "ras", style_preset: "continuous_flood_depth" }),
      makeLayer({ layer_id: "bad", style_preset: "nope_xyz" }),
    ];
    render(<LayerLegend layers={layers} />);
    expect(screen.getAllByTestId("grace2-layer-legend-key")).toHaveLength(1);
  });
});

// Helper: build a sequential frame layer (same pattern as LayerPanel.test.tsx makeFrame).
function makeFrameLayer(hour: number, run = "run-a"): ProjectLayerSummary {
  const hh = String(hour).padStart(2, "0");
  return {
    layer_id: `${run}-f${hh}`,
    name: `HRRR precip F+${hh}h`,
    layer_type: "raster",
    uri: `gs://grace-2/runs/${run}/precip_f${hh}.cog.tif`,
    visible: true,
    opacity: 1,
    z_index: 1,
    style_preset: "continuous_flood_depth",
  };
}

describe("LayerLegend  -  ONE key per sequential group (item 1)", () => {
  it("collapses N frame layers into a single legend key (not N keys)", () => {
    // 3 HRRR forecast frames  -  all same preset, all form a sequential group.
    const layers = [makeFrameLayer(1), makeFrameLayer(3), makeFrameLayer(6)];
    render(<LayerLegend layers={layers} />);
    // Item 1: exactly ONE key for the whole group, not 3.
    expect(screen.getAllByTestId("grace2-layer-legend-key")).toHaveLength(1);
  });

  it("renders a key for the group's representative preset (same gradient)", () => {
    const layers = [makeFrameLayer(1), makeFrameLayer(3), makeFrameLayer(6)];
    render(<LayerLegend layers={layers} />);
    // The single group key still shows the shared preset's label and min/max.
    expect(screen.getByTestId("layer-legend-title")).toHaveTextContent("Max flood depth (m)");
    expect(screen.getByTestId("layer-legend-min-label")).toHaveTextContent("0 m");
    expect(screen.getByTestId("layer-legend-max-label")).toHaveTextContent("3.5 m");
  });

  it("non-grouped layers still get their own key alongside a group key", () => {
    // One sequential group (2 frames) + one unrelated raster = 2 keys total.
    const grouped1 = makeFrameLayer(1);
    const grouped2 = makeFrameLayer(3);
    const standalone = makeLayer({
      layer_id: "surge",
      name: "Storm surge max",
      style_preset: "continuous_flood_depth",
      z_index: 10,
    });
    render(<LayerLegend layers={[standalone, grouped1, grouped2]} />);
    expect(screen.getAllByTestId("grace2-layer-legend-key")).toHaveLength(2);
  });
});

// DATA-DRIVEN LEGEND DEDUP (NATE 2026-06-27: "we surface a key and it gets
// rendered generically"). When the producer emits a real LegendKey on each
// frame of a sequence, the N frames carry the SAME key (same colormap + range),
// so the legend must collapse them to ONE rendered key  -  not N. This guards
// the regression where every animation frame spawned its own legend card.
function makeLegendFrameLayer(
  hour: number,
  legend: ProjectLayerSummary["legend"],
  run = "run-leg",
): ProjectLayerSummary {
  const hh = String(hour).padStart(2, "0");
  return {
    layer_id: `${run}-f${hh}`,
    name: `Flood depth F+${hh}h`,
    layer_type: "raster",
    uri: `gs://grace-2/runs/${run}/depth_f${hh}.cog.tif`,
    visible: true,
    opacity: 1,
    z_index: 1,
    style_preset: "continuous_flood_depth",
    legend,
  };
}

describe("LayerLegend  -  ONE key per data-driven legend series (dedup)", () => {
  const sharedLegend: ProjectLayerSummary["legend"] = {
    kind: "continuous",
    colormap: "reds",
    vmin: 0,
    vmax: 10,
    units: "m",
    label: "Flood depth (m)",
  };

  it("collapses N frames sharing one LegendKey into a single rendered key", () => {
    const layers = [
      makeLegendFrameLayer(1, sharedLegend),
      makeLegendFrameLayer(3, sharedLegend),
      makeLegendFrameLayer(6, sharedLegend),
      makeLegendFrameLayer(12, sharedLegend),
    ];
    render(<LayerLegend layers={layers} />);
    // The whole sequence shares one colormap + range  -  exactly ONE key.
    expect(screen.getAllByTestId("grace2-layer-legend-key")).toHaveLength(1);
  });

  it("renders distinct keys when frames carry genuinely different LegendKeys", () => {
    const depth = { ...sharedLegend };
    const velocity: ProjectLayerSummary["legend"] = {
      kind: "continuous",
      colormap: "viridis",
      vmin: 0,
      vmax: 4,
      units: "m/s",
      label: "Velocity (m/s)",
    };
    const layers = [
      makeLegendFrameLayer(1, depth, "run-depth"),
      makeLegendFrameLayer(3, depth, "run-depth"),
      makeLegendFrameLayer(1, velocity, "run-vel"),
      makeLegendFrameLayer(3, velocity, "run-vel"),
    ];
    render(<LayerLegend layers={layers} />);
    // Two distinct series (different colormap/range/units) = two keys.
    expect(screen.getAllByTestId("grace2-layer-legend-key")).toHaveLength(2);
  });
});

describe("LayerLegend  -  AOI-less fallback placement", () => {
  it("places the key bottom-center when no anchor/barWidth is given", () => {
    render(<LayerLegend layers={[makeLayer()]} />);
    const key = screen.getByTestId("grace2-layer-legend-key");
    // Fallback: left:50% + bottom:24 + a translate (no absolute top).
    expect(key.style.left).toBe("50%");
    expect(key.style.bottom).toBe("24px");
    expect(key.style.transform).toContain("translate");
    expect(key.style.top).toBe("");
  });

  it("falls back to bottom-center when anchor is present but barWidth is null", () => {
    // Snapping needs the full AOI rect (anchor + width); width alone is missing.
    render(<LayerLegend layers={[makeLayer()]} anchor={{ left: 412, top: 300 }} />);
    const key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.style.left).toBe("50%");
    expect(key.style.bottom).toBe("24px");
  });

  it("uses the default 320px key width when no barWidth is provided", () => {
    render(<LayerLegend layers={[makeLayer()]} />);
    expect(screen.getByTestId("grace2-layer-legend-key").style.width).toBe("320px");
  });

  it("clamps the default key width to the AOI on-screen width (barWidth)", () => {
    render(
      <LayerLegend
        layers={[makeLayer()]}
        anchor={{ left: 412, top: 300 }}
        barWidth={248}
      />,
    );
    expect(screen.getByTestId("grace2-layer-legend-key").style.width).toBe("248px");
  });

  it("does not change the value range / tick labels when sized by barWidth", () => {
    render(
      <LayerLegend
        layers={[makeLayer()]}
        anchor={{ left: 412, top: 300 }}
        barWidth={180}
      />,
    );
    expect(screen.getByTestId("layer-legend-min-label")).toHaveTextContent("0 m");
    expect(screen.getByTestId("layer-legend-max-label")).toHaveTextContent("3.5 m");
  });
});

describe("LayerLegend  -  CCW snapping to AOI sides", () => {
  // AOI rect reconstructed from anchor (bottom-edge midpoint) + barWidth.
  const anchor = { left: 500, top: 400 };
  const barWidth = 200;

  function fourKeys(): ProjectLayerSummary[] {
    return [0, 1, 2, 3].map((i) =>
      makeLayer({ layer_id: `k${i}`, z_index: 4 - i }),
    );
  }

  it("assigns sides counter-clockwise: bottom, right, top, left", () => {
    render(
      <LayerLegend layers={fourKeys()} anchor={anchor} barWidth={barWidth} />,
    );
    const keys = screen.getAllByTestId("grace2-layer-legend-key");
    expect(keys.map((k) => k.getAttribute("data-legend-side"))).toEqual([
      "bottom",
      "right",
      "top",
      "left",
    ]);
  });

  it("positions the first (bottom) key below the AOI bottom edge", () => {
    render(
      <LayerLegend layers={[makeLayer({ layer_id: "k0" })]} anchor={anchor} barWidth={barWidth} />,
    );
    const key = screen.getByTestId("grace2-layer-legend-key");
    // Absolute coords (not the 50%/bottom fallback) when an AOI rect exists.
    expect(key.style.left).not.toBe("50%");
    expect(key.style.bottom).toBe("");
    // The bottom key's top is below the AOI bottom edge (400) by the side gap.
    expect(parseFloat(key.style.top)).toBeGreaterThanOrEqual(400);
  });

  it("stacks a 5th key back onto the bottom side without reusing the 1st slot", () => {
    const layers = [0, 1, 2, 3, 4].map((i) => makeLayer({ layer_id: `k${i}` }));
    render(<LayerLegend layers={layers} anchor={anchor} barWidth={barWidth} />);
    const keys = screen.getAllByTestId("grace2-layer-legend-key");
    const sides = keys.map((k) => k.getAttribute("data-legend-side"));
    expect(sides).toEqual(["bottom", "right", "top", "left", "bottom"]);
    // The two bottom keys must not share the same top (they stack).
    const bottomTops = keys
      .filter((k) => k.getAttribute("data-legend-side") === "bottom")
      .map((k) => parseFloat(k.style.top));
    expect(bottomTops[0]).not.toBe(bottomTops[1]);
  });
});

// --- ITEM 5 (NATE 2026-06-22): legend goes vertical on the RIGHT when the
// sequence scrubber is showing (the bottom-center band is occupied by it). ---
describe("LayerLegend  -  scrubber-active right-side vertical rail (ITEM 5)", () => {
  const anchor = { left: 500, top: 400 };
  const barWidth = 200;

  // Build a real sequential group on the shared controller so the legend's
  // useAnimationState() reports an active group (scrubberActive === true).
  function activateScrubber(): void {
    const c = getAnimationController();
    c.setGroups([
      {
        key: "seq-1",
        label: "HRRR precip",
        layerIds: ["f01", "f03", "f06"],
        frameLabels: ["F+01h", "F+03h", "F+06h"],
      },
    ]);
    c.setActiveGroup("seq-1");
  }

  it("with NO scrubber, the first key stays on the BOTTOM (horizontal)", () => {
    render(
      <LayerLegend layers={[makeLayer({ layer_id: "k0" })]} anchor={anchor} barWidth={barWidth} />,
    );
    const key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-side")).toBe("bottom");
    expect(key.getAttribute("data-legend-orientation")).toBe("horizontal");
  });

  it("with the scrubber active, the first key rails on the RIGHT (vertical)", () => {
    activateScrubber();
    // A standalone (non-frame) raster so it still produces ONE legend key while a
    // separate sequence group drives the scrubber-active signal.
    render(
      <LayerLegend
        layers={[makeLayer({ layer_id: "standalone", name: "Storm surge max" })]}
        anchor={anchor}
        barWidth={barWidth}
      />,
    );
    const key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-side")).toBe("right");
    expect(key.getAttribute("data-legend-orientation")).toBe("vertical");
  });

  it("a VERTICAL key renders NARROWER than the horizontal AOI width (item 2)", () => {
    // NATE item 2: a vertical (left/right-docked) key is a tall, NARROW bar, not
    // the full AOI-sized width (which made it nearly square). The horizontal key
    // uses barWidth (200); the vertical key must be substantially narrower.
    activateScrubber();
    render(
      <LayerLegend
        layers={[makeLayer({ layer_id: "standalone", name: "Storm surge max" })]}
        anchor={anchor}
        barWidth={barWidth}
      />,
    );
    const key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-orientation")).toBe("vertical");
    const w = parseFloat(key.style.width);
    // Narrow: well under both the 200px barWidth and the 140px horizontal min.
    expect(w).toBeLessThan(120);
    expect(w).toBeGreaterThan(0);
  });
});

// --- ITEM 3 + ITEM 4 (NATE 2026-06-23): a VERTICAL key rotates its title to
// read vertically (no truncation) and moves the X (hide) to the BOTTOM, inline
// with the colorbar. The horizontal key is unchanged. ----------------------- //
describe("LayerLegend  -  vertical key: rotated title + bottom X (ITEM 3/4)", () => {
  const anchor = { left: 500, top: 400 };
  const barWidth = 200;

  // Drive scrubber-active so the (only) key rails RIGHT -> vertical orientation.
  function activateScrubber(): void {
    const c = getAnimationController();
    c.setGroups([
      {
        key: "seq-v",
        label: "HRRR precip",
        layerIds: ["f01", "f03", "f06"],
        frameLabels: ["F+01h", "F+03h", "F+06h"],
      },
    ]);
    c.setActiveGroup("seq-v");
  }

  it("ITEM 3: the vertical title rotates to read vertically with NO ellipsis truncation", () => {
    activateScrubber();
    render(
      <LayerLegend
        layers={[makeLayer({ layer_id: "standalone", name: "Storm surge max" })]}
        anchor={anchor}
        barWidth={barWidth}
      />,
    );
    const key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-orientation")).toBe("vertical");
    const title = within(key).getByTestId("layer-legend-title");
    // Rotated to read vertically (writing-mode), not laid out horizontally.
    expect(title.style.writingMode).toBe("vertical-rl");
    // NO horizontal-ellipsis clamp (the bug was "Ma..." truncation).
    expect(title.style.whiteSpace).not.toBe("nowrap");
    expect(title.style.textOverflow).not.toBe("ellipsis");
    // The FULL label text is present (not clipped at the DOM level).
    const preset = getStylePreset("continuous_flood_depth");
    expect(title).toHaveTextContent(preset!.label);
  });

  it("ITEM 4: the X (hide) sits at the BOTTOM of the vertical key, inline with the bar", () => {
    activateScrubber();
    render(
      <LayerLegend
        layers={[makeLayer({ layer_id: "standalone", name: "Storm surge max" })]}
        anchor={anchor}
        barWidth={barWidth}
      />,
    );
    const key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-orientation")).toBe("vertical");
    // The X lives INSIDE the value-row column (with the bar + labels), at the
    // bottom - not in a top title row.
    const valueRow = within(key).getByTestId("layer-legend-value-row");
    const hide = within(key).getByTestId("layer-legend-hide");
    expect(valueRow.contains(hide)).toBe(true);
    // It is AFTER the min label in DOM order (bottom of the column). The min
    // label and the X are both children of the value-row column.
    const minLabel = within(key).getByTestId("layer-legend-min-label");
    const children = Array.from(valueRow.children);
    const minIdx = children.findIndex((c) => c.contains(minLabel));
    const hideIdx = children.findIndex((c) => c.contains(hide));
    expect(minIdx).toBeGreaterThanOrEqual(0);
    expect(hideIdx).toBeGreaterThan(minIdx);
  });

  it("the HORIZONTAL key keeps its top title row + X (unchanged)", () => {
    // No scrubber -> bottom/horizontal key. Title is in the top row (not rotated)
    // and the X is in that top row too.
    render(<LayerLegend layers={[makeLayer({ layer_id: "h0" })]} anchor={anchor} barWidth={barWidth} />);
    const key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-orientation")).toBe("horizontal");
    const title = within(key).getByTestId("layer-legend-title");
    // Horizontal title is NOT rotated and keeps its ellipsis clamp.
    expect(title.style.writingMode === "" || title.style.writingMode === "horizontal-tb").toBe(true);
    expect(title.style.whiteSpace).toBe("nowrap");
    // The X is NOT inside the value row for a horizontal key.
    const valueRow = within(key).getByTestId("layer-legend-value-row");
    const hide = within(key).getByTestId("layer-legend-hide");
    expect(valueRow.contains(hide)).toBe(false);
  });
});

describe("LayerLegend  -  snaps to the TRUE projected AOI rect (aoiRect)", () => {
  // A deliberately NON-SQUARE AOI rect: width 400, height 100. If the keys snap
  // off the real rect, the TOP key rails just above top=100; if they fell back to
  // the anchor+width square-ish ESTIMATE (height = width = 400) the top key would
  // be ~300px higher. This is the discriminating geometry for the fix.
  const trueRect = { left: 100, top: 100, right: 500, bottom: 200 };
  // The collapsed anchor+width the old path would have used (bottom midpoint +
  // east-west extent). These describe the SAME bottom edge but carry no height.
  const anchor = { left: 300, top: 200 };
  const barWidth = 400;

  function fourKeys(): ProjectLayerSummary[] {
    return [0, 1, 2, 3].map((i) => makeLayer({ layer_id: `tk${i}`, z_index: 4 - i }));
  }

  it("rails the bottom key just below the true bottom edge (200), not a square estimate", () => {
    render(
      <LayerLegend
        layers={[makeLayer({ layer_id: "tk0" })]}
        aoiRect={trueRect}
        anchor={anchor}
        barWidth={barWidth}
      />,
    );
    const key = screen.getByTestId("grace2-layer-legend-key");
    // Absolute coords (not the 50%/bottom fallback) when a rect is present.
    expect(key.style.left).not.toBe("50%");
    expect(key.style.bottom).toBe("");
    // Bottom side: top = bbox.bottom(200) + SIDE_GAP(10) = 210.
    expect(parseFloat(key.style.top)).toBeCloseTo(210, 0);
  });

  it("rails the top key against the SHORT (height=100) edge, proving the true rect is used", () => {
    render(
      <LayerLegend
        layers={fourKeys()}
        aoiRect={trueRect}
        anchor={anchor}
        barWidth={barWidth}
      />,
    );
    const keys = screen.getAllByTestId("grace2-layer-legend-key");
    const topKey = keys.find((k) => k.getAttribute("data-legend-side") === "top")!;
    // Top side off the TRUE rect: top = bbox.top(100) - SIDE_GAP(10) - keyHeight.
    // (keyHeight is the full ~64px stacking height.) This lands NEAR +26, i.e.
    // close to the real top edge (100). The square ESTIMATE (height=400) would put
    // the top edge at bottom-400 = -200, so the top key would sit far negative
    // (~-274)  -  so a non-negative-ish value here proves the true rect path.
    const top = parseFloat(topKey.style.top);
    expect(top).toBeGreaterThan(0);
    expect(top).toBeLessThan(100);
  });

  it("prefers aoiRect over anchor+width when both are supplied", () => {
    // Same anchor+width, but two DIFFERENT true rects -> the top key must move with
    // the rect height, proving aoiRect (not the collapsed scalars) drives snapping.
    const shortRect = { left: 100, top: 100, right: 500, bottom: 200 }; // h=100
    const tallRect = { left: 100, top: -300, right: 500, bottom: 200 }; // h=500

    const { rerender } = render(
      <LayerLegend
        layers={fourKeys()}
        aoiRect={shortRect}
        anchor={anchor}
        barWidth={barWidth}
      />,
    );
    const topShort = parseFloat(
      screen
        .getAllByTestId("grace2-layer-legend-key")
        .find((k) => k.getAttribute("data-legend-side") === "top")!.style.top,
    );

    rerender(
      <LayerLegend
        layers={fourKeys()}
        aoiRect={tallRect}
        anchor={anchor}
        barWidth={barWidth}
      />,
    );
    const topTall = parseFloat(
      screen
        .getAllByTestId("grace2-layer-legend-key")
        .find((k) => k.getAttribute("data-legend-side") === "top")!.style.top,
    );

    // Taller rect -> top edge is higher (smaller/negative y) -> top key sits higher.
    expect(topTall).toBeLessThan(topShort);
  });

  it("falls back to the anchor+width estimate when aoiRect is absent", () => {
    // No aoiRect -> reconstruct a square-ish rect from anchor + barWidth so the
    // legend still snaps (never silently breaks). Bottom key still rails the
    // exact bottom edge (anchor.top = 200).
    render(
      <LayerLegend
        layers={[makeLayer({ layer_id: "fb0" })]}
        anchor={anchor}
        barWidth={barWidth}
      />,
    );
    const key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.style.left).not.toBe("50%");
    expect(parseFloat(key.style.top)).toBeCloseTo(210, 0);
  });
});

describe("LayerLegend  -  resize", () => {
  it("widens a key when the resize handle is dragged right", () => {
    render(
      <LayerLegend layers={[makeLayer()]} anchor={{ left: 400, top: 300 }} barWidth={200} />,
    );
    const key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.style.width).toBe("200px");
    const handle = within(key).getByTestId("layer-legend-resize");
    fireEvent.pointerDown(handle, { clientX: 100, clientY: 100 });
    fireEvent.pointerMove(window, { clientX: 180, clientY: 100 });
    fireEvent.pointerUp(window);
    // 200 + 80 = 280.
    expect(screen.getByTestId("grace2-layer-legend-key").style.width).toBe("280px");
  });

  it("clamps resize to the min width", () => {
    render(<LayerLegend layers={[makeLayer()]} barWidth={200} />);
    const key = screen.getByTestId("grace2-layer-legend-key");
    const handle = within(key).getByTestId("layer-legend-resize");
    fireEvent.pointerDown(handle, { clientX: 100, clientY: 100 });
    fireEvent.pointerMove(window, { clientX: -500, clientY: 100 });
    fireEvent.pointerUp(window);
    // Clamped to KEY_MIN_WIDTH (140).
    expect(screen.getByTestId("grace2-layer-legend-key").style.width).toBe("140px");
  });
});

describe("LayerLegend  -  hide toggle", () => {
  it("hides the whole legend and shows a re-open pill", () => {
    render(<LayerLegend layers={[makeLayer()]} />);
    fireEvent.click(screen.getByTestId("layer-legend-hide"));
    expect(screen.queryByTestId("grace2-layer-legend-key")).toBeNull();
    expect(screen.getByTestId("grace2-layer-legend-show")).toBeInTheDocument();
  });

  it("the hide control renders an X icon (NOT the old eye emoji) - item 1", () => {
    render(<LayerLegend layers={[makeLayer()]} />);
    const hide = screen.getByTestId("layer-legend-hide");
    // NATE item 1: the hide affordance is now the shared X icon (an <svg>), not
    // the U+1F441 eye emoji. Assert the glyph is an SVG and not the eye codepoint.
    expect(hide.querySelector("svg")).not.toBeNull();
    expect(hide.textContent ?? "").not.toContain("\u{1F441}");
    // aria-label + click behavior are unchanged (covered by the hide tests below).
    expect(hide.getAttribute("aria-label")).toBe("Hide legend");
  });

  it("re-shows the legend when the pill is clicked", () => {
    render(<LayerLegend layers={[makeLayer()]} />);
    fireEvent.click(screen.getByTestId("layer-legend-hide"));
    fireEvent.click(screen.getByTestId("grace2-layer-legend-show"));
    expect(screen.getByTestId("grace2-layer-legend-key")).toBeInTheDocument();
  });

  it("only the first key carries the global hide control", () => {
    const layers = [
      makeLayer({ layer_id: "a" }),
      makeLayer({ layer_id: "b" }),
    ];
    render(<LayerLegend layers={layers} anchor={{ left: 400, top: 300 }} barWidth={200} />);
    // Exactly one hide button across all keys.
    expect(screen.getAllByTestId("layer-legend-hide")).toHaveLength(1);
    // LEGEND v2: there is NO compact/flatten toggle anymore (the key is always
    // the flat two-row card), so no compact-toggle control renders on any key.
    expect(screen.queryAllByTestId("layer-legend-compact-toggle")).toHaveLength(0);
  });
});

// --- JOB WEB-AOI-LEGEND (#157)  -  "Show legend" pill clears the chat composer  //
//
// The collapsed re-open pill must NOT overlap the mobile chat composer (the
// bottom-sheet input form). On mobile it lifts above the composer (safe-area
// inset + clearance); on desktop (no bottom sheet) it keeps the low position.
describe("LayerLegend  -  Show-legend pill position vs mobile composer (#157)", () => {
  /** Mock useIsMobile's media query (max-width:767px) match for one render. */
  function mockIsMobile(mobile: boolean): () => void {
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({
      matches: query.includes("max-width") ? mobile : false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })) as unknown as typeof window.matchMedia;
    return () => {
      window.matchMedia = original;
    };
  }

  it("the mobile pill offset references the safe-area inset + a positive clearance", () => {
    // Source-of-truth: a calc() over the device safe-area inset plus a fixed
    // clearance that lifts the pill clear of the bottom-sheet composer. (jsdom's
    // CSSOM drops calc(env(...)) from an inline `bottom`, so we pin the exported
    // constant directly  -  the same convention Chat's SHEET_BOTTOM_OFFSET_CSS uses.)
    expect(MOBILE_LEGEND_PILL_CLEARANCE_PX).toBeGreaterThan(DESKTOP_LEGEND_PILL_BOTTOM_PX);
    expect(MOBILE_LEGEND_PILL_BOTTOM_CSS).toBe(
      `calc(env(safe-area-inset-bottom) + ${MOBILE_LEGEND_PILL_CLEARANCE_PX}px)`,
    );
    expect(MOBILE_LEGEND_PILL_BOTTOM_CSS).toContain("env(safe-area-inset-bottom)");
  });

  it("DESKTOP no longer has an in-legend hide control or floating pill (LANE D)", () => {
    // LANE D (NATE's DECISION): on desktop the legend is a static bottom-center
    // docked strip with NO in-legend hide control - the Show/Hide toggle moved
    // to BottomRowButtons (next to Settings), and the floating bottom-center
    // pill is gone. So neither the per-key hide button nor the floating
    // "show legend" pill render on desktop.
    const restore = mockIsMobile(false);
    try {
      render(<LayerLegend layers={[makeLayer()]} />);
      expect(screen.queryByTestId("layer-legend-hide")).toBeNull();
      expect(screen.queryByTestId("grace2-layer-legend-show")).toBeNull();
      // The docked strip itself renders (the legend content is still shown).
      expect(screen.getByTestId("grace2-layer-legend")).toHaveAttribute(
        "data-legend-docked",
        "desktop",
      );
    } finally {
      restore();
    }
  });

  it("does NOT use the bare desktop 24px position on MOBILE (would overlap the composer)", () => {
    const restore = mockIsMobile(true);
    try {
      render(<LayerLegend layers={[makeLayer()]} />);
      fireEvent.click(screen.getByTestId("layer-legend-hide"));
      const pill = screen.getByTestId("grace2-layer-legend-show");
      // The mobile branch sets a calc(env(...)) value; jsdom drops it to ""  -  the
      // key invariant is it is NOT the desktop 24px that overlapped the form.
      expect(pill.style.bottom).not.toBe(`${DESKTOP_LEGEND_PILL_BOTTOM_PX}px`);
    } finally {
      restore();
    }
  });
});

// --- DESKTOP SCRUBBER CLEARANCE (NATE 2026-06-27): the desktop docked legend
// strip (LANE D) must LIFT above the sequence scrubber's footprint while the
// scrubber is active so the scrubber (z51) never paints over the strip (z15).
// With the scrubber INACTIVE the strip stays at the bare DESKTOP_DOCK_BOTTOM_PX
// (16). This is the DESKTOP-ONLY (!isMobile) path; the mobile keys reserve the
// bottom band via excludeBottom and are unaffected. ------------------------- //
describe("LayerLegend  -  desktop docked strip clears the active scrubber", () => {
  /** Stub useIsMobile's media query (max-width:767px) match for one render. */
  function mockIsMobile(mobile: boolean): () => void {
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({
      matches: query.includes("max-width") ? mobile : false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })) as unknown as typeof window.matchMedia;
    return () => {
      window.matchMedia = original;
    };
  }

  // Drive the shared AnimationController so the legend's useAnimationState()
  // reports an active group (scrubberActive === true), exactly as the live app.
  function activateScrubber(): void {
    const c = getAnimationController();
    c.setGroups([
      {
        key: "seq-d",
        label: "HRRR precip",
        layerIds: ["f01", "f03", "f06"],
        frameLabels: ["F+01h", "F+03h", "F+06h"],
      },
    ]);
    c.setActiveGroup("seq-d");
  }

  it("with NO scrubber, the desktop strip stays at bottom 16px", () => {
    const restore = mockIsMobile(false);
    try {
      render(<LayerLegend layers={[makeLayer()]} />);
      const strip = screen.getByTestId("grace2-layer-legend");
      expect(strip).toHaveAttribute("data-legend-docked", "desktop");
      expect(strip.style.bottom).toBe("16px");
    } finally {
      restore();
    }
  });

  it("with the scrubber ACTIVE, the desktop strip lifts to bottom 76px (clears the scrubber)", () => {
    const restore = mockIsMobile(false);
    try {
      activateScrubber();
      // A standalone (non-frame) raster so it still emits ONE legend key while a
      // separate sequence group drives the scrubber-active signal.
      render(
        <LayerLegend
          layers={[makeLayer({ layer_id: "standalone", name: "Storm surge max" })]}
        />,
      );
      const strip = screen.getByTestId("grace2-layer-legend");
      expect(strip).toHaveAttribute("data-legend-docked", "desktop");
      // 16 (DESKTOP_DOCK_BOTTOM_PX) + 60 (52 footprint + 8 gap) = 76: the strip's
      // bottom edge sits at the top of the scrubber's reserved band (above its
      // ~66px top), so the z51 scrubber no longer covers the z15 legend.
      expect(strip.style.bottom).toBe("76px");
    } finally {
      restore();
    }
  });

  // ZOOM-OUT HIDE (NATE 2026-06-27, mobile-only): desktop must IGNORE
  // aoiTooSmallToShow entirely - the desktop docked strip renders regardless (it
  // early-returns to the static strip before the mobile hide is read).
  it("DESKTOP IGNORES aoiTooSmallToShow - the docked strip still renders", () => {
    const restore = mockIsMobile(false);
    try {
      render(<LayerLegend layers={[makeLayer()]} aoiTooSmallToShow={true} />);
      const strip = screen.getByTestId("grace2-layer-legend");
      expect(strip).toHaveAttribute("data-legend-docked", "desktop");
      expect(strip.style.bottom).toBe("16px");
    } finally {
      restore();
    }
  });
});

// MOBILE SHEET-TOP DOCK (NATE 2026-06-24) - when App threads the chat sheet's
// top-edge Y (sheetTopPx), the mobile legend (colorbar keys + collapsed pill)
// must dock just ABOVE the sheet top - a clean band at the chat-panel top -
// instead of railing the AOI edges / floating over the map. beforeEach already
// stubs mobile=true; jsdom's window.innerHeight defaults to 768.
describe("LayerLegend  -  docks to the chat sheet top on mobile (sheetTopPx)", () => {
  it("sheetTopDockBottomPx computes viewportH - sheetTopPx + gap", () => {
    // window.innerHeight is 768 under jsdom.
    expect(sheetTopDockBottomPx(500)).toBe(768 - 500 + MOBILE_SHEET_DOCK_GAP_PX);
    expect(MOBILE_SHEET_DOCK_GAP_PX).toBeGreaterThan(0);
  });

  it("SNAPS the colorbar KEYS to the AOI bbox edge when the bbox is on screen (NATE 2026-06-26)", () => {
    // SNAP-TO-BBOX FIX (NATE 2026-06-26): when the AOI bbox IS projected on
    // screen the sheet-top band dock is SUPPRESSED and the keys snap to the REAL
    // bbox edges (railing like desktop), NOT a bottom-center band. The single key
    // lands on the BOTTOM edge: absolute top below the bbox bottom (300), with no
    // `bottom` offset and no left:50% band centering.
    render(
      <LayerLegend
        layers={[makeLayer()]}
        aoiRect={{ left: 100, top: 50, right: 400, bottom: 300 }}
        sheetTopPx={500}
      />,
    );
    const key = screen.getByTestId("grace2-layer-legend-key");
    // AOI-edge absolute position: NOT the band's bottom-from-sheet offset.
    expect(key.style.bottom).toBe("");
    expect(key.style.bottom).not.toBe(`${768 - 500 + MOBILE_SHEET_DOCK_GAP_PX}px`);
    // Absolute left (px), NOT the band's left:50% centering.
    expect(key.style.left).not.toBe("50%");
    // Bottom side rail: top just below the bbox bottom edge (300).
    expect(parseFloat(key.style.top)).toBeGreaterThanOrEqual(300);
  });

  it("docks the colorbar KEYS to the ONE-ROW band ONLY when NO AOI bbox is on screen (fallback)", () => {
    // MOBILE ONE-ROW BAND DOCK (NATE 2026-06-27): no aoiRect -> the single
    // horizontal legend ROW docks just above the chat panel. The ROW container
    // (grace2-layer-legend-band-row) owns the fixed placement; with NO scrubber
    // active the row docks at viewportH - sheetTopPx + scrubberGap (no scrubber
    // footprint reserved). The keys live IN FLOW inside it (position:relative).
    render(<LayerLegend layers={[makeLayer()]} sheetTopPx={500} />);
    const row = screen.getByTestId("grace2-layer-legend-band-row");
    expect(row.style.bottom).toBe(`${768 - 500 + SCRUBBER_SHEET_DOCK_GAP_PX}px`);
    // Centered via left:50% + translateX(-50%) (the band convention).
    expect(row.style.left).toBe("50%");
    // It is one clean horizontal LINE: a flex row, nowrap.
    expect(row.style.display).toBe("flex");
    expect(row.style.flexDirection).toBe("row");
    expect(row.style.flexWrap).toBe("nowrap");
    // The key sits IN FLOW inside the row (no per-card absolute bottom / left:50%).
    const key = within(row).getByTestId("grace2-layer-legend-key");
    expect(key.style.position).toBe("relative");
    expect(key.style.bottom).toBe("");
    expect(key.style.left).toBe("");
  });

  it("docks the collapsed 'Show legend' PILL to the sheet top", () => {
    render(<LayerLegend layers={[makeLayer()]} sheetTopPx={500} />);
    fireEvent.click(screen.getByTestId("layer-legend-hide"));
    const pill = screen.getByTestId("grace2-layer-legend-show");
    expect(pill.style.bottom).toBe(`${768 - 500 + MOBILE_SHEET_DOCK_GAP_PX}px`);
  });

  it("a HIGHER sheet top (expanded sheet) docks the one-row band further up the screen", () => {
    // MOBILE ONE-ROW BAND DOCK (NATE 2026-06-27): no aoiRect -> the band tracks the
    // sheet top via the ROW container's bottom. With NO scrubber active the band
    // bottom = viewportH - sheetTopPx + scrubberGap. With a bbox on screen the keys
    // snap to the bbox edge instead (covered above).
    const { rerender } = render(
      <LayerLegend layers={[makeLayer()]} sheetTopPx={700} />,
    );
    // Collapsed: top edge 700 -> bottom = 768-700+20 = 88.
    expect(
      screen.getByTestId("grace2-layer-legend-band-row").style.bottom,
    ).toBe(`${768 - 700 + SCRUBBER_SHEET_DOCK_GAP_PX}px`);
    // Expanded: top edge rises to 300 -> bottom = 768-300+20 = 488 (higher up).
    rerender(<LayerLegend layers={[makeLayer()]} sheetTopPx={300} />);
    expect(
      screen.getByTestId("grace2-layer-legend-band-row").style.bottom,
    ).toBe(`${768 - 300 + SCRUBBER_SHEET_DOCK_GAP_PX}px`);
  });
});

// MOBILE VIEWPORT CLAMP (NATE 2026-06-24 live-mobile feedback): "when we get the
// legend back it should stay the size of the window and not span past the window
// on mobile." The docked legend band must be CLAMPED to the viewport - a
// viewport-bounded max-width (so a fixed cardWidth wider than a narrow phone
// SHRINKS instead of overflowing), a capped max-height for the band, and
// scroll-within so nothing bleeds past the window edges. Notch insets respected
// via env(). beforeEach already stubs mobile=true; jsdom innerHeight is 768.
describe("LayerLegend  -  mobile legend clamps to the viewport (never spans past the window)", () => {
  it("MOBILE_LEGEND_MAX_WIDTH_CSS is a viewport-bounded calc() over 100dvw minus insets + margin", () => {
    expect(MOBILE_LEGEND_VIEWPORT_MARGIN_PX).toBeGreaterThan(0);
    // The max-width tracks the visual viewport (100dvw), subtracts the left/right
    // safe-area insets (notch) and a side margin on each edge. This guarantees it
    // is NEVER wider than the window.
    expect(MOBILE_LEGEND_MAX_WIDTH_CSS).toContain("100dvw");
    expect(MOBILE_LEGEND_MAX_WIDTH_CSS).toContain("env(safe-area-inset-left)");
    expect(MOBILE_LEGEND_MAX_WIDTH_CSS).toContain("env(safe-area-inset-right)");
    expect(MOBILE_LEGEND_MAX_WIDTH_CSS).toContain(
      `${MOBILE_LEGEND_VIEWPORT_MARGIN_PX * 2}px`,
    );
  });

  it("mobileLegendMaxHeightCss caps the band height below the docked sheet top (respecting the notch)", () => {
    const bottom = sheetTopDockBottomPx(500)!; // 768-500+8 = 276
    const css = mobileLegendMaxHeightCss(bottom);
    // Height = viewport height minus the docked-bottom offset minus the top inset
    // minus a margin, so the card cannot run off the TOP of the window.
    expect(css).toContain("100dvh");
    expect(css).toContain(`${bottom}px`);
    expect(css).toContain("env(safe-area-inset-top)");
    // Null (sheet top unknown) still returns a window-bounded cap (never unbounded).
    const fallback = mobileLegendMaxHeightCss(null);
    expect(fallback).toContain("100dvh");
    expect(fallback).toContain("env(safe-area-inset-top)");
  });

  it("the docked one-row band carries a viewport-bounded max-width (never spans past the window)", () => {
    // MOBILE ONE-ROW BAND DOCK (NATE 2026-06-27): the single horizontal ROW owns
    // the viewport clamp now (the keys live in flow inside it). A wide barWidth
    // can no longer fix the band wider than a phone: the row caps max-width to the
    // window and scrolls horizontally. No aoiRect -> the band path; with a bbox on
    // screen the keys snap to the bbox edge with no band clamp (NATE 2026-06-26).
    render(
      <LayerLegend
        layers={[makeLayer()]}
        barWidth={520}
        sheetTopPx={500}
      />,
    );
    const row = screen.getByTestId("grace2-layer-legend-band-row");
    // The ROW carries the viewport-bounded max-width clamp so it can never exceed
    // the window, regardless of any key's intrinsic width.
    expect(row.style.maxWidth).toBe(MOBILE_LEGEND_MAX_WIDTH_CSS);
    // And it scrolls horizontally within the clamp rather than spilling off-screen.
    expect(row.style.overflowX).toBe("auto");
    expect(row.style.boxSizing).toBe("border-box");
    // Still centered (left:50% + translateX(-50%)) so the clamped row never runs
    // off the left/right edge.
    expect(row.style.left).toBe("50%");
    expect(row.style.transform).toContain("translate");
  });

  it("the docked one-row band carries a viewport-bounded max-height (cannot run off the top)", () => {
    // MOBILE ONE-ROW BAND DOCK (NATE 2026-06-27): the ROW carries the max-height
    // clamp (capped to the band above the dock) so a tall key can never run off the
    // top of the window. No aoiRect -> the band path; with a bbox on screen the keys
    // snap to the bbox edge, no clamp (NATE 2026-06-26).
    render(<LayerLegend layers={[makeLayer()]} sheetTopPx={500} />);
    const row = screen.getByTestId("grace2-layer-legend-band-row");
    // The band bottom (no scrubber active) = viewportH - sheetTopPx + scrubberGap.
    const bottom = legendBandDockBottomPx(500, false)!;
    expect(row.style.maxHeight).toBe(mobileLegendMaxHeightCss(bottom));
  });

  it("does NOT apply the viewport clamp when NOT docked to the sheet top (AOI-snap path unchanged)", () => {
    // No sheetTopPx -> the legacy AOI-snap mobile path. It must keep its absolute
    // sizing (no band clamp injected), so the AOI-edge rail behavior is untouched.
    render(
      <LayerLegend
        layers={[makeLayer()]}
        aoiRect={{ left: 100, top: 100, right: 500, bottom: 200 }}
        barWidth={200}
      />,
    );
    const key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.style.maxWidth).toBe("");
    expect(key.style.maxHeight).toBe("");
    expect(key.style.overflowX).toBe("");
  });

  it("the docked 'Show legend' pill is also viewport-clamped on mobile", () => {
    render(<LayerLegend layers={[makeLayer()]} sheetTopPx={500} />);
    fireEvent.click(screen.getByTestId("layer-legend-hide"));
    const pill = screen.getByTestId("grace2-layer-legend-show");
    expect(pill.style.maxWidth).toBe(MOBILE_LEGEND_MAX_WIDTH_CSS);
  });
});

// MOBILE ONE-ROW BAND DOCK (NATE 2026-06-27) - the mobile legend is a single
// horizontal LINE (one row) docked ABOVE the scrubber (order chat -> scrubber ->
// legend). It pins to a map corner via AOI-snap when the AOI is usefully on screen
// (aoiCornerPlaceable=true), and SNAPS to the above-chat one-row band when the
// corner attach is no longer useful (aoiCornerPlaceable=false: zoomed too far
// in/out / AOI off-screen / tiny dot / fills the viewport). beforeEach stubs
// mobile=true; jsdom innerHeight is 768. These are MOBILE-ONLY; desktop early-
// returns to the static docked strip (covered in the LANE D block, unchanged).
describe("LayerLegend  -  mobile one-row band dock above the scrubber (NATE 2026-06-27)", () => {
  // Drive the shared AnimationController so scrubberActive === true (the legend
  // reserves the scrubber footprint above the chat-clearance gap), as the live app.
  function activateScrubber(): void {
    const c = getAnimationController();
    c.setGroups([
      {
        key: "seq-band",
        label: "HRRR precip",
        layerIds: ["f01", "f03", "f06"],
        frameLabels: ["F+01h", "F+03h", "F+06h"],
      },
    ]);
    c.setActiveGroup("seq-band");
  }

  // --- TASK 1: ONE HORIZONTAL ROW ----------------------------------------- //
  it("renders the keys inside ONE horizontal flex ROW (one clean line)", () => {
    // No AOI -> band dock. Two distinct preset-only rasters -> two keys; they must
    // both sit inside the single flex-row container as one line (not a column).
    render(
      <LayerLegend
        layers={[
          makeLayer({ layer_id: "a", name: "Storm surge max" }),
          makeLayer({ layer_id: "b", name: "Flow velocity" }),
        ]}
        sheetTopPx={500}
      />,
    );
    const row = screen.getByTestId("grace2-layer-legend-band-row");
    // One clean horizontal line: a flex row, nowrap.
    expect(row.style.display).toBe("flex");
    expect(row.style.flexDirection).toBe("row");
    expect(row.style.flexWrap).toBe("nowrap");
    // BOTH keys live inside the SAME row container (not stacked in separate cards).
    const keysInRow = within(row).getAllByTestId("grace2-layer-legend-key");
    expect(keysInRow).toHaveLength(2);
    // Each key is a compact HORIZONTAL card sitting in flow (no per-card absolute
    // bottom / left:50% band centering - the ROW owns the placement).
    for (const key of keysInRow) {
      expect(key.getAttribute("data-legend-orientation")).toBe("horizontal");
      expect(key.style.position).toBe("relative");
      expect(key.style.flexShrink).toBe("0");
      expect(key.style.bottom).toBe("");
      expect(key.style.left).toBe("");
    }
  });

  it("the row scrolls horizontally and is viewport-clamped so it never spans past the window", () => {
    render(<LayerLegend layers={[makeLayer()]} sheetTopPx={500} />);
    const row = screen.getByTestId("grace2-layer-legend-band-row");
    expect(row.style.overflowX).toBe("auto");
    expect(row.style.maxWidth).toBe(MOBILE_LEGEND_MAX_WIDTH_CSS);
    expect(row.style.boxSizing).toBe("border-box");
  });

  // --- TASK 2: DOCK ABOVE THE SCRUBBER (footprint reserve) ----------------- //
  it("legendBandDockBottomPx reserves the scrubber footprint (+52) above the chat-clearance gap when active", () => {
    const sheetTopPx = 500;
    // Scrubber's docked top = viewportH - sheetTopPx + scrubberGap (20).
    const scrubberTop = 768 - sheetTopPx + SCRUBBER_SHEET_DOCK_GAP_PX;
    // With the scrubber ACTIVE the legend lifts the full footprint (52) + a small
    // legend gap above the scrubber top, so the order is chat -> scrubber -> legend.
    expect(legendBandDockBottomPx(sheetTopPx, true)).toBe(
      scrubberTop + MOBILE_LEGEND_SCRUBBER_FOOTPRINT_PX + LEGEND_BAND_DOCK_GAP_PX,
    );
    // The reserve is exactly the scrubber footprint (52) + the legend gap.
    expect(MOBILE_LEGEND_SCRUBBER_FOOTPRINT_PX).toBe(52);
    // With NO scrubber the legend docks straight above the chat sheet (gap only).
    expect(legendBandDockBottomPx(sheetTopPx, false)).toBe(scrubberTop);
    // Active dock is strictly HIGHER than inactive (clears the scrubber band).
    expect(legendBandDockBottomPx(sheetTopPx, true)).toBeGreaterThan(
      legendBandDockBottomPx(sheetTopPx, false)!,
    );
  });

  it("with the scrubber ACTIVE, the band row's bottom includes the +52 scrubber reserve", () => {
    activateScrubber();
    // A standalone (non-frame) raster so it still emits ONE legend key while a
    // separate sequence group drives the scrubber-active signal. No AOI -> band.
    render(
      <LayerLegend
        layers={[makeLayer({ layer_id: "standalone", name: "Storm surge max" })]}
        sheetTopPx={500}
      />,
    );
    const row = screen.getByTestId("grace2-layer-legend-band-row");
    const scrubberTop = 768 - 500 + SCRUBBER_SHEET_DOCK_GAP_PX;
    // The row sits a full scrubber footprint (52) + legend gap above the scrubber.
    expect(row.style.bottom).toBe(
      `${scrubberTop + MOBILE_LEGEND_SCRUBBER_FOOTPRINT_PX + LEGEND_BAND_DOCK_GAP_PX}px`,
    );
  });

  // --- TASK 3: ZOOM-KEYED SWITCH (aoiCornerPlaceable) ---------------------- //
  it("aoiCornerPlaceable=FALSE forces the above-chat one-row dock EVEN WITH an AOI on screen", () => {
    // An AOI rect IS projected, but the corner attach is no longer useful (zoomed
    // too far in/out). The legend must DOCK the one-row band above the chat panel,
    // NOT rail the AOI edges.
    render(
      <LayerLegend
        layers={[makeLayer()]}
        aoiRect={{ left: 100, top: 50, right: 400, bottom: 300 }}
        sheetTopPx={500}
        aoiCornerPlaceable={false}
      />,
    );
    // The one-row band container is present (the band-dock path fired).
    const row = screen.getByTestId("grace2-layer-legend-band-row");
    expect(row.style.bottom).toBe(`${768 - 500 + SCRUBBER_SHEET_DOCK_GAP_PX}px`);
    // The key is IN the row, horizontal, in flow - NOT AOI-edge absolute-positioned.
    const key = within(row).getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-orientation")).toBe("horizontal");
    expect(key.getAttribute("data-legend-side")).toBe("bottom");
    expect(key.style.position).toBe("relative");
    // It did NOT rail the bbox bottom edge (would be an absolute top >= 300).
    expect(key.style.top).toBe("");
  });

  // --- TASK 4: aoiCornerPlaceable=TRUE keeps the corner attach -------------- //
  it("aoiCornerPlaceable=TRUE + AOI on screen KEEPS the AOI corner-attach (no band)", () => {
    // The AOI is usefully on screen for a corner attach (the NORMAL case): keep the
    // existing AOI-snap rail, do NOT dock the one-row band.
    render(
      <LayerLegend
        layers={[makeLayer()]}
        aoiRect={{ left: 100, top: 50, right: 400, bottom: 300 }}
        sheetTopPx={500}
        aoiCornerPlaceable={true}
      />,
    );
    // No one-row band container (the corner-attach path held).
    expect(screen.queryByTestId("grace2-layer-legend-band-row")).toBeNull();
    const key = screen.getByTestId("grace2-layer-legend-key");
    // AOI-edge absolute rail: a real top below the bbox bottom (300), NOT a band.
    expect(key.style.left).not.toBe("50%");
    expect(key.style.bottom).toBe("");
    expect(parseFloat(key.style.top)).toBeGreaterThanOrEqual(300);
  });

  it("an absent aoiCornerPlaceable prop defaults to corner-attach (AOI on screen, no band)", () => {
    // Default true: an absent prop preserves the prior corner-attach behavior.
    render(
      <LayerLegend
        layers={[makeLayer()]}
        aoiRect={{ left: 100, top: 50, right: 400, bottom: 300 }}
        sheetTopPx={500}
      />,
    );
    expect(screen.queryByTestId("grace2-layer-legend-band-row")).toBeNull();
    const key = screen.getByTestId("grace2-layer-legend-key");
    expect(parseFloat(key.style.top)).toBeGreaterThanOrEqual(300);
  });

  it("the collapsed 'Show legend' pill still docks above the chat sheet (unchanged) when band-dock would apply", () => {
    // TASK 4: the collapsed pill behavior is preserved - it docks to the sheet top
    // exactly as before; only the EXPANDED legend follows the one-row dock.
    render(<LayerLegend layers={[makeLayer()]} sheetTopPx={500} aoiCornerPlaceable={false} />);
    fireEvent.click(screen.getByTestId("layer-legend-hide"));
    const pill = screen.getByTestId("grace2-layer-legend-show");
    expect(pill.style.bottom).toBe(`${768 - 500 + MOBILE_SHEET_DOCK_GAP_PX}px`);
    // No one-row band while hidden.
    expect(screen.queryByTestId("grace2-layer-legend-band-row")).toBeNull();
  });
});

// BAND-vs-EDGE GATE + SCRUBBER-WIDTH BAND (NATE 2026-06-28): on mobile, when the
// AOI bbox IS on screen but an AOI-edge-snapped key would INTERSECT the bottom HUD
// (the chat panel, plus the scrubber above it when active), the legend must NOT
// disappear behind the chat - it switches to the BAND form docked above the
// scrubber ("snap to above the scrubber if it is intersecting the chat panel").
// The band must be the SAME WIDTH AS THE SCRUBBER and NOT rescale with the bbox,
// and its title must stay on ONE line (truncated, never new-lining). beforeEach
// stubs mobile=true; jsdom innerWidth is 1024, innerHeight 768. Desktop early-
// returns to the static docked strip (LANE D block, byte-for-byte unchanged).
describe("LayerLegend  -  AOI-snap-overlaps-chat -> band form (NATE 2026-06-28)", () => {
  // An AOI whose BOTTOM edge sits deep down the screen: the single bottom-snapped
  // key (top = bottom + SIDE_GAP_PX(10), height KEY_HEIGHT_FLAT(56)) lands at
  // ~bottom+66, which is below sheetTopPx=500 -> overlaps the chat HUD.
  const overlappingTall = { left: 100, top: 50, right: 400, bottom: 700 };
  // An AOI whose bottom edge sits high up: the same snapped key clears the HUD.
  const clearingShort = { left: 100, top: 50, right: 400, bottom: 300 };

  // --- (a) overlap -> BAND row (not the absolute AOI-snapped key) ------------ //
  it("renders the BAND row (not the absolute AOI-snap) when a snapped key overlaps the chat HUD", () => {
    render(
      <LayerLegend
        layers={[makeLayer()]}
        aoiRect={overlappingTall}
        sheetTopPx={500}
        // Corner attach IS nominally useful; the OVERLAP is what forces the band.
        aoiCornerPlaceable={true}
      />,
    );
    // The one-row band container is present (the overlap forced the band form).
    const row = screen.getByTestId("grace2-layer-legend-band-row");
    // Docked above the chat sheet (no scrubber active -> gap only).
    expect(row.style.bottom).toBe(`${768 - 500 + SCRUBBER_SHEET_DOCK_GAP_PX}px`);
    // The key is IN the row, horizontal, in flow - NOT an absolute AOI-edge rail.
    const key = within(row).getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-orientation")).toBe("horizontal");
    expect(key.getAttribute("data-legend-side")).toBe("bottom");
    expect(key.style.position).toBe("relative");
    // It did NOT rail the bbox bottom edge (would be an absolute top below 700).
    expect(key.style.top).toBe("");
    expect(key.style.bottom).toBe("");
  });

  it("KEEPS the AOI corner-attach (no band) when the snapped key CLEARS the chat HUD", () => {
    // The bbox bottom is high enough that the bottom-snapped key clears sheetTopPx,
    // so there is NO overlap -> the edge form holds (no band row).
    render(
      <LayerLegend
        layers={[makeLayer()]}
        aoiRect={clearingShort}
        sheetTopPx={500}
        aoiCornerPlaceable={true}
      />,
    );
    expect(screen.queryByTestId("grace2-layer-legend-band-row")).toBeNull();
    const key = screen.getByTestId("grace2-layer-legend-key");
    // An absolute AOI-edge rail below the bbox bottom (300), NOT a band.
    expect(key.style.left).not.toBe("50%");
    expect(parseFloat(key.style.top)).toBeGreaterThanOrEqual(300);
  });

  // --- (b) band width = FULL chat-panel width + does NOT change with bbox scale - //
  it("the band row spans the FULL chat-panel width and does NOT rescale when the bbox scale changes", () => {
    // NATE 2026-06-28: the band legend spans the ENTIRE chat-panel width (the
    // mobile sheet is width:100%), = innerWidth - 2 * MOBILE_LEGEND_VIEWPORT_MARGIN_PX.
    // jsdom innerWidth is 1024 -> 1024 - 16 = 1008. It must NOT track the bbox scale.
    const expectedWidth = window.innerWidth - 2 * MOBILE_LEGEND_VIEWPORT_MARGIN_PX;
    expect(expectedWidth).toBe(1008);

    // SMALL overlapping bbox -> aoiScaleFactor clamps LOW (0.6).
    const small = { left: 100, top: 600, right: 250, bottom: 780 };
    // LARGE overlapping bbox -> aoiScaleFactor clamps HIGH (1.6). Different scale,
    // SAME band width (the band must NOT track the bbox).
    const large = { left: 0, top: 0, right: 700, bottom: 780 };

    const { rerender } = render(
      <LayerLegend layers={[makeLayer()]} aoiRect={small} sheetTopPx={500} />,
    );
    const rowSmall = screen.getByTestId("grace2-layer-legend-band-row");
    expect(rowSmall.style.width).toBe(`${expectedWidth}px`);
    const keySmall = within(rowSmall).getByTestId("grace2-layer-legend-key");
    expect(keySmall.style.width).toBe(`${expectedWidth}px`);

    rerender(<LayerLegend layers={[makeLayer()]} aoiRect={large} sheetTopPx={500} />);
    const rowLarge = screen.getByTestId("grace2-layer-legend-band-row");
    // Same scrubber width despite the much larger bbox (no aoiScaleFactor applied).
    expect(rowLarge.style.width).toBe(`${expectedWidth}px`);
    const keyLarge = within(rowLarge).getByTestId("grace2-layer-legend-key");
    expect(keyLarge.style.width).toBe(`${expectedWidth}px`);
  });

  // --- (c) band title is ONE line (nowrap + ellipsis) ----------------------- //
  it("the band-form title is a single non-wrapping line (nowrap + ellipsis)", () => {
    render(
      <LayerLegend
        layers={[makeLayer({ name: "A very long flood depth legend title that must truncate" })]}
        aoiRect={overlappingTall}
        sheetTopPx={500}
      />,
    );
    const row = screen.getByTestId("grace2-layer-legend-band-row");
    const title = within(row).getByTestId("layer-legend-title");
    // ONE line: never wrap; truncate with an ellipsis; shrink within the flex row.
    expect(title.style.whiteSpace).toBe("nowrap");
    expect(title.style.textOverflow).toBe("ellipsis");
    expect(title.style.overflow).toBe("hidden");
    // happy-dom stores a unitless 0 as "0" (mirrors the desktop-bar minWidth test).
    expect(title.style.minWidth).toBe("0");
  });

  // --- (d) ISSUE 2: the band card FITS the row width (no horizontal overflow) - //
  it("the band card uses border-box so its content fits within bandWidthPx (max label not clipped)", () => {
    // NATE 2026-06-28 ISSUE 2: the live bug showed a magma bar with the MAX label
    // cut off the right edge. The band card sets width = bandWidthPx (the full
    // chat-panel width); WITHOUT box-sizing:border-box the 10px horizontal padding
    // + 1px border each side ADD ~22px so the card RENDERS WIDER than the row and
    // the max label spills past the right edge. border-box folds the padding+border
    // INTO bandWidthPx so the card (incl. the value row's max label) fits exactly.
    const expectedWidth = window.innerWidth - 2 * MOBILE_LEGEND_VIEWPORT_MARGIN_PX;
    render(
      <LayerLegend
        layers={[makeLayer({ name: "Storm surge depth (NAVD88)" })]}
        aoiRect={overlappingTall}
        sheetTopPx={500}
      />,
    );
    const row = screen.getByTestId("grace2-layer-legend-band-row");
    const key = within(row).getByTestId("grace2-layer-legend-key");
    // The card's BORDER-BOX width == bandWidthPx, so padding+border are folded in
    // and the rendered card never exceeds the row's content width.
    expect(key.style.boxSizing).toBe("border-box");
    expect(key.style.width).toBe(`${expectedWidth}px`);
    // The maxWidth cap is the belt-and-braces window guard.
    expect(key.style.maxWidth).toBe(MOBILE_LEGEND_MAX_WIDTH_CSS);
    // The horizontal value row: the gradient BAR flexes (flex:1, minWidth:0) to
    // absorb slack, and the min/max labels are nowrap + flexShrink:0, so the MAX
    // label sits at the bar's right end and is never pushed off / clipped.
    const bar = within(key).getByTestId("layer-legend-bar");
    const maxLabel = within(key).getByTestId("layer-legend-max-label");
    // happy-dom expands the `flex:1` shorthand to its longhand "1 1 0%".
    expect(bar.style.flexGrow).toBe("1");
    expect(bar.style.minWidth).toBe("0");
    expect(maxLabel.style.whiteSpace).toBe("nowrap");
    expect(maxLabel.style.flexShrink).toBe("0");
    // The band ROW itself is also border-box at bandWidthPx, so card+row agree.
    expect(row.style.boxSizing).toBe("border-box");
    expect(row.style.width).toBe(`${expectedWidth}px`);
  });

  it("with the scrubber ACTIVE, an overlap-forced band still clears the scrubber footprint", () => {
    // Drive the shared AnimationController so scrubberActive === true.
    const c = getAnimationController();
    c.setGroups([
      {
        key: "seq-overlap",
        label: "HRRR precip",
        layerIds: ["f01", "f03", "f06"],
        frameLabels: ["F+01h", "F+03h", "F+06h"],
      },
    ]);
    c.setActiveGroup("seq-overlap");
    // A standalone raster still emits ONE legend key; the bbox bottom is deep so the
    // (scrubber-offset) snapped key overlaps the chat+scrubber HUD -> band form.
    render(
      <LayerLegend
        layers={[makeLayer({ layer_id: "standalone", name: "Storm surge max" })]}
        aoiRect={overlappingTall}
        sheetTopPx={500}
        aoiCornerPlaceable={true}
      />,
    );
    const row = screen.getByTestId("grace2-layer-legend-band-row");
    const scrubberTop = 768 - 500 + SCRUBBER_SHEET_DOCK_GAP_PX;
    // The band sits a full scrubber footprint (52) + legend gap above the scrubber.
    expect(row.style.bottom).toBe(
      `${scrubberTop + MOBILE_LEGEND_SCRUBBER_FOOTPRINT_PX + LEGEND_BAND_DOCK_GAP_PX}px`,
    );
  });
});

// ISSUE 3 (NATE 2026-06-28): when a key is AOI-snapped to the LEFT edge, the legend
// must sit fully OUTSIDE the bbox (right edge at aoi.left - gap), NOT encroach
// inside. legend_snap places a left key at `left = aoi.left - crossOffset -
// size.width`, so its right edge = left + size.width = aoi.left - crossOffset. This
// is correct ONLY if the value in `sizes` (size.width) EQUALS the actual RENDERED
// card width for a vertical key. The fix (the `sizes` useMemo) mirrors the render's
// cardWidth = Math.round(VERTICAL_KEY_WIDTH * scale) for a vertical non-categorical
// key, so the reserved width matches what paints and the right edge lands OUTSIDE.
describe("LayerLegend  -  LEFT-snap sits OUTSIDE the bbox (ISSUE 3)", () => {
  it("a LEFT-snapped vertical key's rendered right edge (left+width) <= aoi.left (outside, with gap)", () => {
    // A wide, short AOI so a drag to the LEFT edge snaps left (vertical), and so
    // the bbox is large enough that aoiScaleFactor clamps high (a real left edge to
    // sit outside of). No sheetTopPx -> bandDockActive is false -> the AOI edge-snap
    // path renders (not the band), exercising the real left-snap geometry.
    const rect = { left: 300, top: 100, right: 700, bottom: 300 };
    render(<LayerLegend layers={[makeLayer({ layer_id: "L0" })]} aoiRect={rect} />);
    const key0 = screen.getByTestId("grace2-layer-legend-key");
    // Drag the card and release near the LEFT edge so nearestSide -> "left".
    fireEvent.pointerDown(key0, { clientX: 0, clientY: 0 });
    fireEvent.pointerMove(window, { clientX: 305, clientY: 200 }); // near left edge
    fireEvent.pointerUp(window);
    const key = screen.getByTestId("grace2-layer-legend-key");
    // It snapped LEFT and went vertical.
    expect(key.getAttribute("data-legend-side")).toBe("left");
    expect(key.getAttribute("data-legend-orientation")).toBe("vertical");
    // The rendered card's absolute left + its rendered width = its RIGHT edge.
    const left = parseFloat(key.style.left);
    const width = parseFloat(key.style.width);
    expect(Number.isFinite(left)).toBe(true);
    expect(Number.isFinite(width)).toBe(true);
    const rightEdge = left + width;
    // OUTSIDE the bbox: the right edge sits at aoi.left - gap (<= aoi.left), so the
    // card never encroaches inside the bbox (NATE's "encroaching inside" bug).
    expect(rightEdge).toBeLessThanOrEqual(rect.left);
    // And it is the reserved gap OUTSIDE, not flush against the edge.
    expect(rightEdge).toBeLessThan(rect.left);
  });
});

// ISSUE 4 (NATE 2026-06-28): a legend that would overlay the chat must re-dock to
// the BAND even when there is NO scrubber (e.g. OpenQuake's static PGA raster: no
// animation -> scrubberActive=false). `aoiSnapOverlapsHud` uses hudTop = sheetTopPx
// (footprint 0 with no scrubber); when it triggers, bandDockActive becomes true and
// the band docks just above the chat top (bandRowBottomPx). beforeEach stubs
// mobile=true; the AnimationController is reset (no active group) so scrubberActive
// is FALSE - the static-raster case.
describe("LayerLegend  -  overlap re-docks to band with NO scrubber (ISSUE 4)", () => {
  it("scrubber INACTIVE + a tall AOI whose snap dips below sheetTopPx -> band row, docked above the chat", () => {
    // No setGroups/setActiveGroup -> scrubberActive === false (static PGA raster).
    // A tall AOI: the bottom-snapped key (top = bottom + SIDE_GAP_PX(10), height
    // KEY_HEIGHT_FLAT(56)) lands ~bottom+66 = 766, well below sheetTopPx=500, so it
    // overlaps the chat HUD and must re-dock to the band.
    const tall = { left: 100, top: 50, right: 400, bottom: 700 };
    render(
      <LayerLegend
        layers={[makeLayer({ layer_id: "pga", name: "Peak ground acceleration" })]}
        aoiRect={tall}
        sheetTopPx={500}
        aoiCornerPlaceable={true}
      />,
    );
    // The BAND row is present (not the absolute AOI-snap key) - the overlap forced
    // the band even though there is NO scrubber.
    const row = screen.getByTestId("grace2-layer-legend-band-row");
    // Docked just above the chat top: with no scrubber active the band reserves NO
    // scrubber footprint, so bottom = viewportH - sheetTopPx + scrubber gap.
    expect(row.style.bottom).toBe(`${768 - 500 + SCRUBBER_SHEET_DOCK_GAP_PX}px`);
    // The key is IN the row, horizontal + in flow (not an absolute AOI-edge rail).
    const key = within(row).getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-orientation")).toBe("horizontal");
    expect(key.style.position).toBe("relative");
    expect(key.style.top).toBe("");
  });
});

// ZOOM-OUT HIDE (NATE 2026-06-27, MOBILE-ONLY): when the AOI bbox has zoomed OUT
// to a tiny dot on screen, Map.tsx threads `aoiTooSmallToShow` and the MOBILE
// legend HIDES entirely (renders null) - the speck carries no useful colorbar
// context. This takes PRECEDENCE over the AOI-snap / band-dock decision. beforeEach
// stubs mobile=true. The desktop path is byte-for-byte unchanged (it early-returns
// to the static docked strip before the prop is read - asserted in the LANE D
// block too; here we assert mobile behavior + the default-off prop).
describe("LayerLegend  -  zoom-out hide (aoiTooSmallToShow, mobile-only)", () => {
  it("MOBILE: hides the legend entirely when aoiTooSmallToShow is true (no AOI)", () => {
    const { container } = render(
      <LayerLegend layers={[makeLayer()]} aoiTooSmallToShow={true} />,
    );
    expect(container.firstChild).toBeNull();
    expect(screen.queryByTestId("grace2-layer-legend-key")).toBeNull();
  });

  it("MOBILE: hide takes PRECEDENCE over the AOI snap (tiny dot -> hidden, not snapped)", () => {
    // An AOI rect IS projected and would normally corner-attach, but the tiny-dot
    // hide wins: render nothing.
    const { container } = render(
      <LayerLegend
        layers={[makeLayer()]}
        aoiRect={{ left: 100, top: 50, right: 400, bottom: 300 }}
        aoiCornerPlaceable={true}
        aoiTooSmallToShow={true}
      />,
    );
    expect(container.firstChild).toBeNull();
    expect(screen.queryByTestId("grace2-layer-legend-key")).toBeNull();
  });

  it("MOBILE: hide takes PRECEDENCE over the above-chat band dock too", () => {
    const { container } = render(
      <LayerLegend
        layers={[makeLayer()]}
        sheetTopPx={500}
        aoiCornerPlaceable={false}
        aoiTooSmallToShow={true}
      />,
    );
    expect(container.firstChild).toBeNull();
    expect(screen.queryByTestId("grace2-layer-legend-band-row")).toBeNull();
  });

  it("MOBILE: still renders when aoiTooSmallToShow is false (the normal case)", () => {
    render(<LayerLegend layers={[makeLayer()]} aoiTooSmallToShow={false} />);
    expect(screen.getByTestId("grace2-layer-legend-key")).toBeInTheDocument();
  });

  it("defaults to NOT hidden when the prop is omitted (existing callers unaffected)", () => {
    render(<LayerLegend layers={[makeLayer()]} />);
    expect(screen.getByTestId("grace2-layer-legend-key")).toBeInTheDocument();
  });
});

// CHART-OVERLAY HIDE (NATE 2026-06-28, ISSUE 1, MOBILE-ONLY): Chat's full-viewport
// ChartGallery overlay is open (`galleryOpen` lifted to App -> Map -> legend as
// `chartOpen`). On MOBILE the legend portals to document.body and would paint
// above/around the chart, so it renders NOTHING while a chart is open. Default
// false (absent prop = today's behavior). DESKTOP ignores it (it early-returns to
// the static docked strip before the prop is read; the gallery's z=10000 overlay
// covers the legend's z=15 anyway). beforeEach stubs mobile=true.
describe("LayerLegend  -  chart-open hide (chartOpen, mobile-only)", () => {
  it("MOBILE: renders NOTHING when chartOpen is true", () => {
    const { container } = render(
      <LayerLegend layers={[makeLayer()]} chartOpen={true} />,
    );
    expect(container.firstChild).toBeNull();
    expect(screen.queryByTestId("grace2-layer-legend-key")).toBeNull();
  });

  it("MOBILE: hide takes precedence over an AOI snap AND the band dock", () => {
    // An AOI rect IS projected (would normally snap) and a band bottom IS placeable
    // (sheetTopPx present): chart-open still wins and renders nothing.
    const { container } = render(
      <LayerLegend
        layers={[makeLayer()]}
        aoiRect={{ left: 100, top: 50, right: 400, bottom: 700 }}
        sheetTopPx={500}
        aoiCornerPlaceable={false}
        chartOpen={true}
      />,
    );
    expect(container.firstChild).toBeNull();
    expect(screen.queryByTestId("grace2-layer-legend-key")).toBeNull();
    expect(screen.queryByTestId("grace2-layer-legend-band-row")).toBeNull();
  });

  it("MOBILE: renders NORMALLY when chartOpen is false (the normal case)", () => {
    render(<LayerLegend layers={[makeLayer()]} chartOpen={false} />);
    expect(screen.getByTestId("grace2-layer-legend-key")).toBeInTheDocument();
  });

  it("MOBILE: an absent chartOpen prop defaults to NOT hidden", () => {
    render(<LayerLegend layers={[makeLayer()]} />);
    expect(screen.getByTestId("grace2-layer-legend-key")).toBeInTheDocument();
  });

  it("DESKTOP: ignores chartOpen (still renders the static docked strip)", () => {
    // Desktop early-returns to the static bottom-center strip before chartOpen is
    // read, so the legend keeps rendering even with a chart open (the gallery's
    // z=10000 overlay sits above the legend's z=15, so nothing paints over it).
    stubMatchMedia(false);
    render(<LayerLegend layers={[makeLayer()]} chartOpen={true} />);
    expect(screen.getByTestId("grace2-layer-legend-key")).toBeInTheDocument();
  });
});

describe("LayerLegend  -  drag", () => {
  it("moves a key to a free position while dragging, then snaps to a side on release", () => {
    render(
      <LayerLegend layers={[makeLayer()]} anchor={{ left: 400, top: 300 }} barWidth={200} />,
    );
    const key = screen.getByTestId("grace2-layer-legend-key");
    const snappedLeft = key.style.left;
    // Start a drag on the card body (not a control / handle).
    fireEvent.pointerDown(key, { clientX: 410, clientY: 250 });
    fireEvent.pointerMove(window, { clientX: 600, clientY: 100 });
    const dragging = screen.getByTestId("grace2-layer-legend-key");
    // While dragging the key follows the pointer (free position)  -  left changes.
    expect(dragging.style.left).not.toBe(snappedLeft);
    fireEvent.pointerUp(window);
    // On release it SNAPS to a side (free position is dropped -> absolute snapped
    // coords, not the 50%/bottom fallback). SIDE-SNAP: it lands on whichever AOI
    // side it was dropped nearest, which is a settled absolute position.
    const released = screen.getByTestId("grace2-layer-legend-key");
    expect(released.style.left).not.toBe("50%");
    expect(released.style.left.endsWith("px")).toBe(true);
  });

  it("does not start a drag from a control button (the hide eye button)", () => {
    render(
      <LayerLegend layers={[makeLayer()]} anchor={{ left: 400, top: 300 }} barWidth={200} />,
    );
    const key = screen.getByTestId("grace2-layer-legend-key");
    const snappedLeft = key.style.left;
    // LEGEND v2: the hide(eye) button is the only per-key control; it is tagged
    // data-legend-no-drag, so a pointer-down on it must NOT free-drag the card.
    const hide = screen.getByTestId("layer-legend-hide");
    fireEvent.pointerDown(hide, { clientX: 410, clientY: 250 });
    fireEvent.pointerMove(window, { clientX: 600, clientY: 100 });
    expect(screen.getByTestId("grace2-layer-legend-key").style.left).toBe(snappedLeft);
    fireEvent.pointerUp(window);
  });

  it("does not start a drag from the resize handle (it resizes, not free-drags)", () => {
    render(
      <LayerLegend layers={[makeLayer()]} anchor={{ left: 400, top: 300 }} barWidth={200} />,
    );
    const key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.style.width).toBe("200px");
    const handle = within(key).getByTestId("layer-legend-resize");
    // Pointer-down on the resize handle then drag right: this RESIZES (width
    // grows), it does NOT free-drag the card to the pointer position.
    fireEvent.pointerDown(handle, { clientX: 100, clientY: 250 });
    fireEvent.pointerMove(window, { clientX: 260, clientY: 100 });
    const after = screen.getByTestId("grace2-layer-legend-key");
    // Width grew by the drag delta (200 + 160 = 360)  -  the resize gesture ran.
    expect(after.style.width).toBe("360px");
    // The card stayed snapped (absolute top below the AOI bottom edge), not
    // teleported to the pointer's y (100) as a free-drag would.
    expect(parseFloat(after.style.top)).toBeGreaterThanOrEqual(300);
    fireEvent.pointerUp(window);
  });
});

// --- PART C (NATE 2026-06-22): drag the legend to a side -> it SNAPS there with
// the matching orientation (left/right -> vertical, top/bottom -> horizontal).
// The card BODY/EDGE is the drag handle (no dedicated grip icon); the snap +
// reorientation happen on release via legend_snap.nearestSide. -------------- //
describe("LayerLegend  -  drag-to-side snap + reorientation (PART C)", () => {
  // A wide, short AOI rect so the four sides are far apart and a dropped card
  // center maps unambiguously to the nearest edge. jsdom getBoundingClientRect
  // returns zeros, so the dropped card top-left == its center == (move - down).
  const rect = { left: 100, top: 100, right: 500, bottom: 200 };

  function dragCardCenterTo(x: number, y: number): void {
    const key = screen.getByTestId("grace2-layer-legend-key");
    // pointerDown at the origin so offsetX/offsetY are 0 (zeroed bbox), then the
    // move sets the free top-left to exactly (x, y) == the card center.
    fireEvent.pointerDown(key, { clientX: 0, clientY: 0 });
    fireEvent.pointerMove(window, { clientX: x, clientY: y });
    fireEvent.pointerUp(window);
  }

  it("dragging a bottom key to the RIGHT edge snaps it to the right + goes vertical", () => {
    render(<LayerLegend layers={[makeLayer({ layer_id: "s0" })]} aoiRect={rect} />);
    // Default key lands on the BOTTOM (horizontal).
    let key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-side")).toBe("bottom");
    expect(key.getAttribute("data-legend-orientation")).toBe("horizontal");
    // Drop the card center near the RIGHT edge (x-right=500, mid-height).
    dragCardCenterTo(490, 150);
    key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-side")).toBe("right");
    expect(key.getAttribute("data-legend-orientation")).toBe("vertical");
    // It snapped to an absolute position (not the free drag spot, not the fallback).
    expect(key.style.left).not.toBe("50%");
    // Right side: left = aoi.right(500) + gap. So it sits to the right of the box.
    expect(parseFloat(key.style.left)).toBeGreaterThan(500);
  });

  it("dragging to the TOP edge snaps to the top + stays horizontal", () => {
    render(<LayerLegend layers={[makeLayer({ layer_id: "s1" })]} aoiRect={rect} />);
    // Drop the card center near the TOP edge (y-top=100, mid-width).
    dragCardCenterTo(300, 110);
    const key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-side")).toBe("top");
    expect(key.getAttribute("data-legend-orientation")).toBe("horizontal");
  });

  it("dragging to the LEFT edge snaps to the left + goes vertical", () => {
    render(<LayerLegend layers={[makeLayer({ layer_id: "s2" })]} aoiRect={rect} />);
    dragCardCenterTo(110, 150);
    const key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-side")).toBe("left");
    expect(key.getAttribute("data-legend-orientation")).toBe("vertical");
  });

  it("the side override persists (the key stays where it was dragged)", () => {
    render(<LayerLegend layers={[makeLayer({ layer_id: "s3" })]} aoiRect={rect} />);
    dragCardCenterTo(490, 150); // -> right
    expect(
      screen.getByTestId("grace2-layer-legend-key").getAttribute("data-legend-side"),
    ).toBe("right");
    // A no-op rerender (same props) must not reset the snapped side.
    dragCardCenterTo(490, 150);
    expect(
      screen.getByTestId("grace2-layer-legend-key").getAttribute("data-legend-side"),
    ).toBe("right");
  });

  it("with NO AOI rect, a drag just clears free (no side override, stays bottom-center)", () => {
    render(<LayerLegend layers={[makeLayer({ layer_id: "s4" })]} />);
    const key = screen.getByTestId("grace2-layer-legend-key");
    // AOI-less fallback bottom-center.
    expect(key.style.left).toBe("50%");
    fireEvent.pointerDown(key, { clientX: 0, clientY: 0 });
    fireEvent.pointerMove(window, { clientX: 400, clientY: 50 });
    fireEvent.pointerUp(window);
    // No AOI to snap to -> back to the bottom-center fallback, no override.
    const after = screen.getByTestId("grace2-layer-legend-key");
    expect(after.style.left).toBe("50%");
    expect(after.getAttribute("data-legend-orientation")).toBe("horizontal");
  });
});

// --- PART C: the card body/edge IS the drag handle (no dedicated grip icon) -- //
describe("LayerLegend  -  body/edge is the drag handle, no grip icon (PART C)", () => {
  it("renders no dedicated drag-grip element (the body is grabbable)", () => {
    render(<LayerLegend layers={[makeLayer()]} aoiRect={{ left: 100, top: 100, right: 300, bottom: 250 }} />);
    // There must be no separate drag-handle/grip testid; the card itself drags.
    expect(screen.queryByTestId("layer-legend-drag-handle")).toBeNull();
    expect(screen.queryByTestId("layer-legend-grip")).toBeNull();
    // The card body carries grab affordance (cursor:grab) so an edge/body grab works.
    const key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.style.cursor).toBe("grab");
  });

  it("a pointer-down on the card body (not a control) starts the drag", () => {
    render(<LayerLegend layers={[makeLayer()]} aoiRect={{ left: 100, top: 100, right: 500, bottom: 200 }} />);
    const key = screen.getByTestId("grace2-layer-legend-key");
    const startLeft = key.style.left;
    fireEvent.pointerDown(key, { clientX: 0, clientY: 0 });
    fireEvent.pointerMove(window, { clientX: 250, clientY: 400 });
    // The card follows the pointer (free position) while dragging from the body.
    expect(screen.getByTestId("grace2-layer-legend-key").style.left).not.toBe(startLeft);
    fireEvent.pointerUp(window);
  });
});

// =====================================================================
// LEGEND v2 (NATE 2026-06-22) - consolidated spec
//   1. minimal FLATTENED two-row key (no collapsible toggle)
//   2. edge/body drag handle, no grip glyph
//   3. drop-zone signals on drag-start at left/right/top
//   4. snap to LEFT/RIGHT/TOP only (bottom excluded)
// =====================================================================

// --- ITEM 1: minimal flattened two-row key, no collapse/expand toggle -------- //
describe("LayerLegend v2  -  flattened two-row key, no collapsible toggle (item 1)", () => {
  it("renders NO compact/flatten collapse toggle (the key is always flat)", () => {
    render(<LayerLegend layers={[makeLayer()]} />);
    // The old collapse/expand toggle is gone; the key is permanently flat.
    expect(screen.queryByTestId("layer-legend-compact-toggle")).toBeNull();
    // And there is no compact data-flag on the card anymore.
    const key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-compact")).toBeNull();
  });

  it("always shows title + min/max + bar together (flat key, nothing hidden)", () => {
    render(<LayerLegend layers={[makeLayer()]} />);
    expect(screen.getByTestId("layer-legend-title")).toBeInTheDocument();
    expect(screen.getByTestId("layer-legend-min-label")).toBeInTheDocument();
    expect(screen.getByTestId("layer-legend-max-label")).toBeInTheDocument();
    expect(screen.getByTestId("layer-legend-bar")).toBeInTheDocument();
  });

  it("HORIZONTAL key: min value at the LEFT end, max at the RIGHT end, flanking the bar", () => {
    // Bottom-docked (no scrubber) => horizontal. The value row is [min] bar [max].
    render(
      <LayerLegend
        layers={[makeLayer()]}
        anchor={{ left: 400, top: 300 }}
        barWidth={240}
      />,
    );
    const key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-orientation")).toBe("horizontal");
    const row = within(key).getByTestId("layer-legend-value-row");
    // DOM order within the row: min, bar, max  -  i.e. min flanks the LEFT end of
    // the bar and max flanks the RIGHT end.
    const kids = Array.from(row.children);
    const minIdx = kids.findIndex(
      (c) => c.getAttribute("data-testid") === "layer-legend-min-label",
    );
    const barIdx = kids.findIndex(
      (c) => c.getAttribute("data-testid") === "layer-legend-bar",
    );
    const maxIdx = kids.findIndex(
      (c) => c.getAttribute("data-testid") === "layer-legend-max-label",
    );
    expect(minIdx).toBeLessThan(barIdx);
    expect(barIdx).toBeLessThan(maxIdx);
    expect(within(row).getByTestId("layer-legend-min-label")).toHaveTextContent("0 m");
    expect(within(row).getByTestId("layer-legend-max-label")).toHaveTextContent("3.5 m");
  });

  it("VERTICAL key: max value at the TOP, min at the BOTTOM, flanking the bar", () => {
    // Drag a key to the RIGHT edge so it goes vertical, then assert the rotated
    // value placement (max above the bar, min below it).
    const rect = { left: 100, top: 100, right: 500, bottom: 200 };
    render(<LayerLegend layers={[makeLayer({ layer_id: "v0" })]} aoiRect={rect} />);
    const key0 = screen.getByTestId("grace2-layer-legend-key");
    fireEvent.pointerDown(key0, { clientX: 0, clientY: 0 });
    fireEvent.pointerMove(window, { clientX: 490, clientY: 150 }); // near right edge
    fireEvent.pointerUp(window);
    const key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-orientation")).toBe("vertical");
    const row = within(key).getByTestId("layer-legend-value-row");
    const kids = Array.from(row.children);
    const maxIdx = kids.findIndex(
      (c) => c.getAttribute("data-testid") === "layer-legend-max-label",
    );
    const barIdx = kids.findIndex(
      (c) => c.getAttribute("data-testid") === "layer-legend-bar",
    );
    const minIdx = kids.findIndex(
      (c) => c.getAttribute("data-testid") === "layer-legend-min-label",
    );
    // Vertical: max is ABOVE the bar (earlier in column flow), min is BELOW it.
    expect(maxIdx).toBeLessThan(barIdx);
    expect(barIdx).toBeLessThan(minIdx);
  });
});

// --- ITEM 2: edge/body drag handle, no dedicated grip glyph ------------------ //
describe("LayerLegend v2  -  edge/body is the drag handle, no grip glyph (item 2)", () => {
  it("renders no drag-grip glyph element; the card body carries cursor:grab", () => {
    render(
      <LayerLegend layers={[makeLayer()]} aoiRect={{ left: 100, top: 100, right: 300, bottom: 250 }} />,
    );
    expect(screen.queryByTestId("layer-legend-drag-handle")).toBeNull();
    expect(screen.queryByTestId("layer-legend-grip")).toBeNull();
    const key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.style.cursor).toBe("grab");
    // The resize handle no longer paints a diagonal grip glyph (just a hit-target).
    const resize = within(key).getByTestId("layer-legend-resize");
    expect(resize.style.backgroundImage === "" || resize.style.backgroundImage === "none").toBe(
      true,
    );
  });

  it("the hide control is excluded from drag (data-legend-no-drag)", () => {
    render(<LayerLegend layers={[makeLayer()]} aoiRect={{ left: 100, top: 100, right: 500, bottom: 200 }} />);
    const hide = screen.getByTestId("layer-legend-hide");
    // The control (or an ancestor up to the card) carries the no-drag marker.
    expect(hide.closest("[data-legend-no-drag]")).not.toBeNull();
  });
});

// --- ITEM 3: drop-zone signals appear on drag-start at left/right/top -------- //
describe("LayerLegend v2  -  drop-zone signals on drag-start (item 3)", () => {
  const rect = { left: 100, top: 100, right: 500, bottom: 300 };

  it("shows NO drop-zone signals when idle (not dragging)", () => {
    render(<LayerLegend layers={[makeLayer({ layer_id: "d0" })]} aoiRect={rect} />);
    expect(screen.queryAllByTestId("layer-legend-dropzone")).toHaveLength(0);
  });

  it("on drag-start renders signals at exactly left/right/top (never bottom)", () => {
    render(<LayerLegend layers={[makeLayer({ layer_id: "d1" })]} aoiRect={rect} />);
    const key = screen.getByTestId("grace2-layer-legend-key");
    fireEvent.pointerDown(key, { clientX: 0, clientY: 0 });
    const zones = screen.getAllByTestId("layer-legend-dropzone");
    expect(zones).toHaveLength(3);
    const sides = zones.map((z) => z.getAttribute("data-legend-dropzone-side")).sort();
    expect(sides).toEqual(["left", "right", "top"]);
    expect(sides).not.toContain("bottom");
    fireEvent.pointerUp(window);
  });

  it("highlights the nearest target as active while dragging toward it", () => {
    render(<LayerLegend layers={[makeLayer({ layer_id: "d2" })]} aoiRect={rect} />);
    const key = screen.getByTestId("grace2-layer-legend-key");
    fireEvent.pointerDown(key, { clientX: 0, clientY: 0 });
    // Drag the card center toward the RIGHT edge (x near 500, mid-height).
    fireEvent.pointerMove(window, { clientX: 495, clientY: 200 });
    const zones = screen.getAllByTestId("layer-legend-dropzone");
    const active = zones.filter((z) => z.getAttribute("data-legend-dropzone-active") === "1");
    // Exactly one is active and it is the RIGHT target.
    expect(active).toHaveLength(1);
    expect(active[0]!.getAttribute("data-legend-dropzone-side")).toBe("right");
    fireEvent.pointerUp(window);
  });

  it("clears all drop-zone signals on release", () => {
    render(<LayerLegend layers={[makeLayer({ layer_id: "d3" })]} aoiRect={rect} />);
    const key = screen.getByTestId("grace2-layer-legend-key");
    fireEvent.pointerDown(key, { clientX: 0, clientY: 0 });
    expect(screen.getAllByTestId("layer-legend-dropzone").length).toBeGreaterThan(0);
    fireEvent.pointerUp(window);
    expect(screen.queryAllByTestId("layer-legend-dropzone")).toHaveLength(0);
  });

  it("renders no drop-zone signals when there is no AOI rect to snap against", () => {
    render(<LayerLegend layers={[makeLayer({ layer_id: "d4" })]} />);
    const key = screen.getByTestId("grace2-layer-legend-key");
    fireEvent.pointerDown(key, { clientX: 0, clientY: 0 });
    // No AOI => nothing to snap to => no signals.
    expect(screen.queryAllByTestId("layer-legend-dropzone")).toHaveLength(0);
    fireEvent.pointerUp(window);
  });
});

// --- ITEM 4: snap to LEFT/RIGHT/TOP only (bottom excluded) ------------------- //
describe("LayerLegend v2  -  bottom-excluded snap (item 4)", () => {
  // Tall, narrow AOI so a drag toward the bottom is unambiguously nearer the
  // bottom edge than left/right/top in raw pixels - proving the EXCLUSION (not
  // just geometry) is what keeps the key off the bottom.
  const rect = { left: 100, top: 100, right: 300, bottom: 600 };

  function dragCardCenterTo(x: number, y: number): void {
    const key = screen.getByTestId("grace2-layer-legend-key");
    fireEvent.pointerDown(key, { clientX: 0, clientY: 0 });
    fireEvent.pointerMove(window, { clientX: x, clientY: y });
    fireEvent.pointerUp(window);
  }

  it("dragging toward the BOTTOM edge snaps to the nearest of left/right/top (never bottom)", () => {
    render(<LayerLegend layers={[makeLayer({ layer_id: "b0" })]} aoiRect={rect} />);
    // Drop the card center just below the bottom edge, slightly right of center:
    // in raw px the bottom edge (y=600) is closest, but bottom is EXCLUDED, so it
    // must snap to the nearest valid side (right, since x is closer to the right).
    dragCardCenterTo(260, 590);
    const key = screen.getByTestId("grace2-layer-legend-key");
    const side = key.getAttribute("data-legend-side");
    expect(side).not.toBe("bottom");
    expect(["left", "right", "top"]).toContain(side);
    expect(side).toBe("right");
    // Right dock => vertical orientation.
    expect(key.getAttribute("data-legend-orientation")).toBe("vertical");
  });

  it("dragging toward the bottom-LEFT snaps to the left (not bottom)", () => {
    render(<LayerLegend layers={[makeLayer({ layer_id: "b1" })]} aoiRect={rect} />);
    dragCardCenterTo(140, 590);
    const key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-side")).toBe("left");
  });

  it("an explicit drag toward the TOP still snaps to the top (horizontal)", () => {
    render(<LayerLegend layers={[makeLayer({ layer_id: "b2" })]} aoiRect={rect} />);
    dragCardCenterTo(200, 110);
    const key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-side")).toBe("top");
    expect(key.getAttribute("data-legend-orientation")).toBe("horizontal");
  });
});

// FRAME-TRUTH (NATE 2026-06-19)  -  the legend gradient + numeric bounds must
// match what the map actually paints, i.e. the TiTiler rescale + colormap_name
// embedded in the frame layer's XYZ tile-template URL (the SOURCE OF TRUTH),
// falling back to style_preset only when those params are absent / unknown.
describe("LayerLegend  -  TiTiler rescale + colormap from the tile URL (frame truth)", () => {
  // An AWS frame layer whose wms_url is a TiTiler XYZ template carrying the
  // truth as query params (rescale=lo,hi + colormap_name).
  function makeTitilerLayer(
    query: string,
    overrides: Partial<ProjectLayerSummary> = {},
  ): ProjectLayerSummary {
    return makeLayer({
      wms_url: `https://edge.example/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=s3%3A%2F%2Fb%2Fk.tif${query}`,
      ...overrides,
    });
  }

  it("uses rescale=0,3.5 for the min/max labels (real bounds, not the preset)", () => {
    render(
      <LayerLegend
        layers={[makeTitilerLayer("&rescale=0,3.5&colormap_name=blues")]}
      />,
    );
    // The preset default for continuous_flood_depth is 0..3.5 m WITH a unit;
    // the URL rescale drops the unit (arbitrary-layer bounds)  -  assert exact.
    expect(screen.getByTestId("layer-legend-min-label").textContent).toBe("0");
    expect(screen.getByTestId("layer-legend-max-label").textContent).toBe("3.5");
  });

  it("renders the parsed-from-URL bounds even when they differ from the preset", () => {
    render(
      <LayerLegend
        layers={[makeTitilerLayer("&rescale=10,250&colormap_name=viridis")]}
      />,
    );
    expect(screen.getByTestId("layer-legend-min-label").textContent).toBe("10");
    expect(screen.getByTestId("layer-legend-max-label").textContent).toBe("250");
  });

  it("renders a blues gradient from colormap_name=blues", () => {
    render(
      <LayerLegend
        layers={[makeTitilerLayer("&rescale=0,3.5&colormap_name=blues")]}
      />,
    );
    const bar = screen.getByTestId("layer-legend-bar");
    // Blues ramp anchors: light #f7fbff -> dark #08519c (see titiler_colormap).
    expect(bar.style.background).toContain("#f7fbff");
    expect(bar.style.background).toContain("#08519c");
    // The flood preset gradient (rgba blues) must NOT be what painted here.
    expect(bar.style.background).not.toContain("rgba(8,48,107");
  });

  it("falls back to the style_preset gradient + bounds when the URL has no params", () => {
    // wms_url is a plain QGIS WMS endpoint (no rescale / colormap_name), and
    // uri is a gs:// pointer  -  neither carries TiTiler params.
    render(
      <LayerLegend
        layers={[
          makeLayer({
            wms_url: "https://qgis.example/ows/?SERVICE=WMS&LAYERS=depth",
          }),
        ]}
      />,
    );
    // Preset bounds (WITH unit) are preserved.
    expect(screen.getByTestId("layer-legend-min-label")).toHaveTextContent("0 m");
    expect(screen.getByTestId("layer-legend-max-label")).toHaveTextContent("3.5 m");
    // Preset gradient (rgba flood blues) still paints.
    const bar = screen.getByTestId("layer-legend-bar");
    expect(bar.style.background).toContain("rgba(8,48,107");
  });

  it("falls back to the preset gradient for an unknown colormap_name (but uses the URL rescale)", () => {
    render(
      <LayerLegend
        layers={[makeTitilerLayer("&rescale=0,7&colormap_name=nonexistent_cmap")]}
      />,
    );
    // Unknown colormap -> preset gradient fallback (rgba flood blues paints).
    const bar = screen.getByTestId("layer-legend-bar");
    expect(bar.style.background).toContain("rgba(8,48,107");
    expect(bar.style.background).not.toContain("#08519c");
    // The rescale IS valid, so the numeric bounds still come from the URL.
    expect(screen.getByTestId("layer-legend-min-label").textContent).toBe("0");
    expect(screen.getByTestId("layer-legend-max-label").textContent).toBe("7");
  });

  it("parses rescale + colormap from the `uri` field when wms_url lacks them", () => {
    // Some layers carry the TiTiler template in `uri` instead of `wms_url`.
    render(
      <LayerLegend
        layers={[
          makeLayer({
            wms_url: null,
            uri: "https://edge.example/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=s3%3A%2F%2Fb%2Fk.tif&rescale=0,25&colormap_name=reds",
          }),
        ]}
      />,
    );
    expect(screen.getByTestId("layer-legend-min-label").textContent).toBe("0");
    expect(screen.getByTestId("layer-legend-max-label").textContent).toBe("25");
    // Reds ramp anchors (light #fff5f0 -> dark #a50f15).
    const bar = screen.getByTestId("layer-legend-bar");
    expect(bar.style.background).toContain("#fff5f0");
    expect(bar.style.background).toContain("#a50f15");
  });

  it("reflects the representative frame's rescale + colormap for a sequential group (item 4)", () => {
    // All frames in a group share rescale + colormap; parse from the first.
    function makeFrameTitiler(hour: number): ProjectLayerSummary {
      const hh = String(hour).padStart(2, "0");
      return {
        layer_id: `run-a-f${hh}`,
        name: `HRRR precip F+${hh}h`,
        layer_type: "raster",
        uri: `gs://grace-2/runs/run-a/precip_f${hh}.cog.tif`,
        wms_url: `https://edge.example/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=s3%3A%2F%2Fb%2Fprecip_f${hh}.tif&rescale=0,100&colormap_name=blues`,
        visible: true,
        opacity: 1,
        z_index: 1,
        style_preset: "continuous_flood_depth",
      };
    }
    const layers = [makeFrameTitiler(1), makeFrameTitiler(3), makeFrameTitiler(6)];
    render(<LayerLegend layers={layers} />);
    // Exactly ONE group key, and its bounds + gradient come from the frames.
    expect(screen.getAllByTestId("grace2-layer-legend-key")).toHaveLength(1);
    expect(screen.getByTestId("layer-legend-min-label").textContent).toBe("0");
    expect(screen.getByTestId("layer-legend-max-label").textContent).toBe("100");
    const bar = screen.getByTestId("layer-legend-bar");
    expect(bar.style.background).toContain("#f7fbff");
  });
});

// --- Item a (Z-HIERARCHY, NATE 2026-06-20)  -  legend renders BELOW chat/layers - //
//
// The legend keys + the collapsed show-pill must paint BEHIND the chat panel
// (z=32) and the Layers/Cases panels (z=20) so they never cover the user's
// controls. (They previously used z=50, which painted OVER the chat  -  the bug.)
describe("LayerLegend  -  z-index below the chat + layers panels (item a)", () => {
  it("the key card z-index is below the chat (32) and layers panels (20)", () => {
    render(<LayerLegend layers={[makeLayer()]} />);
    const key = screen.getByTestId("grace2-layer-legend-key");
    const z = parseInt(key.style.zIndex, 10);
    expect(z).toBe(LEGEND_Z_INDEX);
    expect(z).toBeLessThan(20); // below the Layers/Cases panels
    expect(z).toBeLessThan(32); // below the chat panel
  });

  it("the collapsed show-pill z-index is also below chat + layers", () => {
    render(<LayerLegend layers={[makeLayer()]} />);
    fireEvent.click(screen.getByTestId("layer-legend-hide"));
    const pill = screen.getByTestId("grace2-layer-legend-show");
    const z = parseInt(pill.style.zIndex, 10);
    expect(z).toBe(LEGEND_Z_INDEX);
    expect(z).toBeLessThan(20);
    expect(z).toBeLessThan(32);
  });
});

// --- Item e (ONE LEGEND per flood-depth series)  -  peak folds into the frames -- //
//
// The per-frame depth COGs ("Flood depth step N") AND the max/peak depth layer
// all paint with the SAME colormap + rescale, so they form ONE series and must
// collapse to ONE legend key  -  not one-per-frame + a separate peak key.
describe("LayerLegend  -  one legend per depth series incl. the peak (item e)", () => {
  function depthFrame(hour: number): ProjectLayerSummary {
    const hh = String(hour).padStart(2, "0");
    return makeLayer({
      layer_id: `run-a-depth-f${hh}`,
      name: `Flood depth step ${hour}`,
      wms_url: `https://edge.example/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=s3%3A%2F%2Fb%2Fdepth_f${hh}.tif&rescale=0,3.5&colormap_name=blues`,
    });
  }
  function peakDepth(): ProjectLayerSummary {
    // SAME colormap + rescale as the frames => same series.
    return makeLayer({
      layer_id: "run-a-depth-peak",
      name: "Max flood depth",
      wms_url:
        "https://edge.example/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=s3%3A%2F%2Fb%2Fdepth_peak.tif&rescale=0,3.5&colormap_name=blues",
    });
  }

  it("collapses N depth frames + the peak into ONE legend key (series dedup)", () => {
    const layers = [peakDepth(), depthFrame(1), depthFrame(3), depthFrame(6)];
    render(<LayerLegend layers={layers} />);
    // Item e: exactly ONE key for the whole depth series (frames + peak).
    expect(screen.getAllByTestId("grace2-layer-legend-key")).toHaveLength(1);
  });

  it("a layer with a DIFFERENT colormap/scale still gets its own key", () => {
    const depthSeries = [depthFrame(1), depthFrame(3), peakDepth()];
    // A velocity raster  -  different colormap + rescale => different series.
    const velocity = makeLayer({
      layer_id: "run-a-velocity",
      name: "Flow velocity",
      wms_url:
        "https://edge.example/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=s3%3A%2F%2Fb%2Fvel.tif&rescale=0,5&colormap_name=viridis",
    });
    render(<LayerLegend layers={[velocity, ...depthSeries]} />);
    // Two series => two keys (depth + velocity).
    expect(screen.getAllByTestId("grace2-layer-legend-key")).toHaveLength(2);
  });
});

// --- Item g (ORIENTATION)  -  vertical on left/right, horizontal on top/bottom -- //
describe("LayerLegend  -  orientation flips by docked side (item g)", () => {
  const anchor = { left: 500, top: 400 };
  const barWidth = 200;

  function fourKeys(): ProjectLayerSummary[] {
    // Four DISTINCT preset-only rasters (no URL colormap => not a series), so
    // each gets its own key and lands on its own CCW side.
    return [0, 1, 2, 3].map((i) =>
      makeLayer({ layer_id: `o${i}`, z_index: 4 - i }),
    );
  }

  it("the bottom + top keys are HORIZONTAL; the left + right keys are VERTICAL", () => {
    render(<LayerLegend layers={fourKeys()} anchor={anchor} barWidth={barWidth} />);
    const keys = screen.getAllByTestId("grace2-layer-legend-key");
    const bySide = (s: string) =>
      keys.find((k) => k.getAttribute("data-legend-side") === s)!;
    expect(bySide("bottom").getAttribute("data-legend-orientation")).toBe("horizontal");
    expect(bySide("top").getAttribute("data-legend-orientation")).toBe("horizontal");
    expect(bySide("right").getAttribute("data-legend-orientation")).toBe("vertical");
    expect(bySide("left").getAttribute("data-legend-orientation")).toBe("vertical");
  });

  it("a vertical (left/right) bar uses a to-top gradient; a horizontal one to-right", () => {
    render(<LayerLegend layers={fourKeys()} anchor={anchor} barWidth={barWidth} />);
    const keys = screen.getAllByTestId("grace2-layer-legend-key");
    const bySide = (s: string) =>
      keys.find((k) => k.getAttribute("data-legend-side") === s)!;
    const rightBar = within(bySide("right")).getByTestId("layer-legend-bar");
    const bottomBar = within(bySide("bottom")).getByTestId("layer-legend-bar");
    expect(rightBar.style.background).toContain("to top");
    expect(bottomBar.style.background).toContain("to right");
  });

  it("the AOI-less bottom-center fallback is horizontal", () => {
    render(<LayerLegend layers={[makeLayer()]} />);
    const key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-orientation")).toBe("horizontal");
  });
});

// --- Item d (SCALE WITH AOI)  -  overlay scales with the AOI on-screen px size -- //
describe("LayerLegend  -  scales the default key width with the AOI px size (item d)", () => {
  it("a tiny on-screen AOI yields a SMALLER default key than a large one", () => {
    // No barWidth override => the default width is sized off STATIC * scale,
    // which tracks the aoiRect's on-screen size (clamped). A tiny rect shrinks it.
    const tiny = { left: 100, top: 100, right: 140, bottom: 140 }; // 40px box
    const huge = { left: 0, top: 0, right: 1200, bottom: 1200 };
    const { rerender } = render(
      <LayerLegend layers={[makeLayer()]} aoiRect={tiny} />,
    );
    const tinyW = parseFloat(screen.getByTestId("grace2-layer-legend-key").style.width);
    rerender(<LayerLegend layers={[makeLayer()]} aoiRect={huge} />);
    const hugeW = parseFloat(screen.getByTestId("grace2-layer-legend-key").style.width);
    expect(tinyW).toBeLessThan(hugeW);
    // Both stay within the usable clamp band (never unusably tiny / huge).
    expect(tinyW).toBeGreaterThanOrEqual(140); // KEY_MIN_WIDTH floor
    expect(hugeW).toBeLessThanOrEqual(520); // KEY_MAX_WIDTH ceiling
  });
});

// --- Item f (legend not obscured by the scrubber)  -  bottom-reserve push ------- //
describe("LayerLegend  -  bottom key clears the scrubber footprint (item f)", () => {
  it("pushes the bottom-side key down by the supplied bottomReservePx", () => {
    const rect = { left: 100, top: 100, right: 500, bottom: 200 };
    const { rerender } = render(
      <LayerLegend layers={[makeLayer({ layer_id: "br0" })]} aoiRect={rect} />,
    );
    const baseTop = parseFloat(
      screen.getByTestId("grace2-layer-legend-key").style.top,
    );
    rerender(
      <LayerLegend
        layers={[makeLayer({ layer_id: "br0" })]}
        aoiRect={rect}
        bottomReservePx={60}
      />,
    );
    const reservedTop = parseFloat(
      screen.getByTestId("grace2-layer-legend-key").style.top,
    );
    // The bottom key is pushed DOWN (greater top) by the reserve so it clears
    // the scrubber that pins just below the AOI bottom edge.
    expect(reservedTop).toBeCloseTo(baseTop + 60, 0);
  });
});

// --- Item b (mobile controlled hide + suppressed pill) ------------------------ //
describe("LayerLegend  -  controlled hide + suppressed floating pill (item b)", () => {
  it("renders nothing for the pill when hidden + suppressShowPill (mobile)", () => {
    render(
      <LayerLegend layers={[makeLayer()]} hidden suppressShowPill />,
    );
    // No floating pill (the in-panel toggle is the only affordance on mobile).
    expect(screen.queryByTestId("grace2-layer-legend-show")).toBeNull();
    // And no keys (hidden).
    expect(screen.queryByTestId("grace2-layer-legend-key")).toBeNull();
  });

  it("honors the controlled `hidden` prop (parent owns the state)", () => {
    const { rerender } = render(
      <LayerLegend layers={[makeLayer()]} hidden={false} suppressShowPill />,
    );
    expect(screen.getByTestId("grace2-layer-legend-key")).toBeInTheDocument();
    rerender(<LayerLegend layers={[makeLayer()]} hidden suppressShowPill />);
    expect(screen.queryByTestId("grace2-layer-legend-key")).toBeNull();
  });

  it("fires onHiddenChange when the per-key hide control is clicked (controlled)", () => {
    const onHiddenChange = vi.fn();
    render(
      <LayerLegend
        layers={[makeLayer()]}
        hidden={false}
        onHiddenChange={onHiddenChange}
      />,
    );
    fireEvent.click(screen.getByTestId("layer-legend-hide"));
    expect(onHiddenChange).toHaveBeenCalledWith(true);
  });
});

// --- legendHasContent + MobileLegendToggle (item b helpers) ------------------- //
describe("legendHasContent helper", () => {
  it("is true when there is an eligible raster legend, false otherwise", () => {
    expect(legendHasContent([makeLayer()])).toBe(true);
    expect(legendHasContent([])).toBe(false);
    expect(legendHasContent([makeLayer({ style_preset: null })])).toBe(false);
  });
});

describe("MobileLegendToggle", () => {
  it("shows 'Hide legend' when visible and toggles to hidden on click", () => {
    const onToggle = vi.fn();
    render(<MobileLegendToggle hidden={false} onToggle={onToggle} />);
    const btn = screen.getByTestId("grace2-mobile-legend-toggle");
    expect(btn).toHaveTextContent("Hide legend");
    expect(btn).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(btn);
    expect(onToggle).toHaveBeenCalledWith(true);
  });

  it("shows 'Show legend' when hidden and toggles to visible on click", () => {
    const onToggle = vi.fn();
    render(<MobileLegendToggle hidden onToggle={onToggle} />);
    const btn = screen.getByTestId("grace2-mobile-legend-toggle");
    expect(btn).toHaveTextContent("Show legend");
    expect(btn).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(btn);
    expect(onToggle).toHaveBeenCalledWith(false);
  });
});

// --- LANE D: DESKTOP docked legend strip (NATE's DECISION) -------------------- //
//
// On DESKTOP the legend is a single STATIC bottom-center docked strip: fixed
// size, NO scaling, NO drag, NO resize, NO AOI-snap. The whole snap/drag/resize
// machinery is mobile-only (tested above with the mobile matchMedia stub). These
// tests force DESKTOP (matchMedia mobile=false).
describe("LayerLegend  -  desktop docked strip (LANE D)", () => {
  beforeEach(() => {
    stubMatchMedia(false); // DESKTOP
  });

  it("renders a static bottom-center docked strip (no snap/drag/resize)", () => {
    render(<LayerLegend layers={[makeLayer()]} />);
    const root = screen.getByTestId("grace2-layer-legend");
    expect(root).toHaveAttribute("data-legend-docked", "desktop");
    // The docked strip pins to a fixed bottom; it is NOT an AOI-snapped card.
    expect(root.style.position).toBe("fixed");
    expect(root.style.bottom).toBe("16px");
    // No drag handle / resize handle / drop-zones on desktop.
    expect(screen.queryByTestId("layer-legend-resize")).toBeNull();
    expect(screen.queryByTestId("layer-legend-dropzone")).toBeNull();
    // The key + title still render (content contract preserved).
    expect(screen.getByTestId("grace2-layer-legend-key")).toBeInTheDocument();
    expect(screen.getByTestId("layer-legend-title")).toHaveTextContent(
      "Max flood depth (m)",
    );
  });

  it("the (m) unit uses a non-breaking space so it never wraps from the value", () => {
    render(<LayerLegend layers={[makeLayer()]} />);
    const minLabel = screen.getByTestId("layer-legend-min-label");
    // The label is "<value> <unit>" (NBSP), never a regular space (which
    // could wrap). continuous_flood_depth has unit "m".
    expect(minLabel.textContent).toBe("0\u00a0m"); // NBSP between value + unit
    expect(minLabel.textContent).not.toContain("0 m"); // never a plain ASCII space
    // The gradient bar is the flex element that absorbs slack (flex:1; minWidth:0).
    const bar = screen.getByTestId("layer-legend-bar");
    expect(bar.style.flexGrow).toBe("1");
    // minWidth:0 lets the bar shrink to absorb slack (happy-dom stores "0").
    expect(bar.style.minWidth).toBe("0");
  });

  it("does not render the AOI-snapped multi-card wrapper attributes on desktop", () => {
    render(
      <LayerLegend
        layers={[makeLayer({ layer_id: "a" }), makeLayer({ layer_id: "b" })]}
        aoiRect={{ left: 100, top: 100, right: 500, bottom: 300 }}
      />,
    );
    // Even with an aoiRect supplied, desktop ignores the snap pipeline: the keys
    // carry no per-side snap (all bottom/horizontal) and there is exactly one
    // docked root.
    const roots = screen.getAllByTestId("grace2-layer-legend");
    expect(roots).toHaveLength(1);
    for (const key of screen.getAllByTestId("grace2-layer-legend-key")) {
      expect(key.getAttribute("data-legend-orientation")).toBe("horizontal");
      expect(key.getAttribute("data-legend-side")).toBe("bottom");
    }
  });

  it("renders nothing when hidden + suppressShowPill (desktop pill is in BottomRowButtons)", () => {
    render(<LayerLegend layers={[makeLayer()]} hidden suppressShowPill />);
    expect(screen.queryByTestId("grace2-layer-legend")).toBeNull();
    expect(screen.queryByTestId("grace2-layer-legend-show")).toBeNull();
  });

  it("the mobile BAND props (sheetTopPx + overlapping AOI) are IGNORED on desktop", () => {
    // BAND-vs-EDGE GATE (NATE 2026-06-28) is mobile-only. With a deep bbox that
    // would force the mobile band form, desktop STILL renders the single desktop
    // docked strip and NEVER a one-row band. (Desktop position now follows the
    // bbox-anchored default - see the desktop draggable-dock block below - but the
    // mobile band machinery is never reached.)
    render(
      <LayerLegend
        layers={[makeLayer()]}
        aoiRect={{ left: 100, top: 50, right: 400, bottom: 700 }}
        sheetTopPx={500}
        aoiCornerPlaceable={true}
      />,
    );
    const root = screen.getByTestId("grace2-layer-legend");
    expect(root).toHaveAttribute("data-legend-docked", "desktop");
    // No mobile one-row band on desktop, ever.
    expect(screen.queryByTestId("grace2-layer-legend-band-row")).toBeNull();
  });
});

// --- DESKTOP DRAGGABLE DOCK (NATE 2026-06-28) -------------------------------- //
//
// NATE: "it should default to the bbox and then I should be able to also drag it
// to the bottom and have it static there." The desktop legend strip:
//   (1) DEFAULTS to the bbox-anchored position (snapped below the AOI bbox);
//   (2) is DRAGGABLE; a drag to the bottom region snaps it to a STATIC bottom
//       dock and it STAYS there;
//   (3) the chosen mode PERSISTS (localStorage) across a remount;
//   (4) a stored "bottom" preference restores on mount.
// DESKTOP-ONLY: the mobile path is byte-for-byte unchanged (asserted by the
// mobile suites above still passing). These force DESKTOP (matchMedia=false) and
// clear the persisted dock mode before each test so they do not leak.
describe("LayerLegend  -  desktop draggable dock (bbox default + bottom park)", () => {
  beforeEach(() => {
    stubMatchMedia(false); // DESKTOP
    try {
      localStorage.removeItem(LS_DESKTOP_LEGEND_DOCK);
    } catch {
      /* jsdom always has localStorage; non-fatal */
    }
  });
  afterEach(() => {
    try {
      localStorage.removeItem(LS_DESKTOP_LEGEND_DOCK);
    } catch {
      /* non-fatal */
    }
  });

  // The projected AOI bbox the desktop strip anchors to by default.
  const aoiRect = { left: 200, top: 100, right: 600, bottom: 400 };

  // (1) DEFAULTS to the bbox-anchored position when an AOI rect is on screen.
  it("defaults to the bbox-anchored position (below the bbox bottom edge)", () => {
    render(<LayerLegend layers={[makeLayer()]} aoiRect={aoiRect} />);
    const root = screen.getByTestId("grace2-layer-legend");
    expect(root).toHaveAttribute("data-legend-docked", "desktop");
    // Default mode is bbox-anchored (not the bottom dock).
    expect(root).toHaveAttribute("data-legend-dock-mode", "bbox");
    // Anchored with an absolute top just BELOW the bbox bottom edge (400) + gap,
    // NOT the static `bottom: 16px` dock.
    expect(root.style.bottom).toBe("");
    expect(parseFloat(root.style.top)).toBe(aoiRect.bottom + DESKTOP_DOCK_BBOX_GAP_PX);
    // Centered on the bbox center X via translateX(-50%); left is an absolute px
    // (not the gutter `calc(50% ...)` of the bottom dock).
    expect(root.style.left.endsWith("px")).toBe(true);
    expect(root.style.transform).toContain("translateX(-50%)");
    // The key content still renders (only POSITION changed).
    expect(screen.getByTestId("grace2-layer-legend-key")).toBeInTheDocument();
    expect(screen.getByTestId("layer-legend-title")).toHaveTextContent(
      "Max flood depth (m)",
    );
  });

  // bbox mode with NO AOI rect falls back to the static bottom dock (never vanish).
  it("falls back to the static bottom dock when bbox mode has no AOI rect", () => {
    render(<LayerLegend layers={[makeLayer()]} />);
    const root = screen.getByTestId("grace2-layer-legend");
    expect(root).toHaveAttribute("data-legend-docked", "desktop");
    // No AOI -> bottom dock (the legacy placement), reported as the "bottom" mode.
    expect(root).toHaveAttribute("data-legend-dock-mode", "bottom");
    expect(root.style.bottom).toBe("16px");
  });

  // (2) DRAGGABLE -> a drag to the BOTTOM region snaps to the static bottom dock.
  it("dragging the strip to the bottom region parks it at the static bottom dock", () => {
    render(<LayerLegend layers={[makeLayer()]} aoiRect={aoiRect} />);
    const root = screen.getByTestId("grace2-layer-legend");
    // Starts bbox-anchored.
    expect(root).toHaveAttribute("data-legend-dock-mode", "bbox");
    // Grab the strip body (pointerDown at the origin so the pointer offset inside
    // the zeroed jsdom bbox is 0 -> the drop top == the move clientY) and drag the
    // pointer deep into the bottom band of the viewport (jsdom innerHeight is 768;
    // the snap band is the bottom 140px, so 760 is inside it).
    fireEvent.pointerDown(root, { clientX: 0, clientY: 0 });
    fireEvent.pointerMove(window, { clientX: 400, clientY: 760 });
    fireEvent.pointerUp(window);
    const after = screen.getByTestId("grace2-layer-legend");
    // It snapped to the STATIC bottom dock and stays there.
    expect(after).toHaveAttribute("data-legend-dock-mode", "bottom");
    expect(after.style.bottom).toBe("16px");
    expect(after.style.top).toBe("");
  });

  // A drag that releases HIGHER up re-anchors to the bbox (round-trip).
  it("dragging the strip back up re-anchors it to the bbox", () => {
    // Seed the bottom dock so we have somewhere to drag UP from.
    writeDesktopDockMode("bottom");
    render(<LayerLegend layers={[makeLayer()]} aoiRect={aoiRect} />);
    const root = screen.getByTestId("grace2-layer-legend");
    expect(root).toHaveAttribute("data-legend-dock-mode", "bottom");
    // Drag the strip up to the top of the viewport (above the bottom band).
    // pointerDown at the origin (zeroed jsdom bbox) so the drop top == clientY.
    fireEvent.pointerDown(root, { clientX: 0, clientY: 0 });
    fireEvent.pointerMove(window, { clientX: 400, clientY: 120 });
    fireEvent.pointerUp(window);
    const after = screen.getByTestId("grace2-layer-legend");
    expect(after).toHaveAttribute("data-legend-dock-mode", "bbox");
    expect(parseFloat(after.style.top)).toBe(aoiRect.bottom + DESKTOP_DOCK_BBOX_GAP_PX);
  });

  // (3) PERSISTS the bottom park across a remount (localStorage).
  it("the bottom-dock choice persists across a remount (localStorage)", () => {
    const { unmount } = render(<LayerLegend layers={[makeLayer()]} aoiRect={aoiRect} />);
    const root = screen.getByTestId("grace2-layer-legend");
    // Drag to the bottom -> "bottom" mode, persisted. pointerDown at the origin
    // (zeroed jsdom bbox) so the drop top == the move clientY (in the bottom band).
    fireEvent.pointerDown(root, { clientX: 0, clientY: 0 });
    fireEvent.pointerMove(window, { clientX: 400, clientY: 760 });
    fireEvent.pointerUp(window);
    expect(localStorage.getItem(LS_DESKTOP_LEGEND_DOCK)).toBe("bottom");
    unmount();
    // Remount: the persisted "bottom" preference restores (NOT the bbox default).
    render(<LayerLegend layers={[makeLayer()]} aoiRect={aoiRect} />);
    const remounted = screen.getByTestId("grace2-layer-legend");
    expect(remounted).toHaveAttribute("data-legend-dock-mode", "bottom");
    expect(remounted.style.bottom).toBe("16px");
  });

  // (4) a stored "bottom" preference restores on mount (even with an AOI on screen).
  it("restores a stored bottom-dock preference on mount", () => {
    writeDesktopDockMode("bottom");
    render(<LayerLegend layers={[makeLayer()]} aoiRect={aoiRect} />);
    const root = screen.getByTestId("grace2-layer-legend");
    // Despite the AOI rect (which would otherwise anchor to the bbox) the stored
    // bottom preference wins, so it stays parked at the bottom dock.
    expect(root).toHaveAttribute("data-legend-dock-mode", "bottom");
    expect(root.style.bottom).toBe("16px");
    expect(root.style.top).toBe("");
  });

  // The strip is the drag handle (cursor:grab); content/colorbar unchanged.
  it("the whole strip is grabbable and keeps the key/colorbar contents", () => {
    render(
      <LayerLegend
        layers={[makeLayer({ layer_id: "a" }), makeLayer({ layer_id: "b" })]}
        aoiRect={aoiRect}
      />,
    );
    const root = screen.getByTestId("grace2-layer-legend");
    expect(root.style.cursor).toBe("grab");
    // One docked root; both keys render as horizontal bottom desktop cards (the
    // content/colorbar render is unchanged - only position/drag changed).
    expect(screen.getAllByTestId("grace2-layer-legend")).toHaveLength(1);
    const keys = screen.getAllByTestId("grace2-layer-legend-key");
    expect(keys).toHaveLength(2);
    for (const key of keys) {
      expect(key.getAttribute("data-legend-orientation")).toBe("horizontal");
      expect(key.getAttribute("data-legend-side")).toBe("bottom");
    }
    expect(screen.getAllByTestId("layer-legend-bar")).toHaveLength(2);
  });

  // The bbox-anchored strip stays inside the viewport (does not run off-screen).
  it("clamps the bbox-anchored strip inside the viewport (does not run off-screen)", () => {
    // A bbox whose bottom edge is BELOW the viewport (jsdom innerHeight 768): the
    // anchored top must be clamped so the strip stays on-screen.
    const offscreen = { left: 200, top: 700, right: 600, bottom: 1200 };
    render(<LayerLegend layers={[makeLayer()]} aoiRect={offscreen} />);
    const root = screen.getByTestId("grace2-layer-legend");
    expect(root).toHaveAttribute("data-legend-dock-mode", "bbox");
    // The clamped top is well within the 768px viewport (not 1200 + gap).
    const top = parseFloat(root.style.top);
    expect(top).toBeLessThan(768);
    expect(top).toBeGreaterThan(0);
  });
});

// --- DESKTOP DRAGGABLE DOCK: pure helpers (NATE 2026-06-28) ------------------ //
describe("desktop dock mode helpers", () => {
  afterEach(() => {
    try {
      localStorage.removeItem(LS_DESKTOP_LEGEND_DOCK);
    } catch {
      /* non-fatal */
    }
  });

  it("desktopDockModeForDrop: bottom band -> 'bottom', higher -> 'bbox'", () => {
    const vh = 800;
    // A drop in the bottom band parks at the bottom dock.
    expect(desktopDockModeForDrop(vh - 10, vh)).toBe("bottom");
    expect(desktopDockModeForDrop(vh - DESKTOP_DOCK_BOTTOM_SNAP_BAND_PX, vh)).toBe(
      "bottom",
    );
    // A drop above the band re-anchors to the bbox.
    expect(
      desktopDockModeForDrop(vh - DESKTOP_DOCK_BOTTOM_SNAP_BAND_PX - 1, vh),
    ).toBe("bbox");
    expect(desktopDockModeForDrop(50, vh)).toBe("bbox");
    // Degenerate viewport height -> bbox (safe default).
    expect(desktopDockModeForDrop(100, 0)).toBe("bbox");
  });

  it("read/write round-trip persists the mode; default is 'bbox'", () => {
    try {
      localStorage.removeItem(LS_DESKTOP_LEGEND_DOCK);
    } catch {
      /* non-fatal */
    }
    // Default when unset.
    expect(readDesktopDockMode()).toBe("bbox");
    writeDesktopDockMode("bottom");
    expect(readDesktopDockMode()).toBe("bottom");
    expect(localStorage.getItem(LS_DESKTOP_LEGEND_DOCK)).toBe("bottom");
    writeDesktopDockMode("bbox");
    expect(readDesktopDockMode()).toBe("bbox");
  });
});

// ---------------------------------------------------------------------------
// DATA-DRIVEN LEGEND (the colormap KEY from the data) - render a layer's
// `LegendKey` directly: continuous from vmin/vmax+colormap, categorical as class
// swatches, and a vector layer (raster-only gate LIFTED) gets a key too. Run on
// DESKTOP so the assertions read the single docked strip (no portal/snap noise).
// ---------------------------------------------------------------------------
describe("LayerLegend  -  data-driven legend (LegendKey)", () => {
  beforeEach(() => {
    stubMatchMedia(false); // DESKTOP
  });

  it("renders a CONTINUOUS raster legend from vmin/vmax + colormap (the real range)", () => {
    // The producer emits the REAL p2/p98 range (0..7.2 m) + the semantic ramp; the
    // legend renders THAT range, not the preset's 0..3.5 guess. Title from the
    // legend label; units verbatim.
    render(
      <LayerLegend
        layers={[
          makeLayer({
            layer_id: "depth-legend",
            // A non-flood preset to prove the legend OVERRIDES the preset bounds.
            style_preset: "continuous_flood_depth",
            legend: {
              kind: "continuous",
              colormap: "blues",
              vmin: 0,
              vmax: 7.2,
              units: "m",
              label: "Surge depth",
            },
          }),
        ]}
      />,
    );
    expect(screen.getByTestId("layer-legend-title")).toHaveTextContent("Surge depth");
    // The numeric labels come from vmin/vmax (with the NBSP-joined unit), NOT the
    // preset's 0..3.5.
    expect(screen.getByTestId("layer-legend-min-label").textContent).toBe("0 m");
    expect(screen.getByTestId("layer-legend-max-label").textContent).toBe("7.2 m");
    // The gradient bar paints (continuous => bar, not swatches).
    expect(screen.getByTestId("layer-legend-bar")).toBeInTheDocument();
    expect(screen.queryByTestId("layer-legend-class")).toBeNull();
    // The bar uses the resolved "blues" ramp stops (a light->dark blue gradient).
    expect(screen.getByTestId("layer-legend-bar").style.background).toContain("linear-gradient");
  });

  it("renders a CATEGORICAL VECTOR legend as class swatches (raster-only gate LIFTED)", () => {
    // A Pelicun-shaped damage choropleth: a VECTOR layer with a categorical legend.
    // Before this change a vector layer got NO legend key; now it does.
    render(
      <LayerLegend
        layers={[
          makeLayer({
            layer_id: "pelicun-damage",
            layer_type: "vector", // <- vector, NOT raster
            style_preset: "pelicun_damage",
            legend: {
              kind: "categorical",
              value_field: "ds_mean",
              units: null,
              label: "Damage state",
              classes: [
                { value_min: 0, value_max: 1, color: "#2DC937", label: "None" },
                { value_min: 1, value_max: 2, color: "#E7B416", label: "Slight" },
                { value_min: 3, value_max: 4, color: "#CC3232", label: "Complete" },
              ],
            },
          }),
        ]}
      />,
    );
    // The key renders (the gate is lifted for a legend-bearing vector layer).
    expect(screen.getByTestId("grace2-layer-legend-key")).toBeInTheDocument();
    expect(screen.getByTestId("layer-legend-title")).toHaveTextContent("Damage state");
    // One swatch row per class, each with its color + label, and NO gradient bar.
    const classes = screen.getAllByTestId("layer-legend-class");
    expect(classes).toHaveLength(3);
    expect(screen.queryByTestId("layer-legend-bar")).toBeNull();
    const swatches = screen.getAllByTestId("layer-legend-swatch");
    expect(swatches[0]!.style.background).toBe("#2DC937");
    expect(within(classes[2]!).getByText("Complete")).toBeInTheDocument();
  });

  it("PRESENT-ONLY: a categorical NLCD legend hides achromatic-grey (absent) filler classes", () => {
    // A paletted NLCD raster's color table materializes unused indices as a
    // neutral-grey filler ramp; those rows are classes NOT present in the rendered
    // raster and must be dropped so the legend lists only the colored (present)
    // classes. Two chromatic NLCD classes + three grey fillers => only the two
    // chromatic rows render, with their colors intact.
    render(
      <LayerLegend
        layers={[
          makeLayer({
            layer_id: "nlcd-landcover",
            layer_type: "raster",
            style_preset: "categorical_landcover",
            legend: {
              kind: "categorical",
              label: "Land cover",
              units: null,
              classes: [
                { value: 11, color: "#486DA2", label: "11" }, // Open Water (chromatic)
                { value: 41, color: "#38814E", label: "41" }, // Forest (chromatic)
                { value: 96, color: "#606060", label: "96" }, // grey filler (absent)
                { value: 97, color: "#616161", label: "97" }, // grey filler (absent)
                { value: 98, color: "#ffffff", label: "98" }, // achromatic filler
              ],
            },
          }),
        ]}
      />,
    );
    const classes = screen.getAllByTestId("layer-legend-class");
    expect(classes).toHaveLength(2);
    const swatches = screen.getAllByTestId("layer-legend-swatch");
    expect(swatches[0]!.style.background).toBe("#486DA2");
    expect(swatches[1]!.style.background).toBe("#38814E");
    // The greyed/absent rows are gone.
    expect(screen.queryByText("96")).toBeNull();
    expect(screen.queryByText("97")).toBeNull();
    expect(screen.queryByText("98")).toBeNull();
  });

  it("NARROW WIDTH: a categorical land-cover key shrinks to content (no wide empty gutter)", () => {
    // NATE 2026-06-29: the land-cover legend used to inherit the wide AOI-sized
    // colorbar width, leaving a big empty gutter beside the short swatch+label
    // rows. A categorical key must size to its content (fit-content) capped at the
    // narrow dock width, NOT the full colorbar width.
    render(
      <LayerLegend
        layers={[
          makeLayer({
            layer_id: "nlcd-narrow",
            layer_type: "raster",
            style_preset: "categorical_landcover",
            legend: {
              kind: "categorical",
              label: "Land cover",
              units: null,
              classes: [
                { value: 11, color: "#486DA2", label: "Open Water" },
                { value: 41, color: "#38814E", label: "Deciduous Forest" },
              ],
            },
          }),
        ]}
      />,
    );
    const card = screen.getByTestId("grace2-layer-legend-key");
    expect(card.style.width).toBe("fit-content");
    expect(card.style.maxWidth).toBe("200px");
  });

  it("DESKTOP categorical lays its swatch chips out HORIZONTALLY (sibling of the bottom-docked colorbar)", () => {
    // PARITY (NATE 2026-06-29): the desktop strip docks BELOW the bbox (a horizontal
    // edge), so the categorical chips flow as a horizontal wrapping ROW - mirroring
    // the colorbar's horizontal [min] bar [max] - NOT a tall vertical column.
    render(
      <LayerLegend
        layers={[
          makeLayer({
            layer_id: "nlcd-horiz",
            layer_type: "raster",
            style_preset: "categorical_landcover",
            legend: {
              kind: "categorical",
              label: "Land cover",
              units: null,
              classes: [
                { value: 11, color: "#486DA2", label: "Open Water" },
                { value: 41, color: "#38814E", label: "Deciduous Forest" },
                { value: 81, color: "#DCD93D", label: "Pasture/Hay" },
              ],
            },
          }),
        ]}
      />,
    );
    const key = screen.getByTestId("grace2-layer-legend-key");
    // The desktop categorical card reads as a HORIZONTAL key, like the colorbar.
    expect(key.getAttribute("data-legend-orientation")).toBe("horizontal");
    const valueRow = within(key).getByTestId("layer-legend-value-row");
    // Chips flow in a wrapping ROW (horizontal), not a column.
    expect(valueRow.style.flexDirection).toBe("row");
    expect(valueRow.style.flexWrap).toBe("wrap");
    expect(screen.getAllByTestId("layer-legend-class")).toHaveLength(3);
    // Still narrow (fit-content, capped) - the prior pass's narrow sizing is kept.
    expect(key.style.width).toBe("fit-content");
  });

  it("DESKTOP categorical NEVER docks OVER the bbox - it sits just below the bbox bottom edge", () => {
    // OUTSIDE-THE-BBOX (NATE 2026-06-29): the strip top must be >= the bbox bottom
    // edge + gap, even for a tall many-class legend (the old height clamp used to
    // pull a tall strip UP over the bbox). A continuous colorbar already does this;
    // the categorical must too.
    const aoiRect = { left: 200, top: 100, right: 600, bottom: 400 };
    const manyClasses = Array.from({ length: 16 }, (_, i) => ({
      value: i,
      color: `#${(0x2266aa + i * 0x050505).toString(16).padStart(6, "0")}`,
      label: `Class ${i}`,
    }));
    render(
      <LayerLegend
        layers={[
          makeLayer({
            layer_id: "nlcd-tall",
            layer_type: "raster",
            style_preset: "categorical_landcover",
            legend: { kind: "categorical", label: "Land cover", units: null, classes: manyClasses },
          }),
        ]}
        aoiRect={aoiRect}
      />,
    );
    const root = screen.getByTestId("grace2-layer-legend");
    // The strip top is BELOW the bbox bottom edge (outside the bbox), never above it.
    expect(parseFloat(root.style.top)).toBeGreaterThanOrEqual(
      aoiRect.bottom + DESKTOP_DOCK_BBOX_GAP_PX,
    );
    expect(root.style.bottom).toBe("");
  });

  it("PRESENT-ONLY: keeps a near-neutral REAL class (Barren Land, chroma ~16) and never blanks an all-grey legend", () => {
    // Barren Land (#B3AFA3) is the least-saturated standard NLCD class; it must
    // survive the grey filter. And if EVERY row reads grey, the key keeps them all
    // (never hide the whole legend).
    render(
      <LayerLegend
        layers={[
          makeLayer({
            layer_id: "nlcd-barren",
            layer_type: "raster",
            style_preset: "categorical_landcover",
            legend: {
              kind: "categorical",
              label: "Land cover",
              units: null,
              classes: [
                { value: 31, color: "#B3AFA3", label: "31" }, // Barren (kept)
                { value: 96, color: "#707070", label: "96" }, // grey (dropped)
              ],
            },
          }),
          makeLayer({
            layer_id: "all-grey",
            layer_type: "raster",
            style_preset: "categorical_landcover",
            legend: {
              kind: "categorical",
              label: "All grey",
              units: null,
              classes: [
                { value: 1, color: "#404040", label: "1" },
                { value: 2, color: "#808080", label: "2" },
              ],
            },
          }),
        ]}
      />,
    );
    // Barren survives, its grey sibling is dropped -> 1 row; the all-grey key keeps
    // BOTH rows (never blanked) -> 2 rows. Total 3 class rows across the two keys.
    expect(screen.getAllByTestId("layer-legend-class")).toHaveLength(3);
    expect(screen.getByText("31")).toBeInTheDocument();
    expect(screen.queryByText("96")).toBeNull();
  });

  it("LEGACY (no legend): a raster layer renders EXACTLY as before via the preset path", () => {
    // The honesty-floor / backward-compat guard: an unchanged layer (no legend) keeps
    // the preset title + the preset 0..3.5 m bounds (NOT a data-driven override).
    render(<LayerLegend layers={[makeLayer({ legend: null })]} />);
    expect(screen.getByTestId("layer-legend-title")).toHaveTextContent(
      "Max flood depth (m)",
    );
    expect(screen.getByTestId("layer-legend-min-label").textContent).toBe("0 m");
    expect(screen.getByTestId("layer-legend-max-label").textContent).toBe("3.5 m");
    // Still a gradient bar (the preset continuous render), no class swatches.
    expect(screen.getByTestId("layer-legend-bar")).toBeInTheDocument();
    expect(screen.queryByTestId("layer-legend-class")).toBeNull();
  });

  it("LEGACY (no legend): a vector layer still gets NO legend key (gate only lifts WITH a legend)", () => {
    // A plain vector layer (no legend) must NOT suddenly grow a key - the gate only
    // lifts for legend-bearing vectors, so non-legend vectors are unchanged.
    render(
      <LayerLegend
        layers={[makeLayer({ layer_id: "v", layer_type: "vector", style_preset: "wdpa_polygon" })]}
      />,
    );
    expect(screen.queryByTestId("grace2-layer-legend-key")).toBeNull();
  });
});

// CATEGORICAL PARITY WITH THE COLORBAR (NATE 2026-06-29) - the categorical
// (land-cover) key must use the EXACT SAME snap-to-bbox-edge + edge-aware
// orientation path as the continuous colorbar key, sitting OUTSIDE the bbox on the
// snapped edge (horizontal on top/bottom, vertical on left/right). The ONLY
// difference between the two is the CONTENT (swatch+label chips vs a gradient bar).
// The global beforeEach stubs mobile=true (the snap pipeline is mobile-only).
describe("LayerLegend  -  categorical key has PARITY with the colorbar key", () => {
  const aoiRect = { left: 100, top: 100, right: 500, bottom: 300 };
  function categoricalLayer(id = "nlcd-m"): ProjectLayerSummary {
    return makeLayer({
      layer_id: id,
      layer_type: "raster",
      style_preset: "categorical_landcover",
      legend: {
        kind: "categorical",
        label: "Land cover",
        units: null,
        classes: [
          { value: 11, color: "#486DA2", label: "Open Water" },
          { value: 41, color: "#38814E", label: "Deciduous Forest" },
        ],
      },
    });
  }
  function activateScrubber(): void {
    const c = getAnimationController();
    c.setGroups([
      { key: "seq-cat", label: "x", layerIds: ["f01", "f03"], frameLabels: ["F+01h", "F+03h"] },
    ]);
    c.setActiveGroup("seq-cat");
  }

  it("BOTTOM edge: categorical AND continuous both snap bottom + render HORIZONTAL (only content differs)", () => {
    const { rerender } = render(<LayerLegend layers={[makeLayer()]} aoiRect={aoiRect} />);
    let key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-side")).toBe("bottom");
    expect(key.getAttribute("data-legend-orientation")).toBe("horizontal");

    rerender(<LayerLegend layers={[categoricalLayer()]} aoiRect={aoiRect} />);
    key = screen.getByTestId("grace2-layer-legend-key");
    // SAME side + orientation as the colorbar - the parity requirement.
    expect(key.getAttribute("data-legend-side")).toBe("bottom");
    expect(key.getAttribute("data-legend-orientation")).toBe("horizontal");
    // Categorical content flows HORIZONTALLY on a horizontal edge (row, wraps).
    const list = within(key).getByTestId("layer-legend-class-list");
    expect(list.style.flexDirection).toBe("row");
    expect(list.style.flexWrap).toBe("wrap");
    // Swatch chips (not a gradient bar) - the only difference from the colorbar.
    expect(within(key).queryByTestId("layer-legend-bar")).toBeNull();
    expect(within(key).getAllByTestId("layer-legend-class")).toHaveLength(2);
  });

  it("RIGHT edge (scrubber active): categorical AND continuous both snap right + render VERTICAL", () => {
    activateScrubber();
    const { rerender } = render(
      <LayerLegend layers={[makeLayer({ layer_id: "cont", name: "Surge" })]} aoiRect={aoiRect} />,
    );
    let key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-side")).toBe("right");
    expect(key.getAttribute("data-legend-orientation")).toBe("vertical");

    rerender(<LayerLegend layers={[categoricalLayer("cat-r")]} aoiRect={aoiRect} />);
    key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-side")).toBe("right");
    expect(key.getAttribute("data-legend-orientation")).toBe("vertical");
    // Categorical content flows VERTICALLY on a vertical edge (a column).
    const list = within(key).getByTestId("layer-legend-class-list");
    expect(list.style.flexDirection).toBe("column");
  });

  it("the categorical legend rect NEVER intersects the bbox (sits OUTSIDE the snapped edge)", () => {
    // BOTTOM edge: the card top is at/below the bbox bottom edge, so the whole card
    // is BELOW the bbox - zero overlap with the bbox rectangle.
    const { rerender } = render(
      <LayerLegend layers={[categoricalLayer("cat-b")]} aoiRect={aoiRect} />,
    );
    let key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-side")).toBe("bottom");
    expect(parseFloat(key.style.top)).toBeGreaterThanOrEqual(aoiRect.bottom);

    // RIGHT edge (scrubber active): the card left is at/right of the bbox right edge,
    // so the whole card is to the RIGHT of the bbox - outside it.
    activateScrubber();
    rerender(<LayerLegend layers={[categoricalLayer("cat-r2")]} aoiRect={aoiRect} />);
    key = screen.getByTestId("grace2-layer-legend-key");
    expect(key.getAttribute("data-legend-side")).toBe("right");
    expect(parseFloat(key.style.left)).toBeGreaterThanOrEqual(aoiRect.right);
  });
});
