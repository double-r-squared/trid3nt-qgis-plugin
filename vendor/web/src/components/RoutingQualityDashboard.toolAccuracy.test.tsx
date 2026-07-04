// GRACE-2 web — RoutingQualityDashboard tool-accuracy + solve-telemetry tests
// (NATE 2026-06-17, tool-accuracy panel + live big-sim readout).
//
// Verifies the SHARED WIRE CONTRACT additions on /api/telemetry/summary:
//   1. The FOUR accuracy KPI cards render — Success rate, Result usability,
//      Routing accuracy (heuristic), p50/p95 latency.
//   2. Null usability/routing render as "—", NOT 0%.
//   3. The per-tool table gains Success / Usability / Routing* / p50 / p95
//      columns, with null per-tool metrics rendering "—".
//   4. The solve_telemetry section renders a compact recent-solves table
//      (resolution / cells / vCPU / wall-clock) + the wall-clock p50/p95.
//   5. Empty / absent solve_telemetry shows the honest empty line, not zeros.

import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";

import {
  RoutingQualityDashboard,
  type RoutingDashboardSummary,
} from "./RoutingQualityDashboard";

afterEach(() => {
  cleanup();
});

// A summary carrying the full accuracy surface PLUS a deliberate null on the
// usability/routing metrics (both top-level and on one per-tool row) so we can
// assert the "—" rendering. The agent track emits these fields; the web side
// only renders them.
const SUMMARY_WITH_ACCURACY: RoutingDashboardSummary = {
  total_dispatches: 100,
  session_count: 8,
  error_rate_overall: 0.1,
  cache_hit_rate: 0.9,
  average_latency_ms: 300,
  success_rate: 0.9,
  result_usability_rate: 0.82,
  routing_accuracy_rate: 0.74,
  latency_p50_ms: 210,
  latency_p95_ms: 1450,
  dispatches_by_tool: [
    {
      name: "run_model_flood_scenario",
      count: 12,
      error_count: 1,
      error_rate: 0.083,
      avg_latency_ms: 42000,
      success_rate: 0.917,
      result_usability_rate: 0.9,
      routing_accuracy_rate: 0.8,
      latency_p50_ms: 41000,
      latency_p95_ms: 73000,
    },
    {
      // This row carries NULL usability + routing → must render "—".
      name: "fetch_dem",
      count: 30,
      error_count: 0,
      error_rate: 0.0,
      avg_latency_ms: 300,
      success_rate: 1.0,
      result_usability_rate: null,
      routing_accuracy_rate: null,
      latency_p50_ms: 250,
      latency_p95_ms: 900,
    },
  ],
  dispatches_by_source: { llm: 80, workflow: 20 },
  error_rate_by_tool: [],
  top_routing_chains: [],
  by_model: [
    {
      // Highest-use model first (sorted by count desc on the agent side).
      model_id: "us.anthropic.claude-sonnet-4-6",
      count: 70,
      success_rate: 0.93,
      result_usability_rate: 0.85,
      routing_accuracy_rate: 0.78,
      latency_p50_ms: 200,
      latency_p95_ms: 1400,
    },
    {
      model_id: "us.amazon.nova-lite-v1:0",
      count: 30,
      success_rate: 0.83,
      // Nova-lite row carries NULL usability + routing → must render "—".
      result_usability_rate: null,
      routing_accuracy_rate: null,
      latency_p50_ms: 90,
      latency_p95_ms: 410,
    },
    {
      // Legacy / pre-feature records bucket under "unknown".
      model_id: "unknown",
      count: 5,
      success_rate: 1.0,
      result_usability_rate: 1.0,
      routing_accuracy_rate: 1.0,
      latency_p50_ms: 150,
      latency_p95_ms: 300,
    },
  ],
  solve_telemetry: {
    recent: [
      {
        run_id: "run-sfincs-001",
        solver: "SFINCS",
        grid_resolution_m: 100,
        active_cell_count: 46000,
        vcpus: 8,
        wall_clock_seconds: 72,
        backend: "aws-batch",
        aoi_km2: 215.4,
      },
      {
        run_id: "run-modflow-002",
        solver: "MODFLOW",
        grid_resolution_m: 250,
        active_cell_count: 1_250_000,
        vcpus: 16,
        wall_clock_seconds: 540,
        backend: "aws-batch",
        aoi_km2: 980,
      },
    ],
    wall_clock_p50_s: 120,
    wall_clock_p95_s: 540,
  },
  source: "mongo",
};

// A summary WITHOUT the accuracy aggregation + with null top-level metrics, to
// prove the dashboard degrades gracefully (— for nullable, derived success).
const SUMMARY_NULL_METRICS: RoutingDashboardSummary = {
  total_dispatches: 50,
  session_count: 4,
  error_rate_overall: 0.2,
  cache_hit_rate: 0.5,
  average_latency_ms: 400,
  // success_rate intentionally omitted → derived as 1 - 0.2 = 80.0%.
  result_usability_rate: null,
  routing_accuracy_rate: null,
  latency_p50_ms: 380,
  latency_p95_ms: 1200,
  dispatches_by_tool: [
    {
      name: "fetch_buildings",
      count: 50,
      error_count: 10,
      error_rate: 0.2,
      avg_latency_ms: 400,
      // no success/usability/routing → success derived, others "—".
    },
  ],
  dispatches_by_source: { llm: 50 },
  error_rate_by_tool: [],
  top_routing_chains: [],
  // solve_telemetry intentionally absent → honest empty line.
  source: "file",
};

describe("RoutingQualityDashboard — tool-accuracy KPIs", () => {
  it("renders the four accuracy metric cards from the summary", () => {
    render(
      <RoutingQualityDashboard
        onClose={() => undefined}
        initialSummary={SUMMARY_WITH_ACCURACY}
        refreshIntervalMs={0}
      />,
    );
    // Success rate — 90.0%.
    expect(
      screen.getByTestId("grace2-routing-dashboard-kpi-success-rate")
        .textContent,
    ).toMatch(/90\.0%/);
    // Result usability — 82.0%.
    expect(
      screen.getByTestId("grace2-routing-dashboard-kpi-usability").textContent,
    ).toMatch(/82\.0%/);
    // Routing accuracy — 74.0% + labelled a heuristic.
    const routing = screen.getByTestId(
      "grace2-routing-dashboard-kpi-routing-accuracy",
    );
    expect(routing.textContent).toMatch(/74\.0%/);
    expect(routing.textContent?.toLowerCase()).toContain("heuristic");
    // p50 / p95 latency — 210 ms / 1.45 s.
    expect(
      screen.getByTestId("grace2-routing-dashboard-kpi-latency-p50")
        .textContent,
    ).toMatch(/210 ms/);
    expect(
      screen.getByTestId("grace2-routing-dashboard-kpi-latency-p95")
        .textContent,
    ).toMatch(/1\.45 s/);
  });

  it("renders null usability + routing as an em-dash, never 0%", () => {
    render(
      <RoutingQualityDashboard
        onClose={() => undefined}
        initialSummary={SUMMARY_NULL_METRICS}
        refreshIntervalMs={0}
      />,
    );
    expect(
      screen.getByTestId("grace2-routing-dashboard-kpi-usability").textContent,
    ).toContain("—");
    expect(
      screen.getByTestId("grace2-routing-dashboard-kpi-routing-accuracy")
        .textContent,
    ).toContain("—");
    // Neither should fabricate a 0% reading.
    expect(
      screen.getByTestId("grace2-routing-dashboard-kpi-usability").textContent,
    ).not.toMatch(/0\.0%/);
  });

  it("derives success rate from error rate when not supplied (80.0%)", () => {
    render(
      <RoutingQualityDashboard
        onClose={() => undefined}
        initialSummary={SUMMARY_NULL_METRICS}
        refreshIntervalMs={0}
      />,
    );
    expect(
      screen.getByTestId("grace2-routing-dashboard-kpi-success-rate")
        .textContent,
    ).toMatch(/80\.0%/);
  });

  it("renders the per-tool accuracy columns incl. null → '—'", () => {
    render(
      <RoutingQualityDashboard
        onClose={() => undefined}
        initialSummary={SUMMARY_WITH_ACCURACY}
        refreshIntervalMs={0}
      />,
    );
    const femRow = screen
      .getAllByTestId("grace2-routing-dashboard-table-row")
      .find((r) => r.getAttribute("data-tool-name") === "fetch_dem");
    expect(femRow).toBeDefined();
    // fetch_dem carries null usability + routing → both "—".
    const usability = femRow!.querySelector(
      '[data-testid="grace2-routing-dashboard-cell-usability"]',
    );
    const routing = femRow!.querySelector(
      '[data-testid="grace2-routing-dashboard-cell-routing"]',
    );
    expect(usability?.textContent).toContain("—");
    expect(routing?.textContent).toContain("—");
    // Its success cell renders 100.0% (success_rate 1.0).
    const success = femRow!.querySelector(
      '[data-testid="grace2-routing-dashboard-cell-success"]',
    );
    expect(success?.textContent).toMatch(/100\.0%/);
  });
});

describe("RoutingQualityDashboard — solve telemetry", () => {
  it("renders the recent-solves table + wall-clock percentiles", () => {
    render(
      <RoutingQualityDashboard
        onClose={() => undefined}
        initialSummary={SUMMARY_WITH_ACCURACY}
        refreshIntervalMs={0}
      />,
    );
    const table = screen.getByTestId("grace2-routing-dashboard-solve-table");
    expect(table).toBeDefined();
    const rows = screen.getAllByTestId("grace2-routing-dashboard-solve-row");
    expect(rows).toHaveLength(2);
    // SFINCS row: 100 m, ~46k cells, 8 vCPU, 72s wall-clock → "1:12".
    const sfincs = rows.find(
      (r) => r.getAttribute("data-run-id") === "run-sfincs-001",
    );
    expect(sfincs).toBeDefined();
    expect(sfincs!.textContent).toContain("SFINCS");
    expect(sfincs!.textContent).toMatch(/100 m/);
    expect(sfincs!.textContent).toMatch(/46k/);
    expect(sfincs!.textContent).toMatch(/8/);
    expect(sfincs!.textContent).toMatch(/1:12/);
    // MODFLOW row: 1.25M cells abbreviates to "1.3M"; 540s → "9:00".
    const modflow = rows.find(
      (r) => r.getAttribute("data-run-id") === "run-modflow-002",
    );
    expect(modflow!.textContent).toMatch(/1\.3M/);
    expect(modflow!.textContent).toMatch(/9:00/);
    // Wall-clock percentiles header: p50 2:00 / p95 9:00.
    const pct = screen.getByTestId(
      "grace2-routing-dashboard-solve-percentiles",
    );
    expect(pct.textContent).toMatch(/2:00/);
    expect(pct.textContent).toMatch(/9:00/);
  });

  it("shows an honest empty line when no solves are recorded", () => {
    render(
      <RoutingQualityDashboard
        onClose={() => undefined}
        initialSummary={SUMMARY_NULL_METRICS}
        refreshIntervalMs={0}
      />,
    );
    expect(
      screen.getByTestId("grace2-routing-dashboard-no-solves"),
    ).toBeDefined();
    expect(
      screen.queryByTestId("grace2-routing-dashboard-solve-table"),
    ).toBeNull();
  });
});

describe("RoutingQualityDashboard — by-model comparison", () => {
  it("renders one row per model with the four metrics, sorted by count", () => {
    render(
      <RoutingQualityDashboard
        onClose={() => undefined}
        initialSummary={SUMMARY_WITH_ACCURACY}
        refreshIntervalMs={0}
      />,
    );
    const table = screen.getByTestId(
      "grace2-routing-dashboard-by-model-table",
    );
    expect(table).toBeDefined();
    const rows = screen.getAllByTestId(
      "grace2-routing-dashboard-by-model-row",
    );
    expect(rows).toHaveLength(3);
    // Agent emits the rows count-descending; the UI renders them in order.
    const ids = rows.map((r) => r.getAttribute("data-model-id"));
    expect(ids).toEqual([
      "us.anthropic.claude-sonnet-4-6",
      "us.amazon.nova-lite-v1:0",
      "unknown",
    ]);
    // Sonnet row: friendly label + the four metrics (success 93.0%, usability
    // 85.0%, routing 78.0%, p50 200 ms / p95 1.40 s).
    const sonnet = rows.find(
      (r) =>
        r.getAttribute("data-model-id") === "us.anthropic.claude-sonnet-4-6",
    )!;
    expect(sonnet.textContent).toContain("Claude Sonnet 4.6");
    expect(sonnet.textContent).toMatch(/93\.0%/);
    expect(sonnet.textContent).toMatch(/85\.0%/);
    expect(sonnet.textContent).toMatch(/78\.0%/);
    expect(sonnet.textContent).toMatch(/200 ms/);
    expect(sonnet.textContent).toMatch(/1\.40 s/);
  });

  it("renders a friendly label for known models and the raw bucket otherwise", () => {
    render(
      <RoutingQualityDashboard
        onClose={() => undefined}
        initialSummary={SUMMARY_WITH_ACCURACY}
        refreshIntervalMs={0}
      />,
    );
    const rows = screen.getAllByTestId(
      "grace2-routing-dashboard-by-model-row",
    );
    const nova = rows.find(
      (r) => r.getAttribute("data-model-id") === "us.amazon.nova-lite-v1:0",
    )!;
    expect(nova.textContent).toContain("Nova Lite");
    // unknown bucket is NOT mislabelled as a real model.
    const unknown = rows.find(
      (r) => r.getAttribute("data-model-id") === "unknown",
    )!;
    expect(unknown.textContent).toContain("Unknown / legacy");
    expect(unknown.textContent).not.toContain("Claude");
  });

  it("renders null per-model usability/routing as '—', never 0%", () => {
    render(
      <RoutingQualityDashboard
        onClose={() => undefined}
        initialSummary={SUMMARY_WITH_ACCURACY}
        refreshIntervalMs={0}
      />,
    );
    const rows = screen.getAllByTestId(
      "grace2-routing-dashboard-by-model-row",
    );
    const nova = rows.find(
      (r) => r.getAttribute("data-model-id") === "us.amazon.nova-lite-v1:0",
    )!;
    // Nova-lite carries null usability + routing → "—" present, no "0.0%".
    expect(nova.textContent).toContain("—");
    // Success (83.0%) is real, but the nullable metrics must not fabricate 0%.
    expect(nova.textContent).toMatch(/83\.0%/);
  });

  it("shows an honest empty line when no per-model telemetry exists", () => {
    render(
      <RoutingQualityDashboard
        onClose={() => undefined}
        initialSummary={SUMMARY_NULL_METRICS}
        refreshIntervalMs={0}
      />,
    );
    expect(
      screen.getByTestId("grace2-routing-dashboard-no-models"),
    ).toBeDefined();
    expect(
      screen.queryByTestId("grace2-routing-dashboard-by-model-table"),
    ).toBeNull();
  });
});
