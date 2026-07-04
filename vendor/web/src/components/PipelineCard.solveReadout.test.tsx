// GRACE-2 web — PipelineCard live big-sim solve-readout tests
// (NATE 2026-06-17, live big-sim readout).
//
// Verifies:
//   1. A RUNNING solver card renders the inline solve readout from a mock
//      SolveProgressPayload ("SFINCS · 100 m · ~46k cells · 8 vCPU · 1:12 ·
//      est ~70s").
//   2. The readout updates in place as a fresh envelope arrives.
//   3. A null/absent ETA omits the "est" segment (no fabrication).
//   4. A terminal (complete) card does NOT render the readout (clears on
//      completion), even if a solve payload is still threaded.
//   5. formatSolveReadout / formatCellCount unit behaviour.

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import {
  PipelineCard,
  formatSolveReadout,
  formatCellCount,
} from "./PipelineCard";
import { PipelineStepSummary, SolveProgressPayload } from "../contracts";

// Reduced-motion mock keeps the gradient deterministic; not load-bearing here
// but mirrors the existing PipelineCard test helper.
function mockReducedMotion(reduce: boolean): () => void {
  const original = window.matchMedia;
  window.matchMedia = ((query: string) => ({
    matches: query.includes("prefers-reduced-motion") ? reduce : false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia;
  return () => {
    window.matchMedia = original;
  };
}

function makeStep(
  partial: Partial<PipelineStepSummary> & {
    state: PipelineStepSummary["state"];
  },
): PipelineStepSummary {
  return {
    step_id: partial.step_id ?? "solver-step-1",
    name: partial.name ?? "run_model_flood_scenario",
    tool_name: partial.tool_name ?? "run_model_flood_scenario",
    state: partial.state,
    started_at: partial.started_at,
    completed_at: partial.completed_at,
    duration_ms: partial.duration_ms,
  };
}

const SOLVE: SolveProgressPayload = {
  envelope_type: "solve-progress",
  run_id: "run-001",
  solver: "SFINCS",
  grid_resolution_m: 100,
  active_cell_count: 46000,
  vcpus: 8,
  elapsed_seconds: 72, // 1:12
  eta_seconds: 70, // est ~70s
};

afterEach(() => {
  cleanup();
});

describe("formatSolveReadout / formatCellCount", () => {
  it("formats the full dot-separated readout", () => {
    expect(formatSolveReadout(SOLVE)).toBe(
      "SFINCS · 100 m · ~46k cells · 8 vCPU · 1:12 · est ~70s",
    );
  });

  it("omits the ETA segment when eta_seconds is null", () => {
    const out = formatSolveReadout({ ...SOLVE, eta_seconds: null });
    expect(out).not.toContain("est");
    expect(out).toBe("SFINCS · 100 m · ~46k cells · 8 vCPU · 1:12");
  });

  it("omits the ETA segment when eta_seconds is absent", () => {
    const { eta_seconds: _omit, ...noEta } = SOLVE;
    void _omit;
    expect(formatSolveReadout(noEta as SolveProgressPayload)).not.toContain(
      "est",
    );
  });

  it("formats an ETA over a minute as m:ss", () => {
    expect(formatSolveReadout({ ...SOLVE, eta_seconds: 130 })).toContain(
      "est ~2:10",
    );
  });

  it("abbreviates cell counts", () => {
    expect(formatCellCount(920)).toBe("920");
    expect(formatCellCount(46000)).toBe("46k");
    expect(formatCellCount(1_250_000)).toBe("1.3M");
  });
});

describe("PipelineCard — live solve readout", () => {
  it("renders the readout on a running solver card", () => {
    const restore = mockReducedMotion(true);
    render(<PipelineCard step={makeStep({ state: "running" })} solve={SOLVE} />);
    const readout = screen.getByTestId("pipeline-card-solve");
    expect(readout.textContent).toBe(
      "SFINCS · 100 m · ~46k cells · 8 vCPU · 1:12 · est ~70s",
    );
    expect(readout.getAttribute("data-run-id")).toBe("run-001");
    restore();
  });

  it("updates the readout in place as a fresh envelope arrives", () => {
    const restore = mockReducedMotion(true);
    const step = makeStep({ state: "running" });
    const { rerender } = render(<PipelineCard step={step} solve={SOLVE} />);
    expect(screen.getByTestId("pipeline-card-solve").textContent).toContain(
      "1:12",
    );
    // Newer progress: more elapsed, more cells, shorter ETA.
    const next: SolveProgressPayload = {
      ...SOLVE,
      elapsed_seconds: 95, // 1:35
      active_cell_count: 47200,
      eta_seconds: 40,
    };
    rerender(<PipelineCard step={step} solve={next} />);
    const readout = screen.getByTestId("pipeline-card-solve");
    expect(readout.textContent).toContain("1:35");
    expect(readout.textContent).toContain("est ~40s");
    expect(readout.textContent).not.toContain("1:12");
    restore();
  });

  it("does NOT render the readout once the step is terminal (clears on completion)", () => {
    const restore = mockReducedMotion(true);
    render(
      <PipelineCard
        step={makeStep({ state: "complete", duration_ms: 72000 })}
        solve={SOLVE}
      />,
    );
    expect(screen.queryByTestId("pipeline-card-solve")).toBeNull();
    restore();
  });

  it("does NOT render a readout when no solve payload is threaded", () => {
    const restore = mockReducedMotion(true);
    render(<PipelineCard step={makeStep({ state: "running" })} />);
    expect(screen.queryByTestId("pipeline-card-solve")).toBeNull();
    restore();
  });
});
