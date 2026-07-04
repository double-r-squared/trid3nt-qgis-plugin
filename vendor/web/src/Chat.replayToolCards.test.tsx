// GRACE-2 web — job-0267 full-stream replay tests.
//
// The agent now persists the FULL stream per Case turn: the user bubble,
// one role="tool" CaseChatMessage per dispatched registry tool (typed
// ToolCardRecord payload — terminal state + authoritative duration_ms), and
// the agent's REAL accumulated narration. On case-open (first open this
// session) `replayStreamFromChatHistory` rebuilds the stream so tool cards
// re-render INLINE between the bubbles exactly where they happened — the
// user-verified Wave 4.12 bug was that only their own messages survived a
// Case reopen.
//
// Pure-helper pattern (Chat cannot mount in happy-dom): exercise
// routeCaseOpen / replayStreamFromChatHistory / buildInterleavedStream
// directly, same as Chat.perCaseStreams.test.tsx.

import { describe, it, expect } from "vitest";
import {
  createChatStreams,
  emptyStreamState,
  getStream,
  routeCaseOpen,
  replayStreamFromChatHistory,
  buildInterleavedStream,
} from "./Chat";
import {
  CaseChatMessage,
  CaseOpenEnvelopePayload,
  CaseSessionState,
} from "./contracts";

const CASE_ID = "01CASEAAAAAAAAAAAAAAAAAAAA";

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

describe("replayStreamFromChatHistory (job-0267)", () => {
  it("rebuilds bubbles AND tool cards from the persisted full stream", () => {
    const s = emptyStreamState();
    replayStreamFromChatHistory(s, fullStreamHistory());

    // Bubbles: user + agent (with the REAL narration text).
    expect(s.messages.map((m) => m.role)).toEqual(["user", "agent"]);
    expect(s.messages[1]!.text).toBe(
      "I fetched the DEM and added it to the map.",
    );

    // Tool card: one synthesized terminal pipeline snapshot in history.
    expect(s.pipeline.history).toHaveLength(1);
    const step = s.pipeline.history[0]!.steps![0]!;
    expect(step.tool_name).toBe("fetch_3dep_dem");
    expect(step.state).toBe("complete");
    expect(step.duration_ms).toBe(2340);
  });

  it("interleaves the replayed card BETWEEN the bubbles (created_at order)", () => {
    const s = emptyStreamState();
    replayStreamFromChatHistory(s, fullStreamHistory());
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

  it("replays failed tool cards with the failed state", () => {
    const s = emptyStreamState();
    const history = fullStreamHistory();
    history[1] = {
      ...history[1]!,
      tool_card: {
        tool_name: "run_solver",
        state: "failed",
        duration_ms: 120,
      },
    };
    replayStreamFromChatHistory(s, history);
    expect(s.pipeline.history[0]!.steps![0]!.state).toBe("failed");
  });

  // BUG 4b (Wave 4.9) — a persisted FAILED tool-card row must survive replay AND
  // reach the rendered stream as a tool entry carrying state:"failed" (the input
  // PipelineCard turns into the red terminal card). This pins the WHOLE pure
  // replay→interleave path, not just the reducer snapshot.
  it("a persisted FAILED tool card interleaves as a tool entry with state=failed (red card)", () => {
    const s = emptyStreamState();
    const history = fullStreamHistory();
    history[1] = {
      ...history[1]!,
      content: '{"tool_name":"run_solver","state":"failed"}',
      tool_card: {
        tool_name: "run_solver",
        state: "failed",
        started_at: "2026-06-10T00:00:01Z",
        duration_ms: 120,
        label: "run_solver",
      },
    };
    replayStreamFromChatHistory(s, history);

    const stream = buildInterleavedStream(
      s.messages,
      s.pipeline.history,
      s.pipeline.live,
      s.messageOrder,
      s.stepOrder,
    );
    // Order preserved: user → tool → agent.
    expect(stream.map((e) => e.kind)).toEqual([
      "user-message",
      "tool",
      "agent-message",
    ]);
    // The tool entry carries the FAILED state through to PipelineCard input.
    const toolEntry = stream.find((e) => e.kind === "tool") as
      | { kind: "tool"; step: { state: string; tool_name: string } }
      | undefined;
    expect(toolEntry).toBeDefined();
    expect(toolEntry!.step.state).toBe("failed");
    expect(toolEntry!.step.tool_name).toBe("run_solver");
  });

  it("skips tool rows without the typed card and unknown roles (no crash)", () => {
    const s = emptyStreamState();
    const history = fullStreamHistory();
    history[1] = { ...history[1]!, tool_card: undefined };
    history.push({
      message_id: "01MSGSYST0000000000000000E",
      case_id: CASE_ID,
      role: "system",
      content: "internal scaffolding",
      created_at: "2026-06-10T00:00:06Z",
    });
    replayStreamFromChatHistory(s, history);
    expect(s.messages.map((m) => m.role)).toEqual(["user", "agent"]);
    expect(s.pipeline.history).toHaveLength(0);
  });

  it("routeCaseOpen replays the full stream on first open", () => {
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

  // Durable SIM-card lifecycle (NATE "nothing about the chat is transient"):
  // the SOLVE card is persisted `running` at mint, so a mid-run reconnect/reopen
  // replays a SPINNING card (state="running") instead of dropping it. After the
  // solve the SAME row is upserted to its terminal state and replays terminal.
  it("replays a RUNNING sim card so a mid-run reconnect keeps the live solve card", () => {
    const s = emptyStreamState();
    const history = fullStreamHistory();
    history[1] = {
      ...history[1]!,
      content: '{"tool_name":"sfincs:solve","state":"running"}',
      tool_card: {
        tool_name: "sfincs:solve",
        state: "running",
        started_at: "2026-06-10T00:00:01Z",
        label: "sfincs solve",
      },
    };
    replayStreamFromChatHistory(s, history);

    expect(s.pipeline.history).toHaveLength(1);
    const step = s.pipeline.history[0]!.steps![0]!;
    expect(step.tool_name).toBe("sfincs:solve");
    expect(step.state).toBe("running");

    // It interleaves as a tool entry carrying running -> PipelineCard spins.
    const stream = buildInterleavedStream(
      s.messages,
      s.pipeline.history,
      s.pipeline.live,
      s.messageOrder,
      s.stepOrder,
    );
    const toolEntry = stream.find((e) => e.kind === "tool") as
      | { kind: "tool"; step: { state: string; tool_name: string } }
      | undefined;
    expect(toolEntry).toBeDefined();
    expect(toolEntry!.step.state).toBe("running");
    expect(toolEntry!.step.tool_name).toBe("sfincs:solve");
  });

  it("replays a CANCELLED sim card (a stopped solve stays traceable)", () => {
    const s = emptyStreamState();
    const history = fullStreamHistory();
    history[1] = {
      ...history[1]!,
      content: '{"tool_name":"sfincs:solve","state":"cancelled"}',
      tool_card: {
        tool_name: "sfincs:solve",
        state: "cancelled",
        started_at: "2026-06-10T00:00:01Z",
        duration_ms: 4200,
        label: "sfincs solve",
      },
    };
    replayStreamFromChatHistory(s, history);
    expect(s.pipeline.history[0]!.steps![0]!.state).toBe("cancelled");
  });
});
