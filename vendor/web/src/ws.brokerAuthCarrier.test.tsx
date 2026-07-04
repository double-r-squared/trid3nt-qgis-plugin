// GRACE-2 web — pre-upgrade Cognito token carrier for the per-session broker.
//
// The FUTURE per-session broker must verify auth at the WSS UPGRADE handshake,
// BEFORE the WebSocket is established — it cannot read the in-band `auth-token`
// envelope (that frame arrives after the upgrade completes). ws.ts therefore
// carries the Cognito ID token on the `?st=<idToken>` QUERY PARAM, the same
// surface the broker's `_extract_identity` parses
// (infra/aws-agent-isolation/broker/app.py — `qs["st"][0]`).
//
// Why the query param and NOT the WebSocket subprotocol: a ~1KB Cognito JWT in
// the `Sec-WebSocket-Protocol` header is Chrome-incompatible — Chromium drops
// the oversize header and the WS through the broker dies ~90ms after open and
// reconnect-storms (PROVEN live 2026-06-29). The accepted cost of `?st` is that
// the (short-lived, TLS-protected) token lands in CloudFront/ALB access logs.
//
// This suite proves:
//   1. With a token available, the `?st=` query param carries it on connect
//      (URL-encoded), the `?sid` routing key is kept, and NO subprotocol is
//      offered (byte-identical 2nd-arg-less `new WebSocket(url)`).
//   2. The token is re-read FRESH on each (re)connect (a refreshed token is
//      carried on reconnect, never memoised at construction).
//   3. With NO token (anonymous / signed-out / disabled), NO `&st=` is appended
//      and NO subprotocol is offered — `?sid` still rides; the construct is
//      byte-identical to the pre-change single box (the non-breaking guarantee).
//   4. The in-band handshake is UNCHANGED: `auth-token` is still the first frame,
//      still precedes `session-resume`, and still carries the same wire shape.
//      The pre-upgrade carrier is purely additive.
//
// happy-dom does NOT track WebSocket instances, so this suite installs its own
// deterministic fake WebSocket that records the constructor args (url +
// protocols) and every send(), and lets the test drive open/close events.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  GraceWs,
  __test_resetSessionHub,
  clearAnonymousUserId,
  type WsHandlers,
} from "./ws";

// ── Deterministic fake WebSocket (captures url + protocols) ────────────── //

class FakeWebSocket {
  static OPEN = 1;
  static CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  readonly OPEN = 1;
  readonly CLOSED = 3;
  url: string;
  /** The 2nd constructor arg — string | string[] | undefined, captured verbatim. */
  protocolsArg: string | string[] | undefined;
  readyState = 0; // CONNECTING
  sent: string[] = [];
  private listeners: Record<string, ((ev: unknown) => void)[]> = {};

  constructor(url: string, protocols?: string | string[]) {
    this.url = url;
    this.protocolsArg = protocols;
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

  fireClose(code: number): void {
    this.readyState = this.CLOSED;
    this.dispatchEvent({ type: "close", code } as { type: string });
  }

  /** The 2nd constructor arg as an array (normalises string | string[] | undefined). */
  protocolList(): string[] {
    if (this.protocolsArg == null) return [];
    return Array.isArray(this.protocolsArg)
      ? this.protocolsArg
      : [this.protocolsArg];
  }

  /** The decoded value of the `?st=` query param, or null if absent. */
  stParam(): string | null {
    const q = this.url.indexOf("?");
    if (q === -1) return null;
    const params = new URLSearchParams(this.url.slice(q + 1));
    return params.get("st");
  }

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

// ── 1: token carried on the ?st= query param, no subprotocol ───────────── //

describe("pre-upgrade carrier — token rides the ?st= query param", () => {
  it("carries ?st=<token> on connect, keeps ?sid, offers NO subprotocol", () => {
    const token = "eyJraWQiOiJhYmM.eyJzdWIiOiJ1MSJ9.sig-Abc_123-Def";
    const idTokenSyncGetter = vi.fn<() => string | null>().mockReturnValue(token);
    const ws = new GraceWs(
      "wss://example.test/ws",
      makeHandlers({ idTokenSyncGetter }),
    );
    ws.connect();

    const sock = lastSocket();
    expect(sock).toBeDefined();
    // The sync getter was consulted to build the carrier at dial time.
    expect(idTokenSyncGetter).toHaveBeenCalled();

    // The token rides the `?st=` query param (URL-encoded; decodes back to the
    // raw JWT, which is what the broker's parse_qs `qs["st"][0]` recovers).
    expect(sock.stParam()).toBe(token);

    // ?sid is kept exactly as-is (unchanged from the prior commit).
    expect(sock.url).toContain("sid=");

    // NO subprotocol is offered — the oversize subprotocol header is exactly what
    // Chromium drops. The 2nd constructor arg is absent.
    expect(sock.protocolsArg).toBeUndefined();
    expect(sock.protocolList()).toEqual([]);

    ws.close();
  });

  it("re-reads the token FRESH on each (re)connect — a refreshed token is carried", () => {
    let current = "token.one.aaa";
    const idTokenSyncGetter = vi
      .fn<() => string | null>()
      .mockImplementation(() => current);
    const ws = new GraceWs(
      "wss://example.test/ws",
      makeHandlers({ idTokenSyncGetter }),
    );

    ws.connect();
    expect(lastSocket().stParam()).toBe("token.one.aaa");

    // The token is refreshed between connects; the next dial must carry the NEW
    // one (proves the read is not memoised at construction time).
    current = "token.two.bbb";
    const first = lastSocket();
    first.fireClose(1006); // transport drop → close handler schedules reconnect
    // Drive the reconnect directly (avoids depending on backoff timing).
    ws.connect();

    const second = lastSocket();
    expect(second).not.toBe(first);
    expect(second.stParam()).toBe("token.two.bbb");

    ws.close();
  });
});

// ── 2: non-breaking — no token ⇒ no ?st, no subprotocol ────────────────── //

describe("pre-upgrade carrier — non-breaking on the single box", () => {
  it("appends NO ?st and NO subprotocol when there is no token (byte-identical construct)", () => {
    const idTokenSyncGetter = vi.fn<() => string | null>().mockReturnValue(null);
    const ws = new GraceWs(
      "wss://example.test/ws",
      makeHandlers({ idTokenSyncGetter }),
    );
    ws.connect();

    const sock = lastSocket();
    // No `?st` carrier when there is no token.
    expect(sock.stParam()).toBeNull();
    expect(sock.url).not.toContain("st=");
    // No 2nd constructor arg at all → identical to the pre-change `new
    // WebSocket(url)`; the current single box sees no unknown subprotocol.
    expect(sock.protocolsArg).toBeUndefined();
    expect(sock.protocolList()).toEqual([]);
    // ?sid is still appended (unchanged).
    expect(sock.url).toContain("sid=");

    ws.close();
  });

  it("never throws while building the carrier (degrades, never breaks the dial)", () => {
    // Any token value is URL-encoded into `?st=`, so even an awkward value can
    // never throw in the constructor (unlike the old subprotocol carrier).
    const idTokenSyncGetter = vi
      .fn<() => string | null>()
      .mockReturnValue("token with spaces & symbols");
    const ws = new GraceWs(
      "wss://example.test/ws",
      makeHandlers({ idTokenSyncGetter }),
    );
    expect(() => ws.connect()).not.toThrow();

    const sock = lastSocket();
    // URL-encoded into the query, decodes back to the original value, no subprotocol.
    expect(sock.stParam()).toBe("token with spaces & symbols");
    expect(sock.protocolsArg).toBeUndefined();

    ws.close();
  });
});

// ── 3: the in-band handshake is UNCHANGED ──────────────────────────────── //

describe("pre-upgrade carrier — in-band auth-token handshake unchanged", () => {
  it("auth-token is still first and precedes session-resume, with the same wire shape", async () => {
    const token = "real.jwt.token";
    // The async in-band getter (auth-token frame) AND the sync carrier getter
    // resolve to the same token — the carrier is purely additive.
    const idTokenGetter = vi
      .fn<() => Promise<string | null>>()
      .mockResolvedValue(token);
    const idTokenSyncGetter = vi
      .fn<() => string | null>()
      .mockReturnValue(token);
    const ws = new GraceWs(
      "wss://example.test/ws",
      makeHandlers({ idTokenGetter, idTokenSyncGetter }),
    );
    ws.connect();
    const sock = lastSocket();

    sock.fireOpen();
    // Let maybeSendAuthToken (await getter()) settle, then the chained resume.
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));

    const types = sock.sentTypes();
    // The carrier did NOT change the in-band ordering / contents.
    expect(types[0]).toBe("auth-token");
    const authIdx = types.indexOf("auth-token");
    const resumeIdx = types.indexOf("session-resume");
    expect(authIdx).toBeLessThan(resumeIdx);

    const authFrame = JSON.parse(sock.sent[authIdx]!);
    expect(authFrame.payload.token).toBe(token);
    expect(authFrame.payload.anonymous).toBe(false);

    ws.close();
  });
});
