// GRACE-2 web — ImpactPanel (Wave 4.11 P4).
//
// Slide-out side panel that renders the ``ImpactEnvelope`` produced by the
// ``compute_impact_envelope`` workflow (Wave 4.11 P3). The agent emits this
// envelope inline in its function_response payload under
// ``result.raw_envelope`` — the Chat / App layer routes it into the
// ``impactEnvelope`` state hook in ``App.tsx``, mirroring the
// ``toolsCatalogOpen`` pattern from Wave 4.10 C1.
//
// SRS reference: Appendix B.6c (``packages/contracts/.../impact_envelope.py``).
//
// Surface (slide-out from the right edge):
//
//   +-----------------------------------------------+
//   |  Impact Summary — Fort Myers · 2026-06-09  ✕  |
//   |                                               |
//   |  [n_structures_damaged] [expected_loss_usd]   |
//   |  [population_displaced]  [impact_area_km2]    |
//   |                                               |
//   |  Damage state distribution                    |
//   |   DS0  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓                       |
//   |   DS1  ▓▓▓▓▓▓                                 |
//   |   DS2  ▓▓                                     |
//   |   DS3  ▓                                      |
//   |   DS4  ▓                                      |
//   |                                               |
//   |  Per-occupancy-class breakdown                |
//   |  +-------+-------+--------+--------+--------+ |
//   |  | Class | Total | Damaged| $ Loss | Pop    | |
//   |  | RES1  |  1234 |   210  | $12.4M |  3210  | |
//   |  | COM1  |    78 |    15  |  $4.2M |   N/A  | |
//   |  +-------+-------+--------+--------+--------+ |
//   |                                               |
//   |  ▼ Provenance                                 |
//   |     pelicun_run_id: 01HF...                   |
//   |     [USACE_NSI]                               |
//   |     damage_layer_uri: gs://...                |
//   |     flood_layer_uri:  gs://...                |
//   +-----------------------------------------------+
//
// Accessibility:
//   - ``aria-live="polite"`` on the panel; terminal transitions announced.
//   - ``prefers-reduced-motion``: slide-in animation falls back to static.
//   - Close button keyboard-focusable; Esc dismisses.

import { useCallback, useEffect, useState } from "react";
import { IconClose } from "./icons";

// ---------------------------------------------------------------------------
// ImpactEnvelope wire shape (mirrors B.6c — kept narrow / forward-compatible).
// ---------------------------------------------------------------------------

export type DamageStateKey =
  | "DS0_none"
  | "DS1_slight"
  | "DS2_moderate"
  | "DS3_extensive"
  | "DS4_complete";

export type StructureInventorySource =
  | "USACE_NSI"
  | "MS_BUILDINGS"
  | "USER_SUPPLIED";

export interface OccupancyClassImpact {
  n_structures: number;
  n_damaged: number;
  n_destroyed: number;
  expected_loss_usd: number;
  loss_percentile_95_usd: number;
  population?: number | null;
  population_displaced?: number | null;
}

export interface ImpactEnvelope {
  schema_version?: "v1";
  // Damage statistics
  n_structures_total: number;
  n_structures_damaged: number;
  n_structures_destroyed: number;
  damage_state_distribution: Partial<Record<DamageStateKey, number>>;
  // Loss statistics
  total_replacement_value_usd: number;
  damaged_replacement_value_usd: number;
  expected_loss_usd: number;
  loss_percentile_95_usd: number;
  // Population
  population_total?: number | null;
  population_displaced?: number | null;
  population_at_high_risk?: number | null;
  // Spatial summary
  impact_area_km2: number;
  bbox: [number, number, number, number];
  // Per-occupancy
  by_occupancy_class: Record<string, OccupancyClassImpact>;
  // Provenance
  pelicun_run_id: string;
  damage_layer_uri: string;
  structure_inventory_source: StructureInventorySource;
  flood_layer_uri: string;
  fragility_set: string;
  realization_count: number;
  generated_at: string;
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ImpactPanelProps {
  envelope: ImpactEnvelope;
  /** Optional Case name surfaced in the header. */
  caseName?: string | null;
  /** Dismiss handler. */
  onClose: () => void;
}

// ---------------------------------------------------------------------------
// Reduced-motion detection (SSR-safe).
// ---------------------------------------------------------------------------

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Number formatters.
// ---------------------------------------------------------------------------

function formatInt(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n.toLocaleString("en-US");
}

function formatUSD(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  if (n >= 1_000_000_000) return `$${(n / 1_000_000_000).toFixed(2)}B`;
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
}

function formatKm2(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  if (n >= 100) return `${n.toFixed(0)} km²`;
  if (n >= 10) return `${n.toFixed(1)} km²`;
  return `${n.toFixed(2)} km²`;
}

function truncateUlid(ulid: string): string {
  // ULIDs are 26 chars; show first 8 + ellipsis + last 4.
  if (ulid.length <= 14) return ulid;
  return `${ulid.slice(0, 8)}…${ulid.slice(-4)}`;
}

function formatTimestamp(iso: string): string {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toISOString().replace("T", " ").slice(0, 19) + " UTC";
  } catch {
    return iso;
  }
}

// ---------------------------------------------------------------------------
// Damage-state ordering + labels.
// ---------------------------------------------------------------------------

const DAMAGE_STATE_ORDER: DamageStateKey[] = [
  "DS0_none",
  "DS1_slight",
  "DS2_moderate",
  "DS3_extensive",
  "DS4_complete",
];

const DAMAGE_STATE_LABELS: Record<DamageStateKey, string> = {
  DS0_none: "DS0 · None",
  DS1_slight: "DS1 · Slight",
  DS2_moderate: "DS2 · Moderate",
  DS3_extensive: "DS3 · Extensive",
  DS4_complete: "DS4 · Complete",
};

const DAMAGE_STATE_COLORS: Record<DamageStateKey, string> = {
  DS0_none: "#3a8f53",
  DS1_slight: "#90c659",
  DS2_moderate: "#e6c34a",
  DS3_extensive: "#e69045",
  DS4_complete: "#d44a4a",
};

// ---------------------------------------------------------------------------
// Styles (dark theme — matches SettingsPopup / ToolsCatalogPopup chrome).
// ---------------------------------------------------------------------------

const backdropStyle: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "rgba(0,0,0,0.35)",
  zIndex: 9_400,
  display: "block",
  fontFamily:
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
};

const panelBaseStyle: React.CSSProperties = {
  position: "fixed",
  top: 0,
  right: 0,
  bottom: 0,
  width: "min(520px, 96vw)",
  background: "rgba(20,22,30,0.98)",
  borderLeft: "1px solid #444",
  color: "#e8eaf0",
  boxShadow: "-12px 0 32px rgba(0,0,0,0.45)",
  zIndex: 9_450,
  display: "flex",
  flexDirection: "column",
  fontFamily:
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
};

const headerStyle: React.CSSProperties = {
  padding: "16px 20px 12px",
  borderBottom: "1px solid #333",
  display: "flex",
  alignItems: "baseline",
  justifyContent: "space-between",
  gap: 12,
};

const headerTitleStyle: React.CSSProperties = {
  fontSize: 16,
  fontWeight: 600,
  color: "#e8eaf0",
  margin: 0,
};

const headerSubtitleStyle: React.CSSProperties = {
  fontSize: 11,
  color: "#9aa0ad",
  marginTop: 2,
};

const closeBtnStyle: React.CSSProperties = {
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

const bodyStyle: React.CSSProperties = {
  padding: "16px 20px 20px",
  overflowY: "auto",
  flex: 1,
  minHeight: 0,
};

const sectionHeadingStyle: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 600,
  letterSpacing: "0.08em",
  textTransform: "uppercase",
  color: "#9aa0ad",
  margin: "20px 0 8px",
};

const headlineGridStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(2, 1fr)",
  gap: 10,
};

const statCardStyle: React.CSSProperties = {
  background: "rgba(30,32,42,0.9)",
  border: "1px solid #3a3d49",
  borderRadius: 8,
  padding: "12px 14px",
  display: "flex",
  flexDirection: "column",
  gap: 4,
};

const statLabelStyle: React.CSSProperties = {
  fontSize: 10,
  fontWeight: 500,
  letterSpacing: "0.06em",
  textTransform: "uppercase",
  color: "#869aae",
};

const statValueStyle: React.CSSProperties = {
  fontSize: 22,
  fontWeight: 600,
  color: "#e8eaf0",
  fontFamily: "'JetBrains Mono', 'Fira Code', 'Menlo', monospace",
};

const statSubvalueStyle: React.CSSProperties = {
  fontSize: 11,
  color: "#9aa0ad",
  marginTop: 2,
};

const dsRowStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "120px 1fr 60px",
  gap: 10,
  alignItems: "center",
  marginBottom: 6,
  fontSize: 11,
};

const dsLabelStyle: React.CSSProperties = {
  color: "#cfd3dc",
};

const dsBarTrackStyle: React.CSSProperties = {
  background: "rgba(15,15,20,0.6)",
  borderRadius: 4,
  height: 14,
  overflow: "hidden",
  display: "block",
  position: "relative",
};

const dsBarFillStyle = (color: string, pct: number): React.CSSProperties => ({
  background: color,
  width: `${pct}%`,
  height: "100%",
  borderRadius: 4,
  minWidth: pct > 0 ? 2 : 0,
  display: "block",
  transition: "width 200ms ease-out",
});

const dsCountStyle: React.CSSProperties = {
  color: "#cfd3dc",
  fontFamily: "'JetBrains Mono', 'Fira Code', 'Menlo', monospace",
  textAlign: "right",
};

const tableStyle: React.CSSProperties = {
  width: "100%",
  borderCollapse: "collapse",
  fontSize: 11,
};

const thStyle: React.CSSProperties = {
  textAlign: "left",
  fontWeight: 500,
  color: "#869aae",
  padding: "6px 8px",
  borderBottom: "1px solid #333",
  letterSpacing: "0.04em",
  textTransform: "uppercase",
  fontSize: 10,
};

const tdStyle: React.CSSProperties = {
  padding: "8px 8px",
  borderBottom: "1px solid #2a2d35",
  color: "#cfd3dc",
};

const tdNumericStyle: React.CSSProperties = {
  ...tdStyle,
  fontFamily: "'JetBrains Mono', 'Fira Code', 'Menlo', monospace",
  textAlign: "right",
};

const provBlockStyle: React.CSSProperties = {
  marginTop: 24,
  padding: 12,
  borderTop: "1px solid #333",
  background: "rgba(15,15,20,0.55)",
  borderRadius: 6,
};

const provBadgeStyle = (source: StructureInventorySource): React.CSSProperties => {
  const colours: Record<StructureInventorySource, { bg: string; color: string; border: string }> = {
    USACE_NSI: { bg: "rgba(40,90,140,0.55)", color: "#cbd9f0", border: "#3b6fa8" },
    MS_BUILDINGS: { bg: "rgba(80,40,140,0.55)", color: "#d7c7f5", border: "#7757b8" },
    USER_SUPPLIED: { bg: "rgba(140,90,40,0.55)", color: "#f5d7b2", border: "#a87633" },
  };
  const c = colours[source];
  return {
    display: "inline-block",
    padding: "3px 8px",
    borderRadius: 4,
    fontSize: 10,
    fontWeight: 600,
    letterSpacing: "0.05em",
    background: c.bg,
    color: c.color,
    border: `1px solid ${c.border}`,
    fontFamily: "'JetBrains Mono', 'Fira Code', 'Menlo', monospace",
  };
};

const provLineStyle: React.CSSProperties = {
  fontSize: 11,
  color: "#aab0bd",
  marginTop: 6,
  display: "flex",
  gap: 6,
  alignItems: "baseline",
};

const provKeyStyle: React.CSSProperties = {
  color: "#869aae",
  letterSpacing: "0.04em",
  textTransform: "uppercase",
  fontSize: 9,
  fontWeight: 600,
  minWidth: 110,
};

const provValueStyle: React.CSSProperties = {
  fontFamily: "'JetBrains Mono', 'Fira Code', 'Menlo', monospace",
  fontSize: 11,
  color: "#cfd3dc",
  wordBreak: "break-all",
};

const provDetailsSummaryStyle: React.CSSProperties = {
  cursor: "pointer",
  fontSize: 11,
  color: "#7aa7ff",
  marginTop: 10,
  outline: "none",
};

// ---------------------------------------------------------------------------
// Animation keyframes — slide-in from right.
// ---------------------------------------------------------------------------

const KEYFRAMES_ID = "grace2-impact-panel-keyframes";

function ensureKeyframes(): void {
  if (typeof document === "undefined") return;
  if (document.getElementById(KEYFRAMES_ID)) return;
  const style = document.createElement("style");
  style.id = KEYFRAMES_ID;
  style.textContent = `
    @keyframes grace2-impact-slide-in {
      from { transform: translateX(100%); }
      to   { transform: translateX(0); }
    }
    @keyframes grace2-impact-fade-in {
      from { opacity: 0; }
      to   { opacity: 1; }
    }
  `;
  document.head.appendChild(style);
}

// ---------------------------------------------------------------------------
// Sort occupancy classes — biggest impact first (n_damaged desc, fallback name).
// ---------------------------------------------------------------------------

function sortOccupancyClasses(
  by: Record<string, OccupancyClassImpact>,
): Array<[string, OccupancyClassImpact]> {
  return Object.entries(by).sort((a, b) => {
    const dDiff = b[1].n_damaged - a[1].n_damaged;
    if (dDiff !== 0) return dDiff;
    return a[0].localeCompare(b[0]);
  });
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ImpactPanel({
  envelope,
  caseName,
  onClose,
}: ImpactPanelProps): JSX.Element {
  const [reduced] = useState<boolean>(prefersReducedMotion);

  // Inject keyframes once.
  useEffect(() => {
    ensureKeyframes();
  }, []);

  // Esc to dismiss.
  useEffect(() => {
    function onKey(e: KeyboardEvent): void {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const handleBackdropClick = useCallback(() => {
    onClose();
  }, [onClose]);

  const hasPopulation =
    envelope.population_total !== null &&
    envelope.population_total !== undefined;

  // Damage-state distribution — total for normalization (sum of values).
  const dsValues = envelope.damage_state_distribution ?? {};
  const dsTotal = DAMAGE_STATE_ORDER.reduce(
    (acc, k) => acc + (dsValues[k] ?? 0),
    0,
  );

  const occupancyRows = sortOccupancyClasses(envelope.by_occupancy_class ?? {});

  // Panel style with animation only when motion is allowed.
  const panelStyle: React.CSSProperties = reduced
    ? panelBaseStyle
    : {
        ...panelBaseStyle,
        animation: "grace2-impact-slide-in 240ms cubic-bezier(0.16, 1, 0.3, 1)",
      };

  const backdropAnimatedStyle: React.CSSProperties = reduced
    ? backdropStyle
    : {
        ...backdropStyle,
        animation: "grace2-impact-fade-in 200ms ease-out",
      };

  return (
    <>
      <div
        data-testid="grace2-impact-panel-backdrop"
        aria-hidden="true"
        style={backdropAnimatedStyle}
        onClick={handleBackdropClick}
      />
      <aside
        data-testid="grace2-impact-panel"
        data-reduced-motion={reduced ? "true" : "false"}
        role="complementary"
        aria-label="Impact summary"
        aria-live="polite"
        style={panelStyle}
        onClick={(e) => e.stopPropagation()}
      >
        <header style={headerStyle}>
          <div>
            <h2 style={headerTitleStyle} data-testid="grace2-impact-panel-title">
              Impact summary
            </h2>
            <div style={headerSubtitleStyle}>
              {caseName ? <span>{caseName} · </span> : null}
              <span
                data-testid="grace2-impact-panel-timestamp"
                title={envelope.generated_at}
              >
                {formatTimestamp(envelope.generated_at)}
              </span>
            </div>
          </div>
          <button
            data-testid="grace2-impact-panel-close"
            aria-label="Close impact summary"
            onClick={onClose}
            style={closeBtnStyle}
          >
            <IconClose size={18} />
          </button>
        </header>

        <div style={bodyStyle}>
          {/* Headline stat cards. */}
          <div
            data-testid="grace2-impact-panel-headline-grid"
            style={headlineGridStyle}
          >
            <div data-testid="grace2-impact-stat-structures" style={statCardStyle}>
              <span style={statLabelStyle}>Structures damaged</span>
              <span style={statValueStyle}>
                {formatInt(envelope.n_structures_damaged)}
              </span>
              <span style={statSubvalueStyle}>
                of {formatInt(envelope.n_structures_total)} assessed
              </span>
            </div>

            <div data-testid="grace2-impact-stat-loss" style={statCardStyle}>
              <span style={statLabelStyle}>Expected loss</span>
              <span style={statValueStyle}>
                {formatUSD(envelope.expected_loss_usd)}
              </span>
              <span style={statSubvalueStyle}>
                P95: {formatUSD(envelope.loss_percentile_95_usd)}
              </span>
            </div>

            {hasPopulation && (
              <div
                data-testid="grace2-impact-stat-population"
                style={statCardStyle}
              >
                <span style={statLabelStyle}>Population displaced</span>
                <span style={statValueStyle}>
                  {formatInt(envelope.population_displaced)}
                </span>
                <span style={statSubvalueStyle}>
                  high risk: {formatInt(envelope.population_at_high_risk)}
                </span>
              </div>
            )}

            <div data-testid="grace2-impact-stat-area" style={statCardStyle}>
              <span style={statLabelStyle}>Impact area</span>
              <span style={statValueStyle}>
                {formatKm2(envelope.impact_area_km2)}
              </span>
              <span style={statSubvalueStyle}>damaged-asset hull</span>
            </div>
          </div>

          {/* Damage-state distribution. */}
          <h3 style={sectionHeadingStyle}>Damage state distribution</h3>
          <div data-testid="grace2-impact-ds-distribution">
            {DAMAGE_STATE_ORDER.map((k) => {
              const count = dsValues[k] ?? 0;
              const pct = dsTotal > 0 ? (count / dsTotal) * 100 : 0;
              return (
                <div
                  key={k}
                  data-testid={`grace2-impact-ds-row-${k}`}
                  style={dsRowStyle}
                >
                  <span style={dsLabelStyle}>{DAMAGE_STATE_LABELS[k]}</span>
                  <span style={dsBarTrackStyle}>
                    <span
                      data-testid={`grace2-impact-ds-bar-${k}`}
                      style={dsBarFillStyle(DAMAGE_STATE_COLORS[k], pct)}
                    />
                  </span>
                  <span style={dsCountStyle}>{formatInt(count)}</span>
                </div>
              );
            })}
          </div>

          {/* Per-occupancy table. */}
          <h3 style={sectionHeadingStyle}>By occupancy class</h3>
          <table
            data-testid="grace2-impact-occupancy-table"
            style={tableStyle}
          >
            <thead>
              <tr>
                <th style={thStyle}>Class</th>
                <th style={{ ...thStyle, textAlign: "right" }}>Total</th>
                <th style={{ ...thStyle, textAlign: "right" }}>Damaged</th>
                <th style={{ ...thStyle, textAlign: "right" }}>Loss</th>
                <th style={{ ...thStyle, textAlign: "right" }}>Pop</th>
              </tr>
            </thead>
            <tbody>
              {occupancyRows.length === 0 && (
                <tr>
                  <td
                    style={{ ...tdStyle, textAlign: "center", color: "#869aae" }}
                    colSpan={5}
                    data-testid="grace2-impact-occupancy-empty"
                  >
                    No occupancy classes in this assessment.
                  </td>
                </tr>
              )}
              {occupancyRows.map(([cls, impact]) => (
                <tr
                  key={cls}
                  data-testid={`grace2-impact-occupancy-row-${cls}`}
                >
                  <td style={tdStyle}>
                    <span
                      style={{
                        fontFamily:
                          "'JetBrains Mono', 'Fira Code', 'Menlo', monospace",
                        fontWeight: 600,
                      }}
                    >
                      {cls}
                    </span>
                  </td>
                  <td style={tdNumericStyle}>{formatInt(impact.n_structures)}</td>
                  <td style={tdNumericStyle}>{formatInt(impact.n_damaged)}</td>
                  <td style={tdNumericStyle}>
                    {formatUSD(impact.expected_loss_usd)}
                  </td>
                  <td style={tdNumericStyle}>
                    {impact.population === null ||
                    impact.population === undefined
                      ? "—"
                      : formatInt(impact.population)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {/* Provenance footer. */}
          <div
            data-testid="grace2-impact-provenance"
            style={provBlockStyle}
          >
            <div style={provLineStyle}>
              <span style={provKeyStyle}>Run ID</span>
              <span
                style={provValueStyle}
                data-testid="grace2-impact-provenance-runid"
                title={envelope.pelicun_run_id}
              >
                {truncateUlid(envelope.pelicun_run_id)}
              </span>
              <span
                data-testid="grace2-impact-provenance-source-badge"
                style={provBadgeStyle(envelope.structure_inventory_source)}
                title={`Structure inventory source: ${envelope.structure_inventory_source}`}
              >
                {envelope.structure_inventory_source}
              </span>
            </div>
            <div style={provLineStyle}>
              <span style={provKeyStyle}>Fragility set</span>
              <span style={provValueStyle}>{envelope.fragility_set}</span>
            </div>
            <div style={provLineStyle}>
              <span style={provKeyStyle}>Realizations</span>
              <span style={provValueStyle}>
                {formatInt(envelope.realization_count)}
              </span>
            </div>

            <details>
              <summary style={provDetailsSummaryStyle}>
                Source LayerURIs
              </summary>
              <div
                data-testid="grace2-impact-provenance-uris"
                style={{ paddingLeft: 4, marginTop: 6 }}
              >
                <div style={provLineStyle}>
                  <span style={provKeyStyle}>Damage layer</span>
                  <span style={provValueStyle}>{envelope.damage_layer_uri}</span>
                </div>
                <div style={provLineStyle}>
                  <span style={provKeyStyle}>Flood layer</span>
                  <span style={provValueStyle}>{envelope.flood_layer_uri}</span>
                </div>
                <div style={provLineStyle}>
                  <span style={provKeyStyle}>BBox</span>
                  <span style={provValueStyle}>
                    [{envelope.bbox.map((n) => n.toFixed(4)).join(", ")}]
                  </span>
                </div>
              </div>
            </details>
          </div>
        </div>
      </aside>
    </>
  );
}
