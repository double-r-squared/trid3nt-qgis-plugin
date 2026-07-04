// GRACE-2 web — job-0253b Finding 2: re-sign-in reconnect for BOTH GraceWs
// instances (App + Chat). Closes OQ-0253-CHAT-WS-4401.
//
// THE BUG: handleAuthFailure's give-up branch (ws.ts) emits onAuthExpired and
// schedules NO reconnect (correct — don't hammer the gate). But nothing
// reconnects LATER: App's ws effect deps are otherwise stable, onAuthChanged
// only cleared authExpired, and Chat keyed on [wsUrl, bump]. So after a
// successful re-sign-in the guard rendered children over two DEAD sockets
// until a full page reload.
//
// THE FIX: App bumps `authEpoch` exactly when a fresh non-anonymous user lands
// WHILE authExpired; `authEpoch` is threaded into BOTH ws effects' deps, so
// each instance tears its dead socket down and opens a fresh one — once per
// recovery, never in disabled/dev mode.
//
// This harness reproduces App.tsx's onAuthChanged→authEpoch logic and the two
// ws effects EXACTLY (same dep structure, same connect()/close() lifecycle),
// driving the REAL GraceWs against a deterministic fake WebSocket. It does not
// mount maplibre/WebGL (which happy-dom can't run) — the collapse-shell pattern
// established in App.test.tsx.

import {
  describe, it, expect, vi, beforeEach, afterEach,
} from "vitest";
import { render, act, cleanup } from "@testing-library/react";
import { useEffect, useRef, useState } from "react";
import {
  GraceWs,
  __test_resetSessionHub,
  clearAnonymousUserId,
  type WsHandlers,
} from "./ws";
import type { AuthUser } from "./auth";

// ── Deterministic fake WebSocket (records connects; never auto-opens) ────── //
class FakeWebSocket {
  static OPEN = 1;
  static CLOSED = 3;
  static instances: FakeWebSocket[] = [];
  readonly OPEN = 1;
  readonly CLOSED = 3;
  readyState = 0;
  url: string;
  private listeners: Record<string, ((ev: unknown) => void)[]> = {};
  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }
  addEventListener(t: string, cb: (ev: unknown) => void): void {
    (this.listeners[t] ??= []).push(cb);
  }
  dispatchEvent(ev: { type: string }): boolean {
    for (const cb of this.listeners[ev.type] ?? []) cb(ev);
    return true;
  }
  send(): void {}
  close(): void {
    this.readyState = this.CLOSED;
  }
  fireClose(code: number): void {
    this.readyState = this.CLOSED;
    this.dispatchEvent({ type: "close", code } as { type: string });
  }
}

// ── Controllable mock of ./auth.onAuthChanged ────────────────────────────── //
// `authSubscriber` is the App's callback; `mockOnAuthChanged` lets a test push
// a user (or null) through it. In disabled mode the real onAuthChanged fires
// null exactly once and never again — we model both modes.
let authSubscriber: ((u: AuthUser | null) => void) | null = null;
function pushAuth(u: AuthUser | null): void {
  act(() => {
    authSubscriber?.(u);
  });
}

/**
 * Flush microtasks + one macrotask inside act() so the async
 * handleAuthFailure chain (getIdToken → onAuthExpired → setAuthExpired) and
 * any React state updates it triggers all settle before the assertion.
 */
async function settle(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));
  });
}

/** Fire a 4401 close on the given sockets, then settle the async aftermath. */
async function expireAll(sockets: FakeWebSocket[]): Promise<void> {
  act(() => {
    for (const s of sockets) s.fireClose(4401);
  });
  await settle();
}
function mockOnAuthChanged(cb: (u: AuthUser | null) => void): () => void {
  authSubscriber = cb;
  return () => {
    authSubscriber = null;
  };
}

const GOOGLE_USER: AuthUser = {
  uid: "g-1", displayName: "G", email: "g@x.com", photoURL: null,
  isAnonymous: false,
};

function makeHandlers(overrides: Partial<WsHandlers> = {}): WsHandlers {
  return {
    onStatus: vi.fn(), onAgentChunk: vi.fn(), onPipelineState: vi.fn(),
    onSessionState: vi.fn(), onError: vi.fn(), ...overrides,
  };
}

// ── Harness: App.tsx onAuthChanged→authEpoch + the two ws effects, verbatim ─ //
// `disabled` simulates Firebase-off: onAuthChanged never delivers a non-null
// user (the real disabled-mode contract), so authExpired is never set and
// authEpoch never bumps.
function ReconnectHarness({ wsUrl }: { wsUrl: string }): JSX.Element {
  const [authExpired, setAuthExpired] = useState(false);
  const [authEpoch, setAuthEpoch] = useState(0);
  const authExpiredRef = useRef(false);
  authExpiredRef.current = authExpired;

  // App's onAuthChanged effect (verbatim logic).
  useEffect(() => {
    const unsub = mockOnAuthChanged((u) => {
      if (u && !u.isAnonymous) {
        if (authExpiredRef.current) setAuthEpoch((n) => n + 1);
        setAuthExpired(false);
      }
    });
    return unsub;
  }, []);

  // App's ws effect (App-instance). onAuthExpired sets the latch; deps include
  // authEpoch.
  const appWsRef = useRef<GraceWs | null>(null);
  useEffect(() => {
    const ws = new GraceWs(wsUrl, makeHandlers({
      onAuthExpired: () => setAuthExpired(true),
    }));
    appWsRef.current = ws;
    ws.connect();
    return () => {
      appWsRef.current = null;
      ws.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsUrl, authEpoch]);

  // Chat's ws effect (Chat-instance). Keyed on [wsUrl, bump, authEpoch].
  const chatWsRef = useRef<GraceWs | null>(null);
  useEffect(() => {
    const ws = new GraceWs(wsUrl, makeHandlers());
    chatWsRef.current = ws;
    ws.connect();
    return () => ws.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsUrl, authEpoch]);

  return <div data-testid="harness" data-epoch={authEpoch} />;
}

let originalWS: typeof WebSocket;
beforeEach(() => {
  __test_resetSessionHub();
  clearAnonymousUserId();
  try { window.localStorage.clear(); } catch { /* ignore */ }
  FakeWebSocket.instances = [];
  authSubscriber = null;
  originalWS = globalThis.WebSocket;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).WebSocket = FakeWebSocket;
});
afterEach(() => {
  cleanup();
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).WebSocket = originalWS;
});

describe("job-0253b — re-sign-in reconnects BOTH instances (App + Chat)", () => {
  it("recovered re-sign-in opens exactly one fresh socket per instance", async () => {
    render(<ReconnectHarness wsUrl="ws://localhost:8765" />);
    // Mount: App + Chat each opened one socket.
    expect(FakeWebSocket.instances.length).toBe(2);
    const afterMount = FakeWebSocket.instances.length;

    // Both sockets get 4401'd by the gate; with no refresh token each surfaces
    // auth-expired (terminal — no reconnect scheduled). We drive both closes.
    await expireAll(FakeWebSocket.instances.slice());
    // No reconnect was scheduled by handleAuthFailure's give-up branch.
    expect(FakeWebSocket.instances.length).toBe(afterMount);

    // The user re-signs-in (fresh non-anonymous user) WHILE authExpired → App
    // bumps authEpoch → BOTH effects re-run → BOTH reconnect.
    pushAuth(GOOGLE_USER);

    // Exactly two NEW sockets (one per instance), no more.
    expect(FakeWebSocket.instances.length).toBe(afterMount + 2);
  });

  it("Chat's instance participates in the recovery (OQ-0253-CHAT-WS-4401 closed)", async () => {
    render(<ReconnectHarness wsUrl="ws://localhost:8765" />);
    await expireAll(FakeWebSocket.instances.slice());
    const before = FakeWebSocket.instances.length;
    pushAuth(GOOGLE_USER);
    // +2 means BOTH App AND Chat reconnected (if Chat were left out it'd be +1).
    expect(FakeWebSocket.instances.length).toBe(before + 2);
  });

  it("a SECOND fresh-user delivery without an intervening expiry does NOT reconnect", async () => {
    render(<ReconnectHarness wsUrl="ws://localhost:8765" />);
    const afterMount = FakeWebSocket.instances.length;
    // First recovery.
    await expireAll(FakeWebSocket.instances.slice());
    pushAuth(GOOGLE_USER);
    const afterFirstRecovery = FakeWebSocket.instances.length;
    expect(afterFirstRecovery).toBe(afterMount + 2);

    // Another onAuthChanged delivery for the SAME signed-in user, but we are
    // NOT auth-expired now → no epoch bump → no reconnect (no double-connect).
    pushAuth(GOOGLE_USER);
    expect(FakeWebSocket.instances.length).toBe(afterFirstRecovery);
  });

  it("disabled/dev mode: onAuthChanged only ever yields null → no reconnect machinery", async () => {
    render(<ReconnectHarness wsUrl="ws://localhost:8765" />);
    const afterMount = FakeWebSocket.instances.length;
    expect(afterMount).toBe(2);
    // Disabled-mode contract: onAuthChanged fires null once and never again.
    pushAuth(null);
    await settle();
    // No authExpired was ever set, no epoch bump → exactly the mount sockets.
    expect(FakeWebSocket.instances.length).toBe(afterMount);
    // Assert no auth-epoch-driven re-mount.
    const epoch = document
      .querySelector('[data-testid="harness"]')
      ?.getAttribute("data-epoch");
    expect(epoch).toBe("0");
  });
});
