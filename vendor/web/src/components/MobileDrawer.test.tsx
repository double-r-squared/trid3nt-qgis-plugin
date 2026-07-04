// GRACE-2 web — MobileDrawer tests (job-0278, mobile-friendly UI).
//
// Pins the drawer's open/close contract:
//   - closed → NOTHING in the DOM (map stays unobstructed);
//   - open → backdrop + panel + children render;
//   - backdrop tap closes; clicks inside the panel do NOT close;
//   - touch-target class + a11y wiring on the menu opener (44px, aria-expanded);
//   - F52 (v2): the drawer column is `pointerEvents: "none"` so gutter taps
//     fall through to the backdrop (close); the opener renders the icon-module
//     menu glyph (no raw unicode).
//
// The drawer is a pure presentational shell — App.tsx owns the open state —
// so a small stateful harness exercises the full open → close cycle the way
// App wires it.

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { useState } from "react";
import { MobileDrawer, MobileDrawerButton } from "./MobileDrawer";

afterEach(() => cleanup());

describe("MobileDrawerButton", () => {
  it("renders a 44px touch target with a11y wiring", () => {
    render(<MobileDrawerButton open={false} onClick={vi.fn()} />);
    const btn = screen.getByTestId("grace2-mobile-drawer-button");
    expect(btn).toHaveAttribute("aria-label", "Open cases and layers");
    expect(btn).toHaveAttribute("aria-expanded", "false");
    expect(btn).toHaveAttribute("aria-controls", "grace2-mobile-drawer");
    expect(btn.style.width).toBe("44px");
    expect(btn.style.height).toBe("44px");
  });

  it("invokes onClick", () => {
    const onClick = vi.fn();
    render(<MobileDrawerButton open={false} onClick={onClick} />);
    fireEvent.click(screen.getByTestId("grace2-mobile-drawer-button"));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  // job-0322 F52 — no raw unicode glyph; the opener renders the icon-module
  // IconMenu (a Phosphor svg), per the project's no-glyph UI policy.
  it("renders the icon-module menu glyph (svg), not a raw unicode ☰", () => {
    render(<MobileDrawerButton open={false} onClick={vi.fn()} />);
    const btn = screen.getByTestId("grace2-mobile-drawer-button");
    expect(btn.querySelector("svg")).toBeTruthy();
    expect(btn.textContent ?? "").not.toContain("☰"); // ☰
  });
});

describe("MobileDrawer", () => {
  it("renders nothing when closed", () => {
    render(
      <MobileDrawer open={false} onClose={vi.fn()}>
        <span data-testid="drawer-child" />
      </MobileDrawer>,
    );
    expect(screen.queryByTestId("grace2-mobile-drawer")).toBeNull();
    expect(screen.queryByTestId("grace2-mobile-drawer-backdrop")).toBeNull();
    expect(screen.queryByTestId("drawer-child")).toBeNull();
  });

  it("renders backdrop + panel + children when open", () => {
    render(
      <MobileDrawer open={true} onClose={vi.fn()}>
        <span data-testid="drawer-child" />
      </MobileDrawer>,
    );
    expect(screen.getByTestId("grace2-mobile-drawer-backdrop")).toBeTruthy();
    const drawer = screen.getByTestId("grace2-mobile-drawer");
    expect(drawer).toBeTruthy();
    expect(screen.getByTestId("drawer-child")).toBeTruthy();
    // Touch-target CSS scope (global.css bump applies only inside this class).
    expect(drawer.className).toContain("grace2-mobile-touch");
    expect(drawer).toHaveAttribute("role", "dialog");
  });

  // job-0284 — map-centric pass: the drawer has NO panel surface (children
  // float as their own translucent cards over the map) and the backdrop is
  // an INVISIBLE full-screen hit area (no dim — the map stays visible).
  it("job-0284: transparent backdrop + surfaceless panel (components float)", () => {
    render(
      <MobileDrawer open={true} onClose={vi.fn()}>
        <span />
      </MobileDrawer>,
    );
    const backdrop = screen.getByTestId("grace2-mobile-drawer-backdrop");
    expect(backdrop.style.background).toBe("transparent");
    const drawer = screen.getByTestId("grace2-mobile-drawer");
    expect(drawer.style.background).toBe("transparent");
    expect(drawer.style.border).toBe("");
    expect(drawer.style.boxShadow).toBe("");
  });

  it("job-0284: NO backdrop-filter — the drawer hosts position:fixed children (ConfirmationDialog)", () => {
    // A non-none backdrop-filter would make the drawer the containing block
    // for position:fixed descendants, trapping CasesPanel's delete
    // ConfirmationDialog inside the 320px column instead of centering it on
    // the viewport (job-0283 hazard). Translucency must stay rgba/alpha-only.
    render(
      <MobileDrawer open={true} onClose={vi.fn()}>
        <span />
      </MobileDrawer>,
    );
    const drawer = screen.getByTestId("grace2-mobile-drawer");
    expect(drawer.style.backdropFilter || "").toBe("");
    expect(drawer.style.filter || "").toBe("");
    expect(drawer.style.transform || "").toBe("");
    expect(drawer.style.willChange || "").toBe("");
  });

  it("backdrop tap calls onClose; taps inside the panel do not", () => {
    const onClose = vi.fn();
    render(
      <MobileDrawer open={true} onClose={onClose}>
        <button data-testid="inner-button">inner</button>
      </MobileDrawer>,
    );
    fireEvent.click(screen.getByTestId("inner-button"));
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("grace2-mobile-drawer-backdrop"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  // job-0322 F52 (v2) — the tap-to-dismiss fix is now pointer-events based,
  // not an onClick guard. The transparent 320px column is `pointerEvents:
  // "none"` so every tap on its empty/gutter space passes THROUGH to the z=40
  // backdrop (onClick=onClose). The column itself has NO onClick handler.
  it("F52: the drawer column is pointerEvents:none (gutter taps fall through to backdrop)", () => {
    render(
      <MobileDrawer open={true} onClose={vi.fn()}>
        <button data-testid="inner-button">inner</button>
      </MobileDrawer>,
    );
    const drawer = screen.getByTestId("grace2-mobile-drawer");
    // The column is click-transparent — pointer events pass through to the
    // backdrop below, which owns the close.
    expect(drawer.style.pointerEvents).toBe("none");
  });

  it("F52: the backdrop still owns close (tap-anywhere-outside dismiss)", () => {
    // With the column click-transparent, the full-screen backdrop is the single
    // close surface — every gutter tap reaches it.
    const onClose = vi.fn();
    render(
      <MobileDrawer open={true} onClose={onClose}>
        <button data-testid="inner-button">inner</button>
      </MobileDrawer>,
    );
    fireEvent.click(screen.getByTestId("grace2-mobile-drawer-backdrop"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("F52: the column carries no onClick close handler (a direct tap is inert)", () => {
    // Regression guard against the old `e.target === e.currentTarget` model:
    // the column must NOT close on its own click anymore (the backdrop does).
    // In jsdom pointer-events does not suppress dispatch, so a direct click on
    // the column is the strongest available proof the handler is gone.
    const onClose = vi.fn();
    render(
      <MobileDrawer open={true} onClose={onClose}>
        <button data-testid="inner-button">inner</button>
      </MobileDrawer>,
    );
    fireEvent.click(screen.getByTestId("grace2-mobile-drawer"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("F52: a nested interactive child's own handler still runs (cards stay tappable)", () => {
    // App.tsx re-enables hit-testing on each real card with `pointerEvents:
    // "auto"`; the drawer must not swallow or hijack those taps.
    const onClose = vi.fn();
    const onInner = vi.fn();
    render(
      <MobileDrawer open={true} onClose={onClose}>
        <div data-testid="floating-card" style={{ pointerEvents: "auto" }}>
          <button data-testid="card-button" onClick={onInner}>
            select
          </button>
        </div>
      </MobileDrawer>,
    );
    fireEvent.click(screen.getByTestId("card-button"));
    expect(onInner).toHaveBeenCalledTimes(1);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("full open → close cycle through a stateful parent (App wiring shape)", () => {
    function Harness(): JSX.Element {
      const [open, setOpen] = useState(false);
      return (
        <>
          {!open && (
            <MobileDrawerButton open={open} onClick={() => setOpen(true)} />
          )}
          <MobileDrawer open={open} onClose={() => setOpen(false)}>
            <button
              data-testid="fake-case-row"
              onClick={() => setOpen(false)}
            >
              Case row
            </button>
          </MobileDrawer>
        </>
      );
    }
    render(<Harness />);
    // Hidden by default.
    expect(screen.queryByTestId("grace2-mobile-drawer")).toBeNull();
    // ☰ opens.
    fireEvent.click(screen.getByTestId("grace2-mobile-drawer-button"));
    expect(screen.getByTestId("grace2-mobile-drawer")).toBeTruthy();
    expect(screen.queryByTestId("grace2-mobile-drawer-button")).toBeNull();
    // Selecting a Case (App closes the drawer in onSelect) dismisses it.
    fireEvent.click(screen.getByTestId("fake-case-row"));
    expect(screen.queryByTestId("grace2-mobile-drawer")).toBeNull();
    // Re-open then dismiss via backdrop.
    fireEvent.click(screen.getByTestId("grace2-mobile-drawer-button"));
    fireEvent.click(screen.getByTestId("grace2-mobile-drawer-backdrop"));
    expect(screen.queryByTestId("grace2-mobile-drawer")).toBeNull();
  });
});
