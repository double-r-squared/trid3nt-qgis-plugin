// GRACE-2 web — mobile bottom-sheet tests (job-0278, mobile-friendly UI).
//
// Chat itself cannot mount in happy-dom (it opens a WebSocket), so — per the
// established pure-helper pattern (pipelineReducer / buildInterleavedStream)
// — these tests pin the EXPORTED sheet primitives Chat composes:
//
//   - mobileSheetContainerStyle(expanded): bottom-pinned, full-width,
//     70vh when expanded / content-height when collapsed;
//   - SheetToggleHandle: 44px toggle with aria-expanded, fires onToggle;
//   - a stateful harness covering the collapsed → expanded → collapsed
//     cycle exactly the way Chat wires it (handle + conditional scroll
//     visibility via display, content kept mounted).

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, act } from "@testing-library/react";
import { useState } from "react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  MOBILE_SHEET_EXPANDED_HEIGHT,
  MobileSheetHeaderRow,
  SANDBOX_PULSE_ANIMATION,
  SANDBOX_PULSE_KEYFRAMES_ID,
  SHEET_BOTTOM_EXTRA_PX,
  SHEET_BOTTOM_OFFSET_CSS,
  SHEET_DRAG_THRESHOLD_PX,
  SheetActiveSandboxStrip,
  SheetActiveStripStack,
  SheetActiveToolStrip,
  SheetToggleHandle,
  buildActiveStripStack,
  clampSheetHeight,
  findRunningSandboxes,
  findRunningToolStep,
  isSandboxRunning,
  isSheetDragGesture,
  mobileSheetContainerStyle,
  readSheetHeight,
  stepInterleaveKey,
  writeSheetHeight,
  type ActiveStripItem,
} from "./Chat";
import { DEFAULT_MODEL_ID } from "./lib/modelRegistry";
import type {
  CodeExecRequestPayload,
  CodeExecResultPayload,
  SandboxCardDecision,
} from "./components/SandboxCard";
import type {
  PipelineStatePayload,
  PipelineStepState,
  PipelineStepSummary,
} from "./contracts";

afterEach(() => cleanup());

// F44 — a TAP is a pointerdown + pointerup at the SAME spot (no
// threshold-crossing travel). The handle distinguishes it from a drag.
function tap(el: Element): void {
  fireEvent.pointerDown(el, { clientX: 100, clientY: 500, pointerId: 1 });
  fireEvent.pointerUp(el, { clientX: 100, clientY: 500, pointerId: 1 });
}

// F44 — a vertical DRAG: pointerdown, a move past the threshold, pointerup.
// clientY decreasing = pointer moving UP = taller sheet (bottom-anchored).
function dragVertical(el: Element, fromY: number, toY: number): void {
  fireEvent.pointerDown(el, { clientX: 100, clientY: fromY, pointerId: 1 });
  fireEvent.pointerMove(el, { clientX: 100, clientY: toY, pointerId: 1 });
  fireEvent.pointerUp(el, { clientX: 100, clientY: toY, pointerId: 1 });
}

// --- job-0325 — NATIVE non-passive touch dispatch (the real-iOS path) ------ //
//
// The real-iOS fix attaches native touchstart/touchmove/touchend listeners
// with { passive:false } directly on the handle DOM node so it can
// preventDefault() the vertical pan (React's JSX onTouch* handlers can't —
// React's root touch listeners are passive). These helpers dispatch genuine
// TouchEvents (happy-dom supports the constructor + a touches init) carrying a
// preventDefault SPY so a test can assert the gesture OWNS the scroll.

interface DispatchedTouch {
  event: Event;
  preventDefault: ReturnType<typeof vi.fn>;
}

/** Build + dispatch a native TouchEvent with a single touch point at (x, y).
 * `cancelable` defaults true (iOS touchmove is cancelable until the gesture is
 * recognised as a scroll). Returns the preventDefault spy so callers can
 * assert whether the handler claimed the gesture. */
function dispatchTouch(
  el: Element,
  type: "touchstart" | "touchmove" | "touchend" | "touchcancel",
  x: number,
  y: number,
  cancelable = true,
): DispatchedTouch {
  const touches =
    type === "touchend" || type === "touchcancel"
      ? []
      : [{ clientX: x, clientY: y, identifier: 1, target: el }];
  // happy-dom's TouchEvent accepts { touches } in its init dict.
  const event = new TouchEvent(type, {
    bubbles: true,
    cancelable,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    touches: touches as any,
  });
  const preventDefault = vi.fn();
  // Override preventDefault with a spy that still flips defaultPrevented.
  Object.defineProperty(event, "preventDefault", {
    configurable: true,
    value: preventDefault,
  });
  el.dispatchEvent(event);
  return { event, preventDefault };
}

/** A native-touch vertical DRAG: touchstart, a move past the threshold,
 * touchend. Returns the preventDefault spy from the threshold-crossing move. */
function touchDragVertical(
  el: Element,
  fromY: number,
  toY: number,
): ReturnType<typeof vi.fn> {
  dispatchTouch(el, "touchstart", 100, fromY);
  const moved = dispatchTouch(el, "touchmove", 100, toY);
  dispatchTouch(el, "touchend", 100, toY);
  return moved.preventDefault;
}

describe("mobileSheetContainerStyle", () => {
  it("pins the sheet to the bottom edge, full width", () => {
    for (const expanded of [false, true]) {
      const s = mobileSheetContainerStyle(expanded);
      expect(s.position).toBe("absolute");
      expect(s.left).toBe(0);
      expect(s.right).toBe(0);
      // NATE 2026-06-19 — the panel EXTENDS to the very bottom edge (bottom:0,
      // bg reaches the screen edge); the safe-area lift moved to bottom PADDING
      // so the composer still clears the iPhone home indicator.
      expect(s.bottom).toBe(0);
      expect(s.paddingBottom).toBe(SHEET_BOTTOM_OFFSET_CSS);
      expect(s.display).toBe("flex");
      expect(s.flexDirection).toBe("column");
    }
  });

  it("collapsed = content height (composer only); expanded = 70vh", () => {
    expect(mobileSheetContainerStyle(false).height).toBe("auto");
    expect(mobileSheetContainerStyle(true).height).toBe(
      MOBILE_SHEET_EXPANDED_HEIGHT,
    );
    expect(MOBILE_SHEET_EXPANDED_HEIGHT).toBe("70vh");
  });

  it("rounds only the TOP corners (sheet idiom) and layers above panels", () => {
    const s = mobileSheetContainerStyle(false);
    // job-0284 — 12px joins the design-family panel radius (was 14).
    expect(s.borderRadius).toBe("12px 12px 0 0");
    // Above panels (20) + hamburgers (30); below drawer backdrop (40).
    expect(s.zIndex).toBe(32);
  });

  // job-0284 / F56 — translucent-surface pins: the sheet keeps a
  // linear-gradient surface with the hairline family border in both states.
  // F56 made the alpha a per-user opacity tier; the LOW tier preserves the
  // original map-reads-through legibility window (0.55–0.7).
  it("job-0284: translucent family gradient + hairline border in both states", () => {
    for (const expanded of [false, true]) {
      const s = mobileSheetContainerStyle(expanded);
      const bg = String(s.background);
      expect(bg).toContain("linear-gradient");
      const alphas = [...bg.matchAll(/rgba\(\d+,\d+,\d+,(0?\.\d+|1)\)/g)].map(
        (m) => Number(m[1]),
      );
      expect(alphas.length).toBeGreaterThan(0);
      expect(s.border).toBe("1px solid rgba(255,255,255,0.10)");
      expect(s.borderBottom).toBe("none");
    }
  });

  it("F56: LOW opacity tier keeps the map-reads-through window (0.55–0.7)", () => {
    for (const expanded of [false, true]) {
      const s = mobileSheetContainerStyle(expanded, 70, "low");
      const alphas = [
        ...String(s.background).matchAll(/rgba\(\d+,\d+,\d+,(0?\.\d+|1)\)/g),
      ].map((m) => Number(m[1]));
      for (const a of alphas) {
        expect(a).toBeGreaterThanOrEqual(0.55);
        expect(a).toBeLessThanOrEqual(0.7);
      }
    }
  });

  it("job-0284: NO backdrop-filter — the sheet hosts position:fixed children (ChartGallery)", () => {
    // A non-none backdrop-filter would make the sheet the containing block
    // for position:fixed descendants, trapping ChartGallery inside the
    // sheet instead of overlaying the viewport (job-0283 hazard).
    for (const expanded of [false, true]) {
      const s = mobileSheetContainerStyle(expanded) as Record<string, unknown>;
      expect(s.backdropFilter).toBeUndefined();
      expect(s.WebkitBackdropFilter).toBeUndefined();
      expect(s.filter).toBeUndefined();
      expect(s.transform).toBeUndefined();
      expect(s.willChange).toBeUndefined();
    }
  });
});

describe("SheetToggleHandle", () => {
  it("renders a >=44px full-width handle with aria-expanded state", () => {
    render(<SheetToggleHandle expanded={false} onToggle={vi.fn()} />);
    const handle = screen.getByTestId("grace2-chat-sheet-toggle");
    expect(handle).toHaveAttribute("aria-expanded", "false");
    expect(handle).toHaveAttribute("aria-label", "Expand chat");
    expect(handle.style.minHeight).toBe("44px");
    expect(handle.style.width).toBe("100%");
  });

  it("job-0280: the chevron arrow is GONE — the bar is the single affordance", () => {
    for (const expanded of [false, true]) {
      const { unmount } = render(
        <SheetToggleHandle expanded={expanded} onToggle={vi.fn()} />,
      );
      const handle = screen.getByTestId("grace2-chat-sheet-toggle");
      // No chevron glyph in either direction…
      expect(handle.textContent ?? "").not.toMatch(/[⌃⌄]/);
      // …exactly one child: the handle bar.
      expect(handle.children.length).toBe(1);
      // The whole handle area stays tappable at the HIG minimum.
      expect(handle.style.minHeight).toBe("44px");
      unmount();
    }
  });

  it("flips label + aria when expanded", () => {
    render(<SheetToggleHandle expanded={true} onToggle={vi.fn()} />);
    const handle = screen.getByTestId("grace2-chat-sheet-toggle");
    expect(handle).toHaveAttribute("aria-expanded", "true");
    expect(handle).toHaveAttribute("aria-label", "Collapse chat");
  });

  it("fires onToggle on a TAP (pointer down+up with no travel)", () => {
    const onToggle = vi.fn();
    render(<SheetToggleHandle expanded={false} onToggle={onToggle} />);
    tap(screen.getByTestId("grace2-chat-sheet-toggle"));
    expect(onToggle).toHaveBeenCalledTimes(1);
  });
});

// --- F44 (job-0322): drag-to-resize + tap-to-fold on the sheet handle ----- //

describe("clampSheetHeight (F44)", () => {
  it("clamps to the [30, 92] vh band and rounds", () => {
    expect(clampSheetHeight(10)).toBe(30);
    expect(clampSheetHeight(999)).toBe(92);
    expect(clampSheetHeight(55.4)).toBe(55);
    expect(clampSheetHeight(55.6)).toBe(56);
  });

  it("passes through an in-band value untouched", () => {
    expect(clampSheetHeight(70)).toBe(70);
    expect(clampSheetHeight(30)).toBe(30);
    expect(clampSheetHeight(92)).toBe(92);
  });

  it("falls back to the 70vh default on non-finite input", () => {
    expect(clampSheetHeight(Number.NaN)).toBe(70);
    expect(clampSheetHeight(Infinity)).toBe(70);
    expect(clampSheetHeight(-Infinity)).toBe(70);
  });
});

describe("readSheetHeight / writeSheetHeight (F44)", () => {
  afterEach(() => {
    try {
      localStorage.clear();
    } catch {
      /* ignore */
    }
  });

  it("defaults to 70vh when nothing is persisted", () => {
    expect(readSheetHeight()).toBe(70);
  });

  it("round-trips a clamped height through localStorage", () => {
    writeSheetHeight(55);
    expect(localStorage.getItem("grace2.chatSheetHeightVh")).toBe("55");
    expect(readSheetHeight()).toBe(55);
  });

  it("persists clamped (out-of-band writes stored at the boundary)", () => {
    writeSheetHeight(9999);
    expect(readSheetHeight()).toBe(92);
    writeSheetHeight(1);
    expect(readSheetHeight()).toBe(30);
  });

  it("garbage in storage degrades to the default", () => {
    localStorage.setItem("grace2.chatSheetHeightVh", "not-a-number");
    expect(readSheetHeight()).toBe(70);
  });
});

describe("isSheetDragGesture (F44 — tap-vs-drag threshold)", () => {
  it("a sub-threshold gesture in EITHER axis is a TAP (not a drag)", () => {
    expect(isSheetDragGesture(0, 0)).toBe(false);
    expect(isSheetDragGesture(SHEET_DRAG_THRESHOLD_PX - 1, 0)).toBe(false);
    expect(isSheetDragGesture(0, SHEET_DRAG_THRESHOLD_PX - 1)).toBe(false);
    expect(isSheetDragGesture(-2, 3)).toBe(false);
  });

  it("a gesture at/over the threshold in either axis is a DRAG", () => {
    expect(isSheetDragGesture(0, SHEET_DRAG_THRESHOLD_PX)).toBe(true);
    expect(isSheetDragGesture(SHEET_DRAG_THRESHOLD_PX, 0)).toBe(true);
    expect(isSheetDragGesture(0, -SHEET_DRAG_THRESHOLD_PX)).toBe(true);
    expect(isSheetDragGesture(100, 100)).toBe(true);
  });
});

describe("SheetToggleHandle — drag-to-resize vs tap-to-fold (F44)", () => {
  // happy-dom reports window.innerHeight (defaults to 768) so the vh math is
  // deterministic: height = (innerHeight - clientY) / innerHeight * 100.
  const VPH = window.innerHeight || 768;

  it("a clean TAP toggles (onToggle) and never resizes", () => {
    const onToggle = vi.fn();
    const onResize = vi.fn();
    const onResizeEnd = vi.fn();
    render(
      <SheetToggleHandle
        expanded={true}
        onToggle={onToggle}
        onResize={onResize}
        onResizeEnd={onResizeEnd}
      />,
    );
    tap(screen.getByTestId("grace2-chat-sheet-toggle"));
    expect(onToggle).toHaveBeenCalledTimes(1);
    expect(onResize).not.toHaveBeenCalled();
    expect(onResizeEnd).not.toHaveBeenCalled();
  });

  it("a vertical DRAG resizes (onResize + onResizeEnd) and never toggles", () => {
    const onToggle = vi.fn();
    const onResize = vi.fn();
    const onResizeEnd = vi.fn();
    render(
      <SheetToggleHandle
        expanded={true}
        onToggle={onToggle}
        onResize={onResize}
        onResizeEnd={onResizeEnd}
      />,
    );
    // Pointer down low (short sheet), drag UP to a high Y (tall sheet).
    const targetY = Math.round(VPH * 0.2); // 80% of the viewport tall
    dragVertical(
      screen.getByTestId("grace2-chat-sheet-toggle"),
      Math.round(VPH * 0.5),
      targetY,
    );
    expect(onToggle).not.toHaveBeenCalled();
    expect(onResize).toHaveBeenCalled();
    expect(onResizeEnd).toHaveBeenCalledTimes(1);
    // The reported height is the clamped vh of the final pointer position.
    const expectedVh = clampSheetHeight(((VPH - targetY) / VPH) * 100);
    expect(onResizeEnd).toHaveBeenLastCalledWith(expectedVh);
  });

  it("a higher pointer (smaller clientY) → a TALLER sheet (bottom-anchored)", () => {
    const onResize = vi.fn();
    render(
      <SheetToggleHandle
        expanded={true}
        onToggle={vi.fn()}
        onResize={onResize}
        onResizeEnd={vi.fn()}
      />,
    );
    const handle = screen.getByTestId("grace2-chat-sheet-toggle");
    fireEvent.pointerDown(handle, {
      clientX: 100,
      clientY: Math.round(VPH * 0.6),
      pointerId: 1,
    });
    fireEvent.pointerMove(handle, {
      clientX: 100,
      clientY: Math.round(VPH * 0.5),
      pointerId: 1,
    });
    fireEvent.pointerMove(handle, {
      clientX: 100,
      clientY: Math.round(VPH * 0.2),
      pointerId: 1,
    });
    fireEvent.pointerUp(handle, {
      clientX: 100,
      clientY: Math.round(VPH * 0.2),
      pointerId: 1,
    });
    const calls = onResize.mock.calls.map((c) => c[0] as number);
    // Monotonically taller as the pointer rises.
    expect(calls[calls.length - 1]).toBeGreaterThan(calls[0]!);
  });

  it("touchAction:'none' on the grip so the browser yields the vertical pan", () => {
    render(<SheetToggleHandle expanded={true} onToggle={vi.fn()} />);
    expect(
      screen.getByTestId("grace2-chat-sheet-toggle").style.touchAction,
    ).toBe("none");
  });

  it("tap still folds when no resize callbacks are wired (collapsed handle)", () => {
    const onToggle = vi.fn();
    render(<SheetToggleHandle expanded={false} onToggle={onToggle} />);
    tap(screen.getByTestId("grace2-chat-sheet-toggle"));
    expect(onToggle).toHaveBeenCalledTimes(1);
  });
});

// --- job-0325 — NATIVE non-passive touch path (the REAL-iOS drag fix) ------ //
//
// The prior fix passed the pointer-event tests above but FAILED on real iOS
// because React's touch listeners are passive: preventDefault() was ignored,
// Safari scrolled the page, and the sheet never resized. The fix attaches
// NATIVE non-passive touch listeners on the handle. These tests drive that
// genuine DOM touch path (NOT React synthetic events) and assert the handler
// (a) resizes live, (b) preventDefaults the scroll once it is a real drag,
// (c) does NOT preventDefault a sub-threshold touch (so small scrolls aren't
// stolen), and (d) toggles on a clean tap.

describe("SheetToggleHandle — native touch drag (job-0325 real-iOS fix)", () => {
  const VPH = window.innerHeight || 768;

  it("a native-touch vertical DRAG resizes (onResize + onResizeEnd), never toggles", () => {
    const onToggle = vi.fn();
    const onResize = vi.fn();
    const onResizeEnd = vi.fn();
    render(
      <SheetToggleHandle
        expanded={true}
        onToggle={onToggle}
        onResize={onResize}
        onResizeEnd={onResizeEnd}
      />,
    );
    const targetY = Math.round(VPH * 0.2);
    touchDragVertical(
      screen.getByTestId("grace2-chat-sheet-toggle"),
      Math.round(VPH * 0.5),
      targetY,
    );
    expect(onToggle).not.toHaveBeenCalled();
    expect(onResize).toHaveBeenCalled();
    expect(onResizeEnd).toHaveBeenCalledTimes(1);
    const expectedVh = clampSheetHeight(((VPH - targetY) / VPH) * 100);
    expect(onResizeEnd).toHaveBeenLastCalledWith(expectedVh);
  });

  it("preventDefault()s the touchmove ONCE the drag crosses the threshold (owns the scroll)", () => {
    const onResize = vi.fn();
    render(
      <SheetToggleHandle
        expanded={true}
        onToggle={vi.fn()}
        onResize={onResize}
        onResizeEnd={vi.fn()}
      />,
    );
    // This is the crux of the iOS bug: without preventDefault the page scrolls
    // and the sheet doesn't move. The drag crosses the threshold, so the
    // handler MUST claim the gesture.
    const pd = touchDragVertical(
      screen.getByTestId("grace2-chat-sheet-toggle"),
      Math.round(VPH * 0.5),
      Math.round(VPH * 0.2),
    );
    expect(pd).toHaveBeenCalled();
  });

  it("does NOT preventDefault a SUB-threshold touchmove (small moves still scroll)", () => {
    const onResize = vi.fn();
    const el = (() => {
      render(
        <SheetToggleHandle
          expanded={true}
          onToggle={vi.fn()}
          onResize={onResize}
          onResizeEnd={vi.fn()}
        />,
      );
      return screen.getByTestId("grace2-chat-sheet-toggle");
    })();
    dispatchTouch(el, "touchstart", 100, 500);
    // Move LESS than the drag threshold — must NOT be treated as a drag.
    const moved = dispatchTouch(
      el,
      "touchmove",
      100,
      500 - (SHEET_DRAG_THRESHOLD_PX - 1),
    );
    dispatchTouch(el, "touchend", 100, 500 - (SHEET_DRAG_THRESHOLD_PX - 1));
    expect(moved.preventDefault).not.toHaveBeenCalled();
    expect(onResize).not.toHaveBeenCalled();
  });

  it("a native-touch TAP (no travel) toggles and never resizes", () => {
    const onToggle = vi.fn();
    const onResize = vi.fn();
    const onResizeEnd = vi.fn();
    render(
      <SheetToggleHandle
        expanded={true}
        onToggle={onToggle}
        onResize={onResize}
        onResizeEnd={onResizeEnd}
      />,
    );
    const el = screen.getByTestId("grace2-chat-sheet-toggle");
    dispatchTouch(el, "touchstart", 100, 500);
    dispatchTouch(el, "touchend", 100, 500);
    expect(onToggle).toHaveBeenCalledTimes(1);
    expect(onResize).not.toHaveBeenCalled();
    expect(onResizeEnd).not.toHaveBeenCalled();
  });

  it("a native-touch drag UPDATES the container height live (Chat wiring shape)", () => {
    function ResizeHarness(): JSX.Element {
      const [heightVh, setHeightVh] = useState(70);
      return (
        <div
          data-testid="sheet"
          style={mobileSheetContainerStyle(true, heightVh, "medium")}
        >
          <SheetToggleHandle
            expanded={true}
            onToggle={() => undefined}
            onResize={(vh) => setHeightVh(vh)}
            onResizeEnd={(vh) => setHeightVh(vh)}
          />
        </div>
      );
    }
    render(<ResizeHarness />);
    expect(screen.getByTestId("sheet").style.height).toBe("70vh");
    const targetY = Math.round(VPH * 0.12);
    // Native (non-React) events drive setState OUTSIDE React's event batching,
    // so wrap the dispatch in act() to flush the re-render before asserting.
    act(() => {
      touchDragVertical(
        screen.getByTestId("grace2-chat-sheet-toggle"),
        Math.round(VPH * 0.5),
        targetY,
      );
    });
    const expectedVh = clampSheetHeight(((VPH - targetY) / VPH) * 100);
    expect(screen.getByTestId("sheet").style.height).toBe(`${expectedVh}vh`);
    expect(expectedVh).toBeGreaterThan(70);
  });

  it("iOS dual-fire: a native touch gesture in flight is NOT double-driven by the synthetic pointer twin", () => {
    // iOS fires BOTH touch and (synthetic) pointer events for one finger. The
    // gesture engine's `input` guard makes the first family to start own the
    // gesture; the other family's events must be inert until it ends.
    const onResize = vi.fn();
    const onResizeEnd = vi.fn();
    const onToggle = vi.fn();
    render(
      <SheetToggleHandle
        expanded={true}
        onToggle={onToggle}
        onResize={onResize}
        onResizeEnd={onResizeEnd}
      />,
    );
    const el = screen.getByTestId("grace2-chat-sheet-toggle");
    // Touch starts the gesture…
    dispatchTouch(el, "touchstart", 100, Math.round(VPH * 0.5));
    // …a synthetic pointerUp twin arrives — it must NOT end/toggle the
    // touch-owned gesture.
    fireEvent.pointerUp(el, {
      clientX: 100,
      clientY: Math.round(VPH * 0.5),
      pointerId: 1,
    });
    expect(onToggle).not.toHaveBeenCalled();
    expect(onResizeEnd).not.toHaveBeenCalled();
    // The real touchend still finishes it (a clean tap → toggle).
    dispatchTouch(el, "touchend", 100, Math.round(VPH * 0.5));
    expect(onToggle).toHaveBeenCalledTimes(1);
  });
});

describe("SheetToggleHandle — iOS touch ergonomics (job-0325)", () => {
  it("sets the iOS tap-flash + callout + user-select guards on the grip", () => {
    render(<SheetToggleHandle expanded={true} onToggle={vi.fn()} />);
    const handle = screen.getByTestId("grace2-chat-sheet-toggle");
    // touch-action:none is the primary signal; the user-select + tap-highlight
    // guards round out the iOS grab-handle feel.
    expect(handle.style.touchAction).toBe("none");
    expect(handle.style.userSelect).toBe("none");
  });
});

describe("bottom-sheet toggle cycle (Chat wiring shape)", () => {
  // Mirrors how Chat.tsx composes the primitives: container style driven by
  // expansion state, handle toggles it, conversation area hides via
  // display:none while STAYING MOUNTED (stream + scroll state survive).
  function SheetHarness(): JSX.Element {
    const [expanded, setExpanded] = useState(false);
    return (
      <div
        data-testid="sheet"
        data-sheet-state={expanded ? "expanded" : "collapsed"}
        style={mobileSheetContainerStyle(expanded)}
      >
        <SheetToggleHandle
          expanded={expanded}
          onToggle={() => setExpanded((v) => !v)}
        />
        <div
          data-testid="sheet-scroll"
          style={{ display: expanded ? "flex" : "none" }}
        >
          conversation
        </div>
        <textarea data-testid="sheet-composer" />
      </div>
    );
  }

  it("starts collapsed: composer visible, conversation hidden but mounted", () => {
    render(<SheetHarness />);
    expect(screen.getByTestId("sheet")).toHaveAttribute(
      "data-sheet-state",
      "collapsed",
    );
    expect(screen.getByTestId("sheet").style.height).toBe("auto");
    expect(screen.getByTestId("sheet-scroll").style.display).toBe("none");
    expect(screen.getByTestId("sheet-composer")).toBeTruthy();
  });

  it("handle expands to 70vh and reveals the conversation; second tap collapses", () => {
    render(<SheetHarness />);
    tap(screen.getByTestId("grace2-chat-sheet-toggle"));
    expect(screen.getByTestId("sheet")).toHaveAttribute(
      "data-sheet-state",
      "expanded",
    );
    expect(screen.getByTestId("sheet").style.height).toBe("70vh");
    expect(screen.getByTestId("sheet-scroll").style.display).toBe("flex");
    tap(screen.getByTestId("grace2-chat-sheet-toggle"));
    expect(screen.getByTestId("sheet")).toHaveAttribute(
      "data-sheet-state",
      "collapsed",
    );
    expect(screen.getByTestId("sheet-scroll").style.display).toBe("none");
    // Still mounted — content was hidden, not destroyed.
    expect(screen.getByTestId("sheet-scroll")).toBeTruthy();
  });
});

describe("drag-resize → container height (F44 Chat wiring shape)", () => {
  // Mirrors how Chat.tsx threads the handle's drag callbacks into the
  // expanded-sheet height: a state vh, applied to mobileSheetContainerStyle,
  // updated live by onResize. Starts EXPANDED so the resize callbacks are
  // wired (Chat only wires them while expanded).
  function ResizeHarness(): JSX.Element {
    const [heightVh, setHeightVh] = useState(70);
    return (
      <div
        data-testid="sheet"
        style={mobileSheetContainerStyle(true, heightVh, "medium")}
      >
        <SheetToggleHandle
          expanded={true}
          onToggle={() => undefined}
          onResize={(vh) => setHeightVh(vh)}
          onResizeEnd={(vh) => setHeightVh(vh)}
        />
      </div>
    );
  }

  it("dragging the handle changes the expanded sheet height", () => {
    const VPH = window.innerHeight || 768;
    render(<ResizeHarness />);
    expect(screen.getByTestId("sheet").style.height).toBe("70vh");
    // Drag UP to ~85% of the viewport.
    const targetY = Math.round(VPH * 0.15);
    dragVertical(
      screen.getByTestId("grace2-chat-sheet-toggle"),
      Math.round(VPH * 0.5),
      targetY,
    );
    const expectedVh = clampSheetHeight(((VPH - targetY) / VPH) * 100);
    expect(screen.getByTestId("sheet").style.height).toBe(`${expectedVh}vh`);
    // It moved (and stayed in band).
    expect(expectedVh).toBeGreaterThan(70);
    expect(expectedVh).toBeLessThanOrEqual(92);
  });
});

// --- Collapsed-sheet active-tool strip (job-0280) -------------------------- //

function step(
  over: Partial<PipelineStepSummary> & { state: PipelineStepState },
): PipelineStepSummary {
  return {
    step_id: over.step_id ?? "step-1",
    name: over.name ?? "fetch_3dep_dem",
    tool_name: over.tool_name ?? "fetch_3dep_dem",
    ...over,
  };
}

function snap(
  pipelineId: string,
  steps: PipelineStepSummary[],
): PipelineStatePayload {
  return { pipeline_id: pipelineId, steps };
}

/** stepOrder map keyed the way Chat records seqs (ux-batch-1 J9):
 *  stepInterleaveKey — step_id for tool steps, `llm_generation|<tool>` for the
 *  thinking pseudo-step. */
function orderOf(entries: Array<[PipelineStepSummary, number]>): Map<string, number> {
  const m = new Map<string, number>();
  for (const [s, seq] of entries) m.set(stepInterleaveKey(s), seq);
  return m;
}

describe("findRunningToolStep", () => {
  it("returns null with no pipeline content at all", () => {
    expect(findRunningToolStep([], null, new Map())).toBeNull();
  });

  it("returns null when every step is terminal (strip hides)", () => {
    const done = step({ step_id: "a", state: "complete" });
    const failed = step({
      step_id: "b",
      name: "compute_slope",
      tool_name: "compute_slope",
      state: "failed",
    });
    expect(
      findRunningToolStep(
        [snap("p1", [done, failed])],
        null,
        orderOf([[done, 1], [failed, 2]]),
      ),
    ).toBeNull();
  });

  it("returns the running step from the live snapshot", () => {
    const running = step({ state: "running" });
    expect(
      findRunningToolStep([], snap("p1", [running]), orderOf([[running, 1]])),
    ).toEqual(running);
  });

  it("excludes the llm_generation thinking pseudo-step", () => {
    const thinking = step({
      step_id: "t",
      name: "llm_generation",
      tool_name: "llm_generation",
      state: "running",
    });
    expect(
      findRunningToolStep([], snap("p1", [thinking]), orderOf([[thinking, 1]])),
    ).toBeNull();
  });

  it("prefers the MOST-RECENT running step by arrival seq", () => {
    const older = step({ step_id: "a", state: "running" });
    const newer = step({
      step_id: "b",
      name: "publish_layer",
      tool_name: "publish_layer",
      state: "running",
    });
    const found = findRunningToolStep(
      [snap("p1", [older])],
      snap("p2", [newer]),
      orderOf([[older, 1], [newer, 2]]),
    );
    expect(found?.step_id).toBe("b");
  });

  it("collapses a SINGLE invocation's running→complete by step_id (pass-1)", () => {
    // One tool invocation has ONE step_id (pipeline_emitter.add_step); its
    // running snapshot archives to history and the complete arrives in live
    // under the SAME step_id. mergeStepsByStepId pass-1 keeps only the terminal
    // card → no running step → strip hides.
    const running = step({ step_id: "a", state: "running" });
    const complete = step({ step_id: "a", state: "complete" });
    expect(
      findRunningToolStep(
        [snap("p1", [running])],
        snap("p2", [complete]),
        orderOf([[running, 1]]),
      ),
    ).toBeNull();
  });

  it("does NOT collapse two DISTINCT step_ids of the same tool — surfaces the running one (J9/F18)", () => {
    // ux-batch-1 J9: two step_ids = two invocations = two cards. The old
    // cross-step_id (name|tool_name) collapse hid a genuinely-running second
    // invocation behind a completed first one (the F18 ordering bug). A
    // still-running distinct step_id must now be surfaced.
    const runningA = step({ step_id: "a", state: "running" });
    const completeB = step({ step_id: "b", state: "complete" });
    const found = findRunningToolStep(
      [snap("p1", [runningA])],
      snap("p2", [completeB]),
      orderOf([[runningA, 1], [completeB, 2]]),
    );
    expect(found?.step_id).toBe("a");
  });
});

describe("SheetActiveToolStrip", () => {
  it("renders the humanized label, a m:ss timer, and a spinner", () => {
    const started = new Date(Date.now() - 65_000).toISOString();
    render(
      <SheetActiveToolStrip
        step={step({ state: "running", started_at: started })}
        onExpand={vi.fn()}
      />,
    );
    const strip = screen.getByTestId("grace2-sheet-tool-strip");
    expect(strip).toBeTruthy();
    // job-0294 — an unmapped tool name title-cases (never raw snake_case);
    // the strip only shows running tools, so the present-tense "…" suffix
    // applies.
    expect(
      screen.getByTestId("grace2-sheet-tool-strip-label").textContent,
    ).toBe("Fetch 3dep Dem…");
    // Anchored on started_at (~65s ago) → a ticking 1:0x, never 0:00.
    expect(
      screen.getByTestId("grace2-sheet-tool-strip-timer").textContent,
    ).toMatch(/^1:0\d$/);
    expect(screen.getByTestId("pipeline-card-indicator")).toBeTruthy();
  });

  it("tap expands the sheet (fires onExpand)", () => {
    const onExpand = vi.fn();
    render(
      <SheetActiveToolStrip
        step={step({ state: "running" })}
        onExpand={onExpand}
      />,
    );
    fireEvent.click(screen.getByTestId("grace2-sheet-tool-strip"));
    expect(onExpand).toHaveBeenCalledTimes(1);
  });

  it("Chat wiring shape: strip renders while a step runs, hides when terminal", () => {
    // Mirrors Chat.tsx: strip mounts IFF findRunningToolStep is non-null on
    // the collapsed sheet's pipeline view-model.
    function StripHarness({
      live,
      order,
    }: {
      live: PipelineStatePayload | null;
      order: Map<string, number>;
    }): JSX.Element {
      const running = findRunningToolStep([], live, order);
      return (
        <div>
          {running && (
            <SheetActiveToolStrip step={running} onExpand={() => undefined} />
          )}
          <textarea data-testid="composer" />
        </div>
      );
    }
    const running = step({ state: "running" });
    const order = orderOf([[running, 1]]);
    const { rerender } = render(
      <StripHarness live={snap("p1", [running])} order={order} />,
    );
    expect(screen.getByTestId("grace2-sheet-tool-strip")).toBeTruthy();

    // Same logical step transitions to complete → strip disappears.
    rerender(
      <StripHarness
        live={snap("p1", [step({ state: "complete", duration_ms: 4200 })])}
        order={order}
      />,
    );
    expect(screen.queryByTestId("grace2-sheet-tool-strip")).toBeNull();
    // Composer (the strip's anchor) is untouched either way.
    expect(screen.getByTestId("composer")).toBeTruthy();
  });
});

// --- F42 (job-0321): collapsed-sheet strip rainbow running animation ------- //
//
// The strip only ever shows a RUNNING tool, so its label always gets the SAME
// animated rainbow-gradient treatment the inline PipelineCard uses for running
// steps — UNLESS the user prefers reduced motion, in which case it falls back
// to the solid label color, exactly like PipelineCard.

/** Force `prefers-reduced-motion: reduce` to the given value for the duration
 *  of a render. Restores the original matchMedia afterwards. */
function mockReducedMotion(reduce: boolean): () => void {
  const original = window.matchMedia;
  window.matchMedia = ((query: string) => ({
    matches: query.includes("prefers-reduced-motion") ? reduce : false,
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

describe("SheetActiveToolStrip — F42 rainbow running animation", () => {
  it("applies the animated rainbow gradient to the label when motion is allowed", () => {
    const restore = mockReducedMotion(false);
    try {
      render(
        <SheetActiveToolStrip
          step={step({ state: "running" })}
          onExpand={vi.fn()}
        />,
      );
      const labelEl = screen.getByTestId(
        "grace2-sheet-tool-strip-label",
      ) as HTMLElement;
      // SAME gradient treatment PipelineCard uses for running steps.
      expect(labelEl.style.backgroundImage).toBe(
        "linear-gradient(90deg, #FF6B6B, #FFD93D, #6BCB77, #4D96FF, #B266FF, #FF6B6B)",
      );
      expect(labelEl.style.backgroundSize).toBe("300% 100%");
      expect(labelEl.style.animation).toBe("grace2-hue-cycle 3s linear infinite");
      // background-clip:text technique — text is painted by the gradient.
      // (The vendor-prefixed -webkit-text-fill-color:transparent twin is set
      // in the component for Safari; happy-dom's CSSOM doesn't reflect that
      // unknown vendor property, so we assert the standard color:transparent.)
      expect(labelEl.style.color).toBe("transparent");
      expect(labelEl.style.backgroundClip).toBe("text");
      // Layout style is preserved (single-line ellipsis truncation).
      expect(labelEl.style.whiteSpace).toBe("nowrap");
      expect(labelEl.style.textOverflow).toBe("ellipsis");
    } finally {
      restore();
    }
  });

  it("falls back to a solid color with NO animation when reduced motion is preferred", () => {
    const restore = mockReducedMotion(true);
    try {
      render(
        <SheetActiveToolStrip
          step={step({ state: "running" })}
          onExpand={vi.fn()}
        />,
      );
      const labelEl = screen.getByTestId(
        "grace2-sheet-tool-strip-label",
      ) as HTMLElement;
      expect(labelEl.style.color).toBe("#eee");
      expect(labelEl.style.animation).toBe("");
      expect(labelEl.style.backgroundImage).toBe("");
      // Layout style still intact.
      expect(labelEl.style.whiteSpace).toBe("nowrap");
    } finally {
      restore();
    }
  });
});

// --- F45 + glyph policy (job-0325) — static source guards ------------------ //
//
// The chat header + desktop close-button live in the full <Chat> component,
// which cannot mount in happy-dom (it opens a WebSocket). So — per the
// established pure-helper / source-guard pattern — these pin the relevant
// SOURCE invariants so a regression is caught by the suite:
//   F45: 'GRACE-2' + build version on the LEFT of the tab/handle row, the
//        connection status on the RIGHT;
//   glyph policy: the raw '›' collapse glyph is replaced by IconChevronRight
//        from the icon module (no raw unicode glyphs rendered in the UI).

// vitest runs with cwd = the web package root, so resolve from there.
const CHAT_SRC = readFileSync(resolve("src/Chat.tsx"), "utf8");

describe("Chat.tsx glyph policy (job-0325)", () => {
  it("imports IconChevronRight from the shared icon module", () => {
    expect(CHAT_SRC).toMatch(
      /import\s*\{[^}]*IconChevronRight[^}]*\}\s*from\s*["']\.\/components\/icons["']/,
    );
  });

  it("renders the collapse control with <IconChevronRight/> (not a raw glyph)", () => {
    expect(CHAT_SRC).toContain("<IconChevronRight");
    // The raw chevron glyph that IconChevronRight replaces must be gone from
    // any JSX text node. (It may appear only inside a comment describing the
    // replacement; we assert it is not used as a rendered child `>›<`.)
    expect(CHAT_SRC).not.toMatch(/>\s*›\s*</);
  });

  it("renders NO emoji or raw decorative unicode as a JSX text child", () => {
    // Forbidden rendered glyphs: chevrons / arrows / check / cross / spinner
    // marks that should always come from the icon module instead.
    const forbidden = ["›", "‹", "✓", "✗", "✕", "×", "⟳"];
    for (const g of forbidden) {
      // As a rendered JSX child: `>GLYPH<`.
      const asChild = new RegExp(`>\\s*${g}\\s*<`);
      expect(CHAT_SRC).not.toMatch(asChild);
    }
  });
});

describe("Chat.tsx header layout (F45, job-0325)", () => {
  it("groups 'TRID3NT' + build version into the LEFT tab group", () => {
    // The LEFT group wraps the strong label + version testid.
    expect(CHAT_SRC).toContain('data-testid="grace2-chat-tab-left"');
    expect(CHAT_SRC).toMatch(/grace2-chat-tab-left[\s\S]*?TRID3NT/);
    expect(CHAT_SRC).toMatch(
      /grace2-chat-tab-left[\s\S]*?grace2-build-version/,
    );
  });

  it("pins a RIGHT-edge control via a flex spacer (marginLeft:auto)", () => {
    // NATE redesign 2026-06-19: the mobile row's RIGHT-edge control is now the
    // model-selector zone (the connection STATUS signal was removed from the
    // mobile row). It pins to the right edge via marginLeft:auto.
    expect(CHAT_SRC).toMatch(/flex:\s*1/);
    expect(CHAT_SRC).toMatch(
      /grace2-sheet-model-zone[\s\S]*?marginLeft:\s*["']auto["']/,
    );
  });

  it("LEFT group precedes the right-edge model zone in source (left-before-right)", () => {
    const leftIdx = CHAT_SRC.indexOf('data-testid="grace2-chat-tab-left"');
    const modelIdx = CHAT_SRC.indexOf('data-testid="grace2-sheet-model-zone"');
    expect(leftIdx).toBeGreaterThan(-1);
    expect(modelIdx).toBeGreaterThan(-1);
    expect(leftIdx).toBeLessThan(modelIdx);
  });
});

// --- NATE 2026-06-17 chat-chrome rework (items 1 + 2 + 6) ----------------- //
//
// The model selector moved into the DESKTOP header (icon-only Brain trigger),
// the connection signal was reduced to a small colored DOT placed to the LEFT
// of the wordmark, and the desktop panel runs flush to the window bottom.
//
// Chat cannot mount in happy-dom (it opens a real WebSocket), so — consistent
// with the rest of this file — these structural guarantees are asserted by
// inspecting the Chat.tsx source string.

describe("Chat.tsx desktop chrome rework (model button + connection dot)", () => {
  // The DESKTOP header is the SECOND grace2-chat-tab-left in source (the first
  // is the mobile MobileSheetHeaderRow). Slice from there to the end of the
  // desktop <header> so the assertions below target the desktop chrome only.
  const desktopHeaderStart = CHAT_SRC.indexOf(
    'data-testid="grace2-chat-tab-left"',
    CHAT_SRC.indexOf('data-testid="grace2-chat-tab-left"') + 1,
  );
  const desktopHeaderSrc = CHAT_SRC.slice(
    desktopHeaderStart,
    desktopHeaderStart + 2000,
  );

  it("imports ModelSelectorButton from the ChatInput module", () => {
    expect(CHAT_SRC).toMatch(
      /import\s*\{[\s\S]*?ModelSelectorButton[\s\S]*?\}\s*from\s*["']\.\/components\/ChatInput["']/,
    );
  });

  it("seeds the selected model from persistence (loadPersistedModelId ?? DEFAULT_MODEL_ID)", () => {
    expect(CHAT_SRC).toMatch(
      /useState<string>\(\s*\(\)\s*=>\s*loadPersistedModelId\(\)\s*\?\?\s*DEFAULT_MODEL_ID/,
    );
  });

  it("renders the icon-only ModelSelectorButton in the desktop header, wired to selectedModelId", () => {
    expect(desktopHeaderSrc).toContain("<ModelSelectorButton");
    expect(desktopHeaderSrc).toMatch(
      /<ModelSelectorButton[\s\S]*?selectedId=\{selectedModelId\}/,
    );
    expect(desktopHeaderSrc).toMatch(
      /<ModelSelectorButton[\s\S]*?onChange=\{setSelectedModelId\}/,
    );
  });

  it("reduces the desktop connection signal to a DOT placed LEFT of the TRID3NT wordmark", () => {
    // Inside the desktop LEFT tab group, the connection-status element appears
    // BEFORE the GRACE-2 wordmark (item 2: dot to the left of the wordmark).
    const dotIdx = desktopHeaderSrc.indexOf('data-testid="connection-status"');
    const wordmarkIdx = desktopHeaderSrc.indexOf("TRID3NT</strong>");
    expect(dotIdx).toBeGreaterThan(-1);
    expect(wordmarkIdx).toBeGreaterThan(-1);
    expect(dotIdx).toBeLessThan(wordmarkIdx);
    // The dot keeps an accessible label/title tied to the WS status.
    expect(desktopHeaderSrc).toMatch(
      /connection-status[\s\S]*?aria-label=\{`WebSocket \$\{STATUS_LABEL\[status\]\}`\}/,
    );
  });

  it("threads the controlled model id into ChatInput (modelId + onModelChange)", () => {
    expect(CHAT_SRC).toMatch(/<ChatInput[\s\S]*?modelId=\{selectedModelId\}/);
    expect(CHAT_SRC).toMatch(
      /<ChatInput[\s\S]*?onModelChange=\{setSelectedModelId\}/,
    );
  });
});

// --- F61 (job-0330): bottom safe-area clearance --------------------------- //
//
// F61 — the mobile sheet floats UP off the bottom edge by the device safe-area
// inset + a few extra px so it clears the iPhone's naturally-curved corners /
// home indicator. The vh height band (drag-resize clamp) is unaffected.

describe("mobileSheetContainerStyle — F61 bottom safe-area clearance", () => {
  it("the container's bottom offset is the safe-area inset + extra px", () => {
    expect(SHEET_BOTTOM_OFFSET_CSS).toBe(
      `calc(env(safe-area-inset-bottom) + ${SHEET_BOTTOM_EXTRA_PX}px)`,
    );
    expect(SHEET_BOTTOM_EXTRA_PX).toBeGreaterThan(0);
  });

  it("applies the safe-area lift as bottom PADDING with the panel anchored to the edge (NATE 2026-06-19)", () => {
    for (const expanded of [false, true]) {
      const s = mobileSheetContainerStyle(expanded);
      // The panel now EXTENDS to the very bottom edge (bottom:0) and the
      // safe-area inset is applied as bottom PADDING, so the panel surface
      // reaches the screen edge while the composer still clears the iPhone
      // home indicator. The lift must still reference env(safe-area-inset-bottom).
      expect(s.bottom).toBe(0);
      expect(s.paddingBottom).toBe(SHEET_BOTTOM_OFFSET_CSS);
      expect(String(s.paddingBottom)).toContain("env(safe-area-inset-bottom)");
    }
  });

  it("F61 does NOT change the vh height band (drag-resize clamp intact)", () => {
    // The bottom OFFSET is a fixed-px lift; the expanded HEIGHT still tracks
    // the clamped vh exactly as before, so clampSheetHeight stays authoritative.
    expect(mobileSheetContainerStyle(true, 55).height).toBe("55vh");
    expect(mobileSheetContainerStyle(true, 999).height).toBe("92vh"); // clamped
    expect(mobileSheetContainerStyle(true, 1).height).toBe("30vh"); // clamped
    expect(mobileSheetContainerStyle(false).height).toBe("auto");
  });

  it("the composer overlay does NOT double-count env(safe-area-inset-bottom)", () => {
    // The CONTAINER owns the safe-area lift (F61). The mobile composer padding
    // must therefore be a plain constant — counting env() in both the container
    // offset AND the composer padding would push the sheet up twice.
    // NATE redesign 2026-06-19 - the mobile composer extends to the VERY BOTTOM
    // edge of the (lifted) sheet, so its bottom padding is 0 (no env() either).
    expect(CHAT_SRC).toContain('padding: "0 10px 0 10px"');
    // No env() in the mobile composer's own padding string.
    expect(CHAT_SRC).not.toMatch(/padding:\s*["']0 10px[^"']*env\(/);
  });

  it("NATE redesign: the desktop composer uses symmetric top+bottom padding", () => {
    // The desktop composer-overlay branch sits balanced in the panel (12px top
    // + 12px bottom padding) rather than hugging the bottom edge.
    expect(CHAT_SRC).toContain('padding: "12px 0"');
  });
});

// --- F45 refined / F45b (job-0330): three-zone handle row ----------------- //
//
// F45 REFINED — the handle row is ONE line with THREE zones: LEFT (GRACE-2 +
// version), CENTER (the grabber rectangle), RIGHT (connection status).
// F45b — labels appear ONLY when expanded; collapsed shows JUST the grabber at
// the TOP (no labels), with the active-strip stack filling the middle when
// tools/sandbox are in use.

describe("MobileSheetHeaderRow — F45 refined three-zone layout (EXPANDED)", () => {
  it("renders LEFT (grace+version), CENTER grabber, RIGHT model selector in one row", () => {
    render(
      <MobileSheetHeaderRow
        expanded={true}
        status="connected"
        onToggle={vi.fn()}
        activeStrips={[]}
        onExpandFromStrip={vi.fn()}
        selectedModelId={DEFAULT_MODEL_ID}
        onModelChange={vi.fn()}
      />,
    );
    // LEFT zone — grace + version.
    const left = screen.getByTestId("grace2-chat-tab-left");
    expect(left.textContent).toContain("TRID3NT");
    expect(screen.getByTestId("grace2-build-version")).toBeTruthy();
    // CENTER zone — the grabber rectangle (the drag handle).
    expect(screen.getByTestId("grace2-sheet-grabber-zone")).toBeTruthy();
    expect(screen.getByTestId("grace2-chat-sheet-toggle")).toBeTruthy();
    // MODEL zone (RIGHT-most) - the Bedrock model selector mirroring the
    // desktop ModelSelectorButton (icon-only Brain trigger).
    expect(screen.getByTestId("grace2-sheet-model-zone")).toBeTruthy();
    expect(screen.getByTestId("model-selector-button")).toBeTruthy();
    // NATE tweak 2026-06-19 - the connection STATUS signal was REMOVED from the
    // mobile row entirely.
    expect(screen.queryByTestId("connection-status")).toBeNull();
    // The row marks itself expanded.
    expect(screen.getByTestId("grace2-sheet-header-row")).toHaveAttribute(
      "data-sheet-row-state",
      "expanded",
    );
  });

  it("orders the zones LEFT → CENTER(grabber) → MODEL(right) in the DOM (no status)", () => {
    render(
      <MobileSheetHeaderRow
        expanded={true}
        status="connected"
        onToggle={vi.fn()}
        activeStrips={[]}
        onExpandFromStrip={vi.fn()}
        selectedModelId={DEFAULT_MODEL_ID}
        onModelChange={vi.fn()}
      />,
    );
    const row = screen.getByTestId("grace2-sheet-header-row");
    const ids = Array.from(row.children).map((c) => c.getAttribute("data-testid"));
    expect(ids).toEqual([
      "grace2-chat-tab-left",
      "grace2-sheet-grabber-zone",
      "grace2-sheet-model-zone",
    ]);
  });

  it("NATE tweak: the model picker mirrors the desktop ModelSelectorButton with the Brain icon LEADING (left)", () => {
    render(
      <MobileSheetHeaderRow
        expanded={true}
        status="connected"
        onToggle={vi.fn()}
        activeStrips={[]}
        onExpandFromStrip={vi.fn()}
        selectedModelId={DEFAULT_MODEL_ID}
        onModelChange={vi.fn()}
      />,
    );
    const zone = screen.getByTestId("grace2-sheet-model-zone");
    const button = screen.getByTestId("model-selector-button");
    // The picker is the SAME icon-only Brain ModelSelectorButton the desktop
    // header renders, and it is the leading (first) child of the model zone.
    expect(zone.firstElementChild).toBe(button);
    // Brain glyph present (icon-only trigger, no model-name text label).
    expect(button.querySelector("svg")).toBeTruthy();
    // It carries the active model id (controlled by selectedModelId).
    expect(button).toHaveAttribute("data-model-id", DEFAULT_MODEL_ID);
  });

  it("the grabber stays the F44 drag affordance (tap toggles)", () => {
    const onToggle = vi.fn();
    render(
      <MobileSheetHeaderRow
        expanded={true}
        status="connected"
        onToggle={onToggle}
        onResize={vi.fn()}
        onResizeEnd={vi.fn()}
        activeStrips={[]}
        onExpandFromStrip={vi.fn()}
        selectedModelId={DEFAULT_MODEL_ID}
        onModelChange={vi.fn()}
      />,
    );
    tap(screen.getByTestId("grace2-chat-sheet-toggle"));
    expect(onToggle).toHaveBeenCalledTimes(1);
  });
});

describe("MobileSheetHeaderRow — F45b collapsed (labels hidden)", () => {
  it("collapsed shows JUST the grabber — NO grace/version/connection labels", () => {
    render(
      <MobileSheetHeaderRow
        expanded={false}
        status="connected"
        onToggle={vi.fn()}
        activeStrips={[]}
        onExpandFromStrip={vi.fn()}
        selectedModelId={DEFAULT_MODEL_ID}
        onModelChange={vi.fn()}
      />,
    );
    // The grabber rectangle is present at the top…
    expect(screen.getByTestId("grace2-chat-sheet-toggle")).toBeTruthy();
    // …but NONE of the labeled three-zone chrome is rendered.
    expect(screen.queryByTestId("grace2-chat-tab-left")).toBeNull();
    expect(screen.queryByTestId("grace2-build-version")).toBeNull();
    expect(screen.queryByTestId("connection-status")).toBeNull();
    expect(screen.getByTestId("grace2-sheet-header-row")).toHaveAttribute(
      "data-sheet-row-state",
      "collapsed",
    );
  });

  it("collapsed + NO active strips → the strip area is absent", () => {
    render(
      <MobileSheetHeaderRow
        expanded={false}
        status="connected"
        onToggle={vi.fn()}
        activeStrips={[]}
        onExpandFromStrip={vi.fn()}
        selectedModelId={DEFAULT_MODEL_ID}
        onModelChange={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("grace2-sheet-collapsed-strips")).toBeNull();
  });

  it("collapsed + active strips → the strip stack fills the middle (below the grabber)", () => {
    const items: ActiveStripItem[] = [
      { kind: "tool", seq: 1, step: step({ state: "running" }) },
    ];
    render(
      <MobileSheetHeaderRow
        expanded={false}
        status="connected"
        onToggle={vi.fn()}
        activeStrips={items}
        onExpandFromStrip={vi.fn()}
        selectedModelId={DEFAULT_MODEL_ID}
        onModelChange={vi.fn()}
      />,
    );
    // Grabber at top + the strip stack as the middle fill.
    expect(screen.getByTestId("grace2-chat-sheet-toggle")).toBeTruthy();
    expect(screen.getByTestId("grace2-sheet-collapsed-strips")).toBeTruthy();
    expect(screen.getByTestId("grace2-sheet-tool-strip")).toBeTruthy();
    // Still no labels while collapsed.
    expect(screen.queryByTestId("grace2-chat-tab-left")).toBeNull();
    expect(screen.queryByTestId("connection-status")).toBeNull();
  });
});

// --- F66 (job-0330): collapsed-mobile sandbox strip ----------------------- //
//
// F66 — a Python-sandbox (code_exec) card shows in the active-strip area the
// SAME WAY tool usage does (SheetActiveToolStrip) but with a PULSATING-BLUE
// animation instead of the rainbow gradient. Multiple active strips STACK.

function req(over: Partial<CodeExecRequestPayload> = {}): CodeExecRequestPayload {
  return {
    envelope_type: "code-exec-request",
    code_exec_id: over.code_exec_id ?? "ce-1",
    python_code: over.python_code ?? "print(1)",
    layer_refs: over.layer_refs ?? {},
    rationale: over.rationale ?? null,
  };
}

function result(
  codeExecId: string,
): CodeExecResultPayload {
  return {
    envelope_type: "code-exec-result",
    code_exec_id: codeExecId,
    status: "ok",
    stdout_tail: "",
    stderr_tail: "",
    result: null,
    truncated: false,
    duration_s: 1,
  };
}

describe("isSandboxRunning (F66)", () => {
  it("running = decided proceed AND no result yet", () => {
    const r = req({ code_exec_id: "a" });
    const decisions = new Map<string, SandboxCardDecision>([["a", "proceed"]]);
    expect(isSandboxRunning(r, new Map(), decisions)).toBe(true);
  });

  it("NOT running once a result lands", () => {
    const r = req({ code_exec_id: "a" });
    const decisions = new Map<string, SandboxCardDecision>([["a", "proceed"]]);
    const results = new Map([["a", result("a")]]);
    expect(isSandboxRunning(r, results, decisions)).toBe(false);
  });

  it("NOT running while un-decided (pending gate) or cancelled", () => {
    const r = req({ code_exec_id: "a" });
    expect(isSandboxRunning(r, new Map(), new Map())).toBe(false);
    const cancelled = new Map<string, SandboxCardDecision>([["a", "cancel"]]);
    expect(isSandboxRunning(r, new Map(), cancelled)).toBe(false);
  });
});

describe("findRunningSandboxes (F66)", () => {
  it("returns only running sandboxes, ordered by arrival seq", () => {
    const a = req({ code_exec_id: "a" });
    const b = req({ code_exec_id: "b" });
    const c = req({ code_exec_id: "c" });
    const decisions = new Map<string, SandboxCardDecision>([
      ["a", "proceed"],
      ["b", "proceed"],
      ["c", "cancel"], // cancelled → excluded
    ]);
    const results = new Map([["a", result("a")]]); // a done → excluded
    const seqs = new Map([["a", 1], ["b", 2], ["c", 3]]);
    const found = findRunningSandboxes([a, b, c], results, decisions, seqs);
    expect(found.map((r) => r.code_exec_id)).toEqual(["b"]);
  });

  it("orders multiple running sandboxes oldest-first", () => {
    const a = req({ code_exec_id: "a" });
    const b = req({ code_exec_id: "b" });
    const decisions = new Map<string, SandboxCardDecision>([
      ["a", "proceed"],
      ["b", "proceed"],
    ]);
    const seqs = new Map([["a", 5], ["b", 2]]);
    const found = findRunningSandboxes([a, b], new Map(), decisions, seqs);
    expect(found.map((r) => r.code_exec_id)).toEqual(["b", "a"]);
  });
});

describe("buildActiveStripStack (F66 — tools + sandboxes interleave)", () => {
  it("interleaves running tools and running sandboxes by arrival seq", () => {
    const toolStep = step({ step_id: "t1", state: "running" });
    const sandbox = req({ code_exec_id: "s1" });
    const stepOrder = orderOf([[toolStep, 2]]);
    const decisions = new Map<string, SandboxCardDecision>([["s1", "proceed"]]);
    const sandboxSeqs = new Map([["s1", 1]]);
    const stack = buildActiveStripStack(
      [],
      snap("p1", [toolStep]),
      stepOrder,
      [sandbox],
      new Map(),
      decisions,
      sandboxSeqs,
    );
    // Sandbox seq 1 < tool seq 2 → sandbox first.
    expect(stack.map((i) => i.kind)).toEqual(["sandbox", "tool"]);
  });

  it("excludes terminal tools, thinking, and non-running sandboxes", () => {
    const done = step({ step_id: "t1", state: "complete" });
    const thinking = step({
      step_id: "tk",
      name: "llm_generation",
      tool_name: "llm_generation",
      state: "running",
    });
    const sandbox = req({ code_exec_id: "s1" });
    const stack = buildActiveStripStack(
      [snap("p0", [done, thinking])],
      null,
      orderOf([[done, 1], [thinking, 2]]),
      [sandbox],
      new Map(),
      new Map(), // sandbox un-decided → excluded
      new Map([["s1", 3]]),
    );
    expect(stack).toEqual([]);
  });

  it("STACKS more than one active item (multiple strips, not just one)", () => {
    const toolA = step({ step_id: "tA", state: "running" });
    const toolB = step({
      step_id: "tB",
      name: "publish_layer",
      tool_name: "publish_layer",
      state: "running",
    });
    const sandbox = req({ code_exec_id: "s1" });
    const decisions = new Map<string, SandboxCardDecision>([["s1", "proceed"]]);
    const stack = buildActiveStripStack(
      [],
      snap("p1", [toolA, toolB]),
      orderOf([[toolA, 1], [toolB, 2]]),
      [sandbox],
      new Map(),
      decisions,
      new Map([["s1", 3]]),
    );
    expect(stack.length).toBe(3);
    expect(stack.map((i) => i.kind)).toEqual(["tool", "tool", "sandbox"]);
  });
});

describe("SheetActiveSandboxStrip — F66 pulsating-blue", () => {
  it("renders a sandbox strip with the running label + pulse dot + icon", () => {
    render(
      <SheetActiveSandboxStrip request={req()} onExpand={vi.fn()} />,
    );
    const strip = screen.getByTestId("grace2-sheet-sandbox-strip");
    expect(strip).toBeTruthy();
    expect(strip).toHaveAttribute("data-code-exec-id", "ce-1");
    expect(
      screen.getByTestId("grace2-sheet-sandbox-strip-label").textContent,
    ).toBe("Running Python sandbox");
    expect(screen.getByTestId("grace2-sheet-sandbox-strip-pulse")).toBeTruthy();
  });

  it("applies the PULSATING-BLUE animation (NOT the rainbow gradient)", () => {
    const restore = mockReducedMotion(false);
    try {
      render(
        <SheetActiveSandboxStrip request={req()} onExpand={vi.fn()} />,
      );
      const labelEl = screen.getByTestId(
        "grace2-sheet-sandbox-strip-label",
      ) as HTMLElement;
      // Pulsating-blue keyframe — distinct from the rainbow hue-cycle.
      expect(labelEl.style.animation).toBe(SANDBOX_PULSE_ANIMATION);
      expect(labelEl.style.animation).toContain("grace2-pulse-blue");
      // Blue text, NOT the rainbow gradient (no background-clip:text).
      expect(labelEl.style.color).toBe("#a5b4fc");
      expect(labelEl.style.backgroundImage).toBe("");
      // The pulse dot breathes too.
      const dot = screen.getByTestId(
        "grace2-sheet-sandbox-strip-pulse",
      ) as HTMLElement;
      expect(dot.style.animation).toBe(SANDBOX_PULSE_ANIMATION);
    } finally {
      restore();
    }
  });

  it("respects prefers-reduced-motion → holds steady (no animation)", () => {
    const restore = mockReducedMotion(true);
    try {
      render(
        <SheetActiveSandboxStrip request={req()} onExpand={vi.fn()} />,
      );
      const labelEl = screen.getByTestId(
        "grace2-sheet-sandbox-strip-label",
      ) as HTMLElement;
      expect(labelEl.style.animation).toBe("");
      const dot = screen.getByTestId(
        "grace2-sheet-sandbox-strip-pulse",
      ) as HTMLElement;
      expect(dot.style.animation).toBe("");
      // Still blue (steady), just not pulsing.
      expect(labelEl.style.color).toBe("#a5b4fc");
    } finally {
      restore();
    }
  });

  it("injects the distinct grace2-pulse-blue keyframe (separate from the rainbow)", () => {
    // The injector runs on module import; the keyframe <style> is in the head.
    const styleEl = document.getElementById(SANDBOX_PULSE_KEYFRAMES_ID);
    expect(styleEl).toBeTruthy();
    expect(styleEl!.textContent).toContain("@keyframes grace2-pulse-blue");
    // It is NOT the rainbow hue-cycle keyframe.
    expect(styleEl!.textContent).not.toContain("grace2-hue-cycle");
  });

  it("tap expands the sheet (fires onExpand)", () => {
    const onExpand = vi.fn();
    render(
      <SheetActiveSandboxStrip request={req()} onExpand={onExpand} />,
    );
    fireEvent.click(screen.getByTestId("grace2-sheet-sandbox-strip"));
    expect(onExpand).toHaveBeenCalledTimes(1);
  });
});

describe("SheetActiveStripStack — F66 stacking", () => {
  it("renders nothing for an empty stack", () => {
    const { container } = render(
      <SheetActiveStripStack items={[]} onExpand={vi.fn()} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders a STACK of tool + sandbox strips (multiple at once)", () => {
    const items: ActiveStripItem[] = [
      { kind: "tool", seq: 1, step: step({ step_id: "t1", state: "running" }) },
      { kind: "sandbox", seq: 2, request: req({ code_exec_id: "s1" }) },
      { kind: "tool", seq: 3, step: step({ step_id: "t2", name: "publish_layer", tool_name: "publish_layer", state: "running" }) },
    ];
    render(<SheetActiveStripStack items={items} onExpand={vi.fn()} />);
    expect(screen.getByTestId("grace2-sheet-strip-stack")).toBeTruthy();
    // Two tool strips + one sandbox strip = three strips stacked.
    expect(screen.getAllByTestId("grace2-sheet-tool-strip").length).toBe(2);
    expect(screen.getAllByTestId("grace2-sheet-sandbox-strip").length).toBe(1);
  });

  it("a sandbox strip in the stack uses the pulsating-blue variant", () => {
    const restore = mockReducedMotion(false);
    try {
      const items: ActiveStripItem[] = [
        { kind: "sandbox", seq: 1, request: req({ code_exec_id: "s1" }) },
      ];
      render(<SheetActiveStripStack items={items} onExpand={vi.fn()} />);
      const labelEl = screen.getByTestId(
        "grace2-sheet-sandbox-strip-label",
      ) as HTMLElement;
      expect(labelEl.style.animation).toBe(SANDBOX_PULSE_ANIMATION);
    } finally {
      restore();
    }
  });
});

// --- Wake-composer redesign (NATE 2026-06-19) ----------------------------- //
//
// The not-connected composer states (connecting / wake / waking) now route
// through the SINGLE WakeOverlay (the separate composer-connecting div is
// gone), the mobile bottom-sheet chrome is hidden ENTIRELY while not
// connected, the chrome eases in a beat after the composer on connect, and the
// connection STATUS signal was removed from the mobile sheet header. The full
// <Chat> can't mount in happy-dom (it opens a WebSocket), so - per the
// established source-guard pattern - these pin the relevant Chat.tsx source
// invariants so a regression is caught by the suite.

describe("Chat.tsx wake-composer redesign (NATE 2026-06-19)", () => {
  it("routes connecting/wake/waking through the SINGLE WakeOverlay (no composer-connecting div)", () => {
    // The separate connecting surface is gone - one overlay handles all three.
    expect(CHAT_SRC).not.toContain('data-testid="composer-connecting"');
    // The overlay phase is fed by the single composerOverlayPhase deriver…
    expect(CHAT_SRC).toContain("const composerOverlayPhase: WakePhase =");
    expect(CHAT_SRC).toMatch(/<WakeOverlay[\s\S]*?phase=\{composerOverlayPhase\}/);
  });

  it("passes accentColor=getModelById(selectedModelId).accentColor to the overlay", () => {
    expect(CHAT_SRC).toContain(
      "const composerAccentColor = getModelById(selectedModelId).accentColor;",
    );
    expect(CHAT_SRC).toMatch(
      /<WakeOverlay[\s\S]*?accentColor=\{composerAccentColor\}/,
    );
  });

  it("hides the mobile sheet chrome ENTIRELY while not connected", () => {
    // The hideMobileChrome gate (mobile AND not-connected) suppresses both the
    // MobileSheetHeaderRow (grabber/labels) and forces the collapsed container.
    expect(CHAT_SRC).toContain("const notConnected = composerPhase !== \"chat\";");
    expect(CHAT_SRC).toContain("const hideMobileChrome = mobile && notConnected;");
    // The header row only renders when chrome is NOT hidden.
    expect(CHAT_SRC).toMatch(/\{mobile && !hideMobileChrome &&/);
    // The container collapses (no 70vh back panel) when chrome is hidden.
    expect(CHAT_SRC).toMatch(
      /mobileSheetContainerStyle\(\s*hideMobileChrome \? false : sheetExpanded/,
    );
  });

  it("staggers the chrome ease-in after the composer on connect (~160ms)", () => {
    expect(CHAT_SRC).toContain("setChromeRevealed");
    expect(CHAT_SRC).toMatch(/setTimeout\(\(\) => setChromeRevealed\(true\), 160\)/);
    // The chrome wrapper opacity/transform is gated by chromeRevealed.
    expect(CHAT_SRC).toMatch(/opacity: chromeRevealed \? 1 : 0/);
  });

  it("removes the connection STATUS signal from the mobile sheet header row", () => {
    // The MobileSheetHeaderRow EXPANDED branch must no longer render a
    // connection-status element. (The desktop header still has its dot, which
    // is asserted elsewhere; the mobile row was the only one with marginLeft
    // auto on the status, now reassigned to the model zone.)
    const expandedBranchStart = CHAT_SRC.indexOf(
      "export function MobileSheetHeaderRow",
    );
    const expandedBranchEnd = CHAT_SRC.indexOf(
      "// COLLAPSED (F45b)",
      expandedBranchStart,
    );
    const expandedBranch = CHAT_SRC.slice(expandedBranchStart, expandedBranchEnd);
    expect(expandedBranch).not.toContain('data-testid="connection-status"');
  });
});

// MOBILE SHEET-TOP DOCK (NATE 2026-06-24; MEASURED-TOP 2026-06-27) - Chat lifts
// its sheet geometry (expanded? + dragged height vh) AND the REAL measured
// top-edge px up to App via onSheetGeometryChange so the App-root overlays
// (SequenceScrubber + LayerLegend) can dock to the sheet's TRUE TOP edge. Chat
// can't mount in happy-dom (it opens a WebSocket), so we assert the wiring at the
// source level (the established CHAT_SRC pattern).
describe("Chat.tsx mobile sheet-geometry lift (sheetTopPx dock)", () => {
  it("declares the onSheetGeometryChange prop in ChatProps with measured topPx", () => {
    // MEASURED-TOP (NATE 2026-06-27): the callback now ALSO carries `topPx`
    // (number | null) - the real getBoundingClientRect top so App can dock to the
    // true panel top instead of an arithmetic estimate.
    expect(CHAT_SRC).toMatch(
      /onSheetGeometryChange\?\s*:\s*\(g:\s*\{\s*expanded:\s*boolean;\s*heightVh:\s*number;\s*topPx:\s*number\s*\|\s*null;\s*\}\s*\)\s*=>\s*void/,
    );
  });

  it("destructures onSheetGeometryChange in the Chat component args", () => {
    const ctorStart = CHAT_SRC.indexOf("export function Chat({");
    const ctorEnd = CHAT_SRC.indexOf("}: ChatProps): JSX.Element {", ctorStart);
    const ctorArgs = CHAT_SRC.slice(ctorStart, ctorEnd);
    expect(ctorArgs).toContain("onSheetGeometryChange");
  });

  it("publishes geometry + measured topPx from a mobile-gated callback", () => {
    // The publisher reads the live expanded/height from refs and measures the
    // container's real top via getBoundingClientRect, then emits all three.
    expect(CHAT_SRC).toContain("const topPx = el ? el.getBoundingClientRect().top : null;");
    expect(CHAT_SRC).toMatch(
      /onSheetGeometryChange\?\.\(\{\s*expanded:\s*sheetExpandedRef\.current,\s*heightVh:\s*sheetHeightRef\.current,\s*topPx,\s*\}\)/,
    );
    // The publisher bails on desktop (no bottom sheet there).
    expect(CHAT_SRC).toMatch(
      /const publishSheetGeometry = useCallback\(\(\): void => \{\s*if \(!mobile\) return;/,
    );
  });

  it("re-publishes on geometry change keyed on the sheet deps (expand/collapse/drag)", () => {
    // The geometry effect re-runs publishSheetGeometry when expanded/height change
    // (post-commit, so the rect reflects the just-applied layout).
    expect(CHAT_SRC).toMatch(
      /useEffect\(\(\) => \{\s*if \(!mobile\) return;\s*publishSheetGeometry\(\);\s*\}, \[mobile, sheetExpanded, sheetHeightVh, publishSheetGeometry\]\)/,
    );
  });

  it("measures the real top under a ResizeObserver - covers connecting/bare/collapsed", () => {
    // A ResizeObserver on the sheet container re-measures whenever the composer
    // card's REAL height changes (connecting -> chat composer swap, bare box,
    // collapsed reflow) - the case the { expanded, heightVh } props cannot see and
    // where the arithmetic estimate floated the overlays mid-screen.
    expect(CHAT_SRC).toContain("const sheetContainerRef = useRef<HTMLDivElement | null>(null);");
    expect(CHAT_SRC).toMatch(
      /const ro = new ResizeObserver\(\(\) => publishSheetGeometry\(\)\);\s*ro\.observe\(el\);/,
    );
    // happy-dom lacks ResizeObserver, so the effect guards on its existence.
    expect(CHAT_SRC).toContain('typeof ResizeObserver === "undefined"');
    // The observer effect is mobile-gated and disconnects on cleanup.
    expect(CHAT_SRC).toMatch(
      /useEffect\(\(\) => \{\s*if \(!mobile\) return undefined;[\s\S]*?return \(\) => ro\.disconnect\(\);/,
    );
  });

  it("attaches the measurement ref to the sheet container ONLY on mobile", () => {
    // Desktop passes no ref -> byte-for-byte unchanged behavior (the geometry
    // effects are mobile-gated and never read it anyway).
    expect(CHAT_SRC).toContain("ref={mobile ? sheetContainerRef : undefined}");
  });
});

// DOCK-TO-VISIBLE-BOTTOM (NATE 2026-06-27, mobile-only) - when the agent is
// OFFLINE/WAKING the chat chrome is hidden and the visible bottom element is the
// floating WakeOverlay box (not the chat container). The publisher must measure
// the WAKE box top in that state so the scrubber + legend dock to the wake card
// instead of the stale online expanded-sheet line, and the measurement must
// RE-FIRE on the connected<->notConnected transition. Chat can't mount in
// happy-dom, so we pin the wiring at the source level (the CHAT_SRC pattern).
describe("Chat.tsx mobile dock-to-visible-bottom (wake box vs chat container)", () => {
  it("holds a ref to the wake box (composer-gate) + a live notConnected ref", () => {
    expect(CHAT_SRC).toContain(
      "const wakeBoxRef = useRef<HTMLDivElement | null>(null);",
    );
    expect(CHAT_SRC).toContain(
      "const notConnectedRef = useRef<boolean>(false);",
    );
  });

  it("publishes the WAKE box top when notConnected, else the chat container top", () => {
    // The publisher selects the visible bottom element per connection state: the
    // wake box (composer-gate) when notConnected, else the chat sheet container.
    expect(CHAT_SRC).toMatch(
      /const el =\s*notConnectedRef\.current && wakeBoxRef\.current\s*\?\s*wakeBoxRef\.current\s*:\s*sheetContainerRef\.current;/,
    );
    // It still measures the chosen element's real top via getBoundingClientRect.
    expect(CHAT_SRC).toContain(
      "const topPx = el ? el.getBoundingClientRect().top : null;",
    );
  });

  it("mirrors notConnected into the ref + RE-MEASURES on the transition", () => {
    // The live mirror keeps the once-bound publisher reading the current state.
    expect(CHAT_SRC).toContain("notConnectedRef.current = notConnected;");
    // A mobile-gated effect re-publishes when notConnected flips (the
    // connected<->notConnected transition) so a fresh wake-box top replaces the
    // stale latched online top.
    expect(CHAT_SRC).toMatch(
      /useEffect\(\(\) => \{\s*if \(!mobile\) return undefined;\s*\/\/[\s\S]*?publishSheetGeometry\(\);[\s\S]*?\}, \[mobile, notConnected, publishSheetGeometry\]\)/,
    );
  });

  it("the ResizeObserver also observes the wake box so its resize re-measures", () => {
    // The observer (bound once) additionally observes the wake element when it is
    // mounted, so a resize of the floating card re-publishes the visible top.
    expect(CHAT_SRC).toContain("const wakeEl = wakeBoxRef.current;");
    expect(CHAT_SRC).toContain("if (wakeEl) ro.observe(wakeEl);");
  });

  it("attaches the wake-box ref to the composer-gate wrapper", () => {
    // The composer-gate directly contains the WakeOverlay box in the not-connected
    // states (no top offset), so its top IS the visible wake box top.
    const gateStart = CHAT_SRC.indexOf('data-testid="composer-gate"');
    expect(gateStart).toBeGreaterThan(-1);
    // The wake-box ref sits on the same composer-gate div (after its comment block),
    // before the gate's style prop closes - slice generously past the comment.
    const gateSlice = CHAT_SRC.slice(gateStart, gateStart + 1200);
    expect(gateSlice).toContain("ref={wakeBoxRef}");
    // And it precedes the gate's style (it is a prop on the SAME element).
    expect(gateSlice.indexOf("ref={wakeBoxRef}")).toBeLessThan(
      gateSlice.indexOf('style={{ position: "relative" }}'),
    );
  });
});
