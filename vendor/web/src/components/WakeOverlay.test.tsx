// GRACE-2 web - WakeOverlay tests (not-connected composer overlay; redesigned
// 2026-06-19 per `project_wake_composer_redesign`).
//
// Verifies the SINGLE overlay's phase machine + interactions:
//   - "hidden"     → renders nothing.
//   - "connecting" → "Connecting" card, yellow shimmer edge + spinner, NOT a
//                    button; tap is inert.
//   - "asleep"     → tap-to-wake "Wake up" card with a STATIC model-color edge;
//                    click / Enter / Space fire onWake.
//   - "waking"     → "Waking up" card, rainbow shimmer edge (no icon, no
//                    spinner); NOT a button; tap is inert.
//   - mid-transparency → the overlay scrim is ~0.45 alpha with no heavy
//                    blur/dim.
//   - single card → "Waking up" is the SAME card shape as the others (one
//                    [wake-overlay-rect], no card-in-card).
//   - NO subtext → only the word renders (no sub-lines).
//   - fade-out  → flipping to "hidden" keeps the node briefly mounted (opacity
//                 transition) then unmounts.
//   - prefers-reduced-motion → no shimmer/spinner; static edges; word still
//                 communicates state.

import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, fireEvent, cleanup, act } from "@testing-library/react";
import { WakeOverlay, WAKE_FADE_MS } from "./WakeOverlay";

function mockReducedMotion(reduced: boolean): void {
  // happy-dom has no matchMedia by default; install a minimal stub.
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: (query: string) => ({
      matches: reduced && query.includes("prefers-reduced-motion"),
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
      onchange: null,
    }),
  });
}

const ACCENT = "#c2603c";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("WakeOverlay - phase rendering", () => {
  it("renders nothing in the 'hidden' phase", () => {
    mockReducedMotion(false);
    render(<WakeOverlay phase="hidden" onWake={() => {}} accentColor={ACCENT} />);
    expect(screen.queryByTestId("wake-overlay")).toBeNull();
  });

  it("shows the 'Connecting' card with a spinner in 'connecting'", () => {
    mockReducedMotion(false);
    render(
      <WakeOverlay phase="connecting" onWake={() => {}} accentColor={ACCENT} />,
    );
    const overlay = screen.getByTestId("wake-overlay");
    expect(overlay).toHaveAttribute("data-phase", "connecting");
    expect(screen.getByText("Connecting")).toBeInTheDocument();
    // Status surface, not a button.
    const rect = screen.getByTestId("wake-overlay-rect");
    expect(rect).toHaveAttribute("role", "status");
    expect(rect).toHaveAttribute("tabindex", "-1");
    // Connecting = the only phase with the spinner.
    expect(screen.getByTestId("wake-overlay-spinner")).toBeInTheDocument();
  });

  it("shows the tap-to-wake 'Wake up' card in 'asleep'", () => {
    mockReducedMotion(false);
    render(<WakeOverlay phase="asleep" onWake={() => {}} accentColor={ACCENT} />);
    const overlay = screen.getByTestId("wake-overlay");
    expect(overlay).toHaveAttribute("data-phase", "asleep");
    expect(screen.getByText("Wake up")).toBeInTheDocument();
    const rect = screen.getByTestId("wake-overlay-rect");
    expect(rect).toHaveAttribute("role", "button");
    expect(rect).toHaveAttribute("tabindex", "0");
    // No spinner in the wake/asleep phase (static model-color edge instead).
    expect(screen.queryByTestId("wake-overlay-spinner")).toBeNull();
  });

  it("shows the 'Waking up' card (single card, rainbow edge, no spinner) in 'waking'", () => {
    mockReducedMotion(false);
    render(<WakeOverlay phase="waking" onWake={() => {}} accentColor={ACCENT} />);
    expect(screen.getByTestId("wake-overlay")).toHaveAttribute(
      "data-phase",
      "waking",
    );
    // Status surface, not a button.
    const rect = screen.getByTestId("wake-overlay-rect");
    expect(rect).toHaveAttribute("role", "status");
    expect(screen.getByText("Waking up")).toBeInTheDocument();
    // Single card - exactly ONE wake-overlay-rect (no card-in-card).
    expect(screen.getAllByTestId("wake-overlay-rect")).toHaveLength(1);
    // No spinner in waking (rainbow EDGE shimmer carries the motion instead).
    expect(screen.queryByTestId("wake-overlay-spinner")).toBeNull();
  });
});

describe("WakeOverlay - redesign invariants", () => {
  it("REPLACES the composer in place - no scrim, no overlay backdrop (NATE 2026-06-19)", () => {
    mockReducedMotion(false);
    render(
      <WakeOverlay phase="connecting" onWake={() => {}} accentColor={ACCENT} />,
    );
    const overlay = screen.getByTestId("wake-overlay") as HTMLElement;
    // NOT an overlay: the wrapper is an in-flow block with NO dim/scrim
    // background (the parent renders this INSTEAD of <ChatInput>, so there is
    // nothing underneath to scrim over).
    expect(overlay.style.background.replace(/\s+/g, "")).not.toContain(
      "rgba(14,15,20,0.45)",
    );
    expect(overlay.style.position).not.toBe("absolute");
    expect(
      overlay.style.backdropFilter === "" || overlay.style.backdropFilter == null,
    ).toBe(true);
    // The box spans the composer slot (it IS the composer box with swapped
    // content), not a floating mini-card.
    const rect = screen.getByTestId("wake-overlay-rect") as HTMLElement;
    expect(rect.style.width).toBe("100%");
  });

  it("renders ONLY the phase word - no subtext sub-lines", () => {
    mockReducedMotion(false);
    for (const [phase, word] of [
      ["connecting", "Connecting"],
      ["asleep", "Wake up"],
      ["waking", "Waking up"],
    ] as const) {
      const { unmount } = render(
        <WakeOverlay phase={phase} onWake={() => {}} accentColor={ACCENT} />,
      );
      // No prior-design subtext leaked through.
      expect(screen.queryByText(/cold start/i)).toBeNull();
      expect(screen.queryByText(/save costs/i)).toBeNull();
      expect(screen.queryByText(/Starting the agent/i)).toBeNull();
      expect(screen.getByText(word)).toBeInTheDocument();
      unmount();
    }
  });

  it("carries the gradient EDGE element in every visible phase", () => {
    mockReducedMotion(false);
    for (const phase of ["connecting", "asleep", "waking"] as const) {
      const { unmount } = render(
        <WakeOverlay phase={phase} onWake={() => {}} accentColor={ACCENT} />,
      );
      expect(screen.getByTestId("wake-overlay-edge")).toBeInTheDocument();
      unmount();
    }
  });

  it("the 'asleep'/'wake' edge is STATIC and tinted by the model accentColor", () => {
    mockReducedMotion(false);
    render(<WakeOverlay phase="asleep" onWake={() => {}} accentColor={ACCENT} />);
    const edge = screen.getByTestId("wake-overlay-edge") as HTMLElement;
    // Static (no shimmer animation) and built from the supplied accent color.
    expect(edge.style.animation === "" || edge.style.animation == null).toBe(true);
    expect(edge.style.backgroundImage).toContain(ACCENT);
  });

  it("the 'connecting' edge shimmers (yellow) and 'waking' shimmers (rainbow)", () => {
    mockReducedMotion(false);
    const { rerender } = render(
      <WakeOverlay phase="connecting" onWake={() => {}} accentColor={ACCENT} />,
    );
    let edge = screen.getByTestId("wake-overlay-edge") as HTMLElement;
    expect(edge.style.animation).toContain("grace2-wake-edge-shimmer");
    expect(edge.style.backgroundImage).toContain("#f5c542"); // yellow tone

    rerender(
      <WakeOverlay phase="waking" onWake={() => {}} accentColor={ACCENT} />,
    );
    edge = screen.getByTestId("wake-overlay-edge") as HTMLElement;
    expect(edge.style.animation).toContain("grace2-wake-edge-shimmer");
    // Rainbow stop present.
    expect(edge.style.backgroundImage).toContain("#4D96FF");
  });
});

describe("WakeOverlay - interactions", () => {
  it("fires onWake on click in 'asleep'", () => {
    mockReducedMotion(false);
    const onWake = vi.fn();
    render(<WakeOverlay phase="asleep" onWake={onWake} accentColor={ACCENT} />);
    fireEvent.click(screen.getByTestId("wake-overlay-rect"));
    expect(onWake).toHaveBeenCalledTimes(1);
  });

  it("fires onWake on Enter and Space (keyboard) in 'asleep'", () => {
    mockReducedMotion(false);
    const onWake = vi.fn();
    render(<WakeOverlay phase="asleep" onWake={onWake} accentColor={ACCENT} />);
    const rect = screen.getByTestId("wake-overlay-rect");
    fireEvent.keyDown(rect, { key: "Enter" });
    fireEvent.keyDown(rect, { key: " " });
    expect(onWake).toHaveBeenCalledTimes(2);
  });

  it("does NOT fire onWake when tapped in 'waking' (already waking)", () => {
    mockReducedMotion(false);
    const onWake = vi.fn();
    render(<WakeOverlay phase="waking" onWake={onWake} accentColor={ACCENT} />);
    fireEvent.click(screen.getByTestId("wake-overlay-rect"));
    expect(onWake).not.toHaveBeenCalled();
  });

  it("does NOT fire onWake when tapped in 'connecting'", () => {
    mockReducedMotion(false);
    const onWake = vi.fn();
    render(
      <WakeOverlay phase="connecting" onWake={onWake} accentColor={ACCENT} />,
    );
    fireEvent.click(screen.getByTestId("wake-overlay-rect"));
    expect(onWake).not.toHaveBeenCalled();
  });
});

describe("WakeOverlay - fade-out on 'hidden'", () => {
  it("keeps the node mounted (opacity 0) through the fade then unmounts", () => {
    mockReducedMotion(false);
    vi.useFakeTimers();
    const { rerender } = render(
      <WakeOverlay phase="waking" onWake={() => {}} accentColor={ACCENT} />,
    );
    expect(screen.getByTestId("wake-overlay")).toBeInTheDocument();

    // Flip to hidden - overlay stays mounted at opacity 0 during the fade.
    rerender(<WakeOverlay phase="hidden" onWake={() => {}} accentColor={ACCENT} />);
    const fading = screen.getByTestId("wake-overlay");
    expect(fading).toHaveStyle({ opacity: "0" });

    // After the fade duration it unmounts.
    act(() => {
      vi.advanceTimersByTime(WAKE_FADE_MS + 10);
    });
    expect(screen.queryByTestId("wake-overlay")).toBeNull();
  });
});

describe("WakeOverlay - prefers-reduced-motion", () => {
  it("declares no shimmer/spinner but still shows state text (static edge)", () => {
    mockReducedMotion(true);
    render(<WakeOverlay phase="waking" onWake={() => {}} accentColor={ACCENT} />);
    const edge = screen.getByTestId("wake-overlay-edge") as HTMLElement;
    // No edge shimmer animation under reduced motion.
    expect(edge.style.animation === "" || edge.style.animation == null).toBe(
      true,
    );
    // Reduced-motion edge falls back to the static accent color.
    expect(edge.style.backgroundImage).toContain(ACCENT);
    // No spinner even though waking → connecting reduced path; waking never
    // had a spinner, and connecting suppresses it under reduced motion.
    expect(screen.queryByTestId("wake-overlay-spinner")).toBeNull();
    // State is still communicated.
    expect(screen.getByText("Waking up")).toBeInTheDocument();
  });

  it("suppresses the connecting spinner under reduced motion", () => {
    mockReducedMotion(true);
    render(
      <WakeOverlay phase="connecting" onWake={() => {}} accentColor={ACCENT} />,
    );
    expect(screen.queryByTestId("wake-overlay-spinner")).toBeNull();
    expect(screen.getByText("Connecting")).toBeInTheDocument();
  });

  it("unmounts immediately on 'hidden' under reduced motion (no lingering layer)", () => {
    mockReducedMotion(true);
    const { rerender } = render(
      <WakeOverlay phase="asleep" onWake={() => {}} accentColor={ACCENT} />,
    );
    expect(screen.getByTestId("wake-overlay")).toBeInTheDocument();
    rerender(<WakeOverlay phase="hidden" onWake={() => {}} accentColor={ACCENT} />);
    expect(screen.queryByTestId("wake-overlay")).toBeNull();
  });
});
