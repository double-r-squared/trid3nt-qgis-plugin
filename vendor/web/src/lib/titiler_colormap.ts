// GRACE-2 web — TiTiler colormap + rescale parsing (legend FRAME-truth fix).
//
// NATE 2026-06-19: the LayerLegend used to derive its gradient + numeric bounds
// purely from the layer's `style_preset` (a GUESS). For AWS frame layers the map
// actually paints from a TiTiler XYZ tile TEMPLATE whose URL already embeds the
// TRUTH as query params, e.g.:
//   https://.../cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=...&rescale=0,3.5&colormap_name=blues
// This module parses that `rescale` (-> numeric min/max) and `colormap_name`
// (-> CSS gradient stops) out of a tile URL so the legend can render the SAME
// colors + bounds the map paints. When the URL carries no such params (e.g. a
// QGIS WMS overlay or a non-animated single raster) the parse returns nulls and
// the caller falls back to the existing style_preset behavior — this NEVER
// throws and NEVER breaks the preset path.
//
// Pure (Invariant 1): every value is read verbatim from the URL the agent
// produced; nothing is computed. No new dependency — the colormap stops are
// baked here from the matplotlib / colorbrewer ramps TiTiler uses, covering the
// `colormap_name` set the agent actually emits (see publish_layer.py
// _TITILER_STYLE_REGISTRY). Unknown names map to null so the caller falls back.

import type { GradientStop } from "./style-presets";

/** The numeric bounds parsed from a TiTiler `rescale=lo,hi` param. */
export interface ParsedRescale {
  min: number;
  max: number;
}

/** rescale + colormap parsed out of a single TiTiler tile-template URL. */
export interface ParsedTitilerStyle {
  /** Numeric min/max from `rescale=lo,hi`, or null when absent / malformed. */
  rescale: ParsedRescale | null;
  /** The raw `colormap_name` value (lowercased), or null when absent. */
  colormapName: string | null;
}

// --------------------------------------------------------------------------- //
// Colormap registry: TiTiler `colormap_name` -> CSS gradient stops.
// --------------------------------------------------------------------------- //
//
// The stops mirror each ramp's canonical anchor colors (matplotlib for the
// perceptual maps, ColorBrewer for the named sequential/diverging maps), sampled
// at evenly spaced positions across [0, 1]. They are intentionally coarse (5-7
// stops) — the CSS `linear-gradient` interpolates between them, which visually
// matches TiTiler's continuous ramp closely enough for a legend key.
//
// Covers the EXACT colormap_name set the agent emits today
// (publish_layer.py _TITILER_STYLE_REGISTRY + the family/precip fallbacks):
//   ylgnbu, reds, blues, rdylbu_r, viridis, rdbu, ylgn, gray, gray_r.
// A `_r` suffix means the ramp is REVERSED (TiTiler convention) — we store the
// base ramp once and reverse its stops programmatically (see getColormapStops).
//
// Add a ramp here only when the agent starts emitting a new colormap_name;
// anything not listed falls back to the style_preset gradient.
const COLORMAP_STOPS: Record<string, readonly string[]> = {
  // matplotlib viridis (perceptual, dark-purple -> yellow).
  viridis: ["#440154", "#414487", "#2a788e", "#22a884", "#7ad151", "#fde725"],
  // ColorBrewer Blues (light -> dark blue).
  blues: ["#f7fbff", "#deebf7", "#c6dbef", "#9ecae1", "#6baed6", "#3182bd", "#08519c"],
  // ColorBrewer Reds (light -> dark red).
  reds: ["#fff5f0", "#fee0d2", "#fcbba1", "#fc9272", "#fb6a4a", "#de2d26", "#a50f15"],
  // ColorBrewer YlGnBu (yellow -> green -> blue).
  ylgnbu: ["#ffffd9", "#edf8b1", "#c7e9b4", "#7fcdbb", "#41b6c4", "#1d91c0", "#225ea8"],
  // ColorBrewer YlGn (yellow -> green).
  ylgn: ["#ffffe5", "#f7fcb9", "#d9f0a3", "#addd8e", "#78c679", "#41ab5d", "#238443"],
  // ColorBrewer RdBu (red -> white -> blue), diverging.
  rdbu: ["#b2182b", "#ef8a62", "#fddbc7", "#f7f7f7", "#d1e5f0", "#67a9cf", "#2166ac"],
  // ColorBrewer RdYlBu (red -> yellow -> blue), diverging.
  rdylbu: ["#d73027", "#fc8d59", "#fee090", "#ffffbf", "#e0f3f8", "#91bfdb", "#4575b4"],
  // Grayscale (black -> white).
  gray: ["#000000", "#404040", "#808080", "#bfbfbf", "#ffffff"],
  // ColorBrewer GnBu (light green -> blue) - wave height / water depth ramps.
  gnbu: ["#f7fcf0", "#e0f3db", "#ccebc5", "#a8ddb5", "#7bccc4", "#4eb3d3", "#2b8cbe", "#08589e"],
  // ColorBrewer Greens (light -> dark green).
  greens: ["#f7fcf5", "#e5f5e0", "#c7e9c0", "#a1d99b", "#74c476", "#41ab5d", "#238b45", "#005a32"],
  // matplotlib magma (perceptual, black -> purple -> white) - seismic PGA etc.
  magma: ["#000004", "#231151", "#5f187f", "#982d80", "#d3436e", "#f8765c", "#febb81", "#fcfdbf"],
  // ColorBrewer RdYlGn (red -> yellow -> green), diverging (its _r reverse covers
  // green -> red, the common damage / suitability direction).
  rdylgn: ["#a50026", "#d73027", "#f46d43", "#fdae61", "#fee08b", "#d9ef8b", "#a6d96a", "#66bd63", "#1a9850", "#006837"],
  // ColorBrewer YlOrRd (yellow -> orange -> red) - slope angle, heat, drought.
  ylorrd: ["#ffffcc", "#ffeda0", "#fed976", "#feb24c", "#fd8d3c", "#fc4e2a", "#e31a1c", "#bd0026", "#800026"],
};

/**
 * Returns the CSS gradient stops for a TiTiler `colormap_name`, or null when the
 * name is unknown (so the caller falls back to the style_preset gradient).
 *
 * Handles the `_r` reverse-suffix convention: `blues_r` reverses `blues`,
 * `gray_r` reverses `gray`, `rdylbu_r` reverses `rdylbu`, etc. Positions are
 * spread evenly across [0, 1].
 */
export function getColormapStops(
  colormapName: string | null | undefined,
): GradientStop[] | null {
  if (typeof colormapName !== "string" || colormapName.length === 0) return null;
  const name = colormapName.toLowerCase();
  const reversed = name.endsWith("_r");
  const base = reversed ? name.slice(0, -2) : name;
  const colors = COLORMAP_STOPS[base];
  if (!colors || colors.length === 0) return null;
  const ordered = reversed ? [...colors].reverse() : colors;
  const last = ordered.length - 1;
  return ordered.map((color, i) => ({
    position: last === 0 ? 0 : i / last,
    color,
  }));
}

/** True when the colormap_name resolves to a known ramp (reverse-suffix aware). */
export function isKnownColormap(colormapName: string | null | undefined): boolean {
  return getColormapStops(colormapName) !== null;
}

/**
 * DATA-DRIVEN LEGEND (the colormap KEY from the data) - resolve a `LegendKey.colormap`
 * field to CSS gradient stops. The producer emits the colormap as EITHER:
 *   - a NAMED ramp (string, e.g. "reds"/"viridis") -> resolved via COLORMAP_STOPS,
 *     reverse-suffix (`_r`) aware (same path as `getColormapStops`); OR
 *   - EXPLICIT stops as `[[stop_0to1, "#rrggbb"], ...]` -> used verbatim, sorted by
 *     position, clamped into [0, 1].
 *
 * Returns null when the input is null/empty, an unknown ramp name, or an explicit
 * array with no valid stops, so the caller falls back to the style_preset gradient.
 * NEVER throws.
 *
 * NOTE: this resolves only the COLORS (the ramp). The numeric range is the
 * LegendKey's `vmin`/`vmax` (the real data range), kept separate by design so the
 * colormap choice and the value range are independent (the NATE principle).
 */
export function resolveLegendColormapStops(
  colormap: string | ReadonlyArray<readonly [number, string]> | null | undefined,
): GradientStop[] | null {
  if (colormap == null) return null;
  // Named ramp: reuse the reverse-suffix-aware resolver.
  if (typeof colormap === "string") return getColormapStops(colormap);
  // Explicit stops: [[stop_0to1, "#rrggbb"], ...]. Coerce + validate each pair,
  // drop malformed entries, sort by position, clamp positions into [0, 1].
  if (!Array.isArray(colormap)) return null;
  const stops: GradientStop[] = [];
  for (const pair of colormap) {
    if (!Array.isArray(pair) || pair.length < 2) continue;
    const position = Number(pair[0]);
    const color = pair[1];
    if (!Number.isFinite(position) || typeof color !== "string" || color.length === 0) {
      continue;
    }
    stops.push({ position: Math.min(1, Math.max(0, position)), color });
  }
  if (stops.length === 0) return null;
  stops.sort((a, b) => a.position - b.position);
  return stops;
}

/**
 * Parses a TiTiler `rescale=lo,hi` param value into numeric { min, max }.
 * Returns null when the value is absent, malformed, non-finite, or degenerate
 * (min >= max) — the caller then keeps the preset bounds.
 */
function parseRescaleValue(raw: string | null): ParsedRescale | null {
  if (!raw) return null;
  // TiTiler accepts "lo,hi"; a tile URL may percent-encode the comma (%2C).
  const decoded = raw.replace(/%2C/gi, ",");
  const parts = decoded.split(",");
  if (parts.length !== 2) return null;
  const min = Number(parts[0]);
  const max = Number(parts[1]);
  if (!Number.isFinite(min) || !Number.isFinite(max)) return null;
  if (max <= min) return null;
  return { min, max };
}

/**
 * Parses `rescale` + `colormap_name` out of a TiTiler tile-template URL.
 *
 * Defensive by design: tries the standard `URL`/`URLSearchParams` first (handles
 * percent-encoding + ordering), then falls back to a regex when the URL is not
 * absolute/parseable (e.g. a `{z}/{x}/{y}` template with curly-brace path
 * segments that some `URL` impls reject). Always returns an object with possibly
 * null fields; NEVER throws.
 */
export function parseTitilerTileStyle(
  url: string | null | undefined,
): ParsedTitilerStyle {
  const empty: ParsedTitilerStyle = { rescale: null, colormapName: null };
  if (typeof url !== "string" || url.length === 0) return empty;

  let rescaleRaw: string | null = null;
  let colormapRaw: string | null = null;

  // Preferred path: parse the query string with URLSearchParams. We slice the
  // query ourselves (after the first '?') so curly-brace path segments in an
  // XYZ template can't trip the absolute-URL `URL` constructor.
  const qIndex = url.indexOf("?");
  if (qIndex >= 0) {
    try {
      const params = new URLSearchParams(url.slice(qIndex + 1));
      rescaleRaw = params.get("rescale");
      colormapRaw = params.get("colormap_name");
    } catch {
      // fall through to the regex path
    }
  }

  // Regex fallback for either param the searchparams path missed (or no '?').
  if (rescaleRaw == null) {
    const m = /[?&]rescale=([^&#]*)/i.exec(url);
    if (m && m[1] != null) rescaleRaw = m[1];
  }
  if (colormapRaw == null) {
    const m = /[?&]colormap_name=([^&#]*)/i.exec(url);
    if (m && m[1] != null) colormapRaw = m[1];
  }

  return {
    rescale: parseRescaleValue(rescaleRaw),
    colormapName:
      typeof colormapRaw === "string" && colormapRaw.length > 0
        ? colormapRaw.toLowerCase()
        : null,
  };
}
