// GRACE-2 web - deployment-mode gating tests (local-cloud fingerprint fixes,
// reports/reviews/local-cloud-fingerprints-2026-07-08.md A7/A8/A10).
//
// Verifies the user-visible local-vs-cloud divergences that gate on
// lib/deployment.ts, and - the HARD RULE - that the CLOUD rendering with the
// flag unset is byte-identical to the pre-seam wording:
//   1. ResolutionPickerCard (A7): "vCPUs:" -> "CPUs:" locally; the "local"
//      compute class reads "local run"; the Spot row never renders locally.
//   2. PipelineCard formatSolveReadout (A8): "8 vCPU" -> "8 CPU" locally.
//   3. modelRegistry (A10): the local build offers ONE generic "Local model"
//      entry (no Bedrock ids); the cloud registry is unchanged.
//
// deployment.ts reads the env at CALL time, so the component tests stub the
// env then render with static imports; modelRegistry computes its list at
// module eval, so those cases use vi.resetModules + dynamic import.

import { describe, expect, it, vi, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { ResolutionPickerCard } from "./ResolutionPickerCard";
import { formatSolveReadout } from "./PipelineCard";
import {
  GranularitySuggestion,
  PayloadWarningEnvelopePayload,
  SolveProgressPayload,
} from "../contracts";

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.resetModules();
});

// --- fixtures (mirror ResolutionPickerCard.test.tsx) ---------------------- //

function granularity(
  overrides: Partial<GranularitySuggestion> = {},
): GranularitySuggestion {
  return {
    engine: "swmm",
    resolution_param: "target_resolution_m",
    suggested_resolution_m: 20,
    resolution_choices: [10, 20, 40],
    estimated_active_cells: 40000,
    estimated_solve_seconds: 120,
    vcpus: 8,
    compute_class: "c7i.2xlarge",
    cell_cap: 250000,
    coarsened: false,
    reason: "Balanced resolution for the requested area.",
    spot_label: "c7i.2xlarge (Spot)",
    ...overrides,
  };
}

function warning(
  g: GranularitySuggestion | null,
): PayloadWarningEnvelopePayload {
  return {
    envelope_type: "tool-payload-warning",
    warning_id: "W-GRAN-1",
    tool_name: "run_model_flood_scenario",
    tool_args: { target_resolution_m: 20 },
    estimated_mb: 5,
    threshold_mb: 25,
    recommendation: "Confirm the mesh resolution before the solver run.",
    options: ["proceed", "narrow_scope", "cancel"],
    granularity: g,
  };
}

function renderCard(g: GranularitySuggestion): void {
  render(
    <ResolutionPickerCard warning={warning(g)} granularity={g} onDecide={vi.fn()} />,
  );
}

// --- 1. ResolutionPickerCard (A7) ------------------------------------------ //

describe("ResolutionPickerCard - deployment-mode wording (A7)", () => {
  it("CLOUD (flag unset): byte-identical prior wording - vCPUs label + Spot row", () => {
    renderCard(granularity());
    expect(screen.getByTestId("resolution-picker-vcpus")).toHaveTextContent(
      "vCPUs: 8",
    );
    expect(
      screen.getByTestId("resolution-picker-compute-class"),
    ).toHaveTextContent("Compute: c7i.2xlarge");
    expect(screen.getByTestId("resolution-picker-spot-label")).toHaveTextContent(
      "Spot: c7i.2xlarge (Spot)",
    );
  });

  it("CLOUD keeps the raw 'local' compute class verbatim (the cloud SWMM lane emits it)", () => {
    renderCard(granularity({ compute_class: "local", spot_label: null }));
    expect(
      screen.getByTestId("resolution-picker-compute-class"),
    ).toHaveTextContent("Compute: local");
  });

  it("LOCAL: 'CPUs:' label, 'local run' compute wording, and NO Spot row", () => {
    vi.stubEnv("VITE_DEPLOYMENT", "local");
    renderCard(granularity({ compute_class: "local", vcpus: 8 }));
    const vcpus = screen.getByTestId("resolution-picker-vcpus");
    expect(vcpus).toHaveTextContent("CPUs: 8");
    expect(vcpus.textContent).not.toContain("vCPU");
    expect(
      screen.getByTestId("resolution-picker-compute-class"),
    ).toHaveTextContent("Compute: local run");
    // Even a (contract-legal) spot_label must not render in the local build.
    expect(
      screen.queryByTestId("resolution-picker-spot-label"),
    ).toBeNull();
  });
});

// --- 2. PipelineCard formatSolveReadout (A8) ------------------------------- //

describe("formatSolveReadout - deployment-mode vCPU segment (A8)", () => {
  const solve: SolveProgressPayload = {
    envelope_type: "solve-progress",
    run_id: "run-001",
    solver: "SFINCS",
    grid_resolution_m: 100,
    active_cell_count: 46000,
    vcpus: 8,
    elapsed_seconds: 72, // 1:12
    eta_seconds: 70, // est ~70s
  };

  it("CLOUD (flag unset): byte-identical '8 vCPU' segment", () => {
    expect(formatSolveReadout(solve)).toBe(
      "SFINCS · 100 m · ~46k cells · 8 vCPU · 1:12 · est ~70s",
    );
  });

  it("LOCAL: the segment reads '8 CPU' (no AWS tier vocabulary)", () => {
    vi.stubEnv("VITE_DEPLOYMENT", "local");
    expect(formatSolveReadout(solve)).toBe(
      "SFINCS · 100 m · ~46k cells · 8 CPU · 1:12 · est ~70s",
    );
  });
});

// --- 3. modelRegistry (A10) ------------------------------------------------ //

describe("modelRegistry - deployment-mode registry (A10)", () => {
  it("CLOUD (flag unset): the Bedrock registry, Sonnet default (unchanged)", async () => {
    const { SELECTABLE_MODELS, DEFAULT_MODEL_ID } = await import(
      "../lib/modelRegistry"
    );
    expect(SELECTABLE_MODELS).toHaveLength(5);
    expect(DEFAULT_MODEL_ID).toBe("us.anthropic.claude-sonnet-4-6");
    expect(SELECTABLE_MODELS.map((m) => m.label)).toContain("Claude Sonnet 4.6");
  });

  it("LOCAL: one generic 'Local model' entry, no Bedrock ids, no cache claim", async () => {
    vi.stubEnv("VITE_DEPLOYMENT", "local");
    const { SELECTABLE_MODELS, DEFAULT_MODEL_ID, getModelById } = await import(
      "../lib/modelRegistry"
    );
    expect(SELECTABLE_MODELS).toHaveLength(1);
    const only = SELECTABLE_MODELS[0]!;
    expect(only.label).toBe("Local model");
    expect(only.id).toBe("local-default");
    expect(only.id).not.toContain("anthropic");
    expect(only.supportsPromptCache).toBe(false);
    expect(DEFAULT_MODEL_ID).toBe("local-default");
    // A persisted CLOUD id from a prior session resolves to the local entry.
    expect(getModelById("us.anthropic.claude-sonnet-4-6").id).toBe("local-default");
  });
});
