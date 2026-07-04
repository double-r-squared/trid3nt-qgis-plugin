// GRACE-2 web - deck.gl ROUTING predicate (deck.gl SPIKE, #169).
//
// LAZY-LOAD SPLIT: this module is the PURE, deck.gl-FREE half of the spike. It
// holds only the routing decision (shouldRouteToDeck + its helpers + thresholds)
// and the DeckRoutableLayer shape - NO import of `@deck.gl/*`. Map.tsx imports it
// STATICALLY (the per-pass reconcile must decide routing synchronously) WITHOUT
// pulling the ~211 KB gz deck.gl bundle into the main app chunk.
//
// The actual deck.gl GeoJsonLayer builder + color/override resolution live in
// `deck_layers.ts`, which DOES import `@deck.gl/layers` and is reached ONLY via a
// dynamic `await import("./lib/deck_layers")` the first time a deck-routed layer
// appears. Keeping NOTHING static in the app import deck_layers.ts is what lets
// Rollup split deck.gl into a separate async chunk.

import type { LayerViewOverride } from "./layer_cache";

// Re-export so existing consumers (and tests) can keep importing the override
// type from the deck modules; this is a pure type re-export (erased).
export type { LayerViewOverride };

/**
 * The minimal shape the deck routing + builder reads off a wire/loaded layer.
 * Structurally compatible with WireLayerSummary / ProjectLayerSummary so Map.tsx
 * can pass either without a cast dance.
 */
export interface DeckRoutableLayer {
  layer_id: string;
  name?: string;
  layer_type?: string;
  style_preset?: string | null;
  /** Inline GeoJSON FeatureCollection (the addVectorLayer fast-path input). */
  inline_geojson?: unknown;
  /** Present => the agent published a tiled (MVT/PMTiles) source; NOT deck-routed
   *  here - vector-tile layers stay on the MapLibre registerVectorTileLayer path. */
  vector_tile_url?: string;
}

/**
 * The feature-count threshold above which an inline-GeoJSON vector is heavy
 * enough to warrant the deck.gl GPU path. ~1500 features is where MapLibre's
 * main-thread fill/outline triangulation + the per-feature hit-test start to
 * cost visibly on mid-range hardware; below it the MapLibre path is fine (and
 * keeps the spike blast-radius small). Matched to the kickoff's "> ~1500"
 * guidance.
 */
export const DECK_FEATURE_COUNT_THRESHOLD = 1500;

/**
 * True when a layer NAME or style_preset signals building footprints. Used as the
 * SECOND half of the routing predicate (the first being the feature-count
 * threshold) so a small-but-clearly-footprint layer also takes the deck path, and
 * so the intent is legible regardless of count. Lowercased substring match on the
 * usual footprint vocabulary (MS Buildings, OSM building footprints, NSI, etc.).
 */
export function isFootprintLayer(layer: DeckRoutableLayer): boolean {
  const hay = `${layer.style_preset ?? ""} ${layer.name ?? ""}`.toLowerCase();
  return (
    hay.includes("building") ||
    hay.includes("footprint") ||
    hay.includes("structures") ||
    hay.includes("structure_footprint")
  );
}

/**
 * THE ROUTING PREDICATE. An inline-GeoJSON vector layer is routed to deck.gl
 * when it is HEAVY (feature count > DECK_FEATURE_COUNT_THRESHOLD) OR it is a
 * FOOTPRINT layer by name/preset. Everything else - light vectors, vector-tile
 * layers (which carry vector_tile_url and are handled by MapLibre's tiled path),
 * rasters - stays on the existing MapLibre paths.
 *
 * Defensive: a layer with no inline_geojson, or a tiled layer, is NEVER routed
 * here (the inline FeatureCollection is the only input this path consumes).
 * Returns false on a non-FeatureCollection so a malformed payload falls back to
 * the MapLibre path's own error handling rather than silently disappearing.
 */
export function shouldRouteToDeck(layer: DeckRoutableLayer): boolean {
  // Tiled vectors are MapLibre's job (registerVectorTileLayer) - never deck here.
  if (
    typeof layer.vector_tile_url === "string" &&
    layer.vector_tile_url.length > 0
  ) {
    return false;
  }
  // Only inline-GeoJSON vectors are deck-routable in the spike.
  if (layer.inline_geojson == null) return false;

  // Footprint by name/preset routes regardless of count (intent-first).
  if (isFootprintLayer(layer)) {
    // Still require a parseable FeatureCollection so we don't route junk.
    return featureCount(layer.inline_geojson) !== null;
  }

  // Otherwise route only when heavy.
  const count = featureCount(layer.inline_geojson);
  return count !== null && count > DECK_FEATURE_COUNT_THRESHOLD;
}

/**
 * Feature count of an inline_geojson value, or null when it is not a
 * FeatureCollection (so callers can distinguish "0 features" from "not parseable").
 */
export function featureCount(inline: unknown): number | null {
  if (
    !inline ||
    typeof inline !== "object" ||
    (inline as { type?: string }).type !== "FeatureCollection" ||
    !Array.isArray((inline as { features?: unknown }).features)
  ) {
    return null;
  }
  return (inline as { features: unknown[] }).features.length;
}
