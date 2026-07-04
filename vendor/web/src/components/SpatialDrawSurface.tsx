// GRACE-2 web — SpatialDrawSurface (FR-WC-13 pick-mode + FR-WC-16 urban
// vector-draw). The on-MAP host for a paused `spatial-input-request`.
//
// Mounted INSIDE the Map container (so it overlays the live MapLibre canvas) by
// Map.tsx whenever the spatial-input bus carries an active request. It owns:
//
//   - A small banner (top-center) echoing the agent's title/description — the
//     reused FR-WC-13 pick-mode banner pattern.
//   - mode "point" / "bbox": a thin pick layer — the user clicks (point) or
//     drags a rectangle (bbox) on the map; the result feeds the bus on Submit.
//   - mode "vector_draw": a draw TOOLBAR (rectangle / line / polygon / select +
//     snip + clear) driving a DrawController (terra-draw), plus a per-segment
//     TAGGING popover (wall=red / flap_gate=green; flap direction in|out).
//   - A Submit + Cancel affordance pinned bottom-center.
//
// The component never touches the WebSocket — it relays the completed geometry
// (or a cancel) through the spatial-input bus to Chat.tsx, the reply owner.

import { useEffect, useMemo, useRef, useState } from "react";
import type { Map as MapLibreMap, MapMouseEvent, GeoJSONSource } from "maplibre-gl";
import type {
  BarrierType,
  SpatialInputRequestPayload,
} from "../contracts";
import type { SpatialInputResult } from "../lib/spatial_input_bus";
import {
  DrawController,
  type DrawControllerDeps,
  type DrawFeatureId,
  type DrawMode,
} from "../lib/draw_controller";
// #170 J-WEB-1 - the bbox drag gesture + ordering / cursor / style helpers were
// extracted into lib/bbox_draw.ts so the request-free AoiPickerCard can reuse
// them. SpatialDrawSurface keeps its own PICK_* layer set + draw helpers (so its
// pick-layer behavior is byte-identical) but now shares the single
// implementation of the gesture (attachBboxDrag) + the pure ordering/cursor/
// style-loaded primitives.
import {
  attachBboxDrag,
  safeStyleLoaded,
  setCursor,
} from "../lib/bbox_draw";
import {
  IconBbox,
  IconPolygon,
  IconLine,
  IconSnip,
  IconMapPin,
  IconClose,
  IconCheck,
  IconWarning,
  IconFlowArrow,
} from "./icons";

// --- Pick-mode (point / bbox) drawing layer ids -------------------------- //

const PICK_SOURCE_ID = "grace2-spatial-pick";
const PICK_FILL_LAYER_ID = "grace2-spatial-pick-fill";
const PICK_LINE_LAYER_ID = "grace2-spatial-pick-line";
const PICK_POINT_LAYER_ID = "grace2-spatial-pick-point";

const PICK_COLOR = "#3b82f6";

// --- Default discard threshold for tiny polygons (m²) -------------------- //

/** Lakes/ponds smaller than this (≈ a 16 m × 16 m square) are dropped by the
 * "discard tiny polygons" control by default; exposed as a slider in the UI. */
export const DEFAULT_DISCARD_AREA_M2 = 250;

export interface SpatialDrawSurfaceProps {
  /** The live MapLibre instance (Map.tsx's `map.current`). */
  map: MapLibreMap;
  /** The active spatial-input request. */
  request: SpatialInputRequestPayload;
  /** Relay a completed pick / draw to Chat (the WS reply owner) via the bus. */
  onSubmit: (result: SpatialInputResult) => void;
  /** Relay a cancellation to Chat via the bus. */
  onCancel: (requestId: string) => void;
  /** Injectable terra-draw factory for tests (defaults to the real lib). */
  drawDeps?: DrawControllerDeps;
}

interface TagTarget {
  id: DrawFeatureId;
}

export function SpatialDrawSurface({
  map,
  request,
  onSubmit,
  onCancel,
  drawDeps,
}: SpatialDrawSurfaceProps): JSX.Element {
  const isVectorDraw = request.mode === "vector_draw";
  // NEUTRAL-LINE request (purpose="line"): the user draws ONE plain elevation /
  // section LineString (for compute_terrain_profile) with NO wall/flap_gate
  // tagging. ADDITIVE + gated on the request -- the default (barrier) SWMM flow
  // is byte-for-byte unchanged.
  const isNeutralLine = isVectorDraw && request.purpose === "line";

  // --- vector_draw: DrawController lifecycle ----------------------------- //
  const controllerRef = useRef<DrawController | null>(null);
  const [activeMode, setActiveMode] = useState<DrawMode>(
    isNeutralLine ? "linestring" : "rectangle",
  );
  const [counts, setCounts] = useState({
    aoi: 0,
    barrier: 0,
    untaggedBarrier: 0,
    point: 0,
    line: 0,
  });
  const [tagTarget, setTagTarget] = useState<TagTarget | null>(null);
  const [flapDirection, setFlapDirection] = useState<"in" | "out">("out");
  const [discardArea, setDiscardArea] = useState<number>(DEFAULT_DISCARD_AREA_M2);
  const [discardNotice, setDiscardNotice] = useState<string | null>(null);

  // --- point / bbox pick state ------------------------------------------ //
  // coordinates carried back: point=[lon,lat]; bbox=[minLon,minLat,maxLon,maxLat]
  const [pickCoords, setPickCoords] = useState<number[] | null>(null);

  // Frame the suggested view so picking is easy (mirrors region-choice fitBounds).
  useEffect(() => {
    const view = request.suggested_view;
    if (!view) return;
    try {
      const [minLon, minLat, maxLon, maxLat] = view.bbox;
      map.fitBounds(
        [
          [minLon, minLat],
          [maxLon, maxLat],
        ],
        { padding: 64, duration: 600, maxZoom: 17 },
      );
    } catch {
      /* degenerate bbox — leave the camera */
    }
    // Re-frame only when the request changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [request.request_id]);

  // Mount / unmount the DrawController for vector_draw.
  useEffect(() => {
    if (!isVectorDraw) return;
    // NEUTRAL-LINE mode (purpose="line"): the controller reads back untagged
    // LineStrings as role="line" (not barrier), and the surface starts in the
    // line tool. Default (barrier) behavior is unchanged.
    const controller = new DrawController(map, {
      ...drawDeps,
      neutralLine: isNeutralLine || drawDeps?.neutralLine,
    });
    controllerRef.current = controller;
    controller.start();
    const startMode: DrawMode = isNeutralLine ? "linestring" : "rectangle";
    controller.setMode(startMode);
    setActiveMode(startMode);
    const refresh = (): void => setCounts(controller.counts());
    const unsubChange = controller.onChanged(refresh);
    const unsubSelect = controller.onSelected((id) => {
      // NEUTRAL-LINE mode never opens the barrier tag popover (a neutral line is
      // submitted plain -- no wall/flap_gate tagging). In the default barrier
      // flow, only barrier LineStrings get the tag popover.
      if (isNeutralLine) {
        setTagTarget(null);
        return;
      }
      const snap = controller.getSnapshot().find((f) => f.id === id);
      if (snap && snap.geometry.type === "LineString") {
        setTagTarget({ id });
      } else {
        setTagTarget(null);
      }
    });
    refresh();
    return () => {
      unsubChange();
      unsubSelect();
      controller.stop();
      controllerRef.current = null;
      setTagTarget(null);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isVectorDraw, request.request_id]);

  // --- point / bbox pick-mode handlers ----------------------------------- //
  useEffect(() => {
    if (isVectorDraw) return;
    ensurePickLayers(map);

    if (request.mode === "point") {
      const onClick = (e: MapMouseEvent): void => {
        const coords = [e.lngLat.lng, e.lngLat.lat];
        setPickCoords(coords);
        drawPickPoint(map, coords);
      };
      map.on("click", onClick);
      const prevCursor = setCursor(map, "crosshair");
      return () => {
        map.off("click", onClick);
        setCursor(map, prevCursor);
        clearPickLayers(map);
      };
    }

    // bbox: drag a rectangle. The down -> move -> up gesture (dragPan disabled
    // during the drag so the rectangle, not the map, moves) lives in
    // attachBboxDrag now; we wire its progress/complete callbacks to this
    // surface's own PICK_* draw layers + pick state.
    const detach = attachBboxDrag(map, {
      onProgress: (bbox) => drawPickBbox(map, bbox),
      onComplete: (bbox) => {
        setPickCoords(bbox);
        drawPickBbox(map, bbox);
      },
    });
    return () => {
      detach();
      clearPickLayers(map);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isVectorDraw, request.mode, request.request_id]);

  // --- Actions ----------------------------------------------------------- //

  function handleSetMode(mode: DrawMode): void {
    const c = controllerRef.current;
    if (!c) return;
    c.setMode(mode);
    setActiveMode(mode);
    if (mode !== "select") setTagTarget(null);
  }

  function handleTag(barrierType: BarrierType): void {
    const c = controllerRef.current;
    if (!c || !tagTarget) return;
    c.tagBarrier(tagTarget.id, barrierType, {
      flapDirection: barrierType === "flap_gate" ? flapDirection : undefined,
    });
    setCounts(c.counts());
    setTagTarget(null);
  }

  function handleSnip(): void {
    const c = controllerRef.current;
    if (!c || !tagTarget) return;
    c.snipFeature(tagTarget.id);
    setCounts(c.counts());
    setTagTarget(null);
  }

  function handleClear(): void {
    const c = controllerRef.current;
    if (!c) return;
    c.clear();
    setCounts(c.counts());
    setTagTarget(null);
    setDiscardNotice(null);
  }

  function handleDiscardSmall(): void {
    const c = controllerRef.current;
    if (!c) return;
    const dropped = c.discardSmallPolygons(discardArea);
    setCounts(c.counts());
    setDiscardNotice(
      dropped.length > 0
        ? `Discarded ${dropped.length} polygon${dropped.length === 1 ? "" : "s"} under ${discardArea} m²`
        : `No polygons under ${discardArea} m²`,
    );
  }

  function handleSubmit(): void {
    // Authoritative gate — never relay a response while submit is blocked (e.g.
    // an untagged barrier would otherwise round-trip a role=="barrier" feature
    // with no barrier_type). The disabled button is the affordance; this guard
    // closes the programmatic path.
    if (!canSubmit) return;
    if (isVectorDraw) {
      const c = controllerRef.current;
      if (!c) return;
      const features = c.getFeatureCollection();
      onSubmit({
        requestId: request.request_id,
        geometryType: "vector_draw",
        coordinates: null,
        features,
      });
    } else {
      if (!pickCoords) return;
      onSubmit({
        requestId: request.request_id,
        geometryType: request.mode === "point" ? "point" : "bbox",
        coordinates: pickCoords,
        features: null,
      });
    }
  }

  function handleCancel(): void {
    onCancel(request.request_id);
  }

  // --- Submit-enabled gate ----------------------------------------------- //
  // FR-WC-16: a vector_draw response must NEVER carry a role=="barrier" feature
  // with no barrier_type (the untagged-barrier mismatch). The toolbar already
  // tracks `counts.untaggedBarrier`; we hard-block submit while ANY drawn
  // barrier is still untagged, and surface the reason. This is the client-side
  // half of the gate that pairs with draw_controller.getFeatureCollection()
  // emitting untyped barriers honestly (never silently coercing them).
  const submitBlockReason = useMemo<string | null>(() => {
    if (isVectorDraw) {
      // NEUTRAL-LINE mode: no barrier tagging is involved at all -- submit is
      // gated only on having drawn at least one line. (The default barrier flow
      // below is byte-for-byte unchanged.)
      if (isNeutralLine) {
        return counts.line > 0 ? null : "Draw a line on the map to submit";
      }
      if (counts.untaggedBarrier > 0) {
        return "Tag every barrier as wall or flap-gate to submit";
      }
      if (counts.aoi + counts.barrier + counts.point === 0) {
        return "Draw an area, barrier, or point to submit";
      }
      return null;
    }
    return pickCoords !== null ? null : "Pick a location on the map to submit";
  }, [isVectorDraw, isNeutralLine, counts, pickCoords]);

  const canSubmit = submitBlockReason === null;

  // --- Render ------------------------------------------------------------ //
  return (
    <div data-testid="spatial-draw-surface" style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
      {/* Banner (reused FR-WC-13 pick-mode banner pattern). */}
      <div data-testid="spatial-draw-banner" style={bannerStyle}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          <IconBbox size={15} color={PICK_COLOR} />
          <span style={{ fontWeight: 600 }}>{request.title}</span>
        </span>
        <span style={{ color: "#cbd5e1", fontSize: 12 }}>{request.description}</span>
      </div>

      {/* vector_draw NEUTRAL-LINE toolbar (purpose="line"): just draw a plain
          elevation/section line -- no AOI/barrier/tag affordances. ADDITIVE; the
          default barrier toolbar below is unchanged. */}
      {isVectorDraw && isNeutralLine && (
        <div data-testid="spatial-draw-toolbar" style={toolbarStyle}>
          <ToolbarBtn
            label="Line"
            active={activeMode === "linestring"}
            onClick={() => handleSetMode("linestring")}
            icon={<IconLine size={16} />}
            testid="draw-mode-linestring"
          />
          <ToolbarBtn
            label="Select / edit"
            active={activeMode === "select"}
            onClick={() => handleSetMode("select")}
            icon={<IconMapPin size={16} />}
            testid="draw-mode-select"
          />
          <ToolbarBtn
            label="Clear all"
            onClick={handleClear}
            icon={<IconClose size={16} />}
            testid="draw-clear"
          />
          <span data-testid="draw-counts" style={countsStyle}>
            {counts.line} line{counts.line === 1 ? "" : "s"}
          </span>
        </div>
      )}

      {/* vector_draw toolbar (default barrier flow). */}
      {isVectorDraw && !isNeutralLine && (
        <div data-testid="spatial-draw-toolbar" style={toolbarStyle}>
          <ToolbarBtn
            label="Rectangle (AOI)"
            active={activeMode === "rectangle"}
            onClick={() => handleSetMode("rectangle")}
            icon={<IconBbox size={16} />}
            testid="draw-mode-rectangle"
          />
          <ToolbarBtn
            label="Polygon (AOI)"
            active={activeMode === "polygon"}
            onClick={() => handleSetMode("polygon")}
            icon={<IconPolygon size={16} />}
            testid="draw-mode-polygon"
          />
          <ToolbarBtn
            label="Line (barrier)"
            active={activeMode === "linestring"}
            onClick={() => handleSetMode("linestring")}
            icon={<IconLine size={16} />}
            testid="draw-mode-linestring"
          />
          <ToolbarBtn
            label="Select / edit"
            active={activeMode === "select"}
            onClick={() => handleSetMode("select")}
            icon={<IconMapPin size={16} />}
            testid="draw-mode-select"
          />
          <div style={{ width: 1, background: "rgba(255,255,255,0.12)", margin: "2px 4px" }} />
          <ToolbarBtn
            label="Discard tiny polygons"
            onClick={handleDiscardSmall}
            icon={<IconWarning size={16} />}
            testid="draw-discard-small"
          />
          <ToolbarBtn
            label="Clear all"
            onClick={handleClear}
            icon={<IconClose size={16} />}
            testid="draw-clear"
          />
          <span data-testid="draw-counts" style={countsStyle}>
            {counts.aoi} AOI · {counts.barrier} barrier
            {counts.untaggedBarrier > 0 ? ` (${counts.untaggedBarrier} untagged)` : ""}
          </span>
        </div>
      )}

      {/* Tagging popover (vector_draw select a barrier segment). */}
      {isVectorDraw && tagTarget && (
        <div data-testid="spatial-draw-tag-popover" style={tagPopoverStyle}>
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>Tag barrier segment</div>
          <div style={{ display: "flex", gap: 6 }}>
            <button
              type="button"
              data-testid="tag-wall"
              onClick={() => handleTag("wall")}
              style={tagBtnStyle("#e53935")}
            >
              Wall (red)
            </button>
            <button
              type="button"
              data-testid="tag-flap-gate"
              onClick={() => handleTag("flap_gate")}
              style={tagBtnStyle("#43a047")}
            >
              Flap gate (green)
            </button>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 8 }}>
            <IconFlowArrow size={14} color="#cbd5e1" />
            <span style={{ fontSize: 11, color: "#cbd5e1" }}>Flap direction:</span>
            <button
              type="button"
              data-testid="flap-dir-out"
              onClick={() => setFlapDirection("out")}
              style={dirBtnStyle(flapDirection === "out")}
            >
              out
            </button>
            <button
              type="button"
              data-testid="flap-dir-in"
              onClick={() => setFlapDirection("in")}
              style={dirBtnStyle(flapDirection === "in")}
            >
              in
            </button>
          </div>
          <button
            type="button"
            data-testid="tag-snip"
            onClick={handleSnip}
            style={{ ...dirBtnStyle(false), marginTop: 8, display: "inline-flex", alignItems: "center", gap: 4 }}
          >
            <IconSnip size={13} /> Snip this segment
          </button>
        </div>
      )}

      {/* discard-area control + notice. (Not shown for a neutral-line draw --
          there are no polygons to discard.) */}
      {isVectorDraw && !isNeutralLine && (
        <div data-testid="spatial-draw-discard-control" style={discardControlStyle}>
          <label style={{ fontSize: 11, color: "#cbd5e1" }}>
            Min polygon area: {discardArea} m²
            <input
              type="range"
              data-testid="draw-discard-slider"
              min={0}
              max={5000}
              step={50}
              value={discardArea}
              onChange={(e) => setDiscardArea(Number(e.target.value))}
              style={{ display: "block", width: 160 }}
            />
          </label>
          {discardNotice && (
            <span data-testid="draw-discard-notice" style={{ fontSize: 11, color: "#fbbf24" }}>
              {discardNotice}
            </span>
          )}
        </div>
      )}

      {/* Submit + Cancel (pinned bottom-center). */}
      <div data-testid="spatial-draw-actions" style={actionsStyle}>
        {submitBlockReason && (
          <span
            data-testid="spatial-draw-submit-reason"
            role="status"
            style={submitReasonStyle}
          >
            <IconWarning size={13} color="#fbbf24" />
            {submitBlockReason}
          </span>
        )}
        <div style={{ display: "flex", gap: 10 }}>
          <button
            type="button"
            data-testid="spatial-draw-cancel"
            onClick={handleCancel}
            style={cancelBtnStyle}
          >
            <IconClose size={14} /> Cancel
          </button>
          <button
            type="button"
            data-testid="spatial-draw-submit"
            onClick={handleSubmit}
            disabled={!canSubmit}
            title={submitBlockReason ?? undefined}
            style={submitBtnStyle(canSubmit)}
          >
            <IconCheck size={14} /> Submit
          </button>
        </div>
      </div>
    </div>
  );
}

// --- Toolbar button ------------------------------------------------------- //

function ToolbarBtn({
  label,
  active,
  onClick,
  icon,
  testid,
}: {
  label: string;
  active?: boolean;
  onClick: () => void;
  icon: JSX.Element;
  testid: string;
}): JSX.Element {
  return (
    <button
      type="button"
      data-testid={testid}
      data-active={active ? "true" : "false"}
      aria-label={label}
      title={label}
      onClick={onClick}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        border: active ? "1px solid #3b82f6" : "1px solid rgba(255,255,255,0.12)",
        background: active ? "rgba(59,130,246,0.22)" : "rgba(28,28,34,0.92)",
        color: "#e5e7eb",
        borderRadius: 6,
        padding: "6px 9px",
        fontSize: 12,
        cursor: "pointer",
        pointerEvents: "auto",
      }}
    >
      {icon}
    </button>
  );
}

// --- Pick-layer helpers (point / bbox) ----------------------------------- //

function ensurePickLayers(map: MapLibreMap): void {
  if (!safeStyleLoaded(map)) {
    map.once("idle", () => ensurePickLayers(map));
    return;
  }
  if (!map.getSource(PICK_SOURCE_ID)) {
    map.addSource(PICK_SOURCE_ID, {
      type: "geojson",
      data: { type: "FeatureCollection", features: [] },
    });
  }
  if (!map.getLayer(PICK_FILL_LAYER_ID)) {
    map.addLayer({
      id: PICK_FILL_LAYER_ID,
      type: "fill",
      source: PICK_SOURCE_ID,
      filter: ["==", ["geometry-type"], "Polygon"],
      paint: { "fill-color": PICK_COLOR, "fill-opacity": 0.15 },
    });
  }
  if (!map.getLayer(PICK_LINE_LAYER_ID)) {
    map.addLayer({
      id: PICK_LINE_LAYER_ID,
      type: "line",
      source: PICK_SOURCE_ID,
      filter: ["==", ["geometry-type"], "Polygon"],
      paint: { "line-color": PICK_COLOR, "line-width": 2 },
    });
  }
  if (!map.getLayer(PICK_POINT_LAYER_ID)) {
    map.addLayer({
      id: PICK_POINT_LAYER_ID,
      type: "circle",
      source: PICK_SOURCE_ID,
      filter: ["==", ["geometry-type"], "Point"],
      paint: {
        "circle-radius": 7,
        "circle-color": PICK_COLOR,
        "circle-stroke-color": "#ffffff",
        "circle-stroke-width": 2,
      },
    });
  }
}

function setPickData(map: MapLibreMap, data: GeoJSON.FeatureCollection): void {
  const src = map.getSource(PICK_SOURCE_ID) as GeoJSONSource | undefined;
  if (src && typeof src.setData === "function") src.setData(data);
}

function drawPickPoint(map: MapLibreMap, coords: number[]): void {
  setPickData(map, {
    type: "FeatureCollection",
    features: [
      {
        type: "Feature",
        geometry: { type: "Point", coordinates: coords },
        properties: {},
      },
    ],
  });
}

function drawPickBbox(map: MapLibreMap, bbox: number[]): void {
  const minLon = bbox[0] ?? 0;
  const minLat = bbox[1] ?? 0;
  const maxLon = bbox[2] ?? 0;
  const maxLat = bbox[3] ?? 0;
  setPickData(map, {
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
  });
}

function clearPickLayers(map: MapLibreMap): void {
  try {
    for (const id of [PICK_POINT_LAYER_ID, PICK_LINE_LAYER_ID, PICK_FILL_LAYER_ID]) {
      if (map.getLayer(id)) map.removeLayer(id);
    }
    if (map.getSource(PICK_SOURCE_ID)) map.removeSource(PICK_SOURCE_ID);
  } catch {
    /* map torn down / style swapped */
  }
}

// --- Styles --------------------------------------------------------------- //

const bannerStyle: React.CSSProperties = {
  position: "absolute",
  top: 12,
  left: "50%",
  transform: "translateX(-50%)",
  background: "rgba(20,20,26,0.92)",
  border: "1px solid rgba(255,255,255,0.08)",
  borderRadius: 8,
  boxShadow: "0 4px 14px rgba(0,0,0,0.4)",
  color: "#e5e7eb",
  padding: "8px 14px",
  display: "flex",
  flexDirection: "column",
  gap: 3,
  fontSize: 13,
  fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
  maxWidth: "70%",
  pointerEvents: "auto",
  zIndex: 5,
};

const toolbarStyle: React.CSSProperties = {
  position: "absolute",
  top: 70,
  left: "50%",
  transform: "translateX(-50%)",
  display: "flex",
  alignItems: "center",
  gap: 4,
  background: "rgba(20,20,26,0.85)",
  border: "1px solid rgba(255,255,255,0.08)",
  borderRadius: 8,
  padding: 5,
  pointerEvents: "auto",
  zIndex: 5,
};

const countsStyle: React.CSSProperties = {
  fontSize: 11,
  color: "#cbd5e1",
  padding: "0 6px",
  fontFamily: "system-ui, sans-serif",
};

const tagPopoverStyle: React.CSSProperties = {
  position: "absolute",
  top: 120,
  left: "50%",
  transform: "translateX(-50%)",
  background: "rgba(20,20,26,0.95)",
  border: "1px solid rgba(255,255,255,0.1)",
  borderRadius: 8,
  boxShadow: "0 4px 14px rgba(0,0,0,0.45)",
  color: "#e5e7eb",
  padding: 12,
  pointerEvents: "auto",
  zIndex: 6,
  fontFamily: "system-ui, sans-serif",
};

const discardControlStyle: React.CSSProperties = {
  position: "absolute",
  top: 70,
  right: 12,
  background: "rgba(20,20,26,0.85)",
  border: "1px solid rgba(255,255,255,0.08)",
  borderRadius: 8,
  padding: 8,
  display: "flex",
  flexDirection: "column",
  gap: 4,
  pointerEvents: "auto",
  zIndex: 5,
  fontFamily: "system-ui, sans-serif",
};

const actionsStyle: React.CSSProperties = {
  position: "absolute",
  bottom: 18,
  left: "50%",
  transform: "translateX(-50%)",
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  gap: 8,
  pointerEvents: "auto",
  zIndex: 6,
};

// Honest "why is Submit disabled" note pinned above the action buttons —
// surfaces the FR-WC-16 untagged-barrier block (and the other empty-draw /
// no-pick cases) in plain language. Amber matches the discard-notice + the
// untagged-barrier line color convention.
const submitReasonStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  background: "rgba(20,20,26,0.92)",
  border: "1px solid rgba(251,191,36,0.45)",
  borderRadius: 8,
  color: "#fbbf24",
  padding: "5px 12px",
  fontSize: 12,
  fontWeight: 500,
  fontFamily: "system-ui, sans-serif",
};

const cancelBtnStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 5,
  border: "1px solid rgba(255,255,255,0.14)",
  background: "rgba(28,28,34,0.95)",
  color: "#cbd5e1",
  borderRadius: 8,
  padding: "8px 16px",
  fontSize: 13,
  fontWeight: 500,
  cursor: "pointer",
  fontFamily: "system-ui, sans-serif",
};

function submitBtnStyle(enabled: boolean): React.CSSProperties {
  return {
    display: "inline-flex",
    alignItems: "center",
    gap: 5,
    border: "1px solid #3b82f6",
    background: enabled ? "#3b82f6" : "rgba(59,130,246,0.35)",
    color: enabled ? "#0b0b0e" : "rgba(255,255,255,0.55)",
    borderRadius: 8,
    padding: "8px 18px",
    fontSize: 13,
    fontWeight: 600,
    cursor: enabled ? "pointer" : "not-allowed",
    fontFamily: "system-ui, sans-serif",
  };
}

function tagBtnStyle(color: string): React.CSSProperties {
  return {
    border: `1px solid ${color}`,
    background: color,
    color: "#0b0b0e",
    borderRadius: 6,
    padding: "6px 10px",
    fontSize: 12,
    fontWeight: 600,
    cursor: "pointer",
    fontFamily: "system-ui, sans-serif",
  };
}

function dirBtnStyle(active: boolean): React.CSSProperties {
  return {
    border: active ? "1px solid #3b82f6" : "1px solid rgba(255,255,255,0.14)",
    background: active ? "rgba(59,130,246,0.22)" : "transparent",
    color: "#e5e7eb",
    borderRadius: 5,
    padding: "3px 8px",
    fontSize: 11,
    cursor: "pointer",
    fontFamily: "system-ui, sans-serif",
  };
}
