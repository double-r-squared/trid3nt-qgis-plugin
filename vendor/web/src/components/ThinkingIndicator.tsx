// GRACE-2 web — Ephemeral "Thinking…" indicator (wave-4-10 thinking-state job).
//
// Memory spec `feedback_thinking_state_ephemeral` (2026-06-09):
//
//   "Thinking state should lose the box and should always be at the end of
//    the chat and when it's 'green' it should disappear."
//
// Canonical pattern across ChatGPT / Claude / Gemini / Cursor / Perplexity / v0:
//   - No card chrome (no border, no background tint)
//   - Italic muted-gray text ("Thinking…")
//   - Subtle pulse / shimmer animation (~1.6s)
//   - Pinned to the BOTTOM of the chat scroll (positionally last)
//   - Vanishes the moment real content arrives — text bubble streams in,
//     non-thinking tool card lands, or pipeline terminates
//
// This is a presence indicator, not a history record. Once the model emits
// content, the indicator's job is done and it unmounts. Tool dispatch cards
// keep their own lifecycle visuals (per `feedback_pipeline_card_visual_states`);
// this exception only applies to the Gemini "llm_generation" step.
//
// Pure presentational — visibility is owned by the parent (Chat.tsx) which
// computes `active` from the pipeline view-model + the streaming message
// state. If `active === false`, the component renders nothing.

import { CSSProperties } from "react";

// --- Reduced-motion detection (SSR-safe) --------------------------------- //

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

// --- Keyframes (mounted once) -------------------------------------------- //
//
// Subtle opacity pulse, 1.6s ease-in-out. Synchronized with the rest of the
// chat-stream's quiet animation language (rainbow gradient is 3s, spinner is
// 1s; this sits in between and reads as "ambient breath" rather than
// attention-grabbing motion).

const KEYFRAMES_ID = "grace2-thinking-indicator-keyframes";

function ensureKeyframes(): void {
  if (typeof document === "undefined") return;
  if (document.getElementById(KEYFRAMES_ID)) return;
  const style = document.createElement("style");
  style.id = KEYFRAMES_ID;
  style.textContent = `
@keyframes grace2-thinking-pulse {
  0%   { opacity: 0.55; }
  50%  { opacity: 1.00; }
  100% { opacity: 0.55; }
}
`;
  document.head.appendChild(style);
}

ensureKeyframes();

// --- Component ----------------------------------------------------------- //

export interface ThinkingIndicatorProps {
  /**
   * Whether the indicator should render. Owned by the parent — true while a
   * Gemini "llm_generation" step is in pending/running state AND no agent
   * text chunk has streamed in / no other tool card has landed yet. False
   * the moment any of those conditions terminate.
   */
  active: boolean;
}

export function ThinkingIndicator({
  active,
}: ThinkingIndicatorProps): JSX.Element | null {
  if (!active) return null;
  const reduced = prefersReducedMotion();

  // Italic muted-gray text matching the chat's design language. The muted
  // color is the same #888 used elsewhere in Chat.tsx (empty-state hint
  // line + agent cursor glyph) so the indicator reads as part of the same
  // typographic system.
  //
  // No card chrome:
  //   - background: transparent  (no panel tint)
  //   - border: none             (no left-edge accent, no outline)
  //   - padding: 0               (no card inset — matches AgentMessage)
  //
  // Margin matches the chat-stream's 14px gap so the indicator doesn't feel
  // cramped against the row above it. (The parent scroll uses gap:14 via
  // flex column; this margin is applied via the parent rather than here, so
  // we just emit a single inline-block span and let the column gap handle
  // separation.)
  const style: CSSProperties = {
    color: "#888",
    fontStyle: "italic",
    fontSize: 13,
    lineHeight: 1.5,
    // Reduced-motion: static muted color, no opacity animation.
    animation: reduced
      ? undefined
      : "grace2-thinking-pulse 1.6s ease-in-out infinite",
    // Pure text — no surrounding chrome.
    background: "transparent",
    border: "none",
    padding: 0,
    margin: 0,
    // Match the chat's body font (system-ui) rather than the monospace stack
    // PipelineCard uses; italic in a serif/system font reads as the "thinking
    // aloud" idiom (ChatGPT / Claude convention).
    fontFamily: "system-ui, sans-serif",
  };

  return (
    <div
      data-testid="thinking-indicator"
      data-active="true"
      role="status"
      aria-live="polite"
      style={style}
    >
      Thinking
      <EllipsisGlyph reduced={reduced} />
    </div>
  );
}

// --- Ellipsis glyph ------------------------------------------------------ //
//
// Use the horizontal ellipsis character (…) rather than three dots so it
// renders consistently across fonts. Kept as a separate component for
// clarity; no animation of its own (the parent's opacity pulse covers it).

function EllipsisGlyph({ reduced: _reduced }: { reduced: boolean }): JSX.Element {
  return <span aria-hidden="true">…</span>;
}
