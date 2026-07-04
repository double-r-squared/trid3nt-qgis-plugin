// GRACE-2 web — sticky anonymous user_id persistence (job-0172 Part C).
//
// On WebSocket connect the agent's H.3 anonymous fallback used to mint a
// fresh ULID every time, orphaning the user's Cases on every browser
// refresh. The fix:
//
//   1. ws.ts ALWAYS sends `auth-token` (even with an empty `id_token`) so
//      the agent receives an `anonymous_user_id` hint.
//   2. On `auth-ack(is_anonymous=true)` ws.ts persists `user_id` in
//      localStorage under `grace2.anonymous_user_id`.
//   3. On the next connect ws.ts reads the cached id and sends it as the
//      hint; the agent re-binds the same User.
//   4. On `auth-ack(is_anonymous=false)` ws.ts CLEARS the cached id (the
//      authenticated identity supersedes the anonymous hint).

import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  GraceWs,
  __test_resetSessionHub,
  type WsHandlers,
  type AuthAckPayload,
  readAnonymousUserId,
  writeAnonymousUserId,
  clearAnonymousUserId,
} from "./ws";

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

function makeEnvelope(type: string, payload: unknown): string {
  return JSON.stringify({
    type,
    id: "01ABCDEFGHJKMNPQRSTVWX0001",
    ts: "2026-06-08T00:00:00.000Z",
    session_id: "01ABCDEFGHJKMNPQRSTVWX0002",
    payload,
  });
}

function lastOpenedSocket(): WebSocket | null {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const sockets = (window as any).__webSockets as WebSocket[] | undefined;
  if (!sockets || sockets.length === 0) return null;
  // The length guard above proves this index is populated; the
  // non-null assertion satisfies noUncheckedIndexedAccess.
  return sockets[sockets.length - 1]!;
}

function injectMessage(ws: WebSocket, raw: string): void {
  ws.dispatchEvent(new MessageEvent("message", { data: raw }));
}

describe("sticky anonymous user_id (job-0172 Part C)", () => {
  beforeEach(() => {
    __test_resetSessionHub();
    clearAnonymousUserId();
    try {
      window.localStorage.clear();
    } catch {
      // ignore
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (window as any).__webSockets = [];
  });

  it("persists user_id on auth-ack with is_anonymous=true", () => {
    const handlers = makeHandlers();
    const ws = new GraceWs("ws://localhost:8765", handlers);
    ws.connect();
    const socket = lastOpenedSocket();
    if (!socket) {
      ws.close();
      return;
    }
    const ack: AuthAckPayload = {
      user_id: "01HV4ZWB9YTPK5G3RA6JM7N8YZ",
      firebase_uid: null,
      is_anonymous: true,
      tier: "free",
    };
    injectMessage(socket, makeEnvelope("auth-ack", ack));
    expect(readAnonymousUserId()).toBe("01HV4ZWB9YTPK5G3RA6JM7N8YZ");
    ws.close();
  });

  it("clears user_id on auth-ack with is_anonymous=false (real sign-in)", () => {
    writeAnonymousUserId("01HV4ZWB9YTPK5G3RA6JM7N8YA");
    expect(readAnonymousUserId()).toBe("01HV4ZWB9YTPK5G3RA6JM7N8YA");
    const handlers = makeHandlers();
    const ws = new GraceWs("ws://localhost:8765", handlers);
    ws.connect();
    const socket = lastOpenedSocket();
    if (!socket) {
      ws.close();
      return;
    }
    const ack: AuthAckPayload = {
      user_id: "01HV4ZWB9YTPK5G3RA6JM7N8YB",
      firebase_uid: "firebase-uid-001",
      is_anonymous: false,
      tier: "free",
    };
    injectMessage(socket, makeEnvelope("auth-ack", ack));
    expect(readAnonymousUserId()).toBeNull();
    ws.close();
  });

  it("forwards auth-ack to the onAuthAck handler when provided", () => {
    const onAuthAck = vi.fn<(p: AuthAckPayload) => void>();
    const handlers = makeHandlers({ onAuthAck });
    const ws = new GraceWs("ws://localhost:8765", handlers);
    ws.connect();
    const socket = lastOpenedSocket();
    if (!socket) {
      ws.close();
      return;
    }
    const ack: AuthAckPayload = {
      user_id: "01HV4ZWB9YTPK5G3RA6JM7N8YC",
      firebase_uid: null,
      is_anonymous: true,
      tier: "free",
    };
    injectMessage(socket, makeEnvelope("auth-ack", ack));
    expect(onAuthAck).toHaveBeenCalledOnce();
    expect(onAuthAck.mock.calls[0]![0].user_id).toBe(
      "01HV4ZWB9YTPK5G3RA6JM7N8YC",
    );
    ws.close();
  });

  it("readAnonymousUserId returns null for malformed (non-ULID) values", () => {
    try {
      window.localStorage.setItem("grace2.anonymous_user_id", "not-a-ulid");
    } catch {
      return;
    }
    expect(readAnonymousUserId()).toBeNull();
  });
});
