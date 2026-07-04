// GRACE-2 web  -  MapLibre GL JS CONUS basemap + WMS overlay wiring.
//
// M3 pivot (job-0025):
//   The default basemap is now sourced from the deployed QGIS Server WMS at
//   /ogc/wms?MAP=/mnt/qgs/grace2-sample.qgs&LAYERS=basemap-osm-conus (see
//   job-0024 audit  -  `.qgs` mounted at `/mnt/qgs/` via Cloud Run gen2 native
//   GCS volume mount; image digest @sha256:a703476049...). This satisfies
//   FR-WC-2 (Tier B / QGIS Server rendering path) and Invariant 4 (rendering
//   through QGIS Server) for the basemap-layer slice. Tier separation
//   (Invariant 5) is preserved: zero `gs://` URLs in client code; the client
//   talks to the QGIS Server endpoint only.
//
//   The OSM-direct raster source from M1 is KEPT in the style as an inactive
//   fallback layer (`layout.visibility = 'none'`). This is the FR-DT-1
//   swappability proof  -  flipping the visibility in the style spec swaps
//   the basemap source without touching the agent. No runtime feature-flag
//   plumbing (per "No legacy support pre-MVP").
//
// FR-WC-1, FR-WC-3, FR-DT-3, Decision I (preserved verbatim from M1):
//   - Initial view fits CONUS (lng -95.5, lat 37, zoom 4).
//   - Camera locked 2D: maxPitch:0, dragRotate disabled, no touch rotate.
//   - Pan + zoom enabled. No layer panel here (LayerPanel.tsx owns that).
//
// job-0068 additions:
//   - Subscribes to session-state.loaded_layers and wires WMS raster sources
//     via MapLibre addSource/addLayer (Invariant 4  -  QGIS Server renders;
//     client only registers URLs). Replace-not-reconcile per A.7: diffs
//     against a useRef<Set<string>> of added source IDs.
//   - Subscribes to map-command and handles zoom-to via map.fitBounds.
//
// The client renders, it never computes  -  every number on the map is a
// MapLibre-internal coordinate (Invariant 1 preserved trivially).

import { useEffect, useRef, useState } from "react";
import maplibregl, { Map as MapLibreMap, StyleSpecification } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { MapCommandPayload, SessionStatePayload, ProjectLayerSummary, RegionCandidate, LegendKey } from "./contracts";
import { compareLayersTopFirst } from "./contracts";
import type { FeatureCollection, Feature, Polygon, Geometry } from "geojson";
import { publicTileBase } from "./lib/public_base";
import { regionChoiceBus, type RegionChoiceBusState } from "./lib/region_choice_bus";
import {
  spatialInputBus,
  type SpatialInputBusState,
} from "./lib/spatial_input_bus";
import { SpatialDrawSurface } from "./components/SpatialDrawSurface";
import { AoiPickerCard } from "./components/AoiPickerCard";
// deck.gl SPIKE (#169): heavy/footprint vectors paint through an INTERLEAVED
// deck.gl MapboxOverlay on the SAME MapLibre map (basemap + every other layer +
// control stay on MapLibre). Map.tsx owns the overlay lifecycle.
//
// LAZY-LOAD (deck.gl SPIKE, #169): the deck.gl bundle (~211 KB gz: @deck.gl/core
// + /layers + /mapbox) is NOT in the main app chunk. We import only the PURE,
// deck.gl-FREE routing predicate + the layer type STATICALLY (the reconcile must
// decide routing synchronously); the deck.gl MapboxOverlay + the GeoJsonLayer
// builder are pulled in via a one-time `await import(...)` the FIRST time a
// deck-routed (footprint/heavy) layer actually appears. `import type` is erased,
// so nothing here reaches deck.gl in the static module graph.
import {
  shouldRouteToDeck,
  isFootprintLayer,
  type DeckRoutableLayer,
} from "./lib/deck_routing";
// Click-to-enrich (NATE 2026-06-27): the footprint popup fetches its full tag
// bag by (osm_type, osm_id) AFTER the slim id-only card paints. Footprint-only;
// every non-footprint popup path is untouched.
import { fetchBuildingDetail } from "./lib/building_enrich";
// Lazily-loaded deck.gl module shapes (the dynamic-import targets). `import type`
// is type-only (erased at build), so referencing these types does NOT pull deck.gl
// into the main chunk - only the runtime `await import(...)` below does, lazily.
import type { MapboxOverlay as MapboxOverlayType } from "@deck.gl/mapbox";
import type { buildDeckGeoJsonLayer as BuildDeckGeoJsonLayerFn } from "./lib/deck_layers";
// NATE map/loading-UX polish item 4 - the always-on "Draw AOI" map control that
// arms the bbox rectangle-draw on demand and stages it for the next prompt.
import { DrawAoiControl } from "./components/DrawAoiControl";
import type { SpatialInputRequestPayload } from "./contracts";
import { LayerLegend } from "./components/LayerLegend";
// C3 (job-0356 / per-case-layer-durability)  -  the CLIENT is the source of truth
// for visibility on a server replay. A genuine fresh-socket resume re-asserts
// visible:true for every active-Case layer (the server keeps no per-user
// visibility state), which would un-hide a layer the user explicitly hid. We
// read the user's persisted override map and let it WIN on replay  -  but ONLY for
// layer_ids the user explicitly toggled (the hasOwnProperty guard inside
// `readLayerVisibilityOverrides`'s consumers), so a never-toggled VISIBLE layer
// keeps rendering across reconnect.
import { readLayerVisibilityOverrides } from "./LayerPanel";
// job-0179 (per-Case client cache + view-state durability  -  "the seatbelt").
// The shared LayerCache gates teardown (allowsEvict: an omitted-but-still-
// tracked layer is NOT torn down on a stale/partial reconcile frame), supplies
// the user's persisted view-overrides (opacity / visibility / zIndex) to
// re-apply after a (re-)add, and records the user's live LayerPanel edits via
// setOverride so they survive a re-render. This SUBSUMES the localStorage
// `grace2.layerVisibility` override map (still read above for back-compat).
import { getLayerCache } from "./lib/layer_cache";
// JOB WEB-ANIM (#157.1)  -  the module-level sequence-animation controller. The
// frame-advance playback used to live inside LayerPanel/SequenceScrubber, so
// closing/unmounting the panel killed the animation. The controller now holds
// the playback state + interval OUTSIDE the React tree; Map.tsx (always mounted)
// registers the frame-visibility emitter so frames keep advancing on the map
// even while the Layers panel is closed.
import { getAnimationController } from "./lib/animation_controller";
// NATE map/loading-UX polish item 2 - preload + hold-until-loaded raster frame
// swap so stepping/playing an animation never shows a black-then-fill gap.
import {
  releaseWarmedFrames,
  swapFrameWithHold,
  type FrameMapAdapter,
} from "./lib/frame_preload";
import { FeaturePopup, type FeaturePopupData, type FeatureAttribute } from "./components/FeaturePopup";
import { useIsMobile } from "./hooks/useIsMobile";
// "3D terrain viz" project track (first cut) - MapLibre setTerrain over a
// terrain-RGB DEM raster-dem source + a hillshade + sky layer, gated on the
// persisted 3D toggle. The pure source-builder + apply/remove helpers live in
// lib/terrain_3d.ts so the wiring here stays thin (and the core is unit-tested
// without a WebGL canvas). DEM origin = TiTiler terrain-RGB of a per-case DEM
// COG when one exists, else the public AWS Terrarium tiles (works today).
import {
  applyTerrain3d,
  removeTerrain3d,
  buildTerrain3dCameraPose,
  buildFlat2dCameraPose,
  buildDrape3dResamplingExpression,
  TERRAIN_3D_EASE_MS,
  startAoiPulseGlow,
  type TerrainMapLike,
  type DrapeResamplingExpression,
  type AoiPulseGlowHandle,
} from "./lib/terrain_3d";
import {
  fetchVectorAsGeoJson,
  vectorResultFromInlineGeoJson,
  resolveVectorColor,
  resolveVectorLineWidth,
  isPelicunDamageLayer,
  isMeshGridLayer,
  MESH_FILL_OPACITY,
  MESH_LINE_WIDTH,
  buildDsMeanExpression,
  legendHasValueField,
  buildLegendFillExpression,
  POLYGON_FILL_OPACITY,
  POLYGON_STROKE_WIDTH,
  CLUSTER_THRESHOLD,
  CLUSTER_RADIUS,
  type VectorGeomKind,
} from "./lib/vector_rendering";

/** UI theme  -  see App.tsx for toggle implementation (job-0076). */
export type MapTheme = "light" | "dark";

/**
 * CartoDB DarkMatter raster tiles (CC-BY, no API key). Used as the dark-theme
 * basemap. Raster (not vector) is chosen for two reasons:
 *   1. The light-theme basemap is also raster (QGIS Server WMS), so swapping
 *      raster-for-raster preserves the layer/source type and avoids re-tuning
 *      paint props for the flood overlay.
 *   2. The vector style.json brings in glyphs/sprites + multiple sub-sources
 *      that complicate the swap path; raster is one-source one-layer.
 * Attribution per CartoDB ToS: "- OpenStreetMap contributors - CARTO".
 */
const CARTO_DARK_TILE_TEMPLATE = "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png";
const CARTO_DARK_ATTRIBUTION =
  '- <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors - <a href="https://carto.com/attributions" target="_blank" rel="noopener noreferrer">CARTO</a>';

// LIGHT-theme basemap  -  CartoDB Positron raster (CC-BY, no API key, CDN).
// ux-batch-1 GCP-DECOUPLE FIX (2026-06-16): the light basemap previously
// pointed at the GCP Cloud Run QGIS Server (DEFAULT_WMS_URL below), a lingering
// GCP dependency missed in the AWS migration  -  and that server is private
// (invoker-only) so the prod site got 403s and the map never settled, which
// stalled every deferred layer/extent draw (the "layers in panel but not on
// map / waits to go light->dark" incident). Positron mirrors the dark CartoDB
// basemap (raster, one-source-one-layer), needs no GCP and no QGIS Server, and
// keeps both themes working until QGIS Server is re-hosted on AWS (sprint-16).
const CARTO_LIGHT_TILE_TEMPLATE = "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png";
const CARTO_LIGHT_ATTRIBUTION = CARTO_DARK_ATTRIBUTION;

// QGIS Server WMS endpoint. Overridable via VITE_GRACE2_WMS_URL at build/dev
// start. Default = deployed M2 substrate (job-0018 + job-0024).
//
// NOTE: the MAP= query string IS part of the WMS endpoint contract here  - 
// QGIS Server keys projects by the filesystem-mounted `.qgs` path. Per the
// FR-QS-2 amendment surfaced from job-0024, `.qgs` reaches QGIS Server via
// the /mnt/qgs/ Cloud Run gen2 native GCS volume mount; layer-data refs
// INSIDE the `.qgs` still use /vsigs/.
const DEFAULT_WMS_URL =
  "https://grace-2-qgis-server-425352658356.us-central1.run.app/ogc/wms?MAP=/mnt/qgs/grace2-sample.qgs";

// job-0255 (sprint-13.5): env-gated QGIS proxy base. When VITE_QGIS_PROXY_BASE
// is set (prod), every QGIS Server WMS URL is rewritten so its
// scheme+host+path is replaced by the agent's /qgis-proxy endpoint and the
// original WMS query string is preserved. The agent proxy (which holds the
// only invoker grant on the now-private QGIS Server) forwards + streams the
// tile, stripping user credentials. ABSENT (dev/today) -> returns the URL
// byte-identical, so behavior is unchanged. Example:
//   VITE_QGIS_PROXY_BASE = "https://agent.example/qgis-proxy"
//   https://qgis.run.app/ogc/wms?MAP=x&LAYERS=y
//     -> https://agent.example/qgis-proxy?MAP=x&LAYERS=y
const QGIS_PROXY_BASE: string | undefined =
  (import.meta.env.VITE_QGIS_PROXY_BASE as string | undefined) || undefined;

export function applyQgisProxy(wmsUrl: string): string {
  if (!QGIS_PROXY_BASE) return wmsUrl; // dev/today: byte-identical passthrough.
  const qIdx = wmsUrl.indexOf("?");
  const query = qIdx >= 0 ? wmsUrl.slice(qIdx + 1) : "";
  const base = QGIS_PROXY_BASE.replace(/[?&]+$/, "");
  return query ? `${base}?${query}` : base;
}

const WMS_BASE_URL: string = applyQgisProxy(
  (import.meta.env.VITE_GRACE2_WMS_URL as string | undefined) ?? DEFAULT_WMS_URL,
);

// MapLibre injects {bbox-epsg-3857} into the tile URL with the tile's
// bounding box in EPSG:3857 (the default Web Mercator projection). QGIS
// Server returns a 256-256 PNG per tile request.
const WMS_TILE_TEMPLATE = `${WMS_BASE_URL}&SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=basemap-osm-conus&CRS=EPSG:3857&FORMAT=image/png&TRANSPARENT=true&BBOX={bbox-epsg-3857}&WIDTH=256&HEIGHT=256&STYLES=`;

// OSM Tier A fallback. Kept committed to demonstrate FR-DT-1 swappability;
// the visibility flag is 'none' so it does not render at runtime.
const OSM_TILE_TEMPLATE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const OSM_ATTRIBUTION =
  '- <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors';
const QGIS_WMS_ATTRIBUTION =
  'Basemap via TRID3NT QGIS Server  -  - <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors';

const CONUS_VIEW = {
  center: [-95.5, 37.0] as [number, number],
  zoom: 4,
};

const STYLE: StyleSpecification = {
  version: 8,
  sources: {
    // LIGHT basemap. ux-batch-1 GCP-decouple (2026-06-16): was the GCP QGIS
    // Server WMS (now private/unreachable from prod -> dead map). Swapped to
    // CartoDB Positron (CDN, no GCP, no QGIS Server). Source id kept as
    // "qgis-wms" so the theme-swap / beforeId logic below is unchanged; it now
    // serves CartoDB Positron tiles. (Re-point at QGIS Server once it is on AWS
    //  -  sprint-16  -  via VITE_GRACE2_WMS_URL.)
    "qgis-wms": {
      type: "raster",
      tiles: [
        (import.meta.env.VITE_GRACE2_WMS_URL as string | undefined)
          ? WMS_TILE_TEMPLATE
          : CARTO_LIGHT_TILE_TEMPLATE,
      ],
      tileSize: 256,
      attribution: (import.meta.env.VITE_GRACE2_WMS_URL as string | undefined)
        ? QGIS_WMS_ATTRIBUTION
        : CARTO_LIGHT_ATTRIBUTION,
      maxzoom: 19,
    },
    // Inactive fallback: OSM direct. FR-DT-1 Tier A swappability proof  - 
    // present in the style spec but `visibility: 'none'`. No runtime swap
    // affordance (no legacy support pre-MVP).
    "osm-fallback": {
      type: "raster",
      tiles: [OSM_TILE_TEMPLATE],
      tileSize: 256,
      attribution: OSM_ATTRIBUTION,
      maxzoom: 19,
    },
  },
  layers: [
    {
      id: "qgis-basemap",
      type: "raster",
      source: "qgis-wms",
      minzoom: 0,
      maxzoom: 22,
    },
    {
      id: "osm-fallback-basemap",
      type: "raster",
      source: "osm-fallback",
      minzoom: 0,
      maxzoom: 22,
      layout: { visibility: "none" },
    },
  ],
};

// Module-level reference so external code (e.g. integration tests, future
// LayerPanel apply paths) can introspect the map. The web side never
// mutates basemap style spec at runtime  -  only future agent-driven layers
// (M4) will append/remove layers via map-command handlers (job-0026+).
let activeMap: MapLibreMap | null = null;

export function getActiveMap(): MapLibreMap | null {
  return activeMap;
}

export type SessionStateSubscriber = (p: SessionStatePayload) => void;

// Wire-layer shape: the agent emits `uri` (not `source_url`) per its Python
// ProjectLayerSummary. contracts.ts uses `source_url` (the older TS mirror).
// This local type reads from the actual wire format. Job-0070 will reconcile
// the schema mismatch. (OQ-0068-URI: see report.md)
interface WireLayerSummary {
  layer_id: string;
  name: string;
  layer_type: string;
  uri: string;          // agent wire format (Python `uri` field)
  visible?: boolean;
  opacity?: number;
  // job-0139  -  vector layer additions. Optional because raster layers omit them.
  style_preset?: string | null;
  // DATA-DRIVEN LEGEND  -  the colormap KEY from the data (mirrors
  // ProjectLayerSummary.legend / execution.LayerURI.legend). When a VECTOR layer
  // carries a legend with a `value_field` the fill is driven GENERICALLY from it
  // (buildLegendFillExpression) instead of the isPelicunDamageLayer exact-match
  // sentinel; absent => the legacy flat preset/palette color path is unchanged.
  legend?: LegendKey | null;
  bbox?: [number, number, number, number] | null;
  // job-0175  -  inline GeoJSON for vector layers. When present, the client
  // skips the `uri` fetch (which would hit Invariant 5's gs:// guardrail
  // and silently no-op) and renders directly from this FeatureCollection.
  // The agent populates this for every cacheable vector fetcher (see
  // `services/agent/src/grace2_agent/pipeline_emitter.py:add_loaded_layer`).
  // Optional  -  older session-state snapshots predate this field.
  inline_geojson?: unknown;
  // F94  -  dense-vector handling. When the agent's tiled path is enabled it
  // emits a client-reachable vector-tile URL ({z}/{x}/{y}.pbf MVT or a
  // pmtiles:// URL) instead of inline GeoJSON, so MapLibre only draws what is
  // in view. When present this takes precedence over `inline_geojson`.
  vector_tile_url?: string;
  // F94  -  geometry family for the tiled source's paint layer (point/line/
  // polygon). The tiled path has no features to classify client-side, so the
  // agent declares the kind. Defaults to "polygon" (the footprint case).
  vector_geom_kind?: string;
  // F94  -  vector source-layer name inside the MVT tiles (PMTiles builder uses
  // "vector" by default). Required to address features in a vector source.
  vector_source_layer?: string;
  // F94  -  honest density tag when a dense layer was simplified/capped on the
  // inline fallback path. Additive (extra-tolerant): surfaced so the user knows
  // the layer was reduced for performance; never a silent drop.
  vector_density?: {
    strategy: string;
    original_feature_count: number;
    emitted_feature_count: number;
    simplified: boolean;
    capped: boolean;
  };
}

// Extended map-command discriminator: contracts.ts only mirrors the 5 layer-CRUD
// verbs (zoom-to etc. are deferred to M4-M5 per job-0025 scope). Map.tsx
// handles zoom-to from the bus (dev-injection + future WS routing). We use
// a widened local type so the switch is type-safe without editing frozen contracts.ts.
interface ZoomToCommand {
  command: "zoom-to";
  args: { bbox: number[] };
}
// job-0294 follow-on (ux-batch-1 F14): clear the analysis-extent rectangle.
// Emitted by App.tsx on Case exit (activeSession -> null) and on opening a Case
// that has no bbox / no zoom-to history, so a prior Case's AOI outline does not
// linger on the map. No args  -  it removes the single extent source + layers.
interface ClearAnalysisExtentCommand {
  command: "clear-analysis-extent";
}
// ux-batch-1 (F-CASES-CLEAR-ALL): snap the camera back to the default CONUS
// view. Emitted by App.tsx on Case EXIT (to the Cases root) so leaving a Case
// visibly resets the map (camera-only  -  no extent rectangle, unlike zoom-to).
interface ResetViewCommand {
  command: "reset-view";
}
type WireMapCommand =
  | MapCommandPayload
  | ZoomToCommand
  | ClearAnalysisExtentCommand
  | ResetViewCommand;

// subscribeMapCommand accepts a callback that can handle the wider WireMapCommand.
// The bus pushes MapCommandPayload values which satisfy WireMapCommand at runtime.
export type MapCommandSubscribeFunc = (cb: (p: WireMapCommand) => void) => () => void;

export interface MapViewProps {
  subscribeSessionState?: (cb: SessionStateSubscriber) => () => void;
  subscribeMapCommand?: MapCommandSubscribeFunc;
  /** Light = QGIS Server WMS basemap. Dark = CartoDB DarkMatter raster.
   *  job-0076 bundled enhancement (dark backdrop makes flood overlay obvious). */
  theme?: MapTheme;
  /**
   * Lifts the TRUE projected AOI screen rectangle (the same `legendRect` the
   * LayerLegend snaps against  -  computeBboxScreenRect over all four bbox
   * corners) up to App so the SequenceScrubber (rendered inside LayerPanel,
   * which has no map handle) can pin bottom-center of the AOI box and track
   * pan/zoom, exactly like the legend keys. Called with the rect whenever it
   * changes, and with null when the AOI leaves the viewport / there is no AOI.
   * `LegendScreenRect` is structurally identical to legend_snap's `ScreenRect`.
   */
  onAoiScreenRectChange?: (rect: LegendScreenRect | null) => void;
  /**
   * ZOOM-OUT HIDE (NATE 2026-06-27, mobile-only) - lifts the "the AOI bbox is a
   * tiny DOT on screen" signal (aoiRectTooSmallToShow over the freshly-projected
   * rect) up to App so the SequenceScrubber (rendered at the App root) can HIDE
   * itself when the user has zoomed OUT far enough that the bbox is a speck. The
   * legend reads the same signal directly (threaded below). Called with the
   * boolean whenever it changes; false when there is no AOI or the box is large
   * enough to be useful. Desktop ignores it (the scrubber never reads it there).
   */
  onAoiTooSmallToShowChange?: (tooSmall: boolean) => void;
  /**
   * Item b (NATE 2026-06-20)  -  CONTROLLED legend hide state, threaded straight
   * to LayerLegend. App owns it on mobile so the show/hide toggle can live in
   * the expanded Layers section (out of the chat composer's way). Undefined =>
   * the legend keeps its own internal hide state (desktop default).
   */
  legendHidden?: boolean;
  /** Item b  -  fired when the legend hide state toggles (controlled mode). */
  onLegendHiddenChange?: (hidden: boolean) => void;
  /**
   * Item b  -  suppress the legend's floating "Show legend" pill (mobile uses the
   * in-panel toggle instead, so the floating pill must not also render).
   */
  suppressLegendShowPill?: boolean;
  /**
   * MOBILE SHEET-TOP DOCK (NATE 2026-06-24)  -  the on-screen Y of the mobile chat
   * sheet's TOP edge, threaded straight to LayerLegend. On mobile, when set, the
   * legend's bottom-center fallback keys + the collapsed "Show legend" pill dock
   * just ABOVE this Y (a clean band at the chat-panel top) instead of floating
   * over the map with a fixed-pixel composer clearance. Null/undefined on desktop
   * (the desktop dock is unaffected).
   */
  legendSheetTopPx?: number | null;
  /**
   * CHART-OVERLAY HIDE-LEGEND (NATE 2026-06-28, mobile) - whether Chat's
   * full-viewport ChartGallery overlay is open. Threaded straight to the
   * LayerLegend as `chartOpen`: on mobile the legend renders nothing while a
   * chart is open so the body-portaled colorbar never paints above/around the
   * chart. Default false; ignored on desktop (the legend already sits below the
   * gallery's z=10000 overlay).
   */
  legendChartOpen?: boolean;
  /**
   * CASES-ROOT NO-LAYERS GATE (NATE 2026-06-22) - whether a Case is currently
   * ENTERED. NATE: "no case layers should be loaded when we are in the cases
   * section; they should only be rendered when we have entered a Case." When
   * false (the cases-list / root view, activeCaseId === null) the map renders
   * NO data overlays and the legend has NO content - any layers from a
   * previously-viewed Case are torn down. When true (a Case is entered) the
   * reconcile + legend behave exactly as before. Undefined defaults to true so
   * older callers / unit fixtures that drive MapView directly (no Case shell)
   * keep rendering layers as they always did.
   */
  caseActive?: boolean;
  /**
   * #170 AOI-first manual case-creation. When true, the AoiPickerCard overlay
   * mounts on the live map so the user can draw / enter the AOI bbox BEFORE the
   * first prompt. This is a LOCAL App signal (NOT the spatial-input bus) - the
   * card is request-free (no active turn; the agent box may be asleep).
   */
  aoiCaptureActive?: boolean;
  /** #170 - confirm the AOI capture with the chosen bbox [minLon,minLat,maxLon,maxLat] + Case name. */
  onAoiCaptureConfirm?: (bbox: [number, number, number, number], name: string) => void;
  /** #170 - skip the AOI step (create with the name + no bbox). */
  onAoiCaptureSkip?: (name: string) => void;
  /** #170 - dismiss the AOI overlay without creating a Case. */
  onAoiCaptureCancel?: () => void;
  /**
   * NATE FIX 2 - the desktop chat panel's current dragged width (px), threaded
   * to DrawAoiControl so the always-on Draw-AOI button rails to the LEFT of the
   * chat panel and tracks it as the panel resizes. Undefined keeps the legacy
   * top-right placement.
   */
  chatWidthPx?: number;
  /**
   * NATE FIX 2 - whether the desktop chat panel is collapsed (the Draw-AOI
   * button then tucks under the top-right chat-expand hamburger).
   */
  chatCollapsed?: boolean;
  /**
   * NATE FIX 2 - mobile chrome (the chat is a bottom sheet; the Draw-AOI button
   * keeps its plain top-right placement).
   */
  mobile?: boolean;
  /**
   * NATE 2026-06-22 (item 4) - whether a long-running sim is in progress. When
   * true the SINGLE analysis-extent AOI rectangle recolors to purple (matching
   * the sim scan tone); false reverts it to blue. No second box is drawn - the
   * existing rectangle's stroke color is mutated in place. Undefined => blue.
   */
  simRunning?: boolean;
  /**
   * ITEM 1 (NATE 2026-06-22) - whether the active case already HAS an AOI /
   * analysis extent set. Threaded to DrawAoiControl: the Draw-AOI control group
   * is for STARTING a case (setting the AOI to begin), so once a case has a
   * bounding box NONE of those controls render. Undefined => false (a fresh
   * no-AOI start) so the control still shows.
   */
  caseHasAoi?: boolean;
  /**
   * NATE 2026-06-22 (item 6) / ITEM 4 (feature #170) - confirm/finalize the
   * staged Draw-AOI box. The always-on DrawAoiControl's green "+" calls this with
   * the staged bbox; App wires it to seed the case AOI to the agent
   * (createCase(null, bbox)) and/or a `zoom-to` map-command so the drawn box
   * becomes the persistent analysis-extent rectangle (and the camera fits it).
   * The draw-and-fit no-agent path is unchanged. Undefined => the "+" is
   * draw-and-fit only and keeps the staged pick overlay.
   */
  onAoiStageConfirm?: (bbox: [number, number, number, number]) => void;
  /**
   * "3D terrain viz" first cut - when true, MapLibre terrain is enabled (a
   * terrain-RGB DEM raster-dem source + hillshade + sky + setTerrain, and
   * two-finger pitch/rotate unlocked). When false, setTerrain(null) and the
   * flat 2D camera is restored. The flag is persisted in localStorage by
   * Settings (lib/terrain_3d.ts); App re-reads it and threads it here.
   * Undefined => 2D (the historical default), so older callers / unit fixtures
   * render flat as before.
   */
  terrain3dEnabled?: boolean;
  /**
   * Contour overlay flag (STUB for the first cut). Persisted + surfaced in
   * Settings so the UX seam is real, but real contour LINES need the
   * maplibre-contour plugin (not a dep) - so when true alongside terrain3d it
   * only logs a TODO. Threaded so the future wire-up has the value.
   */
  contoursEnabled?: boolean;
  /**
   * LANE B #3 (panel-aware fit) - the current width (px) of the desktop LEFT
   * rail (CasesPanel / CaseView) when it is open, else 0. fitBounds otherwise
   * centers the AOI bbox in the WHOLE canvas, so it lands behind the open
   * left rail / right chat panel ("snapping to the left snaps away from the
   * bbox"). The zoom-to / region-choice fits add this (left) + chatWidthPx
   * (right) as asymmetric padding so the box centers in the VISIBLE map gutter.
   * Undefined / 0 = no left rail (mobile, or collapsed) -> uniform padding.
   */
  leftPanelWidthPx?: number;
}

/**
 * Style-preset -> WMS LAYERS value derivation table for upstream tools that
 * emit a bare WMS endpoint (no `?LAYERS=...`) and rely on the client to
 * supply the layer name. Currently used for Iowa State Mesonet NEXRAD
 * (`fetch_nexrad_reflectivity`  -  job-0102/0105 family) whose LayerURI.uri
 * is `https://.../wms/nexrad/<product>.cgi` with the LAYERS value implicit
 * in the path.
 *
 * job-0171: the producer contract documented in
 * `docs/decisions/layer-emission-contract.md:36` says `ProjectLayerSummary.uri`
 * MUST be a full WMS URL with `LAYERS=` baked in. Several Tier-1
 * data-source atomic tools violated that contract by emitting only the
 * service endpoint. This map is the compatibility shim that recovers the
 * intended LAYERS name from `style_preset`; the long-term fix is for those
 * tools to emit a complete URL (raised as OQ-0171-WMS-URL-CONTRACT).
 *
 * The presets here mirror the values registered in
 * `services/agent/src/grace2_agent/tools/fetch_nexrad_reflectivity.py:111-117`
 * (`_PRODUCT_LAYER_NAME`).
 */
const STYLE_PRESET_TO_WMS_LAYERS: Record<string, string> = {
  // job-0171 live diagnosis (evidence/iowa_capabilities_audit.txt): the
  // Iowa Mesonet WMS does NOT publish `nexrad-{product}-wmst` layers  -  that
  // value in the agent tool's `_PRODUCT_LAYER_NAME` table is wrong. The
  // EPSG:3857 (Web Mercator) layer name follows the legacy `-900913`
  // convention (the original Web-Mercator EPSG code, kept for back-compat
  // by Iowa Mesonet). We use the `-900913` suffix because MapLibre's raster
  // source requests tiles in EPSG:3857. Tracked as OQ-0171-NEXRAD-LAYER-NAME.
  nexrad_n0r: "nexrad-n0r-900913",
  nexrad_n0q: "nexrad-n0q-900913",
  nexrad_vil: "nexrad-vil-900913",
};

/**
 * Build the WMS tile URL for a given base WMS URL. MapLibre substitutes
 * `{bbox-epsg-3857}` per tile.
 *
 * Invariant 4: QGIS Server renders; client just registers the URL.
 *
 * Contract (per `docs/decisions/layer-emission-contract.md:36`): the
 * base URL is expected to already include `?` + the WMS service params
 * MAP and LAYERS. job-0171 diagnosis (evidence/radar_diag.json) shows
 * the Iowa State Mesonet NEXRAD tool emits the bare `*.cgi` endpoint
 * without either `?` or `LAYERS=`, which means this helper used to
 * produce malformed URLs like `...n0r.cgi&SERVICE=WMS&...&LAYERS=` (no
 * `LAYERS` value) that the Iowa Mesonet WMS rejects as a 400.
 *
 * This helper now defensively normalises:
 *   1. Use `?` as separator when the base URL has no `?` yet, `&` otherwise.
 *   2. If the base URL is missing a `LAYERS=` param, fall back to the
 *      `style_preset -> STYLE_PRESET_TO_WMS_LAYERS` lookup. Logs a warn
 *      when neither is present so the diagnosis is loud.
 *   3. Add the per-tile WMS GetMap params MapLibre's raster source needs.
 */
export function buildWmsTileUrl(wmsUrl: string, stylePreset?: string | null): string {
  // sprint-14-aws (job-0290): the AWS agent publishes rasters as ready XYZ
  // tile TEMPLATES (TiTiler  -  contains {z}/{x}/{y}). Pass them through
  // untouched: appending WMS params to an XYZ template would 400 every tile.
  if (wmsUrl.includes("{z}")) {
    // sprint-14-aws (job-0296): on the HTTPS CloudFront edge, rewrite a legacy
    // http://<ip>:8080 TiTiler origin (baked into pre-cutover layer URIs) to the
    // public base so persisted tiles aren't mixed-content-blocked. CloudFront's
    // /cog/* behavior routes to TiTiler. No-op when VITE_GRACE2_PUBLIC_BASE is
    // unset (publicTileBase()===null)  -  byte-identical to the http-site path.
    const base = publicTileBase();
    if (base) return wmsUrl.replace(/^https?:\/\/[^/]+:8080/, base);
    return wmsUrl;
  }
  // job-0255: route overlay WMS URLs through the agent proxy when
  // VITE_QGIS_PROXY_BASE is set (no-op otherwise  -  byte-identical).
  wmsUrl = applyQgisProxy(wmsUrl);
  const sep = wmsUrl.includes("?") ? "&" : "?";
  let layersParam = "";
  // The upstream URL may already contain LAYERS=. If it doesn't, attempt to
  // synthesise one from the style preset so the tile request is actually
  // valid (otherwise the WMS server 400s and MapLibre silently paints
  // nothing  -  the user-reported symptom).
  if (!/[?&]LAYERS=/i.test(wmsUrl)) {
    const layers = stylePreset ? STYLE_PRESET_TO_WMS_LAYERS[stylePreset] : undefined;
    if (layers) {
      layersParam = `&LAYERS=${encodeURIComponent(layers)}`;
    } else {
      // No LAYERS in URL and no preset mapping. Tile fetch is doomed; log
      // loudly so this is diagnosable without needing the network panel.
      // We still emit the URL so a future fix can pick up cleanly without
      // changing the call sites (defense in depth, not silent suppression).
      // eslint-disable-next-line no-console
      console.warn(
        "[Map] buildWmsTileUrl: WMS URL has no LAYERS= and no known style preset; tile fetch will likely 400. " +
          "See OQ-0171-WMS-URL-CONTRACT. uri=" + wmsUrl + " style_preset=" + String(stylePreset),
      );
    }
  }
  return `${wmsUrl}${sep}SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&CRS=EPSG:3857&FORMAT=image%2Fpng&TRANSPARENT=true&BBOX={bbox-epsg-3857}&WIDTH=256&HEIGHT=256&STYLES=${layersParam}`;
}

// Layer + source IDs for the swappable basemap. The light basemap source is
// the QGIS Server WMS proxy (already in the seed style); the dark basemap
// source is added/removed at runtime when the theme changes.
const BASEMAP_LAYER_ID = "qgis-basemap";
const BASEMAP_SOURCE_ID = "qgis-wms";
const DARK_BASEMAP_LAYER_ID = "carto-dark-basemap";
const DARK_BASEMAP_SOURCE_ID = "carto-dark";

// job-0294  -  "analysis extent" rectangle. When the agent emits a `zoom-to`
// map-command with a bbox, we ALSO outline that extent as a styled rectangle so
// the user sees exactly what area is being measured. A SINGLE extent rectangle
// (replace-on-new-bbox) is the v0.1 contract  -  the source is the same
// map-command the camera consumes, so persisted-case reopen (App.tsx replays
// the last zoom-to through the bus) redraws it for free. Thin dashed accent
// stroke, faint fill.
const ANALYSIS_EXTENT_SOURCE_ID = "grace2-analysis-extent";
const ANALYSIS_EXTENT_FILL_LAYER_ID = "grace2-analysis-extent-fill";
const ANALYSIS_EXTENT_LINE_LAYER_ID = "grace2-analysis-extent-line";
// NATE 2026-06-22 (item 4): the analysis-extent rectangle is the ONE AOI box.
// Its default stroke is blue; while a sim runs it RECOLORS to purple (matching
// the sim pipeline-card / scan tone), then REVERTS to blue when the sim ends. No
// SECOND box is drawn - the same single rectangle changes color.
const ANALYSIS_EXTENT_COLOR_IDLE = "#4D96FF"; // blue (default).
const ANALYSIS_EXTENT_COLOR_SIM = "#a855f7"; // purple (sim in progress).

/**
 * MOBILE SNAP-BELOW-SHEET (NATE 2026-06-24 live-mobile feedback: "the snap to
 * bbox on mobile just snaps to the center of the screen.") When the mobile chat
 * sheet covers the bottom of the map, a fit with uniform padding centers the AOI
 * in the FULL viewport - i.e. partly BEHIND the sheet. The fix is to pad the
 * BOTTOM of the fit by the area the sheet covers (viewportH - sheetTopPx) plus a
 * small margin, so the AOI frames in the VISIBLE band ABOVE the sheet. When the
 * sheet-top Y is unknown at snap time we fall back to a sensible fraction of the
 * viewport height (the sheet's collapsed footprint is well under this).
 */
export const MOBILE_SNAP_SHEET_MARGIN_PX = 24;
export const MOBILE_SNAP_BOTTOM_FALLBACK_FRACTION = 0.4;

/**
 * LANE B #3 (panel-aware fitBounds) - build an asymmetric PaddingOptions that
 * adds the occluded desktop panel widths so a fit centers the bbox in the
 * VISIBLE map gutter, not behind the panels. The left rail (CasesPanel /
 * CaseView, `leftPanelPx`) occludes the left; the chat panel (`chatPx`, only
 * when not collapsed) occludes the right.
 *
 * MOBILE SNAP-BELOW-SHEET (NATE 2026-06-24): on mobile the left/right panel
 * extras are 0 (no side rails), but the chat BOTTOM-SHEET occludes the lower part
 * of the map. We add a BOTTOM pad equal to the covered area below the sheet top
 * (viewportH - `sheetTopPx`, + a small margin) so the AOI frames in the VISIBLE
 * band ABOVE the sheet instead of centering behind it. When `sheetTopPx` is
 * null/unknown at snap time we fall back to a fraction of the viewport height.
 * Desktop is unchanged (sheetTopPx is null there -> bottom stays the scalar base).
 *
 * The padding is clamped to keep each axis' total below the canvas dimension
 * (MapLibre throws when left+right >= width or top+bottom >= height), with a 24px
 * safety floor so the box never fills edge-to-edge.
 */
function panelAwareFitPadding(
  m: MapLibreMap,
  base: number,
  leftPanelPx: number,
  chatPx: number,
  chatCollapsed: boolean,
  mobile: boolean,
  sheetTopPx: number | null = null,
): maplibregl.PaddingOptions {
  const leftExtra = mobile ? 0 : Math.max(0, leftPanelPx);
  const rightExtra = mobile || chatCollapsed ? 0 : Math.max(0, chatPx);
  let left = base + leftExtra;
  let right = base + rightExtra;
  const top = base;
  let bottom = base;
  // MOBILE SNAP-BELOW-SHEET: pad the bottom by the chat-sheet's covered area so
  // the AOI frames ABOVE the sheet, not behind it. Only on mobile (the bottom
  // sheet only exists there); desktop keeps the scalar base.
  if (mobile) {
    let viewportH = 0;
    try {
      const canvas = m.getCanvas();
      viewportH = canvas.clientHeight || canvas.height || 0;
    } catch {
      viewportH = 0;
    }
    if (viewportH <= 0 && typeof window !== "undefined") {
      viewportH = window.innerHeight || 0;
    }
    if (
      typeof sheetTopPx === "number" &&
      Number.isFinite(sheetTopPx) &&
      viewportH > 0 &&
      sheetTopPx < viewportH
    ) {
      // The sheet covers everything below its top edge; reserve that band plus a
      // small margin so the AOI does not kiss the sheet's top.
      bottom = base + (viewportH - sheetTopPx) + MOBILE_SNAP_SHEET_MARGIN_PX;
    } else if (viewportH > 0) {
      // Sheet-top Y unknown at snap time -> reserve a sensible fraction of the
      // viewport height so the AOI still frames in the upper map band.
      bottom = base + viewportH * MOBILE_SNAP_BOTTOM_FALLBACK_FRACTION;
    }
  }
  try {
    const canvas = m.getCanvas();
    const w = canvas.clientWidth || canvas.width || 0;
    if (w > 0) {
      // Keep left + right strictly below the canvas width (leave a 24px gutter
      // for the box itself) so fitBounds never throws on an over-padded axis.
      const maxSide = Math.max(0, w - 24);
      if (left + right > maxSide) {
        const scale = maxSide / (left + right);
        left = Math.floor(left * scale);
        right = Math.floor(right * scale);
      }
    }
    const h = canvas.clientHeight || canvas.height || 0;
    if (h > 0) {
      // Same guard for the vertical axis - a large mobile bottom pad must not
      // push top + bottom past the canvas height (MapLibre throws otherwise).
      const maxVert = Math.max(0, h - 24);
      if (top + bottom > maxVert) {
        // Keep the small top pad; absorb the overflow into the bottom (the side
        // we are intentionally reserving for the sheet). Floor at 0.
        bottom = Math.max(0, Math.floor(maxVert - top));
      }
    }
  } catch {
    /* no canvas yet (SSR / pre-mount) - fall back to the unclamped pads */
  }
  return { top, bottom, left, right };
}

/**
 * INCIDENT FIX 2026-06-16  -  hung-tile resilience. The reconcile + layer-add
 * paths gated on ``map.isStyleLoaded()``, which maplibre-gl returns false while
 * ANY source cache is still loading. A single HUNG raster source (e.g. a
 * vector .fgb wrongly published behind TiTiler's /cog raster face  -  its tiles
 * never resolve) made ``isStyleLoaded()`` false PERMANENTLY, which froze the
 * whole reconcile loop: NO overlays painted, removals didn't run, the AOI
 * never drew (the "layers in panel, blank map, hit-or-miss" incident).
 *
 * Fix: latch readiness once the style spec has loaded a single time. After the
 * first ``isStyleLoaded()===true`` (or the map's ``load`` event), addSource /
 * addLayer are safe regardless of whether some tiles are still loading or hung,
 * so we stop gating on the tile-sensitive ``isStyleLoaded()`` and use the latch
 * instead. The latch lives on the map instance so both the MapView effect and
 * the module-level ``addVectorLayer`` (which only receives ``m``) can read it.
 */
type ReadyMap = MapLibreMap & {
  __grace2StyleReady?: boolean;
  // FIX 2 (vector AOI clip)  -  the current AOI bbox `[minLon,minLat,maxLon,maxLat]`
  // (EPSG:4326), stashed on the map instance so the MODULE-LEVEL vector add path
  // (`addVectorLayer` / `registerVectorOnMap`, which only receive `m`) can clip
  // features to the AOI without threading React state down. Mirrors the
  // `__grace2StyleReady` latch pattern. Null/undefined => no AOI => no clip.
  __grace2AoiBbox?: [number, number, number, number] | null;
};
export function mapStyleReady(m: MapLibreMap): boolean {
  const rm = m as ReadyMap;
  try {
    if (m.isStyleLoaded()) {
      rm.__grace2StyleReady = true;
      return true;
    }
  } catch {
    return false;
  }
  return rm.__grace2StyleReady === true;
}

/**
 * FIX 2 (vector AOI clip)  -  read/write the AOI bbox stashed on the map instance.
 * The agent fetches NSI points / building footprints with the AOI bbox expanded
 * ~10% (so edge features aren't clipped server-side), which left vectors
 * rendering BEYOND the AOI rectangle the user drew. We clip client-side to the
 * exact AOI bbox before adding the GeoJSON source.
 */
export function setMapAoiBbox(
  m: MapLibreMap,
  bbox: [number, number, number, number] | null,
): void {
  (m as ReadyMap).__grace2AoiBbox = bbox;
}
export function getMapAoiBbox(
  m: MapLibreMap,
): [number, number, number, number] | null {
  return (m as ReadyMap).__grace2AoiBbox ?? null;
}

/** True when a feature's own bbox overlaps the AOI bbox (axis-aligned overlap). */
function bboxesOverlap(
  a: [number, number, number, number],
  b: [number, number, number, number],
): boolean {
  // a/b = [minLon, minLat, maxLon, maxLat]. Overlap iff they intersect on both
  // axes. Touching edges count as overlap (inclusive) so an AOI-edge feature is
  // kept. Pure pixel/coord math  -  no geography computed (Invariant 1).
  return a[0] <= b[2] && a[2] >= b[0] && a[1] <= b[3] && a[3] >= b[1];
}

/** Compute a geometry's [minLon,minLat,maxLon,maxLat] bbox; null when empty. */
function geometryBbox(
  geom: Geometry | null | undefined,
): [number, number, number, number] | null {
  if (!geom) return null;
  let minLon = Infinity;
  let minLat = Infinity;
  let maxLon = -Infinity;
  let maxLat = -Infinity;
  const visit = (coords: unknown): void => {
    if (!Array.isArray(coords)) return;
    // A position is [lon, lat, ...]; recurse otherwise.
    if (typeof coords[0] === "number" && typeof coords[1] === "number") {
      const lon = coords[0];
      const lat = coords[1];
      if (lon < minLon) minLon = lon;
      if (lat < minLat) minLat = lat;
      if (lon > maxLon) maxLon = lon;
      if (lat > maxLat) maxLat = lat;
      return;
    }
    for (const c of coords) visit(c);
  };
  if (geom.type === "GeometryCollection") {
    for (const g of geom.geometries) {
      const sub = geometryBbox(g);
      if (sub) {
        if (sub[0] < minLon) minLon = sub[0];
        if (sub[1] < minLat) minLat = sub[1];
        if (sub[2] > maxLon) maxLon = sub[2];
        if (sub[3] > maxLat) maxLat = sub[3];
      }
    }
  } else {
    visit((geom as { coordinates?: unknown }).coordinates);
  }
  if (
    !Number.isFinite(minLon) ||
    !Number.isFinite(minLat) ||
    !Number.isFinite(maxLon) ||
    !Number.isFinite(maxLat)
  ) {
    return null;
  }
  return [minLon, minLat, maxLon, maxLat];
}

/**
 * FIX 2 (vector AOI clip), RELAXED 2026-06-21.
 *
 * Historically this dropped every feature whose geometry bbox did not overlap
 * the AOI bbox stashed on the map (`__grace2AoiBbox`, set from the LAST zoom-to
 * camera move). That was redundant and actively HARMFUL: the agent (Lane C) now
 * clips vectors to the pinned AOI SERVER-SIDE, so an incoming server-provided
 * layer is already AOI-scoped; meanwhile `__grace2AoiBbox` can be a STALE /
 * SMALLER camera extent than the layer's true data, in which case this silently
 * dropped legitimate buildings/rivers that the user expected to see.
 *
 * The relax: NEVER clip a layer against an AOI bbox that is SMALLER than the
 * layer's own data extent. We first union every feature's bbox into the layer's
 * overall data extent; if that extent is not fully CONTAINED within `aoi`
 * (allowing a tiny coord tolerance), the AOI is stale/smaller than the data and
 * we pass the collection through UNTOUCHED. We only drop outliers when the AOI
 * genuinely encloses the data (the data is a strict subset of the AOI)  -  i.e.
 * the historical "fetched ~10% expanded, trim the fringe" case, where dropping a
 * far-flung stray feature is safe and the bulk of the data is well inside.
 *
 * Edge-overlapping features are always kept (`bboxesOverlap` is inclusive).
 * Features we can't bbox are always kept (never silently drop on a parse miss).
 * Returns the SAME collection (no copy) when `aoi` is null or nothing is
 * dropped, so the common path is allocation-free. Exported for unit testing.
 */
export function clipFeaturesToBbox(
  fc: FeatureCollection,
  aoi: [number, number, number, number] | null,
): FeatureCollection {
  if (!aoi) return fc;

  // Union of all feature bboxes = the layer's own data extent.
  let dataMinLon = Infinity;
  let dataMinLat = Infinity;
  let dataMaxLon = -Infinity;
  let dataMaxLat = -Infinity;
  let anyBbox = false;
  for (const f of fc.features) {
    const gb = geometryBbox(f.geometry as Geometry | null);
    if (gb == null) continue;
    anyBbox = true;
    if (gb[0] < dataMinLon) dataMinLon = gb[0];
    if (gb[1] < dataMinLat) dataMinLat = gb[1];
    if (gb[2] > dataMaxLon) dataMaxLon = gb[2];
    if (gb[3] > dataMaxLat) dataMaxLat = gb[3];
  }

  // No bbox-able features (all unparseable) -> nothing safe to clip against.
  if (!anyBbox) return fc;

  // If the layer's data extent is NOT fully contained within the AOI (allowing a
  // small tolerance), the AOI is stale/smaller than the layer's own data. In
  // that case we MUST NOT clip  -  the server already AOI-scoped this layer, and
  // dropping against a smaller stale camera extent would silently lose coverage.
  // Tolerance is a fraction of the AOI's own span so it scales with zoom.
  const aoiW = aoi[2] - aoi[0];
  const aoiH = aoi[3] - aoi[1];
  const tolX = Math.max(Math.abs(aoiW), 1e-9) * CLIP_CONTAINMENT_TOLERANCE;
  const tolY = Math.max(Math.abs(aoiH), 1e-9) * CLIP_CONTAINMENT_TOLERANCE;
  const dataContainedInAoi =
    dataMinLon >= aoi[0] - tolX &&
    dataMinLat >= aoi[1] - tolY &&
    dataMaxLon <= aoi[2] + tolX &&
    dataMaxLat <= aoi[3] + tolY;
  if (!dataContainedInAoi) return fc;

  // The AOI genuinely encloses the data (data is a strict subset of the AOI):
  // safe to trim any far-flung stray feature that does not overlap the AOI. The
  // per-feature overlap test uses the SAME tolerance-expanded AOI as the
  // containment gate above, so a feature grazing just past the AOI edge (within
  // the tolerance band) is still kept  -  the bias is consistently toward KEEPING.
  const aoiTol: [number, number, number, number] = [
    aoi[0] - tolX,
    aoi[1] - tolY,
    aoi[2] + tolX,
    aoi[3] + tolY,
  ];
  const kept: Feature[] = [];
  let dropped = 0;
  for (const f of fc.features) {
    const gb = geometryBbox(f.geometry as Geometry | null);
    if (gb == null || bboxesOverlap(gb, aoiTol)) {
      kept.push(f);
    } else {
      dropped += 1;
    }
  }
  if (dropped === 0) return fc;
  return { ...fc, features: kept };
}

/**
 * Fractional tolerance for the "data extent contained within AOI" containment
 * check in clipFeaturesToBbox: the AOI is grown by this fraction of its own span
 * on each axis before testing containment, so a feature whose bbox grazes the
 * AOI edge (or sits within the agent's ~10% fetch expansion) still counts as
 * contained. Generous on purpose  -  the bias is toward KEEPING features.
 */
export const CLIP_CONTAINMENT_TOLERANCE = 0.1;

/**
 * DATA-DRIVEN LEGEND - resolve the polygon `fill-color` for a vector layer,
 * GENERICALLY honoring a `LegendKey` with a `value_field`.
 *
 * Resolution order:
 *   1. legend.value_field present AND buildLegendFillExpression succeeds -> the
 *      data-driven MapLibre expression (categorical match / continuous interpolate).
 *      This is the generic replacement for the isPelicunDamageLayer exact-match
 *      branch, so Pelicun (now emitting legend.value_field="ds_mean") renders via
 *      this path - the old style_preset token mismatch is gone by construction.
 *   2. legacy Pelicun sentinel (style_preset=="pelicun_damage", no legend) ->
 *      buildDsMeanExpression(), preserved for any pre-legend Pelicun output.
 *   3. otherwise -> the flat preset/palette `color`.
 *
 * Returns a MapLibre-native fill-color value (expression array OR hex string).
 */
function resolvePolygonFillColor(
  legend: LegendKey | null | undefined,
  stylePreset: string | null | undefined,
  color: string,
): unknown {
  if (legendHasValueField(legend)) {
    const expr = buildLegendFillExpression(legend);
    if (expr) return expr;
  }
  if (isPelicunDamageLayer(stylePreset)) return buildDsMeanExpression();
  return color;
}

/**
 * DATA-DRIVEN LEGEND - the polygon fill-opacity for a vector layer. A graduated /
 * categorical layer (a data-driven legend.value_field, OR the legacy Pelicun
 * sentinel) paints BOLD (0.7) so the choropleth reads clearly; mesh stays faint
 * (wireframe); everything else uses the default POLYGON_FILL_OPACITY. Kept here so
 * the three registration sites + the opacity-slider helper share one rule.
 */
function resolvePolygonFillOpacity(
  baseOpacity: number,
  legend: LegendKey | null | undefined,
  stylePreset: string | null | undefined,
): number {
  if (isMeshGridLayer(stylePreset)) return baseOpacity * MESH_FILL_OPACITY;
  if (legendHasValueField(legend) || isPelicunDamageLayer(stylePreset)) {
    return baseOpacity * 0.7;
  }
  return baseOpacity * POLYGON_FILL_OPACITY;
}

/**
 * Async vector-layer registration (job-0139). Fetches the layer's GeoJSON
 * (or FlatGeobuf-converted-to-GeoJSON), adds a `geojson` source, and adds an
 * appropriate paint layer based on geometry kind. Generation-guarded so a
 * remove-before-resolve race terminates cleanly without leaving an orphan
 * source on the map.
 *
 * Why this is exported (`MapView`-local closure would be cleaner): the
 * function captures several refs as parameters so it can be exercised in
 * isolation by unit tests without rendering a full MapView. Sole call site
 * is the apply loop inside MapView's session-state effect.
 *
 * Invariant 1: every coordinate painted on the map comes from `fc.features`
 *  -  we never compute geometry client-side.
 */
export async function addVectorLayer(
  m: MapLibreMap,
  layer: {
    layer_id: string;
    uri: string;
    opacity?: number;
    visible?: boolean;
    style_preset?: string | null;
    /** DATA-DRIVEN LEGEND  -  when present with a `value_field` the polygon fill
     *  is driven generically from it (buildLegendFillExpression); absent => the
     *  legacy flat preset/palette color + Pelicun sentinel path is unchanged. */
    legend?: LegendKey | null;
    /** job-0175: inline GeoJSON FeatureCollection from the agent. When present
     *  the client renders from this directly, bypassing the `uri` fetch path
     *  that would otherwise hit the gs:// guardrail in `fetchVectorAsGeoJson`
     *  (Invariant 5) and silently no-op. */
    inline_geojson?: unknown;
  },
  generation: number,
  fetchGenRef: { current: Map<string, number> },
  geomKindRef: { current: Map<string, VectorGeomKind> },
  addedSourceIdsRef: { current: Set<string> },
): Promise<void> {
  const opacity = layer.opacity ?? 1;
  const visible = layer.visible !== false;

  // Debug-only console.log behind import.meta.env.DEV (matches existing
  // diagnostic-seam pattern). Helps the Playwright capture confirm the
  // vector branch was actually entered.
  if (import.meta.env.DEV) {
    // eslint-disable-next-line no-console
    console.log(`[MapView] addVectorLayer start: ${layer.layer_id} gen=${generation} inline=${layer.inline_geojson !== undefined}`);
  }
  let fc;
  let geomKind: VectorGeomKind;
  // job-0175: prefer inline GeoJSON over URI fetch. The agent populates
  // `inline_geojson` for every vector layer it can read from GCS; falling
  // back to URI is preserved for layers the agent could not inline (failure
  // is logged + the row still appears in the LayerPanel without rendering).
  if (layer.inline_geojson !== undefined && layer.inline_geojson !== null) {
    try {
      const result = vectorResultFromInlineGeoJson(layer.inline_geojson);
      fc = result.featureCollection;
      geomKind = result.geomKind;
      if (import.meta.env.DEV) {
        // eslint-disable-next-line no-console
        console.log(
          `[MapView] addVectorLayer inline-geojson hit: ${layer.layer_id} features=${fc.features.length} kind=${geomKind}`,
        );
      }
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn(`[MapView] inline GeoJSON parse failed for ${layer.layer_id}:`, err);
      if (addedSourceIdsRef.current.has(layer.layer_id)) {
        addedSourceIdsRef.current.delete(layer.layer_id);
      }
      return;
    }
  } else {
    try {
      const result = await fetchVectorAsGeoJson(layer.uri);
      fc = result.featureCollection;
      geomKind = result.geomKind;
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn(`[MapView] vector fetch failed for ${layer.layer_id}:`, err);
      // Release the slot so a future session-state push with the same layer_id
      // can retry.
      if (addedSourceIdsRef.current.has(layer.layer_id)) {
        addedSourceIdsRef.current.delete(layer.layer_id);
      }
      return;
    }
  }

  // Resolve the paint colour now that geomKind is known: an unknown style_preset
  // colours by GEOMETRY FAMILY (line=amber, polygon=slate, point=orange) instead
  // of a per-layer-id hash, so e.g. two rivers from different AOIs read the same.
  const color = resolveVectorColor(layer.layer_id, layer.style_preset, geomKind);

  // FIX 2 (vector AOI clip)  -  drop features that fall OUTSIDE the AOI bbox. The
  // agent fetches NSI points / building footprints with the AOI bbox expanded
  // ~10% (so edge features aren't lost), which without this leaves vectors
  // painting beyond the AOI rectangle the user drew. We clip to the exact AOI
  // bbox stashed on the map instance (getMapAoiBbox). No AOI => no-op pass-through.
  fc = clipFeaturesToBbox(fc, getMapAoiBbox(m));

  // Race-guard: if a remove or re-add happened during the fetch, the
  // generation counter advanced. Bail out cleanly.
  const currentGen = fetchGenRef.current.get(layer.layer_id) ?? -1;
  if (currentGen !== generation) {
    if (import.meta.env.DEV) {
      // eslint-disable-next-line no-console
      console.log(`[MapView] addVectorLayer abort (gen): ${layer.layer_id} expected=${generation} actual=${currentGen}`);
    }
    return;
  }
  // Race-guard: addedSourceIdsRef may have been cleared by removal.
  if (!addedSourceIdsRef.current.has(layer.layer_id)) {
    if (import.meta.env.DEV) {
      // eslint-disable-next-line no-console
      console.log(`[MapView] addVectorLayer abort (removed): ${layer.layer_id}`);
    }
    return;
  }
  // If the map has been torn down (e.g. component unmount during fetch),
  // there's nothing to add to. The MapLibre instance throws on calls after
  // remove(). INCIDENT FIX 2026-06-16: gate on mapStyleReady (a one-time latch)
  // NOT raw isStyleLoaded()  -  a hung sibling raster tile keeps isStyleLoaded()
  // false forever and would block this vector add (which renders from inline
  // GeoJSON and does not even need tiles) indefinitely. Once the style has
  // loaded once, proceed.
  let styleLoaded = false;
  try {
    styleLoaded = mapStyleReady(m);
  } catch {
    return;
  }
  if (!styleLoaded) {
    if (import.meta.env.DEV) {
      // eslint-disable-next-line no-console
      console.log(`[MapView] addVectorLayer defer (style not loaded): ${layer.layer_id}`);
    }
    // The MapLibre style is mid-load  -  typically because a SIBLING vector
    // layer's addSource we just kicked off triggered tile resolution, or
    // the basemap's WMS tiles are still resolving. We must NOT abandon the
    // layer (otherwise a multi-layer Case 1 push only lands the first one).
    // Chain retries via m.once("idle", ...) until either the style settles
    // or the generation guard signals the layer was removed.
    //
    // Why m.once instead of a setTimeout: idle fires exactly when all
    // pending source/tile requests settle, which is the cheapest accurate
    // "ready" signal MapLibre exposes. Each retry guards against runaway
    // chains by capping at MAX_RETRIES.
    const MAX_RETRIES = 20;
    let attempt = 0;
    const retry = () => {
      attempt += 1;
      // Race-recheck guards before touching the map.
      if ((fetchGenRef.current.get(layer.layer_id) ?? -1) !== generation) return;
      if (!addedSourceIdsRef.current.has(layer.layer_id)) return;
      let nowLoaded = false;
      try { nowLoaded = m.isStyleLoaded() ?? false; } catch { return; }
      if (!nowLoaded) {
        if (attempt < MAX_RETRIES) {
          m.once("idle", retry);
        } else if (import.meta.env.DEV) {
          // eslint-disable-next-line no-console
          console.warn(`[MapView] addVectorLayer giving up after ${MAX_RETRIES} retries: ${layer.layer_id}`);
        }
        return;
      }
      if (import.meta.env.DEV) {
        // eslint-disable-next-line no-console
        console.log(`[MapView] addVectorLayer addSource (retry ${attempt}): ${layer.layer_id} kind=${geomKind} features=${fc.features.length}`);
      }
      registerVectorOnMap(m, layer, fc, geomKind, color, opacity, visible, geomKindRef);
    };
    m.once("idle", retry);
    return;
  }
  if (import.meta.env.DEV) {
    // eslint-disable-next-line no-console
    console.log(`[MapView] addVectorLayer addSource (sync): ${layer.layer_id} kind=${geomKind} features=${fc.features.length}`);
  }
  registerVectorOnMap(m, layer, fc, geomKind, color, opacity, visible, geomKindRef);
}

/**
 * Inner registration helper  -  adds a GeoJSON source + the right paint layer
 * to the map. Pure side-effect; no race-guard logic (the caller handles
 * those before invoking).
 *
 * job-0146 additions:
 *   - Pelicun damage polygon path: uses ds_mean choropleth expression (Part 2)
 *   - POLYGON_FILL_OPACITY constant (0.4) for basemap readability (Part 3)
 *   - POLYGON_STROKE_WIDTH constant (1.5px) for polygon edge visibility (Part 3)
 *   - Cluster source for dense point layers >CLUSTER_THRESHOLD features (Part 4)
 */
function registerVectorOnMap(
  m: MapLibreMap,
  // DATA-DRIVEN LEGEND: `legend` is optional + drives the polygon fill generically
  // when it carries a value_field (resolvePolygonFillColor); absent => legacy color.
  layer: { layer_id: string; style_preset?: string | null; legend?: LegendKey | null },
  fc: FeatureCollection,
  geomKind: VectorGeomKind,
  color: string,
  opacity: number,
  visible: boolean,
  geomKindRef: { current: Map<string, VectorGeomKind> },
): void {
  // Add the GeoJSON source. For dense point layers (>CLUSTER_THRESHOLD features),
  // enable MapLibre clustering so thousands of GBIF/iNat/eBird points don't
  // paint as individual overlapping circles at low zoom (Part 4).
  const isPointLayer = geomKind === "point";
  const isDense = isPointLayer && fc.features.length > CLUSTER_THRESHOLD;

  if (isDense) {
    m.addSource(layer.layer_id, {
      type: "geojson",
      data: fc,
      cluster: true,
      clusterRadius: CLUSTER_RADIUS,
      clusterMaxZoom: 14, // clusters disappear above z14 -> individual points show
    });
  } else {
    m.addSource(layer.layer_id, {
      type: "geojson",
      data: fc,
    });
  }

  // Add the paint layer. We place vector overlays at the TOP of the stack
  // (no beforeId), matching the raster-overlay convention. Future enhancement:
  // place beneath labels using a known beforeId (e.g. "waterway-label")
  // when one is detected in the active style.
  if (geomKind === "point") {
    if (isDense) {
      // Cluster circle layer (shows aggregate circles with count text).
      m.addLayer({
        id: `${layer.layer_id}-clusters`,
        type: "circle",
        source: layer.layer_id,
        filter: ["has", "point_count"],
        paint: {
          "circle-radius": [
            "step",
            ["get", "point_count"],
            12, 10,   // < 10 points -> r12
            18, 100,  // 10-99 points -> r18
            24,       // -100 points -> r24
          ],
          "circle-color": color,
          "circle-stroke-color": "#ffffff",
          "circle-stroke-width": 1.5,
          "circle-opacity": opacity * 0.85,
          // FIX 3  -  fade the cluster's white stroke with the fill so the whole
          // symbol dims when the opacity slider moves (matches applyLayerOpacity).
          "circle-stroke-opacity": opacity * 0.85,
        },
        layout: { visibility: visible ? "visible" : "none" },
      });
      // Cluster count label layer.
      m.addLayer({
        id: `${layer.layer_id}-cluster-count`,
        type: "symbol",
        source: layer.layer_id,
        filter: ["has", "point_count"],
        layout: {
          "text-field": "{point_count_abbreviated}",
          "text-size": 11,
          "text-font": ["Open Sans Regular"],
          visibility: visible ? "visible" : "none",
        },
        paint: {
          "text-color": "#ffffff",
        },
      });
      // Individual unclustered points at high zoom.
      m.addLayer({
        id: layer.layer_id,
        type: "circle",
        source: layer.layer_id,
        filter: ["!", ["has", "point_count"]],
        paint: {
          "circle-radius": 5,
          "circle-color": color,
          "circle-stroke-color": "#ffffff",
          "circle-stroke-width": 1,
          "circle-opacity": opacity,
          "circle-stroke-opacity": opacity,
        },
        layout: { visibility: visible ? "visible" : "none" },
      });
    } else {
      m.addLayer({
        id: layer.layer_id,
        type: "circle",
        source: layer.layer_id,
        paint: {
          "circle-radius": 5,
          "circle-color": color,
          "circle-stroke-color": "#ffffff",
          "circle-stroke-width": 1,
          "circle-opacity": opacity,
          "circle-stroke-opacity": opacity,
        },
        layout: { visibility: visible ? "visible" : "none" },
      });
    }
  } else if (geomKind === "line") {
    m.addLayer({
      id: layer.layer_id,
      type: "line",
      source: layer.layer_id,
      paint: {
        "line-color": color,
        // Mesh-grid presets render as a hairline lattice; data lines stay 2px.
        "line-width": resolveVectorLineWidth(layer.style_preset),
        "line-opacity": opacity,
      },
      layout: { visibility: visible ? "visible" : "none" },
    });
  } else if (geomKind === "polygon") {
    // DATA-DRIVEN LEGEND: a layer whose legend names a `value_field` paints its
    // fill GENERICALLY from the legend (categorical match / continuous ramp);
    // the legacy Pelicun sentinel still maps to the ds_mean choropleth; all other
    // polygons take the flat preset/palette `color` (Part 2/3).
    const fillColor = resolvePolygonFillColor(layer.legend, layer.style_preset, color);

    m.addLayer({
      id: layer.layer_id,
      type: "fill",
      source: layer.layer_id,
      paint: {
        // MapLibre fill-color accepts expression arrays natively.
        "fill-color": fillColor as string,
        // Reduced fill opacity (0.4) so basemap labels stay readable underneath
        // polygon fills (Part 3). Mesh-grid (NATE #156) uses a faint MESH_FILL_OPACITY
        // so it reads as a wireframe; a data-driven/graduated legend (and the legacy
        // Pelicun sentinel) paints bold (0.7) so the choropleth reads clearly.
        "fill-opacity": resolvePolygonFillOpacity(opacity, layer.legend, layer.style_preset),
        // Subtle stroke softens the CDP-rectangle look while keeping edges
        // distinguishable (Part 3 / Pelicun "less rectangular" ask).
        "fill-outline-color": color,
      },
      layout: { visibility: visible ? "visible" : "none" },
    });
    // Add a separate line layer for the polygon stroke so we can set stroke
    // width (fill-outline-color only draws 1px; line layer gives us 1.5px).
    // Mesh-grid (NATE #156) gets the hairline so the lattice reads as scaffold.
    m.addLayer({
      id: `${layer.layer_id}-outline`,
      type: "line",
      source: layer.layer_id,
      paint: {
        "line-color": color,
        "line-width": isMeshGridLayer(layer.style_preset)
          ? MESH_LINE_WIDTH
          : POLYGON_STROKE_WIDTH,
        "line-opacity": opacity * 0.6,
      },
      layout: { visibility: visible ? "visible" : "none" },
    });
  } else {
    // Unknown geometry  -  leave the source registered but skip the paint
    // layer. The LayerPanel still shows the row (driven by session-state),
    // and the next style-preset addition can rescue.
    // eslint-disable-next-line no-console
    console.warn(`[MapView] unknown geometry kind for ${layer.layer_id}; skipping paint layer`);
  }

  geomKindRef.current.set(layer.layer_id, geomKind);
}

/**
 * F94  -  register a DENSE vector layer as a MapLibre VECTOR-TILE source + paint
 * layer, so the browser fetches and draws ONLY the tiles in the current
 * viewport instead of one giant inline GeoJSON FeatureCollection (the OSM
 * building-footprint lag NATE reported). This is the agent's PREFERRED dense
 * path; it activates when a wire layer carries `vector_tile_url`.
 *
 * The url is either a `{z}/{x}/{y}.pbf` MVT template (plain MapLibre `vector`
 * source, no extra dependency) or a `pmtiles://...` URL (requires the pmtiles
 * protocol to be registered with MapLibre  -  a follow-on once a serving face
 * exists). Either way the source `type` is `vector`; only the `tiles`/`url`
 * field differs. We reuse the SAME geometry-kind paint styling as the inline
 * path (fill/line/circle) for visual consistency.
 *
 * Pure side-effect; the caller handles race-guards + style-ready gating
 * (same contract as `registerVectorOnMap`).
 */
export function registerVectorTileLayer(
  m: MapLibreMap,
  layer: {
    layer_id: string;
    vector_tile_url: string;
    vector_geom_kind?: string;
    vector_source_layer?: string;
    style_preset?: string | null;
    /** DATA-DRIVEN LEGEND  -  drives the polygon fill generically when it carries
     *  a `value_field` (buildLegendFillExpression); absent => legacy flat color. */
    legend?: LegendKey | null;
    opacity?: number;
    visible?: boolean;
  },
  geomKindRef: { current: Map<string, VectorGeomKind> },
): void {
  const opacity = layer.opacity ?? 1;
  const visible = layer.visible !== false;
  const geomKind = (
    ["point", "line", "polygon"].includes(layer.vector_geom_kind ?? "")
      ? layer.vector_geom_kind
      : "polygon"
  ) as VectorGeomKind;
  const color = resolveVectorColor(layer.layer_id, layer.style_preset, geomKind);
  const sourceLayer = layer.vector_source_layer || "vector";
  const url = layer.vector_tile_url;

  // pmtiles:// URLs are consumed via the pmtiles protocol's `url` field;
  // {z}/{x}/{y} templates are a plain `tiles` array. MapLibre source type is
  // `vector` in both cases.
  const vectorSource: maplibregl.VectorSourceSpecification = url.startsWith(
    "pmtiles://",
  )
    ? { type: "vector", url }
    : { type: "vector", tiles: [url], minzoom: 0, maxzoom: 14 };
  m.addSource(layer.layer_id, vectorSource);

  if (geomKind === "point") {
    m.addLayer({
      id: layer.layer_id,
      type: "circle",
      source: layer.layer_id,
      "source-layer": sourceLayer,
      paint: {
        "circle-radius": 5,
        "circle-color": color,
        "circle-stroke-color": "#ffffff",
        "circle-stroke-width": 1,
        "circle-opacity": opacity,
        "circle-stroke-opacity": opacity,
      },
      layout: { visibility: visible ? "visible" : "none" },
    } as unknown as Parameters<MapLibreMap["addLayer"]>[0]);
  } else if (geomKind === "line") {
    m.addLayer({
      id: layer.layer_id,
      type: "line",
      source: layer.layer_id,
      "source-layer": sourceLayer,
      paint: {
        "line-color": color,
        // Mesh-grid presets render as a hairline lattice; data lines stay 2px.
        "line-width": resolveVectorLineWidth(layer.style_preset),
        "line-opacity": opacity,
      },
      layout: { visibility: visible ? "visible" : "none" },
    } as unknown as Parameters<MapLibreMap["addLayer"]>[0]);
  } else {
    // DATA-DRIVEN LEGEND: drive the fill generically from a legend.value_field
    // when present (categorical / continuous ramp); else the legacy Pelicun
    // sentinel ds_mean choropleth; else the flat color.
    const fillColor = resolvePolygonFillColor(layer.legend, layer.style_preset, color);
    m.addLayer({
      id: layer.layer_id,
      type: "fill",
      source: layer.layer_id,
      "source-layer": sourceLayer,
      paint: {
        "fill-color": fillColor as string,
        // Mesh-grid (NATE #156): faint fill so the lattice reads as a wireframe
        // (cells visible, still clickable); a data-driven/graduated legend (and
        // the legacy Pelicun sentinel) stays bold at 0.7.
        "fill-opacity": resolvePolygonFillOpacity(opacity, layer.legend, layer.style_preset),
        "fill-outline-color": color,
      },
      layout: { visibility: visible ? "visible" : "none" },
    } as unknown as Parameters<MapLibreMap["addLayer"]>[0]);
    m.addLayer({
      id: `${layer.layer_id}-outline`,
      type: "line",
      source: layer.layer_id,
      "source-layer": sourceLayer,
      paint: {
        "line-color": color,
        "line-width": isMeshGridLayer(layer.style_preset)
          ? MESH_LINE_WIDTH
          : POLYGON_STROKE_WIDTH,
        "line-opacity": opacity * 0.6,
      },
      layout: { visibility: visible ? "visible" : "none" },
    } as unknown as Parameters<MapLibreMap["addLayer"]>[0]);
  }

  geomKindRef.current.set(layer.layer_id, geomKind);
  if (import.meta.env.DEV) {
    // eslint-disable-next-line no-console
    console.log(
      `[MapView] registerVectorTileLayer: ${layer.layer_id} kind=${geomKind} url=${url}`,
    );
  }
}

// --- Layer-control application helpers (job-0258) ----------------------- //
//
// ROOT-CAUSE CONTEXT: until job-0258, the LayerPanel's user controls
// (opacity slider / visibility checkbox / drag-reorder) dispatched ONLY to
// the panel's local reducer ("M3 local intent" stubs, LayerPanel.tsx) and
// never reached the MapLibre instance  -  and Map.tsx had no `moveLayer` call
// anywhere, so stack reordering was impossible even for agent-driven
// `set-layer-order` envelopes. These exported helpers are the single shared
// "apply to map" path, used by BOTH the session-state reconciliation loop
// and the map-command subscription that the LayerPanel now feeds through
// the App bus.
//
// One logical GRACE-2 layer (`layer_id`) can own several MapLibre layers:
//   - dense point layers:  `${id}-clusters`, `${id}-cluster-count`, `${id}`
//   - polygon layers:      `${id}`, `${id}-outline`
//   - raster/line/points:  `${id}` only
// (see `registerVectorOnMap` above). Every control operation must address
// the whole group, otherwise outlines/clusters get visually orphaned.

/**
 * Existing MapLibre layer ids belonging to one logical layer, in
 * bottom-to-top paint order (the order `registerVectorOnMap` added them).
 */
export function layerGroupMemberIds(m: MapLibreMap, layerId: string): string[] {
  const candidates = [
    `${layerId}-clusters`,
    `${layerId}-cluster-count`,
    layerId,
    `${layerId}-outline`,
  ];
  return candidates.filter((id) => {
    try {
      return Boolean(m.getLayer(id));
    } catch {
      return false;
    }
  });
}

/**
 * Apply a 0..1 opacity to every paint property of the layer group, using the
 * same per-geometry multipliers `registerVectorOnMap` used at creation time
 * (cluster circles -0.85, polygon fill -POLYGON_FILL_OPACITY or -0.7 for
 * Pelicun damage, outline -0.6). Raster/unknown falls through to
 * `raster-opacity`  -  the original flood-COG path.
 */
export function applyLayerOpacity(
  m: MapLibreMap,
  layerId: string,
  opacity: number,
  geomKind: VectorGeomKind | undefined,
  stylePreset?: string | null,
  // DATA-DRIVEN LEGEND - additive optional arg: a layer with a legend.value_field
  // (graduated/categorical fill) keeps its BOLD 0.7 fill-opacity when the slider
  // moves, exactly like the legacy Pelicun sentinel. Absent => unchanged behavior.
  legend?: LegendKey | null,
): void {
  if (!m.getLayer(layerId)) return;
  if (geomKind === "point") {
    m.setPaintProperty(layerId, "circle-opacity", opacity);
    // FIX 3  -  fade the OUTLINE (white stroke) with the fill so the whole symbol
    // dims, not just the inner circle. The individual-point layer's stroke is
    // wired here AND on the cluster circle below.
    m.setPaintProperty(layerId, "circle-stroke-opacity", opacity);
    if (m.getLayer(`${layerId}-clusters`)) {
      m.setPaintProperty(`${layerId}-clusters`, "circle-opacity", opacity * 0.85);
      // FIX 3  -  the cluster circle carries a white stroke too; fade it in step.
      m.setPaintProperty(`${layerId}-clusters`, "circle-stroke-opacity", opacity * 0.85);
    }
    if (m.getLayer(`${layerId}-cluster-count`)) {
      m.setPaintProperty(`${layerId}-cluster-count`, "text-opacity", opacity);
    }
  } else if (geomKind === "line") {
    m.setPaintProperty(layerId, "line-opacity", opacity);
  } else if (geomKind === "polygon") {
    // Mesh-grid (NATE #156) keeps the faint wireframe fill when the LayerPanel
    // opacity slider moves; a data-driven/graduated legend (and the legacy Pelicun
    // sentinel) stays bold at 0.7; others use the default. Shared rule with the
    // registration sites so creation + slider stay in lockstep.
    const polyOpacity = resolvePolygonFillOpacity(opacity, legend, stylePreset);
    m.setPaintProperty(layerId, "fill-opacity", polyOpacity);
    m.setPaintProperty(layerId, "fill-outline-color", resolveVectorColor(layerId, stylePreset, geomKind));
    if (m.getLayer(`${layerId}-outline`)) {
      m.setPaintProperty(`${layerId}-outline`, "line-opacity", opacity * 0.6);
    }
  } else {
    // Raster or unknown  -  preserve the raster path so the flood-depth COG
    // keeps responding (the original demo symptom).
    m.setPaintProperty(layerId, "raster-opacity", opacity);
  }
}

/** Flip layout visibility on every member of the layer group. */
export function applyLayerVisibility(
  m: MapLibreMap,
  layerId: string,
  visible: boolean,
): void {
  for (const id of layerGroupMemberIds(m, layerId)) {
    m.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
  }
}

/**
 * Re-stack overlay layer groups to match `layerIdsTopFirst` (the LayerPanel /
 * `set-layer-order` convention: first element renders ON TOP). MapLibre's
 * `moveLayer(id)` with no beforeId moves a layer to the top of the stack, so
 * iterating bottom-first pulls each group to the top in turn  -  the last
 * (top-of-panel) group ends up painted last, i.e. on top. Basemap layers are
 * never named in the command, so they stay at the bottom. Group members move
 * in their internal bottom-to-top order so sublayers keep their relative
 * stacking (e.g. cluster counts above cluster circles).
 */
/**
 * BUG 1 (secondary): ORDER-SENSITIVE structural equality for the legend list -
 * compares two ordered ProjectLayerSummary arrays by (layer_id + z_index) at each
 * position. Used to short-circuit setLegendLayers on an unchanged ~12-25s
 * heartbeat so the MapView subtree stops re-rendering forever. ORDER-SENSITIVE by
 * design (per bug 2: the order is now a deterministic function of the set, so any
 * real reorder is a genuine change worth committing). Exported for unit testing.
 */
export function legendOrderEqual(
  a: ProjectLayerSummary[],
  b: ProjectLayerSummary[],
): boolean {
  if (a === b) return true;
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    const x = a[i];
    const y = b[i];
    if (!x || !y) return false;
    if (x.layer_id !== y.layer_id) return false;
    if ((x.z_index ?? 0) !== (y.z_index ?? 0)) return false;
  }
  return true;
}

export function applyLayerOrder(m: MapLibreMap, layerIdsTopFirst: string[]): void {
  const bottomFirst = [...layerIdsTopFirst].reverse();
  for (const layerId of bottomFirst) {
    for (const member of layerGroupMemberIds(m, layerId)) {
      try {
        m.moveLayer(member);
      } catch {
        // Mid-removal race (style mutation between getLayer and moveLayer)  - 
        // skip; the next session-state reconciliation restores consistency.
      }
    }
  }
}

/**
 * job-0294  -  draw (or replace) the single "analysis extent" rectangle for a
 * bbox `[minLon, minLat, maxLon, maxLat]`. Idempotent: the first call adds the
 * GeoJSON source + a faint fill layer + a dashed accent outline; subsequent
 * calls call `setData` so the extent REPLACES (one extent at a time, v0.1).
 *
 * The bbox comes from the same `zoom-to` map-command the camera consumes, so no
 * agent change is needed; case-reopen replays the last zoom-to (App.tsx) and
 * redraws the rectangle for free. Pure rendering  -  no numbers are computed
 * (Invariant 1): the geometry is built verbatim from the received bbox corners.
 */
export function drawAnalysisExtent(
  m: MapLibreMap,
  bbox: [number, number, number, number],
  // NATE 2026-06-22 (item 4): paint the AOI box PURPLE when a sim is running,
  // blue otherwise. Optional (default blue) so the existing 2-arg call sites +
  // tests are byte-preserved; the live redraw paths pass the current sim flag.
  simRunning = false,
): void {
  const [minLon, minLat, maxLon, maxLat] = bbox;
  const ring: [number, number][] = [
    [minLon, minLat],
    [maxLon, minLat],
    [maxLon, maxLat],
    [minLon, maxLat],
    [minLon, minLat],
  ];
  const data: Feature<Polygon> = {
    type: "Feature",
    properties: {},
    geometry: { type: "Polygon", coordinates: [ring] },
  };

  // AWS-migration hardening (bbox track): make this idempotent AND
  // partial-state tolerant. A prior call that threw mid-mutation (the live
  // failure mode  -  addSource succeeded but an addLayer threw, or the camera
  // animation churned the style between the two addLayer calls) can leave the
  // source present but one/both layers missing. The old code early-returned
  // the moment the source existed, so a half-built extent never self-healed
  // and the dashed rectangle was permanently absent. Now: (1) swap data on the
  // existing source, then (2) re-add ANY missing layer; on a clean first call
  // both source and layers are added. Each add is existence-guarded so a
  // duplicate-id throw cannot abort the function.
  const existing = m.getSource(ANALYSIS_EXTENT_SOURCE_ID) as
    | maplibregl.GeoJSONSource
    | undefined;
  if (existing) {
    // Replace-on-new-bbox: swap the data; layers (re-)asserted below.
    existing.setData(data);
  } else {
    m.addSource(ANALYSIS_EXTENT_SOURCE_ID, { type: "geojson", data });
  }

  // job-0321 (F40)  -  OUTLINE-ONLY AOI. The AOI rectangle previously painted a
  // translucent fill (#4D96FF @ 0.06) over the whole extent, which tinted every
  // layer rendered beneath it (the user-reported "blue wash over my layers").
  // We now draw the dashed outline ONLY  -  no fill layer is added. The fill
  // LAYER ID constant + the clearAnalysisExtent() removal guard are KEPT intact
  // so a stale fill left over from a previous app version / partial style still
  // gets torn down cleanly (idempotent, partial-state tolerant).
  //
  // Thin dashed accent outline  -  the primary "here's the measured extent" cue.
  if (!m.getLayer(ANALYSIS_EXTENT_LINE_LAYER_ID)) {
    m.addLayer({
      id: ANALYSIS_EXTENT_LINE_LAYER_ID,
      type: "line",
      source: ANALYSIS_EXTENT_SOURCE_ID,
      paint: {
        "line-color": simRunning
          ? ANALYSIS_EXTENT_COLOR_SIM
          : ANALYSIS_EXTENT_COLOR_IDLE,
        "line-width": 1.5,
        "line-dasharray": [3, 2],
        "line-opacity": 0.9,
      },
    });
  } else {
    // The layer already exists (replace-on-new-bbox / redraw): re-assert the
    // stroke color for the current sim state so a redraw during a sim stays
    // purple (and an idle redraw stays blue). Idempotent + cheap.
    setAnalysisExtentSimColor(m, simRunning);
  }
}

/**
 * NATE 2026-06-22 (item 4): recolor the SINGLE analysis-extent AOI rectangle by
 * sim state - purple while a sim runs, blue otherwise. Mutates the existing line
 * layer's stroke in place (no second box). Missing-layer / torn-down tolerant.
 */
export function setAnalysisExtentSimColor(
  m: MapLibreMap,
  simRunning: boolean,
): void {
  try {
    if (!m.getLayer(ANALYSIS_EXTENT_LINE_LAYER_ID)) return;
    m.setPaintProperty(
      ANALYSIS_EXTENT_LINE_LAYER_ID,
      "line-color",
      simRunning ? ANALYSIS_EXTENT_COLOR_SIM : ANALYSIS_EXTENT_COLOR_IDLE,
    );
  } catch {
    /* map torn down / style swapped mid-mutation - non-fatal */
  }
}

/**
 * ux-batch-1 (F14)  -  remove the analysis-extent rectangle (fill + outline +
 * source). Inverse of drawAnalysisExtent. Idempotent and partial-state
 * tolerant: each removal is existence-guarded so a half-built extent (source
 * present, a layer missing) still clears cleanly and a missing extent is a
 * no-op. Layers must be removed before their source (MapLibre rejects removing
 * a source still referenced by a layer).
 */
export function clearAnalysisExtent(m: MapLibreMap): void {
  if (m.getLayer(ANALYSIS_EXTENT_FILL_LAYER_ID)) {
    m.removeLayer(ANALYSIS_EXTENT_FILL_LAYER_ID);
  }
  if (m.getLayer(ANALYSIS_EXTENT_LINE_LAYER_ID)) {
    m.removeLayer(ANALYSIS_EXTENT_LINE_LAYER_ID);
  }
  if (m.getSource(ANALYSIS_EXTENT_SOURCE_ID)) {
    m.removeSource(ANALYSIS_EXTENT_SOURCE_ID);
  }
}

// --- Region-disambiguation choropleth (state-bbox-fallback narrowing) ----- //
//
// When a `geocode_location` result snaps to a whole-state bbox, the agent
// offers a narrower county pick (region-choice-request). The candidate counties
// render as a tappable CHOROPLETH on the map, SYNCED with the in-chat
// RegionPickerCard list via the region-choice bus: hovering/selecting a region
// in either surface highlights its polygon, and tapping a polygon picks it
// (same reply path as clicking the card row). Each candidate carries an
// EPSG:4326 bbox (`RegionCandidate.bbox`); we draw one rectangle polygon per
// candidate keyed by `region_id`. Invariant 1: the geometry is built verbatim
// from the received candidate bboxes  -  no geography is computed.
//
// Reuses the same MapLibre GeoJSON fill+line vector pattern the analysis-extent
// rectangle / vector layers use (Invariant 4: the client just registers
// sources/layers). Per-feature highlight is driven by `feature-state`
// (hovered / selected) set on the source so a hover repaints only the touched
// polygon without re-issuing the whole FeatureCollection.

export const REGION_CHOICE_SOURCE_ID = "grace2-region-choice";
export const REGION_CHOICE_FILL_LAYER_ID = "grace2-region-choice-fill";
export const REGION_CHOICE_LINE_LAYER_ID = "grace2-region-choice-line";

const REGION_ACCENT = "#3b82f6"; // matches RegionPickerCard ACCENT (blue)

/**
 * Build the candidate county choropleth FeatureCollection from the request's
 * candidates. One rectangle Polygon per candidate, keyed by `region_id` as the
 * feature id (so `setFeatureState` can target it) AND in `properties.region_id`
 * + `properties.name` (so a tap hit-test reads them back). Pure  -  exported for
 * unit testing.
 */
export function buildRegionChoiceGeoJson(
  candidates: RegionCandidate[],
): FeatureCollection<Polygon> {
  const features: Feature<Polygon>[] = candidates.map((c) => {
    const [minLon, minLat, maxLon, maxLat] = c.bbox;
    const ring: [number, number][] = [
      [minLon, minLat],
      [maxLon, minLat],
      [maxLon, maxLat],
      [minLon, maxLat],
      [minLon, minLat],
    ];
    return {
      type: "Feature",
      id: c.region_id,
      properties: { region_id: c.region_id, name: c.name },
      geometry: { type: "Polygon", coordinates: [ring] },
    };
  });
  return { type: "FeatureCollection", features };
}

/**
 * Render (or update) the candidate county choropleth from a region-choice
 * request. Idempotent + partial-state tolerant (mirrors drawAnalysisExtent):
 * swaps the data on an existing source, re-adds any missing layer. The fill
 * uses a feature-state-driven opacity ramp (selected > hovered > base) so the
 * highlighted county pops without re-issuing the data; the line gives the
 * county outline a crisp edge.
 */
export function drawRegionChoropleth(
  m: MapLibreMap,
  candidates: RegionCandidate[],
): void {
  const data = buildRegionChoiceGeoJson(candidates);
  const existing = m.getSource(REGION_CHOICE_SOURCE_ID) as
    | maplibregl.GeoJSONSource
    | undefined;
  if (existing) {
    existing.setData(data);
  } else {
    m.addSource(REGION_CHOICE_SOURCE_ID, {
      type: "geojson",
      data,
      // promoteId so the candidate's region_id is the canonical feature id
      // feature-state targets (a GeoJSON source feature id must be set this way
      // to be addressable by setFeatureState across data swaps).
      promoteId: "region_id",
    });
  }

  if (!m.getLayer(REGION_CHOICE_FILL_LAYER_ID)) {
    m.addLayer({
      id: REGION_CHOICE_FILL_LAYER_ID,
      type: "fill",
      source: REGION_CHOICE_SOURCE_ID,
      paint: {
        "fill-color": REGION_ACCENT,
        // selected (0.42) > hovered (0.30) > base (0.12)  -  the highlighted
        // county reads as the focus while the rest stay tappable hints.
        "fill-opacity": [
          "case",
          ["boolean", ["feature-state", "selected"], false],
          0.42,
          ["boolean", ["feature-state", "hovered"], false],
          0.3,
          0.12,
        ],
      },
    });
  }
  if (!m.getLayer(REGION_CHOICE_LINE_LAYER_ID)) {
    m.addLayer({
      id: REGION_CHOICE_LINE_LAYER_ID,
      type: "line",
      source: REGION_CHOICE_SOURCE_ID,
      paint: {
        "line-color": REGION_ACCENT,
        "line-width": [
          "case",
          ["boolean", ["feature-state", "selected"], false],
          2.5,
          ["boolean", ["feature-state", "hovered"], false],
          2,
          1,
        ],
        "line-opacity": 0.9,
      },
    });
  }
}

/**
 * Remove the candidate county choropleth (fill + line + source). Inverse of
 * drawRegionChoropleth. Idempotent + partial-state tolerant; layers removed
 * before their source (MapLibre rejects removing a referenced source).
 */
export function clearRegionChoropleth(m: MapLibreMap): void {
  if (m.getLayer(REGION_CHOICE_FILL_LAYER_ID)) {
    m.removeLayer(REGION_CHOICE_FILL_LAYER_ID);
  }
  if (m.getLayer(REGION_CHOICE_LINE_LAYER_ID)) {
    m.removeLayer(REGION_CHOICE_LINE_LAYER_ID);
  }
  if (m.getSource(REGION_CHOICE_SOURCE_ID)) {
    m.removeSource(REGION_CHOICE_SOURCE_ID);
  }
}

/**
 * Apply the bus-synced hover + selection to the choropleth's feature-state.
 * `prevIds` is the set of region_ids that currently carry a non-default state
 * (so we can clear stale highlights without enumerating every candidate). Pure
 * side effect on the map; returns the new set of region_ids carrying state for
 * the next diff. No-op-safe when the source is absent (mid-teardown).
 */
export function applyRegionChoiceHighlight(
  m: MapLibreMap,
  hoveredId: string | null,
  selectedId: string | null,
  prevIds: Set<string>,
): Set<string> {
  if (!m.getSource(REGION_CHOICE_SOURCE_ID)) return new Set();
  const nextIds = new Set<string>();
  if (hoveredId) nextIds.add(hoveredId);
  if (selectedId) nextIds.add(selectedId);
  // Clear any region that was highlighted but no longer is.
  for (const id of prevIds) {
    if (nextIds.has(id)) continue;
    try {
      m.setFeatureState(
        { source: REGION_CHOICE_SOURCE_ID, id },
        { hovered: false, selected: false },
      );
    } catch {
      /* feature gone mid-swap  -  ignore */
    }
  }
  // Apply current state to the touched regions.
  for (const id of nextIds) {
    try {
      m.setFeatureState(
        { source: REGION_CHOICE_SOURCE_ID, id },
        { hovered: hoveredId === id, selected: selectedId === id },
      );
    } catch {
      /* feature gone mid-swap  -  ignore */
    }
  }
  return nextIds;
}

// --- FIX 1 (NATE 2026-06-17)  -  generic whole-feature tap HIGHLIGHT --------- //
//
// Tapping a vector feature opens FeaturePopup but used to leave the feature
// unmarked, so the user couldn't tell WHICH polygon/line/point they hit. We now
// outline the ENTIRE tapped geometry, GENERICALLY across every vector overlay
// type and geometry, with ONE dedicated highlight source + three paint layers:
//   - a fill layer    -> paints the interior of a tapped POLYGON
//   - a line layer    -> paints a thick stroke for a tapped LINE *and* the
//                       boundary of a tapped polygon (MapLibre's line layer
//                       renders polygon rings too), so one layer covers both
//   - a circle layer  -> paints an enlarged ring for a tapped POINT
// MapLibre only paints the geometry kinds each layer type understands, so a
// single highlight source carrying ONE feature lights up exactly the right
// layer(s) regardless of geometry  -  no per-overlay-type branching.
//
// The highlight lives in MAP space (a geojson source), so it pans with the map
// and scales with zoom for free (FIX 1 acceptance). It is cleared when the popup
// closes (X / Esc / a no-hit tap) and REPLACED when another feature is tapped.
//
// Invariant 1: the highlight geometry is CLONED verbatim from the tapped
// feature's own geometry  -  no geography is computed client-side.

export const FEATURE_HIGHLIGHT_SOURCE_ID = "grace2-feature-highlight";
export const FEATURE_HIGHLIGHT_FILL_LAYER_ID = "grace2-feature-highlight-fill";
export const FEATURE_HIGHLIGHT_LINE_LAYER_ID = "grace2-feature-highlight-line";
export const FEATURE_HIGHLIGHT_CIRCLE_LAYER_ID = "grace2-feature-highlight-circle";

// A warm accent distinct from the blue AOI outline / region choropleth so the
// highlight reads as "this is the thing you tapped".
const HIGHLIGHT_ACCENT = "#facc15"; // amber-400

/**
 * Build a single-feature FeatureCollection from a tapped feature's geometry.
 * The geometry is CLONED (structuredClone / JSON round-trip) so a later
 * setData / source teardown can never mutate MapLibre's own feature objects.
 * Returns an empty FeatureCollection when the geometry is absent (defensive  - 
 * a hit with no geometry simply clears the highlight). Pure  -  exported for unit
 * testing without a live map.
 */
export function buildHighlightGeoJson(
  geometry: Geometry | null | undefined,
): FeatureCollection {
  if (!geometry) return { type: "FeatureCollection", features: [] };
  let cloned: Geometry;
  try {
    cloned =
      typeof structuredClone === "function"
        ? (structuredClone(geometry) as Geometry)
        : (JSON.parse(JSON.stringify(geometry)) as Geometry);
  } catch {
    return { type: "FeatureCollection", features: [] };
  }
  return {
    type: "FeatureCollection",
    features: [{ type: "Feature", properties: {}, geometry: cloned }],
  };
}

/**
 * Set (or replace) the generic feature highlight to the given geometry.
 * Idempotent + partial-state tolerant (mirrors drawAnalysisExtent): the first
 * call adds the source + the fill/line/circle paint layers; subsequent calls
 * swap the data on the existing source and re-add any layer that went missing.
 * The three layers are ALWAYS present so the SAME highlight source lights up the
 * correct one(s) for whatever geometry it currently holds (polygon -> fill+line,
 * line -> line, point -> circle). Pure side-effect on the map (Invariant 4).
 */
export function setFeatureHighlight(
  m: MapLibreMap,
  geometry: Geometry | null | undefined,
): void {
  const data = buildHighlightGeoJson(geometry);
  const existing = m.getSource(FEATURE_HIGHLIGHT_SOURCE_ID) as
    | maplibregl.GeoJSONSource
    | undefined;
  if (existing) {
    existing.setData(data);
  } else {
    m.addSource(FEATURE_HIGHLIGHT_SOURCE_ID, { type: "geojson", data });
  }

  // Polygon interior  -  a faint amber wash so the tapped polygon reads as filled.
  if (!m.getLayer(FEATURE_HIGHLIGHT_FILL_LAYER_ID)) {
    m.addLayer({
      id: FEATURE_HIGHLIGHT_FILL_LAYER_ID,
      type: "fill",
      source: FEATURE_HIGHLIGHT_SOURCE_ID,
      paint: {
        "fill-color": HIGHLIGHT_ACCENT,
        "fill-opacity": 0.25,
      },
    });
  }
  // Bold outline for polygons AND a thick stroke for lines (roads / rivers).
  if (!m.getLayer(FEATURE_HIGHLIGHT_LINE_LAYER_ID)) {
    m.addLayer({
      id: FEATURE_HIGHLIGHT_LINE_LAYER_ID,
      type: "line",
      source: FEATURE_HIGHLIGHT_SOURCE_ID,
      paint: {
        "line-color": HIGHLIGHT_ACCENT,
        "line-width": 4,
        "line-opacity": 0.95,
      },
    });
  }
  // Enlarged ring for a tapped point. FOOTPRINT HIGHLIGHT DOT FILTER (NATE
  // 2026-06-28): gate the circle to POINT geometry only. Without this filter
  // MapLibre paints the ring at EVERY POLYGON VERTEX of a footprint highlight
  // (the stray dots around the selected building); a genuine point tap
  // (gauge/occurrence/NSI) still keeps its ring. Same idiom as bbox_draw.ts /
  // SpatialDrawSurface.tsx.
  if (!m.getLayer(FEATURE_HIGHLIGHT_CIRCLE_LAYER_ID)) {
    m.addLayer({
      id: FEATURE_HIGHLIGHT_CIRCLE_LAYER_ID,
      type: "circle",
      source: FEATURE_HIGHLIGHT_SOURCE_ID,
      filter: ["==", ["geometry-type"], "Point"],
      paint: {
        "circle-radius": 10,
        "circle-color": "rgba(0,0,0,0)", // ring only  -  don't blanket the point
        "circle-stroke-color": HIGHLIGHT_ACCENT,
        "circle-stroke-width": 3,
        "circle-stroke-opacity": 0.95,
      },
    });
  }
}

/**
 * Remove the generic feature highlight (all three layers + source). Inverse of
 * setFeatureHighlight; idempotent + partial-state tolerant. Layers removed
 * before the source (MapLibre rejects removing a referenced source). Used when
 * the popup is dismissed and on map teardown.
 */
export function clearFeatureHighlight(m: MapLibreMap): void {
  for (const id of [
    FEATURE_HIGHLIGHT_FILL_LAYER_ID,
    FEATURE_HIGHLIGHT_LINE_LAYER_ID,
    FEATURE_HIGHLIGHT_CIRCLE_LAYER_ID,
  ]) {
    try {
      if (m.getLayer(id)) m.removeLayer(id);
    } catch {
      /* mid-removal race  -  best effort */
    }
  }
  try {
    if (m.getSource(FEATURE_HIGHLIGHT_SOURCE_ID)) {
      m.removeSource(FEATURE_HIGHLIGHT_SOURCE_ID);
    }
  } catch {
    /* still referenced / gone  -  next clear retries */
  }
}

// job-0321 (F43)  -  legend bottom-edge anchor geometry.
//
// RETAINED FOR TESTS / as a standalone helper. The LIVE legend path no longer
// calls this: Map.tsx now derives the legend anchor from the full projected AOI
// rectangle (computeBboxScreenRect) in one pass. This helper is kept because its
// unit tests (Map.test.tsx) still exercise the bottom-edge-midpoint projection
// and it is a clean, pure primitive; it has no other production caller.
//
// It projects the two bottom corners of the bbox to screen space and returns the
// bottom-edge MIDPOINT (anchor x) at the LOWEST (max-y) of the two projected
// corners (anchor y)  -  the bbox can be slightly rotated on screen by the
// Web-Mercator projection at the poles, so we take the lower of the two so a
// legend hung here would always clear the box.
//
// Returns null when the bbox is off-screen (the midpoint falls outside the map
// canvas). Pure  -  every number comes from MapLibre's project() (Invariant 1:
// the client renders, it never computes geography).
export interface LegendAnchor {
  left: number;
  top: number;
}
export function computeBboxBottomAnchor(
  m: MapLibreMap,
  bbox: [number, number, number, number],
): LegendAnchor | null {
  // Only the bottom edge ([minLat]) is needed  -  maxLat is intentionally unused.
  const [minLon, minLat, maxLon] = bbox;
  let bl: { x: number; y: number };
  let br: { x: number; y: number };
  try {
    bl = m.project([minLon, minLat]);
    br = m.project([maxLon, minLat]);
  } catch {
    return null;
  }
  const left = (bl.x + br.x) / 2;
  // Anchor at the lower of the two projected bottom corners so the legend
  // clears the box edge regardless of slight projection skew.
  const top = Math.max(bl.y, br.y);

  // Off-screen test: if the anchor midpoint is outside the visible canvas,
  // signal the caller to fall back to bottom-center (legend never vanishes).
  let size: { x: number; y: number } | null = null;
  try {
    const c = m.getCanvas();
    if (c) size = { x: c.clientWidth, y: c.clientHeight };
  } catch {
    size = null;
  }
  if (size) {
    if (left < 0 || left > size.x || top < 0 || top > size.y) return null;
  }
  return { left, top };
}

// --- FIX 4 (NATE 2026-06-17)  -  legend WIDTH sized to the AOI bbox on-screen -- //
//
// RETAINED FOR TESTS / as a standalone helper. The LIVE legend path no longer
// calls this either: legendBarWidth is now derived as (right-left) of the full
// projected rect (computeBboxScreenRect) in the same pass. Kept because its unit
// tests (Map.featureInspect.test.tsx) exercise the bottom-edge width projection
// + clamps; it has no other production caller.
//
// The colorbar was a static 320px. This sizes its width to the AOI bbox's
// ON-SCREEN east-west extent: project the bbox's two bottom corners and take the
// horizontal pixel distance between them. That makes the colorbar SPAN the box
// and SHRINK as you zoom out (the bbox gets smaller on screen), reading as the
// physical key for that AOI. Clamped to a sane min (so it never becomes an
// illegible sliver) and to the viewport width minus margins (so it never
// overflows). Returns null when the bbox can't be projected (off-screen / no
// canvas). Pure  -  every number comes from MapLibre's project() (Invariant 1: the
// client renders, never computes geography).
export const LEGEND_MIN_WIDTH_PX = 160;
export const LEGEND_VIEWPORT_MARGIN_PX = 24; // px kept clear on each side.

// MOBILE-ONLY HUD (NATE 2026-06-27) - thresholds for the `aoiCornerPlaceable`
// signal (Map -> LayerLegend, mobile path only). They decide when a corner
// attach to the AOI box stops being useful so the mobile legend docks above the
// chat instead. Conservative on purpose: the normal AOI stays corner-placeable.
//   - AOI_CORNER_MIN_EXTENT_PX: when the SMALLER on-screen AOI extent is <= this,
//     the box is a tiny dot (smaller than one legend key row, KEY_HEIGHT_FLAT=56)
//     so a corner snap is meaningless -> not placeable.
//   - AOI_CORNER_FILL_FRACTION: when the projected box spans >= this fraction of
//     BOTH canvas axes the AOI fills the viewport (every edge off-screen) so there
//     is no usable on-screen corner left -> not placeable. Requiring BOTH axes
//     keeps a wide-but-short / tall-but-narrow AOI placeable.
export const AOI_CORNER_MIN_EXTENT_PX = 24;
export const AOI_CORNER_FILL_FRACTION = 0.92;

// ZOOM-OUT HIDE (NATE 2026-06-27, mobile-only) - a DISTINCT "AOI is a tiny dot on
// screen" threshold for HIDING the scrubber + legend entirely (NOT the
// corner-placeable / viewport-fill decision). When the user zooms OUT far enough
// that the bbox's SMALLER on-screen extent shrinks below this, the AOI reads as a
// speck and the overlays add nothing but clutter, so both render null. Set a touch
// above AOI_CORNER_MIN_EXTENT_PX so the hide engages in the same zoomed-out-tiny
// neighborhood the corner-placeable check already flips on (24px), giving a small,
// honest dot-band where the legend has already band-docked before it disappears.
// This is purely the TINY-dot case: a viewport-FILLING AOI (zoomed in) is huge on
// both axes and never trips this, and an ABSENT bbox keeps today's behavior.
export const AOI_MIN_VISIBLE_EXTENT_PX = 36;

export function computeBboxScreenWidth(
  m: MapLibreMap,
  bbox: [number, number, number, number],
): number | null {
  // Width spans the bottom edge ([minLat]); the east-west corners are enough.
  const [minLon, minLat, maxLon] = bbox;
  let bl: { x: number; y: number };
  let br: { x: number; y: number };
  try {
    bl = m.project([minLon, minLat]);
    br = m.project([maxLon, minLat]);
  } catch {
    return null;
  }
  const raw = Math.abs(br.x - bl.x);
  if (!Number.isFinite(raw) || raw <= 0) return null;

  // Clamp: min so it stays legible; max so it can never overflow the viewport.
  let maxWidth = Number.POSITIVE_INFINITY;
  try {
    const c = m.getCanvas();
    if (c && c.clientWidth) maxWidth = c.clientWidth - LEGEND_VIEWPORT_MARGIN_PX * 2;
  } catch {
    maxWidth = Number.POSITIVE_INFINITY;
  }
  // Guard a degenerate (tiny) canvas so the max clamp can't drop below the min.
  if (maxWidth < LEGEND_MIN_WIDTH_PX) maxWidth = LEGEND_MIN_WIDTH_PX;
  return Math.max(LEGEND_MIN_WIDTH_PX, Math.min(raw, maxWidth));
}

// --- FIX 4 (legend EDGE-RAIL snap)  -  full projected AOI screen rectangle ----- //
//
// The legend overlay used to snap only to the AOI bbox bottom-edge CENTER
// (computeBboxBottomAnchor returns the bottom-edge midpoint). NATE's overlay
// spec wants the gradient colorbar to EDGE-RAIL snap to the nearest AOI side and
// slide ALONG it, placeable anywhere around the AOI perimeter. Edge-rail
// snapping needs the FULL projected AOI rectangle (all four edges), not just the
// bottom midpoint  -  given only anchor+width the legend's fallback estimator
// (legend_snap.rectFromAnchorAndWidth) has to GUESS the height as square, which
// makes top/left snapping imprecise for non-square or skewed AOIs.
//
// This helper projects ALL FOUR bbox corners to screen space and returns a
// {left, top, right, bottom} ScreenRect covering the box's true on-screen extent
// (min/max over the projected corners, so it reflects the real AOI aspect ratio
// + on-screen skew even when Web-Mercator skews the box at high latitude). That
// rectangle is threaded straight into LayerLegend (via the `aoiRect` prop) and
// used directly as the snap geometry: legend_snap.layoutKeysCcw rails the
// colorbar keys CCW along its four edges. Returns null when the box can't be
// projected / is off-screen so the legend falls back to its bottom-center stack
// and never vanishes. Pure  -  every number comes from MapLibre's project()
// (Invariant 1).
export interface LegendScreenRect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}
export function computeBboxScreenRect(
  m: MapLibreMap,
  bbox: [number, number, number, number],
): LegendScreenRect | null {
  const [minLon, minLat, maxLon, maxLat] = bbox;
  let p: { x: number; y: number }[];
  try {
    p = [
      m.project([minLon, minLat]), // SW
      m.project([maxLon, minLat]), // SE
      m.project([maxLon, maxLat]), // NE
      m.project([minLon, maxLat]), // NW
    ];
  } catch {
    return null;
  }
  let left = Infinity;
  let top = Infinity;
  let right = -Infinity;
  let bottom = -Infinity;
  for (const { x, y } of p) {
    if (x < left) left = x;
    if (x > right) right = x;
    if (y < top) top = y;
    if (y > bottom) bottom = y;
  }
  if (
    !Number.isFinite(left) ||
    !Number.isFinite(top) ||
    !Number.isFinite(right) ||
    !Number.isFinite(bottom) ||
    right <= left ||
    bottom <= top
  ) {
    return null;
  }
  // Off-screen test: drop the rect when its center falls outside the canvas, so
  // the legend reverts to the bottom-center fallback (never vanishes).
  try {
    const c = m.getCanvas();
    if (c) {
      const cx = (left + right) / 2;
      const cy = (top + bottom) / 2;
      if (cx < 0 || cx > c.clientWidth || cy < 0 || cy > c.clientHeight) {
        return null;
      }
    }
  } catch {
    /* no canvas in test env  -  return the rect as-is */
  }
  return { left, top, right, bottom };
}

// MOBILE-ONLY HUD (NATE 2026-06-27) - is the projected AOI rectangle usefully
// on-screen for a CORNER attach, or has the user zoomed/panned so far that a
// corner snap is no longer useful (so the mobile legend should dock above the
// chat instead of clinging to a speck / a fill-the-screen box / nothing)? Pure +
// directly unit-testable; the live caller (the legendRect recompute effect)
// passes the freshly-projected `rect` (from computeBboxScreenRect, which already
// returns null when the bbox center is off-canvas) plus the live canvas size.
//
// Deliberately CONSERVATIVE - true in the normal case so the existing
// corner-attach behavior is preserved; false only in the clearly-too-zoomed
// cases:
//   - rect == null  -> false. computeBboxScreenRect already returns null when the
//     bbox CENTER is off-canvas (zoomed/panned away), so there is no on-screen AOI
//     to corner-attach to.
//   - too-small (a dot) -> false. The SMALLER on-screen AOI extent is
//     <= AOI_CORNER_MIN_EXTENT_PX: the box is a tiny dot (smaller than one legend
//     key row), so a corner attach is meaningless.
//   - too-large (fills the viewport) -> false. The box spans
//     >= AOI_CORNER_FILL_FRACTION of BOTH canvas axes: every AOI edge runs
//     off-screen, so there is no usable on-screen AOI corner. Requiring BOTH axes
//     keeps a wide-but-short / tall-but-narrow AOI placeable (it still has
//     on-screen edges to snap to). Only judged when the canvas dims are known
//     (> 0); without them (test env) we cannot tell, so we do NOT mark too-large.
//   - otherwise -> true (the normal corner-attach case).
export function aoiRectCornerPlaceable(
  rect: LegendScreenRect | null | undefined,
  canvasW: number,
  canvasH: number,
): boolean {
  if (!rect) return false;
  const w = Math.abs(rect.right - rect.left);
  const h = Math.abs(rect.bottom - rect.top);
  if (Math.min(w, h) <= AOI_CORNER_MIN_EXTENT_PX) return false;
  const haveCanvas = canvasW > 0 && canvasH > 0;
  if (
    haveCanvas &&
    w >= canvasW * AOI_CORNER_FILL_FRACTION &&
    h >= canvasH * AOI_CORNER_FILL_FRACTION
  ) {
    return false;
  }
  return true;
}

// ZOOM-OUT HIDE (NATE 2026-06-27, mobile-only) - is the AOI bbox a tiny DOT on
// screen (zoomed OUT far) such that BOTH the scrubber AND the legend should be
// HIDDEN entirely? True ONLY when an AOI rect IS present (a bbox is projected) AND
// its SMALLER on-screen extent is below AOI_MIN_VISIBLE_EXTENT_PX. Distinct from
// aoiRectCornerPlaceable (which conflates the tiny-dot case with the
// viewport-filling case): this is the zoomed-OUT speck case ONLY -- a
// viewport-filling AOI is large on both axes and never trips this, and an ABSENT
// rect returns false so today's no-bbox behavior (the band-dock / static fallback)
// is preserved. Consumed only by the mobile legend + scrubber hide gate.
export function aoiRectTooSmallToShow(
  rect: LegendScreenRect | null | undefined,
): boolean {
  if (!rect) return false;
  const w = Math.abs(rect.right - rect.left);
  const h = Math.abs(rect.bottom - rect.top);
  return Math.min(w, h) < AOI_MIN_VISIBLE_EXTENT_PX;
}

// --- F74b feature-click/tap-to-inspect ---------------------------------- //
//
// The agent advertises "click polygons to see name / designation / IUCN", but
// until this feature nothing in the web client hit-tested rendered features.
// These helpers turn a hit feature's `properties` bag into popup-ready content.
// All of it is pure (Invariant 1: we surface received values verbatim  -  no
// geography is computed). The MapLibre wiring (queryRenderedFeatures, the
// click/touch handlers, the cursor) lives inside MapView below.

/**
 * Property keys (lowercased) we treat as the feature's NAME, in priority order.
 * Covers the WDPA live schema (`name_eng`), GBIF/iNat species fields, NWS
 * alerts, admin boundaries, OSM, and the generic `name`/`title` fallbacks.
 */
const NAME_KEYS: readonly string[] = [
  "name_eng",
  "name",
  "title",
  "orig_name",
  "site_name",
  "scientificname",
  "species",
  "vernacularname",
  "common_name",
  "event",
  "headline",
  "namelsad",
];

/**
 * Property keys (lowercased) we treat as the feature's DESIGNATION / TYPE, in
 * priority order. WDPA designation is `desig_eng`; others fall back to generic
 * type/category fields.
 */
const DESIGNATION_KEYS: readonly string[] = [
  "desig_eng",
  "designation",
  "desig",
  "type",
  "category",
  "feature_type",
  "highway",
  "landcover",
];

/** Property keys (lowercased) we treat as the IUCN category, in priority order. */
const IUCN_KEYS: readonly string[] = ["iucn_cat", "iucn_category", "iucn"];

/**
 * Style-preset prefixes that mark a vector layer as a STATION layer
 * (L3-web-station-csv). A tap on one of these layers gets the Download-CSV
 * affordance on its popup (USGS gauges, ASOS/METAR, RAWS weather, NOAA CO-OPS).
 * Prefix match so preset variants (e.g. `coops_water_level`) all qualify.
 */
const STATION_PRESET_PREFIXES: readonly string[] = [
  "usgs_gauges",
  "asos_metar",
  "raws_weather",
  "coops_",
];

/** True when `preset` marks a station layer (prefix match, case-insensitive). */
export function isStationPreset(preset: string | null | undefined): boolean {
  if (typeof preset !== "string") return false;
  const p = preset.toLowerCase();
  return STATION_PRESET_PREFIXES.some((prefix) => p.startsWith(prefix));
}

/**
 * Keys we DROP from the generic attribute list because they are either internal
 * IDs / geometry noise or already surfaced as the title/subtitle/IUCN rows.
 */
const HIDDEN_ATTR_KEYS: ReadonlySet<string> = new Set([
  "geometry",
  "bbox",
  "id",
  "fid",
  "objectid",
  "shape_length",
  "shape_area",
  "shape__length",
  "shape__area",
  // Raw OSM identifiers -- plumbing, not user-facing attributes, in the same
  // class as id/fid/objectid hidden above. Globally hidden so NO popup (footprint
  // or otherwise) surfaces a bare "Osm Id" row; for slim footprints the human
  // detail (name/height/...) arrives via the click-to-enrich fetch instead.
  "osm_id",
  "osm_type",
]);

/** Humanize a raw property key for display: `name_eng` -> "Name Eng", `iucn_cat` -> "Iucn Cat". */
export function humanizePropertyKey(key: string): string {
  const cleaned = key.replace(/[_-]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").trim();
  if (!cleaned) return key;
  return cleaned
    .split(/\s+/)
    .map((w) => (w.length <= 1 ? w.toUpperCase() : w.charAt(0).toUpperCase() + w.slice(1)))
    .join(" ");
}

/** Coerce a property value to a compact display string, or null when not worth showing. */
export function stringifyPropertyValue(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") {
    const t = value.trim();
    return t.length > 0 ? t : null;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return null;
    // Trim long floats so coordinates / areas don't blow out the card.
    return Number.isInteger(value) ? String(value) : String(Math.round(value * 1000) / 1000);
  }
  if (typeof value === "boolean") return value ? "Yes" : "No";
  // Objects / arrays  -  JSON, but keep it short so the card stays compact.
  try {
    const s = JSON.stringify(value);
    if (!s || s === "{}" || s === "[]") return null;
    return s.length > 120 ? `${s.slice(0, 117)}...` : s;
  } catch {
    return null;
  }
}

/** Find the first present, non-empty value among `keys` (case-insensitive). */
function pickByKeys(
  props: Record<string, unknown>,
  lowerMap: Map<string, string>,
  keys: readonly string[],
): { key: string; value: string } | null {
  for (const k of keys) {
    const actual = lowerMap.get(k);
    if (actual === undefined) continue;
    const v = stringifyPropertyValue(props[actual]);
    if (v !== null) return { key: actual, value: v };
  }
  return null;
}

/**
 * RFC-4180 CSV serialization of an array of property bags (L3-web-station-csv).
 *
 * PURE + EXPORTED for unit testing. Flattens `rows` to a CSV string:
 *   - header  = union of keys across all rows, in first-seen order, EXCLUDING
 *               geometry + internal/noise keys (HIDDEN_ATTR_KEYS, plus the
 *               literal "geometry" GeoJSON member). An explicit `columns`
 *               argument overrides the derived header (used as-is, order kept).
 *   - a cell  = the property stringified (objects/arrays JSON-encoded); a
 *               missing key on a row yields an empty cell.
 *   - quoting = RFC-4180 - a field is wrapped in double quotes when it contains
 *               a comma, double quote, CR or LF; embedded double quotes are
 *               doubled. Rows are joined with CRLF.
 *
 * Invariant 1: this only serializes RECEIVED feature properties - it never
 * computes geography (the "geometry" member is deliberately excluded).
 */
export function csvFromFeatures(
  rows: ReadonlyArray<Record<string, unknown> | null | undefined>,
  columns?: readonly string[],
): string {
  const safeRows: Record<string, unknown>[] = (rows ?? []).map((r) => r ?? {});

  // Resolve the column set. An explicit `columns` wins; otherwise take the
  // union of keys in first-seen order, dropping geometry + internal/noise keys.
  let header: string[];
  if (columns && columns.length > 0) {
    header = [...columns];
  } else {
    const seen = new Set<string>();
    header = [];
    for (const row of safeRows) {
      for (const key of Object.keys(row)) {
        if (seen.has(key)) continue;
        const lk = key.toLowerCase();
        if (lk === "geometry") continue;
        if (HIDDEN_ATTR_KEYS.has(lk)) continue;
        seen.add(key);
        header.push(key);
      }
    }
  }

  const cellToString = (value: unknown): string => {
    if (value === null || value === undefined) return "";
    if (typeof value === "string") return value;
    if (typeof value === "number") return Number.isFinite(value) ? String(value) : "";
    if (typeof value === "boolean") return value ? "true" : "false";
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  };

  // RFC-4180: quote when the field contains a comma, quote, CR or LF; double up
  // embedded quotes.
  const escapeCell = (raw: string): string => {
    if (/[",\r\n]/.test(raw)) {
      return `"${raw.replace(/"/g, '""')}"`;
    }
    return raw;
  };

  const lines: string[] = [];
  lines.push(header.map((h) => escapeCell(h)).join(","));
  for (const row of safeRows) {
    lines.push(header.map((h) => escapeCell(cellToString(row[h]))).join(","));
  }
  return lines.join("\r\n");
}

/**
 * Build the popup payload from a hit feature's properties + the originating
 * layer name + the screen point. `geomKindLabel` is a fallback title when the
 * feature has no name-like property (e.g. "Polygon"). Returns popup data with a
 * title, optional subtitle (designation/type/layer), IUCN row first when
 * present, then the remaining attributes (humanized, de-noised). Gracefully
 * handles a null/empty properties bag.
 */
export function buildFeaturePopupData(
  properties: Record<string, unknown> | null | undefined,
  point: { x: number; y: number },
  opts: { layerName?: string; geomKindLabel?: string } = {},
): FeaturePopupData {
  const props = properties ?? {};
  // Case-insensitive lookup map: lowercased key -> original key.
  const lowerMap = new Map<string, string>();
  for (const k of Object.keys(props)) lowerMap.set(k.toLowerCase(), k);

  const nameHit = pickByKeys(props, lowerMap, NAME_KEYS);
  const desigHit = pickByKeys(props, lowerMap, DESIGNATION_KEYS);
  const iucnHit = pickByKeys(props, lowerMap, IUCN_KEYS);

  const usedKeys = new Set<string>();
  if (nameHit) usedKeys.add(nameHit.key.toLowerCase());
  if (desigHit) usedKeys.add(desigHit.key.toLowerCase());
  if (iucnHit) usedKeys.add(iucnHit.key.toLowerCase());

  const title = nameHit?.value ?? opts.layerName ?? opts.geomKindLabel ?? "Feature";
  // Subtitle prefers the designation; otherwise the layer name (when it wasn't
  // already used as the title).
  let subtitle: string | undefined;
  if (desigHit) subtitle = desigHit.value;
  else if (opts.layerName && opts.layerName !== title) subtitle = opts.layerName;

  const attributes: FeatureAttribute[] = [];
  // IUCN category leads the attribute list when present (advertised explicitly).
  if (iucnHit) attributes.push({ label: "IUCN Category", value: iucnHit.value });

  // Remaining properties  -  humanized, de-noised, in declaration order.
  for (const key of Object.keys(props)) {
    const lk = key.toLowerCase();
    if (usedKeys.has(lk)) continue;
    if (HIDDEN_ATTR_KEYS.has(lk)) continue;
    const v = stringifyPropertyValue(props[key]);
    if (v === null) continue;
    attributes.push({ label: humanizePropertyKey(key), value: v });
  }

  return { title, subtitle, attributes, point };
}

/**
 * Click-to-enrich (NATE 2026-06-27): merge a fetched OSM tag bag into an open
 * FOOTPRINT popup's attributes. PURE + exported for unit testing.
 *
 * - Humanizes + de-noises each tag the same way buildFeaturePopupData does
 *   (skips HIDDEN_ATTR_KEYS + null/empty values).
 * - Does NOT duplicate a row already present (case-insensitive label match), so
 *   re-merging is idempotent and a slim attr is not shown twice.
 * - Promotes a real `name`/`addr:*`-derived title when the slim card's title was
 *   only a geometry-kind / layer-name fallback (so "Polygon" becomes "Maison").
 *
 * Returns a NEW FeaturePopupData (never mutates the input).
 */
export function mergeTagsIntoAttributes(
  data: FeaturePopupData,
  tags: Record<string, unknown>,
): FeaturePopupData {
  const existingLabels = new Set(
    data.attributes.map((a) => a.label.toLowerCase()),
  );
  const merged: FeatureAttribute[] = [...data.attributes];
  // Pull a name out of the tags for a possible title promotion.
  let tagName: string | undefined;
  for (const k of Object.keys(tags)) {
    if (NAME_KEYS.includes(k.toLowerCase())) {
      const v = stringifyPropertyValue(tags[k]);
      if (v) {
        tagName = v;
        break;
      }
    }
  }
  for (const key of Object.keys(tags)) {
    const lk = key.toLowerCase();
    if (HIDDEN_ATTR_KEYS.has(lk)) continue;
    // The name became the title (when promoted) - don't also list it as a row.
    if (tagName && NAME_KEYS.includes(lk)) continue;
    const v = stringifyPropertyValue(tags[key]);
    if (v === null) continue;
    const label = humanizePropertyKey(key);
    if (existingLabels.has(label.toLowerCase())) continue;
    existingLabels.add(label.toLowerCase());
    merged.push({ label, value: v });
  }
  // Promote the title only when the slim card had a non-name fallback title.
  const fallbackTitles = new Set(["feature", "polygon", "point", "line"]);
  let title = data.title;
  if (
    tagName &&
    (fallbackTitles.has(data.title.toLowerCase()) || data.title === data.subtitle)
  ) {
    title = tagName;
  }
  return { ...data, title, attributes: merged };
}

export function MapView({ subscribeSessionState, subscribeMapCommand, theme = "light", onAoiScreenRectChange, onAoiTooSmallToShowChange, legendHidden, onLegendHiddenChange, suppressLegendShowPill, legendSheetTopPx = null, legendChartOpen = false, caseActive = true, aoiCaptureActive, onAoiCaptureConfirm, onAoiCaptureSkip, onAoiCaptureCancel, chatWidthPx, chatCollapsed, mobile, simRunning = false, caseHasAoi, onAoiStageConfirm, terrain3dEnabled = false, contoursEnabled = false, leftPanelWidthPx = 0 }: MapViewProps = {}): JSX.Element {
  const container = useRef<HTMLDivElement | null>(null);
  const map = useRef<MapLibreMap | null>(null);
  // job-0179  -  the shared per-Case layer cache (the seatbelt). Stable singleton;
  // gates teardown (allowsEvict), supplies persisted view-overrides, and records
  // the user's live LayerPanel edits. App.tsx keeps `.activeCaseId` in lockstep.
  const layerCache = getLayerCache();
  // useRef so this survives effect re-runs without triggering re-render (A.7).
  const addedSourceIds = useRef<Set<string>>(new Set());
  // deck.gl SPIKE (#169): the single interleaved MapboxOverlay (constructed +
  // addControl'd in the map-init `m.once("load")` below) and the set of layer_ids
  // we ROUTED to deck (so add/remove/case-clear + opacity/visibility/order
  // overrides drive the overlay, not the MapLibre vector path). The geometry +
  // resolved properties for each routed layer are kept so a picked deck feature
  // can be adapted into the same FeaturePopup payload the MapLibre click builds.
  const deckOverlay = useRef<MapboxOverlayType | null>(null);
  const deckRoutedIds = useRef<Set<string>>(new Set());
  const deckRoutedLayers = useRef<Map<string, DeckRoutableLayer>>(new Map());
  // LAZY-LOAD (deck.gl SPIKE, #169): cached ref to the dynamically-imported deck.gl
  // GeoJsonLayer builder. Null until the first deck-routed layer triggers
  // `ensureDeckLoaded()` (one `await import(...)` for @deck.gl/mapbox +
  // ./lib/deck_layers); once cached, every subsequent reconcile pass reads it (and
  // the overlay) SYNCHRONOUSLY. `deckLoadInFlight` dedupes concurrent triggers so we
  // construct exactly ONE overlay.
  const buildDeckGeoJsonLayerFn = useRef<typeof BuildDeckGeoJsonLayerFn | null>(
    null,
  );
  const deckLoadInFlight = useRef<boolean>(false);
  // deck.gl SPIKE (#169): true while an agent spatial-input (draw/pick) request OR
  // a region-choice pick is in flight. While set, deck layers are rebuilt
  // NON-pickable so deck does not swallow the canvas pointer events terra-draw /
  // the pick surface / the region-choropleth need (mirrors the queryRenderedFeatures
  // pick path staying inert during those flows). A ref (not state) so the stable
  // applyLatest reconcile closure reads the LIVE value; a setter effect re-runs the
  // deck rebuild when it flips.
  const deckPickSuppressed = useRef<boolean>(false);
  // deck.gl SPIKE (#169): the picking-bridge click handler (assigned by the click
  // effect that owns buildFeaturePopupData) + a rebuild trigger so flipping the
  // suppress flag re-emits the overlay layers non-pickable without waiting for the
  // next session-state push. Both are refs so the stable applyLatest closure reads
  // the live values.
  const onDeckClickRef = useRef<((info: unknown) => void) | null>(null);
  const rebuildDeckLayersRef = useRef<(() => void) | null>(null);
  // CASES-ROOT NO-LAYERS GATE (NATE 2026-06-22)  -  mirror `caseActive` into a ref
  // so the session-state reconcile (a STABLE closure with deps [subscribeSessionState],
  // deliberately not re-created on prop flips to avoid churning the subscription)
  // reads the LIVE value. When false (cases-list / root view) the reconcile
  // force-clears every overlay and the legend has no content. Synced below.
  const caseActiveRef = useRef<boolean>(caseActive);
  // Holds the reconcile effect's `applyLatest` so the caseActive sync effect can
  // re-run the reconcile when the Case-entered state flips (root -> the overlays
  // tear down; entered -> any persisted layers re-paint) without a new
  // session-state push. Assigned inside the reconcile effect below.
  const applyLatestRef = useRef<(() => void) | null>(null);
  // Per-layer geometry kind for added vector layers. Lets the update branch
  // pick the right paint property name (`circle-opacity` vs `line-opacity`
  // vs `fill-opacity`) when opacity/visibility changes on a known vector layer.
  // Also lets the visibility/opacity update path skip raster-only ops on vectors.
  const vectorGeomKinds = useRef<Map<string, VectorGeomKind>>(new Map());
  // job-0258: style_preset per layer_id, recorded when the layer is wired in.
  // The map-command opacity path needs it for the Pelicun fill multiplier,
  // and the command envelope itself doesn't carry presets.
  const layerStylePresets = useRef<Map<string, string | null>>(new Map());
  // DATA-DRIVEN LEGEND - mirror of layerStylePresets keyed by layer_id, so the
  // map-command opacity-slider path (which has only the layer_id) can recover the
  // layer's LegendKey to keep a graduated/categorical fill BOLD when the slider
  // moves. Populated + cleared alongside layerStylePresets in the reconcile loop.
  const layerLegends = useRef<Map<string, LegendKey | null>>(new Map());
  // Tracks the in-flight vector-fetch generation per layer_id. When a layer is
  // removed mid-fetch, this counter advances so a late-arriving fetch resolves
  // into a no-op rather than re-registering the source (kickoff -scope:
  // "Cleanup on remove: when a layer is removed... remove both source and
  // layer cleanly").
  const vectorFetchGen = useRef<Map<string, number>>(new Map());
  // AWS-migration hardening (bbox track): the last zoom-to bbox corners. The
  // analysis-extent rectangle and the camera move share one handler; if a
  // style (re)load happens AFTER the rectangle was drawn (theme setStyle,
  // per-Case MapView remount replay) the rectangle's source/layers are gone
  // with the old style. Remembering the corners lets a follow-up redraw
  // re-assert the rectangle without needing the bus to re-deliver the
  // command. Null until the first zoom-to. Kept inside this track's
  // ownership (no LayerPanel bus replay buffer  -  see crossTrackChanges).
  const lastZoomToCorners = useRef<[number, number, number, number] | null>(null);
  // BUG 3 (cold rasters don't paint until WS connect): the LAST zoom-to bbox that
  // arrived, RETAINED so it can be replayed on the map's first `load`. A zoom-to
  // dispatched BEFORE the map existed / before the style loaded used to be DROPPED
  // (the subscriber bailed at `if (!m) return`), leaving the camera at CONUS zoom
  // so no viewport tiles ever fetched. Written at the TOP of the map-command
  // subscriber (before the !m guard) and consumed by the map "load" handler.
  const pendingZoomToRef = useRef<[number, number, number, number] | null>(null);
  // BUG 3: the map-command effect stores its zoom-to applier here so the map
  // "load" handler can REPLAY a retained cold zoom-to through the exact same path
  // (fitBounds + AOI bbox stash + extent draw) without duplicating that logic.
  const applyZoomToRef = useRef<
    ((corners: [number, number, number, number]) => void) | null
  >(null);
  // NATE 2026-06-22 (item 4): mirror the sim-running flag into a ref so the
  // (stable-closure) redraw paths paint the AOI rectangle in the right color
  // when they re-assert it, and a dedicated effect recolors the live box the
  // moment the flag flips. Kept in lockstep with the `simRunning` prop below.
  const simRunningRef = useRef<boolean>(simRunning);
  // ROOT-CAUSE FIX (job-0076 diagnosis): the prior implementation read
  // `payload.loaded_layers` synchronously in the subscriber and bailed if
  // `m.isStyleLoaded()` was false  -  so when session-state arrived BEFORE the
  // remote QGIS Server basemap tiles finished loading, the entire flood-layer
  // wiring was dropped on the floor with no retry. Diagnosis evidence:
  // `reports/inflight/job-0076-*/evidence/diagnosis.log` shows 69 basemap
  // tile responses + ZERO flood tile responses, and the post-injection style
  // spec contained only the basemap sources (no `flood-depth-job-0075-demo`
  // source/layer entries). Headline screenshots since job-0066 were
  // basemap-only because of this race.
  //
  // Fix: stash the latest session-state payload in a ref, and run an apply
  // function that (a) executes immediately if the style is ready, OR
  // (b) defers to the next `idle` / `load` event. The ref always carries
  // the latest payload, so multiple in-flight events collapse to the
  // most-recent state (still replace-not-reconcile per A.7).
  const latestSessionState = useRef<SessionStatePayload | null>(null);

  // job-0321 (F43)  -  the legend (depth-key / colorbar) now lives INSIDE the map
  // container so it can anchor to the AOI bounding box. Three pieces of state:
  //   1. legendLayers  -  the ordered ProjectLayerSummary list the legend needs,
  //      sourced from this component's own session-state subscription (App.tsx
  //      no longer mounts the legend, so it passes nothing). Ordered top-of-
  //      stack-first (z_index desc) to match LayerPanel + LayerLegend's
  //      `layers.find(...)` "topmost wins" contract.
  //   2. aoiBbox  -  the current AOI bbox corners (mirrors lastZoomToCorners into
  //      state so a re-render projects it). Null = no AOI -> bottom-center.
  //   3. legendRect  -  the TRUE projected AOI screen rectangle {left,top,right,
  //      bottom} (computeBboxScreenRect: min/max over all four projected bbox
  //      corners), recomputed on map move/zoom/render (rAF-throttled). This is
  //      the snap source of truth: it is passed straight to LayerLegend, which
  //      rails the colorbar keys CCW along ITS four edges  -  so the snap follows
  //      the real AOI aspect ratio + on-screen skew, not a square-ish estimate.
  //      Null when there is no AOI / the box is off-screen -> legend falls back to
  //      bottom-center so it never disappears.
  //   4. legendAnchor  -  the projected {left, top} bottom-edge midpoint of the AOI
  //      box, derived from the SAME rect in the same pass. Used only for the
  //      legend's vertical placement nudge (resolvedAnchor), NOT the snap math.
  //   5. legendBarWidth  -  the box's on-screen EAST-WEST extent (right-left),
  //      also from the same rect. Used only to SIZE the default colorbar width.
  const [legendLayers, setLegendLayers] = useState<ProjectLayerSummary[]>([]);
  // BUG 1 (secondary - heartbeat re-render storm): the last ORDERED legend list
  // we committed. The server re-emits a full session-state on every ~12-25s
  // heartbeat; each one rebuilt a fresh reversed/sorted array and called
  // setLegendLayers unconditionally, re-rendering the whole MapView subtree
  // forever. We guard with an ORDER-SENSITIVE structural compare (id + effective
  // z) against this ref so an unchanged heartbeat is a no-op (React bails).
  const lastLegendOrderedRef = useRef<ProjectLayerSummary[]>([]);
  const [aoiBbox, setAoiBbox] = useState<[number, number, number, number] | null>(null);
  // The TRUE projected AOI rectangle the legend snaps against (all four corners).
  const [legendRect, setLegendRect] = useState<LegendScreenRect | null>(null);
  // LANE C (flicker): the last legendRect we COMMITTED to state, used to skip a
  // setLegendRect (and the MapView re-render it triggers) when the per-render
  // reprojection yields the identical rect - the map's 'render' event fires
  // every paint, so without this guard the legend subtree churned continuously.
  const lastLocalRectRef = useRef<LegendScreenRect | null>(null);
  const [legendAnchor, setLegendAnchor] = useState<LegendAnchor | null>(null);
  // FIX 4 (NATE 2026-06-17)  -  the AOI bbox's ON-SCREEN width in px, projected on
  // each map move/zoom (same listeners as legendAnchor). Null when there is no
  // AOI / the bbox is off-screen -> LayerLegend uses its static 320 fallback.
  const [legendBarWidth, setLegendBarWidth] = useState<number | null>(null);
  // MOBILE-ONLY HUD (NATE 2026-06-27) - two derived legend signals threaded to
  // LayerLegend so its MOBILE path can decide corner-attach (snap to the AOI box)
  // vs dock-above-chat (snap to a clean horizontal row above the scrubber):
  //   - legendMapZoom: the LIVE map zoom, tracked on every move/zoom by the
  //     always-on zoom effect below (NOT the popup-only `currentZoom`, which is
  //     null whenever no feature popup is open). Threaded as the `mapZoom` prop.
  //     Null until the first projection.
  //   - aoiCornerPlaceable: true when the AOI box is usefully on-screen for a
  //     corner attach, false in the clearly-too-zoomed cases (off-screen / fills
  //     the viewport / projects to a tiny dot). Computed in the legendRect
  //     recompute below. Conservatively TRUE in the normal case so existing
  //     corner-attach behavior is preserved; the legend's MOBILE band-dock only
  //     fires when this is false. DESKTOP ignores both props (byte-for-byte
  //     unchanged): the legend's desktop path never reads mapZoom /
  //     aoiCornerPlaceable.
  const [legendMapZoom, setLegendMapZoom] = useState<number | null>(null);
  const [aoiCornerPlaceable, setAoiCornerPlaceable] = useState<boolean>(false);
  // ZOOM-OUT HIDE (NATE 2026-06-27, mobile-only) - the AOI bbox is a tiny DOT on
  // screen (zoomed OUT far). Threaded to the LayerLegend (which early-returns null)
  // and lifted to App (for the SequenceScrubber) so BOTH hide. Default false so an
  // absent/normal bbox keeps both overlays visible. DESKTOP never reads it.
  const [aoiTooSmallToShow, setAoiTooSmallToShow] = useState<boolean>(false);
  const isMobile = useIsMobile();

  // F74b feature-click/tap-to-inspect. `featurePopup` is the currently-shown
  // popup payload (null = none). `mapCanvasSize` mirrors the canvas dimensions
  // so the popup can clamp itself on screen. The click/touch handler reads
  // `vectorGeomKinds` (which tracks every rendered vector layer_id) to build the
  // list of queryable layers, then queryRenderedFeatures hit-tests the point.
  const [featurePopup, setFeaturePopup] = useState<FeaturePopupData | null>(null);
  const [mapCanvasSize, setMapCanvasSize] = useState<{ width: number; height: number }>({
    width: 0,
    height: 0,
  });
  // FIX 3 (NATE 2026-06-17)  -  the live map zoom, tracked so the popup can scale
  // with zoom (scale = 2^(zoom - refZoom), clamped). Updated on map move/zoom by
  // the popup-pin effect below. Null until the first projection.
  const [currentZoom, setCurrentZoom] = useState<number | null>(null);

  // FR-WC-13 / FR-WC-16  -  the active spatial-input request (null = no pick/draw
  // in flight). Published by Chat.tsx onto the spatialInputBus when a
  // `spatial-input-request` arrives; the SpatialDrawSurface overlay mounts while
  // non-null. The drawn / picked result rides BACK through the bus to Chat (the
  // WS reply owner)  -  Map never touches the WebSocket. Mirrors the region-choice
  // bus pattern exactly.
  const [spatialRequest, setSpatialRequest] =
    useState<SpatialInputRequestPayload | null>(null);

  useEffect(() => {
    if (!container.current || map.current) return;
    const m = new maplibregl.Map({
      container: container.current,
      style: STYLE,
      center: CONUS_VIEW.center,
      zoom: CONUS_VIEW.zoom,
      // BUG 1 (memory crash): cap per-source tile retention. The frame-preload
      // warm path keeps temporal-raster frames renderable so their tiles fetch;
      // MapLibre's default tile cache grows unbounded across a 144/288-frame
      // SFINCS/HRRR sweep until the tab OOMs. This is a hard backstop CAP on how
      // many tiles MapLibre retains per source (releaseWarmedFrames below is the
      // primary fix - it frees whole SourceCaches by flipping warmed frames to
      // visibility:none; this cap bounds the rest). 64 is a sane viewport-ish
      // working set; lower trades a touch of re-fetch for a smaller ceiling.
      maxTileCacheSize: 64,
      maxPitch: 0,
      dragRotate: false,
      pitchWithRotate: false,
      touchPitch: false,
      // job-0152: attribution tag removed for v0.1 demo (overlays other UI).
      // Users zoom via scroll/pinch/keyboard  -  no NavigationControl added below.
      // Production hosting should restore attribution per OSM tile-use terms.
      attributionControl: false,
    });
    // Decision I: 2D-only navigation. Belt + suspenders  -  explicitly disable
    // rotation in addition to constructor options so a future MapLibre default
    // change can't silently re-enable it.
    m.touchZoomRotate.disableRotation();
    m.keyboard.disableRotation();
    // job-0152: NavigationControl (zoom +/- and compass) removed  -  overlays
    // other UI elements. Scroll-zoom, pinch-zoom, and keyboard +/- remain
    // active (MapLibre defaults; no code change needed). See OQ below re: OSM
    // attribution terms.
    map.current = m;
    activeMap = m;
    // INCIDENT FIX 2026-06-16: latch style-readiness on the first `load` so a
    // later hung tile (which flips isStyleLoaded() back to false) can never
    // re-block layer adds/removals. See mapStyleReady().
    m.once("load", () => {
      (m as ReadyMap).__grace2StyleReady = true;
      // deck.gl SPIKE (#169) + LAZY-LOAD: the interleaved MapboxOverlay is NO LONGER
      // constructed here. The deck.gl bundle (~211 KB gz) is kept out of the main
      // chunk; the overlay is built ON DEMAND by ensureDeckLoaded() the FIRST time a
      // deck-routed (footprint/heavy) layer appears in applyLatest. A session with
      // no footprint layer never loads deck.gl at all.
      // BUG 3 (cold rasters don't paint until WS connect): a session-state push or
      // a zoom-to that arrived BEFORE this `load` was never applied - the map sat
      // quiescent (no idle re-emit on a cold map) so the deferral stalled until a
      // later WS session-state push, and the dropped zoom-to left the camera at
      // CONUS so no viewport tiles fetched. On first load we now (1) apply any
      // RETAINED session-state via applyLatest (adds the raster sources) and
      // (2) REPLAY any retained zoom-to (snaps the camera to the AOI so tiles
      // load). Both are idempotent, so a normal warm start is unaffected.
      try {
        applyLatestRef.current?.();
      } catch {
        /* best-effort cold apply */
      }
      const pending = pendingZoomToRef.current;
      if (pending && applyZoomToRef.current) {
        try {
          applyZoomToRef.current(pending);
        } catch {
          /* best-effort cold camera replay */
        }
      }
    });

    // Dev-only seam: expose the live MapLibre instance so the Playwright
    // diagnostic driver (reports/inflight/job-0076-*/evidence/) can introspect
    // m.getStyle()  -  i.e. confirm flood layer was added, capture the actual
    // tile URL template, etc. Production builds drop this via import.meta.env.
    if (import.meta.env.DEV) {
      (window as unknown as { __grace2GetMap?: () => MapLibreMap | null }).__grace2GetMap = () => map.current;
    }

    return () => {
      // deck.gl SPIKE (#169): drop the overlay before the map so deck releases its
      // GL resources cleanly. Best-effort - m.remove() below also tears the context.
      // LAZY-LOAD null-guard: the overlay may NEVER have been constructed (no
      // footprint layer this session, or an import still in flight), so removeControl
      // only runs when it exists; the bookkeeping + lazy-load state always resets so
      // a remount re-imports cleanly.
      if (deckOverlay.current) {
        try {
          m.removeControl(deckOverlay.current as unknown as maplibregl.IControl);
        } catch {
          /* already detached / map mid-teardown */
        }
        deckOverlay.current = null;
      }
      deckRoutedIds.current.clear();
      deckRoutedLayers.current.clear();
      buildDeckGeoJsonLayerFn.current = null;
      deckLoadInFlight.current = false;
      m.remove();
      map.current = null;
      if (activeMap === m) activeMap = null;
      if (import.meta.env.DEV) {
        delete (window as unknown as { __grace2GetMap?: () => MapLibreMap | null }).__grace2GetMap;
      }
    };
  }, []);

  // "3D terrain viz" first cut - apply / remove MapLibre terrain when the
  // persisted 3D toggle flips. Gated on mapStyleReady (the same one-time latch
  // the layer reconcile uses) so addSource/addLayer/setTerrain run only after
  // the style has loaded once; a not-yet-ready map re-arms on the next idle.
  // The pure source-builder + side-effect helpers live in lib/terrain_3d.ts;
  // this effect just sequences them against the live instance. Re-runs whenever
  // the 3D / contour flags change. Teardown on unmount/disable restores 2D.
  useEffect(() => {
    const m = map.current;
    if (!m) return;
    const tm = m as unknown as TerrainMapLike;

    // prefers-reduced-motion: the 3D enter / exit is a ~1.2s camera flight. When
    // the user has asked for reduced motion, jump (duration 0) instead of easing.
    const prefersReducedMotion =
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const easeMs = prefersReducedMotion ? 0 : TERRAIN_3D_EASE_MS;

    // NATE 2026-06-26 / 2026-06-29: overlay raster crispness under 3D. Session-
    // added raster layers paint with raster-resampling: "nearest" (job-0078) so
    // their COG cells stay 1:1 with the basemap grid in the flat 2D top-down view
    // - the only visually-irrefutable proof of per-cell geographic alignment. When
    // those nearest-sampled cells DRAPE over a pitched terrain mesh they can read
    // as hard blocks, so the first cut blanket-switched them to "linear" in 3D -
    // but that made them BLURRY the moment you zoomed out even a little. NATE
    // wants them CRISP at a moderate zoom-out and soft ONLY when zoomed VERY far.
    // So the 3D value is now a zoom-STEP expression (buildDrape3dResamplingExpr:
    // "linear" below z6, "nearest" at/above) instead of a flat "linear"; 2D
    // restores the scalar "nearest" default (byte-for-byte unchanged). We sweep
    // addedSourceIds.current here - rather than at the raster-add block, whose
    // effect dep array does NOT re-run on the 3D toggle - so this also fixes
    // rasters that were added BEFORE 3D was enabled.
    const setOverlayRasterResampling = (
      mode: "nearest" | DrapeResamplingExpression,
    ): void => {
      const mm = map.current;
      if (!mm) return;
      for (const id of addedSourceIds.current) {
        try {
          // Only raster overlay layers carry raster-resampling; setting it on a
          // vector layer throws. getLayer(...).type guards that (and skips ids
          // whose layer is mid-add / already torn down).
          const layer = mm.getLayer(id) as { type?: string } | undefined;
          if (layer?.type !== "raster") continue;
          mm.setPaintProperty(id, "raster-resampling", mode);
        } catch {
          /* one bad layer must not abort the sweep - skip + continue */
        }
      }
    };

    // Disable path: ease the camera FLAT (top-down, north-up) FIRST, THEN tear
    // the terrain down + re-lock 2D once the move settles. Easing before teardown
    // means the user sees the relief lower back to flat rather than the terrain
    // popping away under a still-pitched camera. removeTerrain3d is fully
    // defensive (try/catch + getLayer/getSource guards), so it is safe before the
    // style has loaded and must NOT gate on mapStyleReady - gating here would
    // latch __grace2StyleReady on mount for the (default) 2D case, which would
    // defeat the layer-reconcile's "style not loaded yet" deferral elsewhere.
    if (!terrain3dEnabled) {
      let torndown = false;
      const teardown = () => {
        if (torndown) return;
        torndown = true;
        if (!map.current) return;
        try {
          // setMaxPitch(0) inside removeTerrain3d re-locks 2D AFTER the camera is
          // already flat, so the pitch we just eased to is not clamped mid-flight.
          removeTerrain3d(map.current as unknown as TerrainMapLike);
          // NATE 2026-06-26: restore the 2D nearest-resampling default so overlay
          // rasters return to crisp per-cell alignment once the terrain is gone.
          setOverlayRasterResampling("nearest");
        } catch {
          /* map already torn down - fine */
        }
      };
      // Only fly the camera flat when it is actually pitched (i.e. 3D was on).
      // On a fresh 2D mount (the default) the camera is already flat, so skip the
      // easeTo entirely and tear down immediately - this also avoids registering
      // a spurious moveend handler on every render.
      let pitched = false;
      try {
        pitched = typeof m.getPitch === "function" && m.getPitch() > 0.5;
      } catch {
        pitched = false;
      }
      if (!pitched) {
        teardown();
        return;
      }
      try {
        const flat = buildFlat2dCameraPose();
        m.once("moveend", teardown);
        m.easeTo({ pitch: flat.pitch, bearing: flat.bearing, duration: easeMs });
        // Belt + suspenders: if the move never fires moveend (already flat, no
        // animation), tear down on a timer so terrain is never left dangling.
        window.setTimeout(teardown, easeMs + 80);
      } catch {
        // easeTo unavailable (test stub / pre-init) - tear down immediately so
        // the toggle still works without the camera flourish.
        teardown();
      }
      return;
    }

    // Enable path: addSource/addLayer/setTerrain need the style loaded once.
    // Gate on the mapStyleReady latch (re-arm on idle) like the layer reconcile.
    // Once terrain is applied, easeTo a PITCHED pose (keeping the live center +
    // zoom) so 3D is immediately + unmistakably 3D rather than a flat hillshade.
    const run = () => {
      if (!map.current) return;
      if (!mapStyleReady(map.current)) {
        map.current.once("idle", run);
        return;
      }
      // DEM origin: no per-case DEM COG is published yet, so buildTerrainDemSource
      // (called inside applyTerrain3d with no demSource) falls back to the public
      // AWS Terrarium tiles. Once the agent emits a per-case DEM COG, thread it
      // through here as a prop -> TiTiler terrain-RGB path (see FOLLOW-UPS).
      applyTerrain3d(tm, { contoursRequested: contoursEnabled });
      // NATE 2026-06-29: keep draped overlay rasters CRISP at moderate zoom-out
      // (zoom-step "nearest" at/above z6) and soften to "linear" only when zoomed
      // VERY far out - replaces the old blanket "linear" that blurred them on any
      // zoom-out. 3D-drape-only; the 2D path keeps the scalar "nearest" default.
      setOverlayRasterResampling(buildDrape3dResamplingExpression());
      // Pitch the camera so the relief actually reads. applyTerrain3d already
      // unlocked maxPitch (75) + rotate, so this easeTo is not clamped. Keep
      // center + zoom (easeTo merges over the current pose).
      try {
        const pose = buildTerrain3dCameraPose();
        map.current.easeTo({
          pitch: pose.pitch,
          bearing: pose.bearing,
          duration: easeMs,
        });
      } catch {
        /* easeTo unavailable - terrain still renders, just without the tilt-in */
      }
    };
    run();
  }, [terrain3dEnabled, contoursEnabled]);

  // Subscribe to session-state and wire WMS raster sources (job-0068, change 4;
  // job-0076 race-condition fix). Replace-not-reconcile per A.7: diff
  // loaded_layers against addedSourceIds ref.
  // Invariant 4: QGIS Server renders all Tier B raster data; Map.tsx only
  // registers tile URLs  -  never computes colors, reads COGs, or touches GCS.
  useEffect(() => {
    if (!subscribeSessionState) return;

    /**
     * LAZY-LOAD (deck.gl SPIKE, #169): one-time dynamic import of the deck.gl
     * bundle (@deck.gl/mapbox MapboxOverlay + the deck_layers builder), construct
     * the ONE interleaved overlay, addControl it, then re-run the reconcile so the
     * deck setProps path runs SYNCHRONOUSLY now that the modules are cached. Called
     * fire-and-forget from applyLatest the FIRST time a deck-routed layer appears.
     * Idempotent + deduped: a second call while the import is in flight is a no-op,
     * and once the overlay exists it returns immediately. A failed import leaves the
     * MapLibre paths fully functional (deckOverlay stays null -> the heavy vector
     * falls back to the MapLibre vector path on the same/next pass).
     */
    const ensureDeckLoaded = (): void => {
      if (deckOverlay.current || deckLoadInFlight.current) return;
      const m = map.current;
      if (!m) return;
      deckLoadInFlight.current = true;
      void (async () => {
        try {
          const [mapboxMod, layersMod] = await Promise.all([
            import("@deck.gl/mapbox"),
            import("./lib/deck_layers"),
          ]);
          buildDeckGeoJsonLayerFn.current = layersMod.buildDeckGeoJsonLayer;
          // The map may have torn down while the import was resolving.
          const live = map.current;
          if (!live) return;
          if (!deckOverlay.current) {
            // interleaved:true draws deck layers INTO the MapLibre GL context (so
            // they respect the beforeId ordering we assign + share the basemap
            // depth buffer). addControl accepts any IControl; the cast bridges the
            // maplibre-gl vs mapbox-gl IControl nominal-type gap.
            const overlay = new mapboxMod.MapboxOverlay({
              interleaved: true,
              layers: [],
            });
            deckOverlay.current = overlay;
            live.addControl(overlay as unknown as maplibregl.IControl);
          }
          // Re-run the (idempotent) reconcile so the now-loaded overlay gets its
          // deck layers built + setProps'd in the synchronous rebuild block.
          applyLatestRef.current?.();
        } catch (err) {
          // Additive feature: a failure leaves MapLibre fully functional.
          console.warn(
            "[grace2] deck.gl lazy import / overlay init failed; falling back to MapLibre",
            err,
          );
          deckOverlay.current = null;
        } finally {
          deckLoadInFlight.current = false;
        }
      })();
    };

    /**
     * Apply the latest session-state payload (from `latestSessionState`) to
     * the live map. Idempotent  -  reads the ref each call so multiple deferred
     * calls collapse to the most-recent payload. Called both from the bus
     * subscription AND from the map "idle" handler in case the bus event
     * arrived before the style finished loading.
     */
    const applyLatest = () => {
      const m = map.current;
      const payload = latestSessionState.current;
      if (!m || !payload) return;
      // If the style isn't loaded yet, RE-ARM and retry on the next idle.
      // job-0258 live-probe finding: the previous `return` here did NOT
      // re-arm  -  the subscriber registers exactly one once("idle") per push,
      // and when that idle callback ran right after applyTheme had mutated
      // the style in the SAME idle dispatch (dark theme swaps the basemap),
      // isStyleLoaded() was false again and the whole layer batch was
      // silently dropped until the next session-state push. Re-arming makes
      // the deferral actually converge; applyLatest is idempotent
      // (replace-not-reconcile diff against addedSourceIds), so extra idle
      // invocations are harmless.
      // INCIDENT FIX 2026-06-16: gate on the mapStyleReady LATCH, not raw
      // isStyleLoaded(). A hung raster tile (vector-as-raster) keeps
      // isStyleLoaded() false forever  -  the old gate then deferred this whole
      // reconcile (adds AND removals AND the AOI) indefinitely, so the map
      // froze. Once the style has loaded once, proceed regardless of stuck
      // tiles; addSource/addLayer/removeLayer are all safe.
      if (!mapStyleReady(m)) {
        m.once("idle", applyLatest);
        // BUG 3 (cold rasters don't paint until WS connect): a QUIESCENT cold map
        // never re-emits `idle` on its own (no in-flight requests to settle), so
        // this deferral would stall until the next WS session-state push. Nudge a
        // repaint so the map converges and fires `idle`, running the deferred
        // apply now instead of waiting for the socket.
        try {
          m.triggerRepaint();
        } catch {
          /* triggerRepaint absent (older map / test mock) - harmless */
        }
        return;
      }

      // F97: DEDUP by layer_id so one logical layer_id == exactly one entry.
      // MapLibre sources are keyed by layer_id (addedSourceIds + addSource(id)),
      // so two snapshot entries sharing a layer_id collide: the second add is
      // skipped, yet both occupy ONE shared source  -  and a later delete-by-id
      // tears that source down, making BOTH vanish (the F97 bug). The primary
      // fix mints a unique layer_id per fetch server-side; this dedup is the
      // client-side defense-in-depth so that IF a duplicate id ever reaches the
      // reconcile, it never desyncs the source map. Keep the LAST occurrence
      // (the freshest metadata  -  matches the server's append/replace-by-id
      // merge order) while preserving overall order.
      // CASES-ROOT NO-LAYERS GATE (NATE 2026-06-22)  -  when no Case is entered
      // (the cases-list / root view) the map renders NO data overlays: NATE's
      // "no case layers should be loaded when we are in the cases section; they
      // should only be rendered when we have entered a Case." We force the
      // reconcile target to the EMPTY set + an AUTHORITATIVE replace, so any
      // overlay still on the map from a previously-viewed Case is torn down (the
      // teardown loop below still defers to the cache's allowsEvict, but at root
      // layerCache.activeCaseId is null so allowsEvict returns true  -  nothing is
      // protected). The legend is cleared in the same spirit at the subscription.
      const rootView = !caseActiveRef.current;
      const rawLayers = rootView ? [] : payload.loaded_layers ?? [];
      const dedupById = new Map<string, (typeof rawLayers)[number]>();
      for (const l of rawLayers) {
        dedupById.set(l.layer_id, l);
      }
      // BUG 2 (random-reorder): order the add/stack loop by the SHARED comparator
      // (z_index desc, layer_id tiebreak) instead of raw Map-insertion order, so
      // the overlay stack matches the LayerPanel rows + App `layers` BY
      // CONSTRUCTION. The add loop registers bottom-to-top; addLayer with no
      // beforeId appends on TOP, so iterating top-first then bottom-up (reverse)
      // would leave the topmost on top - but to keep stacking identical to the
      // panel's top-first contract we add in top-first order and let the explicit
      // applyLayerOrder below (when a z-override exists) finalize. The key
      // invariant this enforces is a DETERMINISTIC total order on a null z_index.
      const currentLayers = Array.from(dedupById.values()).sort(
        compareLayersTopFirst as (
          a: (typeof rawLayers)[number],
          b: (typeof rawLayers)[number],
        ) => number,
      );
      const currentIds = new Set(currentLayers.map((l) => l.layer_id));

      // job-0357 (per-Case layer DURABILITY)  -  REMOVE only on an AUTHORITATIVE
      // replace. `replace_layers` is the client-only hint App.tsx stamps:
      //   - true / absent -> full replace-not-reconcile (Case switch / exit, or
      //     a server snapshot received while the socket is healthy -> live adds
      //     AND deletes apply). Absent defaults to true to preserve the
      //     historical behavior for older callers + unit fixtures.
      //   - false -> additive reconcile: ADD/update layers in the snapshot but
      //     do NOT tear down tracked overlays absent from it. Set for server
      //     snapshots received while the socket is NOT `connected` (the
      //     disconnect / reconnect window) so a transient EMPTY or partial
      //     snapshot during a bare WS reconnect can never wipe the active
      //     Case's already-rendered layers (the bug this job fixes). The
      //     agent's resume replay carries the FULL persisted layer set, so on a
      //     healthy reconnect it lands as an idempotent no-op either way.
      // At the cases-list / root view (rootView) the clear is ALWAYS
      // authoritative so the teardown loop runs and drops every overlay.
      const authoritativeReplace =
        rootView ||
        (payload as { replace_layers?: boolean }).replace_layers !== false;

      // Remove layers that are gone (replace-not-reconcile).
      //
      // F84 ROOT-CAUSE FIX: a session-state replace (Case switch / Case exit
      // with loaded_layers:[]) MUST drop EVERY currently-rendered overlay whose
      // layer_id is not in the new set  -  raster AND inline-GeoJSON vector. The
      // prior code only removed the single MapLibre layer named `id` plus its
      // source. That is correct for rasters (one MapLibre layer per source) but
      // WRONG for vectors: `registerVectorOnMap` adds SEVERAL MapLibre layers
      // per geojson source  - 
      //     polygon:      `${id}` (fill) + `${id}-outline` (line)
      //     dense point:  `${id}-clusters` + `${id}-cluster-count` + `${id}`
      // so removing only `${id}` left e.g. the `${id}-outline` layer still
      // referencing the source. MapLibre then THROWS on removeSource(id)
      // ("Source can't be removed while layer is using it"), and because that
      // throw was uncaught it aborted the whole removal loop  -  so WDPA-style
      // polygon vectors persisted across Case switches / Case exit (the bug).
      //
      // Fix: remove EVERY member of the layer group (via layerGroupMemberIds,
      // the same bottom-to-top member list registerVectorOnMap built) BEFORE
      // removing the source, each guarded so one bad call can't abort the loop.
      // An empty currentIds (loaded_layers:[]) => every tracked overlay is gone
      // => all overlays removed (fresh slate). Basemap layers are never tracked
      // in addedSourceIds, so they are untouched.
      //
      // job-0357: this teardown is SKIPPED entirely on a non-authoritative
      // (additive) reconcile  -  a reconnect top-up never removes durable layers.
      // The ADD/update loop below always runs, so an additive snapshot still
      // registers any newly-rendered layer it carries.
      if (authoritativeReplace) {
      // Fresh slate on Case switch / authoritative replace: clear any lingering
      // feature highlight + inspect popup so they never carry across Cases
      // (job-0357 must-fix). A bare reconnect is replace_layers===false and does
      // NOT reach here, so durable layers (and a highlight) survive a reconnect.
      try {
        if (m.getSource(FEATURE_HIGHLIGHT_SOURCE_ID)) clearFeatureHighlight(m);
      } catch {
        /* highlight already gone  -  best-effort */
      }
      setFeaturePopup(null);
      // job-0179 (per-Case client cache  -  "the seatbelt")  -  the shared cache is
      // the final teardown arbiter: even on an authoritative replace, an overlay
      // is torn down ONLY if the cache agrees it may be evicted. On a genuine
      // Case switch / exit / delete the cache has already dropped the layer (so
      // allowsEvict is true and teardown proceeds exactly as before); the gate
      // ONLY ever PREVENTS a teardown  -  it can never force one  -  so a layer the
      // cache still tracks (omitted by a stale frame that nonetheless slipped in
      // as authoritative) survives. Resolved against the cache's active Case.
      const evictCaseId = layerCache.activeCaseId;
      for (const id of addedSourceIds.current) {
        if (!currentIds.has(id) && layerCache.allowsEvict(evictCaseId, id)) {
          // Remove all MapLibre paint layers belonging to this logical layer
          // (fill + outline, or cluster + cluster-count + points, or the lone
          // raster/point/line layer) so the source is no longer referenced.
          for (const member of layerGroupMemberIds(m, id)) {
            try {
              m.removeLayer(member);
            } catch {
              // Mid-removal race / already gone  -  keep going so a single bad
              // member can't leave the rest (or the source) behind.
            }
          }
          try {
            if (m.getSource(id)) m.removeSource(id);
          } catch {
            // Source still referenced (a member we couldn't remove) or already
            // gone  -  best-effort; the next reconcile re-attempts.
          }
          addedSourceIds.current.delete(id);
          // job-0139: tear down vector bookkeeping too. Bump fetch generation
          // so any in-flight fetch for this layer_id resolves into a no-op.
          vectorGeomKinds.current.delete(id);
          layerStylePresets.current.delete(id);
          layerLegends.current.delete(id);
          vectorFetchGen.current.set(id, (vectorFetchGen.current.get(id) ?? 0) + 1);
        }
      }
      // deck.gl SPIKE (#169): mirror the teardown for deck-ROUTED layers. They
      // have no MapLibre source (they live in the overlay), so the addedSourceIds
      // loop above never touches them - drop their bookkeeping here on the same
      // cache-gated authoritative replace so a Case switch / exit / delete clears
      // footprints from the overlay too. The actual overlay.setProps rebuild below
      // emits the surviving set (the removed id is simply absent from it).
      for (const id of Array.from(deckRoutedIds.current)) {
        if (!currentIds.has(id) && layerCache.allowsEvict(evictCaseId, id)) {
          deckRoutedIds.current.delete(id);
          deckRoutedLayers.current.delete(id);
        }
      }
      }

      // C3 (job-0356 / per-case-layer-durability)  -  the CLIENT is the source of
      // truth for visibility across a server replay. A genuine fresh-socket
      // resume re-asserts visible:true for every active-Case layer (the server
      // keeps no per-user visibility state), which without this guard would
      // un-hide a layer the user had explicitly hidden. Read the persisted
      // override map ONCE per reconcile pass and prefer it  -  but ONLY for
      // layer_ids the user EXPLICITLY toggled (the hasOwnProperty guard below).
      // INVARIANT: a never-toggled VISIBLE layer has no override key, so it keeps
      // rendering across reconnect (we never blanket-hide).
      const visibilityOverrides = readLayerVisibilityOverrides();

      // Add new layers; update opacity/visibility on existing.
      // Cast to WireLayerSummary: the agent emits `uri` on the wire even though
      // contracts.ts uses `source_url` (schema mismatch; tracked as OQ-0068-URI).
      for (const _layer of currentLayers) {
        const layer = _layer as unknown as WireLayerSummary;
        // job-0179 (per-Case client cache  -  "the seatbelt"): the user's persisted
        // VIEW-OVERRIDE (opacity / visibility, set via the LayerPanel) WINS over
        // the wire value so a (re-)add after a reconnect / re-render never resets
        // the user's edit. The cache subsumes the localStorage visibility map; we
        // still read that map as a fallback for back-compat with older sessions
        // whose override was written before this cache existed.
        const cacheOverride = layerCache.getOverride(
          layerCache.activeCaseId,
          layer.layer_id,
        );
        const opacity =
          cacheOverride?.opacity !== undefined
            ? cacheOverride.opacity
            : layer.opacity ?? 1;
        // C3: effectiveVisible = the user's explicit override when present, else
        // the wire value. The cache override wins first; then the legacy
        // localStorage visibility map (hasOwnProperty guards so only an
        // explicitly-toggled layer_id is affected); else the server `visible`.
        const visible =
          cacheOverride?.visible !== undefined
            ? cacheOverride.visible
            : Object.prototype.hasOwnProperty.call(
                  visibilityOverrides,
                  layer.layer_id,
                )
              ? visibilityOverrides[layer.layer_id] === true
              : layer.visible !== false;
        const layerType = layer.layer_type;
        // C3: the vector / vector-tile registration helpers read `visible` off
        // the layer object they receive (not the local `visible` const), so a
        // freshly RECREATED vector layer would otherwise ignore the user's hide.
        // Stamp the effective visibility onto a shallow copy and pass THAT to the
        // new-layer creation branches so a recreated layer is created hidden when
        // the user hid it. (The raster branch + existing-layer update branch use
        // the local `visible` const directly.)
        const effectiveLayer = { ...layer, visible } as WireLayerSummary;
        // job-0258: keep the preset bookkeeping current for the map-command
        // opacity path (Pelicun fill multiplier).
        layerStylePresets.current.set(layer.layer_id, layer.style_preset ?? null);
        // DATA-DRIVEN LEGEND: mirror the legend too so the opacity-slider path
        // keeps a graduated/categorical fill bold (resolvePolygonFillOpacity).
        layerLegends.current.set(layer.layer_id, layer.legend ?? null);

        // deck.gl SPIKE (#169) + LAZY-LOAD: ROUTE heavy/footprint inline-GeoJSON
        // vectors to the interleaved deck.gl overlay instead of the MapLibre vector
        // path. Light vectors, vector-tile layers, and rasters fall through
        // unchanged below. The overlay is rebuilt declaratively once per pass
        // (overlay.setProps), so we just RECORD the latest layer object here
        // (covering both first-add and a metadata/inline refresh) and skip the
        // MapLibre add/update paths.
        //
        // The routing predicate is PURE + deck.gl-free, so we decide it WITHOUT
        // loading the bundle. The FIRST deck-routed layer kicks ensureDeckLoaded()
        // (one-time `await import(...)` of @deck.gl/mapbox + the builder), which on
        // resolve constructs the overlay + re-runs this reconcile so the setProps
        // rebuild block below paints it. We still RECORD + `continue` while the
        // import is in flight (the layer is tracked; it renders on the deck rebuild
        // once the overlay mounts) so it never double-renders on the MapLibre path.
        if (shouldRouteToDeck(layer as unknown as DeckRoutableLayer)) {
          if (!deckOverlay.current) ensureDeckLoaded();
          deckRoutedIds.current.add(layer.layer_id);
          // Store the EFFECTIVE layer (carries the visibility-resolved copy) so a
          // recreated/refreshed deck layer honors the user's hide on rebuild.
          deckRoutedLayers.current.set(
            layer.layer_id,
            effectiveLayer as unknown as DeckRoutableLayer,
          );
          continue;
        }

        if (addedSourceIds.current.has(layer.layer_id)) {
          // Update paint/layout on existing layer via the shared helpers
          // (job-0258)  -  these branch on the tracked geometry kind AND cover
          // sublayers (`-outline`, `-clusters`, `-cluster-count`) that the
          // previous inline branch missed.
          if (m.getLayer(layer.layer_id)) {
            const geomKind = vectorGeomKinds.current.get(layer.layer_id);
            applyLayerOpacity(m, layer.layer_id, opacity, geomKind, layer.style_preset, layer.legend);
            applyLayerVisibility(m, layer.layer_id, visible);
          }
          continue;
        }

        // New layer  -  branch on layer_type.
        if (
          (layerType === "vector" || layerType === "geojson") &&
          typeof layer.vector_tile_url === "string" &&
          layer.vector_tile_url.length > 0
        ) {
          // F94: DENSE vector path. The agent published a vector-tile source
          // (MVT / PMTiles) instead of inline GeoJSON, so MapLibre fetches +
          // draws only the tiles in view. Synchronous register (no async
          // fetch); guard on style-ready the same way the inline path does.
          addedSourceIds.current.add(layer.layer_id);
          let styleReady = false;
          try {
            styleReady = mapStyleReady(m);
          } catch {
            styleReady = false;
          }
          if (styleReady) {
            registerVectorTileLayer(m, effectiveLayer as unknown as Parameters<typeof registerVectorTileLayer>[1], vectorGeomKinds);
          } else {
            // Defer until the style settles (mirrors addVectorLayer's retry).
            m.once("idle", () => {
              if (!addedSourceIds.current.has(layer.layer_id)) return;
              try {
                if (!m.isStyleLoaded()) return;
              } catch {
                return;
              }
              registerVectorTileLayer(m, effectiveLayer as unknown as Parameters<typeof registerVectorTileLayer>[1], vectorGeomKinds);
            });
          }
        } else if (layerType === "vector" || layerType === "geojson") {
          // job-0139: vector layer path. Fetch GeoJSON/FlatGeobuf, add a
          // GeoJSON source, paint per geometry kind.
          //
          // We mark the slot reserved (addedSourceIds.add) BEFORE the async
          // fetch resolves so a second session-state push during the fetch
          // doesn't double-register. The fetch generation counter guards
          // against the re-add race where a layer is removed + re-added
          // before the original fetch resolves.
          addedSourceIds.current.add(layer.layer_id);
          const gen = (vectorFetchGen.current.get(layer.layer_id) ?? 0) + 1;
          vectorFetchGen.current.set(layer.layer_id, gen);
          void addVectorLayer(m, effectiveLayer, gen, vectorFetchGen, vectorGeomKinds, addedSourceIds);
        } else {
          // Raster (existing path).
          //
          // MapLibre paints layers in insertion order; we don't pass an
          // explicit beforeId here because the basemap was added first via
          // the seed style spec, so any flood layer added now will paint
          // ABOVE it (correct stacking). The dark-theme swap path
          // (`applyTheme` below) preserves this invariant by re-adding the
          // basemap with `beforeId =` first overlay layer, so overlays
          // always stay on top of whichever basemap is active.
          // job-0171: pass style_preset so the LAYERS= shim can recover the
          // missing parameter for tools that emit only the bare WMS endpoint
          // (e.g. fetch_nexrad_reflectivity). See OQ-0171-WMS-URL-CONTRACT.
          const tileUrl = buildWmsTileUrl(layer.uri, layer.style_preset ?? null);

          // RASTER ADD HARDENING (Lane 3 finding #2): mirror the vector-tile
          // branch's guards so ONE bad raster (a duplicate / half-torn-down id,
          // an addLayer during a dark-theme style churn, a malformed uri) cannot
          // throw and ABORT the whole add loop, leaving every later raster in the
          // snapshot unpainted. (1) Reserve the slot BEFORE the add (like the
          // vector branches) so a re-push during a deferral does not double-
          // register. (2) Gate on mapStyleReady with an m.once('idle', ...)
          // deferral. (3) Wrap addSource+addLayer in try/catch (log + continue).
          addedSourceIds.current.add(layer.layer_id);

          // raster-resampling: nearest preserves discrete COG cell boundaries
          // (job-0078 diagnosis). Without this, MapLibre's default `linear`
          // bilinear interpolation smears flood-depth cells across screen pixels,
          // making it impossible to visually verify per-cell alignment with
          // underlying basemap features (streets, building blocks). nearest
          // shows the source-projection grid 1:1  -  the user can see that each
          // flood cell sits over the specific street/lot it covers, which is
          // the only visually-irrefutable proof of geographic alignment.
          const addRaster = (): void => {
            // The layer may have been removed/re-added during a deferral; only
            // act if it is still the tracked source for this id.
            if (!addedSourceIds.current.has(layer.layer_id)) return;
            try {
              if (m.getSource(layer.layer_id)) return; // already added (idle race)
              m.addSource(layer.layer_id, {
                type: "raster",
                tiles: [tileUrl],
                tileSize: 256,
              });
              m.addLayer({
                id: layer.layer_id,
                type: "raster",
                source: layer.layer_id,
                paint: {
                  "raster-opacity": opacity,
                  "raster-resampling": "nearest",
                },
                layout: { visibility: visible ? "visible" : "none" },
              });
            } catch (err) {
              // One bad raster must not abort the loop / deferral. Drop the
              // reservation so a later re-push can retry, and continue.
              addedSourceIds.current.delete(layer.layer_id);
              console.warn(
                `[grace2] raster layer add failed (${layer.layer_id}); skipping`,
                err,
              );
            }
          };

          let rasterStyleReady = false;
          try {
            rasterStyleReady = mapStyleReady(m);
          } catch {
            rasterStyleReady = false;
          }
          if (rasterStyleReady) {
            addRaster();
          } else {
            // Defer until the style settles (mirrors the vector branch retry).
            m.once("idle", () => {
              try {
                if (!m.isStyleLoaded()) return;
              } catch {
                return;
              }
              addRaster();
            });
            // BUG 3: nudge a repaint so a QUIESCENT cold map actually emits the
            // `idle` this deferral waits on (otherwise the raster source add stalls
            // until the next WS session-state push - the cold-raster symptom).
            try {
              m.triggerRepaint();
            } catch {
              /* triggerRepaint absent - harmless */
            }
          }
        }
      }

      // job-0179 (per-Case client cache  -  "the seatbelt") + BUG 2 (random
      // reorder): re-assert the deterministic stack order after every (re-)add so
      // a reconnect / re-render never leaves the overlay stack in raw add order.
      // The effective per-layer z is the user's cached zIndex override when
      // present, else the wire `z_index` (null/undefined -> 0). We sort with the
      // SHARED total-order comparator (z desc, layer_id tiebreak) so the map stack
      // matches the LayerPanel rows + App `layers` BY CONSTRUCTION, even when the
      // agent emits null z_index for every layer. (Previously this only ran when a
      // zIndex override existed, leaving the no-override path on add-order - which,
      // with null z, differed from the panel order = the visible "random reorder".)
      const caseOverrides = layerCache.overridesFor(layerCache.activeCaseId);
      const ordered = currentLayers
        .filter((l) => addedSourceIds.current.has(l.layer_id))
        .map((l) => {
          const ov = caseOverrides[l.layer_id];
          const wireZ = (l as { z_index?: number | null }).z_index;
          const z =
            ov?.zIndex !== undefined
              ? ov.zIndex
              : typeof wireZ === "number"
                ? wireZ
                : 0;
          return { id: l.layer_id, z };
        })
        // Same comparator shape as compareLayersTopFirst (z desc, id tiebreak),
        // applied over the EFFECTIVE z (override-or-wire) the map stacks against.
        .sort((a, b) => b.z - a.z || a.id.localeCompare(b.id))
        .map((e) => e.id);
      if (ordered.length > 0) applyLayerOrder(m, ordered);

      // deck.gl SPIKE (#169): rebuild the interleaved overlay from the deck-ROUTED
      // set, declaratively. setProps with the full layer array each pass IS the
      // add/update/remove path (deck diffs by layer id), so a removed id simply
      // drops out, an opacity/visibility/z override re-applies, and a refreshed
      // inline re-paints - all in one call.
      //
      // LAZY-LOAD guard: both the overlay AND the lazily-imported builder must be
      // resolved. On the very first footprint pass neither is ready yet (the import
      // is in flight); ensureDeckLoaded() re-runs this reconcile on resolve, at
      // which point both refs are populated and the layers paint synchronously.
      const buildDeckLayer = buildDeckGeoJsonLayerFn.current;
      if (deckOverlay.current && buildDeckLayer) {
        // Order deck layers by the SAME effective-z comparator the MapLibre stack
        // uses (override.zIndex -> wire z_index -> 0), top-first, so the overlay's
        // own draw order matches the LayerPanel.
        const deckOrdered = currentLayers
          .filter((l) => deckRoutedIds.current.has(l.layer_id))
          .map((l) => {
            const ov = caseOverrides[l.layer_id];
            const wireZ = (l as { z_index?: number | null }).z_index;
            const z =
              ov?.zIndex !== undefined
                ? ov.zIndex
                : typeof wireZ === "number"
                  ? wireZ
                  : 0;
            return { id: l.layer_id, z };
          })
          .sort((a, b) => b.z - a.z || a.id.localeCompare(b.id));

        // beforeId: place each deck layer so the MIXED MapLibre+deck stack still
        // respects LayerPanel order. `ordered` is the MapLibre stack top-first; the
        // FIRST MapLibre layer whose effective z is <= the deck layer's z is the one
        // the deck layer should draw BELOW (i.e. deck inserts BEFORE it). Reuses the
        // same effective-z the applyLayerOrder intent above computes. When no such
        // MapLibre layer exists the deck layer goes on top (beforeId undefined).
        const mapLibreZById = new Map<string, number>();
        for (const l of currentLayers) {
          if (!addedSourceIds.current.has(l.layer_id)) continue;
          const ov = caseOverrides[l.layer_id];
          const wireZ = (l as { z_index?: number | null }).z_index;
          mapLibreZById.set(
            l.layer_id,
            ov?.zIndex !== undefined
              ? ov.zIndex
              : typeof wireZ === "number"
                ? wireZ
                : 0,
          );
        }
        const beforeIdFor = (deckZ: number): string | undefined => {
          // `ordered` is MapLibre ids top-first (highest z first). Walk bottom-up
          // (lowest z first) to find the lowest MapLibre layer that sits ABOVE this
          // deck layer (its z >= deckZ) and exists on the map; deck draws before it.
          for (let i = ordered.length - 1; i >= 0; i--) {
            const id = ordered[i]!;
            const z = mapLibreZById.get(id) ?? 0;
            if (z >= deckZ) {
              try {
                if (m.getLayer(id)) return id;
              } catch {
                /* layer mid-removal - skip */
              }
            }
          }
          return undefined;
        };

        const suppress = deckPickSuppressed.current;
        const deckLayers = deckOrdered.map(({ id, z }) => {
          const routed = deckRoutedLayers.current.get(id)!;
          const ov = layerCache.getOverride(layerCache.activeCaseId, id);
          const built = buildDeckLayer(routed, ov, {
            suppressPicking: suppress,
            onClick: onDeckClickRef.current ?? undefined,
          });
          // beforeId is a deck.gl interleaved-mode prop (not a GeoJsonLayer ctor
          // field), so clone with it. Cast: deck's per-layer beforeId is accepted
          // by MapboxOverlay's interleaved path but not in the layer prop types.
          const bid = beforeIdFor(z);
          return bid
            ? (built.clone({ beforeId: bid } as unknown as Record<string, unknown>))
            : built;
        });
        try {
          deckOverlay.current.setProps({ layers: deckLayers });
        } catch (err) {
          console.warn("[grace2] deck overlay setProps failed", err);
        }
      }
    };

    // Expose applyLatest so the caseActive sync effect can re-run the reconcile
    // when the Case-entered state flips (without waiting for a session-state push).
    applyLatestRef.current = applyLatest;
    // deck.gl SPIKE (#169): the suppress-flip effect re-runs the full (idempotent)
    // reconcile so the overlay rebuilds with deck layers pickable / non-pickable.
    // applyLatest is a no-op when there is no retained session-state, so an early
    // flip before the first push is harmless.
    rebuildDeckLayersRef.current = applyLatest;

    const unsub = subscribeSessionState((payload) => {
      latestSessionState.current = payload;

      // job-0321 (F43)  -  capture the ordered layer list the legend needs from
      // THIS component's own subscription (App.tsx no longer owns the legend).
      // Order top-of-stack-first (z_index DESCENDING) to match LayerPanel and
      // LayerLegend's `layers.find(...)` "topmost wins" contract. The wire
      // payload carries z_index when present; when it's absent on every layer
      // (older snapshots) the sort is stable and preserves emission order,
      // which is already roughly top-of-stack-last -> we reverse in that case so
      // the most-recently-added (topmost) layer is first. We detect "no usable
      // z_index" as: every layer's z_index is undefined.
      // CASES-ROOT NO-LAYERS GATE (NATE 2026-06-22)  -  no legend content at the
      // cases-list / root view (no Case entered); the legend only has content
      // once a Case is entered, mirroring the overlay gate in applyLatest.
      const raw = caseActiveRef.current
        ? ((payload.loaded_layers ?? []) as ProjectLayerSummary[])
        : [];
      // BUG 2 (random-reorder): order the legend with the SHARED deterministic
      // comparator (z desc, layer_id tiebreak) so it matches the panel + map +
      // App order by construction - including when the agent emits null z_index on
      // every layer (the old `anyZ ? sort : reverse` branch gave a NON-total order
      // there, which let the legend disagree with the other surfaces).
      const ordered = [...raw].sort(compareLayersTopFirst);
      // BUG 1 (secondary - heartbeat re-render storm): the server re-emits a full
      // session-state every ~12-25s. Without a guard, setLegendLayers(ordered)
      // built + committed a fresh array on EVERY heartbeat, re-rendering the whole
      // MapView subtree forever. Commit ONLY when the ordered list actually
      // changed (order-sensitive id+z compare against the last committed one), so
      // an unchanged heartbeat is a no-op and React bails.
      if (!legendOrderEqual(lastLegendOrderedRef.current, ordered)) {
        lastLegendOrderedRef.current = ordered;
        setLegendLayers(ordered);
      }

      const m = map.current;
      if (!m) return;
      const ready = m.isStyleLoaded();
      if (ready) {
        applyLatest();
      }
      // Whether or not we applied synchronously, attach an idle handler so
      // any subsequent style-load completes the reconciliation. `idle` fires
      // once per loop-tick when all in-flight requests settle.
      m.once("idle", applyLatest);
      // BUG 3 (cold rasters don't paint until WS connect): when the style was NOT
      // ready we just deferred to `idle`, but a QUIESCENT cold map never re-emits
      // `idle` on its own, so the apply would stall until the next WS push. Nudge
      // a repaint so the map converges + fires the idle this deferral waits on.
      if (!ready) {
        try {
          m.triggerRepaint();
        } catch {
          /* triggerRepaint absent (older map / test mock) - harmless */
        }
      }
    });
    return unsub;
  }, [subscribeSessionState]);

  // CASES-ROOT NO-LAYERS GATE (NATE 2026-06-22)  -  keep caseActiveRef in lockstep
  // and re-reconcile when the Case-entered state flips. Returning to the cases
  // list (caseActive false) tears down every overlay (the reconcile force-clears
  // when rootView) AND clears the legend content directly (the legend list is
  // owned by the subscription, which won't re-fire on a pure prop flip). Entering
  // a Case (caseActive true) re-applies the latest session-state so any persisted
  // layers re-paint. The applyLatest call is idempotent (replace-not-reconcile),
  // so re-running it here never double-adds.
  useEffect(() => {
    caseActiveRef.current = caseActive;
    if (!caseActive) {
      // BUG 1 (secondary): keep the heartbeat-guard ref in lockstep with the
      // direct clear so re-entering a Case re-commits the legend (an empty ref
      // would otherwise let an identical first frame fail the equality guard - or
      // worse, a stale non-empty ref could suppress the real re-populate).
      lastLegendOrderedRef.current = [];
      setLegendLayers([]);
      // MOBILE BBOX-ON-EXIT (NATE 2026-06-24): tear down the on-map AOI here, on
      // the DETERMINISTIC caseActive=false flip, instead of relying ONLY on the
      // laggy `clear-analysis-extent` / `reset-view` bus map-commands. Those
      // commands clear the AOI bbox / stashed map AOI / dashed analysis-extent
      // but are mapStyleReady/`idle`-GATED and depend on a map re-projection to
      // re-push the cleared screen rect up (App.tsx:1144 flags this path as one
      // that "can lag / not re-fire" when the box is busy/asleep). On MOBILE the
      // open Cases drawer occludes the map so the user never pans -> the
      // aoiBbox->legendRect projection effect never re-fires, and a sleeping box
      // starves `idle`, so the dashed AOI rectangle LINGERS after backing out of
      // a Case. This flip runs synchronously on EVERY case exit for BOTH layouts:
      //   - lastZoomToCorners=null so a late style (re)load can't re-assert the
      //     rectangle via the moveend/idle redraw path;
      //   - setAoiBbox(null) drives the projection effect (dep [aoiBbox]) to
      //     setLegendRect(null) immediately, which pushes null up via
      //     onAoiScreenRectChange -> App clears aoiScreenRect + the
      //     BboxProgressOverlay durably, no map re-projection required;
      //   - clearAnalysisExtent removes the dashed rectangle WITHOUT waiting on
      //     `idle`, and setMapAoiBbox(null) drops the stashed clip bbox so a new
      //     Case's vectors are not clipped to the prior AOI.
      // The existing bus clear-analysis-extent/reset-view commands stay as
      // belt-and-suspenders (camera reset + redundant clear).
      lastZoomToCorners.current = null;
      setAoiBbox(null);
      const m = map.current;
      if (m) {
        try {
          setMapAoiBbox(m, null);
          clearAnalysisExtent(m);
        } catch {
          /* best-effort; a missing/half-built extent self-heals on next draw */
        }
      }
    }
    applyLatestRef.current?.();
  }, [caseActive]);

  // NATE 2026-06-22 (item 4) - recolor the SINGLE analysis-extent AOI rectangle
  // by sim state: purple while a sim runs, blue when it ends. We mutate the
  // existing line layer's stroke in place (setAnalysisExtentSimColor) - NO second
  // box is drawn. The ref keeps the (stable-closure) redraw paths painting the
  // right color when they re-assert the rectangle.
  useEffect(() => {
    simRunningRef.current = simRunning;
    const m = map.current;
    if (m) setAnalysisExtentSimColor(m, simRunning);
  }, [simRunning]);

  // LANE E (3D AOI pulse-glow) - in 3D terrain mode the 2D scan overlay is
  // suppressed (resolveBboxProgress -> "none") because its axis-aligned rect no
  // longer traces the pitched/rotated AOI box. Carry the "working" cue with an
  // in-map PULSE-GLOW on the REAL AOI line layer instead: it is geographic
  // geometry, so it drapes over terrain and follows the camera, hugging the box.
  // Start the rAF glow when 3D is on AND an AOI box is present; stop + restore
  // the static line paint when 3D turns off or the box clears. (re-keyed on
  // simRunning so the static restore on teardown lands after a sim-color swap;
  // the glow itself only touches width/opacity/blur, never color, so the
  // sim-color effect above keeps owning blue<->purple.)
  useEffect(() => {
    const m = map.current;
    if (!m || !terrain3dEnabled || aoiBbox === null) return undefined;
    let handle: AoiPulseGlowHandle | null = null;
    // The AOI line layer may not be added yet (zoom-to draw can race the 3D
    // toggle); arm on the next idle if it is missing, else start now.
    const begin = (): void => {
      const mm = map.current;
      if (!mm) return;
      if (!mm.getLayer(ANALYSIS_EXTENT_LINE_LAYER_ID)) {
        mm.once("idle", begin);
        return;
      }
      handle = startAoiPulseGlow(mm, ANALYSIS_EXTENT_LINE_LAYER_ID);
    };
    begin();
    return () => {
      handle?.stop();
      // After stopping the glow, re-assert the correct (blue/purple) static
      // stroke for the current sim state (the glow left width/opacity/blur at
      // their static values; color is owned by the sim-color effect).
      const mm = map.current;
      if (mm) setAnalysisExtentSimColor(mm, simRunningRef.current);
    };
  }, [terrain3dEnabled, aoiBbox, simRunning]);

  // Subscribe to theme prop changes and swap the basemap source+layer
  // (job-0076 bundled enhancement). The swap pattern:
  //   1. Pick the lowest-priority existing flood-overlay layer as the
  //      beforeId target so the new basemap renders UNDER everything else.
  //   2. Remove the current basemap layer + source.
  //   3. Add the new basemap source + layer, passing beforeId so MapLibre
  //      inserts it underneath the flood overlays.
  // Order-preservation note: flood overlays were added via addLayer with no
  // beforeId, so they live at the TOP of the layer stack. Re-inserting the
  // basemap underneath them keeps the same painter's-algorithm order.
  useEffect(() => {
    const m = map.current;
    if (!m) return;

    const applyTheme = () => {
      const currentMap = map.current;
      if (!currentMap || !currentMap.isStyleLoaded()) {
        // Defer until style is ready.
        currentMap?.once("idle", applyTheme);
        return;
      }

      const style = currentMap.getStyle();
      const layerIds = style.layers.map((l) => l.id);

      // The lowest flood-overlay layer (i.e. the first one we added beyond the
      // basemap layers) is our `beforeId` target  -  the new basemap layer
      // should be inserted just before it. If no flood overlays exist yet,
      // append; the basemap will be the top layer until a flood overlay is
      // added, at which point the flood overlay will paint above it (correct).
      const firstFloodLayer = layerIds.find(
        (id) => id !== BASEMAP_LAYER_ID && id !== DARK_BASEMAP_LAYER_ID && id !== "osm-fallback-basemap",
      );

      if (theme === "dark") {
        // Remove light basemap layer+source if present.
        if (currentMap.getLayer(BASEMAP_LAYER_ID)) currentMap.removeLayer(BASEMAP_LAYER_ID);
        // (Leave the qgis-wms source in place  -  removing it can race with
        // any pending tile requests; harmless to keep since it has no layer
        // referencing it.)
        // Add dark basemap if not already there.
        if (!currentMap.getSource(DARK_BASEMAP_SOURCE_ID)) {
          currentMap.addSource(DARK_BASEMAP_SOURCE_ID, {
            type: "raster",
            tiles: [CARTO_DARK_TILE_TEMPLATE],
            tileSize: 256,
            attribution: CARTO_DARK_ATTRIBUTION,
            maxzoom: 19,
          });
        }
        if (!currentMap.getLayer(DARK_BASEMAP_LAYER_ID)) {
          currentMap.addLayer(
            {
              id: DARK_BASEMAP_LAYER_ID,
              type: "raster",
              source: DARK_BASEMAP_SOURCE_ID,
              minzoom: 0,
              maxzoom: 22,
            },
            firstFloodLayer,
          );
        }
      } else {
        // light theme  -  restore QGIS WMS basemap.
        if (currentMap.getLayer(DARK_BASEMAP_LAYER_ID)) currentMap.removeLayer(DARK_BASEMAP_LAYER_ID);
        if (!currentMap.getLayer(BASEMAP_LAYER_ID)) {
          // Source was kept; just re-add the layer.
          currentMap.addLayer(
            {
              id: BASEMAP_LAYER_ID,
              type: "raster",
              source: BASEMAP_SOURCE_ID,
              minzoom: 0,
              maxzoom: 22,
            },
            firstFloodLayer,
          );
        }
      }

      // AWS-migration hardening (bbox track): a theme swap mutates the style;
      // if the analysis-extent rectangle was ever drawn, re-assert it so a
      // future setStyle-based theme path (or any style churn that dropped it)
      // self-heals. drawAnalysisExtent is idempotent + missing-layer-healing,
      // so this is a no-op when the extent is already intact.
      if (lastZoomToCorners.current) {
        try {
          drawAnalysisExtent(
            currentMap,
            lastZoomToCorners.current,
            simRunningRef.current,
          );
        } catch (err) {
          if (import.meta.env.DEV) {
            // eslint-disable-next-line no-console
            console.warn("[MapView] extent redraw on theme change threw:", err);
          }
        }
      }
    };

    applyTheme();
    // No cleanup  -  basemap state lives in the map's style; the next theme
    // change will reconcile it.
  }, [theme]);

  // Subscribe to map-command for zoom-to and transient camera/animation verbs
  // (job-0068, change 5 client side) PLUS the layer-control verbs
  // set-layer-opacity / set-layer-visibility / set-layer-order (job-0258  - 
  // the LayerPanel user controls emit these through the App bus; until this
  // handler existed they never reached the map, which is why the panel's
  // opacity slider and drag-reorder were dead in the live demo). Layer CRUD
  // (load-layer / remove-layer) stays DEFERRED to the session-state path per
  // layer-emission-contract.md.
  // WireMapCommand extends frozen contracts.ts MapCommandPayload with zoom-to
  // (which is deferred in contracts.ts but needed here per kickoff).
  useEffect(() => {
    if (!subscribeMapCommand) return undefined;
    return subscribeMapCommand((payload: WireMapCommand) => {
      // BUG 3 (cold camera): RETAIN the latest zoom-to bbox BEFORE the `!m` guard,
      // so a zoom-to that arrives while the map does not yet exist / its style has
      // not loaded is replayed on the map's first `load` (the camera then snaps to
      // the AOI and tiles fetch). A clear/reset forgets it. Done at the very top so
      // even a payload dropped by the guard below is still captured here.
      if (payload.command === "zoom-to") {
        const zb = (payload as ZoomToCommand).args?.bbox;
        if (zb && zb.length === 4) {
          pendingZoomToRef.current = zb as [number, number, number, number];
        }
      } else if (
        payload.command === "clear-analysis-extent" ||
        payload.command === "reset-view"
      ) {
        pendingZoomToRef.current = null;
      }
      const m = map.current;
      if (!m) return;
      if (payload.command === "zoom-to") {
        const { bbox } = (payload as ZoomToCommand).args;
        if (bbox && bbox.length === 4) {
          // BUG 3: the zoom-to apply body is a named closure stored in
          // applyZoomToRef so the map "load" handler can REPLAY a retained cold
          // zoom-to through THIS exact path (fitBounds + AOI stash + extent draw)
          // - a cold camera left at CONUS zoom fetched no tiles.
          const runZoomTo = (corners: [number, number, number, number]): void => {
          const m = map.current;
          if (!m) return;
          const [minLon, minLat, maxLon, maxLat] = corners;
          // Respect prefers-reduced-motion: a 1200ms camera flight is motion.
          // When the user has asked for reduced motion, jump (duration 0) so
          // there is no animation  -  the moveend below still fires synchronously
          // enough that the extent redraw lands without an animated sweep.
          const prefersReducedMotion =
            typeof window !== "undefined" &&
            typeof window.matchMedia === "function" &&
            window.matchMedia("(prefers-reduced-motion: reduce)").matches;
          m.fitBounds(
            [[minLon, minLat], [maxLon, maxLat]],
            {
              // LANE B #3 - panel-aware padding so the bbox centers in the
              // VISIBLE gutter, not behind the open chat / left rail. On mobile
              // the chat BOTTOM-SHEET occludes the lower map, so we also reserve
              // a bottom pad below the sheet top (legendSheetTopPx) - otherwise
              // the AOI "snaps to the center of the screen" partly behind the
              // sheet (NATE 2026-06-24 live-mobile feedback).
              padding: panelAwareFitPadding(
                m,
                40,
                leftPanelWidthPx,
                chatWidthPx ?? 0,
                chatCollapsed ?? false,
                mobile ?? false,
                legendSheetTopPx,
              ),
              duration: prefersReducedMotion ? 0 : 1200,
            },
          );
          // Remember the last extent corners so a late style (re)load (theme
          // setStyle, case-reopen remount) can re-assert the rectangle. Stays
          // inside this track's ownership (a ref, not the cross-track bus
          // replay buffer  -  see crossTrackChanges).
          lastZoomToCorners.current = corners;
          // job-0321 (F43)  -  mirror the corners into state so the legend can
          // re-project against the AOI box (the legend hangs off its bottom
          // edge). The projection effect recomputes legendAnchor whenever aoiBbox
          // or the camera changes.
          setAoiBbox(corners);
          // FIX 2 (vector AOI clip)  -  stash the AOI bbox on the map instance so
          // the module-level vector add path clips features to it.
          setMapAoiBbox(m, corners);
          // job-0294  -  ALSO outline the extent as a styled rectangle so the
          // user sees exactly what area is being measured. The fitBounds above
          // is camera-only; this draws the bbox on the map.
          //
          // AWS-migration root-cause fix (bbox track): the prior code wrapped
          // drawAnalysisExtent in a bare `catch {}` that SILENTLY SWALLOWED any
          // throw  -  so a transient MapLibre "style not done loading" /
          // source-not-ready / mid-camera-animation style-churn throw dropped
          // the rectangle forever (camera moved, no rectangle: the live
          // symptom). Now a throw RE-SCHEDULES the draw on the next idle, with
          // a small bounded retry counter so a persistently-broken style (dead
          // basemap WMS post-migration) can't loop unbounded. We also defer
          // while the style is not loaded (case-reopen replay can race the
          // first style load) AND re-assert AFTER the camera flight settles
          // (moveend) to cover the window where the raster/vector source add
          // churns the style mid-animation. drawAnalysisExtent is idempotent
          // and self-healing, so every extra invocation is harmless.
          let retries = 0;
          const MAX_RETRIES = 3;
          const drawExtent = (): void => {
            if (!map.current) return;
            // INCIDENT FIX 2026-06-16: a Case-exit clear-analysis-extent sets
            // lastZoomToCorners=null. If a redraw was already queued (the
            // moveend/idle re-assert below), it must NOT re-add the rectangle
            // after the clear  -  otherwise the AOI box persists after leaving the
            // Case (user-reported). Bail when the corners were cleared.
            if (lastZoomToCorners.current === null) return;
            // JOB WEB-AOI-LEGEND (#159)  -  SINGLE rectangle, replace-on-new. This
            // closure used to draw the per-invocation captured `corners`. A
            // moveend/idle callback queued by an EARLIER (smaller) zoom-to could
            // therefore fire AFTER a newer (floored, larger) zoom-to had already
            // setData-replaced the extent, redrawing the STALE small box over the
            // new large one  -  the "small box + large box both show" symptom. We
            // now always draw the CURRENT corners (lastZoomToCorners.current), so
            // a late callback re-asserts the latest extent instead of a stale
            // one. drawAnalysisExtent setData-replaces the single source, so the
            // map only ever holds one rectangle.
            const liveCorners = lastZoomToCorners.current;
            // INCIDENT FIX 2026-06-16: gate the AOI draw on the mapStyleReady
            // LATCH, not raw isStyleLoaded()  -  a hung raster tile keeps
            // isStyleLoaded() false forever and would stall the bounding-box
            // draw indefinitely (the "no bounding box" symptom). Once the style
            // has loaded once, draw regardless of stuck tiles.
            if (!mapStyleReady(map.current)) {
              map.current.once("idle", drawExtent);
              return;
            }
            try {
              drawAnalysisExtent(map.current, liveCorners, simRunningRef.current);
            } catch (err) {
              // Mid style-mutation race; re-schedule rather than drop. Bounded
              // so a permanently-broken style cannot loop forever.
              if (import.meta.env.DEV) {
                // eslint-disable-next-line no-console
                console.warn(
                  `[MapView] drawAnalysisExtent threw (retry ${retries + 1}/${MAX_RETRIES}):`,
                  err,
                );
              }
              if (retries < MAX_RETRIES && map.current) {
                retries += 1;
                map.current.once("idle", drawExtent);
              }
            }
          };
          drawExtent();
          // Re-assert AFTER the camera flight settles. The agent emits
          // session-state (raster/vector source add) BEFORE this zoom-to, but
          // the animated fitBounds keeps mutating the style for ~1200ms; a
          // redraw on moveend lands the dashed outline once the style is quiet.
          // Idempotent: drawAnalysisExtent setData-replaces + heals missing
          // layers, so this never double-adds.
          m.once("moveend", drawExtent);
          }; // end runZoomTo
          // Expose for the cold-load replay (bug 3), then run it now.
          applyZoomToRef.current = runZoomTo;
          runZoomTo(bbox as [number, number, number, number]);
        }
      } else if (payload.command === "clear-analysis-extent") {
        // ux-batch-1 (F14): Case exit (or opening a Case with no AOI) must not
        // leave the prior Case's analysis-extent rectangle on the map. Forget
        // the remembered corners FIRST so a late style (re)load can't re-assert
        // the rectangle via the moveend/idle redraw path, then remove it.
        lastZoomToCorners.current = null;
        // job-0321 (F43)  -  drop the AOI bbox so the legend falls back to its
        // bottom-center placement (no AOI to anchor to anymore).
        setAoiBbox(null);
        // FIX 2 (vector AOI clip)  -  clear the stashed AOI bbox so subsequent
        // vector layers (e.g. a new Case) aren't clipped to the prior AOI.
        setMapAoiBbox(m, null);
        // INCIDENT FIX 2026-06-16: gate on mapStyleReady, not raw
        // isStyleLoaded(). A hung tile (or a mid-flight camera animation) kept
        // isStyleLoaded() false, so the clear deferred forever and the AOI
        // rectangle PERSISTED after leaving a Case (user-reported). Once the
        // style has loaded once, removeLayer/removeSource are safe  -  clear now.
        if (mapStyleReady(m)) {
          try {
            clearAnalysisExtent(m);
          } catch {
            // Mid style-mutation race; the extent is removed best-effort. A
            // missing/half-built extent is harmless and self-heals on next draw.
          }
        } else {
          // LANE B #1 (durable exit-clear): the deferred-to-idle clear can be
          // STARVED on exit-to-root - the reset-view flyTo (800ms) plus any
          // wedged basemap tile keeps the map busy, so 'idle' never fires and the
          // dashed AOI rectangle LINGERS after leaving a Case (NATE: "the bbox is
          // STILL THERE when I exit the case"). removeLayer/removeSource are safe
          // whenever the layer/source exists regardless of style-load state, so
          // attempt the removal IMMEDIATELY in a try/catch as well (a starved
          // 'idle' can no longer strand the rectangle). The m.once('idle') below
          // stays as a belt-and-suspenders for the case where the layer was not
          // yet built when this command arrived.
          try {
            clearAnalysisExtent(m);
          } catch {
            /* best-effort immediate clear  -  the idle backup below retries */
          }
          m.once("idle", () => {
            if (!map.current) return;
            try {
              clearAnalysisExtent(map.current);
            } catch {
              /* best-effort  -  see above */
            }
          });
        }
      } else if (payload.command === "reset-view") {
        // ux-batch-1 (F-CASES-CLEAR-ALL): leaving a Case snaps the camera back
        // to the default CONUS view so the user clearly sees they are no longer
        // in a Case. Camera-only  -  the extent rectangle is cleared separately
        // by the clear-analysis-extent command App also emits on exit.
        // job-0321 (F43)  -  also drop the AOI bbox so the legend stops trying to
        // anchor to a box that is no longer on screen (belt + suspenders with
        // the clear-analysis-extent command).
        setAoiBbox(null);
        // FIX 2 (vector AOI clip)  -  clear the stashed AOI bbox too.
        setMapAoiBbox(m, null);
        const prefersReducedMotion =
          typeof window !== "undefined" &&
          typeof window.matchMedia === "function" &&
          window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        m.flyTo({
          center: CONUS_VIEW.center,
          zoom: CONUS_VIEW.zoom,
          duration: prefersReducedMotion ? 0 : 800,
        });
      } else if (payload.command === "set-layer-opacity") {
        const opacity = Math.max(0, Math.min(1, payload.opacity));
        applyLayerOpacity(
          m,
          payload.layer_id,
          opacity,
          vectorGeomKinds.current.get(payload.layer_id),
          layerStylePresets.current.get(payload.layer_id) ?? null,
          layerLegends.current.get(payload.layer_id) ?? null,
        );
        // job-0179  -  write-through so the edit survives a re-render / reconnect.
        layerCache.setOverride(layerCache.activeCaseId, payload.layer_id, {
          opacity,
        });
      } else if (payload.command === "set-layer-visibility") {
        applyLayerVisibility(m, payload.layer_id, payload.visible);
        // job-0179  -  write-through (subsumes the localStorage visibility map).
        layerCache.setOverride(layerCache.activeCaseId, payload.layer_id, {
          visible: payload.visible,
        });
      } else if (payload.command === "set-layer-order") {
        applyLayerOrder(m, payload.layer_ids);
        // job-0179  -  write-through: stamp each layer's resulting z-index (the
        // command is top-first, so the LAST element gets the lowest z) so a
        // re-render / reconnect re-asserts the user's drag-reorder.
        const ids = payload.layer_ids;
        ids.forEach((id, idx) => {
          layerCache.setOverride(layerCache.activeCaseId, id, {
            zIndex: ids.length - idx,
          });
        });
      } else {
        // eslint-disable-next-line no-console
        console.warn("[MapView] MapCommand not yet implemented:", payload.command);
      }
    });
  }, [subscribeMapCommand]);

  // JOB WEB-ANIM (#157.1)  -  register the sequence-animation frame-visibility
  // emitter. The module-level AnimationController holds the playback state + the
  // advance interval OUTSIDE the React tree (so closing the LayerPanel no longer
  // stops playback); whenever it changes the active frame it calls this emitter
  // to flip MapLibre layer visibility directly. Showing frame `visibleIndex`
  // means: that member visible, every sibling hidden. We ALSO write each member's
  // visibility through the layer cache (the seatbelt) so a reconcile/reconnect
  // re-asserts the single-visible-frame state. Mounted once with the map; the
  // emitter no-ops until the map style is ready. Map.tsx is always mounted, so
  // frames keep advancing on the map while the panel is closed.
  // NATE item 2 - the id of the frame the LAST raster swap raised, threaded into
  // swapFrameWithHold so it knows which single frame to HOLD underneath until the
  // new frame's tiles load (the hold = no black gap). Reset when the active group
  // changes (a different layerIds set), so a held id from another group is never
  // carried over.
  const prevFrameTargetRef = useRef<{ groupKey: string; target: string | null }>(
    { groupKey: "", target: null },
  );
  useEffect(() => {
    const controller = getAnimationController();
    // Shared FrameMapAdapter builder so the per-frame swap AND the release seam
    // (bug 1) drive MapLibre through the same guarded surface.
    const makeFrameAdapter = (m: MapLibreMap): FrameMapAdapter => ({
      hasLayer: (id) => {
        try {
          return Boolean(m.getLayer(id));
        } catch {
          return false;
        }
      },
      setVisibility: (id, visible) => {
        try {
          m.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
        } catch {
          /* mid-add race - the reconcile restores it */
        }
      },
      setOpacity: (id, opacity) => {
        try {
          m.setPaintProperty(id, "raster-opacity", opacity);
        } catch {
          /* mid-add race */
        }
      },
      isSourceLoaded: (id) => {
        try {
          return m.isSourceLoaded(id) === true;
        } catch {
          return false;
        }
      },
      onceSourceSettled: (cb) => {
        try {
          m.once("idle", cb);
        } catch {
          cb();
        }
      },
    });

    const unregister = controller.setEmitter((layerIds, visibleIndex) => {
      const m = map.current;
      if (!m) return;

      // NATE item 2 - kill the black-then-fill on raster frame swaps. A frame
      // group of RASTER overlays (the common case: HRRR hours / temporal COGs)
      // gets the PRELOAD + hold-until-loaded opacity swap (lib/frame_preload):
      // every frame is warmed (visible, opacity 0) so its tiles load, the target
      // is raised to opacity 1 immediately, and the OTHER frames are dimmed only
      // once the target's tiles are loaded - so the prior frame holds underneath
      // and there is never a black gap, even on first play. A group that contains
      // any non-raster (vector) member falls back to the plain visibility toggle
      // (vectors render from already-fetched GeoJSON, so they have no tile gap).
      const allRaster = layerIds.every(
        (id) => !vectorGeomKinds.current.has(id),
      );

      if (allRaster && layerIds.length > 1) {
        const adapter = makeFrameAdapter(m);
        // Thread the previous target so the swap holds the right frame. A new
        // group (different member set) starts with no held frame.
        const groupKey = layerIds.join("|");
        const prev =
          prevFrameTargetRef.current.groupKey === groupKey
            ? prevFrameTargetRef.current.target
            : null;
        const { target } = swapFrameWithHold(adapter, layerIds, visibleIndex, prev);
        prevFrameTargetRef.current = { groupKey, target };
        // Persist the single-visible-frame intent through the cache so a
        // reconcile / reconnect re-asserts it (the cache models visibility, not
        // opacity; the opacity dance is a transient anti-flash treatment).
        layerIds.forEach((id, i) => {
          layerCache.setOverride(layerCache.activeCaseId, id, {
            visible: i === visibleIndex,
          });
        });
        return;
      }

      // Non-raster (or single-frame) group: the original visibility toggle.
      layerIds.forEach((id, i) => {
        const visible = i === visibleIndex;
        try {
          applyLayerVisibility(m, id, visible);
        } catch {
          // Layer not on the style yet (mid-add)  -  the next session-state
          // reconcile + the cache override below restore the right state.
        }
        layerCache.setOverride(layerCache.activeCaseId, id, { visible });
      });
    });

    // BUG 1 (memory crash): the RELEASE seam. The controller fires it on
    // group-change / scrubber-stop / reset() with the group's full member list +
    // the frame to keep visible; we flip the out-of-window frames to
    // visibility:none so MapLibre frees their SourceCache + GPU textures (the
    // warm path left them all visible, which OOMed the tab on a 144/288-frame
    // sweep). The held-target ref is reset so a re-played group re-warms cleanly.
    const unregisterRelease = controller.setReleaseEmitter(
      (layerIds, keepVisibleIndex) => {
        const m = map.current;
        if (!m) return;
        releaseWarmedFrames(makeFrameAdapter(m), layerIds, keepVisibleIndex);
        if (prevFrameTargetRef.current.groupKey === layerIds.join("|")) {
          prevFrameTargetRef.current = { groupKey: "", target: null };
        }
      },
    );

    return () => {
      unregister();
      unregisterRelease();
    };
    // layerCache is a stable singleton; map.current is read live. Intentionally
    // empty deps so the emitter registers once for the map's lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // job-0321 (F43)  -  keep the legend anchored to the AOI box's bottom edge as
  // the camera moves. Re-project the bbox bottom-edge midpoint on every map
  // `move` / `zoom` / `render` (render fires throughout the fitBounds flight),
  // throttled to one update per animation frame so a 60fps pan doesn't thrash
  // setState. When aoiBbox is null (AOI-less Case / after Case-exit) the anchor
  // is cleared so the legend reverts to bottom-center. Listeners are cleaned up
  // on unmount / when aoiBbox changes.
  useEffect(() => {
    const m = map.current;
    if (!m) return undefined;

    if (!aoiBbox) {
      setLegendRect(null);
      setLegendAnchor(null);
      // FIX 4  -  no AOI bbox -> drop the projected width so the legend reverts to
      // its static 320 fallback.
      setLegendBarWidth(null);
      // MOBILE-ONLY HUD - no AOI bbox => not corner-placeable (the mobile legend
      // band-docks above the chat instead of corner-attaching).
      setAoiCornerPlaceable(false);
      return undefined;
    }

    let rafId: number | null = null;
    let disposed = false;
    const recompute = () => {
      rafId = null;
      if (disposed) return;
      const cur = map.current;
      if (!cur) return;
      // EDGE-RAIL snap  -  project the FULL AOI rectangle in ONE pass
      // (computeBboxScreenRect: min/max over all four projected bbox corners) and
      // thread that TRUE rectangle to the legend as `legendRect`. The legend snaps
      // its colorbar keys CCW directly against that rect's four edges, so the rail
      // follows the real AOI aspect ratio + on-screen skew (correct even when
      // Web-Mercator skews the box at high latitude)  -  the colorbar slides ALONG
      // the actual AOI edges, placeable around the whole perimeter, NOT off a
      // square-ish estimate. From the SAME rect we also derive two convenience
      // scalars that are NOT used for snapping:
      //   - legendAnchor = the bottom-edge midpoint ({(left+right)/2, bottom}),
      //     consumed only by resolvedAnchor for the legend's vertical placement;
      //   - legendBarWidth = the on-screen EAST-WEST extent (right-left), used
      //     only to SIZE the default colorbar width.
      // When the rect can't be projected / is off-screen we clear all three so the
      // legend reverts to its bottom-center fallback (it never vanishes).
      const rect = computeBboxScreenRect(cur, aoiBbox);
      // LANE C (flicker): m.on('render') fires on EVERY map repaint (far more
      // often under the v5 globe projection), and each frame called
      // setLegendRect with a fresh object + no equality guard - so MapView
      // re-rendered continuously while the map painted, churning the whole
      // legend / scrubber subtree. Only push new legend state when the four
      // edges (or the null<->present transition) actually change.
      const prevLocal = lastLocalRectRef.current;
      const sameLocal =
        (prevLocal == null && rect == null) ||
        (prevLocal != null &&
          rect != null &&
          prevLocal.left === rect.left &&
          prevLocal.top === rect.top &&
          prevLocal.right === rect.right &&
          prevLocal.bottom === rect.bottom);
      if (sameLocal) return;
      lastLocalRectRef.current = rect;
      if (rect) {
        setLegendRect(rect);
        setLegendAnchor({ left: (rect.left + rect.right) / 2, top: rect.bottom });
        setLegendBarWidth(rect.right - rect.left);
      } else {
        setLegendRect(null);
        setLegendAnchor(null);
        setLegendBarWidth(null);
      }
      // MOBILE-ONLY HUD (NATE 2026-06-27) - derive `aoiCornerPlaceable`: is the AOI
      // box usefully on-screen for a CORNER attach, or has the user zoomed/panned
      // so far that the corner snap is no longer useful and the legend should dock
      // above the chat instead? Computed from the SAME freshly-projected `rect`
      // (the per-corner projections already done by computeBboxScreenRect) plus the
      // live canvas size. Deliberately CONSERVATIVE: true in the normal case so the
      // existing corner-attach behavior is preserved; false only in the clearly-
      // too-zoomed cases. This scalar is consumed only by the legend's MOBILE path
      // (DESKTOP ignores it).
      //   - rect == null  -> false. computeBboxScreenRect returns null when the
      //     bbox CENTER falls off-canvas (zoomed/panned away), so there is no AOI
      //     on-screen to corner-attach to.
      //   - too-large (fills the viewport) -> false. When the projected box spans
      //     >= 92% of BOTH the canvas width AND height, every AOI edge runs off-
      //     screen, so there is no usable on-screen AOI corner left to hang off of.
      //     Requiring BOTH axes keeps a wide-but-short (or tall-but-narrow) AOI
      //     placeable - it still has on-screen edges to snap to.
      //   - too-small (a dot) -> false. When the SMALLER on-screen extent is <= 24px
      //     the AOI is a tiny dot (smaller than one legend key row), so a corner
      //     attach is meaningless and would have the legend cling to a speck.
      //   - otherwise -> true (the normal corner-attach case).
      // (See aoiRectCornerPlaceable above for the full threshold rationale.)
      let cw = 0;
      let ch = 0;
      try {
        const c = cur.getCanvas();
        if (c) {
          cw = c.clientWidth || c.width || 0;
          ch = c.clientHeight || c.height || 0;
        }
      } catch {
        /* no canvas in test env - treat as unknown (cw/ch stay 0) */
      }
      setAoiCornerPlaceable(aoiRectCornerPlaceable(rect, cw, ch));
      // ZOOM-OUT HIDE (NATE 2026-06-27, mobile-only) - derive the DISTINCT
      // "tiny dot on screen" signal from the SAME freshly-projected rect. True only
      // when a bbox IS projected AND its smaller on-screen extent is below
      // AOI_MIN_VISIBLE_EXTENT_PX (zoomed OUT far). Unlike aoiCornerPlaceable this
      // does NOT trip on a viewport-filling AOI (huge on both axes) - only the
      // speck case. Consumed by the legend (hides) + lifted to App for the scrubber.
      setAoiTooSmallToShow(aoiRectTooSmallToShow(rect));
      // Mirror the LIVE map zoom for the legend's mobile dock decision. This is a
      // continuously-tracked value (move/zoom/render listeners on this effect),
      // unlike the popup-only `currentZoom`. Guarded read so a torn-down map can't
      // throw.
      try {
        const z = cur.getZoom();
        if (typeof z === "number" && Number.isFinite(z)) setLegendMapZoom(z);
      } catch {
        /* map may be mid-teardown - keep the last zoom */
      }
    };
    const schedule = () => {
      if (rafId != null) return; // already queued this frame
      if (typeof requestAnimationFrame === "function") {
        rafId = requestAnimationFrame(recompute);
      } else {
        // SSR / test environments without rAF  -  compute synchronously.
        recompute();
      }
    };

    // Initial projection + on every camera change.
    schedule();
    m.on("move", schedule);
    m.on("zoom", schedule);
    m.on("render", schedule);

    return () => {
      disposed = true;
      if (rafId != null && typeof cancelAnimationFrame === "function") {
        cancelAnimationFrame(rafId);
      }
      try {
        m.off("move", schedule);
        m.off("zoom", schedule);
        m.off("render", schedule);
      } catch {
        /* map may already be torn down */
      }
    };
  }, [aoiBbox]);

  // Lift `legendRect` up to App so the SequenceScrubber (rendered inside
  // LayerPanel, which has no map handle) can pin bottom-center of the AOI box
  // and track pan/zoom exactly like the legend keys. The recompute effect above
  // re-derives `legendRect` on every move/zoom/render (rAF-throttled), so this
  // could fire very often with identical values  -  guard with a ref that holds
  // the last rect we reported and only invoke the callback when the four edges
  // actually change (or transition to/from null). That keeps App's state stable
  // and avoids render loops.
  const lastReportedRectRef = useRef<LegendScreenRect | null>(null);
  useEffect(() => {
    if (!onAoiScreenRectChange) return;
    const prev = lastReportedRectRef.current;
    const same =
      (prev == null && legendRect == null) ||
      (prev != null &&
        legendRect != null &&
        prev.left === legendRect.left &&
        prev.top === legendRect.top &&
        prev.right === legendRect.right &&
        prev.bottom === legendRect.bottom);
    if (same) return;
    lastReportedRectRef.current = legendRect;
    onAoiScreenRectChange(legendRect);
  }, [legendRect, onAoiScreenRectChange]);

  // ZOOM-OUT HIDE (NATE 2026-06-27, mobile-only) - lift the "AOI is a tiny dot"
  // signal up to App so the SequenceScrubber (rendered at the App root) can hide
  // when the bbox is a speck. Only fires when the boolean actually flips (React
  // already dedups identical setState, but the callback is App's, so guard it).
  useEffect(() => {
    if (!onAoiTooSmallToShowChange) return;
    onAoiTooSmallToShowChange(aoiTooSmallToShow);
  }, [aoiTooSmallToShow, onAoiTooSmallToShowChange]);

  // MOBILE-ONLY HUD (NATE 2026-06-27) - track the LIVE map zoom independently of
  // the AOI projection effect (which only runs while aoiBbox is non-null) and of
  // the popup-pin effect (which only runs while a feature popup is open). The
  // mobile legend uses this zoom (threaded as the `mapZoom` prop) alongside
  // aoiCornerPlaceable to decide corner-attach vs dock-above-chat. rAF-throttled
  // on move/zoom so a 60fps pan does not thrash setState; equality-guarded so an
  // unchanged zoom does not re-render. DESKTOP ignores `mapZoom` entirely, so this
  // adds nothing to the desktop render path.
  useEffect(() => {
    // MOBILE-ONLY: the live-zoom signal feeds only the mobile legend dock; gate
    // the listener attach on isMobile so the DESKTOP render path is byte-for-byte
    // unchanged (no extra move/zoom listeners, no setState).
    if (!isMobile) return undefined;
    const m = map.current;
    if (!m) return undefined;
    let rafId: number | null = null;
    let disposed = false;
    const recomputeZoom = () => {
      rafId = null;
      if (disposed) return;
      const cur = map.current;
      if (!cur) return;
      let z: number;
      try {
        z = cur.getZoom();
      } catch {
        return;
      }
      if (typeof z !== "number" || !Number.isFinite(z)) return;
      setLegendMapZoom((prev) => (prev === z ? prev : z));
    };
    const schedule = () => {
      if (rafId != null) return;
      if (typeof requestAnimationFrame === "function") {
        rafId = requestAnimationFrame(recomputeZoom);
      } else {
        recomputeZoom(); // SSR / test env without rAF -> synchronous.
      }
    };
    schedule(); // seed the initial zoom immediately.
    m.on("move", schedule);
    m.on("zoom", schedule);
    return () => {
      disposed = true;
      if (rafId != null && typeof cancelAnimationFrame === "function") {
        cancelAnimationFrame(rafId);
      }
      try {
        m.off("move", schedule);
        m.off("zoom", schedule);
      } catch {
        /* map may already be torn down */
      }
    };
    // map.current is a ref (stable); re-attach only when the mobile/desktop env
    // flips so desktop never carries the listeners.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isMobile]);

  // F74b feature-click/tap-to-inspect. The agent advertises "click polygons to
  // see name / designation / IUCN", so a click OR a tap on a rendered vector
  // feature must surface its attributes. Mechanism:
  //   - On `click` (fires for mouse AND for a tap on touch devices  -  MapLibre
  //     synthesizes a click from a tap that did not pan), run
  //     queryRenderedFeatures at the point restricted to the rendered vector
  //     paint layers (tracked in vectorGeomKinds  -  every layer_id we painted as
  //     a circle/line/fill). On a hit, open the popup at the point.
  //   - Desktop hover: set the canvas cursor to "pointer" over a hittable
  //     feature (mouseenter/mouseleave per layer) so users know it's clickable.
  //   - Dismiss: tapping the empty map (no hit) closes any open popup; the X
  //     button and Esc are handled inside FeaturePopup.
  // queryRenderedFeatures is the MapLibre hit-test  -  Invariant 1 holds: every
  // value shown comes from the feature's own properties, nothing is computed.
  useEffect(() => {
    const m = map.current;
    if (!m) return undefined;

    // Geometry-kind -> human label for the no-name title fallback.
    const geomLabel = (kind: VectorGeomKind | undefined): string => {
      switch (kind) {
        case "point":
          return "Point";
        case "line":
          return "Line";
        case "polygon":
          return "Polygon";
        default:
          return "Feature";
      }
    };

    // The set of MapLibre layer ids we hit-test: the main paint layer for every
    // tracked vector layer_id that currently exists on the map. (Cluster
    // sublayers / polygon outlines are intentionally excluded  -  the main paint
    // layer carries the real per-feature properties.)
    const queryableLayerIds = (): string[] => {
      const ids: string[] = [];
      for (const id of vectorGeomKinds.current.keys()) {
        try {
          if (m.getLayer(id)) ids.push(id);
        } catch {
          /* layer mid-removal  -  skip */
        }
      }
      return ids;
    };

    const readCanvasSize = (): { width: number; height: number } => {
      try {
        const c = m.getCanvas();
        if (c) return { width: c.clientWidth, height: c.clientHeight };
      } catch {
        /* fall through */
      }
      return { width: 0, height: 0 };
    };

    // Click-to-enrich (NATE 2026-06-27). For a FOOTPRINT layer that carries only
    // id-only props (slim inline GeoJSON), the popup paints immediately with a
    // "Loading details..." row; this kicks off the async tag fetch by
    // (osm_type, osm_id) and MERGES the returned tags into the open popup on
    // resolve. STRICTLY footprint-scoped: returns the slim popup data with
    // `enriching:false` for any non-footprint layer, so stations / WDPA / admin
    // popups are byte-for-byte unchanged.
    const buildPopupWithEnrich = (
      props: Record<string, unknown> | null,
      point: { x: number; y: number },
      opts: { layerName?: string; geomKindLabel?: string },
      stylePreset: string | null,
      extra: Partial<FeaturePopupData>,
    ): FeaturePopupData => {
      const base = buildFeaturePopupData(props, point, opts);
      const routable: DeckRoutableLayer = {
        layer_id: opts.layerName ?? "",
        name: opts.layerName,
        style_preset: stylePreset,
      };
      const p = props ?? {};
      // Case-insensitive read of the slim join-keys the agent emits.
      const lower = new Map<string, unknown>();
      for (const k of Object.keys(p)) lower.set(k.toLowerCase(), p[k]);
      const osmType = lower.get("osm_type");
      const osmId = lower.get("osm_id");
      const fidVal = lower.get("fid");
      const isFootprint = isFootprintLayer(routable);
      const canEnrich =
        isFootprint &&
        osmType != null &&
        String(osmType) !== "" &&
        osmId != null &&
        String(osmId) !== "";
      if (!canEnrich) {
        return { ...base, ...extra };
      }
      const enrichFid =
        typeof fidVal === "string" && fidVal !== ""
          ? fidVal
          : `${String(osmType).slice(0, 1)}${String(osmId)}`;
      // Fire-and-merge: render the slim card now, fetch + merge on resolve.
      void fetchBuildingDetail(String(osmType), String(osmId)).then((tags) => {
        // Merge into the CURRENT popup only if it is still the same footprint
        // (same enrichFid) and still awaiting enrichment - drop a stale resolve.
        setFeaturePopup((cur) => {
          if (!cur || cur.enrichFid !== enrichFid || !cur.enriching) return cur;
          if (!tags) {
            // FOOTPRINT ENRICH TERMINAL STATE (NATE 2026-06-28): the detail
            // fetch failed/timed out (building_enrich returns null on any
            // failure incl. the 10s COLD_FETCH_TIMEOUT_MS, e.g. the agent box is
            // asleep). Set a TERMINAL enrichFailed flag (not just enriching:false)
            // so FeaturePopup shows an HONEST "details unavailable" line instead
            // of silently collapsing to a bare card (reads as "loaded then
            // stopped"). The stale-resolve guard above still drops a resolve for
            // a since-dismissed/replaced popup, so this never paints onto the
            // wrong feature.
            return { ...cur, enriching: false, enrichFailed: true };
          }
          const merged = mergeTagsIntoAttributes(cur, tags);
          return { ...merged, enriching: false };
        });
      });
      return { ...base, ...extra, enriching: true, enrichFid };
    };

    const onMapClick = (e: maplibregl.MapMouseEvent): void => {
      const layers = queryableLayerIds();
      if (layers.length === 0) {
        setFeaturePopup(null);
        // FIX 1  -  no vector layers -> nothing to highlight; clear any stale one.
        try {
          if (m.getSource(FEATURE_HIGHLIGHT_SOURCE_ID)) clearFeatureHighlight(m);
        } catch {
          /* best effort */
        }
        return;
      }
      let features: maplibregl.MapGeoJSONFeature[] = [];
      try {
        features = m.queryRenderedFeatures(e.point, { layers });
      } catch {
        features = [];
      }
      if (!features || features.length === 0) {
        // Tap on empty map (or basemap/raster) dismisses any open popup AND
        // clears the highlight (FIX 1  -  a no-hit tap replaces/clears it).
        setFeaturePopup(null);
        try {
          if (m.getSource(FEATURE_HIGHLIGHT_SOURCE_ID)) clearFeatureHighlight(m);
        } catch {
          /* best effort */
        }
        return;
      }
      const hit = features[0]!;
      const sourceId =
        typeof (hit as { layer?: { source?: unknown } }).layer?.source === "string"
          ? ((hit as unknown as { layer: { source: string } }).layer.source as string)
          : undefined;
      const geomKind = sourceId ? vectorGeomKinds.current.get(sourceId) : undefined;
      // FIX 1  -  highlight the ENTIRE tapped feature geometry. Generic across
      // polygon/line/point: setFeatureHighlight feeds the cloned geometry into a
      // single highlight source whose fill/line/circle layers paint whichever
      // kind matches. Map-space, so it pans + scales with zoom for free. Replaces
      // any prior highlight (a new tap = a new highlight).
      try {
        setFeatureHighlight(m, (hit.geometry ?? null) as Geometry | null);
      } catch {
        /* highlight is best-effort; the popup still opens */
      }
      // FIX 2  -  capture the feature's geographic anchor so the popup stays glued
      // to its MAP location across pans/zooms. FIX 3  -  capture the zoom at tap as
      // the scale reference. e.lngLat is MapLibre's geographic coordinate of the
      // tap point (Invariant 1: received from MapLibre, not computed).
      const lngLat =
        e.lngLat && typeof e.lngLat.lng === "number" && typeof e.lngLat.lat === "number"
          ? { lng: e.lngLat.lng, lat: e.lngLat.lat }
          : undefined;
      let refZoom: number | undefined;
      try {
        refZoom = m.getZoom();
      } catch {
        refZoom = undefined;
      }
      // L3-web-station-csv: when the tapped layer is a STATION layer (USGS
      // gauges / ASOS-METAR / RAWS / NOAA CO-OPS, by style_preset prefix),
      // capture the raw tapped properties AND - when still available - the
      // whole-layer feature set so the popup can offer a Download-CSV dump. All
      // of this is already client-side; Invariant 1 holds because we only carry
      // received feature properties, never computed geography.
      const stationPreset = sourceId
        ? layerStylePresets.current.get(sourceId) ?? null
        : null;
      let rawProperties: Record<string, unknown> | undefined;
      let layerFeatures: Record<string, unknown>[] | undefined;
      let stationLayerName: string | undefined;
      if (isStationPreset(stationPreset)) {
        rawProperties =
          (hit.properties as Record<string, unknown> | null | undefined) ?? {};
        stationLayerName = sourceId;
        try {
          const src = sourceId
            ? (m.getSource(sourceId) as maplibregl.GeoJSONSource | undefined)
            : undefined;
          // GeoJSONSource keeps the data we set on it; read it back for the
          // all-stations dump. Only the FeatureCollection shape carries the
          // per-station rows we want.
          const sd = src
            ? (src as unknown as { _data?: unknown })._data
            : undefined;
          const fc = sd as { features?: Array<{ properties?: unknown }> } | undefined;
          if (fc && Array.isArray(fc.features)) {
            layerFeatures = fc.features.map(
              (f) => (f?.properties as Record<string, unknown> | null) ?? {},
            );
          }
        } catch {
          /* source gone / not a GeoJSON source - fall back to the single hit */
        }
      }
      // buildPopupWithEnrich returns the slim popup immediately and, ONLY for a
      // footprint layer carrying id-only props, kicks off the async tag enrich +
      // merge. For stations / WDPA / admin it is a pass-through (canEnrich=false)
      // so those popups stay byte-for-byte unchanged.
      const data: FeaturePopupData = buildPopupWithEnrich(
        (hit.properties ?? null) as Record<string, unknown> | null,
        { x: e.point.x, y: e.point.y },
        { layerName: sourceId, geomKindLabel: geomLabel(geomKind) },
        stationPreset,
        { lngLat, refZoom, rawProperties, layerFeatures, stationLayerName },
      );
      setMapCanvasSize(readCanvasSize());
      if (typeof refZoom === "number") setCurrentZoom(refZoom);
      setFeaturePopup(data);
    };

    // deck.gl SPIKE (#169) PICKING BRIDGE: queryRenderedFeatures is BLIND to deck
    // layers, so a footprint tap must come back through deck's own onClick. We
    // adapt deck's PickingInfo into the SAME FeaturePopup payload the MapLibre
    // click path builds (buildFeaturePopupData), so clicking a deck footprint
    // opens the popup identically. info.object is the picked GeoJSON Feature;
    // info.layer.id is the layer_id we routed; info.coordinate is [lng,lat].
    const onDeckClick = (info: unknown): void => {
      const i = info as {
        object?: { properties?: Record<string, unknown> | null; geometry?: unknown } | null;
        layer?: { id?: string } | null;
        x?: number;
        y?: number;
        coordinate?: number[] | null;
      };
      if (!i || !i.object) return; // a miss (clicked empty deck area) - ignore.
      const layerId = i.layer?.id;
      const px = typeof i.x === "number" ? i.x : 0;
      const py = typeof i.y === "number" ? i.y : 0;
      // Highlight the tapped footprint geometry via the same shared source.
      try {
        setFeatureHighlight(m, (i.object.geometry ?? null) as Geometry | null);
      } catch {
        /* highlight best-effort */
      }
      const lngLat =
        Array.isArray(i.coordinate) &&
        typeof i.coordinate[0] === "number" &&
        typeof i.coordinate[1] === "number"
          ? { lng: i.coordinate[0]!, lat: i.coordinate[1]! }
          : undefined;
      let refZoom: number | undefined;
      try {
        refZoom = m.getZoom();
      } catch {
        refZoom = undefined;
      }
      // The deck-routed layer carries the style_preset + name we need to detect
      // a footprint (the layer_id alone may not contain "building"). Footprints
      // route to deck, so this is the PRIMARY click-to-enrich path.
      const routed = layerId
        ? deckRoutedLayers.current.get(layerId)
        : undefined;
      const deckPreset = routed?.style_preset ?? null;
      const deckLayerName = routed?.name ?? layerId;
      const data: FeaturePopupData = buildPopupWithEnrich(
        (i.object.properties ?? null) as Record<string, unknown> | null,
        { x: px, y: py },
        { layerName: deckLayerName, geomKindLabel: geomLabel("polygon") },
        deckPreset,
        { lngLat, refZoom },
      );
      setMapCanvasSize(readCanvasSize());
      if (typeof refZoom === "number") setCurrentZoom(refZoom);
      setFeaturePopup(data);
    };
    // Publish the bridge so the reconcile's deck rebuild wires it into each
    // pickable deck layer's onClick.
    onDeckClickRef.current = onDeckClick;

    // Desktop cursor affordance  -  pointer over a hittable feature. We attach a
    // single mousemove handler (cheap) instead of per-layer enter/leave so it
    // keeps working as vector layers come and go without re-binding.
    const onMouseMove = (e: maplibregl.MapMouseEvent): void => {
      const layers = queryableLayerIds();
      if (layers.length === 0) {
        m.getCanvas().style.cursor = "";
        return;
      }
      let features: maplibregl.MapGeoJSONFeature[] = [];
      try {
        features = m.queryRenderedFeatures(e.point, { layers });
      } catch {
        features = [];
      }
      m.getCanvas().style.cursor = features && features.length > 0 ? "pointer" : "";
    };

    m.on("click", onMapClick);
    m.on("mousemove", onMouseMove);

    return () => {
      try {
        m.off("click", onMapClick);
        m.off("mousemove", onMouseMove);
      } catch {
        /* map may already be torn down */
      }
      // deck.gl SPIKE (#169): drop the picking bridge so a stale handler can't
      // fire after the effect (and its `m`) is torn down.
      onDeckClickRef.current = null;
    };
  }, []);

  // FIX 2 + FIX 3 (NATE 2026-06-17)  -  keep the popup PINNED TO THE MAP and
  // TRACK ZOOM for the scale transform. While a popup with a geographic anchor
  // (`lngLat`) is open, re-project that lng/lat to a screen point on every map
  // `move` / `zoom` so the card stays glued to the feature's MAP location (pans
  // with the map, same spot on the map), and mirror the live map zoom into
  // `currentZoom` so the card scales like a map-drawn label. rAF-throttled so a
  // 60fps pan doesn't thrash setState. Re-projection updates only `point` so the
  // popup content / lngLat / refZoom are preserved. Re-armed whenever the popup
  // identity (its lngLat) changes; torn down when the popup closes.
  const popupLng = featurePopup?.lngLat?.lng;
  const popupLat = featurePopup?.lngLat?.lat;
  useEffect(() => {
    const m = map.current;
    if (!m) return undefined;
    if (typeof popupLng !== "number" || typeof popupLat !== "number") {
      return undefined; // no geographic anchor (older fixtures) -> stays screen-anchored.
    }

    let rafId: number | null = null;
    let disposed = false;
    const recompute = () => {
      rafId = null;
      if (disposed) return;
      const cur = map.current;
      if (!cur) return;
      let pt: { x: number; y: number };
      try {
        pt = cur.project([popupLng, popupLat]);
      } catch {
        return;
      }
      let z: number;
      try {
        z = cur.getZoom();
      } catch {
        z = currentZoom ?? 0;
      }
      setCurrentZoom(z);
      // Update only `point`  -  keep the rest of the popup payload intact so the
      // card re-renders glued to the feature's projected map location.
      setFeaturePopup((prev) =>
        prev ? { ...prev, point: { x: pt.x, y: pt.y } } : prev,
      );
    };
    const schedule = () => {
      if (rafId != null) return;
      if (typeof requestAnimationFrame === "function") {
        rafId = requestAnimationFrame(recompute);
      } else {
        recompute(); // SSR / test env without rAF -> synchronous.
      }
    };

    // Project once now (so the card lands on the anchor immediately), then on
    // every camera change.
    schedule();
    m.on("move", schedule);
    m.on("zoom", schedule);

    return () => {
      disposed = true;
      if (rafId != null && typeof cancelAnimationFrame === "function") {
        cancelAnimationFrame(rafId);
      }
      try {
        m.off("move", schedule);
        m.off("zoom", schedule);
      } catch {
        /* map may already be torn down */
      }
    };
    // currentZoom is intentionally omitted  -  it's a fallback read, not a trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [popupLng, popupLat]);

  // --- Region-disambiguation choropleth sync (state-bbox-fallback) -------- //
  //
  // Subscribe to the region-choice bus so the candidate county choropleth stays
  // in lockstep with the in-chat RegionPickerCard list:
  //   - A new request draws the choropleth + zooms to the whole-state bbox so
  //     the candidate counties are in view; a cleared request (the user
  //     answered) tears it down. Replace-on-new-request (Appendix A.7).
  //   - hovered/selected ids drive each polygon's feature-state highlight so a
  //     CARD-ROW hover/click highlights the matching polygon.
  //   - A polygon TAP (or hover) feeds the bus (pickRegion / setHovered) so the
  //     map drives the card the same way the card drives the map  -  one reply
  //     path (Chat owns the WebSocket; the bus relays the map tap to it).
  //
  // All MapLibre source/layer work is gated on style-readiness + existence
  // guards (mirrors the analysis-extent + vector-layer paths). Invariant 4: the
  // client only registers sources/layers; Invariant 1: geometry is verbatim
  // from the received candidate bboxes.
  useEffect(() => {
    const highlightIds = { current: new Set<string>() };
    let currentRequestId: string | null = null;

    // Apply a bus snapshot to the map. Idempotent; safe to call before the
    // style is ready (it re-arms on the next idle via the map "idle" handler
    // registered below).
    const apply = (st: RegionChoiceBusState): void => {
      const m = map.current;
      if (!m) return;
      if (!mapStyleReady(m)) return;
      const req = st.request;
      if (!req) {
        // Cleared  -  tear down the choropleth.
        if (currentRequestId !== null) {
          clearRegionChoropleth(m);
          highlightIds.current = new Set();
          currentRequestId = null;
        }
        return;
      }
      // New request (or first paint)  -  draw + frame the whole-state bbox so the
      // candidate counties are visible.
      if (currentRequestId !== req.request_id) {
        drawRegionChoropleth(m, req.candidates);
        currentRequestId = req.request_id;
        highlightIds.current = new Set();
        try {
          const [minLon, minLat, maxLon, maxLat] = req.state_bbox;
          m.fitBounds(
            [
              [minLon, minLat],
              [maxLon, maxLat],
            ],
            {
              // LANE B #3 - panel-aware padding (see the zoom-to fit above),
              // incl. the mobile bottom-sheet bottom-pad so the framed candidate
              // counties land ABOVE the chat sheet, not behind it.
              padding: panelAwareFitPadding(
                m,
                48,
                leftPanelWidthPx,
                chatWidthPx ?? 0,
                chatCollapsed ?? false,
                mobile ?? false,
                legendSheetTopPx,
              ),
              duration: 600,
              maxZoom: 8,
            },
          );
        } catch {
          /* fitBounds can throw on a degenerate bbox  -  leave the camera */
        }
      } else {
        // Same request  -  keep the data fresh (candidates are stable, but a
        // re-emit is harmless) without re-framing the camera.
        drawRegionChoropleth(m, req.candidates);
      }
      highlightIds.current = applyRegionChoiceHighlight(
        m,
        st.hoveredRegionId,
        st.selectedRegionId,
        highlightIds.current,
      );
    };

    // The bus fires immediately on subscribe with the current state, so a Map
    // mounting AFTER the request arrived paints the choropleth right away.
    const unsub = regionChoiceBus.subscribe((st) => {
      const m = map.current;
      // Nothing to draw / tear down (no active request and none currently
      // painted) -> do not arm an idle deferral. This keeps the common no-pick
      // case (the vast majority of sessions) from touching the map at all.
      if (!st.request && currentRequestId === null) return;
      if (m && !mapStyleReady(m)) {
        // Defer until the style is ready; re-read the live bus state then so we
        // don't paint a stale snapshot.
        m.once("idle", () => apply(regionChoiceBus.getState()));
        return;
      }
      apply(st);
    });

    // Map TAP on a candidate polygon -> relay a pick to the bus (Chat sends the
    // reply). A hover over a polygon highlights it via the bus too.
    const m0 = map.current;
    const onChoroplethClick = (e: maplibregl.MapMouseEvent): void => {
      const m = map.current;
      if (!m || !m.getLayer(REGION_CHOICE_FILL_LAYER_ID)) return;
      let hits: maplibregl.MapGeoJSONFeature[] = [];
      try {
        hits = m.queryRenderedFeatures(e.point, {
          layers: [REGION_CHOICE_FILL_LAYER_ID],
        });
      } catch {
        hits = [];
      }
      const id = hits[0]?.properties?.region_id;
      if (typeof id === "string") {
        // Stop the generic feature-inspect click handler from also firing a
        // popup for this tap (it's a pick, not an inspect).
        if (typeof (e as { preventDefault?: () => void }).preventDefault === "function") {
          (e as { preventDefault?: () => void }).preventDefault!();
        }
        regionChoiceBus.pickRegion(id);
      }
    };
    const onChoroplethMove = (e: maplibregl.MapMouseEvent): void => {
      const m = map.current;
      if (!m || !m.getLayer(REGION_CHOICE_FILL_LAYER_ID)) return;
      let hits: maplibregl.MapGeoJSONFeature[] = [];
      try {
        hits = m.queryRenderedFeatures(e.point, {
          layers: [REGION_CHOICE_FILL_LAYER_ID],
        });
      } catch {
        hits = [];
      }
      const id = hits[0]?.properties?.region_id;
      regionChoiceBus.setHovered(typeof id === "string" ? id : null);
      try {
        m.getCanvas().style.cursor = typeof id === "string" ? "pointer" : "";
      } catch {
        /* canvas gone */
      }
    };
    if (m0) {
      m0.on("click", onChoroplethClick);
      m0.on("mousemove", onChoroplethMove);
    }

    return () => {
      unsub();
      const m = map.current;
      if (m) {
        try {
          m.off("click", onChoroplethClick);
          m.off("mousemove", onChoroplethMove);
          clearRegionChoropleth(m);
        } catch {
          /* map torn down */
        }
      }
    };
  }, []);

  // FR-WC-13 / FR-WC-16  -  subscribe to the spatial-input bus and mirror the
  // active request into component state so the SpatialDrawSurface overlay mounts
  // (point/bbox pick mode or the terra-draw surface). The bus fires immediately
  // on subscribe with the current state, so a Map mounting AFTER the request
  // arrived opens the surface right away. The surface's Submit/Cancel ride back
  // through the bus (bus.submit / bus.cancel) to Chat  -  Map never sends WS.
  useEffect(() => {
    const unsub = spatialInputBus.subscribe((st: SpatialInputBusState) => {
      setSpatialRequest(st.request);
    });
    return unsub;
  }, []);

  // deck.gl SPIKE (#169): SUPPRESS deck picking while a draw / pick / region-choice
  // request is in flight, so deck does not swallow the canvas pointer events
  // terra-draw / the SpatialDrawSurface / the region-choropleth pick need. A draw
  // request is `spatialRequest != null` (also the #170 AOI-capture card), and a
  // region-choice is the bus's `request != null`. When either is active we flip
  // the ref + rebuild the overlay non-pickable; when both clear we restore picking.
  useEffect(() => {
    let regionActive = regionChoiceBus.getState().request != null;
    const apply = () => {
      const suppressed = spatialRequest != null || aoiCaptureActive === true || regionActive;
      if (deckPickSuppressed.current === suppressed) return; // no change.
      deckPickSuppressed.current = suppressed;
      // Re-run the (idempotent) reconcile so the overlay rebuilds with the deck
      // layers pickable / non-pickable to match.
      try {
        rebuildDeckLayersRef.current?.();
      } catch {
        /* best-effort - the next session-state push reconciles anyway */
      }
    };
    apply();
    const unsub = regionChoiceBus.subscribe((st: RegionChoiceBusState) => {
      regionActive = st.request != null;
      apply();
    });
    return unsub;
  }, [spatialRequest, aoiCaptureActive]);

  // job-0321 (F43)  -  resolve the legend placement.
  //   - aoiBbox + on-screen anchor -> hang off the box's bottom edge, nudged
  //     down a small gap so it clears the dashed outline. On mobile the box can
  //     sit behind the collapsed bottom sheet, so we add the same ~116px sheet
  //     clearance App used for the old bottom-center mobile legend  -  but only as
  //     a floor: if the anchored position is already higher than that, keep it.
  //   - no anchor (AOI-less / off-screen / no map yet) -> null, and LayerLegend
  //     falls back to its own bottom-center placement.
  const LEGEND_GAP_PX = 10; // small gap below the bbox bottom edge.
  const MOBILE_SHEET_CLEARANCE_PX = 116; // matches the prior App mobile offset.
  let resolvedAnchor: LegendAnchor | null = null;
  if (legendAnchor) {
    let top = legendAnchor.top + LEGEND_GAP_PX;
    if (isMobile) {
      // Keep the legend above the collapsed bottom sheet. We can only clamp in
      // screen space relative to the container; the container fills the map, so
      // its height is the canvas height. Use the projected-canvas height if we
      // can read it, else leave the anchored top as-is.
      const cur = map.current;
      let canvasH: number | null = null;
      try {
        const c = cur?.getCanvas();
        if (c) canvasH = c.clientHeight;
      } catch {
        canvasH = null;
      }
      if (canvasH != null) {
        const maxTop = canvasH - MOBILE_SHEET_CLEARANCE_PX;
        if (top > maxTop) top = Math.max(0, maxTop);
      }
    }
    resolvedAnchor = { left: legendAnchor.left, top };
  }

  // MOBILE-ONLY HUD (NATE 2026-06-27) - the two NEW legend signals, bundled into a
  // typed object and SPREAD onto <LayerLegend> below. This phase OWNS Map.tsx but
  // NOT LayerLegend.tsx (the Legend phase adds the consuming `mapZoom` /
  // `aoiCornerPlaceable` props to LayerLegendProps and implements the dock
  // decision). Spreading a typed variable (rather than passing the props as a JSX
  // object literal) bypasses TypeScript's excess-property check, so Map.tsx
  // typechecks GREEN now and stays correct once the Legend phase lands the props.
  // Contract handed forward:
  //   - mapZoom: number | null - the LIVE map zoom (continuously tracked on
  //     move/zoom, null until the first read). Supplementary zoom signal.
  //   - aoiCornerPlaceable: boolean - true when the AOI box is usefully on-screen
  //     for a corner attach (the normal case, preserves existing corner-snap);
  //     false only in the clearly-too-zoomed cases (AOI off-screen / fills the
  //     viewport / is a tiny dot) so the mobile legend docks above the chat.
  const legendHudExtras: {
    mapZoom: number | null;
    aoiCornerPlaceable: boolean;
    aoiTooSmallToShow: boolean;
  } = {
    mapZoom: legendMapZoom,
    aoiCornerPlaceable,
    // ZOOM-OUT HIDE (NATE 2026-06-27, mobile-only) - true when the AOI bbox is a
    // tiny dot on screen; the legend's mobile path early-returns null. DESKTOP
    // ignores it (the desktop legend never reads the HUD extras).
    aoiTooSmallToShow,
  };

  return (
    <div
      ref={container}
      data-testid="grace2-map"
      style={{ position: "absolute", inset: 0 }}
    >
      {/* job-0321 (F43)  -  the legend now lives INSIDE the map container so it
          can anchor to the AOI box. `anchor` non-null = hang off the box's
          bottom edge; null = LayerLegend's own bottom-center fallback. */}
      {/* EDGE-RAIL snap (aoiRect)  -  the TRUE projected AOI rectangle is the snap
          source of truth: the colorbar keys rail CCW along its real edges. anchor
          (resolvedAnchor) drives only the vertical placement nudge; barWidth only
          sizes the default colorbar width. */}
      <LayerLegend
        layers={legendLayers}
        aoiRect={legendRect}
        anchor={resolvedAnchor}
        barWidth={legendBarWidth}
        /* Item b  -  controlled hide state (App owns it on mobile so the toggle
           lives in the Layers section, off the chat composer). */
        hidden={legendHidden}
        onHiddenChange={onLegendHiddenChange}
        suppressShowPill={suppressLegendShowPill}
        /* MOBILE SHEET-TOP DOCK (NATE 2026-06-24) - dock the mobile colorbar keys
           + collapsed pill to the chat sheet's TOP edge (a clean band) instead
           of floating over the map. Null on desktop (the desktop dock ignores
           it). */
        sheetTopPx={legendSheetTopPx}
        /* CHART-OVERLAY HIDE-LEGEND (NATE 2026-06-28, mobile) - when Chat's
           full-viewport ChartGallery is open, the legend renders nothing on
           mobile so the body-portaled colorbar never paints above/around the
           chart. Default false; desktop ignores it (it sits below the gallery's
           z=10000 overlay anyway). */
        chartOpen={legendChartOpen}
        /* MOBILE-ONLY HUD (NATE 2026-06-27) - mapZoom (live map zoom) +
           aoiCornerPlaceable (is the AOI usefully on-screen for a corner attach),
           spread from legendHudExtras above so the legend's MOBILE path can decide
           corner-attach (snap to the AOI box) vs dock-above-chat (a clean
           horizontal row above the scrubber). DESKTOP ignores both. Spread (not a
           JSX literal) so it typechecks before the Legend phase adds the props. */
        {...legendHudExtras}
        /* LANE D (desktop dock) - center the static bottom-center legend strip
           in the VISIBLE gutter between the left rail + right chat panel. */
        desktopLeftInsetPx={leftPanelWidthPx}
        desktopRightInsetPx={chatCollapsed ? 0 : chatWidthPx ?? 0}
      />

      {/* F74b / FIX 2 / FIX 3  -  feature-click/tap-to-inspect popup. Shown when a
          click/tap hits a rendered vector feature; PINNED TO THE FEATURE'S MAP
          LOCATION (re-projected on pan/zoom so it pans with the map) and SCALED
          with the map zoom (shrinks zoomed out, grows zoomed in; clamped). It
          PERSISTS until the user taps elsewhere (a no-hit click dismisses it),
          taps another feature (it moves there), or hits the X / Esc  -  and the
          generic feature HIGHLIGHT (FIX 1) is cleared on any of those. */}
      {featurePopup ? (
        <FeaturePopup
          data={featurePopup}
          canvasSize={mapCanvasSize}
          isMobile={isMobile}
          currentZoom={currentZoom ?? undefined}
          onClose={() => {
            setFeaturePopup(null);
            // FIX 1  -  clear the highlight when the popup is dismissed (X / Esc).
            const m = map.current;
            if (m) {
              try {
                clearFeatureHighlight(m);
              } catch {
                /* best effort */
              }
            }
          }}
        />
      ) : null}

      {/* FR-WC-13 / FR-WC-16  -  the on-map spatial-input surface. Mounts while a
          `spatial-input-request` is active (mirrored from the spatialInputBus).
          For vector_draw it hosts the terra-draw toolbar + tagging popover; for
          point/bbox it enters pick mode. Submit/Cancel funnel back through the
          bus to Chat (the WS reply owner). Gated on a live map instance so the
          terra-draw adapter has a real MapLibre target. */}
      {spatialRequest && map.current ? (
        <SpatialDrawSurface
          key={spatialRequest.request_id}
          map={map.current}
          request={spatialRequest}
          onSubmit={(result) => spatialInputBus.submit(result)}
          onCancel={(requestId) => spatialInputBus.cancel(requestId)}
        />
      ) : null}

      {/* #170 AOI-first manual case-creation. Mounts when App's local
          aoiCaptureActive signal is set (NOT the spatial-input bus) so the user
          can draw / enter the AOI bbox BEFORE the first prompt. REQUEST-FREE:
          the card never touches the WS / bus; confirm rides App's createCase
          (the durable sendOrQueue path). Skip / Cancel close the overlay. */}
      {aoiCaptureActive && onAoiCaptureConfirm && onAoiCaptureSkip && onAoiCaptureCancel ? (
        <AoiPickerCard
          map={map.current}
          onConfirm={onAoiCaptureConfirm}
          onSkip={onAoiCaptureSkip}
          onCancel={onAoiCaptureCancel}
        />
      ) : null}

      {/* NATE item 4 - the ALWAYS-ON "Draw AOI" control. Persistent map control
          that arms the bbox rectangle-draw on demand; the drawn box stages as the
          next-prompt analysis extent (aoiStageBus), non-destructive, with an easy
          clear. Suppressed while another draw surface owns the gesture (an
          agent-requested spatial-input pick, or the #170 AOI-capture card) so two
          drag handlers never fight. NO-CLOBBER: only this control arms the draw. */}
      {!spatialRequest && !aoiCaptureActive ? (
        <DrawAoiControl
          map={map.current}
          chatWidthPx={chatWidthPx}
          chatCollapsed={chatCollapsed}
          mobile={mobile}
          caseHasAoi={caseHasAoi}
          onConfirmAoi={onAoiStageConfirm}
        />
      ) : null}
    </div>
  );
}
