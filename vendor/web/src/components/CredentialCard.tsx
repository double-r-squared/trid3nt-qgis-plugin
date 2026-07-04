// GRACE-2 web — CredentialCard (credential-request flow; SRS §F.3 amendment).
//
// Inline Claude Code-styled chat card the user sees when a keyed tool dispatch
// pauses on a missing/invalid API key. The agent emits a `credential-request`
// envelope (server -> client) naming the provider + the canonical secret key
// it needs + a self-serve signup URL; Chat.tsx subscribes to that envelope and
// renders one of these cards inline in the conversation scroll.
//
// The card surfaces three affordances:
//   1. A SIGNUP LINK — opens `signup_url` in a NEW TAB (rel="noopener
//      noreferrer") so the user can obtain a key. Hidden when the provider has
//      no self-serve URL (`signup_url == null`).
//   2. A KEY-ENTRY form — `<input type="password">` + Save. On Save the
//      consumer (Chat.tsx) saves the key via the EXISTING `secret-add` path
//      (the only envelope that ever carries a raw key value — Decision F) and
//      then signals the agent to retry the paused tool via
//      `credential-provided` (echoing the request_id).
//   3. A "Not now" decline affordance — the consumer emits `credential-provided`
//      with `provided: false` so the agent narrates honestly and abandons the
//      paused tool (no silent dead-end, no hallucinated success).
//
// Security (mirrors SecretsPanel / Decision F):
//   - the key field is `<input type="password">` to suppress shoulder-surfing
//     and the wrong password-manager autofill.
//   - the key value is cleared from local state IMMEDIATELY after Save.
//   - the key value is NEVER logged, persisted to localStorage, or echoed in
//     the DOM after Save (the input is reset).
//
// This is a pure presentation + local-form-state component: it owns the
// transient keystroke buffer only and emits `onSave(keyValue)` / `onDecline()`
// callbacks. All WebSocket side effects (secret-add, credential-provided) live
// in the consumer (Chat.tsx).
//
// Inline-card family (NATE 2026-06-17 live test): the card sits INLINE in the
// chat scroll in arrival order, exactly like the PipelineCard tool cards (the
// consumer interleaves it by first-arrival seq). It has two visual modes:
//   - PENDING  → the full form (provider header + message + signup link + key
//                field + Save / Not now). Blue accent reads "action-needed".
//   - RESOLVED → it FOLDS into a COMPACT one-line summary card matching the
//                PipelineCard terminal chrome (green-tinted "saved" / muted
//                "declined", a leading state icon, a chevron to re-expand). The
//                agent's subsequent narration then resumes AFTER it in the
//                stream. Folding mirrors how a finished tool card collapses so
//                the credential card reads as part of the same card system.
//
// No raw glyphs / emoji — every icon comes from the shared icons module per
// the project UI policy.

import { useState } from "react";
import { CredentialRequestPayload } from "../contracts";
import {
  IconKey,
  IconArrowRight,
  IconCheck,
  IconChevronDown,
  IconChevronRight,
} from "./icons";

// --- Props --------------------------------------------------------------- //

export interface CredentialCardProps {
  /** The originating credential-request envelope. */
  request: CredentialRequestPayload;
  /**
   * Resolved state of this prompt. When set, the WHOLE card folds into a
   * compact one-line summary ("NASA FIRMS key saved" / "credential declined")
   * matching the PipelineCard terminal chrome — the form + signup link + agent
   * message all collapse away so a resolved prompt cannot be re-submitted and
   * the subsequent narration resumes after it. A chevron re-expands the full
   * card (read-only — the form stays gone once resolved).
   * `null`/undefined = still active (full form shown).
   */
  resolved?: "saved" | "declined" | null;
  /**
   * Save callback. Receives the raw key value the user typed. The consumer
   * routes it through the existing `secret-add` path then emits
   * `credential-provided` (provided=true). The card clears its own key state
   * immediately after invoking this.
   */
  onSave: (keyValue: string) => void;
  /**
   * Decline callback. The consumer emits `credential-provided`
   * (provided=false) so the agent narrates honestly + abandons the tool.
   */
  onDecline: () => void;
}

// --- Styles -------------------------------------------------------------- //
//
// Mirror the InlineChatCard visual language (semi-transparent surface, soft
// shadow, variant accent on the left edge) so the credential card sits in the
// same family as the other inline cards. The blue accent reads "info /
// action-needed" rather than "warning / danger".

const ACCENT = "#3b82f6"; // blue — matches InlineChatCard "info" variant

// Terminal tints mirror PipelineCard.cardVisual() so the folded credential card
// reads as the SAME inline-card family as a finished tool card.
const SAVED_TINT = "rgba(40, 200, 100, 0.18)"; // PipelineCard "complete" green
const SAVED_TEXT = "#10b981";
const DECLINED_TINT = "rgba(255,255,255,0.06)"; // muted neutral (skipped)
const DECLINED_TEXT = "#9ca3af";

const cardStyle: React.CSSProperties = {
  background: "rgba(28,28,34,0.92)",
  border: "1px solid rgba(255,255,255,0.07)",
  borderLeft: `3px solid ${ACCENT}`,
  borderRadius: 8,
  boxShadow: "0 4px 14px rgba(0,0,0,0.35)",
  color: "#e5e7eb",
  padding: "10px 12px",
  display: "flex",
  flexDirection: "column",
  gap: 8,
  fontSize: 12,
  lineHeight: 1.45,
  fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
  width: "100%",
  boxSizing: "border-box",
};

// Compact (folded) card — one-line summary chrome matching a terminal
// PipelineCard (tinted background, no left accent, soft shadow, single row).
function compactCardStyle(resolved: "saved" | "declined"): React.CSSProperties {
  return {
    display: "flex",
    // Column so the expanded detail (body) stacks UNDERNEATH the title row
    // instead of beside it. The title row is its own inner flex-row (icon +
    // summary + chevron); the read-only detail flows below it on expand.
    // job-0352 — body-under-title is the standard for expandable cards.
    flexDirection: "column",
    alignItems: "stretch",
    gap: 6,
    fontSize: 12,
    lineHeight: 1.4,
    padding: "8px 10px",
    borderRadius: 6,
    background: resolved === "saved" ? SAVED_TINT : DECLINED_TINT,
    boxShadow: "0 1px 3px rgba(0,0,0,0.25)",
    color: "#e5e7eb",
    fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
    width: "100%",
    boxSizing: "border-box",
    transition: "background-color 200ms ease-in-out",
  };
}

const inputStyle: React.CSSProperties = {
  background: "rgba(40,40,50,0.9)",
  border: "1px solid rgba(255,255,255,0.14)",
  borderRadius: 8,
  color: "#ddd",
  padding: "6px 8px",
  fontSize: 12,
  fontFamily: "inherit",
  width: "100%",
  boxSizing: "border-box",
};

const signupLinkStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 4,
  color: ACCENT,
  fontSize: 12,
  fontWeight: 600,
  textDecoration: "none",
};

function btnStyle(
  tone: "primary" | "muted",
  disabled: boolean,
): React.CSSProperties {
  const base: React.CSSProperties = {
    border: "1px solid transparent",
    borderRadius: 6,
    padding: "6px 12px",
    fontSize: 12,
    fontWeight: 600,
    cursor: disabled ? "default" : "pointer",
    fontFamily: "inherit",
    lineHeight: 1.2,
    display: "inline-flex",
    alignItems: "center",
    gap: 5,
    transition: "background 0.12s ease, border-color 0.12s ease",
  };
  if (disabled) {
    return {
      ...base,
      background: "rgba(255,255,255,0.04)",
      color: "#555",
      borderColor: "#333",
    };
  }
  if (tone === "primary") {
    return { ...base, background: ACCENT, color: "#0b0b0e", borderColor: ACCENT };
  }
  // muted: text-only quietest affordance
  return {
    ...base,
    background: "transparent",
    color: "#9ca3af",
    borderColor: "transparent",
    fontWeight: 500,
  };
}

// --- Component ----------------------------------------------------------- //

export function CredentialCard({
  request,
  resolved,
  onSave,
  onDecline,
}: CredentialCardProps): JSX.Element {
  const [keyValue, setKeyValue] = useState<string>("");
  // Folded-card re-expand toggle. A resolved card folds to the compact summary
  // by default; the chevron reveals the original (read-only) detail below it.
  const [expanded, setExpanded] = useState<boolean>(false);
  const isResolved = resolved === "saved" || resolved === "declined";
  const inputId = `credential-key-${request.request_id}`;

  function handleSubmit(e: React.FormEvent<HTMLFormElement>): void {
    e.preventDefault();
    if (isResolved) return;
    const trimmed = keyValue.trim();
    if (!trimmed) return;
    // Security: clear the key from local state IMMEDIATELY after handing it
    // to the consumer (Decision F — key never lingers in the DOM / state).
    onSave(keyValue);
    setKeyValue("");
  }

  // --- Folded (resolved) compact card ------------------------------------ //
  //
  // Once the user saves / declines, the WHOLE card collapses to a single
  // tool-card-style line: a state icon, a one-line summary, and a chevron to
  // re-expand the (read-only) detail. The form + signup link + agent message
  // are gone — the prompt cannot be re-submitted and the subsequent narration
  // resumes after this compact card in the stream.
  if (isResolved) {
    const saved = resolved === "saved";
    const summary = saved
      ? `${request.provider_label} key saved`
      : `${request.provider_label} credential declined`;
    const summaryColor = saved ? SAVED_TEXT : DECLINED_TEXT;
    return (
      <div
        data-testid={`credential-card-${request.request_id}`}
        data-provider={request.provider_id}
        data-resolved={resolved}
        data-variant="compact"
        role="region"
        aria-label={`${request.provider_label} API key ${summary}`}
        style={compactCardStyle(resolved)}
      >
        <div
          data-testid={`credential-card-resolved-${request.request_id}`}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            width: "100%",
          }}
        >
          <span
            aria-hidden="true"
            style={{ display: "inline-flex", alignItems: "center", flexShrink: 0 }}
          >
            {saved ? (
              <IconCheck size={13} color={SAVED_TEXT} />
            ) : (
              <IconKey size={13} color={DECLINED_TEXT} />
            )}
          </span>
          <span
            style={{
              flex: 1,
              color: summaryColor,
              fontWeight: 600,
              fontSize: 12,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
            title={summary}
          >
            {summary}
          </span>
          {/* Chevron re-expands the original detail (read-only). */}
          <button
            type="button"
            data-testid={`credential-card-expand-${request.request_id}`}
            aria-label={expanded ? "Collapse details" : "Show details"}
            aria-expanded={expanded}
            onClick={() => setExpanded((v) => !v)}
            style={{
              background: "transparent",
              border: "none",
              padding: 2,
              margin: 0,
              cursor: "pointer",
              display: "inline-flex",
              alignItems: "center",
              color: "#9ca3af",
              flexShrink: 0,
            }}
          >
            {expanded ? (
              <IconChevronDown size={13} color="#9ca3af" />
            ) : (
              <IconChevronRight size={13} color="#9ca3af" />
            )}
          </button>
        </div>
        {expanded && (
          <div
            data-testid={`credential-card-detail-${request.request_id}`}
            style={{
              width: "100%",
              marginTop: 6,
              paddingTop: 6,
              borderTop: "1px solid rgba(255,255,255,0.08)",
              color: "#d1d5db",
              fontSize: 11,
              lineHeight: 1.5,
              display: "flex",
              flexDirection: "column",
              gap: 4,
            }}
          >
            <div style={{ wordBreak: "break-word" }}>{request.message}</div>
            <div style={{ color: "#9ca3af" }}>Key name: {request.secret_key_name}</div>
          </div>
        )}
      </div>
    );
  }

  // --- Active (pending) full form ---------------------------------------- //
  return (
    <div
      data-testid={`credential-card-${request.request_id}`}
      data-provider={request.provider_id}
      role="region"
      aria-label={`${request.provider_label} API key needed`}
      style={cardStyle}
    >
      {/* Header row: key icon + provider title */}
      <div style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
        <span
          aria-hidden="true"
          style={{
            color: ACCENT,
            flexShrink: 0,
            marginTop: 1,
            display: "inline-flex",
            alignItems: "center",
          }}
        >
          <IconKey size={14} color={ACCENT} />
        </span>
        <strong
          data-testid={`credential-card-title-${request.request_id}`}
          style={{
            fontSize: 13,
            fontWeight: 600,
            color: "#f3f4f6",
            flex: 1,
            wordBreak: "break-word",
          }}
        >
          {request.provider_label} needs an API key
        </strong>
      </div>

      {/* Agent's user-facing explanation. */}
      <div
        data-testid={`credential-card-message-${request.request_id}`}
        style={{
          color: "#d1d5db",
          fontSize: 12,
          lineHeight: 1.5,
          wordBreak: "break-word",
        }}
      >
        {request.message}
      </div>

      {/* Signup link — opens in a NEW TAB. Hidden when no self-serve URL. */}
      {request.signup_url && (
        <a
          data-testid={`credential-card-signup-${request.request_id}`}
          href={request.signup_url}
          target="_blank"
          rel="noopener noreferrer"
          style={signupLinkStyle}
        >
          Get a {request.provider_label} key
          <IconArrowRight size={12} color={ACCENT} />
        </a>
      )}

      {/* Key-entry form + actions. (Resolved prompts fold into the compact
          card above via the early return — the form is never reachable once
          saved / declined, so it can't be re-submitted.) */}
      <form
        data-testid={`credential-card-form-${request.request_id}`}
        onSubmit={handleSubmit}
        autoComplete="off"
        style={{ display: "flex", flexDirection: "column", gap: 8 }}
      >
        <label htmlFor={inputId} style={{ fontSize: 11, color: "#aaa" }}>
          {request.secret_key_name}
        </label>
        <input
          id={inputId}
          data-testid={`credential-card-input-${request.request_id}`}
          // Security: type=password suppresses shoulder-surfing;
          // autocomplete=new-password keeps managers from filling the
          // wrong saved credential.
          type="password"
          autoComplete="new-password"
          value={keyValue}
          onChange={(e) => setKeyValue(e.target.value)}
          placeholder={`Paste your ${request.provider_label} API key`}
          maxLength={2048}
          style={inputStyle}
        />
        {/* Action row: Save (primary) + Not now (muted). Wraps on narrow
            viewports so the card stays mobile-friendly. */}
        <div
          style={{
            display: "flex",
            gap: 6,
            flexWrap: "wrap",
            marginTop: 2,
          }}
        >
          <button
            type="submit"
            data-testid={`credential-card-save-${request.request_id}`}
            aria-label={`Save ${request.provider_label} API key`}
            disabled={keyValue.trim().length === 0}
            style={btnStyle("primary", keyValue.trim().length === 0)}
          >
            <IconCheck
              size={12}
              color={keyValue.trim().length === 0 ? "#555" : "#0b0b0e"}
            />
            Save
          </button>
          <button
            type="button"
            data-testid={`credential-card-decline-${request.request_id}`}
            aria-label="Skip this credential"
            onClick={onDecline}
            style={btnStyle("muted", false)}
          >
            Not now
          </button>
        </div>
      </form>
    </div>
  );
}
