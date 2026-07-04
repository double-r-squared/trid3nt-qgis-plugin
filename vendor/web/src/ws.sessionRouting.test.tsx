// GRACE-2 web — per-user-agent-isolation (NATE 2026-06-22), code piece 2 of 2.
//
// The future per-session broker must choose / provision the right per-session
// Fargate task BEFORE proxying the WebSocket upgrade. The only routing datum
// available to it pre-upgrade is the request line's query string, so ws.ts
// carries the stable per-session id as `?sid=<id>` on the connect URL — in
// ADDITION to (never instead of) the existing `auth-token` + `session-resume`
// handshake the agent reads post-upgrade.
//
// This suite pins:
//   1. The connect URL carries `?sid=<sessionId>` and the id equals the SAME
//      stable session ULID exposed by `ws.session` (no parallel id).
//   2. The sid on the URL matches the session_id sent in the `session-resume`
//      envelope (broker pre-upgrade key == agent post-upgrade binding).
//   3. NON-BREAKING for the current single box: the auth-token + session-resume
//      wire handshake is byte-for-byte unchanged; the box ignores the unknown
//      query param.
//   4. A URL that already carries a query string gets the sid appended with `&`
//      (robust against a future URL shape), never dropping the existing params.
//
// happy-dom does not track WebSocket instances, so the suite installs its own
// deterministic fake WebSocket (mirrors ws.authwireorder.test.tsx).

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  GraceWs,
  __test_resetSessionHub,
  clearAnonymousUserId,
  type WsHandlers,
} from "./ws";

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

  fireOpen(): void {
    this.readyState = this.OPEN;
    this.dispatchEvent({ type: "open" });
  }

  /** The parsed envelope of every frame sent so far, in order. */
  sentEnvelopes(): Array<Record<string, unknown>> {
    return this.sent.map((raw) => {
      try {
        return JSON.parse(raw) as Record<string, unknown>;
      } catch {
        return {};
      }
    });
  }

  sentTypes(): string[] {
    return this.sentEnvelopes().map((e) => (e.type as string) ?? "<unparseable>");
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

async function settle(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await new Promise((r) => setTimeout(r, 0));
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

describe("per-session broker routing — ?sid carrier", () => {
  it("carries ?sid=<sessionId> on the connect URL, reusing the stable session id", () => {
    const ws = new GraceWs("wss://agent.example/ws", makeHandlers());
    ws.connect();
    const sock = lastSocket();
    expect(sock).toBeDefined();

    const u = new URL(sock.url);
    expect(u.searchParams.get("sid")).toBe(ws.session);
    // The session id is the stable 26-char ULID (persisted in localStorage),
    // not a fresh per-connection value.
    expect(ws.session.length).toBe(26);
  });

  it("the URL sid matches the session_id in the session-resume envelope", async () => {
    const ws = new GraceWs("wss://agent.example/ws", makeHandlers());
    ws.connect();
    const sock = lastSocket();
    sock.fireOpen();
    await settle();

    const sidParam = new URL(sock.url).searchParams.get("sid");
    const resume = sock
      .sentEnvelopes()
      .find((e) => e.type === "session-resume");
    expect(resume).toBeDefined();
    // Pre-upgrade routing key (sid) and post-upgrade session binding agree.
    expect(resume!.session_id).toBe(sidParam);
  });

  it("is NON-BREAKING: the auth-token + session-resume handshake is unchanged", async () => {
    const idTokenGetter = vi
      .fn<() => Promise<string | null>>()
      .mockResolvedValue("real.jwt.token");
    const ws = new GraceWs(
      "ws://localhost:8765",
      makeHandlers({ idTokenGetter }),
    );
    ws.connect();
    const sock = lastSocket();
    sock.fireOpen();
    await settle();

    const types = sock.sentTypes();
    // auth-token is still strictly first, session-resume still follows — adding
    // ?sid to the URL did not perturb the application-level frames the current
    // single box reads.
    expect(types[0]).toBe("auth-token");
    expect(types.indexOf("auth-token")).toBeLessThan(
      types.indexOf("session-resume"),
    );
  });

  it("appends sid with & when the URL already has a query string", () => {
    const ws = new GraceWs("wss://agent.example/ws?region=us", makeHandlers());
    ws.connect();
    const sock = lastSocket();

    expect(sock.url).toContain("&sid=");
    const u = new URL(sock.url);
    // Pre-existing query params are preserved alongside the new sid.
    expect(u.searchParams.get("region")).toBe("us");
    expect(u.searchParams.get("sid")).toBe(ws.session);
  });
});
