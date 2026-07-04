// GRACE-2 web — SpatialInputCard (FR-WC-13 pick-mode + FR-WC-16 urban
// vector-draw). The IN-CHAT prompt card for a paused `spatial-input-request`.
//
// Modeled on RegionPickerCard.tsx — the proven inline interactive-card pattern.
// When the agent pauses the turn to ask the user to pick a point / bbox or DRAW
// geometry (AOIs + tagged barrier walls / flap gates for the SWMM urban-flood
// engine), Chat.tsx renders one of these cards inline in the conversation
// scroll. The ACTUAL pick / draw happens on the MAP (SpatialDrawSurface); this
// card is the honest in-chat prompt + a folded "answered" summary after the user
// submits / cancels (so the narration that resumes flows AFTER it).
//
// The card carries no draw logic — the on-map terra-draw surface owns that. The
// card just tells the user where to act and reflects the resolution. A "Cancel"
// affordance on the card mirrors the on-map Cancel so the user can dismiss the
// request from either surface (Invariant 8 — cancellation is first-class).
//
// No raw glyphs / emoji — every icon comes from the shared icons module.

import { useState } from "react";
import type { SpatialInputRequestPayload } from "../contracts";
import {
  IconBbox,
  IconLine,
  IconMapPin,
  IconPolygon,
  IconCheck,
  IconClose,
  IconChevronDown,
  IconChevronRight,
} from "./icons";

export type SpatialInputResolution = "submitted" | "cancelled";

export interface SpatialInputCardProps {
  /** The originating spatial-input-request envelope. */
  request: SpatialInputRequestPayload;
  /**
   * Resolved state. When set, the card folds into a compact one-line summary
   * ("Geometry submitted" / "Pick cancelled") so a resolved prompt cannot be
   * re-answered and the subsequent narration resumes after it. null/undefined =
   * still active (the prompt + on-map hint shown).
   */
  resolved?: SpatialInputResolution | null;
  /** Cancel the request from the chat card (mirrors the on-map Cancel). */
  onCancel: () => void;
}

const ACCENT = "#3b82f6";
const RESOLVED_TINT = "rgba(40, 200, 100, 0.18)";
const CANCELLED_TINT = "rgba(120, 120, 130, 0.18)";
const RESOLVED_TEXT = "#10b981";
const CANCELLED_TEXT = "#9ca3af";

const cardStyle: React.CSSProperties = {
  background: "rgba(28,28,34,0.92)",
  border: "1px solid rgba(255,255,255,0.07)",
  borderLeft: `3px solid ${ACCENT}`,
  borderRadius: 8,
  boxShadow: "0 4px 14px rgba(0,0,0,0.35)",
  color: "#e5e7eb",
  padding: "10px 12px",
  display: "flex",
  flexDirection: "column",
  gap: 8,
  fontSize: 12,
  lineHeight: 1.45,
  fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
  width: "100%",
  boxSizing: "border-box",
};

function compactCardStyle(cancelled: boolean): React.CSSProperties {
  return {
    display: "flex",
    flexDirection: "column",
    alignItems: "stretch",
    gap: 6,
    fontSize: 12,
    lineHeight: 1.4,
    padding: "8px 10px",
    borderRadius: 6,
    background: cancelled ? CANCELLED_TINT : RESOLVED_TINT,
    boxShadow: "0 1px 3px rgba(0,0,0,0.25)",
    color: "#e5e7eb",
    fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
    width: "100%",
    boxSizing: "border-box",
    transition: "background-color 200ms ease-in-out",
  };
}

function modeIcon(mode: string, color: string, isLine = false): JSX.Element {
  if (mode === "point") return <IconMapPin size={13} color={color} />;
  if (mode === "bbox") return <IconBbox size={13} color={color} />;
  // vector_draw: a neutral elevation/section line (purpose="line") gets the
  // line glyph; the default barrier/AOI draw keeps the polygon glyph.
  if (isLine) return <IconLine size={13} color={color} />;
  return <IconPolygon size={13} color={color} />;
}

function modeLabel(mode: string, isLine = false): string {
  if (mode === "point") return "Click a point on the map";
  if (mode === "bbox") return "Drag a box on the map";
  // vector_draw: neutral-line requests ask for one plain line.
  if (isLine) return "Draw a line on the map";
  return "Draw on the map (rectangle / line / polygon)";
}

export function SpatialInputCard({
  request,
  resolved,
  onCancel,
}: SpatialInputCardProps): JSX.Element {
  const [expanded, setExpanded] = useState<boolean>(false);
  const isResolved = resolved === "submitted" || resolved === "cancelled";
  // NEUTRAL-LINE request (purpose="line"): a plain elevation/section line draw,
  // no barrier tagging. Affects only the icon + hint copy.
  const isLine = request.mode === "vector_draw" && request.purpose === "line";

  // --- Folded (resolved) compact card ------------------------------------ //
  if (isResolved) {
    const cancelled = resolved === "cancelled";
    const text = cancelled ? CANCELLED_TEXT : RESOLVED_TEXT;
    const summary = cancelled ? "Pick cancelled" : "Geometry submitted";
    return (
      <div
        data-testid={`spatial-input-card-${request.request_id}`}
        data-mode={request.mode}
        data-resolved={resolved}
        data-variant="compact"
        role="region"
        aria-label={summary}
        style={compactCardStyle(cancelled)}
      >
        <div
          data-testid={`spatial-input-resolved-${request.request_id}`}
          style={{ display: "flex", alignItems: "center", gap: 8, width: "100%" }}
        >
          <span
            aria-hidden="true"
            style={{ display: "inline-flex", alignItems: "center", flexShrink: 0 }}
          >
            {cancelled ? (
              <IconClose size={13} color={text} />
            ) : (
              <IconCheck size={13} color={text} />
            )}
          </span>
          <span
            style={{
              flex: 1,
              color: text,
              fontWeight: 600,
              fontSize: 12,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
            title={summary}
          >
            {summary}
          </span>
          <button
            type="button"
            data-testid={`spatial-input-expand-${request.request_id}`}
            aria-label={expanded ? "Collapse details" : "Show details"}
            aria-expanded={expanded}
            onClick={() => setExpanded((v) => !v)}
            style={{
              background: "transparent",
              border: "none",
              padding: 2,
              margin: 0,
              cursor: "pointer",
              display: "inline-flex",
              alignItems: "center",
              color: "#9ca3af",
              flexShrink: 0,
            }}
          >
            {expanded ? (
              <IconChevronDown size={13} color="#9ca3af" />
            ) : (
              <IconChevronRight size={13} color="#9ca3af" />
            )}
          </button>
        </div>
        {expanded && (
          <div
            data-testid={`spatial-input-detail-${request.request_id}`}
            style={{
              width: "100%",
              marginTop: 6,
              paddingTop: 6,
              borderTop: "1px solid rgba(255,255,255,0.08)",
              color: "#d1d5db",
              fontSize: 11,
              lineHeight: 1.5,
            }}
          >
            <div style={{ wordBreak: "break-word" }}>{request.description}</div>
          </div>
        )}
      </div>
    );
  }

  // --- Active (pending) prompt ------------------------------------------- //
  return (
    <div
      data-testid={`spatial-input-card-${request.request_id}`}
      data-mode={request.mode}
      role="region"
      aria-label={request.title}
      style={cardStyle}
    >
      {/* Header row: mode icon + title */}
      <div style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
        <span
          aria-hidden="true"
          style={{
            color: ACCENT,
            flexShrink: 0,
            marginTop: 1,
            display: "inline-flex",
            alignItems: "center",
          }}
        >
          {modeIcon(request.mode, ACCENT, isLine)}
        </span>
        <strong
          data-testid={`spatial-input-title-${request.request_id}`}
          style={{
            fontSize: 13,
            fontWeight: 600,
            color: "#f3f4f6",
            flex: 1,
            wordBreak: "break-word",
          }}
        >
          {request.title}
        </strong>
      </div>

      {/* Agent's prompt + the on-map action hint. */}
      <div
        data-testid={`spatial-input-message-${request.request_id}`}
        style={{
          color: "#d1d5db",
          fontSize: 12,
          lineHeight: 1.5,
          wordBreak: "break-word",
        }}
      >
        {request.description}
      </div>
      <div
        data-testid={`spatial-input-hint-${request.request_id}`}
        style={{
          color: "#93c5fd",
          fontSize: 11,
          fontStyle: "italic",
          display: "flex",
          alignItems: "center",
          gap: 6,
        }}
      >
        {modeIcon(request.mode, "#93c5fd", isLine)}
        {modeLabel(request.mode, isLine)}, then press Submit on the map.
      </div>

      {/* Cancel affordance (mirrors the on-map Cancel; Invariant 8). */}
      <div style={{ display: "flex", gap: 6, marginTop: 2 }}>
        <button
          type="button"
          data-testid={`spatial-input-cancel-${request.request_id}`}
          aria-label="Cancel the request"
          onClick={onCancel}
          style={{
            border: "1px solid rgba(255,255,255,0.14)",
            borderRadius: 6,
            padding: "6px 12px",
            fontSize: 12,
            fontWeight: 500,
            cursor: "pointer",
            fontFamily: "inherit",
            background: "transparent",
            color: "#9ca3af",
            display: "inline-flex",
            alignItems: "center",
            gap: 5,
          }}
        >
          <IconClose size={12} color="#9ca3af" />
          Cancel
        </button>
      </div>
    </div>
  );
}
