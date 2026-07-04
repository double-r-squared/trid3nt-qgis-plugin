// GRACE-2 web — SequenceScrubber (sequential-layer-grouping feature).
//
// A STATIC bottom-of-screen overlay that steps the ACTIVE sequential layer
// group's frames. NATE's ask: enumerated temporal raster stacks (e.g. 3 HRRR
// forecast hours F+01h / F+03h / F+06h) collapse into ONE group you can step
// through.
//
// STATIC SCRUBBER (NATE 2026-06-26): the scrubber is now a STATIC pill pinned at
// the bottom of the screen. NATE: "im starting to think maybe we just need a
// static scrubber - fighting all the movement around is killing me, put it at
// the bottom of the screen." This SUPERSEDES the entire AOI-bbox-snap +
// dock-hysteresis + width-tracks-bbox + sheet-top-dock saga (18cc0da / 49662e2 /
// cc7233c): there is NO per-frame reprojection, NO docking latch, NO
// width-tracks-bbox. It simply pins bottom-center and stays put. The ONLY
// position input that can move it is a side-panel open/close (desktop), which is
// a deliberate, stable shift - never per animation frame.
//
// Layout: `▶ < ——●—— > x/N` — a PLAY/PAUSE toggle, prev-arrow, track/slider,
// next-arrow, plus a compact `x/N` readout. The group label and frame label are
// omitted from the scrubber (they show in the LayerPanel group row).
//
// It is rendered FROM App.tsx and appears WHENEVER a sequential group is active
// on the module-level AnimationController - regardless of whether the Layers
// panel is open. Pure presentation: all frame state + callbacks come in as
// props. The auto-advance INTERVAL lives in the AnimationController (so playback
// survives a panel unmount); this component only reflects `playing` + the live
// frame index and toggles via onPlayToggle.

import { useCallback, useRef } from "react";
import { createPortal } from "react-dom";
import {
  IconArrowLeft,
  IconArrowRight,
  IconPlay,
  IconPause,
} from "./icons";
import { useIsMobile } from "../hooks/useIsMobile";

// STATIC WIDTH (NATE 2026-06-26): a fixed, comfortable pill width - no longer
// tracks the AOI bbox. Clamped to the open desktop gutter (and the mobile
// viewport) so it never runs off-screen or under the side panels.
// Exported so the mobile LayerLegend BAND form can match the scrubber width
// exactly (NATE 2026-06-28: the band must stay the SAME WIDTH AS THE SCRUBBER).
export const SCRUBBER_WIDTH_DEFAULT = 420;
const SCRUBBER_MIN_WIDTH = 200; // floor so the buttons stay tappable in a narrow gutter.
// Side margin reserved on each edge (mobile viewport / desktop gutter).
export const SCRUBBER_EDGE_MARGIN_PX = 16;

/**
 * MOBILE SCRUBBER WIDTH (NATE 2026-06-28) - the resolved on-screen scrubber pill
 * width on MOBILE for a given viewport width: the fixed default (420) clamped to
 * the viewport minus a side margin on each edge, floored at the min width. This is
 * the SAME math the mobile branch of this component uses for `widthPx`; it is
 * exported so the mobile LayerLegend band form can render at the EXACT scrubber
 * width (so the docked band reads as one bar in line with the scrubber, and does
 * NOT rescale with the AOI bbox). `viewportW` null/unknown (SSR) => the default.
 */
export function scrubberMobileWidthPx(viewportW: number | null | undefined): number {
  if (viewportW == null || !Number.isFinite(viewportW)) return SCRUBBER_WIDTH_DEFAULT;
  const avail = viewportW - 2 * SCRUBBER_EDGE_MARGIN_PX;
  return Math.max(SCRUBBER_MIN_WIDTH, Math.min(SCRUBBER_WIDTH_DEFAULT, avail));
}

// Desktop bottom offset - lifts the pill off the very viewport edge.
const SCRUBBER_BOTTOM_DESKTOP_PX = 24;

// MOBILE bottom offset: clear the collapsed chat composer + the device
// safe-area inset (env() is invisible to JS, so it must be reserved in CSS).
// Mirrors LayerLegend.MOBILE_LEGEND_PILL_BOTTOM_CSS / Chat SHEET_BOTTOM_OFFSET so
// the bottom overlays share one clearance story. The scrubber sits UNDER the
// chat sheet in z-order, so when the sheet is expanded over this band the sheet
// wins; when collapsed the scrubber sits just above the composer.
export const SCRUBBER_MOBILE_SHEET_CLEARANCE_PX = 116;
export const SCRUBBER_MOBILE_BOTTOM_CSS = `calc(env(safe-area-inset-bottom) + ${SCRUBBER_MOBILE_SHEET_CLEARANCE_PX}px)`;

// TASK E (NATE 2026-06-26): on MOBILE the scrubber docks to the TOP EDGE of the
// chat sheet (the panel, not the composer/text-form) and TRACKS it as the sheet
// is dragged / collapsed. App threads the sheet's live top in viewport px as
// `sheetTopPx`; the scrubber sits this many px ABOVE that edge. Higher
// sheetTopPx = the sheet is more collapsed (its top is lower); a LOWER
// sheetTopPx (sheet expanded, its top moves UP the screen) lifts the scrubber.
// MOBILE-ONLY chat-clearance gap: 20px keeps a clear breathing space between the
// scrubber and the chat panel (was 8px, almost touching). Desktop is untouched.
export const SCRUBBER_SHEET_DOCK_GAP_PX = 20;

// MOBILE Z-ORDER (NATE 2026-06-22): on mobile the chat is a bottom sheet at
// zIndex 32 (Chat.tsx mobileSheetContainerStyle). The scrubber must sit
// UNDERNEATH it so it never covers the chat composer. On desktop the chat is a
// right-side panel (not over the bottom-center scrubber), so the scrubber keeps
// its original higher z there.
const SCRUBBER_Z_DESKTOP = 51;
const SCRUBBER_Z_MOBILE = 31; // below the mobile chat sheet (zIndex 32)

// GUTTER MARGIN (desktop): keep a small gap between the pill and the side panels.
const GUTTER_MARGIN_PX = 12;

export interface SequenceScrubberProps {
  /** Short group label, e.g. the shared source/tool ("HRRR forecast"). */
  label: string;
  /** Per-frame short labels in series order, e.g. ["F+01h","F+03h","F+06h"]. */
  frameLabels: string[];
  /** Active frame index (0-based) into `frameLabels`. */
  activeIndex: number;
  /** Step to an absolute frame index (clamped by the owner). */
  onStep: (index: number) => void;
  /** Whether the scrubber is auto-advancing. */
  playing: boolean;
  /** Toggle play/pause. */
  onPlayToggle: () => void;
  /** Auto-advance cadence in ms while playing. Default 1100. */
  intervalMs?: number;
  /**
   * GUTTER CLAMP (desktop only) - the open map gutter geometry so the static
   * pill centers BETWEEN the left layers rail (when open) and the right chat
   * panel, never under/past them. These are stable layout values (they change
   * only when a panel opens/closes), so the pill stays put across animation
   * frames. Mobile panels are overlays (not a horizontal gutter), so these are
   * ignored on mobile - it centers in the viewport.
   */
  leftPanelWidthPx?: number;
  /** Right chat panel width in px (0 when collapsed). */
  chatWidthPx?: number;
  /** Whether the right chat panel is collapsed (then its width is 0). */
  chatCollapsed?: boolean;
  /**
   * TASK E (NATE 2026-06-26) - MOBILE ONLY: the chat bottom-sheet's live TOP
   * edge in viewport px (0 = top of viewport). When provided the mobile scrubber
   * docks its BOTTOM just above this edge and TRACKS the sheet as it is
   * adjusted/collapsed (a parallel owner threads it from App). null/undefined =
   * unknown -> the scrubber falls back to SCRUBBER_MOBILE_BOTTOM_CSS (safe-area +
   * composer clearance). Ignored on desktop (the chat is a side panel there).
   */
  sheetTopPx?: number | null;
  /**
   * ZOOM-OUT HIDE (NATE 2026-06-27, MOBILE-ONLY) - the AOI bbox has zoomed OUT to a
   * tiny DOT on screen (Map.tsx aoiRectTooSmallToShow). When true the MOBILE
   * scrubber HIDES entirely (renders null), mirroring the legend - a speck-sized
   * bbox has no useful frame context to step. Default false so existing callers /
   * tests are unaffected (no hide), and gated to mobile in the body so desktop is
   * byte-for-byte unchanged. The scrubber has no other bbox awareness; this single
   * boolean is the whole hide contract.
   */
  aoiTooSmallToShow?: boolean;
}

/** Clamp `i` into [0, n) with wraparound so the scrubber loops cleanly. */
export function wrapIndex(i: number, n: number): number {
  if (n <= 0) return 0;
  return ((i % n) + n) % n;
}

export function SequenceScrubber({
  label,
  frameLabels,
  activeIndex,
  onStep,
  playing,
  onPlayToggle,
  // intervalMs is retained in the props contract for backward compatibility but
  // is no longer used here — the AnimationController owns the advance interval.
  intervalMs: _intervalMs,
  leftPanelWidthPx = 0,
  chatWidthPx = 0,
  chatCollapsed = false,
  sheetTopPx = null,
  // ZOOM-OUT HIDE (NATE 2026-06-27, mobile-only) - default false so existing
  // callers / tests are unaffected; consumed ONLY on mobile in the hide guard
  // below so desktop is byte-for-byte unchanged.
  aoiTooSmallToShow = false,
}: SequenceScrubberProps): JSX.Element | null {
  const n = frameLabels.length;
  const isMobile = useIsMobile();
  // Hold the latest active index in a ref so prev/next step from the current
  // frame even if the parent re-renders between presses.
  const activeRef = useRef(activeIndex);
  activeRef.current = activeIndex;
  const onStepRef = useRef(onStep);
  onStepRef.current = onStep;

  const stepBy = useCallback(
    (delta: number): void => {
      onStepRef.current(wrapIndex(activeRef.current + delta, n));
    },
    [n],
  );

  if (n === 0) return null;

  // ZOOM-OUT HIDE (NATE 2026-06-27, MOBILE-ONLY) - the AOI bbox is a tiny dot on
  // screen (the user zoomed OUT far). HIDE the scrubber entirely, mirroring the
  // legend. Gated to MOBILE so desktop (the static bottom-center pill) is
  // byte-for-byte unchanged; default-false prop keeps existing callers / tests
  // green. Placed after the hooks above so hook order stays stable.
  if (isMobile && aoiTooSmallToShow) return null;

  // The slider + counter both read this SAME live index, so the handle tracks
  // auto-advance: when the controller advances a frame it notifies -> App
  // re-renders -> activeIndex updates -> the controlled slider's value moves the
  // thumb (autoplay-handle fix, NATE 2026-06-26).
  const safeIndex = wrapIndex(activeIndex, n);

  // window may be undefined under SSR; guard and skip the viewport clamps then.
  const viewportW =
    typeof window !== "undefined" && Number.isFinite(window.innerWidth)
      ? window.innerWidth
      : null;
  // TASK E (NATE 2026-06-26): viewport HEIGHT, guarded the same way - needed to
  // convert the chat sheet's top edge (measured from the viewport top) into a
  // CSS `bottom` offset (measured from the viewport bottom) for the mobile dock.
  const viewportH =
    typeof window !== "undefined" && Number.isFinite(window.innerHeight)
      ? window.innerHeight
      : null;

  // STATIC POSITION (NATE 2026-06-26): pin bottom-center. Mobile centers in the
  // viewport (above the composer via the safe-area-inclusive bottom offset);
  // desktop centers in the OPEN map gutter so it never hides under the side
  // panels. Width is a fixed comfortable band, clamped to the available span.
  let widthPx = SCRUBBER_WIDTH_DEFAULT;
  let posStyle: React.CSSProperties;

  if (isMobile) {
    // Mobile: viewport-centered, clamped to the viewport minus side margins.
    // Shared helper (scrubberMobileWidthPx) is the single source of truth so the
    // mobile LayerLegend band form can match this width exactly.
    if (viewportW != null) {
      widthPx = scrubberMobileWidthPx(viewportW);
    }
    // TASK E (NATE 2026-06-26): DOCK to the chat SHEET's top edge and TRACK it.
    // The sheet's top is given in viewport px (from the top); convert to a CSS
    // `bottom` (from the bottom): bottom = viewportH - sheetTopPx + gap, so the
    // scrubber's bottom edge sits GAP px above the sheet top. A more-expanded
    // sheet has a SMALLER sheetTopPx (its top is higher up the screen) -> a
    // LARGER bottom -> the scrubber lifts WITH it. When the sheet top is unknown
    // (sheetTopPx null, or no viewport height) fall back to the safe-area +
    // composer-clearance offset (the prior static placement).
    const dockBottom: number | string =
      sheetTopPx != null && viewportH != null
        ? Math.max(0, viewportH - sheetTopPx + SCRUBBER_SHEET_DOCK_GAP_PX)
        : SCRUBBER_MOBILE_BOTTOM_CSS;
    posStyle = {
      position: "fixed",
      left: "50%",
      bottom: dockBottom,
      transform: "translateX(-50%)",
      transformOrigin: "bottom center",
    };
  } else {
    // Desktop: center in the open gutter between the (optional) left rail and the
    // (optional) right chat panel. Stable - moves only on a panel toggle.
    const rightInsetPx = chatCollapsed ? 0 : Math.max(0, chatWidthPx);
    const gutterLeft = Math.max(0, leftPanelWidthPx) + GUTTER_MARGIN_PX;
    const gutterRight =
      viewportW != null ? viewportW - rightInsetPx - GUTTER_MARGIN_PX : null;
    let centerX: number | string = "50%";
    if (gutterRight != null) {
      const gutterWidth = Math.max(SCRUBBER_MIN_WIDTH, gutterRight - gutterLeft);
      widthPx = Math.max(
        SCRUBBER_MIN_WIDTH,
        Math.min(SCRUBBER_WIDTH_DEFAULT, gutterWidth),
      );
      centerX = Math.round((gutterLeft + gutterRight) / 2);
    }
    posStyle = {
      position: "fixed",
      left: typeof centerX === "number" ? centerX : "50%",
      bottom: SCRUBBER_BOTTOM_DESKTOP_PX,
      transform: "translateX(-50%)",
      transformOrigin: "bottom center",
    };
  }

  // Portal to document.body so `fixed` positioning resolves against the
  // VIEWPORT, not the LayerPanel's transformed/filtered stacking context (the
  // panel is absolutely positioned + backdrop-filtered — same reason
  // ConfirmationDialog portals). This keeps the scrubber pinned bottom-center
  // while still being mounted from within the App tree.
  //
  // Layout: `▶ < ——●—— > x/N` — play/pause, prev-arrow, slider/track,
  // next-arrow, then a compact `x/N` counter. The group label + frame label
  // text are omitted (shown in the LayerPanel group row).
  return createPortal(
    <div
      data-testid="grace2-sequence-scrubber"
      role="group"
      aria-label={`${label} sequence scrubber`}
      style={{
        ...posStyle,
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "7px 12px",
        // The pill has an explicit width; box-sizing + overflow:hidden contain
        // every child (buttons + slider + x/N counter) WITHIN the rounded
        // bounds so nothing leaks past the right edge.
        boxSizing: "border-box",
        overflow: "hidden",
        // Joins the panel surface family (matches LayerLegend chrome).
        background: "rgba(17,18,23,0.82)",
        backdropFilter: "blur(6px)",
        WebkitBackdropFilter: "blur(6px)",
        border: "1px solid rgba(255,255,255,0.08)",
        borderRadius: 10,
        boxShadow: "0 2px 12px rgba(0,0,0,0.45)",
        fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
        color: "#e8e8ec",
        // MOBILE Z-ORDER (NATE 2026-06-22): on mobile sit UNDERNEATH the chat
        // bottom sheet (zIndex 32) so the scrubber never covers the composer; on
        // desktop the chat is a side panel, so keep the original higher z.
        zIndex: isMobile ? SCRUBBER_Z_MOBILE : SCRUBBER_Z_DESKTOP,
        width: widthPx,
      }}
    >
      {/* Play / pause toggle. Drives the shared AnimationController's `playing`
          state (App wires onPlayToggle). */}
      <ScrubButton
        testId="scrubber-play"
        label={playing ? "Pause sequence" : "Play sequence"}
        onClick={onPlayToggle}
        disabled={n <= 1}
      >
        {playing ? <IconPause size={14} /> : <IconPlay size={14} />}
      </ScrubButton>

      {/* Prev arrow */}
      <ScrubButton
        testId="scrubber-prev"
        label="Previous frame"
        onClick={() => stepBy(-1)}
        disabled={n <= 1}
      >
        <IconArrowLeft size={15} />
      </ScrubButton>

      {/* The slider — one detent per frame; dragging steps frames. The track
          sits between the two arrows. value tracks the LIVE frame index so the
          handle moves during auto-advance. */}
      <input
        type="range"
        min={0}
        max={Math.max(0, n - 1)}
        step={1}
        value={safeIndex}
        onChange={(e) => onStep(wrapIndex(Number(e.target.value), n))}
        aria-label={`${label} frame`}
        data-testid="scrubber-slider"
        style={{
          flex: 1,
          // A smaller min-width lets the slider YIELD so the buttons + x/N
          // counter always fit WITHIN the pill (no overflow past the right edge).
          minWidth: 24,
          height: 16,
          accentColor: "#4aa3ff",
          cursor: "pointer",
        }}
      />

      {/* Next arrow */}
      <ScrubButton
        testId="scrubber-next"
        label="Next frame"
        onClick={() => stepBy(1)}
        disabled={n <= 1}
      >
        <IconArrowRight size={15} />
      </ScrubButton>

      {/* Compact x/N counter — the only text readout on the scrubber. */}
      <span
        data-testid="scrubber-frame-label"
        style={{
          fontSize: 11,
          color: "#9aa1ab",
          fontVariantNumeric: "tabular-nums",
          flexShrink: 0,
          minWidth: 36,
          textAlign: "right",
        }}
      >
        {safeIndex + 1}/{n}
      </span>
    </div>,
    document.body,
  );
}

interface ScrubButtonProps {
  testId: string;
  label: string;
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}

function ScrubButton({
  testId,
  label,
  onClick,
  disabled,
  children,
}: ScrubButtonProps): JSX.Element {
  return (
    <button
      type="button"
      data-testid={testId}
      aria-label={label}
      title={label}
      onClick={onClick}
      disabled={disabled}
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        width: 26,
        height: 26,
        flexShrink: 0,
        padding: 0,
        background: "rgba(255,255,255,0.06)",
        border: "1px solid rgba(255,255,255,0.08)",
        borderRadius: 7,
        color: disabled ? "#5a626d" : "#cfd4db",
        cursor: disabled ? "default" : "pointer",
        transition: "color 120ms ease, background 120ms ease",
      }}
    >
      {children}
    </button>
  );
}
