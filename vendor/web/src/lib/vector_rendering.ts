// GRACE-2 web — vector layer rendering helpers (job-0139 + job-0146).
//
// Resolves OQ-PAY-MAP-VECTOR-UNSUPPORTED surfaced by the Playwright UI capture
// agent (2026-06-08): Map.tsx prior to this job only handled raster WMS layers.
// Vector layers added to `loaded_layers` (GBIF points, WDPA polygons, NWS alerts,
// OSM roads, MTBS burn perimeters, FIRMS active fire, eBird, IUCN ranges,
// Movebank tracks — 12+ Wave 1/1.5/2 fetchers all return `layer_type='vector'`)
// showed in `LayerPanel` but never rendered on the map.
//
// job-0146 adds:
//   - 12-colour curated palette replacing the FNV-1a generic colours (Part 1)
//   - Expanded style_preset registry with curated per-preset colours (Part 1)
//   - ds_mean choropleth expression builder for Pelicun damage layers (Part 2)
//   - Polygon fill opacity constant (0.4) for basemap-label readability (Part 3)
//
// This module is responsible for the data-fetch + style-derivation seam ONLY.
// MapLibre source/layer registration stays inside Map.tsx (per file-ownership
// boundary in the job-0139 kickoff §scope).
//
// Invariants preserved:
//   - 1. Determinism boundary: every coordinate/value rendered comes from the
//     fetched GeoJSON; we never compute new geographic numbers.
//   - 4. Rendering through QGIS Server: vector layers are agent-served
//     FlatGeobuf / GeoJSON URIs (NOT GCS direct reads); the web client
//     consumes pre-served bytes only.
//   - 5. Tier separation: the client never reaches `gs://` URLs directly —
//     vector URIs MUST be https://-style URLs handed off by the agent.
//
// Architecture note: per the job-0139 kickoff §2, we keep v0.1 simple by
// fetching FlatGeobuf via the npm `flatgeobuf` package + converting to GeoJSON
// in-browser. A future enhancement would stream-deserialize via the package's
// AsyncGenerator, but for v0.1 (Case 1 demo headline = single panther / spoonbill
// / alligator collection sized in the low-thousands of features), in-memory
// collection + a single addSource is the right altitude.

import type { FeatureCollection, Feature, Geometry } from "geojson";
import { deserialize } from "flatgeobuf/lib/mjs/geojson.js";
import type { LegendKey } from "../contracts";
import { resolveLegendColormapStops } from "./titiler_colormap";

/** Geometry families MapLibre paints distinctly. */
export type VectorGeomKind = "point" | "line" | "polygon" | "unknown";

/** Result of `fetchVectorAsGeoJson`. */
export interface VectorFetchResult {
  featureCollection: FeatureCollection;
  geomKind: VectorGeomKind;
}

/**
 * Classify the geometry of the first non-null feature in a FeatureCollection.
 * MapLibre needs the geometry family up-front to pick the layer type
 * (circle / line / fill). Multi* variants collapse to their base kind.
 */
export function detectGeomKind(fc: FeatureCollection): VectorGeomKind {
  for (const f of fc.features) {
    const g: Geometry | null = f.geometry as Geometry | null;
    if (!g) continue;
    switch (g.type) {
      case "Point":
      case "MultiPoint":
        return "point";
      case "LineString":
      case "MultiLineString":
        return "line";
      case "Polygon":
      case "MultiPolygon":
        return "polygon";
      // GeometryCollection: inspect first sub-geometry.
      case "GeometryCollection": {
        const sub = g.geometries[0];
        if (sub) {
          switch (sub.type) {
            case "Point":
            case "MultiPoint":
              return "point";
            case "LineString":
            case "MultiLineString":
              return "line";
            case "Polygon":
            case "MultiPolygon":
              return "polygon";
          }
        }
        return "unknown";
      }
      default:
        return "unknown";
    }
  }
  return "unknown";
}

/**
 * Build a `VectorFetchResult` from an already-materialized GeoJSON
 * FeatureCollection (job-0175). Used by the inline-GeoJSON code path:
 * when the agent embeds the parsed FeatureCollection on the
 * `ProjectLayerSummary.inline_geojson` field, the client skips the URI
 * fetch entirely and just classifies geometry.
 *
 * Why this exists as a sibling to `fetchVectorAsGeoJson` rather than
 * folding into it: the fetch path is async (network); this is purely
 * synchronous validation. Keeping them apart keeps the type and call
 * sites unambiguous in `Map.tsx`.
 *
 * Throws when the input is not a FeatureCollection — the caller logs and
 * skips.
 */
export function vectorResultFromInlineGeoJson(
  inline: unknown,
): VectorFetchResult {
  if (
    !inline ||
    typeof inline !== "object" ||
    (inline as { type?: string }).type !== "FeatureCollection" ||
    !Array.isArray((inline as { features?: unknown }).features)
  ) {
    throw new Error("[vector_rendering] inline_geojson is not a FeatureCollection");
  }
  const fc = inline as FeatureCollection;
  return { featureCollection: fc, geomKind: detectGeomKind(fc) };
}

/**
 * Fetch a vector layer URI and return it as a GeoJSON FeatureCollection plus
 * its geometry kind. Supports:
 *   - .fgb (FlatGeobuf): parsed via the `flatgeobuf` npm package.
 *   - .geojson / .json: fetched + JSON-parsed directly.
 *
 * `uri` MUST be an https://-style URL — the client never fetches `gs://`
 * directly (Invariant 5; the agent rewrites GCS pointers to served URLs).
 *
 * Throws on:
 *   - non-2xx HTTP status
 *   - malformed FlatGeobuf bytes (the underlying parser raises)
 *   - GeoJSON that does not parse to a FeatureCollection
 *
 * The caller is expected to catch + log per-layer failures so one bad layer
 * does not break the entire map.
 */
export async function fetchVectorAsGeoJson(
  uri: string,
  fetchImpl: typeof fetch = fetch,
): Promise<VectorFetchResult> {
  if (uri.startsWith("gs://")) {
    // Invariant 5 guardrail. The agent should never hand us a `gs://` URL;
    // surface loudly rather than silently fail.
    throw new Error(
      `[vector_rendering] refusing to fetch gs:// URL from client (invariant 5): ${uri}`,
    );
  }

  const isFgb = /\.fgb(\?|$)/i.test(uri);

  if (isFgb) {
    // FlatGeobuf path: fetch bytes, parse via the flatgeobuf package, collect
    // features into a FeatureCollection. For v0.1 we materialise the full
    // collection (kickoff §scope: "convert to GeoJSON using the flatgeobuf
    // npm package"); streaming render is a future enhancement.
    const resp = await fetchImpl(uri);
    if (!resp.ok) {
      throw new Error(
        `[vector_rendering] fetch FlatGeobuf failed: ${resp.status} ${resp.statusText} (${uri})`,
      );
    }
    const buf = await resp.arrayBuffer();
    const typedArray = new Uint8Array(buf);
    const features: Feature[] = [];
    // deserialize returns an AsyncGenerator<IGeoJsonFeature> — collect.
    for await (const feat of deserialize(typedArray)) {
      features.push(feat as Feature);
    }
    const fc: FeatureCollection = { type: "FeatureCollection", features };
    return { featureCollection: fc, geomKind: detectGeomKind(fc) };
  }

  // GeoJSON path.
  const resp = await fetchImpl(uri);
  if (!resp.ok) {
    throw new Error(
      `[vector_rendering] fetch GeoJSON failed: ${resp.status} ${resp.statusText} (${uri})`,
    );
  }
  const data = (await resp.json()) as unknown;
  if (
    !data ||
    typeof data !== "object" ||
    (data as { type?: string }).type !== "FeatureCollection" ||
    !Array.isArray((data as { features?: unknown }).features)
  ) {
    throw new Error(
      `[vector_rendering] not a FeatureCollection: ${uri}`,
    );
  }
  const fc = data as FeatureCollection;
  return { featureCollection: fc, geomKind: detectGeomKind(fc) };
}

// ---------------------------------------------------------------------------
// Style derivation (job-0146 — curated palette + preset registry)
// ---------------------------------------------------------------------------

/**
 * Curated 12-colour categorical palette for vector layers without a style_preset.
 * Designed for (job-0146 Part 1):
 *   - High contrast against CartoDB DarkMatter dark basemap
 *   - Color-blind friendliness (avoids problematic red/green pairs as sole
 *     distinguishers; uses hue + lightness variance)
 *   - Distinctiveness when 6+ species layers are stacked simultaneously
 *
 * Palette rationale (by slot):
 *   0  #FF7F0E  orange         — large mammals (panther, bear)
 *   1  #00BFFF  bright cyan    — birds (spoonbill, roseate, wading)
 *   2  #ADFF2F  lime green     — reptiles (alligator, sea turtle)
 *   3  #40E0D0  aqua/turquoise — marine species
 *   4  #FF1493  deep pink      — plants / flora
 *   5  #708090  slate grey     — admin boundaries (WDPA, census)
 *   6  #FF4444  fire red       — fire data (MTBS, FIRMS fallback)
 *   7  #4477FF  sky blue       — flood / hydrological data
 *   8  #FFD700  gold           — roads / infrastructure
 *   9  #DA70D6  orchid         — generic fallback 1
 *  10  #98FF98  pale green     — generic fallback 2
 *  11  #FFA07A  light salmon   — generic fallback 3
 *
 * The palette is exported for tests; callers use `paletteColorFor(layerId)`.
 */
export const VECTOR_PALETTE: readonly string[] = [
  "#FF7F0E", // orange        — large mammals
  "#00BFFF", // bright cyan   — birds
  "#ADFF2F", // lime green    — reptiles
  "#40E0D0", // aqua          — marine
  "#FF1493", // deep pink     — plants
  "#708090", // slate grey    — admin/boundaries
  "#FF4444", // fire red      — fire data
  "#4477FF", // sky blue      — flood/hydro
  "#FFD700", // gold          — roads/infra
  "#DA70D6", // orchid        — generic 1
  "#98FF98", // pale green    — generic 2
  "#FFA07A", // light salmon  — generic 3
];

/**
 * Deterministic palette colour for a given layer_id. Uses a simple
 * 32-bit FNV-1a hash so the same layer_id always gets the same colour
 * across reloads (cheap-and-stable: cryptographic hashing is overkill,
 * and we want determinism more than collision-resistance).
 */
export function paletteColorFor(layerId: string): string {
  // FNV-1a 32-bit
  let h = 0x811c9dc5;
  for (let i = 0; i < layerId.length; i++) {
    h ^= layerId.charCodeAt(i);
    // multiply by FNV prime mod 2^32
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return VECTOR_PALETTE[h % VECTOR_PALETTE.length] ?? VECTOR_PALETTE[0]!;
}

/**
 * Map a vector layer's `style_preset` (if any) to a primary colour. Returns
 * `undefined` when no preset is known, signalling the caller to fall back to
 * `paletteColorFor`. Mirrors `style-presets.ts` but operates on vector layer
 * colours (the raster preset uses gradient stops; vectors only need a single
 * primary colour for circle/line/fill paint).
 *
 * Curated preset registry (job-0146 Part 1):
 *   - 'gbif_occurrences'        → orange  #FF7F0E
 *   - 'inaturalist_observations' → bright cyan #00BFFF
 *   - 'wdpa_protected_areas', 'wdpa_polygon', 'wdpa', 'protected_area'
 *                               → slate grey #708090
 *   - 'nws_alerts', 'nws_alert', 'nws_warning', 'alert'
 *                               → fire red #FF4444
 *   - 'mtbs_burn_severity', 'burn_perimeter', 'mtbs'
 *                               → fire red #FF4444
 *   - 'firms_active_fire', 'firms', 'active_fire'
 *                               → bright red #FF4444
 *   - 'osm_roads', 'osm_road', 'roads'
 *                               → gold/muted yellow #FFD700
 *   - 'pelicun_damage'          → special: uses ds_mean choropleth expression
 *                                 (caller must invoke buildDsMeanExpression)
 *
 * Note: 'pelicun_damage' returns a sentinel string so the caller knows to
 * use the choropleth expression path (Part 2) rather than a flat colour.
 *
 * Extend as engine specialists land vector style presets.
 */
export const PELICUN_DAMAGE_PRESET = "pelicun_damage" as const;

export function presetColorFor(stylePreset: string | null | undefined): string | undefined {
  if (!stylePreset) return undefined;
  const key = stylePreset.toLowerCase();
  // Species / biodiversity fetchers
  if (key === "gbif_occurrences" || key.startsWith("gbif")) return "#FF7F0E";
  if (key === "inaturalist_observations" || key.startsWith("inat")) return "#00BFFF";
  // Protected areas / boundaries
  if (key.includes("wdpa") || key.includes("protected_area")) return "#708090";
  // Alerts
  if (key.includes("nws_alert") || key.includes("nws_warning") || key.includes("alert")) return "#FF4444";
  // Fire data
  if (key.includes("burn_perimeter") || key.includes("mtbs")) return "#FF4444";
  if (key.includes("firms") || key.includes("active_fire")) return "#FF4444";
  // Seismic fault traces (#207 input-layer surfacing): geologic fault lines in a
  // strong crimson so they read as the source structure feeding the PGA map,
  // distinct from the alert/fire red and the water blue.
  if (key.includes("fault")) return "#D7263D";
  // Roads / infrastructure
  if (key.includes("osm_road") || key === "roads" || key === "osm_roads") return "#FFD700";
  // Computational mesh wireframe (NATE #156): a cool cyan/grey scaffold colour
  // so the quad-cell lattice reads as scaffolding, distinct from rivers (blue)
  // and roads (amber). Must come BEFORE the water branch so 'mesh' never bleeds
  // into the hydro match.
  if (key.includes("mesh")) return "#5BC0DE";
  // Water / hydrography — rivers, streams, waterways, NHDPlus flowlines, contours.
  // Sky-blue so every water vector reads the same regardless of AOI (fixes the
  // yellow-vs-blue split where two rivers hashed to different palette slots).
  if (
    key.includes("waterway") ||
    key.includes("river") ||
    key.includes("stream") ||
    key.includes("nhdplus") ||
    key.includes("flowline") ||
    key.includes("hydro")
  ) {
    return "#4477FF";
  }
  // Pelicun damage: sentinel — caller must use choropleth expression
  if (key === PELICUN_DAMAGE_PRESET) return PELICUN_DAMAGE_PRESET;
  // MODFLOW Wave-4 PRT capture-zone and wellhead-protection polygon layers
  // (sprint-18 Wave-4). Violet protection-zone colour so these planning-level
  // envelopes read visually distinct from hydro-blue rivers and mesh-cyan grids.
  if (key === "capture_zone" || key === "wellhead_protection") return "#9B59B6";
  // MODFLOW Wave-5 saltwater-intrusion transect + toe point (sprint-18 Wave-5).
  // Teal #1ABC9C: distinct from capture-zone violet, hydro-blue rivers, mesh-cyan,
  // and roads-amber; reads as a coastal/saltwater boundary on the map.
  if (key === "saltwater_intrusion") return "#1ABC9C";
  return undefined;
}

/**
 * Fill opacity for polygon layers (Part 3). Reduced to 0.4 (from 0.5) so
 * basemap labels remain readable underneath polygon fill. The caller should
 * multiply this by the layer opacity setting.
 */
export const POLYGON_FILL_OPACITY = 0.4;

/**
 * Stroke width for polygon outline layers (Part 3). 1.5px so polygon edges
 * remain visible against the lower fill opacity.
 */
export const POLYGON_STROKE_WIDTH = 1.5;

/**
 * Default stroke width (px) for vector LINE layers. 2px matches the rivers /
 * roads convention so flowlines and infrastructure read as data.
 */
export const VECTOR_LINE_WIDTH = 2;

/**
 * Thinner stroke width (px) for computational-mesh wireframe lines (NATE #156).
 * The quad-cell lattice can be dense, so a hairline keeps it reading as
 * scaffolding rather than competing with the data layers painted on top.
 */
export const MESH_LINE_WIDTH = 0.6;

/**
 * Faint fill opacity (NATE #156) for computational-mesh POLYGON layers, so the
 * mesh reads as a WIREFRAME (you see the grid cells) instead of a solid cyan
 * blanket. A fill-opacity > 0 (rather than 0) keeps each cell CLICKABLE for the
 * feature popup; 0.06 is just enough tint that the basemap/AOI shows through.
 * The caller multiplies this by the layer opacity setting.
 */
export const MESH_FILL_OPACITY = 0.06;

/**
 * True when a layer's `style_preset` is a computational-mesh preset (NATE #156).
 * Mirrors the other mesh checks (lowercased + includes "mesh"); Map.tsx uses it
 * to gate mesh polygons to a faint fill + hairline outline (wireframe look).
 */
export function isMeshGridLayer(
  stylePreset: string | null | undefined,
): boolean {
  return Boolean(stylePreset && stylePreset.toLowerCase().includes("mesh"));
}

/**
 * Resolve the MapLibre `line-width` for a vector line layer keyed on its
 * `style_preset`. Mesh-grid presets get a hairline (MESH_LINE_WIDTH); every
 * other line keeps the default (VECTOR_LINE_WIDTH). Additive + deterministic:
 * unknown presets fall through to the default, so existing layers are unchanged.
 */
export function resolveVectorLineWidth(
  stylePreset: string | null | undefined,
): number {
  if (stylePreset && stylePreset.toLowerCase().includes("mesh")) {
    return MESH_LINE_WIDTH;
  }
  return VECTOR_LINE_WIDTH;
}

/**
 * Build a MapLibre `fill-color` expression mapping a `ds_mean` property
 * (0–1 damage state mean) through a green → yellow → red gradient (Part 2).
 *
 * The interpolation uses three stops:
 *   0.0 → green  #2DC937 (no damage)
 *   0.5 → yellow #E7B416 (moderate damage)
 *   1.0 → red    #CC3232 (heavy damage)
 *
 * When `ds_mean` is absent the fallback color is slate (#708090) to visually
 * distinguish "damage data missing" from any point in the gradient.
 *
 * The expression is a MapLibre-native expression array so it can be assigned
 * directly to `paint["fill-color"]` without any runtime JS interpolation on
 * the client (invariant 1: we emit received values, not computed numbers).
 */
export function buildDsMeanExpression(): unknown[] {
  return [
    "case",
    ["has", "ds_mean"],
    [
      "interpolate",
      ["linear"],
      ["get", "ds_mean"],
      0.0, "#2DC937",  // green  — no damage
      0.5, "#E7B416",  // yellow — moderate damage
      1.0, "#CC3232",  // red    — heavy damage
    ],
    "#708090",  // fallback: slate — ds_mean absent
  ];
}

// ---------------------------------------------------------------------------
// DATA-DRIVEN LEGEND -> generic vector fill expression (the colormap KEY from the
// data). When a layer carries a `LegendKey` with a `value_field`, the producer
// has told us EXACTLY which GeoJSON property drives the color AND the
// classes/ramp+range to map it through. We build the MapLibre fill-color
// expression from that GENERICALLY, so a new graduated/categorical vector tool
// needs ZERO web changes (it replaces the isPelicunDamageLayer exact-match
// branch). presetColorFor + the hash palette stay as the fallback for vectors
// with no legend.
// ---------------------------------------------------------------------------

/** Fallback fill color when a feature has no value / falls outside the legend
 *  (slate -- visually distinguishes "no data" from any ramp/class color). */
export const LEGEND_FILL_FALLBACK = "#708090";

/**
 * True when a layer's `LegendKey` drives the vector FILL from a feature property
 * (i.e. it has a `value_field`). This is the generic replacement for the Pelicun
 * exact-match sentinel: ANY vector legend with a `value_field` paints via
 * `buildLegendFillExpression`, not just `pelicun_damage`.
 */
export function legendHasValueField(
  legend: LegendKey | null | undefined,
): legend is LegendKey {
  return Boolean(
    legend &&
      typeof legend.value_field === "string" &&
      legend.value_field.length > 0,
  );
}

/**
 * Build a MapLibre `fill-color` expression for a VECTOR layer GENERICALLY from
 * its data-driven `LegendKey`. The legend names the feature property
 * (`value_field`) and how to color it:
 *
 *   - CATEGORICAL (kind="categorical" or `classes` present): a `case` over the
 *     classes. Discrete `value` classes match the property exactly; numeric
 *     `value_min`/`value_max` classes match a half-open bin
 *     [value_min, value_max). Classes are tried in order; the first match wins.
 *   - CONTINUOUS (a `colormap` + `vmin`/`vmax`): an `interpolate` over the
 *     property, with the colormap stops (named ramp OR explicit) scaled from
 *     [0,1] onto [vmin, vmax]. The colormap is resolved via
 *     resolveLegendColormapStops; vmin/vmax default to 0/1 when absent.
 *
 * Every branch is wrapped so a feature missing the property (or outside every
 * class) falls back to LEGEND_FILL_FALLBACK -- the honesty floor: "no value
 * here" reads as the neutral fallback, never as a fabricated ramp color.
 *
 * Returns null when the legend cannot drive a fill (no `value_field`, or neither
 * a usable class list nor a resolvable continuous ramp), so the caller falls
 * back to the flat preset/palette color. The result is a MapLibre-native
 * expression array (invariant 1: emit received values, not computed numbers).
 */
export function buildLegendFillExpression(
  legend: LegendKey | null | undefined,
): unknown[] | null {
  if (!legendHasValueField(legend)) return null;
  const field = legend.value_field as string;
  const getValue = ["get", field];

  // CATEGORICAL: a `case` over the ordered classes. Discrete-value classes match
  // exactly; numeric-bin classes match [value_min, value_max). First match wins.
  const classes = legend.classes;
  if (legend.kind === "categorical" || (classes && classes.length > 0)) {
    if (!classes || classes.length === 0) return null;
    const expr: unknown[] = ["case"];
    let added = 0;
    for (const c of classes) {
      if (typeof c.color !== "string" || c.color.length === 0) continue;
      if (c.value !== undefined && c.value !== null) {
        // Discrete match: property == value.
        expr.push(["==", getValue, c.value]);
        expr.push(c.color);
        added += 1;
      } else if (
        typeof c.value_min === "number" &&
        typeof c.value_max === "number"
      ) {
        // Half-open numeric bin: value_min <= property < value_max. Guarded by
        // `has` so a missing property never matches a bin (-> fallback).
        expr.push([
          "all",
          ["has", field],
          [">=", getValue, c.value_min],
          ["<", getValue, c.value_max],
        ]);
        expr.push(c.color);
        added += 1;
      }
    }
    if (added === 0) return null;
    expr.push(LEGEND_FILL_FALLBACK); // fallback: no class matched.
    return expr;
  }

  // CONTINUOUS: an `interpolate` over the property, colormap scaled to [vmin,vmax].
  const stops = resolveLegendColormapStops(legend.colormap);
  if (!stops || stops.length === 0) return null;
  const vmin = typeof legend.vmin === "number" ? legend.vmin : 0;
  const vmaxRaw = typeof legend.vmax === "number" ? legend.vmax : 1;
  // Degenerate range guard: a flat range can't interpolate; fall back.
  const vmax = vmaxRaw > vmin ? vmaxRaw : vmin + 1;
  const interp: unknown[] = ["interpolate", ["linear"], getValue];
  for (const s of stops) {
    interp.push(vmin + s.position * (vmax - vmin));
    interp.push(s.color);
  }
  // Wrap so a feature missing the property reads the fallback (honesty floor).
  return ["case", ["has", field], interp, LEGEND_FILL_FALLBACK];
}

/**
 * Cluster source configuration parameters for dense point layers (Part 4).
 * When a point FeatureCollection has > CLUSTER_THRESHOLD features, Map.tsx
 * should create the GeoJSON source with clustering enabled using these params.
 */
export const CLUSTER_THRESHOLD = 500;
export const CLUSTER_RADIUS = 50;

/**
 * Default colour by GEOMETRY FAMILY for vector layers with no known preset.
 * Replaces the per-layer-id random palette hash (which gave two rivers from
 * different AOIs different colours): line=amber, polygon=slate, point=orange.
 * This is deterministic across AOIs — every unknown line reads amber, etc.
 */
export function geomFamilyColor(geomKind: VectorGeomKind): string {
  switch (geomKind) {
    case "line":
      return "#FFD700"; // amber — matches the roads line convention
    case "polygon":
      return "#708090"; // slate
    case "point":
      return "#FF7F0E"; // orange
    default:
      // "unknown" geometry: fall back to slate (neutral).
      return "#708090";
  }
}

/**
 * Final colour to use for a vector layer (preset > geometry family).
 *
 * Resolution order:
 *   1. A known `style_preset` (presetColorFor) always wins.
 *   2. The pelicun sentinel resolves to neutral grey (real colour comes from
 *      buildDsMeanExpression()).
 *   3. Unknown preset + a `geomKind` => deterministic GEOMETRY-FAMILY colour
 *      (line=amber, polygon=slate, point=orange). No per-layer-id hashing, so
 *      the same geometry family always reads the same colour across AOIs.
 *   4. Unknown preset + no `geomKind` (legacy callers) => the original
 *      deterministic palette hash, preserved for backward compatibility.
 */
export function resolveVectorColor(
  layerId: string,
  stylePreset: string | null | undefined,
  geomKind?: VectorGeomKind,
): string {
  const preset = presetColorFor(stylePreset);
  // If the preset is the pelicun sentinel, fall back to a neutral grey
  // since the real color comes from buildDsMeanExpression()
  if (preset === PELICUN_DAMAGE_PRESET) return "#708090";
  if (preset) return preset;
  // Unknown preset: colour by geometry family when known, else legacy hash.
  if (geomKind !== undefined) return geomFamilyColor(geomKind);
  return paletteColorFor(layerId);
}

/**
 * Returns true when the layer should use the Pelicun ds_mean choropleth
 * expression instead of a flat fill-color.
 */
export function isPelicunDamageLayer(stylePreset: string | null | undefined): boolean {
  if (!stylePreset) return false;
  return stylePreset.toLowerCase() === PELICUN_DAMAGE_PRESET;
}
