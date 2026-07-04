// GRACE-2 web — Session-durability Job D: composer-stuck-as-Stop after a lost
// completion frame.
//
// Root cause: when a turn completes server-side but the completion/close frame
// is lost on a dropped socket, the client's in-flight latch
// (currentPipelineFromSession / a running pipeline step) is never cleared, so
// the send button renders Stop forever — a tap routes to cancel and Enter
// early-returns, so a real prompt never sends (zero user-message reaches the
// server).
//
// This suite proves the three fixes, in two layers:
//
//   Chat pure-helper layer (the React component cannot mount in happy-dom — it
//   opens a WebSocket — so, following the established Chat.test.tsx pattern, we
//   exercise the exported stream-routing core that the component's watchdog +
//   onReconnectResumed handlers call):
//     (1) a stream with a non-null currentPipelineFromSession (an in-flight
//         turn whose terminal frame was lost) is force-settled by a
//         routeTurnComplete into the VISIBLE key (what the watchdog dispatches
//         after the no-inbound-activity bound, and what onReconnectResumed
//         dispatches on every successful resume) -> shouldShowCancel goes false
//         -> the composer returns to send-enabled.
//     (2) the settlement targets the VISIBLE stream independent of owning-case
//         routing: a different (non-visible) case's stream is NOT touched.
//
//   ws.ts layer (the FakeWebSocket harness mirrors ws.caseAuthority.test.tsx):
//     (3) sendCancel issued while the socket is NOT OPEN is QUEUED (not dropped)
//         and FLUSHED on the next open — a tap on a stuck Stop button mid-
//         reconnect is honoured.
//     (4) onReconnectResumed fires after the resume handshake on every (re)open,
//         with firstOpen=true on the first connect and false on the reconnect.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  ROOT_STREAM_KEY,
  createChatStreams,
  getStream,
  streamKeyFor,
  routePipelineState,
  routeSessionState,
  routeTurnComplete,
  shouldShowCancel,
} from "./Chat";
import type { PipelineStatePayload, SessionStatePayload } from "./contracts";
import {
  GraceWs,
  __test_resetSessionHub,
  clearAnonymousUserId,
  type WsHandlers,
} from "./ws";

// ── Chat pure-helper layer ─────────────────────────────────────────────── //

/** A session-state with a non-null current_pipeline = "turn is in-flight". */
function inFlightSessionState(pipelineId: string): SessionStatePayload {
  return {
    chat_history: [],
    loaded_layers: [],
    map_view: null,
    current_pipeline: {
      pipeline_id: pipelineId,
      started_at: "2026-06-22T00:00:00.000Z",
      completed_at: null,
      final_state: null,
      steps: [
        { step_id: "s1", tool_name: "fetch_dem", state: "running", label: null },
      ],
    },
    pipeline_history: [],
  } as unknown as SessionStatePayload;
}

/** A running pipeline-state frame (a tool card mid-flight). */
function runningPipeline(pipelineId: string): PipelineStatePayload {
  return {
    pipeline_id: pipelineId,
    started_at: "2026-06-22T00:00:00.000Z",
    completed_at: null,
    final_state: null,
    steps: [
      { step_id: "s1", tool_name: "fetch_dem", state: "running", label: null },
    ],
  } as unknown as PipelineStatePayload;
}

describe("Job D — composer un-sticks via routeTurnComplete into the visible stream", () => {
  it("a lost-terminal-frame latch (non-null current_pipeline) clears on a turn-complete into the visible key", () => {
    const cs = createChatStreams();
    const visibleKey = ROOT_STREAM_KEY;
    // Turn starts: a running pipeline-state + an in-flight session-state land in
    // the visible stream and set the cancel latch (the composer shows Stop).
    routePipelineState(cs, runningPipeline("p1"), visibleKey);
    routeSessionState(cs, inFlightSessionState("p1"), visibleKey);
    expect(shouldShowCancel(getStream(cs, visibleKey).pipeline)).toBe(true);

    // The completion/close frame was lost on a dropped socket: no terminal
    // frame ever arrives. The watchdog (no inbound activity) / onReconnectResumed
    // force-dispatch a turn-complete into the VISIBLE key.
    routeTurnComplete(cs, {}, streamKeyFor(null));

    // The latch is cleared: the composer returns to idle (send-enabled).
    expect(shouldShowCancel(getStream(cs, visibleKey).pipeline)).toBe(false);
    expect(
      getStream(cs, visibleKey).pipeline.currentPipelineFromSession,
    ).toBeNull();
    // The previously-running step is settled to a terminal state (no card spins).
    const live = getStream(cs, visibleKey).pipeline;
    const allSteps = [
      ...(live.live?.steps ?? []),
      ...live.history.flatMap((h) => h.steps ?? []),
    ];
    expect(allSteps.some((s) => s.state === "running")).toBe(false);
  });

  it("settling the visible stream does NOT clear a different (non-visible) case's in-flight latch", () => {
    const cs = createChatStreams();
    const visibleKey = ROOT_STREAM_KEY;
    const otherKey = "01CASEAAAAAAAAAAAAAAAAAAAA";
    // A genuinely-running turn lives in another (non-visible) case's stream.
    routePipelineState(cs, runningPipeline("pOther"), otherKey);
    routeSessionState(cs, inFlightSessionState("pOther"), otherKey);
    // The visible stream also has a stuck latch (its terminal frame was lost).
    routePipelineState(cs, runningPipeline("pVisible"), visibleKey);
    routeSessionState(cs, inFlightSessionState("pVisible"), visibleKey);

    // Watchdog/resume settle the VISIBLE key only.
    routeTurnComplete(cs, {}, streamKeyFor(null));

    // Visible composer un-sticks; the other case's in-flight turn is untouched.
    expect(shouldShowCancel(getStream(cs, visibleKey).pipeline)).toBe(false);
    expect(shouldShowCancel(getStream(cs, otherKey).pipeline)).toBe(true);
  });

  it("is idempotent: a turn-complete on an already-idle visible stream is a no-op", () => {
    const cs = createChatStreams();
    const visibleKey = ROOT_STREAM_KEY;
    // No in-flight turn — the composer is already idle.
    expect(shouldShowCancel(getStream(cs, visibleKey).pipeline)).toBe(false);
    routeTurnComplete(cs, {}, streamKeyFor(null));
    routeTurnComplete(cs, {}, streamKeyFor(null));
    expect(shouldShowCancel(getStream(cs, visibleKey).pipeline)).toBe(false);
  });
});

// ── ws.ts layer (FakeWebSocket harness) ────────────────────────────────── //

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

  sentTypes(): string[] {
    return this.sent.map((raw) => JSON.parse(raw).type as string);
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
    // Empty-token anonymous path so the open handler settles deterministically
    // without a Firebase dependency.
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

describe("Job D (3) — sendCancel is queued when the socket is not OPEN", () => {
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

  it("buffers a cancel tapped while CONNECTING and flushes it on open (not dropped)", async () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    ws.connect();
    const sock = lastSocket();
    // Socket is still CONNECTING (never fired open): a tap on the stuck Stop
    // button calls sendCancel. Pre-fix this was a bare sendEnvelope -> dropped.
    expect(sock.readyState).toBe(0);
    ws.sendCancel("user-cancel");
    expect(sock.sent.length).toBe(0); // nothing sent yet — buffered

    // Socket opens; the open handler flushes auth-token + session-resume + the
    // queued cancel.
    sock.fireOpen();
    await settleOpen();
    const types = sock.sentTypes();
    expect(types).toContain("auth-token");
    expect(types).toContain("session-resume");
    expect(types).toContain("cancel");
    // Order: the cancel flushes AFTER auth-token + session-resume (the gate's
    // first-frame rule holds; the cancel is not the first frame).
    expect(types.indexOf("cancel")).toBeGreaterThan(types.indexOf("auth-token"));
    expect(types.indexOf("cancel")).toBeGreaterThan(
      types.indexOf("session-resume"),
    );
    ws.close();
  });

  it("sends a cancel immediately when the socket is OPEN (fast path unchanged)", async () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    ws.connect();
    const sock = lastSocket();
    sock.fireOpen();
    await settleOpen();
    const before = sock.sent.length;
    ws.sendCancel("user-cancel");
    expect(sock.sentTypes()).toContain("cancel");
    expect(sock.sent.length).toBe(before + 1);
    ws.close();
  });
});

describe("Job D (2) — onReconnectResumed fires after the resume handshake", () => {
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

  it("fires with firstOpen=true on the first open and false on a reconnect", async () => {
    const onReconnectResumed =
      vi.fn<(firstOpen: boolean) => void>();
    const ws = new GraceWs(
      "ws://localhost:8765",
      makeHandlers({ onReconnectResumed }),
    );
    ws.connect();
    const sock1 = lastSocket();
    sock1.fireOpen();
    await settleOpen();
    expect(onReconnectResumed).toHaveBeenCalledTimes(1);
    expect(onReconnectResumed.mock.calls[0]![0]).toBe(true);

    // The socket drops; drive a fresh open via forceReconnect() (the iOS
    // zombie-socket path: unconditionally tears the current socket down and
    // re-opens). This is the genuine-reconnect case onReconnectResumed must mark
    // firstOpen=false.
    ws.forceReconnect();
    const sock2 = lastSocket();
    expect(sock2).not.toBe(sock1);
    sock2.fireOpen();
    await settleOpen();
    expect(onReconnectResumed).toHaveBeenCalledTimes(2);
    // Second fire is a reconnect, not the first open.
    expect(onReconnectResumed.mock.calls[1]![0]).toBe(false);
    ws.close();
  });

  it("does not throw when onReconnectResumed is absent (optional handler)", async () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    ws.connect();
    const sock = lastSocket();
    expect(() => {
      sock.fireOpen();
    }).not.toThrow();
    await settleOpen();
    ws.close();
  });
});
