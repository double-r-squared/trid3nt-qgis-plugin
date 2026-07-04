// GRACE-2 web — job-0253b: auth-token wire-ordering + re-sign-in reconnect.
//
// Two findings from the job-0253 adversarial panel, both prod-only
// (dev/disabled mode never engages the agent's AUTH_REQUIRED gate):
//
//   FINDING 1 (wire order) — `auth-token` MUST be the FIRST envelope on every
//   connection. The agent gate (server.py:4047-4063) dispatches in arrival
//   order and rejects the FIRST non-auth-token frame with 4401. Before the fix
//   ws.ts sent `session-resume` synchronously on open and `auth-token` only
//   after an awaited getIdToken() — so a signed-in user's valid token was
//   never read. This suite captures the literal on-wire ordering on a fresh
//   connection for BOTH the real-token path and the empty-token anonymous
//   path and asserts auth-token strictly precedes session-resume.
//
//   FINDING 2 (re-sign-in reconnect) — covered at the App/Chat integration
//   layer in App.resignin.test.tsx; here we prove the ws.ts primitive the App
//   relies on: a fresh connect() opens exactly one new socket and resets the
//   auth latches (no double-connect of a live socket; a getIdToken failure
//   never wedges the open handler).
//
// happy-dom does NOT track WebSocket instances, so this suite installs its own
// deterministic fake WebSocket that records every send() and lets the test
// drive open/close/message events synchronously.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  GraceWs,
  __test_resetSessionHub,
  clearAnonymousUserId,
  type WsHandlers,
} from "./ws";

// ── Deterministic fake WebSocket ───────────────────────────────────────── //
// Records sent frames; the test fires open()/close() explicitly. Mirrors the
// browser WebSocket surface ws.ts touches: readyState, send, close,
// addEventListener, dispatchEvent.

class FakeWebSocket {
  static OPEN = 1;
  static CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  readonly OPEN = 1;
  readonly CLOSED = 3;
  url: string;
  readyState = 0; // CONNECTING
  sent: string[] = [];
  private listeners: Record<string, ((ev: unknown) => void)[]> = {};

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  addEventListener(type: string, cb: (ev: unknown) => void): void {
    (this.listeners[type] ??= []).push(cb);
  }

  dispatchEvent(ev: { type: string }): boolean {
    for (const cb of this.listeners[ev.type] ?? []) cb(ev);
    return true;
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.readyState = this.CLOSED;
  }

  // Test drivers.
  fireOpen(): void {
    this.readyState = this.OPEN;
    this.dispatchEvent({ type: "open" });
  }

  fireClose(code: number): void {
    this.readyState = this.CLOSED;
    this.dispatchEvent({ type: "close", code } as { type: string });
  }

  /** The envelope `type` field of every frame sent so far, in order. */
  sentTypes(): string[] {
    return this.sent.map((raw) => {
      try {
        return JSON.parse(raw).type as string;
      } catch {
        return "<unparseable>";
      }
    });
  }
}

let originalWebSocket: typeof WebSocket;

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

function lastSocket(): FakeWebSocket {
  const s = FakeWebSocket.instances;
  return s[s.length - 1]!;
}

beforeEach(() => {
  __test_resetSessionHub();
  clearAnonymousUserId();
  try {
    window.localStorage.clear();
  } catch {
    /* ignore */
  }
  FakeWebSocket.instances = [];
  originalWebSocket = globalThis.WebSocket;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).WebSocket = FakeWebSocket;
});

afterEach(() => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).WebSocket = originalWebSocket;
});

// ── FINDING 1: auth-token strictly first ───────────────────────────────── //

describe("job-0253b — auth-token is the FIRST envelope (real-token path)", () => {
  it("auth-token precedes session-resume on connect with a real token", async () => {
    const idTokenGetter = vi.fn<() => Promise<string | null>>()
      .mockResolvedValue("real.jwt.token");
    const ws = new GraceWs("ws://localhost:8765", makeHandlers({ idTokenGetter }));
    ws.connect();
    const sock = lastSocket();
    expect(sock).toBeDefined();

    sock.fireOpen();
    // Let maybeSendAuthToken (await getter()) settle, then the chained resume.
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));

    const types = sock.sentTypes();
    const authIdx = types.indexOf("auth-token");
    const resumeIdx = types.indexOf("session-resume");
    expect(authIdx).toBeGreaterThanOrEqual(0);
    expect(resumeIdx).toBeGreaterThanOrEqual(0);
    // THE invariant the agent gate enforces: auth-token strictly first.
    expect(authIdx).toBeLessThan(resumeIdx);
    // And it is literally the FIRST frame on the wire.
    expect(types[0]).toBe("auth-token");

    // The token actually rode on the auth-token frame.
    const authFrame = JSON.parse(sock.sent[authIdx]!);
    expect(authFrame.payload.token).toBe("real.jwt.token");
    expect(authFrame.payload.anonymous).toBe(false);

    ws.close();
  });
});

describe("job-0253b — auth-token is the FIRST envelope (empty-token anonymous path)", () => {
  it("auth-token (empty) STILL precedes session-resume — dev/anon byte-order preserved", async () => {
    // No token: the anonymous path. job-0172 Part C always sends an auth-token
    // envelope (empty token) so ordering must hold here too.
    const idTokenGetter = vi.fn<() => Promise<string | null>>()
      .mockResolvedValue(null);
    const ws = new GraceWs("ws://localhost:8765", makeHandlers({ idTokenGetter }));
    ws.connect();
    const sock = lastSocket();

    sock.fireOpen();
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));

    const types = sock.sentTypes();
    expect(types[0]).toBe("auth-token");
    const authIdx = types.indexOf("auth-token");
    const resumeIdx = types.indexOf("session-resume");
    expect(authIdx).toBeLessThan(resumeIdx);

    const authFrame = JSON.parse(sock.sent[authIdx]!);
    expect(authFrame.payload.token).toBe("");
    expect(authFrame.payload.anonymous).toBe(true);

    ws.close();
  });

  it("a getIdToken FAILURE does not wedge the open handler — resume still follows", async () => {
    // getIdToken throws (network error, expired refresh). maybeSendAuthToken
    // swallows it (empty-token send); the chained session-resume must STILL be
    // emitted after the await settles.
    const idTokenGetter = vi.fn<() => Promise<string | null>>()
      .mockRejectedValue(new Error("network down"));
    const ws = new GraceWs("ws://localhost:8765", makeHandlers({ idTokenGetter }));
    ws.connect();
    const sock = lastSocket();

    sock.fireOpen();
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));

    const types = sock.sentTypes();
    // Both frames present, auth-token first, in the right order — no wedge.
    expect(types[0]).toBe("auth-token");
    expect(types.indexOf("auth-token")).toBeLessThan(
      types.indexOf("session-resume"),
    );

    ws.close();
  });
});

// ── FINDING 2 (ws-primitive half): fresh connect() resets latches, once ── //

describe("job-0253b — connect() reconnect primitive (re-sign-in recovery)", () => {
  it("a fresh connect() after auth-expired opens exactly ONE new socket", async () => {
    const onAuthExpired = vi.fn();
    const idTokenGetter = vi.fn<() => Promise<string | null>>()
      .mockResolvedValue(null);
    const ws = new GraceWs(
      "ws://localhost:8765",
      makeHandlers({ onAuthExpired, idTokenGetter }),
    );
    ws.connect();
    const first = lastSocket();
    // Gate rejects → 4401 close. No refresh token → auth-expired (terminal).
    first.fireClose(4401);
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));
    expect(onAuthExpired).toHaveBeenCalledOnce();
    const countAfterExpiry = FakeWebSocket.instances.length;

    // The App, on a recovered re-sign-in, re-runs the ws effect → new GraceWs
    // + connect(). Here we model the connect() primitive directly: exactly one
    // new socket, latches reset (a subsequent normal close reconnects).
    ws.connect();
    expect(FakeWebSocket.instances.length).toBe(countAfterExpiry + 1);

    ws.close();
  });
});
