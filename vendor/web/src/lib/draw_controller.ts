// GRACE-2 web — DrawController (FR-WC-16 urban vector-draw).
//
// A thin, framework-agnostic wrapper around terra-draw (core) +
// terra-draw-maplibre-gl-adapter that owns the urban vector-draw surface for the
// SWMM urban-flood engine. It exposes a small imperative API the Map.tsx React
// host drives from the spatial-input bus, and a pure GeoJSON readback that
// produces the role-tagged FeatureCollection `spatial-input-response` carries.
//
// WHY a wrapper (not the raw @watergis toolbar Control): the prebuilt toolbar
// hides the low-level store API we need for per-segment barrier tagging
// (updateFeatureProperties), segment snip (removeFeatures), and area-threshold
// discard (getSnapshot filter + removeFeatures). We use the raw terra-draw core
// + the official MapLibre adapter directly (DRAW LIB brief).
//
// DRAW MODES exposed:
//   - "rectangle"  TerraDrawRectangleMode  -> AOI rectangles (role "aoi")
//   - "polygon"    TerraDrawPolygonMode    -> simple polygons (role "aoi"; e.g. lakes)
//   - "linestring" TerraDrawLineStringMode -> barrier segments (role "barrier")
//   - "select"     TerraDrawSelectMode     -> vertex move/insert/delete + pick a
//                                             segment to tag / snip
//
// TAGGING (DRAW LIB Pattern A — one LineString feature per barrier segment): a
// barrier segment is its own LineString. The user draws it, selects it, and tags
// barrier_type wall|flap_gate via `tagBarrier(id, ...)`. The per-feature style
// function colors each segment by its tag: wall = RED (#e53935), flap_gate =
// GREEN (#43a047) — matching the engine convention (red wall = omitted conduit,
// green flap = one-way orifice). Untagged barrier lines render NEUTRAL until
// tagged. Pattern A maps 1:1 to the engine's tagged-LineString FeatureCollection
// so `getFeatureCollection()` round-trips straight into run_swmm_urban_flood's
// `barriers` kwarg with zero translation.
//
// SNIP (DRAW LIB edit/snip): because every barrier segment is its own feature,
// "snip a stray segment" is just `snipFeature(id)` -> removeFeatures([id]).
//
// AREA-THRESHOLD DISCARD: `discardSmallPolygons(minAreaM2)` computes each
// Polygon's planar-in-metres area (local equirectangular approximation — exact
// enough for the small AOIs this targets, and dependency-free; @turf/area is NOT
// a project dependency) and removes the ones below the threshold. Returns the
// dropped ids so the host can surface a count.

import {
  TerraDraw,
  TerraDrawLineStringMode,
  TerraDrawPolygonMode,
  TerraDrawRectangleMode,
  TerraDrawSelectMode,
  type GeoJSONStoreFeatures,
  type HexColor,
} from "terra-draw";
import { TerraDrawMapLibreGLAdapter } from "terra-draw-maplibre-gl-adapter";
import type { Map as MapLibreMap } from "maplibre-gl";
import type {
  BarrierType,
  FlapDirection,
  SpatialDrawFeature,
  SpatialDrawFeatureCollection,
  SpatialDrawRole,
} from "../contracts";

// --- Visual convention (engine semantics) -------------------------------- //

/** RED wall = omitted conduit (hard dam). */
export const WALL_COLOR: HexColor = "#e53935";
/** GREEN flap gate = one-way orifice. */
export const FLAP_GATE_COLOR: HexColor = "#43a047";
/** Neutral amber for an untagged barrier line (drawn, not yet typed). */
export const UNTAGGED_BARRIER_COLOR: HexColor = "#fbbf24";
/** AOI rectangle / polygon outline + fill accent (blue). */
export const AOI_COLOR: HexColor = "#3b82f6";

export type DrawMode = "rectangle" | "polygon" | "linestring" | "select";

/** terra-draw store feature id. */
export type DrawFeatureId = string | number;

/** Injectable terra-draw factory so unit tests can swap a stub for the real lib
 * (the real TerraDraw needs a live MapLibre adapter, which happy-dom lacks). */
export interface DrawControllerDeps {
  makeDraw?: (map: MapLibreMap) => TerraDraw;
  /**
   * NEUTRAL-LINE mode (purpose="line"): when true, an untagged drawn LineString
   * is read back as role="line" (a plain elevation/section line for
   * compute_terrain_profile) instead of role="barrier". No wall/flap_gate tag is
   * ever required and `counts().untaggedBarrier` stays 0 for these lines.
   * ADDITIVE -- default false keeps the SWMM barrier flow byte-for-byte
   * unchanged.
   */
  neutralLine?: boolean;
}

/** Properties terra-draw stamps on its own features (mode key) plus our tags. */
interface DrawProps {
  mode?: string;
  role?: SpatialDrawRole;
  barrier_type?: BarrierType;
  flap_direction?: FlapDirection;
  protected_side?: "left" | "right";
  [key: string]: unknown;
}

/**
 * Build the real TerraDraw instance bound to a live MapLibre map. terra-draw
 * renders into the map's own style, so the caller MUST ensure the style has
 * loaded before `start()` (Map.tsx gates on `mapStyleReady`).
 */
export function buildTerraDraw(map: MapLibreMap): TerraDraw {
  // Per-feature style fn — receives the FULL feature so barrier_type drives the
  // line color. wall=red, flap_gate=green, untagged barrier=amber.
  const barrierColor = (feature: GeoJSONStoreFeatures): HexColor => {
    const props = (feature.properties ?? {}) as DrawProps;
    if (props.barrier_type === "wall") return WALL_COLOR;
    if (props.barrier_type === "flap_gate") return FLAP_GATE_COLOR;
    return UNTAGGED_BARRIER_COLOR;
  };
  return new TerraDraw({
    adapter: new TerraDrawMapLibreGLAdapter({ map }),
    modes: [
      new TerraDrawRectangleMode({
        styles: {
          fillColor: AOI_COLOR,
          fillOpacity: 0.12,
          outlineColor: AOI_COLOR,
          outlineWidth: 2,
        },
      }),
      new TerraDrawPolygonMode({
        styles: {
          fillColor: AOI_COLOR,
          fillOpacity: 0.12,
          outlineColor: AOI_COLOR,
          outlineWidth: 2,
        },
        // edit vertices while drawing
        showCoordinatePoints: true,
      }),
      new TerraDrawLineStringMode({
        editable: true,
        showCoordinatePoints: true,
        styles: {
          lineStringColor: barrierColor,
          lineStringWidth: 4,
        },
      }),
      new TerraDrawSelectMode({
        flags: {
          rectangle: {
            feature: {
              draggable: true,
              coordinates: { resizable: "opposite", draggable: true },
            },
          },
          polygon: {
            feature: {
              draggable: true,
              coordinates: { midpoints: true, draggable: true, deletable: true },
            },
          },
          linestring: {
            feature: {
              draggable: true,
              coordinates: { midpoints: true, draggable: true, deletable: true },
            },
          },
        },
      }),
    ],
  });
}

/**
 * Imperative controller over a single terra-draw surface. Lifecycle:
 *   const c = new DrawController(map); c.start(); c.setMode("rectangle");
 *   ... user draws / tags / snips ...
 *   const fc = c.getFeatureCollection();   // role-tagged, ready for the agent
 *   c.stop();                              // tears down terra-draw layers
 */
export class DrawController {
  private draw: TerraDraw;
  private started = false;
  /** NEUTRAL-LINE mode: untagged LineStrings read back as role="line". */
  private readonly neutralLine: boolean;
  private changeListeners = new Set<() => void>();
  private selectListeners = new Set<(id: DrawFeatureId) => void>();
  private readonly onChange = (): void => {
    for (const fn of this.changeListeners) fn();
  };
  private readonly onSelect = (id: DrawFeatureId): void => {
    for (const fn of this.selectListeners) fn(id);
  };

  constructor(map: MapLibreMap, deps: DrawControllerDeps = {}) {
    this.draw = (deps.makeDraw ?? buildTerraDraw)(map);
    this.neutralLine = deps.neutralLine === true;
  }

  /** Begin drawing (registers the adapter + terra-draw layers on the map). */
  start(): void {
    if (this.started) return;
    this.draw.start();
    this.draw.on("change", this.onChange);
    this.draw.on("select", this.onSelect);
    this.started = true;
  }

  /** Stop drawing (deregisters the adapter, clears the store + map layers). */
  stop(): void {
    if (!this.started) return;
    try {
      this.draw.off("change", this.onChange);
      this.draw.off("select", this.onSelect);
    } catch {
      /* best effort */
    }
    try {
      this.draw.stop();
    } catch {
      /* already stopped / map torn down */
    }
    this.started = false;
    this.changeListeners.clear();
    this.selectListeners.clear();
  }

  /** Switch the active draw mode (toolbar button). */
  setMode(mode: DrawMode): void {
    this.draw.setMode(mode);
  }

  /** The active mode name. */
  getMode(): string {
    return this.draw.getMode();
  }

  /** Subscribe to any store change (add / edit / delete). Returns unsub. */
  onChanged(fn: () => void): () => void {
    this.changeListeners.add(fn);
    return () => this.changeListeners.delete(fn);
  }

  /** Subscribe to a feature SELECT (opens the tagging UI). Returns unsub. */
  onSelected(fn: (id: DrawFeatureId) => void): () => void {
    this.selectListeners.add(fn);
    return () => this.selectListeners.delete(fn);
  }

  /** Wipe every drawn feature (Clear-all). */
  clear(): void {
    this.draw.clear();
  }

  /** Snip (remove) one feature — a stray barrier segment / unwanted shape. */
  snipFeature(id: DrawFeatureId): void {
    this.draw.removeFeatures([id]);
  }

  /**
   * Tag a barrier segment (Pattern A: one LineString feature == one segment).
   * Writes role="barrier" + barrier_type, and (for flap_gate) flap_direction +
   * optional protected_side. The per-feature style fn re-colors it on the next
   * render (wall=red, flap_gate=green).
   */
  tagBarrier(
    id: DrawFeatureId,
    barrierType: BarrierType,
    opts: { flapDirection?: FlapDirection; protectedSide?: "left" | "right" } = {},
  ): void {
    const props: Record<string, string | number | undefined> = {
      role: "barrier",
      barrier_type: barrierType,
    };
    if (barrierType === "flap_gate") {
      if (opts.flapDirection !== undefined) props.flap_direction = opts.flapDirection;
      if (opts.protectedSide !== undefined) props.protected_side = opts.protectedSide;
    } else {
      // A wall carries no flap direction — clear any stale value if it was
      // previously tagged as a flap gate.
      props.flap_direction = undefined;
      props.protected_side = undefined;
    }
    this.draw.updateFeatureProperties(id, props);
  }

  /** Raw terra-draw snapshot (every drawn feature). */
  getSnapshot(): GeoJSONStoreFeatures[] {
    return this.draw.getSnapshot();
  }

  /**
   * Discard tiny polygons (the "area-threshold discard of small enclosed
   * lakes/polygons" requirement). Computes each Polygon's area in m² (local
   * planar approximation) and removes those below `minAreaM2`. Returns the
   * dropped feature ids. LineStrings / Points / rectangles drawn as polygons
   * above the threshold are untouched.
   */
  discardSmallPolygons(minAreaM2: number): DrawFeatureId[] {
    const dropped: DrawFeatureId[] = [];
    for (const f of this.draw.getSnapshot()) {
      if (f.geometry.type !== "Polygon") continue;
      if (f.id === undefined) continue;
      const area = polygonAreaM2(f.geometry.coordinates as number[][][]);
      if (area < minAreaM2) dropped.push(f.id);
    }
    if (dropped.length > 0) this.draw.removeFeatures(dropped);
    return dropped;
  }

  /**
   * Read the drawn geometry back as the role-tagged GeoJSON FeatureCollection
   * the `spatial-input-response` carries. Each feature is normalized to the
   * spatial-draw contract shape:
   *   - Polygon  -> role "aoi"
   *   - LineString that was tagged -> role "barrier" + barrier_type (+ flap dir)
   *   - LineString untagged -> role "barrier" (no barrier_type — surfaced
   *     honestly rather than silently coercing OR silently dropping)
   *   - Point    -> role "point"
   * terra-draw's own `mode` property is stripped; everything else is preserved.
   *
   * UNTAGGED-BARRIER CONTRACT (FR-WC-16): this readback is a faithful, lossless
   * mirror of what is on the surface — it deliberately does NOT omit or coerce
   * an untagged barrier (that would either silently drop the user's drawn work
   * or fabricate a wall/flap they never chose). The guarantee that a SUBMITTED
   * response never carries a role=="barrier" feature without barrier_type is
   * enforced by the SpatialDrawSurface submit gate (canSubmit is false while
   * `counts().untaggedBarrier > 0`), and by the server-side ws.py validator.
   * Keeping the readback honest here means the gate has the real inventory to
   * decide on; the gate, not this method, is the single chokepoint.
   */
  getFeatureCollection(): SpatialDrawFeatureCollection {
    const features: SpatialDrawFeature[] = [];
    for (const f of this.draw.getSnapshot()) {
      const props = (f.properties ?? {}) as DrawProps;
      const geomType = f.geometry.type;
      let role: SpatialDrawRole;
      if (
        props.role === "aoi" ||
        props.role === "barrier" ||
        props.role === "point" ||
        props.role === "line"
      ) {
        role = props.role;
      } else if (geomType === "LineString") {
        // NEUTRAL-LINE mode: an untagged drawn LineString is a plain
        // elevation/section line (role="line"), NOT a SWMM barrier. The default
        // (barrier) flow is untouched.
        role = this.neutralLine ? "line" : "barrier";
      } else if (geomType === "Point") {
        role = "point";
      } else {
        role = "aoi"; // Polygon / Rectangle-as-Polygon
      }
      const cleaned: SpatialDrawFeature["properties"] = { role };
      if (role === "barrier") {
        if (props.barrier_type) cleaned.barrier_type = props.barrier_type;
        if (props.flap_direction !== undefined) cleaned.flap_direction = props.flap_direction;
        if (props.protected_side !== undefined) cleaned.protected_side = props.protected_side;
      }
      features.push({
        type: "Feature",
        geometry: {
          type: geomType,
          coordinates: f.geometry.coordinates as unknown,
        },
        properties: cleaned,
      });
    }
    return { type: "FeatureCollection", features };
  }

  /** Count drawn barriers / AOIs / lines (toolbar badge / submit-enabled gate).
   *
   * In NEUTRAL-LINE mode a drawn LineString counts as a `line` (never a
   * `barrier`), so `untaggedBarrier` stays 0 and the submit gate is NOT blocked
   * on tagging. In the default (barrier) mode the line counting is unchanged. */
  counts(): {
    aoi: number;
    barrier: number;
    untaggedBarrier: number;
    point: number;
    line: number;
  } {
    let aoi = 0;
    let barrier = 0;
    let untaggedBarrier = 0;
    let point = 0;
    let line = 0;
    for (const f of this.draw.getSnapshot()) {
      const props = (f.properties ?? {}) as DrawProps;
      const geomType = f.geometry.type;
      if (geomType === "Polygon") aoi += 1;
      else if (geomType === "Point") point += 1;
      else if (geomType === "LineString") {
        if (this.neutralLine) {
          line += 1;
        } else {
          barrier += 1;
          if (!props.barrier_type) untaggedBarrier += 1;
        }
      }
    }
    return { aoi, barrier, untaggedBarrier, point, line };
  }
}

// --- Pure geometry helper ------------------------------------------------- //

const EARTH_RADIUS_M = 6_378_137;
const DEG_TO_RAD = Math.PI / 180;

/**
 * Approximate the area (m²) of a GeoJSON Polygon ring set using a local
 * equirectangular projection centered on the ring's mean latitude, then the
 * planar shoelace formula. Exact enough for the small urban AOIs this targets
 * (sub-km), dependency-free (no @turf/area), and stable for the area-threshold
 * "drop tiny lakes" use case. Outer ring area minus inner-ring (hole) areas.
 */
export function polygonAreaM2(rings: number[][][]): number {
  if (!rings || rings.length === 0) return 0;
  let total = 0;
  for (let r = 0; r < rings.length; r++) {
    const ring = rings[r]!;
    const ringArea = Math.abs(ringAreaM2(ring));
    // First ring is the outer boundary (+), subsequent rings are holes (-).
    total += r === 0 ? ringArea : -ringArea;
  }
  return Math.max(0, total);
}

function ringAreaM2(ring: number[][]): number {
  if (ring.length < 3) return 0;
  // Mean latitude for the local equirectangular scale.
  let latSum = 0;
  for (const pt of ring) latSum += pt[1] ?? 0;
  const meanLat = (latSum / ring.length) * DEG_TO_RAD;
  const mPerDegLon = EARTH_RADIUS_M * DEG_TO_RAD * Math.cos(meanLat);
  const mPerDegLat = EARTH_RADIUS_M * DEG_TO_RAD;
  // Shoelace in projected metres.
  let area = 0;
  for (let i = 0; i < ring.length - 1; i++) {
    const [lon1, lat1] = ring[i]!;
    const [lon2, lat2] = ring[i + 1]!;
    const x1 = (lon1 ?? 0) * mPerDegLon;
    const y1 = (lat1 ?? 0) * mPerDegLat;
    const x2 = (lon2 ?? 0) * mPerDegLon;
    const y2 = (lat2 ?? 0) * mPerDegLat;
    area += x1 * y2 - x2 * y1;
  }
  return area / 2;
}
