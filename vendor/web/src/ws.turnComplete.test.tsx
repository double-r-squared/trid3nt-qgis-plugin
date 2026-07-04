// GRACE-2 web — lane W2: ws.ts turn-complete dispatch (C2 terminal durability).
//
// The agent emits a `turn-complete` envelope at the END of every turn (and
// re-emits it on session-resume). ws.ts must route it to the optional
// `onTurnComplete` handler, must NOT throw when the handler is absent, and the
// type must be session-scoped so a sibling GraceWs (App.tsx's connection)
// receives it via the fan-out hub.
//
// Same mock-socket harness as ws.test.tsx (happy-dom WebSocket stub +
// MessageEvent injection).

import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  GraceWs,
  __test_resetSessionHub,
  type WsHandlers,
} from "./ws";
import type { TurnCompletePayload } from "./contracts";

function makeHandlers(overrides: Partial<WsHandlers> = {}): WsHandlers {
  return {
    onStatus: vi.fn(),
    onAgentChunk: vi.fn(),
    onPipelineState: vi.fn(),
    onSessionState: vi.fn(),
    onError: vi.fn(),
    ...overrides,
  };
}

function makeEnvelope(type: string, payload: unknown, caseId?: string): string {
  return JSON.stringify({
    type,
    id: "01ABCDEFGHJKMNPQRSTVWX0001",
    ts: "2026-06-18T00:00:00.000Z",
    session_id: "01ABCDEFGHJKMNPQRSTVWX0002",
    ...(caseId ? { case_id: caseId } : {}),
    payload,
  });
}

function lastOpenedSocket(): WebSocket | null {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const sockets = (window as any).__webSockets as WebSocket[] | undefined;
  if (!sockets || sockets.length === 0) return null;
  return sockets[sockets.length - 1]!;
}

function injectMessage(ws: WebSocket, raw: string): void {
  ws.dispatchEvent(new MessageEvent("message", { data: raw }));
}

describe("GraceWs — turn-complete dispatch (C2)", () => {
  beforeEach(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (window as any).__webSockets = [];
    __test_resetSessionHub();
  });

  it("dispatches turn-complete to onTurnComplete with the case_id tag", () => {
    const onTurnComplete =
      vi.fn<(p: TurnCompletePayload, caseId?: string | null) => void>();
    const ws = new GraceWs("ws://localhost:8765", makeHandlers({ onTurnComplete }));
    ws.connect();
    const socket = lastOpenedSocket();
    if (!socket) {
      ws.close();
      return;
    }
    injectMessage(
      socket,
      makeEnvelope("turn-complete", { final_state: "complete" }, "01CASEX"),
    );
    expect(onTurnComplete).toHaveBeenCalledOnce();
    expect(onTurnComplete.mock.calls[0]![1]).toBe("01CASEX");
    ws.close();
  });

  it("does not throw when onTurnComplete is absent (optional handler)", () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    ws.connect();
    const socket = lastOpenedSocket();
    if (!socket) {
      ws.close();
      return;
    }
    expect(() => {
      injectMessage(socket, makeEnvelope("turn-complete", {}));
    }).not.toThrow();
    ws.close();
  });

  it("accepts a bare {} payload as a valid whole-turn idle (no required field)", () => {
    const onTurnComplete = vi.fn();
    const ws = new GraceWs("ws://localhost:8765", makeHandlers({ onTurnComplete }));
    ws.connect();
    const socket = lastOpenedSocket();
    if (!socket) {
      ws.close();
      return;
    }
    injectMessage(socket, makeEnvelope("turn-complete", {}));
    expect(onTurnComplete).toHaveBeenCalledOnce();
    ws.close();
  });

  it("fans turn-complete out to a sibling GraceWs on the same session", () => {
    // Both instances pull the SAME session_id from localStorage (Chat + App in
    // one tab) — turn-complete is session-scoped so a sibling receives it.
    const chatOnTurnComplete = vi.fn();
    const appOnTurnComplete = vi.fn();
    const chat = new GraceWs(
      "ws://localhost:8765",
      makeHandlers({ onTurnComplete: chatOnTurnComplete }),
    );
    const app = new GraceWs(
      "ws://localhost:8765",
      makeHandlers({ onTurnComplete: appOnTurnComplete }),
    );
    expect(chat.session).toBe(app.session);
    chat.connect();
    app.connect();
    const sockets = (window as unknown as { __webSockets?: WebSocket[] })
      .__webSockets;
    if (!sockets || sockets.length < 2) {
      chat.close();
      app.close();
      return;
    }
    const chatSocket = sockets[sockets.length - 2]!;
    // Server delivers turn-complete on Chat's wire only; App must see it via
    // the fan-out hub (the card it must force-complete lives in Chat's stream,
    // but the turn's tools may have run on App's connection).
    injectMessage(
      chatSocket,
      makeEnvelope("turn-complete", { final_state: "complete" }),
    );
    expect(chatOnTurnComplete).toHaveBeenCalledOnce();
    expect(appOnTurnComplete).toHaveBeenCalledOnce();
    chat.close();
    app.close();
  });
});
