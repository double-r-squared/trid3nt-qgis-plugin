// GRACE-2 web — RegionPickerCard (region-disambiguation flow; state-bbox-fallback
// narrowing). Modeled on CredentialCard.tsx — the proven in-chat interactive-card
// pattern.
//
// Inline chat card the user sees when a `geocode_location` result snapped to a
// WHOLE STATE bbox (a vague/regional query like "south Florida" with no precise
// OSM match). The agent emits a `region-choice-request` envelope (server ->
// client) carrying the whole-state bbox + the candidate sub-regions (default:
// counties) + an honest prompt; Chat.tsx subscribes to that envelope and renders
// one of these cards inline in the conversation scroll.
//
// The card surfaces:
//   1. An HONEST prompt — the agent says it snapped to the whole state and is
//      OFFERING a narrower pick (the fallback honesty floor).
//   2. A scrollable LIST of the candidate regions (counties) from the request
//      envelope. Selecting a region (clicking a list row OR tapping its polygon
//      on the map — both synced via the region-choice bus) sends a
//      `region-choice-provided` envelope with choice="region" +
//      selected_region_id + selected_bbox.
//   3. A default "Use whole state of <State>" button. Sends
//      `region-choice-provided` with choice="whole_state" (the honest
//      already-resolved default — this IS the decline path, Invariant 8).
//
// MAP SYNC (region-choice bus): hovering a list row highlights its polygon on
// the map and vice-versa; the bus carries hover + pre-reply selection so both
// surfaces stay in lockstep. The card REPORTS hover to the bus via
// onHoverRegion; the consumer (Chat.tsx) relays it to the bus. The card READS
// the bus-synced hovered/selected ids via the `hoveredRegionId` /
// `selectedRegionId` props so a MAP hover highlights the matching list row.
//
// Folds to a compact resolved state after answering (like CredentialCard): a
// one-line "Narrowed to <County>" / "Using whole state of <State>" summary with
// a chevron to re-expand the (read-only) detail.
//
// No raw glyphs / emoji — every icon comes from the shared icons module per the
// project UI policy.

import { useState } from "react";
import { RegionCandidate, RegionChoiceRequestPayload } from "../contracts";
import {
  IconGlobe,
  IconBbox,
  IconChevronDown,
  IconChevronRight,
} from "./icons";

// --- Props --------------------------------------------------------------- //

export interface RegionPickerCardProps {
  /** The originating region-choice-request envelope. */
  request: RegionChoiceRequestPayload;
  /**
   * Resolved state of this prompt. When set, the WHOLE card folds into a
   * compact one-line summary ("Narrowed to Lee County" / "Using whole state of
   * Florida") matching the PipelineCard terminal chrome — the prompt + list +
   * whole-state button all collapse away so a resolved prompt cannot be
   * re-submitted and the subsequent narration resumes after it. `null`/undefined
   * = still active (full picker shown).
   */
  resolved?: "region" | "whole_state" | null;
  /**
   * The `region_id` of the resolved candidate when `resolved === "region"`
   * (used to render the compact summary label). null for whole_state.
   */
  resolvedRegionId?: string | null;
  /**
   * Bus-synced hover id (the region hovered in EITHER the card list OR on the
   * map polygon). The matching list row highlights. null = none.
   */
  hoveredRegionId?: string | null;
  /**
   * Bus-synced pre-reply selection id (the region clicked/tapped in EITHER
   * surface, before the reply resolves the card). The matching list row reads
   * as chosen. null = none.
   */
  selectedRegionId?: string | null;
  /**
   * Hover-report callback. The card reports the hovered candidate id (or null
   * on mouse-leave); the consumer relays it to the region-choice bus so the
   * map polygon highlights in sync.
   */
  onHoverRegion: (regionId: string | null) => void;
  /**
   * Region-pick callback. The consumer emits `region-choice-provided`
   * (choice="region") echoing the request_id + the candidate's region_id +
   * bbox, then resolves the card.
   */
  onPickRegion: (candidate: RegionCandidate) => void;
  /**
   * Whole-state callback. The consumer emits `region-choice-provided`
   * (choice="whole_state") so the agent keeps the honest already-resolved
   * whole-state bbox.
   */
  onUseWholeState: () => void;
}

// --- Styles -------------------------------------------------------------- //
//
// Mirror the CredentialCard / InlineChatCard visual language (semi-transparent
// surface, soft shadow, accent on the left edge) so the region picker sits in
// the same inline-card family.

const ACCENT = "#3b82f6"; // blue — matches CredentialCard "info" variant

const RESOLVED_TINT = "rgba(40, 200, 100, 0.18)"; // PipelineCard "complete" green
const RESOLVED_TEXT = "#10b981";

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

function compactCardStyle(): React.CSSProperties {
  return {
    display: "flex",
    flexDirection: "column",
    alignItems: "stretch",
    gap: 6,
    fontSize: 12,
    lineHeight: 1.4,
    padding: "8px 10px",
    borderRadius: 6,
    background: RESOLVED_TINT,
    boxShadow: "0 1px 3px rgba(0,0,0,0.25)",
    color: "#e5e7eb",
    fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
    width: "100%",
    boxSizing: "border-box",
    transition: "background-color 200ms ease-in-out",
  };
}

const listStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 2,
  // Scrollable when many counties; capped so the card never dominates the
  // chat scroll.
  maxHeight: 200,
  overflowY: "auto",
  margin: 0,
  padding: 0,
  listStyle: "none",
  border: "1px solid rgba(255,255,255,0.08)",
  borderRadius: 6,
};

function rowStyle(active: boolean, selected: boolean): React.CSSProperties {
  return {
    display: "flex",
    alignItems: "center",
    gap: 6,
    width: "100%",
    textAlign: "left",
    border: "none",
    borderLeft: selected
      ? `3px solid ${ACCENT}`
      : active
      ? "3px solid rgba(59,130,246,0.5)"
      : "3px solid transparent",
    background: selected
      ? "rgba(59,130,246,0.22)"
      : active
      ? "rgba(59,130,246,0.12)"
      : "transparent",
    color: "#e5e7eb",
    fontSize: 12,
    fontFamily: "inherit",
    padding: "6px 8px",
    cursor: "pointer",
    transition: "background 0.1s ease, border-color 0.1s ease",
  };
}

function btnStyle(
  tone: "primary" | "muted",
): React.CSSProperties {
  const base: React.CSSProperties = {
    border: "1px solid transparent",
    borderRadius: 6,
    padding: "6px 12px",
    fontSize: 12,
    fontWeight: 600,
    cursor: "pointer",
    fontFamily: "inherit",
    lineHeight: 1.2,
    display: "inline-flex",
    alignItems: "center",
    gap: 5,
    transition: "background 0.12s ease, border-color 0.12s ease",
  };
  if (tone === "primary") {
    return { ...base, background: ACCENT, color: "#0b0b0e", borderColor: ACCENT };
  }
  return {
    ...base,
    background: "transparent",
    color: "#9ca3af",
    borderColor: "rgba(255,255,255,0.14)",
    fontWeight: 500,
  };
}

// --- Component ----------------------------------------------------------- //

export function RegionPickerCard({
  request,
  resolved,
  resolvedRegionId,
  hoveredRegionId,
  selectedRegionId,
  onHoverRegion,
  onPickRegion,
  onUseWholeState,
}: RegionPickerCardProps): JSX.Element {
  const [expanded, setExpanded] = useState<boolean>(false);
  const isResolved = resolved === "region" || resolved === "whole_state";

  // --- Folded (resolved) compact card ------------------------------------ //
  if (isResolved) {
    const narrowed = resolved === "region";
    const pickedName = narrowed
      ? request.candidates.find((c) => c.region_id === resolvedRegionId)?.name
      : undefined;
    const summary = narrowed
      ? `Narrowed to ${pickedName ?? "selected region"}`
      : `Using whole state of ${request.state_name}`;
    return (
      <div
        data-testid={`region-picker-card-${request.request_id}`}
        data-state={request.state_code}
        data-resolved={resolved}
        data-variant="compact"
        role="region"
        aria-label={summary}
        style={compactCardStyle()}
      >
        <div
          data-testid={`region-picker-resolved-${request.request_id}`}
          style={{ display: "flex", alignItems: "center", gap: 8, width: "100%" }}
        >
          <span
            aria-hidden="true"
            style={{ display: "inline-flex", alignItems: "center", flexShrink: 0 }}
          >
            {narrowed ? (
              <IconBbox size={13} color={RESOLVED_TEXT} />
            ) : (
              <IconGlobe size={13} color={RESOLVED_TEXT} />
            )}
          </span>
          <span
            style={{
              flex: 1,
              color: RESOLVED_TEXT,
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
            data-testid={`region-picker-expand-${request.request_id}`}
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
            data-testid={`region-picker-detail-${request.request_id}`}
            style={{
              width: "100%",
              marginTop: 6,
              paddingTop: 6,
              borderTop: "1px solid rgba(255,255,255,0.08)",
              color: "#d1d5db",
              fontSize: 11,
              lineHeight: 1.5,
              display: "flex",
              flexDirection: "column",
              gap: 4,
            }}
          >
            <div style={{ wordBreak: "break-word" }}>{request.message}</div>
          </div>
        )}
      </div>
    );
  }

  // --- Active (pending) full picker -------------------------------------- //
  return (
    <div
      data-testid={`region-picker-card-${request.request_id}`}
      data-state={request.state_code}
      role="region"
      aria-label={`Pick an area in ${request.state_name}`}
      style={cardStyle}
    >
      {/* Header row: globe icon + title */}
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
          <IconGlobe size={14} color={ACCENT} />
        </span>
        <strong
          data-testid={`region-picker-title-${request.request_id}`}
          style={{
            fontSize: 13,
            fontWeight: 600,
            color: "#f3f4f6",
            flex: 1,
            wordBreak: "break-word",
          }}
        >
          Pick an area in {request.state_name}
        </strong>
      </div>

      {/* Agent's honest user-facing prompt (snapped to whole state, offering a
          narrower pick — the fallback honesty floor). */}
      <div
        data-testid={`region-picker-message-${request.request_id}`}
        style={{
          color: "#d1d5db",
          fontSize: 12,
          lineHeight: 1.5,
          wordBreak: "break-word",
        }}
      >
        {request.message}
      </div>

      {/* Candidate region list — scrollable. Each row syncs hover + click with
          the map choropleth via the region-choice bus. Hidden when the
          region-set build produced no candidates (honest degrade: only the
          whole-state default remains). */}
      {request.candidates.length > 0 ? (
        <ul
          data-testid={`region-picker-list-${request.request_id}`}
          style={listStyle}
        >
          {request.candidates.map((c) => {
            const active = hoveredRegionId === c.region_id;
            const selected = selectedRegionId === c.region_id;
            return (
              <li key={c.region_id} style={{ margin: 0 }}>
                <button
                  type="button"
                  data-testid={`region-picker-row-${request.request_id}-${c.region_id}`}
                  data-region-id={c.region_id}
                  data-active={active ? "true" : "false"}
                  data-selected={selected ? "true" : "false"}
                  aria-label={`Use ${c.name}`}
                  onMouseEnter={() => onHoverRegion(c.region_id)}
                  onMouseLeave={() => onHoverRegion(null)}
                  onFocus={() => onHoverRegion(c.region_id)}
                  onBlur={() => onHoverRegion(null)}
                  onClick={() => onPickRegion(c)}
                  style={rowStyle(active, selected)}
                >
                  <IconBbox
                    size={12}
                    color={selected || active ? ACCENT : "#9ca3af"}
                  />
                  <span style={{ flex: 1, wordBreak: "break-word" }}>{c.name}</span>
                </button>
              </li>
            );
          })}
        </ul>
      ) : (
        <div
          data-testid={`region-picker-no-candidates-${request.request_id}`}
          style={{ color: "#9ca3af", fontSize: 11, fontStyle: "italic" }}
        >
          No sub-regions available — use the whole state below.
        </div>
      )}

      {/* Default action: use the whole state (the honest already-resolved
          fallback). Primary button so it reads as the safe default. */}
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 2 }}>
        <button
          type="button"
          data-testid={`region-picker-whole-state-${request.request_id}`}
          aria-label={`Use the whole state of ${request.state_name}`}
          onClick={onUseWholeState}
          style={btnStyle("muted")}
        >
          <IconGlobe size={12} color="#9ca3af" />
          Use whole state of {request.state_name}
        </button>
      </div>
    </div>
  );
}
