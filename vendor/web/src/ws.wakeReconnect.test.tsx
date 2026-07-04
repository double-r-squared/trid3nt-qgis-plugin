// GRACE-2 web — ws.ts reconnect / wake-signal wiring tests.
//
// sleep/wake STAGE 2 (NATE 2026-06-18) — NEVER AUTO-WAKE. The reconnect loop no
// longer POSTs the wake endpoint. This file verifies the STAGE-2 contract:
//   1. When a socket drops and the close handler schedules a reconnect,
//      `onWakeNeeded(attempt)` fires with an incrementing attempt counter — but
//      NO wake POST is issued (the box is woken ONLY by an explicit user tap).
//   2. The attempt counter increments across consecutive failed reconnects and
//      resets after a successful (re)open.
//   3. The reconnect backoff still revives a fresh socket (the loop is intact).
//   4. `reportWakeState()` delegates a REPORT-ONLY GET to the injected waker
//      (asleep detection) and NEVER POSTs / wakes the box.
//
// Mobile connect-attempt timeout (transport surface): a socket that never opens
// (the box is STOPPED and the TCP connect hangs) is torn down after
// CONNECT_ATTEMPT_TIMEOUT_MS so the EXISTING close handler runs scheduleReconnect
// -> onWakeNeeded - surfacing the wake overlay in ~10s instead of the browser's
// default 30-120s connect timeout. A socket that opens promptly CLEARS the timer
// and is never torn down by it (no regression to the no-10s-cycling contract).

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  GraceWs,
  CONNECT_ATTEMPT_TIMEOUT_MS,
  __test_resetSessionHub,
  type WsHandlers,
} from "./ws";
import { AgentWaker } from "./lib/wake";

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

function instanceSocket(ws: GraceWs): WebSocket | null {
  return (ws as unknown as { socket: WebSocket | null }).socket;
}

function forceReadyState(socket: WebSocket, state: number): void {
  Object.defineProperty(socket, "readyState", {
    configurable: true,
    get: () => state,
  });
}

describe("GraceWs — reconnect signal + report-only wake (sleep/wake STAGE 2)", () => {
  beforeEach(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (window as any).__webSockets = [];
    __test_resetSessionHub();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.unstubAllEnvs();
    vi.resetModules();
    vi.restoreAllMocks();
  });

  function dropSocket(ws: GraceWs): void {
    const s = instanceSocket(ws);
    expect(s).not.toBeNull();
    forceReadyState(s!, 3); // CLOSED
    s!.dispatchEvent(new CloseEvent("close", { code: 1006 }));
  }

  it("fires onWakeNeeded(attempt) on a scheduled reconnect but NEVER auto-POSTs wake", async () => {
    vi.stubEnv("VITE_GRACE2_WAKE_URL", "https://explicit.example/wake");
    const fetchFn = vi.fn(async () => ({ ok: true, status: 200 }));
    const waker = new AgentWaker({ fetchFn });
    const onWakeNeeded = vi.fn();

    const ws = new GraceWs(
      "ws://localhost:8765",
      makeHandlers({ onWakeNeeded }),
      { waker },
    );
    ws.connect();
    dropSocket(ws);

    // The close handler scheduled a reconnect → the UI signal fired…
    expect(onWakeNeeded).toHaveBeenCalledTimes(1);
    expect(onWakeNeeded).toHaveBeenLastCalledWith(1);
    // …but NO wake POST was issued (never auto-wake). Flush microtasks to be sure.
    await Promise.resolve();
    await Promise.resolve();
    expect(fetchFn).not.toHaveBeenCalled();

    ws.close();
  });

  it("increments the attempt counter across consecutive failed reconnects (no POST)", () => {
    vi.stubEnv("VITE_GRACE2_WAKE_URL", "https://explicit.example/wake");
    const fetchFn = vi.fn(async () => ({ ok: true, status: 200 }));
    const waker = new AgentWaker({ fetchFn });
    const onWakeNeeded = vi.fn();

    const ws = new GraceWs(
      "ws://localhost:8765",
      makeHandlers({ onWakeNeeded }),
      { waker },
    );
    ws.connect();

    dropSocket(ws);
    expect(onWakeNeeded).toHaveBeenLastCalledWith(1);

    // Advance the backoff so the scheduled reconnect opens a fresh socket,
    // then drop THAT one too. Job 1b raised the backoff floor to 1500ms and
    // added jitter (delay = base * factor, factor in [0.5,1.0), base caps at
    // maxBackoffMs=5000), so advance by the max backoff (5000) to reliably fire
    // the pending reconnect. 5000 >= any jittered delay yet < the connect-attempt
    // timeout (10000) and the 25s keepalive, so ONLY the reconnect fires.
    vi.advanceTimersByTime(5000);
    dropSocket(ws);
    expect(onWakeNeeded).toHaveBeenLastCalledWith(2);

    // Still no wake POST across either reconnect.
    expect(fetchFn).not.toHaveBeenCalled();

    ws.close();
  });

  it("still revives a fresh socket on the backoff reconnect", () => {
    vi.stubEnv("VITE_GRACE2_WAKE_URL", "https://explicit.example/wake");
    const fetchFn = vi.fn(async () => ({ ok: true, status: 200 }));
    const waker = new AgentWaker({ fetchFn });
    const ws = new GraceWs(
      "ws://localhost:8765",
      makeHandlers(),
      { waker },
    );
    ws.connect();
    const first = instanceSocket(ws);
    dropSocket(ws);

    // Backoff fires → a brand-new socket is opened. Job 1b raised the backoff
    // floor to 1500ms + jitter (base caps at maxBackoffMs=5000), so advance by
    // the max backoff (5000) to reliably fire the pending reconnect.
    vi.advanceTimersByTime(5000);
    const revived = instanceSocket(ws);
    expect(revived).not.toBeNull();
    expect(revived).not.toBe(first);

    ws.close();
  });

  it("resets the attempt counter after a successful (re)open", () => {
    vi.stubEnv("VITE_GRACE2_WAKE_URL", "https://explicit.example/wake");
    const fetchFn = vi.fn(async () => ({ ok: true, status: 200 }));
    const waker = new AgentWaker({ fetchFn });
    const onWakeNeeded = vi.fn();
    const ws = new GraceWs(
      "ws://localhost:8765",
      makeHandlers({ onWakeNeeded }),
      { waker },
    );
    ws.connect();

    dropSocket(ws);
    expect(onWakeNeeded).toHaveBeenLastCalledWith(1);

    // Let the backoff reconnect, then mark the fresh socket OPEN (fire 'open').
    // Job 1b raised the backoff floor to 1500ms + jitter (base caps at
    // maxBackoffMs=5000), so advance by the max backoff (5000) to reliably fire
    // the pending reconnect.
    vi.advanceTimersByTime(5000);
    const revived = instanceSocket(ws);
    expect(revived).not.toBeNull();
    forceReadyState(revived!, 1); // OPEN
    revived!.dispatchEvent(new Event("open"));

    // A subsequent drop starts the attempt counter over at 1.
    dropSocket(ws);
    expect(onWakeNeeded).toHaveBeenLastCalledWith(1);

    ws.close();
  });

  it("reportWakeState() delegates a REPORT-ONLY GET (asleep detection; never POSTs)", async () => {
    vi.stubEnv("VITE_GRACE2_WAKE_URL", "https://explicit.example/wake");
    const fetchFn = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ state: "stopped" }),
    }));
    const waker = new AgentWaker({ fetchFn });
    const ws = new GraceWs("ws://localhost:8765", makeHandlers(), { waker });

    const state = await ws.reportWakeState();
    expect(state).toBe("stopped");
    expect(fetchFn).toHaveBeenCalledTimes(1);
    expect(fetchFn).toHaveBeenCalledWith(
      "https://explicit.example/wake",
      expect.objectContaining({ method: "GET" }),
    );
    // The probe must NEVER POST.
    expect(fetchFn).not.toHaveBeenCalledWith(
      "https://explicit.example/wake",
      expect.objectContaining({ method: "POST" }),
    );

    ws.close();
  });
});

// ---------------------------------------------------------------------------
// Mobile connect-attempt timeout (transport surface). When the agent box is
// STOPPED the new WebSocket sits in CONNECTING while the browser waits out its
// default TCP connect timeout (30-120s) before firing close. ws.ts arms a
// one-shot CONNECT_ATTEMPT_TIMEOUT_MS timer the instant the socket is created;
// if it is still CONNECTING when the timer fires, it tears the socket down via
// ws.close() so the EXISTING close handler runs scheduleReconnect ->
// onWakeNeeded and the wake overlay surfaces in ~10s. A socket that opens
// promptly CLEARS the timer in its open handler and is never torn down by it.
// ---------------------------------------------------------------------------

describe("GraceWs - mobile connect-attempt timeout (transport surface)", () => {
  beforeEach(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (window as any).__webSockets = [];
    __test_resetSessionHub();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.unstubAllEnvs();
    vi.resetModules();
    vi.restoreAllMocks();
  });

  it("a socket that never opens fires onWakeNeeded within the connect timeout", () => {
    const onWakeNeeded = vi.fn();
    const ws = new GraceWs(
      "ws://localhost:8765",
      makeHandlers({ onWakeNeeded }),
    );
    ws.connect();

    const sock = instanceSocket(ws);
    expect(sock).not.toBeNull();
    // The socket is still CONNECTING (never opened) - exactly the stopped-box case.
    expect(sock!.readyState).toBe(0); // CONNECTING

    // Spy on close so we (a) confirm the connect-timer tears the socket down and
    // (b) stop happy-dom's async close from racing the manual close-event drive
    // below; the production close handler is exercised via the dispatched event.
    const closeSpy = vi
      .spyOn(sock!, "close")
      .mockImplementation(() => undefined);

    // BEFORE the timeout fires nothing has happened on the wake path.
    vi.advanceTimersByTime(CONNECT_ATTEMPT_TIMEOUT_MS - 1);
    expect(closeSpy).not.toHaveBeenCalled();
    expect(onWakeNeeded).not.toHaveBeenCalled();

    // The connect-attempt timer fires: the still-CONNECTING socket is torn down.
    vi.advanceTimersByTime(1);
    expect(closeSpy).toHaveBeenCalledTimes(1);

    // Drive the resulting close event (force CLOSED + dispatch) so the EXISTING
    // close handler runs scheduleReconnect -> onWakeNeeded - the whole point.
    forceReadyState(sock!, 3); // CLOSED
    sock!.dispatchEvent(new CloseEvent("close", { code: 1006 }));
    expect(onWakeNeeded).toHaveBeenCalledTimes(1);
    expect(onWakeNeeded).toHaveBeenLastCalledWith(1);

    ws.close();
  });

  it("a socket that opens promptly clears the timer and is never torn down by it", () => {
    const onWakeNeeded = vi.fn();
    // Inject a token getter so the open handler's maybeSendAuthToken resolves
    // without touching real Firebase.
    const idTokenGetter = vi.fn(async () => null);
    const ws = new GraceWs(
      "ws://localhost:8765",
      makeHandlers({ onWakeNeeded, idTokenGetter }),
    );
    ws.connect();

    const sock = instanceSocket(ws);
    expect(sock).not.toBeNull();
    const closeSpy = vi
      .spyOn(sock!, "close")
      .mockImplementation(() => undefined);
    // The forced OPEN readyState makes ws.ts treat the socket as OPEN, but the
    // happy-dom socket is internally still CONNECTING and its real send() would
    // throw; stub send to a no-op so the open handler's auth-token/session-resume
    // sends are harmless (this test is only about the connect-timer lifecycle).
    vi.spyOn(sock!, "send").mockImplementation(() => undefined);

    // The socket opens BEFORE the connect-attempt timeout. The open handler
    // clears the connect timer.
    forceReadyState(sock!, 1); // OPEN
    sock!.dispatchEvent(new Event("open"));

    // Advance well PAST the connect timeout: the (now cleared) timer must NOT
    // fire, so the OPEN socket is never closed and the wake path never runs.
    vi.advanceTimersByTime(CONNECT_ATTEMPT_TIMEOUT_MS * 3);
    expect(closeSpy).not.toHaveBeenCalled();
    expect(onWakeNeeded).not.toHaveBeenCalled();
    // The same socket is still the live one (it was not torn down + replaced).
    expect(instanceSocket(ws)).toBe(sock);

    ws.close();
  });
});
