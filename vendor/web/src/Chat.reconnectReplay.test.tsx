// GRACE-2 web — task #208: replay persisted tool/pipeline cards on the
// RECONNECT / session-state path so they survive a bare WS reconnect AND a hard
// refresh (the dual-socket resume) — not just case-open.
//
// Before this fix, routeSessionState IGNORED the resume session-state's
// chat_history (it only fed current_pipeline), so on reconnect the replayed
// tool cards were never rebuilt and NATE saw them flicker out. The fix makes
// routeSessionState replay chat_history through the SAME helper + the SAME
// empty/placeholder guard routeCaseOpen uses — so it rebuilds the cards once on
// the cold stream and is a strict NO-OP (no duplicate cards) on every
// subsequent session-state once the stream holds content.
//
// Pure-helper pattern (Chat cannot mount in happy-dom — it opens a WebSocket),
// same as Chat.replayToolCards.test.tsx / Chat.terminalDurability.test.tsx:
// exercise routeSessionState / routeCaseOpen / replayStreamFromChatHistory /
// buildInterleavedStream directly.

import { describe, it, expect } from "vitest";
import {
  createChatStreams,
  getStream,
  routeSessionState,
  routeCaseOpen,
  routePipelineState,
  buildInterleavedStream,
  streamKeyFor,
} from "./Chat";
import {
  CaseChatMessage,
  CaseOpenEnvelopePayload,
  CaseSessionState,
  SessionStatePayload,
} from "./contracts";

const CASE_ID = "01CASEAAAAAAAAAAAAAAAAAAAA";

// A persisted full-stream history: user bubble + one terminal tool card +
// agent narration (the exact three row kinds the agent persists per turn).
function fullStreamHistory(): CaseChatMessage[] {
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
      content: "I fetched the DEM and added it to the map.",
      created_at: "2026-06-10T00:00:05Z",
    },
  ];
}

// A resume session-state envelope carrying chat_history (the shape the agent
// re-emits on session-resume). current_pipeline=null = turn-idle resume.
function sessionStateWith(
  history: CaseChatMessage[],
  currentPipeline: unknown = null,
): SessionStatePayload {
  return {
    chat_history: history,
    loaded_layers: [],
    pipeline_history: [],
    current_pipeline: currentPipeline,
  };
}

function caseOpenWith(history: CaseChatMessage[]): CaseOpenEnvelopePayload {
  const session: CaseSessionState = {
    case: {
      case_id: CASE_ID,
      title: "Replay Case",
      created_at: "2026-06-10T00:00:00Z",
      updated_at: "2026-06-10T00:00:00Z",
      status: "active",
    },
    chat_history: history,
    loaded_layers: [],
    pipeline_history: [],
  };
  return { session_state: session };
}

describe("routeSessionState replays tool cards on reconnect/refresh (task #208)", () => {
  it("a session-state with chat_history on an EMPTY/placeholder stream replays the tool cards", () => {
    const cs = createChatStreams();
    // Force the lazy placeholder stream to exist (the render path creates an
    // empty stream for the active Case BEFORE the resume session-state arrives).
    const placeholder = getStream(cs, streamKeyFor(CASE_ID));
    expect(placeholder.messages).toHaveLength(0);
    expect(placeholder.pipeline.history).toHaveLength(0);

    routeSessionState(cs, sessionStateWith(fullStreamHistory()), CASE_ID);

    const s = getStream(cs, streamKeyFor(CASE_ID));
    // Bubbles rebuilt: user + agent (with the REAL narration).
    expect(s.messages.map((m) => m.role)).toEqual(["user", "agent"]);
    expect(s.messages[1]!.text).toBe(
      "I fetched the DEM and added it to the map.",
    );
    // The role="tool" row became a pipeline history card.
    expect(s.pipeline.history).toHaveLength(1);
    const step = s.pipeline.history[0]!.steps![0]!;
    expect(step.tool_name).toBe("fetch_3dep_dem");
    expect(step.state).toBe("complete");
    expect(step.duration_ms).toBe(2340);

    // And it interleaves into the rendered stream exactly where it happened.
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
  });

  it("a SUBSEQUENT session-state on the now-populated stream does NOT duplicate the cards (idempotent)", () => {
    const cs = createChatStreams();
    // First resume rebuilds the cards.
    routeSessionState(cs, sessionStateWith(fullStreamHistory()), CASE_ID);
    const after1 = getStream(cs, streamKeyFor(CASE_ID));
    expect(after1.messages).toHaveLength(2);
    expect(after1.pipeline.history).toHaveLength(1);

    // A second session-state arrives (these fire on EVERY layer/pipeline
    // change) carrying the same chat_history. The stream now holds content, so
    // the replay must be a strict no-op — no second copy of the bubbles or the
    // tool card.
    routeSessionState(cs, sessionStateWith(fullStreamHistory()), CASE_ID);
    const after2 = getStream(cs, streamKeyFor(CASE_ID));
    expect(after2.messages).toHaveLength(2);
    expect(after2.pipeline.history).toHaveLength(1);

    // Third one for good measure — still no duplication.
    routeSessionState(cs, sessionStateWith(fullStreamHistory()), CASE_ID);
    const after3 = getStream(cs, streamKeyFor(CASE_ID));
    expect(after3.messages).toHaveLength(2);
    expect(after3.pipeline.history).toHaveLength(1);

    // The interleaved render is still a single user/tool/agent triple.
    const stream = buildInterleavedStream(
      after3.messages,
      after3.pipeline.history,
      after3.pipeline.live,
      after3.messageOrder,
      after3.stepOrder,
    );
    expect(stream.map((e) => e.kind)).toEqual([
      "user-message",
      "tool",
      "agent-message",
    ]);
  });

  it("does NOT replay (or clobber) once the stream holds live content from this session", () => {
    const cs = createChatStreams();
    // The case-open path already rebuilt the cards this session.
    routeCaseOpen(cs, caseOpenWith(fullStreamHistory()));
    const before = getStream(cs, CASE_ID);
    expect(before.messages).toHaveLength(2);
    expect(before.pipeline.history).toHaveLength(1);

    // A reconnect session-state then arrives carrying the same history — it
    // must NOT re-replay on top of the already-populated stream (the race the
    // fix neutralizes: whoever populates first wins, the other no-ops).
    routeSessionState(cs, sessionStateWith(fullStreamHistory()), CASE_ID);
    const after = getStream(cs, CASE_ID);
    expect(after.messages).toHaveLength(2);
    expect(after.pipeline.history).toHaveLength(1);
  });

  it("an EMPTY chat_history on a placeholder stream is a no-op (no fabricated rows)", () => {
    const cs = createChatStreams();
    getStream(cs, streamKeyFor(CASE_ID)); // lazy placeholder
    routeSessionState(cs, sessionStateWith([]), CASE_ID);
    const s = getStream(cs, streamKeyFor(CASE_ID));
    expect(s.messages).toHaveLength(0);
    expect(s.pipeline.history).toHaveLength(0);
  });

  it("still feeds current_pipeline alongside the replay (existing behavior unchanged)", () => {
    const cs = createChatStreams();
    getStream(cs, streamKeyFor(CASE_ID));
    const running = {
      pipeline_id: "01PIPE000000000000000000A",
      steps: [
        {
          step_id: "s1",
          name: "run_solver",
          tool_name: "run_solver",
          state: "running",
        },
      ],
    };
    routeSessionState(
      cs,
      sessionStateWith(fullStreamHistory(), running),
      CASE_ID,
    );
    const s = getStream(cs, streamKeyFor(CASE_ID));
    // Replay still happened (cold placeholder).
    expect(s.messages).toHaveLength(2);
    expect(s.pipeline.history).toHaveLength(1);
    // And current_pipeline (the cancel predicate's (b)) was fed.
    expect(s.pipeline.currentPipelineFromSession).not.toBeNull();
    expect(s.pipeline.currentPipelineFromSession!.pipeline_id).toBe(
      "01PIPE000000000000000000A",
    );
  });
});

// --------------------------------------------------------------------------- //
// Bare-reconnect CARD SURFACE (NATE: "I had to refresh to see the sim card").
// A silent mid-solve reconnect keeps the in-memory transcript (the stream is
// NON-placeholder), so the wholesale replay above is skipped. The additive
// merge must surface a card the client is MISSING (the running SIM/dispatch
// cards minted while the socket was down) with NO refresh, and never duplicate
// a card the client already shows.
// --------------------------------------------------------------------------- //

const SIM_PIPE = "01PIPESIM00000000000000000";

function withRunningSim(history: CaseChatMessage[]): CaseChatMessage[] {
  return [
    ...history,
    {
      message_id: "01MSGSIM00000000000000000Z",
      case_id: CASE_ID,
      role: "tool",
      content: '{"tool_name":"sfincs:solve","state":"running"}',
      pipeline_id: SIM_PIPE,
      tool_card: {
        tool_name: "sfincs:solve",
        state: "running",
        started_at: "2026-06-10T00:00:10Z",
        label: "sfincs solve",
      },
      created_at: "2026-06-10T00:00:10Z",
    },
  ];
}

describe("bare reconnect surfaces a MISSING card without refresh", () => {
  it("injects the running sim card the non-placeholder stream is missing", () => {
    const cs = createChatStreams();
    // The client already holds the user prompt + a prior complete card (a live
    // mid-solve stream), but NOT the sim card (it was minted while the socket
    // was down). current_pipeline carries the live SIM pipeline so the resume
    // does NOT force-settle the injected running card.
    routeCaseOpen(cs, caseOpenWith(fullStreamHistory()));
    const before = getStream(cs, CASE_ID);
    expect(before.pipeline.history).toHaveLength(1); // fetch card only

    routeSessionState(
      cs,
      sessionStateWith(withRunningSim(fullStreamHistory()), {
        pipeline_id: SIM_PIPE,
        steps: [
          {
            step_id: "live-sim",
            name: "sfincs solve",
            tool_name: "sfincs:solve",
            state: "running",
          },
        ],
      }),
      CASE_ID,
    );

    const s = getStream(cs, CASE_ID);
    // The sim card was ADDITIVELY injected (history grew 1 -> 2); the prior
    // fetch card + bubbles are untouched.
    expect(s.pipeline.history).toHaveLength(2);
    const sim = s.pipeline.history
      .flatMap((h) => h.steps ?? [])
      .find((st) => st.tool_name === "sfincs:solve");
    expect(sim).toBeDefined();
    expect(sim!.state).toBe("running");
    expect(s.messages).toHaveLength(2); // bubbles unchanged
  });

  it("does NOT duplicate the sim card when the client already has it LIVE", () => {
    const cs = createChatStreams();
    routeCaseOpen(cs, caseOpenWith(fullStreamHistory()));
    // The client received the live SIM card (wire step_id, NOT a replay id) for
    // pipeline SIM_PIPE + tool sfincs:solve.
    routePipelineState(
      cs,
      {
        pipeline_id: SIM_PIPE,
        steps: [
          {
            step_id: "01LIVESTEP000000000000000",
            name: "sfincs solve",
            tool_name: "sfincs:solve",
            state: "running",
          },
        ],
      },
      CASE_ID,
    );
    const before = getStream(cs, CASE_ID);
    const simCountBefore = [
      ...before.pipeline.history,
      ...(before.pipeline.live ? [before.pipeline.live] : []),
    ]
      .flatMap((h) => h.steps ?? [])
      .filter((st) => st.tool_name === "sfincs:solve").length;
    expect(simCountBefore).toBe(1);

    // A reconnect resume carries the persisted twin of that SAME card (a
    // different synthesized step_id but the SAME pipeline_id + tool_name).
    routeSessionState(
      cs,
      sessionStateWith(withRunningSim(fullStreamHistory()), {
        pipeline_id: SIM_PIPE,
        steps: [
          {
            step_id: "01LIVESTEP000000000000000",
            name: "sfincs solve",
            tool_name: "sfincs:solve",
            state: "running",
          },
        ],
      }),
      CASE_ID,
    );

    const after = getStream(cs, CASE_ID);
    const simCountAfter = [
      ...after.pipeline.history,
      ...(after.pipeline.live ? [after.pipeline.live] : []),
    ]
      .flatMap((h) => h.steps ?? [])
      .filter((st) => st.tool_name === "sfincs:solve").length;
    expect(simCountAfter).toBe(1); // dedup by pipeline_id::tool_name — no twin
  });

  it("is idempotent — a second reconnect resume injects nothing new", () => {
    const cs = createChatStreams();
    routeCaseOpen(cs, caseOpenWith(fullStreamHistory()));
    const resume = () =>
      routeSessionState(
        cs,
        sessionStateWith(withRunningSim(fullStreamHistory()), {
          pipeline_id: SIM_PIPE,
          steps: [
            {
              step_id: "live-sim",
              name: "sfincs solve",
              tool_name: "sfincs:solve",
              state: "running",
            },
          ],
        }),
        CASE_ID,
      );
    resume();
    const after1 = getStream(cs, CASE_ID).pipeline.history.length;
    resume();
    resume();
    const after3 = getStream(cs, CASE_ID).pipeline.history.length;
    expect(after3).toBe(after1); // strict no-op after the first surface
  });
});

describe("routeCaseOpen behavior is unchanged by the reconnect-replay fix", () => {
  it("routeCaseOpen still replays the full stream on first open", () => {
    const cs = createChatStreams();
    const opened = routeCaseOpen(cs, caseOpenWith(fullStreamHistory()));
    expect(opened).toBe(CASE_ID);
    const s = getStream(cs, CASE_ID);
    expect(s.messages).toHaveLength(2);
    expect(s.pipeline.history).toHaveLength(1);
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
  });

  it("a reconnect session-state BEFORE case-open primes the stream; the later case-open is then a no-op (no duplicate)", () => {
    const cs = createChatStreams();
    // Reconnect lands first and rebuilds the cards.
    routeSessionState(cs, sessionStateWith(fullStreamHistory()), CASE_ID);
    let s = getStream(cs, CASE_ID);
    expect(s.messages).toHaveLength(2);
    expect(s.pipeline.history).toHaveLength(1);

    // case-open then arrives for the same Case — its placeholder guard sees a
    // populated stream and skips its own replay (no duplication).
    const opened = routeCaseOpen(cs, caseOpenWith(fullStreamHistory()));
    expect(opened).toBe(CASE_ID);
    s = getStream(cs, CASE_ID);
    expect(s.messages).toHaveLength(2);
    expect(s.pipeline.history).toHaveLength(1);
  });
});
