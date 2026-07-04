// GRACE-2 web - nested sub-step visibility tests (task-168).
//
// Verifies the "live breadcrumb + expand" surface for a composer's INTERNAL
// atomic-tool calls:
//   - children (parent_step_id set) are COLLECTED under their parent and NEVER
//     render as their own top-level interleaved card (chat stays clean by
//     default) - buildInterleavedStream level.
//   - the parent's ``children`` array is ordered + attached.
//   - the LIVE breadcrumb shows while running with a substep_label.
//   - the expanded nested timeline lists children with humanized labels +
//     durations, a failed child reads red while siblings stay green, and chat
//     never shows raw snake_case (PipelineCard level).

import { describe, it, expect, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { buildInterleavedStream, InterleavedEntry } from "./Chat";
import { PipelineCard } from "./components/PipelineCard";
import { PipelineStatePayload, PipelineStepSummary } from "./contracts";

afterEach(() => cleanup());

function makeStep(
  partial: Partial<PipelineStepSummary> & {
    step_id: string;
    state: PipelineStepSummary["state"];
  },
): PipelineStepSummary {
  return {
    step_id: partial.step_id,
    name: partial.name ?? "fetch_dem",
    tool_name: partial.tool_name ?? "fetch_dem",
    state: partial.state,
    parent_step_id: partial.parent_step_id ?? null,
    substep_label: partial.substep_label ?? null,
    substep_index: partial.substep_index ?? null,
    substep_total: partial.substep_total ?? null,
    duration_ms: partial.duration_ms ?? null,
    started_at: partial.started_at ?? null,
  };
}

function snap(
  pipelineId: string,
  steps: PipelineStepSummary[],
): PipelineStatePayload {
  return { pipeline_id: pipelineId, steps };
}

function toolEntries(stream: InterleavedEntry[]) {
  return stream.filter(
    (e): e is Extract<InterleavedEntry, { kind: "tool" }> => e.kind === "tool",
  );
}

// --- buildInterleavedStream: children are collected, not top-level ------- //

describe("buildInterleavedStream - nested sub-steps (task-168)", () => {
  it("renders ONE top-level card for a parent with children; children are NOT top-level", () => {
    const parent = makeStep({
      step_id: "parent-1",
      name: "run_model_flood_scenario",
      tool_name: "run_model_flood_scenario",
      state: "running",
      substep_label: "fetch_topobathy",
      substep_index: 2,
      substep_total: 7,
    });
    const childA = makeStep({
      step_id: "child-a",
      name: "fetch_dem",
      tool_name: "fetch_dem",
      state: "complete",
      parent_step_id: "parent-1",
      duration_ms: 1200,
    });
    const childB = makeStep({
      step_id: "child-b",
      name: "fetch_topobathy",
      tool_name: "fetch_topobathy",
      state: "running",
      parent_step_id: "parent-1",
    });
    const live = snap("pipe-1", [parent, childA, childB]);

    const stepOrder = new Map<string, number>([
      ["parent-1", 1],
      ["child-a", 2],
      ["child-b", 3],
    ]);
    const stream = buildInterleavedStream(
      [],
      [],
      live,
      new Map(),
      stepOrder,
    );
    const tools = toolEntries(stream);
    // Exactly ONE top-level tool card (the parent).
    expect(tools).toHaveLength(1);
    expect(tools[0]!.step.step_id).toBe("parent-1");
    // The two children are attached, in encounter order, NOT top-level.
    expect(tools[0]!.children.map((c) => c.step_id)).toEqual([
      "child-a",
      "child-b",
    ]);
    // No top-level entry carries a child step_id.
    expect(tools.some((t) => t.step.step_id === "child-a")).toBe(false);
    expect(tools.some((t) => t.step.step_id === "child-b")).toBe(false);
  });

  it("a parentless step renders top-level with no children", () => {
    const plain = makeStep({
      step_id: "p1",
      name: "fetch_dem",
      state: "complete",
    });
    const stream = buildInterleavedStream(
      [],
      [],
      snap("pipe-1", [plain]),
      new Map(),
      new Map([["p1", 1]]),
    );
    const tools = toolEntries(stream);
    expect(tools).toHaveLength(1);
    expect(tools[0]!.children).toEqual([]);
  });

  it("a child whose parent we never saw degrades to a top-level card (never dropped)", () => {
    const orphan = makeStep({
      step_id: "orphan-1",
      name: "fetch_dem",
      state: "complete",
      parent_step_id: "ghost-parent",
    });
    const stream = buildInterleavedStream(
      [],
      [],
      snap("pipe-1", [orphan]),
      new Map(),
      new Map([["orphan-1", 1]]),
    );
    const tools = toolEntries(stream);
    expect(tools).toHaveLength(1);
    expect(tools[0]!.step.step_id).toBe("orphan-1");
  });
});

// --- PipelineCard: breadcrumb + nested timeline -------------------------- //

describe("PipelineCard - live breadcrumb (task-168)", () => {
  it("shows the humanized breadcrumb with index/total while running", () => {
    render(
      <PipelineCard
        step={makeStep({
          step_id: "parent-1",
          name: "run_model_flood_scenario",
          state: "running",
          substep_label: "fetch_topobathy",
          substep_index: 2,
          substep_total: 7,
        })}
      />,
    );
    const crumb = screen.getByTestId("pipeline-card-breadcrumb");
    // Humanized child label (never raw snake_case) + "k/total".
    expect(crumb.textContent).toContain("Fetching topobathy");
    expect(crumb.textContent).toContain("2/7");
    expect(crumb.textContent).not.toContain("fetch_topobathy");
  });

  it("shows label + 'step k' when the total is unknown", () => {
    render(
      <PipelineCard
        step={makeStep({
          step_id: "parent-1",
          name: "run_model_flood_scenario",
          state: "running",
          substep_label: "publish_layer",
          substep_index: 3,
          substep_total: null,
        })}
      />,
    );
    const crumb = screen.getByTestId("pipeline-card-breadcrumb");
    expect(crumb.textContent).toContain("Publishing layer");
    expect(crumb.textContent).toContain("step 3");
    expect(crumb.textContent).not.toContain("/");
  });

  it("does NOT show a breadcrumb on a terminal (settled) card", () => {
    // The server clears substep_label on terminal; even if a stale label
    // leaked, a terminal card paints no breadcrumb.
    render(
      <PipelineCard
        step={makeStep({
          step_id: "parent-1",
          name: "run_model_flood_scenario",
          state: "complete",
          substep_label: null,
          duration_ms: 5000,
        })}
      />,
    );
    expect(screen.queryByTestId("pipeline-card-breadcrumb")).toBeNull();
  });
});

describe("PipelineCard - nested timeline (task-168)", () => {
  const children: PipelineStepSummary[] = [
    makeStep({
      step_id: "child-a",
      name: "fetch_topobathy",
      tool_name: "fetch_topobathy",
      state: "complete",
      parent_step_id: "parent-1",
      duration_ms: 2000,
    }),
    makeStep({
      step_id: "child-b",
      name: "run_solver",
      tool_name: "run_solver",
      state: "failed",
      parent_step_id: "parent-1",
      duration_ms: 8000,
    }),
    makeStep({
      step_id: "child-c",
      name: "publish_layer",
      tool_name: "publish_layer",
      state: "complete",
      parent_step_id: "parent-1",
      duration_ms: 1500,
    }),
  ];

  it("collapses the nested timeline by default behind a sub-steps chevron", () => {
    render(
      <PipelineCard
        step={makeStep({
          step_id: "parent-1",
          name: "run_model_flood_scenario",
          state: "complete",
          duration_ms: 12000,
        })}
        children={children}
      />,
    );
    // Chevron present + shows the child count.
    expect(screen.getByTestId("pipeline-card-substeps-toggle")).toBeInTheDocument();
    expect(screen.getByTestId("pipeline-card-substeps-count").textContent).toBe(
      "3",
    );
    // Timeline collapsed by default.
    expect(screen.queryByTestId("pipeline-card-substep-timeline")).toBeNull();
  });

  it("expands to list children with humanized labels + durations", () => {
    render(
      <PipelineCard
        step={makeStep({
          step_id: "parent-1",
          name: "run_model_flood_scenario",
          state: "complete",
          duration_ms: 12000,
        })}
        children={children}
      />,
    );
    fireEvent.click(screen.getByTestId("pipeline-card-substeps-toggle"));
    expect(
      screen.getByTestId("pipeline-card-substep-timeline"),
    ).toBeInTheDocument();
    const rows = screen.getAllByTestId("pipeline-card-substep");
    expect(rows).toHaveLength(3);
    // Humanized labels, never raw snake_case.
    const names = screen
      .getAllByTestId("pipeline-card-substep-name")
      .map((n) => n.textContent ?? "");
    expect(names[0]).toContain("topobathy"); // fetch_topobathy complete -> "Loaded topobathy"
    expect(names.join(" ")).not.toContain("fetch_topobathy");
    expect(names.join(" ")).not.toContain("run_solver");
    expect(names.join(" ")).not.toContain("publish_layer");
    // Durations rendered (2000ms -> "0:02", 8000ms -> "0:08", 1500ms -> "0:01").
    const timers = screen
      .getAllByTestId("pipeline-card-substep-timer")
      .map((t) => t.textContent ?? "");
    expect(timers).toContain("0:02");
    expect(timers).toContain("0:08");
  });

  it("a failed child reads red inline while sibling complete children stay green", () => {
    render(
      <PipelineCard
        step={makeStep({
          step_id: "parent-1",
          name: "run_model_flood_scenario",
          state: "complete",
          duration_ms: 12000,
        })}
        children={children}
      />,
    );
    fireEvent.click(screen.getByTestId("pipeline-card-substeps-toggle"));
    const rows = screen.getAllByTestId("pipeline-card-substep");
    const byId = new Map(
      rows.map((r) => [r.getAttribute("data-step-id"), r]),
    );
    // Failed child row name tints red (#fca5a5); sibling complete rows do not.
    const failedName = byId
      .get("child-b")!
      .querySelector('[data-testid="pipeline-card-substep-name"]') as HTMLElement;
    const okName = byId
      .get("child-a")!
      .querySelector('[data-testid="pipeline-card-substep-name"]') as HTMLElement;
    expect(failedName.style.color).toBe("#fca5a5"); // red inline
    expect(okName.style.color).not.toBe("#fca5a5");
    // The failed-child state survives on the row dataset (honesty floor).
    expect(byId.get("child-b")!.getAttribute("data-state")).toBe("failed");
  });

  it("does not render a sub-steps chevron when there are no children", () => {
    render(
      <PipelineCard
        step={makeStep({
          step_id: "p1",
          name: "fetch_dem",
          state: "complete",
          duration_ms: 1000,
        })}
      />,
    );
    expect(screen.queryByTestId("pipeline-card-substeps-toggle")).toBeNull();
  });
});
