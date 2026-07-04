// GRACE-2 web — SourceSuggestionInline (job-0145, sprint-12-mega Wave 4).
//
// Inline Claude Code-styled chat card the user sees when the server's
// candidate detector finds a useful data source on a page the agent just
// fetched. Replaces the prior Mode2OfferModal — same envelope subscription
// (the server-side `mode2-candidate` wire name is internal), but the user
// surface is now an inline message-shaped card in the chat column rather
// than a full-screen modal + corner toasts.
//
// Discipline (kickoff §2):
//   - NO user-visible reference to "Mode 2", "Mode 1", "Tier 1/2", or
//     "OQ-*". The card translates the internal labels to plain language.
//   - Confidence shown as a percentage (`70% match`), never a raw decimal.
//   - `detected_patterns` server tokens are translated to user-friendly
//     phrases (see `PATTERN_PHRASES` below).
//   - Actions: "Add data source" / "Maybe later" / "Don't suggest this
//     domain again". Suppression persists per-domain via localStorage.
//
// Subscription model:
//   The parent (App.tsx) registers a setter that pushes incoming candidate
//   envelopes into the card's queue (same seam as Mode2OfferModal had). The
//   card iterates the queue and renders one inline card per active
//   candidate. Suppressed-domain candidates never surface. Each action
//   surfaces an audit event upstream + drops the card from the queue.

import { useCallback, useEffect, useState } from "react";
import {
  SourceCandidate,
  SourceCandidatePayload,
  isSuppressed,
  suppressDomain,
} from "../lib/source_suggestion_suppression";
import { InlineChatCard, InlineChatCardAction } from "./InlineChatCard";

// --- Public types -------------------------------------------------------- //

/** Action emitted to the parent so it can route to ws.ts + audit log. */
export type SourceSuggestionAction =
  | { kind: "add"; candidate: SourceCandidate }
  | { kind: "dismiss"; candidate: SourceCandidate }
  | { kind: "suppress"; candidate: SourceCandidate };

export interface SourceSuggestionInlineProps {
  /**
   * Subscription seam — the parent (App.tsx) registers a setter that pushes
   * incoming candidate envelopes into the card's queue. Returns an
   * unsubscribe function.
   */
  subscribeCandidate: (
    cb: (p: SourceCandidatePayload) => void,
  ) => () => void;

  /**
   * Action callback the parent uses to (a) emit the add-confirmed envelope
   * upstream and (b) write an audit-log envelope.
   */
  onAction: (action: SourceSuggestionAction) => void;
}

// --- User-friendly translation tables ----------------------------------- //
//
// Translates server-internal `detected_patterns` tokens into plain-language
// phrases the user can act on. Unknown tokens are silently dropped (we
// would rather show fewer chips than expose internal jargon).
//
// Keep this list ordered by descending user value: when we cap the rendered
// pattern list, we render the top N from this table.

const PATTERN_PHRASES: Record<string, string> = {
  "json-ld": "Has machine-readable metadata",
  "schema-org": "Has structured metadata",
  "data-download-link": "Offers data downloads",
  "openapi-spec-link": "Has a documented API",
  "rest-endpoint-pattern": "Looks like a REST API",
  "tabular-data": "Contains tabular data",
  "csv-download": "Offers CSV downloads",
  "geojson-link": "Offers map-ready data",
  "wms-endpoint": "Offers a map tile service",
  "wfs-endpoint": "Offers a vector feature service",
  "ckan-portal": "Looks like a data portal",
  "arcgis-rest": "Offers an ArcGIS feature service",
  "dataset-landing-page": "Looks like a dataset landing page",
};

function translatePatterns(tokens: string[]): string[] {
  const out: string[] = [];
  for (const t of tokens) {
    const phrase = PATTERN_PHRASES[t];
    if (phrase && !out.includes(phrase)) {
      out.push(phrase);
    }
  }
  // Cap at 3 — the kickoff scopes the surface to "2-3 detected capabilities".
  return out.slice(0, 3);
}

// --- Component ----------------------------------------------------------- //

export function SourceSuggestionInline({
  subscribeCandidate,
  onAction,
}: SourceSuggestionInlineProps): JSX.Element | null {
  // Queue of active candidates. We render one card per active candidate so a
  // multi-source burst (rare but possible) does not silently drop entries.
  const [queue, setQueue] = useState<SourceCandidate[]>([]);

  // Subscribe to candidate emissions; drop suppressed domains entirely.
  useEffect(() => {
    const unsub = subscribeCandidate((p) => {
      const c = p.candidate;
      if (!c) return;
      if (isSuppressed(c.domain)) {
        // eslint-disable-next-line no-console
        console.debug(
          `[source-suggestion] suppressed ${c.domain} (candidate ${c.candidate_id})`,
        );
        return;
      }
      setQueue((cur) => {
        // Dedupe by candidate_id so a duplicate emit doesn't stack.
        if (cur.some((t) => t.candidate_id === c.candidate_id)) return cur;
        return [...cur, c];
      });
    });
    return unsub;
  }, [subscribeCandidate]);

  const dropCandidate = useCallback((candidateId: string) => {
    setQueue((cur) => cur.filter((t) => t.candidate_id !== candidateId));
  }, []);

  const handleAdd = useCallback(
    (c: SourceCandidate) => {
      onAction({ kind: "add", candidate: c });
      dropCandidate(c.candidate_id);
    },
    [onAction, dropCandidate],
  );

  const handleDismiss = useCallback(
    (c: SourceCandidate) => {
      onAction({ kind: "dismiss", candidate: c });
      dropCandidate(c.candidate_id);
    },
    [onAction, dropCandidate],
  );

  const handleSuppress = useCallback(
    (c: SourceCandidate) => {
      suppressDomain(c.domain);
      onAction({ kind: "suppress", candidate: c });
      dropCandidate(c.candidate_id);
    },
    [onAction, dropCandidate],
  );

  if (queue.length === 0) return null;

  return (
    <div
      data-testid="source-suggestion-stack"
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      {queue.map((c) => (
        <SourceSuggestionCard
          key={c.candidate_id}
          candidate={c}
          onAdd={() => handleAdd(c)}
          onDismiss={() => handleDismiss(c)}
          onSuppress={() => handleSuppress(c)}
        />
      ))}
    </div>
  );
}

// --- One-card subcomponent ---------------------------------------------- //

interface SourceSuggestionCardProps {
  candidate: SourceCandidate;
  onAdd: () => void;
  onDismiss: () => void;
  onSuppress: () => void;
}

function SourceSuggestionCard({
  candidate,
  onAdd,
  onDismiss,
  onSuppress,
}: SourceSuggestionCardProps): JSX.Element {
  const capabilities = translatePatterns(candidate.detected_patterns);
  const confidencePct = Math.round(candidate.confidence * 100);

  const actions: InlineChatCardAction[] = [
    {
      label: "Add data source",
      onClick: onAdd,
      tone: "primary",
      testId: `source-suggestion-add-${candidate.candidate_id}`,
    },
    {
      label: "Maybe later",
      onClick: onDismiss,
      tone: "secondary",
      testId: `source-suggestion-dismiss-${candidate.candidate_id}`,
    },
    {
      label: "Don't suggest this domain again",
      onClick: onSuppress,
      tone: "muted",
      testId: `source-suggestion-suppress-${candidate.candidate_id}`,
    },
  ];

  const body = (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {/* Domain + (optional) page title */}
      <div
        data-testid={`source-suggestion-domain-${candidate.candidate_id}`}
        style={{
          fontFamily:
            'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
          fontSize: 11,
          color: "#9ca3af",
          wordBreak: "break-all",
        }}
      >
        {candidate.domain}
      </div>
      {candidate.title && (
        <div
          data-testid={`source-suggestion-title-${candidate.candidate_id}`}
          style={{ color: "#e5e7eb", fontSize: 12, fontWeight: 500 }}
        >
          {candidate.title}
        </div>
      )}

      {/* Detected capabilities (translated) — at most 3 chips. */}
      {capabilities.length > 0 && (
        <div
          data-testid={`source-suggestion-capabilities-${candidate.candidate_id}`}
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: 4,
            marginTop: 2,
          }}
        >
          {capabilities.map((phrase) => (
            <span
              key={phrase}
              style={{
                background: "rgba(255,255,255,0.06)",
                border: "1px solid rgba(255,255,255,0.08)",
                borderRadius: 999,
                padding: "2px 8px",
                fontSize: 11,
                color: "#d1d5db",
                whiteSpace: "nowrap",
              }}
            >
              {phrase}
            </span>
          ))}
        </div>
      )}

      {/* Snippet (optional) — a short excerpt of the detected page. */}
      {candidate.snippet && (
        <div
          data-testid={`source-suggestion-snippet-${candidate.candidate_id}`}
          style={{
            marginTop: 4,
            padding: "6px 8px",
            background: "rgba(0,0,0,0.25)",
            border: "1px solid rgba(255,255,255,0.05)",
            borderRadius: 4,
            fontFamily:
              'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
            fontSize: 11,
            color: "#9ca3af",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            maxHeight: 80,
            overflowY: "auto",
          }}
        >
          {candidate.snippet}
        </div>
      )}

      {/* Confidence — rendered as a subtle percentage match. */}
      <div
        data-testid={`source-suggestion-confidence-${candidate.candidate_id}`}
        style={{ marginTop: 2, fontSize: 11, color: "#6b7280" }}
      >
        {confidencePct}% match
      </div>
    </div>
  );

  return (
    <InlineChatCard
      variant="info"
      title="Found a useful data source you might want to add"
      body={body}
      actions={actions}
      testId={`source-suggestion-inline-${candidate.candidate_id}`}
      ariaLabel="Data source suggestion"
    />
  );
}
