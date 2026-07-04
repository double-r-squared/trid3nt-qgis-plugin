// GRACE-2 web - BboxProgressOverlay render tests.
//
// NATE 2026-06-24 simplification: ONLY the polished GRID ("fill") renders now;
// the SCAN mode is gone (resolveBboxProgress never returns it, and the component
// no longer draws anything for it). These tests assert the grid renders + is
// polished, and that any stray "scan" mode draws NOTHING.

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { BboxProgressOverlay } from "./BboxProgressOverlay";
import type { ScreenRect } from "../lib/legend_snap";

const RECT: ScreenRect = { left: 100, top: 100, right: 300, bottom: 260 };

describe("BboxProgressOverlay - grid only (scan removed)", () => {
  it("renders nothing when mode is none", () => {
    render(<BboxProgressOverlay rect={RECT} mode="none" tone="blue" />);
    expect(screen.queryByTestId("grace2-bbox-progress-overlay")).toBeNull();
  });

  it("renders nothing when there is no rect", () => {
    render(<BboxProgressOverlay rect={null} mode="fill" tone="blue" />);
    expect(screen.queryByTestId("grace2-bbox-progress-overlay")).toBeNull();
  });

  it("renders a FILL grid overlay anchored to the rect", () => {
    render(<BboxProgressOverlay rect={RECT} mode="fill" tone="blue" />);
    const el = screen.getByTestId("grace2-bbox-progress-overlay");
    expect(el.getAttribute("data-mode")).toBe("fill");
    expect(el.style.left).toBe("100px");
    expect(el.style.top).toBe("100px");
    expect(el.style.width).toBe("200px");
    expect(el.style.height).toBe("160px");
  });

  it("the FILL grid animates (motion allowed) and never intercepts pointers", () => {
    render(
      <BboxProgressOverlay
        rect={RECT}
        mode="fill"
        tone="blue"
        reducedMotionOverride={false}
      />,
    );
    const el = screen.getByTestId("grace2-bbox-progress-overlay");
    // The polished grid shimmer animation is applied (the ONE drifting cue).
    expect(el.style.animation).toContain("grace2-bbox-fill-shimmer");
    // A clean single frame: a thin border + a soft inset glow (the polish), and
    // it never intercepts pointers. (The multi-layer linear-gradient grid
    // BACKGROUND is set in code but happy-dom's CSS parser drops the unparsable
    // multi-layer `background` shorthand, so we assert the serializable polish
    // props instead - the keyframe test below proves the grid shimmer exists.)
    expect(el.style.border).toContain("solid");
    expect(el.style.boxShadow).toContain("inset");
    expect(el.style.pointerEvents).toBe("none");
  });

  // SCAN IS GONE: passing the (now never-produced) "scan" mode renders nothing,
  // so there is no second box / sweep / pulsing border anywhere.
  it("mode 'scan' renders NOTHING (the scan path was removed)", () => {
    render(
      <BboxProgressOverlay
        rect={RECT}
        mode="scan"
        tone="blue"
        reducedMotionOverride={false}
      />,
    );
    expect(screen.queryByTestId("grace2-bbox-progress-overlay")).toBeNull();
    // The sweeping scan bar no longer exists at all.
    expect(screen.queryByTestId("grace2-bbox-progress-sweep")).toBeNull();
  });

  it("mode 'scan' renders nothing for the purple (sim) tone either", () => {
    render(
      <BboxProgressOverlay
        rect={RECT}
        mode="scan"
        tone="purple"
        reducedMotionOverride={false}
      />,
    );
    expect(screen.queryByTestId("grace2-bbox-progress-overlay")).toBeNull();
    expect(screen.queryByTestId("grace2-bbox-progress-sweep")).toBeNull();
  });

  it("the scan-sweep + border-pulse keyframes are GONE (grid keyframe only)", () => {
    render(<BboxProgressOverlay rect={RECT} mode="fill" tone="blue" />);
    const style = document.getElementById("grace2-bbox-progress-keyframes");
    expect(style).not.toBeNull();
    const css = style?.textContent ?? "";
    // The one remaining keyframe is the grid shimmer.
    expect(css).toContain("grace2-bbox-fill-shimmer");
    // The removed scan keyframes are absent.
    expect(css).not.toContain("grace2-bbox-scan-sweep");
    expect(css).not.toContain("grace2-bbox-border-pulse");
  });

  it("reduced-motion: fill degrades to a static grid tint (no animation)", () => {
    render(
      <BboxProgressOverlay
        rect={RECT}
        mode="fill"
        tone="blue"
        reducedMotionOverride={true}
      />,
    );
    const el = screen.getByTestId("grace2-bbox-progress-overlay");
    expect(el.getAttribute("data-reduced")).toBe("true");
    expect(el.style.animation === "" || el.style.animation === undefined).toBe(true);
    // The static grid frame still reads (border + inset glow); the lattice
    // BACKGROUND is set in code but happy-dom drops the multi-layer shorthand.
    expect(el.style.border).toContain("solid");
  });

  it("never intercepts pointer events (fill)", () => {
    render(<BboxProgressOverlay rect={RECT} mode="fill" tone="blue" />);
    const el = screen.getByTestId("grace2-bbox-progress-overlay");
    expect(el.style.pointerEvents).toBe("none");
  });

  it("renders nothing for a degenerate (zero-area) rect", () => {
    render(
      <BboxProgressOverlay
        rect={{ left: 100, top: 100, right: 100, bottom: 100 }}
        mode="fill"
        tone="blue"
      />,
    );
    expect(screen.queryByTestId("grace2-bbox-progress-overlay")).toBeNull();
  });
});
