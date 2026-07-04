// GRACE-2 web - JOB WEB-ANIM (#157.1-.3) integration tests.
//
// The keystone behaviour NATE reported broken on mobile: closing the Layers
// panel KILLED the animation and dropped the scrubber, because the playback
// state + interval + scrubber all lived inside LayerPanel. After the fix:
//   #157.1 - playback (the `playing` flag + the advance interval) lives in the
//            module-level AnimationController and KEEPS RUNNING across a
//            LayerPanel unmount; the controller drives frame visibility via an
//            emitter (Map.tsx in prod) independent of the panel.
//   #157.2 - the scrubber renders WHENEVER a sequence is active on the
//            controller, regardless of whether the Layers panel is open.
//   #157.3 - the scrubber carries its own play/pause button wired to the
//            controller's playing state.
//
// These tests compose the SAME pieces App.tsx wires (LayerPanel as a control +
// an App-owned SequenceScrubber driven by the shared controller), so they
// exercise the real cross-component contract without booting the full App shell
// (WS / auth / map). The App-internal AppSequenceScrubber is mirrored here as a
// tiny harness with identical wiring.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, act, cleanup } from "@testing-library/react";
import { useMemo } from "react";
import { LayerPanel } from "./LayerPanel";
import { SequenceScrubber } from "./components/SequenceScrubber";
import {
  AnimationController,
  setAnimationController,
  getAnimationController,
  type AnimTimers,
  type FrameVisibilityEmitter,
} from "./lib/animation_controller";
import { useAnimationState } from "./lib/use_animation_controller";
import { LayerCache, setLayerCache } from "./lib/layer_cache";
import type { ProjectLayerSummary } from "./contracts";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// App.tsx boots the full shell (WS / auth / map) and cannot mount in happy-dom,
// so the App-internal sheet-top derivation is asserted at the source level (the
// same CHAT_SRC pattern Chat tests use). MEASURED-TOP (NATE 2026-06-27).
const APP_SRC = readFileSync(resolve("src/App.tsx"), "utf8");

// A controllable fake-timer seam so we can fire the controller's advance tick
// deterministically without real wall-clock time.
//
// ITEM 5 (NATE 2026-06-22): setGroups now AUTO-PLAYS a freshly-loaded multi-frame
// group. The existing scrubber-CONTROL tests assert the manual play/pause +
// stepping mechanics from a PAUSED baseline, so they install the controller with
// reduced-motion ON (auto-play suppressed). The dedicated item-5 block at the
// bottom installs it with reduced-motion OFF to verify the auto-play + first-
// frame default.
let fireTick: () => void = () => {};
function installFakeTimerController(
  reducedMotion = true,
  autoPlay = false,
): AnimationController {
  let cb: (() => void) | null = null;
  const timers: AnimTimers = {
    setInterval: (fn) => {
      cb = fn;
      return 1;
    },
    clearInterval: () => {
      cb = null;
    },
  };
  fireTick = () => cb?.();
  const c = new AnimationController({
    timers,
    prefersReducedMotion: () => reducedMotion,
    // AUTOPLAY-OFF (NATE 2026-06-24): auto-play is opt-in; default off so the
    // scrubber appears showing frame 0, paused, until the user presses play.
    autoPlay,
  });
  setAnimationController(c);
  return c;
}

const noopBackend = {
  async load() {
    return {};
  },
  async save() {
    /* no-op */
  },
};

beforeEach(() => {
  cleanup();
  setLayerCache(new LayerCache({ backend: noopBackend }));
  try {
    localStorage.clear();
  } catch {
    /* ignore */
  }
});

function makeFrame(hour: number): ProjectLayerSummary {
  const hh = String(hour).padStart(2, "0");
  return {
    layer_id: `run-a-f${hh}`,
    name: `HRRR precip F+${hh}h`,
    layer_type: "raster",
    uri: `s3://grace-2/runs/run-a/precip_f${hh}.cog.tif`,
    visible: true,
    opacity: 1,
    z_index: 1,
    style_preset: "hrrr_precip",
  };
}

const FRAMES = [makeFrame(1), makeFrame(3), makeFrame(6)];

// Mirror of App.tsx's AppSequenceScrubber: render the scrubber from the shared
// controller whenever a group is active, independent of the LayerPanel.
//
// ITEM 2 (NATE 2026-06-23): App passes `hidden={isMobile && mobileDrawerOpen}`
// so the scrubber (a MAP overlay) does NOT float over the full-screen mobile
// Layers drawer's rows. The harness mirrors that gate.
function AppScrubberHarness({
  hidden = false,
  // TASK E (NATE 2026-06-26): App threads the chat sheet's live top edge (px) so
  // the MOBILE scrubber docks to it; mirror that pass-through here.
  sheetTopPx = null,
}: {
  hidden?: boolean;
  sheetTopPx?: number | null;
}): JSX.Element | null {
  const controller = useMemo(() => getAnimationController(), []);
  const anim = useAnimationState(controller);
  const active =
    anim.activeGroupKey != null
      ? anim.groups.find((g) => g.key === anim.activeGroupKey) ?? null
      : null;
  if (!active) return null;
  if (hidden) return null;
  return (
    <SequenceScrubber
      label={active.label}
      frameLabels={active.frameLabels}
      activeIndex={controller.frameIndexFor(active.key)}
      onStep={(idx) => controller.stepGroupTo(active.key, idx)}
      playing={anim.playing}
      sheetTopPx={sheetTopPx}
      onPlayToggle={() => {
        controller.setActiveGroup(active.key);
        controller.togglePlaying();
      }}
    />
  );
}

describe("JOB WEB-ANIM #157.2 - scrubber renders whenever a sequence animates", () => {
  beforeEach(() => {
    installFakeTimerController();
  });

  it("renders the scrubber when a sequence is active even with the panel CLOSED", () => {
    // Render ONLY the App-owned scrubber harness (no LayerPanel = panel closed),
    // then push a group into the controller as LayerPanel would on mount.
    render(<AppScrubberHarness />);
    // No group yet -> no scrubber.
    expect(screen.queryByTestId("grace2-sequence-scrubber")).toBeNull();
    act(() => {
      getAnimationController().setGroups([
        {
          key: "grp",
          label: "HRRR precip",
          layerIds: ["run-a-f01", "run-a-f03", "run-a-f06"],
          frameLabels: ["F+01h", "F+03h", "F+06h"],
        },
      ]);
    });
    // Now the scrubber appears - panel never mounted.
    expect(screen.getByTestId("grace2-sequence-scrubber")).toBeInTheDocument();
  });

  it("keeps the scrubber mounted after the LayerPanel unmounts (panel close)", () => {
    // Mount the panel (detects + pushes the group) alongside the App scrubber.
    const panel = render(<LayerPanel initialLayers={FRAMES} />);
    render(<AppScrubberHarness />);
    expect(screen.getByTestId("grace2-sequence-scrubber")).toBeInTheDocument();
    // Close the panel (unmount) - the scrubber must stay because it is driven by
    // the controller, not by the panel's lifetime.
    act(() => {
      panel.unmount();
    });
    expect(screen.getByTestId("grace2-sequence-scrubber")).toBeInTheDocument();
  });
});

describe("JOB WEB-ANIM #157.1 - playback survives a LayerPanel unmount", () => {
  beforeEach(() => {
    installFakeTimerController();
  });

  it("keeps PLAYING + advancing frames after the panel unmounts", () => {
    const emitted: number[] = [];
    const emitter: FrameVisibilityEmitter = (_ids, idx) => emitted.push(idx);
    getAnimationController().setEmitter(emitter);

    // Panel mounts, detects the group, seeds the default (last) frame.
    const panel = render(<LayerPanel initialLayers={FRAMES} />);
    const ctrl = getAnimationController();
    expect(ctrl.getActiveGroup()).not.toBeNull();

    // Start from frame 0 and begin playback (as the play button would).
    act(() => {
      ctrl.stepGroupTo(ctrl.getActiveGroup()!.key, 0);
      ctrl.setPlaying(true);
    });
    expect(ctrl.isPlaying()).toBe(true);
    emitted.length = 0;

    // Close the Layers panel - the keystone scenario.
    act(() => {
      panel.unmount();
    });

    // The controller is STILL playing after unmount.
    expect(ctrl.isPlaying()).toBe(true);

    // And frames STILL advance on the next tick(s) - driving the emitter (the
    // map) even though the panel is gone.
    act(() => {
      fireTick(); // 0 -> 1
    });
    expect(ctrl.frameIndexFor(ctrl.getActiveGroup()!.key)).toBe(1);
    expect(emitted).toContain(1);
    act(() => {
      fireTick(); // 1 -> 2
    });
    expect(ctrl.frameIndexFor(ctrl.getActiveGroup()!.key)).toBe(2);
    expect(emitted).toContain(2);
  });
});

describe("JOB WEB-ANIM #157.3 - the scrubber play button toggles playing", () => {
  beforeEach(() => {
    installFakeTimerController();
  });

  it("clicking the scrubber play button flips the controller's playing flag", () => {
    render(<LayerPanel initialLayers={FRAMES} />);
    render(<AppScrubberHarness />);
    const ctrl = getAnimationController();
    expect(ctrl.isPlaying()).toBe(false);
    const playBtn = screen.getByTestId("scrubber-play");
    expect(playBtn).toHaveAttribute("aria-label", "Play sequence");
    act(() => {
      fireEvent.click(playBtn);
    });
    expect(ctrl.isPlaying()).toBe(true);
    // The button reflects the new state.
    expect(screen.getByTestId("scrubber-play")).toHaveAttribute(
      "aria-label",
      "Pause sequence",
    );
    act(() => {
      fireEvent.click(screen.getByTestId("scrubber-play"));
    });
    expect(ctrl.isPlaying()).toBe(false);
  });

  it("the scrubber advancing the controller also advances the panel-driven map", () => {
    // Step via the scrubber slider; the controller records the frame + emits.
    // ITEM 5: the group now defaults to frame 0, so step to a DIFFERENT frame (2)
    // to exercise a real slider change + emit (a change to the current value 0
    // would be a controlled-input no-op in the DOM).
    const emitted: number[] = [];
    getAnimationController().setEmitter((_ids, idx) => emitted.push(idx));
    render(<LayerPanel initialLayers={FRAMES} />);
    render(<AppScrubberHarness />);
    emitted.length = 0;
    act(() => {
      fireEvent.change(screen.getByTestId("scrubber-slider"), {
        target: { value: "2" },
      });
    });
    expect(getAnimationController().frameIndexFor(
      getAnimationController().getActiveGroup()!.key,
    )).toBe(2);
    expect(emitted).toContain(2);
  });
});

// --- Item b/c (NATE 2026-06-20) - mobile legend toggle + case-exit clearing --- //
//
// These compose the real pieces App.tsx wires: the MOBILE legend show/hide
// toggle rendered INSIDE the LayerPanel's expanded section (off the chat
// composer), and the AnimationController.reset() App calls on Case exit to clear
// the scrubber (which, on exit, the unmounting LayerPanel can no longer clear).
import {
  MobileLegendToggle,
  legendHasContent,
} from "./components/LayerLegend";

describe("Item b - mobile legend toggle lives INSIDE the Layers section", () => {
  beforeEach(() => {
    installFakeTimerController();
  });

  it("renders the MobileLegendToggle inside the LayerPanel body (not floating)", () => {
    // App passes <MobileLegendToggle/> as LayerPanel's `legendControl` on mobile.
    let hidden = false;
    render(
      <LayerPanel
        initialLayers={FRAMES}
        mobile
        legendControl={
          <MobileLegendToggle hidden={hidden} onToggle={(h) => (hidden = h)} />
        }
      />,
    );
    // The toggle sits in the panel's dedicated legend-control slot.
    const slot = screen.getByTestId("grace2-layer-panel-legend-control");
    const toggle = screen.getByTestId("grace2-mobile-legend-toggle");
    expect(slot.contains(toggle)).toBe(true);
    // It is a child of the Layers panel (in-flow), not portaled to the body root.
    const panel = screen.getByTestId("grace2-layer-panel");
    expect(panel.contains(toggle)).toBe(true);
  });

  it("LayerPanel renders no legend-control slot when none is supplied (desktop)", () => {
    render(<LayerPanel initialLayers={FRAMES} />);
    expect(screen.queryByTestId("grace2-layer-panel-legend-control")).toBeNull();
  });

  it("legendHasContent gates whether App renders the mobile toggle", () => {
    // A raster with a KNOWN preset has a legend => the toggle should render.
    const depth: ProjectLayerSummary = {
      layer_id: "depth-1",
      name: "Max flood depth",
      layer_type: "raster",
      uri: "s3://b/depth.cog.tif",
      visible: true,
      opacity: 1,
      z_index: 1,
      style_preset: "continuous_flood_depth",
    };
    expect(legendHasContent([depth])).toBe(true);
    // No eligible raster legend => no toggle.
    expect(legendHasContent([])).toBe(false);
  });
});

// --- STATIC SCRUBBER + AUTOPLAY HANDLE (NATE 2026-06-26) -------------------- //
//
// The scrubber is now STATIC at the bottom (no AOI-bbox snap/dock). The
// remaining behavioural contract worth pinning at the integration level is the
// AUTOPLAY-HANDLE fix: when the controller advances a frame (auto tick), the
// App-owned scrubber re-renders (useAnimationState) and the slider HANDLE tracks
// the new frame index - NATE: "the frame number changes but the handle does not
// move." This proves the live activeIndex reaches the controlled slider.
describe("static scrubber - slider handle tracks the autoplay frame (NATE 2026-06-26)", () => {
  beforeEach(() => {
    installFakeTimerController();
  });

  it("advances the slider value as the controller auto-advances frames", () => {
    render(<LayerPanel initialLayers={FRAMES} />);
    render(<AppScrubberHarness />);
    const ctrl = getAnimationController();
    const key = ctrl.getActiveGroup()!.key;
    // Start playback from frame 0.
    act(() => {
      ctrl.stepGroupTo(key, 0);
      ctrl.setPlaying(true);
    });
    const slider0 = screen.getByTestId("scrubber-slider") as HTMLInputElement;
    expect(slider0.value).toBe("0");
    // One auto tick -> frame 1; the slider HANDLE (value) must move with it.
    act(() => {
      fireTick();
    });
    expect(ctrl.frameIndexFor(key)).toBe(1);
    expect((screen.getByTestId("scrubber-slider") as HTMLInputElement).value).toBe(
      "1",
    );
    expect(screen.getByTestId("scrubber-frame-label")).toHaveTextContent(
      `2/${FRAMES.length}`,
    );
  });
});

// --- ITEM 2 (NATE 2026-06-23) - hide the scrubber over the mobile Layers drawer //
describe("Item 2 - scrubber hidden while the mobile Layers drawer is open", () => {
  beforeEach(() => {
    installFakeTimerController();
  });

  it("renders the scrubber when the drawer is CLOSED (hidden=false)", () => {
    render(<AppScrubberHarness hidden={false} />);
    act(() => {
      getAnimationController().setGroups([
        {
          key: "grp",
          label: "HRRR precip",
          layerIds: ["run-a-f01", "run-a-f03", "run-a-f06"],
          frameLabels: ["F+01h", "F+03h", "F+06h"],
        },
      ]);
    });
    expect(screen.getByTestId("grace2-sequence-scrubber")).toBeInTheDocument();
  });

  it("HIDES the scrubber when the mobile drawer is OPEN (hidden=true)", () => {
    const { rerender } = render(<AppScrubberHarness hidden={false} />);
    act(() => {
      getAnimationController().setGroups([
        {
          key: "grp",
          label: "HRRR precip",
          layerIds: ["run-a-f01", "run-a-f03", "run-a-f06"],
          frameLabels: ["F+01h", "F+03h", "F+06h"],
        },
      ]);
    });
    expect(screen.getByTestId("grace2-sequence-scrubber")).toBeInTheDocument();
    // Open the mobile Layers drawer -> the scrubber (a map overlay) must vanish
    // so it does not float over the layer rows.
    rerender(<AppScrubberHarness hidden={true} />);
    expect(screen.queryByTestId("grace2-sequence-scrubber")).toBeNull();
    // Closing the drawer again restores it (the group is still active).
    rerender(<AppScrubberHarness hidden={false} />);
    expect(screen.getByTestId("grace2-sequence-scrubber")).toBeInTheDocument();
  });
});

describe("Item c - Case exit clears the scrubber (controller reset)", () => {
  it("after the panel unmounts (Case exit) + reset(), the App scrubber clears", () => {
    installFakeTimerController();
    // Mount the panel (pushes the group) + the App scrubber harness.
    const panel = render(<LayerPanel initialLayers={FRAMES} />);
    render(<AppScrubberHarness />);
    expect(screen.getByTestId("grace2-sequence-scrubber")).toBeInTheDocument();
    // On Case EXIT: the LayerPanel unmounts (the rail shows the Cases list, not
    // CaseView) - so nothing re-pushes the old Case's groups - and App's
    // Case-switch handler resets the shared controller.
    act(() => {
      panel.unmount();
      getAnimationController().reset();
    });
    // The scrubber renders only when a group is active; reset cleared it, and
    // (unlike a panel close alone) nothing re-pushes the group, so the scrubber
    // stays gone - item c: the scrubber clears on Case exit.
    expect(screen.queryByTestId("grace2-sequence-scrubber")).toBeNull();
  });
});

// --- ITEM 5 + AUTOPLAY-OFF (NATE 2026-06-24): scrubber defaults to the FIRST
// frame; auto-play is now OPT-IN (off by default). The scrubber appears showing
// frame 1/N, paused, until the user presses play. --------------------------- //
describe("scrubber defaults to first frame; auto-play is opt-in", () => {
  it("a freshly-loaded sequence starts on frame 1/N and is PAUSED by default", () => {
    // Default install: reduced-motion ON and autoPlay OFF -> paused on load.
    installFakeTimerController();
    render(<LayerPanel initialLayers={FRAMES} />);
    render(<AppScrubberHarness />);
    const ctrl = getAnimationController();
    const key = ctrl.getActiveGroup()!.key;
    // FIRST frame default (index 0 -> "1/N" in the scrubber readout).
    expect(ctrl.frameIndexFor(key)).toBe(0);
    expect(screen.getByTestId("scrubber-frame-label")).toHaveTextContent(
      `1/${FRAMES.length}`,
    );
    // AUTOPLAY-OFF: the controller is PAUSED + the play button shows Play.
    expect(ctrl.isPlaying()).toBe(false);
    expect(screen.getByTestId("scrubber-play")).toHaveAttribute(
      "aria-label",
      "Play sequence",
    );
  });

  it("auto-plays on load only when auto-play is explicitly opted in", () => {
    // reduced-motion OFF, autoPlay ON -> auto-play fires (the opt-in path).
    installFakeTimerController(false, true);
    render(<LayerPanel initialLayers={FRAMES} />);
    render(<AppScrubberHarness />);
    const ctrl = getAnimationController();
    const key = ctrl.getActiveGroup()!.key;
    expect(ctrl.frameIndexFor(key)).toBe(0);
    expect(ctrl.isPlaying()).toBe(true);
    expect(screen.getByTestId("scrubber-play")).toHaveAttribute(
      "aria-label",
      "Pause sequence",
    );
  });

  it("does NOT auto-play under prefers-reduced-motion even when opted in (stays paused on frame 1)", () => {
    installFakeTimerController(true, true); // reduced-motion ON, autoPlay ON
    render(<LayerPanel initialLayers={FRAMES} />);
    render(<AppScrubberHarness />);
    const ctrl = getAnimationController();
    const key = ctrl.getActiveGroup()!.key;
    expect(ctrl.frameIndexFor(key)).toBe(0); // still defaults to the first frame
    expect(ctrl.isPlaying()).toBe(false); // but no autoplay
    expect(screen.getByTestId("scrubber-play")).toHaveAttribute(
      "aria-label",
      "Play sequence",
    );
  });
});

// --- TASK E (NATE 2026-06-26): the MOBILE scrubber docks to the chat sheet's
// TOP EDGE (the panel, not the composer) and TRACKS it as the sheet is
// adjusted/collapsed. App threads the sheet's live top in viewport px as
// `sheetTopPx`; the harness mirrors that pass-through. Desktop ignores it. ---- //
describe("TASK E - mobile scrubber docks to + tracks the chat sheet top edge", () => {
  const ORIG_H = window.innerHeight;
  beforeEach(() => {
    installFakeTimerController();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    Object.defineProperty(window, "innerHeight", {
      value: ORIG_H,
      configurable: true,
      writable: true,
    });
  });

  function stubPlatform(mobile: boolean): void {
    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockReturnValue({
        matches: mobile,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
      }),
    );
  }

  function setViewportHeight(px: number): void {
    Object.defineProperty(window, "innerHeight", {
      value: px,
      configurable: true,
      writable: true,
    });
  }

  function pushGroup(): void {
    act(() => {
      getAnimationController().setGroups([
        {
          key: "grp",
          label: "HRRR precip",
          layerIds: ["run-a-f01", "run-a-f03", "run-a-f06"],
          frameLabels: ["F+01h", "F+03h", "F+06h"],
        },
      ]);
    });
  }

  it("mobile: docks the bottom to (viewportH - sheetTopPx + gap)", () => {
    stubPlatform(true);
    setViewportHeight(800);
    render(<AppScrubberHarness sheetTopPx={520} />);
    pushGroup();
    const el = screen.getByTestId("grace2-sequence-scrubber");
    // gap is 20 (SCRUBBER_SHEET_DOCK_GAP_PX): 800 - 520 + 20 = 300.
    expect(el.style.bottom).toBe("300px");
    expect(el.style.left).toBe("50%");
  });

  it("mobile: a HIGHER sheetTopPx (sheet expanded) lifts the scrubber", () => {
    stubPlatform(true);
    setViewportHeight(800);
    const { rerender } = render(<AppScrubberHarness sheetTopPx={600} />);
    pushGroup();
    const collapsedBottom = Number.parseFloat(
      screen.getByTestId("grace2-sequence-scrubber").style.bottom,
    );
    rerender(<AppScrubberHarness sheetTopPx={400} />);
    const expandedBottom = Number.parseFloat(
      screen.getByTestId("grace2-sequence-scrubber").style.bottom,
    );
    expect(expandedBottom).toBeGreaterThan(collapsedBottom);
  });

  it("DESKTOP ignores sheetTopPx (stays bottom-pinned at 24px)", () => {
    stubPlatform(false);
    setViewportHeight(800);
    render(<AppScrubberHarness sheetTopPx={400} />);
    pushGroup();
    const el = screen.getByTestId("grace2-sequence-scrubber");
    expect(el.style.bottom).toBe("24px");
    expect(el.style.top).toBe("");
  });
});

// --- MEASURED-TOP (NATE 2026-06-27, mobile-only): App PREFERS the real measured
// sheet top (Chat's getBoundingClientRect under a ResizeObserver) over the
// arithmetic COLLAPSED_SHEET_PX estimate, so the scrubber + legend dock above the
// REAL connecting/bare/collapsed composer instead of floating mid-screen. App
// boots the full shell (WS/auth/map) so we assert the derivation at the source
// level. -------------------------------------------------------------------- //
describe("App.tsx mobile sheetTopPx prefers the measured top (MEASURED-TOP)", () => {
  it("holds the lifted measured top in its own state", () => {
    expect(APP_SRC).toContain(
      "const [sheetTopMeasuredPx, setSheetTopMeasuredPx] = useState<number | null>(",
    );
  });

  it("handleSheetGeometryChange consumes topPx and keeps the last non-null measurement", () => {
    // The geometry callback now also receives topPx (number | null) and only
    // commits real measurements (a transient null mid-teardown must not pop the
    // overlays back to center).
    expect(APP_SRC).toMatch(
      /handleSheetGeometryChange = useCallback\(\s*\(g: \{\s*expanded: boolean;\s*heightVh: number;\s*topPx: number \| null;\s*\}\): void =>/,
    );
    expect(APP_SRC).toContain("if (g.topPx != null) setSheetTopMeasuredPx(g.topPx);");
  });

  it("sheetTopPx prefers the measured top, falling back to the arithmetic estimate only before the first measurement", () => {
    // Desktop short-circuits to null first; mobile prefers sheetTopMeasuredPx;
    // the arithmetic estimate is the last resort (sheetTopMeasuredPx == null).
    expect(APP_SRC).toMatch(
      /const sheetTopPx = !isMobile\s*\?\s*null\s*:\s*sheetTopMeasuredPx != null\s*\?\s*sheetTopMeasuredPx\s*:\s*viewportH > 0/,
    );
    // The arithmetic fallback is unchanged (expanded vh vs COLLAPSED_SHEET_PX).
    expect(APP_SRC).toContain(
      "? Math.round((clampSheetHeight(sheetHeightVh) / 100) * viewportH)",
    );
    expect(APP_SRC).toContain(": COLLAPSED_SHEET_PX)");
  });

  it("DESKTOP keeps sheetTopPx null (no measurement, byte-for-byte unchanged)", () => {
    // The first ternary arm is `!isMobile ? null`, so desktop never reads either
    // measured or arithmetic path.
    expect(APP_SRC).toMatch(/const sheetTopPx = !isMobile\s*\?\s*null/);
  });
});
