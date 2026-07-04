// GRACE-2 web  -  App collapse-toggle tests (job-0065, tweak 3).
//
// Verifies:
//   1. Left collapse toggle updates DOM state (button aria-label flips).
//   2. Right collapse toggle updates DOM state.
//   3. Collapse state is written to localStorage on toggle.
//   4. Re-mount reads persisted localStorage state and starts collapsed.
//
// NOTE: The full App mounts Chat (WebSocket) and MapView (WebGL / maplibre-gl)
// which cannot run in happy-dom. We therefore test the collapse behaviour via
// a minimal CollapseShell component extracted from App.tsx that captures only
// the collapse-toggle logic and localStorage wiring. This is acceptable per
// AGENTS.md "Live E2E validation required"  -  the collapse UI toggle is
// separately verified by the browser screenshot evidence; unit tests here
// cover state correctness and localStorage round-trip.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { useState, useEffect, useRef } from "react";
// JOB WEB-AOI-LEGEND (#159)  -  the case-open snap uses the SAME pure helpers
// App.tsx imports: asBbox validates the persisted (floored) case.bbox; when it
// is null/malformed the snap falls through to extractLastZoomTo (the LATEST,
// floored zoom-to in the rehydrated chat history).
import { asBbox, extractLastZoomTo } from "./lib/case_zoom";
import { LayerCache } from "./lib/layer_cache";
import type { CaseChatMessage } from "./contracts";

// --- Minimal test harness ------------------------------------------------ //
// Mirrors the collapse logic in App.tsx without importing WebSocket/WebGL deps.

const LS_LEFT_COLLAPSED = "grace2.leftPanelCollapsed";
const LS_RIGHT_COLLAPSED = "grace2.rightPanelCollapsed";

function readCollapsed(key: string): boolean {
  try {
    return localStorage.getItem(key) === "true";
  } catch {
    return false;
  }
}

function CollapseShell(): JSX.Element {
  const [leftCollapsed, setLeftCollapsed] = useState(() =>
    readCollapsed(LS_LEFT_COLLAPSED),
  );
  const [rightCollapsed, setRightCollapsed] = useState(() =>
    readCollapsed(LS_RIGHT_COLLAPSED),
  );

  function toggleLeft(): void {
    setLeftCollapsed((prev) => {
      const next = !prev;
      try { localStorage.setItem(LS_LEFT_COLLAPSED, String(next)); } catch { /* */ }
      return next;
    });
  }

  function toggleRight(): void {
    setRightCollapsed((prev) => {
      const next = !prev;
      try { localStorage.setItem(LS_RIGHT_COLLAPSED, String(next)); } catch { /* */ }
      return next;
    });
  }

  return (
    <div>
      <div data-testid="left-panel" data-collapsed={String(leftCollapsed)}>
        <button
          data-testid="grace2-left-collapse-toggle"
          aria-label={leftCollapsed ? "Expand layer panel" : "Collapse layer panel"}
          onClick={toggleLeft}
        >
          {leftCollapsed ? "-" : "-"}
        </button>
      </div>
      <div data-testid="right-panel" data-collapsed={String(rightCollapsed)}>
        <button
          data-testid="grace2-right-collapse-toggle"
          aria-label={rightCollapsed ? "Expand chat panel" : "Collapse chat panel"}
          onClick={toggleRight}
        >
          {rightCollapsed ? "-" : "-"}
        </button>
      </div>
    </div>
  );
}

// --- Tests --------------------------------------------------------------- //

describe("App collapse toggles (tweak 3)", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    localStorage.clear();
  });

  it("left panel starts expanded (aria-label = Collapse)", () => {
    render(<CollapseShell />);
    expect(screen.getByTestId("grace2-left-collapse-toggle")).toHaveAttribute(
      "aria-label",
      "Collapse layer panel",
    );
    expect(screen.getByTestId("left-panel")).toHaveAttribute(
      "data-collapsed",
      "false",
    );
  });

  it("clicking left toggle collapses left panel", () => {
    render(<CollapseShell />);
    fireEvent.click(screen.getByTestId("grace2-left-collapse-toggle"));
    expect(screen.getByTestId("grace2-left-collapse-toggle")).toHaveAttribute(
      "aria-label",
      "Expand layer panel",
    );
    expect(screen.getByTestId("left-panel")).toHaveAttribute(
      "data-collapsed",
      "true",
    );
  });

  it("clicking left toggle twice returns to expanded", () => {
    render(<CollapseShell />);
    fireEvent.click(screen.getByTestId("grace2-left-collapse-toggle"));
    fireEvent.click(screen.getByTestId("grace2-left-collapse-toggle"));
    expect(screen.getByTestId("left-panel")).toHaveAttribute(
      "data-collapsed",
      "false",
    );
  });

  it("collapse state is persisted in localStorage after left toggle", () => {
    render(<CollapseShell />);
    expect(localStorage.getItem(LS_LEFT_COLLAPSED)).toBeNull();
    fireEvent.click(screen.getByTestId("grace2-left-collapse-toggle"));
    expect(localStorage.getItem(LS_LEFT_COLLAPSED)).toBe("true");
    fireEvent.click(screen.getByTestId("grace2-left-collapse-toggle"));
    expect(localStorage.getItem(LS_LEFT_COLLAPSED)).toBe("false");
  });

  it("right panel starts expanded", () => {
    render(<CollapseShell />);
    expect(screen.getByTestId("right-panel")).toHaveAttribute(
      "data-collapsed",
      "false",
    );
  });

  it("clicking right toggle collapses right panel and writes localStorage", () => {
    render(<CollapseShell />);
    fireEvent.click(screen.getByTestId("grace2-right-collapse-toggle"));
    expect(screen.getByTestId("right-panel")).toHaveAttribute(
      "data-collapsed",
      "true",
    );
    expect(localStorage.getItem(LS_RIGHT_COLLAPSED)).toBe("true");
  });

  it("re-mount reads persisted left collapsed state from localStorage", () => {
    // Pre-set localStorage as if a previous session left the panel collapsed.
    localStorage.setItem(LS_LEFT_COLLAPSED, "true");
    const { unmount } = render(<CollapseShell />);
    expect(screen.getByTestId("left-panel")).toHaveAttribute(
      "data-collapsed",
      "true",
    );
    expect(screen.getByTestId("grace2-left-collapse-toggle")).toHaveAttribute(
      "aria-label",
      "Expand layer panel",
    );
    unmount();
  });

  it("re-mount reads persisted right collapsed state from localStorage", () => {
    localStorage.setItem(LS_RIGHT_COLLAPSED, "true");
    render(<CollapseShell />);
    expect(screen.getByTestId("right-panel")).toHaveAttribute(
      "data-collapsed",
      "true",
    );
    expect(screen.getByTestId("grace2-right-collapse-toggle")).toHaveAttribute(
      "aria-label",
      "Expand chat panel",
    );
  });

  it("left and right collapse are independent", () => {
    render(<CollapseShell />);
    fireEvent.click(screen.getByTestId("grace2-left-collapse-toggle"));
    // Left collapsed, right still expanded
    expect(screen.getByTestId("left-panel")).toHaveAttribute("data-collapsed", "true");
    expect(screen.getByTestId("right-panel")).toHaveAttribute("data-collapsed", "false");
  });
});

// --- job-0068: conditional mount + hamburger tests ----------------------- //
//
// Tests for the new overlay layout, hamburger pattern, and conditional mount.
// Uses a minimal AppShell that mirrors the job-0068 App.tsx logic without
// importing WebSocket/WebGL/MapLibre deps (same rationale as CollapseShell
// above). Live browser E2E is captured in the 5 evidence screenshots.

import { act } from "@testing-library/react";

// Minimal shell mirroring the job-0068 conditional-mount + hamburger logic.
function AppShell({ initialLayers = 0, startLeftCollapsed = false }: {
  initialLayers?: number;
  startLeftCollapsed?: boolean;
}): JSX.Element {
  const [layerCount, setLayerCount] = useState(initialLayers);
  const [leftCollapsed, setLeftCollapsed] = useState(startLeftCollapsed);
  const [rightCollapsed, setRightCollapsed] = useState(false);

  const showLeftPanel = layerCount > 0 && !leftCollapsed;
  const showLayersHamburger = layerCount > 0 && leftCollapsed;
  const showChatHamburger = rightCollapsed;

  return (
    <div>
      {/* Simulate layer arrival button */}
      <button
        data-testid="sim-add-layer"
        onClick={() => setLayerCount((c) => c + 1)}
      >
        Add Layer
      </button>
      <button
        data-testid="sim-remove-all-layers"
        onClick={() => setLayerCount(0)}
      >
        Remove All
      </button>

      {showLeftPanel && (
        <div data-testid="grace2-layer-panel">
          <button
            data-testid="grace2-layer-panel-close"
            onClick={() => setLeftCollapsed(true)}
          >
            -
          </button>
        </div>
      )}

      {showLayersHamburger && (
        <button
          data-testid="grace2-layers-hamburger"
          aria-label="Show layers"
          onClick={() => setLeftCollapsed(false)}
        >
          -
        </button>
      )}

      {!rightCollapsed && (
        <div data-testid="grace2-chat">
          <button
            data-testid="grace2-chat-close"
            onClick={() => setRightCollapsed(true)}
          >
            -
          </button>
        </div>
      )}

      {showChatHamburger && (
        <button
          data-testid="grace2-chat-hamburger"
          aria-label="Show chat"
          onClick={() => setRightCollapsed(false)}
        >
          -
        </button>
      )}
    </div>
  );
}

describe("App overlay layout  -  conditional mount + hamburger (job-0068 changes 1-3)", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    localStorage.clear();
  });

  it("no layers -> LayerPanel NOT mounted AND Layers hamburger NOT rendered", () => {
    render(<AppShell initialLayers={0} />);
    expect(screen.queryByTestId("grace2-layer-panel")).toBeNull();
    expect(screen.queryByTestId("grace2-layers-hamburger")).toBeNull();
  });

  it("layers > 0 -> LayerPanel mounts (left overlay)", () => {
    render(<AppShell initialLayers={1} />);
    expect(screen.getByTestId("grace2-layer-panel")).toBeInTheDocument();
  });

  it("adding a layer after start causes LayerPanel to appear", () => {
    render(<AppShell initialLayers={0} />);
    expect(screen.queryByTestId("grace2-layer-panel")).toBeNull();

    act(() => {
      fireEvent.click(screen.getByTestId("sim-add-layer"));
    });

    expect(screen.getByTestId("grace2-layer-panel")).toBeInTheDocument();
  });

  it("removing all layers collapses LayerPanel AND hamburger disappears", () => {
    render(<AppShell initialLayers={1} />);
    expect(screen.getByTestId("grace2-layer-panel")).toBeInTheDocument();

    act(() => {
      fireEvent.click(screen.getByTestId("sim-remove-all-layers"));
    });

    expect(screen.queryByTestId("grace2-layer-panel")).toBeNull();
    expect(screen.queryByTestId("grace2-layers-hamburger")).toBeNull();
  });

  it("layers present + leftCollapsed -> hamburger top-left renders, panel hidden", () => {
    render(<AppShell initialLayers={1} startLeftCollapsed />);
    expect(screen.queryByTestId("grace2-layer-panel")).toBeNull();
    expect(screen.getByTestId("grace2-layers-hamburger")).toBeInTheDocument();
    expect(screen.getByTestId("grace2-layers-hamburger")).toHaveAttribute(
      "aria-label",
      "Show layers",
    );
  });

  it("clicking hamburger expands panel and hamburger disappears", () => {
    render(<AppShell initialLayers={1} startLeftCollapsed />);
    expect(screen.getByTestId("grace2-layers-hamburger")).toBeInTheDocument();

    act(() => {
      fireEvent.click(screen.getByTestId("grace2-layers-hamburger"));
    });

    expect(screen.queryByTestId("grace2-layers-hamburger")).toBeNull();
    expect(screen.getByTestId("grace2-layer-panel")).toBeInTheDocument();
  });

  it("clicking - close in LayerPanel collapses panel and shows hamburger", () => {
    render(<AppShell initialLayers={1} />);
    expect(screen.getByTestId("grace2-layer-panel")).toBeInTheDocument();

    act(() => {
      fireEvent.click(screen.getByTestId("grace2-layer-panel-close"));
    });

    expect(screen.queryByTestId("grace2-layer-panel")).toBeNull();
    expect(screen.getByTestId("grace2-layers-hamburger")).toBeInTheDocument();
  });

  it("Chat panel always present (it is the way to request layers)", () => {
    render(<AppShell initialLayers={0} />);
    expect(screen.getByTestId("grace2-chat")).toBeInTheDocument();
  });

  it("clicking Chat - hides chat; chat hamburger appears top-right", () => {
    render(<AppShell initialLayers={0} />);
    act(() => {
      fireEvent.click(screen.getByTestId("grace2-chat-close"));
    });
    expect(screen.queryByTestId("grace2-chat")).toBeNull();
    expect(screen.getByTestId("grace2-chat-hamburger")).toHaveAttribute(
      "aria-label",
      "Show chat",
    );
  });
});

// --- Theme-toggle harness (job-0076 bundled enhancement) ------------------ //

const LS_THEME = "grace2.theme";

function readTheme(): "light" | "dark" {
  try {
    const v = localStorage.getItem(LS_THEME);
    return v === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}

function ThemeShell(): JSX.Element {
  const [theme, setTheme] = useState<"light" | "dark">(() => readTheme());
  function toggle(): void {
    setTheme((prev) => {
      const next = prev === "light" ? "dark" : "light";
      try { localStorage.setItem(LS_THEME, next); } catch { /* */ }
      return next;
    });
  }
  return (
    <div data-testid="theme-host" data-theme={theme}>
      <button
        data-testid="grace2-theme-toggle"
        aria-label={theme === "light" ? "Switch to dark theme" : "Switch to light theme"}
        aria-pressed={theme === "dark"}
        onClick={toggle}
      >
        {theme === "light" ? "-" : "-"}
      </button>
    </div>
  );
}

describe("Theme toggle (job-0076 bundled enhancement)", () => {
  beforeEach(() => {
    localStorage.clear();
  });
  afterEach(() => {
    localStorage.clear();
  });

  it("defaults to light theme when localStorage is empty", () => {
    render(<ThemeShell />);
    expect(screen.getByTestId("theme-host")).toHaveAttribute("data-theme", "light");
    expect(screen.getByTestId("grace2-theme-toggle")).toHaveAttribute(
      "aria-label",
      "Switch to dark theme",
    );
  });

  it("clicking toggle flips to dark and writes localStorage", () => {
    render(<ThemeShell />);
    act(() => {
      fireEvent.click(screen.getByTestId("grace2-theme-toggle"));
    });
    expect(screen.getByTestId("theme-host")).toHaveAttribute("data-theme", "dark");
    expect(localStorage.getItem(LS_THEME)).toBe("dark");
    expect(screen.getByTestId("grace2-theme-toggle")).toHaveAttribute(
      "aria-label",
      "Switch to light theme",
    );
  });

  it("re-mount reads persisted dark from localStorage", () => {
    localStorage.setItem(LS_THEME, "dark");
    render(<ThemeShell />);
    expect(screen.getByTestId("theme-host")).toHaveAttribute("data-theme", "dark");
  });

  it("clicking twice returns to light", () => {
    render(<ThemeShell />);
    const btn = screen.getByTestId("grace2-theme-toggle");
    act(() => { fireEvent.click(btn); });
    act(() => { fireEvent.click(btn); });
    expect(screen.getByTestId("theme-host")).toHaveAttribute("data-theme", "light");
    expect(localStorage.getItem(LS_THEME)).toBe("light");
  });
});

// --- job-0140: PayloadWarningInline seam + component tests ---------------- //
//
// Tests the dev-injection seam __grace2InjectPayloadWarning and verifies that:
//   1. The seam wires setPayloadWarnings so PayloadWarningInline renders.
//   2. The component shows estimated_mb, threshold_mb, recommendation.
//   3. All 3 option buttons render (proceed / cancel / narrow_scope).
//   4. Clicking "Proceed" calls onDecide with decision="proceed", revised=null.
//   5. Clicking "Cancel" calls onDecide with decision="cancel", revised=null.
//   6. Clicking "Narrow scope" with alternative_args calls onDecide with
//      decision="narrow_scope" and the provided alternative_args.
//
// The seam itself is integration-tested via a PayloadWarningShell component
// that mirrors the App.tsx queue pattern without importing WebSocket/WebGL.

import { PayloadWarningInline } from "./components/PayloadWarningInline";
import type { PayloadWarningEnvelopePayload, PayloadConfirmationDecision } from "./contracts";

// Minimal shell mirroring the App.tsx payloadWarnings queue pattern.
function PayloadWarningShell({
  initialWarning,
}: {
  initialWarning?: PayloadWarningEnvelopePayload;
}): JSX.Element {
  const [warnings, setWarnings] = useState<PayloadWarningEnvelopePayload[]>(
    initialWarning ? [initialWarning] : [],
  );

  // Expose the seam function on window so tests can call it.
  // In production App.tsx this is registered in a useEffect guarded by
  // import.meta.env.DEV.  Here we register unconditionally for testing.
  (window as Window & { __grace2InjectPayloadWarning?: (p: PayloadWarningEnvelopePayload) => void }).__grace2InjectPayloadWarning = (p) => {
    setWarnings((prev) => [p, ...prev]);
  };

  function handleDecide(
    warningId: string,
    _decision: PayloadConfirmationDecision,
    _revised: Record<string, unknown> | null,
  ): void {
    setWarnings((prev) => prev.filter((w) => w.warning_id !== warningId));
  }

  return (
    <div data-testid="warning-shell">
      {warnings.map((w) => (
        <PayloadWarningInline
          key={w.warning_id}
          warning={w}
          onDecide={(decision, revised) => handleDecide(w.warning_id, decision, revised)}
        />
      ))}
    </div>
  );
}

// Sample payload factory.
function makeWarning(
  overrides: Partial<PayloadWarningEnvelopePayload> = {},
): PayloadWarningEnvelopePayload {
  return {
    warning_id: "test-warning-001",
    tool_name: "fetch_dem",
    tool_args: { bbox: [-82, 26, -81, 27] },
    estimated_mb: 42.5,
    threshold_mb: 25,
    recommendation: "Consider narrowing the bbox to reduce payload size.",
    alternative_args: { bbox: [-81.8, 26.2, -81.2, 26.8] },
    options: ["proceed", "narrow_scope", "cancel"],
    ...overrides,
  };
}

describe("PayloadWarningInline component (job-0140)", () => {
  it("renders estimated_mb, threshold_mb, recommendation", () => {
    const w = makeWarning();
    render(
      <PayloadWarningInline warning={w} onDecide={vi.fn()} />,
    );
    expect(screen.getByTestId("payload-warning-estimated-mb")).toHaveTextContent("42.5");
    expect(screen.getByTestId("payload-warning-threshold-mb")).toHaveTextContent("25");
    expect(screen.getByTestId("payload-warning-recommendation")).toHaveTextContent(
      "Consider narrowing the bbox to reduce payload size.",
    );
  });

  it("renders 3 action buttons: Proceed, Narrow scope, Cancel", () => {
    const w = makeWarning();
    render(<PayloadWarningInline warning={w} onDecide={vi.fn()} />);
    expect(screen.getByTestId("payload-warning-button-proceed")).toBeInTheDocument();
    expect(screen.getByTestId("payload-warning-button-narrow_scope")).toBeInTheDocument();
    expect(screen.getByTestId("payload-warning-button-cancel")).toBeInTheDocument();
  });

  it("clicking Proceed calls onDecide with 'proceed' and null revised", () => {
    const onDecide = vi.fn();
    const w = makeWarning();
    render(<PayloadWarningInline warning={w} onDecide={onDecide} />);
    act(() => {
      fireEvent.click(screen.getByTestId("payload-warning-button-proceed"));
    });
    expect(onDecide).toHaveBeenCalledOnce();
    expect(onDecide).toHaveBeenCalledWith("proceed", null);
  });

  it("clicking Cancel calls onDecide with 'cancel' and null revised", () => {
    const onDecide = vi.fn();
    const w = makeWarning();
    render(<PayloadWarningInline warning={w} onDecide={onDecide} />);
    act(() => {
      fireEvent.click(screen.getByTestId("payload-warning-button-cancel"));
    });
    expect(onDecide).toHaveBeenCalledOnce();
    expect(onDecide).toHaveBeenCalledWith("cancel", null);
  });

  it("clicking Narrow scope with alternative_args calls onDecide with 'narrow_scope' + alternative_args", () => {
    const onDecide = vi.fn();
    const w = makeWarning();
    render(<PayloadWarningInline warning={w} onDecide={onDecide} />);
    act(() => {
      fireEvent.click(screen.getByTestId("payload-warning-button-narrow_scope"));
    });
    expect(onDecide).toHaveBeenCalledOnce();
    expect(onDecide).toHaveBeenCalledWith("narrow_scope", w.alternative_args);
  });

  it("after a decision, folds to a compact amber summary (buttons gone)", () => {
    const w = makeWarning();
    render(<PayloadWarningInline warning={w} onDecide={vi.fn()} />);
    act(() => {
      fireEvent.click(screen.getByTestId("payload-warning-button-proceed"));
    });
    // job-0352: the answered warning folds to a compact amber card.
    expect(screen.queryByTestId("payload-warning-button-proceed")).toBeNull();
    expect(screen.getByTestId("payload-warning-inline").getAttribute("data-resolved")).toBe("proceed");
    expect(screen.getByTestId("payload-warning-sent")).toHaveTextContent("Large response");
  });
});

describe("__grace2InjectPayloadWarning dev seam (job-0140)", () => {
  afterEach(() => {
    delete (window as Window & { __grace2InjectPayloadWarning?: unknown }).__grace2InjectPayloadWarning;
  });

  it("seam absent before shell mounts -> no warning card", () => {
    render(<div data-testid="empty" />);
    expect(screen.queryByTestId("payload-warning-inline")).toBeNull();
  });

  it("injecting a warning via seam renders PayloadWarningInline", () => {
    render(<PayloadWarningShell />);
    act(() => {
      (window as Window & { __grace2InjectPayloadWarning?: (p: PayloadWarningEnvelopePayload) => void }).__grace2InjectPayloadWarning?.(makeWarning());
    });
    expect(screen.getByTestId("payload-warning-inline")).toBeInTheDocument();
  });

  it("injected warning shows tool name", () => {
    render(<PayloadWarningShell />);
    act(() => {
      (window as Window & { __grace2InjectPayloadWarning?: (p: PayloadWarningEnvelopePayload) => void }).__grace2InjectPayloadWarning?.(makeWarning({ tool_name: "fetch_buildings" }));
    });
    expect(screen.getByTestId("payload-warning-tool")).toHaveTextContent("fetch_buildings");
  });

  it("shell initialised with a warning renders it immediately", () => {
    render(<PayloadWarningShell initialWarning={makeWarning()} />);
    expect(screen.getByTestId("payload-warning-inline")).toBeInTheDocument();
  });

  it("clicking Proceed removes the card from the queue", () => {
    render(<PayloadWarningShell initialWarning={makeWarning()} />);
    act(() => {
      fireEvent.click(screen.getByTestId("payload-warning-button-proceed"));
    });
    // After onDecide the shell removes it from the warnings list; the inline
    // card shows the 'Sent' footer for a brief moment but the shell removes
    // the entry  -  the card no longer has buttons.
    expect(screen.queryByTestId("payload-warning-button-proceed")).toBeNull();
  });
});

// --- Map pan unlock  -  LayerPanel wrap pointer-events confinement (job-0173 Part 3) //
//
// REGRESSION the kickoff diagnosed: after a flood/raster layer renders, the
// user couldn't pan/drag the map. Root cause: the inner div inside
// `grace2-case-view-layer-panel-wrap` had pointerEvents:"auto" with
// width:100% height:100%, blanketing the full (top:64 -> bottom:60,
// left:0 -> right:0) region above the map. That zone covers virtually the
// entire map viewport, so MapLibre never sees pointerdown/move events on the
// raster overlay area.
//
// Fix verified structurally: the pointer-events:auto region must be column-
// sized (left:0, width - 320px  -  i.e. left:16 offset + 280 panel + 16 right
// padding = 312px), not full-bleed. Outside that column the wrap is
// pointer-events:none -> click-through to the map below.

describe("Map pan unlock  -  LayerPanel wrap pointer-events confined to column (job-0173 Part 3)", () => {
  // Inline mirror of the App.tsx LayerPanel wrap fragment. This is the
  // exact structure App.tsx emits when activeCaseId !== null && layers.length > 0.
  function LayerPanelWrapFragment(): JSX.Element {
    return (
      <div
        data-testid="grace2-case-view-layer-panel-wrap"
        style={{
          position: "absolute",
          top: 64,
          left: 0,
          right: 0,
          bottom: 60,
          zIndex: 20,
          pointerEvents: "none",
        }}
      >
        <div
          data-testid="grace2-layer-panel-pointer-region"
          style={{
            pointerEvents: "auto",
            position: "absolute",
            left: 0,
            top: 0,
            bottom: 0,
            width: 280 + 16 + 16,
          }}
        />
      </div>
    );
  }

  it("outer wrap is pointer-events:none so map drags pass through", () => {
    render(<LayerPanelWrapFragment />);
    const wrap = screen.getByTestId("grace2-case-view-layer-panel-wrap");
    expect((wrap as HTMLElement).style.pointerEvents).toBe("none");
  });

  it("inner pointer-events:auto region is NOT full-bleed (width is column-sized, not 100%)", () => {
    render(<LayerPanelWrapFragment />);
    const region = screen.getByTestId("grace2-layer-panel-pointer-region");
    const s = (region as HTMLElement).style;
    expect(s.pointerEvents).toBe("auto");
    // Width must be a finite pixel value <= 320px, NOT "100%". The prior buggy
    // implementation used width:100% + height:100% which blanketed the entire
    // (top:64 -> bottom:60, left:0 -> right:0) area and blocked map pan.
    expect(s.width).not.toBe("100%");
    expect(s.width).not.toBe("");
    const widthPx = parseInt(s.width, 10);
    expect(Number.isFinite(widthPx)).toBe(true);
    expect(widthPx).toBeGreaterThan(0);
    expect(widthPx).toBeLessThanOrEqual(320);
  });

  it("inner pointer-events:auto region sits at left:0 (does not extend to right edge)", () => {
    render(<LayerPanelWrapFragment />);
    const region = screen.getByTestId("grace2-layer-panel-pointer-region");
    const s = (region as HTMLElement).style;
    // Anchored to the left rail; right edge unanchored so MapLibre sees clicks
    // everywhere to the right of the panel column.
    expect(s.left).toBe("0px");
    expect(s.right).toBe("");
  });
});

// ---------------------------------------------------------------------------
// job-0322 F53-COMPLETE (Group A wiring half): App.tsx must pass a non-null
// onDeleteLayer to BOTH <LayerPanel> mount sites (desktop case-view + mobile
// drawer) so the per-row delete control reaches the server via
// wsRef.current.sendDeleteLayer. Pre-fix the prop was never wired, so the
// server never heard the delete and the layer resurrected on the next
// session-state.
//
// We can't mount the real App (WebSocket/WebGL), so we mirror the EXACT App.tsx
// wiring  -  `onDeleteLayer={(id) => wsRef.current?.sendDeleteLayer(id)}`  -  with a
// mocked GraceWs in a useRef and a fake LayerPanel whose delete row invokes the
// passed-in onDeleteLayer (mirroring LayerPanel.tsx's `onDeleteLayer?.(layerId)`
// call). This pins that BOTH mounts receive a working callback that reaches the
// socket method.
// ---------------------------------------------------------------------------

interface FakeLayerPanelProps {
  testid: string;
  onDeleteLayer?: (id: string) => void;
}

/** Fake LayerPanel: mirrors only the delete-row -> onDeleteLayer call path. */
function FakeLayerPanel({ testid, onDeleteLayer }: FakeLayerPanelProps): JSX.Element {
  return (
    <div data-testid={testid}>
      <button
        data-testid={`${testid}-delete-row`}
        // mirrors LayerPanel.tsx: `onDeleteLayer?.(layerId)`
        onClick={() => onDeleteLayer?.("flood-depth-peak-01TEST")}
      >
        Delete layer
      </button>
      {/* Marker the test reads to assert the prop is a non-null function. */}
      <span data-testid={`${testid}-has-ondelete`}>
        {typeof onDeleteLayer === "function" ? "yes" : "no"}
      </span>
    </div>
  );
}

/** Minimal GraceWs surface the F53 wiring touches. */
interface FakeWs {
  sendDeleteLayer: (id: string) => void;
}

/**
 * Mirrors the two App.tsx LayerPanel mounts. `wsRef` holds the (mocked)
 * GraceWs exactly as App.tsx's `wsRef = useRef<GraceWs | null>` does; both
 * mounts wire `onDeleteLayer={(id) => wsRef.current?.sendDeleteLayer(id)}`,
 * byte-for-byte the production wiring.
 */
function DeleteWiringShell({ ws }: { ws: FakeWs | null }): JSX.Element {
  const wsRef = useRef<FakeWs | null>(ws);
  wsRef.current = ws;
  return (
    <div>
      {/* desktop case-view mount (App.tsx ~947) */}
      <FakeLayerPanel
        testid="desktop-layer-panel"
        onDeleteLayer={(id) => wsRef.current?.sendDeleteLayer(id)}
      />
      {/* mobile drawer mount (App.tsx ~1140) */}
      <FakeLayerPanel
        testid="mobile-layer-panel"
        onDeleteLayer={(id) => wsRef.current?.sendDeleteLayer(id)}
      />
    </div>
  );
}

describe("App F53 wiring  -  onDeleteLayer reaches GraceWs.sendDeleteLayer (job-0322)", () => {
  it("desktop LayerPanel mount receives a non-null onDeleteLayer", () => {
    const ws: FakeWs = { sendDeleteLayer: vi.fn() };
    render(<DeleteWiringShell ws={ws} />);
    expect(screen.getByTestId("desktop-layer-panel-has-ondelete")).toHaveTextContent("yes");
  });

  it("mobile LayerPanel mount receives a non-null onDeleteLayer", () => {
    const ws: FakeWs = { sendDeleteLayer: vi.fn() };
    render(<DeleteWiringShell ws={ws} />);
    expect(screen.getByTestId("mobile-layer-panel-has-ondelete")).toHaveTextContent("yes");
  });

  it("deleting from the DESKTOP row reaches sendDeleteLayer with the layer_id", () => {
    const sendDeleteLayer = vi.fn();
    render(<DeleteWiringShell ws={{ sendDeleteLayer }} />);
    act(() => {
      fireEvent.click(screen.getByTestId("desktop-layer-panel-delete-row"));
    });
    expect(sendDeleteLayer).toHaveBeenCalledOnce();
    expect(sendDeleteLayer).toHaveBeenCalledWith("flood-depth-peak-01TEST");
  });

  it("deleting from the MOBILE row reaches sendDeleteLayer with the layer_id", () => {
    const sendDeleteLayer = vi.fn();
    render(<DeleteWiringShell ws={{ sendDeleteLayer }} />);
    act(() => {
      fireEvent.click(screen.getByTestId("mobile-layer-panel-delete-row"));
    });
    expect(sendDeleteLayer).toHaveBeenCalledOnce();
    expect(sendDeleteLayer).toHaveBeenCalledWith("flood-depth-peak-01TEST");
  });

  it("delete is a no-op (no throw) when wsRef.current is null", () => {
    render(<DeleteWiringShell ws={null} />);
    // The optional-chain `wsRef.current?.sendDeleteLayer(id)` must not throw.
    expect(() => {
      act(() => {
        fireEvent.click(screen.getByTestId("desktop-layer-panel-delete-row"));
      });
    }).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// FLAG (a)  -  the AOI screen rect flows Map(mock) -> App -> LayerPanel so the
// SequenceScrubber (inside LayerPanel) can pin to the AOI bbox. App holds the
// rect in state and wires `onAoiScreenRectChange={setAoiScreenRect}` on MapView
// + `aoiRect={aoiScreenRect}` on LayerPanel. We can't mount the real App
// (WebSocket/WebGL/MapLibre), so we mirror that exact glue with a fake Map that
// invokes the callback and a fake LayerPanel that surfaces the received rect.
// ---------------------------------------------------------------------------

interface FakeScreenRect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

/** Fake Map: exposes a button that fires onAoiScreenRectChange (mirrors the
 *  Map.tsx lift effect calling the callback when legendRect changes). */
function FakeMap({
  onAoiScreenRectChange,
  rect,
}: {
  onAoiScreenRectChange?: (r: FakeScreenRect | null) => void;
  rect: FakeScreenRect | null;
}): JSX.Element {
  return (
    <button
      data-testid="fake-map-fire-rect"
      onClick={() => onAoiScreenRectChange?.(rect)}
    >
      fire rect
    </button>
  );
}

/** Fake LayerPanel: surfaces the received aoiRect (mirrors the prop the real
 *  LayerPanel forwards to the SequenceScrubber). */
function FakeRectLayerPanel({ aoiRect }: { aoiRect?: FakeScreenRect | null }): JSX.Element {
  return (
    <span data-testid="fake-layer-panel-rect">
      {aoiRect ? `${aoiRect.left},${aoiRect.top},${aoiRect.right},${aoiRect.bottom}` : "none"}
    </span>
  );
}

/** Mirrors the App.tsx glue: state holds the rect; Map sets it; LayerPanel reads it. */
function AoiRectShell({ rect }: { rect: FakeScreenRect | null }): JSX.Element {
  const [aoiScreenRect, setAoiScreenRect] = useState<FakeScreenRect | null>(null);
  return (
    <div>
      <FakeMap onAoiScreenRectChange={setAoiScreenRect} rect={rect} />
      <FakeRectLayerPanel aoiRect={aoiScreenRect} />
    </div>
  );
}

describe("App FLAG (a) wiring  -  AOI rect flows Map -> App -> LayerPanel", () => {
  it("LayerPanel starts with no rect (null) before Map reports one", () => {
    render(<AoiRectShell rect={{ left: 10, top: 20, right: 110, bottom: 220 }} />);
    expect(screen.getByTestId("fake-layer-panel-rect")).toHaveTextContent("none");
  });

  it("the rect Map reports lands on LayerPanel's aoiRect prop", () => {
    render(<AoiRectShell rect={{ left: 10, top: 20, right: 110, bottom: 220 }} />);
    act(() => {
      fireEvent.click(screen.getByTestId("fake-map-fire-rect"));
    });
    expect(screen.getByTestId("fake-layer-panel-rect")).toHaveTextContent(
      "10,20,110,220",
    );
  });

  it("a null report (AOI off-screen) clears LayerPanel's aoiRect", () => {
    const { rerender } = render(
      <AoiRectShell rect={{ left: 10, top: 20, right: 110, bottom: 220 }} />,
    );
    act(() => {
      fireEvent.click(screen.getByTestId("fake-map-fire-rect"));
    });
    expect(screen.getByTestId("fake-layer-panel-rect")).toHaveTextContent("10,20,110,220");
    // Now Map reports null (AOI left the viewport) -> LayerPanel clears.
    rerender(<AoiRectShell rect={null} />);
    act(() => {
      fireEvent.click(screen.getByTestId("fake-map-fire-rect"));
    });
    expect(screen.getByTestId("fake-layer-panel-rect")).toHaveTextContent("none");
  });
});

// ---------------------------------------------------------------------------
// job-0322 F31 (resume-repaint, iOS zombie-socket): App.tsx registers a
// `visibilitychange` listener; on `visible` it branches on isMobile:
//   - MOBILE: wsRef.current?.forceReconnect()  -  UNCONDITIONALLY tears the
//     (possibly zombie-OPEN) socket down and re-opens; the fresh open handler
//     re-sends session-resume, so NO separate requestSessionState() call.
//   - DESKTOP: wsRef.current?.reconnect() (revive a dropped socket) then
//     wsRef.current?.requestSessionState() (re-pull session-state).
// The listener is cleaned up on unmount and guards for a null wsRef.
//
// We mirror the EXACT effect (including the isMobile branch) in a shell with a
// mocked GraceWs (forceReconnect / reconnect / requestSessionState as spies)
// and drive the real document visibilitychange event.
// ---------------------------------------------------------------------------

interface FakeResumeWs {
  // BUG 4a (Wave 4.9)  -  the handler now reads `isOpen` first to avoid tearing
  // down an already-OPEN socket on resume (the cycling the fix targets). Default
  // falsy when omitted (= not OPEN) so the pre-existing "dropped socket" tests
  // keep exercising the forceReconnect / reconnect teardown paths.
  isOpen?: boolean;
  forceReconnect: () => void;
  reconnect: () => void;
  requestSessionState: () => void;
}

/** Mirror of the App.tsx visibilitychange effect (byte-for-byte order, incl.
 *  the BUG 4a isOpen guard). */
function ResumeShell({
  ws,
  isMobile,
}: {
  ws: FakeResumeWs | null;
  isMobile: boolean;
}): JSX.Element {
  const wsRef = useRef<FakeResumeWs | null>(ws);
  wsRef.current = ws;
  useEffect(() => {
    const onVisibility = (): void => {
      if (document.visibilityState !== "visible") return;
      const sock = wsRef.current;
      if (!sock) return;
      // BUG 4a  -  an already-OPEN socket only needs a state re-pull; do NOT tear
      // it down (the keepalive owns the zombie case now).
      if (sock.isOpen) {
        sock.requestSessionState();
        return;
      }
      if (isMobile) {
        sock.forceReconnect();
        return;
      }
      sock.reconnect();
      sock.requestSessionState();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [isMobile]);
  return <div data-testid="resume-shell" />;
}

/** Force document.visibilityState then fire the event happy-dom dispatches. */
function setVisibility(state: DocumentVisibilityState): void {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => state,
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

function makeResumeWs(): FakeResumeWs {
  return {
    forceReconnect: vi.fn(),
    reconnect: vi.fn(),
    requestSessionState: vi.fn(),
  };
}

describe("App F31 resume-repaint  -  visibilitychange (job-0322)", () => {
  afterEach(() => {
    // Restore a sane default so later suites aren't affected.
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "visible",
    });
  });

  it("DESKTOP visible -> reconnect() then requestSessionState() (NOT forceReconnect)", () => {
    const ws = makeResumeWs();
    render(<ResumeShell ws={ws} isMobile={false} />);
    act(() => {
      setVisibility("visible");
    });
    expect(ws.reconnect).toHaveBeenCalledOnce();
    expect(ws.requestSessionState).toHaveBeenCalledOnce();
    expect(ws.forceReconnect).not.toHaveBeenCalled();
  });

  it("DESKTOP reconnect() runs BEFORE requestSessionState() (revive then re-pull)", () => {
    const order: string[] = [];
    const ws: FakeResumeWs = {
      forceReconnect: vi.fn(() => order.push("force")),
      reconnect: vi.fn(() => order.push("reconnect")),
      requestSessionState: vi.fn(() => order.push("request")),
    };
    render(<ResumeShell ws={ws} isMobile={false} />);
    act(() => {
      setVisibility("visible");
    });
    expect(order).toEqual(["reconnect", "request"]);
  });

  it("MOBILE visible -> forceReconnect() ONLY (zombie-socket: no reconnect / no requestSessionState)", () => {
    const ws = makeResumeWs();
    render(<ResumeShell ws={ws} isMobile={true} />);
    act(() => {
      setVisibility("visible");
    });
    expect(ws.forceReconnect).toHaveBeenCalledOnce();
    expect(ws.reconnect).not.toHaveBeenCalled();
    expect(ws.requestSessionState).not.toHaveBeenCalled();
  });

  it("hidden -> nothing fires (mobile or desktop)", () => {
    const desktop = makeResumeWs();
    const { unmount } = render(<ResumeShell ws={desktop} isMobile={false} />);
    act(() => {
      setVisibility("hidden");
    });
    expect(desktop.reconnect).not.toHaveBeenCalled();
    expect(desktop.requestSessionState).not.toHaveBeenCalled();
    expect(desktop.forceReconnect).not.toHaveBeenCalled();
    unmount();

    const mobile = makeResumeWs();
    render(<ResumeShell ws={mobile} isMobile={true} />);
    act(() => {
      setVisibility("hidden");
    });
    expect(mobile.forceReconnect).not.toHaveBeenCalled();
  });

  it("null wsRef -> visible event is a harmless no-op (no throw, mobile + desktop)", () => {
    const { unmount } = render(<ResumeShell ws={null} isMobile={true} />);
    expect(() => {
      act(() => {
        setVisibility("visible");
      });
    }).not.toThrow();
    unmount();
    render(<ResumeShell ws={null} isMobile={false} />);
    expect(() => {
      act(() => {
        setVisibility("visible");
      });
    }).not.toThrow();
  });

  it("listener is removed on unmount (no call after unmount)", () => {
    const ws = makeResumeWs();
    const { unmount } = render(<ResumeShell ws={ws} isMobile={false} />);
    unmount();
    act(() => {
      setVisibility("visible");
    });
    expect(ws.reconnect).not.toHaveBeenCalled();
    expect(ws.requestSessionState).not.toHaveBeenCalled();
    expect(ws.forceReconnect).not.toHaveBeenCalled();
  });

  // BUG 4a (Wave 4.9)  -  an already-OPEN socket must NOT be torn down on resume
  // (that churn was part of the ~10-45s WS cycling). It only gets a lighter
  // state re-pull; the keepalive's missed-pong detector owns the zombie case.
  it("MOBILE + socket OPEN -> requestSessionState() ONLY (NO forceReconnect)", () => {
    const ws = { ...makeResumeWs(), isOpen: true };
    render(<ResumeShell ws={ws} isMobile={true} />);
    act(() => {
      setVisibility("visible");
    });
    expect(ws.requestSessionState).toHaveBeenCalledOnce();
    expect(ws.forceReconnect).not.toHaveBeenCalled();
    expect(ws.reconnect).not.toHaveBeenCalled();
  });

  it("DESKTOP + socket OPEN -> requestSessionState() ONLY (NO reconnect teardown)", () => {
    const ws = { ...makeResumeWs(), isOpen: true };
    render(<ResumeShell ws={ws} isMobile={false} />);
    act(() => {
      setVisibility("visible");
    });
    expect(ws.requestSessionState).toHaveBeenCalledOnce();
    expect(ws.reconnect).not.toHaveBeenCalled();
    expect(ws.forceReconnect).not.toHaveBeenCalled();
  });

  it("MOBILE + socket NOT OPEN -> forceReconnect() (dropped-socket revive)", () => {
    const ws = { ...makeResumeWs(), isOpen: false };
    render(<ResumeShell ws={ws} isMobile={true} />);
    act(() => {
      setVisibility("visible");
    });
    expect(ws.forceReconnect).toHaveBeenCalledOnce();
    expect(ws.requestSessionState).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// BUG 4a (Wave 4.9)  -  GraceWs-creation effect STABILITY.
//
// App.tsx mounts the GraceWs in a useEffect keyed on
// [bus, fanoutSourceSuggestion, useCases_onCaseList, useCases_onCaseOpen,
//  handleChartEmission, authEpoch]  -  all STABLE references. An unrelated state
// change (a re-render) must NOT re-run that effect (which would close + re-open
// the socket = the cycling this fix targets). We mirror the effect's deps +
// lifecycle in a shell with a mock GraceWs whose connect/close are spies, then
// trigger an unrelated re-render and assert connect() ran exactly once.
// ---------------------------------------------------------------------------

interface FakeConnectWs {
  connect: () => void;
  close: () => void;
}

/**
 * Mirror of App.tsx's GraceWs-creation effect lifecycle. The deps mimic the
 * production array: a stable `bus` (useMemo) + stable callbacks (useCallback)
 * + `authEpoch`. `bump` drives an UNRELATED re-render (mirrors a chat-width /
 * theme / layers state change)  -  it is NOT in the effect deps, so the effect
 * must not re-run.
 */
function ConnectStabilityShell({
  factory,
  authEpoch,
}: {
  factory: () => FakeConnectWs;
  authEpoch: number;
}): JSX.Element {
  // Stable references, exactly like App.tsx (useMemo([]) / useCallback([])).
  const bus = useRef({}).current;
  const cbA = useRef(() => undefined).current;
  const cbB = useRef(() => undefined).current;
  const [bump, setBump] = useState(0);
  useEffect(() => {
    const ws = factory();
    ws.connect();
    return () => {
      ws.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bus, cbA, cbB, authEpoch]);
  return (
    <button data-testid="bump" onClick={() => setBump((n) => n + 1)}>
      {bump}
    </button>
  );
}

describe("App GraceWs creation effect stability (BUG 4a)", () => {
  it("an UNRELATED re-render does NOT recreate the GraceWs (connect called once)", () => {
    const connect = vi.fn();
    const close = vi.fn();
    const factory = vi.fn<() => FakeConnectWs>(() => ({ connect, close }));

    render(<ConnectStabilityShell factory={factory} authEpoch={0} />);
    expect(connect).toHaveBeenCalledTimes(1);

    // Three unrelated re-renders (bump state changes  -  not an effect dep).
    act(() => {
      fireEvent.click(screen.getByTestId("bump"));
    });
    act(() => {
      fireEvent.click(screen.getByTestId("bump"));
    });
    act(() => {
      fireEvent.click(screen.getByTestId("bump"));
    });

    // The socket was created + connected EXACTLY ONCE; never closed/recreated.
    expect(factory).toHaveBeenCalledTimes(1);
    expect(connect).toHaveBeenCalledTimes(1);
    expect(close).not.toHaveBeenCalled();
  });

  it("a change to a real effect dep (authEpoch) DOES recreate the socket (re-sign-in recovery)", () => {
    const connect = vi.fn();
    const close = vi.fn();
    const factory = vi.fn<() => FakeConnectWs>(() => ({ connect, close }));

    const { rerender } = render(
      <ConnectStabilityShell factory={factory} authEpoch={0} />,
    );
    expect(connect).toHaveBeenCalledTimes(1);

    // authEpoch bump (the only intended recreate trigger) tears the old socket
    // down and opens a fresh one.
    rerender(<ConnectStabilityShell factory={factory} authEpoch={1} />);
    expect(close).toHaveBeenCalledTimes(1);
    expect(connect).toHaveBeenCalledTimes(2);
    expect(factory).toHaveBeenCalledTimes(2);
  });
});

// --- F84  -  Case-exit fresh slate (AOI cleared + layers emptied) ---------- //
//
// The full App mounts Chat (WebSocket) + MapView (WebGL), which cannot run in
// happy-dom, so  -  per the CollapseShell / AppShell convention above  -  we mirror
// App.tsx's `activeSession` case-rehydration effect (App.tsx:681-764) in a
// minimal harness over a mock bus and assert the emission CONTRACT:
//
//   - Exiting to the Cases root (activeSession -> null) pushes an EMPTY
//     session-state (loaded_layers:[]) so Map.tsx removes ALL overlays (raster
//     AND vector  -  the F84 Map.tsx fix), AND a `clear-analysis-extent` command
//     so the prior Case's AOI rectangle does not linger (fresh slate), AND a
//     `reset-view` so the camera snaps back to CONUS.
//   - Opening a Case WITH a bbox pushes the Case's layers AND a `zoom-to`
//     command carrying that bbox so the new/auto-gen Case shows its bounding
//     box via the existing zoom-to/extent path.
//
// This pins the App side of F84 (the Map.tsx side  -  vector removal on an empty
// set  -  is covered by Map.test.tsx).

interface MockBusCommand {
  command: string;
  args?: { bbox?: number[] };
}
interface MockBusSession {
  loaded_layers: Array<{ layer_id: string }>;
}

/** Records every session-state + map-command the harness pushes, in order. */
function makeRecordingBus() {
  const sessionPushes: MockBusSession[] = [];
  const commandPushes: MockBusCommand[] = [];
  return {
    sessionPushes,
    commandPushes,
    pushSessionState: (p: MockBusSession) => sessionPushes.push(p),
    pushMapCommand: (p: MockBusCommand) => commandPushes.push(p),
  };
}

type HarnessSession = {
  loaded_layers: Array<{ layer_id: string }>;
  case: { bbox: number[] | null };
  chat_history: unknown[];
} | null;

/**
 * Mirror of App.tsx's activeSession effect (the F84-relevant branches only:
 * empty-session clear on exit, and bbox->zoom-to on open). Charts/Impact resets
 * and the chat-history zoom-to replay fallback are intentionally omitted  -  they
 * are unrelated to the F84 fresh-slate contract under test.
 */
function CaseExitShell({
  bus,
  activeSession,
}: {
  bus: ReturnType<typeof makeRecordingBus>;
  activeSession: HarnessSession;
}): JSX.Element {
  useEffect(() => {
    if (activeSession === null) {
      // Exit to Cases root: empty layers + clear AOI + reset camera.
      bus.pushSessionState({ loaded_layers: [] });
      bus.pushMapCommand({ command: "clear-analysis-extent" });
      bus.pushMapCommand({ command: "reset-view" });
      return;
    }
    bus.pushSessionState({ loaded_layers: activeSession.loaded_layers });
    const bbox = activeSession.case.bbox;
    if (bbox && bbox.length === 4) {
      bus.pushMapCommand({ command: "zoom-to", args: { bbox } });
    } else {
      bus.pushMapCommand({ command: "clear-analysis-extent" });
    }
  }, [activeSession, bus]);
  return <div data-testid="case-exit-shell" />;
}

describe("App  -  Case-exit fresh slate contract (F84)", () => {
  it("exiting to Cases (activeSession -> null) pushes empty layers + clears the AOI + resets the view", () => {
    const bus = makeRecordingBus();
    render(<CaseExitShell bus={bus} activeSession={null} />);

    // Empty session-state -> Map.tsx removes ALL overlays (raster + vector).
    expect(bus.sessionPushes).toHaveLength(1);
    expect(bus.sessionPushes[0]!.loaded_layers).toEqual([]);
    // The AOI rectangle (not part of loaded_layers) is explicitly cleared.
    const cmds = bus.commandPushes.map((c) => c.command);
    expect(cmds).toContain("clear-analysis-extent");
    // Camera snaps back to CONUS.
    expect(cmds).toContain("reset-view");
    // No lingering AOI: no zoom-to is emitted on exit.
    expect(cmds).not.toContain("zoom-to");
  });

  it("opening a Case WITH a bbox shows its bounding box via zoom-to (no clear)", () => {
    const bus = makeRecordingBus();
    render(
      <CaseExitShell
        bus={bus}
        activeSession={{
          loaded_layers: [{ layer_id: "wdpa-new-case" }],
          case: { bbox: [-122.5, 37.7, -122.3, 37.85] },
          chat_history: [],
        }}
      />,
    );

    // The new Case's layers are pushed (replace-not-reconcile drops the old).
    expect(bus.sessionPushes[0]!.loaded_layers).toEqual([{ layer_id: "wdpa-new-case" }]);
    // The Case's bbox is shown via the existing zoom-to/extent path.
    const zoom = bus.commandPushes.find((c) => c.command === "zoom-to");
    expect(zoom).toBeDefined();
    expect(zoom!.args!.bbox).toEqual([-122.5, 37.7, -122.3, 37.85]);
    // A Case WITH an AOI does NOT clear  -  the zoom-to replaces the extent.
    expect(bus.commandPushes.map((c) => c.command)).not.toContain("clear-analysis-extent");
  });

  it("opening a Case with NO bbox clears any stale AOI from the prior Case", () => {
    const bus = makeRecordingBus();
    render(
      <CaseExitShell
        bus={bus}
        activeSession={{
          loaded_layers: [],
          case: { bbox: null },
          chat_history: [],
        }}
      />,
    );
    expect(bus.commandPushes.map((c) => c.command)).toContain("clear-analysis-extent");
    expect(bus.commandPushes.map((c) => c.command)).not.toContain("zoom-to");
  });
});

// --- JOB WEB-AOI-LEGEND (#159)  -  case-open snaps to the FINAL/floored bbox -- //
//
// App.tsx's case-open snap (App.tsx ~line 1000) now snaps to the FLOORED AOI:
//   1. Prefer activeSession.case.bbox VALIDATED via asBbox (the agent-AOI job
//      now persists the floored bbox there)  -  a null / malformed / non-finite
//      persisted bbox must NOT produce a broken fitBounds.
//   2. Else replay the LAST zoom-to (extractLastZoomTo walks newest-first, so
//      it returns the latest floored zoom-to, never the first/small pre-floor).
//   3. Else clear any stale AOI from the prior Case.
//
// This shell mirrors the FULL snap branch (unlike the F84 shell above, which
// intentionally omits the replay fallback) using the SAME pure helpers App.tsx
// imports, so the selection precedence is pinned without WebSocket/WebGL deps.

type SnapSession = {
  case: { bbox: number[] | null };
  chat_history: CaseChatMessage[];
};

/** Mirror of App.tsx's case-open snap selection (validated case.bbox first,
 *  else the LATEST zoom-to, else clear). Records the emitted command. */
function CaseOpenSnapShell({
  bus,
  activeSession,
}: {
  bus: ReturnType<typeof makeRecordingBus>;
  activeSession: SnapSession;
}): JSX.Element {
  useEffect(() => {
    const caseBbox = asBbox(activeSession.case.bbox);
    if (caseBbox) {
      bus.pushMapCommand({ command: "zoom-to", args: { bbox: caseBbox } });
    } else {
      const replay = extractLastZoomTo(activeSession.chat_history);
      if (replay) {
        bus.pushMapCommand(replay);
      } else {
        bus.pushMapCommand({ command: "clear-analysis-extent" });
      }
    }
  }, [activeSession, bus]);
  return <div data-testid="case-open-snap-shell" />;
}

let snapSeq = 0;
function snapMsg(emissions: unknown[]): CaseChatMessage {
  snapSeq += 1;
  return {
    message_id: `01SNAPMSG0${String(snapSeq).padStart(16, "0")}`,
    case_id: "01SNAPCASE000000000000000",
    role: "agent",
    content: "narration",
    created_at: "2026-06-20T00:00:00Z",
    map_command_emissions: emissions as never,
  } as CaseChatMessage;
}

describe("App  -  case-open snaps to the FINAL/floored bbox (#159)", () => {
  const SMALL: [number, number, number, number] = [-82.0, 26.55, -81.95, 26.6];
  const FLOORED: [number, number, number, number] = [-82.2, 26.4, -81.7, 26.8];

  it("prefers the persisted (floored) case.bbox when present", () => {
    const bus = makeRecordingBus();
    render(
      <CaseOpenSnapShell
        bus={bus}
        activeSession={{
          case: { bbox: FLOORED },
          // A small early zoom-to in history must be IGNORED in favor of case.bbox.
          chat_history: [snapMsg([{ command: "zoom-to", args: { bbox: SMALL } }])],
        }}
      />,
    );
    const zoom = bus.commandPushes.find((c) => c.command === "zoom-to");
    expect(zoom).toBeDefined();
    expect(zoom!.args!.bbox).toEqual(FLOORED);
  });

  it("falls through to the LAST (floored) zoom-to when case.bbox is null", () => {
    const bus = makeRecordingBus();
    render(
      <CaseOpenSnapShell
        bus={bus}
        activeSession={{
          case: { bbox: null },
          // Newest-first: the LATER FLOORED zoom-to wins over the earlier SMALL one.
          chat_history: [
            snapMsg([{ command: "zoom-to", args: { bbox: SMALL } }]),
            snapMsg([{ command: "zoom-to", args: { bbox: FLOORED } }]),
          ],
        }}
      />,
    );
    const zoom = bus.commandPushes.find((c) => c.command === "zoom-to");
    expect(zoom).toBeDefined();
    expect(zoom!.args!.bbox).toEqual(FLOORED);
  });

  it("treats a MALFORMED case.bbox as absent and uses the last zoom-to (no broken fitBounds)", () => {
    const bus = makeRecordingBus();
    render(
      <CaseOpenSnapShell
        bus={bus}
        activeSession={{
          // Non-finite / wrong-arity persisted bbox must NOT be snapped to.
          case: { bbox: [NaN, 26.4, -81.7, 26.8] },
          chat_history: [snapMsg([{ command: "zoom-to", args: { bbox: FLOORED } }])],
        }}
      />,
    );
    const zoom = bus.commandPushes.find((c) => c.command === "zoom-to");
    expect(zoom).toBeDefined();
    expect(zoom!.args!.bbox).toEqual(FLOORED);
  });

  it("clears the stale AOI when there is no bbox AND no zoom-to in history", () => {
    const bus = makeRecordingBus();
    render(
      <CaseOpenSnapShell
        bus={bus}
        activeSession={{ case: { bbox: null }, chat_history: [] }}
      />,
    );
    const cmds = bus.commandPushes.map((c) => c.command);
    expect(cmds).toContain("clear-analysis-extent");
    expect(cmds).not.toContain("zoom-to");
  });
});

// --- job-0357  -  per-Case layer DURABILITY across a WS reconnect ---------- //
//
// App.tsx stamps a client-only `replace_layers` flag onto every session-state
// it pushes onto the LayerPanel bus, derived from the live WebSocket status:
//   - server snapshot received while `connected`  -> replace_layers:true
//     (authoritative  -  live layer add AND delete apply via replace-not-
//     reconcile);
//   - server snapshot received while NOT `connected` (the disconnect /
//     reconnect window) -> replace_layers:false (additive top-up  -  Map.tsx
//     never tears down the active Case's already-rendered layers).
//
// The full App can't mount in happy-dom (WebSocket + WebGL), so  -  per the
// CollapseShell / ResumeShell / CaseExitShell convention above  -  this mirrors
// App.tsx's onStatus + onSessionState stamping over a recording bus and drives
// a simulated WS close + reopen to the SAME Case. The Map.tsx consumer side
// (additive-vs-replace reconcile) is pinned in Map.test.tsx.

type WireConnStatus =
  | "connecting"
  | "connected"
  | "disconnected"
  | "reconnecting";

interface StampedSession {
  loaded_layers: Array<{ layer_id: string }>;
  replace_layers?: boolean;
}

/**
 * Mirror of App.tsx's GraceWs onStatus + onSessionState handlers: status is
 * held in a ref, and every server session-state is stamped
 * `replace_layers: status === "connected"` before being pushed onto the bus.
 * The harness exposes imperative `setStatus` / `deliverSessionState` seams so
 * the test can script a close -> reconnect -> resume sequence deterministically.
 */
function DurabilityShell({
  onReady,
  push,
}: {
  onReady: (api: {
    setStatus: (s: WireConnStatus) => void;
    deliverSessionState: (
      p: { loaded_layers: Array<{ layer_id: string }> },
      fannedOut?: boolean,
    ) => void;
  }) => void;
  push: (p: StampedSession) => void;
}): JSX.Element {
  const statusRef = useRef<WireConnStatus>("connecting");
  const pushRef = useRef(push);
  pushRef.current = push;
  useEffect(() => {
    onReady({
      setStatus: (s) => {
        statusRef.current = s;
      },
      deliverSessionState: (p, fannedOut) => {
        // EXACT mirror of App.tsx onSessionState (CLIENT FLICKER FIX + ITEM 1
        // roads-flash eviction fix): a server-delivered snapshot is authoritative
        // (replace-not-reconcile) ONLY when it is THIS socket's OWN frame
        // (NOT hub-fanned-out from a stale sibling) AND the socket is `connected`
        // AND it actually carries layers. An EMPTY connected frame is a
        // NON-authoritative top-up (additive no-op); a FANNED-OUT frame is
        // likewise additive-only (a sibling's possibly-stale view must never evict
        // a layer this socket already has). Only the explicit Case switch/exit
        // path stamps replace_layers:true on its empty clear.
        pushRef.current({
          ...p,
          replace_layers:
            !fannedOut &&
            statusRef.current === "connected" &&
            (p.loaded_layers?.length ?? 0) > 0,
        });
      },
    });
  }, [onReady]);
  return <div data-testid="durability-shell" />;
}

describe("App  -  per-Case layer durability across WS reconnect (job-0357)", () => {
  it("stamps replace_layers:true on a server snapshot while CONNECTED", () => {
    const pushes: StampedSession[] = [];
    let api!: Parameters<Parameters<typeof DurabilityShell>[0]["onReady"]>[0];
    render(
      <DurabilityShell onReady={(a) => (api = a)} push={(p) => pushes.push(p)} />,
    );
    act(() => {
      api.setStatus("connected");
      api.deliverSessionState({ loaded_layers: [{ layer_id: "flood-demo" }] });
    });
    expect(pushes).toHaveLength(1);
    expect(pushes[0]!.replace_layers).toBe(true);
  });

  it("layers SURVIVE a simulated WS close + reopen to the SAME Case (no clearing snapshot)", () => {
    const pushes: StampedSession[] = [];
    let api!: Parameters<Parameters<typeof DurabilityShell>[0]["onReady"]>[0];
    render(
      <DurabilityShell onReady={(a) => (api = a)} push={(p) => pushes.push(p)} />,
    );

    // 1. Connected: the Case's layer arrives authoritatively.
    act(() => {
      api.setStatus("connected");
      api.deliverSessionState({ loaded_layers: [{ layer_id: "flood-demo" }] });
    });

    // 2. The socket drops (close) then starts reconnecting. During this
    //    window any server snapshot is NON-authoritative  -  an empty/partial
    //    one must NOT be allowed to wipe the durable layer.
    act(() => {
      api.setStatus("disconnected");
      api.deliverSessionState({ loaded_layers: [] }); // transient empty
      api.setStatus("reconnecting");
    });
    // The transient empty snapshot was stamped non-authoritative.
    const transient = pushes[1]!;
    expect(transient.loaded_layers).toEqual([]);
    expect(transient.replace_layers).toBe(false);

    // 3. Reconnect completes and the agent replays the FULL persisted layer
    //    set as a normal session-state. By the time it is processed the
    //    socket is `connected` again, so it lands authoritative  -  and because
    //    it carries the same layer it reconciles idempotently (no wipe).
    act(() => {
      api.setStatus("connected");
      api.deliverSessionState({ loaded_layers: [{ layer_id: "flood-demo" }] });
    });
    const resume = pushes[2]!;
    expect(resume.loaded_layers).toEqual([{ layer_id: "flood-demo" }]);
    expect(resume.replace_layers).toBe(true);
    // No snapshot in the whole sequence both EMPTIED layers AND claimed to be
    // an authoritative replace  -  i.e. nothing could have blanked the map.
    const blanking = pushes.find(
      (p) => p.replace_layers === true && p.loaded_layers.length === 0,
    );
    expect(blanking).toBeUndefined();
  });

  it("an EMPTY connected frame is a NO-OP (layers persist until an explicit Case switch)", () => {
    // NEW semantics (CLIENT FLICKER FIX): the server re-ships a full
    // session-state on every resume INCLUDING the 25s keepalive heartbeat, and a
    // heartbeat (or a reconnect mid-flight) can momentarily carry an EMPTY
    // loaded_layers for the SAME Case. Under the OLD `replace_layers = connected`
    // stamp that wiped the map then refilled on the next good frame (the flicker
    // + a durability-HARD-REQ violation). The stamp now also requires the frame
    // to actually CARRY layers, so an EMPTY connected frame is NON-authoritative
    // (replace_layers:false): Map.tsx treats it as an additive no-op and the
    // already-rendered overlays survive. A real DELETE is delivered NOT as an
    // empty session-state but via the explicit `layer-delete` envelope ->
    // server-persisted list -> a fresh snapshot that still carries the REMAINING
    // layers (or the explicit Case-switch clear, which stamps replace_layers:true
    // itself - see the F84 Case-exit tests above).
    const pushes: StampedSession[] = [];
    let api!: Parameters<Parameters<typeof DurabilityShell>[0]["onReady"]>[0];
    render(
      <DurabilityShell onReady={(a) => (api = a)} push={(p) => pushes.push(p)} />,
    );
    act(() => {
      api.setStatus("connected");
      api.deliverSessionState({ loaded_layers: [{ layer_id: "flood-demo" }] });
      // A keepalive/heartbeat empty frame for the SAME Case - must NOT blank.
      api.deliverSessionState({ loaded_layers: [] });
    });
    // The first frame carried a layer while connected -> authoritative replace.
    expect(pushes[0]!.loaded_layers).toEqual([{ layer_id: "flood-demo" }]);
    expect(pushes[0]!.replace_layers).toBe(true);
    // The empty connected frame is a NON-authoritative no-op (additive top-up):
    // Map.tsx keeps the durable layer, so this can never blank the map.
    expect(pushes[1]!.loaded_layers).toEqual([]);
    expect(pushes[1]!.replace_layers).toBe(false);
  });

  it("a FANNED-OUT session-state stays additive-only and never evicts a live-added vector (roads-flash fix)", () => {
    // ITEM 1 (NATE 2026-06-22): the tab runs two WS connections (Chat runs the
    // turn, App renders). A live-added vector (roads) lands on the Chat socket
    // and FANS OUT to App (paints). ~25s later App's OWN keepalive resume reply,
    // built from App's STALE emitter, carries the flood RASTER only (NOT roads).
    // Under the old stamp that App-own frame was authoritative (connected +
    // non-empty), so mergeSnapshot evicted roads (flash-then-vanish).
    const pushes: StampedSession[] = [];
    let api!: Parameters<Parameters<typeof DurabilityShell>[0]["onReady"]>[0];
    render(
      <DurabilityShell onReady={(a) => (api = a)} push={(p) => pushes.push(p)} />,
    );
    act(() => {
      api.setStatus("connected");
      // The flood raster arrives natively on App's own socket (authoritative).
      api.deliverSessionState({ loaded_layers: [{ layer_id: "flood-raster" }] });
      // The roads vector is added on the CHAT socket and FANS OUT to App.
      api.deliverSessionState(
        { loaded_layers: [{ layer_id: "roads-vector" }] },
        /* fannedOut */ true,
      );
      // App's own keepalive resume reply, from App's STALE emitter  -  flood only,
      // NO roads. This is the frame that USED to evict roads.
      api.deliverSessionState({ loaded_layers: [{ layer_id: "flood-raster" }] });
    });

    // The fanned-out roads frame is NON-authoritative (additive-only)  -  it adds
    // roads to the cache but, being non-authoritative, can never evict on its own.
    expect(pushes[1]!.loaded_layers).toEqual([{ layer_id: "roads-vector" }]);
    expect(pushes[1]!.replace_layers).toBe(false);
    // App's own stale resume frame omits roads but is authoritative; the seatbelt
    // (mergeSnapshot, pinned in layer_cache.test.ts) keeps roads because no
    // fanned-out add was ever stamped authoritative. The key guarantee here: the
    // ONLY frame that could evict roads (the own stale resume) carries the flood
    // layer that roads was added ALONGSIDE  -  never a full authoritative replace
    // that the fanned roads add participated in. No fanned frame is authoritative.
    const fannedAuthoritative = [pushes[1]!].find((p) => p.replace_layers === true);
    expect(fannedAuthoritative).toBeUndefined();
  });
});

// --- PART B (NATE 2026-06-22): no case layers at the cases-list / root view -- //
//
// NATE: "no case layers should be loaded when we are in the cases section; they
// should only be rendered when we have entered a Case." App.tsx's layer-lift
// effect now gates on the shared cache's activeCaseId: at root (null) it forces
// the LayerPanel-feeding `layers` list EMPTY regardless of the incoming snapshot;
// inside a Case it merges through the seatbelt as before. This shell mirrors the
// EXACT gate (App.tsx layer-lift) over the REAL LayerCache so the contract is
// pinned without WebSocket/WebGL deps. (The Map.tsx overlay/legend side of the
// gate is pinned in Map.test.tsx.)
describe("App  -  cases-root layer gate (PART B)", () => {
  function liftLayers(
    cache: LayerCache,
    incoming: Array<{ layer_id: string }>,
    replaceLayers = true,
  ): Array<{ layer_id: string }> {
    // EXACT mirror of App.tsx's bus.subscribeSessionState layer-lift.
    const caseId = cache.activeCaseId;
    if (caseId === null) return [];
    const authoritativeReplace = replaceLayers !== false;
    return cache.mergeSnapshot(
      caseId,
      incoming as unknown as Parameters<LayerCache["mergeSnapshot"]>[1],
      { authoritativeReplace },
    ) as unknown as Array<{ layer_id: string }>;
  }

  it("returns EMPTY layers when no Case is active (root view), even with incoming layers", () => {
    const cache = new LayerCache();
    cache.activeCaseId = null; // cases-list / root view
    expect(liftLayers(cache, [{ layer_id: "old-case-flood" }])).toEqual([]);
  });

  it("returns the merged layers once a Case is entered (activeCaseId set)", () => {
    const cache = new LayerCache();
    cache.activeCaseId = "case-A";
    const out = liftLayers(cache, [{ layer_id: "flood-demo" }]);
    expect(out.map((l) => l.layer_id)).toEqual(["flood-demo"]);
  });

  it("clears the lifted layers the instant the Case is exited (active -> null)", () => {
    const cache = new LayerCache();
    cache.activeCaseId = "case-A";
    expect(liftLayers(cache, [{ layer_id: "flood-demo" }]).length).toBe(1);
    // Exit to the cases list.
    cache.activeCaseId = null;
    expect(liftLayers(cache, [{ layer_id: "flood-demo" }])).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// CASE-SWITCH LAYER LEAK FIX (NATE 2026-06-19).
//
// NATE: switching Case A (urban flood) -> Case B (Mexico Beach) loaded B's
// layers, then A's layers RE-ASSERTED and cleared B's. Root cause: the live WS
// `onSessionState(p, caseId)` handler ignored the envelope-level `case_id`, so a
// TRAILING server snapshot STILL tagged with Case A (a late solve-finish frame,
// or the resume replay racing the new Case's case-open) was applied authoritative
// over B. The fix drops any snapshot whose tag != the active Case. This shell
// mirrors the EXACT filter App.tsx now applies in its onSessionState handler.
// ---------------------------------------------------------------------------

function CaseFilterShell({
  onReady,
  push,
}: {
  onReady: (api: {
    setStatus: (s: WireConnStatus) => void;
    setActiveCase: (id: string | null) => void;
    deliverSessionState: (
      p: { loaded_layers: Array<{ layer_id: string }> },
      caseId: string | null,
    ) => void;
  }) => void;
  push: (p: StampedSession) => void;
}): JSX.Element {
  const statusRef = useRef<WireConnStatus>("connecting");
  const activeCaseIdRef = useRef<string | null>(null);
  const pushRef = useRef(push);
  pushRef.current = push;
  useEffect(() => {
    onReady({
      setStatus: (s) => {
        statusRef.current = s;
      },
      setActiveCase: (id) => {
        activeCaseIdRef.current = id;
      },
      deliverSessionState: (p, caseId) => {
        // EXACT mirror of App.tsx onSessionState (CASE-SWITCH LAYER LEAK FIX):
        // drop a snapshot tagged with a Case that is not the active one; both
        // non-null mismatch => ignore. Untagged frames + the root view apply.
        const active = activeCaseIdRef.current;
        if (caseId != null && active != null && caseId !== active) return;
        pushRef.current({
          ...p,
          replace_layers:
            statusRef.current === "connected" &&
            (p.loaded_layers?.length ?? 0) > 0,
        });
      },
    });
  }, [onReady]);
  return <div data-testid="case-filter-shell" />;
}

describe("App  -  case-switch layer leak (NATE 2026-06-19)", () => {
  it("DROPS a trailing snapshot tagged with the PREVIOUS Case after switching", () => {
    const pushes: StampedSession[] = [];
    let api!: Parameters<Parameters<typeof CaseFilterShell>[0]["onReady"]>[0];
    render(
      <CaseFilterShell onReady={(a) => (api = a)} push={(p) => pushes.push(p)} />,
    );
    act(() => {
      api.setStatus("connected");
      // Open Case A and paint its layer.
      api.setActiveCase("caseA");
      api.deliverSessionState({ loaded_layers: [{ layer_id: "urban-flood" }] }, "caseA");
      // User switches to Case B; B's layers paint.
      api.setActiveCase("caseB");
      api.deliverSessionState({ loaded_layers: [{ layer_id: "mexico-beach" }] }, "caseB");
      // A TRAILING snapshot for Case A arrives (late solve-finish / resume race).
      api.deliverSessionState({ loaded_layers: [{ layer_id: "urban-flood" }] }, "caseA");
    });
    // Exactly TWO snapshots reached the bus: Case A's (while A active) and
    // Case B's (while B active). The trailing Case-A snapshot was DROPPED.
    expect(pushes).toHaveLength(2);
    expect(pushes[0]!.loaded_layers).toEqual([{ layer_id: "urban-flood" }]);
    expect(pushes[1]!.loaded_layers).toEqual([{ layer_id: "mexico-beach" }]);
    // The map's last authoritative state is Case B's layers  -  never re-asserted
    // back to Case A.
    const last = pushes[pushes.length - 1]!;
    expect(last.loaded_layers).toEqual([{ layer_id: "mexico-beach" }]);
  });

  it("APPLIES an untagged snapshot (older builds / root view)  -  durability unaffected", () => {
    const pushes: StampedSession[] = [];
    let api!: Parameters<Parameters<typeof CaseFilterShell>[0]["onReady"]>[0];
    render(
      <CaseFilterShell onReady={(a) => (api = a)} push={(p) => pushes.push(p)} />,
    );
    act(() => {
      api.setStatus("connected");
      api.setActiveCase("caseB");
      // An UNTAGGED frame (caseId null) for the active Case must still apply  - 
      // a reconnect resume for the SAME Case is either tagged caseB or untagged.
      api.deliverSessionState({ loaded_layers: [{ layer_id: "mexico-beach" }] }, null);
    });
    expect(pushes).toHaveLength(1);
    expect(pushes[0]!.loaded_layers).toEqual([{ layer_id: "mexico-beach" }]);
  });

  it("APPLIES a snapshot tagged with the ACTIVE Case (the normal live path)", () => {
    const pushes: StampedSession[] = [];
    let api!: Parameters<Parameters<typeof CaseFilterShell>[0]["onReady"]>[0];
    render(
      <CaseFilterShell onReady={(a) => (api = a)} push={(p) => pushes.push(p)} />,
    );
    act(() => {
      api.setStatus("connected");
      api.setActiveCase("caseB");
      api.deliverSessionState({ loaded_layers: [{ layer_id: "mexico-beach" }] }, "caseB");
    });
    expect(pushes).toHaveLength(1);
    expect(pushes[0]!.replace_layers).toBe(true);
  });
});
