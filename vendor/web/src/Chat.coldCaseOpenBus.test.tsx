// GRACE-2 web — COLD chat-history render over the case-open bus (job-0179).
//
// Today, opening a Case while the agent box is ASLEEP leaves the conversation
// blank even though the cold serverless snapshot carries the full
// chat_history. ROOT CAUSE: the only code that materializes chat bubbles is
// routeCaseOpen -> replayStreamFromChatHistory, and in production that was
// reachable ONLY from Chat's OWN live-WS onCaseOpen handler. The cold path
// (App.fetchCaseView -> useCases_onCaseOpen) and App's live onCaseOpen routed
// the CaseOpenEnvelopePayload only into App's useCases state, NEVER into Chat's
// stream map; Chat did not subscribe to App's bus at all.
//
// FIX: createLayerPanelBus now carries a CASE-OPEN channel
// (pushCaseOpen / subscribeCaseOpen). App pushes every case-open (cold + live)
// onto it; Chat subscribes and runs the SAME body as its live onCaseOpen
// handler. This suite exercises that exact channel end to end at the bus level
// (the data path the new Chat effect runs) and locks the idempotency invariant.

import { describe, it, expect } from "vitest";
import { createLayerPanelBus } from "./LayerPanel";
import { routeCaseOpen, createChatStreams } from "./Chat";
import { CaseOpenEnvelopePayload, CaseSessionState } from "./contracts";

const COLD_CASE_ID = "01HV4ZWB9YTPK5G3RA6JM7N8YZ";

function makeSessionState(): CaseSessionState {
  return {
    case: {
      case_id: COLD_CASE_ID,
      title: "Cold Case",
      created_at: "2026-06-19T00:00:00Z",
      updated_at: "2026-06-19T00:00:00Z",
      status: "active",
    },
    chat_history: [
      {
        message_id: "01HV4ZWB9YTPK5G3RA6JM7N8M1",
        case_id: COLD_CASE_ID,
        role: "user",
        content: "what is the flood risk here?",
        created_at: "2026-06-19T00:00:00Z",
      },
      {
        message_id: "01HV4ZWB9YTPK5G3RA6JM7N8M2",
        case_id: COLD_CASE_ID,
        role: "agent",
        content: "Here is the inundation summary...",
        created_at: "2026-06-19T00:00:01Z",
      },
    ],
    loaded_layers: [],
    pipeline_history: [],
  };
}

function makeColdPayload(): CaseOpenEnvelopePayload {
  return { session_state: makeSessionState() };
}

describe("createLayerPanelBus case-open channel (job-0179)", () => {
  it("exposes pushCaseOpen + subscribeCaseOpen", () => {
    const bus = createLayerPanelBus();
    expect(typeof bus.pushCaseOpen).toBe("function");
    expect(typeof bus.subscribeCaseOpen).toBe("function");
  });

  it("delivers a pushed CaseOpenEnvelopePayload to every subscriber", () => {
    const bus = createLayerPanelBus();
    const seenA: CaseOpenEnvelopePayload[] = [];
    const seenB: CaseOpenEnvelopePayload[] = [];
    bus.subscribeCaseOpen((p) => seenA.push(p));
    bus.subscribeCaseOpen((p) => seenB.push(p));
    const payload = makeColdPayload();
    bus.pushCaseOpen(payload);
    expect(seenA).toEqual([payload]);
    expect(seenB).toEqual([payload]);
  });

  it("stops delivering after the unsubscribe is called", () => {
    const bus = createLayerPanelBus();
    const seen: CaseOpenEnvelopePayload[] = [];
    const unsub = bus.subscribeCaseOpen((p) => seen.push(p));
    bus.pushCaseOpen(makeColdPayload());
    unsub();
    bus.pushCaseOpen(makeColdPayload());
    expect(seen).toHaveLength(1);
  });
});

describe("COLD chat-history renders through the bus (job-0179)", () => {
  it("materializes the chat bubbles into Chat's stream map", () => {
    // This wires the EXACT data path the new Chat effect runs: a cold case-open
    // pushed onto the bus -> the subscriber routes it into Chat's streams via
    // routeCaseOpen -> replayStreamFromChatHistory hard-assigns s.messages.
    const bus = createLayerPanelBus();
    const streams = createChatStreams();
    bus.subscribeCaseOpen((p) => {
      routeCaseOpen(streams, p);
    });

    // Box ASLEEP: nothing has built this Case's stream yet.
    expect(streams.streams.has(COLD_CASE_ID)).toBe(false);

    bus.pushCaseOpen(makeColdPayload());

    const stream = streams.streams.get(COLD_CASE_ID);
    expect(stream).toBeDefined();
    // The conversation rendered: both non-system rows became chat bubbles.
    expect(stream!.messages).toHaveLength(2);
    expect(stream!.messages[0]!.role).toBe("user");
    expect(stream!.messages[0]!.text).toBe("what is the flood risk here?");
    expect(stream!.messages[0]!.done).toBe(true);
    expect(stream!.messages[1]!.role).toBe("agent");
    expect(stream!.messages[1]!.text).toBe("Here is the inundation summary...");
  });

  it("is idempotent: a second case-open (cold + live race) does NOT double render", () => {
    // routeCaseOpen rebuilds a stream ONLY when !cs.streams.has(caseId), so
    // whichever of the cold push / live onCaseOpen fires first builds the
    // stream and the other is a no-op. Push twice and assert the messages are
    // not duplicated and the stream object is the SAME reference.
    const bus = createLayerPanelBus();
    const streams = createChatStreams();
    bus.subscribeCaseOpen((p) => {
      routeCaseOpen(streams, p);
    });

    bus.pushCaseOpen(makeColdPayload()); // first source (e.g. cold snapshot)
    const firstStream = streams.streams.get(COLD_CASE_ID);
    bus.pushCaseOpen(makeColdPayload()); // second source (e.g. live onCaseOpen)
    const secondStream = streams.streams.get(COLD_CASE_ID);

    expect(secondStream).toBe(firstStream); // same object, not rebuilt
    expect(secondStream!.messages).toHaveLength(2); // not 4
  });

  it("a null session_state clears the root stream and builds no Case stream", () => {
    const bus = createLayerPanelBus();
    const streams = createChatStreams();
    bus.subscribeCaseOpen((p) => {
      routeCaseOpen(streams, p);
    });
    bus.pushCaseOpen({ session_state: null });
    expect(streams.streams.has(COLD_CASE_ID)).toBe(false);
  });
});
