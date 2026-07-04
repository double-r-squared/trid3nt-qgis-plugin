// GRACE-2 web — LANE CASE-WEB: client-is-authoritative-about-its-case.
//
// Root cause (wf_baa3273e): the client never told the server which Case it was
// looking at, and a case-select tapped while the socket wasn't OPEN was
// silently dropped (sendEnvelope no-op). On reconnect the server replayed its
// STALE in-memory active case — the snap. This suite proves the ws.ts fixes:
//
//   1. setCurrentCaseId stamps `case_id` onto the session-resume sent on
//      (re)connect — so a reconnect re-asserts the client's current Case.
//   2. setCurrentCaseId stamps `case_id` onto every outbound user-message — so
//      the server binds the turn to the case the client is actually viewing.
//   3. A case-command(select) issued while the socket is NOT OPEN is QUEUED and
//      FLUSHED on open (after auth-token + session-resume), never dropped.
//   4. A user-message issued while NOT OPEN is likewise queued + flushed.
//   5. The flush order on open is: auth-token, session-resume, then the queued
//      intent frames — the gate's first-frame rule + case re-assert hold.
//
// happy-dom does NOT track WebSocket instances, so this suite installs its own
// deterministic fake WebSocket (mirrors ws.authwireorder.test.tsx).

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  GraceWs,
  __test_resetSessionHub,
  clearAnonymousUserId,
  type WsHandlers,
} from "./ws";

// ── Deterministic fake WebSocket ───────────────────────────────────────── //

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

  fireClose(code = 1006): void {
    this.readyState = this.CLOSED;
    this.dispatchEvent({ type: "close", code } as { type: string });
  }

  /** Parsed frames in send order. */
  frames(): Array<{ type: string; payload: Record<string, unknown> }> {
    return this.sent.map((raw) => JSON.parse(raw));
  }

  sentTypes(): string[] {
    return this.frames().map((f) => f.type);
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
    // Default to the empty-token anonymous path so the open handler settles
    // deterministically without a Firebase dependency.
    idTokenGetter: vi.fn<() => Promise<string | null>>().mockResolvedValue(null),
    ...overrides,
  };
}

function lastSocket(): FakeWebSocket {
  const s = FakeWebSocket.instances;
  return s[s.length - 1]!;
}

/** Let the open handler's awaited maybeSendAuthToken + chained resume settle. */
async function settleOpen(): Promise<void> {
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

// ── 1. session-resume carries the current case on (re)connect ──────────── //

describe("LANE CASE-WEB — session-resume re-asserts the client's current case", () => {
  it("stamps case_id onto session-resume when a case is active", async () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    ws.setCurrentCaseId("01CASEAAAAAAAAAAAAAAAAAAAA");
    ws.connect();
    const sock = lastSocket();
    sock.fireOpen();
    await settleOpen();

    const resume = sock.frames().find((f) => f.type === "session-resume");
    expect(resume).toBeDefined();
    expect(resume!.payload.case_id).toBe("01CASEAAAAAAAAAAAAAAAAAAAA");
    ws.close();
  });

  it("sends case_id:null (root view) when no case is active — empty-payload shape preserved", async () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    ws.connect();
    const sock = lastSocket();
    sock.fireOpen();
    await settleOpen();

    const resume = sock.frames().find((f) => f.type === "session-resume");
    expect(resume).toBeDefined();
    expect(resume!.payload.case_id).toBeNull();
    ws.close();
  });

  it("RECONNECT re-asserts the case the client switched to while disconnected", async () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    ws.connect();
    const first = lastSocket();
    first.fireOpen();
    await settleOpen();

    // Socket drops; the client switches Case while reconnecting.
    first.fireClose(1006);
    ws.setCurrentCaseId("01NEWCASEBBBBBBBBBBBBBBBBBB");

    // Model the reconnect primitive directly (a fresh connect opens a new
    // socket); the open handler reads the LATEST currentCaseId at connect time.
    ws.connect();
    const second = lastSocket();
    second.fireOpen();
    await settleOpen();

    const resume = second.frames().find((f) => f.type === "session-resume");
    expect(resume!.payload.case_id).toBe("01NEWCASEBBBBBBBBBBBBBBBBBB");
    ws.close();
  });
});

// ── 2. user-message carries the current case ───────────────────────────── //

describe("LANE CASE-WEB — user-message is stamped with the current case", () => {
  it("stamps case_id onto an outbound user-message", async () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    ws.setCurrentCaseId("01CASECCCCCCCCCCCCCCCCCCCC");
    ws.connect();
    const sock = lastSocket();
    sock.fireOpen();
    await settleOpen();

    ws.sendUserMessage("model the flood", "research", null);
    const msg = sock.frames().find((f) => f.type === "user-message");
    expect(msg).toBeDefined();
    expect(msg!.payload.case_id).toBe("01CASECCCCCCCCCCCCCCCCCCCC");
    expect(msg!.payload.text).toBe("model the flood");
    ws.close();
  });

  it("stamps case_id:null when sent from the root view", async () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    ws.connect();
    const sock = lastSocket();
    sock.fireOpen();
    await settleOpen();

    ws.sendUserMessage("hello", "research", null);
    const msg = sock.frames().find((f) => f.type === "user-message");
    expect(msg!.payload.case_id).toBeNull();
    ws.close();
  });
});

// ── 3. case-command(select) issued while NOT OPEN is queued + flushed ───── //

describe("LANE CASE-WEB — a case-select tapped mid-reconnect is delivered, not dropped", () => {
  it("queues case-command(select) while connecting and flushes it on open", async () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    ws.connect();
    const sock = lastSocket();
    // Socket is still CONNECTING (fireOpen NOT called yet). Tap select now.
    expect(sock.readyState).toBe(0);
    ws.sendCaseCommand("select", "01CASEDDDDDDDDDDDDDDDDDDDD", {});
    // Nothing sent yet — it was buffered, not dropped (the old no-op).
    expect(sock.sentTypes()).not.toContain("case-command");

    sock.fireOpen();
    await settleOpen();

    const types = sock.sentTypes();
    expect(types).toContain("case-command");
    const cmd = sock.frames().find((f) => f.type === "case-command");
    expect(cmd!.payload.command).toBe("select");
    expect(cmd!.payload.case_id).toBe("01CASEDDDDDDDDDDDDDDDDDDDD");
    ws.close();
  });

  it("a queued select updates currentCaseId so the session-resume ALSO re-asserts it", async () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    ws.connect();
    const sock = lastSocket();
    ws.sendCaseCommand("select", "01CASEEEEEEEEEEEEEEEEEEEEEE", {});
    expect(ws.caseId).toBe("01CASEEEEEEEEEEEEEEEEEEEEEE");

    sock.fireOpen();
    await settleOpen();

    const resume = sock.frames().find((f) => f.type === "session-resume");
    expect(resume!.payload.case_id).toBe("01CASEEEEEEEEEEEEEEEEEEEEEE");
    ws.close();
  });

  it("flush order on open is auth-token, session-resume, THEN the queued select", async () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    ws.connect();
    const sock = lastSocket();
    ws.sendCaseCommand("select", "01CASEFFFFFFFFFFFFFFFFFFFFF", {});

    sock.fireOpen();
    await settleOpen();

    const types = sock.sentTypes();
    const authIdx = types.indexOf("auth-token");
    const resumeIdx = types.indexOf("session-resume");
    const cmdIdx = types.indexOf("case-command");
    expect(authIdx).toBeGreaterThanOrEqual(0);
    expect(resumeIdx).toBeGreaterThan(authIdx);
    expect(cmdIdx).toBeGreaterThan(resumeIdx);
    ws.close();
  });
});

// ── 4. user-message issued while NOT OPEN is queued + flushed ───────────── //

describe("LANE CASE-WEB — a user-message sent while connecting is queued + flushed", () => {
  it("queues a user-message while connecting and flushes it (case-stamped) on open", async () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    ws.setCurrentCaseId("01CASEGGGGGGGGGGGGGGGGGGGGG");
    ws.connect();
    const sock = lastSocket();
    ws.sendUserMessage("queued prompt", "research", null);
    expect(sock.sentTypes()).not.toContain("user-message");

    sock.fireOpen();
    await settleOpen();

    const msg = sock.frames().find((f) => f.type === "user-message");
    expect(msg).toBeDefined();
    expect(msg!.payload.text).toBe("queued prompt");
    expect(msg!.payload.case_id).toBe("01CASEGGGGGGGGGGGGGGGGGGGGG");
    ws.close();
  });
});

// ── 5. queued frames survive across a never-opened socket / are not duped ─ //

describe("LANE CASE-WEB — queue hygiene", () => {
  it("flushes each queued frame exactly once", async () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    ws.connect();
    const sock = lastSocket();
    ws.sendCaseCommand("select", "01CASEHHHHHHHHHHHHHHHHHHHHH", {});

    sock.fireOpen();
    await settleOpen();

    const cmdFrames = sock.frames().filter((f) => f.type === "case-command");
    expect(cmdFrames).toHaveLength(1);
    ws.close();
  });

  it("a closed (user-teardown) connection drops its queue — no stale replay", async () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    ws.connect();
    ws.sendCaseCommand("select", "01CASEIIIIIIIIIIIIIIIIIIIII", {});
    ws.close();
    // Re-open the SAME instance: the queue was cleared by close().
    ws.connect();
    const sock2 = lastSocket();
    sock2.fireOpen();
    await settleOpen();
    expect(sock2.sentTypes()).not.toContain("case-command");
    ws.close();
  });
});
