// GRACE-2 web  -  LayerLegend (job-0065; interactive AOI-snapping keys, NATE
// overlay-layout spec 2026-06-17).
//
// Renders matplotlib-style colorbar "keys", one per continuous-raster layer that
// has a known style_preset. LEGEND v2 (NATE 2026-06-22): the key is ALWAYS a
// compact, FLAT two-row card (the old collapse/expand toggle is gone). Each key:
//   1. FLAT TWO-ROW LAYOUT - row 1 = title + hide(eye); row 2 = [min] gradient
//      bar [max] (horizontal: min at the LEFT end, max at the RIGHT end; vertical
//      when docked left/right: min at the BOTTOM, max at the TOP).
//   2. DRAGGABLE by the card BODY/EDGE (no dedicated grip icon); on release it
//      AUTO-SNAPS to the nearest VALID AOI side - LEFT, RIGHT, or TOP only.
//      BOTTOM is reserved for the sequence scrubber, so the legend never docks
//      there (legend_snap.nearestSide({ excludeBottom: true })).
//   3. DROP-ZONE SIGNALS - on drag-start, thin "area signal" affordances paint
//      along the valid snap targets (left/right/top edges of the AOI bbox) and
//      the nearest one highlights as the active target as the user drags toward
//      it; they clear on release/cancel (legend_snap.dropZoneSignals).
//   4. RESIZABLE per-key via a plain corner handle (width; height follows
//      content). SNAP-ORDERED counter-clockwise by stack order and STACKED
//      (offset outward) when keys share a side, so they never overlap.
//
// Positioning / data flow:
//   The component is rendered INSIDE the map container div (in Map.tsx) so it
//   anchors to the AOI box. Map.tsx passes:
//     - `layers`   : ordered layer list, top-of-stack first (LayerPanel order).
//     - `aoiRect`  : the TRUE projected AOI screen rectangle {left,top,right,
//                    bottom} (min/max over all four projected bbox corners). This
//                    is what the keys SNAP against  -  it carries the real AOI
//                    aspect ratio and on-screen skew, so the colorbar rails along
//                    the actual AOI edges, not a square-ish estimate.
//     - `anchor`   : the AOI bbox BOTTOM-edge midpoint {left, top} (projected)  - 
//                    used for the (already gap-nudged) vertical positioning the
//                    owner resolves; not the snap geometry.
//     - `barWidth` : the AOI bbox on-screen EAST-WEST extent in px (projected)  - 
//                    used to SIZE the default colorbar width.
//   Snap source of truth: when `aoiRect` is provided the keys snap CCW to ITS
//   four edges directly. When it is absent (off-screen / not yet projected) we
//   FALL BACK to reconstructing an approximate rect from `anchor` + `barWidth`
//   (legend_snap `rectFromAnchorAndWidth`  -  the bottom edge is exact, the height
//   is a square-ish estimate). When there is no AOI at all (no rect AND no
//   anchor/barWidth) the keys fall back to a static bottom-center stack so the
//   legend never vanishes.
//
// Invariant 1: this component displays received values only  -  no geography is
//   computed. minValue / maxValue / stops come from the preset registry (mirrors
//   the QML); the layer name comes from the ProjectLayerSummary wire. The
//   snap geometry is pure pixel math over the already-projected AOI rectangle.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ProjectLayerSummary, type LegendKey, type LegendClass } from "../contracts";
import { getStylePreset, StylePreset, type GradientStop } from "../lib/style-presets";
import {
  aoiScaleFactor,
  dropZoneSignals,
  layoutKeysToSides,
  nearestSide,
  rectFromAnchorAndWidth,
  sideForIndex,
  type AoiSide,
  type DropZoneSignal,
  type KeySize,
  type ScreenRect,
} from "../lib/legend_snap";
import { detectSequentialGroups } from "../LayerPanel";
import {
  getColormapStops,
  parseTitilerTileStyle,
  resolveLegendColormapStops,
  type ParsedRescale,
} from "../lib/titiler_colormap";
import { useIsMobile } from "../hooks/useIsMobile";
import { useAnimationState } from "../lib/use_animation_controller";
import { IconClose } from "./icons";
import { SCRUBBER_SHEET_DOCK_GAP_PX } from "./SequenceScrubber";

// JOB WEB-AOI-LEGEND (#157)  -  the collapsed "Show legend" pill must clear the
// mobile chat composer (the bottom-sheet at the foot of the screen). The pill
// is portaled to document.body with position:fixed, so on mobile we lift it
// above the composer by the device safe-area inset PLUS a fixed clearance that
// clears the collapsed sheet (drag handle + composer card + the sheet's own
// SHEET_BOTTOM_OFFSET lift). On desktop the chat is a right-side panel, not a
// bottom sheet, so the pill keeps its original low bottom-center position.
export const MOBILE_LEGEND_PILL_CLEARANCE_PX = 96;
export const MOBILE_LEGEND_PILL_BOTTOM_CSS = `calc(env(safe-area-inset-bottom) + ${MOBILE_LEGEND_PILL_CLEARANCE_PX}px)`;
export const DESKTOP_LEGEND_PILL_BOTTOM_PX = 24;

// MOBILE SHEET-TOP DOCK (NATE 2026-06-24)  -  gap (px) between the docked legend's
// bottom edge and the chat sheet's TOP edge, so the band sits just above the
// sheet without touching it. Mirrors the SequenceScrubber's dock gap.
export const MOBILE_SHEET_DOCK_GAP_PX = 8;

// MOBILE VIEWPORT CLAMP (NATE 2026-06-24 live-mobile feedback: "when we get the
// legend back it should stay the size of the window and not span past the window
// on mobile.") The docked legend band must NEVER bleed past the window edges. We
// clamp the key card to the viewport width minus a small side margin (and the
// notch safe-area insets), and to the band height available between the top
// safe-area and the docked sheet top. A card whose intrinsic content is wider
// than the clamp scrolls/shrinks WITHIN the clamp instead of overflowing the
// screen. These are pure CSS expressions so notched devices stay clear via env().
//
// Side margin (px) kept on EACH edge so the card never kisses the window border.
export const MOBILE_LEGEND_VIEWPORT_MARGIN_PX = 8;
/**
 * The mobile legend card's max-width: the full dynamic viewport width minus the
 * left+right safe-area insets and a side margin on each edge. `100dvw` tracks the
 * VISUAL viewport (so it shrinks with the mobile URL bar) and falls back to the
 * layout width via the CSS cascade on engines without dvw. Capping max-width (not
 * width) lets the existing fixed `cardWidth` shrink to fit a narrow phone while
 * never exceeding the window.
 */
export const MOBILE_LEGEND_MAX_WIDTH_CSS = `calc(100dvw - env(safe-area-inset-left) - env(safe-area-inset-right) - ${
  MOBILE_LEGEND_VIEWPORT_MARGIN_PX * 2
}px)`;
/**
 * MOBILE VIEWPORT CLAMP  -  the docked legend card's max-height: the screen height
 * from just below the top safe-area down to the docked sheet top (the band the
 * keys live in), minus a small margin. A tall stack of keys (or one very tall
 * card) scrolls WITHIN this rather than running off the top of the window. Given
 * the dock `bottom` offset (already viewportH - sheetTopPx + gap), the available
 * band height is sheetTopPx - gap - topInset - margin; we express it from the top
 * via env() so the notch is respected. Returns a CSS string. `sheetBottomPx` is
 * the resolved dock bottom (from sheetTopDockBottomPx); when it is null/unknown we
 * fall back to a generous viewport-height cap so we still never exceed the window.
 */
export function mobileLegendMaxHeightCss(sheetBottomPx: number | null): string {
  if (sheetBottomPx == null) {
    // No known sheet top -> just keep it inside the window (minus the insets).
    return `calc(100dvh - env(safe-area-inset-top) - env(safe-area-inset-bottom) - ${
      MOBILE_LEGEND_VIEWPORT_MARGIN_PX * 2
    }px)`;
  }
  // The card bottom sits `sheetBottomPx` up from the window bottom; the band
  // available above it is the rest of the window height minus the top safe-area
  // and a margin. 100dvh - sheetBottomPx - topInset - margin.
  return `calc(100dvh - ${sheetBottomPx}px - env(safe-area-inset-top) - ${MOBILE_LEGEND_VIEWPORT_MARGIN_PX}px)`;
}

/**
 * MOBILE SHEET-TOP DOCK (NATE 2026-06-24)  -  the CSS `bottom` offset that docks a
 * portaled (position:fixed) overlay just ABOVE the chat sheet's top edge, given
 * the sheet-top screen Y. bottom = viewportH - sheetTopPx + gap. Returns null
 * when the viewport / sheetTopPx is unavailable (SSR / no window) so the caller
 * falls back to its legacy placement. Exported for unit tests.
 */
export function sheetTopDockBottomPx(sheetTopPx: number): number | null {
  if (typeof window === "undefined" || !Number.isFinite(window.innerHeight)) {
    return null;
  }
  return Math.max(0, window.innerHeight - sheetTopPx + MOBILE_SHEET_DOCK_GAP_PX);
}

// MOBILE ONE-ROW BAND DOCK (NATE 2026-06-27) - mobile-only. The single horizontal
// legend row docks ABOVE the chat panel AND above the sequence scrubber, so the
// bottom-to-top HUD order is: chat panel -> scrubber -> legend. The scrubber's own
// mobile dock bottom is (viewportH - sheetTopPx + SCRUBBER_SHEET_DOCK_GAP_PX); the
// legend band must clear the FULL scrubber footprint above that, then a small
// legend gap. So:
//   legend bottom = (viewportH - sheetTopPx + scrubberGap) + SCRUBBER_FOOTPRINT_PX
//                   + LEGEND_BAND_DOCK_GAP_PX
// We reuse the foundation phase's scrubber gap (SCRUBBER_SHEET_DOCK_GAP_PX = 20)
// so the legend and the scrubber stay consistent, and the scrubber footprint
// (52, the same value SCRUBBER_FOOTPRINT_PX below uses) so the legend never sits
// on top of the scrubber.
//
// SCRUBBER_FOOTPRINT_PX mirrors the per-instance const in the component body
// (scrubber height ~40 + its 12px gap). Kept module-level here so the pure dock
// helper can reference it without the component scope.
export const MOBILE_LEGEND_SCRUBBER_FOOTPRINT_PX = 52;
// Small gap (px) between the legend row's bottom edge and the top of the
// scrubber's reserved band, so the separation reads as intentional breathing room.
export const LEGEND_BAND_DOCK_GAP_PX = 8;
/**
 * MOBILE ONE-ROW BAND DOCK (NATE 2026-06-27) - the CSS `bottom` offset that docks
 * the single horizontal legend row just ABOVE the scrubber (which is itself docked
 * above the chat sheet). Returns null when the viewport / sheetTopPx is
 * unavailable (SSR / no window) so the caller falls back. Exported for unit tests.
 *
 * `scrubberActive` controls whether the scrubber footprint is reserved: when the
 * scrubber is showing the legend lifts the full footprint above it; when it is not
 * showing the legend docks straight above the chat sheet (scrubber gap only),
 * mirroring the scrubber's own placement so the row stays just above the composer.
 */
export function legendBandDockBottomPx(
  sheetTopPx: number,
  scrubberActive: boolean,
): number | null {
  if (typeof window === "undefined" || !Number.isFinite(window.innerHeight)) {
    return null;
  }
  // The scrubber's docked top edge (the same math the SequenceScrubber uses).
  const scrubberTop = window.innerHeight - sheetTopPx + SCRUBBER_SHEET_DOCK_GAP_PX;
  // Reserve the scrubber footprint + a small legend gap ONLY when the scrubber is
  // actually on screen; otherwise dock straight above the chat sheet top band.
  const reserve = scrubberActive
    ? MOBILE_LEGEND_SCRUBBER_FOOTPRINT_PX + LEGEND_BAND_DOCK_GAP_PX
    : 0;
  return Math.max(0, scrubberTop + reserve);
}

/**
 * CHAT-OVERLAP HIDE (NATE 2026-06-28): true when an AOI-edge-snapped legend key
 * (its ABSOLUTE viewport top + height) would dip into the bottom HUD region --
 * the chat panel, plus the scrubber stacked above it when active. When a tall
 * AOI bbox extends down behind the chat, the right/left vertical key snaps over
 * the chat bar; NATE wants it to DISAPPEAR, not overlay. The caller hides such a
 * key on mobile. Pure + window-free (callers pass the measured chat-top px).
 *
 * `hudTopPx` = `sheetTopPx` minus the scrubber footprint (the scrubber sits
 * above the chat, so the legend must clear it too). Returns false when the
 * chat-top is unknown (SSR / desktop) so nothing is hidden off this path.
 */
export function legendKeyOverlapsBottomHud(
  keyTopPx: number,
  keyHeightPx: number,
  sheetTopPx: number | null,
  scrubberFootprintPx: number,
): boolean {
  if (sheetTopPx == null) return false;
  const hudTopPx = sheetTopPx - Math.max(0, scrubberFootprintPx);
  return keyTopPx + keyHeightPx > hudTopPx;
}

// Item a (Z-HIERARCHY, NATE 2026-06-20)  -  the legend must render BEHIND the chat
// (z=32) and the Layers/Cases panels (z=20) and the desktop hamburgers (z=30),
// but ABOVE the map. A single low z keeps the legend in the map's chrome layer
// so a user can always reach the chat + layers controls over it. (Previously the
// keys used z=50, which painted OVER the chat + panels  -  the reported bug.)
// On mobile the Layers drawer (z=40/41) is a transient OVERLAY; the legend
// staying at z=15 means it sits behind the open drawer, which is correct (the
// drawer is the focused surface). The mobile show/hide toggle moves INTO the
// drawer's expanded Layers section (item b) so it is never lost behind the chat.
export const LEGEND_Z_INDEX = 15;

export interface LayerLegendProps {
  /** Ordered layer list, top-of-stack first (same order as LayerPanel). */
  layers: ProjectLayerSummary[];
  /**
   * EDGE-RAIL snap (NATE 2026-06-17)  -  the TRUE projected AOI screen rectangle
   * {left, top, right, bottom} in absolute map-container coords. The owner
   * (Map.tsx) projects ALL FOUR bbox corners each move/zoom/render and passes
   * their min/max box here (computeBboxScreenRect). When present this is the
   * snap source of truth: the keys rail CCW along ITS four edges, so the snap
   * follows the real AOI aspect ratio + on-screen skew (not a square estimate).
   * Null/undefined => no true rect (off-screen / not yet projected) => the keys
   * fall back to reconstructing an approximate rect from `anchor` + `barWidth`.
   */
  aoiRect?: ScreenRect | null;
  /**
   * job-0321 (F43)  -  optional screen-space anchor: the AOI bbox BOTTOM-edge
   * midpoint {left, top} (absolute, map-container coords). The owner (Map.tsx)
   * projects it each move/zoom/render. Used as the FALLBACK snap-rect source
   * (with `barWidth`, via rectFromAnchorAndWidth) only when `aoiRect` is absent.
   * Null/undefined AND no `aoiRect` => no AOI on screen => the keys fall back to
   * a static bottom-center stack so they never vanish.
   */
  anchor?: { left: number; top: number } | null;
  /**
   * FIX 4 (NATE 2026-06-17)  -  the AOI bbox's ON-SCREEN east-west extent in px
   * (already clamped by Map.tsx). Used to SIZE the default colorbar width, and
   * (with `anchor`) to reconstruct the FALLBACK AOI rectangle for snapping when
   * `aoiRect` is absent. Null => no AOI bbox => static fallback width +
   * bottom-center stack.
   */
  barWidth?: number | null;
  /**
   * Item f (NATE 2026-06-20)  -  reserve vertical px below the AOI bottom edge so
   * the bottom-side keys clear the SCRUBBER (which pins bottom-center of the AOI
   * box). When > 0 the bottom-side keys are pushed down past the scrubber's
   * footprint so the legend is never obscured by it. 0 / undefined => no reserve.
   */
  bottomReservePx?: number | null;
  /**
   * Item b (NATE 2026-06-20)  -  CONTROLLED hidden state. When provided the
   * parent owns whether the legend is shown (the toggle lives in the Layers
   * panel on mobile). When omitted the legend keeps its own internal hidden
   * state (desktop default). Pair with `onHiddenChange`.
   */
  hidden?: boolean;
  /** Item b  -  fired when the user toggles hide/show (controlled mode). */
  onHiddenChange?: (hidden: boolean) => void;
  /**
   * Item b  -  suppress the floating "Show legend" pill entirely. On mobile the
   * show/hide affordance lives INSIDE the expanded Layers section (out of the
   * way of the chat composer), so the floating pill must not also render. The
   * keys themselves still render when not hidden.
   */
  suppressShowPill?: boolean;
  /**
   * LANE D (desktop dock) - the open desktop LEFT rail width (px) and RIGHT chat
   * width (px), used ONLY to center the desktop docked legend strip in the
   * VISIBLE map gutter (it shifts right by half the left inset, left by half the
   * right inset). 0 / undefined => centered on the full viewport. Ignored on
   * mobile (the snap pipeline is used there).
   */
  desktopLeftInsetPx?: number;
  desktopRightInsetPx?: number;
  /**
   * MOBILE SHEET-TOP DOCK (NATE 2026-06-24)  -  the on-screen Y of the mobile chat
   * sheet's TOP edge (App lifts the sheet geometry out of Chat). On MOBILE, when
   * provided, the bottom-center fallback colorbar keys AND the collapsed "Show
   * legend" pill dock their BOTTOM edge just ABOVE this Y (a clean band at the
   * chat-panel top, mirroring the desktop single-band dock) instead of floating
   * over the map with a fixed env()+clearance offset. We also suppress the
   * AOI-side snap on mobile in favor of this band when sheetTopPx is present,
   * since NATE wants the keys docked to the chat-panel top, not the AOI edges.
   * Null/undefined => the legacy mobile placement (AOI snap / bottom:24 +
   * env() pill). Ignored on desktop (the desktop dock is fixed-position).
   */
  sheetTopPx?: number | null;
  /**
   * MOBILE-ONLY HUD (NATE 2026-06-27) - the LIVE map zoom (MapLibre getZoom()),
   * tracked continuously on every move/zoom by Map.tsx (NOT the popup-only
   * currentZoom). Null until the first read / map teardown. A SUPPLEMENTARY signal
   * only - `aoiCornerPlaceable` is the primary dock trigger; this is threaded so
   * the legend can refine its zoom-keyed behavior in future without a new wire.
   * Ignored entirely on desktop (the desktop dock never reads it).
   */
  mapZoom?: number | null;
  /**
   * MOBILE-ONLY HUD (NATE 2026-06-27) - is the AOI box usefully on-screen for a
   * CORNER attach? Computed by Map.tsx (aoiRectCornerPlaceable).
   *   true  => keep the existing AOI-snap corner-attach behavior (the NORMAL,
   *            conservative case): the keys rail the real bbox edges.
   *   false => the corner attach is no longer useful (no AOI on screen / a tiny
   *            dot / fills the viewport / zoomed-panned away), so DOCK the single
   *            horizontal legend row above the chat panel (above the scrubber)
   *            instead of drifting or snapping to the AOI.
   * Default true so an absent prop preserves the prior corner-attach behavior.
   * Ignored entirely on desktop.
   */
  aoiCornerPlaceable?: boolean;
  /**
   * ZOOM-OUT HIDE (NATE 2026-06-27, MOBILE-ONLY) - the AOI bbox has zoomed OUT to a
   * tiny DOT on screen (its smaller on-screen extent < AOI_MIN_VISIBLE_EXTENT_PX,
   * computed by Map.tsx aoiRectTooSmallToShow). When true the mobile legend HIDES
   * entirely (renders null) - a speck-sized bbox carries no useful colorbar context
   * and the keys would just clutter. This takes PRECEDENCE over the AOI-snap /
   * band-dock decision (tiny dot -> hidden, not snapped). Default false so an absent
   * prop preserves today's behavior (no hide). Ignored entirely on desktop (the
   * desktop dock path early-returns before this is read).
   */
  aoiTooSmallToShow?: boolean;
  /**
   * CHART-OVERLAY HIDE (NATE 2026-06-28, MOBILE-ONLY) - Chat's full-viewport
   * ChartGallery overlay (`galleryOpen`) is open. The legend portals to
   * document.body and would otherwise paint ABOVE/around the chart on mobile, so
   * when true the mobile legend HIDES entirely (renders null). Default false so
   * an absent prop preserves today's behavior. Ignored on desktop (the prop
   * stays false there, and the gallery's z=10000 overlay already covers the
   * legend's z=15 anyway). Threaded App.chartGalleryOpen -> Map -> here.
   */
  chartOpen?: boolean;
}

/**
 * Item e (NATE 2026-06-20)  -  the SERIES IDENTITY of a raster layer: the colormap
 * + scale it paints with. Per-frame depth COGs ("Flood depth step N") AND the
 * max/peak depth layer all share the SAME colormap + rescale, so they form ONE
 * series and must collapse to ONE legend key (not one-per-frame + a peak key).
 *
 * The key is the TiTiler colormap_name + rescale (the SOURCE OF TRUTH for what
 * the map paints) when present  -  this is what the depth frames + the peak depth
 * layer all carry, so they share ONE key. When a layer carries NO TiTiler
 * colormap on its URL (a plain QGIS-WMS / preset-only single raster, with no
 * frame-truth scale), it is NOT part of a TiTiler series, so we key it by its
 * own layer_id (one key per such layer  -  the prior behavior). This keeps
 * distinct preset-only rasters each legible while folding the genuine
 * same-colormap depth series into a single key (item e).
 */
function seriesKeyFor(
  layer: ProjectLayerSummary,
  style: { rescale: ParsedRescale | null; colormapName: string | null },
): string {
  if (style.colormapName) {
    const r = style.rescale ? `${style.rescale.min},${style.rescale.max}` : "";
    return `cmap:${style.colormapName}|rescale:${r}`;
  }
  // No URL colormap -> not a TiTiler series; key per-layer so each distinct
  // preset-only raster keeps its own legend key.
  return `layer:${layer.layer_id}`;
}

/** A raster layer that resolved to a known preset  -  one legend key per entry. */
interface LegendKeyModel {
  layerId: string;
  preset: StylePreset;
  /**
   * FRAME-TRUTH (NATE 2026-06-19)  -  the rescale + colormap parsed from the
   * layer's TiTiler tile-template URL, when present. This is the SOURCE OF
   * TRUTH: when set, the key renders these bounds/colors (what the map actually
   * paints) instead of the preset guess. Null when the URL carries no such
   * params (QGIS WMS / non-animated single raster) => preset fallback.
   */
  rescale: ParsedRescale | null;
  /** Parsed-colormap CSS gradient stops (from `colormap_name`), or null. */
  colormapStops: GradientStop[] | null;
  /**
   * DATA-DRIVEN LEGEND (the colormap KEY from the data) - the resolved render
   * payload built DIRECTLY from a layer's `LegendKey` when present. When set this
   * is the SOURCE OF TRUTH (it OVERRIDES the preset + URL-rescale path), so a
   * layer that carries a legend renders its real range/colormap/classes/units.
   * Null/undefined => the legacy preset + URL-rescale path (legacy layers are
   * byte-for-byte unchanged). Built once in selectKeyModels via legendModelFor.
   */
  data?: ResolvedLegendData | null;
}

/**
 * DATA-DRIVEN LEGEND - the render payload resolved from a `LegendKey`. A
 * `continuous` key carries `stops` + numeric `min`/`max` (the colorbar); a
 * `categorical` key carries `classes` (one swatch row each). `title` + `unit` are
 * the legend label / units. Built in legendModelFor; consumed by the render path.
 */
interface ResolvedLegendData {
  kind: "continuous" | "categorical";
  title: string;
  unit: string;
  /** continuous: gradient stops + numeric bounds. */
  stops: GradientStop[] | null;
  min: number | null;
  max: number | null;
  /** categorical: the swatch rows (color + label, in order). */
  classes: LegendClass[] | null;
}

/**
 * DATA-DRIVEN LEGEND - resolve a layer's `LegendKey` into a ResolvedLegendData,
 * or null when it cannot drive a colorbar/class list (so the caller falls back to
 * the preset + URL-rescale path). Pure (Invariant 1): every value is read from the
 * legend the producer emitted; nothing is computed.
 *
 *   - continuous: stops from `colormap` (named ramp resolved via COLORMAP_STOPS,
 *     OR explicit [stop,hex] stops) + min/max from `vmin`/`vmax`. Null stops =>
 *     not renderable as a gradient -> fall back.
 *   - categorical: the `classes` list verbatim (color + label). Empty => fall back.
 *
 * `fallbackTitle` (the layer name) is used when the legend carries no `label`.
 */
/**
 * PRESENT-ONLY LAND-COVER LEGEND - a paletted raster (NLCD land cover) carries an
 * embedded GDAL color table that GDAL materializes to all 256 indices. The producer
 * (`_categorical_legend_from_colormap`) drops the transparent + opaque-black filler
 * slots, but the remaining UNUSED indices come back as a NEUTRAL-GREY filler ramp
 * (roughly `(i, i, i)`) it cannot tell apart from a real class -- so the emitted
 * legend includes greyed-out rows for classes the rendered raster does NOT contain
 * (e.g. NLCD 96/97), which makes the key very tall. Every REAL land-cover class
 * carries a chromatic NLCD color (the least-saturated standard class, Barren Land,
 * still has chroma ~16); a filler is achromatic (R==G==B, chroma 0). So "present"
 * == "actually has color" == chromatic. We drop the achromatic greys here so the
 * legend shows only present classes. The threshold sits between 0 (filler) and ~16
 * (the most-neutral real class) so no colored class is ever hidden. Applies to ANY
 * categorical legend, but ONLY achromatic-grey rows are dropped, so chromatic
 * categorical legends (Pelicun damage states, drought D0-D4) are untouched.
 */
const LANDCOVER_GREY_CHROMA_MAX = 10;

function legendClassHasColor(color: string): boolean {
  const hex = color.trim().replace(/^#/, "");
  let r: number;
  let g: number;
  let b: number;
  if (/^[0-9a-fA-F]{6}$/.test(hex)) {
    const n = parseInt(hex, 16);
    r = (n >> 16) & 0xff;
    g = (n >> 8) & 0xff;
    b = n & 0xff;
  } else if (/^[0-9a-fA-F]{3}$/.test(hex)) {
    r = parseInt(hex[0]! + hex[0]!, 16);
    g = parseInt(hex[1]! + hex[1]!, 16);
    b = parseInt(hex[2]! + hex[2]!, 16);
  } else {
    // Unparseable color -> keep it (never hide a class we cannot classify).
    return true;
  }
  const chroma = Math.max(r, g, b) - Math.min(r, g, b);
  return chroma > LANDCOVER_GREY_CHROMA_MAX;
}

function legendModelFor(
  legend: LegendKey | null | undefined,
  fallbackTitle: string,
): ResolvedLegendData | null {
  if (!legend) return null;
  const title = legend.label ?? fallbackTitle;
  const unit = legend.units ?? "";
  if (legend.kind === "categorical" || (legend.classes && legend.classes.length > 0)) {
    const classes = legend.classes;
    if (!classes || classes.length === 0) return null;
    // PRESENT-ONLY: drop achromatic-grey filler rows (absent NLCD classes). Never
    // blank a legend -- if every row reads as grey (no chromatic class survives)
    // keep the original list rather than hiding the whole key.
    const present = classes.filter((c) => legendClassHasColor(c.color));
    const shown = present.length > 0 ? present : classes;
    return { kind: "categorical", title, unit, stops: null, min: null, max: null, classes: shown };
  }
  // Continuous: resolve the colormap (named OR explicit) to stops.
  const stops = resolveLegendColormapStops(legend.colormap);
  if (!stops || stops.length === 0) return null;
  const min = typeof legend.vmin === "number" ? legend.vmin : null;
  const max = typeof legend.vmax === "number" ? legend.vmax : null;
  return { kind: "continuous", title, unit, stops, min, max, classes: null };
}

/** Per-key interactive UI state the user can drive (width + free pos + snap). */
interface KeyUiState {
  /** User-chosen width override (px). Undefined => default snapped width. */
  width?: number;
  /** While dragging, the free top-left screen position (overrides the snap). */
  free?: { left: number; top: number } | null;
  /**
   * SIDE-SNAP (NATE 2026-06-22) - the AOI side this key was dragged to on the
   * last release. When set it OVERRIDES the key's CCW index-assigned side, so
   * dragging a key to (say) the right edge SNAPS it there AND flips its
   * orientation to vertical (left/right -> vertical, top/bottom -> horizontal).
   * Undefined => the key keeps its default CCW side. Cleared is not needed; a
   * later drag overwrites it. Only meaningful when an AOI rect is present.
   */
  sideOverride?: AoiSide;
}

/**
 * DATA-DRIVEN LEGEND - a neutral StylePreset placeholder for a key whose render
 * payload comes from a `LegendKey` (`LegendKeyModel.data`). The model's `preset`
 * field is required, but when `data` is set the render path reads from `data`, so
 * this placeholder is never displayed; it only satisfies the model shape for a
 * legend-bearing layer that has no matching style_preset (e.g. a vector Pelicun
 * choropleth).
 */
const LEGEND_DATA_PLACEHOLDER_PRESET: StylePreset = {
  label: "",
  minValue: 0,
  maxValue: 1,
  unit: "",
  stops: [
    { position: 0, color: "#708090" },
    { position: 1, color: "#708090" },
  ],
};

/** Builds a CSS linear-gradient string from gradient stops (sorted by caller). */
function buildGradient(stops: GradientStop[]): string {
  const parts = stops
    .map((s) => `${s.color} ${(s.position * 100).toFixed(2)}%`)
    .join(", ");
  return `linear-gradient(to right, ${parts})`;
}

/**
 * FRAME-TRUTH (NATE 2026-06-19)  -  parses the TiTiler rescale + colormap out of a
 * layer's tile-template URL. The AWS frame layers carry the truth (rescale +
 * colormap_name) as query params on the XYZ template. We check `wms_url` first
 * (the field Map.tsx registers the tile source from  -  it holds the `{z}`
 * template for TiTiler layers) and fall back to `uri`. Returns null fields when
 * neither carries the params (QGIS WMS / non-animated single raster), so the
 * caller keeps the style_preset behavior. Never throws.
 */
function parseLayerTitilerStyle(layer: ProjectLayerSummary): {
  rescale: ParsedRescale | null;
  colormapStops: GradientStop[] | null;
  colormapName: string | null;
} {
  const fromWms = parseTitilerTileStyle(layer.wms_url);
  const fromUri = parseTitilerTileStyle(layer.uri);
  // Prefer whichever field actually carried each param (wms_url first).
  const rescale = fromWms.rescale ?? fromUri.rescale;
  const colormapName = fromWms.colormapName ?? fromUri.colormapName;
  return {
    rescale,
    colormapStops: getColormapStops(colormapName),
    colormapName: colormapName ?? null,
  };
}

// Default colorbar width when there is no AOI bbox to size against.
const STATIC_LEGEND_WIDTH = 320;
// Min/max width a user may resize a key to.
const KEY_MIN_WIDTH = 140;
const KEY_MAX_WIDTH = 520;
// Estimated key heights for the snap layout. These only feed the stacking math
// (so keys don't overlap); the rendered card sizes itself. LEGEND v2: the key is
// always the FLAT two-row card (title row + value/bar row), so there is a single
// horizontal-dock height; vertical-docked (left/right) keys are taller (a tall
// bar) so we feed a separate vertical height to the stacking math.
const KEY_HEIGHT_FLAT = 56;
// ITEM 3/4 (NATE 2026-06-23): the vertical card now stacks a ROTATED title
// (top) + max label + tall bar + min label + the X (bottom), so its footprint
// is taller than the old 150 (title-on-top-row + bar). Bump the stacking height
// so stacked vertical keys don't overlap. (Feeds only the snap-stacking math;
// the rendered card sizes itself.)
const KEY_HEIGHT_VERTICAL = 220;
// NATE 2026-06-22 (item 2): a VERTICAL-docked key (left/right side) is a TALL,
// NARROW bar - the wide horizontal width made it render nearly square. The card
// only has to fit the thin gradient bar + the centered min/max labels + padding,
// so we cap the vertical card to a narrow fixed width (the title ellipsizes).
// Horizontal keys keep the full AOI-sized width. Snapping is untouched - this is
// purely the rendered card width per orientation.
const VERTICAL_KEY_WIDTH = 76;
// CATEGORICAL NARROW WIDTH (NATE 2026-06-29) - a categorical key (NLCD land
// cover) renders swatch+label ROWS, NOT a gradient bar, so it must NOT inherit
// the wide AOI-sized colorbar width (`defaultWidth`, up to KEY_MAX_WIDTH=520):
// that produced a very WIDE card with a big empty gutter to the right of the
// short "swatch + label" rows. The land-cover legend should read like the OTHER
// side-docked legends: NARROW, snug to its content. A swatch (~12px) + gap + a
// typical NLCD label ("Developed, Medium Intensity") fits comfortably in ~190px;
// longer labels ellipsize (existing behavior). Applied to BOTH the snap-placement
// math and the rendered card width so the snap never drifts (the LEFT-snap offset
// depends on the rendered width matching the placement width).
const CATEGORICAL_KEY_WIDTH = 190;
// Horizontal gap between keys when falling back to the bottom-center stack.
const FALLBACK_STACK_GAP = 10;
// MOBILE ONE-ROW BAND DOCK (NATE 2026-06-28) - mobile-only. The BAND form's keys
// no longer use a compact fixed width; each fills the SCRUBBER WIDTH
// (scrubberMobileWidthPx) so the single common-case key reads as one clean bar in
// line with the scrubber and the band never rescales with the AOI bbox. The row
// scrolls horizontally (overflowX:auto) if multiple keys exceed the scrubber width.
// MOBILE ONE-ROW BAND DOCK - the horizontal gap (px) between key cards in the row.
const MOBILE_BAND_KEY_GAP_PX = 8;

// LANE D (desktop dock) - FIXED metrics for the static bottom-center desktop
// legend strip. No scaling: these are constants (NATE: "fixed size, no scaling,
// it just sticks there"). Each key is a compact horizontal card; the gradient
// BAR flexes to absorb slack so the value+unit labels never wrap.
const DESKTOP_DOCK_BOTTOM_PX = 16;
const DESKTOP_DOCK_GAP_PX = 8;
// desktop-only: the scrubber's reserved footprint (its height + its own 12px gap,
// the SAME value the mobile bottom-reserve uses as SCRUBBER_FOOTPRINT_PX).
const DESKTOP_DOCK_SCRUBBER_FOOTPRINT_PX = 52;
// desktop-only: extra bottom lift applied to the docked legend strip WHILE the
// sequence scrubber is active so the strip clears the scrubber's reserved
// footprint. The scrubber pins bottom-center (bottom 24, ~42 tall -> top ~66, z51);
// the legend strip sits at z15, so without this lift the scrubber paints over it.
// Lift = the scrubber footprint PLUS an explicit DESKTOP_DOCK_GAP_PX so the
// separation is intentional and survives a future scrubber-size change:
// 16 + (52 + 8) = 76, which sits at the top of the scrubber's reserved band.
const DESKTOP_DOCK_SCRUBBER_CLEARANCE_PX =
  DESKTOP_DOCK_SCRUBBER_FOOTPRINT_PX + DESKTOP_DOCK_GAP_PX;
const DESKTOP_DOCK_KEY_WIDTH = 200;
const DESKTOP_DOCK_TITLE_FONT = 11;
const DESKTOP_DOCK_LABEL_FONT = 10;
const DESKTOP_DOCK_BAR_THICKNESS = 12;

// DESKTOP DRAGGABLE DOCK (NATE 2026-06-28: "it should default to the bbox and
// then I should be able to also drag it to the bottom and have it static
// there.") The desktop legend strip is no longer locked to the bottom. It has
// TWO snap MODES the user toggles by DRAGGING the strip and releasing:
//   - "bbox":   the DEFAULT - the strip snaps just BELOW the projected AOI bbox
//               bottom edge (centered on the bbox), so it reads as the key for
//               that AOI. When no AOI rect is on screen it falls back to the
//               bottom dock so the strip never vanishes (and the legacy
//               no-aoiRect tests still see the bottom placement).
//   - "bottom": the static bottom-center dock (the prior LANE D behavior,
//               byte-for-byte) - the user dragged the strip down to the bottom
//               region and it stays parked there across reconnects/reloads.
// The chosen mode PERSISTS to localStorage (keyed stably) so "static there"
// actually sticks; default = "bbox" when nothing is stored. This is DESKTOP-ONLY
// (mobile keeps its AOI-snap / band-dock pipeline, byte-for-byte unchanged).
export type DesktopDockMode = "bbox" | "bottom";
export const LS_DESKTOP_LEGEND_DOCK = "grace2.desktopLegendDock";
// The viewport BOTTOM band (px from the window bottom): a drag release whose
// strip top-left falls within this band of the screen snaps to the "bottom"
// dock; a release above it snaps to the "bbox"-anchored mode. Generous so the
// user does not have to be pixel-precise to park it at the bottom.
export const DESKTOP_DOCK_BOTTOM_SNAP_BAND_PX = 140;
// Px gap kept BELOW the AOI bbox bottom edge when the strip is bbox-anchored
// (mirrors legend_snap's SIDE_GAP_PX so it rails the bbox consistently).
export const DESKTOP_DOCK_BBOX_GAP_PX = 10;
// Margin (px) kept on every viewport edge so the bbox-anchored strip never drags
// off-screen (NATE: "constrained to the viewport").
export const DESKTOP_DOCK_VIEWPORT_MARGIN_PX = 8;

/** Read the persisted desktop legend dock mode; default "bbox" on unset/garbage. */
export function readDesktopDockMode(): DesktopDockMode {
  try {
    const raw = localStorage.getItem(LS_DESKTOP_LEGEND_DOCK);
    return raw === "bottom" ? "bottom" : "bbox";
  } catch {
    return "bbox";
  }
}

/** Persist the desktop legend dock mode. Non-fatal on failure. */
export function writeDesktopDockMode(mode: DesktopDockMode): void {
  try {
    localStorage.setItem(LS_DESKTOP_LEGEND_DOCK, mode);
  } catch {
    /* non-fatal */
  }
}

/**
 * DESKTOP DRAGGABLE DOCK - which mode a drag RELEASE snaps to, from the dropped
 * strip's top-left Y (`dropTopPx`) and the viewport height. A release in the
 * bottom band of the screen parks it at the static bottom dock; anything higher
 * re-anchors it to the AOI bbox. Pure (window-free) so it is unit-testable.
 */
export function desktopDockModeForDrop(
  dropTopPx: number,
  viewportHeightPx: number,
): DesktopDockMode {
  if (!Number.isFinite(viewportHeightPx) || viewportHeightPx <= 0) return "bbox";
  return dropTopPx >= viewportHeightPx - DESKTOP_DOCK_BOTTOM_SNAP_BAND_PX
    ? "bottom"
    : "bbox";
}

/**
 * Selects one legend key per eligible raster layer, in stack order
 * (top-of-stack first).
 *
 * SEQUENTIAL-GROUP DEDUP (item 1): layers that belong to a sequential group
 * (enumerated temporal stack) all share the same colormap / preset. Rendering
 * one key per frame would crowd the screen with N identical bars. Instead we
 * detect groups here and emit exactly ONE key per group (using the active /
 * first member's preset). Non-grouped raster layers each still get their own
 * key.
 *
 * SERIES DEDUP (item e, NATE 2026-06-20): beyond sequential groups, ANY two
 * layers that share the SAME series identity (colormap + rescale, see
 * seriesKeyFor) collapse to ONE key. This folds the max/PEAK depth layer into
 * the same series as the per-frame depth COGs  -  they all paint with the same
 * colormap + scale, so they read off one legend, not one-per-frame + a peak.
 * The FIRST eligible layer (group or standalone) to claim a series key wins;
 * later layers with the same series key are skipped.
 */
/**
 * Stable series signature for a DATA-DRIVEN legend key. Two layers/frames whose
 * legends are identical (same kind + colormap + range + value_field + units +
 * class shape) MEAN the same thing, so they fold into ONE legend key (the same
 * dedup the legacy colormap+rescale series key gives). Returns null when there is
 * no legend (caller falls back to a per-layer key).
 */
function legendSeriesKey(legend: LegendKey | null | undefined): string | null {
  if (!legend) return null;
  const cmap = Array.isArray(legend.colormap)
    ? legend.colormap.map((s) => `${s[0]}:${s[1]}`).join(",")
    : (legend.colormap ?? "");
  const classes = legend.classes
    ? legend.classes
        .map((c) => `${c.value ?? ""}|${c.value_min ?? ""}|${c.value_max ?? ""}|${c.color}`)
        .join(";")
    : "";
  return [
    "legend",
    legend.kind,
    cmap,
    legend.vmin ?? "",
    legend.vmax ?? "",
    legend.value_field ?? "",
    legend.units ?? "",
    classes,
  ].join("|");
}

function selectKeyModels(layers: ProjectLayerSummary[]): LegendKeyModel[] {
  // Detect sequential groups to emit one key per group.
  const groups = detectSequentialGroups(layers);
  // Collect layer_ids that belong to a group; track which groups we've emitted.
  const groupedIds = new Set<string>();
  const emittedGroupKeys = new Set<string>();
  for (const g of groups) {
    for (const l of g.layers) groupedIds.add(l.layer_id);
  }

  // Item e  -  every series identity already emitted (by a group OR a standalone
  // layer). A later layer sharing one of these is the same colormap/scale, so it
  // dedups into the existing key rather than spawning a duplicate.
  const emittedSeries = new Set<string>();

  const out: LegendKeyModel[] = [];
  for (const l of layers) {
    // DATA-DRIVEN LEGEND (the colormap KEY from the data) - when a layer carries a
    // `LegendKey` we render it DIRECTLY, BEFORE the raster-only / preset gate. This
    // LIFTS the raster-only gate: a VECTOR layer (Pelicun choropleth, NLCD-style
    // classes, graduated damage) that carries a legend ALSO gets a legend key. The
    // resolved render payload (continuous gradient OR categorical swatches) is the
    // source of truth and overrides the preset/URL-rescale path below. One key per
    // layer_id here (legend-bearing layers are not part of the TiTiler colormap
    // series dedup, which keys off the colormap_name URL params the engines emit).
    const legendData = legendModelFor(l.legend, l.name);
    if (legendData) {
      // Route legend-bearing layers through the SAME group + series dedup the
      // legacy path uses, so a multi-frame animation series (every frame carrying
      // the same data-driven legend) yields ONE key, not one-per-frame. The series
      // identity for a legend layer is the legend's own signature (kind + colormap
      // + range + value_field + units + class shape) -- two layers/frames with the
      // same legend MEAN the same thing, so they fold into one key.
      const placeholderPreset =
        getStylePreset(l.style_preset ?? "") ?? LEGEND_DATA_PLACEHOLDER_PRESET;
      if (groupedIds.has(l.layer_id)) {
        const g = groups.find((gr) =>
          gr.layers.some((m) => m.layer_id === l.layer_id),
        );
        if (!g || emittedGroupKeys.has(g.key)) continue;
        emittedGroupKeys.add(g.key);
        const rep = g.layers[0] ?? l;
        const repLegend = legendModelFor(rep.legend, rep.name) ?? legendData;
        const gSeries = legendSeriesKey(rep.legend) ?? `group:${g.key}`;
        if (emittedSeries.has(gSeries)) continue;
        emittedSeries.add(gSeries);
        out.push({
          layerId: `group:${g.key}`,
          preset: placeholderPreset,
          rescale: null,
          colormapStops: null,
          data: repLegend,
        });
        continue;
      }
      const lSeries = legendSeriesKey(l.legend) ?? `legend:${l.layer_id}`;
      if (emittedSeries.has(lSeries)) continue;
      emittedSeries.add(lSeries);
      out.push({
        layerId: l.layer_id,
        // A synthetic preset placeholder: it is NOT read when `data` is set (the
        // render path reads from `data`). Required by the model shape only.
        preset: placeholderPreset,
        rescale: null,
        colormapStops: null,
        data: legendData,
      });
      continue;
    }

    if (l.layer_type !== "raster") continue;
    if (l.style_preset == null) continue;
    const preset = getStylePreset(l.style_preset);
    if (!preset) continue;

    if (groupedIds.has(l.layer_id)) {
      // Find the group this layer belongs to and emit one key for that group.
      const g = groups.find((gr) => gr.layers.some((m) => m.layer_id === l.layer_id));
      if (!g || emittedGroupKeys.has(g.key)) continue; // already emitted or no group
      emittedGroupKeys.add(g.key);
      // Use the first member of the group as the key representative (they all
      // share the same preset / colormap / rescale). layer_id keys the UI state.
      // FRAME-TRUTH: all frames share the same rescale+colormap, so parse them
      // from the representative frame's tile URL (item 4).
      const rep = g.layers[0];
      if (!rep) continue;
      const repPreset = getStylePreset(rep.style_preset ?? "");
      const repStyle = parseLayerTitilerStyle(rep);
      // Item e  -  register the group's series so a standalone peak/max layer with
      // the same colormap + rescale folds INTO this key instead of adding its own.
      const repSeries = seriesKeyFor(rep, {
        rescale: repStyle.rescale,
        colormapName: repStyle.colormapName,
      });
      if (emittedSeries.has(repSeries)) continue;
      emittedSeries.add(repSeries);
      out.push({
        layerId: `group:${g.key}`,
        // Fallback to the current layer's preset if the rep doesn't resolve.
        preset: repPreset ?? preset,
        rescale: repStyle.rescale,
        colormapStops: repStyle.colormapStops,
      });
    } else {
      const style = parseLayerTitilerStyle(l);
      // Item e  -  one key per SERIES. A standalone layer sharing a series with an
      // already-emitted group/layer (e.g. the peak depth alongside depth frames)
      // dedups into that existing key.
      const series = seriesKeyFor(l, {
        rescale: style.rescale,
        colormapName: style.colormapName,
      });
      if (emittedSeries.has(series)) continue;
      emittedSeries.add(series);
      out.push({
        layerId: l.layer_id,
        preset,
        rescale: style.rescale,
        colormapStops: style.colormapStops,
      });
    }
  }
  return out;
}

/**
 * Item b/e (NATE 2026-06-20)  -  does the legend have ANY content for these
 * layers? Exported so the Layers panel can decide whether to render the mobile
 * "show/hide legend" toggle (only when there's a legend to toggle).
 */
export function legendHasContent(layers: ProjectLayerSummary[]): boolean {
  return selectKeyModels(layers).length > 0;
}

export function LayerLegend({
  layers,
  aoiRect: trueRect,
  anchor,
  barWidth,
  bottomReservePx,
  hidden: hiddenProp,
  onHiddenChange,
  suppressShowPill,
  desktopLeftInsetPx = 0,
  desktopRightInsetPx = 0,
  sheetTopPx = null,
  // MOBILE-ONLY HUD (NATE 2026-06-27) - default aoiCornerPlaceable true so an
  // absent prop preserves the prior corner-attach behavior; mapZoom defaults null.
  // Both are consumed ONLY in the mobile path below (desktop early-returns first).
  mapZoom = null,
  aoiCornerPlaceable = true,
  // ZOOM-OUT HIDE (NATE 2026-06-27, mobile-only) - default false so an absent prop
  // keeps today's behavior; consumed ONLY in the mobile path below (desktop
  // early-returns first, so it is byte-for-byte unchanged).
  aoiTooSmallToShow = false,
  // CHART-OVERLAY HIDE (NATE 2026-06-28, mobile-only) - default false so an absent
  // prop keeps today's behavior; consumed ONLY in the mobile path below (desktop
  // early-returns first, so it is byte-for-byte unchanged).
  chartOpen = false,
}: LayerLegendProps): JSX.Element | null {
  // mapZoom is a supplementary signal threaded for future zoom-keyed refinement;
  // aoiCornerPlaceable is the primary dock trigger. Reference mapZoom so the lint
  // gate does not flag the consumed-but-not-yet-branched prop.
  void mapZoom;
  const dockLeftInsetPx = Math.max(0, desktopLeftInsetPx);
  const dockRightInsetPx = Math.max(0, desktopRightInsetPx);
  // One key per eligible raster layer, in stack order.
  const keyModels = useMemo(() => selectKeyModels(layers), [layers]);

  // JOB WEB-AOI-LEGEND (#157)  -  lift the collapsed "Show legend" pill above the
  // mobile chat composer so it does not overlap the bottom-sheet input form.
  const isMobile = useIsMobile();

  // Item f  -  is the SCRUBBER currently showing? The scrubber pins bottom-center
  // of the AOI box (just below its bottom edge), exactly where the legend's
  // bottom-side key would otherwise sit. The scrubber renders whenever the
  // shared AnimationController has an active group, so we read that here to
  // push the legend's bottom-side keys past the scrubber's footprint (the
  // explicit `bottomReservePx` prop, when supplied, overrides this default).
  const anim = useAnimationState();
  const scrubberActive = anim.activeGroupKey != null;

  // Per-key interactive state, keyed by layer_id so it survives reorders.
  const [uiState, setUiState] = useState<Record<string, KeyUiState>>({});
  // Whether the whole legend is hidden (the eye toggle on the first key).
  // Item b  -  CONTROLLED when `hidden` is supplied (the parent owns it so the
  // toggle can live in the Layers panel on mobile); else internal state.
  const [hiddenInternal, setHiddenInternal] = useState(false);
  const isControlled = hiddenProp !== undefined;
  const hidden = isControlled ? !!hiddenProp : hiddenInternal;
  const setHidden = useCallback(
    (next: boolean) => {
      if (!isControlled) setHiddenInternal(next);
      onHiddenChange?.(next);
    },
    [isControlled, onHiddenChange],
  );

  // DESKTOP DRAGGABLE DOCK (NATE 2026-06-28) - the persisted dock MODE
  // ("bbox" default vs "bottom" static dock) the user toggles by dragging the
  // desktop strip. Seeded from localStorage so a prior choice ("static there")
  // survives a reconnect/refresh; default "bbox". DESKTOP-ONLY (the mobile path
  // never reads it). Persisted on every change via writeDesktopDockMode.
  const [desktopDockMode, setDesktopDockModeState] =
    useState<DesktopDockMode>(readDesktopDockMode);
  const setDesktopDockMode = useCallback((mode: DesktopDockMode) => {
    setDesktopDockModeState(mode);
    writeDesktopDockMode(mode);
  }, []);
  // The free screen top-left WHILE the desktop strip is mid-drag (null when idle
  // / parked). On release this clears and the strip settles into its mode dock.
  const [desktopDragPos, setDesktopDragPos] = useState<{
    left: number;
    top: number;
  } | null>(null);
  // Live desktop-drag bookkeeping (pointer offset inside the strip + the latest
  // top-left), in a ref so the window listeners read fresh values without
  // re-binding per render.
  const desktopDragRef = useRef<{
    offsetX: number;
    offsetY: number;
    width: number;
    height: number;
    last: { left: number; top: number };
  } | null>(null);

  // Live drag bookkeeping. Tracks the key being dragged, the pointer offset
  // inside the card, the card size (so release can compute its CENTER for
  // nearest-side snapping), and the latest free top-left. Stored in a ref so the
  // window listeners read fresh values without re-binding each render.
  const dragRef = useRef<{
    layerId: string;
    offsetX: number;
    offsetY: number;
    width: number;
    height: number;
    last: { left: number; top: number };
  } | null>(null);

  // LEGEND v2 (DROP-ZONE SIGNALS, NATE 2026-06-22)  -  while a key is being
  // dragged we paint thin "area signal" affordances along the VALID snap targets
  // (left/right/top edges of the AOI; never bottom) and HIGHLIGHT the nearest one
  // as the live active target. `dragActive` is the layerId mid-drag (null when
  // idle), `activeDropSide` the side currently nearest the dragged card. Both are
  // React state (not just the drag ref) so the signals re-render as the drag
  // moves and clear on release/cancel.
  const [dragActive, setDragActive] = useState<string | null>(null);
  const [activeDropSide, setActiveDropSide] = useState<AoiSide | null>(null);

  // The AOI rectangle in screen space that the keys SNAP against. Prefer the
  // TRUE projected rect (all four bbox corners, min/max box) threaded from
  // Map.tsx  -  it carries the real AOI aspect ratio + on-screen skew, so the
  // CCW edge-rail follows the actual AOI edges. Only when the true rect is
  // absent (off-screen / not yet projected) do we fall back to reconstructing
  // an APPROXIMATE rect from anchor + barWidth (square-ish height estimate).
  // Null from both => no AOI on screen => bottom-center stack fallback.
  const aoiRect: ScreenRect | null = useMemo(
    () => trueRect ?? rectFromAnchorAndWidth(anchor, barWidth),
    [trueRect, anchor, barWidth],
  );
  // SIDE-SNAP (NATE 2026-06-22) - mirror the snap rect into a ref so the window
  // pointer-up handler (endDrag, a stable useCallback) can read the CURRENT rect
  // to compute the nearest AOI side on release without re-binding per render.
  const aoiRectRef = useRef<ScreenRect | null>(aoiRect);
  aoiRectRef.current = aoiRect;

  // Item d (SCALE WITH AOI, NATE 2026-06-20)  -  the legend chrome (font, padding,
  // bar height) scales with the AOI's on-screen size so a zoomed-out tiny bbox
  // gets a proportionally small legend (not a fixed-px one that dwarfs it) and a
  // zoomed-in big bbox gets a larger one  -  both clamped to [min, max] so the
  // legend is never unusably tiny or absurdly huge. Recomputes whenever the rect
  // changes (Map.tsx re-projects on every move/zoom and re-threads aoiRect).
  const scale = useMemo(() => aoiScaleFactor(aoiRect), [aoiRect]);

  // Default per-key width: the AOI on-screen width (clamped) when available,
  // else the static fallback (also scaled). A user resize overrides this per key.
  const defaultWidth = useMemo(() => {
    const w =
      typeof barWidth === "number" && Number.isFinite(barWidth) && barWidth > 0
        ? barWidth
        : STATIC_LEGEND_WIDTH * scale;
    return Math.max(KEY_MIN_WIDTH, Math.min(w, KEY_MAX_WIDTH));
  }, [barWidth, scale]);

  const widthFor = useCallback(
    (layerId: string): number => {
      const override = uiState[layerId]?.width;
      if (typeof override === "number" && override > 0) {
        return Math.max(KEY_MIN_WIDTH, Math.min(override, KEY_MAX_WIDTH));
      }
      return defaultWidth;
    },
    [uiState, defaultWidth],
  );

  // ITEM 5 (NATE 2026-06-22)  -  when the SCRUBBER is showing, the bottom-center
  // band is occupied by it, so START the CCW key layout on the RIGHT side (offset
  // +1 in the bottom->right->top->left order). The first key then rails VERTICALLY
  // down the right edge of the bbox (orientation follows the side, below), and
  // the legend + scrubber never collide. When NO scrubber is shown the offset is
  // 0 (the canonical bottom-first placement is unchanged).
  const sideStartOffset = scrubberActive ? 1 : 0;

  // SIDE-SNAP (NATE 2026-06-22) - the side each key docks on: the user's
  // drag-snap override when present (set by endDrag via nearestSide), else the
  // canonical CCW index side (with the scrubber start offset). This single array
  // is the source of truth for BOTH the snapped position AND the orientation /
  // side label below, so a dragged key snaps to its side with the matching
  // orientation. Keyed off keyModels so it stays in lockstep with the rendered keys.
  const resolvedSides: AoiSide[] = useMemo(
    () =>
      keyModels.map(
        (k, idx) =>
          uiState[k.layerId]?.sideOverride ??
          sideForIndex(idx + sideStartOffset),
      ),
    [keyModels, uiState, sideStartOffset],
  );

  // LEGEND v2  -  the orientation a key dock-side implies: left/right -> vertical
  // (a tall bar), top/bottom -> horizontal. Used for BOTH the stacking-height
  // math (a vertical key consumes a taller footprint) and the rendered bar.
  const orientationForSide = useCallback(
    (side: AoiSide): "vertical" | "horizontal" =>
      side === "left" || side === "right" ? "vertical" : "horizontal",
    [],
  );

  // Compute the snapped position for every key. When an AOI rect is present we
  // lay keys out CCW (bottom, right, top, left, stacking on repeat). With no AOI
  // we stack them along a static bottom-center row. A key being actively dragged
  // uses its `free` position instead of the snapped one. LEGEND v2: the key is
  // always the FLAT two-row card, so the stacking HEIGHT is the flat height for
  // a horizontal dock and the taller vertical-bar height for a left/right dock.
  const sizes: KeySize[] = useMemo(
    () =>
      keyModels.map((k, idx) => {
        const side = resolvedSides[idx] ?? sideForIndex(idx + sideStartOffset);
        const vertical = orientationForSide(side) === "vertical";
        const isCategorical = k.data?.kind === "categorical";
        // OUTSIDE-THE-BBOX (NATE 2026-06-29): legend_snap places a TOP-side key at
        // `aoi.top - gap - size.height` and a LEFT-side key at `aoi.left - gap -
        // size.width`, so size MUST be >= the ACTUAL rendered card or the card's
        // far edge creeps back INTO the bbox. A categorical card is taller than the
        // flat colorbar (a title + one swatch row per class), so we feed a
        // CONSERVATIVE height (base + per-class rows, scaled) that is >= the card in
        // EITHER orientation (a horizontal categorical wraps to FEWER rows than this
        // column estimate). Overestimating only pushes the card further OUTSIDE the
        // bbox - never inside it. Continuous keys keep the flat/vertical constants.
        const nClasses = isCategorical ? k.data?.classes?.length ?? 0 : 0;
        const height = isCategorical
          ? Math.round(20 + (28 + nClasses * 20) * scale)
          : vertical
            ? KEY_HEIGHT_VERTICAL
            : KEY_HEIGHT_FLAT;
        // LEFT-SNAP OFFSET FIX (NATE 2026-06-28): legend_snap places a LEFT-side
        // key at `aoi.left - gap - size.width`, so size.width MUST equal the key's
        // ACTUAL rendered width or the key lands too far left (the right side uses
        // `aoi.right + gap`, width-independent, which is why only the LEFT bar
        // drifted). A vertical (left/right) NON-categorical key renders as a
        // NARROW bar (VERTICAL_KEY_WIDTH*scale), not the full horizontal card --
        // mirror the render's `cardWidth` here so placement matches what paints.
        // A categorical key (land cover) is a NARROW swatch+label list - never the
        // wide AOI-sized colorbar width (NATE 2026-06-29). A non-categorical
        // vertical key is the slim gradient bar. Everything else gets the AOI
        // width. Keep this in lockstep with the render `cardWidth` below.
        const width = isCategorical
          ? Math.round(CATEGORICAL_KEY_WIDTH * scale)
          : vertical
            ? Math.round(VERTICAL_KEY_WIDTH * scale)
            : widthFor(k.layerId);
        return { width, height };
      }),
    [keyModels, widthFor, resolvedSides, sideStartOffset, orientationForSide, scale],
  );

  // Item f  -  extra px to push bottom-side keys past the scrubber's footprint
  // (the scrubber pins bottom-center of the AOI box). The explicit prop wins;
  // otherwise default to a sensible reserve WHENEVER the scrubber is active so
  // the legend is never obscured by it. 0 when neither applies.
  const SCRUBBER_FOOTPRINT_PX = 52; // scrubber height (~40) + its 12px gap.
  const bottomReserve =
    typeof bottomReservePx === "number" && bottomReservePx > 0
      ? bottomReservePx
      : scrubberActive
        ? SCRUBBER_FOOTPRINT_PX
        : 0;

  const snapped = useMemo(() => {
    if (aoiRect) {
      // Lay each key against its RESOLVED side (override or CCW), stacking keys
      // that share a side so they never overlap.
      const base = layoutKeysToSides(aoiRect, sizes, resolvedSides);
      // Item f  -  shove any bottom-side keys down past the scrubber so the legend
      // is never obscured by it (the scrubber sits just below the AOI bottom
      // edge). Top/right/left keys are untouched. With sideStartOffset=1 the
      // first 3 keys avoid the bottom entirely; this still guards a 4th+ key.
      if (bottomReserve <= 0) return base;
      return base.map((r) =>
        r.side === "bottom" ? { ...r, top: r.top + bottomReserve } : r,
      );
    }
    // No AOI: lay the keys out as a bottom-center row (each key centered, then
    // stacked upward so they don't overlap). We synthesize a degenerate rect at
    // a nominal bottom-center point; this keeps the legend visible.
    let consumed = 0;
    return sizes.map((s) => {
      const top = -(FALLBACK_STACK_GAP + consumed + s.height);
      consumed += s.height + FALLBACK_STACK_GAP;
      return { left: -s.width / 2, top, side: "bottom" as AoiSide };
    });
  }, [aoiRect, sizes, bottomReserve, resolvedSides]);

  // MOBILE SHEET-TOP DOCK (NATE 2026-06-24)  -  a bottom-center-RELATIVE stack
  // (left/top offsets from a bottom-center origin, same convention as the
  // AOI-less fallback above) used ONLY by the mobile sheet-top band dock. Unlike
  // `snapped` (which returns ABSOLUTE coords when an AOI rect is present), this is
  // ALWAYS bottom-center-relative so the band can be realized with left:50% + a
  // translate even while an AOI box is on screen (the band dock SUPPRESSES the
  // AOI snap on mobile). Keys stack upward so they never overlap.
  const mobileBandStack = useMemo(() => {
    let consumed = 0;
    return sizes.map((s) => {
      const top = -(FALLBACK_STACK_GAP + consumed + s.height);
      consumed += s.height + FALLBACK_STACK_GAP;
      return { left: -s.width / 2, top };
    });
  }, [sizes]);

  // --- drag wiring --------------------------------------------------------- //

  // LEGEND v2  -  the dropped card's CENTER mapped to the nearest VALID snap side
  // (left/right/top; BOTTOM excluded - it is reserved for the scrubber). Returns
  // undefined when there is no AOI rect (off-screen) so the caller leaves the
  // AOI-less bottom-center fallback in place. Shared by the live drag (to drive
  // the active drop-zone highlight) and the release (to set the side override).
  const nearestLegendSide = useCallback(
    (centerLeft: number, centerTop: number, w: number, h: number): AoiSide | undefined => {
      const rect = aoiRectRef.current;
      if (!rect) return undefined;
      return nearestSide(
        rect,
        { x: centerLeft + w / 2, y: centerTop + h / 2 },
        { excludeBottom: true },
      );
    },
    [],
  );

  const endDrag = useCallback(() => {
    const drag = dragRef.current;
    dragRef.current = null;
    // Clear the drop-zone signals + active highlight on release/cancel.
    setDragActive(null);
    setActiveDropSide(null);
    if (!drag) return;
    const layerId = drag.layerId;
    // SIDE-SNAP / LEGEND v2 - on release the key SNAPS to the VALID AOI side
    // NEAREST where it was dropped (LEFT/RIGHT/TOP only - bottom is reserved for
    // the scrubber). We take the dropped card's CENTER and ask legend_snap's
    // bottom-excluded nearestSide which edge it is closest to, then store that as
    // a per-key sideOverride. The layout + orientation + side label all honor the
    // override, so dragging a key toward the right snaps it there AND flips it to
    // a vertical bar, and a drag toward the BOTTOM snaps to the nearest of
    // left/right/top instead. With NO AOI rect (off-screen) there is no side to
    // snap to, so we just clear `free` and the bottom-center fallback holds.
    const snappedSide = nearestLegendSide(
      drag.last.left,
      drag.last.top,
      drag.width,
      drag.height,
    );
    setUiState((prev) => {
      const next = { ...prev };
      const cur = next[layerId] ?? {};
      next[layerId] = {
        ...cur,
        free: null,
        // Only record an override when we actually resolved a side (AOI present).
        ...(snappedSide ? { sideOverride: snappedSide } : {}),
      };
      return next;
    });
  }, [nearestLegendSide]);

  const onPointerMoveWindow = useCallback(
    (ev: PointerEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      const left = ev.clientX - drag.offsetX;
      const top = ev.clientY - drag.offsetY;
      // Track the latest free top-left on the drag ref so release can compute the
      // card center for nearest-side snapping (the ref read is fresh; state is not).
      drag.last = { left, top };
      // LEGEND v2  -  live-highlight the drop-zone signal nearest the card center
      // so the user sees which side it will snap to (left/right/top only).
      const side = nearestLegendSide(left, top, drag.width, drag.height);
      setActiveDropSide(side ?? null);
      setUiState((prev) => {
        const next = { ...prev };
        const cur = next[drag.layerId] ?? {};
        next[drag.layerId] = { ...cur, free: { left, top } };
        return next;
      });
    },
    [nearestLegendSide],
  );

  // Bind window listeners once; they read the live ref so no re-bind per drag.
  useEffect(() => {
    const move = (e: PointerEvent) => onPointerMoveWindow(e);
    const up = () => endDrag();
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
    };
  }, [onPointerMoveWindow, endDrag]);

  const startDrag = useCallback(
    (layerId: string, ev: React.PointerEvent<HTMLElement>) => {
      // EDGE/BODY-GRAB (NATE 2026-06-22) - the whole legend card body IS the drag
      // handle (there is no separate grip icon - that was visual clutter NATE
      // asked to drop); a pointer-down anywhere on the chrome starts the drag.
      // We only EXCLUDE the interactive controls (the hide button) and the resize
      // handle, all tagged data-legend-no-drag, so a click on those does its own
      // thing instead of dragging.
      const target = ev.target as HTMLElement;
      if (target.closest("[data-legend-no-drag]")) return;
      const card = ev.currentTarget.getBoundingClientRect();
      dragRef.current = {
        layerId,
        offsetX: ev.clientX - card.left,
        offsetY: ev.clientY - card.top,
        width: card.width,
        height: card.height,
        last: { left: card.left, top: card.top },
      };
      // LEGEND v2  -  arm the drop-zone signals for this drag and seed the active
      // side from the card's current center, so the targets appear immediately.
      setDragActive(layerId);
      setActiveDropSide(
        nearestLegendSide(card.left, card.top, card.width, card.height) ?? null,
      );
      // Seed a free position at the current spot so the first move is smooth.
      setUiState((prev) => {
        const next = { ...prev };
        const cur = next[layerId] ?? {};
        next[layerId] = {
          ...cur,
          free: { left: card.left, top: card.top },
        };
        return next;
      });
    },
    [nearestLegendSide],
  );

  // --- DESKTOP DRAGGABLE DOCK wiring (NATE 2026-06-28) --------------------- //
  // The WHOLE desktop strip is the drag handle. While dragging it follows the
  // pointer (free position, viewport-clamped); on release the drop Y decides the
  // mode: dropped in the bottom band -> "bottom" static dock; higher up ->
  // "bbox"-anchored. The chosen mode persists (localStorage). Listeners are bound
  // once and read the live ref (no per-drag rebind). DESKTOP-ONLY usage.
  const endDesktopDrag = useCallback(() => {
    const drag = desktopDragRef.current;
    desktopDragRef.current = null;
    setDesktopDragPos(null);
    if (!drag) return;
    // Snap to a mode from where the strip's top-left landed (its top Y vs the
    // viewport bottom band). Persist so "static there" sticks across reloads.
    const vh =
      typeof window !== "undefined" && Number.isFinite(window.innerHeight)
        ? window.innerHeight
        : 0;
    setDesktopDockMode(desktopDockModeForDrop(drag.last.top, vh));
  }, [setDesktopDockMode]);

  const onDesktopDragMove = useCallback((ev: PointerEvent) => {
    const drag = desktopDragRef.current;
    if (!drag) return;
    // Clamp the free top-left so the strip never drags off-screen (NATE:
    // "constrained to the viewport"). Keep at least one margin of the strip on
    // screen on every edge.
    const vw =
      typeof window !== "undefined" && Number.isFinite(window.innerWidth)
        ? window.innerWidth
        : drag.width;
    const vh =
      typeof window !== "undefined" && Number.isFinite(window.innerHeight)
        ? window.innerHeight
        : drag.height;
    const m = DESKTOP_DOCK_VIEWPORT_MARGIN_PX;
    const rawLeft = ev.clientX - drag.offsetX;
    const rawTop = ev.clientY - drag.offsetY;
    const left = Math.max(m, Math.min(rawLeft, Math.max(m, vw - drag.width - m)));
    const top = Math.max(m, Math.min(rawTop, Math.max(m, vh - drag.height - m)));
    drag.last = { left, top };
    setDesktopDragPos({ left, top });
  }, []);

  useEffect(() => {
    const move = (e: PointerEvent) => onDesktopDragMove(e);
    const up = () => endDesktopDrag();
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
    };
  }, [onDesktopDragMove, endDesktopDrag]);

  const startDesktopDrag = useCallback(
    (ev: React.PointerEvent<HTMLElement>) => {
      const strip = ev.currentTarget.getBoundingClientRect();
      desktopDragRef.current = {
        offsetX: ev.clientX - strip.left,
        offsetY: ev.clientY - strip.top,
        width: strip.width,
        height: strip.height,
        last: { left: strip.left, top: strip.top },
      };
      setDesktopDragPos({ left: strip.left, top: strip.top });
    },
    [],
  );

  // --- resize wiring ------------------------------------------------------- //

  const resizeRef = useRef<{
    layerId: string;
    startX: number;
    startWidth: number;
  } | null>(null);

  const onResizeMove = useCallback((ev: PointerEvent) => {
    const r = resizeRef.current;
    if (!r) return;
    const delta = ev.clientX - r.startX;
    const w = Math.max(KEY_MIN_WIDTH, Math.min(r.startWidth + delta, KEY_MAX_WIDTH));
    setUiState((prev) => {
      const next = { ...prev };
      const cur = next[r.layerId] ?? {};
      next[r.layerId] = { ...cur, width: w };
      return next;
    });
  }, []);

  const endResize = useCallback(() => {
    resizeRef.current = null;
  }, []);

  useEffect(() => {
    const move = (e: PointerEvent) => onResizeMove(e);
    const up = () => endResize();
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
    };
  }, [onResizeMove, endResize]);

  const startResize = useCallback(
    (layerId: string, ev: React.PointerEvent<HTMLElement>) => {
      ev.stopPropagation();
      ev.preventDefault();
      resizeRef.current = {
        layerId,
        startX: ev.clientX,
        startWidth: widthFor(layerId),
      };
    },
    [widthFor],
  );

  // Nothing eligible => render nothing (preserves the old hide contract).
  if (keyModels.length === 0) return null;

  // ZOOM-OUT HIDE (NATE 2026-06-27, MOBILE-ONLY) - the AOI bbox is a tiny dot on
  // screen (the user zoomed OUT far). HIDE the legend entirely - a speck-sized
  // bbox carries no useful colorbar context. Placed AFTER every hook (so hook
  // order is stable) and gated to MOBILE so desktop is byte-for-byte unchanged
  // (desktop never receives this prop and would early-return at !isMobile anyway).
  // Takes PRECEDENCE over the AOI-snap / band-dock decision below (tiny dot ->
  // hidden, not snapped).
  if (isMobile && aoiTooSmallToShow) return null;

  // CHART-OVERLAY HIDE (NATE 2026-06-28, MOBILE-ONLY) - Chat's full-viewport
  // ChartGallery overlay is open. The legend portals to document.body and would
  // paint ABOVE/around the chart on mobile, so HIDE it entirely while a chart is
  // showing. Placed AFTER every hook (stable hook order) and gated to MOBILE so
  // desktop is byte-for-byte unchanged (desktop keeps the prop false and would
  // early-return at !isMobile below anyway; the gallery's z=10000 overlay also
  // already covers the legend's z=15 there).
  if (isMobile && chartOpen) return null;

  // LEGEND v2 (DROP-ZONE SIGNALS)  -  while a key is being dragged, paint a thin
  // "area signal" along each VALID snap target (left/right/top edges of the AOI;
  // never bottom) and highlight the one nearest the dragged card. Computed here
  // (after the hidden/empty guards) only when a drag is in flight AND an AOI rect
  // exists; cleared automatically on release/cancel (dragActive -> null).
  const dropSignals: DropZoneSignal[] =
    dragActive && aoiRect
      ? dropZoneSignals(aoiRect, { activeSide: activeDropSide })
      : [];

  // MOBILE SHEET-TOP DOCK (NATE 2026-06-24)  -  when the chat sheet's top edge Y is
  // known, dock the mobile bottom-center fallback keys AND the collapsed pill
  // just ABOVE the sheet (a clean band at the chat-panel top) instead of the
  // legacy env()+clearance bottom. Null on desktop / SSR -> the legacy placement
  // is used. Recomputed each render (App threads a fresh sheetTopPx on resize /
  // expand / collapse / drag).
  const mobileDockBottomPx =
    isMobile && sheetTopPx != null ? sheetTopDockBottomPx(sheetTopPx) : null;

  // MOBILE ONE-ROW BAND DOCK (NATE 2026-06-27) - mobile-only. The CSS `bottom` for
  // the single horizontal legend ROW: docked just ABOVE the scrubber (which is
  // itself docked above the chat sheet), so the bottom-to-top order is
  // chat -> scrubber -> legend. Null on desktop / SSR / no sheetTopPx. Distinct
  // from `mobileDockBottomPx` (which docks the collapsed pill straight above the
  // chat sheet, unchanged); the expanded ROW lifts the scrubber footprint above it.
  const bandRowBottomPx =
    isMobile && sheetTopPx != null
      ? legendBandDockBottomPx(sheetTopPx, scrubberActive)
      : null;

  // BAND FORM WIDTH (NATE 2026-06-28) - the band must be the SAME WIDTH AS THE
  // FULL CHAT-PANEL WIDTH (NATE 2026-06-28): the band legend spans the ENTIRE
  // width of the mobile chat panel (the bottom sheet is width:100%), NOT the
  // narrower scrubber pill and NOT the AOI `aoiScaleFactor` scale -- one clean
  // full-width bar above the scrubber that does not change size with the bbox.
  // Full content width = the live viewport minus the standard symmetric side
  // margin (matches the MOBILE_LEGEND_MAX_WIDTH_CSS cap / the full-bleed sheet).
  // SSR/no-window -> a sane mobile fallback (the band only renders on mobile).
  const bandWidthPx =
    typeof window !== "undefined" && Number.isFinite(window.innerWidth)
      ? Math.max(0, window.innerWidth - 2 * MOBILE_LEGEND_VIEWPORT_MARGIN_PX)
      : 360;

  // BAND-vs-EDGE GATE (NATE 2026-06-28) - on mobile, when the AOI bbox IS on screen
  // and would normally corner-attach, an AOI-edge-snapped key can still dip DOWN
  // behind the bottom HUD (the chat panel, plus the scrubber stacked above it when
  // active) - e.g. a tall bbox extending past the chat, where the left/right
  // VERTICAL key rails over the chat bar. NATE: "do not make it disappear, snap to
  // above the scrubber if it is intersecting the chat panel." So we detect any
  // snapped key whose ABSOLUTE viewport span crosses the HUD top and, when one
  // does, switch to the BAND form (docked above the scrubber) instead of the edge
  // form. `snapped` + `sizes` are the same arrays the edge render uses, so the
  // overlap check matches exactly what would paint. Mobile-only + only when a band
  // bottom is placeable (sheetTopPx present); desktop never reaches here.
  const aoiSnapOverlapsHud =
    isMobile &&
    aoiRect != null &&
    bandRowBottomPx != null &&
    snapped.some((pos, idx) =>
      legendKeyOverlapsBottomHud(
        pos.top,
        sizes[idx]?.height ?? 0,
        sheetTopPx,
        scrubberActive ? MOBILE_LEGEND_SCRUBBER_FOOTPRINT_PX : 0,
      ),
    );

  // MOBILE ONE-ROW BAND DOCK (NATE 2026-06-27) - mobile-only. Dock the single
  // horizontal legend row above the chat panel when ANY of: there is no AOI bbox on
  // screen (the prior fallback); the AOI corner attach is no longer useful
  // (aoiCornerPlaceable === false: zoomed too far in/out, AOI off-screen / a tiny
  // dot / filling the viewport); OR an AOI-edge snap would INTERSECT the bottom HUD
  // (aoiSnapOverlapsHud, NATE 2026-06-28 - "snap to above the scrubber if it is
  // intersecting the chat panel"). When the corner attach IS useful AND no snapped
  // key overlaps the HUD we KEEP the AOI-edge (corner-attach) behavior below. Gated
  // on a known band bottom (sheetTopPx present) so we only dock when we can place
  // the row; else the legacy AOI-snap / bottom-center fallback holds.
  const bandDockActive =
    isMobile &&
    bandRowBottomPx != null &&
    (!aoiRect || aoiCornerPlaceable === false || aoiSnapOverlapsHud);

  // When fully hidden, render only a tiny "show legend" pill (bottom-center).
  // Portal to document.body so it appears above the mobile chat panel.
  //
  // Item b  -  when `suppressShowPill` is set the floating pill is NOT rendered:
  // the show/hide affordance lives inside the expanded Layers section instead
  // (the parent renders <MobileLegendToggle/>), so the pill must not also float
  // over the chat. We render nothing in that case (the parent owns re-showing).
  if (hidden) {
    if (suppressShowPill) return null;
    return createPortal(
      <button
        type="button"
        data-testid="grace2-layer-legend-show"
        onClick={() => setHidden(false)}
        style={{
          position: "fixed",
          // MOBILE SHEET-TOP DOCK (NATE 2026-06-24)  -  dock the pill just ABOVE the
          // chat sheet's top edge (a clean band) when its Y is known. Else fall
          // back to JOB WEB-AOI-LEGEND (#157): on mobile sit ABOVE the composer
          // (safe-area inset + collapsed-sheet clearance); on desktop keep the
          // original low bottom-center position (no bottom sheet).
          bottom:
            mobileDockBottomPx != null
              ? mobileDockBottomPx
              : isMobile
                ? MOBILE_LEGEND_PILL_BOTTOM_CSS
                : DESKTOP_LEGEND_PILL_BOTTOM_PX,
          left: "50%",
          transform: "translateX(-50%)",
          // MOBILE VIEWPORT CLAMP (NATE 2026-06-24)  -  keep the docked pill inside
          // the window too (it is short text so rarely at risk, but the band must
          // never span past the edges). Desktop keeps its natural width.
          ...(isMobile
            ? { maxWidth: MOBILE_LEGEND_MAX_WIDTH_CSS, boxSizing: "border-box" as const }
            : {}),
          padding: "5px 12px",
          background: "rgba(17,18,23,0.78)",
          backdropFilter: "blur(6px)",
          WebkitBackdropFilter: "blur(6px)",
          border: "1px solid rgba(255,255,255,0.10)",
          borderRadius: 999,
          color: "#ddd",
          fontFamily: "system-ui, sans-serif",
          fontSize: 11,
          fontWeight: 600,
          cursor: "pointer",
          pointerEvents: "auto",
          // Item a  -  BELOW the chat (z=32) + panels (z=20); the pill is part of
          // the legend's map-chrome layer, never over the chat/layers controls.
          zIndex: LEGEND_Z_INDEX,
        }}
      >
        Show legend
      </button>,
      document.body,
    );
  }

  // DESKTOP DRAGGABLE DOCK (NATE 2026-06-28: "it should default to the bbox and
  // then I should be able to also drag it to the bottom and have it static
  // there.") The desktop legend strip is DRAGGABLE with TWO snap modes:
  //   - "bbox" (DEFAULT): the strip snaps just BELOW the projected AOI bbox
  //     bottom edge, centered on the bbox - it reads as the key for that AOI.
  //     When no AOI rect is on screen we fall back to the bottom dock so the
  //     strip never vanishes (and the legacy no-aoiRect placement is preserved).
  //   - "bottom": the static bottom-center dock (the prior LANE D behavior,
  //     byte-for-byte) - the user dragged the strip to the bottom and it stays.
  // The mode PERSISTS to localStorage (default "bbox"). While dragging, the strip
  // follows the pointer (viewport-clamped). The whole AOI-snap / drag / resize /
  // scale machinery for the per-key MOBILE cards stays ONLY for mobile (the
  // `!isMobile` gate); this desktop strip uses its OWN drag wiring above.
  if (!isMobile) {
    // The static bottom-center dock style (the prior LANE D placement) - reused
    // for the "bottom" mode AND as the fallback when "bbox" mode has no AOI rect.
    const bottomDockStyle: React.CSSProperties = {
      position: "fixed",
      // Lift the strip above the active scrubber footprint so the scrubber (z51)
      // does not overlay the legend (z15).
      bottom: scrubberActive
        ? DESKTOP_DOCK_BOTTOM_PX + DESKTOP_DOCK_SCRUBBER_CLEARANCE_PX
        : DESKTOP_DOCK_BOTTOM_PX,
      // Center in the gutter between the left rail and the right chat.
      left: `calc(50% + ${Math.round((dockLeftInsetPx - dockRightInsetPx) / 2)}px)`,
      transform: "translateX(-50%)",
    };

    // Resolve the strip position by mode + drag state.
    let posStyle: React.CSSProperties;
    if (desktopDragPos) {
      // MID-DRAG: free top-left (already viewport-clamped in onDesktopDragMove).
      posStyle = {
        position: "fixed",
        left: desktopDragPos.left,
        top: desktopDragPos.top,
        transform: "none",
      };
    } else if (desktopDockMode === "bbox" && aoiRect) {
      // BBOX-ANCHORED (default): center the strip on the AOI bbox center X, just
      // BELOW the bbox bottom edge, clamped to the viewport so it never drifts
      // off-screen. translateX(-50%) centers it on that X. We clamp the center X
      // and the top against an estimated half-width / height so the clamp holds
      // even before the rendered width is known.
      const vw =
        typeof window !== "undefined" && Number.isFinite(window.innerWidth)
          ? window.innerWidth
          : 1024;
      const vh =
        typeof window !== "undefined" && Number.isFinite(window.innerHeight)
          ? window.innerHeight
          : 768;
      const m = DESKTOP_DOCK_VIEWPORT_MARGIN_PX;
      // Estimated strip footprint (fixed-metric desktop cards) for the clamp.
      const estWidth =
        keyModels.length * DESKTOP_DOCK_KEY_WIDTH +
        Math.max(0, keyModels.length - 1) * DESKTOP_DOCK_GAP_PX;
      // OUTSIDE-THE-BBOX PARITY (NATE 2026-06-29): the desktop strip is the SAME for
      // the colorbar and the categorical key, so the categorical must clamp exactly
      // like the colorbar. A categorical card now lays its chips out HORIZONTALLY
      // (a short, wrapping row - see DesktopLegendKey), NOT a tall column, so we no
      // longer reserve a TALL per-class height here (the old `64 + rows*18` estimate
      // pulled a tall land-cover strip UP and OVER the bbox - the reported bug). A
      // small flat reserve keeps the dock JUST BELOW the bbox bottom edge (outside
      // it) in the normal case, only clamping up when the bbox bottom is itself near
      // / past the viewport bottom (the same on-screen guard the colorbar uses).
      const estHeight = 64;
      const half = estWidth / 2;
      const centerX = Math.max(
        m + half,
        Math.min((aoiRect.left + aoiRect.right) / 2, vw - m - half),
      );
      const top = Math.max(
        m,
        Math.min(aoiRect.bottom + DESKTOP_DOCK_BBOX_GAP_PX, vh - m - estHeight),
      );
      posStyle = {
        position: "fixed",
        left: centerX,
        top,
        transform: "translateX(-50%)",
      };
    } else {
      // BOTTOM dock ("bottom" mode, OR "bbox" with no AOI rect -> never vanish).
      posStyle = bottomDockStyle;
    }

    // Rendered directly (NOT portaled) so it stays a DESCENDANT of the map
    // container (the job-0321 F43 contract: the legend lives inside grace2-map).
    return (
      <div
        data-testid="grace2-layer-legend"
        data-legend-docked="desktop"
        data-legend-dock-mode={
          desktopDockMode === "bbox" && aoiRect ? "bbox" : "bottom"
        }
        onPointerDown={startDesktopDrag}
        style={{
          ...posStyle,
          display: "flex",
          flexDirection: "row",
          flexWrap: "nowrap",
          gap: DESKTOP_DOCK_GAP_PX,
          maxWidth: "92vw",
          overflowX: "auto",
          pointerEvents: "auto",
          // The whole strip is the drag handle.
          cursor: "grab",
          touchAction: "none",
          userSelect: "none",
          // Below the chat (z=32) + panels (z=20); above the map.
          zIndex: LEGEND_Z_INDEX,
        }}
      >
        {keyModels.map((model) => (
          <DesktopLegendKey key={model.layerId} model={model} />
        ))}
      </div>
    );
  }

  // The wrapper keeps a stable testid so existing tests + Map.tsx mounting
  // expectations hold. It is a zero-size placeholder; the actual key cards
  // portal to document.body with position:fixed so they escape the map
  // container's stacking context and appear above the mobile chat panel
  // (item 6 fix: z-index 50 > drawer z-index 30-41).
  //
  // position:fixed keys use the SAME snapped coordinates as before because
  // the map container is position:absolute;inset:0 relative to the app shell
  // which is position:fixed;inset:0  -  so map-container coords == viewport coords.
  //
  // MOBILE ONE-ROW BAND DOCK (NATE 2026-06-27): hoist the per-key render so the
  // result can be EITHER portaled per-key (AOI snap / free-drag / legacy band) OR
  // wrapped in a SINGLE horizontal flex ROW container (the band dock). Each entry
  // is a per-key portal element in the non-band case, or the RAW card in the band
  // case (collected into the row below).
  const renderedKeys = keyModels.map((model, idx) => {
        const layerId = model.layerId;
        const preset = model.preset;
        const ui = uiState[layerId] ?? {};
        const width = widthFor(layerId);
        // `snapped` is built 1:1 from `keyModels`, so this is always defined;
        // the fallback satisfies noUncheckedIndexedAccess.
        const snapPos = snapped[idx] ?? { left: 0, top: 0, side: "bottom" as AoiSide };

        // Position priority:
        //   free (user-dragged) > MOBILE sheet-top band dock > AOI snap >
        //   fallback bottom-center.
        // Keys use position:fixed (portaled to document.body) so coords map
        // 1:1 to viewport space (map container is inset:0 -> same origin).
        //
        // MOBILE SHEET-TOP DOCK (NATE 2026-06-24): when the chat sheet's top edge
        // Y is known we SUPPRESS the AOI-side snap on mobile and dock the keys in
        // a clean horizontal band just ABOVE the sheet top (NATE wants them at the
        // chat-panel top, NOT railing the AOI edges) - mirroring the desktop
        // single-band dock. snapPos.left/top are bottom-center-relative offsets
        // (same as the fallback below), realized with left:50% + translate.
        let posStyle: React.CSSProperties;
        // MOBILE VIEWPORT CLAMP (NATE 2026-06-24)  -  extra style applied ONLY on the
        // mobile sheet-top band dock so the keys can never bleed past the window
        // edges (the live-mobile bug: the legend band "spans past the window"). We
        // cap max-width to the viewport (minus safe-area insets + a side margin) and
        // max-height to the band between the top safe-area and the docked sheet, and
        // let a card whose intrinsic content is wider/taller scroll WITHIN the clamp
        // instead of overflowing. left:50% + translateX(-50%) keeps it CENTERED, so
        // a clamped width never runs off either edge. Empty on every other path
        // (desktop early-returns above; AOI snap / free-drag are unchanged).
        let clampStyle: React.CSSProperties = {};
        if (bandDockActive && !ui.free) {
          // MOBILE ONE-ROW BAND DOCK (NATE 2026-06-27) - mobile-only. The keys live
          // in a SINGLE horizontal flex ROW (the bandRow container below), so each
          // card sits IN FLOW (position:relative) - the row container owns the
          // fixed dock placement (bottom = bandRowBottomPx, above the scrubber).
          // We force the compact HORIZONTAL card here (sideLabel/orientation are
          // pinned to bottom/horizontal in the render below when bandDockActive),
          // so the row reads as one clean line. No per-card absolute coords.
          // flexShrink:0 keeps each card's width in the flex row (the row scrolls
          // horizontally if the keys exceed its max-width).
          posStyle = { position: "relative", flexShrink: 0 };
          // BAND FIT (NATE 2026-06-28, ISSUE 2) - the band card sets width =
          // bandWidthPx (the full chat-panel width); WITHOUT border-box the 10px
          // horizontal padding + 1px border each side would ADD ~22px so the card
          // RENDERS WIDER than the row, pushing the MAX label off the right edge
          // (the live magma-bar clipping bug). border-box folds the padding+border
          // INTO bandWidthPx so the card content (incl. the max label) fits exactly
          // within the row's bandWidthPx. The maxWidth cap is the belt-and-braces
          // window guard. The inner value-row is already flex (bar:flex:1 minWidth:0
          // + nowrap labels), so the bar absorbs slack and the labels never clip.
          clampStyle = {
            maxWidth: MOBILE_LEGEND_MAX_WIDTH_CSS,
            boxSizing: "border-box",
          };
        } else if (ui.free) {
          posStyle = { left: ui.free.left, top: ui.free.top };
        } else if (mobileDockBottomPx != null && !aoiRect) {
          // SNAP-TO-BBOX FIX (NATE 2026-06-26)  -  gate the sheet-top band dock on
          // the ABSENCE of a real AOI rect. When the AOI bbox IS projected on
          // screen the band branch is SKIPPED and the aoiRect snap branch below
          // takes over on mobile too, so the keys rail along the REAL bbox edges
          // (left/right vertical, top horizontal) - identical to desktop. The
          // band stays ONLY as the AOI-less fallback so the keys still clear the
          // composer when no bbox is projected. (Bug: the band's left:50% +
          // translate transform corrupted the snapped positions, landing the keys
          // in a bottom-center band instead of on the bbox edge.)
          //
          // Band dock: use the bottom-center-RELATIVE stack (not `snapPos`, which
          // is absolute when an AOI rect is present) so left:50% + translate puts
          // the keys in a clean horizontal band just above the chat sheet top.
          const band = mobileBandStack[idx] ?? { left: -width / 2, top: 0 };
          posStyle = {
            left: "50%",
            bottom: mobileDockBottomPx,
            transform: `translate(calc(-50% + ${band.left + width / 2}px), ${band.top}px)`,
          };
          // VIEWPORT CLAMP: max-width to the window (overrides the fixed `width` so
          // it SHRINKS on a narrow phone) + max-height to the docked band, both
          // scrollable so nothing spills off-screen. Each colorbar key inside the
          // card is already flex (bar:flex:1,minWidth:0 + nowrap labels), so the
          // content reflows within the clamp; overflow is the safety net.
          clampStyle = {
            maxWidth: MOBILE_LEGEND_MAX_WIDTH_CSS,
            maxHeight: mobileLegendMaxHeightCss(mobileDockBottomPx),
            overflowX: "auto",
            overflowY: "auto",
            boxSizing: "border-box",
          };
        } else if (aoiRect) {
          posStyle = { left: snapPos.left, top: snapPos.top };
        } else {
          // Fallback: snapPos.left/top are offsets from bottom-center; realize
          // them with left:50% + a translate so the row sits bottom-center.
          posStyle = {
            left: "50%",
            bottom: 24,
            transform: `translate(calc(-50% + ${snapPos.left + width / 2}px), ${snapPos.top}px)`,
          };
        }

        // DATA-DRIVEN LEGEND (the colormap KEY from the data) - when the layer
        // carries a resolved legend it is the SOURCE OF TRUTH (overriding both the
        // URL-rescale and the style_preset): the title/bounds/unit come straight
        // from the producer-emitted LegendKey. A categorical legend renders class
        // swatches instead of a bar (handled in the value-row render below).
        // FRAME-TRUTH (NATE 2026-06-19) is the next fallback: the parsed-from-URL
        // colormap/rescale match what the map paints; the style_preset is last.
        const data = model.data ?? null;
        const isCategorical = data?.kind === "categorical";
        const minLabel = data?.kind === "continuous"
          ? (data.min ?? "")
          : model.rescale ? model.rescale.min : preset.minValue;
        const maxLabel = data?.kind === "continuous"
          ? (data.max ?? "")
          : model.rescale ? model.rescale.max : preset.maxValue;
        // The preset unit is meaningful only for the preset's own scale; when
        // the bounds come from the URL rescale (an arbitrary layer), drop the
        // unit so we never mislabel (e.g. tagging a temperature ramp with "m").
        // A data-driven legend carries its OWN units (or none) - use them verbatim.
        const unitLabel = data ? data.unit : model.rescale ? "" : preset.unit;
        // The card title: the legend's label/layer-name when data-driven, else the
        // preset label.
        const titleLabel = data ? data.title : preset.label;
        // SIDE-SNAP / ITEM 5  -  the side label MUST match the snapped layout: it
        // reads from the SAME resolvedSides array (the user's drag-snap override
        // when present, else the CCW index side incl. the scrubber-active start
        // offset). So a key dragged to the right reads as a vertical RIGHT bar.
        // AOI-less fallback stays bottom-horizontal.
        // MOBILE ONE-ROW BAND DOCK (NATE 2026-06-27): in the band dock the keys are
        // a single horizontal ROW (chat -> scrubber -> legend), so EVERY key reads
        // as a bottom/horizontal compact card regardless of any AOI snap, so the
        // row is one clean line.
        const sideLabel: AoiSide = bandDockActive
          ? "bottom"
          : aoiRect
            ? resolvedSides[idx] ?? sideForIndex(idx + sideStartOffset)
            : "bottom";

        // Item g (ORIENTATION, NATE 2026-06-20)  -  the colorbar is VERTICAL (a
        // tall bar) when the key docks on the LEFT or RIGHT side of the AOI, and
        // HORIZONTAL when it docks on TOP or BOTTOM (and in the AOI-less
        // bottom-center fallback). The gradient direction follows: bottom->top
        // for vertical (min at the bottom, max at the top), left->right for
        // horizontal (min at the left, max at the right).
        const orientation: "vertical" | "horizontal" =
          sideLabel === "left" || sideLabel === "right" ? "vertical" : "horizontal";
        // NATE 2026-06-22 (item 2): a VERTICAL key renders as a tall, NARROW bar.
        // The horizontal card needs the full AOI-sized `width` (min .. bar .. max
        // laid out in a row); a vertical card stacks max/bar/min in a column, so
        // it only needs a slim fixed width (scaled). Snapping is untouched.
        const cardWidth = bandDockActive
          ? // BAND FORM (NATE 2026-06-28): the band key fills the SCRUBBER WIDTH so
            // the docked band reads as ONE clean bar in line with the scrubber, and
            // does NOT change scale with the AOI bbox (we deliberately do NOT apply
            // the aoiScaleFactor `scale` here). The single common-case key IS the
            // row; with multiple keys each stays scrubber-width and the row scrolls
            // horizontally (overflowX:auto on the band-row container).
            bandWidthPx
          : // CATEGORICAL keys (NLCD land cover) render swatch+label ROWS, never a
            // gradient rail. They get a NARROW fixed width (NATE 2026-06-29) snug to
            // the swatch+label content - NOT the wide AOI-sized colorbar width that
            // left a big empty gutter to the right. Longer labels ellipsize. This
            // matches the placement `width` in `sizes` so the snap never drifts.
            isCategorical
            ? Math.round(CATEGORICAL_KEY_WIDTH * scale)
            : // A non-categorical vertical (left/right) key is the slim gradient bar.
              orientation === "vertical"
              ? Math.round(VERTICAL_KEY_WIDTH * scale)
              : width;
        // DATA-DRIVEN LEGEND: continuous data-driven stops win over the
        // URL-parsed colormap and the preset; categorical keys have no gradient
        // (they render swatches) so we keep the preset stops only as a harmless
        // default the categorical branch never paints.
        const stops =
          data?.kind === "continuous" && data.stops
            ? data.stops
            : model.colormapStops ?? preset.stops;
        const gradient =
          orientation === "vertical"
            ? `linear-gradient(to top, ${stops
                .map((s) => `${s.color} ${(s.position * 100).toFixed(2)}%`)
                .join(", ")})`
            : buildGradient(stops);

        // Item d  -  scaled type + chrome metrics (clamped via the scale factor).
        // LEGEND v2: a single FLAT key (no compact branch), so one metric set.
        const titleFont = Math.round(11 * scale);
        const labelFont = Math.round(10 * scale);
        const barThickness = Math.round(12 * scale);
        // A vertical bar needs a sensible height to read as a tall colorbar.
        const verticalBarHeight = Math.round(120 * scale);

        // LANE D unit fix: NON-BREAKING SPACE ( ) between value + unit so
        // "(m)" can never wrap to a new line; the bar (flex:1) absorbs slack.
        const minText = `${minLabel}${unitLabel ? ` ${unitLabel}` : ""}`;
        const maxText = `${maxLabel}${unitLabel ? ` ${unitLabel}` : ""}`;

        const keyCard = (
          <div
            key={layerId}
            data-testid="grace2-layer-legend-key"
            data-legend-side={sideLabel}
            data-legend-orientation={orientation}
            onPointerDown={(e) => startDrag(layerId, e)}
            style={{
              position: "fixed",
              ...posStyle,
              width: cardWidth,
              // MOBILE VIEWPORT CLAMP  -  applied AFTER width so max-width/height cap
              // the fixed card to the window on the mobile band dock (empty {} on
              // every other path). Nothing here bleeds past the window edges.
              ...clampStyle,
              padding: "7px 10px 8px",
              background: "rgba(17,18,23,0.78)",
              backdropFilter: "blur(6px)",
              WebkitBackdropFilter: "blur(6px)",
              border: "1px solid rgba(255,255,255,0.06)",
              borderRadius: 10,
              boxShadow: "0 2px 12px rgba(0,0,0,0.45)",
              fontFamily: "system-ui, sans-serif",
              color: "#eee",
              pointerEvents: "auto",
              cursor: "grab",
              userSelect: "none",
              touchAction: "none",
              // Item a  -  BELOW the chat (z=32) + Layers/Cases panels (z=20) +
              // hamburgers (z=30); above the map. (Was z=50, which painted OVER
              // the chat + layers  -  the reported bug.)
              zIndex: LEGEND_Z_INDEX,
            }}
          >
            {/* LEGEND v2 - ROW 1 (HORIZONTAL only): title + hide(X). On a VERTICAL
                key the title is rotated to read vertically and the X moves to the
                BOTTOM of the column (item 3 + item 4), so the top title row is
                skipped here for vertical. DATA-DRIVEN LEGEND: a CATEGORICAL key
                renders its OWN title above the swatch list, so skip this row too. */}
            {orientation !== "vertical" && !isCategorical ? (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  marginBottom: 5,
                  gap: 6,
                  // minWidth:0 lets the title span shrink below its content width in
                  // this flex row so its ellipsis actually engages (a flex item
                  // defaults to min-content width, which would let nowrap text
                  // OVERFLOW instead of truncate). Required for the BAND form (NATE
                  // 2026-06-28: the title must be on ONE line, truncated, never
                  // wrapping); harmless for the horizontal edge form.
                  minWidth: 0,
                }}
              >
                <span
                  data-testid="layer-legend-title"
                  style={{
                    fontSize: titleFont,
                    fontWeight: 600,
                    letterSpacing: "0.03em",
                    color: "#ddd",
                    // ONE-LINE TRUNCATION (NATE 2026-06-28): never wrap the title to a
                    // new line; truncate with an ellipsis. minWidth:0 + flex:1 lets the
                    // span shrink within the (scrubber-width) card so the ellipsis
                    // engages instead of the text overflowing or new-lining.
                    flex: 1,
                    minWidth: 0,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {titleLabel}
                </span>
                <LegendControls idx={idx} onHide={() => setHidden(true)} />
              </div>
            ) : null}

            {/* LEGEND v2 - ROW 2: the gradient bar flanked by the min/max values.
                HORIZONTAL (top/bottom dock): [min] bar [max] in a row, min at the
                LEFT end of the bar, max at the RIGHT. VERTICAL (left/right dock):
                the same rotates - max at the TOP, min at the BOTTOM of a tall
                vertical bar. The bar grows to fill so the values flank its ends.

                ITEM 3 (NATE 2026-06-23): on a VERTICAL key the title used to
                ellipsize to "Ma..." in the cramped slim card. Instead we ROTATE
                the title to read VERTICALLY (writing-mode: vertical-rl) so the
                FULL label is legible alongside the bar - NO truncation.
                ITEM 4: the close (X) sits at the BOTTOM of the column, inline
                with the colorbar (below the min label), not at the top.

                DATA-DRIVEN LEGEND: a CATEGORICAL key renders a column of class
                swatches (color chip + label) instead of a gradient bar, with its
                own title on top + the hide(X) at the bottom (orientation-agnostic). */}
            {isCategorical ? (
              <div
                data-testid="layer-legend-value-row"
                style={{
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "stretch",
                  gap: 3,
                  marginTop: 2,
                }}
              >
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: 6,
                    marginBottom: 2,
                  }}
                >
                  <span
                    data-testid="layer-legend-title"
                    style={{
                      fontSize: titleFont,
                      fontWeight: 600,
                      letterSpacing: "0.03em",
                      color: "#ddd",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {titleLabel}
                  </span>
                  <LegendControls idx={idx} onHide={() => setHidden(true)} />
                </div>
                {/* EDGE-AWARE LAYOUT (NATE 2026-06-29): the class swatch+label chips
                    flow the SAME way the colorbar bar flows - a horizontal ROW (that
                    wraps) when the key is docked HORIZONTAL (top/bottom), a vertical
                    COLUMN when docked VERTICAL (left/right). So a categorical key and a
                    continuous key on the SAME edge are siblings (same anchor +
                    orientation), differing only in content. */}
                <div
                  data-testid="layer-legend-class-list"
                  style={{
                    display: "flex",
                    flexDirection: orientation === "vertical" ? "column" : "row",
                    flexWrap: orientation === "vertical" ? "nowrap" : "wrap",
                    alignItems: orientation === "vertical" ? "stretch" : "center",
                    gap: orientation === "vertical" ? 3 : 8,
                  }}
                >
                  {(data?.classes ?? []).map((cls, ci) => (
                    <div
                      key={`${layerId}-class-${ci}`}
                      data-testid="layer-legend-class"
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 6,
                        minWidth: 0,
                      }}
                    >
                      <span
                        data-testid="layer-legend-swatch"
                        style={{
                          width: barThickness,
                          height: barThickness,
                          borderRadius: 3,
                          background: cls.color,
                          border: "1px solid rgba(255,255,255,0.20)",
                          flexShrink: 0,
                        }}
                      />
                      <span
                        style={{
                          fontSize: labelFont,
                          color: "#cfd4db",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {cls.label}
                        {unitLabel ? ` ${unitLabel}` : ""}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            ) : orientation === "vertical" ? (
              <div
                data-testid="layer-legend-value-row"
                style={{
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  gap: 4,
                  marginTop: 2,
                }}
              >
                <span
                  data-testid="layer-legend-title"
                  style={{
                    fontSize: titleFont,
                    fontWeight: 600,
                    letterSpacing: "0.03em",
                    color: "#ddd",
                    // ITEM 3: rotate to read vertically (writing-mode:
                    // vertical-rl reads naturally top->bottom). The FULL label
                    // shows - no ellipsis/nowrap truncation. Cap the rotated run
                    // to the bar height so a very long label wraps onto a second
                    // vertical column (vertical-rl flows columns right->left)
                    // rather than pushing the card past the stacking-height math.
                    writingMode: "vertical-rl",
                    maxHeight: verticalBarHeight,
                    overflowWrap: "anywhere",
                  }}
                >
                  {titleLabel}
                </span>
                <span
                  data-testid="layer-legend-max-label"
                  style={{ fontSize: labelFont, color: "#bbb" }}
                >
                  {maxText}
                </span>
                <div
                  data-testid="layer-legend-bar"
                  style={{
                    width: barThickness,
                    height: verticalBarHeight,
                    borderRadius: 3,
                    background: gradient,
                    border: "1px solid rgba(255,255,255,0.12)",
                    flexShrink: 0,
                  }}
                />
                <span
                  data-testid="layer-legend-min-label"
                  style={{ fontSize: labelFont, color: "#bbb" }}
                >
                  {minText}
                </span>
                {/* ITEM 4 - the X lives at the BOTTOM, inline with the bar. */}
                <LegendControls idx={idx} onHide={() => setHidden(true)} />
              </div>
            ) : (
              <div
                data-testid="layer-legend-value-row"
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  marginTop: 2,
                }}
              >
                <span
                  data-testid="layer-legend-min-label"
                  style={{
                    fontSize: labelFont,
                    color: "#bbb",
                    flexShrink: 0,
                    whiteSpace: "nowrap",
                  }}
                >
                  {minText}
                </span>
                <div
                  data-testid="layer-legend-bar"
                  style={{
                    flex: 1,
                    minWidth: 0,
                    height: barThickness,
                    borderRadius: 3,
                    background: gradient,
                    border: "1px solid rgba(255,255,255,0.12)",
                  }}
                />
                <span
                  data-testid="layer-legend-max-label"
                  style={{
                    fontSize: labelFont,
                    color: "#bbb",
                    flexShrink: 0,
                    whiteSpace: "nowrap",
                  }}
                >
                  {maxText}
                </span>
              </div>
            )}

            {/* Resize handle (bottom-right corner). LEGEND v2: a plain hit-target
                (no diagonal grip glyph - NATE asked to drop the visual clutter);
                the card body itself is the drag handle. */}
            <div
              data-legend-no-drag=""
              data-testid="layer-legend-resize"
              onPointerDown={(e) => startResize(layerId, e)}
              style={{
                position: "absolute",
                right: 2,
                bottom: 2,
                width: 12,
                height: 12,
                cursor: "ew-resize",
                borderBottomRightRadius: 8,
              }}
              aria-label="Resize legend key"
            />
          </div>
        );

        // The raw card. In the MOBILE ONE-ROW BAND DOCK the cards live IN FLOW
        // inside the single horizontal ROW container (the map result is wrapped in
        // it below), so we return the RAW card. Otherwise (AOI snap / free-drag /
        // legacy sheet-top band) each card portals to document.body so it escapes
        // the map container's stacking context and renders above the mobile drawer.
        return bandDockActive
          ? keyCard
          : createPortal(keyCard, document.body, `legend-key-${layerId}`);
      });

  // The wrapper keeps a stable testid so existing tests + Map.tsx mounting
  // expectations hold. It is a zero-size placeholder; the actual key cards either
  // portal individually (AOI snap / free-drag / legacy band) OR sit inside the
  // single horizontal band-row container (the mobile one-row dock), both rendered
  // here as children of the anchor (the portals escape to document.body anyway).
  return (
    <div
      data-testid="grace2-layer-legend"
      style={{
        position: "absolute",
        inset: 0,
        pointerEvents: "none",
        // Zero z-index on the anchor wrapper  -  the actual keys are portaled.
        zIndex: 0,
      }}
    >
      {/* MOBILE ONE-ROW BAND DOCK (NATE 2026-06-27) - mobile-only. When the band
          dock is active the per-key render returned RAW cards; wrap them in a
          SINGLE horizontal flex ROW, portaled to document.body and docked just
          ABOVE the scrubber (bottom = bandRowBottomPx, so the bottom-to-top order
          is chat -> scrubber -> legend). One clean line: flexDirection row, nowrap,
          a small gap, horizontal scroll if the keys exceed ~92vw. The row owns the
          viewport clamp (max-width + scroll) so nothing bleeds past the window
          edges. Otherwise the renderedKeys are already per-key portals. Desktop
          never reaches here (it early-returns above). */}
      {bandDockActive && bandRowBottomPx != null
        ? createPortal(
            <div
              data-testid="grace2-layer-legend-band-row"
              style={{
                position: "fixed",
                left: "50%",
                bottom: bandRowBottomPx,
                transform: "translateX(-50%)",
                display: "flex",
                flexDirection: "row",
                flexWrap: "nowrap",
                alignItems: "flex-end",
                gap: MOBILE_BAND_KEY_GAP_PX,
                // BAND FORM WIDTH (NATE 2026-06-28): the row is the SAME WIDTH AS
                // THE SCRUBBER (scrubberMobileWidthPx) so the single common-case key
                // fills it as one clean bar and the band never rescales with the AOI
                // bbox. With multiple keys the row STAYS scrubber-width and scrolls
                // horizontally (overflowX:auto). The width is already viewport-
                // clamped by the scrubber math; we keep maxWidth as a belt-and-braces
                // cap so a very narrow phone still never spans past the window.
                width: bandWidthPx,
                // VIEWPORT CLAMP: never wider than the window; scroll the row
                // horizontally if the keys exceed the clamp. The row is one short
                // line so height is rarely at risk, but cap max-height to the band
                // above the dock so a tall key can never run off the top of the
                // window (respecting the notch via env()).
                maxWidth: MOBILE_LEGEND_MAX_WIDTH_CSS,
                maxHeight: mobileLegendMaxHeightCss(bandRowBottomPx),
                overflowX: "auto",
                overflowY: "visible",
                boxSizing: "border-box",
                pointerEvents: "auto",
                // Below the chat (z=32) + Layers/Cases panels (z=20); above the map.
                zIndex: LEGEND_Z_INDEX,
              }}
            >
              {renderedKeys}
            </div>,
            document.body,
            "legend-band-row",
          )
        : renderedKeys}

      {/* LEGEND v2 (DROP-ZONE SIGNALS) - while a key is being dragged, paint a
          thin "area signal" along each VALID snap target (left/right/top edges of
          the AOI; never bottom) and HIGHLIGHT the one nearest the dragged card.
          Portaled to document.body (position:fixed) like the keys so they overlay
          the map at the AOI edges. Cleared automatically on release/cancel. */}
      {dropSignals.map((sig) =>
        createPortal(
          <div
            key={`legend-dropzone-${sig.side}`}
            data-testid="layer-legend-dropzone"
            data-legend-dropzone-side={sig.side}
            data-legend-dropzone-active={sig.active ? "1" : "0"}
            aria-hidden="true"
            style={{
              position: "fixed",
              left: sig.rect.left,
              top: sig.rect.top,
              width: Math.max(0, sig.rect.right - sig.rect.left),
              height: Math.max(0, sig.rect.bottom - sig.rect.top),
              borderRadius: 3,
              background: sig.active
                ? "rgba(74,163,255,0.85)"
                : "rgba(74,163,255,0.28)",
              boxShadow: sig.active ? "0 0 8px rgba(74,163,255,0.7)" : "none",
              pointerEvents: "none",
              transition: "background 80ms linear",
              zIndex: LEGEND_Z_INDEX,
            }}
          />,
          document.body,
          `legend-dropzone-${sig.side}`,
        ),
      )}
    </div>
  );
}

/**
 * LANE D (desktop dock) - a single STATIC legend key card for the bottom-center
 * desktop strip. Fixed metrics (no AOI scaling), horizontal only, no drag /
 * resize / hide control. The value+unit use a NON-BREAKING SPACE so "(m)" never
 * wraps to a new line (NATE: "the legend goes to a new line for (m) ... it should
 * shrink the gradient or just extend the bar"); the gradient BAR is the flex:1
 * element that absorbs slack, and the labels are flexShrink:0 nowrap, so the bar
 * shrinks before the unit can ever wrap.
 */
function DesktopLegendKey({ model }: { model: LegendKeyModel }): JSX.Element {
  const preset = model.preset;
  // DATA-DRIVEN LEGEND (the colormap KEY from the data) - when the layer carries a
  // resolved legend it is the SOURCE OF TRUTH (title/bounds/unit/stops, or a
  // categorical swatch list), overriding the URL-rescale + preset path. FRAME-TRUTH
  // (URL rescale/colormap) is the next fallback; the style_preset is last.
  const data = model.data ?? null;
  const isCategorical = data?.kind === "categorical";
  const minLabel = data?.kind === "continuous"
    ? (data.min ?? "")
    : model.rescale ? model.rescale.min : preset.minValue;
  const maxLabel = data?.kind === "continuous"
    ? (data.max ?? "")
    : model.rescale ? model.rescale.max : preset.maxValue;
  // The preset unit is meaningful only for the preset's own scale; a URL rescale
  // is an arbitrary layer, so drop the unit there (never mislabel). A data-driven
  // legend carries its own units verbatim.
  const unitLabel = data ? data.unit : model.rescale ? "" : preset.unit;
  const titleLabel = data ? data.title : preset.label;
  const stops =
    data?.kind === "continuous" && data.stops
      ? data.stops
      : model.colormapStops ?? preset.stops;
  const gradient = buildGradient(stops);
  //   = NON-BREAKING SPACE: keeps the value and its unit on ONE line.
  const minText = `${minLabel}${unitLabel ? ` ${unitLabel}` : ""}`;
  const maxText = `${maxLabel}${unitLabel ? ` ${unitLabel}` : ""}`;
  return (
    <div
      data-testid="grace2-layer-legend-key"
      data-legend-orientation="horizontal"
      data-legend-side="bottom"
      style={{
        // CATEGORICAL (NLCD land cover) shrinks to its swatch+label content so the
        // card is NARROW with no empty gutter (NATE 2026-06-29); a typical label
        // fits, longer ones ellipsize at the cap. A CONTINUOUS key keeps the fixed
        // width so its [min] bar [max] colorbar row has room to flex.
        ...(isCategorical
          ? { width: "fit-content", maxWidth: DESKTOP_DOCK_KEY_WIDTH }
          : { width: DESKTOP_DOCK_KEY_WIDTH }),
        flex: "0 0 auto",
        padding: "7px 10px 8px",
        background: "rgba(17,18,23,0.85)",
        border: "1px solid rgba(255,255,255,0.06)",
        borderRadius: 10,
        boxShadow: "0 2px 12px rgba(0,0,0,0.45)",
        fontFamily: "system-ui, sans-serif",
        color: "#eee",
      }}
    >
      <div
        data-testid="layer-legend-title"
        style={{
          fontSize: DESKTOP_DOCK_TITLE_FONT,
          fontWeight: 600,
          letterSpacing: "0.03em",
          color: "#ddd",
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          marginBottom: 5,
        }}
      >
        {titleLabel}
      </div>
      {/* DATA-DRIVEN LEGEND: a CATEGORICAL key lists class swatches; a continuous
          key keeps the [min] bar [max] colorbar row. */}
      {isCategorical ? (
        <div
          data-testid="layer-legend-value-row"
          style={{
            // EDGE-AWARE LAYOUT (NATE 2026-06-29): the desktop strip docks BELOW the
            // bbox (a horizontal edge), so the categorical chips flow as a horizontal
            // ROW that WRAPS - mirroring how the colorbar lays out horizontally on the
            // bottom edge - instead of a tall vertical column. Kept narrow
            // (fit-content/maxWidth on the card), so chips wrap within the cap.
            display: "flex",
            flexDirection: "row",
            flexWrap: "wrap",
            alignItems: "center",
            gap: 8,
          }}
        >
          {(data?.classes ?? []).map((cls, ci) => (
            <div
              key={`desktop-class-${ci}`}
              data-testid="layer-legend-class"
              style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}
            >
              <span
                data-testid="layer-legend-swatch"
                style={{
                  width: DESKTOP_DOCK_BAR_THICKNESS,
                  height: DESKTOP_DOCK_BAR_THICKNESS,
                  borderRadius: 3,
                  background: cls.color,
                  border: "1px solid rgba(255,255,255,0.20)",
                  flexShrink: 0,
                }}
              />
              <span
                style={{
                  fontSize: DESKTOP_DOCK_LABEL_FONT,
                  color: "#cfd4db",
                  whiteSpace: "nowrap",
                }}
              >
                {cls.label}
                {unitLabel ? ` ${unitLabel}` : ""}
              </span>
            </div>
          ))}
        </div>
      ) : (
      <div
        data-testid="layer-legend-value-row"
        style={{ display: "flex", alignItems: "center", gap: 6 }}
      >
        <span
          data-testid="layer-legend-min-label"
          style={{
            fontSize: DESKTOP_DOCK_LABEL_FONT,
            color: "#bbb",
            flexShrink: 0,
            whiteSpace: "nowrap",
          }}
        >
          {minText}
        </span>
        <div
          data-testid="layer-legend-bar"
          style={{
            // The bar yields width so the labels (flexShrink:0) never wrap.
            flex: 1,
            minWidth: 0,
            height: DESKTOP_DOCK_BAR_THICKNESS,
            borderRadius: 3,
            background: gradient,
            border: "1px solid rgba(255,255,255,0.12)",
          }}
        />
        <span
          data-testid="layer-legend-max-label"
          style={{
            fontSize: DESKTOP_DOCK_LABEL_FONT,
            color: "#bbb",
            flexShrink: 0,
            whiteSpace: "nowrap",
          }}
        >
          {maxText}
        </span>
      </div>
      )}
    </div>
  );
}

/** LEGEND v2 - per-key control cluster: just the HIDE (eye) button. The compact
 * collapse/expand toggle is GONE (the key is always a flat two-row card). Only
 * the FIRST key carries the global hide control, to avoid clutter. Tagged
 * `data-legend-no-drag` so a click on it does not initiate a card drag. */
function LegendControls({
  idx,
  onHide,
}: {
  idx: number;
  onHide: () => void;
}): JSX.Element | null {
  // Only the first key carries the hide control; the rest render no controls.
  if (idx !== 0) return null;
  return (
    <span
      data-legend-no-drag=""
      style={{ display: "inline-flex", alignItems: "center", gap: 4, flexShrink: 0 }}
    >
      <button
        type="button"
        data-testid="layer-legend-hide"
        data-legend-no-drag=""
        onClick={onHide}
        title="Hide legend"
        aria-label="Hide legend"
        style={controlBtnStyle}
      >
        {/* NATE 2026-06-22 (item 1): the hide affordance is an X glyph (was an
            eye). Same click behavior + aria-label; the X reads as "dismiss the
            legend" instead of the ambiguous eye. Shared icon (no raw unicode). */}
        <IconClose size={12} />
      </button>
    </span>
  );
}

const controlBtnStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  width: 18,
  height: 16,
  lineHeight: "14px",
  padding: 0,
  fontSize: 11,
  fontWeight: 700,
  color: "#bbb",
  background: "rgba(255,255,255,0.06)",
  border: "1px solid rgba(255,255,255,0.10)",
  borderRadius: 4,
  cursor: "pointer",
};

/**
 * Item b (NATE 2026-06-20)  -  the MOBILE legend show/hide control, rendered
 * INSIDE the expanded Layers section (the LayerPanel) instead of floating over
 * the chat composer. It is a plain inline row (no portal), so it sits in the
 * panel's normal flow, out of the way. The legend's own floating pill is
 * suppressed on mobile (`suppressShowPill`), so this is the ONLY show/hide
 * affordance there.
 *
 * Pure controlled component: the parent owns the `hidden` boolean (App threads
 * the same value into LayerLegend's `hidden` prop). Render it only when there is
 * legend content to toggle (`legendHasContent(layers)`).
 */
export function MobileLegendToggle({
  hidden,
  onToggle,
}: {
  hidden: boolean;
  onToggle: (hidden: boolean) => void;
}): JSX.Element {
  return (
    <button
      type="button"
      data-testid="grace2-mobile-legend-toggle"
      aria-pressed={!hidden}
      onClick={() => onToggle(!hidden)}
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 8,
        width: "100%",
        padding: "8px 10px",
        background: "rgba(255,255,255,0.04)",
        border: "1px solid rgba(255,255,255,0.08)",
        borderRadius: 8,
        color: "#cfd4db",
        fontFamily: "system-ui, sans-serif",
        fontSize: 12,
        fontWeight: 600,
        cursor: "pointer",
      }}
    >
      <span>{hidden ? "Show legend" : "Hide legend"}</span>
      <span
        aria-hidden="true"
        style={{
          fontSize: 11,
          color: hidden ? "#8a929e" : "#4aa3ff",
          fontWeight: 700,
        }}
      >
        {hidden ? "OFF" : "ON"}
      </span>
    </button>
  );
}
