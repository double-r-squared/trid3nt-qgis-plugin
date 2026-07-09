// GRACE-2 web — FeaturePopup (F74b feature-click/tap-to-inspect).
//
// The agent advertises "click polygons to see name / designation / IUCN" but
// no such handler existed in the web client. This component is the popup half
// of that feature: Map.tsx runs queryRenderedFeatures on a click/tap against
// the rendered inline-GeoJSON vector layers (job-0175) and, on a hit, renders
// THIS popup at the screen point with the feature's key attributes.
//
// Design choices:
//   - React overlay (NOT maplibregl.Popup) so we get full control over mobile
//     positioning, our icon set (icons.tsx — NO raw glyphs per the project
//     policy), and a tap-anywhere/Esc/X dismiss model that works for touch.
//     maplibregl.Popup's anchor logic can clip off the small-screen viewport;
//     a self-positioned overlay lets us clamp into the visible canvas.
//   - Pure presentational + dismiss callbacks. All hit-testing, property
//     extraction and screen-point math live in Map.tsx (Invariant 1: the
//     client renders received values — here, feature.properties — it never
//     computes geography).
//
// Invariant 1: every value shown comes straight from feature.properties; no
// number is computed here.

import { useEffect } from "react";
import { IconClose, IconDownload } from "./icons";
import { isLocalDeployment } from "../lib/deployment";
// csvFromFeatures is a PURE helper (no React / no map dependency) exported by
// Map.tsx. The import edge is call-time only (used inside the click handler
// below), so the Map<->FeaturePopup module cycle never bites at load time.
import { csvFromFeatures } from "../Map";

/** Enrich-failure copy (fingerprint audit A11): "awake" is cloud sleep/wake
 *  vocabulary - the LOCAL agent is always-on, so the local build blames a
 *  plain load failure instead. Cloud copy byte-identical when VITE_DEPLOYMENT
 *  is unset/cloud. Read at call time so vitest can stub the env. */
function enrichFailedCopy(): string {
  return isLocalDeployment()
    ? "Details unavailable -- the agent could not load building details."
    : "Details unavailable -- the agent must be awake to load building details.";
}

/** One attribute row in the popup. `label` is the human-facing key. */
export interface FeatureAttribute {
  label: string;
  value: string;
}

/** Screen-space point (canvas-relative px) where the feature was hit. */
export interface PopupPoint {
  x: number;
  y: number;
}

/** Geographic anchor (lng/lat) of the tapped feature, so the popup can stay
 *  glued to its MAP location across pans/zooms (FIX 2). */
export interface PopupLngLat {
  lng: number;
  lat: number;
}

/** Fully-resolved popup content + placement, produced by Map.tsx. */
export interface FeaturePopupData {
  /** Bold heading — the feature's best "name" (or a geometry-kind fallback). */
  title: string;
  /** Optional sub-heading — designation / type / layer name. */
  subtitle?: string;
  /** Ordered attribute rows (already humanized + stringified by the caller). */
  attributes: FeatureAttribute[];
  /**
   * Canvas-relative pixel point of the click/tap. FIX 2: this is now the
   * CURRENT projected screen point of `lngLat`, refreshed by Map.tsx on every
   * map move/zoom so the popup pans with the map. On the first paint it is the
   * raw tap point.
   */
  point: PopupPoint;
  /**
   * FIX 2: geographic anchor of the tapped feature. The popup is pinned to this
   * MAP location — Map.tsx re-projects it to `point` on every map move/zoom so
   * the card stays at the same spot ON the map (pans with it). Optional so older
   * callers / fixtures that only set `point` still render (screen-anchored).
   */
  lngLat?: PopupLngLat;
  /**
   * FIX 3: the map zoom captured at tap time — the reference for the
   * scale-with-zoom transform (scale = 2^(currentZoom - refZoom), clamped).
   * Optional; absent → no scaling (scale 1).
   */
  refZoom?: number;
  /**
   * L3-web-station-csv: the RAW property bag of the tapped station feature.
   * Present ONLY when the tapped layer is a station layer (USGS gauges /
   * ASOS-METAR / RAWS / NOAA CO-OPS). When set, the popup header shows a
   * Download-CSV button. Invariant 1: these are received feature properties
   * only - never computed geography.
   */
  rawProperties?: Record<string, unknown>;
  /**
   * L3-web-station-csv: the property bags for the WHOLE station layer (an
   * all-stations dump), captured from the GeoJSON source at tap time. Optional
   * - absent when the source was already gone, in which case the CSV falls back
   * to the single tapped feature (`rawProperties`).
   */
  layerFeatures?: Record<string, unknown>[];
  /** L3-web-station-csv: the station layer's id/name, used to derive a CSV
   *  filename when the tapped feature has no site identifier. */
  stationLayerName?: string;
  /**
   * Click-to-enrich (NATE 2026-06-27): true while a FOOTPRINT popup is fetching
   * its full tag bag by (osm_type, osm_id). When set, the card shows a small
   * "Loading details..." row beneath the (slim) id-only attributes. Map.tsx
   * flips it false + merges the returned tags into `attributes` on resolve.
   * Absent / false for every NON-footprint popup, which stays byte-for-byte
   * unchanged.
   */
  enriching?: boolean;
  /**
   * Click-to-enrich: the composite footprint id (e.g. "w123456") the async
   * detail fetch is keyed to. Used by Map.tsx ONLY to verify the in-flight
   * enrich still matches the open popup before merging tags (so a stale resolve
   * for a since-dismissed/replaced popup is dropped). Footprint-only; not
   * rendered.
   */
  enrichFid?: string;
  /**
   * Footprint enrich TERMINAL FAILURE (NATE 2026-06-28): set true by Map.tsx
   * when the detail fetch resolved null (failed / timed out -- e.g. the agent
   * box is asleep). The card then renders a one-line honest "details
   * unavailable" message instead of silently collapsing to a bare card (which
   * read as "loaded then stopped"). Footprint-only; absent/false for every
   * other popup, which stays byte-for-byte unchanged.
   */
  enrichFailed?: boolean;
}

export interface FeaturePopupProps {
  data: FeaturePopupData;
  /** Width/height of the map canvas (for off-screen clamping). */
  canvasSize: { width: number; height: number };
  /**
   * Mobile viewport. FIX 3 (F86): the card is anchored at the tap point on BOTH
   * surfaces now — this only widens the card slightly for touch
   * (CARD_WIDTH_MOBILE) and is otherwise no longer a positioning switch.
   */
  isMobile: boolean;
  /**
   * FIX 3 (NATE 2026-06-17): the CURRENT map zoom, so the card can scale like a
   * map-drawn label (shrinks zoomed out, grows zoomed in) relative to
   * `data.refZoom` captured at tap. Optional — absent → scale 1 (no scaling).
   */
  currentZoom?: number;
  /** Dismiss (X tap / Esc). Tap-elsewhere dismissal is wired in Map.tsx. */
  onClose: () => void;
}

// FIX 3 — scale clamp. NATE: "statically sized so we can zoom out and it gets
// smaller." The card scales with the map: scale = 2^(currentZoom - refZoom),
// clamped to a sane range so it never becomes illegible or huge. One zoom level
// doubles/halves on-screen feature size, so 2^Δzoom keeps the card the same
// MAP-relative size as the feature it labels.
export const POPUP_MIN_SCALE = 0.5;
export const POPUP_MAX_SCALE = 1.5;

/**
 * Pure, exported for unit testing. Returns the clamped CSS scale factor for the
 * popup given the reference zoom (at tap) and the current map zoom. Returns 1
 * when either zoom is missing (no scaling — the screen-anchored fallback).
 */
export function resolvePopupScale(
  refZoom: number | undefined,
  currentZoom: number | undefined,
): number {
  if (
    typeof refZoom !== "number" ||
    typeof currentZoom !== "number" ||
    !Number.isFinite(refZoom) ||
    !Number.isFinite(currentZoom)
  ) {
    return 1;
  }
  const raw = Math.pow(2, currentZoom - refZoom);
  return Math.max(POPUP_MIN_SCALE, Math.min(POPUP_MAX_SCALE, raw));
}

// Card sizing. Kept narrow so it does not blanket a phone screen, and wide
// enough on desktop to show a name + a few attributes without wrapping hard.
const CARD_WIDTH_DESKTOP = 260;
const CARD_WIDTH_MOBILE = 280;
const EDGE_GAP = 12; // min px between the card and the canvas edge.
const POINT_OFFSET = 14; // px the card is nudged from the clicked point.
const EST_CARD_HEIGHT = 220; // rough height used only for vertical clamping.

/**
 * Resolve the absolute {left, top} for the card.
 *
 * FIX 3 (F86, NATE 2026-06-17): the popup is ANCHORED AT THE TAP/CLICK POINT on
 * BOTH mobile and desktop ("the popup should be where I tapped"). The earlier
 * behaviour pinned the mobile card to the bottom-center of the canvas, which
 * detached it from where the user touched. We now place it just to the
 * upper-right of the point on every surface, then CLAMP it fully into the
 * canvas so it can never run off an edge (the clamp is what kept the
 * bottom-center fallback "safe" — here it does the same job at the point).
 *
 * Pure — exported so the placement math is unit-testable without rendering.
 */
export function resolvePopupPlacement(
  point: PopupPoint,
  canvasSize: { width: number; height: number },
  isMobile: boolean,
): { left: number; top: number; width: number } {
  const width = isMobile ? CARD_WIDTH_MOBILE : CARD_WIDTH_DESKTOP;
  const w = canvasSize.width || width + EDGE_GAP * 2;
  const h = canvasSize.height || EST_CARD_HEIGHT + EDGE_GAP * 2;

  // Place to the upper-right of the point, then clamp into the canvas. On a
  // narrow (mobile) viewport the wider card + the edge clamp keep it on-screen
  // exactly the way the desktop card already did — but now it stays at the tap.
  let left = point.x + POINT_OFFSET;
  let top = point.y - POINT_OFFSET;
  if (left + width + EDGE_GAP > w) {
    // Not enough room on the right — flip to the left of the point.
    left = point.x - width - POINT_OFFSET;
  }
  left = Math.min(Math.max(EDGE_GAP, left), Math.max(EDGE_GAP, w - width - EDGE_GAP));
  top = Math.min(
    Math.max(EDGE_GAP, top),
    Math.max(EDGE_GAP, h - EST_CARD_HEIGHT - EDGE_GAP),
  );
  return { left, top, width };
}

/**
 * L3-web-station-csv: derive a safe CSV filename for a station download. Prefers
 * the tapped feature's `site_no` (USGS / CO-OPS station id), then its `title`,
 * then the layer name; falls back to a generic "stations" base. Sanitized to
 * filesystem-safe characters and suffixed with `.csv`. Pure + exported so it is
 * unit-testable. `data` carries the raw property bag captured at tap time.
 */
export function stationCsvFilename(data: FeaturePopupData): string {
  const props = data.rawProperties ?? {};
  // Case-insensitive lookup for the site identifier.
  let siteNo: unknown;
  for (const k of Object.keys(props)) {
    if (k.toLowerCase() === "site_no") {
      siteNo = props[k];
      break;
    }
  }
  const candidate =
    (typeof siteNo === "string" && siteNo.trim()) ||
    (typeof siteNo === "number" && Number.isFinite(siteNo) && String(siteNo)) ||
    (data.title && data.title.trim()) ||
    (data.stationLayerName && data.stationLayerName.trim()) ||
    "stations";
  const base = String(candidate)
    .replace(/[^A-Za-z0-9._-]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 80);
  return `${base || "stations"}.csv`;
}

export function FeaturePopup({
  data,
  canvasSize,
  isMobile,
  currentZoom,
  onClose,
}: FeaturePopupProps): JSX.Element {
  // Esc dismisses (desktop + bluetooth-keyboard mobile). Tap-elsewhere is wired
  // in Map.tsx (it owns the map canvas + document listeners).
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const { left, top, width } = resolvePopupPlacement(
    data.point,
    canvasSize,
    isMobile,
  );

  // FIX 3 — scale the card to the map zoom so it reads like a map-drawn label.
  const scale = resolvePopupScale(data.refZoom, currentZoom);

  // L3-web-station-csv: the Download-CSV affordance shows ONLY when a station
  // CSV payload was attached (so non-station popups are completely unaffected).
  // Prefer the whole-layer dump; fall back to the single tapped feature when
  // the source was already gone.
  const csvRows: Record<string, unknown>[] | null =
    data.layerFeatures && data.layerFeatures.length > 0
      ? data.layerFeatures
      : data.rawProperties
        ? [data.rawProperties]
        : null;

  const onDownloadCsv = (): void => {
    if (!csvRows) return;
    try {
      // Invariant 1: csvFromFeatures serializes only the received property bags
      // (geometry is excluded), never computed geography.
      const csv = csvFromFeatures(csvRows);
      const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = stationCsvFilename(data);
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      // best-effort - a failed download must never break the popup.
    }
  };

  return (
    <div
      data-testid="grace2-feature-popup"
      data-popup-scale={scale}
      role="dialog"
      aria-label={data.title}
      // The popup must capture pointer events even though it sits over the map.
      style={{
        position: "absolute",
        left,
        top,
        width,
        maxWidth: "calc(100% - 24px)",
        maxHeight: "60%",
        overflowY: "auto",
        background: "rgba(17,18,23,0.92)",
        backdropFilter: "blur(8px)",
        WebkitBackdropFilter: "blur(8px)",
        border: "1px solid rgba(255,255,255,0.10)",
        borderRadius: 10,
        boxShadow: "0 6px 24px rgba(0,0,0,0.55)",
        color: "#eee",
        fontFamily: "system-ui, sans-serif",
        zIndex: 20, // above the legend (zIndex 10), below modals (>=2000).
        pointerEvents: "auto",
        // FIX 3 — scale keyed to map zoom. transform-origin at the anchor (the
        // tap point relative to the card's top-left = POINT_OFFSET, POINT_OFFSET)
        // so the card grows/shrinks AROUND the feature, not its corner.
        transform: scale !== 1 ? `scale(${scale})` : undefined,
        transformOrigin: `${POINT_OFFSET}px ${POINT_OFFSET}px`,
      }}
      // Stop taps inside the card from bubbling to the map's tap-elsewhere
      // dismissal (Map.tsx listens on the document).
      onPointerDown={(e) => e.stopPropagation()}
      onClick={(e) => e.stopPropagation()}
    >
      {/* Header: title + subtitle + close button. */}
      <div
        style={{
          display: "flex",
          alignItems: "flex-start",
          gap: 8,
          padding: "10px 10px 8px 12px",
          borderBottom:
            data.attributes.length > 0
              ? "1px solid rgba(255,255,255,0.08)"
              : "none",
        }}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          <div
            data-testid="feature-popup-title"
            style={{
              fontSize: 13,
              fontWeight: 600,
              lineHeight: 1.25,
              wordBreak: "break-word",
              color: "#fff",
            }}
          >
            {data.title}
          </div>
          {data.subtitle ? (
            <div
              data-testid="feature-popup-subtitle"
              style={{
                fontSize: 11,
                color: "#9aa3b2",
                marginTop: 2,
                wordBreak: "break-word",
              }}
            >
              {data.subtitle}
            </div>
          ) : null}
        </div>
        {/* L3-web-station-csv: Download-CSV button - rendered ONLY for station
            popups (csvRows present), so non-station popups are unaffected. */}
        {csvRows ? (
          <button
            type="button"
            data-testid="feature-popup-download-csv"
            aria-label="Download CSV"
            title="Download CSV"
            onClick={onDownloadCsv}
            style={{
              flex: "0 0 auto",
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              width: 28,
              height: 28,
              // Generous touch target for mobile (>=28px).
              padding: 0,
              background: "transparent",
              border: "none",
              borderRadius: 6,
              color: "#aab2c0",
              cursor: "pointer",
            }}
          >
            <IconDownload size={16} />
          </button>
        ) : null}
        <button
          type="button"
          data-testid="feature-popup-close"
          aria-label="Close"
          onClick={onClose}
          style={{
            flex: "0 0 auto",
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            width: 28,
            height: 28,
            // Generous touch target for mobile.
            padding: 0,
            background: "transparent",
            border: "none",
            borderRadius: 6,
            color: "#aab2c0",
            cursor: "pointer",
          }}
        >
          <IconClose size={16} />
        </button>
      </div>

      {/* Attribute list — compact key/value rows. */}
      {data.attributes.length > 0 ? (
        <div
          data-testid="feature-popup-attributes"
          style={{ padding: "8px 12px 12px" }}
        >
          {data.attributes.map((attr, i) => (
            <div
              key={`${attr.label}-${i}`}
              style={{
                display: "flex",
                justifyContent: "space-between",
                gap: 12,
                padding: "3px 0",
                fontSize: 12,
                lineHeight: 1.35,
              }}
            >
              <span
                style={{
                  color: "#8b93a3",
                  flex: "0 0 auto",
                  maxWidth: "45%",
                  wordBreak: "break-word",
                }}
              >
                {attr.label}
              </span>
              <span
                style={{
                  color: "#e8eaee",
                  textAlign: "right",
                  wordBreak: "break-word",
                  minWidth: 0,
                }}
              >
                {attr.value}
              </span>
            </div>
          ))}
          {/* Click-to-enrich: a small "loading details..." row while the
              footprint's full tag bag is fetched. Footprint-only; absent for
              every other popup. */}
          {data.enriching ? (
            <div
              data-testid="feature-popup-enriching"
              style={{
                padding: "5px 0 0",
                fontSize: 11,
                color: "#8b93a3",
                fontStyle: "italic",
              }}
            >
              Loading details...
            </div>
          ) : data.enrichFailed ? (
            // FOOTPRINT ENRICH TERMINAL STATE (NATE 2026-06-28): the detail
            // fetch failed/timed out (the agent box is asleep). Show an honest
            // one-line message instead of silently dropping the loading row.
            <div
              data-testid="feature-popup-enrich-failed"
              style={{
                padding: "5px 0 0",
                fontSize: 11,
                color: "#b8893a",
                fontStyle: "italic",
              }}
            >
              {enrichFailedCopy()}
            </div>
          ) : null}
        </div>
      ) : data.enriching ? (
        // No slim attributes yet, but a footprint enrich is in flight -- show the
        // loading affordance instead of the "No additional attributes" empty.
        <div
          data-testid="feature-popup-enriching"
          style={{
            padding: "8px 12px 12px",
            fontSize: 11,
            color: "#8b93a3",
            fontStyle: "italic",
          }}
        >
          Loading details...
        </div>
      ) : data.enrichFailed ? (
        // FOOTPRINT ENRICH TERMINAL STATE (NATE 2026-06-28): no slim attributes
        // AND the enrich fetch failed/timed out -- the honest terminal message
        // instead of a bare "No additional attributes." card.
        <div
          data-testid="feature-popup-enrich-failed"
          style={{
            padding: "8px 12px 12px",
            fontSize: 11,
            color: "#b8893a",
            fontStyle: "italic",
          }}
        >
          {enrichFailedCopy()}
        </div>
      ) : (
        <div
          data-testid="feature-popup-empty"
          style={{ padding: "8px 12px 12px", fontSize: 12, color: "#8b93a3" }}
        >
          No additional attributes.
        </div>
      )}
    </div>
  );
}
