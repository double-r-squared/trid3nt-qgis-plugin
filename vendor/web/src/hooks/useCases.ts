// GRACE-2 web — useCases hook (job-0137, sprint-12-mega Wave 3 — FR-MP-6).
//
// Encapsulates the Case state machine (FR-MP-6 left-rail + chat-replay
// rehydration) so App.tsx stays focused on layout + WS wiring. The hook:
//
//   1. Tracks `cases` (left-rail list) — updated on every `case-list` frame.
//   2. Tracks `activeCaseId` (the open Case) — updated on `case-open`.
//   3. Tracks `activeSession` (rehydration envelope) — chat + layers + map.
//   4. Tracks `persistenceState` — drives PersistenceChip:
//        - "saved"      at rest
//        - "saving"     while a case-command is in flight
//        - "anonymous"  when no signed-in user
//   5. Exposes typed emitters: createCase / selectCase / renameCase /
//      archiveCase / deleteCase — each calls the GraceWs.sendCaseCommand
//      seam.
//
// The hook does NOT own the WS itself. App.tsx passes a stable
// `sendCaseCommand` callback (bound to `wsRef.current.sendCaseCommand`) plus
// the GraceWs event handlers (onCaseList / onCaseOpen) wired through its
// existing GraceWs instance. This keeps the hook free of WebSocket lifecycle
// and Firebase Auth — both are App.tsx's responsibility.
//
// Invariants honored:
//   - 1 (determinism boundary): the hook displays / forwards received Case
//     envelopes verbatim — no number / id / chat / layer is fabricated here.
//   - 8 (cancellation is first-class): there is no "cancel case-command"
//     surface; in-flight tool cancellation flows through the existing
//     `cancel` envelope on Chat.tsx. The hook only optimistically marks
//     `saving` until the next case-list / case-open frame.
//   - 9 (no cost theater): no cost / quota / quote field anywhere.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CaseCommand,
  CaseListEnvelopePayload,
  CaseOpenEnvelopePayload,
  CaseSessionState,
  CaseSummary,
  ProjectLayerSummary,
} from "../contracts";
import type { BBox } from "../lib/bbox_draw";

/**
 * COLD-LIST RASTER FALLBACK (defense in depth - coldview_layers_fix.md FIX C).
 *
 * A raster `ProjectLayerSummary` is cold-renderable ONLY if its `uri` is an
 * already-resolved TiTiler tile template served by the always-on
 * TiTiler+CloudFront box (e.g. `https://.../cog/tiles/.../{z}/{x}/{y}.png?...`).
 * A bare `s3://...` / `gs://...` object handle is an agent-only DATA pointer the
 * browser holds no creds to read, so it is NOT cold-renderable and is dropped
 * here. Vector layers are dropped regardless (their cold-render needs inline
 * GeoJSON the list summary does not carry - that is the per-case snapshot's job,
 * J2). This is deliberately conservative: the list fallback paints what is
 * provably cold-renderable and nothing else, so a stale per-case snapshot
 * degrades to "rasters paint" instead of "no layers loaded", never to a broken /
 * unauthorized tile request.
 */
export function coldRenderableRasterSummaries(
  summaries: ReadonlyArray<ProjectLayerSummary> | undefined | null,
): ProjectLayerSummary[] {
  if (!Array.isArray(summaries)) return [];
  return summaries.filter((s) => {
    if (!s || s.layer_type !== "raster") return false;
    const uri = typeof s.uri === "string" ? s.uri.trim() : "";
    // Cold-renderable raster URIs are browser-fetchable http(s) tile templates
    // (the resolved TiTiler /cog/tiles/.../{z}/{x}/{y}.png?... face). A bare
    // object-store handle (s3:// / gs://) or empty uri is NOT cold-renderable.
    return /^https?:\/\//i.test(uri);
  });
}
/**
 * Closed enum of persistence states used by the Cases lifecycle. Previously
 * lived on `components/PersistenceChip.tsx`; that floating chip was removed
 * in job-0143 (auth controls live in Settings now). The type stays here
 * because `useCases` drives it and downstream consumers (App.tsx, future
 * status surfaces) read it as a closed-vocabulary signal.
 *
 *   - "saved"        — no in-flight case-command, signed-in user.
 *   - "saving"       — one or more case-commands awaiting server ack.
 *   - "anonymous"    — no signed-in user; persistence is best-effort.
 *   - "disconnected" — WS dropped (reserved; not currently emitted).
 */
export type PersistenceState =
  | "saved"
  | "saving"
  | "anonymous"
  | "disconnected";

/**
 * ACTIVE-CASE RESTORE (NATE 2026-06-26) - on reload (felt most on mobile) the
 * app dropped back to the Cases LIST instead of staying in the open Case,
 * because nothing persisted the active Case id client-side: the hook inited
 * `activeCaseId` to null every load, and the server's reconnect path
 * (`_handle_session_resume`) re-emits session-state + case-list but NEVER a
 * `case-open`, so the open Case was forgotten. We mirror the active Case id to
 * localStorage on every transition and seed from it on mount; App.tsx then
 * dispatches one `selectCase(restored)` after the socket is wired so layers /
 * chat / map rehydrate. A stale / deleted persisted id self-heals via the
 * existing archived/deleted reconcile effect + deletion tombstones.
 */
export const LS_ACTIVE_CASE = "grace2.activeCaseId";

/** Read the persisted active Case id, guarded against storage being unavailable. */
function readPersistedActiveCase(): string | null {
  try {
    const v = localStorage.getItem(LS_ACTIVE_CASE);
    return v && v.trim().length > 0 ? v : null;
  } catch {
    return null;
  }
}

/**
 * DURABLE DELETION TOMBSTONES (B-CLIENT, NATE 2026-06-26) - box-OFF a deleted
 * Case reappears because the in-memory tombstone Set is lost on reload while the
 * `delete` WS command sits QUEUED (never reaches the asleep server), so the next
 * cold authoritative /case-list (which is STALE - pre-delete - yet flagged
 * authoritative) re-adds the Case. Persisting the tombstone set to localStorage
 * lets a reload-before-server-confirm still suppress the deleted Case, and the
 * onCaseList reconcile no longer clears a tombstone just because a (stale)
 * authoritative list still carries the id.
 */
export const LS_DELETED_CASE_IDS = "grace2.deletedCaseIds";

/** Read the persisted deletion tombstone set, guarded against storage failure. */
function readPersistedDeletedIds(): Set<string> {
  try {
    const raw = localStorage.getItem(LS_DELETED_CASE_IDS);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(
      parsed.filter((v): v is string => typeof v === "string" && v.length > 0),
    );
  } catch {
    return new Set();
  }
}

/** Mirror the tombstone set to localStorage (JSON array), guarded against failure. */
function persistDeletedIds(ids: Set<string>): void {
  try {
    localStorage.setItem(LS_DELETED_CASE_IDS, JSON.stringify([...ids]));
  } catch {
    /* storage unavailable - tombstone durability is best-effort */
  }
}

/** Bound emitter for the `case-command` envelope. Matches GraceWs.sendCaseCommand. */
export type CaseCommandEmitter = (
  command: CaseCommand,
  caseId: string | null,
  args: Record<string, unknown>,
) => void;

export interface UseCasesOptions {
  /** Bound emitter the hook uses to dispatch `case-command` envelopes. */
  sendCaseCommand: CaseCommandEmitter;
  /**
   * Whether a real (non-anonymous) user is signed in. When false, the
   * persistence chip surfaces "Sign in to save" but the hook still operates
   * — the agent's anonymous fallback handles the session-id placeholder so
   * dev / unauthenticated flows still work end-to-end (sprint-12-mega Wave 2
   * persistence track default per kickoff §5).
   */
  isSignedIn: boolean;
  /**
   * COLD-LIST RASTER FALLBACK (defense in depth - coldview_layers_fix.md FIX
   * C). Optional sink the hook calls from `onCaseList` with the OPEN Case's
   * cold-renderable RASTER `loaded_layer_summaries` (filtered via
   * `coldRenderableRasterSummaries`). App.tsx wires this to push those rasters
   * onto the map channel as a NON-authoritative reconcile, so when the per-case
   * `case-views/{id}.json` snapshot is missing / stale / 404 the cold case-LIST
   * still paints the raster overlays (which are always-on TiTiler tile
   * templates) instead of leaving "no layers loaded". Because the push is
   * non-authoritative (and rasters merge idempotently with the snapshot's
   * loaded_layers), it never wipes the warm path nor strands vectors. Absent =>
   * the hook does nothing extra (the warm path is byte-identical to before).
   */
  onListLayerSummaries?: (
    caseId: string,
    rasterSummaries: ProjectLayerSummary[],
  ) => void;
}

export interface UseCasesReturn {
  /** Left-rail list from the most recent `case-list` envelope. */
  cases: CaseSummary[];
  /** ULID of the currently-open Case, or null when no Case is open. */
  activeCaseId: string | null;
  /**
   * ACTIVE-CASE RESTORE (NATE 2026-06-26). The active Case id that was seeded
   * from localStorage at mount (null if none was persisted). App.tsx reads this
   * ONCE to dispatch a single `selectCase(restored)` after the socket is wired,
   * so the persisted open Case rehydrates (the server never re-emits a
   * `case-open` on resume). Stable for the life of the hook (it is the initial
   * seed, not the live `activeCaseId`).
   */
  restoredActiveCaseId: string | null;
  /** The most recent rehydration envelope (chat + layers + map). */
  activeSession: CaseSessionState | null;
  /** Drives PersistenceChip. */
  persistenceState: PersistenceState;
  /**
   * CASE-LIST LOADING SIGNAL (BUG 1: late spinner). False until the FIRST
   * `case-list` frame (live WS or cold authoritative fetch) lands, then true
   * forever. CasesPanel reads this to distinguish "still loading the list"
   * (spinner) from "loaded, genuinely zero cases" (empty stub): the empty
   * stub must NOT flash while the very first list is still inbound. Starts
   * false so the spinner shows IMMEDIATELY on first paint (before any await),
   * which fixes the perceived gap where the empty stub rendered first and the
   * list "froze" momentarily.
   */
  casesSettled: boolean;

  // --- Envelope handlers (App.tsx wires these into GraceWs handlers) ---- //
  /**
   * Reconcile a case-list frame into the left rail.
   *
   * `isAuthoritative` distinguishes the SOURCE of an EMPTY list:
   *   - false (DEFAULT - the live WS path): an empty incoming list is a
   *     NON-authoritative keepalive/heartbeat blip; keep the current rail
   *     (the flicker fix). A non-empty list always replaces.
   *   - true (the /case-list cold FETCH path): an empty list is a GENUINE
   *     zero-cases answer and clears the rail (so deleting the last case
   *     followed by an authoritative empty list correctly empties it).
   */
  onCaseList: (
    payload: CaseListEnvelopePayload,
    isAuthoritative?: boolean,
  ) => void;
  onCaseOpen: (payload: CaseOpenEnvelopePayload) => void;

  // --- Emitters --------------------------------------------------------- //
  /**
   * Create a new Case with optional title hint (defaults server-side to
   * "Untitled Case") and optional AOI bbox (#170). When a bbox is supplied it
   * rides into the create args as `bbox: [minLon, minLat, maxLon, maxLat]`; the
   * server seeds CaseSummary.bbox + state.case_bbox so the FIRST turn reuses
   * the extent (no re-geocode). The no-bbox call is byte-identical to before.
   */
  createCase: (title?: string | null, bbox?: BBox | null) => void;
  /** Open / hydrate an existing Case by id. */
  selectCase: (caseId: string) => void;
  /** Rename a Case (in-place title edit from CasesPanel). */
  renameCase: (caseId: string, newTitle: string) => void;
  /** Archive a Case (soft, reversible). */
  archiveCase: (caseId: string) => void;
  /** Delete a Case (soft-delete; CasesPanel confirms before calling). */
  deleteCase: (caseId: string) => void;
  /** Clear active Case locally — used when archive/delete targets the active Case. */
  clearActive: () => void;
}

/**
 * Track Case lifecycle + emit case-command envelopes.
 *
 * Stable callback identity for emitters is preserved via useCallback so
 * downstream subscribers (CasesPanel, App.tsx jumpTo wiring) don't re-render
 * on every parent render.
 */
export function useCases(opts: UseCasesOptions): UseCasesReturn {
  const { sendCaseCommand, isSignedIn, onListLayerSummaries } = opts;

  const [cases, setCases] = useState<CaseSummary[]>([]);
  // ACTIVE-CASE RESTORE (NATE 2026-06-26) - seed lazily from localStorage so a
  // reload re-opens the last-open Case instead of dropping to the Cases list.
  // The init runs once; `restoredActiveCaseId` captures that seed so App can
  // dispatch a single `selectCase(restored)` to rehydrate over the WS.
  //
  // B-CLIENT (NATE 2026-06-26) - but if that persisted active id is a Case the
  // user deleted box-OFF (the `delete` command queued, never confirmed, so the
  // active-id mirror still points at it), the 371caa3 restore would re-open the
  // just-deleted Case. Consult the DURABLE tombstone set at the same mount tick
  // (both read from localStorage) and treat a tombstoned restored id as NO
  // active Case, so the deleted Case does not resurrect via restore.
  const [activeCaseId, setActiveCaseId] = useState<string | null>(() => {
    const persisted = readPersistedActiveCase();
    if (persisted && readPersistedDeletedIds().has(persisted)) return null;
    return persisted;
  });
  const restoredActiveCaseIdRef = useRef<string | null>(activeCaseId);
  const [activeSession, setActiveSession] = useState<CaseSessionState | null>(
    null,
  );

  // CASE-LIST LOADING SIGNAL (BUG 1). Flips true on the FIRST `case-list` frame
  // (any source) and stays true. See `casesSettled` in UseCasesReturn.
  const [casesSettled, setCasesSettled] = useState(false);

  // DELETION TOMBSTONES (BUG 2: stale case-list resurrects a deleted Case).
  // A just-deleted Case id is recorded here SYNCHRONOUSLY on `deleteCase`. The
  // server's delete write + the user-scoped `list_cases_for_user` are correct,
  // but a NON-authoritative case-list frame (a 25s keepalive resume, or a
  // reconnect's fresh-resume case-list) can RACE the soft-delete write and
  // still carry the just-deleted Case -> a wholesale replace then resurrects it
  // in the rail (the same class as the layer-eviction tombstone + the raster
  // cold re-add). We filter EVERY incoming case-list AND the onCaseOpen upsert
  // against this set so a stale frame can never re-add a Case the user deleted
  // this session. Kept in a ref (read inside the stable useCallback handlers
  // without going stale).
  //
  // DURABLE TOMBSTONES (B-CLIENT, NATE 2026-06-26) - box-OFF the `delete` WS
  // command is QUEUED (never reaches the asleep server), so the tombstone is the
  // ONLY thing suppressing the Case until a real serverless delete lands. An
  // in-memory Set is lost on reload, after which the stale cold /case-list
  // (authoritative-yet-pre-delete) resurrects the Case. We therefore SEED the
  // set from localStorage at mount and MIRROR it on every add, so a reload before
  // the server confirms still suppresses the deleted Case. We also NO LONGER
  // clear a tombstone just because an authoritative list still carries the id
  // (that list can be the box-off cold stale snapshot) - a tombstone is removed
  // only on POSITIVE proof of un-delete (e.g. a future serverless-delete error
  // path) or a bounded TTL, neither wired yet.
  const deletedIdsRef = useRef<Set<string>>(readPersistedDeletedIds());

  // COLD-LIST RASTER FALLBACK - `onCaseList` is a stable useCallback([]) so it
  // cannot read `activeCaseId` / `onListLayerSummaries` from its closure
  // without going stale. Mirror both into refs kept in lockstep below so the
  // list handler always sees the CURRENT open Case + sink.
  const activeCaseIdRef = useRef<string | null>(null);
  activeCaseIdRef.current = activeCaseId;
  const onListLayerSummariesRef = useRef(onListLayerSummaries);
  onListLayerSummariesRef.current = onListLayerSummaries;
  // Optimistic in-flight count: how many case-commands have been emitted
  // without a corresponding case-list / case-open reply. The reply clears
  // the counter back to zero. We keep this as a ref + state pair: the ref
  // is for the increment/decrement arithmetic (synchronous), the state is
  // for re-render triggering on transitions.
  const inFlightRef = useRef(0);
  const [inFlight, setInFlight] = useState(0);

  function bumpInFlight(delta: number): void {
    inFlightRef.current = Math.max(0, inFlightRef.current + delta);
    setInFlight(inFlightRef.current);
  }

  function settle(): void {
    inFlightRef.current = 0;
    setInFlight(0);
  }

  // --- Envelope handlers -------------------------------------------------- //
  const onCaseList = useCallback(
    (payload: CaseListEnvelopePayload, isAuthoritative = false) => {
      // CLIENT FLICKER FIX (per-Case layer DURABILITY) - the server re-ships a
      // full case-list on every resume INCLUDING the 25s keepalive heartbeat. A
      // heartbeat (or a reconnect mid-flight) can momentarily carry an EMPTY /
      // stale list; a wholesale `setCases(payload.cases ?? [])` then blanked the
      // left rail and refilled on the next good frame -> the flicker (and, since
      // the active-Case tombstone guard reads `cases`, a transient empty list
      // could race-clear the open Case). Reconcile instead: an EMPTY incoming
      // list while we already hold cases is treated as a non-authoritative pong
      // (keep what we have); a NON-empty list is authoritative and replaces
      // (covers genuine create / rename / archive / delete refreshes).
      //
      // LAST-CASE EDGE FIX (sleep/wake STAGE 2) - the empty-keep rule above is
      // correct for a WS keepalive blip but WRONG for the /case-list cold FETCH:
      // when the cold fetch returns an empty list it is the GENUINE truth (the
      // user has zero cases, e.g. just deleted the last one), so it MUST clear
      // the rail. `isAuthoritative` carries that source distinction: only an
      // authoritative empty list replaces; a non-authoritative (live-WS) empty
      // list keeps the current rail (preserving the flicker fix).
      // BUG 1: the FIRST list of any source settles the loading spinner.
      setCasesSettled(true);

      const incoming = payload.cases ?? [];

      // DELETION TOMBSTONE RECONCILE (BUG 2 + B-CLIENT box-off, NATE
      // 2026-06-26). A tombstoned id is ALWAYS filtered out of the rail -
      // from a non-authoritative (keepalive / fresh-resume) frame AND from an
      // authoritative one. The earlier code cleared a tombstone whenever an
      // authoritative list still carried the id (assuming authoritative ==
      // un-delete proof); but box-OFF the `delete` WS command is QUEUED and
      // never reaches the asleep server, while the cold /case-list fetch is
      // dispatched isAuthoritative=true even though it is STALE (pre-delete) -
      // so that clear resurrected the just-deleted Case on the device. We no
      // longer treat "an authoritative list carries the id" as un-delete proof:
      // the tombstone is kept and the id stays suppressed. A tombstone should be
      // cleared only on POSITIVE proof of un-delete (a future serverless-delete
      // error path) or a bounded TTL - neither wired yet, so for now we never
      // clear it here.
      const tombstones = deletedIdsRef.current;
      const filteredIncoming =
        tombstones.size > 0
          ? incoming.filter((c) => !tombstones.has(c.case_id))
          : incoming;

      setCases((prev) =>
        filteredIncoming.length === 0 && prev.length > 0 && !isAuthoritative
          ? prev
          : filteredIncoming,
      );
      settle();

      // COLD-LIST RASTER FALLBACK (defense in depth - coldview_layers_fix.md
      // FIX C). When a Case is open but its per-case snapshot is missing /
      // stale / 404 (the box-stop lost-write race), the map shows "no layers".
      // The cold case-LIST envelope already carries each Case's
      // `loaded_layer_summaries`; surface the OPEN Case's cold-renderable RASTER
      // summaries (TiTiler tile templates via always-on CloudFront) to the sink
      // so they still paint. We forward only the open Case's rasters (the map
      // channel shows one Case at a time); vectors + bare object-store handles
      // are dropped by `coldRenderableRasterSummaries` (vectors need inline
      // GeoJSON from the snapshot - J2's job). The sink pushes a
      // NON-authoritative reconcile, so this is idempotent with a later snapshot
      // / live frame and never wipes a warm Case.
      const sink = onListLayerSummariesRef.current;
      const openId = activeCaseIdRef.current;
      if (sink && openId !== null) {
        const openCase = filteredIncoming.find((c) => c.case_id === openId);
        const rasters = coldRenderableRasterSummaries(
          openCase?.loaded_layer_summaries,
        );
        if (rasters.length > 0) sink(openId, rasters);
      }
    },
    [],
  );

  const onCaseOpen = useCallback((payload: CaseOpenEnvelopePayload) => {
    const session = payload.session_state ?? null;
    // job-0273: optimistically upsert the opened Case into the rail list.
    // The auto-create flow emits case-open BEFORE the refreshed case-list
    // (observed live: 27ms apart). With a non-empty rail, the tombstone
    // guard below saw activeCaseId pointing at a Case that was not yet in
    // `cases` and bounced the user back to root — while Chat's adoption had
    // already cleared the root stream, leaving a fully EMPTY chat for the
    // whole turn. The envelope carries the full CaseSummary; the case-list
    // frame that follows canonicalizes.
    if (session) {
      // BUG 2: a late case-open for a Case the user just deleted (e.g. a queued
      // `select` that round-trips after the `delete`) must NOT re-add it. Skip
      // the optimistic upsert when the id is tombstoned.
      const openId = session.case.case_id;
      if (!deletedIdsRef.current.has(openId)) {
        setCases((prev) =>
          prev.some((c) => c.case_id === openId)
            ? prev
            : [...prev, session.case],
        );
      }
    }
    setActiveSession(session);
    setActiveCaseId(session?.case.case_id ?? null);
    settle();
  }, []);

  // --- Emitters ---------------------------------------------------------- //
  const createCase = useCallback(
    (title: string | null = null, bbox: BBox | null = null) => {
      // The no-bbox path is byte-identical to before (Skip = current behavior):
      // args carries only the title hint when present. When a bbox IS supplied
      // (#170 AOI-first), it rides in as `bbox` so the server seeds
      // CaseSummary.bbox + state.case_bbox for the first turn.
      const args: Record<string, unknown> = {};
      if (title && title.trim().length > 0) args.title = title.trim();
      if (bbox) args.bbox = bbox;
      bumpInFlight(+1);
      sendCaseCommand("create", null, args);
    },
    [sendCaseCommand],
  );

  const selectCase = useCallback(
    (caseId: string) => {
      // B-CLIENT (NATE 2026-06-26) - never re-select a tombstoned (deleted) id.
      // App dispatches one selectCase(restoredActiveCaseId) on mount to rehydrate
      // the last-open Case; box-OFF that restored id can be a Case the user just
      // deleted (the `delete` command is queued, never confirmed), and the
      // 371caa3 restore would otherwise re-open it. Consulting the durable
      // tombstone keeps the deleted Case from resurrecting via re-select.
      if (deletedIdsRef.current.has(caseId)) return;
      // sleep/wake STAGE 2 (NATE 2026-06-19) - ALWAYS set the active Case
      // LOCALLY, not only via the server's case-open reply. When the agent box
      // is asleep the WS `select` below merely QUEUES (ws.ts sendOrQueue) and no
      // `case-open` ever round-trips, so without this the App cold-load effect
      // (keyed on activeCaseId) would never arm and the Case never paints. The
      // local set is IDEMPOTENT with the live case-open reply (which sets the
      // same id + the rehydrated session); when the box is up the reply simply
      // re-affirms it. We do NOT clear activeSession here - a queued select that
      // never lands must leave any cold-loaded session in place.
      setActiveCaseId(caseId);
      bumpInFlight(+1);
      sendCaseCommand("select", caseId, {});
    },
    [sendCaseCommand],
  );

  const renameCase = useCallback(
    (caseId: string, newTitle: string) => {
      const trimmed = newTitle.trim();
      if (trimmed.length === 0) return; // server rejects empty; preempt
      // Optimistic: patch local cases list immediately so the row reflects
      // the new title without waiting for the server round-trip. The
      // case-list frame that follows will canonicalize.
      setCases((prev) =>
        prev.map((c) =>
          c.case_id === caseId ? { ...c, title: trimmed } : c,
        ),
      );
      bumpInFlight(+1);
      sendCaseCommand("rename", caseId, { title: trimmed });
    },
    [sendCaseCommand],
  );

  const archiveCase = useCallback(
    (caseId: string) => {
      bumpInFlight(+1);
      sendCaseCommand("archive", caseId, {});
    },
    [sendCaseCommand],
  );

  const deleteCase = useCallback(
    (caseId: string) => {
      // LAST-CASE LIVE FIX (box-off batch) - OPTIMISTICALLY drop the deleted
      // Case from the local rail immediately, instead of waiting on an
      // authoritative case-list to clear it. On the LIVE (connected) path the
      // server's follow-up empty case-list is NON-authoritative by design (the
      // flicker fix keeps a non-empty rail on an empty keepalive blip), so
      // deleting the user's LAST Case would otherwise leave it lingering in the
      // rail until a cold authoritative fetch. Removing it here covers the
      // last-Case case AND every non-last delete cleanly; the case-list frame
      // that follows canonicalizes. This is symmetric with the optimistic
      // rename patch above and does NOT touch the empty-keep keepalive rule.
      //
      // BUG 2 (stale-reappear trap): also TOMBSTONE the id synchronously so a
      // case-list frame (keepalive / fresh-resume / cold authoritative) that
      // races the server soft-delete write and still carries this Case cannot
      // resurrect it in the rail.
      //
      // DURABLE TOMBSTONE (B-CLIENT, NATE 2026-06-26) - mirror the set to
      // localStorage on add. Box-OFF the `delete` command is QUEUED (never
      // reaches the asleep server), so a reload would otherwise lose the
      // in-memory tombstone and the stale cold /case-list would resurrect the
      // Case; persisting it keeps the Case suppressed across that reload until a
      // real serverless delete lands.
      deletedIdsRef.current.add(caseId);
      persistDeletedIds(deletedIdsRef.current);
      setCases((prev) => prev.filter((c) => c.case_id !== caseId));
      bumpInFlight(+1);
      sendCaseCommand("delete", caseId, {});
    },
    [sendCaseCommand],
  );

  const clearActive = useCallback(() => {
    setActiveCaseId(null);
    setActiveSession(null);
    // job-0269: tell the SERVER the client left the Case. Without this the
    // session-scoped active Case kept pointing at the last-opened Case, so
    // prompts from the root view skipped auto-create and dispatched into the
    // stale Case (live 2026-06-10: terrain prompt landed in the flood Case).
    sendCaseCommand("deselect", null, {});
  }, [sendCaseCommand]);

  // ACTIVE-CASE RESTORE (NATE 2026-06-26) - mirror EVERY active-Case transition
  // to localStorage. One effect covers all the paths that set activeCaseId
  // (onCaseOpen, selectCase, clearActive, deleteCase, the archived/deleted
  // reconcile below) so the persisted id always tracks the live open Case. A
  // null active Case removes the key (so exit-to-root genuinely forgets the open
  // Case and the next reload lands on the Cases list). Guarded so a storage
  // failure (private mode / quota) never throws.
  useEffect(() => {
    try {
      if (activeCaseId) localStorage.setItem(LS_ACTIVE_CASE, activeCaseId);
      else localStorage.removeItem(LS_ACTIVE_CASE);
    } catch {
      /* storage unavailable - restore is best-effort */
    }
  }, [activeCaseId]);

  // If the active Case was archived/deleted, clear local active state so the
  // map / chat reset cleanly. (case-list frame is the source of truth.) We
  // skip this check when `cases` is empty — case-open can arrive before
  // case-list (or independently of it in unit tests / dev injection), and
  // an empty list MUST NOT race-clear the active Case. The case-list frame
  // that follows will canonicalize on its own.
  useEffect(() => {
    if (activeCaseId === null) return;
    if (cases.length === 0) return;
    const found = cases.find((c) => c.case_id === activeCaseId);
    if (!found || found.status !== "active") {
      setActiveCaseId(null);
      setActiveSession(null);
    }
  }, [cases, activeCaseId]);

  // --- Persistence state derivation -------------------------------------- //
  const persistenceState: PersistenceState = useMemo(() => {
    if (!isSignedIn) return "anonymous";
    if (inFlight > 0) return "saving";
    return "saved";
  }, [isSignedIn, inFlight]);

  return {
    cases,
    activeCaseId,
    restoredActiveCaseId: restoredActiveCaseIdRef.current,
    activeSession,
    persistenceState,
    casesSettled,
    onCaseList,
    onCaseOpen,
    createCase,
    selectCase,
    renameCase,
    archiveCase,
    deleteCase,
    clearActive,
  };
}
