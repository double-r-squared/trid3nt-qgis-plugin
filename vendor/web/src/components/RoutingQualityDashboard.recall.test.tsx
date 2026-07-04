// GRACE-2 web — RoutingQualityDashboard recall@k tests
// (tool-retrieval kickoff, orchestrator half).
//
// Verifies the recall_at_k section the agent folds into /api/telemetry/summary:
//   1. The recall@k heading + overall % + per-flow table render.
//   2. The missed-tool list (tools the LLM used that retrieval would have
//      dropped) renders with its name + count.
//   3. A summary with NO measured turns shows the honest "no shadow turns" line.
//   4. A summary WITHOUT a recall_at_k section hides the recall@k UI entirely
//      (backward compatible with pre-shadow-wiring payloads).

import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";

import {
  RoutingQualityDashboard,
  type RoutingDashboardSummary,
} from "./RoutingQualityDashboard";

afterEach(() => {
  cleanup();
});

function baseSummary(): RoutingDashboardSummary {
  return {
    total_dispatches: 5,
    session_count: 1,
    error_rate_overall: 0,
    cache_hit_rate: 0,
    average_latency_ms: 100,
    dispatches_by_tool: [
      {
        name: "fetch_dem",
        count: 5,
        error_count: 0,
        error_rate: 0,
        avg_latency_ms: 100,
      },
    ],
    dispatches_by_source: { llm: 5 },
    error_rate_by_tool: [],
    top_routing_chains: [],
    source: "file",
  };
}

const SUMMARY_WITH_RECALL: RoutingDashboardSummary = {
  ...baseSummary(),
  recall_at_k: {
    overall: 0.8,
    turns_measured: 2,
    dispatches_measured: 5,
    hits: 4,
    misses: 1,
    k: 25,
    by_flow: [
      { flow: "SWMM", recall: 2 / 3, turns: 1, dispatches: 3, hits: 2, misses: 1 },
      { flow: "SFINCS", recall: 1.0, turns: 1, dispatches: 2, hits: 2, misses: 0 },
      { flow: "MODFLOW", recall: null, turns: 0, dispatches: 0, hits: 0, misses: 0 },
    ],
    missed_tools: [{ name: "fetch_buildings", count: 1, flows: ["SWMM"] }],
  },
};

describe("RoutingQualityDashboard — recall@k", () => {
  it("renders overall recall@k, the k, and per-flow rows", () => {
    render(
      <RoutingQualityDashboard
        onClose={() => undefined}
        initialSummary={SUMMARY_WITH_RECALL}
        refreshIntervalMs={0}
      />,
    );
    // Overall recall = 80.0%.
    expect(
      screen.getByTestId("grace2-routing-dashboard-recall-overall").textContent,
    ).toMatch(/80\.0%/);
    // Per-flow rows present for the three North-Star flows.
    const rows = screen.getAllByTestId(
      "grace2-routing-dashboard-recall-flow-row",
    );
    expect(rows.length).toBe(3);
    const swmm = rows.find((r) => r.getAttribute("data-flow") === "SWMM");
    expect(swmm?.textContent).toMatch(/66\.7%/); // 2/3
    const modflow = rows.find((r) => r.getAttribute("data-flow") === "MODFLOW");
    // null recall renders as the em-dash, never 0%.
    expect(modflow?.textContent).toMatch(/—/);
  });

  it("renders the missed-tool list with name + count + flow", () => {
    render(
      <RoutingQualityDashboard
        onClose={() => undefined}
        initialSummary={SUMMARY_WITH_RECALL}
        refreshIntervalMs={0}
      />,
    );
    const missed = screen.getByTestId("grace2-routing-dashboard-missed-tool");
    expect(missed.getAttribute("data-tool-name")).toBe("fetch_buildings");
    expect(missed.textContent).toMatch(/fetch_buildings/);
    expect(missed.textContent).toMatch(/SWMM/);
  });

  it("shows an honest empty line when no shadow turns measured", () => {
    const summary: RoutingDashboardSummary = {
      ...baseSummary(),
      recall_at_k: {
        overall: null,
        turns_measured: 0,
        dispatches_measured: 0,
        hits: 0,
        misses: 0,
        k: null,
        by_flow: [],
        missed_tools: [],
      },
    };
    render(
      <RoutingQualityDashboard
        onClose={() => undefined}
        initialSummary={summary}
        refreshIntervalMs={0}
      />,
    );
    expect(
      screen.getByTestId("grace2-routing-dashboard-no-recall"),
    ).toBeTruthy();
  });

  it("hides the recall@k section entirely when the summary omits it", () => {
    render(
      <RoutingQualityDashboard
        onClose={() => undefined}
        initialSummary={baseSummary()}
        refreshIntervalMs={0}
      />,
    );
    expect(
      screen.queryByTestId("grace2-routing-dashboard-recall-overall"),
    ).toBeNull();
    expect(
      screen.queryByTestId("grace2-routing-dashboard-no-recall"),
    ).toBeNull();
  });
});
