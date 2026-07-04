// GRACE-2 web — lane W2 tests: tool-card IO rehydrate (C1), the live
// "Running…" output placeholder (FIX 2), and turn-complete / force-complete of
// stuck running cards (C2).
//
// Pure-helper pattern (Chat cannot mount in happy-dom — it opens a WebSocket),
// same as Chat.replayToolCards.test.tsx / Chat.perCaseStreams.test.tsx: exercise
// replayStreamFromChatHistory / toolIoFromCardRecord / resolveCardIo /
// routeTurnComplete / routeSessionState / forceRunningStepsToComplete /
// pipelineReducer directly.

import { describe, it, expect } from "vitest";
import {
  createChatStreams,
  emptyStreamState,
  getStream,
  routeUserMessage,
  routePipelineState,
  routeTurnComplete,
  routeSessionState,
  replayStreamFromChatHistory,
  toolIoFromCardRecord,
  resolveCardIo,
  forceRunningStepsToComplete,
  pipelineReducer,
  streamKeyFor,
  RUNNING_IO_PLACEHOLDER,
  type PipelineInlineState,
} from "./Chat";
import {
  CaseChatMessage,
  PipelineStatePayload,
  PipelineStepSummary,
  ToolCardRecord,
} from "./contracts";

const CASE_ID = "01CASEAAAAAAAAAAAAAAAAAAAA";

function runningStep(over: Partial<PipelineStepSummary> = {}): PipelineStepSummary {
  return {
    step_id: over.step_id ?? "s1",
    name: over.name ?? "fetch_3dep_dem",
    tool_name: over.tool_name ?? "fetch_3dep_dem",
    state: over.state ?? "running",
    ...over,
  };
}

function snap(steps: PipelineStepSummary[], pid = "01PIPE000000000000000000A"): PipelineStatePayload {
  return { pipeline_id: pid, steps };
}

// --- C1: tool-card IO rehydrate ------------------------------------------- //

describe("toolIoFromCardRecord (C1)", () => {
  it("rebuilds a ToolIoPayload from the persisted IO fields (same names as live)", () => {
    const card: ToolCardRecord = {
      tool_name: "geocode_location",
      state: "complete",
      raw_args: '{"location_name":"Boulder, CO"}',
      function_response: '{"status":"ok","bbox":[-105.3,39.9,-105.2,40.1]}',
      is_error: false,
      args_truncated: false,
      response_truncated: true,
      args_bytes: 31,
      response_bytes: 999999,
    };
    const io = toolIoFromCardRecord("replay-01MSG", card);
    expect(io).not.toBeNull();
    expect(io!.step_id).toBe("replay-01MSG");
    expect(io!.tool_name).toBe("geocode_location");
    expect(io!.raw_args).toBe('{"location_name":"Boulder, CO"}');
    expect(io!.function_response).toContain('"status":"ok"');
    expect(io!.response_truncated).toBe(true);
    expect(io!.response_bytes).toBe(999999);
  });

  it("returns null for a pre-C1 card with no persisted IO (chevron stays absent)", () => {
    const card: ToolCardRecord = { tool_name: "fetch_3dep_dem", state: "complete" };
    expect(toolIoFromCardRecord("replay-x", card)).toBeNull();
  });

  it("infers is_error from a failed card when the flag is absent", () => {
    const card: ToolCardRecord = {
      tool_name: "run_solver",
      state: "failed",
      function_response: '{"status":"error","message":"solver crashed"}',
    };
    const io = toolIoFromCardRecord("replay-y", card)!;
    expect(io.is_error).toBe(true);
  });
});

describe("replayStreamFromChatHistory rehydrates the IO drop-down (C1)", () => {
  function historyWithIo(): CaseChatMessage[] {
    return [
      {
        message_id: "01MSGUSER0000000000000000A",
        case_id: CASE_ID,
        role: "user",
        content: "geocode boulder",
        created_at: "2026-06-10T00:00:00Z",
      },
      {
        message_id: "01MSGTOOL0000000000000000B",
        case_id: CASE_ID,
        role: "tool",
        content: "{}",
        pipeline_id: "01PIPE0000000000000000000C",
        tool_card: {
          tool_name: "geocode_location",
          state: "complete",
          duration_ms: 120,
          label: "geocode_location",
          raw_args: '{"location_name":"Boulder, CO"}',
          function_response: '{"status":"ok"}',
          is_error: false,
          args_bytes: 31,
          response_bytes: 16,
        },
        created_at: "2026-06-10T00:00:01Z",
      },
    ];
  }

  it("populates s.toolIo keyed by the synthesized replay step_id", () => {
    const s = emptyStreamState();
    replayStreamFromChatHistory(s, historyWithIo());
    // The replayed step_id is `replay-<message_id>` — the exact key the render
    // looks up via toolIo.get(entry.step.step_id).
    const stepId = "replay-01MSGTOOL0000000000000000B";
    expect(s.pipeline.history[0]!.steps![0]!.step_id).toBe(stepId);
    expect(s.toolIo.get(stepId)).toBeDefined();
    expect(s.toolIo.get(stepId)!.raw_args).toBe('{"location_name":"Boulder, CO"}');
    expect(s.toolIo.get(stepId)!.function_response).toBe('{"status":"ok"}');
  });

  it("leaves toolIo empty when the replayed card carried no persisted IO", () => {
    const hist = historyWithIo();
    // Strip the IO fields → a pre-C1 card.
    hist[1] = {
      ...hist[1]!,
      tool_card: { tool_name: "geocode_location", state: "complete" },
    };
    const s = emptyStreamState();
    replayStreamFromChatHistory(s, hist);
    expect(s.toolIo.size).toBe(0);
  });
});

// --- FIX 2: live "Running…" output placeholder ---------------------------- //

describe("resolveCardIo (FIX 2 — live Running… placeholder)", () => {
  it("synthesizes a placeholder io for a running step with no io yet (chevron renders)", () => {
    const io = resolveCardIo(runningStep(), undefined);
    expect(io).not.toBeNull();
    expect(io!.function_response).toBe(RUNNING_IO_PLACEHOLDER);
    expect(io!.raw_args).toBe("");
  });

  it("keeps the input but swaps an EMPTY output for Running… on a running step", () => {
    const early = {
      step_id: "s1",
      tool_name: "fetch_3dep_dem",
      raw_args: '{"bbox":[-105,39,-104,40]}',
      function_response: "",
      is_error: false,
      args_truncated: false,
      response_truncated: false,
      args_bytes: 24,
      response_bytes: 0,
    };
    const io = resolveCardIo(runningStep(), early)!;
    expect(io.raw_args).toBe('{"bbox":[-105,39,-104,40]}');
    expect(io.function_response).toBe(RUNNING_IO_PLACEHOLDER);
  });

  it("does NOT override a running step whose response already landed", () => {
    const withResp = {
      step_id: "s1",
      tool_name: "fetch_3dep_dem",
      raw_args: "{}",
      function_response: '{"status":"ok"}',
      is_error: false,
      args_truncated: false,
      response_truncated: false,
      args_bytes: 2,
      response_bytes: 16,
    };
    expect(resolveCardIo(runningStep(), withResp)!.function_response).toBe(
      '{"status":"ok"}',
    );
  });

  it("returns the real io verbatim for a terminal (complete) step", () => {
    const final = {
      step_id: "s1",
      tool_name: "fetch_3dep_dem",
      raw_args: "{}",
      function_response: '{"status":"ok"}',
      is_error: false,
      args_truncated: false,
      response_truncated: false,
      args_bytes: 2,
      response_bytes: 16,
    };
    expect(resolveCardIo(runningStep({ state: "complete" }), final)).toBe(final);
    // No io + non-running → null (no fabricated chevron on a completed card).
    expect(resolveCardIo(runningStep({ state: "complete" }), null)).toBeNull();
  });
});

// --- FIX 3 (NATE 2026-06-26): no fabricated IO chevron on a compute card --- //
//
// A compute-role step (the sim / solve dispatch twin) never receives a real
// tool-io envelope, so the synthetic RUNNING_IO_PLACEHOLDER below would render
// an empty "Running…" IO chevron the solve card should not have. resolveCardIo
// must short-circuit to null for a running compute step (no chevron), while
// still fabricating the placeholder for ordinary TOOL-role cards.
describe("resolveCardIo (FIX 3 — compute-role cards get NO fabricated chevron)", () => {
  it("returns null for a RUNNING compute-role step with no io (no chevron)", () => {
    expect(
      resolveCardIo(runningStep({ role: "compute" }), undefined),
    ).toBeNull();
    expect(resolveCardIo(runningStep({ role: "compute" }), null)).toBeNull();
  });

  it("passes a REAL io through verbatim for a compute step (never fabricates)", () => {
    const realIo = {
      step_id: "s1",
      tool_name: "run_solver",
      raw_args: '{"engine":"sfincs"}',
      function_response: '{"status":"ok"}',
      is_error: false,
      args_truncated: false,
      response_truncated: false,
      args_bytes: 20,
      response_bytes: 16,
    };
    // A real io still flows (in case one ever lands) — only the synthetic
    // placeholder is suppressed for compute cards.
    expect(resolveCardIo(runningStep({ role: "compute" }), realIo)).toBe(realIo);
  });

  it("STILL fabricates the placeholder for a running TOOL-role card (no regression)", () => {
    const io = resolveCardIo(runningStep({ role: "tool" }), undefined);
    expect(io).not.toBeNull();
    expect(io!.function_response).toBe(RUNNING_IO_PLACEHOLDER);
    // The default (role omitted) is the tool path — also still fabricates.
    const ioDefault = resolveCardIo(runningStep(), undefined);
    expect(ioDefault!.function_response).toBe(RUNNING_IO_PLACEHOLDER);
  });
});

// --- C2: force-complete stuck running cards ------------------------------- //

describe("forceRunningStepsToComplete (C2)", () => {
  it("flips every running step across live + history to complete", () => {
    const state: PipelineInlineState = {
      live: snap([runningStep({ step_id: "live1" })]),
      history: [snap([runningStep({ step_id: "h1" }), runningStep({ step_id: "h2", state: "complete" })], "pidH")],
      currentPipelineFromSession: null,
    };
    const out = forceRunningStepsToComplete(state);
    expect(out.live!.steps![0]!.state).toBe("complete");
    expect(out.history[0]!.steps![0]!.state).toBe("complete");
    // An already-complete sibling is untouched.
    expect(out.history[0]!.steps![1]!.state).toBe("complete");
  });

  it("is a no-op (and does not mark failure) when nothing is running", () => {
    const state: PipelineInlineState = {
      live: null,
      history: [snap([runningStep({ step_id: "h1", state: "complete" })], "pidH")],
      currentPipelineFromSession: null,
    };
    const out = forceRunningStepsToComplete(state);
    expect(out.history[0]!.steps![0]!.state).toBe("complete");
  });
});

describe("routeTurnComplete + pipelineReducer turn-complete (C2)", () => {
  it("force-completes a card stuck running and clears the in-flight pipeline", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, streamKeyFor(CASE_ID), "fetch dem");
    // A running tool card lands but its terminal frame is LOST.
    routePipelineState(cs, snap([runningStep({ step_id: "s1" })]), CASE_ID);
    // Mid-turn session-state keeps current_pipeline set → no premature complete.
    routeSessionState(
      cs,
      { current_pipeline: { pipeline_id: "p1", steps: [runningStep({ step_id: "s1" })] } } as never,
      CASE_ID,
    );
    let s = getStream(cs, CASE_ID);
    // Still running before the turn ends.
    const runningNow =
      (s.pipeline.live?.steps?.some((x) => x.state === "running") ?? false) ||
      s.pipeline.history.some((h) => h.steps?.some((x) => x.state === "running"));
    expect(runningNow).toBe(true);

    // The end-of-turn signal arrives.
    routeTurnComplete(cs, {}, CASE_ID);
    s = getStream(cs, CASE_ID);
    const stillRunning =
      (s.pipeline.live?.steps?.some((x) => x.state === "running") ?? false) ||
      s.pipeline.history.some((h) => h.steps?.some((x) => x.state === "running"));
    expect(stillRunning).toBe(false);
    expect(s.pipeline.currentPipelineFromSession).toBeNull();
  });

  it("a session-state with current_pipeline=null also force-completes (resume belt)", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, streamKeyFor(CASE_ID), "x");
    routePipelineState(cs, snap([runningStep({ step_id: "s1" })]), CASE_ID);
    // Resume re-emits a session-state with no in-flight pipeline.
    routeSessionState(cs, { current_pipeline: null } as never, CASE_ID);
    const s = getStream(cs, CASE_ID);
    const stillRunning =
      (s.pipeline.live?.steps?.some((x) => x.state === "running") ?? false) ||
      s.pipeline.history.some((h) => h.steps?.some((x) => x.state === "running"));
    expect(stillRunning).toBe(false);
  });

  it("turn-complete is idempotent and harmless with no running cards", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, streamKeyFor(CASE_ID), "x");
    routePipelineState(cs, snap([runningStep({ step_id: "s1", state: "complete" })]), CASE_ID);
    routeTurnComplete(cs, {}, CASE_ID);
    routeTurnComplete(cs, {}, CASE_ID); // duplicate / fanned-out copy
    const s = getStream(cs, CASE_ID);
    // The completed card stays complete; nothing crashes.
    const states = [
      ...(s.pipeline.live?.steps ?? []),
      ...s.pipeline.history.flatMap((h) => h.steps ?? []),
    ].map((x) => x.state);
    expect(states.every((st) => st === "complete")).toBe(true);
  });

  it("does not mark a lost-frame card as FAILED (settles to complete, not red)", () => {
    const base: PipelineInlineState = {
      live: snap([runningStep({ step_id: "s1" })]),
      history: [],
      currentPipelineFromSession: null,
    };
    const out = pipelineReducer(base, { type: "turn-complete" });
    // The step archived into history (live cleared) and is complete, never failed.
    const settled = out.history.flatMap((h) => h.steps ?? []).find((x) => x.step_id === "s1");
    expect(settled?.state).toBe("complete");
  });
});
