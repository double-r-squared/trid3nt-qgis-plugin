// GRACE-2 web - task-168 nested sub-step READ-ONLY persistence/replay tests.
//
// The live nested sub-step feature (committed 256a587) renders a composer's
// internal atomic-tool calls as CHILD steps under the parent workflow card.
// This file pins the REMAINING work: those children must PERSIST and REPLAY
// read-only on Case reopen - warm (via the agent) AND box-off cold (via the
// serverless snapshot). BOTH paths feed the SAME CaseOpenEnvelopePayload through
// routeCaseOpen -> replayStreamFromChatHistory, so exercising that one helper
// covers both. No re-execution: these are terminal persisted records.
//
// A persisted tool-card row with children[] must rebuild the SAME nested
// timeline the live feature renders (parent card + expandable children timeline
// with humanized labels + durations + honest failed-child state); children must
// NOT render as their own top-level cards. A row with NO children must replay
// exactly as before (back-compat).
//
// Pure-helper pattern (Chat cannot mount in happy-dom). For the rendered
// timeline we feed the rebuilt children straight into PipelineCard, the same
// component InterleavedChatStream mounts.

import { describe, it, expect, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import {
  emptyStreamState,
  replayStreamFromChatHistory,
  buildInterleavedStream,
  InterleavedEntry,
} from "./Chat";
import { PipelineCard } from "./components/PipelineCard";
import { CaseChatMessage, PersistedSubStepRecord } from "./contracts";

afterEach(() => cleanup());

const CASE_ID = "01CASEAAAAAAAAAAAAAAAAAAAA";

function childRecord(
  partial: Partial<PersistedSubStepRecord> & {
    step_id: string;
    tool_name: string;
    state: "complete" | "failed";
  },
): PersistedSubStepRecord {
  return { ...partial };
}

/** A full-stream history: user -> tool card WITH children -> agent. The tool
 *  card is the top-level composer workflow; its children are the internal
 *  atomic-tool calls. */
function historyWithChildren(
  children: PersistedSubStepRecord[],
): CaseChatMessage[] {
  return [
    {
      message_id: "01MSGUSER0000000000000000A",
      case_id: CASE_ID,
      role: "user",
      content: "model the flood",
      created_at: "2026-06-10T00:00:00Z",
    },
    {
      message_id: "01MSGTOOL0000000000000000B",
      case_id: CASE_ID,
      role: "tool",
      content: '{"tool_name":"run_model_flood_scenario","state":"complete"}',
      pipeline_id: "01PIPE0000000000000000000C",
      tool_card: {
        tool_name: "run_model_flood_scenario",
        state: "complete",
        started_at: "2026-06-10T00:00:01Z",
        duration_ms: 12000,
        label: "run_model_flood_scenario",
        children,
      },
      created_at: "2026-06-10T00:00:01Z",
    },
    {
      message_id: "01MSGAGNT0000000000000000D",
      case_id: CASE_ID,
      role: "agent",
      content: "I modeled the flood and added the layers.",
      created_at: "2026-06-10T00:00:13Z",
    },
  ];
}

const THREE_CHILDREN: PersistedSubStepRecord[] = [
  childRecord({
    step_id: "wire-child-a",
    parent_step_id: "wire-parent",
    name: "fetch_topobathy",
    tool_name: "fetch_topobathy",
    state: "complete",
    duration_ms: 2000,
  }),
  childRecord({
    step_id: "wire-child-b",
    parent_step_id: "wire-parent",
    name: "run_solver",
    tool_name: "run_solver",
    state: "failed",
    duration_ms: 8000,
    error_code: "SOLVER_FAILED",
    error_message: "mesh diverged",
  }),
  childRecord({
    step_id: "wire-child-c",
    parent_step_id: "wire-parent",
    name: "publish_layer",
    tool_name: "publish_layer",
    state: "complete",
    duration_ms: 1500,
  }),
];

function toolEntries(stream: InterleavedEntry[]) {
  return stream.filter(
    (e): e is Extract<InterleavedEntry, { kind: "tool" }> => e.kind === "tool",
  );
}

describe("replayStreamFromChatHistory - nested sub-steps (task-168)", () => {
  it("rebuilds the parent card + nested children timeline from children[]", () => {
    const s = emptyStreamState();
    replayStreamFromChatHistory(s, historyWithChildren(THREE_CHILDREN));

    // ONE synthesized snapshot: parent step + the three children re-parented to
    // the synthesized replay parent step_id.
    expect(s.pipeline.history).toHaveLength(1);
    const steps = s.pipeline.history[0]!.steps!;
    expect(steps).toHaveLength(4);

    const parent = steps[0]!;
    expect(parent.tool_name).toBe("run_model_flood_scenario");
    expect(parent.state).toBe("complete");
    expect(parent.duration_ms).toBe(12000);
    expect(parent.parent_step_id ?? null).toBeNull();

    // Every child re-parented to the parent's synthesized step_id (NOT the
    // wire-only persisted ids, which are absent from the replayed snapshot).
    const parentId = parent.step_id;
    const children = steps.slice(1);
    expect(children.every((c) => c.parent_step_id === parentId)).toBe(true);
    // Order + content preserved off the persisted records.
    expect(children.map((c) => c.tool_name)).toEqual([
      "fetch_topobathy",
      "run_solver",
      "publish_layer",
    ]);
    expect(children.map((c) => c.state)).toEqual([
      "complete",
      "failed",
      "complete",
    ]);
    expect(children.map((c) => c.duration_ms)).toEqual([2000, 8000, 1500]);
    // Honest failed-child error metadata survives replay.
    expect(children[1]!.error_code).toBe("SOLVER_FAILED");
    expect(children[1]!.error_message).toBe("mesh diverged");
  });

  it("interleaves ONE top-level parent card; children are NOT top-level", () => {
    const s = emptyStreamState();
    replayStreamFromChatHistory(s, historyWithChildren(THREE_CHILDREN));
    const stream = buildInterleavedStream(
      s.messages,
      s.pipeline.history,
      s.pipeline.live,
      s.messageOrder,
      s.stepOrder,
    );
    // Stream order: user -> tool -> agent (children do not add top-level rows).
    expect(stream.map((e) => e.kind)).toEqual([
      "user-message",
      "tool",
      "agent-message",
    ]);
    const tools = toolEntries(stream);
    expect(tools).toHaveLength(1);
    expect(tools[0]!.step.tool_name).toBe("run_model_flood_scenario");
    // The three children are attached to the parent entry in order, never as
    // their own top-level cards.
    expect(tools[0]!.children.map((c) => c.tool_name)).toEqual([
      "fetch_topobathy",
      "run_solver",
      "publish_layer",
    ]);
    expect(
      tools.some((t) => t.step.tool_name === "fetch_topobathy"),
    ).toBe(false);
  });

  it("renders the replayed parent card with an expandable children timeline (humanized labels + durations)", () => {
    const s = emptyStreamState();
    replayStreamFromChatHistory(s, historyWithChildren(THREE_CHILDREN));
    const stream = buildInterleavedStream(
      s.messages,
      s.pipeline.history,
      s.pipeline.live,
      s.messageOrder,
      s.stepOrder,
    );
    const tool = toolEntries(stream)[0]!;

    // Mount the parent card with the rebuilt children, exactly as the
    // InterleavedChatStream does.
    render(<PipelineCard step={tool.step} children={tool.children} />);

    // A historical (terminal) parent paints NO live breadcrumb - the nested
    // timeline lives behind the sub-steps chevron only.
    expect(screen.queryByTestId("pipeline-card-breadcrumb")).toBeNull();
    expect(
      screen.getByTestId("pipeline-card-substeps-toggle"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("pipeline-card-substeps-count").textContent,
    ).toBe("3");
    // Collapsed by default.
    expect(screen.queryByTestId("pipeline-card-substep-timeline")).toBeNull();

    // Expand the historical timeline.
    fireEvent.click(screen.getByTestId("pipeline-card-substeps-toggle"));
    expect(
      screen.getByTestId("pipeline-card-substep-timeline"),
    ).toBeInTheDocument();
    const rows = screen.getAllByTestId("pipeline-card-substep");
    expect(rows).toHaveLength(3);

    // Humanized labels - never raw snake_case.
    const names = screen
      .getAllByTestId("pipeline-card-substep-name")
      .map((n) => n.textContent ?? "");
    const joined = names.join(" ");
    expect(joined).not.toContain("fetch_topobathy");
    expect(joined).not.toContain("run_solver");
    expect(joined).not.toContain("publish_layer");
    expect(joined).toContain("topobathy");

    // Durations rendered (2000ms -> "0:02", 8000ms -> "0:08", 1500ms -> "0:01").
    const timers = screen
      .getAllByTestId("pipeline-card-substep-timer")
      .map((t) => t.textContent ?? "");
    expect(timers).toContain("0:02");
    expect(timers).toContain("0:08");
    expect(timers).toContain("0:01");

    // The failed child reads red while the complete siblings do not; honest
    // state survives on the row dataset.
    const byId = new Map(
      rows.map((r) => [r.getAttribute("data-step-id"), r] as const),
    );
    const failedRow = rows.find((r) => r.getAttribute("data-state") === "failed");
    expect(failedRow).toBeDefined();
    const failedName = failedRow!.querySelector(
      '[data-testid="pipeline-card-substep-name"]',
    ) as HTMLElement;
    expect(failedName.style.color).toBe("#fca5a5");
    expect(byId.size).toBe(3);
  });

  it("rehydrates a child's IO drop-down when the persisted child carried IO", () => {
    const s = emptyStreamState();
    const childWithIo = childRecord({
      step_id: "wire-child-io",
      parent_step_id: "wire-parent",
      name: "fetch_topobathy",
      tool_name: "fetch_topobathy",
      state: "complete",
      duration_ms: 2000,
      raw_args: '{"bbox":[1,2,3,4]}',
      function_response: '{"status":"ok"}',
      args_bytes: 18,
      response_bytes: 15,
    });
    replayStreamFromChatHistory(s, historyWithChildren([childWithIo]));

    // The child's synthesized step_id keys the rebuilt ToolIoPayload. The parent
    // is step 0; the single child is step 1, re-parented to the parent id with a
    // deterministic ``<parentId>-child-0`` step_id.
    const steps = s.pipeline.history[0]!.steps!;
    const childStepId = steps[1]!.step_id;
    const io = s.toolIo.get(childStepId);
    expect(io).toBeDefined();
    expect(io!.tool_name).toBe("fetch_topobathy");
    expect(io!.raw_args).toBe('{"bbox":[1,2,3,4]}');
    expect(io!.function_response).toBe('{"status":"ok"}');
    expect(io!.is_error).toBe(false);
  });
});

// --- Back-compat: a row with NO children replays exactly as before ---------- //

describe("replayStreamFromChatHistory - back-compat (no children)", () => {
  function plainHistory(): CaseChatMessage[] {
    return [
      {
        message_id: "01MSGUSER0000000000000000A",
        case_id: CASE_ID,
        role: "user",
        content: "fetch the DEM",
        created_at: "2026-06-10T00:00:00Z",
      },
      {
        message_id: "01MSGTOOL0000000000000000B",
        case_id: CASE_ID,
        role: "tool",
        content: '{"tool_name":"fetch_3dep_dem","state":"complete"}',
        pipeline_id: "01PIPE0000000000000000000C",
        // No children field at all - a pre-task-168 persisted document.
        tool_card: {
          tool_name: "fetch_3dep_dem",
          state: "complete",
          started_at: "2026-06-10T00:00:01Z",
          duration_ms: 2340,
          label: "fetch_3dep_dem",
        },
        created_at: "2026-06-10T00:00:01Z",
      },
      {
        message_id: "01MSGAGNT0000000000000000D",
        case_id: CASE_ID,
        role: "agent",
        content: "I fetched the DEM.",
        created_at: "2026-06-10T00:00:05Z",
      },
    ];
  }

  it("a row with NO children replays as a single top-level card, no nested timeline", () => {
    const s = emptyStreamState();
    replayStreamFromChatHistory(s, plainHistory());

    // Exactly ONE step in the snapshot (the top-level card) - no synthesized
    // child steps.
    expect(s.pipeline.history).toHaveLength(1);
    const steps = s.pipeline.history[0]!.steps!;
    expect(steps).toHaveLength(1);
    expect(steps[0]!.tool_name).toBe("fetch_3dep_dem");
    expect(steps[0]!.duration_ms).toBe(2340);

    const stream = buildInterleavedStream(
      s.messages,
      s.pipeline.history,
      s.pipeline.live,
      s.messageOrder,
      s.stepOrder,
    );
    expect(stream.map((e) => e.kind)).toEqual([
      "user-message",
      "tool",
      "agent-message",
    ]);
    const tool = toolEntries(stream)[0]!;
    expect(tool.children).toEqual([]);

    // Rendered: no sub-steps chevron when there are no children.
    render(<PipelineCard step={tool.step} children={tool.children} />);
    expect(screen.queryByTestId("pipeline-card-substeps-toggle")).toBeNull();
  });

  it("an explicit empty children[] also replays as a plain card (additive null/empty)", () => {
    const s = emptyStreamState();
    const h = plainHistory();
    h[1] = {
      ...h[1]!,
      tool_card: { ...h[1]!.tool_card!, children: [] },
    };
    replayStreamFromChatHistory(s, h);
    const steps = s.pipeline.history[0]!.steps!;
    expect(steps).toHaveLength(1);
    const stream = buildInterleavedStream(
      s.messages,
      s.pipeline.history,
      s.pipeline.live,
      s.messageOrder,
      s.stepOrder,
    );
    expect(toolEntries(stream)[0]!.children).toEqual([]);
  });
});
