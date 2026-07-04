// GRACE-2 web — BUG 1b: reconnect-backoff hardening (floor + jitter).
//
// The reconnect "storm" was the client's exponential-backoff ladder reacting to
// a burst of transport-level drops. The fix raises the FLOOR (500ms -> 1500ms)
// so the first reconnect waits longer, and adds JITTER to the scheduled delay so
// many tabs/sockets do not reconnect in lockstep and a flapping socket does not
// retry on a fixed cadence. The doubling and the 5000ms ceiling are unchanged.
//
// These tests drive the private `scheduleReconnect` directly (no live socket
// needed — the ladder math is socket-independent) and stub the randomness source
// via the `__test_setRng` hook so the jitter is deterministic. We capture the
// delay handed to window.setTimeout with fake timers.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { GraceWs, type WsHandlers } from "./ws";

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

// Reach the private scheduleReconnect without changing its visibility.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function scheduleReconnect(ws: GraceWs): void {
  (ws as unknown as { scheduleReconnect(): void }).scheduleReconnect();
}

// Read the private backoffMs (the base ladder value) for floor assertions.
function backoffMs(ws: GraceWs): number {
  return (ws as unknown as { backoffMs: number }).backoffMs;
}

describe("GraceWs — reconnect backoff hardening (BUG 1b)", () => {
  let setTimeoutSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.useFakeTimers();
    // Capture every scheduled delay so we can assert the jitter math. Fake
    // timers route window.setTimeout through Vitest's mock, so a spy here sees
    // the (callback, delay) pair scheduleReconnect hands it.
    setTimeoutSpy = vi.spyOn(window, "setTimeout");
  });

  afterEach(() => {
    setTimeoutSpy.mockRestore();
    vi.useRealTimers();
  });

  // Pull the delay (2nd arg) of the most recent window.setTimeout call.
  function lastScheduledDelay(): number {
    const calls = setTimeoutSpy.mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    return calls[calls.length - 1]![1] as number;
  }

  it("starts the backoff ladder at the raised FLOOR (>= 1500ms, was 500)", () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    // A fresh instance has not yet doubled, so backoffMs is the floor.
    expect(backoffMs(ws)).toBeGreaterThanOrEqual(1500);
    ws.close();
  });

  it("keeps the 5000ms ceiling (doubling unchanged)", () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    // rng = 0 -> the jitter factor is exactly 0.5 (lower edge); the BASE ladder
    // still doubles regardless of jitter, so after enough schedules it pins at
    // the 5000ms ceiling.
    ws.__test_setRng(() => 0);
    for (let i = 0; i < 12; i += 1) scheduleReconnect(ws);
    expect(backoffMs(ws)).toBe(5000);
    ws.close();
  });

  it("applies jitter: rng=0 yields exactly 0.5 x base (earliest retry)", () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    ws.__test_setRng(() => 0);
    // First schedule uses base = floor = 1500; factor = 0.5 + 0.5*0 = 0.5.
    scheduleReconnect(ws);
    expect(lastScheduledDelay()).toBe(Math.round(1500 * 0.5)); // 750
    ws.close();
  });

  it("applies jitter: rng~1 approaches 1.0 x base (never later than base)", () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    // rng() returns < 1 (like Math.random); 0.999999 -> factor ~= 1.0.
    ws.__test_setRng(() => 0.999999);
    scheduleReconnect(ws);
    const delay = lastScheduledDelay();
    // Upper bound: strictly <= base (1500); lower bound: > 0.99 x base.
    expect(delay).toBeLessThanOrEqual(1500);
    expect(delay).toBeGreaterThan(1500 * 0.99);
    ws.close();
  });

  it("scheduled delay always lands inside [0.5, 1.0] x base for any rng in [0,1)", () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    // Sweep a few deterministic rng values; each scheduled delay must sit in
    // the jitter window of the base value used for THAT schedule. We recompute
    // the base ourselves by mirroring the doubling so the assertion is tight.
    const seeds = [0, 0.25, 0.5, 0.75, 0.9999];
    let base = 1500; // floor; matches RECONNECT_FLOOR_MS
    for (const seed of seeds) {
      ws.__test_setRng(() => seed);
      scheduleReconnect(ws);
      const delay = lastScheduledDelay();
      expect(delay).toBeGreaterThanOrEqual(Math.round(base * 0.5));
      expect(delay).toBeLessThanOrEqual(base);
      base = Math.min(base * 2, 5000);
    }
    ws.close();
  });

  it("two different rng draws on the same base produce different delays (no lockstep)", () => {
    const wsA = new GraceWs("ws://localhost:8765", makeHandlers());
    const wsB = new GraceWs("ws://localhost:8765", makeHandlers());
    wsA.__test_setRng(() => 0.1);
    wsB.__test_setRng(() => 0.9);
    scheduleReconnect(wsA);
    const delayA = lastScheduledDelay();
    scheduleReconnect(wsB);
    const delayB = lastScheduledDelay();
    // Same base (both at floor), different rng -> different scheduled delay.
    expect(delayA).not.toBe(delayB);
    wsA.close();
    wsB.close();
  });
});
