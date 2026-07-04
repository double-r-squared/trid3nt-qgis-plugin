// GRACE-2 web — MobileDrawer (job-0278, mobile-friendly UI).
//
// Mobile (<768px) replacement for the desktop left rail: a slide-in drawer
// hidden by default, opened via the top-left ☰ button (44px touch target),
// rendered as a full-height left-anchored overlay with its own backdrop.
// Tapping the backdrop closes it; App.tsx also closes it when a Case row is
// selected. The drawer hosts the SAME children the desktop rail shows
// (CasesPanel at root, CaseView + LayerPanel inside a Case) plus the
// Settings/Secrets pills folded into its footer.
//
// job-0284 — map-centric pass: the drawer LOST its solid panel surface. The
// container is now a transparent layout column and the backdrop is an
// INVISIBLE full-screen tap-to-close hit area — the Case rows / breadcrumb /
// LayerPanel float as individual translucent hairline cards directly over
// the map (per-card backgrounds via the `.grace2-mobile-touch` scope in
// global.css). The map is the app; the chrome floats on it.
//
// Mounted ONLY when useIsMobile() is true — desktop rendering is untouched.
//
// The `grace2-mobile-touch` class scopes the global.css touch-target bump
// (min 44px on Case-row / breadcrumb / pill buttons) AND the job-0284
// floating-card surfaces to this drawer.

import { useEffect } from "react";
import { IconMenu } from "./icons";

export interface MobileDrawerButtonProps {
  onClick: () => void;
  /** Mirrors drawer visibility for aria-expanded. */
  open: boolean;
}

/**
 * Top-left ☰ opener. 44x44 — Apple HIG minimum touch target. z-index 30
 * matches the desktop hamburgers (above panels z=20 / legend z=10, below
 * the drawer backdrop z=40 so the open drawer covers it).
 */
export function MobileDrawerButton({
  onClick,
  open,
}: MobileDrawerButtonProps): JSX.Element {
  return (
    <button
      data-testid="grace2-mobile-drawer-button"
      aria-label="Open cases and layers"
      aria-expanded={open}
      aria-controls="grace2-mobile-drawer"
      onClick={onClick}
      style={{
        position: "absolute",
        top: 12,
        left: 12,
        width: 44,
        height: 44,
        // job-0284 — joins the hairline surface family (desktop hamburger
        // chrome, job-0283). Leaf surface: hosts no fixed descendants, so
        // backdrop blur is safe here (and ONLY on leaves like this).
        background: "rgba(18,19,24,0.92)",
        border: "1px solid rgba(255,255,255,0.08)",
        borderRadius: 10,
        boxShadow: "0 2px 12px rgba(0,0,0,0.35)",
        backdropFilter: "blur(6px)",
        WebkitBackdropFilter: "blur(6px)",
        color: "#cfd4db",
        padding: 0,
        cursor: "pointer",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 30,
        lineHeight: 1,
      }}
    >
      {/* job-0322 F52 — icon-module glyph (no raw unicode ☰). */}
      <IconMenu size={20} />
    </button>
  );
}

export interface MobileDrawerProps {
  open: boolean;
  /** Backdrop tap / programmatic close (Case selected, layer-panel ×). */
  onClose: () => void;
  children: React.ReactNode;
}

/**
 * The drawer surface itself. Returns null when closed (nothing in the DOM —
 * the map stays full-screen underneath). When open: an INVISIBLE full-screen
 * backdrop hit area (tap = close; job-0284 dropped the dim so the map stays
 * fully visible) + a transparent full-height layout column from the left
 * edge, width min(320px, 85vw), whose children float as individual cards.
 */
export function MobileDrawer({
  open,
  onClose,
  children,
}: MobileDrawerProps): JSX.Element | null {
  // job-0279: Escape dismisses, matching every other overlay's convention
  // (SaveGateModal, popups) — an open drawer is a click shield over the
  // sheet/composer, so a keyboard escape hatch matters on desktop-sized
  // mobile emulation and tablets with keyboards.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <>
      <div
        data-testid="grace2-mobile-drawer-backdrop"
        aria-hidden="true"
        onClick={onClose}
        style={{
          position: "absolute",
          inset: 0,
          // job-0284 — invisible hit area: tap-to-close works exactly as
          // before, but the map is no longer dimmed (map-centric pass).
          background: "transparent",
          zIndex: 40,
        }}
      />
      <div
        id="grace2-mobile-drawer"
        data-testid="grace2-mobile-drawer"
        className="grace2-mobile-touch"
        role="dialog"
        aria-label="Cases and layers"
        // job-0322 F52 (v2) — tap-anywhere-outside-a-component dismiss. The
        // earlier `e.target === e.currentTarget` guard only closed on a tap
        // that landed DIRECTLY on this bare column element; taps that landed on
        // the App.tsx layout wrappers (the flex:1 cases wrapper / the
        // position:relative LayerPanel wrapper) or any card subtree set
        // e.target to a descendant and so never closed. The new approach is
        // pointer-events based: this transparent column is `pointerEvents:
        // "none"` so every tap on its empty/gutter space passes THROUGH to the
        // full-screen invisible backdrop below (z=40, onClick=onClose) and
        // closes the drawer. The ACTUAL interactive cards re-enable hit-testing
        // with `pointerEvents: "auto"` (CasesPanel / CaseView / LayerPanel /
        // empty-layers card, wired from App.tsx), so taps on real components
        // still work and the fixed-position ConfirmationDialog (a descendant in
        // an `auto` subtree) keeps receiving events. No onClick on the column
        // itself anymore — the backdrop owns close.
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          bottom: 0,
          width: "min(320px, 85vw)",
          // job-0284 — NO panel surface: transparent layout column; the
          // children (Case rows, breadcrumb, LayerPanel, pills) carry their
          // own translucent hairline-card backgrounds (global.css
          // `.grace2-mobile-touch` scope) and float over the map.
          //
          // NO backdrop-filter here, EVER: a non-none backdrop-filter would
          // make this drawer the containing block for position:fixed
          // descendants — CasesPanel mounts ConfirmationDialog (delete
          // confirm, position:fixed) inside this subtree, and it must center
          // on the VIEWPORT, not inside the 320px column (hazard documented
          // by job-0283 at its two removal sites). Translucency comes from
          // the children's rgba/alpha backgrounds only.
          background: "transparent",
          zIndex: 41,
          // job-0322 F52 (v2) — the column itself is click-transparent so empty
          // gutter taps fall through to the z=40 backdrop (tap-to-close). The
          // floating cards re-enable hit-testing via `pointerEvents: "auto"`
          // (set by App.tsx on each interactive card / their wrappers).
          pointerEvents: "none",
          display: "flex",
          flexDirection: "column",
          gap: 8,
          padding: 12,
          // job-0284 — clear the collapsed bottom sheet (~126px incl. its
          // safe-area pad): with the backdrop no longer dimming, the sheet
          // stays visible under the open drawer, and the drawer's footer
          // pills must float ABOVE it instead of overlapping the composer.
          paddingBottom: "calc(138px + env(safe-area-inset-bottom))",
          boxSizing: "border-box",
          overflow: "hidden",
        }}
      >
        {children}
      </div>
    </>
  );
}
