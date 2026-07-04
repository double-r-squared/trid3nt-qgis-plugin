// GRACE-2 web — client-side style-preset registry (job-0065).
//
// A preset entry holds everything the LayerLegend component needs to render
// a matplotlib-style horizontal colorbar: the CSS gradient stops, the value
// range, the human-readable label for the title, and the unit label used for
// the axis ticks.
//
// Stops mirror the QML color ramp in `styles/continuous_flood_depth.qml`
// (job-0062). The QML uses a singlebandpseudocolor renderer with an
// INTERPOLATED colorrampshader; we bake the same 9 stops here as CSS rgba()
// values. Alpha values are normalised from the 0-255 QML alpha to 0-1.
//
// If a layer carries an unknown `style_preset`, the LayerLegend hides rather
// than rendering a guess. Add presets here as the engine specialist adds new
// QML style files.

export interface GradientStop {
  /** Position in the [0, 1] range along the bar (0 = left/min, 1 = right/max). */
  position: number;
  /** CSS colour string (rgba is preferred so alpha transparency works). */
  color: string;
}

export interface StylePreset {
  /** Human-readable title shown above the colorbar. */
  label: string;
  /** Physical minimum value (left end of the bar). */
  minValue: number;
  /** Physical maximum value (right end of the bar). */
  maxValue: number;
  /** Unit string appended to the tick labels (e.g. "m", "°C"). */
  unit: string;
  /** Colour stops for the CSS linear-gradient. Sorted by position ascending. */
  stops: GradientStop[];
}

// ---------------------------------------------------------------------------
// Registry
// ---------------------------------------------------------------------------

/**
 * Registry of known style presets keyed by the `style_preset` string the
 * agent places on `ProjectLayerSummary`. The web side is a passive consumer:
 * it never invents a preset; it only renders what's registered here.
 */
export const STYLE_PRESETS: Record<string, StylePreset> = {
  /**
   * Continuous flood depth (Blues ramp, 0–3.5 m).
   *
   * Mirrors `styles/continuous_flood_depth.qml` item stops exactly:
   *   0.00 m → #f7fbff (alpha 0)     — dry / near-transparent
   *   0.05 m → #deebf7 (alpha ~0.78)
   *   0.50 m → #c6dbef (alpha ~0.86)
   *   1.00 m → #9ecae1 (alpha ~0.90)
   *   1.50 m → #6baed6 (alpha ~0.94)
   *   2.00 m → #4292c6 (alpha ~0.96)
   *   2.50 m → #2171b5 (alpha ~0.98)
   *   3.00 m → #08519c (alpha 1.00)
   *   3.50 m → #08306b (alpha 1.00)
   *
   * The legend bar intentionally starts from the 0.05 m stop (near-white) so
   * the left edge shows a visible colour even though 0 m is transparent in
   * the raster (dry cells). This gives the user an accurate sense of the
   * colour that ANY wet pixel will display.
   */
  continuous_flood_depth: {
    label: "Max flood depth (m)",
    minValue: 0,
    maxValue: 3.5,
    unit: "m",
    stops: [
      { position: 0 / 3.5, color: "rgba(247,251,255,0)" },
      { position: 0.05 / 3.5, color: "rgba(222,235,247,0.78)" },
      { position: 0.5 / 3.5, color: "rgba(198,219,239,0.86)" },
      { position: 1.0 / 3.5, color: "rgba(158,202,225,0.90)" },
      { position: 1.5 / 3.5, color: "rgba(107,174,214,0.94)" },
      { position: 2.0 / 3.5, color: "rgba(66,146,198,0.96)" },
      { position: 2.5 / 3.5, color: "rgba(33,113,181,0.98)" },
      { position: 3.0 / 3.5, color: "rgba(8,81,156,1)" },
      { position: 3.5 / 3.5, color: "rgba(8,48,107,1)" },
    ],
  },
};

/**
 * Returns the preset for the given name, or undefined if unknown.
 * Callers should hide the legend when this returns undefined.
 */
export function getStylePreset(name: string): StylePreset | undefined {
  return STYLE_PRESETS[name];
}
