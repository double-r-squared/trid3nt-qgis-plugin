// GRACE-2 web — Chat case-open replace-not-reconcile tests (job-0172 Part A).
//
// When `case-open` arrives the chat panel must FLUSH its local message
// buffer AND its inline pipeline view-model (live + history + session
// cursor) BEFORE rehydrating from `session_state.chat_history`. Otherwise
// switching Cases shows stale messages from the prior Case.

import { describe, it, expect } from "vitest";
import {
  pipelineReducer,
  rehydrateMessagesFromCaseOpen,
  PipelineInlineState,
} from "./Chat";
import {
  CaseOpenEnvelopePayload,
  CaseSessionState,
  PipelineStatePayload,
} from "./contracts";

function makeLivePipeline(): PipelineStatePayload {
  return {
    pipeline_id: "pipe-stale",
    steps: [
      {
        step_id: "s1",
        name: "stale_step",
        tool_name: "stale_tool",
        state: "running",
      },
    ],
  };
}

function makeSessionState(): CaseSessionState {
  return {
    case: {
      case_id: "01HV4ZWB9YTPK5G3RA6JM7N8YZ",
      title: "Test Case",
      created_at: "2026-06-08T00:00:00Z",
      updated_at: "2026-06-08T00:00:00Z",
      status: "active",
    },
    chat_history: [
      {
        message_id: "01HV4ZWB9YTPK5G3RA6JM7N8M1",
        case_id: "01HV4ZWB9YTPK5G3RA6JM7N8YZ",
        role: "user",
        content: "first prompt",
        created_at: "2026-06-08T00:00:00Z",
      },
      {
        message_id: "01HV4ZWB9YTPK5G3RA6JM7N8M2",
        case_id: "01HV4ZWB9YTPK5G3RA6JM7N8YZ",
        role: "agent",
        content: "first reply",
        created_at: "2026-06-08T00:00:01Z",
      },
      {
        message_id: "01HV4ZWB9YTPK5G3RA6JM7N8M3",
        case_id: "01HV4ZWB9YTPK5G3RA6JM7N8YZ",
        role: "system",
        content: "internal scaffolding",
        created_at: "2026-06-08T00:00:02Z",
      },
    ],
    loaded_layers: [],
    pipeline_history: [],
  };
}

describe("pipelineReducer case-open replace-not-reconcile (job-0172 Part A)", () => {
  it("flushes live + history + session cursor on case-open", () => {
    const stale: PipelineInlineState = {
      live: makeLivePipeline(),
      history: [
        {
          pipeline_id: "pipe-prior",
          steps: [
            {
              step_id: "p1",
              name: "complete_step",
              tool_name: "done_tool",
              state: "complete",
            },
          ],
        },
      ],
      currentPipelineFromSession: {
        pipeline_id: "session-pipe",
        started_at: null,
        completed_at: null,
        final_state: null,
        steps: [],
      },
    };
    const next = pipelineReducer(stale, { type: "case-open" });
    expect(next.live).toBeNull();
    expect(next.history).toEqual([]);
    expect(next.currentPipelineFromSession).toBeNull();
  });
});

describe("rehydrateMessagesFromCaseOpen (job-0172 Part A)", () => {
  it("returns [] for a null session_state", () => {
    const payload: CaseOpenEnvelopePayload = { session_state: null };
    expect(rehydrateMessagesFromCaseOpen(payload)).toEqual([]);
  });

  it("returns [] for an undefined chat_history", () => {
    const session = makeSessionState();
    delete session.chat_history;
    expect(
      rehydrateMessagesFromCaseOpen({ session_state: session }),
    ).toEqual([]);
  });

  it("converts chat_history into ChatMessage[] with done=true", () => {
    const out = rehydrateMessagesFromCaseOpen({
      session_state: makeSessionState(),
    });
    expect(out).toHaveLength(2); // system filtered
    expect(out[0]!.role).toBe("user");
    expect(out[0]!.text).toBe("first prompt");
    expect(out[0]!.done).toBe(true);
    expect(out[1]!.role).toBe("agent");
    expect(out[1]!.text).toBe("first reply");
  });

  it("filters out 'system' role messages (not renderable in the local view)", () => {
    const out = rehydrateMessagesFromCaseOpen({
      session_state: makeSessionState(),
    });
    expect(out.find((m) => m.role === "system" as unknown as "user")).toBeUndefined();
  });
});
