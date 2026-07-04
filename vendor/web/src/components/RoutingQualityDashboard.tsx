// GRACE-2 web — RoutingQualityDashboard (Wave 4.11 M7).
//
// Full-screen overlay that surfaces aggregated tool-routing telemetry over
// the most recent 30 sessions. Backed by ``GET /api/telemetry/summary`` on
// the agent service's HTTP listener (default port 8766; override via
// ``VITE_GRACE2_HTTP_URL``).
//
// Visible surface:
//
//   +---------------------------------------------------------------+
//   | Routing quality                                              ✕ |
//   |   Live snapshot — last 30 sessions   [Refresh ⟳]              |
//   |                                                                |
//   |   [Total dispatches] [Error rate] [Cache hits] [Avg latency]   |
//   |                                                                |
//   |   ── Top 15 tools by dispatch count ──                         |
//   |   fetch_dem          ████████████████ 41                       |
//   |   compute_hillshade  ██████████ 25                             |
//   |   ...                                                          |
//   |                                                                |
//   |   ── Per-tool stats ──                                         |
//   |   Tool | Count | Error rate | Avg latency                      |
//   |                                                                |
//   |   ── Recent chains ──                                          |
//   |   fetch_dem → compute_hillshade   (× 12)                       |
//   +---------------------------------------------------------------+
//
// Auto-refresh: every 30 seconds while mounted. Manual refresh via the
// header button. Esc / backdrop / X to dismiss.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { IconClose, IconChevronRight } from "./icons";
import { SELECTABLE_MODELS } from "../lib/modelRegistry";
import { httpBase } from "../lib/public_base";

// Friendly label / accent for a by_model row's model_id. We do NOT use
// modelRegistry.getModelById here: that helper falls back to Sonnet for any
// unknown id (including the "unknown" legacy bucket), which would mislabel a
// non-selectable / pre-feature model as "Claude Sonnet 4.6". For the dashboard
// we want an HONEST label — a known selectable model gets its short label +
// accent; anything else (incl. "unknown") shows the raw id with a neutral tint.
const MODEL_LABELS: Record<string, { label: string; accent: string }> =
  Object.fromEntries(
    SELECTABLE_MODELS.map((m) => [
      m.id,
      { label: m.label, accent: m.accentColor },
    ]),
  );

function modelDisplay(modelId: string): { label: string; accent: string } {
  const known = MODEL_LABELS[modelId];
  if (known) return known;
  if (modelId === "unknown" || !modelId) {
    return { label: "Unknown / legacy", accent: "#6f7585" };
  }
  return { label: modelId, accent: "#6f7585" };
}

// ---------------------------------------------------------------------------
// Wire types — mirror /api/telemetry/summary response shape.
// ---------------------------------------------------------------------------

export interface RoutingDashboardToolRow {
  name: string;
  count: number;
  error_count: number;
  error_rate: number;
  avg_latency_ms: number;
  // --- Tool-accuracy panel additions (SHARED WIRE CONTRACT, NATE 2026-06-17) //
  // The agent track aggregates + emits these on each per_tool[] entry. All
  // optional here so an older summary (or one without the new aggregation)
  // renders without crashing — missing → undefined → rendered as "—" (for the
  // null-able usability/routing metrics) or 0% (success, derived from errors).
  /** 1 - error_rate. 0..1. */
  success_rate?: number;
  /** Fraction of dispatches whose RESULT was usable (0..1). null = not measured. */
  result_usability_rate?: number | null;
  /** Heuristic first-tool / sequence routing-accuracy (0..1). null = not measured. */
  routing_accuracy_rate?: number | null;
  /** Median per-dispatch latency (ms). */
  latency_p50_ms?: number;
  /** 95th-percentile per-dispatch latency (ms). */
  latency_p95_ms?: number;
}

export interface RoutingDashboardChain {
  chain: string[];
  count: number;
}

// --- by_model section (SHARED WIRE CONTRACT, NATE 2026-06-17) -------------- //
//
// Per-Bedrock-model breakdown of the SAME four accuracy metrics, so NATE can
// A/B the models the in-chat selector exposes (Sonnet 4.6 / Haiku 4.5 / Nova
// Pro / Nova Lite). The agent emits one row per distinct `model_id` seen in the
// telemetry window; legacy records with no model_id bucket under "unknown".
// Sorted by dispatch count descending ("unknown" last). Nullable usability /
// routing render as "—" exactly like the top-level metrics.
export interface RoutingDashboardModelRow {
  /** Bedrock model id (e.g. "us.anthropic.claude-sonnet-4-6") or "unknown". */
  model_id: string;
  /** Dispatch count attributed to this model. */
  count: number;
  /** 1 - error_rate for this model. 0..1. */
  success_rate: number;
  /** Fraction of this model's dispatches whose result was usable. null = n/a. */
  result_usability_rate: number | null;
  /** Heuristic routing accuracy for this model. null = n/a. */
  routing_accuracy_rate: number | null;
  /** Median per-dispatch latency for this model (ms). */
  latency_p50_ms: number;
  /** 95th-percentile per-dispatch latency for this model (ms). */
  latency_p95_ms: number;
}

// --- solve_telemetry section (SHARED WIRE CONTRACT, NATE 2026-06-17) ------- //
//
// Recent heavy-compute solves recorded by the agent's big-sim telemetry. Each
// row is one external solver run (SFINCS / MODFLOW / Pelicun on the AWS Batch
// substrate). Empty `recent` + zero percentiles when no solves have run.
export interface SolveTelemetryRow {
  run_id: string;
  solver: string;
  grid_resolution_m: number;
  active_cell_count: number;
  vcpus: number;
  wall_clock_seconds: number;
  backend: string;
  aoi_km2: number;
}

export interface SolveTelemetrySection {
  recent: SolveTelemetryRow[];
  wall_clock_p50_s: number;
  wall_clock_p95_s: number;
}

// --- recall@k section (tool-retrieval SHADOW, tool-retrieval kickoff) ------ //
//
// Recall of the tool-retrieval shadow selection: of the tools the LLM actually
// dispatched in a turn, what fraction were present in that turn's would-be-
// visible set. Surfaced so NATE can watch recall climb toward the >= 0.99/flow
// enforce gate before flipping the cloud flag. Absent on summaries emitted
// before the shadow wiring landed -> the section hides.
export interface RecallFlowRow {
  /** North-Star flow id: "SWMM" | "SFINCS" | "MODFLOW". */
  flow: string;
  /** Recall for this flow (0..1). null = no measurable dispatches yet. */
  recall: number | null;
  /** Turns attributed to this flow (dispatched its terminal solver). */
  turns: number;
  /** Dispatched llm tools across those turns. */
  dispatches: number;
  hits: number;
  misses: number;
}

export interface RecallMissedTool {
  /** Tool the LLM used that retrieval would have DROPPED. */
  name: string;
  /** Times it was missed in the window. */
  count: number;
  /** Flows it was missed under (may be empty for non-flow turns). */
  flows: string[];
}

export interface RecallAtKSection {
  /** Overall recall@k (0..1). null = no measurable turns yet. */
  overall: number | null;
  /** Turns with a shadow row AND >= 1 llm dispatch. */
  turns_measured: number;
  /** Total dispatched llm tools across measured turns. */
  dispatches_measured: number;
  hits: number;
  misses: number;
  /** The k the shadow rows were taken at (modal). null when unknown. */
  k: number | null;
  by_flow: RecallFlowRow[];
  /** Tools the LLM used that retrieval would have dropped (recall MISSES). */
  missed_tools: RecallMissedTool[];
}

export interface RoutingDashboardSummary {
  total_dispatches: number;
  session_count: number;
  error_rate_overall: number;
  cache_hit_rate: number;
  average_latency_ms: number;
  // --- Top-level tool-accuracy metrics (SHARED WIRE CONTRACT) -------------- //
  // All optional for backward compatibility with summaries emitted before the
  // accuracy aggregation landed. Null usability/routing render as "—".
  /** 1 - error_rate_overall. 0..1. */
  success_rate?: number;
  /** Overall fraction of dispatches whose result was usable (0..1). null = n/a. */
  result_usability_rate?: number | null;
  /** Overall heuristic routing accuracy (0..1). null = n/a. */
  routing_accuracy_rate?: number | null;
  /** Median per-dispatch latency across all tools (ms). */
  latency_p50_ms?: number;
  /** 95th-percentile per-dispatch latency across all tools (ms). */
  latency_p95_ms?: number;
  dispatches_by_tool: RoutingDashboardToolRow[];
  dispatches_by_source: Record<string, number>;
  error_rate_by_tool: {
    name: string;
    error_rate: number;
    error_count: number;
    total: number;
  }[];
  top_routing_chains: RoutingDashboardChain[];
  /**
   * Per-Bedrock-model accuracy breakdown (NATE 2026-06-17 model selector A/B).
   * Optional for backward compatibility with summaries emitted before the
   * model dimension landed — missing/empty → the by-model section hides.
   */
  by_model?: RoutingDashboardModelRow[];
  /** Big-sim solve telemetry (NATE 2026-06-17). Absent on older summaries. */
  solve_telemetry?: SolveTelemetrySection;
  /**
   * Tool-retrieval SHADOW recall@k (tool-retrieval kickoff). Absent on
   * summaries emitted before the shadow wiring landed -> the section hides.
   */
  recall_at_k?: RecallAtKSection;
  /** Provenance — "mongo" | "file" | "empty" | "telemetry". */
  source: string;
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface RoutingQualityDashboardProps {
  /** Dismiss handler. */
  onClose: () => void;
  /**
   * Optional pre-fetched summary (tests inject this to bypass network).
   * When present, the component skips the initial fetch and renders
   * immediately.
   */
  initialSummary?: RoutingDashboardSummary | null;
  /** Optional fetch URL override. Tests pass a stubbed URL. */
  summaryUrl?: string;
  /**
   * Optional override of the auto-refresh interval, in milliseconds.
   * Defaults to 30_000 ms. Tests pass a small value or disable via 0.
   */
  refreshIntervalMs?: number;
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const overlayStyle: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "rgba(0,0,0,0.55)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 9_500,
  fontFamily:
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
};

const cardStyle: React.CSSProperties = {
  background: "rgba(20,22,30,0.98)",
  border: "1px solid #444",
  borderRadius: 12,
  width: "min(860px, 96vw)",
  maxHeight: "90vh",
  display: "flex",
  flexDirection: "column",
  color: "#e8eaf0",
  boxShadow: "0 24px 64px rgba(0,0,0,0.55)",
  position: "relative",
  padding: "20px 22px 18px",
};

const headerRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "baseline",
  gap: 10,
  marginBottom: 12,
};

const headerTitleStyle: React.CSSProperties = {
  fontSize: 20,
  fontWeight: 600,
  margin: 0,
  color: "#e8eaf0",
};

const subtitleStyle: React.CSSProperties = {
  fontSize: 12,
  color: "#9aa0ad",
  fontWeight: 400,
};

const closeBtnStyle: React.CSSProperties = {
  position: "absolute",
  top: 12,
  right: 12,
  background: "transparent",
  border: "none",
  color: "#aaa",
  fontSize: 18,
  cursor: "pointer",
  width: 28,
  height: 28,
  borderRadius: 6,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
};

const refreshBtnStyle: React.CSSProperties = {
  marginLeft: "auto",
  background: "rgba(40,42,52,0.9)",
  border: "1px solid #555",
  borderRadius: 6,
  color: "#ddd",
  padding: "4px 10px",
  fontSize: 11,
  cursor: "pointer",
  fontFamily: "inherit",
};

const scrollBodyStyle: React.CSSProperties = {
  overflowY: "auto",
  flex: 1,
  minHeight: 0,
  paddingTop: 4,
  paddingRight: 4,
};

const kpiGridStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
  gap: 10,
  marginBottom: 18,
};

const kpiCardStyle: React.CSSProperties = {
  background: "rgba(30,32,42,0.9)",
  border: "1px solid #3a3d49",
  borderRadius: 8,
  padding: "10px 12px",
};

const kpiLabelStyle: React.CSSProperties = {
  fontSize: 10,
  color: "#9aa0ad",
  textTransform: "uppercase",
  letterSpacing: "0.06em",
  marginBottom: 6,
};

const kpiValueStyle: React.CSSProperties = {
  fontSize: 22,
  fontWeight: 600,
  color: "#dfe5f0",
  fontVariantNumeric: "tabular-nums",
};

const kpiHintStyle: React.CSSProperties = {
  fontSize: 10,
  color: "#6f7585",
  marginTop: 3,
};

const sectionHeadingStyle: React.CSSProperties = {
  fontSize: 12,
  color: "#9aa0ad",
  textTransform: "uppercase",
  letterSpacing: "0.05em",
  marginTop: 12,
  marginBottom: 8,
  borderBottom: "1px solid #2a2d35",
  paddingBottom: 6,
};

const barRowStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "180px 1fr 50px",
  alignItems: "center",
  gap: 8,
  marginBottom: 4,
  fontSize: 11,
};

const barNameStyle: React.CSSProperties = {
  fontFamily:
    "'JetBrains Mono', 'Fira Code', 'Menlo', monospace",
  color: "#cfd3dc",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const barTrackStyle: React.CSSProperties = {
  background: "rgba(40,42,52,0.5)",
  borderRadius: 3,
  height: 12,
  overflow: "hidden",
  border: "1px solid #2a2d35",
};

const barFillStyle: React.CSSProperties = {
  background: "linear-gradient(90deg, #3b82f6 0%, #6ea1f6 100%)",
  height: "100%",
};

const barCountStyle: React.CSSProperties = {
  color: "#9aa0ad",
  textAlign: "right",
  fontVariantNumeric: "tabular-nums",
};

const tableStyle: React.CSSProperties = {
  width: "100%",
  borderCollapse: "collapse",
  fontSize: 12,
};

const thStyle: React.CSSProperties = {
  textAlign: "left",
  color: "#9aa0ad",
  fontSize: 10,
  textTransform: "uppercase",
  letterSpacing: "0.05em",
  padding: "6px 4px",
  borderBottom: "1px solid #3a3d49",
};

const tdStyle: React.CSSProperties = {
  padding: "5px 4px",
  borderBottom: "1px solid #2a2d35",
  color: "#cfd3dc",
  fontVariantNumeric: "tabular-nums",
};

const chainPillStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  background: "rgba(30,32,42,0.9)",
  border: "1px solid #3a3d49",
  borderRadius: 6,
  padding: "4px 8px",
  fontSize: 11,
  marginRight: 6,
  marginBottom: 6,
};

// ---------------------------------------------------------------------------
// URL resolution
// ---------------------------------------------------------------------------

function defaultSummaryUrl(): string {
  // Use the SHARED http-base resolver (public_base.ts) so production routes the
  // telemetry fetch through the same CloudFront origin the rest of the app uses
  // (VITE_GRACE2_HTTP_URL > VITE_GRACE2_PUBLIC_BASE > <host>:8766). The previous
  // inline builder ignored VITE_GRACE2_PUBLIC_BASE and hit <cloudfront-host>:8766,
  // a port the edge does not route -> the fetch hung forever (stuck "loading").
  return httpBase() + "/api/telemetry/summary";
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatPct(rate: number): string {
  if (!Number.isFinite(rate)) return "0%";
  return `${(rate * 100).toFixed(1)}%`;
}

/**
 * Nullable-rate formatter for the accuracy metrics whose value can be genuinely
 * "not measured" (result usability / routing accuracy). null / undefined / NaN
 * render as an em-dash so the user can tell "0% measured" apart from "we have
 * no signal yet" — NEVER fabricate a 0%.
 */
function formatNullablePct(rate: number | null | undefined): string {
  if (rate === null || rate === undefined || !Number.isFinite(rate)) return "—";
  return `${(rate * 100).toFixed(1)}%`;
}

function formatMs(ms: number): string {
  if (!Number.isFinite(ms)) return "0 ms";
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)} s`;
  return `${Math.round(ms)} ms`;
}

/** Format wall-clock seconds as "Ss" or "M:SS" for the solve table. */
function formatSeconds(s: number): string {
  if (!Number.isFinite(s) || s <= 0) return "0s";
  // Round the WHOLE value first so a fractional input near a minute boundary
  // can't render "1:60" / "60s" (e.g. 119.6 -> "2:00", 59.6 -> "1:00").
  const r = Math.round(s);
  if (r < 60) return `${r}s`;
  const minutes = Math.floor(r / 60);
  const seconds = r % 60;
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

/** Abbreviate a cell count for the solve table: 46123 → "46k", 1.25e6 → "1.3M". */
function formatCells(cells: number): string {
  const n = Math.max(0, Math.floor(cells ?? 0));
  if (n >= 1_000_000) {
    const m = n / 1_000_000;
    return `${m >= 10 ? Math.round(m) : Number(m.toFixed(1))}M`;
  }
  if (n >= 1_000) {
    const k = n / 1_000;
    return `${k >= 10 ? Math.round(k) : Number(k.toFixed(1))}k`;
  }
  return `${n}`;
}

function formatCount(n: number): string {
  return Number(n ?? 0).toLocaleString();
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

type LoadState = "loading" | "ready" | "error";

export function RoutingQualityDashboard({
  onClose,
  initialSummary = null,
  summaryUrl,
  refreshIntervalMs = 30_000,
}: RoutingQualityDashboardProps): JSX.Element {
  const [summary, setSummary] = useState<RoutingDashboardSummary | null>(
    initialSummary,
  );
  const [state, setState] = useState<LoadState>(
    initialSummary ? "ready" : "loading",
  );
  const [errorText, setErrorText] = useState<string | null>(null);
  const [refreshTick, setRefreshTick] = useState<number>(0);
  const cancelRef = useRef<boolean>(false);

  // Esc to dismiss.
  useEffect(() => {
    function onKey(e: KeyboardEvent): void {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Initial + on-tick fetch.
  useEffect(() => {
    if (initialSummary && refreshTick === 0) return; // first render uses inject
    let cancelled = false;
    cancelRef.current = false;
    const url = summaryUrl ?? defaultSummaryUrl();
    // Bound the fetch so an unreachable endpoint (e.g. the agent box is asleep,
    // or /api is not routed at the edge) degrades to an HONEST error instead of
    // an infinite "loading" spinner.
    const TIMEOUT_MS = 10_000;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    (async () => {
      // Only show loading on the first fetch; subsequent ticks should not
      // blank the dashboard while fresh data is in flight.
      if (refreshTick === 0 && !initialSummary) {
        setState("loading");
      }
      try {
        const resp = await fetch(url, {
          method: "GET",
          signal: controller.signal,
        });
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}`);
        }
        const json = (await resp.json()) as RoutingDashboardSummary;
        if (cancelled) return;
        setSummary(json);
        setErrorText(null);
        setState("ready");
      } catch (err) {
        if (cancelled) return;
        const isAbort =
          err instanceof DOMException && err.name === "AbortError";
        setErrorText(
          isAbort
            ? `request timed out after ${TIMEOUT_MS / 1000}s (the agent may be asleep)`
            : err instanceof Error
              ? err.message
              : "unknown fetch error",
        );
        setState("error");
      } finally {
        clearTimeout(timer);
      }
    })();
    return () => {
      cancelled = true;
      clearTimeout(timer);
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [summaryUrl, refreshTick]);

  // Auto-refresh — 30s default.
  useEffect(() => {
    if (!refreshIntervalMs || refreshIntervalMs <= 0) return;
    const handle = setInterval(() => {
      setRefreshTick((t) => t + 1);
    }, refreshIntervalMs);
    return () => clearInterval(handle);
  }, [refreshIntervalMs]);

  const onManualRefresh = useCallback(() => {
    setRefreshTick((t) => t + 1);
  }, []);

  const topTools = useMemo<RoutingDashboardToolRow[]>(() => {
    if (!summary) return [];
    return summary.dispatches_by_tool.slice(0, 15);
  }, [summary]);

  const maxBarCount = useMemo(() => {
    if (topTools.length === 0) return 1;
    return Math.max(...topTools.map((t) => t.count), 1);
  }, [topTools]);

  const hasData =
    state === "ready" &&
    summary !== null &&
    summary.total_dispatches > 0;
  const isEmpty =
    state === "ready" &&
    summary !== null &&
    summary.total_dispatches === 0;

  return (
    <div
      data-testid="grace2-routing-dashboard"
      role="dialog"
      aria-modal="true"
      aria-label="Routing quality dashboard"
      style={overlayStyle}
      onClick={onClose}
    >
      <div
        data-testid="grace2-routing-dashboard-card"
        style={cardStyle}
        onClick={(e) => e.stopPropagation()}
      >
        <button
          data-testid="grace2-routing-dashboard-close"
          aria-label="Close routing quality dashboard"
          onClick={onClose}
          style={closeBtnStyle}
        >
          <IconClose size={18} />
        </button>

        <div style={headerRowStyle}>
          <h2 style={headerTitleStyle}>Routing quality</h2>
          <span style={subtitleStyle}>
            {summary
              ? `last ${summary.session_count} session${
                  summary.session_count === 1 ? "" : "s"
                } — source: ${summary.source}`
              : "loading..."}
          </span>
          <button
            data-testid="grace2-routing-dashboard-refresh"
            style={refreshBtnStyle}
            onClick={onManualRefresh}
            aria-label="Refresh dashboard data"
          >
            Refresh
          </button>
        </div>

        <div style={scrollBodyStyle}>
          {state === "loading" && (
            <div
              data-testid="grace2-routing-dashboard-loading"
              style={{ padding: 20, color: "#9aa0ad", fontSize: 12 }}
            >
              Loading routing-quality summary...
            </div>
          )}

          {state === "error" && (
            <div
              data-testid="grace2-routing-dashboard-error"
              style={{
                padding: 14,
                color: "#f9c1c1",
                background: "rgba(60,20,20,0.4)",
                borderRadius: 6,
                border: "1px solid #6b3030",
                fontSize: 12,
                lineHeight: 1.5,
              }}
            >
              Could not load routing-quality summary: {errorText}.
              <br />
              Make sure the agent service is running and that{" "}
              <code style={{ fontFamily: "monospace" }}>
                /api/telemetry/summary
              </code>{" "}
              is reachable.
            </div>
          )}

          {isEmpty && (
            <div
              data-testid="grace2-routing-dashboard-empty"
              style={{
                padding: 24,
                color: "#9aa0ad",
                fontSize: 12,
                textAlign: "center",
                lineHeight: 1.6,
              }}
            >
              No routing telemetry has been recorded yet.
              <br />
              Drive the agent through a few tool calls and refresh to see
              dispatch counts, error rates, and chains here.
            </div>
          )}

          {hasData && summary && (
            <>
              {/* KPI cards */}
              <div
                data-testid="grace2-routing-dashboard-kpis"
                style={kpiGridStyle}
              >
                <div
                  data-testid="grace2-routing-dashboard-kpi-total"
                  style={kpiCardStyle}
                >
                  <div style={kpiLabelStyle}>Total dispatches</div>
                  <div style={kpiValueStyle}>
                    {formatCount(summary.total_dispatches)}
                  </div>
                  <div style={kpiHintStyle}>
                    across {summary.session_count} session
                    {summary.session_count === 1 ? "" : "s"}
                  </div>
                </div>
                <div
                  data-testid="grace2-routing-dashboard-kpi-error-rate"
                  style={kpiCardStyle}
                >
                  <div style={kpiLabelStyle}>Error rate</div>
                  <div
                    style={{
                      ...kpiValueStyle,
                      color:
                        summary.error_rate_overall > 0.1
                          ? "#f9c1c1"
                          : "#dfe5f0",
                    }}
                  >
                    {formatPct(summary.error_rate_overall)}
                  </div>
                  <div style={kpiHintStyle}>
                    {summary.error_rate_by_tool.reduce(
                      (acc, r) => acc + r.error_count,
                      0,
                    )}{" "}
                    failed calls
                  </div>
                </div>
                <div
                  data-testid="grace2-routing-dashboard-kpi-cache-hit"
                  style={kpiCardStyle}
                >
                  <div style={kpiLabelStyle}>Cache hit rate</div>
                  <div style={kpiValueStyle}>
                    {formatPct(summary.cache_hit_rate)}
                  </div>
                  <div style={kpiHintStyle}>cached content tokens</div>
                </div>
                <div
                  data-testid="grace2-routing-dashboard-kpi-latency"
                  style={kpiCardStyle}
                >
                  <div style={kpiLabelStyle}>Average latency</div>
                  <div style={kpiValueStyle}>
                    {formatMs(summary.average_latency_ms)}
                  </div>
                  <div style={kpiHintStyle}>per dispatch</div>
                </div>

                {/* --- Tool-accuracy panel (NATE 2026-06-17) ----------------- */}
                {/* Success rate. Falls back to 1 - error_rate_overall when the */}
                {/* aggregation didn't supply it explicitly (derivable, not     */}
                {/* fabricated). */}
                <div
                  data-testid="grace2-routing-dashboard-kpi-success-rate"
                  style={kpiCardStyle}
                >
                  <div style={kpiLabelStyle}>Success rate</div>
                  <div style={kpiValueStyle}>
                    {formatPct(
                      summary.success_rate ?? 1 - summary.error_rate_overall,
                    )}
                  </div>
                  <div style={kpiHintStyle}>dispatches without error</div>
                </div>
                {/* Result usability — nullable: "—" when not measured. */}
                <div
                  data-testid="grace2-routing-dashboard-kpi-usability"
                  style={kpiCardStyle}
                >
                  <div style={kpiLabelStyle}>Result usability</div>
                  <div style={kpiValueStyle}>
                    {formatNullablePct(summary.result_usability_rate)}
                  </div>
                  <div style={kpiHintStyle}>usable tool results</div>
                </div>
                {/* Routing accuracy — labelled a HEURISTIC; nullable → "—". */}
                <div
                  data-testid="grace2-routing-dashboard-kpi-routing-accuracy"
                  style={kpiCardStyle}
                >
                  <div style={kpiLabelStyle}>Routing accuracy</div>
                  <div style={kpiValueStyle}>
                    {formatNullablePct(summary.routing_accuracy_rate)}
                  </div>
                  <div style={kpiHintStyle}>heuristic estimate</div>
                </div>
                {/* p50 / p95 latency — one card, both percentiles. */}
                <div
                  data-testid="grace2-routing-dashboard-kpi-latency-percentiles"
                  style={kpiCardStyle}
                >
                  <div style={kpiLabelStyle}>Latency p50 / p95</div>
                  <div style={kpiValueStyle}>
                    <span data-testid="grace2-routing-dashboard-kpi-latency-p50">
                      {formatMs(summary.latency_p50_ms ?? 0)}
                    </span>
                    <span style={{ color: "#6f7585", margin: "0 4px" }}>/</span>
                    <span data-testid="grace2-routing-dashboard-kpi-latency-p95">
                      {formatMs(summary.latency_p95_ms ?? 0)}
                    </span>
                  </div>
                  <div style={kpiHintStyle}>median / 95th percentile</div>
                </div>
              </div>

              {/* Bar chart — top 15 tools */}
              <div style={sectionHeadingStyle}>
                Top {topTools.length} tools by dispatch count
              </div>
              <div data-testid="grace2-routing-dashboard-bars">
                {topTools.map((t) => {
                  const pct = (t.count / maxBarCount) * 100;
                  return (
                    <div
                      key={t.name}
                      data-testid="grace2-routing-dashboard-bar-row"
                      data-tool-name={t.name}
                      style={barRowStyle}
                    >
                      <span style={barNameStyle} title={t.name}>
                        {t.name}
                      </span>
                      <span style={barTrackStyle}>
                        <span
                          style={{
                            ...barFillStyle,
                            width: `${Math.max(pct, 2)}%`,
                            display: "block",
                          }}
                        />
                      </span>
                      <span style={barCountStyle}>{formatCount(t.count)}</span>
                    </div>
                  );
                })}
              </div>

              {/* Per-tool stats table */}
              <div style={sectionHeadingStyle}>Per-tool stats</div>
              <table
                data-testid="grace2-routing-dashboard-table"
                style={tableStyle}
              >
                <thead>
                  <tr>
                    <th style={thStyle}>Tool</th>
                    <th style={{ ...thStyle, textAlign: "right" }}>Count</th>
                    <th style={{ ...thStyle, textAlign: "right" }}>Success</th>
                    <th style={{ ...thStyle, textAlign: "right" }}>
                      Error rate
                    </th>
                    <th style={{ ...thStyle, textAlign: "right" }}>
                      Usability
                    </th>
                    <th
                      style={{ ...thStyle, textAlign: "right" }}
                      title="Heuristic routing-accuracy estimate"
                    >
                      Routing*
                    </th>
                    <th style={{ ...thStyle, textAlign: "right" }}>p50</th>
                    <th style={{ ...thStyle, textAlign: "right" }}>p95</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.dispatches_by_tool.map((t) => (
                    <tr
                      key={t.name}
                      data-testid="grace2-routing-dashboard-table-row"
                      data-tool-name={t.name}
                    >
                      <td
                        style={{
                          ...tdStyle,
                          fontFamily:
                            "'JetBrains Mono', 'Fira Code', monospace",
                          fontSize: 11,
                          color: "#dfe5f0",
                        }}
                      >
                        {t.name}
                      </td>
                      <td style={{ ...tdStyle, textAlign: "right" }}>
                        {formatCount(t.count)}
                      </td>
                      <td
                        data-testid="grace2-routing-dashboard-cell-success"
                        style={{ ...tdStyle, textAlign: "right" }}
                      >
                        {formatPct(t.success_rate ?? 1 - t.error_rate)}
                      </td>
                      <td
                        style={{
                          ...tdStyle,
                          textAlign: "right",
                          color:
                            t.error_rate > 0.1 ? "#f9c1c1" : "#cfd3dc",
                        }}
                      >
                        {formatPct(t.error_rate)}
                      </td>
                      <td
                        data-testid="grace2-routing-dashboard-cell-usability"
                        style={{ ...tdStyle, textAlign: "right" }}
                      >
                        {formatNullablePct(t.result_usability_rate)}
                      </td>
                      <td
                        data-testid="grace2-routing-dashboard-cell-routing"
                        style={{ ...tdStyle, textAlign: "right" }}
                      >
                        {formatNullablePct(t.routing_accuracy_rate)}
                      </td>
                      <td style={{ ...tdStyle, textAlign: "right" }}>
                        {formatMs(t.latency_p50_ms ?? t.avg_latency_ms)}
                      </td>
                      <td style={{ ...tdStyle, textAlign: "right" }}>
                        {formatMs(t.latency_p95_ms ?? t.avg_latency_ms)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div
                style={{
                  ...kpiHintStyle,
                  marginTop: 4,
                }}
              >
                * Routing accuracy is a heuristic estimate. "—" = not measured.
              </div>

              {/* --- By-model comparison (NATE 2026-06-17 selector A/B) ----- */}
              {/* The SAME four accuracy metrics broken out per Bedrock model    */}
              {/* the in-chat selector exposes, so models can be A/B'd. Hidden   */}
              {/* when the summary carries no by_model section (older payloads). */}
              <div style={sectionHeadingStyle}>By model</div>
              {!summary.by_model || summary.by_model.length === 0 ? (
                <div
                  data-testid="grace2-routing-dashboard-no-models"
                  style={{
                    color: "#9aa0ad",
                    fontSize: 11,
                    fontStyle: "italic",
                    padding: "4px 0 8px",
                  }}
                >
                  No per-model telemetry recorded yet.
                </div>
              ) : (
                <table
                  data-testid="grace2-routing-dashboard-by-model-table"
                  style={tableStyle}
                >
                  <thead>
                    <tr>
                      <th style={thStyle}>Model</th>
                      <th style={{ ...thStyle, textAlign: "right" }}>Calls</th>
                      <th style={{ ...thStyle, textAlign: "right" }}>Success</th>
                      <th style={{ ...thStyle, textAlign: "right" }}>
                        Usability
                      </th>
                      <th
                        style={{ ...thStyle, textAlign: "right" }}
                        title="Heuristic routing-accuracy estimate"
                      >
                        Routing*
                      </th>
                      <th style={{ ...thStyle, textAlign: "right" }}>p50</th>
                      <th style={{ ...thStyle, textAlign: "right" }}>p95</th>
                    </tr>
                  </thead>
                  <tbody>
                    {summary.by_model.map((m) => {
                      const disp = modelDisplay(m.model_id);
                      return (
                        <tr
                          key={m.model_id}
                          data-testid="grace2-routing-dashboard-by-model-row"
                          data-model-id={m.model_id}
                        >
                          <td style={tdStyle}>
                            <span
                              style={{
                                display: "inline-block",
                                width: 8,
                                height: 8,
                                borderRadius: 2,
                                background: disp.accent,
                                marginRight: 7,
                                verticalAlign: "middle",
                              }}
                            />
                            <span style={{ color: "#dfe5f0" }}>
                              {disp.label}
                            </span>
                          </td>
                          <td style={{ ...tdStyle, textAlign: "right" }}>
                            {formatCount(m.count)}
                          </td>
                          <td
                            data-testid="grace2-routing-dashboard-by-model-cell-success"
                            style={{ ...tdStyle, textAlign: "right" }}
                          >
                            {formatPct(m.success_rate)}
                          </td>
                          <td style={{ ...tdStyle, textAlign: "right" }}>
                            {formatNullablePct(m.result_usability_rate)}
                          </td>
                          <td style={{ ...tdStyle, textAlign: "right" }}>
                            {formatNullablePct(m.routing_accuracy_rate)}
                          </td>
                          <td style={{ ...tdStyle, textAlign: "right" }}>
                            {formatMs(m.latency_p50_ms)}
                          </td>
                          <td style={{ ...tdStyle, textAlign: "right" }}>
                            {formatMs(m.latency_p95_ms)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}

              {/* --- Big-sim solve telemetry (NATE 2026-06-17) ------------- */}
              {/* Recent heavy-compute solves (SFINCS / MODFLOW / Pelicun on  */}
              {/* the external per-job substrate) + wall-clock p50/p95. When   */}
              {/* no solves have been recorded the section shows an honest     */}
              {/* empty line rather than a zeroed table.                       */}
              <div style={sectionHeadingStyle}>
                Big-sim solves
                {summary.solve_telemetry &&
                summary.solve_telemetry.recent.length > 0 ? (
                  <span
                    data-testid="grace2-routing-dashboard-solve-percentiles"
                    style={{ ...subtitleStyle, marginLeft: 8 }}
                  >
                    wall-clock p50 {formatSeconds(
                      summary.solve_telemetry.wall_clock_p50_s,
                    )}{" "}
                    / p95 {formatSeconds(summary.solve_telemetry.wall_clock_p95_s)}
                  </span>
                ) : null}
              </div>
              {!summary.solve_telemetry ||
              summary.solve_telemetry.recent.length === 0 ? (
                <div
                  data-testid="grace2-routing-dashboard-no-solves"
                  style={{
                    color: "#9aa0ad",
                    fontSize: 11,
                    fontStyle: "italic",
                    padding: "4px 0 8px",
                  }}
                >
                  No heavy-compute solves recorded yet.
                </div>
              ) : (
                <table
                  data-testid="grace2-routing-dashboard-solve-table"
                  style={tableStyle}
                >
                  <thead>
                    <tr>
                      <th style={thStyle}>Solver</th>
                      <th style={{ ...thStyle, textAlign: "right" }}>Res</th>
                      <th style={{ ...thStyle, textAlign: "right" }}>Cells</th>
                      <th style={{ ...thStyle, textAlign: "right" }}>vCPU</th>
                      <th style={{ ...thStyle, textAlign: "right" }}>
                        Wall-clock
                      </th>
                      <th style={{ ...thStyle, textAlign: "right" }}>AOI</th>
                      <th style={thStyle}>Backend</th>
                    </tr>
                  </thead>
                  <tbody>
                    {summary.solve_telemetry.recent.map((r) => (
                      <tr
                        key={r.run_id}
                        data-testid="grace2-routing-dashboard-solve-row"
                        data-run-id={r.run_id}
                      >
                        <td
                          style={{
                            ...tdStyle,
                            fontFamily:
                              "'JetBrains Mono', 'Fira Code', monospace",
                            fontSize: 11,
                            color: "#dfe5f0",
                          }}
                        >
                          {r.solver}
                        </td>
                        <td style={{ ...tdStyle, textAlign: "right" }}>
                          {r.grid_resolution_m} m
                        </td>
                        <td style={{ ...tdStyle, textAlign: "right" }}>
                          {formatCells(r.active_cell_count)}
                        </td>
                        <td style={{ ...tdStyle, textAlign: "right" }}>
                          {formatCount(r.vcpus)}
                        </td>
                        <td style={{ ...tdStyle, textAlign: "right" }}>
                          {formatSeconds(r.wall_clock_seconds)}
                        </td>
                        <td style={{ ...tdStyle, textAlign: "right" }}>
                          {Number(r.aoi_km2 ?? 0).toLocaleString(undefined, {
                            maximumFractionDigits: 1,
                          })}{" "}
                          km²
                        </td>
                        <td style={{ ...tdStyle, color: "#9aa0ad" }}>
                          {r.backend}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}

              {/* --- Tool-retrieval recall@k (tool-retrieval kickoff) ------- */}
              {/* Recall of the SHADOW selection: of the tools the LLM actually */}
              {/* used in a turn, what fraction were in that turn's would-be-    */}
              {/* visible set. Watched toward the >= 0.99/flow enforce gate.     */}
              {/* Hidden entirely when the summary carries no recall_at_k        */}
              {/* (pre-shadow-wiring payload); honest empty line when no turns   */}
              {/* have been measured (shadow mode not yet exercised).            */}
              {summary.recall_at_k && (
                <>
                  <div style={sectionHeadingStyle}>
                    Tool-retrieval recall@k
                    {summary.recall_at_k.k != null ? (
                      <span style={{ ...subtitleStyle, marginLeft: 8 }}>
                        k = {summary.recall_at_k.k}
                      </span>
                    ) : null}
                  </div>
                  {summary.recall_at_k.turns_measured === 0 ? (
                    <div
                      data-testid="grace2-routing-dashboard-no-recall"
                      style={{
                        color: "#9aa0ad",
                        fontSize: 11,
                        fontStyle: "italic",
                        padding: "4px 0 8px",
                      }}
                    >
                      No shadow-mode turns measured yet. Run the agent with
                      GRACE2_TOOL_RETRIEVAL=shadow to record recall@k.
                    </div>
                  ) : (
                    <>
                      <div
                        data-testid="grace2-routing-dashboard-recall-overall"
                        style={{
                          display: "flex",
                          alignItems: "baseline",
                          gap: 10,
                          marginBottom: 8,
                        }}
                      >
                        <span
                          style={{
                            fontSize: 22,
                            fontWeight: 600,
                            fontVariantNumeric: "tabular-nums",
                            color:
                              (summary.recall_at_k.overall ?? 0) >= 0.99
                                ? "#a7e8b0"
                                : "#dfe5f0",
                          }}
                        >
                          {formatNullablePct(summary.recall_at_k.overall)}
                        </span>
                        <span style={subtitleStyle}>
                          overall ({summary.recall_at_k.hits}/
                          {summary.recall_at_k.dispatches_measured} dispatches
                          across {summary.recall_at_k.turns_measured} turn
                          {summary.recall_at_k.turns_measured === 1 ? "" : "s"})
                        </span>
                      </div>
                      <table
                        data-testid="grace2-routing-dashboard-recall-table"
                        style={tableStyle}
                      >
                        <thead>
                          <tr>
                            <th style={thStyle}>Flow</th>
                            <th style={{ ...thStyle, textAlign: "right" }}>
                              Recall
                            </th>
                            <th style={{ ...thStyle, textAlign: "right" }}>
                              Turns
                            </th>
                            <th style={{ ...thStyle, textAlign: "right" }}>
                              Dispatches
                            </th>
                            <th style={{ ...thStyle, textAlign: "right" }}>
                              Misses
                            </th>
                          </tr>
                        </thead>
                        <tbody>
                          {summary.recall_at_k.by_flow.map((f) => (
                            <tr
                              key={f.flow}
                              data-testid="grace2-routing-dashboard-recall-flow-row"
                              data-flow={f.flow}
                            >
                              <td
                                style={{
                                  ...tdStyle,
                                  fontFamily:
                                    "'JetBrains Mono', 'Fira Code', monospace",
                                  fontSize: 11,
                                  color: "#dfe5f0",
                                }}
                              >
                                {f.flow}
                              </td>
                              <td
                                data-testid="grace2-routing-dashboard-recall-flow-cell"
                                style={{
                                  ...tdStyle,
                                  textAlign: "right",
                                  color:
                                    f.recall != null && f.recall >= 0.99
                                      ? "#a7e8b0"
                                      : "#cfd3dc",
                                }}
                              >
                                {formatNullablePct(f.recall)}
                              </td>
                              <td style={{ ...tdStyle, textAlign: "right" }}>
                                {formatCount(f.turns)}
                              </td>
                              <td style={{ ...tdStyle, textAlign: "right" }}>
                                {formatCount(f.dispatches)}
                              </td>
                              <td
                                style={{
                                  ...tdStyle,
                                  textAlign: "right",
                                  color: f.misses > 0 ? "#f9c1c1" : "#cfd3dc",
                                }}
                              >
                                {formatCount(f.misses)}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>

                      {/* Missed-tool list -- the tools the LLM used that      */}
                      {/* retrieval would have DROPPED. The actionable signal   */}
                      {/* for tuning before enforce.                            */}
                      <div
                        style={{
                          ...sectionHeadingStyle,
                          textTransform: "none",
                          fontSize: 11,
                          marginTop: 10,
                        }}
                      >
                        Missed tools (retrieval would have dropped)
                      </div>
                      {summary.recall_at_k.missed_tools.length === 0 ? (
                        <div
                          data-testid="grace2-routing-dashboard-no-missed-tools"
                          style={{
                            color: "#a7e8b0",
                            fontSize: 11,
                            padding: "4px 0 8px",
                          }}
                        >
                          None — every dispatched tool was in its turn's
                          retrieved set.
                        </div>
                      ) : (
                        <div
                          data-testid="grace2-routing-dashboard-missed-tools"
                          style={{ paddingBottom: 8 }}
                        >
                          {summary.recall_at_k.missed_tools.map((m) => (
                            <span
                              key={m.name}
                              data-testid="grace2-routing-dashboard-missed-tool"
                              data-tool-name={m.name}
                              style={{
                                ...chainPillStyle,
                                borderColor: "#6b3030",
                              }}
                            >
                              <code
                                style={{
                                  fontFamily:
                                    "'JetBrains Mono', 'Fira Code', monospace",
                                  color: "#f9c1c1",
                                }}
                              >
                                {m.name}
                              </code>
                              <span style={{ color: "#9aa0ad" }}>
                                × {m.count}
                                {m.flows.length > 0
                                  ? ` (${m.flows.join(", ")})`
                                  : ""}
                              </span>
                            </span>
                          ))}
                        </div>
                      )}
                    </>
                  )}
                </>
              )}

              {/* Recent chains */}
              <div style={sectionHeadingStyle}>Top routing chains</div>
              {summary.top_routing_chains.length === 0 ? (
                <div
                  data-testid="grace2-routing-dashboard-no-chains"
                  style={{
                    color: "#9aa0ad",
                    fontSize: 11,
                    fontStyle: "italic",
                    padding: "4px 0 8px",
                  }}
                >
                  No multi-tool sequences recorded yet.
                </div>
              ) : (
                <div data-testid="grace2-routing-dashboard-chains">
                  {summary.top_routing_chains.map((c, i) => (
                    <span
                      key={`chain-${i}`}
                      data-testid="grace2-routing-dashboard-chain"
                      style={chainPillStyle}
                    >
                      <code
                        style={{
                          fontFamily:
                            "'JetBrains Mono', 'Fira Code', monospace",
                          color: "#dfe5f0",
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 2,
                        }}
                      >
                        {c.chain.map((step, si) => (
                          <span
                            key={`step-${si}`}
                            style={{ display: "inline-flex", alignItems: "center", gap: 2 }}
                          >
                            {si > 0 && (
                              <IconChevronRight size={11} color="#9aa0ad" />
                            )}
                            <span>{step}</span>
                          </span>
                        ))}
                      </code>
                      <span style={{ color: "#9aa0ad" }}>× {c.count}</span>
                    </span>
                  ))}
                </div>
              )}

              {/* Sources mix — a small inline rendering of llm/workflow split. */}
              <div style={sectionHeadingStyle}>Dispatch sources</div>
              <div
                data-testid="grace2-routing-dashboard-sources"
                style={{
                  display: "flex",
                  gap: 16,
                  fontSize: 11,
                  color: "#cfd3dc",
                  paddingBottom: 12,
                }}
              >
                {Object.entries(summary.dispatches_by_source).map(
                  ([source, count]) => (
                    <span
                      key={source}
                      data-testid={`grace2-routing-dashboard-source-${source}`}
                    >
                      <span style={{ color: "#9aa0ad" }}>{source}: </span>
                      <span style={{ fontWeight: 600 }}>
                        {formatCount(count)}
                      </span>
                    </span>
                  ),
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
