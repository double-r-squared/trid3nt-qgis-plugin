// GRACE-2 web — WebSocket client with reconnect + session-resume.
//
// Talks the Appendix-A protocol against the agent service from job-0015.
// Default endpoint is the local dev agent at ws://localhost:8765.
//
// Reconnect strategy (NFR-R-2 basic for M1):
//   - On open, send `session-resume` carrying the persisted `session_id`
//     (envelope-level; payload is empty per A.3).
//   - On close, schedule a reconnect with capped exponential backoff.
//   - The session_id is generated once and persisted in localStorage so
//     reload preserves the session. M3 will use it to rebuild chat / layers /
//     pipeline from the returned `session-state`; M1 only reuses the id.
//
// State callbacks let the React layer render connection status and dispatch
// incoming frames without coupling to MapLibre or the chat panel.

import {
  AgentMessageChunkPayload,
  CancelPayload,
  CaseCommand,
  CaseCommandEnvelopePayload,
  CaseListEnvelopePayload,
  CaseOpenEnvelopePayload,
  Envelope,
  ErrorPayload,
  MapCommandPayload,
  PayloadConfirmationDecision,
  PayloadConfirmationEnvelopePayload,
  PayloadWarningEnvelopePayload,
  PipelineStatePayload,
  ProviderID,
  ResearchMode,
  SecretAddPayload,
  SecretRevokePayload,
  SecretsListPayload,
  SessionResumePayload,
  SessionStatePayload,
  UserMessagePayload,
  envelope,
  newUlid,
} from "../src/contracts";
import type { ImpactEnvelope } from "../src/components/ImpactPanel";
import type { ChartPayload } from "../src/components/ChartStack";
import type { CodeExecRequestPayload, CodeExecResultPayload } from "../src/components/SandboxCard";
import { getIdToken } from "../src/auth";
// Wire-shape mirrors for the server's source-suggestion candidate envelopes.
// Server-internal envelope_type names (`mode2-candidate`, etc.) are preserved
// on the wire; UI text never references them (translated by
// SourceSuggestionInline). job-0145 renamed the local TS module from
// mode2_suppression → source_suggestion_suppression and the type aliases;
// envelope_type literals and method names on the wire are unchanged so the
// server contract is not affected.
import {
  SourceAddConfirmedPayload as Mode2AddConfirmedPayload,
  SourceAuditEventPayload as Mode2AuditEventPayload,
  SourceCandidatePayload as Mode2CandidatePayload,
  SourceSuggestedKind as Mode2SuggestedKind,
} from "../src/lib/source_suggestion_suppression";

/**
 * TypeScript-side representation of the `auth-token` envelope payload
 * (job-0123, sprint-12-mega Wave 2). Used internally in this file;
 * NOT transmitted on the wire with these field names.
 *
 * The server's `AuthTokenEnvelope` (packages/contracts/auth.py) uses
 * different field names:
 *   - `id_token`  → wire name `token`
 *   - `provider`  → wire name `anonymous` (bool)
 *
 * `maybeSendAuthToken` translates to the server's field names before
 * serialising. This interface is kept separate so callers have a
 * documented description of each field's meaning without exposing the
 * wire mismatch to every consumer.
 *
 * H.5 names the connect-frame mechanism as a Wave 2 schema decision; we
 * implement the envelope-after-connect path because the WebSocket handshake
 * subprotocol surface is awkward for a long JWT (chrome rejects oversize
 * headers). Surfaced as OQ-0123-AUTH-TOKEN-HANDSHAKE-VS-ENVELOPE.
 */
export interface AuthTokenPayload {
  /** Firebase ID JWT (1h lifetime). Empty string triggers anonymous fallback. */
  id_token: string;
  /** Best-effort signal: "firebase" when a real token is present, "anonymous" otherwise. */
  provider: "firebase" | "anonymous";
  /**
   * job-0172 Part C — sticky anonymous user_id hint. When ``id_token`` is
   * empty (anonymous fallback) the agent consults this field; if it carries
   * a ULID matching an existing anonymous ``UserDocument``, the same User
   * is re-bound and the user's Cases stay reachable. Ignored entirely when
   * ``id_token`` verifies (the JWT is the credential).
   */
  anonymous_user_id?: string | null;
}

/**
 * Token retrieval seam — injectable so unit tests don't need a real Firebase
 * Auth subsystem. Defaults to `getIdToken()` from `./auth`.
 */
export type IdTokenGetter = () => Promise<string | null>;

export type ConnectionStatus =
  | "connecting"
  | "connected"
  | "disconnected"
  | "reconnecting";

export interface WsHandlers {
  onStatus: (s: ConnectionStatus) => void;
  onAgentChunk: (p: AgentMessageChunkPayload, caseId?: string | null) => void;
  onPipelineState: (p: PipelineStatePayload, caseId?: string | null) => void;
  onSessionState: (p: SessionStatePayload, caseId?: string | null) => void;
  onError: (p: ErrorPayload, caseId?: string | null) => void;
  // OQ-0068-MAPCMD-WS: production routing for map-command envelopes (job-0072).
  // Optional so existing callers (App.tsx, Chat.tsx) need no change; callers that
  // own a LayerPanelBus should pass `onMapCommand: (p) => bus.pushMapCommand(p)`.
  onMapCommand?: (p: MapCommandPayload) => void;
  /**
   * Per-Case secrets list (job-0125, sprint-12-mega Wave 2 — SRS §F.3).
   * Optional so existing callers don't need to change; SecretsPanel mount
   * paths wire this to push payloads into a SecretsBus subscription.
   */
  onSecretsList?: (p: SecretsListPayload) => void;
  /**
   * Tool payload-warning envelope (job-0127, sprint-12-mega Wave 2). Optional
   * so chat-only callers can ignore. Chat.tsx mounts the inline warning card
   * by subscribing here and emits the matching `tool-payload-confirmation`
   * via {@link GraceWs.sendPayloadConfirmation}.
   */
  onPayloadWarning?: (p: PayloadWarningEnvelopePayload) => void;
  /**
   * Mode 2 candidate envelope (job-0126, sprint-12-mega Wave 2). Optional so
   * existing callers (Chat.tsx) don't need to change. App.tsx wires this into
   * the Mode2OfferModal subscription bus.
   */
  onMode2Candidate?: (p: Mode2CandidatePayload) => void;
  /**
   * Case-list envelope (job-0137, sprint-12-mega Wave 3 — FR-MP-6). Optional
   * so chat-only callers can ignore. CasesPanel mount wires this to refresh
   * the left-rail list.
   */
  onCaseList?: (p: CaseListEnvelopePayload) => void;
  /**
   * Case-open envelope (job-0137, sprint-12-mega Wave 3 — FR-MP-6). Optional
   * so chat-only callers can ignore. App.tsx wires this to drive Case state
   * machine: hydrate chat + loaded_layers + map_view on open; clear cleanly
   * when session_state is null.
   */
  onCaseOpen?: (p: CaseOpenEnvelopePayload) => void;
  /**
   * Impact-envelope (Wave 4.11 P4 — SRS Appendix B.6c). Emitted by the agent
   * after ``compute_impact_envelope`` completes. App.tsx wires this to
   * ``setImpactEnvelope`` which surfaces the ImpactPanel slide-out. Optional
   * so chat-only callers need no change. The envelope is also session-scoped
   * (added to SESSION_SCOPED_TYPES) so the App.tsx GraceWs instance receives
   * it even when the tool ran on Chat.tsx's WebSocket connection.
   */
  onImpactEnvelope?: (p: ImpactEnvelope) => void;
  /**
   * Chart-emission envelope (sprint-13, job-0231 — conversational analysis layer).
   * Emitted by the agent after a chart-generation tool computes chart data and
   * builds a Vega-Lite v5 spec. App.tsx accumulates these per-session in a
   * ``charts`` state array; Case switch resets the array (replace-not-reconcile).
   * Optional so chat-only callers need no change.
   */
  onChartEmission?: (p: ChartPayload, caseId?: string | null) => void;
  /**
   * Code-exec-request envelope (sprint-13 job-0234). Emitted by the agent
   * BEFORE dispatching the sandbox so the user can approve/deny. Chat.tsx
   * renders a SandboxCard gate card and calls sendPayloadConfirmation with
   * the code_exec_id as the warning_id. Optional so existing callers need
   * no change.
   */
  onCodeExecRequest?: (p: CodeExecRequestPayload, caseId?: string | null) => void;
  /**
   * Code-exec-result envelope (sprint-13 job-0234). Emitted by the agent
   * AFTER the sandbox returns. Chat.tsx updates the matching SandboxCard
   * (keyed on code_exec_id) to RESULT state. Optional.
   */
  onCodeExecResult?: (p: CodeExecResultPayload, caseId?: string | null) => void;
  /**
   * Auth-token retriever (job-0123). Optional — when absent we fall back to
   * `getIdToken()` from `./auth` directly. Injected by tests to avoid
   * dynamic-importing Firebase.
   */
  idTokenGetter?: IdTokenGetter;
  /**
   * job-0172 Part C — auth-ack handler. Fires once per WebSocket connect
   * after the server has either verified the Firebase ID token OR fallen
   * through to the H.3 anonymous fallback. Optional so existing callers
   * don't need to opt in; ws.ts always persists the sticky anonymous
   * user_id internally regardless. Consumers (App.tsx) can use it to
   * drive auth-aware UI without a separate round-trip.
   */
  onAuthAck?: (p: AuthAckPayload) => void;
  /**
   * job-0253 (sprint-13.5) — auth-expired handler. Fires when the agent's
   * production auth gate rejects the connection: WebSocket close code 4401
   * (A.5) and/or an `error` envelope carrying `AUTH_FAILED` (A.6). When it
   * fires, ws.ts has ALREADY suppressed the reconnect loop — an invalid or
   * expired token would otherwise hammer the gate on every backoff tick.
   * One `getIdToken(forceRefresh)` retry is attempted internally first; this
   * handler fires only after that retry also fails (or there is no token to
   * refresh). App.tsx maps it to the AuthGuard's sign-in surface. Optional so
   * existing/anonymous-mode callers need no change.
   */
  onAuthExpired?: (p: ErrorPayload | null) => void;
}

// job-0253 — A.5 auth-failure close code. The agent's production gate
// (`AUTH_REQUIRED=true`) sends an `AUTH_FAILED` error envelope then closes the
// socket with this code (see services/agent/src/grace2_agent/auth.py
// `AUTH_CLOSE_CODE=4401`). A 4401 means "your credential was rejected" — NOT a
// transient network drop — so reconnecting is wrong: an invalid token would
// re-trip the gate on every backoff tick. We treat it as a terminal,
// user-actionable state (re-sign-in), not a reconnectable one.
export const AUTH_FAILED_CLOSE_CODE = 4401;

const SESSION_KEY = "grace2.session_id";
// job-0172 Part C — sticky anonymous user_id. The server's H.3 anonymous
// fallback mints a fresh ULID on every connect; without a client-side cache,
// reconnects (browser refresh, WS drop + reconnect) orphan the user's Cases
// because the new connection binds to a different user_id. We persist the
// auth-ack's user_id when ``is_anonymous=true`` and replay it on the next
// auth-token envelope as a hint; the agent re-binds the same User record.
//
// Cleared by ``clearAnonymousUserId()`` after a real sign-in lands (the
// authenticated identity takes over and the anonymous hint is moot).
const ANONYMOUS_USER_ID_KEY = "grace2.anonymous_user_id";

function loadOrCreateSessionId(): string {
  try {
    const cached = window.localStorage.getItem(SESSION_KEY);
    if (cached && cached.length === 26) return cached;
  } catch {
    // localStorage may be disabled (privacy mode)
  }
  const id = newUlid();
  try {
    window.localStorage.setItem(SESSION_KEY, id);
  } catch {
    // ignore
  }
  return id;
}

/** job-0172 Part C — read the persisted anonymous user_id hint, if any. */
export function readAnonymousUserId(): string | null {
  try {
    const v = window.localStorage.getItem(ANONYMOUS_USER_ID_KEY);
    if (v && v.length === 26) return v;
    return null;
  } catch {
    return null;
  }
}

/** job-0172 Part C — store the assigned anonymous user_id hint. */
export function writeAnonymousUserId(userId: string): void {
  try {
    if (userId && userId.length === 26) {
      window.localStorage.setItem(ANONYMOUS_USER_ID_KEY, userId);
    }
  } catch {
    // ignore
  }
}

/** job-0172 Part C — wipe the cached anonymous user_id (e.g. on sign-in). */
export function clearAnonymousUserId(): void {
  try {
    window.localStorage.removeItem(ANONYMOUS_USER_ID_KEY);
  } catch {
    // ignore
  }
}

/**
 * job-0172 Part C — Wire shape for ``auth-ack`` (server -> client).
 *
 * The agent sends this exactly once after WebSocket connect — either after
 * verifying a Firebase ID token OR after the H.3 anonymous fallback. We
 * read ``is_anonymous`` + ``user_id`` to persist the sticky anonymous
 * identity (the server mints a fresh anonymous user every connect
 * otherwise, orphaning the user's Cases on every refresh).
 *
 * The full ack shape lives in ``packages/contracts/.../auth.py``
 * (``AuthAckEnvelope``); this is the minimal subset ws.ts needs to drive
 * the persistence side-effect. Extra fields (``firebase_uid``, ``tier``)
 * are passed through to ``onAuthAck`` consumers but not used by ws.ts
 * itself.
 */
export interface AuthAckPayload {
  user_id: string;
  firebase_uid?: string | null;
  is_anonymous: boolean;
  tier?: "free" | "pro" | "enterprise";
}

// ---------------------------------------------------------------------------
// job-0159: per-session fan-out hub for envelopes that drive shared UI state.
//
// Problem this solves: the agent's `PipelineEmitter` is bound 1:1 to a single
// `ServerConnection` (see services/agent/src/grace2_agent/server.py:1180-1188).
// When the user types a message, the tool runs on the WebSocket that
// delivered the `user-message` — and the resulting `session-state`,
// `map-command`, `case-list`, `case-open`, `secrets-list`, `mode2-candidate`,
// and `tool-payload-warning` envelopes go out ONLY on that wire. But the
// web client mounts TWO `GraceWs` instances per tab — Chat.tsx (chat panel)
// and App.tsx (map + layer panel + secrets + cases) — each with its own
// connection. Pre-job-0159 the App-side instance never saw the workflow's
// session-state, so the flood-depth raster never reached MapLibre even
// though `add_loaded_layer` had fired server-side.
//
// Fix: keep one socket each, but fan out the SESSION-SCOPED envelope types
// in-process across all `GraceWs` instances that share the same
// `session_id`. Message-level envelopes (`agent-message-chunk`,
// `pipeline-state`, `error`) are NOT fanned out — those follow the
// user-message that originated them and routing them across instances
// would duplicate chat messages and pipeline cards.
//
// The hub is a passive event bus; subscribers are existing `GraceWs`
// instances. Registration is automatic in the constructor; unregistration
// is automatic in `close()`. Listeners deliver to their bound handlers
// only — there is no observer-of-observers pattern.
// ---------------------------------------------------------------------------

/** Envelope types that carry session-scoped state and therefore need fan-out. */
const SESSION_SCOPED_TYPES = new Set<string>([
  "session-state",
  "map-command",
  "case-list",
  "case-open",
  "secrets-list",
  "mode2-candidate",
  "tool-payload-warning",
  // Wave 4.11 P4: impact-envelope is session-scoped so App.tsx GraceWs sees it
  // even when the tool ran on Chat.tsx's WebSocket connection.
  "impact-envelope",
  // sprint-13: chart-emission is session-scoped so App.tsx GraceWs sees it
  // even when the chart-generation tool ran on Chat.tsx's WebSocket connection.
  "chart-emission",
  // sprint-13 job-0234: code-exec envelopes are session-scoped so Chat.tsx
  // GraceWs sees them even when the tool ran on App.tsx's connection.
  "code-exec-request",
  "code-exec-result",
]);

const SESSION_HUB: Map<string, Set<GraceWs>> = new Map();

function hubRegister(ws: GraceWs, sessionId: string): void {
  let set = SESSION_HUB.get(sessionId);
  if (!set) {
    set = new Set();
    SESSION_HUB.set(sessionId, set);
  }
  set.add(ws);
}

function hubUnregister(ws: GraceWs, sessionId: string): void {
  const set = SESSION_HUB.get(sessionId);
  if (!set) return;
  set.delete(ws);
  if (set.size === 0) SESSION_HUB.delete(sessionId);
}

function hubBroadcast(
  fromWs: GraceWs,
  sessionId: string,
  envType: string,
  payload: unknown,
  caseId: string | null = null,
): void {
  const set = SESSION_HUB.get(sessionId);
  if (!set) return;
  for (const peer of set) {
    if (peer === fromWs) continue;
    peer.deliverFannedOut(envType, payload, caseId);
  }
}

// Exposed for tests (Vitest). Production code does not call these directly.
export function __test_resetSessionHub(): void {
  SESSION_HUB.clear();
}
export function __test_sessionHubSize(sessionId: string): number {
  return SESSION_HUB.get(sessionId)?.size ?? 0;
}

export class GraceWs {
  private url: string;
  private handlers: WsHandlers;
  private socket: WebSocket | null = null;
  private sessionId: string;
  private backoffMs = 500;
  private readonly maxBackoffMs = 5000;
  private reconnectTimer: number | null = null;
  private closedByUser = false;
  // job-0253 — set true once the agent's auth gate rejects us (4401 /
  // AUTH_FAILED). While true, the close handler does NOT schedule a reconnect:
  // a rejected credential is terminal until the user re-authenticates. Cleared
  // on the next explicit `connect()` (e.g. after a fresh sign-in).
  private authFailed = false;
  // job-0253 — guard so the one-shot forceRefresh retry runs at most once per
  // connect attempt and we don't loop refresh→reject→refresh.
  private authRefreshAttempted = false;

  constructor(url: string, handlers: WsHandlers) {
    this.url = url;
    this.handlers = handlers;
    this.sessionId = loadOrCreateSessionId();
    // job-0159: register with the per-session fan-out hub so envelopes
    // received by sibling GraceWs instances (e.g. App's instance when the
    // tool ran on Chat's instance) are still delivered to OUR handlers.
    hubRegister(this, this.sessionId);
  }

  /** Current session ULID; survives page reload via localStorage. */
  get session(): string {
    return this.sessionId;
  }

  connect(): void {
    this.closedByUser = false;
    // job-0253 — a fresh connect() is the post-sign-in entry point; clear the
    // auth-failure latch so the new credential gets a clean attempt.
    this.authFailed = false;
    this.authRefreshAttempted = false;
    this.openSocket("connecting");
  }

  close(): void {
    this.closedByUser = true;
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.socket) {
      try {
        this.socket.close();
      } catch {
        // ignore
      }
      this.socket = null;
    }
    // job-0159: drop our hub registration so a re-mount doesn't leak.
    hubUnregister(this, this.sessionId);
    this.handlers.onStatus("disconnected");
  }

  /**
   * job-0159: deliver a session-scoped envelope that originated on a
   * SIBLING `GraceWs` instance for the same `session_id`. Called by the
   * fan-out hub; never invoked directly. Routes through the same handler
   * fan-out as a natively-received envelope so subscribers can't tell the
   * difference, which is the whole point.
   */
  deliverFannedOut(
    envType: string,
    payload: unknown,
    caseId: string | null = null,
  ): void {
    this.dispatchEnvelope(envType, payload, caseId);
  }

  sendUserMessage(text: string, researchMode: ResearchMode = "research"): void {
    const payload: UserMessagePayload = {
      text,
      research_mode: researchMode,
    };
    const env: Envelope<UserMessagePayload> = envelope(
      "user-message",
      this.sessionId,
      payload,
    );
    this.sendEnvelope(env);
  }

  sendCancel(reason: string | null = null): void {
    const payload: CancelPayload = { reason };
    const env: Envelope<CancelPayload> = envelope(
      "cancel",
      this.sessionId,
      payload,
    );
    this.sendEnvelope(env);
  }

  /**
   * Emit a `secret-add` envelope (job-0125 / SRS §F.3).
   *
   * Carries the transient `key_value` to the agent service; the server
   * writes the key to the vault on receipt and clears the field before
   * any logging / persistence. The web client does NOT echo or persist
   * the key value anywhere — SecretsPanel clears its form state
   * immediately after calling this method.
   */
  sendSecretAdd(args: {
    provider: ProviderID;
    case_id: string | null;
    label: string | null;
    key_value: string;
  }): void {
    const payload: SecretAddPayload = {
      envelope_type: "secret-add",
      provider: args.provider,
      case_id: args.case_id,
      label: args.label,
      key_value: args.key_value,
    };
    const env: Envelope<SecretAddPayload> = envelope(
      "secret-add",
      this.sessionId,
      payload,
    );
    this.sendEnvelope(env);
  }

  /**
   * Emit a `secret-revoke` envelope (job-0125 / SRS §F.3).
   *
   * Soft-revoke — the server flips `is_active=False` on the matching
   * SecretRecord but does NOT delete the vault entry (audit-trail
   * preservation). The response is a fresh `secrets-list` envelope.
   */
  sendSecretRevoke(secretId: string): void {
    const payload: SecretRevokePayload = {
      envelope_type: "secret-revoke",
      secret_id: secretId,
    };
    const env: Envelope<SecretRevokePayload> = envelope(
      "secret-revoke",
      this.sessionId,
      payload,
    );
    this.sendEnvelope(env);
  }

  /**
   * Emit a `tool-payload-confirmation` envelope (job-0127, sprint-12-mega Wave 2).
   *
   * Returns the user's decision on the inline payload-warning card to the
   * agent's paused dispatch coroutine. `decision="narrow_scope"` REQUIRES
   * `revisedArgs` (a dict — may be the agent's `alternative_args` echoed back
   * or a user-edited variant). `proceed` and `cancel` MUST NOT carry
   * `revisedArgs` — the contract validator on the agent side rejects them.
   */
  sendPayloadConfirmation(
    warningId: string,
    decision: PayloadConfirmationDecision,
    revisedArgs: Record<string, unknown> | null = null,
  ): void {
    const payload: PayloadConfirmationEnvelopePayload = {
      envelope_type: "tool-payload-confirmation",
      warning_id: warningId,
      decision,
      revised_args: decision === "narrow_scope" ? revisedArgs ?? {} : null,
    };
    const env: Envelope<PayloadConfirmationEnvelopePayload> = envelope(
      "tool-payload-confirmation",
      this.sessionId,
      payload,
    );
    this.sendEnvelope(env);
  }

  /**
   * Emit a `mode2-add-confirmed` envelope (job-0126, sprint-12-mega Wave 2).
   *
   * Sent when the user clicks "Add to Mode 2 catalog" on Mode2OfferModal.
   * The agent-side receiver shape is NOT YET REGISTERED in
   * packages/contracts/.../ws.py (kickoff §1 explicitly notes "define in
   * Wave 1.5 ws.py registry if not present — surface as OQ if missing");
   * tracked as OQ-0126-MODE2-ADD-CONFIRMED-SCHEMA. The payload mirrors the
   * minimal subset of `Mode2Candidate` the server needs to (a) correlate to
   * the originating audit-log entry by candidate_id and (b) hand off to the
   * heavier `offer-catalog-addition` flow (sprint-08).
   */
  sendMode2AddConfirmed(args: {
    candidate_id: string;
    url: string;
    domain: string;
    suggested_tool_kind: Mode2SuggestedKind;
  }): void {
    const payload: Mode2AddConfirmedPayload = {
      envelope_type: "mode2-add-confirmed",
      candidate_id: args.candidate_id,
      url: args.url,
      domain: args.domain,
      suggested_tool_kind: args.suggested_tool_kind,
    };
    const env: Envelope<Mode2AddConfirmedPayload> = envelope(
      "mode2-add-confirmed",
      this.sessionId,
      payload,
    );
    this.sendEnvelope(env);
  }

  /**
   * Emit a `case-command` envelope (job-0137, sprint-12-mega Wave 3 — FR-MP-6).
   *
   * Sent when the user creates / selects / renames / archives / deletes a
   * Case via CasesPanel. `case_id` is REQUIRED for every command except
   * `create` (the server generates the ULID on create). `args` is
   * command-specific:
   *
   *   - create:  optional { title: "..." } hint (defaults to "Untitled Case"
   *              server-side).
   *   - rename:  required { title: "<new title>" }.
   *   - select / archive / delete: ignored (empty {} is fine).
   *
   * The server response is `case-open` (create / select) or `case-list`
   * (rename / archive / delete) — both arrive on the existing handlers above.
   *
   * Invariant 9 (no cost theater): no cost / quota / quote field. Invariant 8
   * (cancellation): cancellation of an in-flight tool flows through the
   * existing `cancel` envelope, not a case-command.
   */
  sendCaseCommand(
    command: CaseCommand,
    caseId: string | null = null,
    args: Record<string, unknown> = {},
  ): void {
    const payload: CaseCommandEnvelopePayload = {
      envelope_type: "case-command",
      command,
      case_id: caseId,
      args,
    };
    const env: Envelope<CaseCommandEnvelopePayload> = envelope(
      "case-command",
      this.sessionId,
      payload,
    );
    this.sendEnvelope(env);
  }

  /**
   * Emit a `mode2-audit-event` envelope (job-0126, sprint-12-mega Wave 2).
   *
   * Fired on every Mode2OfferModal display + user action so the server
   * audit-log captures the full lifecycle (display-modal, display-toast,
   * add, dismiss, suppress). Server-side persistence is
   * OQ-0126-AUDIT-PERSISTENCE — the agent's default-branch
   * console.debug suffices until schema promotes it.
   */
  sendMode2AuditEvent(payload: Mode2AuditEventPayload): void {
    const full: Mode2AuditEventPayload = {
      envelope_type: "mode2-audit-event",
      ...payload,
    };
    const env: Envelope<Mode2AuditEventPayload> = envelope(
      "mode2-audit-event",
      this.sessionId,
      full,
    );
    this.sendEnvelope(env);
  }

  private openSocket(initialStatus: ConnectionStatus): void {
    this.handlers.onStatus(initialStatus);
    let ws: WebSocket;
    try {
      ws = new WebSocket(this.url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = ws;
    ws.addEventListener("open", () => {
      this.backoffMs = 500;
      this.handlers.onStatus("connected");
      // Resume the session (envelope carries the persisted id; payload empty).
      const resume: Envelope<SessionResumePayload> = envelope(
        "session-resume",
        this.sessionId,
        {} as SessionResumePayload,
      );
      this.sendEnvelope(resume);
      // Send the Firebase ID token if available (job-0123, SRS Appendix H.5).
      // If no token (Firebase disabled, signed-out, or fetch fails), we skip
      // the auth-token envelope and let the agent fall back to anonymous —
      // kickoff §4: "skip and let server fall back to anonymous."
      void this.maybeSendAuthToken();
    });
    ws.addEventListener("message", (ev) => this.handleMessage(ev.data));
    ws.addEventListener("close", (ev) => {
      this.socket = null;
      if (this.closedByUser) return;
      // job-0253 — the agent's auth gate closes with code 4401 (A.5) when the
      // credential is rejected. Do NOT reconnect: an invalid/expired token
      // would re-trip the gate on every backoff tick (a reconnect storm
      // against the gate). Instead try one fresh-token retry, then surface
      // auth-expired. `CloseEvent.code` is read defensively — some test
      // harnesses dispatch a bare Event with no `code`.
      const code = (ev as CloseEvent | undefined)?.code;
      if (code === AUTH_FAILED_CLOSE_CODE || this.authFailed) {
        void this.handleAuthFailure(null);
        return;
      }
      this.scheduleReconnect();
    });
    ws.addEventListener("error", () => {
      // close will follow; let close handler schedule the reconnect (or, on a
      // 4401, the close handler routes to handleAuthFailure instead).
    });
  }

  private handleMessage(raw: unknown): void {
    if (typeof raw !== "string") return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      return;
    }
    if (!parsed || typeof parsed !== "object") return;
    const env = parsed as { type?: unknown; payload?: unknown };
    if (typeof env.type !== "string" || typeof env.payload !== "object") return;
    const payload = env.payload as Record<string, unknown>;
    // job-0277: envelope-level Case tag (the agent's turn pin). Streaming
    // handlers route tagged envelopes to the OWNING Case's stream.
    const envCaseId =
      typeof (parsed as { case_id?: unknown }).case_id === "string"
        ? ((parsed as { case_id: string }).case_id)
        : null;
    // job-0159: fan out session-scoped envelope types to sibling GraceWs
    // instances bound to the same session_id BEFORE dispatching locally.
    // Order doesn't matter for correctness (both deliveries are synchronous
    // and independent) but fanning out first keeps the cross-instance
    // arrival close in time to the local arrival, which is friendlier to
    // any UI ordering assumptions downstream.
    if (SESSION_SCOPED_TYPES.has(env.type)) {
      hubBroadcast(this, this.sessionId, env.type, payload, envCaseId);
    }
    this.dispatchEnvelope(env.type, payload, envCaseId);
  }

  /**
   * Dispatch a parsed envelope to the bound handlers. Extracted from
   * `handleMessage` so the job-0159 hub fan-out can deliver an envelope
   * received by a sibling instance through the same routing logic.
   */
  private dispatchEnvelope(
    envType: string,
    rawPayload: unknown,
    caseId: string | null = null,
  ): void {
    if (!rawPayload || typeof rawPayload !== "object") return;
    const payload = rawPayload as Record<string, unknown>;
    switch (envType) {
      case "agent-message-chunk":
        this.handlers.onAgentChunk(payload as unknown as AgentMessageChunkPayload, caseId);
        break;
      case "pipeline-state":
        this.handlers.onPipelineState(
          payload as unknown as PipelineStatePayload,
          caseId,
        );
        break;
      case "session-state":
        this.handlers.onSessionState(
          payload as unknown as SessionStatePayload,
          caseId,
        );
        break;
      case "error": {
        const errPayload = payload as unknown as ErrorPayload;
        // job-0253 — the agent's production auth gate emits an `AUTH_FAILED`
        // error envelope IMMEDIATELY BEFORE closing the socket with 4401. Latch
        // it here so the close handler routes to handleAuthFailure (no
        // reconnect) even on harnesses where the close event lacks a `code`.
        // The latch alone does not surface auth-expired — handleAuthFailure
        // (driven by the close, or by this same branch when the socket is
        // already gone) owns the one-shot refresh + the onAuthExpired callback.
        if (errPayload && errPayload.error_code === "AUTH_FAILED") {
          this.authFailed = true;
        }
        this.handlers.onError(errPayload, caseId);
        break;
      }
      case "map-command":
        // OQ-0068-MAPCMD-WS: production routing for map-command envelopes (job-0072).
        // Callers that own a LayerPanelBus pass `onMapCommand: (p) => bus.pushMapCommand(p)`.
        if (this.handlers.onMapCommand) {
          this.handlers.onMapCommand(payload as unknown as MapCommandPayload);
        }
        break;
      case "secrets-list":
        // job-0125: server -> client secrets list (§F.3). Optional handler so
        // chat-only callers can ignore. SecretsPanel mount wires it via the
        // SecretsBus subscription.
        if (this.handlers.onSecretsList) {
          this.handlers.onSecretsList(
            payload as unknown as SecretsListPayload,
          );
        }
        break;
      case "mode2-candidate":
        // job-0126: Mode 2 candidate envelope from the Wave 1 classifier
        // (services/agent/src/grace2_agent/mode2_classifier.py). App.tsx
        // wires this into the Mode2OfferModal subscription bus when mounted.
        if (this.handlers.onMode2Candidate) {
          this.handlers.onMode2Candidate(
            payload as unknown as Mode2CandidatePayload,
          );
        }
        break;
      case "case-list":
        // job-0137: FR-MP-6 Case left-rail refresh. CasesPanel subscribes
        // through App.tsx's useCases hook. Server emits on connect and after
        // every successful case-command (create / rename / archive / delete).
        if (this.handlers.onCaseList) {
          this.handlers.onCaseList(
            payload as unknown as CaseListEnvelopePayload,
          );
        }
        break;
      case "case-open":
        // job-0137: FR-MP-6 Case rehydration. App.tsx hydrates chat history,
        // loaded_layers, and map_view from session_state; null = empty state
        // (server couldn't rehydrate — Case archived/deleted between list+select).
        if (this.handlers.onCaseOpen) {
          this.handlers.onCaseOpen(
            payload as unknown as CaseOpenEnvelopePayload,
          );
        }
        break;
      case "tool-payload-warning":
        // job-0127: Tool payload-warning envelope. Chat.tsx subscribes and
        // renders an inline PayloadWarningInline card with the proceed /
        // cancel / narrow-scope options the agent advertised. The user's
        // decision rides back via sendPayloadConfirmation().
        if (this.handlers.onPayloadWarning) {
          this.handlers.onPayloadWarning(
            payload as unknown as PayloadWarningEnvelopePayload,
          );
        }
        break;
      case "impact-envelope":
        // Wave 4.11 P4: agent emits this after compute_impact_envelope
        // completes. App.tsx wires onImpactEnvelope → setImpactEnvelope
        // which surfaces the ImpactPanel slide-out. Payload is validated
        // by presence of ``n_structures_total`` (the B.6c sentinel field);
        // malformed payloads are silently dropped to avoid crashing the
        // React tree.
        if (this.handlers.onImpactEnvelope) {
          const imp = payload as unknown as ImpactEnvelope;
          if (imp && typeof imp.n_structures_total === "number") {
            this.handlers.onImpactEnvelope(imp);
          } else {
            // eslint-disable-next-line no-console
            console.warn("[ws] impact-envelope dropped: missing n_structures_total", payload);
          }
        }
        break;
      case "chart-emission":
        // sprint-13 job-0231: chart-emission arrives after a chart-generation
        // tool runs. App.tsx wires onChartEmission → setCharts to accumulate
        // charts per session (Case switch resets). Malformed payloads (missing
        // chart_id or vega_lite_spec) are dropped with a console.warn to avoid
        // crashing the React tree.
        if (this.handlers.onChartEmission) {
          const c = payload as unknown as ChartPayload;
          if (c && typeof c.chart_id === "string" && c.vega_lite_spec && typeof c.vega_lite_spec === "object") {
            this.handlers.onChartEmission(c);
          } else {
            // eslint-disable-next-line no-console
            console.warn("[ws] chart-emission dropped: missing chart_id or vega_lite_spec", payload);
          }
        }
        break;
      case "code-exec-request":
        // sprint-13 job-0234: agent emits this before sandbox dispatch so the
        // user can approve/deny. Chat.tsx renders a SandboxCard gate card.
        // Malformed payloads (missing code_exec_id or python_code) are dropped
        // with a console.warn to avoid crashing the React tree.
        if (this.handlers.onCodeExecRequest) {
          const req = payload as unknown as CodeExecRequestPayload;
          if (req && typeof req.code_exec_id === "string" && typeof req.python_code === "string") {
            this.handlers.onCodeExecRequest(req);
          } else {
            // eslint-disable-next-line no-console
            console.warn("[ws] code-exec-request dropped: missing code_exec_id or python_code", payload);
          }
        }
        break;
      case "code-exec-result":
        // sprint-13 job-0234: agent emits this after the sandbox returns.
        // Chat.tsx updates the matching SandboxCard to RESULT state.
        // Malformed payloads (missing code_exec_id or status) are dropped.
        if (this.handlers.onCodeExecResult) {
          const res = payload as unknown as CodeExecResultPayload;
          if (res && typeof res.code_exec_id === "string" && typeof res.status === "string") {
            this.handlers.onCodeExecResult(res);
          } else {
            // eslint-disable-next-line no-console
            console.warn("[ws] code-exec-result dropped: missing code_exec_id or status", payload);
          }
        }
        break;
      case "auth-ack": {
        // job-0172 Part C — server's auth-ack confirms the resolved identity.
        // When ``is_anonymous=true`` we cache the assigned user_id so the
        // next reconnect can replay it as a hint and re-bind the same User.
        // When ``is_anonymous=false`` (real sign-in) we clear the cached
        // hint — the authenticated identity supersedes anything anonymous.
        const ack = payload as unknown as AuthAckPayload;
        if (ack && typeof ack.user_id === "string") {
          if (ack.is_anonymous === true) {
            writeAnonymousUserId(ack.user_id);
          } else {
            clearAnonymousUserId();
          }
        }
        if (this.handlers.onAuthAck) {
          this.handlers.onAuthAck(ack);
        }
        break;
      }
      default:
        // Ignores tool-call-*, location-resolved, and the pick-mode requests.
        // Logging only.
        // eslint-disable-next-line no-console
        console.debug("[ws] unhandled frame type:", envType);
    }
  }

  private sendEnvelope<P>(env: Envelope<P>): void {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return;
    this.socket.send(JSON.stringify(env));
  }

  /**
   * Fetch the Firebase ID token (if any) and emit the `auth-token` envelope.
   *
   * Job-0123 / SRS H.5: when a token is available, the agent's
   * connection-acceptor verifies it via `firebase_admin.auth.verify_id_token`
   * and binds the resolved User to the session. When no token is available
   * (Firebase disabled / signed out / fetch failed), we skip the envelope
   * entirely — the agent's anonymous fallback handles the session.
   */
  private async maybeSendAuthToken(): Promise<void> {
    const getter = this.handlers.idTokenGetter ?? getIdToken;
    let token: string | null = null;
    try {
      token = await getter();
    } catch {
      // Treat any error as no-token (anonymous fallback). The Firebase SDK
      // can throw on network errors, expired refresh tokens, etc.
      token = null;
    }
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return;
    // job-0172 Part C — always send the auth-token envelope (even with an
    // empty token) so the agent receives the sticky ``anonymous_user_id``
    // hint and re-binds the same anonymous User on reconnect. Previously
    // we returned early when ``token`` was null and relied on the agent's
    // implicit-anonymous fallback (which mints a FRESH user_id every
    // connect — the bug this part fixes).
    const stickyHint = token ? null : readAnonymousUserId();
    // Wire the payload using the server's AuthTokenEnvelope field names:
    //   ``token``     — the Firebase ID JWT (server expects ``token``, not
    //                   ``id_token``, per AuthTokenEnvelope in auth.py).
    //   ``anonymous`` — bool hint that this is an anonymous path (server
    //                   expects ``anonymous``, not ``provider``).
    // The TypeScript-side AuthTokenPayload interface uses ``id_token`` /
    // ``provider`` for readability, but those names must NOT appear on the
    // wire because AuthTokenEnvelope has extra="forbid" and would reject them
    // with AUTH_TOKEN_INVALID (the H.3 anonymous fallback still runs, but it
    // produces a spurious error envelope on every connect — OQ-env-inv fix).
    const wirePayload = {
      token: token ?? "",
      anonymous: !token,
      anonymous_user_id: stickyHint ?? undefined,
    };
    const env = envelope(
      "auth-token",
      this.sessionId,
      wirePayload,
    );
    this.sendEnvelope(env);
  }

  /**
   * job-0253 — handle an auth-gate rejection (4401 / AUTH_FAILED) WITHOUT
   * entering the reconnect loop.
   *
   * Sequence:
   *   1. Latch `authFailed` so any stray close events don't reconnect.
   *   2. Cancel any pending reconnect timer (defence in depth).
   *   3. ONE-SHOT: force-refresh the Firebase ID token. A token can be rejected
   *      simply because it expired (1h lifetime) while a still-valid Firebase
   *      session can mint a fresh one. If a NEW (non-empty) token comes back,
   *      reconnect exactly once with it — the kickoff's "one forceRefresh retry
   *      is acceptable before giving up". The retry guard (`authRefreshAttempted`)
   *      ensures we never loop refresh→reject→refresh.
   *   4. If there is no fresh token (Firebase disabled, signed out, or refresh
   *      failed), surface `auth-expired`: emit `disconnected` status and call
   *      `onAuthExpired`. App.tsx maps that to the sign-in surface.
   */
  private async handleAuthFailure(err: ErrorPayload | null): Promise<void> {
    this.authFailed = true;
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }

    if (!this.authRefreshAttempted) {
      this.authRefreshAttempted = true;
      const getter = this.handlers.idTokenGetter ?? ((): Promise<string | null> => getIdToken(true));
      let fresh: string | null = null;
      try {
        // When a custom getter is injected (tests / non-default), call it with
        // no args; the default getter above already forces a refresh.
        fresh = await getter();
      } catch {
        fresh = null;
      }
      if (fresh) {
        // A fresh credential — give the gate exactly one more chance. We clear
        // the failure latch and re-open; `authRefreshAttempted` stays true so a
        // second rejection falls straight through to the surface step below.
        this.authFailed = false;
        this.handlers.onStatus("reconnecting");
        this.openSocket("connecting");
        return;
      }
    }

    // No fresh token (or the refreshed token was also rejected): give up the
    // socket and hand control to the UI for re-authentication.
    this.handlers.onStatus("disconnected");
    if (this.handlers.onAuthExpired) {
      this.handlers.onAuthExpired(err);
    }
  }

  private scheduleReconnect(): void {
    this.handlers.onStatus("reconnecting");
    const delay = this.backoffMs;
    this.backoffMs = Math.min(this.backoffMs * 2, this.maxBackoffMs);
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.openSocket("connecting");
    }, delay);
  }
}
