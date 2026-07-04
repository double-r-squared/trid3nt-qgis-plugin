// GRACE-2 web - BboxProgressOverlay (NATE map/loading-UX SIMPLIFICATION).
//
// A single loading animation anchored to the projected AOI bbox screen rectangle
// (the SAME `aoiScreenRect` the LayerLegend + SequenceScrubber pin against,
// lifted from Map.tsx via onAoiScreenRectChange). It communicates "the map is
// working, fetching the first layers" without a separate spinner chrome.
//
// NATE 2026-06-24: there used to be TWO visuals - a sweeping SCAN-BORDER and a
// FILL-GRID. NATE asked to drop the scan entirely and keep ONLY the polished
// grid, shown only when there are truly no layers yet. So the only mode this
// component renders is "fill" (the grid). The "scan" mode is no longer produced
// by resolveBboxProgress and is no longer rendered here (a defensive no-op keeps
// the component from drawing anything if a stale "scan" ever arrives).
//
//   - mode "fill"  -> a polished FILL-GRID SHIMMER inside the bbox: a faint grid
//     lattice with a soft vertical sheen that drifts down through the box. Used
//     while the FIRST layers fetch (zero layers on the map yet), so covering the
//     box is fine; it clears the instant the first layer paints.
//
// The state DECISION lives in lib/bbox_progress.resolveBboxProgress (pure +
// unit-tested); this component is the render half: given a rect + mode it paints
// the grid. It is purely presentational - no signals logic.
//
// prefers-reduced-motion: the drifting sheen is replaced by a SUBTLE STATIC
// grid + tint, so the "loading" cue still reads without motion.
//
// pointer-events:none throughout - the overlay never intercepts map gestures.

import { useEffect } from "react";
import type { ScreenRect } from "../lib/legend_snap";
import type { BboxProgressMode, BboxProgressTone } from "../lib/bbox_progress";
import { prefersReducedMotion } from "./PipelineCard";

export interface BboxProgressOverlayProps {
  /** The projected AOI bbox screen rectangle, or null when there is no AOI. */
  rect: ScreenRect | null;
  /** Which animation to paint ("none" renders nothing). */
  mode: BboxProgressMode;
  /** Scan-border tone (ignored for "fill"). */
  tone: BboxProgressTone;
  /**
   * Test seam: force the reduced-motion branch on/off. Undefined (default)
   * consults the live `prefers-reduced-motion` media query.
   */
  reducedMotionOverride?: boolean;
}

// Keyframes are injected once at module-eval (idempotent, id-guarded), mirroring
// App.tsx's ensureAppSpinKeyframes pattern. SSR/test-safe (no-op without document).
const KEYFRAMES_ID = "grace2-bbox-progress-keyframes";
function ensureKeyframes(): void {
  if (typeof document === "undefined") return;
  if (document.getElementById(KEYFRAMES_ID)) return;
  const style = document.createElement("style");
  style.id = KEYFRAMES_ID;
  // ONE keyframe: the polished grid shimmer. NATE 2026-06-24 dropped the scan,
  // so the scan-sweep + border-pulse keyframes are gone. The grid's soft sheen
  // band drifts smoothly down through the box (background-position) while a
  // gentle opacity breathe keeps it from reading as a hard scan line. The grid
  // lattice (the other two background layers) is held STILL - only the sheen
  // moves - so the box outline never appears to grow / shrink / jitter.
  style.textContent = `
@keyframes grace2-bbox-fill-shimmer {
  0%   { background-position: 0% 0%, 0 0, 0 0; opacity: 0.42; }
  50%  { opacity: 0.60; }
  100% { background-position: 0% 100%, 0 0, 0 0; opacity: 0.42; }
}
`;
  document.head.appendChild(style);
}
ensureKeyframes();

export function BboxProgressOverlay({
  rect,
  mode,
  tone,
  reducedMotionOverride,
}: BboxProgressOverlayProps): JSX.Element | null {
  // Re-assert the keyframes if a hot-reload / late mount dropped the style node.
  useEffect(() => {
    ensureKeyframes();
  }, []);

  // NATE 2026-06-24: ONLY the grid ("fill") renders now. The resolver never
  // returns "scan", but be defensive - any non-"fill" mode (none / a stale scan)
  // draws nothing.
  if (mode !== "fill" || !rect) return null;

  const width = rect.right - rect.left;
  const height = rect.bottom - rect.top;
  if (!(width > 0) || !(height > 0)) return null;

  const reduced =
    reducedMotionOverride !== undefined
      ? reducedMotionOverride
      : prefersReducedMotion();
  // `tone` is retained on the props for back-compat; the grid is always blue.
  void tone;

  // The anchored frame: absolutely positioned over the bbox extent. position is
  // relative to the map container (which fills the viewport), matching the
  // legend/scrubber anchoring convention. Never intercepts pointer events.
  const frameStyle: React.CSSProperties = {
    position: "absolute",
    left: rect.left,
    top: rect.top,
    width,
    height,
    pointerEvents: "none",
    boxSizing: "border-box",
    // Below the legend/scrubber (z 51) + panels, above the map overlays.
    zIndex: 12,
    overflow: "hidden",
    borderRadius: 4,
  };

  // FILL-GRID SHIMMER (the ONE polished loading visual). A faint blue grid
  // lattice with a soft vertical sheen band that drifts down through the box.
  // Polish (NATE 2026-06-24):
  //   - a slightly finer 20px cell + a touch lighter grid line so it reads as a
  //     crisp lattice, not heavy bars;
  //   - the sheen band is taller (a smooth 200% travel) and uses ease-in-out so
  //     it glides rather than snaps;
  //   - a single thin border + a soft inset glow so the box edge reads cleanly
  //     over terrain / basemap without a second pulsing outline.
  // The grid lattice background layers are held STILL (only the sheen animates),
  // so the box NEVER appears to grow / shrink - it just shimmers in place.
  // Reduced-motion -> a static faint tint + grid (no drift).
  const gridLine = "rgba(74,163,255,0.16)";
  const gridCell = "20px"; // finer than the old 22px -> crisper lattice.
  const tint = "rgba(74,163,255,0.05)";
  const fillStyle: React.CSSProperties = reduced
    ? {
        ...frameStyle,
        // Static: a faint tint + grid so the "filling" cue still reads.
        background: `
          linear-gradient(${gridLine} 1px, transparent 1px) 0 0 / 100% ${gridCell},
          linear-gradient(90deg, ${gridLine} 1px, transparent 1px) 0 0 / ${gridCell} 100%,
          ${tint}`,
        opacity: 0.5,
        border: `1px solid rgba(74,163,255,0.28)`,
        boxShadow: "inset 0 0 12px rgba(74,163,255,0.10)",
      }
    : {
        ...frameStyle,
        background: `
          linear-gradient(180deg, rgba(74,163,255,0.0) 0%, rgba(74,163,255,0.24) 50%, rgba(74,163,255,0.0) 100%) 0 0 / 100% 200%,
          linear-gradient(${gridLine} 1px, transparent 1px) 0 0 / 100% ${gridCell},
          linear-gradient(90deg, ${gridLine} 1px, transparent 1px) 0 0 / ${gridCell} 100%,
          ${tint}`,
        border: `1px solid rgba(74,163,255,0.28)`,
        boxShadow: "inset 0 0 12px rgba(74,163,255,0.10)",
        // NATE 2026-06-24: PING-PONG the sheen (alternate) so it never hard-cuts
        // from the end back to the start (the visible "repeat"). It sweeps down
        // (1 -> n) then reverses up (n -> n-1 -> ... -> 1); ease-in-out softens the
        // turnaround, and the 200%-tall band stays covering the box across 0%<->100%
        // so there is no empty flash at either end.
        animation: "grace2-bbox-fill-shimmer 2.4s ease-in-out infinite alternate",
      };
  return (
    <div
      data-testid="grace2-bbox-progress-overlay"
      data-mode="fill"
      data-reduced={reduced ? "true" : "false"}
      aria-hidden
      style={fillStyle}
    />
  );
}
