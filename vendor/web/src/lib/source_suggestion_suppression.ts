// GRACE-2 web — source-suggestion client-side types + per-domain suppression
// (job-0145, sprint-12-mega Wave 4).
//
// This module replaces the prior `mode2_suppression.ts` — the wire envelope
// name (`mode2-candidate`) remains the server-side internal identifier, but
// every surface the user sees calls these "data source suggestions". This
// module:
//   1. Mirrors the server's candidate payload shape locally (canonical
//      pydantic envelope still tracked as OQ-0126-MODE2-ADD-CONFIRMED-SCHEMA).
//   2. Provides the localStorage-backed "Don't suggest again for this
//      domain" suppression list that backs the SourceSuggestionInline card.
//
// Wire-shape note: the envelope_type label `mode2-candidate` is INTERNAL.
// User-facing strings never reference it; see SourceSuggestionInline for the
// surface translation. Renaming the wire field would be a schema concern and
// is intentionally left untouched here (pre-MVP scope rule does not require
// us to break the bidirectional contract just to rename a key).

// --- Types: source candidate ------------------------------------------- //

/** TLD bucket the server classifier reports. Wire-shape mirror; the UI
 *  translates these to user-friendly labels when shown. */
export type SourceCandidateTLD = "gov" | "edu" | "mil" | "int" | "other";

/**
 * Hint the server classifier emits for the suggested catalog entry kind.
 * Wire-shape mirror.
 */
export type SourceSuggestedKind = "fetcher" | "endpoint" | "reference";

/**
 * Wire-shape mirror of the server-side candidate. Field-by-field mirror.
 * Adding a field server-side requires mirroring it here.
 */
export interface SourceCandidate {
  candidate_id: string;
  url: string;
  domain: string;
  domain_tld: SourceCandidateTLD;
  confidence: number;
  detected_patterns: string[];
  title: string | null;
  suggested_tool_kind: SourceSuggestedKind;
  snippet: string | null;
}

/**
 * Wire-shape mirror of the candidate envelope payload. Keeps the legacy
 * `envelope_type` literal so the server-side router does not need to change.
 */
export interface SourceCandidatePayload {
  envelope_type?: "mode2-candidate";
  candidate: SourceCandidate;
}

/**
 * Outbound "add this data source" wire shape. Mirrors the prior
 * `mode2-add-confirmed` envelope; the field set is unchanged so the server
 * receiver does not need to bump.
 */
export interface SourceAddConfirmedPayload {
  envelope_type?: "mode2-add-confirmed";
  candidate_id: string;
  url: string;
  domain: string;
  suggested_tool_kind: SourceSuggestedKind;
}

/**
 * Audit-event envelope payload. One emitted per surface display and per
 * user action.
 */
export type SourceAuditAction =
  | "display-modal"
  | "display-toast"
  | "display-inline"
  | "add"
  | "dismiss"
  | "suppress";

export interface SourceAuditEventPayload {
  envelope_type?: "mode2-audit-event";
  candidate_id: string;
  domain: string;
  action: SourceAuditAction;
  confidence: number;
  surface: "modal" | "toast" | "inline";
}

// --- Suppression list (localStorage) ------------------------------------ //
//
// Backs the "Don't suggest this domain again" affordance on
// SourceSuggestionInline.
//
// Storage shape: a JSON-serialized array of lowercase host strings under
// ``grace2.source_suggestion_suppressed_domains``. Lowercase normalization
// happens on add and on read so callers can pass mixed-case hosts without
// surprises.
//
// localStorage may be disabled (privacy mode); every read/write is wrapped
// in a try/catch so the card degrades to "always suggest" rather than
// crashing.
//
// Backward compatibility: callers that previously imported
// `mode2_suppression` continue to work via the re-export shim (see bottom
// of file) until the orchestrator opens a follow-up rename job.

const STORAGE_KEY = "grace2.source_suggestion_suppressed_domains";
// Legacy key kept for one cycle so users who suppressed under the old
// implementation are not asked again. Migration is "merge legacy into new
// on first read, then write back" — idempotent and contained to the read
// path so we don't write on hot paths.
const LEGACY_STORAGE_KEY = "grace2.mode2_suppressed_domains";

function readSuppressionList(): string[] {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const legacyRaw = window.localStorage.getItem(LEGACY_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    const legacyParsed = legacyRaw ? JSON.parse(legacyRaw) : [];
    const merged = new Set<string>();
    if (Array.isArray(parsed)) {
      parsed
        .filter((d): d is string => typeof d === "string")
        .forEach((d) => merged.add(d.toLowerCase()));
    }
    if (Array.isArray(legacyParsed)) {
      legacyParsed
        .filter((d): d is string => typeof d === "string")
        .forEach((d) => merged.add(d.toLowerCase()));
    }
    return Array.from(merged);
  } catch {
    return [];
  }
}

function writeSuppressionList(domains: string[]): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(domains));
  } catch {
    // localStorage unavailable; silently degrade — the next isSuppressed call
    // will return false (surfacing the card again is strictly less harmful
    // than throwing).
  }
}

/** Return true if ``domain`` (case-insensitive) is on the suppression list. */
export function isSuppressed(domain: string): boolean {
  const host = domain.toLowerCase();
  return readSuppressionList().includes(host);
}

/** Add ``domain`` to the suppression list. Idempotent — duplicate calls are a no-op. */
export function suppressDomain(domain: string): void {
  const host = domain.toLowerCase();
  const current = readSuppressionList();
  if (current.includes(host)) return;
  writeSuppressionList([...current, host]);
}

/** Remove ``domain`` from the suppression list (settings/reset hook). */
export function unsuppressDomain(domain: string): void {
  const host = domain.toLowerCase();
  const next = readSuppressionList().filter((d) => d !== host);
  writeSuppressionList(next);
}

/** Return the current list of suppressed domains (lowercase, copied). */
export function listSuppressed(): string[] {
  return [...readSuppressionList()];
}

/** Clear all suppressions. Test-only / future settings surface. */
export function clearSuppressions(): void {
  writeSuppressionList([]);
  try {
    window.localStorage.removeItem(LEGACY_STORAGE_KEY);
  } catch {
    // ignore
  }
}

/** Exposed for tests so they can isolate the storage key in setup/teardown. */
export const SOURCE_SUGGESTION_SUPPRESSION_STORAGE_KEY = STORAGE_KEY;
