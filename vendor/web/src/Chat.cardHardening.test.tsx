// GRACE-2 web - card-render hardening tests (NATE 2026-06-22, web side of the
// pipeline-card-drop fix; the agent side already landed).
//
// Three independent guards in Chat.tsx:
//   (a) pipelineReducer 'pipeline-state' MERGES a SHORT same-pipeline frame onto
//       the live snapshot (by step_id) instead of WHOLESALE-replacing it, so a
//       partial/short frame can never wipe already-rendered cards. Equal-or-
//       larger cumulative frames keep the wholesale replace.
//   (b) buildInterleavedStream EXCLUDES thinking pseudo-steps from topLevelIds,
//       so a child whose parent_step_id points at an llm_generation/thinking
//       step is NOT swallowed (skipped as a thinking-child AND never rendered).
//   (c) forceMostRecentRunningToFailed prefers the tool_name match over the
//       'latest running step', so an error from tool A cannot flip tool B's
//       card to red under concurrent solves.

import { describe, it, expect } from "vitest";
import {
  pipelineReducer,
  mergeShortFrameOntoLive,
  forceMostRecentRunningToFailed,
  buildInterleavedStream,
  routeError,
  createChatStreams,
  getStream,
  streamKeyFor,
  THINKING_STEP_NAME,
  type PipelineInlineState,
  type InterleavedEntry,
} from "./Chat";
import {
  ErrorPayload,
  PipelineStatePayload,
  PipelineStepSummary,
} from "./contracts";

function step(
  over: Partial<PipelineStepSummary> & {
    step_id: string;
    state: PipelineStepSummary["state"];
  },
): PipelineStepSummary {
  return {
    step_id: over.step_id,
    name: over.name ?? "fetch_dem",
    tool_name: over.tool_name ?? over.name ?? "fetch_dem",
    state: over.state,
    parent_step_id: over.parent_step_id ?? null,
  };
}

function snap(pid: string, steps: PipelineStepSummary[]): PipelineStatePayload {
  return { pipeline_id: pid, steps };
}

const EMPTY: PipelineInlineState = {
  live: null,
  history: [],
  currentPipelineFromSession: null,
};

function toolEntries(stream: InterleavedEntry[]) {
  return stream.filter(
    (e): e is Extract<InterleavedEntry, { kind: "tool" }> => e.kind === "tool",
  );
}

// --- (a) short-frame merge ------------------------------------------------- //

describe("pipelineReducer short-frame merge (NATE 2026-06-22)", () => {
  it("MERGES a SHORT same-pipeline frame instead of wiping live cards", () => {
    // Live snapshot has THREE running cards.
    const live = snap("pipe-1", [
      step({ step_id: "a", state: "running" }),
      step({ step_id: "b", state: "running" }),
      step({ step_id: "c", state: "running" }),
    ]);
    const seeded = pipelineReducer(EMPTY, { type: "pipeline-state", payload: live });
    expect(seeded.live?.steps).toHaveLength(3);

    // A SHORT frame for the SAME pipeline carries only ONE step (b -> complete).
    const short = snap("pipe-1", [step({ step_id: "b", state: "complete" })]);
    const merged = pipelineReducer(seeded, { type: "pipeline-state", payload: short });

    // All three cards survive; b took the incoming complete state.
    const ids = (merged.live?.steps ?? []).map((s) => s.step_id);
    expect(ids).toEqual(["a", "b", "c"]);
    const byId = new Map((merged.live?.steps ?? []).map((s) => [s.step_id, s]));
    expect(byId.get("a")!.state).toBe("running");
    expect(byId.get("b")!.state).toBe("complete");
    expect(byId.get("c")!.state).toBe("running");
  });

  it("WHOLESALE-replaces an EQUAL-or-LARGER cumulative frame (contract path)", () => {
    const live = snap("pipe-1", [
      step({ step_id: "a", state: "running" }),
      step({ step_id: "b", state: "running" }),
    ]);
    const seeded = pipelineReducer(EMPTY, { type: "pipeline-state", payload: live });
    // A LARGER frame (3 steps) supersedes the prior one verbatim.
    const grown = snap("pipe-1", [
      step({ step_id: "a", state: "complete" }),
      step({ step_id: "b", state: "running" }),
      step({ step_id: "d", state: "running" }),
    ]);
    const out = pipelineReducer(seeded, { type: "pipeline-state", payload: grown });
    expect((out.live?.steps ?? []).map((s) => s.step_id)).toEqual(["a", "b", "d"]);
  });

  it("a SHORT frame for a DIFFERENT pipeline does NOT merge (archives prior live)", () => {
    const live = snap("pipe-1", [
      step({ step_id: "a", state: "running" }),
      step({ step_id: "b", state: "running" }),
    ]);
    const seeded = pipelineReducer(EMPTY, { type: "pipeline-state", payload: live });
    const other = snap("pipe-2", [step({ step_id: "z", state: "running" })]);
    const out = pipelineReducer(seeded, { type: "pipeline-state", payload: other });
    // The new (different) pipeline is live verbatim; the old one is archived.
    expect((out.live?.steps ?? []).map((s) => s.step_id)).toEqual(["z"]);
    expect(out.history).toHaveLength(1);
    expect(out.history[0]!.pipeline_id).toBe("pipe-1");
  });

  it("mergeShortFrameOntoLive keeps live order + appends incoming-only ids", () => {
    const live = snap("pipe-1", [
      step({ step_id: "a", state: "running" }),
      step({ step_id: "b", state: "running" }),
    ]);
    const short = snap("pipe-1", [
      step({ step_id: "b", state: "complete" }),
      step({ step_id: "x", state: "running" }), // incoming-only
    ]);
    const merged = mergeShortFrameOntoLive(live, short);
    const mergedSteps = merged.steps ?? [];
    expect(mergedSteps.map((s) => s.step_id)).toEqual(["a", "b", "x"]);
    expect(mergedSteps.find((s) => s.step_id === "b")!.state).toBe("complete");
  });
});

// --- (b) thinking parents do not swallow children -------------------------- //

describe("buildInterleavedStream excludes thinking steps from topLevelIds", () => {
  it("renders a child whose parent_step_id points at a thinking step (not swallowed)", () => {
    const thinking = step({
      step_id: "think-1",
      name: THINKING_STEP_NAME,
      tool_name: "gemini",
      state: "running",
    });
    // A real tool card that (mis)attributes its parent to the thinking step.
    const orphanChild = step({
      step_id: "tool-1",
      name: "fetch_dem",
      tool_name: "fetch_dem",
      state: "complete",
      parent_step_id: "think-1",
    });
    const live = snap("pipe-1", [thinking, orphanChild]);
    const stream = buildInterleavedStream(
      [],
      [],
      live,
      new Map(),
      new Map<string, number>([
        ["think-1", 1],
        ["tool-1", 2],
      ]),
    );
    const tools = toolEntries(stream);
    // The thinking step is filtered out; the child is NOT nested under it
    // (thinking is excluded from topLevelIds) so it renders as its OWN card.
    expect(tools.map((t) => t.step.step_id)).toEqual(["tool-1"]);
  });

  it("still nests a child under a REAL (non-thinking) parent", () => {
    const parent = step({ step_id: "p", name: "run_model", tool_name: "run_model", state: "running" });
    const child = step({
      step_id: "c",
      name: "fetch_dem",
      tool_name: "fetch_dem",
      state: "complete",
      parent_step_id: "p",
    });
    const live = snap("pipe-1", [parent, child]);
    const stream = buildInterleavedStream(
      [],
      [],
      live,
      new Map(),
      new Map<string, number>([
        ["p", 1],
        ["c", 2],
      ]),
    );
    const tools = toolEntries(stream);
    expect(tools).toHaveLength(1);
    expect(tools[0]!.step.step_id).toBe("p");
    expect(tools[0]!.children.map((c) => c.step_id)).toEqual(["c"]);
  });
});

// --- (c) error tool_name match wins over latest-running -------------------- //

describe("forceMostRecentRunningToFailed prefers tool_name match", () => {
  const err: ErrorPayload = { error_code: "TOOL_TIMEOUT", message: "A timed out" };

  it("flips the tool_name-matched card, NOT the latest running step", () => {
    // Two concurrent running solves: toolA (earlier) + toolB (later).
    const state: PipelineInlineState = {
      live: snap("pipe-1", [
        step({ step_id: "sa", name: "run_a", tool_name: "run_a", state: "running" }),
        step({ step_id: "sb", name: "run_b", tool_name: "run_b", state: "running" }),
      ]),
      history: [],
      currentPipelineFromSession: null,
    };
    // An error attributed to run_a (the EARLIER step) must flip run_a, even though
    // run_b is the most-recent running step.
    const out = forceMostRecentRunningToFailed(state, err, "run_a");
    const byId = new Map((out.live?.steps ?? []).map((s) => [s.step_id, s]));
    expect(byId.get("sa")!.state).toBe("failed");
    expect(byId.get("sb")!.state).toBe("running"); // untouched
  });

  it("falls back to the latest running step when tool_name is null", () => {
    const state: PipelineInlineState = {
      live: snap("pipe-1", [
        step({ step_id: "sa", name: "run_a", tool_name: "run_a", state: "running" }),
        step({ step_id: "sb", name: "run_b", tool_name: "run_b", state: "running" }),
      ]),
      history: [],
      currentPipelineFromSession: null,
    };
    const out = forceMostRecentRunningToFailed(state, err, null);
    const byId = new Map((out.live?.steps ?? []).map((s) => [s.step_id, s]));
    expect(byId.get("sb")!.state).toBe("failed"); // latest running
    expect(byId.get("sa")!.state).toBe("running");
  });

  it("routeError threads ErrorPayload.tool_name through to the matched card", () => {
    const cs = createChatStreams();
    // Seed two running cards in the (untagged) root stream - the same key
    // routeError resolves for caseId=null (owningKey(cs, null) === targetKey).
    const key = streamKeyFor(null);
    const s = getStream(cs, key);
    s.pipeline = pipelineReducer(s.pipeline, {
      type: "pipeline-state",
      payload: snap("pipe-1", [
        step({ step_id: "sa", name: "run_a", tool_name: "run_a", state: "running" }),
        step({ step_id: "sb", name: "run_b", tool_name: "run_b", state: "running" }),
      ]),
    });
    // The error carries tool_name=run_a (a future-amendment field).
    const errWithTool: ErrorPayload & { tool_name: string } = {
      error_code: "SOLVER_FAILED",
      message: "run_a failed",
      tool_name: "run_a",
    };
    routeError(cs, errWithTool, null);
    const after = getStream(cs, key).pipeline;
    // run_a is failed; run_b stays running across (live ∪ history).
    const all = [...after.history, ...(after.live ? [after.live] : [])].flatMap(
      (snp) => snp.steps ?? [],
    );
    const byId = new Map(all.map((st) => [st.step_id, st]));
    expect(byId.get("sa")!.state).toBe("failed");
    expect(byId.get("sb")!.state).toBe("running");
  });
});
