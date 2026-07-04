// GRACE-2 web - ResolutionPickerCard (#154 pre-run granularity gate, sprint-16).
//
// The IN-CHAT confirmation card the user sees when the agent pauses a heavy
// solver run (SWMM / SFINCS) to confirm the mesh GRANULARITY before the burn.
// It is an OPTIONAL enrichment on a `tool-payload-warning` envelope: when the
// envelope carries a `granularity` (GranularitySuggestion), Chat.tsx renders
// THIS card instead of the generic PayloadWarningInline. When `granularity` is
// absent the existing card renders unchanged (full back-compat).
//
// COMBINED RUN-SETTINGS GATE (sprint-16): when the envelope ALSO carries a
// `time_scale` (TimeScaleSuggestion), this card grows a SECOND section so the
// user reviews + overrides BOTH the spatial resolution AND the temporal
// cadence/window in ONE card, ONE interaction. The time-scale section shows the
// agent's suggested minutes-per-frame + simulation window as EDITABLE numeric
// fields and a LIVE frame-count estimate that recomputes as the user types
// (frames ~= duration_hr*60 / interval, clamped to [1, max_frames]). On confirm
// the resolution override AND the time-scale override go back in the SAME
// `revised_args` dict (decision="narrow_scope"); when nothing changed it sends
// "proceed". The granularity-only path is UNCHANGED when `time_scale` is absent
// (pluvial flood = hourly cadence, no time-scale row) - full back-compat.
//
// LAYOUT (built on the InlineChatCard primitive - same chrome as the
// payload-warning / source-suggestion cards):
//   - Title: "Confirm mesh resolution"
//   - Metadata row: suggested resolution (m), estimated active cells, estimated
//     solve time, vCPUs, compute class, Spot label (omitted when null).
//   - Caption: the agent's `reason`, prefixed "Coarsened -" when the suggestion
//     coarsened the user's request (Invariant 1 - honest about the adjustment).
//   - OVERRIDE control: a CHOICE-CHIP row over `resolution_choices` (NOT a
//     slider - discrete published rungs). Picking a rung LIVE-recomputes the
//     displayed cells + ETA client-side from the chosen rung, by area-invariant
//     scaling off the suggested rung's authoritative numbers:
//         cells ~= round(estimated_active_cells * (suggested/chosen)^2)
//         eta   ~= estimated_solve_seconds * (cells_chosen / cells_suggested)
//     (finer rung -> more cells -> longer; coarser -> fewer -> shorter). These
//     are labelled ESTIMATES - the authoritative numbers come from the agent's
//     suggestion; the client never claims its recompute is exact.
//   - Actions: Confirm (primary) + Cancel (muted, RIGHTMOST per the project
//     button-order convention).
//       * chosen rung == suggested  -> decision "proceed",       revised null
//       * chosen rung != suggested  -> decision "narrow_scope",  revised
//                                       { [resolution_param]: chosen }
//       * Cancel                    -> decision "cancel",        revised null
//
// LOCK + FOLD: once the user decides, the card locks (no re-answer) and folds to
// a compact one-line summary - same active->resolved pattern as SpatialInputCard
// / SandboxCard / PayloadWarningInline. `resolved` (externally recorded in the
// per-Case stream) seeds the decided state so the fold survives a remount
// (Case switch + return).
//
// CONFIRM WIRING: the decision rides back on the EXISTING
// `tool-payload-confirmation` envelope via the SAME onDecide signature the
// PayloadWarningInline uses - Chat.tsx wires it to handlePayloadDecide ->
// GraceWs.sendPayloadConfirmation(warning_id, decision, revised). No new WS
// type, no new StreamState field, no new route helper.
//
// Invariant 9 (no cost theater): cells / seconds / vCPUs / Spot label are
// capacity + capability descriptors, NOT dollar figures. No dollar field.
//
// No raw glyphs / emoji - every icon comes from the shared icons module.

import { useState } from "react";
import {
  GranularitySuggestion,
  PayloadConfirmationDecision,
  PayloadWarningEnvelopePayload,
  TimeScaleSuggestion,
} from "../contracts";
import { InlineChatCard, InlineChatCardAction } from "./InlineChatCard";
import {
  formatCellCount,
  formatEta,
} from "./PipelineCard";
import { IconGrid, IconCheck, IconChevronDown, IconChevronRight } from "./icons";

const ACCENT = "#eab308"; // amber - same family as the payload-warning card

// Resolved (answered) fold tint - amber, matching the payload-warning fold so
// the two confirm-gate cards read as the same lineage.
const compactStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  alignItems: "stretch",
  gap: 6,
  fontSize: 12,
  lineHeight: 1.4,
  padding: "8px 10px",
  borderRadius: 6,
  background: "rgba(234,179,8,0.18)",
  boxShadow: "0 1px 3px rgba(0,0,0,0.25)",
  color: "#e5e7eb",
  fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
  width: "100%",
  boxSizing: "border-box",
};

const RESOLVED_SUMMARY: Record<PayloadConfirmationDecision, string> = {
  proceed: "Mesh resolution confirmed",
  narrow_scope: "Mesh resolution overridden",
  cancel: "Mesh-resolution gate cancelled",
};

// Combined run-settings card (granularity + time_scale) uses run-settings
// language in the folded summary so the two-lever card reads honestly.
const RUN_SETTINGS_RESOLVED_SUMMARY: Record<
  PayloadConfirmationDecision,
  string
> = {
  proceed: "Run settings confirmed",
  narrow_scope: "Run settings overridden",
  cancel: "Run-settings gate cancelled",
};

// --- Client-side area-invariant recompute -------------------------------- //
//
// Cell count scales with the inverse square of cell edge length: halving the
// resolution (finer) quadruples the cells over the same area. ETA scales with
// the cell ratio (more cells -> proportionally longer). Both are ESTIMATES off
// the agent's authoritative suggested-rung numbers.

/** Estimated active cells for `chosen` from the suggested-rung baseline. */
export function estimateCellsForResolution(
  g: GranularitySuggestion,
  chosen: number,
): number {
  if (chosen <= 0 || g.suggested_resolution_m <= 0) {
    return g.estimated_active_cells;
  }
  const ratio = g.suggested_resolution_m / chosen;
  return Math.round(g.estimated_active_cells * ratio * ratio);
}

/** Estimated solve seconds for `chosen`, scaled by the cell ratio. */
export function estimateSolveSecondsForResolution(
  g: GranularitySuggestion,
  chosen: number,
): number {
  const baseCells = g.estimated_active_cells;
  if (baseCells <= 0) return g.estimated_solve_seconds;
  const cells = estimateCellsForResolution(g, chosen);
  return g.estimated_solve_seconds * (cells / baseCells);
}

// --- Live frame-count recompute (time-scale section) --------------------- //
//
// The animation frame count = simulation window (minutes) / cadence (minutes
// per frame), clamped to [1, max_frames]. Mirrors the agent's
// `_estimate_frame_count` so the card readout agrees with the server's
// pre-cap snapshot count. Both inputs are the user's LIVE edited values; the
// interval is floored at `min_interval_min` (the deck floor) so a finer edit
// never advertises more frames than the deck emits.

/** Live animation frame-count estimate for an edited cadence + window. */
export function estimateFrameCount(
  ts: TimeScaleSuggestion,
  intervalMin: number,
  durationHr: number,
): number {
  const floorMin = ts.min_interval_min > 0 ? ts.min_interval_min : 1;
  const interval = Math.max(floorMin, intervalMin > 0 ? intervalMin : floorMin);
  const duration = durationHr > 0 ? durationHr : 0;
  if (interval <= 0 || duration <= 0) return 1;
  const raw = Math.round((duration * 60) / interval);
  const cap = ts.max_frames > 0 ? ts.max_frames : raw;
  return Math.max(1, Math.min(cap, raw));
}

export interface ResolutionPickerCardProps {
  /** The originating tool-payload-warning envelope (carries `granularity`). */
  warning: PayloadWarningEnvelopePayload;
  /** The granularity suggestion (the caller already null-checked it). */
  granularity: GranularitySuggestion;
  /**
   * OPTIONAL time-scale suggestion (combined run-settings gate). When present,
   * the card grows a second section to review + override the animation cadence
   * (minutes per frame) + the simulation window, with a LIVE frame-count
   * readout. On confirm the time-scale override rides back in the SAME
   * revised_args dict as the resolution override (ONE interaction). When absent
   * the card is the granularity-only resolution gate (back-compat).
   */
  timeScale?: TimeScaleSuggestion | null;
  /**
   * Called when the user confirms or cancels. The caller wires this into
   * GraceWs.sendPayloadConfirmation(warning.warning_id, decision, revised) via
   * the EXISTING handlePayloadDecide path. `revised` is null for proceed/cancel;
   * for narrow_scope it carries the chosen overrides:
   *   { [resolution_param]: chosenRes, output_interval_min?, duration_hr? }.
   */
  onDecide: (
    decision: PayloadConfirmationDecision,
    revised: Record<string, unknown> | null,
  ) => void;
  /**
   * Externally-recorded resolution (held in the per-Case stream's
   * payloadResolved map) so the card stays answered across a remount. Seeds the
   * internal `decided` state. Undefined / null = unanswered.
   */
  resolved?: PayloadConfirmationDecision | null;
}

export function ResolutionPickerCard({
  warning,
  granularity,
  timeScale = null,
  onDecide,
  resolved = null,
}: ResolutionPickerCardProps): JSX.Element {
  const g = granularity;
  const ts = timeScale;
  // Combined run-settings card iff a time_scale block accompanies the
  // granularity block. Drives the title / folded-summary language + the second
  // (time-scale) section.
  const combined = ts != null;

  // The chip selection. Default to the suggested rung. If the suggested rung is
  // not among the published choices (defensive), fall back to the first choice
  // so a chip is always selected.
  const initialChoice =
    g.resolution_choices.includes(g.suggested_resolution_m)
      ? g.suggested_resolution_m
      : g.resolution_choices[0] ?? g.suggested_resolution_m;
  const [chosen, setChosen] = useState<number>(initialChoice);

  // Time-scale editable fields (combined card only). Held as STRINGS so a
  // partially-typed value (e.g. "" mid-edit) doesn't snap to a number; parsed
  // on confirm. Seeded from the agent's suggestion (pre-filled, editable).
  const [intervalStr, setIntervalStr] = useState<string>(() =>
    ts ? String(ts.suggested_interval_min) : "",
  );
  const [durationStr, setDurationStr] = useState<string>(() =>
    ts ? String(ts.suggested_duration_hr) : "",
  );

  // Lock + fold once decided. Seed from the externally-recorded resolution.
  const [decided, setDecided] = useState<PayloadConfirmationDecision | null>(
    resolved,
  );
  const [expanded, setExpanded] = useState<boolean>(false);

  // --- Parsed (sanitized) time-scale values for the readout + confirm ------ //
  const floorMin = ts && ts.min_interval_min > 0 ? ts.min_interval_min : 1;
  const parsedInterval = ts
    ? (() => {
        const v = Number(intervalStr);
        return Number.isFinite(v) && v > 0
          ? Math.max(floorMin, v)
          : ts.suggested_interval_min;
      })()
    : 0;
  const parsedDuration = ts
    ? (() => {
        const v = Number(durationStr);
        return Number.isFinite(v) && v > 0 ? v : ts.suggested_duration_hr;
      })()
    : 0;
  // Did the user change either time-scale field from the suggestion?
  const intervalChanged = ts != null && parsedInterval !== ts.suggested_interval_min;
  const durationChanged = ts != null && parsedDuration !== ts.suggested_duration_hr;
  const liveFrameCount = ts
    ? estimateFrameCount(ts, parsedInterval, parsedDuration)
    : 0;

  function decide(
    decision: PayloadConfirmationDecision,
    revised: Record<string, unknown> | null,
  ): void {
    if (decided !== null) return; // already answered - cannot re-answer
    setDecided(decision);
    onDecide(decision, revised);
  }

  function handleConfirm(): void {
    const resChanged = chosen !== g.suggested_resolution_m;
    const tsChanged = intervalChanged || durationChanged;
    if (!resChanged && !tsChanged) {
      // Nothing overridden -> proceed with the agent's suggested settings.
      decide("proceed", null);
      return;
    }
    // At least one lever changed -> narrow_scope carrying BOTH overrides in ONE
    // revised_args dict. Always include the resolution param (the server falls
    // back to the suggestion when it matches anyway) + the changed time-scale
    // fields. ONE interaction, both levers.
    const revised: Record<string, unknown> = {
      [g.resolution_param]: chosen,
    };
    if (ts) {
      if (intervalChanged) revised[ts.cadence_param] = parsedInterval;
      if (durationChanged) revised[ts.duration_param] = parsedDuration;
    }
    decide("narrow_scope", revised);
  }
  function handleCancel(): void {
    decide("cancel", null);
  }

  // --- Folded (resolved) compact card ------------------------------------ //
  const resolvedSummary = combined
    ? RUN_SETTINGS_RESOLVED_SUMMARY
    : RESOLVED_SUMMARY;
  if (decided !== null) {
    return (
      <div
        data-testid="resolution-picker-card"
        data-resolved={decided}
        data-variant="compact"
        data-combined={combined ? "true" : "false"}
        role="status"
        aria-label={resolvedSummary[decided]}
        style={compactStyle}
      >
        <div
          style={{ display: "flex", alignItems: "center", gap: 8, width: "100%" }}
        >
          <span
            aria-hidden="true"
            style={{ display: "inline-flex", alignItems: "center", flexShrink: 0 }}
          >
            <IconCheck size={13} color={ACCENT} />
          </span>
          <span
            data-testid="resolution-picker-resolved"
            style={{
              flex: 1,
              minWidth: 0,
              color: ACCENT,
              fontWeight: 600,
              fontSize: 12,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
            title={resolvedSummary[decided]}
          >
            {resolvedSummary[decided]}
          </span>
          <button
            type="button"
            data-testid="resolution-picker-expand"
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
            data-testid="resolution-picker-detail"
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
            <div style={{ wordBreak: "break-word" }}>
              {g.engine.toUpperCase()} mesh ·{" "}
              <strong style={{ color: "#e5e7eb" }}>{chosen} m</strong> ·{" "}
              ~{formatCellCount(estimateCellsForResolution(g, chosen))} cells (est)
            </div>
            {ts && (
              <div
                data-testid="resolution-picker-detail-timescale"
                style={{ wordBreak: "break-word", marginTop: 4 }}
              >
                Animation ·{" "}
                <strong style={{ color: "#e5e7eb" }}>
                  {parsedInterval}
                </strong>{" "}
                min/frame ·{" "}
                <strong style={{ color: "#e5e7eb" }}>
                  {parsedDuration}
                </strong>{" "}
                h window · ~
                <strong style={{ color: "#e5e7eb" }}>{liveFrameCount}</strong>{" "}
                frames (est)
              </div>
            )}
          </div>
        )}
      </div>
    );
  }

  // --- Active (pending) prompt ------------------------------------------- //

  // Live-recomputed numbers for the chosen rung.
  const chosenCells = estimateCellsForResolution(g, chosen);
  const chosenSeconds = estimateSolveSecondsForResolution(g, chosen);
  const isSuggested = chosen === g.suggested_resolution_m;

  const body = (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {/* Metadata row - authoritative suggested-rung descriptors. */}
      <div
        data-testid="resolution-picker-metadata"
        style={{
          display: "flex",
          gap: 12,
          fontSize: 11,
          color: "#9ca3af",
          flexWrap: "wrap",
        }}
      >
        <span data-testid="resolution-picker-suggested-m">
          Suggested:{" "}
          <strong style={{ color: "#e5e7eb" }}>
            {g.suggested_resolution_m} m
          </strong>
        </span>
        <span data-testid="resolution-picker-cells">
          Cells:{" "}
          <strong style={{ color: "#e5e7eb" }}>
            ~{formatCellCount(g.estimated_active_cells)}
          </strong>
        </span>
        <span data-testid="resolution-picker-eta">
          Solve:{" "}
          <strong style={{ color: "#e5e7eb" }}>
            {formatEta(g.estimated_solve_seconds)}
          </strong>
        </span>
        <span data-testid="resolution-picker-vcpus">
          vCPUs:{" "}
          <strong style={{ color: "#e5e7eb" }}>{g.vcpus}</strong>
        </span>
        <span data-testid="resolution-picker-compute-class">
          Compute:{" "}
          <strong style={{ color: "#e5e7eb" }}>{g.compute_class}</strong>
        </span>
        {g.spot_label && (
          <span data-testid="resolution-picker-spot-label">
            Spot:{" "}
            <strong style={{ color: "#e5e7eb" }}>{g.spot_label}</strong>
          </span>
        )}
      </div>

      {/* Caption - the agent's reason (coarsened-prefixed when applicable). */}
      <div
        data-testid="resolution-picker-reason"
        style={{ color: "#d1d5db", lineHeight: 1.45 }}
      >
        {g.coarsened ? `Coarsened - ${g.reason}` : g.reason}
      </div>

      {/* Override control - choice chips over the published rungs. */}
      <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
        <div
          style={{
            fontSize: 10,
            color: "#6b7280",
            textTransform: "uppercase",
            letterSpacing: "0.05em",
          }}
        >
          Resolution
        </div>
        <div
          data-testid="resolution-picker-chips"
          role="radiogroup"
          aria-label="Mesh resolution"
          style={{ display: "flex", gap: 6, flexWrap: "wrap" }}
        >
          {g.resolution_choices.map((rung) => {
            const selected = rung === chosen;
            const suggested = rung === g.suggested_resolution_m;
            return (
              <button
                key={rung}
                type="button"
                role="radio"
                aria-checked={selected}
                data-testid={`resolution-picker-chip-${rung}`}
                data-selected={selected ? "true" : "false"}
                onClick={() => setChosen(rung)}
                style={{
                  border: `1px solid ${selected ? ACCENT : "#3f3f46"}`,
                  borderRadius: 14,
                  padding: "3px 10px",
                  fontSize: 12,
                  fontWeight: selected ? 700 : 500,
                  cursor: "pointer",
                  fontFamily: "inherit",
                  lineHeight: 1.3,
                  background: selected ? "rgba(234,179,8,0.18)" : "transparent",
                  color: selected ? ACCENT : "#d1d5db",
                  fontVariantNumeric: "tabular-nums",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 4,
                }}
              >
                {rung} m
                {suggested && (
                  <span
                    aria-hidden="true"
                    style={{ color: "#9ca3af", fontSize: 10, fontWeight: 500 }}
                  >
                    (suggested)
                  </span>
                )}
              </button>
            );
          })}
        </div>

        {/* Live recompute readout for the chosen rung (tabular-nums). */}
        <div
          data-testid="resolution-picker-readout"
          style={{
            fontSize: 11,
            color: "#9ca3af",
            fontVariantNumeric: "tabular-nums",
          }}
        >
          At <strong style={{ color: "#e5e7eb" }}>{chosen} m</strong>:{" "}
          <strong
            data-testid="resolution-picker-readout-cells"
            style={{ color: "#e5e7eb" }}
          >
            ~{formatCellCount(chosenCells)}
          </strong>{" "}
          cells ·{" "}
          <strong
            data-testid="resolution-picker-readout-eta"
            style={{ color: "#e5e7eb" }}
          >
            {formatEta(chosenSeconds)}
          </strong>{" "}
          <span style={{ color: "#6b7280" }}>
            ({isSuggested ? "suggested rung" : "estimated"})
          </span>
        </div>
      </div>

      {/* TIME SCALE section (combined run-settings card only). Editable cadence
          (minutes per frame) + simulation window (hours), with a LIVE
          frame-count readout that recomputes as the user types. */}
      {ts && (
        <div
          data-testid="resolution-picker-timescale"
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 6,
            marginTop: 2,
            paddingTop: 8,
            borderTop: "1px solid rgba(255,255,255,0.08)",
          }}
        >
          <div
            style={{
              fontSize: 10,
              color: "#6b7280",
              textTransform: "uppercase",
              letterSpacing: "0.05em",
            }}
          >
            Time scale
          </div>
          <div
            data-testid="resolution-picker-timescale-reason"
            style={{ color: "#d1d5db", lineHeight: 1.45, fontSize: 12 }}
          >
            {ts.reason}
          </div>
          <div style={{ display: "flex", gap: 14, flexWrap: "wrap" }}>
            {/* Cadence (minutes per frame) - editable numeric field. */}
            <label
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 3,
                fontSize: 11,
                color: "#9ca3af",
              }}
            >
              Minutes / frame
              <input
                type="number"
                inputMode="decimal"
                min={ts.min_interval_min}
                step="any"
                data-testid="resolution-picker-interval-input"
                value={intervalStr}
                onChange={(e) => setIntervalStr(e.target.value)}
                style={tsInputStyle}
              />
            </label>
            {/* Simulation window (hours) - editable numeric field. */}
            <label
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 3,
                fontSize: 11,
                color: "#9ca3af",
              }}
            >
              Window (hours)
              <input
                type="number"
                inputMode="decimal"
                min={0}
                step="any"
                data-testid="resolution-picker-duration-input"
                value={durationStr}
                onChange={(e) => setDurationStr(e.target.value)}
                style={tsInputStyle}
              />
            </label>
          </div>

          {/* Quick-pick cadence chips (optional ladder; free-edit still allowed). */}
          {ts.interval_choices.length > 0 && (
            <div
              data-testid="resolution-picker-interval-chips"
              role="group"
              aria-label="Animation cadence presets"
              style={{ display: "flex", gap: 6, flexWrap: "wrap" }}
            >
              {ts.interval_choices.map((rung) => {
                const selected = parsedInterval === rung;
                const suggested = rung === ts.suggested_interval_min;
                return (
                  <button
                    key={rung}
                    type="button"
                    data-testid={`resolution-picker-interval-chip-${rung}`}
                    data-selected={selected ? "true" : "false"}
                    onClick={() => setIntervalStr(String(rung))}
                    style={{
                      border: `1px solid ${selected ? ACCENT : "#3f3f46"}`,
                      borderRadius: 14,
                      padding: "2px 9px",
                      fontSize: 11,
                      fontWeight: selected ? 700 : 500,
                      cursor: "pointer",
                      fontFamily: "inherit",
                      lineHeight: 1.3,
                      background: selected
                        ? "rgba(234,179,8,0.18)"
                        : "transparent",
                      color: selected ? ACCENT : "#d1d5db",
                      fontVariantNumeric: "tabular-nums",
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 4,
                    }}
                  >
                    {rung} min
                    {suggested && (
                      <span
                        aria-hidden="true"
                        style={{
                          color: "#9ca3af",
                          fontSize: 9,
                          fontWeight: 500,
                        }}
                      >
                        (suggested)
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          )}

          {/* LIVE frame-count readout - recomputes as the fields change. */}
          <div
            data-testid="resolution-picker-frame-readout"
            style={{
              fontSize: 11,
              color: "#9ca3af",
              fontVariantNumeric: "tabular-nums",
            }}
          >
            ~
            <strong
              data-testid="resolution-picker-frame-count"
              style={{ color: "#e5e7eb" }}
            >
              {liveFrameCount}
            </strong>{" "}
            animation frames at{" "}
            <strong style={{ color: "#e5e7eb" }}>{parsedInterval}</strong> min
            over a <strong style={{ color: "#e5e7eb" }}>{parsedDuration}</strong>{" "}
            h window{" "}
            <span style={{ color: "#6b7280" }}>
              (max {ts.max_frames})
            </span>
          </div>
        </div>
      )}
    </div>
  );

  const actions: InlineChatCardAction[] = [
    {
      label: "Confirm",
      onClick: handleConfirm,
      tone: "primary",
      testId: "resolution-picker-confirm",
    },
    {
      label: "Cancel",
      onClick: handleCancel,
      tone: "muted",
      testId: "resolution-picker-cancel",
    },
  ];

  const cardTitle = combined ? "Confirm run settings" : "Confirm mesh resolution";
  return (
    <InlineChatCard
      variant="warning"
      title={cardTitle}
      body={body}
      actions={actions}
      icon={<IconGrid size={14} color={ACCENT} />}
      testId="resolution-picker-card"
      ariaLabel={cardTitle}
      extraAttrs={{
        "data-warning-id": warning.warning_id,
        "data-engine": g.engine,
        "data-combined": combined ? "true" : "false",
      }}
    />
  );
}

// Shared style for the editable numeric time-scale fields. Dark input matching
// the PayloadWarningInline clarifier textarea palette.
const tsInputStyle: React.CSSProperties = {
  background: "rgba(0,0,0,0.4)",
  color: "#e5e7eb",
  border: "1px solid #3f3f46",
  borderRadius: 6,
  padding: "5px 8px",
  fontFamily: "inherit",
  fontSize: 12,
  width: 96,
  fontVariantNumeric: "tabular-nums",
  boxSizing: "border-box",
};
