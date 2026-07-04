// GRACE-2 web - deck.gl interleaved-overlay layer builder (deck.gl SPIKE, #169).
//
// PURPOSE
//   Heavy vector layers (building footprints, dense polygon sets) painted as a
//   MapLibre GeoJSON source choke the main thread: every vertex becomes a
//   triangulated fill/outline in the MapLibre style, and the per-feature
//   queryRenderedFeatures hit-test walks them all. deck.gl's GeoJsonLayer is
//   GPU-instanced and far cheaper for thousands of polygons, so the spike routes
//   ONLY the heavy/footprint vectors through an INTERLEAVED deck.gl
//   MapboxOverlay while every other layer (raster, vector-tile, light vector)
//   stays on the existing MapLibre paths. The basemap and all controls remain on
//   MapLibre untouched.
//
// SCOPE (kept deliberately tight - this is a spike):
//   - This module is PURE (no React, no MapLibre, no deck.gl side effects beyond
//     constructing a GeoJsonLayer instance). Map.tsx owns the MapboxOverlay
//     lifecycle (construct, addControl, setProps). Picking + draw-suppression are
//     wired in Map.tsx; this module only declares the layer + its props.
//   - Outline-only polygons (NATE wants the OUTLINE, not filled dots/circles):
//     stroked:true, a faint fill (so the polygon is still pickable across its
//     interior, mirroring MESH_FILL_OPACITY's "clickable but see-through"
//     rationale), getLineWidth small, pointType "circle" but tiny - no big vertex
//     dots.
//   - The same inline_geojson + style_preset the addVectorLayer path consumes,
//     plus the layer_cache {opacity, visible, zIndex} overrides, drive the deck
//     layer props so a footprint layer honors LayerPanel edits exactly like a
//     MapLibre vector layer would.
//
// Invariant 1 (determinism boundary): every coordinate rendered comes from the
// fetched/inlined GeoJSON; we never compute new geographic numbers - we hand the
// FeatureCollection to deck.gl verbatim.

// LAZY-LOAD NOTE (deck.gl SPIKE, #169): this module imports `@deck.gl/layers`
// normally - that is INTENTIONAL. It is the deck.gl-DEPENDENT half of the spike
// and is reached ONLY via a dynamic `await import("./deck_layers")` in Map.tsx
// (the first time a deck-routed layer appears), so Rollup emits deck.gl as a
// SEPARATE async chunk instead of folding it into the main app bundle. The PURE
// routing predicate (shouldRouteToDeck + helpers) lives in deck_routing.ts and is
// imported STATICALLY by Map.tsx; we re-export it here for back-compat / tests.
import { GeoJsonLayer } from "@deck.gl/layers";
import type { FeatureCollection } from "geojson";
import {
  resolveVectorColor,
  vectorResultFromInlineGeoJson,
  type VectorGeomKind,
} from "./vector_rendering";
import type { LayerViewOverride } from "./layer_cache";
import type { DeckRoutableLayer } from "./deck_routing";

// Re-export the pure routing surface so existing importers (and the unit tests)
// can keep importing them from `deck_layers`; the canonical home is deck_routing.
export {
  DECK_FEATURE_COUNT_THRESHOLD,
  isFootprintLayer,
  shouldRouteToDeck,
  featureCount,
  type DeckRoutableLayer,
} from "./deck_routing";

/** Faint deck.gl polygon fill alpha (0-255). Mirrors MESH_FILL_OPACITY's intent:
 *  > 0 so the polygon INTERIOR stays pickable (deck picks the filled area, not
 *  just the 1px outline), but barely visible so the OUTLINE reads as the layer.
 *  ~0.06 * 255 ~= 15. */
export const DECK_POLYGON_FILL_ALPHA = 15;

/** deck.gl outline width, in pixels (widthUnits "pixels"). Thin so footprints
 *  read as crisp outlines rather than fat strokes. */
export const DECK_LINE_WIDTH_PX = 1;

/** Parse a "#rrggbb" hex color to a deck.gl [r,g,b] triple (0-255). */
export function hexToRgb(hex: string): [number, number, number] {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return [112, 128, 144]; // slate fallback (#708090).
  const n = parseInt(m[1]!, 16);
  return [(n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff];
}

/** The props a built deck GeoJsonLayer carries, surfaced for unit assertions
 *  (deck.gl's own `.props` is read-only at runtime but typed loosely). */
export interface BuiltDeckLayerProps {
  id: string;
  data: FeatureCollection;
  visible: boolean;
  opacity: number;
  stroked: boolean;
  filled: boolean;
  pickable: boolean;
  lineColor: [number, number, number];
  fillColor: [number, number, number, number];
  lineWidthPx: number;
  /** Effective z used for the MapLibre beforeId ordering decision in Map.tsx. */
  zIndex: number;
}

/**
 * Resolve the effective deck-layer props from a routable layer + its layer_cache
 * override. Pure + fully unit-testable (no deck.gl construction), so the routing
 * + override + color logic is verifiable in happy-dom (which cannot run WebGL).
 *
 *   - opacity: override.opacity wins, else 1.
 *   - visible: override.visible wins, else true.
 *   - zIndex: override.zIndex wins, else 0 (Map.tsx feeds the wire z_index in
 *     when no override exists; this module only knows about the override).
 *   - colors: resolveVectorColor (preset > geometry-family) -> outline color; the
 *     same hue at DECK_POLYGON_FILL_ALPHA for the faint interior fill.
 */
export function resolveDeckLayerProps(
  layer: DeckRoutableLayer,
  override: LayerViewOverride | undefined,
): BuiltDeckLayerProps {
  const parsed = vectorResultFromInlineGeoJson(layer.inline_geojson);
  const fc = parsed.featureCollection;
  const geomKind: VectorGeomKind = parsed.geomKind;

  const opacity =
    override?.opacity !== undefined && Number.isFinite(override.opacity)
      ? clamp01(override.opacity)
      : 1;
  const visible = override?.visible !== undefined ? override.visible : true;
  const zIndex =
    override?.zIndex !== undefined && Number.isFinite(override.zIndex)
      ? override.zIndex
      : 0;

  const [r, g, b] = hexToRgb(
    resolveVectorColor(layer.layer_id, layer.style_preset, geomKind),
  );

  return {
    id: layer.layer_id,
    data: fc,
    visible,
    opacity,
    stroked: true,
    filled: true, // faint fill (alpha below) keeps the interior pickable.
    pickable: true,
    lineColor: [r, g, b],
    fillColor: [r, g, b, DECK_POLYGON_FILL_ALPHA],
    lineWidthPx: DECK_LINE_WIDTH_PX,
    zIndex,
  };
}

/**
 * Build a deck.gl GeoJsonLayer for a heavy/footprint inline-GeoJSON layer.
 *
 * OUTLINE-ONLY styling (NATE): polygons render as crisp outlines with a barely-
 * there fill (so the interior is still pickable for the popup bridge), and points
 * render as small "circle" markers (NOT the giant default dots) so a footprint
 * centroid layer does not blanket the map in blobs.
 *
 * `pickable` is forced false when `suppressPicking` is true (a draw / pick /
 * region-choice request is in flight) so deck does not swallow those interactions
 * - Map.tsx passes the live flag.
 *
 * `onClick` (when provided) receives deck's pick info; Map.tsx adapts it into the
 * same FeaturePopup payload the MapLibre click path builds, so clicking a
 * footprint opens the popup.
 */
export function buildDeckGeoJsonLayer(
  layer: DeckRoutableLayer,
  override: LayerViewOverride | undefined,
  opts: {
    suppressPicking?: boolean;
    onClick?: (info: unknown) => void;
  } = {},
): GeoJsonLayer {
  const p = resolveDeckLayerProps(layer, override);
  const pickable = opts.suppressPicking ? false : p.pickable;
  return new GeoJsonLayer({
    id: p.id,
    data: p.data,
    pickable,
    // Outline-only polygons.
    stroked: p.stroked,
    filled: p.filled,
    getFillColor: p.fillColor,
    getLineColor: p.lineColor,
    lineWidthUnits: "pixels",
    getLineWidth: p.lineWidthPx,
    lineWidthMinPixels: p.lineWidthPx,
    // Points (if any): small circle markers, never big vertex dots.
    pointType: "circle",
    getPointRadius: 2,
    pointRadiusUnits: "pixels",
    pointRadiusMinPixels: 2,
    // Per-layer opacity + visibility from the layer_cache override.
    opacity: p.opacity,
    visible: p.visible,
    onClick: opts.onClick,
    // Cheap, footprint-friendly: no auto-highlight (the MapLibre highlight source
    // owns the selected-feature outline in the spike).
    autoHighlight: false,
  });
}

function clamp01(x: number): number {
  if (Number.isNaN(x)) return 0;
  if (x < 0) return 0;
  if (x > 1) return 1;
  return x;
}
