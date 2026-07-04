// GRACE-2 web — RoutingQualityDashboard tests (Wave 4.11 M7).
//
// Verifies:
//   1. KPI cards render values from the API response.
//   2. Empty state appears when no telemetry has been recorded.
//   3. Loading state appears on initial mount with no inject.
//   4. Error state appears on a 500 response.
//   5. Auto-refresh triggers a second fetch after the interval (fake timers).
//   6. Per-tool table is sorted by count descending.
//   7. Close button + Esc dismiss invoke onClose.

import {
  describe,
  it,
  expect,
  vi,
  afterEach,
} from "vitest";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  waitFor,
  act,
} from "@testing-library/react";

import {
  RoutingQualityDashboard,
  type RoutingDashboardSummary,
} from "./RoutingQualityDashboard";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const FAKE_SUMMARY: RoutingDashboardSummary = {
  total_dispatches: 142,
  session_count: 12,
  error_rate_overall: 0.085,
  cache_hit_rate: 0.62,
  average_latency_ms: 234.5,
  dispatches_by_tool: [
    {
      name: "fetch_dem",
      count: 41,
      error_count: 2,
      error_rate: 0.049,
      avg_latency_ms: 312.5,
    },
    {
      name: "compute_hillshade",
      count: 28,
      error_count: 0,
      error_rate: 0.0,
      avg_latency_ms: 145.0,
    },
    {
      name: "publish_layer",
      count: 22,
      error_count: 3,
      error_rate: 0.136,
      avg_latency_ms: 510.2,
    },
    {
      name: "fetch_nws_alerts_conus",
      count: 14,
      error_count: 1,
      error_rate: 0.071,
      avg_latency_ms: 88.4,
    },
    {
      name: "fetch_administrative_boundaries",
      count: 11,
      error_count: 0,
      error_rate: 0.0,
      avg_latency_ms: 198.7,
    },
  ],
  dispatches_by_source: {
    llm: 118,
    workflow: 24,
  },
  error_rate_by_tool: [
    {
      name: "fetch_dem",
      error_rate: 0.049,
      error_count: 2,
      total: 41,
    },
    {
      name: "publish_layer",
      error_rate: 0.136,
      error_count: 3,
      total: 22,
    },
  ],
  top_routing_chains: [
    { chain: ["fetch_dem", "compute_hillshade"], count: 18 },
    { chain: ["fetch_dem", "publish_layer"], count: 12 },
    { chain: ["compute_hillshade", "publish_layer"], count: 9 },
  ],
  source: "mongo",
};

const EMPTY_SUMMARY: RoutingDashboardSummary = {
  total_dispatches: 0,
  session_count: 0,
  error_rate_overall: 0,
  cache_hit_rate: 0,
  average_latency_ms: 0,
  dispatches_by_tool: [],
  dispatches_by_source: {},
  error_rate_by_tool: [],
  top_routing_chains: [],
  source: "empty",
};

describe("RoutingQualityDashboard", () => {
  it("renders KPI cards from API response", () => {
    render(
      <RoutingQualityDashboard
        onClose={() => undefined}
        initialSummary={FAKE_SUMMARY}
        refreshIntervalMs={0}
      />,
    );
    // Total dispatches
    expect(
      screen.getByTestId("grace2-routing-dashboard-kpi-total").textContent,
    ).toMatch(/142/);
    // Error rate (8.5%)
    expect(
      screen
        .getByTestId("grace2-routing-dashboard-kpi-error-rate")
        .textContent,
    ).toMatch(/8\.5%/);
    // Cache hit rate (62.0%)
    expect(
      screen
        .getByTestId("grace2-routing-dashboard-kpi-cache-hit")
        .textContent,
    ).toMatch(/62\.0%/);
    // Average latency — 234.5 ms (formatMs shows ms or s)
    expect(
      screen.getByTestId("grace2-routing-dashboard-kpi-latency").textContent,
    ).toMatch(/235 ms|234 ms/);
  });

  it("renders empty state when no telemetry available", () => {
    render(
      <RoutingQualityDashboard
        onClose={() => undefined}
        initialSummary={EMPTY_SUMMARY}
        refreshIntervalMs={0}
      />,
    );
    expect(
      screen.getByTestId("grace2-routing-dashboard-empty"),
    ).toBeTruthy();
    // KPI cards should NOT be present.
    expect(
      screen.queryByTestId("grace2-routing-dashboard-kpis"),
    ).toBeNull();
  });

  it("shows loading state on initial fetch", () => {
    // Hang the fetch promise so the dashboard stays in loading state.
    const fetchMock = vi.fn(() => new Promise(() => undefined));
    vi.stubGlobal("fetch", fetchMock);
    render(
      <RoutingQualityDashboard
        onClose={() => undefined}
        refreshIntervalMs={0}
      />,
    );
    expect(
      screen.getByTestId("grace2-routing-dashboard-loading"),
    ).toBeTruthy();
  });

  it("shows error state when fetch returns 500", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify({ error: "boom" }), {
          status: 500,
          statusText: "Internal Server Error",
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(
      <RoutingQualityDashboard
        onClose={() => undefined}
        refreshIntervalMs={0}
      />,
    );
    await waitFor(() => {
      expect(
        screen.getByTestId("grace2-routing-dashboard-error"),
      ).toBeTruthy();
    });
    expect(
      screen.getByTestId("grace2-routing-dashboard-error").textContent,
    ).toMatch(/HTTP 500/);
  });

  it("auto-refresh fires after the interval (fake timers)", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify(FAKE_SUMMARY), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    // initialSummary skips the first fetch — auto-refresh should still tick.
    render(
      <RoutingQualityDashboard
        onClose={() => undefined}
        initialSummary={FAKE_SUMMARY}
        refreshIntervalMs={30_000}
      />,
    );
    // No fetch on mount (initialSummary present).
    expect(fetchMock).not.toHaveBeenCalled();
    // Advance the timer past the auto-refresh interval.
    await act(async () => {
      vi.advanceTimersByTime(30_001);
      // Flush microtasks scheduled inside the timer callback.
      await Promise.resolve();
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it("per-tool table is sorted by count descending", () => {
    render(
      <RoutingQualityDashboard
        onClose={() => undefined}
        initialSummary={FAKE_SUMMARY}
        refreshIntervalMs={0}
      />,
    );
    const rows = screen.getAllByTestId("grace2-routing-dashboard-table-row");
    const names = rows.map((r) => r.getAttribute("data-tool-name"));
    expect(names).toEqual([
      "fetch_dem",
      "compute_hillshade",
      "publish_layer",
      "fetch_nws_alerts_conus",
      "fetch_administrative_boundaries",
    ]);
  });

  it("close button + Esc invoke onClose", () => {
    const onClose = vi.fn();
    render(
      <RoutingQualityDashboard
        onClose={onClose}
        initialSummary={FAKE_SUMMARY}
        refreshIntervalMs={0}
      />,
    );
    // X button
    fireEvent.click(
      screen.getByTestId("grace2-routing-dashboard-close"),
    );
    expect(onClose).toHaveBeenCalledTimes(1);
    // Esc
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(2);
    // Backdrop
    fireEvent.click(screen.getByTestId("grace2-routing-dashboard"));
    expect(onClose).toHaveBeenCalledTimes(3);
    // Card click does NOT bubble through
    fireEvent.click(
      screen.getByTestId("grace2-routing-dashboard-card"),
    );
    expect(onClose).toHaveBeenCalledTimes(3);
  });

  it("manual refresh button triggers a new fetch", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify(FAKE_SUMMARY), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(
      <RoutingQualityDashboard
        onClose={() => undefined}
        initialSummary={FAKE_SUMMARY}
        refreshIntervalMs={0}
      />,
    );
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(
      screen.getByTestId("grace2-routing-dashboard-refresh"),
    );
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
  });
});
