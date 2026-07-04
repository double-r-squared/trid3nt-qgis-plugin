// GRACE-2 web — Chat solve-progress routing + step-matching tests
// (NATE 2026-06-17, live big-sim readout).
//
// Chat itself opens a WebSocket and cannot mount in happy-dom, so the live
// big-sim plumbing is verified through the exported pure helpers, following
// the established pattern (routePipelineState / matchSolveForStep et al.):
//   1. routeSolveProgress stores the latest payload per run_id in the OWNING
//      stream, replacing in place per run.
//   2. solve-progress routes to the case_id-tagged stream (turn pin), or the
//      submit-time targetKey when untagged.
//   3. matchSolveForStep paints a readout ONLY on a running solver step, picks
//      the right family among concurrent runs, and declines on a non-solver /
//      non-running step.

import { describe, expect, it } from "vitest";

import {
  createChatStreams,
  getStream,
  routeSolveProgress,
  routeUserMessage,
  streamKeyFor,
  matchSolveForStep,
  isSolverStep,
} from "./Chat";
import { PipelineStepSummary, SolveProgressPayload } from "./contracts";

function solve(partial: Partial<SolveProgressPayload>): SolveProgressPayload {
  return {
    run_id: partial.run_id ?? "run-001",
    solver: partial.solver ?? "SFINCS",
    grid_resolution_m: partial.grid_resolution_m ?? 100,
    active_cell_count: partial.active_cell_count ?? 46000,
    vcpus: partial.vcpus ?? 8,
    elapsed_seconds: partial.elapsed_seconds ?? 30,
    eta_seconds: partial.eta_seconds ?? 60,
  };
}

function step(
  partial: Partial<PipelineStepSummary> & {
    state: PipelineStepSummary["state"];
  },
): PipelineStepSummary {
  return {
    step_id: partial.step_id ?? "s1",
    name: partial.name ?? "run_model_flood_scenario",
    tool_name: partial.tool_name ?? "run_model_flood_scenario",
    state: partial.state,
  };
}

describe("routeSolveProgress", () => {
  it("stores the latest payload per run_id in the owning stream", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, streamKeyFor("CASE_A"), "flood in A");
    routeSolveProgress(cs, solve({ run_id: "r1", elapsed_seconds: 10 }));
    routeSolveProgress(cs, solve({ run_id: "r1", elapsed_seconds: 42 }));

    const s = getStream(cs, "CASE_A");
    expect(s.solveProgress.size).toBe(1);
    expect(s.solveProgress.get("r1")?.elapsed_seconds).toBe(42);
  });

  it("routes to the case-tagged stream over the submit-time target", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, streamKeyFor("CASE_A"), "flood in A");
    // A solve-progress envelope tagged for a DIFFERENT case lands there.
    routeSolveProgress(cs, solve({ run_id: "r9" }), "CASE_B");

    expect(getStream(cs, "CASE_A").solveProgress.size).toBe(0);
    expect(getStream(cs, "CASE_B").solveProgress.get("r9")).toBeDefined();
  });

  it("assigns a fresh Map so React referential equality detects the change", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, streamKeyFor("CASE_A"), "x");
    const before = getStream(cs, "CASE_A").solveProgress;
    routeSolveProgress(cs, solve({ run_id: "r1" }));
    const after = getStream(cs, "CASE_A").solveProgress;
    expect(after).not.toBe(before);
  });
});

describe("isSolverStep", () => {
  it("flags heavy-solver step names", () => {
    expect(isSolverStep(step({ state: "running", name: "run_model_flood_scenario" }))).toBe(true);
    expect(isSolverStep(step({ state: "running", name: "run_modflow_job", tool_name: "run_modflow_job" }))).toBe(true);
    expect(isSolverStep(step({ state: "running", name: "run_pelicun_damage_assessment", tool_name: "run_pelicun_damage_assessment" }))).toBe(true);
  });

  it("does not flag ordinary fetch / compute tools", () => {
    expect(isSolverStep(step({ state: "running", name: "fetch_dem", tool_name: "fetch_dem" }))).toBe(false);
    expect(isSolverStep(step({ state: "running", name: "compute_hillshade", tool_name: "compute_hillshade" }))).toBe(false);
  });
});

describe("matchSolveForStep", () => {
  it("matches the single tracked run to a running solver step", () => {
    const m = new Map<string, SolveProgressPayload>([
      ["r1", solve({ run_id: "r1", solver: "SFINCS" })],
    ]);
    const matched = matchSolveForStep(step({ state: "running" }), m);
    expect(matched?.run_id).toBe("r1");
  });

  it("returns null for a non-running solver step (clears on completion)", () => {
    const m = new Map<string, SolveProgressPayload>([
      ["r1", solve({ run_id: "r1" })],
    ]);
    expect(matchSolveForStep(step({ state: "complete" }), m)).toBeNull();
    expect(matchSolveForStep(step({ state: "pending" }), m)).toBeNull();
  });

  it("returns null for a non-solver tool even while running", () => {
    const m = new Map<string, SolveProgressPayload>([
      ["r1", solve({ run_id: "r1" })],
    ]);
    expect(
      matchSolveForStep(
        step({ state: "running", name: "fetch_dem", tool_name: "fetch_dem" }),
        m,
      ),
    ).toBeNull();
  });

  it("returns null when there is no solve-progress tracked", () => {
    expect(matchSolveForStep(step({ state: "running" }), new Map())).toBeNull();
    expect(matchSolveForStep(step({ state: "running" }), null)).toBeNull();
  });

  it("disambiguates concurrent runs by solver family", () => {
    const m = new Map<string, SolveProgressPayload>([
      ["rs", solve({ run_id: "rs", solver: "SFINCS" })],
      ["rm", solve({ run_id: "rm", solver: "MODFLOW" })],
    ]);
    // A flood-scenario step prefers the SFINCS run.
    const flood = matchSolveForStep(
      step({ state: "running", name: "run_model_flood_scenario", tool_name: "run_model_flood_scenario" }),
      m,
    );
    expect(flood?.solver).toBe("SFINCS");
    // A groundwater step prefers the MODFLOW run.
    const gw = matchSolveForStep(
      step({ state: "running", name: "run_modflow_job", tool_name: "run_modflow_job" }),
      m,
    );
    expect(gw?.solver).toBe("MODFLOW");
  });

  it("declines (null) on ambiguous multi-run with no family hint", () => {
    const m = new Map<string, SolveProgressPayload>([
      ["a", solve({ run_id: "a", solver: "GenericSolverA" })],
      ["b", solve({ run_id: "b", solver: "GenericSolverB" })],
    ]);
    // run_solver has no flood/groundwater/pelicun keyword → no family hint.
    expect(
      matchSolveForStep(
        step({ state: "running", name: "run_solver", tool_name: "run_solver" }),
        m,
      ),
    ).toBeNull();
  });
});
