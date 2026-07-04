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
  CredentialProvidedPayload,
  CredentialRequestPayload,
  Envelope,
  ErrorPayload,
  LayerDeletePayload,
  MapCommandPayload,
  PayloadConfirmationDecision,
  PayloadConfirmationEnvelopePayload,
  PayloadWarningEnvelopePayload,
  PipelineStatePayload,
  ProviderID,
  RegionBBox,
  RegionChoiceProvidedPayload,
  RegionChoiceRequestPayload,
  ResearchMode,
  SecretAddPayload,
  SecretRevokePayload,
  SecretsListPayload,
  SessionResumePayload,
  SessionStatePayload,
  SolveProgressPayload,
  SpatialDrawFeatureCollection,
  SpatialInputRequestPayload,
  SpatialInputResponsePayload,
  ToolIoPayload,
  TurnCompletePayload,
  UserMessagePayload,
  envelope,
  newUlid,
} from "./contracts";
import type { ImpactEnvelope } from "./components/ImpactPanel";
import type { ChartPayload } from "./components/ChartStack";
import type { CodeExecRequestPayload, CodeExecResultPayload } from "./components/SandboxCard";
import { getIdToken, getIdTokenSync } from "./auth";
import { AgentWaker, WakeState } from "./lib/wake";
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
} from "./lib/source_suggestion_suppression";

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

/**
 * Synchronous token-read seam for the pre-upgrade broker-auth carrier — the WS
 * subprotocol must be supplied to the `WebSocket` constructor, which is a
 * synchronous call, so the token has to be read WITHOUT awaiting. Injectable so
 * unit tests can supply a token without a real Cognito subsystem. Defaults to
 * `getIdTokenSync()` from `./auth`, which reads the SAME cache the async
 * `getIdToken()` populates (no new fetch).
 */
export type IdTokenSyncGetter = () => string | null;

export type ConnectionStatus =
  | "connecting"
  | "connected"
  | "disconnected"
  | "reconnecting";

export interface WsHandlers {
  onStatus: (s: ConnectionStatus) => void;
  onAgentChunk: (p: AgentMessageChunkPayload, caseId?: string | null) => void;
  onPipelineState: (p: PipelineStatePayload, caseId?: string | null) => void;
  /**
   * Session-state frame. `fannedOut` is TRUE when this frame did NOT arrive on
   * THIS GraceWs instance's own socket but was DELIVERED by the per-session
   * fan-out hub from a SIBLING instance (Item 1, NATE 2026-06-22  -  roads-flash
   * eviction fix). A hub-fanned session-state is built from the SIBLING socket's
   * emitter, which can be STALE relative to this instance's view (e.g. the App
   * socket's keepalive resume reply carries the flood raster but NOT a roads
   * vector the Chat socket just added), so the App-side handler must treat a
   * fanned-out frame as ADDITIVE-ONLY (it may add layers, never evict) and never
   * stamp it authoritative. Only this socket's OWN frame (`fannedOut === false`)
   * or an explicit Case switch may authoritatively replace.
   */
  onSessionState: (
    p: SessionStatePayload,
    caseId?: string | null,
    fannedOut?: boolean,
  ) => void;
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
   * Just-in-time credential request (§F.3 amendment). Emitted when a keyed
   * tool dispatch needs a missing/invalid credential. Optional so chat-only
   * callers can ignore; the credential-prompt surface subscribes here, runs
   * the existing `secret-add` path to save the key, then signals retry via
   * {@link GraceWs.sendCredentialProvided}. Behaviour is out of scope for the
   * contract-only landing — this is the type seam both sides compile against.
   */
  onCredentialRequest?: (p: CredentialRequestPayload) => void;
  /**
   * Region-disambiguation request (state-bbox-fallback narrowing). Emitted when
   * a `geocode_location` result snapped to a whole-state bbox and the agent is
   * offering a narrower county pick. Optional so chat-only callers can ignore.
   * Chat.tsx mounts the inline RegionPickerCard (+ publishes the request to the
   * region-choice bus so Map.tsx paints the synced county choropleth) by
   * subscribing here; the user's pick rides back via
   * {@link GraceWs.sendRegionChoiceProvided}. region-choice-request is
   * session-scoped (SESSION_SCOPED_TYPES) so it fans out to Chat's GraceWs even
   * when the paused geocode tool ran on App.tsx's connection — mirrors the
   * credential-request rationale exactly.
   */
  onRegionChoiceRequest?: (p: RegionChoiceRequestPayload) => void;
  /**
   * Spatial-input request (FR-WC-13 pick-mode + FR-WC-16 urban vector-draw).
   * Emitted when the agent needs the user to pick a point/bbox or DRAW geometry
   * (AOIs + tagged barrier walls / flap gates) for the urban-flood engine. The
   * agent PAUSES the turn awaiting the reply (mirrors the region-choice /
   * credential pause/resume seam). Map.tsx enters pick-mode (point/bbox) or
   * opens the terra-draw surface (vector_draw); Chat.tsx renders the inline
   * prompt card. The drawn / picked result rides back via
   * {@link GraceWs.sendSpatialInputResponse}. `spatial-input-request` is
   * session-scoped (SESSION_SCOPED_TYPES) so it fans out to Chat's GraceWs even
   * when the paused tool ran on App.tsx's connection — mirrors the
   * region-choice / credential rationale exactly. Optional so chat-only callers
   * can ignore.
   */
  onSpatialInputRequest?: (p: SpatialInputRequestPayload) => void;
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
   * Live big-sim solve-progress envelope (NATE 2026-06-17). Emitted by the
   * agent while a heavy-compute solver (SFINCS / MODFLOW / Pelicun on the
   * external per-job execution substrate) burns wall-clock so the running
   * tool / pipeline card can surface a live readout. `solve-progress` is
   * session-scoped (SESSION_SCOPED_TYPES) so Chat's GraceWs receives it via
   * the fan-out hub even when the solver step ran on App.tsx's connection.
   * Malformed payloads (missing run_id) are dropped. Optional so chat-only
   * callers can ignore.
   */
  onSolveProgress?: (p: SolveProgressPayload, caseId?: string | null) => void;
  /**
   * Tool-IO sidecar (tool-card-expand-output spec). Emitted by the agent right
   * after a tool dispatch with the RAW input args + RAW function_response,
   * keyed by the dispatch's pipeline step_id. Chat.tsx stores it per step_id in
   * the owning stream so the matching tool card's expander reveals it. Follows
   * the SAME wire as the dispatch's `pipeline-state` (message-scoped, not in
   * SESSION_SCOPED_TYPES) so it lands on the right stream naturally. Malformed
   * payloads (missing step_id) are dropped. Optional so chat-only callers can
   * ignore.
   */
  onToolIo?: (p: ToolIoPayload, caseId?: string | null) => void;
  /**
   * Turn-complete / idle signal (C2 terminal-state durability). Emitted by the
   * agent at the END of every turn (and re-emitted on session-resume). Chat.tsx
   * subscribes to force-complete any tool card still rendering `running` when
   * the turn ends — the terminal `pipeline-state` frame can be LOST on a socket
   * drop, leaving a card spinning forever. `turn-complete` is session-scoped
   * (SESSION_SCOPED_TYPES) so it fans out to Chat's GraceWs even when the turn's
   * tools ran on App.tsx's connection — mirrors the solve-progress rationale.
   * Malformed payloads never crash (no required field). Optional so chat-only /
   * older callers can ignore.
   */
  onTurnComplete?: (p: TurnCompletePayload, caseId?: string | null) => void;
  /**
   * Auth-token retriever (job-0123). Optional — when absent we fall back to
   * `getIdToken()` from `./auth` directly. Injected by tests to avoid
   * dynamic-importing Firebase.
   */
  idTokenGetter?: IdTokenGetter;
  /**
   * Pre-upgrade broker-auth carrier — SYNCHRONOUS id-token reader used to put
   * the token on the WebSocket subprotocol at dial time (the constructor is
   * synchronous; we cannot await here). Optional — when absent we fall back to
   * `getIdTokenSync()` from `./auth`, which reads the SAME token cache the async
   * `idTokenGetter` / `getIdToken()` use. Injected by tests to supply a token
   * deterministically. Returning null (anonymous / signed-out / disabled) means
   * NO subprotocol is offered, so the connect is byte-identical to the
   * pre-change single-box behaviour.
   */
  idTokenSyncGetter?: IdTokenSyncGetter;
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
  /**
   * Wake-on-reconnect hook (auto-stop/wake infra, NATE 2026-06-17). Fires when
   * the reconnect loop schedules a retry against a socket that won't open —
   * the agent box may have been STOPPED by the idle-check Lambda. The web
   * fires a single (debounced) POST to the wake endpoint to ask the wake
   * Lambda to StartInstances, then the existing backoff reconnect re-dials.
   *
   * This handler is the SIGNAL to the UI ("the box looks down, we're waking
   * it") — App.tsx maps it to the "Wake up agent" overlay's auto-waking state.
   * The actual wake POST is issued by ws.ts via its injected {@link AgentWaker}
   * (see `waker` below) regardless of whether this handler is wired, so wake
   * works even for callers that don't render the overlay. Optional.
   *
   * `attempt` is the count of consecutive failed reconnect schedules since the
   * last successful open — the UI uses it to avoid flashing the overlay on a
   * single transient blip (show only after a couple of failed attempts).
   */
  onWakeNeeded?: (attempt: number) => void;
  /**
   * Session-durability Job D (2) - RECONNECT-RESUMED hook. Fires once after a
   * successful (re)open has sent its `auth-token` + `session-resume` handshake
   * (i.e. the socket is live again and the server has been asked to re-emit the
   * authoritative `session-state`).
   *
   * Why it exists: the composer-stuck-as-Stop bug latches when a turn completes
   * server-side but the completion/close frame is lost on the dropped socket.
   * The client's in-flight latch (`currentPipelineFromSession` / a running
   * pipeline step) is then never cleared, so the send button renders Stop
   * forever. The server WILL re-emit a fresh `session-state` (with
   * `current_pipeline === null` if the turn is over) on the resume below, and
   * that already settles the latch via `routeSessionState`. But that re-emitted
   * `session-state`/`turn-complete` is tagged with the turn's OWNING case_id;
   * if the user has since navigated, it can settle the WRONG (non-visible)
   * stream and leave the visible composer stuck. This hook lets the consumer
   * (Chat.tsx) belt-and-suspenders force-settle the VISIBLE / targetKey stream
   * on resume so the composer cannot stay stuck on a successful reconnect,
   * independent of which case the server's re-emitted clear is tagged with.
   *
   * `firstOpen` is true on the very first connect of this instance and false on
   * every subsequent reconnect; consumers may use it to skip the (harmless,
   * idempotent) clear on the initial connect where there is nothing to settle.
   * Optional so existing callers need no change. A handler throw is swallowed so
   * it can never wedge the open handler.
   */
  onReconnectResumed?: (firstOpen: boolean) => void;
}

// job-0253 — A.5 auth-failure close code. The agent's production gate
// (`AUTH_REQUIRED=true`) sends an `AUTH_FAILED` error envelope then closes the
// socket with this code (see services/agent/src/grace2_agent/auth.py
// `AUTH_CLOSE_CODE=4401`). A 4401 means "your credential was rejected" — NOT a
// transient network drop — so reconnecting is wrong: an invalid token would
// re-trip the gate on every backoff tick. We treat it as a terminal,
// user-actionable state (re-sign-in), not a reconnectable one.
export const AUTH_FAILED_CLOSE_CODE = 4401;

// BUG 4a (Wave 4.9) — application-level keepalive. The agent WS was observed
// dropping + reconnecting every ~10-45s ("no close frame received or sent"
// server-side) because an IDLE socket behind CloudFront (d125yfbyjrpbre
// .cloudfront.net → EC2) is silently culled by the proxy's idle timeout. The
// browser `WebSocket` API cannot send protocol-level ping frames, and the agent
// server has NO custom `{type:"ping"}` handler — its dispatch loop replies to an
// unknown message type with an `INTERNAL_ERROR` error envelope (server.py
// `else: _send_error(... "unknown message type")`), which would spam the chat
// error path and force-fail pipeline cards. So the keepalive ping is a
// `session-resume` envelope: the server DOES handle it (re-emits an
// authoritative `session-state`), it is idempotent (replace-not-reconcile makes
// a repeat snapshot a no-op when the layer sets match — Appendix A.7), and the
// `session-state` reply is a real proof-of-life we can treat as the "pong".
//
// Liveness model: every inbound frame (the resume reply OR ordinary traffic)
// counts as activity and clears the pending pong deadline. We send a ping every
// KEEPALIVE_INTERVAL_MS while OPEN; if NO inbound activity arrives within
// KEEPALIVE_PONG_TIMEOUT_MS of a ping, the socket is treated as dead and force-
// reconnected (this also covers the iOS zombie-socket case where readyState
// stays OPEN on a dead connection). Timers are torn down on close/teardown.
export const KEEPALIVE_INTERVAL_MS = 25_000;
export const KEEPALIVE_PONG_TIMEOUT_MS = 10_000;

// Mobile connect-attempt timeout (transport surface). When the agent box has
// been STOPPED by the idle-check Lambda the new WebSocket sits in CONNECTING
// while the browser waits out its default TCP connect timeout (30-120s) before
// firing `error`/`close`. That delays the wake overlay by up to two minutes on
// mobile. We arm a one-shot timer the instant we create the socket; if it is
// still CONNECTING (never reached OPEN) when the timer fires, we tear it down
// via `ws.close()` so the EXISTING close handler runs `scheduleReconnect` ->
// `onWakeNeeded`, surfacing the wake overlay in ~10s instead.
//
// This is a CONNECT-PHASE timer ONLY: it is armed in `openSocket` right after
// the socket is created and CLEARED the instant the open handler fires (and in
// the close handler). It NEVER interacts with the keepalive ping / pong timers
// (those run only while OPEN) so it cannot regress the no-10s-cycling reconnect
// contract - by the time the keepalive is live this timer is already cleared.
export const CONNECT_ATTEMPT_TIMEOUT_MS = 10_000;

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

/**
 * Per-user-agent-isolation (NATE 2026-06-22) — carry the stable per-session id
 * as a `?sid=<id>` query param on the wss connect URL so the FUTURE per-session
 * broker can read it PRE-UPGRADE (at the HTTP upgrade handshake, before the
 * WebSocket is established) and pick / provision the right per-session Fargate
 * task to route the connection to.
 *
 * Why a query param (not the existing `auth-token` / `session-resume`
 * envelopes): those are application-level frames the agent reads AFTER the
 * upgrade completes — far too late for a broker that must choose the upstream
 * task BEFORE proxying the upgrade. The query string is the one piece of
 * routing data available to the broker at the `GET ...?sid=... Upgrade:
 * websocket` request line.
 *
 * Non-breaking TODAY: the CURRENT single-box agent never inspects the request
 * query string — an unknown `?sid` is simply ignored by the WebSocket handler,
 * so the connection behaves identically. The id is the SAME stable session
 * ULID already minted by {@link loadOrCreateSessionId} (the value sent in the
 * `session-resume` envelope); we reuse it rather than inventing a parallel id
 * so the broker's pre-upgrade routing key and the agent's post-upgrade session
 * binding agree.
 */
function withSessionQueryParam(
  url: string,
  sessionId: string,
  idToken?: string | null,
): string {
  if (!sessionId && !idToken) return url;
  // Preserve any pre-existing query string / fragment. URLs today never carry
  // one (ws://host:8765, wss://base/ws), but append robustly so a future URL
  // shape can't silently drop the sid / st.
  const hashIdx = url.indexOf("#");
  const base = hashIdx === -1 ? url : url.slice(0, hashIdx);
  const frag = hashIdx === -1 ? "" : url.slice(hashIdx);
  const parts: string[] = [];
  if (sessionId) parts.push(`sid=${encodeURIComponent(sessionId)}`);
  // `?st=<idToken>` — the pre-upgrade auth carrier (see openSocket). URL-encode
  // it: a JWT is already URL-safe but encoding is defensive against any future
  // token shape. The broker reads `qs["st"][0]` (parse_qs URL-decodes for us).
  if (idToken) parts.push(`st=${encodeURIComponent(idToken)}`);
  if (parts.length === 0) return `${base}${frag}`;
  const sep = base.includes("?") ? "&" : "?";
  return `${base}${sep}${parts.join("&")}${frag}`;
}

/**
 * Per-user-agent-isolation (NATE 2026-06-22) — carry the Cognito ID token to the
 * FUTURE per-session broker so it can verify auth PRE-UPGRADE (at the HTTP
 * upgrade handshake, BEFORE the WebSocket is established) and route the
 * connection to the right per-session task. The broker CANNOT read the in-band
 * `auth-token` envelope — that frame arrives only AFTER the upgrade completes,
 * far too late for a routing decision — so the token must ride the handshake
 * itself, alongside the `?sid` routing key already appended by
 * {@link withSessionQueryParam}.
 *
 * Carrier = the `?st=<idToken>` QUERY PARAM, NOT the WebSocket SUBPROTOCOL
 * (`Sec-WebSocket-Protocol`). A ~1KB Cognito JWT in the subprotocol header is
 * CHROME-INCOMPATIBLE: Chromium rejects/drops the oversize `Sec-WebSocket-Protocol`
 * value, so the browser WS through the broker dies ~90ms after open and
 * reconnect-storms (PROVEN live 2026-06-29; broker `app.py:23` + the Python
 * canary, which worked ONLY because it used `?st`). The broker
 * (`infra/aws-agent-isolation/broker/app.py` `_extract_identity`) reads the token
 * from EITHER `?st=<token>` (query) or the `base64UrlBearerAuthorization.<token>`
 * subprotocol; the query path is the one browsers honour, so that is what we dial.
 *
 * TRADE-OFF: `?st` lands the token in CloudFront / ALB access logs (the
 * subprotocol header would not be). That is the ACCEPTED cost of browser
 * compatibility — the token is short-lived and the connection is TLS-protected
 * (wss), and the subprotocol carrier is Chrome-incompatible for a JWT-length
 * value, so it is not a usable alternative for the browser.
 *
 * NON-BREAKING on the CURRENT single box: the agent's WebSocket handler never
 * inspects the request query string, so an unknown `?st` (like the existing
 * `?sid`) is simply ignored and the connection behaves identically; the box
 * reads its token from the in-band `auth-token` message. No subprotocol is
 * offered at all, so the `new WebSocket(url)` construct is byte-identical to the
 * pre-change call. When there is no token (anonymous / signed-out / Cognito
 * disabled) no `&st=` is appended, so only the pre-existing `?sid` rides.
 */
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

/**
 * "Cases vanish on refresh" durable fix - mint a STABLE client-owned anonymous
 * user_id on FIRST load and persist it (a sibling of loadOrCreateSessionId).
 *
 * Root cause this addresses: the tab opens TWO GraceWs sockets (App + Chat),
 * each running an independent auth handshake. With no Cognito token and no
 * pre-existing stored anon hint, EACH connection used to make the server mint a
 * DIFFERENT anon ULID; the last server-minted id won the localStorage write
 * (last-write-wins) but Cases got created under whichever socket carried the
 * user message. On refresh the case-list was scoped to one id while the cases
 * were owned by the other -> empty rail.
 *
 * By minting the anon id on the CLIENT before either socket connects, both
 * sockets present the SAME anonymous_user_id from frame one and every refresh
 * reuses it. The server re-binds the same anonymous User -> cases stay
 * owner-scoped. We reuse newUlid() (the same generator that mints session_id)
 * so the value is a valid 26-char ULID the contracts package accepts.
 */
export function loadOrCreateAnonId(): string {
  try {
    const cached = window.localStorage.getItem(ANONYMOUS_USER_ID_KEY);
    if (cached && cached.length === 26) return cached;
  } catch {
    // localStorage may be disabled (privacy mode)
  }
  const id = newUlid();
  try {
    window.localStorage.setItem(ANONYMOUS_USER_ID_KEY, id);
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

/**
 * job-0172 Part C — store the anonymous user_id hint.
 *
 * "Cases vanish on refresh" fix - EARLIEST-WINS: never overwrite a non-empty
 * stored id. The client now mints+persists a stable anon id at first load
 * (loadOrCreateAnonId), so a later server-minted auth-ack must NEVER clobber
 * the client-owned id (which would re-introduce the id divergence between the
 * two sockets). Only the first writer (loadOrCreateAnonId, before any socket
 * connects) establishes the id; subsequent writes are no-ops unless the slot
 * is empty (e.g. localStorage was cleared mid-session).
 */
export function writeAnonymousUserId(userId: string): void {
  try {
    if (!userId || userId.length !== 26) return;
    const existing = window.localStorage.getItem(ANONYMOUS_USER_ID_KEY);
    if (existing && existing.length === 26) {
      // earliest-wins: a valid id is already present; do not clobber it.
      return;
    }
    window.localStorage.setItem(ANONYMOUS_USER_ID_KEY, userId);
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
  // credential-request is session-scoped: a keyed tool may dispatch on
  // Chat.tsx's socket but the credential prompt + SecretsPanel form live on
  // App.tsx's connection. Fan it out so the prompt reaches the form regardless
  // of which socket the paused tool ran on. Mirrors the secrets-list rationale.
  "credential-request",
  // region-choice-request is session-scoped for the SAME reason as
  // credential-request: the geocode tool may dispatch on Chat.tsx's socket but
  // the picker card + the map choropleth live across both Chat + App
  // connections. Fan it out so the prompt reaches the picker regardless of
  // which socket the paused geocode ran on.
  "region-choice-request",
  // spatial-input-request is session-scoped for the SAME reason as
  // region-choice-request: the paused tool (e.g. the urban-flood flow) may
  // dispatch on Chat.tsx's socket but the pick-mode UI / terra-draw surface +
  // the inline prompt card live across both Chat + App connections. Fan it out
  // so the prompt reaches the draw surface regardless of which socket the
  // paused tool ran on (FR-WC-13 / FR-WC-16).
  "spatial-input-request",
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
  // NATE 2026-06-17: live big-sim solve-progress is session-scoped so Chat.tsx
  // GraceWs sees it even when the solver step ran on App.tsx's connection —
  // the running tool card lives in Chat's stream. Mirrors pipeline-state's
  // rationale but pipeline-state is message-scoped (follows its user-message);
  // solve-progress fans out because the card it enriches can be on either wire.
  "solve-progress",
  // C2 terminal-state durability: turn-complete is session-scoped so Chat's
  // GraceWs sees the end-of-turn signal even when the turn's tools ran on
  // App.tsx's connection — Chat owns the tool cards that must be force-
  // completed when the turn ends. Mirrors solve-progress's rationale exactly.
  "turn-complete",
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
  // BUG 1b — reconnect-backoff FLOOR. Raised from 500ms to 1500ms so the FIRST
  // reconnect after a transport drop waits longer; a burst of transport-level
  // drops (the reconnect "storm") no longer hammers the agent at ~500ms cadence.
  // The doubling and the 5000ms ceiling are unchanged.
  private static readonly RECONNECT_FLOOR_MS = 1500;
  private backoffMs = GraceWs.RECONNECT_FLOOR_MS;
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
  // Session-durability Job D (2) - true until the FIRST successful open of this
  // instance completes its resume handshake; flips false thereafter. Passed to
  // ``onReconnectResumed`` so the consumer can distinguish the initial connect
  // (nothing to settle) from a genuine reconnect (where a lost completion frame
  // may have stranded the composer as Stop).
  private hasOpenedOnce = false;
  // BUG 4a — keepalive timers. ``keepaliveTimer`` fires every
  // KEEPALIVE_INTERVAL_MS while the socket is OPEN and sends a `session-resume`
  // ping; ``pongDeadlineTimer`` is armed when a ping is sent and fires (=> force-
  // reconnect) only if NO inbound frame arrives within KEEPALIVE_PONG_TIMEOUT_MS.
  // Any inbound frame (the resume reply or ordinary traffic) clears the deadline.
  private keepaliveTimer: number | null = null;
  private pongDeadlineTimer: number | null = null;
  // Mobile connect-attempt timeout (transport surface). Armed in openSocket the
  // instant the socket is created; if it fires while the socket is still
  // CONNECTING (never reached OPEN), we tear the socket down via ws.close() so
  // the existing close handler runs scheduleReconnect -> onWakeNeeded and the
  // wake overlay surfaces in ~CONNECT_ATTEMPT_TIMEOUT_MS instead of the
  // browser's default TCP connect timeout (30-120s) on a stopped box. CLEARED
  // the instant the open handler fires AND in the close handler - it is a
  // connect-phase-only timer and never touches the keepalive ping/pong timers.
  private connectTimer: number | null = null;
  // Auto-stop/wake (NATE 2026-06-17) — count of consecutive failed reconnect
  // schedules since the last successful open. Drives the wake-on-reconnect
  // POST (we only start poking the wake endpoint after the FIRST schedule, but
  // a single transient blip shouldn't flash the UI's "Wake up agent" overlay —
  // App.tsx reads `attempt` and shows the overlay only past a small threshold).
  // Reset to 0 in the open handler.
  private reconnectAttempts = 0;
  // Injected waker so the reconnect loop can ask the wake Lambda to
  // StartInstances when the box appears stopped. Debounced internally; a no-op
  // when no wake endpoint is configured (dev/LAN). Injectable for tests.
  private readonly waker: AgentWaker;
  // LANE CASE-WEB — the CLIENT's CURRENT active Case (mirrors
  // useCases.activeCaseId). The app keeps this updated via setCurrentCaseId.
  // It is STAMPED onto every outbound user-message AND onto the session-resume
  // sent on (re)connect / keepalive / explicit re-pull, so the SERVER always
  // learns the client's current case and uses it as the authority — closing the
  // two-sources-of-truth gap (server `_SESSION_ACTIVE_CASE` vs client) where a
  // reconnect-replay or a stale turn-bind snapped to the wrong case. `null` =
  // root view (no active Case).
  private currentCaseId: string | null = null;
  // LANE CASE-WEB — outbound queue for envelopes issued while the socket is NOT
  // OPEN. sendEnvelope previously no-opped silently (ws.ts "send to no one while
  // connecting"), so a case-command(select) or user-message tapped during
  // connecting/reconnecting was LOST and the server stranded on the wrong case.
  // We buffer those intent-bearing frames here and FLUSH them in the open
  // handler AFTER auth-token + session-resume. Liveness/ack frames
  // (session-resume keepalive, auth-token, cancel) are NOT queued — they are
  // either re-issued naturally on reconnect or meaningless once the socket is
  // gone.
  private outboundQueue: string[] = [];
  // BUG 1b — randomness source for reconnect-delay JITTER. Defaults to
  // Math.random; injectable so tests can seed a deterministic value. Returns a
  // float in [0, 1) like Math.random. See scheduleReconnect for how it is
  // mapped onto the [0.5, 1.0] delay-multiplier window.
  private rng: () => number = Math.random;

  /**
   * BUG 1b — test-only hook to make the reconnect JITTER deterministic. Injects
   * the [0, 1) randomness source used by scheduleReconnect. Production code
   * never calls this (the default is Math.random).
   */
  __test_setRng(rng: () => number): void {
    this.rng = rng;
  }

  constructor(url: string, handlers: WsHandlers, opts?: { waker?: AgentWaker }) {
    this.url = url;
    this.handlers = handlers;
    this.sessionId = loadOrCreateSessionId();
    // "Cases vanish on refresh" durable fix - establish the STABLE client-owned
    // anonymous user_id the INSTANT any GraceWs is constructed (App + Chat each
    // construct one at first load, before any socket connects). Minting it here
    // guarantees the id exists in localStorage before maybeSendAuthToken runs on
    // EITHER socket, so both present the SAME anonymous_user_id from frame one
    // and the server reuses it (instead of minting a fresh ULID per connect ->
    // the two-id divergence that orphaned cases on refresh). Idempotent across
    // siblings + refreshes (earliest-wins): the first call persists, the rest
    // read the cached value.
    loadOrCreateAnonId();
    this.waker = opts?.waker ?? new AgentWaker();
    // job-0159: register with the per-session fan-out hub so envelopes
    // received by sibling GraceWs instances (e.g. App's instance when the
    // tool ran on Chat's instance) are still delivered to OUR handlers.
    hubRegister(this, this.sessionId);
  }

  /** Current session ULID; survives page reload via localStorage. */
  get session(): string {
    return this.sessionId;
  }

  /**
   * LANE CASE-WEB — tell this connection the client's CURRENT active Case
   * (useCases.activeCaseId). App.tsx calls this whenever the active Case
   * changes (and on connect). The value is stamped onto every subsequent
   * outbound user-message + session-resume so the server treats the client as
   * the case authority. `null` = root view (no active Case). Idempotent.
   */
  setCurrentCaseId(caseId: string | null): void {
    this.currentCaseId = caseId;
  }

  /** LANE CASE-WEB — the client's current active Case as this socket knows it. */
  get caseId(): string | null {
    return this.currentCaseId;
  }

  /**
   * sleep/wake STAGE 2 — REPORT the agent box's lifecycle state via the injected
   * waker's report-only GET (it NEVER wakes the box; only an explicit tap ->
   * POST wakes). App.tsx calls this on the App-socket's onWakeNeeded signal so
   * the composer machine can branch to the Wake UI when the box is asleep
   * (state stopped/stopping). "unknown" when wake is unconfigured / the probe
   * fails — the caller then keeps plain Connecting/reconnect. Delegates to the
   * SAME shared AgentWaker the explicit-tap wake() uses so they stay coherent.
   */
  reportWakeState(): Promise<WakeState> {
    return this.waker.reportState();
  }

  /**
   * BUG 4a — true iff the current socket reports OPEN. App.tsx's
   * visibilitychange handler reads this to avoid tearing down an already-OPEN
   * socket: a healthy connection takes the lighter `requestSessionState()`
   * re-pull path; only a closed/closing/never-connected socket gets the
   * `forceReconnect()` teardown. (A genuinely-dead socket that LIES about being
   * OPEN — the iOS zombie — is now caught by the keepalive's missed-pong
   * detector instead of an unconditional resume-time teardown.)
   */
  get isOpen(): boolean {
    return this.socket?.readyState === WebSocket.OPEN;
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
    // LANE CASE-WEB — an explicit teardown abandons any buffered intent frames;
    // a fresh GraceWs (App re-mount) starts with an empty queue so stale
    // selects/messages from a torn-down connection never replay.
    this.outboundQueue = [];
    // Mobile connect-attempt timeout - clear the connect-phase timer on explicit
    // teardown so a pending connect timeout can't fire after close().
    this.clearConnectTimer();
    // BUG 4a — stop the keepalive ping/pong timers on explicit teardown.
    this.stopKeepalive();
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
   * job-0322 F31 — re-request the session-state on the LIVE socket.
   *
   * Mobile browsers tear down the WebSocket when the tab is backgrounded;
   * on resume the socket is often still nominally OPEN but the client's
   * in-memory layers were never re-pulled, so the map looks empty until a
   * Case reopen. This sends a fresh `session-resume` envelope (mirroring the
   * private send inside `openSocket`'s open handler) so the server re-emits
   * the authoritative `session-state` and the Map reconciles the layers back
   * via replace-not-reconcile (Appendix A.7).
   *
   * No-op unless THIS socket is currently OPEN — a closed/closing socket is
   * the `reconnect()` path's responsibility, not this one. Idempotent:
   * calling it when already connected just costs one harmless `session-resume`
   * round-trip.
   */
  requestSessionState(): void {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return;
    // LANE CASE-WEB — re-pull also re-asserts the client's CURRENT active Case
    // so a resume-time replay never snaps the server back to a stale case.
    const resume: Envelope<SessionResumePayload> = envelope(
      "session-resume",
      this.sessionId,
      { case_id: this.currentCaseId },
    );
    this.sendEnvelope(resume);
  }

  /**
   * job-0322 F31 — revive a dropped socket without disturbing the session.
   *
   * If the socket is closed or closing (or null — never connected / torn
   * down on background), run the existing `connect()` path so `openSocket`
   * re-opens against the same `session_id`; its open handler re-sends
   * `auth-token` then `session-resume`, so the layers come back on resume.
   *
   * When the socket is already OPEN (or still CONNECTING) this is a no-op —
   * we must not tear down a healthy connection. App.tsx pairs this with
   * `requestSessionState()` so the common case (resume while still connected)
   * re-pulls state via the live socket and the dead-socket case revives it.
   *
   * Note: `connect()` already clears the auth-failure latch and resets the
   * refresh guard; we do NOT touch the SESSION_HUB registration (it persists
   * for the lifetime of this instance and is only dropped in `close()`).
   */
  reconnect(): void {
    const stale = this.socket;
    const rs = stale?.readyState;
    if (rs === WebSocket.OPEN || rs === WebSocket.CONNECTING) return;
    // A socket in CLOSING state still has a pending `close` event. We drop our
    // reference and ask it to finish closing; its late close event is rendered
    // harmless by the close handler's identity guard (it only mutates instance
    // state when `this.socket` is still itself or null — so it can't null out
    // the FRESH socket `connect()` is about to install). We do NOT call the
    // full `close()` here: that sets `closedByUser` and unregisters from the
    // SESSION_HUB, which would permanently break fan-out for this instance.
    if (stale) {
      try {
        stale.close();
      } catch {
        // ignore — already closed/closing
      }
      this.socket = null;
    }
    this.connect();
  }

  /**
   * job-0322 F31 — UNCONDITIONALLY revive the socket (iOS zombie-socket fix).
   *
   * The bug: after the mobile browser is backgrounded and brought back, the
   * WebSocket's `readyState` can stay `OPEN` even though the underlying
   * connection is dead (iOS Safari freezes the socket without firing a
   * `close`). `reconnect()` early-returns on an OPEN socket, and
   * `requestSessionState()` then sends `session-resume` into that dead socket —
   * so the server never re-emits `session-state` and the map's layers stay
   * gone until a manual Case reopen.
   *
   * The fix: tear down the CURRENT socket no matter what its `readyState` is
   * (OPEN included), drop the reference, then `connect()`. The fresh open
   * handler re-sends `auth-token` then `session-resume`, so the server
   * re-emits the authoritative `session-state` and the layers reconcile back
   * via replace-not-reconcile (Appendix A.7).
   *
   * This mirrors `reconnect()`'s careful teardown (detach + ask the stale
   * socket to close; its late `close` event is rendered harmless by the
   * open handler's identity guard, which only mutates instance state for the
   * socket THIS handler was registered for) but DROPS the OPEN/CONNECTING
   * early-return. We do NOT call the full `close()`: that sets
   * `closedByUser` and unregisters from the SESSION_HUB, which would
   * permanently break fan-out for this instance. `connect()` already clears
   * the auth-failure latch and resets the refresh guard.
   */
  forceReconnect(): void {
    const stale = this.socket;
    // Detach BEFORE closing so the stale socket's `close` event (which may fire
    // synchronously or later) sees `this.socket !== stale` and bails via the
    // open handler's identity guard — it can't null out the fresh socket
    // `connect()` is about to install or schedule a spurious reconnect.
    this.socket = null;
    if (stale) {
      try {
        stale.close();
      } catch {
        // ignore — already closed/closing/never-opened
      }
    }
    this.connect();
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
    // Item 1 (NATE 2026-06-22)  -  mark fanned-out so consumers (App.tsx's
    // onSessionState) know this frame originated on a SIBLING socket and may be
    // STALE relative to this instance's view; it must be ADDITIVE-ONLY (add, never
    // evict) and never stamped authoritative. A natively-received frame
    // (handleMessage -> dispatchEnvelope) leaves fannedOut at its false default.
    this.dispatchEnvelope(envType, payload, caseId, true);
  }

  sendUserMessage(
    text: string,
    researchMode: ResearchMode = "research",
    modelId: string | null = null,
  ): void {
    const payload: UserMessagePayload = {
      text,
      research_mode: researchMode,
      // Only include model_id when the caller explicitly passes one — null
      // means "use server's current selection" (omit the field entirely so
      // older server builds that don't know the field still parse cleanly).
      ...(modelId != null ? { model_id: modelId } : {}),
      // LANE CASE-WEB — STAMP the client's CURRENT active Case so the server
      // binds the turn to the case the client is actually looking at (the
      // authority), not a possibly-stale in-memory active case. `null` = root.
      case_id: this.currentCaseId,
    };
    const env: Envelope<UserMessagePayload> = envelope(
      "user-message",
      this.sessionId,
      payload,
    );
    // LANE CASE-WEB — a user-message issued while the socket isn't OPEN must NOT
    // be silently dropped; QUEUE it so the open handler flushes it once
    // connected (after auth-token + session-resume). When OPEN this sends
    // immediately via sendEnvelope as before.
    this.sendOrQueue(env);
  }

  sendCancel(reason: string | null = null): void {
    const payload: CancelPayload = { reason };
    const env: Envelope<CancelPayload> = envelope(
      "cancel",
      this.sessionId,
      payload,
    );
    // Session-durability Job D (3) - route cancel through sendOrQueue, NOT a
    // bare sendEnvelope. The composer-stuck-as-Stop bug surfaces precisely when
    // the socket dropped at the moment of completion: the user taps the lingering
    // Stop button, which calls sendCancel, but on a CLOSED/reconnecting socket a
    // bare sendEnvelope silently no-ops so the cancel never lands and the turn
    // (already terminal server-side or genuinely still running) is never told to
    // stop. Queuing it means the open handler flushes the cancel once the socket
    // re-opens (after auth-token + session-resume), so a tap on a stuck Stop is
    // honoured instead of swallowed mid-reconnect. When OPEN this still sends
    // immediately via sendOrQueue's fast path.
    this.sendOrQueue(env);
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
   * Emit a `credential-provided` envelope (§F.3 amendment).
   *
   * Sent AFTER the key has been saved via the existing `secret-add` path
   * ({@link GraceWs.sendSecretAdd}) in response to a `credential-request`.
   * Carries NO key material — `secret-add` is the only envelope that
   * transports the raw key value (Decision F). `request_id` echoes the
   * pending `credential-request` so the agent retries the exact paused tool;
   * `secretId` is the SecretRecord id the `secret-add` minted. Pass
   * `provided: false` (with `secretId: null`) to signal the user declined the
   * prompt — the agent then narrates honestly and abandons the paused tool.
   */
  sendCredentialProvided(args: {
    request_id: string;
    secret_id: string | null;
    provided?: boolean;
  }): void {
    const payload: CredentialProvidedPayload = {
      envelope_type: "credential-provided",
      request_id: args.request_id,
      secret_id: args.secret_id,
      provided: args.provided ?? true,
    };
    const env: Envelope<CredentialProvidedPayload> = envelope(
      "credential-provided",
      this.sessionId,
      payload,
    );
    this.sendEnvelope(env);
  }

  /**
   * Emit a `region-choice-provided` envelope (state-bbox-fallback narrowing).
   *
   * Sent when the user answers a `region-choice-request` — either by tapping a
   * candidate county (in the in-chat card list OR on the map choropleth, both
   * synced) or by keeping the whole-state default. `request_id` echoes the
   * pending request verbatim so the agent resumes the exact paused turn.
   *
   *   - choice="region": carries `selected_region_id` (the chosen candidate's
   *     `region_id` the agent re-resolves authoritatively against its candidate
   *     set) + `selected_bbox` (the candidate's bbox, echoed — a fallback the
   *     agent prefers re-resolution by id over, so a tampered bbox cannot
   *     redirect the workflow).
   *   - choice="whole_state": carries neither (the honest already-resolved
   *     whole-state bbox is kept — this IS the decline path, Invariant 8).
   */
  sendRegionChoiceProvided(args: {
    request_id: string;
    choice: "region" | "whole_state";
    selected_region_id?: string | null;
    selected_bbox?: RegionBBox | null;
  }): void {
    const isRegion = args.choice === "region";
    const payload: RegionChoiceProvidedPayload = {
      envelope_type: "region-choice-provided",
      request_id: args.request_id,
      choice: args.choice,
      // Only carry the selection fields on a region pick; whole_state sends
      // null for both (mirrors the pydantic defaults).
      selected_region_id: isRegion ? args.selected_region_id ?? null : null,
      selected_bbox: isRegion ? args.selected_bbox ?? null : null,
    };
    const env: Envelope<RegionChoiceProvidedPayload> = envelope(
      "region-choice-provided",
      this.sessionId,
      payload,
    );
    this.sendEnvelope(env);
  }

  /**
   * Emit a `spatial-input-response` envelope (FR-WC-13 pick-mode + FR-WC-16
   * urban vector-draw).
   *
   * Returns the user's geometry (or a cancellation) on a paused
   * `spatial-input-request`. `request_id` echoes the pending request verbatim so
   * the agent resumes the exact paused turn. Three shapes ride this one sender:
   *
   *   - geometry_type="point":  coordinates=[lon, lat].
   *   - geometry_type="bbox":   coordinates=[minLon, minLat, maxLon, maxLat].
   *   - geometry_type="vector_draw": features = the drawn GeoJSON
   *     FeatureCollection (role-tagged: "aoi" | "barrier" | "point"; a "barrier"
   *     LineString also carries barrier_type "wall" | "flap_gate"). The
   *     role=="barrier" subset is field-for-field the urban (SWMM) engine's
   *     `barriers` FeatureCollection.
   *   - cancelled=true:         the user dismissed the request (Invariant 8 —
   *     cancellation is first-class); geometry fields stay null.
   */
  sendSpatialInputResponse(args: {
    request_id: string;
    geometry_type?: "point" | "bbox" | "vector_draw" | null;
    coordinates?: number[] | null;
    features?: SpatialDrawFeatureCollection | null;
    cancelled?: boolean;
  }): void {
    const cancelled = args.cancelled ?? false;
    const payload: SpatialInputResponsePayload = {
      envelope_type: "spatial-input-response",
      request_id: args.request_id,
      // On a cancellation the geometry fields stay null (mirrors the pydantic
      // defaults); otherwise carry exactly the shape for the geometry_type.
      geometry_type: cancelled ? null : args.geometry_type ?? null,
      coordinates: cancelled ? null : args.coordinates ?? null,
      features: cancelled ? null : args.features ?? null,
      cancelled,
    };
    const env: Envelope<SpatialInputResponsePayload> = envelope(
      "spatial-input-response",
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
    // LANE CASE-WEB — a case-command (above all `select`) issued while the
    // socket isn't OPEN (a select tapped mid-reconnect) must NOT be silently
    // dropped — that is exactly what stranded the server on the wrong case.
    // QUEUE it so the open handler flushes it once connected (after auth-token +
    // session-resume re-assert the case). When OPEN this sends immediately.
    // ``select`` ALSO updates our stamped currentCaseId so the very next
    // session-resume / user-message re-asserts the same case even if the queued
    // frame and the resume race.
    if (command === "select" && caseId) {
      this.currentCaseId = caseId;
    } else if (command === "deselect") {
      this.currentCaseId = null;
    }
    this.sendOrQueue(env);
  }

  /**
   * Emit a `layer-delete` envelope (job-0325 F53).
   *
   * Sent when the user clicks the per-row delete control in the LayerPanel.
   * The server removes the layer from the session's loaded_layers, persists
   * the post-deletion list authoritatively (DynamoDB / Mongo-MCP — replace,
   * NOT union), and emits a fresh `session-state` without the layer. The map
   * removes the overlay automatically via replace-not-reconcile (Appendix A.7)
   * and the agent's loaded-layers awareness reflects the deletion.
   *
   * `map-command` is server->client only, so this is a NEW client->server
   * envelope rather than a reuse of the inbound `remove-layer` discriminant
   * (which would overload the direction semantics).
   */
  sendDeleteLayer(layerId: string): void {
    const payload: LayerDeletePayload = {
      envelope_type: "layer-delete",
      layer_id: layerId,
    };
    const env: Envelope<LayerDeletePayload> = envelope(
      "layer-delete",
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
    // Per-user-agent-isolation — carry the Cognito ID token to the FUTURE broker
    // on the `?st=` QUERY PARAM so it can verify auth PRE-UPGRADE (see the
    // withSessionQueryParam doc-block: the subprotocol carrier is Chrome-
    // incompatible for a JWT-length value — Chromium drops the oversize
    // `Sec-WebSocket-Protocol` header and the WS dies ~90ms after open). Re-read
    // FRESH on every (re)connect from the SAME token cache the in-band
    // `auth-token` handshake uses — a SYNCHRONOUS read so the socket construction
    // (and the reconnect/keepalive timers it owns) stay synchronous, and so a
    // refreshed token is carried on reconnect. No token (anonymous / signed-out /
    // disabled) => no `&st=` => only `?sid` rides. The async `auth-token` +
    // `session-resume` handshake below is UNCHANGED.
    let dialToken: string | null = null;
    try {
      const syncGetter = this.handlers.idTokenSyncGetter ?? getIdTokenSync;
      dialToken = syncGetter();
    } catch {
      dialToken = null;
    }
    let ws: WebSocket;
    try {
      // Carry the stable per-session id as `?sid=` so the future broker can route
      // this connection to its own per-session Fargate task at upgrade time, and
      // (additively) the id token as `?st=` so the broker can verify auth at the
      // same pre-upgrade point. NO subprotocol is offered — the oversize
      // subprotocol header is exactly what Chromium drops. Purely additive: the
      // current single box ignores BOTH unknown query params and reads its token
      // from the in-band `auth-token` message.
      const dialUrl = withSessionQueryParam(
        this.url,
        this.sessionId,
        dialToken,
      );
      ws = new WebSocket(dialUrl);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = ws;
    // Mobile connect-attempt timeout (transport surface). Arm a one-shot timer
    // the instant the socket is created. If it fires while the socket is still
    // CONNECTING (the open handler never ran - e.g. the box is STOPPED and the
    // TCP connect is hanging), tear it down via ws.close() so the EXISTING close
    // handler runs scheduleReconnect -> onWakeNeeded and the wake overlay
    // surfaces in ~CONNECT_ATTEMPT_TIMEOUT_MS instead of the browser default
    // (30-120s). Cleared in BOTH the open handler and the close handler below,
    // so it can only act during the connect phase and never races the keepalive.
    this.connectTimer = window.setTimeout(() => {
      this.connectTimer = null;
      // Only act if the socket never opened (still CONNECTING). An OPEN socket
      // already cleared this timer in its open handler; a CLOSING/CLOSED one is
      // already being handled by the close path.
      if (this.socket !== ws || ws.readyState !== WebSocket.CONNECTING) return;
      try {
        ws.close();
      } catch {
        // ignore - already closing; the close handler still runs scheduleReconnect.
      }
    }, CONNECT_ATTEMPT_TIMEOUT_MS);
    ws.addEventListener("open", () => {
      // Connect phase succeeded - clear the connect-attempt timeout so it can
      // never tear down this now-OPEN socket. MUST run before any keepalive
      // arming so the two timer families never overlap.
      this.clearConnectTimer();
      // BUG 1b — reset the ladder back to the FLOOR (not a bare literal) so a
      // successful open restores the same gentler first-reconnect delay.
      this.backoffMs = GraceWs.RECONNECT_FLOOR_MS;
      // Auto-stop/wake — a successful open means the box is up; clear the
      // consecutive-failure counter so the next drop starts fresh (the UI
      // overlay only shows past a threshold of consecutive failures).
      this.reconnectAttempts = 0;
      this.handlers.onStatus("connected");
      // BUG 4a — start the application-level keepalive now that the socket is
      // OPEN. Re-armed fresh on every (re)open; torn down in the close handler
      // + close()/teardown.
      this.startKeepalive();
      // job-0253b — `auth-token` MUST be the FIRST envelope on every
      // connection. The agent's AUTH_REQUIRED gate (server.py:4047-4063)
      // dispatches in arrival order and rejects the FIRST non-auth-token frame
      // before the handshake completes (4401 "auth-token envelope required
      // before any other message"). Previously `session-resume` was sent
      // synchronously here while `auth-token` followed only after an awaited
      // `getIdToken()` — so under the gate a signed-in user's valid token was
      // never read and every prod connection 4401'd.
      //
      // `maybeSendAuthToken()` ALWAYS emits the auth-token envelope (even with
      // an empty token — job-0172 Part C sticky-anon hint), so awaiting it
      // before `session-resume` is a pure ordering change: dev/anonymous stays
      // byte-equivalent at the protocol level (auth-token, then session-resume,
      // same as the post-fix prod order). `maybeSendAuthToken` swallows any
      // `getIdToken()` failure internally (timeout/throw → empty-token send),
      // so a token-fetch failure can NEVER wedge this open handler — the
      // `session-resume` below still runs after the await settles.
      void (async (): Promise<void> => {
        await this.maybeSendAuthToken();
        // The socket may have closed (or been re-opened) while awaiting the
        // token; sendEnvelope no-ops unless THIS socket is still OPEN.
        if (this.socket !== ws || ws.readyState !== WebSocket.OPEN) return;
        // Resume the session. LANE CASE-WEB — STAMP the client's CURRENT active
        // Case so the server's reconnect-replay re-asserts the client's case as
        // the authority (instead of snapping back to its stale in-memory active
        // case). `null` = root view → empty-payload wire shape preserved.
        const resume: Envelope<SessionResumePayload> = envelope(
          "session-resume",
          this.sessionId,
          { case_id: this.currentCaseId },
        );
        this.sendEnvelope(resume);
        // LANE CASE-WEB — FLUSH any envelopes that were issued while the socket
        // was NOT OPEN (case-command(select) tapped mid-reconnect, a queued
        // user-message). They go out AFTER auth-token + session-resume so the
        // gate's first-frame rule holds and the server's case is already
        // re-asserted before the select/message lands. Done only for THIS
        // socket while it is still OPEN.
        this.flushOutboundQueue(ws);
        // Session-durability Job D (2) - the resume handshake is sent; signal
        // the consumer that the session was (re)resumed so it can force-settle
        // the VISIBLE / targetKey stream's in-flight latch. This is the recovery
        // for the composer-stuck-as-Stop bug: if a turn completed while the
        // socket was dropping (its completion frame lost), the server's
        // re-emitted session-state above clears the OWNING stream, but this hook
        // additionally clears the stream the user is actually looking at so the
        // composer can never stay stuck after a successful reconnect. Fired only
        // for THIS socket while still OPEN; a handler throw must not wedge the
        // open handler. `firstOpen` lets the consumer skip the (idempotent) clear
        // on the very first connect.
        const firstOpen = !this.hasOpenedOnce;
        this.hasOpenedOnce = true;
        try {
          this.handlers.onReconnectResumed?.(firstOpen);
        } catch {
          /* a UI handler throw must not wedge the open handler */
        }
      })();
    });
    ws.addEventListener("message", (ev) => this.handleMessage(ev.data));
    ws.addEventListener("close", (ev) => {
      // job-0322 F31 — identity guard. `reconnect()` may detach a CLOSING
      // socket and open a fresh one before the stale socket's `close` event
      // fires; without this guard the late close would null out the NEW
      // socket (`this.socket`) and schedule a spurious reconnect. Only the
      // close of the socket THIS handler was registered for may mutate
      // instance state. (`this.socket` is already the new socket — or null —
      // in the detach case, so simply bail.)
      if (this.socket !== null && this.socket !== ws) return;
      this.socket = null;
      // Mobile connect-attempt timeout - the socket reached a terminal close
      // (whether it ever opened or not); clear the connect-phase timer so it
      // can't fire against a fresh socket a subsequent (re)open installs. Safe
      // when already cleared (the open handler clears it on a successful open).
      this.clearConnectTimer();
      // BUG 4a — the live socket is gone; stop the keepalive so its ping timer
      // can't fire against a dead socket or arm a spurious pong deadline. A
      // fresh open re-arms it.
      this.stopKeepalive();
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
    // BUG 4a — ANY inbound frame is proof-of-life: clear the pending pong
    // deadline so a healthy socket (the keepalive `session-state` reply OR
    // ordinary agent traffic) is never force-reconnected. Done before the
    // string/JSON guards below so even a non-string / unparseable frame still
    // counts as the connection being alive.
    this.noteInboundActivity();
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
    // Item 1 (NATE 2026-06-22)  -  true only when delivered via the fan-out hub
    // from a sibling instance (deliverFannedOut). Forwarded to onSessionState so
    // the App handler can keep a fanned-out (possibly stale) frame additive-only.
    fannedOut = false,
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
          fannedOut,
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
      case "credential-request":
        // §F.3 amendment: server -> client just-in-time credential prompt. A
        // keyed tool paused on a missing/invalid credential; the prompt
        // surface saves the key via the existing secret-add path then signals
        // retry via sendCredentialProvided(). Optional handler so chat-only
        // callers can ignore. Behaviour is out of scope for the contract-only
        // landing — this routes the typed envelope to whoever subscribes.
        if (this.handlers.onCredentialRequest) {
          this.handlers.onCredentialRequest(
            payload as unknown as CredentialRequestPayload,
          );
        }
        break;
      case "region-choice-request":
        // Region-disambiguation prompt (state-bbox-fallback narrowing). A
        // geocode snapped to a whole-state bbox and the agent is offering a
        // narrower county pick. Chat.tsx renders the inline RegionPickerCard +
        // publishes to the region-choice bus so Map.tsx paints the synced
        // choropleth. The user's pick rides back via sendRegionChoiceProvided.
        // Malformed payloads (missing request_id) are dropped to avoid crashing
        // the React tree. Optional handler so chat-only callers can ignore.
        if (this.handlers.onRegionChoiceRequest) {
          const rc = payload as unknown as RegionChoiceRequestPayload;
          if (rc && typeof rc.request_id === "string") {
            this.handlers.onRegionChoiceRequest(rc);
          } else {
            // eslint-disable-next-line no-console
            console.warn("[ws] region-choice-request dropped: missing request_id", payload);
          }
        }
        break;
      case "spatial-input-request":
        // Spatial-input prompt (FR-WC-13 pick-mode + FR-WC-16 urban
        // vector-draw). The agent paused the turn awaiting a point / bbox /
        // drawn FeatureCollection. Map.tsx enters pick-mode or opens the
        // terra-draw surface; Chat.tsx renders the inline prompt card. The
        // user's geometry rides back via sendSpatialInputResponse. Malformed
        // payloads (missing request_id) are dropped to avoid crashing the React
        // tree. Optional handler so chat-only callers can ignore.
        if (this.handlers.onSpatialInputRequest) {
          const si = payload as unknown as SpatialInputRequestPayload;
          if (si && typeof si.request_id === "string") {
            this.handlers.onSpatialInputRequest(si);
          } else {
            // eslint-disable-next-line no-console
            console.warn("[ws] spatial-input-request dropped: missing request_id", payload);
          }
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
      case "solve-progress":
        // NATE 2026-06-17: live big-sim readout. The agent emits this while a
        // heavy solver burns wall-clock; Chat threads it onto the running
        // solver step's PipelineCard. Malformed payloads (missing run_id) are
        // dropped with a console.warn to avoid crashing the React tree.
        if (this.handlers.onSolveProgress) {
          const sp = payload as unknown as SolveProgressPayload;
          if (sp && typeof sp.run_id === "string") {
            this.handlers.onSolveProgress(sp, caseId);
          } else {
            // eslint-disable-next-line no-console
            console.warn("[ws] solve-progress dropped: missing run_id", payload);
          }
        }
        break;
      case "tool-io":
        // tool-card-expand-output spec: the agent emits the RAW args +
        // function_response for a tool dispatch keyed by step_id. Chat stores it
        // so the matching tool card's expander reveals it. Malformed payloads
        // (missing step_id) are dropped with a console.warn.
        if (this.handlers.onToolIo) {
          const io = payload as unknown as ToolIoPayload;
          if (io && typeof io.step_id === "string") {
            this.handlers.onToolIo(io, caseId);
          } else {
            // eslint-disable-next-line no-console
            console.warn("[ws] tool-io dropped: missing step_id", payload);
          }
        }
        break;
      case "turn-complete":
        // C2 terminal-state durability: end-of-turn signal. Chat.tsx force-
        // completes any tool card still rendering `running` (its terminal
        // pipeline-state frame may have been lost on a socket drop). No
        // required field — a bare `{}` payload is a valid whole-turn idle, so
        // there is nothing to validate/drop here. Optional handler so chat-only
        // / older callers can ignore.
        if (this.handlers.onTurnComplete) {
          this.handlers.onTurnComplete(
            payload as unknown as TurnCompletePayload,
            caseId,
          );
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
        // Ignores tool-call-* and location-resolved (the spatial-input
        // pick-mode / vector-draw request is now handled above). Logging only.
        // eslint-disable-next-line no-console
        console.debug("[ws] unhandled frame type:", envType);
    }
  }

  private sendEnvelope<P>(env: Envelope<P>): void {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return;
    this.socket.send(JSON.stringify(env));
  }

  /**
   * LANE CASE-WEB — send NOW if the socket is OPEN, else BUFFER for the open
   * handler to flush. Used for intent-bearing client->server frames
   * (case-command(select), user-message) so a select/message issued during
   * connecting / reconnecting / waking is delivered once connected instead of
   * being silently dropped (NATE's "don't send to no one while connecting").
   *
   * The frame is pre-serialized here so the queued bytes are stable (the
   * stamped case_id / model_id are captured at call time, matching the user's
   * intent at the moment they acted). A small cap guards against unbounded
   * growth while a box is stopped for a long time; the OLDEST frames are
   * dropped first (keep the most recent intent).
   */
  private sendOrQueue<P>(env: Envelope<P>): void {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(env));
      return;
    }
    this.outboundQueue.push(JSON.stringify(env));
    const MAX_QUEUE = 50;
    if (this.outboundQueue.length > MAX_QUEUE) {
      this.outboundQueue.splice(0, this.outboundQueue.length - MAX_QUEUE);
    }
  }

  /**
   * LANE CASE-WEB — flush the buffered intent frames onto the just-opened
   * socket, in FIFO order, AFTER auth-token + session-resume have been sent (the
   * open handler calls this last). Guards that THIS socket is still the live
   * OPEN one before each batch so a mid-flush close can't blast frames into a
   * dead socket. Anything still queued if the socket isn't OPEN stays buffered
   * for the next open.
   */
  private flushOutboundQueue(ws: WebSocket): void {
    if (this.outboundQueue.length === 0) return;
    if (this.socket !== ws || ws.readyState !== WebSocket.OPEN) return;
    const pending = this.outboundQueue;
    this.outboundQueue = [];
    for (const raw of pending) {
      if (this.socket !== ws || ws.readyState !== WebSocket.OPEN) {
        // Socket died mid-flush — re-buffer the rest (this frame included).
        this.outboundQueue.push(raw);
        continue;
      }
      ws.send(raw);
    }
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
    // "Cases vanish on refresh" durable fix - when there is NO Cognito token,
    // ALWAYS attach the STABLE client-owned anon id (loadOrCreateAnonId mints it
    // on first load and returns the same value forever after). Using
    // loadOrCreateAnonId (not readAnonymousUserId) guarantees a non-null, valid
    // ULID hint from the FIRST connect of BOTH sockets even if the constructor's
    // mint somehow had not landed yet (e.g. localStorage cleared mid-session) -
    // the server then reuses this one id for the session, so every refresh and
    // every sibling socket bind to the SAME anonymous User and cases stay
    // owner-scoped. The contract field (auth.py AuthTokenEnvelope) is
    // anonymous_user_id: ULIDStr|None, so a valid 26-char ULID is required here.
    const stickyHint = token ? null : loadOrCreateAnonId();
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

  // -------------------------------------------------------------------------
  // BUG 4a — application-level keepalive.
  // -------------------------------------------------------------------------

  /**
   * Arm the keepalive interval for the CURRENT (just-opened) socket. Clears any
   * prior timers first (idempotent — a stale interval from a previous socket
   * must never run against the new one). Each tick sends a `session-resume`
   * ping (see KEEPALIVE_INTERVAL_MS doc) and arms a pong deadline; an inbound
   * frame before the deadline clears it (``noteInboundActivity``).
   */
  private startKeepalive(): void {
    this.stopKeepalive();
    this.keepaliveTimer = window.setInterval(() => {
      this.sendKeepalivePing();
    }, KEEPALIVE_INTERVAL_MS);
  }

  /**
   * Mobile connect-attempt timeout - clear the one-shot connect-phase timer.
   * Safe to call when it is not armed (idempotent). Called the instant the open
   * handler fires (success) and in the close handler / explicit teardown.
   * Deliberately separate from stopKeepalive so the connect-phase timer and the
   * keepalive ping/pong timers never share a clear path or accidentally overlap.
   */
  private clearConnectTimer(): void {
    if (this.connectTimer !== null) {
      window.clearTimeout(this.connectTimer);
      this.connectTimer = null;
    }
  }

  /** Clear both keepalive timers. Safe to call when neither is armed. */
  private stopKeepalive(): void {
    if (this.keepaliveTimer !== null) {
      window.clearInterval(this.keepaliveTimer);
      this.keepaliveTimer = null;
    }
    if (this.pongDeadlineTimer !== null) {
      window.clearTimeout(this.pongDeadlineTimer);
      this.pongDeadlineTimer = null;
    }
  }

  /**
   * Send one keepalive ping (a `session-resume` envelope — the server-supported
   * proxy-warming frame) and arm the pong deadline. No-op when the socket is not
   * OPEN (the close handler will have stopped the keepalive anyway). If a pong
   * deadline is already pending we do NOT stack a second one — the prior one is
   * still counting down and an inbound frame clears it.
   */
  private sendKeepalivePing(): void {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return;
    // LANE CASE-WEB — the keepalive resume also carries the client's CURRENT
    // active Case so the server's authoritative active case never drifts away
    // from the client between turns (the resume reply is replace-not-reconcile,
    // so a matching case is a no-op).
    const resume: Envelope<SessionResumePayload> = envelope(
      "session-resume",
      this.sessionId,
      { case_id: this.currentCaseId },
    );
    this.sendEnvelope(resume);
    if (this.pongDeadlineTimer === null) {
      this.pongDeadlineTimer = window.setTimeout(() => {
        this.pongDeadlineTimer = null;
        // No inbound frame answered the ping within the timeout → the socket is
        // dead (idle-culled by the proxy, or an iOS zombie that still reports
        // OPEN). forceReconnect() tears it down unconditionally and re-opens; the
        // fresh open handler re-sends auth-token + session-resume and re-arms the
        // keepalive. closedByUser / authFailed are honoured by forceReconnect →
        // connect()/openSocket downstream.
        if (this.closedByUser || this.authFailed) return;
        this.forceReconnect();
      }, KEEPALIVE_PONG_TIMEOUT_MS);
    }
  }

  /**
   * Record that an inbound frame arrived: clear the pending pong deadline so a
   * live socket is never force-reconnected. Called for EVERY inbound message
   * (including the keepalive's own `session-state` reply).
   */
  private noteInboundActivity(): void {
    if (this.pongDeadlineTimer !== null) {
      window.clearTimeout(this.pongDeadlineTimer);
      this.pongDeadlineTimer = null;
    }
  }

  private scheduleReconnect(): void {
    this.handlers.onStatus("reconnecting");
    // sleep/wake STAGE 2 (NATE 2026-06-18) — NEVER AUTO-WAKE. A scheduled
    // reconnect means the socket would not open / dropped, and the agent box may
    // have been STOPPED by the idle-check Lambda. Under STAGE 1 we POSTed the
    // wake endpoint here on EVERY reconnect schedule (StartInstances), so a
    // case-open / tab-blip / reconnect would silently wake the box without any
    // user intent. STAGE 2 SEVERS that: the wake POST happens ONLY on the user's
    // explicit TAP of the composer's Wake UI (App.tsx handleWakeTap ->
    // AgentWaker.wake). We do NOT even GET-report here — the App-socket's
    // onWakeNeeded signal below tells the composer machine to run a report-only
    // GET (wakeState) so it can branch to the Wake UI. The reconnect backoff
    // keeps retrying the WS so a box that is up (or that the user woke) reconnects
    // on its own.
    this.reconnectAttempts += 1;
    // Signal the UI ("the box is unreachable on this socket"). The attempt count
    // lets App.tsx debounce the wake probe so a single transient blip doesn't
    // flash the Wake UI. NO wake POST is issued here (never auto-wake).
    try {
      this.handlers.onWakeNeeded?.(this.reconnectAttempts);
    } catch {
      /* a UI handler throw must not wedge the reconnect loop */
    }
    // BUG 1b — JITTER the scheduled delay so many tabs/sockets do not reconnect
    // in lockstep and a single flapping socket does not retry on a fixed
    // cadence. The base ladder (backoffMs) still DOUBLES toward the 5000ms
    // ceiling; only the actual wait is randomized within [0.5, 1.0] x base
    // (i.e. up to a 50 percent earlier retry, never later than the base). With
    // rng() in [0, 1), the factor is 0.5 + 0.5*rng() in [0.5, 1.0).
    const base = this.backoffMs;
    this.backoffMs = Math.min(this.backoffMs * 2, this.maxBackoffMs);
    const jitterFactor = 0.5 + 0.5 * this.rng();
    const delay = Math.round(base * jitterFactor);
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.openSocket("connecting");
    }, delay);
  }
}
