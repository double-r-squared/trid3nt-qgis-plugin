// GRACE-2 web - frame preload + hold-until-loaded swap unit tests (NATE item 2).

import { describe, it, expect } from "vitest";
import {
  framesToWarm,
  releaseWarmedFrames,
  swapFrameWithHold,
  type FrameMapAdapter,
} from "./frame_preload";

// A fake map adapter that records every call so we can assert the warm/swap/hold.
function makeAdapter(opts: { sourceLoaded?: boolean } = {}) {
  const visibility = new Map<string, boolean>();
  const opacity = new Map<string, number>();
  let settledCb: (() => void) | null = null;
  const adapter: FrameMapAdapter = {
    hasLayer: () => true,
    setVisibility: (id, v) => visibility.set(id, v),
    setOpacity: (id, o) => opacity.set(id, o),
    isSourceLoaded: () => opts.sourceLoaded ?? true,
    onceSourceSettled: (cb) => {
      settledCb = cb;
    },
  };
  return {
    adapter,
    visibility,
    opacity,
    fireSettle: () => settledCb?.(),
    hasPendingSettle: () => settledCb !== null,
  };
}

const FRAMES = ["f0", "f1", "f2"];

describe("framesToWarm", () => {
  it("warms every (valid) frame id", () => {
    expect(framesToWarm(FRAMES)).toEqual(["f0", "f1", "f2"]);
  });
  it("drops empty / non-string ids", () => {
    expect(framesToWarm(["a", "", "b"])).toEqual(["a", "b"]);
  });
});

describe("swapFrameWithHold - preload + hold swap", () => {
  it("makes EVERY frame visible (warms tiles)", () => {
    const { adapter, visibility } = makeAdapter();
    swapFrameWithHold(adapter, FRAMES, 1);
    expect(visibility.get("f0")).toBe(true);
    expect(visibility.get("f1")).toBe(true);
    expect(visibility.get("f2")).toBe(true);
  });

  it("raises the target to opacity 1 and dims non-target/non-held to 0", () => {
    const { adapter, opacity } = makeAdapter();
    // No prev target: f1 -> 1, the others -> 0 immediately.
    swapFrameWithHold(adapter, FRAMES, 1);
    expect(opacity.get("f1")).toBe(1);
    expect(opacity.get("f0")).toBe(0);
    expect(opacity.get("f2")).toBe(0);
  });

  it("HOLDS the previous frame until the target source loads (no black gap)", () => {
    const { adapter, opacity, fireSettle, hasPendingSettle } = makeAdapter({
      sourceLoaded: false,
    });
    // Stepping f0 -> f1 with f0 as the held previous frame and target not loaded.
    swapFrameWithHold(adapter, FRAMES, 1, "f0");
    // f1 raised immediately; f0 (held) NOT yet dimmed (held underneath).
    expect(opacity.get("f1")).toBe(1);
    expect(opacity.get("f0")).not.toBe(0);
    expect(hasPendingSettle()).toBe(true);
    // Once the target's tiles settle, the held frame is dimmed.
    fireSettle();
    expect(opacity.get("f0")).toBe(0);
  });

  it("dims the held frame synchronously when the target is already loaded", () => {
    const { adapter, opacity, hasPendingSettle } = makeAdapter({
      sourceLoaded: true,
    });
    swapFrameWithHold(adapter, FRAMES, 2, "f1");
    expect(opacity.get("f2")).toBe(1);
    expect(opacity.get("f1")).toBe(0); // dimmed right away
    expect(hasPendingSettle()).toBe(false); // no deferral needed
  });

  it("returns the new target so the caller can thread it forward", () => {
    const { adapter } = makeAdapter();
    const r = swapFrameWithHold(adapter, FRAMES, 1, "f0");
    expect(r.target).toBe("f1");
  });

  it("re-step to the SAME frame keeps it visible (no dim of itself)", () => {
    const { adapter, opacity } = makeAdapter();
    const r = swapFrameWithHold(adapter, FRAMES, 1, "f1");
    expect(opacity.get("f1")).toBe(1);
    expect(r.target).toBe("f1");
  });

  it("out-of-range index is a no-op for the target (returns prior held)", () => {
    const { adapter } = makeAdapter();
    const r = swapFrameWithHold(adapter, FRAMES, 9, "f1");
    expect(r.target).toBe("f1");
  });

  it("skips a missing target layer gracefully", () => {
    const visibility = new Map<string, boolean>();
    const adapter: FrameMapAdapter = {
      hasLayer: (id) => id !== "f1", // f1 is missing
      setVisibility: (id, v) => visibility.set(id, v),
      setOpacity: () => {},
      isSourceLoaded: () => true,
      onceSourceSettled: () => {},
    };
    const r = swapFrameWithHold(adapter, FRAMES, 1, null);
    expect(r.target).toBeNull(); // target missing -> no crash, no target
  });
});

// BUG 1 (memory crash): releaseWarmedFrames flips out-of-window warmed frames to
// visibility:none so MapLibre frees their SourceCache/textures (the warm path
// only ever DIMS them to opacity 0 and leaves them visible -> OOM on a 144/288-
// frame sweep). The window around the kept frame stays warmed-but-hidden so an
// adjacent step is still gap-free.
const MANY = ["f0", "f1", "f2", "f3", "f4", "f5"];

describe("releaseWarmedFrames - free out-of-window warmed frames (bug 1)", () => {
  it("sets visibility:none on every frame OUTSIDE the keep window", () => {
    const { adapter, visibility } = makeAdapter();
    // Keep index 3, window 1 -> {2,3,4} stay visible; {0,1,5} -> visibility:none.
    releaseWarmedFrames(adapter, MANY, 3, 1);
    expect(visibility.get("f0")).toBe(false);
    expect(visibility.get("f1")).toBe(false);
    expect(visibility.get("f5")).toBe(false);
    // In-window frames are kept renderable (visible) so an adjacent step is gap-free.
    expect(visibility.get("f2")).toBe(true);
    expect(visibility.get("f3")).toBe(true);
    expect(visibility.get("f4")).toBe(true);
  });

  it("keeps the shown frame at opacity 1 and the rest of the window at opacity 0", () => {
    const { adapter, opacity } = makeAdapter();
    releaseWarmedFrames(adapter, MANY, 3, 1);
    // The kept (shown) frame is NOT dimmed (left as-is); its window neighbors are
    // explicitly dimmed to opacity 0 (warmed-but-hidden).
    expect(opacity.get("f2")).toBe(0);
    expect(opacity.get("f4")).toBe(0);
    expect(opacity.get("f3")).toBeUndefined(); // shown frame untouched here
  });

  it("releases ALL frames when keepIndex is out of range (stop/teardown)", () => {
    const { adapter, visibility } = makeAdapter();
    // keepIndex -1 (used on group-change/reset) => nothing is in-window => every
    // frame is flipped to visibility:none (full release).
    releaseWarmedFrames(adapter, MANY, -1);
    for (const id of MANY) expect(visibility.get(id)).toBe(false);
  });

  it("skips frames whose layer is missing (race-tolerant)", () => {
    const visibility = new Map<string, boolean>();
    const adapter: FrameMapAdapter = {
      hasLayer: (id) => id !== "f0", // f0 not on the style
      setVisibility: (id, v) => visibility.set(id, v),
      setOpacity: () => {},
      isSourceLoaded: () => true,
      onceSourceSettled: () => {},
    };
    releaseWarmedFrames(adapter, MANY, -1);
    expect(visibility.has("f0")).toBe(false); // never touched (missing layer)
    expect(visibility.get("f5")).toBe(false); // others released
  });
});
