// GRACE-2 web - bbox progress state-machine + settings unit tests (NATE item 1).

import { describe, it, expect, beforeEach } from "vitest";
import {
  resolveBboxProgress,
  readBboxAnimationsEnabled,
  writeBboxAnimationsEnabled,
  isPipelineRunning,
  LS_BBOX_ANIM,
  type BboxProgressSignals,
} from "./bbox_progress";

// A signals baseline: a bbox is on screen, nothing else happening, anim enabled.
const BASE: BboxProgressSignals = {
  hasBbox: true,
  layerCount: 0,
  layersLoading: false,
  connecting: false,
  simRunning: false,
  animationsEnabled: true,
};

describe("resolveBboxProgress - GRID-ONLY, zero-layers-gated (NATE 2026-06-24)", () => {
  it("renders nothing when there is no bbox anchor", () => {
    const s = resolveBboxProgress({ ...BASE, hasBbox: false, layersLoading: true });
    expect(s.mode).toBe("none");
  });

  it("FIRST fetch (loading, ZERO layers yet) -> FILL grid (the ONLY visual)", () => {
    const s = resolveBboxProgress({ ...BASE, layersLoading: true, layerCount: 0 });
    expect(s.mode).toBe("fill");
    expect(s.tone).toBe("blue");
    expect(s.toggleExempt).toBe(false);
  });

  // CORE NATE ASK: the grid appears ONLY when there are TRULY zero layers. The
  // instant a layer is present, NOTHING animates - even while still loading.
  it("loading but >=1 layer already present -> NONE (zero-layers gate)", () => {
    expect(
      resolveBboxProgress({ ...BASE, layersLoading: true, layerCount: 1 }).mode,
    ).toBe("none");
    expect(
      resolveBboxProgress({ ...BASE, layersLoading: true, layerCount: 5 }).mode,
    ).toBe("none");
  });

  // The SCAN is GONE: connecting no longer paints a box animation on its own.
  it("CONNECTING no longer produces any box animation (scan removed)", () => {
    expect(
      resolveBboxProgress({ ...BASE, connecting: true, layerCount: 0 }).mode,
    ).toBe("none");
    expect(
      resolveBboxProgress({ ...BASE, connecting: true, layerCount: 2 }).mode,
    ).toBe("none");
  });

  // A running sim no longer animates the box either (scan removed).
  it("a running SIM no longer produces any box animation (scan removed)", () => {
    expect(
      resolveBboxProgress({ ...BASE, simRunning: true, layerCount: 4 }).mode,
    ).toBe("none");
    // Even with zero layers + loading, the sim flag does not turn it purple/scan;
    // it is the plain loading grid (zero layers + loading) at most.
    expect(
      resolveBboxProgress({ ...BASE, simRunning: true, layerCount: 4, layersLoading: true })
        .mode,
    ).toBe("none");
  });

  // The resolver NEVER returns "scan" anymore (proves the scan path is dead).
  it("NEVER returns mode 'scan' for any signal combination", () => {
    const combos: Array<Partial<BboxProgressSignals>> = [
      { connecting: true },
      { simRunning: true },
      { connecting: true, simRunning: true },
      { layersLoading: true, layerCount: 0 },
      { layersLoading: true, layerCount: 3 },
      { connecting: true, layerCount: 0 },
      { connecting: true, animationsEnabled: false },
    ];
    for (const c of combos) {
      expect(resolveBboxProgress({ ...BASE, ...c }).mode).not.toBe("scan");
    }
  });

  it("DISABLED toggle suppresses the grid (no connecting/sim exception now)", () => {
    expect(
      resolveBboxProgress({
        ...BASE,
        layersLoading: true,
        layerCount: 0,
        animationsEnabled: false,
      }).mode,
    ).toBe("none");
    // Connecting used to be toggle-exempt; the scan is gone, so disabling is total.
    expect(
      resolveBboxProgress({
        ...BASE,
        connecting: true,
        layerCount: 0,
        animationsEnabled: false,
      }).mode,
    ).toBe("none");
  });

  it("idle (bbox present, nothing loading, no layers) -> none", () => {
    expect(resolveBboxProgress({ ...BASE }).mode).toBe("none");
  });

  // 3D terrain: the 2D grid is suppressed (the in-map AOI line stays statically
  // visible instead). The grid never paints misaligned over a pitched box.
  it("3D terrain suppresses the 2D grid for the loading state (-> none)", () => {
    expect(
      resolveBboxProgress({ ...BASE, terrain3d: true, layersLoading: true, layerCount: 0 })
        .mode,
    ).toBe("none");
  });

  // suppressLoadingReplay is now subsumed by the zero-layers gate: any
  // layers-present context is a no-show regardless of the replay flag.
  it("layers-present is a no-show whether or not replay-suppress is set", () => {
    expect(
      resolveBboxProgress({
        ...BASE,
        layersLoading: true,
        layerCount: 3,
        suppressLoadingReplay: true,
      }).mode,
    ).toBe("none");
    expect(
      resolveBboxProgress({
        ...BASE,
        layersLoading: true,
        layerCount: 3,
        suppressLoadingReplay: false,
      }).mode,
    ).toBe("none");
  });

  it("still shows the grid on a genuine first fetch even when replay-suppress is set", () => {
    // No layers yet => the first-fetch grid must still show.
    expect(
      resolveBboxProgress({
        ...BASE,
        layersLoading: true,
        layerCount: 0,
        suppressLoadingReplay: true,
      }).mode,
    ).toBe("fill");
  });
});

describe("bbox-animation settings persistence (default ON)", () => {
  beforeEach(() => {
    try {
      localStorage.clear();
    } catch {
      /* ignore */
    }
  });

  it("defaults to ON when nothing is persisted", () => {
    expect(readBboxAnimationsEnabled()).toBe(true);
  });

  it("persists + reads back false", () => {
    writeBboxAnimationsEnabled(false);
    expect(localStorage.getItem(LS_BBOX_ANIM)).toBe("false");
    expect(readBboxAnimationsEnabled()).toBe(false);
  });

  it("persists + reads back true", () => {
    writeBboxAnimationsEnabled(false);
    writeBboxAnimationsEnabled(true);
    expect(readBboxAnimationsEnabled()).toBe(true);
  });

  it("treats any non-'false' value as enabled (default-ON bias)", () => {
    localStorage.setItem(LS_BBOX_ANIM, "garbage");
    expect(readBboxAnimationsEnabled()).toBe(true);
  });
});

describe("isPipelineRunning - long-running-sim signal", () => {
  it("false for null / non-object", () => {
    expect(isPipelineRunning(null)).toBe(false);
    expect(isPipelineRunning(undefined)).toBe(false);
    expect(isPipelineRunning("x")).toBe(false);
  });

  it("true when a step is running and the pipeline has not terminated", () => {
    expect(
      isPipelineRunning({
        steps: [{ state: "complete" }, { state: "running" }],
      }),
    ).toBe(true);
  });

  it("false when the pipeline has a terminal final_state", () => {
    expect(
      isPipelineRunning({
        final_state: "complete",
        steps: [{ state: "running" }],
      }),
    ).toBe(false);
  });

  it("false when no step is running", () => {
    expect(
      isPipelineRunning({ steps: [{ state: "pending" }, { state: "complete" }] }),
    ).toBe(false);
  });

  it("false when steps is missing / not an array", () => {
    expect(isPipelineRunning({})).toBe(false);
    expect(isPipelineRunning({ steps: "x" })).toBe(false);
  });
});
