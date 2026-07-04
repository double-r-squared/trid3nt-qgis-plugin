// GRACE-2 web — SandboxCard (sprint-13, job-0234).
//
// Chat-inline card for the Python sandbox code-exec lifecycle, wired to the
// ``code-exec-request`` / ``code-exec-result`` envelopes from job-0233.
//
// STATES
// ------
// REQUEST  — show the exact python_code the agent wants to execute (monospace,
//            scrollable, syntax-dimmed), the rationale caption, and the gate
//            buttons: Proceed (primary) + Cancel (muted, rightmost). This is the
//            hard confirm gate (Invariant 9 — running arbitrary code is a
//            consequential action). Cancel rightmost per the
//            ``feedback_payload_warning_ux_redesign`` button-order memory.
//            Decision rides back on the EXISTING ``tool-payload-confirmation``
//            envelope with the code_exec_id as warning_id (NO new reply type).
//
// RUNNING  — same ephemeral treatment as other in-flight pipeline cards:
//            rainbow-gradient spinner. No buttons.
//
// RESULT   — status chip (ok=green / error=red / timeout=amber / blocked=red),
//            stdout tail (collapsible, hidden by default), result descriptor
//            rendered inline (scalar → plain text; dict/json → pretty JSON
//            capped at 40 lines; chart → note: handled by the chart-emission
//            envelope separately; too_large → marker), truncated=true marker,
//            Save button (downloads the result JSON).
//
// CONFIRM-WIRING
// --------------
// Proceed/Cancel call ``onDecide`` which the parent (Chat.tsx) wires to
// GraceWs.sendPayloadConfirmation(code_exec_id, decision) — same method the
// PayloadWarningInline uses. No new reply type invented.
//
// Invariant 1 (Determinism): displayed numbers come from the result descriptor
// the sandbox computed; the SandboxCard never fabricates them.
// Invariant 9 (No cost theater): no dollar/quota field. duration_s is latency.
//
// This component is a pure presentation surface — all state and side effects
// live in the parent (Chat.tsx).

import { useState } from "react";
import {
  IconSandbox,
  IconWarning,
  IconChevronRight,
  IconChevronDown,
  IconArrowRight,
  IconCheck,
  IconClose,
} from "./icons";

// ---------------------------------------------------------------------------
// Wire shapes (mirrors sandbox_contracts.py — hand-mirrored, no codegen).
// ---------------------------------------------------------------------------

export type CodeExecStatus = "ok" | "error" | "timeout" | "blocked";

/** Mirrors CodeExecRequestPayload from packages/contracts/.../sandbox_contracts.py */
export interface CodeExecRequestPayload {
  envelope_type: "code-exec-request";
  code_exec_id: string;
  python_code: string;
  /**
   * {var_name: layer_uri} the sandbox will pre-open. A value may be a single URI
   * string (one handle) OR an ordered list of frame URIs (an animation sequence
   * pre-opened as a list of handles) — the ADDITIVE multi-frame extension.
   */
  layer_refs: Record<string, string | string[]>;
  rationale?: string | null;
}

/** Mirrors CodeExecResultPayload from packages/contracts/.../sandbox_contracts.py */
export interface CodeExecResultPayload {
  envelope_type: "code-exec-result";
  code_exec_id: string;
  status: CodeExecStatus;
  stdout_tail: string;
  stderr_tail: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  result: Record<string, any> | null;
  truncated: boolean;
  duration_s: number;
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export type SandboxCardDecision = "proceed" | "cancel";

export interface SandboxCardProps {
  /**
   * The code-exec-request payload. The card always renders this (the code +
   * rationale + layer refs are shown in both REQUEST and RESULT states so
   * the user can cross-check what was approved).
   */
  request: CodeExecRequestPayload;
  /**
   * When present, the card switches to RESULT state showing the outcome.
   * While absent AND decided === "proceed", the card shows RUNNING state.
   */
  result?: CodeExecResultPayload;
  /**
   * The decision the user made (set when the user clicks Proceed or Cancel).
   * Null until the user decides; locks the gate buttons after click.
   */
  decided: SandboxCardDecision | null;
  /**
   * Called when the user clicks Proceed or Cancel. The parent wires this to
   * GraceWs.sendPayloadConfirmation(code_exec_id, decision).
   */
  onDecide: (decision: SandboxCardDecision) => void;
}

// ---------------------------------------------------------------------------
// Status chip colors / labels
// ---------------------------------------------------------------------------

const STATUS_BG: Record<CodeExecStatus, string> = {
  ok:      "rgba(16,185,129,0.18)",
  error:   "rgba(239,68,68,0.18)",
  timeout: "rgba(234,179,8,0.18)",
  blocked: "rgba(239,68,68,0.18)",
};
const STATUS_COLOR: Record<CodeExecStatus, string> = {
  ok:      "#10b981",
  error:   "#ef4444",
  timeout: "#eab308",
  blocked: "#ef4444",
};
const STATUS_LABEL: Record<CodeExecStatus, string> = {
  ok:      "ok",
  error:   "error",
  timeout: "timeout",
  blocked: "blocked",
};

// Per-accent solid color (matches the STATUS_COLOR chip palette ~116-121 so the
// card border / icon / summary read as the same lineage as the status chip).
// NATE 2026-06-26: FAILED must be red on the WHOLE card, not just the chip, and
// the indigo border can no longer be hardcoded — derive the accent below.
const INDIGO_ACCENT = "#6366f1"; // pending / running
const STATUS_ACCENT: Record<CodeExecStatus, string> = {
  ok:      "#10b981", // green
  error:   "#ef4444", // red
  timeout: "#eab308", // amber
  blocked: "#ef4444", // red
};

/**
 * Derive the single card accent from the lifecycle state. PENDING/RUNNING are
 * indigo; a result maps to its status color (success green, error/blocked red,
 * timeout amber); a cancelled-without-result card keeps the neutral indigo (the
 * compact summary itself uses a muted grey glyph). NATE 2026-06-26.
 */
function deriveAccent(result: CodeExecResultPayload | undefined): string {
  if (result === undefined) return INDIGO_ACCENT;
  return STATUS_ACCENT[result.status];
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Render the result descriptor into a displayable string, capped at lines. */
function renderResultDescriptor(
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  result: Record<string, any>,
  maxLines = 40,
): { text: string; capped: boolean } {
  const kind = result["kind"];
  // Chart results: the inline PNG is rendered by ResultDescriptorView (the
  // <img> path). This text helper is only reached for the JSON/Save fallback, so
  // keep an honest note here — the PNG path never relies on this string.
  if (kind === "chart") {
    return { text: "(figure rendered above)", capped: false };
  }
  // too_large → honest marker
  if (kind === "too_large") {
    const originalBytes: number | undefined = result["original_bytes"] as number | undefined;
    const note = originalBytes
      ? `Result too large to display (${(originalBytes / 1024).toFixed(0)} KiB)`
      : "Result too large to display";
    return { text: note, capped: false };
  }
  // Scalar / json / dataframe: pretty-print the value field (or whole dict)
  let raw: string;
  try {
    const value = "value" in result ? result["value"] : result;
    raw = JSON.stringify(value, null, 2);
  } catch {
    raw = String(result);
  }
  const lines = raw.split("\n");
  if (lines.length > maxLines) {
    return {
      text: lines.slice(0, maxLines).join("\n") + "\n… (truncated)",
      capped: true,
    };
  }
  return { text: raw, capped: false };
}

/**
 * Cheap one-line hint appended to the success summary when the result descriptor
 * carries a scalar value or a figure. Returns "" when nothing cheap is available
 * (dict/json/dataframe/too_large) — we never pretty-print a whole dict into the
 * folded summary. NATE 2026-06-26.
 */
function cheapResultHint(
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  result: Record<string, any> | null,
): string {
  if (!result) return "";
  const kind = result["kind"];
  if (kind === "chart") return "figure";
  if (kind === "json") {
    const value = result["value"];
    const isScalar =
      typeof value === "number" ||
      typeof value === "boolean" ||
      typeof value === "string";
    if (isScalar) return String(value);
  }
  return "";
}

/** Download a JSON blob as a file. */
function downloadJson(data: unknown, filename: string): void {
  try {
    const blob = new Blob([JSON.stringify(data, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  } catch {
    // ignore
  }
}

// ---------------------------------------------------------------------------
// Style constants
// ---------------------------------------------------------------------------

const MONO_FONT =
  'ui-monospace, SFMono-Regular, Menlo, Consolas, "Courier New", monospace';

// NATE 2026-06-26: the left border is now a function of the derived accent (was a
// hardcoded indigo #6366f1) so FAILED reads red / timeout amber / success green
// on the whole card, not just the status chip.
function cardStyle(accent: string): React.CSSProperties {
  return {
    background: "rgba(16,18,24,0.96)",
    border: "1px solid rgba(255,255,255,0.07)",
    borderLeft: `3px solid ${accent}`,
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
}

const HEADER_STYLE: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
};

const TITLE_STYLE: React.CSSProperties = {
  fontSize: 13,
  fontWeight: 600,
  color: "#f3f4f6",
  flex: 1,
};

const CODE_BLOCK_STYLE: React.CSSProperties = {
  background: "rgba(0,0,0,0.45)",
  border: "1px solid #2a2d35",
  borderRadius: 6,
  padding: "8px 10px",
  fontFamily: MONO_FONT,
  fontSize: 11,
  color: "#c8d0e0",
  overflowX: "auto",
  overflowY: "auto",
  maxHeight: 200,
  whiteSpace: "pre",
  lineHeight: 1.5,
  // syntax-dim: soften pure white so code doesn't glare
  opacity: 0.9,
};

const ACTION_ROW_STYLE: React.CSSProperties = {
  display: "flex",
  gap: 6,
  flexWrap: "wrap",
  marginTop: 2,
};

function btnStyle(
  tone: "primary" | "secondary" | "muted",
  disabled: boolean,
): React.CSSProperties {
  const base: React.CSSProperties = {
    border: "1px solid transparent",
    borderRadius: 6,
    padding: "5px 10px",
    fontSize: 12,
    fontWeight: 600,
    cursor: disabled ? "default" : "pointer",
    fontFamily: "inherit",
    lineHeight: 1.2,
    transition: "background 0.12s ease, border-color 0.12s ease",
  };
  if (disabled) {
    return { ...base, background: "rgba(255,255,255,0.04)", color: "#555", borderColor: "#333" };
  }
  if (tone === "primary") {
    return { ...base, background: "#6366f1", color: "#f9fafb", borderColor: "#6366f1" };
  }
  if (tone === "secondary") {
    return { ...base, background: "rgba(255,255,255,0.05)", color: "#e5e7eb", borderColor: "#3f3f46" };
  }
  // muted
  return { ...base, background: "transparent", color: "#9ca3af", borderColor: "transparent", fontWeight: 500 };
}

// ---------------------------------------------------------------------------
// RUNNING sub-component (ephemeral treatment)
// ---------------------------------------------------------------------------

function RunningIndicator(): JSX.Element {
  return (
    <div
      data-testid="sandbox-card-running"
      style={{ display: "flex", alignItems: "center", gap: 8 }}
    >
      <span
        aria-hidden="true"
        style={{
          display: "inline-block",
          width: 12,
          height: 12,
          borderRadius: "50%",
          border: "2px solid #6366f1",
          borderTopColor: "transparent",
          animation: "sandbox-spin 0.8s linear infinite",
        }}
      />
      <span style={{ color: "#a5b4fc", fontSize: 12, fontStyle: "italic" }}>
        Running Python sandbox…
      </span>
      <style>{`
        @keyframes sandbox-spin {
          to { transform: rotate(360deg); }
        }
        @media (prefers-reduced-motion: reduce) {
          .sandbox-spin { animation: none; }
        }
      `}</style>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function SandboxCard({
  request,
  result,
  decided,
  onDecide,
}: SandboxCardProps): JSX.Element {
  // Collapsible stdout / stderr (default collapsed to keep card tidy)
  const [stdoutOpen, setStdoutOpen] = useState(false);
  const [stderrOpen, setStderrOpen] = useState(false);
  // NATE 2026-06-26: a decided/resolved card folds to a compact one-line summary
  // (mirrors SpatialInputCard / ResolutionPickerCard); the full chrome lives
  // under this expander so a resolved card no longer shows a verbose footer +
  // a forever-expanded result section.
  const [expanded, setExpanded] = useState(false);

  // ---- State machine ---------------------------------------------------- //
  // PENDING        : decided === null && !hasResult
  // RUNNING        : decided === "proceed" && !hasResult
  // RESOLVED-*     : hasResult (status drives success/failed)
  // CANCELLED      : decided === "cancel" && !hasResult
  const isRunning = decided === "proceed" && result === undefined;
  const isCancelled = decided === "cancel";
  const hasResult = result !== undefined;
  const isPending = decided === null && !hasResult;
  // The card folds whenever it is decided OR resolved (i.e. anything but PENDING
  // and RUNNING — RUNNING keeps full chrome + glow while the work is in flight).
  const isFolded = !isPending && !isRunning;
  const isSuccess = hasResult && result!.status === "ok";

  // Derive the single card accent (indigo pending/running, status color on a
  // result). NATE 2026-06-26.
  const accent = deriveAccent(result);

  // Stable data-state for tests + downstream styling.
  const dataState = hasResult
    ? isSuccess
      ? "resolved-ok"
      : "resolved-failed"
    : isCancelled
      ? "cancelled"
      : isRunning
        ? "running"
        : "pending";

  // Gate buttons (REQUEST state only)
  function handleProceed(): void {
    onDecide("proceed");
  }
  function handleCancel(): void {
    onDecide("cancel");
  }

  const hasLayerRefs =
    request.layer_refs && Object.keys(request.layer_refs).length > 0;

  // ---- Compact summary (folded state) ----------------------------------- //
  // Leading glyph + one-line text per resolved/cancelled state. Success shows a
  // cheap scalar/figure hint when available; failed shows the status; cancelled
  // is a plain note. NATE 2026-06-26.
  function renderCompactSummary(): JSX.Element {
    let glyph: JSX.Element;
    let text: string;
    let textColor: string;
    if (hasResult && result) {
      if (isSuccess) {
        const hint = cheapResultHint(result.result);
        glyph = <IconCheck size={13} color={accent} />;
        text = hint ? `Python sandbox - ok (${hint})` : "Python sandbox - ok";
        textColor = accent;
      } else {
        glyph = <IconWarning size={13} color={accent} />;
        text = `Python sandbox - ${result.status}`;
        textColor = accent;
      }
    } else {
      // Cancelled-without-result.
      glyph = <IconClose size={13} color="#9ca3af" />;
      text = "Execution cancelled";
      textColor = "#9ca3af";
    }
    return (
      <div
        data-testid="sandbox-card-summary"
        style={{ display: "flex", alignItems: "center", gap: 8, width: "100%" }}
      >
        <span
          aria-hidden="true"
          style={{ display: "inline-flex", alignItems: "center", flexShrink: 0 }}
        >
          {glyph}
        </span>
        <span
          style={{
            flex: 1,
            minWidth: 0,
            color: textColor,
            fontWeight: 600,
            fontSize: 12,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
          title={text}
        >
          {text}
        </span>
        <button
          type="button"
          data-testid="sandbox-card-expand"
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
    );
  }

  // The card root carries the running glow (guarded for prefers-reduced-motion
  // via the @media block inside the <style> below). NATE 2026-06-26.
  const rootStyle: React.CSSProperties = {
    ...cardStyle(accent),
    ...(isRunning
      ? { animation: "sandbox-glow 1.6s ease-in-out infinite" }
      : {}),
  };

  return (
    <div
      data-testid="sandbox-card"
      data-code-exec-id={request.code_exec_id}
      data-state={dataState}
      data-accent={accent}
      style={rootStyle}
      role="region"
      aria-label="Python sandbox code execution"
    >
      {/* Running glow keyframes (disabled under reduced-motion). */}
      <style>{`
        @keyframes sandbox-glow {
          0%, 100% { box-shadow: 0 0 0 0 rgba(99,102,241,0); }
          50% { box-shadow: 0 0 12px 2px rgba(99,102,241,0.55); }
        }
        @media (prefers-reduced-motion: reduce) {
          [data-testid="sandbox-card"][data-state="running"] { animation: none !important; }
        }
      `}</style>

      {/* Compact summary row (folded states: resolved / cancelled). The full
          chrome below renders only when expanded. */}
      {isFolded && renderCompactSummary()}

      {/* Full chrome: always for PENDING / RUNNING; behind the expander when
          folded (resolved / cancelled). NATE 2026-06-26. */}
      {(!isFolded || expanded) && (
        <div
          data-testid="sandbox-card-detail"
          style={{ display: "flex", flexDirection: "column", gap: 8 }}
        >
      {/* Header */}
      <div style={HEADER_STYLE}>
        <span aria-hidden="true" style={{ color: "#6366f1", lineHeight: 1.2, flexShrink: 0, display: "inline-flex" }}>
          <IconSandbox size={14} weight="bold" />
        </span>
        <strong
          data-testid="sandbox-card-title"
          style={TITLE_STYLE}
        >
          {hasResult
            ? "Python sandbox result"
            : isCancelled
              ? "Python sandbox cancelled"
              : isRunning
                ? "Running Python sandbox"
                : "Python sandbox — confirm execution"}
        </strong>
        {/* Status chip for result state */}
        {hasResult && result && (
          <span
            data-testid="sandbox-card-status-chip"
            data-status={result.status}
            style={{
              background: STATUS_BG[result.status],
              color: STATUS_COLOR[result.status],
              border: `1px solid ${STATUS_COLOR[result.status]}`,
              borderRadius: 12,
              padding: "1px 8px",
              fontSize: 11,
              fontWeight: 600,
              lineHeight: 1.5,
              flexShrink: 0,
            }}
          >
            {STATUS_LABEL[result.status]}
          </span>
        )}
      </div>

      {/* Rationale caption (shown when present in any state) */}
      {request.rationale && (
        <div
          data-testid="sandbox-card-rationale"
          style={{ color: "#9ca3af", fontSize: 11, lineHeight: 1.4 }}
        >
          {request.rationale}
        </div>
      )}

      {/* Code block (always visible — user confirmed what they're approving) */}
      <div>
        <div
          style={{
            fontSize: 10,
            color: "#6b7280",
            marginBottom: 4,
            fontFamily: "inherit",
            textTransform: "uppercase",
            letterSpacing: "0.05em",
          }}
        >
          Python code
        </div>
        <pre
          data-testid="sandbox-card-code"
          style={CODE_BLOCK_STYLE}
        >
          {request.python_code}
        </pre>
      </div>

      {/* Layer refs (if any) */}
      {hasLayerRefs && (
        <div
          data-testid="sandbox-card-layer-refs"
          style={{ display: "flex", flexDirection: "column", gap: 2 }}
        >
          <div style={{ fontSize: 10, color: "#6b7280", textTransform: "uppercase", letterSpacing: "0.05em" }}>
            Layers
          </div>
          {Object.entries(request.layer_refs).map(([varName, ref]) => {
            // A ref is either a single URI string or an ordered list of frame
            // URIs (multi-frame extension). Show the count + first URI for a list.
            const isList = Array.isArray(ref);
            const display = isList
              ? `${(ref as string[]).length} frames${(ref as string[])[0] ? ` — ${(ref as string[])[0]}` : ""}`
              : (ref as string);
            return (
              <div key={varName} style={{ display: "flex", gap: 6, fontSize: 11 }}>
                <span style={{ fontFamily: MONO_FONT, color: "#93c5fd" }}>{varName}</span>
                <span style={{ color: "#4b5563", display: "inline-flex", alignItems: "center" }}>
                  <IconArrowRight size={11} />
                </span>
                <span style={{ color: "#6b7280", wordBreak: "break-all" }}>{display}</span>
              </div>
            );
          })}
        </div>
      )}

      {/* Running indicator */}
      {isRunning && <RunningIndicator />}

      {/* Cancelled note */}
      {isCancelled && !hasResult && (
        <div
          data-testid="sandbox-card-cancelled-note"
          style={{ color: "#6b7280", fontSize: 11, fontStyle: "italic" }}
        >
          Execution cancelled by user.
        </div>
      )}

      {/* RESULT content */}
      {hasResult && result && (
        <div
          data-testid="sandbox-card-result-section"
          style={{ display: "flex", flexDirection: "column", gap: 6 }}
        >
          {/* Duration */}
          {result.duration_s > 0 && (
            <div
              data-testid="sandbox-card-duration"
              style={{ color: "#6b7280", fontSize: 11 }}
            >
              Duration: {result.duration_s.toFixed(2)}s
            </div>
          )}

          {/* Truncated marker */}
          {result.truncated && (
            <div
              data-testid="sandbox-card-truncated"
              style={{
                background: "rgba(234,179,8,0.12)",
                border: "1px solid rgba(234,179,8,0.3)",
                borderRadius: 4,
                padding: "3px 8px",
                fontSize: 11,
                color: "#eab308",
                display: "flex",
                alignItems: "center",
                gap: 5,
              }}
            >
              <IconWarning size={12} />
              Output was truncated — some data may be missing.
            </div>
          )}

          {/* Result descriptor */}
          {result.result !== null && (
            <div
              data-testid="sandbox-card-result-descriptor"
              style={{ display: "flex", flexDirection: "column", gap: 4 }}
            >
              <div style={{ fontSize: 10, color: "#6b7280", textTransform: "uppercase", letterSpacing: "0.05em" }}>
                Result
              </div>
              <ResultDescriptorView descriptor={result.result} />
            </div>
          )}

          {/* Stdout (collapsible) */}
          {result.stdout_tail && (
            <CollapsibleSection
              label="stdout"
              content={result.stdout_tail}
              open={stdoutOpen}
              onToggle={() => setStdoutOpen((v) => !v)}
              testId="sandbox-card-stdout"
            />
          )}

          {/* Stderr (collapsible) */}
          {result.stderr_tail && (
            <CollapsibleSection
              label="stderr"
              content={result.stderr_tail}
              open={stderrOpen}
              onToggle={() => setStderrOpen((v) => !v)}
              testId="sandbox-card-stderr"
            />
          )}

          {/* Save button */}
          <div style={{ marginTop: 2 }}>
            <button
              type="button"
              data-testid="sandbox-card-save-button"
              onClick={() =>
                downloadJson(
                  { request, result },
                  `code-exec-${request.code_exec_id}.json`,
                )
              }
              style={btnStyle("secondary", false)}
            >
              Save result JSON
            </button>
          </div>
        </div>
      )}

      {/* Gate buttons (REQUEST state only — not yet decided) */}
      {decided === null && !hasResult && (
        <div
          data-testid="sandbox-card-actions"
          style={ACTION_ROW_STYLE}
        >
          {/* Proceed is primary; Cancel is muted and rightmost per memory */}
          <button
            type="button"
            data-testid="sandbox-card-proceed"
            onClick={handleProceed}
            style={btnStyle("primary", false)}
          >
            Proceed
          </button>
          <button
            type="button"
            data-testid="sandbox-card-cancel"
            onClick={handleCancel}
            style={btnStyle("muted", false)}
          >
            Cancel
          </button>
        </div>
      )}
        </div>
      )}
      {/* NATE 2026-06-26: the verbose "Decision sent: <x>" footer is gone — a
          decided card now folds to the compact summary row above (the gate is
          implied by the resolved/cancelled summary). */}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Result descriptor sub-component
// ---------------------------------------------------------------------------

function ResultDescriptorView({
  descriptor,
}: {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  descriptor: Record<string, any>;
}): JSX.Element {
  const kind = descriptor["kind"];

  if (kind === "chart") {
    // The sandbox executor renders a matplotlib Figure to PNG and carries it
    // inline as ``png_base64`` (capped at the executor's MAX_FIGURE_PNG_BYTES;
    // ``png_truncated`` is true when the PNG was dropped for being over-cap).
    // Render the figure directly in the card (capped to card width) when the
    // PNG is present; fall back to the honest "dropped" note only when it isn't.
    const pngBase64: string | undefined = descriptor["png_base64"] as
      | string
      | undefined;
    const pngTruncated = Boolean(descriptor["png_truncated"]);
    const title: string | undefined = descriptor["title"] as string | undefined;
    if (pngBase64 && !pngTruncated) {
      return (
        <div
          data-testid="sandbox-result-chart-image"
          style={{ display: "flex", flexDirection: "column", gap: 4 }}
        >
          <img
            src={`data:image/png;base64,${pngBase64}`}
            alt={title || "Sandbox figure"}
            style={{
              maxWidth: "100%",
              height: "auto",
              borderRadius: 6,
              border: "1px solid #1d2233",
              background: "#fff",
              display: "block",
            }}
          />
          {title && (
            <div style={{ color: "#9ca3af", fontSize: 11, textAlign: "center" }}>
              {title}
            </div>
          )}
        </div>
      );
    }
    // PNG absent (too large to inline, or a render error) — honest note.
    return (
      <div
        data-testid="sandbox-result-chart-note"
        style={{ color: "#a5b4fc", fontSize: 11, fontStyle: "italic" }}
      >
        {pngTruncated
          ? "Figure too large to display inline (it exceeded the size cap)."
          : "Figure was produced but could not be rendered."}
      </div>
    );
  }

  if (kind === "too_large") {
    const originalBytes: number | undefined = descriptor["original_bytes"] as number | undefined;
    return (
      <div
        data-testid="sandbox-result-too-large"
        style={{ color: "#f87171", fontSize: 11 }}
      >
        {originalBytes
          ? `Result too large to display (${(originalBytes / 1024).toFixed(0)} KiB)`
          : "Result too large to display"}
      </div>
    );
  }

  // Scalar: just show the value inline (plain text, not code block)
  if (kind === "json") {
    const value = descriptor["value"];
    const isScalar =
      typeof value === "number" ||
      typeof value === "boolean" ||
      typeof value === "string";
    if (isScalar) {
      return (
        <span
          data-testid="sandbox-result-scalar"
          style={{ color: "#d1fae5", fontSize: 13, fontWeight: 600 }}
        >
          {String(value)}
        </span>
      );
    }
  }

  // Default: pretty JSON in a code block (capped)
  const { text, capped } = renderResultDescriptor(descriptor);
  return (
    <div>
      <pre
        data-testid="sandbox-result-json"
        style={{
          ...CODE_BLOCK_STYLE,
          maxHeight: 160,
          background: "rgba(0,0,0,0.3)",
          borderColor: "#1d2233",
        }}
      >
        {text}
      </pre>
      {capped && (
        <div style={{ color: "#6b7280", fontSize: 10, marginTop: 2 }}>
          (output capped at 40 lines)
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Collapsible section (stdout / stderr)
// ---------------------------------------------------------------------------

interface CollapsibleSectionProps {
  label: string;
  content: string;
  open: boolean;
  onToggle: () => void;
  testId: string;
}

function CollapsibleSection({
  label,
  content,
  open,
  onToggle,
  testId,
}: CollapsibleSectionProps): JSX.Element {
  return (
    <div data-testid={testId} style={{ display: "flex", flexDirection: "column", gap: 3 }}>
      <button
        type="button"
        data-testid={`${testId}-toggle`}
        onClick={onToggle}
        style={{
          background: "none",
          border: "none",
          cursor: "pointer",
          color: "#9ca3af",
          fontSize: 11,
          display: "flex",
          alignItems: "center",
          gap: 4,
          padding: 0,
          fontFamily: "inherit",
          textAlign: "left",
        }}
        aria-expanded={open}
      >
        <span aria-hidden="true" style={{ display: "inline-flex", transition: "transform 0.15s", transform: open ? "rotate(90deg)" : "none" }}>
          <IconChevronRight size={12} />
        </span>
        {label}
        <span style={{ color: "#4b5563" }}>({content.length} chars)</span>
      </button>
      {open && (
        <pre
          data-testid={`${testId}-content`}
          style={{
            ...CODE_BLOCK_STYLE,
            maxHeight: 140,
            background: "rgba(0,0,0,0.3)",
            borderColor: "#1d2233",
            color: label === "stderr" ? "#fca5a5" : "#c8d0e0",
          }}
        >
          {content}
        </pre>
      )}
    </div>
  );
}
