// GRACE-2 web — Chat inline pipeline card tests (job-0064).
//
// Verifies:
//   1. pipeline-state arrives → inline card appears with correct name + %.
//   2. Multiple steps → multiple cards stacked in call order.
//   3. Step completion (all terminal) → "done" state cards in history group.
//   4. shouldShowCancel predicate (exported for testability).
//
// Chat itself cannot be fully mounted in happy-dom (it creates a WebSocket),
// so:
//   - The pipelineReducer logic is tested via shouldShowCancel + a minimal
//     state exerciser.
//   - PipelineCard rendering is tested directly (its own test suite).
//   - PipelineStepGroup is tested via PipelineCard (transitively).
//
// This follows the same pattern as App.test.tsx (App mounts WebSocket +
// WebGL, which happy-dom can't run; logic extracted into pure helpers).

import { describe, it, expect, afterEach } from "vitest";
import {
  shouldShowCancel,
  mergeStepsByStepId,
  forceMostRecentRunningToFailed,
  pipelineReducer,
  PipelineInlineState,
  buildInterleavedStream,
  InterleavedEntry,
  isThinkingActive,
  isThinkingStep,
  THINKING_STEP_NAME,
  desktopChatContainerStyle,
  mobileSheetContainerStyle,
  readChatWidth,
  writeChatWidth,
  clampChatWidth,
  readChatOpacity,
  writeChatOpacity,
  clampChatOpacityTier,
  chatOpacityAlphas,
  CHAT_OPACITY_DEFAULT,
  CHAT_OPACITY_TIERS,
  LS_CHAT_OPACITY,
  CHAT_OPACITY_CHANGED_EVENT,
  type ChatOpacityTier,
} from "./Chat";
import {
  ErrorPayload,
  PipelineStatePayload,
  PipelineStepSummary,
  CredentialRequestPayload,
  PayloadWarningEnvelopePayload,
} from "./contracts";

// --- shouldShowCancel predicate ------------------------------------------ //

function makeStep(
  id: string,
  state: PipelineStepSummary["state"],
  progress?: number,
): PipelineStepSummary {
  return {
    step_id: id,
    name: `op_${id}`,
    tool_name: `tool_${id}`,
    state,
    progress_percent: progress,
  };
}

function makePipelineState(steps: PipelineStepSummary[]): PipelineStatePayload {
  return { pipeline_id: "pipe-001", steps };
}

describe("shouldShowCancel", () => {
  it("returns false when no pipeline data", () => {
    expect(
      shouldShowCancel({
        live: null,
        history: [],
        currentPipelineFromSession: null,
      }),
    ).toBe(false);
  });

  it("returns true when live pipeline has a running step", () => {
    const payload = makePipelineState([
      makeStep("s1", "complete"),
      makeStep("s2", "running", 47),
    ]);
    expect(
      shouldShowCancel({
        live: payload,
        history: [],
        currentPipelineFromSession: null,
      }),
    ).toBe(true);
  });

  it("returns false when live pipeline has no running steps", () => {
    const payload = makePipelineState([
      makeStep("s1", "complete"),
      makeStep("s2", "pending"),
    ]);
    expect(
      shouldShowCancel({
        live: payload,
        history: [],
        currentPipelineFromSession: null,
      }),
    ).toBe(false);
  });

  it("returns true when session-state current_pipeline is non-null (predicate b)", () => {
    expect(
      shouldShowCancel({
        live: null,
        history: [],
        currentPipelineFromSession: {
          pipeline_id: "pipe-session",
          steps: [],
          started_at: null,
          completed_at: null,
          final_state: null,
        },
      }),
    ).toBe(true);
  });

  it("returns true when both conditions are true", () => {
    const payload = makePipelineState([makeStep("s1", "running", 50)]);
    expect(
      shouldShowCancel({
        live: payload,
        history: [],
        currentPipelineFromSession: {
          pipeline_id: "pipe-session",
          steps: [],
          started_at: null,
          completed_at: null,
          final_state: null,
        },
      }),
    ).toBe(true);
  });
});

// --- mergeStepsByStepId (job-0162) --------------------------------------- //
//
// The agent emits a fresh `pipeline_id` per tool dispatch (server.py
// per-tool start_pipeline + close_pipeline). Before job-0162 each tool
// dispatch rendered as a separate "group" in the chat — the result was a
// stale running card stacked above a completed card for the same tool. This
// helper merges every snapshot (history + live) by `step_id` so each tool
// dispatch renders as exactly one transitioning card.

describe("mergeStepsByStepId", () => {
  it("returns an empty list when there is no history and no live snapshot", () => {
    expect(mergeStepsByStepId([], null)).toEqual([]);
  });

  it("renders one card per step_id when a tool transitions pending → running → complete across separate pipeline_ids", () => {
    // Simulates the server's per-tool start_pipeline emission pattern: each
    // tool wraps in its own pipeline_id, but the step_id is stable within
    // the tool's lifecycle.
    const pendingSnap: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [makeStep("step-1", "pending")],
    };
    const runningSnap: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [makeStep("step-1", "running", 50)],
    };
    const completeSnap: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [makeStep("step-1", "complete")],
    };
    // History accumulates the terminal snapshot; live is null after close.
    const merged = mergeStepsByStepId(
      [pendingSnap, runningSnap, completeSnap],
      null,
    );
    expect(merged).toHaveLength(1);
    expect(merged[0]!.state).toBe("complete");
  });

  it("two tool dispatches with two distinct step_ids render as two cards in encounter order", () => {
    const tool1: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [makeStep("step-1", "complete")],
    };
    const tool2: PipelineStatePayload = {
      pipeline_id: "pipe-B",
      steps: [makeStep("step-2", "running", 25)],
    };
    const merged = mergeStepsByStepId([tool1], tool2);
    expect(merged).toHaveLength(2);
    expect(merged[0]!.step_id).toBe("step-1");
    expect(merged[0]!.state).toBe("complete");
    expect(merged[1]!.step_id).toBe("step-2");
    expect(merged[1]!.state).toBe("running");
  });

  it("live snapshot wins over history for the same step_id", () => {
    const historical: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [makeStep("step-1", "pending")],
    };
    const live: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [makeStep("step-1", "running", 80)],
    };
    const merged = mergeStepsByStepId([historical], live);
    expect(merged).toHaveLength(1);
    expect(merged[0]!.state).toBe("running");
    expect(merged[0]!.progress_percent).toBe(80);
  });

  it("preserves first-encountered order even when a later snapshot updates state", () => {
    const snapA: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [makeStep("step-1", "pending"), makeStep("step-2", "pending")],
    };
    const snapB: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [makeStep("step-2", "complete"), makeStep("step-1", "complete")],
    };
    const merged = mergeStepsByStepId([snapA, snapB], null);
    expect(merged.map((s) => s.step_id)).toEqual(["step-1", "step-2"]);
    expect(merged.every((s) => s.state === "complete")).toBe(true);
  });

  // job-0166 Part 3 — same (name, tool_name) across two different step_ids
  // collapses to a single card so the user sees one transitioning llm_generation
  // card, not a stale running card stacked above a completed one.
  it("collapses two cards sharing (name, tool_name) but different step_ids to a single most-recent card", () => {
    const stalePipe: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [
        {
          step_id: "step-llm-1",
          name: "llm_generation",
          tool_name: "gemini_generate",
          state: "running",
        },
      ],
    };
    const completePipe: PipelineStatePayload = {
      pipeline_id: "pipe-B",
      steps: [
        {
          step_id: "step-llm-2",
          name: "llm_generation",
          tool_name: "gemini_generate",
          state: "complete",
        },
      ],
    };
    const merged = mergeStepsByStepId([stalePipe, completePipe], null);
    expect(merged).toHaveLength(1);
    expect(merged[0]!.state).toBe("complete");
    // First-encountered position is preserved (the original stale step's slot).
    expect(merged[0]!.step_id).toBe("step-llm-2");
  });

  it("does NOT collapse distinct tools — only matching (name, tool_name) pairs", () => {
    const llm: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [
        {
          step_id: "step-llm",
          name: "llm_generation",
          tool_name: "gemini_generate",
          state: "complete",
        },
      ],
    };
    const fetchDem: PipelineStatePayload = {
      pipeline_id: "pipe-B",
      steps: [
        {
          step_id: "step-fetch",
          name: "fetch_dem",
          tool_name: "fetch_dem",
          state: "running",
        },
      ],
    };
    const merged = mergeStepsByStepId([llm, fetchDem], null);
    expect(merged).toHaveLength(2);
    expect(merged[0]!.name).toBe("llm_generation");
    expect(merged[1]!.name).toBe("fetch_dem");
  });
});

// --- forceMostRecentRunningToFailed (job-0166 Part 1) --------------------- //
//
// When an `error` envelope arrives without an accompanying terminal
// pipeline-state (LLM_UNAVAILABLE / tool TypeError on the agent side),
// the client must force the most-recent running step to `failed` so the
// rainbow-animated "running" card transitions to the RED no-animation
// terminal state with the typed error_code chip.

describe("forceMostRecentRunningToFailed", () => {
  const ERR: ErrorPayload = {
    error_code: "LLM_UNAVAILABLE",
    message: "Gemini generation failed: 500",
    retryable: true,
  };

  it("flips the live running step to failed with error fields attached", () => {
    const live: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [makeStep("step-llm", "running", 30)],
    };
    const next = forceMostRecentRunningToFailed(
      { live, history: [], currentPipelineFromSession: null },
      ERR,
      null,
    );
    expect(next.live).not.toBeNull();
    const s = next.live!.steps![0]!;
    expect(s.state).toBe("failed");
    expect(s.error_code).toBe("LLM_UNAVAILABLE");
    expect(s.error_message).toBe("Gemini generation failed: 500");
  });

  it("flips a history step to failed when no live snapshot has a running step", () => {
    const archived: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [makeStep("step-llm", "running")],
    };
    const next = forceMostRecentRunningToFailed(
      { live: null, history: [archived], currentPipelineFromSession: null },
      ERR,
      null,
    );
    expect(next.history[0]!.steps![0]!.state).toBe("failed");
  });

  it("prefers the most-recent running step when multiple are running", () => {
    const snapA: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [makeStep("step-1", "running")],
    };
    const snapB: PipelineStatePayload = {
      pipeline_id: "pipe-B",
      steps: [makeStep("step-2", "running")],
    };
    const next = forceMostRecentRunningToFailed(
      { live: snapB, history: [snapA], currentPipelineFromSession: null },
      ERR,
      null,
    );
    // The live snapshot's step is the most-recent → it gets flipped.
    expect(next.live!.steps![0]!.state).toBe("failed");
    // The archived running step is left alone (it belongs to a prior turn).
    expect(next.history[0]!.steps![0]!.state).toBe("running");
  });

  it("matches by tool_name when supplied", () => {
    const snap: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [
        {
          step_id: "step-a",
          name: "fetch_dem",
          tool_name: "fetch_dem",
          state: "running",
        },
        {
          step_id: "step-b",
          name: "publish_layer",
          tool_name: "publish_layer",
          state: "running",
        },
      ],
    };
    const next = forceMostRecentRunningToFailed(
      { live: snap, history: [], currentPipelineFromSession: null },
      ERR,
      "fetch_dem",
    );
    const steps = next.live!.steps!;
    expect(steps.find((s) => s.step_id === "step-a")!.state).toBe("failed");
    expect(steps.find((s) => s.step_id === "step-b")!.state).toBe("running");
  });

  it("is a no-op when no running step exists anywhere", () => {
    const snap: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [makeStep("step-1", "complete")],
    };
    const state = {
      live: snap,
      history: [],
      currentPipelineFromSession: null,
    };
    const next = forceMostRecentRunningToFailed(state, ERR, null);
    expect(next).toEqual(state);
  });

  it("does NOT flip already-terminal steps to failed", () => {
    const snap: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [
        makeStep("step-done", "complete"),
        makeStep("step-cancelled", "cancelled"),
      ],
    };
    const next = forceMostRecentRunningToFailed(
      { live: snap, history: [], currentPipelineFromSession: null },
      ERR,
      null,
    );
    expect(next.live!.steps![0]!.state).toBe("complete");
    expect(next.live!.steps![1]!.state).toBe("cancelled");
  });
});

// --- pipelineReducer error → ChatInput idle (job-0173 Part 2) ----------- //
//
// Kickoff: when an `error` envelope arrives (Gemini failure, agent crash +
// reconnect, dispatch TypeError, etc.), force-transition ChatInput state
// back to `idle` so the user can send a new prompt. The cancel predicate
// reads (a) live.steps.some(running) and (b) currentPipelineFromSession.
// After error: both must be false.

describe("pipelineReducer — error → ChatInput force-idle (job-0173 Part 2)", () => {
  const ERR: ErrorPayload = {
    error_code: "LLM_UNAVAILABLE",
    message: "Gemini generation failed: 500",
  } as ErrorPayload;

  it("clears currentPipelineFromSession on error so shouldShowCancel returns false", () => {
    const live: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [makeStep("s1", "running", 30)],
    };
    const state: PipelineInlineState = {
      live,
      history: [],
      currentPipelineFromSession: {
        pipeline_id: "pipe-A",
        steps: [makeStep("s1", "running")],
        started_at: null,
        completed_at: null,
        final_state: null,
      },
    };
    const next = pipelineReducer(state, {
      type: "error",
      payload: ERR,
      tool_name: null,
    });
    expect(next.currentPipelineFromSession).toBeNull();
    expect(shouldShowCancel(next)).toBe(false);
  });

  it("moves the live snapshot to history when no step is still running after the flip", () => {
    const live: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [
        makeStep("s1", "complete"),
        makeStep("s2", "running", 50),
      ],
    };
    const state: PipelineInlineState = {
      live,
      history: [],
      currentPipelineFromSession: null,
    };
    const next = pipelineReducer(state, {
      type: "error",
      payload: ERR,
      tool_name: null,
    });
    // live should be null (moved to history); history should contain the
    // rewritten snapshot with s2 → failed.
    expect(next.live).toBeNull();
    expect(next.history).toHaveLength(1);
    const movedSteps = next.history[0]!.steps!;
    expect(movedSteps.find((s) => s.step_id === "s2")!.state).toBe("failed");
    expect(shouldShowCancel(next)).toBe(false);
  });

  it("leaves live in place when a sibling step is still running (multi-step pipeline)", () => {
    // Two running steps; error flips only the most-recent → the other is still
    // running, so live should stay live and the cancel button still shows.
    const live: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [
        makeStep("s1", "running", 20),
        makeStep("s2", "running", 80),
      ],
    };
    const state: PipelineInlineState = {
      live,
      history: [],
      currentPipelineFromSession: null,
    };
    const next = pipelineReducer(state, {
      type: "error",
      payload: ERR,
      tool_name: null,
    });
    // One step was flipped to failed; the other remains running → live stays.
    expect(next.live).not.toBeNull();
    const failed = next.live!.steps!.filter((s) => s.state === "failed");
    const running = next.live!.steps!.filter((s) => s.state === "running");
    expect(failed).toHaveLength(1);
    expect(running).toHaveLength(1);
    // Cancel button is still appropriate (a sibling tool truly is still running).
    expect(shouldShowCancel(next)).toBe(true);
  });

  it("end-to-end: live running + session current_pipeline → after error, idle", () => {
    // The canonical bug pattern from the kickoff: dispatch fails, session-state
    // never gets a terminal update, current_pipeline lingers; the live running
    // step is the only step. After error the ChatInput must return to idle.
    const live: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [makeStep("only-step", "running", 10)],
    };
    const state: PipelineInlineState = {
      live,
      history: [],
      currentPipelineFromSession: {
        pipeline_id: "pipe-A",
        steps: live.steps!,
        started_at: null,
        completed_at: null,
        final_state: null,
      },
    };
    const next = pipelineReducer(state, {
      type: "error",
      payload: ERR,
      tool_name: null,
    });
    expect(shouldShowCancel(next)).toBe(false); // ChatInput renders idle (up-arrow)
  });
});

// --- buildInterleavedStream (job-0176) ---------------------------------- //
//
// The interleave refactor's pure helper. Verifies arrival-order sorting,
// that tool cards land between agent text bubbles at their natural slot,
// and that re-ordered tool snapshots keep their first-arrival position.

describe("buildInterleavedStream (job-0176 — chronological interleave)", () => {
  it("returns an empty list when nothing has arrived yet", () => {
    expect(
      buildInterleavedStream([], [], null, new Map(), new Map()),
    ).toEqual([]);
  });

  it("orders [user → agent → tool → agent] from seq 1..4 as the user sees it", () => {
    // The canonical kickoff scenario:
    //   1. user prompt
    //   2. agent narration "I'm locating the area..."
    //   3. geocode_location tool card
    //   4. agent narration "I've added the location."
    const messageOrder = new Map<string, number>([
      ["user-0", 1],
      ["msg-pre", 2],
      ["msg-post", 4],
    ]);
    // ux-batch-1 J9: stepOrder now keys tool steps by step_id (stepInterleaveKey).
    const stepOrder = new Map<string, number>([["step-geo", 3]]);
    const messages = [
      { id: "user-0", role: "user" as const, text: "Show me Fort Myers", done: true },
      { id: "msg-pre", role: "agent" as const, text: "I'm locating...", done: true },
      { id: "msg-post", role: "agent" as const, text: "Added.", done: true },
    ];
    const toolSnap: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [
        {
          step_id: "step-geo",
          name: "geocode_location",
          tool_name: "geocode_location",
          state: "complete",
        },
      ],
    };
    const stream = buildInterleavedStream(
      messages,
      [toolSnap],
      null,
      messageOrder,
      stepOrder,
    );
    expect(stream.map((e: InterleavedEntry) => e.kind)).toEqual([
      "user-message",
      "agent-message",
      "tool",
      "agent-message",
    ]);
    expect(stream.map((e: InterleavedEntry) => e.seq)).toEqual([1, 2, 3, 4]);
  });

  it("places a NEW tool card AT THE END when its first-arrival seq is the latest", () => {
    // User has scrolled and an agent message arrived (seq=1); then a tool
    // dispatches (seq=2) → tool card should land AFTER the agent bubble.
    const messageOrder = new Map<string, number>([["msg-1", 1]]);
    const stepOrder = new Map<string, number>([["step-dem", 2]]);
    const messages = [
      { id: "msg-1", role: "agent" as const, text: "Working...", done: false },
    ];
    const snap: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [
        {
          step_id: "step-dem",
          name: "fetch_dem",
          tool_name: "fetch_dem",
          state: "running",
          progress_percent: 25,
        },
      ],
    };
    const stream = buildInterleavedStream(
      messages,
      [],
      snap,
      messageOrder,
      stepOrder,
    );
    expect(stream).toHaveLength(2);
    expect(stream[0]!.kind).toBe("agent-message");
    expect(stream[1]!.kind).toBe("tool");
  });

  it("does NOT move a tool card when later snapshots change its state (sticky slot)", () => {
    // Tool first arrives at seq=2, then transitions running → complete.
    // The card must remain at slot 2 (between msg-1 seq=1 and msg-2 seq=3),
    // never jump to the bottom on completion.
    const messageOrder = new Map<string, number>([
      ["msg-1", 1],
      ["msg-2", 3],
    ]);
    const stepOrder = new Map<string, number>([["step-dem", 2]]);
    const messages = [
      { id: "msg-1", role: "agent" as const, text: "Pre", done: true },
      { id: "msg-2", role: "agent" as const, text: "Post", done: true },
    ];
    // Two snapshots — running then complete; merge picks the complete state.
    const running: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [
        {
          step_id: "step-dem",
          name: "fetch_dem",
          tool_name: "fetch_dem",
          state: "running",
        },
      ],
    };
    const complete: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [
        {
          step_id: "step-dem",
          name: "fetch_dem",
          tool_name: "fetch_dem",
          state: "complete",
        },
      ],
    };
    const stream = buildInterleavedStream(
      messages,
      [running, complete],
      null,
      messageOrder,
      stepOrder,
    );
    expect(stream.map((e: InterleavedEntry) => e.kind)).toEqual([
      "agent-message",
      "tool",
      "agent-message",
    ]);
    // Tool card is in the COMPLETE state at its sticky slot.
    const tool = stream[1]! as Extract<InterleavedEntry, { kind: "tool" }>;
    expect(tool.step.state).toBe("complete");
  });

  it("interleaves multiple tool dispatches between multiple agent narrations", () => {
    // Pattern from kickoff:
    //   msg-1 → tool-A (geocode) → msg-2 → tool-B (WDPA) → msg-3
    const messageOrder = new Map<string, number>([
      ["user-0", 1],
      ["msg-1", 2],
      ["msg-2", 4],
      ["msg-3", 6],
    ]);
    const stepOrder = new Map<string, number>([
      ["step-geo", 3],
      ["step-wdpa", 5],
    ]);
    const messages = [
      { id: "user-0", role: "user" as const, text: "Q", done: true },
      { id: "msg-1", role: "agent" as const, text: "Locating...", done: true },
      { id: "msg-2", role: "agent" as const, text: "Fetching WDPA...", done: true },
      { id: "msg-3", role: "agent" as const, text: "Added 2.", done: true },
    ];
    const snap: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [
        {
          step_id: "step-geo",
          name: "geocode_location",
          tool_name: "geocode_location",
          state: "complete",
        },
        {
          step_id: "step-wdpa",
          name: "fetch_wdpa_protected_areas",
          tool_name: "fetch_wdpa_protected_areas",
          state: "complete",
        },
      ],
    };
    const stream = buildInterleavedStream(
      messages,
      [snap],
      null,
      messageOrder,
      stepOrder,
    );
    expect(stream.map((e: InterleavedEntry) => e.kind)).toEqual([
      "user-message",
      "agent-message",
      "tool",
      "agent-message",
      "tool",
      "agent-message",
    ]);
  });

  it("falls back to MAX_SAFE_INTEGER and renders deterministically when seq is missing", () => {
    // Belt-and-suspenders: a message or step without a recorded seq sorts
    // AFTER everything that has one (per the function's contract).
    const messageOrder = new Map<string, number>([["msg-1", 1]]);
    const stepOrder = new Map<string, number>(); // empty
    const messages = [
      { id: "msg-1", role: "agent" as const, text: "Hi", done: true },
    ];
    const snap: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [
        {
          step_id: "step-x",
          name: "unknown",
          tool_name: "unknown",
          state: "complete",
        },
      ],
    };
    const stream = buildInterleavedStream(
      messages,
      [],
      snap,
      messageOrder,
      stepOrder,
    );
    expect(stream).toHaveLength(2);
    expect(stream[0]!.kind).toBe("agent-message");
    expect(stream[1]!.kind).toBe("tool");
  });

  // --- wave-4-10 thinking-state filtering -------------------------------- //
  //
  // The Gemini "llm_generation" step is special-cased OUT of the interleaved
  // stream — it renders as a separate ephemeral indicator (ThinkingIndicator)
  // pinned to the bottom of the chat scroll, not as a tool card. Other tool
  // dispatches continue to interleave as normal.

  it("filters thinking-shaped steps (llm_generation) out of the interleaved stream", () => {
    const messageOrder = new Map<string, number>([["user-0", 1]]);
    const stepOrder = new Map<string, number>([
      [`${THINKING_STEP_NAME}|gemini_generate`, 2],
      ["step-dem", 3],
    ]);
    const messages = [
      { id: "user-0", role: "user" as const, text: "Hi", done: true },
    ];
    const snap: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [
        {
          step_id: "step-llm",
          name: THINKING_STEP_NAME,
          tool_name: "gemini_generate",
          state: "running",
        },
        {
          step_id: "step-dem",
          name: "fetch_dem",
          tool_name: "fetch_dem",
          state: "running",
        },
      ],
    };
    const stream = buildInterleavedStream(
      messages,
      [],
      snap,
      messageOrder,
      stepOrder,
    );
    // user-message + fetch_dem tool card; the llm_generation card is filtered.
    expect(stream.map((e: InterleavedEntry) => e.kind)).toEqual([
      "user-message",
      "tool",
    ]);
    const tool = stream[1]! as Extract<InterleavedEntry, { kind: "tool" }>;
    expect(tool.step.name).toBe("fetch_dem");
  });

  it("F18: re-running the SAME tool in a later turn is a NEW card AFTER the new prompt", () => {
    // Turn 1: user(seq1) -> fetch_roads_osm step-r1 (seq2).
    // Turn 2: user(seq3) -> fetch_roads_osm step-r2 (seq4) — SAME name/tool, a
    // fresh step_id (pipeline_emitter mints a new ULID per invocation). The
    // turn-2 card must render AFTER the turn-2 prompt, NOT collapse into the
    // turn-1 slot (the "card shows up behind the last prompt" bug).
    const messageOrder = new Map<string, number>([
      ["user-0", 1],
      ["user-1", 3],
    ]);
    const stepOrder = new Map<string, number>([
      ["step-r1", 2],
      ["step-r2", 4],
    ]);
    const messages = [
      { id: "user-0", role: "user" as const, text: "roads", done: true },
      { id: "user-1", role: "user" as const, text: "roads again", done: true },
    ];
    const turn1: PipelineStatePayload = {
      pipeline_id: "p1",
      steps: [
        {
          step_id: "step-r1",
          name: "fetch_roads_osm",
          tool_name: "fetch_roads_osm",
          state: "complete",
        },
      ],
    };
    const turn2: PipelineStatePayload = {
      pipeline_id: "p2",
      steps: [
        {
          step_id: "step-r2",
          name: "fetch_roads_osm",
          tool_name: "fetch_roads_osm",
          state: "complete",
        },
      ],
    };
    const stream = buildInterleavedStream(
      messages,
      [turn1, turn2],
      null,
      messageOrder,
      stepOrder,
    );
    // Two DISTINCT tool cards (not collapsed), in true chronological order.
    expect(stream.map((e: InterleavedEntry) => e.kind)).toEqual([
      "user-message",
      "tool",
      "user-message",
      "tool",
    ]);
    expect(stream.map((e: InterleavedEntry) => e.seq)).toEqual([1, 2, 3, 4]);
    const toolIds = stream
      .filter((e): e is Extract<InterleavedEntry, { kind: "tool" }> => e.kind === "tool")
      .map((e) => e.step.step_id);
    expect(toolIds).toEqual(["step-r1", "step-r2"]);
  });

  // --- credential prompts interleave inline (NATE 2026-06-17) ------------ //
  //
  // A keyed tool paused on a missing API key emits a credential-request; the
  // card must land at its first-arrival seq BETWEEN the narration that came
  // before it and the narration that resumes after it — never break out to
  // the bottom of the scroll.

  function credReq(
    requestId: string,
    overrides: Partial<CredentialRequestPayload> = {},
  ): CredentialRequestPayload {
    return {
      envelope_type: "credential-request",
      request_id: requestId,
      provider_id: "ebird",
      provider_label: "eBird",
      signup_url: "https://ebird.org/api/keygen",
      secret_key_name: "EBIRD_API_KEY",
      message: "eBird needs an API key.",
      tool_name: "fetch_ebird_observations",
      ...overrides,
    };
  }

  it("interleaves a credential card at its arrival seq between narration bubbles", () => {
    // 1. user prompt; 2. agent "I need a key…"; 3. credential card;
    // 4. agent narration resumes AFTER the card.
    const messageOrder = new Map<string, number>([
      ["user-0", 1],
      ["msg-pre", 2],
      ["msg-post", 4],
    ]);
    const messages = [
      { id: "user-0", role: "user" as const, text: "bird sightings", done: true },
      { id: "msg-pre", role: "agent" as const, text: "I need an eBird key.", done: true },
      { id: "msg-post", role: "agent" as const, text: "Thanks — retrying.", done: true },
    ];
    const credentialSeqs = new Map<string, number>([["REQ1", 3]]);
    const stream = buildInterleavedStream(
      messages,
      [],
      null,
      messageOrder,
      new Map(),
      [credReq("REQ1")],
      credentialSeqs,
      new Map(),
    );
    expect(stream.map((e: InterleavedEntry) => e.kind)).toEqual([
      "user-message",
      "agent-message",
      "credential",
      "agent-message",
    ]);
    expect(stream.map((e: InterleavedEntry) => e.seq)).toEqual([1, 2, 3, 4]);
    // The card carries the request + its (unresolved) state.
    const cred = stream[2]! as Extract<InterleavedEntry, { kind: "credential" }>;
    expect(cred.requestId).toBe("REQ1");
    expect(cred.request.provider_label).toBe("eBird");
    expect(cred.resolved).toBeNull();
  });

  it("threads the resolved state onto the credential entry (folds in place, no reorder)", () => {
    // Card arrives at seq=2; once resolved it stays at slot 2 (does NOT jump
    // to the end) — the agent's follow-up at seq=3 still renders after it.
    const messageOrder = new Map<string, number>([
      ["msg-pre", 1],
      ["msg-post", 3],
    ]);
    const messages = [
      { id: "msg-pre", role: "agent" as const, text: "Need a key.", done: true },
      { id: "msg-post", role: "agent" as const, text: "Got it.", done: true },
    ];
    const stream = buildInterleavedStream(
      messages,
      [],
      null,
      messageOrder,
      new Map(),
      [credReq("REQ1")],
      new Map<string, number>([["REQ1", 2]]),
      new Map<string, "saved" | "declined">([["REQ1", "saved"]]),
    );
    expect(stream.map((e: InterleavedEntry) => e.kind)).toEqual([
      "agent-message",
      "credential",
      "agent-message",
    ]);
    const cred = stream[1]! as Extract<InterleavedEntry, { kind: "credential" }>;
    expect(cred.resolved).toBe("saved");
  });

  it("omitting the credential args keeps the legacy stream shape (no credential rows)", () => {
    const messageOrder = new Map<string, number>([["m1", 1]]);
    const messages = [
      { id: "m1", role: "agent" as const, text: "hi", done: true },
    ];
    const stream = buildInterleavedStream(messages, [], null, messageOrder, new Map());
    expect(stream.every((e: InterleavedEntry) => e.kind !== "credential")).toBe(true);
  });

  // --- payload-warning cards interleave inline (FIX 2, NATE 2026-06-17) --- //
  //
  // A large-payload tool dispatch paused with a tool-payload-warning; the card
  // must land at its first-arrival seq BETWEEN the narration that came before
  // it and the narration that resumes after the user answers — never break out
  // to a separate banner "hat" above the chat.

  function payloadWarn(
    warningId: string,
    overrides: Partial<PayloadWarningEnvelopePayload> = {},
  ): PayloadWarningEnvelopePayload {
    return {
      envelope_type: "tool-payload-warning",
      warning_id: warningId,
      tool_name: "fetch_buildings",
      tool_args: {},
      estimated_mb: 42.5,
      threshold_mb: 25,
      recommendation: "Narrow the bbox.",
      options: ["proceed", "narrow_scope", "cancel"],
      ...overrides,
    };
  }

  it("interleaves a payload-warning card at its arrival seq between narration bubbles", () => {
    // 1. user prompt; 2. agent "this is large…"; 3. payload-warning card;
    // 4. agent narration resumes AFTER the card.
    const messageOrder = new Map<string, number>([
      ["user-0", 1],
      ["msg-pre", 2],
      ["msg-post", 4],
    ]);
    const messages = [
      { id: "user-0", role: "user" as const, text: "fetch everything", done: true },
      { id: "msg-pre", role: "agent" as const, text: "That's a large area.", done: true },
      { id: "msg-post", role: "agent" as const, text: "Proceeding.", done: true },
    ];
    const payloadSeqs = new Map<string, number>([["W1", 3]]);
    const stream = buildInterleavedStream(
      messages,
      [],
      null,
      messageOrder,
      new Map(),
      [],
      new Map(),
      new Map(),
      [payloadWarn("W1")],
      payloadSeqs,
      new Map(),
    );
    expect(stream.map((e: InterleavedEntry) => e.kind)).toEqual([
      "user-message",
      "agent-message",
      "payload-warning",
      "agent-message",
    ]);
    expect(stream.map((e: InterleavedEntry) => e.seq)).toEqual([1, 2, 3, 4]);
    const w = stream[2]! as Extract<InterleavedEntry, { kind: "payload-warning" }>;
    expect(w.warningId).toBe("W1");
    expect(w.warning.estimated_mb).toBe(42.5);
    expect(w.resolved).toBeNull();
  });

  it("threads the resolved decision onto the payload-warning entry (folds in place)", () => {
    // Card arrives at seq=2; once answered it stays at slot 2 (does NOT jump to
    // the end) — the agent's follow-up at seq=3 still renders after it.
    const messageOrder = new Map<string, number>([
      ["msg-pre", 1],
      ["msg-post", 3],
    ]);
    const messages = [
      { id: "msg-pre", role: "agent" as const, text: "Large.", done: true },
      { id: "msg-post", role: "agent" as const, text: "Done.", done: true },
    ];
    const stream = buildInterleavedStream(
      messages,
      [],
      null,
      messageOrder,
      new Map(),
      [],
      new Map(),
      new Map(),
      [payloadWarn("W1")],
      new Map<string, number>([["W1", 2]]),
      new Map([["W1", "proceed" as const]]),
    );
    expect(stream.map((e: InterleavedEntry) => e.kind)).toEqual([
      "agent-message",
      "payload-warning",
      "agent-message",
    ]);
    const w = stream[1]! as Extract<InterleavedEntry, { kind: "payload-warning" }>;
    expect(w.resolved).toBe("proceed");
  });

  it("omitting the payload-warning args keeps the legacy stream shape (no warning rows)", () => {
    const messageOrder = new Map<string, number>([["m1", 1]]);
    const messages = [
      { id: "m1", role: "agent" as const, text: "hi", done: true },
    ];
    const stream = buildInterleavedStream(messages, [], null, messageOrder, new Map());
    expect(stream.every((e: InterleavedEntry) => e.kind !== "payload-warning")).toBe(true);
  });
});

// --- isThinkingActive predicate (wave-4-10) ----------------------------- //
//
// Drives the visibility of the ephemeral ThinkingIndicator pinned to the
// bottom of the chat scroll. Per `feedback_thinking_state_ephemeral`:
//   - Active when a Gemini llm_generation step exists in pending/running
//     and no real content has superseded it (no agent text bubble, no
//     non-thinking tool card recorded with seq >= thinking seq).
//   - Inactive on terminal thinking state (complete / failed / cancelled).
//   - Inactive when an agent text chunk arrives (the bubble replaces it).
//   - Inactive when a non-thinking tool card lands (the tool card itself
//     is the "agent is working" affordance).

describe("isThinkingActive (wave-4-10 thinking-state)", () => {
  it("isThinkingStep returns true for the llm_generation step name only", () => {
    expect(
      isThinkingStep({
        step_id: "s1",
        name: THINKING_STEP_NAME,
        tool_name: "gemini_generate",
        state: "running",
      }),
    ).toBe(true);
    expect(
      isThinkingStep({
        step_id: "s2",
        name: "fetch_dem",
        tool_name: "fetch_dem",
        state: "running",
      }),
    ).toBe(false);
  });

  it("returns false when no thinking step exists in history or live", () => {
    expect(
      isThinkingActive([], [], null, new Map(), new Map()),
    ).toBe(false);
  });

  it("returns true when a running thinking step exists and nothing has superseded it", () => {
    const stepOrder = new Map<string, number>([
      [`${THINKING_STEP_NAME}|gemini_generate`, 1],
    ]);
    const live: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [
        {
          step_id: "step-llm",
          name: THINKING_STEP_NAME,
          tool_name: "gemini_generate",
          state: "running",
        },
      ],
    };
    expect(
      isThinkingActive([], [], live, new Map(), stepOrder),
    ).toBe(true);
  });

  it("returns true when a pending thinking step exists (not yet started)", () => {
    const stepOrder = new Map<string, number>([
      [`${THINKING_STEP_NAME}|gemini_generate`, 1],
    ]);
    const live: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [
        {
          step_id: "step-llm",
          name: THINKING_STEP_NAME,
          tool_name: "gemini_generate",
          state: "pending",
        },
      ],
    };
    expect(
      isThinkingActive([], [], live, new Map(), stepOrder),
    ).toBe(true);
  });

  it("returns false the moment the thinking step transitions to COMPLETE", () => {
    const stepOrder = new Map<string, number>([
      [`${THINKING_STEP_NAME}|gemini_generate`, 1],
    ]);
    const live: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [
        {
          step_id: "step-llm",
          name: THINKING_STEP_NAME,
          tool_name: "gemini_generate",
          state: "complete",
        },
      ],
    };
    expect(
      isThinkingActive([], [], live, new Map(), stepOrder),
    ).toBe(false);
  });

  it("returns false on terminal FAILED thinking state", () => {
    const stepOrder = new Map<string, number>([
      [`${THINKING_STEP_NAME}|gemini_generate`, 1],
    ]);
    const live: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [
        {
          step_id: "step-llm",
          name: THINKING_STEP_NAME,
          tool_name: "gemini_generate",
          state: "failed",
        },
      ],
    };
    expect(
      isThinkingActive([], [], live, new Map(), stepOrder),
    ).toBe(false);
  });

  it("returns false on terminal CANCELLED thinking state", () => {
    const stepOrder = new Map<string, number>([
      [`${THINKING_STEP_NAME}|gemini_generate`, 1],
    ]);
    const live: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [
        {
          step_id: "step-llm",
          name: THINKING_STEP_NAME,
          tool_name: "gemini_generate",
          state: "cancelled",
        },
      ],
    };
    expect(
      isThinkingActive([], [], live, new Map(), stepOrder),
    ).toBe(false);
  });

  it("returns false when an agent text bubble with content streams in after thinking", () => {
    // Thinking arrives at seq=1; agent text bubble arrives at seq=2 with
    // content — the bubble replaces the indicator.
    const messageOrder = new Map<string, number>([["msg-1", 2]]);
    const stepOrder = new Map<string, number>([
      [`${THINKING_STEP_NAME}|gemini_generate`, 1],
    ]);
    const messages = [
      {
        id: "msg-1",
        role: "agent" as const,
        text: "I'm working on it.",
        done: false,
      },
    ];
    const live: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [
        {
          step_id: "step-llm",
          name: THINKING_STEP_NAME,
          tool_name: "gemini_generate",
          state: "running",
        },
      ],
    };
    expect(
      isThinkingActive(messages, [], live, messageOrder, stepOrder),
    ).toBe(false);
  });

  it("stays active when an EMPTY agent text bubble has been allocated but no content streamed yet", () => {
    // Defensive: the bubble may exist in the messages list with an empty
    // string (placeholder before deltas arrive). It does NOT replace the
    // indicator until at least one character of text has streamed.
    const messageOrder = new Map<string, number>([["msg-1", 2]]);
    const stepOrder = new Map<string, number>([
      [`${THINKING_STEP_NAME}|gemini_generate`, 1],
    ]);
    const messages = [
      { id: "msg-1", role: "agent" as const, text: "", done: false },
    ];
    const live: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [
        {
          step_id: "step-llm",
          name: THINKING_STEP_NAME,
          tool_name: "gemini_generate",
          state: "running",
        },
      ],
    };
    expect(
      isThinkingActive(messages, [], live, messageOrder, stepOrder),
    ).toBe(true);
  });

  it("returns false when a non-thinking tool card lands after thinking", () => {
    // Thinking arrives at seq=1; fetch_dem tool card arrives at seq=2 → the
    // tool card is the "agent is doing real work" affordance, hide indicator.
    const stepOrder = new Map<string, number>([
      [`${THINKING_STEP_NAME}|gemini_generate`, 1],
      ["fetch_dem|fetch_dem", 2],
    ]);
    const live: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [
        {
          step_id: "step-llm",
          name: THINKING_STEP_NAME,
          tool_name: "gemini_generate",
          state: "running",
        },
        {
          step_id: "step-dem",
          name: "fetch_dem",
          tool_name: "fetch_dem",
          state: "running",
        },
      ],
    };
    expect(
      isThinkingActive([], [], live, new Map(), stepOrder),
    ).toBe(false);
  });

  it("user-message bubble does NOT hide the indicator (only agent text counts)", () => {
    // The user's message arrives BEFORE thinking — user-message at seq=1,
    // thinking at seq=2. Even if it were at seq=3 the predicate ignores
    // user-role messages: only agent text bubbles count as superseding
    // content.
    const messageOrder = new Map<string, number>([["user-0", 3]]);
    const stepOrder = new Map<string, number>([
      [`${THINKING_STEP_NAME}|gemini_generate`, 2],
    ]);
    const messages = [
      { id: "user-0", role: "user" as const, text: "Q", done: true },
    ];
    const live: PipelineStatePayload = {
      pipeline_id: "pipe-A",
      steps: [
        {
          step_id: "step-llm",
          name: THINKING_STEP_NAME,
          tool_name: "gemini_generate",
          state: "running",
        },
      ],
    };
    expect(
      isThinkingActive(messages, [], live, messageOrder, stepOrder),
    ).toBe(true);
  });
});

// --- desktopChatContainerStyle (job-0283 — desktop sleekness pass;          //
//     job-0294 — width toggle) -------------------------------------------- //
//
// Chat cannot mount in happy-dom (WebSocket), so the desktop container style is
// exported as a factory — same pattern as mobileSheetContainerStyle.
// Pins: (a) the surface joined the job-0264 LayerPanel family (12px radius,
// hairline border, gradient, soft shadow); (b) the COLLAPSED geometry is the
// historical default (right/top/bottom 16, ~380px column); (c) job-0294 the
// EXPANDED variant widens the column.

describe("desktopChatContainerStyle (job-0283 + ux-batch-1 J1 drag-resize)", () => {
  it("joins the panel surface family (radius 12 + hairline border + gradient)", () => {
    const s = desktopChatContainerStyle();
    expect(s.borderRadius).toBe(12);
    expect(String(s.border).replace(/\s/g, "")).toContain(
      "rgba(255,255,255,0.06)",
    );
    expect(String(s.background)).toContain("linear-gradient");
    expect(String(s.boxShadow)).toContain("rgba(0,0,0");
  });

  it("keeps the historical geometry + default width when called with no arg", () => {
    const s = desktopChatContainerStyle();
    expect(s.position).toBe("absolute");
    expect(s.right).toBe(16);
    expect(s.top).toBe(16);
    // NATE 2026-06-22 chat-chrome rework (item 6 → item 7) — the desktop panel
    // bottom edge now aligns with the Settings button (bottom: 12), creating a
    // clean visual band with matching offsets across the left-rail controls.
    expect(s.bottom).toBe(12);
    expect(String(s.width)).toContain("384px");
    expect(s.overflow).toBe("hidden");
  });

  it("reflects the user-dragged width (px) in the column width", () => {
    const s = desktopChatContainerStyle(500);
    expect(String(s.width)).toContain("500px");
    // Anchoring unchanged — only the width grows.
    expect(s.right).toBe(16);
    expect(s.position).toBe("absolute");
  });

  it("clamps an out-of-band width to [min, max] before applying", () => {
    expect(String(desktopChatContainerStyle(80).width)).toContain("320px");
    expect(String(desktopChatContainerStyle(5000).width)).toContain("760px");
  });

  it("FIX 1 - stacks ABOVE the map bbox overlay (zIndex > 12)", () => {
    // BboxProgressOverlay paints at zIndex 12; the chat panel must always sit
    // above it ("the bounding box should always be under the chat"). An explicit
    // positive z-index is required: a z:auto panel is painted UNDER any
    // positively-z-indexed sibling regardless of DOM order.
    const z = desktopChatContainerStyle().zIndex;
    expect(typeof z).toBe("number");
    expect(z as number).toBeGreaterThan(12);
  });
});

// --- chat-width persistence (ux-batch-1 J1 drag-resize) ------------------ //

describe("readChatWidth / writeChatWidth / clampChatWidth (ux-batch-1 J1)", () => {
  afterEach(() => {
    try { localStorage.clear(); } catch { /* ignore */ }
  });

  it("clampChatWidth clamps to the [320, 760] band and rounds", () => {
    expect(clampChatWidth(80)).toBe(320);
    expect(clampChatWidth(5000)).toBe(760);
    expect(clampChatWidth(450.6)).toBe(451);
  });

  it("clampChatWidth falls back to the default on non-finite input", () => {
    // Number.isFinite(NaN) and Number.isFinite(Infinity) are both false, so
    // both degrade to the default rather than clamping to a boundary.
    expect(clampChatWidth(Number.NaN)).toBe(384);
    expect(clampChatWidth(Infinity)).toBe(384);
  });

  it("readChatWidth defaults to ~384 when nothing is persisted", () => {
    expect(readChatWidth()).toBe(384);
  });

  it("round-trips a clamped width through localStorage", () => {
    writeChatWidth(500);
    expect(localStorage.getItem("grace2.chatWidthPx")).toBe("500");
    expect(readChatWidth()).toBe(500);
  });

  it("persists clamped (out-of-band writes are stored at the boundary)", () => {
    writeChatWidth(99999);
    expect(readChatWidth()).toBe(760);
  });
});

// --- chat-opacity tier (F56, job-0322 — shared key owner) ---------------- //
//
// Chat.tsx OWNS the per-user chat-opacity persist key + tier model;
// SettingsPopup (Group D) IMPORTS readChatOpacity / writeChatOpacity. These
// tests pin: the tier model + default MEDIUM, the read/write round-trip
// (single per-user key, NOT per-case), clampChatOpacityTier's junk handling,
// and that the resolved alpha reflects the tier in BOTH container styles.

describe("chat-opacity tier model (F56)", () => {
  afterEach(() => {
    try { localStorage.clear(); } catch { /* ignore */ }
  });

  it("exposes exactly the three tiers low|medium|high, default MEDIUM", () => {
    expect(CHAT_OPACITY_TIERS).toEqual(["low", "medium", "high"]);
    expect(CHAT_OPACITY_DEFAULT).toBe("medium");
  });

  it("clampChatOpacityTier passes valid tiers and defaults junk to MEDIUM", () => {
    expect(clampChatOpacityTier("low")).toBe("low");
    expect(clampChatOpacityTier("medium")).toBe("medium");
    expect(clampChatOpacityTier("high")).toBe("high");
    expect(clampChatOpacityTier(null)).toBe("medium");
    expect(clampChatOpacityTier(undefined)).toBe("medium");
    expect(clampChatOpacityTier("HIGH")).toBe("medium");
    expect(clampChatOpacityTier(0.5)).toBe("medium");
    expect(clampChatOpacityTier("0.99")).toBe("medium");
  });

  it("chatOpacityAlphas bands are ordered low < medium < high per surface", () => {
    const surfaces: Array<keyof ReturnType<typeof chatOpacityAlphas>> = [
      "desktop",
      "mobileCollapsed",
      "mobileExpanded",
    ];
    for (const surface of surfaces) {
      const lo = chatOpacityAlphas("low")[surface];
      const md = chatOpacityAlphas("medium")[surface];
      const hi = chatOpacityAlphas("high")[surface];
      expect(lo).toBeLessThan(md);
      expect(md).toBeLessThan(hi);
      // All alphas are valid [0, 1].
      for (const a of [lo, md, hi]) {
        expect(a).toBeGreaterThan(0);
        expect(a).toBeLessThanOrEqual(1);
      }
    }
  });

  it("MEDIUM (default) is MORE opaque/frosted than the pre-F56 alphas", () => {
    const med = chatOpacityAlphas("medium");
    // Old fixed alphas: desktop 0.96, mobile collapsed 0.58, expanded 0.68.
    expect(med.desktop).toBeGreaterThan(0.96);
    expect(med.mobileCollapsed).toBeGreaterThan(0.58);
    expect(med.mobileExpanded).toBeGreaterThan(0.68);
  });

  it("mobile bands stay BELOW desktop (sheet keeps map-reads-through)", () => {
    for (const tier of CHAT_OPACITY_TIERS) {
      const b = chatOpacityAlphas(tier);
      expect(b.mobileCollapsed).toBeLessThanOrEqual(b.desktop);
      expect(b.mobileExpanded).toBeLessThanOrEqual(b.desktop);
      // Collapsed is the most see-through of the two sheet states.
      expect(b.mobileCollapsed).toBeLessThan(b.mobileExpanded);
    }
  });
});

describe("readChatOpacity / writeChatOpacity (F56 per-user persistence)", () => {
  afterEach(() => {
    try { localStorage.clear(); } catch { /* ignore */ }
  });

  it("the persist key is the single shared per-user key (not per-case)", () => {
    expect(LS_CHAT_OPACITY).toBe("grace2.chatOpacityTier");
  });

  it("defaults to MEDIUM when nothing is persisted", () => {
    expect(readChatOpacity()).toBe("medium");
  });

  it("round-trips every tier through the shared key", () => {
    for (const tier of CHAT_OPACITY_TIERS) {
      writeChatOpacity(tier);
      expect(localStorage.getItem(LS_CHAT_OPACITY)).toBe(tier);
      expect(readChatOpacity()).toBe(tier);
    }
  });

  it("normalizes an out-of-range write to MEDIUM before persisting", () => {
    writeChatOpacity("nonsense" as unknown as ChatOpacityTier);
    expect(readChatOpacity()).toBe("medium");
  });

  it("garbage already in storage degrades to MEDIUM on read", () => {
    localStorage.setItem(LS_CHAT_OPACITY, "0.42");
    expect(readChatOpacity()).toBe("medium");
  });
});

describe("opacity tier → container-style alpha (F56 applied to both surfaces)", () => {
  function alphasIn(bg: string): number[] {
    return [...bg.matchAll(/rgba\(\d+,\d+,\d+,(0?\.\d+|1)\)/g)].map((m) =>
      Number(m[1]),
    );
  }

  it("desktop background alpha reflects the tier", () => {
    for (const tier of CHAT_OPACITY_TIERS) {
      const s = desktopChatContainerStyle(384, tier);
      const want = chatOpacityAlphas(tier).desktop;
      for (const a of alphasIn(String(s.background))) {
        expect(a).toBe(want);
      }
    }
  });

  it("desktop defaults to the MEDIUM band when no tier is passed", () => {
    const s = desktopChatContainerStyle(384);
    for (const a of alphasIn(String(s.background))) {
      expect(a).toBe(chatOpacityAlphas("medium").desktop);
    }
  });

  it("mobile collapsed/expanded background alpha reflects the tier + state", () => {
    for (const tier of CHAT_OPACITY_TIERS) {
      const collapsed = mobileSheetContainerStyle(false, 70, tier);
      const expanded = mobileSheetContainerStyle(true, 70, tier);
      for (const a of alphasIn(String(collapsed.background))) {
        expect(a).toBe(chatOpacityAlphas(tier).mobileCollapsed);
      }
      for (const a of alphasIn(String(expanded.background))) {
        expect(a).toBe(chatOpacityAlphas(tier).mobileExpanded);
      }
    }
  });
});

// --- SAME-TAB REACTIVITY (F56 fix, job-0322) ----------------------------- //
//
// THE BUG: changing the opacity tier in Settings persisted but never
// re-applied to the mounted chat container LIVE — the user reported "opacity
// still doesn't work." ROOT CAUSE: a plain `localStorage.setItem` does NOT
// fire the `storage` event in the SAME tab (the spec only fires it in OTHER
// tabs), and Chat read the tier ONCE into useState with no setter / no
// subscription. THE FIX: `writeChatOpacity` ALSO dispatches
// CHAT_OPACITY_CHANGED_EVENT on `window`; Chat subscribes and re-reads +
// re-applies the alphas to BOTH surfaces live.
//
// Chat itself can't mount in happy-dom (it opens a WebSocket), so we verify
// the reactive CONTRACT the component depends on: writeChatOpacity dispatches
// the event, and a subscriber re-reading via readChatOpacity sees the live
// tier — which then flows into desktopChatContainerStyle / mobileSheet-
// ContainerStyle (verified above). This is the same window-event + re-read
// loop Chat's useEffect runs.
describe("chat-opacity same-tab reactivity (F56 fix)", () => {
  afterEach(() => {
    try { localStorage.clear(); } catch { /* ignore */ }
  });

  it("exports a stable custom-event name for the reactivity bus", () => {
    expect(CHAT_OPACITY_CHANGED_EVENT).toBe("grace2:chat-opacity-changed");
  });

  it("writeChatOpacity dispatches CHAT_OPACITY_CHANGED_EVENT on window", () => {
    const seen: string[] = [];
    const onChange = (): void => {
      seen.push(readChatOpacity());
    };
    window.addEventListener(CHAT_OPACITY_CHANGED_EVENT, onChange);
    try {
      writeChatOpacity("low");
      writeChatOpacity("high");
    } finally {
      window.removeEventListener(CHAT_OPACITY_CHANGED_EVENT, onChange);
    }
    // The subscriber fired once per write, each time re-reading the LIVE tier
    // from the shared key (not the stale initial value).
    expect(seen).toEqual(["low", "high"]);
  });

  it("the event carries the normalized tier in detail", () => {
    let detail: ChatOpacityTier | null = null;
    const onChange = (e: Event): void => {
      detail = (e as CustomEvent<ChatOpacityTier>).detail;
    };
    window.addEventListener(CHAT_OPACITY_CHANGED_EVENT, onChange);
    try {
      // An out-of-range write is normalized to MEDIUM before both persist AND
      // dispatch, so subscribers never see junk.
      writeChatOpacity("nonsense" as unknown as ChatOpacityTier);
    } finally {
      window.removeEventListener(CHAT_OPACITY_CHANGED_EVENT, onChange);
    }
    expect(detail).toBe("medium");
  });

  it("toggling the tier updates the applied alpha LIVE on BOTH surfaces", () => {
    // Mirror Chat's reactive state: a subscriber re-reads the tier on the
    // event, and the container styles are recomputed from that live tier.
    let tier: ChatOpacityTier = readChatOpacity(); // MEDIUM (nothing persisted)
    const onChange = (): void => {
      tier = readChatOpacity();
    };
    window.addEventListener(CHAT_OPACITY_CHANGED_EVENT, onChange);
    try {
      const alphasIn = (bg: string): number[] =>
        [...bg.matchAll(/rgba\(\d+,\d+,\d+,(0?\.\d+|1)\)/g)].map((m) =>
          Number(m[1]),
        );

      // Before any toggle — both surfaces paint the MEDIUM band.
      expect(tier).toBe("medium");
      for (const a of alphasIn(String(desktopChatContainerStyle(384, tier).background))) {
        expect(a).toBe(chatOpacityAlphas("medium").desktop);
      }

      // User picks LOW in Settings → writeChatOpacity → event → live re-read.
      writeChatOpacity("low");
      expect(tier).toBe("low");
      // Desktop surface re-applies the LOW alpha live.
      for (const a of alphasIn(String(desktopChatContainerStyle(384, tier).background))) {
        expect(a).toBe(chatOpacityAlphas("low").desktop);
      }
      // Mobile sheet (collapsed + expanded) re-applies the LOW alpha live.
      for (const a of alphasIn(String(mobileSheetContainerStyle(false, 70, tier).background))) {
        expect(a).toBe(chatOpacityAlphas("low").mobileCollapsed);
      }
      for (const a of alphasIn(String(mobileSheetContainerStyle(true, 70, tier).background))) {
        expect(a).toBe(chatOpacityAlphas("low").mobileExpanded);
      }

      // User bumps to HIGH → both surfaces track again, no reload.
      writeChatOpacity("high");
      expect(tier).toBe("high");
      for (const a of alphasIn(String(desktopChatContainerStyle(384, tier).background))) {
        expect(a).toBe(chatOpacityAlphas("high").desktop);
      }
      for (const a of alphasIn(String(mobileSheetContainerStyle(true, 70, tier).background))) {
        expect(a).toBe(chatOpacityAlphas("high").mobileExpanded);
      }
    } finally {
      window.removeEventListener(CHAT_OPACITY_CHANGED_EVENT, onChange);
    }
  });

  it("a cross-tab `storage` event for the shared key also re-reads the tier", () => {
    // Chat additionally listens to the native `storage` event (fires in OTHER
    // tabs). Simulate it: another tab persisted HIGH, then dispatched storage.
    let tier: ChatOpacityTier = readChatOpacity();
    const onStorage = (e: StorageEvent): void => {
      if (e.key === null || e.key === LS_CHAT_OPACITY) tier = readChatOpacity();
    };
    window.addEventListener("storage", onStorage);
    try {
      localStorage.setItem(LS_CHAT_OPACITY, "high");
      window.dispatchEvent(new StorageEvent("storage", { key: LS_CHAT_OPACITY }));
      expect(tier).toBe("high");

      // An unrelated key must NOT cause a re-read churn (tier stays put).
      localStorage.setItem("grace2.somethingElse", "x");
      window.dispatchEvent(
        new StorageEvent("storage", { key: "grace2.somethingElse" }),
      );
      expect(tier).toBe("high");
    } finally {
      window.removeEventListener("storage", onStorage);
    }
  });
});
