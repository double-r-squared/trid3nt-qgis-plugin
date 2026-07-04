// GRACE-2 web - ResolutionPickerCard tests (#154 pre-run granularity gate).
//
// Verifies the in-chat mesh-resolution confirm card:
//   1. Renders the suggested-rung metadata (resolution / cells / ETA / vCPUs /
//      compute class / Spot label) from the GranularitySuggestion.
//   2. Picking a finer / coarser rung LIVE-recomputes the displayed cells + ETA
//      client-side (area-invariant scaling off the suggested-rung baseline).
//   3. Confirm UNCHANGED (chosen == suggested) -> decision "proceed", revised null.
//   4. Confirm AFTER override -> decision "narrow_scope", revised
//      { [resolution_param]: chosen }.
//   5. Cancel -> decision "cancel", revised null.
//   6. The card LOCKS + FOLDS after a decision so it cannot be re-answered.
//   7. Pure recompute helpers (area-invariant cells + proportional ETA).

import { describe, expect, it, vi, afterEach } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import {
  ResolutionPickerCard,
  estimateCellsForResolution,
  estimateSolveSecondsForResolution,
  estimateFrameCount,
} from "./ResolutionPickerCard";
import {
  GranularitySuggestion,
  PayloadWarningEnvelopePayload,
  TimeScaleSuggestion,
} from "../contracts";

afterEach(() => {
  cleanup();
});

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
  overrides: Partial<PayloadWarningEnvelopePayload> = {},
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
    ...overrides,
  };
}

describe("ResolutionPickerCard - rendering", () => {
  it("renders the suggested-rung metadata numbers", () => {
    const g = granularity();
    render(
      <ResolutionPickerCard
        warning={warning(g)}
        granularity={g}
        onDecide={vi.fn()}
      />,
    );
    expect(screen.getByTestId("resolution-picker-suggested-m")).toHaveTextContent(
      "20 m",
    );
    // 40000 cells -> "40k"
    expect(screen.getByTestId("resolution-picker-cells")).toHaveTextContent("~40k");
    // 120s -> "~2:00" (>= 90s rolls into m:ss)
    expect(screen.getByTestId("resolution-picker-eta")).toHaveTextContent("~2:00");
    expect(screen.getByTestId("resolution-picker-vcpus")).toHaveTextContent("8");
    expect(
      screen.getByTestId("resolution-picker-compute-class"),
    ).toHaveTextContent("c7i.2xlarge");
    expect(screen.getByTestId("resolution-picker-spot-label")).toHaveTextContent(
      "c7i.2xlarge (Spot)",
    );
  });

  it("omits the Spot label row when spot_label is null", () => {
    const g = granularity({ spot_label: null });
    render(
      <ResolutionPickerCard
        warning={warning(g)}
        granularity={g}
        onDecide={vi.fn()}
      />,
    );
    expect(
      screen.queryByTestId("resolution-picker-spot-label"),
    ).not.toBeInTheDocument();
  });

  it("prefixes the caption with 'Coarsened' when coarsened is true", () => {
    const g = granularity({ coarsened: true, reason: "Area too large at 10 m." });
    render(
      <ResolutionPickerCard
        warning={warning(g)}
        granularity={g}
        onDecide={vi.fn()}
      />,
    );
    expect(screen.getByTestId("resolution-picker-reason")).toHaveTextContent(
      "Coarsened - Area too large at 10 m.",
    );
  });

  it("renders one chip per resolution choice and marks the suggested rung", () => {
    const g = granularity();
    render(
      <ResolutionPickerCard
        warning={warning(g)}
        granularity={g}
        onDecide={vi.fn()}
      />,
    );
    expect(screen.getByTestId("resolution-picker-chip-10")).toBeInTheDocument();
    expect(screen.getByTestId("resolution-picker-chip-20")).toBeInTheDocument();
    expect(screen.getByTestId("resolution-picker-chip-40")).toBeInTheDocument();
    // Default selection is the suggested rung.
    expect(screen.getByTestId("resolution-picker-chip-20")).toHaveAttribute(
      "data-selected",
      "true",
    );
    expect(screen.getByTestId("resolution-picker-chip-20")).toHaveTextContent(
      "(suggested)",
    );
  });
});

describe("ResolutionPickerCard - live recompute on chip change", () => {
  it("live-updates cells + ETA when a finer rung is picked", () => {
    const g = granularity(); // 20 m / 40000 cells / 120s
    render(
      <ResolutionPickerCard
        warning={warning(g)}
        granularity={g}
        onDecide={vi.fn()}
      />,
    );
    // Initial readout at the suggested 20 m rung.
    expect(
      screen.getByTestId("resolution-picker-readout-cells"),
    ).toHaveTextContent("~40k");
    expect(
      screen.getByTestId("resolution-picker-readout-eta"),
    ).toHaveTextContent("~2:00");

    // Pick the finer 10 m rung -> (20/10)^2 = 4x cells = 160000 ("160k") and
    // 4x ETA = 480s ("~8:00").
    fireEvent.click(screen.getByTestId("resolution-picker-chip-10"));
    expect(
      screen.getByTestId("resolution-picker-readout-cells"),
    ).toHaveTextContent("~160k");
    expect(
      screen.getByTestId("resolution-picker-readout-eta"),
    ).toHaveTextContent("~8:00");
  });

  it("live-updates cells + ETA when a coarser rung is picked", () => {
    const g = granularity(); // 20 m / 40000 cells / 120s
    render(
      <ResolutionPickerCard
        warning={warning(g)}
        granularity={g}
        onDecide={vi.fn()}
      />,
    );
    // Pick the coarser 40 m rung -> (20/40)^2 = 0.25x cells = 10000 ("10k") and
    // 0.25x ETA = 30s ("~30s").
    fireEvent.click(screen.getByTestId("resolution-picker-chip-40"));
    expect(
      screen.getByTestId("resolution-picker-readout-cells"),
    ).toHaveTextContent("~10k");
    expect(
      screen.getByTestId("resolution-picker-readout-eta"),
    ).toHaveTextContent("~30s");
  });
});

describe("ResolutionPickerCard - decisions", () => {
  it("Confirm UNCHANGED -> proceed with null revised_args", () => {
    const g = granularity();
    const onDecide = vi.fn();
    render(
      <ResolutionPickerCard
        warning={warning(g)}
        granularity={g}
        onDecide={onDecide}
      />,
    );
    fireEvent.click(screen.getByTestId("resolution-picker-confirm"));
    expect(onDecide).toHaveBeenCalledTimes(1);
    expect(onDecide).toHaveBeenCalledWith("proceed", null);
  });

  it("Confirm AFTER override -> narrow_scope with { resolution_param: chosen }", () => {
    const g = granularity(); // resolution_param = target_resolution_m
    const onDecide = vi.fn();
    render(
      <ResolutionPickerCard
        warning={warning(g)}
        granularity={g}
        onDecide={onDecide}
      />,
    );
    fireEvent.click(screen.getByTestId("resolution-picker-chip-10"));
    fireEvent.click(screen.getByTestId("resolution-picker-confirm"));
    expect(onDecide).toHaveBeenCalledTimes(1);
    expect(onDecide).toHaveBeenCalledWith("narrow_scope", {
      target_resolution_m: 10,
    });
  });

  it("uses the engine-specific resolution_param key for the override", () => {
    const g = granularity({
      engine: "sfincs",
      resolution_param: "grid_resolution_m",
      suggested_resolution_m: 100,
      resolution_choices: [50, 100, 200],
      estimated_active_cells: 50000,
      estimated_solve_seconds: 200,
    });
    const onDecide = vi.fn();
    render(
      <ResolutionPickerCard
        warning={warning(g)}
        granularity={g}
        onDecide={onDecide}
      />,
    );
    fireEvent.click(screen.getByTestId("resolution-picker-chip-50"));
    fireEvent.click(screen.getByTestId("resolution-picker-confirm"));
    expect(onDecide).toHaveBeenCalledWith("narrow_scope", {
      grid_resolution_m: 50,
    });
  });

  // NATE 2026-06-26: the #154 gate now also describes a FETCHER resolution
  // choice (dem / topobathy fetch, resolution_param "resolution_m"). The card
  // is value-agnostic: it renders whatever ladder + key the contract carries
  // and writes the chosen rung back under that exact key. No component change.
  it("renders a FETCHER (dem) ladder and overrides under resolution_m", () => {
    const g = granularity({
      engine: "dem",
      resolution_param: "resolution_m",
      suggested_resolution_m: 10,
      resolution_choices: [1, 3, 10, 30],
      estimated_active_cells: 90000,
      estimated_solve_seconds: 0,
      vcpus: 1,
      compute_class: "fetch",
      coarsened: false,
      spot_label: null,
    });
    const onDecide = vi.fn();
    render(
      <ResolutionPickerCard
        warning={warning(g)}
        granularity={g}
        onDecide={onDecide}
      />,
    );
    // The chip row renders the full fetch ladder, suggested rung defaulted.
    expect(screen.getByTestId("resolution-picker-chip-1")).toBeInTheDocument();
    expect(screen.getByTestId("resolution-picker-chip-3")).toBeInTheDocument();
    expect(screen.getByTestId("resolution-picker-chip-10")).toBeInTheDocument();
    expect(screen.getByTestId("resolution-picker-chip-30")).toBeInTheDocument();
    expect(screen.getByTestId("resolution-picker-chip-10")).toHaveAttribute(
      "data-selected",
      "true",
    );
    // Override to the finer 1 m rung -> narrow_scope under the fetch key.
    fireEvent.click(screen.getByTestId("resolution-picker-chip-1"));
    fireEvent.click(screen.getByTestId("resolution-picker-confirm"));
    expect(onDecide).toHaveBeenCalledTimes(1);
    expect(onDecide).toHaveBeenCalledWith("narrow_scope", {
      resolution_m: 1,
    });
  });

  it("Cancel -> cancel with null revised_args", () => {
    const g = granularity();
    const onDecide = vi.fn();
    render(
      <ResolutionPickerCard
        warning={warning(g)}
        granularity={g}
        onDecide={onDecide}
      />,
    );
    fireEvent.click(screen.getByTestId("resolution-picker-cancel"));
    expect(onDecide).toHaveBeenCalledTimes(1);
    expect(onDecide).toHaveBeenCalledWith("cancel", null);
  });
});

describe("ResolutionPickerCard - lock + fold after a decision", () => {
  it("folds to the compact summary and cannot be re-answered after Confirm", () => {
    const g = granularity();
    const onDecide = vi.fn();
    render(
      <ResolutionPickerCard
        warning={warning(g)}
        granularity={g}
        onDecide={onDecide}
      />,
    );
    fireEvent.click(screen.getByTestId("resolution-picker-confirm"));
    // Active controls are gone - the card folded.
    expect(
      screen.queryByTestId("resolution-picker-confirm"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("resolution-picker-chips"),
    ).not.toBeInTheDocument();
    // The fold shows the resolved summary.
    const card = screen.getByTestId("resolution-picker-card");
    expect(card).toHaveAttribute("data-resolved", "proceed");
    expect(
      screen.getByTestId("resolution-picker-resolved"),
    ).toHaveTextContent("Mesh resolution confirmed");
    // onDecide fired exactly once (no re-answer possible).
    expect(onDecide).toHaveBeenCalledTimes(1);
  });

  it("seeds the folded state from an externally-recorded resolution", () => {
    const g = granularity();
    render(
      <ResolutionPickerCard
        warning={warning(g)}
        granularity={g}
        onDecide={vi.fn()}
        resolved="narrow_scope"
      />,
    );
    // Mounts already folded (Case switch + return).
    expect(
      screen.queryByTestId("resolution-picker-confirm"),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId("resolution-picker-card")).toHaveAttribute(
      "data-resolved",
      "narrow_scope",
    );
    expect(
      screen.getByTestId("resolution-picker-resolved"),
    ).toHaveTextContent("Mesh resolution overridden");
  });
});

// --- Combined run-settings card (granularity + time_scale) ---------------- //

function timeScale(
  overrides: Partial<TimeScaleSuggestion> = {},
): TimeScaleSuggestion {
  return {
    cadence_param: "output_interval_min",
    suggested_interval_min: 5,
    interval_choices: [1, 2, 5, 10, 30, 60],
    duration_param: "duration_hr",
    suggested_duration_hr: 6,
    estimated_frame_count: 72,
    max_frames: 240,
    min_interval_min: 1,
    is_coastal: true,
    reason: "Coastal: 5-min frames over a 6 h window animate the roll-in.",
    ...overrides,
  };
}

// An SFINCS granularity for the flood combined card.
function sfincsGranularity(
  overrides: Partial<GranularitySuggestion> = {},
): GranularitySuggestion {
  return granularity({
    engine: "sfincs",
    resolution_param: "grid_resolution_m",
    suggested_resolution_m: 30,
    resolution_choices: [30, 50, 100, 200],
    estimated_active_cells: 46000,
    estimated_solve_seconds: 70,
    compute_class: "standard",
    spot_label: null,
    ...overrides,
  });
}

describe("ResolutionPickerCard - combined run-settings (time scale)", () => {
  it("renders BOTH the resolution chips and the time-scale section", () => {
    const g = sfincsGranularity();
    const ts = timeScale();
    render(
      <ResolutionPickerCard
        warning={warning(g, { time_scale: ts })}
        granularity={g}
        timeScale={ts}
        onDecide={vi.fn()}
      />,
    );
    // Resolution section still present.
    expect(screen.getByTestId("resolution-picker-chips")).toBeInTheDocument();
    // Time-scale section + editable fields present.
    expect(screen.getByTestId("resolution-picker-timescale")).toBeInTheDocument();
    expect(
      screen.getByTestId("resolution-picker-interval-input"),
    ).toHaveValue(5);
    expect(
      screen.getByTestId("resolution-picker-duration-input"),
    ).toHaveValue(6);
    // The title reads "Confirm run settings" (combined).
    const card = screen.getByTestId("resolution-picker-card");
    expect(card).toHaveAttribute("data-combined", "true");
  });

  it("shows a LIVE frame-count seeded from the suggestion", () => {
    const g = sfincsGranularity();
    const ts = timeScale(); // 6 h / 5 min -> 72 frames
    render(
      <ResolutionPickerCard
        warning={warning(g, { time_scale: ts })}
        granularity={g}
        timeScale={ts}
        onDecide={vi.fn()}
      />,
    );
    expect(
      screen.getByTestId("resolution-picker-frame-count"),
    ).toHaveTextContent("72");
  });

  it("LIVE-recomputes the frame count as the interval is edited", () => {
    const g = sfincsGranularity();
    const ts = timeScale(); // 6 h window
    render(
      <ResolutionPickerCard
        warning={warning(g, { time_scale: ts })}
        granularity={g}
        timeScale={ts}
        onDecide={vi.fn()}
      />,
    );
    // 6 h @ 5 min = 72; change to 2 min -> 6*60/2 = 180 frames.
    fireEvent.change(screen.getByTestId("resolution-picker-interval-input"), {
      target: { value: "2" },
    });
    expect(
      screen.getByTestId("resolution-picker-frame-count"),
    ).toHaveTextContent("180");
  });

  it("LIVE-recomputes the frame count as the window is edited", () => {
    const g = sfincsGranularity();
    const ts = timeScale(); // 5 min cadence
    render(
      <ResolutionPickerCard
        warning={warning(g, { time_scale: ts })}
        granularity={g}
        timeScale={ts}
        onDecide={vi.fn()}
      />,
    );
    // 12 h @ 5 min = 144 frames.
    fireEvent.change(screen.getByTestId("resolution-picker-duration-input"), {
      target: { value: "12" },
    });
    expect(
      screen.getByTestId("resolution-picker-frame-count"),
    ).toHaveTextContent("144");
  });

  it("clamps the LIVE frame count to max_frames", () => {
    const g = sfincsGranularity();
    const ts = timeScale({ max_frames: 100 }); // cap below the raw count
    render(
      <ResolutionPickerCard
        warning={warning(g, { time_scale: ts })}
        granularity={g}
        timeScale={ts}
        onDecide={vi.fn()}
      />,
    );
    // 6 h @ 1 min = 360 raw -> clamped to 100.
    fireEvent.change(screen.getByTestId("resolution-picker-interval-input"), {
      target: { value: "1" },
    });
    expect(
      screen.getByTestId("resolution-picker-frame-count"),
    ).toHaveTextContent("100");
  });

  it("a cadence preset chip sets the interval field", () => {
    const g = sfincsGranularity();
    const ts = timeScale();
    render(
      <ResolutionPickerCard
        warning={warning(g, { time_scale: ts })}
        granularity={g}
        timeScale={ts}
        onDecide={vi.fn()}
      />,
    );
    fireEvent.click(
      screen.getByTestId("resolution-picker-interval-chip-10"),
    );
    expect(
      screen.getByTestId("resolution-picker-interval-input"),
    ).toHaveValue(10);
    // 6 h @ 10 min = 36 frames.
    expect(
      screen.getByTestId("resolution-picker-frame-count"),
    ).toHaveTextContent("36");
  });

  it("Confirm with NOTHING changed -> proceed (null revised)", () => {
    const g = sfincsGranularity();
    const ts = timeScale();
    const onDecide = vi.fn();
    render(
      <ResolutionPickerCard
        warning={warning(g, { time_scale: ts })}
        granularity={g}
        timeScale={ts}
        onDecide={onDecide}
      />,
    );
    fireEvent.click(screen.getByTestId("resolution-picker-confirm"));
    expect(onDecide).toHaveBeenCalledWith("proceed", null);
  });

  it("Confirm after editing the cadence -> narrow_scope with BOTH overrides", () => {
    const g = sfincsGranularity();
    const ts = timeScale();
    const onDecide = vi.fn();
    render(
      <ResolutionPickerCard
        warning={warning(g, { time_scale: ts })}
        granularity={g}
        timeScale={ts}
        onDecide={onDecide}
      />,
    );
    fireEvent.change(screen.getByTestId("resolution-picker-interval-input"), {
      target: { value: "2" },
    });
    fireEvent.click(screen.getByTestId("resolution-picker-confirm"));
    // The resolution rides back (still the suggested rung) + the changed cadence.
    expect(onDecide).toHaveBeenCalledWith("narrow_scope", {
      grid_resolution_m: 30,
      output_interval_min: 2,
    });
  });

  it("Confirm after editing BOTH resolution + cadence + window -> all three", () => {
    const g = sfincsGranularity();
    const ts = timeScale();
    const onDecide = vi.fn();
    render(
      <ResolutionPickerCard
        warning={warning(g, { time_scale: ts })}
        granularity={g}
        timeScale={ts}
        onDecide={onDecide}
      />,
    );
    fireEvent.click(screen.getByTestId("resolution-picker-chip-100"));
    fireEvent.change(screen.getByTestId("resolution-picker-interval-input"), {
      target: { value: "10" },
    });
    fireEvent.change(screen.getByTestId("resolution-picker-duration-input"), {
      target: { value: "8" },
    });
    fireEvent.click(screen.getByTestId("resolution-picker-confirm"));
    expect(onDecide).toHaveBeenCalledWith("narrow_scope", {
      grid_resolution_m: 100,
      output_interval_min: 10,
      duration_hr: 8,
    });
  });

  it("floors a below-floor cadence edit at min_interval_min on confirm", () => {
    const g = sfincsGranularity();
    const ts = timeScale({ min_interval_min: 1 });
    const onDecide = vi.fn();
    render(
      <ResolutionPickerCard
        warning={warning(g, { time_scale: ts })}
        granularity={g}
        timeScale={ts}
        onDecide={onDecide}
      />,
    );
    fireEvent.change(screen.getByTestId("resolution-picker-interval-input"), {
      target: { value: "0.1" },
    });
    fireEvent.click(screen.getByTestId("resolution-picker-confirm"));
    expect(onDecide).toHaveBeenCalledWith("narrow_scope", {
      grid_resolution_m: 30,
      output_interval_min: 1,
    });
  });

  it("folds to the run-settings summary after a combined decision", () => {
    const g = sfincsGranularity();
    const ts = timeScale();
    render(
      <ResolutionPickerCard
        warning={warning(g, { time_scale: ts })}
        granularity={g}
        timeScale={ts}
        onDecide={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("resolution-picker-confirm"));
    expect(
      screen.getByTestId("resolution-picker-resolved"),
    ).toHaveTextContent("Run settings confirmed");
  });

  it("granularity-only (no time_scale) stays the resolution gate", () => {
    const g = granularity();
    render(
      <ResolutionPickerCard
        warning={warning(g)}
        granularity={g}
        onDecide={vi.fn()}
      />,
    );
    expect(
      screen.queryByTestId("resolution-picker-timescale"),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId("resolution-picker-card")).toHaveAttribute(
      "data-combined",
      "false",
    );
  });
});

describe("ResolutionPickerCard - estimateFrameCount helper", () => {
  const ts = timeScale({ max_frames: 240, min_interval_min: 1 });

  it("frames = duration*60 / interval", () => {
    expect(estimateFrameCount(ts, 5, 6)).toBe(72); // 360/5
    expect(estimateFrameCount(ts, 2, 6)).toBe(180); // 360/2
    expect(estimateFrameCount(ts, 10, 12)).toBe(72); // 720/10
  });

  it("clamps to max_frames", () => {
    expect(estimateFrameCount(ts, 1, 24)).toBe(240); // 1440 raw -> 240 cap
  });

  it("floors the interval at min_interval_min", () => {
    // 0.5 min floored to 1 -> 360/1 = 360 -> clamped to 240.
    expect(estimateFrameCount(ts, 0.5, 6)).toBe(240);
  });

  it("returns >= 1 for a degenerate window", () => {
    expect(estimateFrameCount(ts, 5, 0)).toBe(1);
  });
});

describe("ResolutionPickerCard - pure recompute helpers", () => {
  it("estimateCellsForResolution scales by the inverse square of the rung", () => {
    const g = granularity(); // 20 m / 40000 cells
    expect(estimateCellsForResolution(g, 20)).toBe(40000); // suggested -> exact
    expect(estimateCellsForResolution(g, 10)).toBe(160000); // 4x finer
    expect(estimateCellsForResolution(g, 40)).toBe(10000); // 0.25x coarser
  });

  it("estimateSolveSecondsForResolution scales proportionally to the cell ratio", () => {
    const g = granularity(); // 120s baseline at 40000 cells
    expect(estimateSolveSecondsForResolution(g, 20)).toBe(120);
    expect(estimateSolveSecondsForResolution(g, 10)).toBe(480); // 4x cells
    expect(estimateSolveSecondsForResolution(g, 40)).toBe(30); // 0.25x cells
  });
});
