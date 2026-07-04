// GRACE-2 web - AoiPickerCard (#170 manual-case onboarding, redesigned).
//
// A guided TWO-STEP onboarding for manually building a Case, replacing the old
// single popup that sat OVER the map and blocked the draw (NATE: "the popup
// covers the map so drawing feels impossible").
//
//   STEP 1 - NAME: a text input for the Case name. Next/continue -> step 2.
//   STEP 2 - AOI : prompt to set the analysis extent. Actions:
//       - "Draw AOI" -> the onboarding card DISMISSES (THE KEY FIX: the card is
//         fully hidden so the map is clear + interactive) and bbox-draw arms
//         (reuse lib/bbox_draw.ts). The user drags a rectangle on the clear map;
//         a SAVE / RETRY / CANCEL control then appears anchored at the bbox
//         BOTTOM-CENTER (the same projected-rect bottom-center anchoring the
//         SequenceScrubber / LayerLegend use, via projectBboxScreenRect).
//             SAVE   -> commit the bbox -> create the Case with (name, bbox).
//             RETRY  -> clear the rectangle, stay in draw mode to redraw.
//             CANCEL -> discard the bbox, return to the STEP 2 AOI prompt.
//       - "Skip"   -> create the Case with the name + NO bbox (current behavior).
//       - "Cancel" -> abort the whole onboarding (no Case created).
//
// GATING: while in DRAW mode the user cannot proceed (no create, no prompt)
// until they SAVE or CANCEL - there is no half-drawn limbo. Once an AOI is set
// (here, or by the agent on a turn) a stray map drag must NOT clobber it: draw
// only ever arms from the explicit Draw-AOI / Retry actions, never as a free
// free-draw over a committed extent.
//
// PSEUDO-LAYER: the IN-PROGRESS drawn rectangle paints through the bbox_draw
// BBOX_* pick layers (this card owns them). On SAVE the committed extent becomes
// the map's persistent analysis-extent overlay via the normal #170 data flow
// (createCase(name, bbox) -> CaseSummary.bbox -> the agent's zoom-to -> Map.tsx
// drawAnalysisExtent) - i.e. the AOI lives in the map's overlay system, not a
// floating popup. See the report note on this interpretation.
//
// REQUEST-FREE by construction: this card never touches the spatial-input bus /
// SpatialDrawSurface / any WS wire (there is no active turn when a Case is being
// created; the agent box may be asleep). It only reports up via onConfirm (the
// chosen [minLon,minLat,maxLon,maxLat] + name) / onSkip (name, no bbox) /
// onCancel; the case-command(create) it drives rides the durable sendOrQueue
// path in the parent.

import { useCallback, useEffect, useRef, useState } from "react";
import type { Map as MapLibreMap } from "maplibre-gl";
import { createPortal } from "react-dom";
import {
  attachBboxDrag,
  clearPickLayers,
  drawPickBbox,
  ensurePickLayers,
  projectBboxScreenRect,
  type BBox,
  type BboxScreenRect,
} from "../lib/bbox_draw";
import { IconBbox, IconCheck, IconClose, IconRefresh } from "./icons";

const ACCENT = "#3b82f6";

/** The onboarding step the card is on. "draw" hides the card so the map is clear. */
type Step = "name" | "aoi" | "draw";

export interface AoiPickerCardProps {
  /** The live MapLibre instance (Map.tsx's `map.current`). May be absent in
   *  headless tests / before the map is ready - the name step still works and
   *  Skip still creates a no-bbox Case. */
  map?: MapLibreMap | null;
  /** Confirm: create the Case WITH the chosen name + AOI bbox. */
  onConfirm: (bbox: BBox, name: string) => void;
  /** Skip the AOI step - create the Case with the name + NO bbox (current behavior). */
  onSkip: (name: string) => void;
  /** Dismiss the whole onboarding without creating a Case. */
  onCancel: () => void;
}

export function AoiPickerCard({
  map,
  onConfirm,
  onSkip,
  onCancel,
}: AoiPickerCardProps): JSX.Element {
  const [step, setStep] = useState<Step>("name");
  const [name, setName] = useState<string>("");
  // The currently-drawn (un-committed) bbox in draw mode. null until the user
  // has dragged a rectangle. SAVE is gated on this being non-null.
  const [drawnBbox, setDrawnBbox] = useState<BBox | null>(null);
  // The projected on-screen rect of the drawn bbox, tracked so the SAVE/RETRY/
  // CANCEL control pins to its bottom-center and follows pan/zoom.
  const [drawnRect, setDrawnRect] = useState<BboxScreenRect | null>(null);

  // Keep the live map in a ref so the draw effect can read the current instance
  // without re-arming the gesture on every render.
  const mapRef = useRef<MapLibreMap | null>(map ?? null);
  mapRef.current = map ?? null;

  const trimmedName = name.trim();

  // --- DRAW mode: arm the drag-rectangle gesture only while step === "draw".
  // NO-CLOBBER: the gesture is armed ONLY in draw mode, which is entered solely
  // via the explicit "Draw AOI" / RETRY actions - never as an ambient free-draw.
  // So a committed AOI (or any plain map drag once we leave draw mode) is never
  // clobbered by a stray drag.
  useEffect(() => {
    const m = map;
    if (!m || step !== "draw") return undefined;
    ensurePickLayers(m);
    // Re-paint any already-drawn rectangle when (re)entering draw mode.
    if (drawnBbox) drawPickBbox(m, drawnBbox);
    const detach = attachBboxDrag(m, {
      onProgress: (b) => drawPickBbox(m, b),
      onComplete: (b) => {
        setDrawnBbox(b);
        drawPickBbox(m, b);
      },
    });
    return () => {
      detach();
      clearPickLayers(m);
    };
    // drawnBbox intentionally omitted: re-arming on every drag would detach the
    // in-flight gesture. The initial-repaint above only matters on RE-ENTRY.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, step]);

  // --- Track the drawn bbox's projected on-screen rect so the SAVE/RETRY/CANCEL
  // control pins to its bottom-center and follows the camera (same pattern as
  // the legend/scrubber). Cleared when there is no drawn bbox.
  useEffect(() => {
    const m = map;
    if (!m || step !== "draw" || !drawnBbox) {
      setDrawnRect(null);
      return undefined;
    }
    let rafId: number | null = null;
    let disposed = false;
    const recompute = (): void => {
      rafId = null;
      if (disposed) return;
      setDrawnRect(projectBboxScreenRect(m, drawnBbox));
    };
    const schedule = (): void => {
      if (rafId != null) return;
      if (typeof requestAnimationFrame === "function") {
        rafId = requestAnimationFrame(recompute);
      } else {
        recompute();
      }
    };
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
        /* map torn down */
      }
    };
  }, [map, step, drawnBbox]);

  // --- Step transitions ---------------------------------------------------- //

  const goToAoi = useCallback(() => setStep("aoi"), []);
  const backToName = useCallback(() => setStep("name"), []);

  // STEP 2 -> draw: dismiss the card (step "draw" renders no card) so the map is
  // clear, then the draw effect arms the gesture.
  const startDraw = useCallback(() => {
    setDrawnBbox(null);
    setStep("draw");
  }, []);

  // DRAW: RETRY - clear the current rectangle, stay in draw mode to redraw.
  const retryDraw = useCallback(() => {
    const m = mapRef.current;
    if (m) {
      // Repaint an empty rectangle (clear) so the stale box doesn't linger.
      ensurePickLayers(m);
      drawPickBbox(m, [0, 0, 0, 0]);
    }
    setDrawnBbox(null);
    setDrawnRect(null);
  }, []);

  // DRAW: CANCEL - discard the drawn bbox, return to the STEP 2 AOI prompt.
  const cancelDraw = useCallback(() => {
    setDrawnBbox(null);
    setDrawnRect(null);
    setStep("aoi");
  }, []);

  // DRAW: SAVE - commit the drawn bbox -> create with (name, bbox). Gated on a
  // drawn bbox existing (no half-drawn limbo).
  const saveDraw = useCallback(() => {
    if (!drawnBbox) return;
    onConfirm(drawnBbox, trimmedName);
  }, [drawnBbox, onConfirm, trimmedName]);

  const skip = useCallback(() => onSkip(trimmedName), [onSkip, trimmedName]);

  // --- DRAW mode: the card is DISMISSED; only the bbox-anchored SAVE/RETRY/
  // CANCEL control renders (over the otherwise-clear, interactive map). ------ //
  if (step === "draw") {
    return (
      <DrawControls
        rect={drawnRect}
        canSave={drawnBbox !== null}
        onSave={saveDraw}
        onRetry={retryDraw}
        onCancel={cancelDraw}
      />
    );
  }

  // --- STEPS 1 + 2: the onboarding card. ----------------------------------- //
  return (
    <div data-testid="aoi-picker-card" style={cardStyle}>
      {step === "name" ? (
        <NameStep
          name={name}
          onChange={setName}
          onNext={goToAoi}
          onCancel={onCancel}
        />
      ) : (
        <AoiStep
          name={trimmedName}
          onDraw={startDraw}
          onSkip={skip}
          onBack={backToName}
          onCancel={onCancel}
        />
      )}
    </div>
  );
}

// --- STEP 1: NAME --------------------------------------------------------- //

function NameStep({
  name,
  onChange,
  onNext,
  onCancel,
}: {
  name: string;
  onChange: (v: string) => void;
  onNext: () => void;
  onCancel: () => void;
}): JSX.Element {
  // A name is optional (server defaults to "Untitled Case"), so Next is always
  // enabled; Enter advances for fast keyboard flow.
  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>): void => {
    if (e.key === "Enter") {
      e.preventDefault();
      onNext();
    }
  };
  return (
    <div data-testid="aoi-step-name" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={headerRow}>
        <IconBbox size={16} color={ACCENT} />
        <span style={titleStyle}>Name your case</span>
        <span style={stepBadge}>1 / 2</span>
      </div>
      <p style={subtleText}>Give this case a short, descriptive name. You can change it later.</p>
      <input
        type="text"
        data-testid="aoi-name-input"
        aria-label="Case name"
        autoFocus
        value={name}
        placeholder="e.g. Mexico Beach surge"
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKeyDown}
        style={textInputStyle}
      />
      <div style={actionsRow}>
        <button type="button" data-testid="aoi-cancel" onClick={onCancel} style={ghostBtnStyle}>
          <IconClose size={13} /> Cancel
        </button>
        <button type="button" data-testid="aoi-name-next" onClick={onNext} style={primaryBtnStyle(true)}>
          Next <IconCheck size={13} />
        </button>
      </div>
    </div>
  );
}

// --- STEP 2: AOI prompt --------------------------------------------------- //

function AoiStep({
  name,
  onDraw,
  onSkip,
  onBack,
  onCancel,
}: {
  name: string;
  onDraw: () => void;
  onSkip: () => void;
  onBack: () => void;
  onCancel: () => void;
}): JSX.Element {
  const shownName = name.length > 0 ? name : "Untitled Case";
  return (
    <div data-testid="aoi-step-aoi" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={headerRow}>
        <IconBbox size={16} color={ACCENT} />
        <span style={titleStyle}>Set the area of interest</span>
        <span style={stepBadge}>2 / 2</span>
      </div>
      <p style={subtleText}>
        <span style={{ color: "#e5e7eb", fontWeight: 600 }}>{shownName}</span> - draw the analysis
        extent on the map, or skip and let the agent pick the extent from your first prompt.
      </p>
      <button type="button" data-testid="aoi-draw" onClick={onDraw} style={primaryBtnStyle(true)}>
        <IconBbox size={14} /> Draw AOI on map
      </button>
      <div style={actionsRow}>
        <button type="button" data-testid="aoi-back" onClick={onBack} style={ghostBtnStyle}>
          Back
        </button>
        <button type="button" data-testid="aoi-skip" onClick={onSkip} style={ghostBtnStyle}>
          Skip
        </button>
        <button type="button" data-testid="aoi-cancel" onClick={onCancel} style={ghostBtnStyle}>
          <IconClose size={13} /> Cancel
        </button>
      </div>
    </div>
  );
}

// --- DRAW mode controls (bbox bottom-center anchored) --------------------- //
//
// Anchored at the BOTTOM-CENTER of the drawn bbox's projected on-screen rect
// (same pattern as SequenceScrubber: cx = (left+right)/2, top = rect.bottom +
// gap, translateX(-50%)). Falls back to a fixed bottom-center band before any
// rectangle exists / while the bbox can't be projected, so the hint+Cancel are
// always reachable. Portaled to document.body so `fixed` positioning resolves
// against the viewport (not the map container's stacking context).

const DRAW_CONTROL_GAP_PX = 14;

function DrawControls({
  rect,
  canSave,
  onSave,
  onRetry,
  onCancel,
}: {
  rect: BboxScreenRect | null;
  canSave: boolean;
  onSave: () => void;
  onRetry: () => void;
  onCancel: () => void;
}): JSX.Element {
  let posStyle: React.CSSProperties;
  if (rect) {
    const cx = (rect.left + rect.right) / 2;
    posStyle = {
      position: "fixed",
      left: cx,
      top: rect.bottom + DRAW_CONTROL_GAP_PX,
      transform: "translateX(-50%)",
      transformOrigin: "top center",
    };
  } else {
    posStyle = {
      position: "fixed",
      left: "50%",
      bottom: 96,
      transform: "translateX(-50%)",
      transformOrigin: "bottom center",
    };
  }

  const body = (
    <div data-testid="aoi-draw-controls" style={{ ...posStyle, ...drawControlsShellStyle }}>
      {!canSave && (
        <span data-testid="aoi-draw-hint" style={drawHintStyle}>
          <IconBbox size={13} color={ACCENT} /> Drag a rectangle on the map
        </span>
      )}
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <button
          type="button"
          data-testid="aoi-save"
          onClick={onSave}
          disabled={!canSave}
          title={canSave ? undefined : "Draw a rectangle first"}
          style={primaryBtnStyle(canSave)}
        >
          <IconCheck size={13} /> Save
        </button>
        <button
          type="button"
          data-testid="aoi-retry"
          onClick={onRetry}
          disabled={!canSave}
          title={canSave ? undefined : "Nothing drawn yet"}
          style={ghostBtnStyle}
        >
          <IconRefresh size={13} /> Retry
        </button>
        <button type="button" data-testid="aoi-draw-cancel" onClick={onCancel} style={ghostBtnStyle}>
          <IconClose size={13} /> Cancel
        </button>
      </div>
    </div>
  );

  // Portal to body so fixed positioning resolves against the viewport (the map
  // container is position:fixed but the parent overlay layer may be transformed).
  if (typeof document !== "undefined" && document.body) {
    return createPortal(body, document.body);
  }
  return body;
}

// --- Shared styles -------------------------------------------------------- //

const headerRow: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
};

const titleStyle: React.CSSProperties = { fontWeight: 600, fontSize: 14, flex: 1 };

const stepBadge: React.CSSProperties = {
  fontSize: 10,
  fontWeight: 700,
  color: "#94a3b8",
  background: "rgba(255,255,255,0.06)",
  border: "1px solid rgba(255,255,255,0.1)",
  borderRadius: 6,
  padding: "2px 6px",
  letterSpacing: 0.5,
};

const subtleText: React.CSSProperties = { margin: 0, fontSize: 12, color: "#cbd5e1", lineHeight: 1.45 };

const textInputStyle: React.CSSProperties = {
  background: "rgba(0,0,0,0.35)",
  border: "1px solid rgba(255,255,255,0.14)",
  borderRadius: 7,
  color: "#e5e7eb",
  padding: "9px 11px",
  fontSize: 13,
  width: "100%",
  boxSizing: "border-box",
  fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
};

const actionsRow: React.CSSProperties = {
  display: "flex",
  gap: 8,
  justifyContent: "flex-end",
  marginTop: 2,
  flexWrap: "wrap",
};

const cardStyle: React.CSSProperties = {
  position: "absolute",
  top: 16,
  left: "50%",
  transform: "translateX(-50%)",
  width: 340,
  maxWidth: "92%",
  background: "rgba(20,20,26,0.97)",
  border: "1px solid rgba(255,255,255,0.1)",
  borderRadius: 12,
  boxShadow: "0 8px 28px rgba(0,0,0,0.5)",
  color: "#e5e7eb",
  padding: 16,
  display: "flex",
  flexDirection: "column",
  gap: 10,
  fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
  pointerEvents: "auto",
  zIndex: 7,
};

const drawControlsShellStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  gap: 8,
  padding: "10px 12px",
  background: "rgba(17,18,23,0.92)",
  backdropFilter: "blur(6px)",
  WebkitBackdropFilter: "blur(6px)",
  border: "1px solid rgba(255,255,255,0.1)",
  borderRadius: 12,
  boxShadow: "0 4px 18px rgba(0,0,0,0.5)",
  color: "#e5e7eb",
  fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
  pointerEvents: "auto",
  // Above the map + analysis-extent overlay; below the mobile chat sheet is not
  // a concern here (the composer is not shown during onboarding draw).
  zIndex: 8,
};

const drawHintStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  fontSize: 12,
  color: "#cbd5e1",
  background: "rgba(59,130,246,0.12)",
  border: "1px solid rgba(59,130,246,0.3)",
  borderRadius: 7,
  padding: "5px 9px",
  whiteSpace: "nowrap",
};

const ghostBtnStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 5,
  border: "1px solid rgba(255,255,255,0.14)",
  background: "transparent",
  color: "#cbd5e1",
  borderRadius: 8,
  padding: "8px 13px",
  fontSize: 12,
  fontWeight: 500,
  cursor: "pointer",
};

function primaryBtnStyle(enabled: boolean): React.CSSProperties {
  return {
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    gap: 6,
    border: "1px solid #3b82f6",
    background: enabled ? "#3b82f6" : "rgba(59,130,246,0.35)",
    color: enabled ? "#0b0b0e" : "rgba(255,255,255,0.55)",
    borderRadius: 8,
    padding: "9px 15px",
    fontSize: 13,
    fontWeight: 600,
    cursor: enabled ? "pointer" : "not-allowed",
  };
}
