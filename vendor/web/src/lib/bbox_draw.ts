// GRACE-2 web - bbox draw util (#170 J-WEB-1).
//
// The bbox drag-rectangle gesture + the pick-layer GeoJSON plumbing, extracted
// verbatim out of components/SpatialDrawSurface.tsx so it can be reused by the
// REQUEST-FREE AoiPickerCard (#170 J-WEB-2) WITHOUT going through the
// spatial-input bus / SpatialDrawSurface (no active turn; the agent box may be
// asleep). SpatialDrawSurface keeps calling these helpers, so its existing
// behavior + tests are byte-preserved.
//
// Two surfaces:
//   - pure helpers (orderBbox / ensurePickLayers / drawPickBbox / drawPickPoint
//     / clearPickLayers / setCursor) - the same primitives the draw surface
//     used inline; lifted so both call sites share one implementation.
//   - attachBboxDrag(map, onBbox) - the down -> move -> up rectangle gesture as
//     a single self-cleaning attach (returns a detach fn). This is the reusable
//     "drag a rectangle on the live map" primitive the AoiPickerCard arms.

import type { Map as MapLibreMap, MapMouseEvent, GeoJSONSource } from "maplibre-gl";

// --- Pick-mode (point / bbox) drawing layer ids -------------------------- //
// Distinct ids from SpatialDrawSurface's PICK_* so an AoiPickerCard overlay and
// a (hypothetical) concurrent draw surface never collide on the same source.

export const BBOX_SOURCE_ID = "grace2-bbox-pick";
export const BBOX_FILL_LAYER_ID = "grace2-bbox-pick-fill";
export const BBOX_LINE_LAYER_ID = "grace2-bbox-pick-line";
export const BBOX_POINT_LAYER_ID = "grace2-bbox-pick-point";

export const BBOX_PICK_COLOR = "#3b82f6";

/** EPSG:4326 [minLon, minLat, maxLon, maxLat]. */
export type BBox = [number, number, number, number];

/** Layer id bundle so a caller can drive its OWN pick source (e.g. the draw
 * surface keeps its legacy ids; the AoiPickerCard uses the BBOX_* set). */
export interface PickLayerIds {
  sourceId: string;
  fillLayerId: string;
  lineLayerId: string;
  pointLayerId: string;
  color: string;
}

export const DEFAULT_PICK_LAYER_IDS: PickLayerIds = {
  sourceId: BBOX_SOURCE_ID,
  fillLayerId: BBOX_FILL_LAYER_ID,
  lineLayerId: BBOX_LINE_LAYER_ID,
  pointLayerId: BBOX_POINT_LAYER_ID,
  color: BBOX_PICK_COLOR,
};

// --- Geometry / ordering -------------------------------------------------- //

/** Order two corners into [minLon, minLat, maxLon, maxLat]. */
export function orderBbox(a: [number, number], b: [number, number]): BBox {
  return [
    Math.min(a[0], b[0]),
    Math.min(a[1], b[1]),
    Math.max(a[0], b[0]),
    Math.max(a[1], b[1]),
  ];
}

// --- Map helpers ---------------------------------------------------------- //

export function safeStyleLoaded(map: MapLibreMap): boolean {
  try {
    return map.isStyleLoaded() === true;
  } catch {
    return false;
  }
}

export function setCursor(map: MapLibreMap, cursor: string): string {
  try {
    const c = map.getCanvas();
    const prev = c.style.cursor;
    c.style.cursor = cursor;
    return prev;
  } catch {
    return "";
  }
}

// --- Pick layers ---------------------------------------------------------- //

export function ensurePickLayers(
  map: MapLibreMap,
  ids: PickLayerIds = DEFAULT_PICK_LAYER_IDS,
): void {
  if (!safeStyleLoaded(map)) {
    map.once("idle", () => ensurePickLayers(map, ids));
    return;
  }
  if (!map.getSource(ids.sourceId)) {
    map.addSource(ids.sourceId, {
      type: "geojson",
      data: { type: "FeatureCollection", features: [] },
    });
  }
  if (!map.getLayer(ids.fillLayerId)) {
    map.addLayer({
      id: ids.fillLayerId,
      type: "fill",
      source: ids.sourceId,
      filter: ["==", ["geometry-type"], "Polygon"],
      paint: { "fill-color": ids.color, "fill-opacity": 0.15 },
    });
  }
  if (!map.getLayer(ids.lineLayerId)) {
    map.addLayer({
      id: ids.lineLayerId,
      type: "line",
      source: ids.sourceId,
      filter: ["==", ["geometry-type"], "Polygon"],
      paint: { "line-color": ids.color, "line-width": 2 },
    });
  }
  if (!map.getLayer(ids.pointLayerId)) {
    map.addLayer({
      id: ids.pointLayerId,
      type: "circle",
      source: ids.sourceId,
      filter: ["==", ["geometry-type"], "Point"],
      paint: {
        "circle-radius": 7,
        "circle-color": ids.color,
        "circle-stroke-color": "#ffffff",
        "circle-stroke-width": 2,
      },
    });
  }
}

function setPickData(
  map: MapLibreMap,
  data: GeoJSON.FeatureCollection,
  ids: PickLayerIds = DEFAULT_PICK_LAYER_IDS,
): void {
  const src = map.getSource(ids.sourceId) as GeoJSONSource | undefined;
  if (src && typeof src.setData === "function") src.setData(data);
}

export function drawPickPoint(
  map: MapLibreMap,
  coords: number[],
  ids: PickLayerIds = DEFAULT_PICK_LAYER_IDS,
): void {
  setPickData(
    map,
    {
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          geometry: { type: "Point", coordinates: coords },
          properties: {},
        },
      ],
    },
    ids,
  );
}

export function drawPickBbox(
  map: MapLibreMap,
  bbox: number[],
  ids: PickLayerIds = DEFAULT_PICK_LAYER_IDS,
): void {
  const minLon = bbox[0] ?? 0;
  const minLat = bbox[1] ?? 0;
  const maxLon = bbox[2] ?? 0;
  const maxLat = bbox[3] ?? 0;
  setPickData(
    map,
    {
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          geometry: {
            type: "Polygon",
            coordinates: [
              [
                [minLon, minLat],
                [maxLon, minLat],
                [maxLon, maxLat],
                [minLon, maxLat],
                [minLon, minLat],
              ],
            ],
          },
          properties: {},
        },
      ],
    },
    ids,
  );
}

export function clearPickLayers(
  map: MapLibreMap,
  ids: PickLayerIds = DEFAULT_PICK_LAYER_IDS,
): void {
  try {
    for (const id of [ids.pointLayerId, ids.lineLayerId, ids.fillLayerId]) {
      if (map.getLayer(id)) map.removeLayer(id);
    }
    if (map.getSource(ids.sourceId)) map.removeSource(ids.sourceId);
  } catch {
    /* map torn down / style swapped */
  }
}

// --- The drag-rectangle gesture ------------------------------------------ //

export interface BboxDragHandlers {
  /** Fired on every move while dragging - the in-progress (ordered) bbox. */
  onProgress?: (bbox: BBox) => void;
  /** Fired on mouseup - the final (ordered) bbox. */
  onComplete: (bbox: BBox) => void;
}

/**
 * Attach the bbox drag gesture (down -> move -> up) to the live map and return
 * a detach fn that removes the listeners + re-enables dragPan + restores the
 * cursor. This is the exact gesture SpatialDrawSurface ran inline (dragPan
 * disabled during the drag so the rectangle, not the map, moves), lifted out so
 * the request-free AoiPickerCard can reuse it.
 *
 * NOTE: this attach owns ONLY the gesture (listeners + cursor + dragPan). It
 * does NOT draw onto the map; the caller wires `onProgress` / `onComplete` to
 * `drawPickBbox` (and `ensurePickLayers` up front) so the same plumbing serves
 * both call sites without this util reaching into a specific layer set.
 */
export function attachBboxDrag(
  map: MapLibreMap,
  handlers: BboxDragHandlers,
): () => void {
  let anchor: [number, number] | null = null;
  const onDown = (e: MapMouseEvent): void => {
    anchor = [e.lngLat.lng, e.lngLat.lat];
    map.dragPan.disable();
  };
  const onMove = (e: MapMouseEvent): void => {
    if (!anchor) return;
    const cur: [number, number] = [e.lngLat.lng, e.lngLat.lat];
    handlers.onProgress?.(orderBbox(anchor, cur));
  };
  const onUp = (e: MapMouseEvent): void => {
    if (!anchor) return;
    const cur: [number, number] = [e.lngLat.lng, e.lngLat.lat];
    handlers.onComplete(orderBbox(anchor, cur));
    anchor = null;
    map.dragPan.enable();
  };
  map.on("mousedown", onDown);
  map.on("mousemove", onMove);
  map.on("mouseup", onUp);
  const prevCursor = setCursor(map, "crosshair");
  return () => {
    map.off("mousedown", onDown);
    map.off("mousemove", onMove);
    map.off("mouseup", onUp);
    try {
      map.dragPan.enable();
    } catch {
      /* map torn down */
    }
    setCursor(map, prevCursor);
  };
}

// --- bbox -> screen rect projection (for AOI-anchored overlays) ---------- //

/** A screen-space rectangle in CSS pixels (origin = map container top-left).
 *  Mirrors legend_snap.ScreenRect / Map.tsx LegendScreenRect so an overlay
 *  pinned to a drawn bbox (e.g. the draw-mode Save/Retry/Cancel control) can
 *  reuse the same bottom-center anchoring math the legend + scrubber use. */
export interface BboxScreenRect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

/**
 * Project an EPSG:4326 bbox into the map's on-screen pixel rectangle (min/max
 * over all four projected corners), the same one-pass projection Map.tsx's
 * computeBboxScreenRect does for the legend. Returns null when the box can't be
 * projected (map torn down / mid style swap). Pure projection (Invariant 1: the
 * client only renders  -  every number comes from MapLibre's project()).
 */
export function projectBboxScreenRect(
  map: MapLibreMap,
  bbox: BBox | number[],
): BboxScreenRect | null {
  const minLon = bbox[0] ?? 0;
  const minLat = bbox[1] ?? 0;
  const maxLon = bbox[2] ?? 0;
  const maxLat = bbox[3] ?? 0;
  let pts: { x: number; y: number }[];
  try {
    pts = [
      map.project([minLon, minLat]), // SW
      map.project([maxLon, minLat]), // SE
      map.project([maxLon, maxLat]), // NE
      map.project([minLon, maxLat]), // NW
    ];
  } catch {
    return null;
  }
  let left = Infinity;
  let top = Infinity;
  let right = -Infinity;
  let bottom = -Infinity;
  for (const { x, y } of pts) {
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
    if (x < left) left = x;
    if (x > right) right = x;
    if (y < top) top = y;
    if (y > bottom) bottom = y;
  }
  if (!Number.isFinite(left) || !Number.isFinite(top)) return null;
  return { left, top, right, bottom };
}

// --- bbox validation (mirrors the server _is_finite_bbox4) --------------- //

/**
 * Validate + normalize a 4-number bbox the way the server's _is_finite_bbox4 /
 * _coerce_bbox4 does: all four finite, lon in [-180, 180], lat in [-90, 90],
 * and min/max ordered (so a user typing max < min still yields a valid box).
 * Returns the ordered BBox on success, or null on any out-of-range / non-finite
 * value. A zero-area degenerate box (min == max) is rejected (not a usable AOI).
 */
export function validateBbox(
  minLon: number,
  minLat: number,
  maxLon: number,
  maxLat: number,
): BBox | null {
  const vals = [minLon, minLat, maxLon, maxLat];
  if (vals.some((v) => !Number.isFinite(v))) return null;
  const loLon = Math.min(minLon, maxLon);
  const hiLon = Math.max(minLon, maxLon);
  const loLat = Math.min(minLat, maxLat);
  const hiLat = Math.max(minLat, maxLat);
  if (loLon < -180 || hiLon > 180) return null;
  if (loLat < -90 || hiLat > 90) return null;
  // Reject degenerate (zero-width or zero-height) extents.
  if (loLon === hiLon || loLat === hiLat) return null;
  return [loLon, loLat, hiLon, hiLat];
}
