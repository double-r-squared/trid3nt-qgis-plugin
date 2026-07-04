// GRACE-2 web — per-Case chat stream tests (job-0266).
//
// The product shape (LAW): each Case owns its chat stream — messages + tool
// cards + sandbox cards + charts — keyed by case_id. Switching Cases swaps
// the ENTIRE visible stream; navigating to the Cases root clears the visible
// chat; envelopes route to the stream of the Case that owns the in-flight
// turn (buffered, never painted into the visible stream of another Case);
// and a prompt typed from root flips into the server-auto-created Case
// (job-0262) showing the thread from turn 1.
//
// Chat cannot mount in happy-dom (it opens a WebSocket), so — following the
// established pure-helper pattern of Chat.test.tsx — these tests exercise
// the exported stream-routing core directly: createChatStreams / getStream /
// routeUserMessage / routeAgentChunk / routePipelineState / routeError /
// routeChartEmission / routeCodeExecRequest / routeCodeExecResult /
// routeCaseOpen / clearRootStream. The React component is a thin shell that
// calls exactly these functions from its GraceWs handlers and renders
// `getStream(cs, streamKeyFor(activeCaseId))`.

import { describe, it, expect } from "vitest";
import {
  ROOT_STREAM_KEY,
  createChatStreams,
  emptyStreamState,
  getStream,
  streamKeyFor,
  clearRootStream,
  routeUserMessage,
  routeAgentChunk,
  routePipelineState,
  routeSessionState,
  routeError,
  routeChartEmission,
  routeCodeExecRequest,
  routeCodeExecResult,
  recordSandboxDecision,
  routeCaseOpen,
  chartsFromSession,
  buildInterleavedStream,
} from "./Chat";
import {
  CaseOpenEnvelopePayload,
  CaseSessionState,
  PipelineStatePayload,
} from "./contracts";

// --- Fixtures ------------------------------------------------------------ //

const CASE_A = "01CASEAAAAAAAAAAAAAAAAAAAA";
const CASE_B = "01CASEBBBBBBBBBBBBBBBBBBBB";

function makeSession(
  caseId: string,
  history: Array<{ id: string; role: "user" | "agent"; content: string }> = [],
): CaseSessionState {
  return {
    case: {
      case_id: caseId,
      title: `Case ${caseId.slice(-4)}`,
      created_at: "2026-06-10T00:00:00Z",
      updated_at: "2026-06-10T00:00:00Z",
      status: "active",
    },
    chat_history: history.map((m, i) => ({
      message_id: m.id,
      case_id: caseId,
      role: m.role,
      content: m.content,
      created_at: `2026-06-10T00:00:0${i}Z`,
    })),
    loaded_layers: [],
    pipeline_history: [],
  };
}

function caseOpen(
  caseId: string,
  history: Array<{ id: string; role: "user" | "agent"; content: string }> = [],
): CaseOpenEnvelopePayload {
  return { session_state: makeSession(caseId, history) };
}

function runningPipeline(id: string, tool: string): PipelineStatePayload {
  return {
    pipeline_id: id,
    steps: [
      { step_id: `${id}-s1`, name: tool, tool_name: tool, state: "running" },
    ],
  };
}

// --- Stream swap on case-open --------------------------------------------- //

describe("routeCaseOpen — stream swap (job-0266)", () => {
  it("first open of a Case builds its stream from rehydrated chat_history", () => {
    const cs = createChatStreams();
    const opened = routeCaseOpen(
      cs,
      caseOpen(CASE_A, [
        { id: "m1", role: "user", content: "first prompt" },
        { id: "m2", role: "agent", content: "first reply" },
      ]),
    );
    expect(opened).toBe(CASE_A);
    const s = getStream(cs, CASE_A);
    expect(s.messages.map((m) => m.text)).toEqual([
      "first prompt",
      "first reply",
    ]);
    // Rehydrated rows keep chronological arrival seqs for the interleave.
    expect(s.messageOrder.get("m1")).toBe(1);
    expect(s.messageOrder.get("m2")).toBe(2);
  });

  it("replays chat_history into a PRE-EXISTING EMPTY placeholder stream (older-case display fix)", () => {
    // CHAT-HISTORY DISPLAY FIX (NATE 2026-06-19): the render path calls
    // getStream(streams, activeCaseId), which LAZILY CREATES an empty placeholder
    // stream for the active Case BEFORE the case-open envelope arrives. The old
    // `!streams.has(caseId)` guard then saw the placeholder and SKIPPED the
    // replay, so an OLDER Case with persisted history showed a BLANK chat even
    // though the data exists in DynamoDB. Now routeCaseOpen also replays into a
    // provably-empty placeholder.
    const cs = createChatStreams();
    // Simulate the render-time lazy creation of the placeholder for CASE_A.
    const placeholder = getStream(cs, CASE_A);
    expect(placeholder.messages).toHaveLength(0); // empty placeholder exists
    // Now the case-open (live or cold) arrives with the persisted history.
    const opened = routeCaseOpen(
      cs,
      caseOpen(CASE_A, [
        { id: "h1", role: "user", content: "older prompt" },
        { id: "h2", role: "agent", content: "older reply" },
      ]),
    );
    expect(opened).toBe(CASE_A);
    // The history is replayed INTO the same placeholder (no blank chat).
    expect(getStream(cs, CASE_A).messages.map((m) => m.text)).toEqual([
      "older prompt",
      "older reply",
    ]);
  });

  it("does NOT clobber a placeholder that already received LIVE content", () => {
    // Guard the other direction: if a turn has already started arriving into the
    // (no-longer-placeholder) stream, a trailing case-open must NOT wipe it.
    const cs = createChatStreams();
    getStream(cs, CASE_A); // lazy placeholder
    routeUserMessage(cs, CASE_A, "live prompt"); // now has real content
    const populated = getStream(cs, CASE_A);
    expect(populated.messages).toHaveLength(1);
    routeCaseOpen(cs, caseOpen(CASE_A, [{ id: "h1", role: "user", content: "older prompt" }]));
    // Same stream object, live content intact — NOT replaced by the replay.
    expect(getStream(cs, CASE_A)).toBe(populated);
    expect(getStream(cs, CASE_A).messages.map((m) => m.text)).toEqual([
      "live prompt",
    ]);
  });

  it("two Cases hold DISTINCT streams; opening B leaves A's stream intact", () => {
    const cs = createChatStreams();
    routeCaseOpen(cs, caseOpen(CASE_A, [{ id: "a1", role: "user", content: "flood in Fort Myers" }]));
    routeCaseOpen(cs, caseOpen(CASE_B, [{ id: "b1", role: "user", content: "wildfire in NorCal" }]));
    expect(getStream(cs, CASE_A).messages.map((m) => m.text)).toEqual([
      "flood in Fort Myers",
    ]);
    expect(getStream(cs, CASE_B).messages.map((m) => m.text)).toEqual([
      "wildfire in NorCal",
    ]);
  });

  it("re-opening a Case visited this session keeps its in-memory buffer (no repaint)", () => {
    const cs = createChatStreams();
    routeCaseOpen(cs, caseOpen(CASE_A, [{ id: "a1", role: "user", content: "hello" }]));
    // A live turn lands richer content into A's buffer than the persisted
    // history carries (tool cards, partial agent text).
    routeUserMessage(cs, CASE_A, "follow-up");
    routeAgentChunk(cs, { message_id: "live1", delta: "working…", done: false });
    const buffered = getStream(cs, CASE_A);
    expect(buffered.messages).toHaveLength(3);
    // Server re-emits case-open for A (e.g. the user re-selects it). The
    // buffer is authoritative for this session — NOT replaced.
    routeCaseOpen(cs, caseOpen(CASE_A, [{ id: "a1", role: "user", content: "hello" }]));
    expect(getStream(cs, CASE_A)).toBe(buffered);
    expect(getStream(cs, CASE_A).messages).toHaveLength(3);
  });

  it("null session_state resets the root stream and returns null", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, ROOT_STREAM_KEY, "typed at root");
    expect(getStream(cs, ROOT_STREAM_KEY).messages).toHaveLength(1);
    const opened = routeCaseOpen(cs, { session_state: null });
    expect(opened).toBeNull();
    expect(getStream(cs, ROOT_STREAM_KEY).messages).toHaveLength(0);
  });

  it("rehydrates persisted session charts on first open", () => {
    const cs = createChatStreams();
    const session = makeSession(CASE_A) as CaseSessionState & {
      charts?: unknown[];
    };
    session.charts = [
      { chart_id: "c1", vega_lite_spec: { mark: "bar" } },
      { chart_id: "bad-no-spec" }, // malformed → filtered
    ];
    routeCaseOpen(cs, { session_state: session });
    const s = getStream(cs, CASE_A);
    expect(s.charts).toHaveLength(1);
    expect(s.charts[0]!.chart_id).toBe("c1");
  });
});

// --- Root navigation clears ----------------------------------------------- //

describe("root stream clearing (job-0266)", () => {
  it("clearRootStream empties the visible root chat (navigate-out-of-Case rule)", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, ROOT_STREAM_KEY, "stateless chatter");
    routeAgentChunk(cs, { message_id: "m1", delta: "reply", done: true });
    expect(getStream(cs, ROOT_STREAM_KEY).messages).toHaveLength(2);
    clearRootStream(cs);
    const root = getStream(cs, ROOT_STREAM_KEY);
    expect(root.messages).toHaveLength(0);
    expect(root.pipeline.live).toBeNull();
    expect(root.pipeline.history).toEqual([]);
    expect(root.charts).toEqual([]);
    expect(root.lastError).toBeNull();
  });

  it("clearing the root does NOT touch Case streams", () => {
    const cs = createChatStreams();
    routeCaseOpen(cs, caseOpen(CASE_A, [{ id: "a1", role: "agent", content: "kept" }]));
    clearRootStream(cs);
    expect(getStream(cs, CASE_A).messages.map((m) => m.text)).toEqual(["kept"]);
  });
});

// --- Envelope routing to the owning Case ----------------------------------- //

describe("envelope routing — owning Case, not visible Case (job-0266)", () => {
  it("streaming envelopes follow the turn submitted in Case A even after Case B opens", () => {
    const cs = createChatStreams();
    routeCaseOpen(cs, caseOpen(CASE_A));
    routeCaseOpen(cs, caseOpen(CASE_B));

    // User submits in A (visible = A): A owns the turn.
    routeUserMessage(cs, CASE_A, "model the flood");
    expect(cs.targetKey).toBe(CASE_A);

    // User clicks Case B mid-turn → case-open B arrives. Ownership must NOT
    // move (targetKey is only adopted from the ROOT sentinel).
    routeCaseOpen(cs, caseOpen(CASE_B));
    expect(cs.targetKey).toBe(CASE_A);

    // Late envelopes for A's turn arrive while B is visible.
    routeAgentChunk(cs, { message_id: "mA", delta: "Modeling…", done: false });
    routePipelineState(cs, runningPipeline("p1", "run_model_flood_scenario"));
    routeChartEmission(cs, {
      chart_id: "chart-1",
      vega_lite_spec: { mark: "bar" },
    } as never);

    // They buffer into A's stream…
    const a = getStream(cs, CASE_A);
    expect(a.messages.map((m) => m.text)).toEqual(["model the flood", "Modeling…"]);
    expect(a.pipeline.live?.pipeline_id).toBe("p1");
    expect(a.charts.map((c) => c.chart_id)).toEqual(["chart-1"]);

    // …and B's visible stream stays untouched.
    const b = getStream(cs, CASE_B);
    expect(b.messages).toHaveLength(0);
    expect(b.pipeline.live).toBeNull();
    expect(b.charts).toHaveLength(0);
  });

  it("error envelopes land in the owning Case's stream (red card buffers too)", () => {
    const cs = createChatStreams();
    routeCaseOpen(cs, caseOpen(CASE_A));
    routeUserMessage(cs, CASE_A, "prompt");
    routePipelineState(cs, runningPipeline("p1", "fetch_dem"));
    routeCaseOpen(cs, caseOpen(CASE_B)); // navigate away mid-turn
    routeError(cs, { error_code: "LLM_UNAVAILABLE", message: "boom" } as never);
    const a = getStream(cs, CASE_A);
    expect(a.lastError).toBe("LLM_UNAVAILABLE: boom");
    // The running step was force-flipped to failed in A's pipeline.
    const allSteps = [
      ...a.pipeline.history.flatMap((h) => h.steps ?? []),
      ...(a.pipeline.live?.steps ?? []),
    ];
    expect(allSteps.some((s) => s.state === "failed")).toBe(true);
    expect(getStream(cs, CASE_B).lastError).toBeNull();
  });

  it("session-state cursor routes to the owning Case", () => {
    const cs = createChatStreams();
    routeCaseOpen(cs, caseOpen(CASE_A));
    routeUserMessage(cs, CASE_A, "prompt");
    routeSessionState(cs, {
      loaded_layers: [],
      chat_history: [],
      pipeline_history: [],
      current_pipeline: {
        pipeline_id: "p-live",
        started_at: null,
        completed_at: null,
        final_state: null,
        steps: [],
      },
      map_view: null,
    } as never);
    expect(
      getStream(cs, CASE_A).pipeline.currentPipelineFromSession?.pipeline_id,
    ).toBe("p-live");
  });

  it("code-exec result resolves the card in the stream that holds its request", () => {
    const cs = createChatStreams();
    routeCaseOpen(cs, caseOpen(CASE_A));
    routeUserMessage(cs, CASE_A, "analyze");
    routeCodeExecRequest(cs, {
      code_exec_id: "ce-1",
      python_code: "print(1)",
    } as never);
    // User opens B and submits there — targetKey moves to B.
    routeCaseOpen(cs, caseOpen(CASE_B));
    routeUserMessage(cs, CASE_B, "other prompt");
    expect(cs.targetKey).toBe(CASE_B);
    // The sandbox result for A's request still lands next to its card in A.
    routeCodeExecResult(cs, { code_exec_id: "ce-1", status: "ok" } as never);
    expect(getStream(cs, CASE_A).sandboxResults.get("ce-1")).toBeTruthy();
    expect(getStream(cs, CASE_B).sandboxResults.size).toBe(0);
  });

  it("sandbox decisions record against the stream the card lives in", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "x");
    routeCodeExecRequest(cs, {
      code_exec_id: "ce-9",
      python_code: "1+1",
    } as never);
    recordSandboxDecision(cs, CASE_A, "ce-9", "proceed");
    expect(getStream(cs, CASE_A).sandboxDecisions.get("ce-9")).toBe("proceed");
  });

  it("chart-emission de-dupes on chart_id within the owning stream", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "x");
    const chart = { chart_id: "c1", vega_lite_spec: { mark: "bar" } } as never;
    routeChartEmission(cs, chart);
    routeChartEmission(cs, chart); // hub + direct double-delivery
    expect(getStream(cs, CASE_A).charts).toHaveLength(1);
  });
});

// --- Auto-create flow (job-0262 hand-off) ---------------------------------- //

describe("auto-create from root — flip into the new Case (job-0262 + job-0266)", () => {
  it("adopts the root turn into the auto-created Case and shows the thread from turn 1", () => {
    const cs = createChatStreams();

    // 1. User types from the Cases root. The bubble lands in the root
    //    stream and the root owns the turn.
    routeUserMessage(cs, ROOT_STREAM_KEY, "flood depth for Fort Myers");
    expect(cs.targetKey).toBe(ROOT_STREAM_KEY);
    expect(getStream(cs, ROOT_STREAM_KEY).messages).toHaveLength(1);

    // 2. job-0262: the server auto-creates the Case, persists the user turn
    //    FIRST, then emits case-open whose chat_history carries it.
    const opened = routeCaseOpen(
      cs,
      caseOpen(CASE_A, [
        { id: "m1", role: "user", content: "flood depth for Fort Myers" },
      ]),
    );
    expect(opened).toBe(CASE_A);

    // 3. The new Case ADOPTED the in-flight turn…
    expect(cs.targetKey).toBe(CASE_A);
    // …its stream shows the conversation from turn 1…
    expect(getStream(cs, CASE_A).messages.map((m) => m.text)).toEqual([
      "flood depth for Fort Myers",
    ]);
    // …and the root buffer is clean for the next visit.
    expect(getStream(cs, ROOT_STREAM_KEY).messages).toHaveLength(0);

    // 4. The turn's streaming envelopes that follow land in the new Case.
    routeAgentChunk(cs, { message_id: "mA", delta: "On it.", done: false });
    routePipelineState(cs, runningPipeline("p1", "fetch_dem"));
    const a = getStream(cs, CASE_A);
    expect(a.messages.map((m) => m.text)).toEqual([
      "flood depth for Fort Myers",
      "On it.",
    ]);
    expect(a.pipeline.live?.pipeline_id).toBe("p1");
  });

  it("does NOT adopt when a Case already owns the in-flight turn", () => {
    const cs = createChatStreams();
    routeCaseOpen(cs, caseOpen(CASE_A));
    routeUserMessage(cs, CASE_A, "prompt in A");
    routeCaseOpen(cs, caseOpen(CASE_B));
    expect(cs.targetKey).toBe(CASE_A);
  });

  // FIX 1 (NATE 2026-06-26) — the agent auto-creates the Case and stamps the
  // FIRST pipeline-state / tool card with the NEW case_id BEFORE case-open
  // arrives. The card must self-heal into the auto-created Case's stream (the
  // SAME object the root user bubble already populated) so it never strands in
  // a non-visible stream until reload. The just-typed bubble must survive the
  // migration, and a later case-open replay must not double-append.
  it("self-heals a case_id-stamped card from root into the auto-created Case (case-open arrives LAST)", () => {
    const cs = createChatStreams();

    // 1. User types from root; the bubble lands in the root stream.
    routeUserMessage(cs, ROOT_STREAM_KEY, "flood depth for Fort Myers");
    expect(cs.targetKey).toBe(ROOT_STREAM_KEY);

    // 2. The agent auto-creates CASE_A and stamps the first card with it —
    //    BEFORE case-open. adoptRootInto migrates the live root stream into
    //    CASE_A so the card lands where the user will look, not a dead stream.
    routePipelineState(cs, runningPipeline("p1", "fetch_dem"), CASE_A);
    routeAgentChunk(cs, { message_id: "mA", delta: "On it.", done: false }, CASE_A);

    // targetKey moved to CASE_A and the root buffer is clean.
    expect(cs.targetKey).toBe(CASE_A);
    expect(getStream(cs, ROOT_STREAM_KEY).messages).toHaveLength(0);

    // The migrated CASE_A stream carries the ORIGINAL user bubble (not lost),
    // the live card, and the agent delta — all in arrival order.
    const a = getStream(cs, CASE_A);
    expect(a.messages.map((m) => m.text)).toEqual([
      "flood depth for Fort Myers",
      "On it.",
    ]);
    expect(a.pipeline.live?.pipeline_id).toBe("p1");

    // 3. case-open arrives LAST with the rehydrated history. routeCaseOpen's
    //    isPlaceholder guard sees the now-non-empty adopted stream and leaves
    //    it intact — NO double-append of the user bubble.
    const opened = routeCaseOpen(
      cs,
      caseOpen(CASE_A, [
        { id: "m1", role: "user", content: "flood depth for Fort Myers" },
      ]),
    );
    expect(opened).toBe(CASE_A);
    const after = getStream(cs, CASE_A);
    expect(after.messages.map((m) => m.text)).toEqual([
      "flood depth for Fort Myers",
      "On it.",
    ]);
    expect(after.pipeline.live?.pipeline_id).toBe("p1");
  });

  // The migration must reuse the SAME StreamState object — proving the
  // just-typed bubble + its order maps carry over by identity, not a copy.
  it("migrates the SAME root StreamState object into the Case slot", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, ROOT_STREAM_KEY, "hi");
    const rootStream = getStream(cs, ROOT_STREAM_KEY);
    routePipelineState(cs, runningPipeline("p1", "fetch_dem"), CASE_A);
    expect(getStream(cs, CASE_A)).toBe(rootStream);
  });

  // An already-Case-owned turn (targetKey !== ROOT) never re-adopts even when a
  // streaming envelope is stamped with a DIFFERENT case_id (preserves the
  // ROOT-sentinel guard — same invariant as the routeCaseOpen test above).
  it("does NOT re-adopt a Case-owned turn on a differently-stamped streaming card", () => {
    const cs = createChatStreams();
    routeCaseOpen(cs, caseOpen(CASE_A));
    routeUserMessage(cs, CASE_A, "prompt in A");
    expect(cs.targetKey).toBe(CASE_A);
    // A card stamped with CASE_B routes by owningKey to CASE_B's stream but
    // must NOT move targetKey (the turn is A-owned).
    routePipelineState(cs, runningPipeline("pB", "fetch_dem"), CASE_B);
    expect(cs.targetKey).toBe(CASE_A);
    expect(getStream(cs, CASE_B).pipeline.live?.pipeline_id).toBe("pB");
  });
});

// --- View-model integrity -------------------------------------------------- //

describe("per-stream view-model integrity (job-0266)", () => {
  it("streamKeyFor maps null/undefined to the root sentinel", () => {
    expect(streamKeyFor(null)).toBe(ROOT_STREAM_KEY);
    expect(streamKeyFor(undefined)).toBe(ROOT_STREAM_KEY);
    expect(streamKeyFor(CASE_A)).toBe(CASE_A);
  });

  it("getStream lazily creates an empty stream per key", () => {
    const cs = createChatStreams();
    const s = getStream(cs, CASE_B);
    expect(s.messages).toEqual([]);
    expect(s.pipeline).toEqual(emptyStreamState().pipeline);
    expect(getStream(cs, CASE_B)).toBe(s); // stable identity
  });

  it("interleave seqs are PER-STREAM — each Case's stream sorts independently", () => {
    const cs = createChatStreams();
    // Case A gets a message then a tool; Case B (opened later) gets a
    // message — its seq counter starts fresh at 1.
    routeUserMessage(cs, CASE_A, "a-prompt");
    routePipelineState(cs, runningPipeline("p1", "fetch_dem"));
    routeUserMessage(cs, CASE_B, "b-prompt");

    const a = getStream(cs, CASE_A);
    const streamA = buildInterleavedStream(
      a.messages,
      a.pipeline.history,
      a.pipeline.live,
      a.messageOrder,
      a.stepOrder,
    );
    expect(streamA.map((e) => e.kind)).toEqual(["user-message", "tool"]);

    const b = getStream(cs, CASE_B);
    expect(b.messageOrder.get("user-0")).toBe(1);
    const streamB = buildInterleavedStream(
      b.messages,
      b.pipeline.history,
      b.pipeline.live,
      b.messageOrder,
      b.stepOrder,
    );
    expect(streamB.map((e) => e.kind)).toEqual(["user-message"]);
  });

  it("chartsFromSession reads the loose sprint-13 charts field defensively", () => {
    const session = makeSession(CASE_A);
    expect(chartsFromSession(session)).toEqual([]);
    (session as unknown as { charts: unknown }).charts = "not-an-array";
    expect(chartsFromSession(session)).toEqual([]);
  });
});
