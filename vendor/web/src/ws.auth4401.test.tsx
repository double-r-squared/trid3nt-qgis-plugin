// GRACE-2 web — 4401 auth-gate handling tests (job-0253, sprint-13.5).
//
// The agent's production auth gate (`AUTH_REQUIRED=true`) emits an
// `AUTH_FAILED` error envelope then closes the WebSocket with code 4401 (A.5).
// ws.ts must NOT enter the reconnect loop on that close — an invalid/expired
// token would re-trip the gate on every backoff tick. Instead it tries ONE
// fresh-token retry, then surfaces `auth-expired` via `onAuthExpired`.
//
// Verified here:
//   1. close(4401) with NO fresh token → no reconnect storm + onAuthExpired.
//   2. close(4401) WITH a fresh token  → exactly one reconnect, no auth-expired.
//   3. close(4401) → fresh token → still rejected → auth-expired (no loop).
//   4. AUTH_FAILED error envelope latches the failure so a code-less close
//      still routes to auth-expired (not reconnect).
//   5. A normal (non-4401) close STILL reconnects (no regression).
//   6. The auth-token envelope is still sent on connect (existing path green).

import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  GraceWs,
  AUTH_FAILED_CLOSE_CODE,
  __test_resetSessionHub,
  clearAnonymousUserId,
  type WsHandlers,
} from "./ws";
import type { ErrorPayload } from "./contracts";

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
    ts: "2026-06-11T00:00:00.000Z",
    session_id: "01ABCDEFGHJKMNPQRSTVWX0002",
    payload,
  });
}

function openedSockets(): WebSocket[] {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return ((window as any).__webSockets as WebSocket[] | undefined) ?? [];
}

function lastOpenedSocket(): WebSocket | null {
  const s = openedSockets();
  return s.length === 0 ? null : s[s.length - 1]!;
}

function injectMessage(ws: WebSocket, raw: string): void {
  ws.dispatchEvent(new MessageEvent("message", { data: raw }));
}

/**
 * Dispatch a close event carrying a code. happy-dom's CloseEvent is available;
 * fall back to a plain Event with a patched `code` if the constructor rejects
 * the init dict in some environment.
 */
function injectClose(ws: WebSocket, code: number): void {
  let ev: Event;
  try {
    ev = new CloseEvent("close", { code });
  } catch {
    ev = new Event("close");
    Object.defineProperty(ev, "code", { value: code });
  }
  ws.dispatchEvent(ev);
}

const AUTH_FAILED_ERR: ErrorPayload = {
  error_code: "AUTH_FAILED",
  message: "Authentication required",
  retryable: false,
};

describe("ws.ts — 4401 auth-gate handling (job-0253)", () => {
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
    vi.useFakeTimers();
  });

  it("exposes the A.5 close code constant", () => {
    expect(AUTH_FAILED_CLOSE_CODE).toBe(4401);
  });

  it("close(4401) with no fresh token: no reconnect storm + onAuthExpired fires", async () => {
    const onAuthExpired = vi.fn();
    // No fresh token available — getter returns null (signed-out / refresh fail).
    const idTokenGetter = vi.fn().mockResolvedValue(null);
    const ws = new GraceWs("ws://localhost:8765", makeHandlers({ onAuthExpired, idTokenGetter }));
    ws.connect();
    const socket = lastOpenedSocket();
    if (!socket) {
      ws.close();
      return;
    }
    const socketCountBefore = openedSockets().length;

    injectClose(socket, AUTH_FAILED_CLOSE_CODE);
    // Let the async handleAuthFailure (token-refresh await) settle.
    await vi.runAllTimersAsync();

    // No NEW socket was opened (no reconnect storm).
    expect(openedSockets().length).toBe(socketCountBefore);
    // Auth-expired surfaced.
    expect(onAuthExpired).toHaveBeenCalledOnce();
    ws.close();
  });

  it("close(4401) WITH a fresh token: exactly ONE reconnect, no auth-expired", async () => {
    const onAuthExpired = vi.fn();
    // Fresh token available on refresh — one retry should reconnect.
    const idTokenGetter = vi.fn().mockResolvedValue("fresh.jwt.token");
    const ws = new GraceWs("ws://localhost:8765", makeHandlers({ onAuthExpired, idTokenGetter }));
    ws.connect();
    const socket = lastOpenedSocket();
    if (!socket) {
      ws.close();
      return;
    }
    const countBefore = openedSockets().length;

    injectClose(socket, AUTH_FAILED_CLOSE_CODE);
    await vi.runAllTimersAsync();

    // Exactly one new socket opened (the single retry), no auth-expired yet.
    expect(openedSockets().length).toBe(countBefore + 1);
    expect(onAuthExpired).not.toHaveBeenCalled();
    ws.close();
  });

  it("close(4401) → refresh → still rejected: surfaces auth-expired, no loop", async () => {
    const onAuthExpired = vi.fn();
    const idTokenGetter = vi.fn().mockResolvedValue("fresh.jwt.token");
    const ws = new GraceWs("ws://localhost:8765", makeHandlers({ onAuthExpired, idTokenGetter }));
    ws.connect();
    let socket = lastOpenedSocket();
    if (!socket) {
      ws.close();
      return;
    }
    // First rejection → one retry (reconnect).
    injectClose(socket, AUTH_FAILED_CLOSE_CODE);
    await vi.runAllTimersAsync();
    const countAfterRetry = openedSockets().length;

    // The retried socket is rejected again.
    socket = lastOpenedSocket()!;
    injectClose(socket, AUTH_FAILED_CLOSE_CODE);
    await vi.runAllTimersAsync();

    // No further reconnect (retry was one-shot) and auth-expired surfaced.
    expect(openedSockets().length).toBe(countAfterRetry);
    expect(onAuthExpired).toHaveBeenCalledOnce();
    ws.close();
  });

  it("AUTH_FAILED error envelope latches the failure (code-less close → auth-expired, not reconnect)", async () => {
    const onAuthExpired = vi.fn();
    const onError = vi.fn();
    const idTokenGetter = vi.fn().mockResolvedValue(null);
    const ws = new GraceWs(
      "ws://localhost:8765",
      makeHandlers({ onAuthExpired, onError, idTokenGetter }),
    );
    ws.connect();
    const socket = lastOpenedSocket();
    if (!socket) {
      ws.close();
      return;
    }
    const countBefore = openedSockets().length;

    // Agent sends AUTH_FAILED error envelope FIRST, then closes WITHOUT a code.
    injectMessage(socket, makeEnvelope("error", AUTH_FAILED_ERR));
    expect(onError).toHaveBeenCalledOnce();
    injectClose(socket, 1006 /* abnormal close, no auth code */);
    await vi.runAllTimersAsync();

    // The latch routed the code-less close to auth-expired, not reconnect.
    expect(openedSockets().length).toBe(countBefore);
    expect(onAuthExpired).toHaveBeenCalledOnce();
    ws.close();
  });

  it("a normal (non-4401) close STILL reconnects — no regression", async () => {
    const onAuthExpired = vi.fn();
    const ws = new GraceWs("ws://localhost:8765", makeHandlers({ onAuthExpired }));
    ws.connect();
    const socket = lastOpenedSocket();
    if (!socket) {
      ws.close();
      return;
    }
    const countBefore = openedSockets().length;

    injectClose(socket, 1006 /* transient drop */);
    await vi.runAllTimersAsync();

    // A new socket was opened (reconnect happened) and auth-expired did NOT fire.
    expect(openedSockets().length).toBeGreaterThan(countBefore);
    expect(onAuthExpired).not.toHaveBeenCalled();
    ws.close();
  });

  it("still sends the auth-token envelope on connect (existing path green)", async () => {
    const idTokenGetter = vi.fn().mockResolvedValue("a.real.jwt");
    const ws = new GraceWs("ws://localhost:8765", makeHandlers({ idTokenGetter }));
    ws.connect();
    const socket = lastOpenedSocket();
    if (!socket) {
      ws.close();
      return;
    }
    const sendSpy = vi.spyOn(socket, "send");
    // The "open" handler runs maybeSendAuthToken (async). Flush microtasks.
    socket.dispatchEvent(new Event("open"));
    await vi.runAllTimersAsync();

    const authFrames = sendSpy.mock.calls
      .map((c) => String(c[0]))
      .filter((s) => s.includes('"auth-token"'));
    expect(authFrames.length).toBeGreaterThan(0);
    // The fresh token is carried on the wire under the server's `token` field.
    expect(authFrames.some((s) => s.includes("a.real.jwt"))).toBe(true);
    ws.close();
  });
});
