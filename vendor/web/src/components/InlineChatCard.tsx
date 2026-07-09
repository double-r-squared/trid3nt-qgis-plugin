// GRACE-2 web — InlineChatCard (job-0145, sprint-12-mega Wave 4).
//
// Common primitive for Claude Code-style inline informational cards that
// render as message-like surfaces inside (or anchored over) the chat scroll.
// Replaces the ad-hoc per-warning styling that PayloadWarningInline and the
// former Mode2OfferModal carried independently.
//
// Visual language (kickoff §1, §3):
//   - Subtle semi-transparent background sitting over the chat panel
//   - Rounded corners + soft drop shadow
//   - Icon at left (variant-driven glyph)
//   - Title + body text + action row
//   - Dark-theme aware muted palette (matches Chat.tsx panel surface)
//   - Width matches chat message width, NOT full chat panel — see consumer
//
// Variants:
//   - "warning" — amber accent (payload-warning, soft-cap)
//   - "danger"  — red accent (payload-warning hard-cap; reserved for future)
//   - "info"    — blue accent (source suggestion / data-source detection)
//   - "success" — green accent (reserved; e.g. action confirmation)
//
// The card is a pure presentation primitive — it owns NO state and emits NO
// side effects. Consumers pass actions; consumers decide what happens on
// click. A11y roles default to `role="status"` (info/success/warning) or
// `role="alert"` (danger); consumers may override via the `role` prop.

import { CSSProperties, ReactNode, useEffect, useRef, useState } from "react";
import { IconWarning, IconInfo, IconCheck } from "./icons";
import type { IconProps } from "./icons";
import type { FC } from "react";

// --- Variant config ------------------------------------------------------ //

export type InlineChatCardVariant =
  | "warning"
  | "danger"
  | "info"
  | "success";

const VARIANT_ACCENT: Record<InlineChatCardVariant, string> = {
  warning: "#eab308", // amber
  danger: "#ef4444",  // red
  info: "#3b82f6",    // blue
  success: "#10b981", // green
};

// The leading glyph for each variant now comes from the shared icon module
// (Phosphor) rather than a raw unicode character, per the project UI policy.
const VARIANT_ICON: Record<InlineChatCardVariant, FC<IconProps>> = {
  warning: IconWarning,
  danger: IconWarning,
  info: IconInfo,
  success: IconCheck,
};

const VARIANT_ROLE: Record<InlineChatCardVariant, "status" | "alert"> = {
  warning: "status",
  danger: "alert",
  info: "status",
  success: "status",
};

// --- Action shape -------------------------------------------------------- //

/**
 * One action button in the card's action row. `tone` drives styling:
 *   - "primary"   — filled with the variant accent
 *   - "secondary" — subtle bordered button
 *   - "muted"     — text-only / quietest affordance
 *
 * `disabled` short-circuits onClick and applies the disabled visual state.
 */
export interface InlineChatCardAction {
  label: string;
  onClick: () => void;
  tone?: "primary" | "secondary" | "muted";
  disabled?: boolean;
  /** Optional stable test id for the button. */
  testId?: string;
  /** Optional ARIA label override (defaults to `label`). */
  ariaLabel?: string;
}

// --- Props --------------------------------------------------------------- //

export interface InlineChatCardProps {
  variant: InlineChatCardVariant;
  /** Card title — short, sentence-case. */
  title: string;
  /**
   * Body content. Plain string renders as a single paragraph; ReactNode
   * lets consumers compose richer layouts (metadata rows, snippets, chips)
   * while keeping the outer chrome consistent.
   */
  body?: string | ReactNode;
  /** Action buttons rendered in a row beneath the body. */
  actions?: InlineChatCardAction[];
  /**
   * Optional override for the leading icon (defaults to the variant icon from
   * the shared icon module). May be any ReactNode (e.g. a custom icon element)
   * or a string. Pass an empty string to suppress the icon entirely.
   */
  icon?: ReactNode;
  /** Optional footer ReactNode (e.g. "Sent: proceed"). */
  footer?: ReactNode;
  /** Stable test id for the outer card element. */
  testId?: string;
  /** ARIA role override (defaults derived from variant). */
  role?: "status" | "alert" | "region";
  /** Optional ARIA labelledby/describedby ids (consumer-managed). */
  ariaLabel?: string;
  /**
   * Optional extra data-* attributes spread onto the root element.
   * Use for consumer-specific identifiers (e.g. data-warning-id) that
   * don't belong in the shared card API.
   */
  extraAttrs?: Record<string, string>;
  /**
   * Gate UX (live-feedback 2026-07-09): when true, animate a brief amber
   * attention-pulse on mount so the card is visually distinct while the user
   * has not yet answered it. Pass true for unresolved gate cards (payload /
   * resolution / solver-confirm), false or undefined for resolved or non-gate
   * cards. Respects prefers-reduced-motion (pulses once on mount only; static
   * on reduced-motion).
   */
  highlight?: boolean;
}

// --- Style helpers ------------------------------------------------------- //

function btnStyle(
  tone: "primary" | "secondary" | "muted",
  accent: string,
  disabled: boolean,
): CSSProperties {
  const base: CSSProperties = {
    border: "1px solid transparent",
    borderRadius: 6,
    padding: "5px 10px",
    fontSize: 12,
    fontWeight: 600,
    cursor: disabled ? "default" : "pointer",
    fontFamily: "inherit",
    lineHeight: 1.2,
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
    return {
      ...base,
      background: accent,
      color: "#0b0b0e",
      borderColor: accent,
    };
  }
  if (tone === "secondary") {
    return {
      ...base,
      background: "rgba(255,255,255,0.05)",
      color: "#e5e7eb",
      borderColor: "#3f3f46",
    };
  }
  // muted: text-only, no visible border
  return {
    ...base,
    background: "transparent",
    color: "#9ca3af",
    borderColor: "transparent",
    fontWeight: 500,
  };
}

// --- Attention-pulse CSS (gate UX, live-feedback 2026-07-09) ------------ //
//
// A two-phase amber glow that fires once on mount when `highlight` is true.
// Phase 1 (0-0.6s): ramp the outline alpha up to 0.7 (attention peak).
// Phase 2 (0.6-1.2s): fade back out to 0 (settles invisible).
// The animation is fill-mode=forwards so it stays gone after completion.
// prefers-reduced-motion: the card mounts without any pulse.

const GATE_PULSE_CSS = `
@keyframes grace2-gate-pulse {
  0%   { box-shadow: 0 4px 14px rgba(0,0,0,0.35), 0 0 0 0 rgba(234,179,8,0); }
  40%  { box-shadow: 0 4px 14px rgba(0,0,0,0.35), 0 0 0 4px rgba(234,179,8,0.55); }
  100% { box-shadow: 0 4px 14px rgba(0,0,0,0.35), 0 0 0 8px rgba(234,179,8,0); }
}
@media (prefers-reduced-motion: reduce) {
  .grace2-gate-pulse { animation: none !important; }
}
`;

// --- Component ----------------------------------------------------------- //

/**
 * Claude Code-style inline informational card. Pure presentation; all state
 * + side effects live in the consumer.
 */
export function InlineChatCard({
  variant,
  title,
  body,
  actions,
  icon,
  footer,
  testId,
  role,
  ariaLabel,
  extraAttrs,
  highlight,
}: InlineChatCardProps): JSX.Element {
  const accent = VARIANT_ACCENT[variant];
  const ariaRole = role ?? VARIANT_ROLE[variant];

  // Gate pulse: active for 1.2s on mount, then cleared so the card settles.
  const [pulseActive, setPulseActive] = useState(highlight === true);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (!highlight) return;
    setPulseActive(true);
    timerRef.current = setTimeout(() => setPulseActive(false), 1200);
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
    // highlight changes when the card transitions from unresolved -> resolved;
    // we intentionally do NOT re-trigger on subsequent renders.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Icon resolution:
  //   - icon === ""        → suppress the icon entirely
  //   - icon provided      → render the consumer-supplied node/string
  //   - icon === undefined → default to the variant icon from the icon module
  const VariantIcon = VARIANT_ICON[variant];
  const showIcon = icon !== "";
  const iconContent: ReactNode =
    icon !== undefined ? icon : <VariantIcon size={14} color={accent} />;

  return (
    <>
      {pulseActive && <style>{GATE_PULSE_CSS}</style>}
    <div
      data-testid={testId ?? "inline-chat-card"}
      data-variant={variant}
      role={ariaRole}
      aria-label={ariaLabel}
      className={pulseActive ? "grace2-gate-pulse" : undefined}
      {...extraAttrs}
      style={{
        // Semi-transparent surface over the chat background; subtle border
        // tinted by the variant accent for at-a-glance categorization.
        background: "rgba(28,28,34,0.92)",
        border: `1px solid rgba(255,255,255,0.07)`,
        borderLeft: `3px solid ${accent}`,
        borderRadius: 8,
        boxShadow: "0 4px 14px rgba(0,0,0,0.35)",
        animation: pulseActive ? "grace2-gate-pulse 1.2s ease-out forwards" : undefined,
        color: "#e5e7eb",
        padding: "10px 12px",
        display: "flex",
        flexDirection: "column",
        gap: 8,
        fontSize: 12,
        lineHeight: 1.45,
        fontFamily:
          "system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
        width: "100%",
        boxSizing: "border-box",
      }}
    >
      {/* Header row: icon + title */}
      <div
        style={{
          display: "flex",
          alignItems: "flex-start",
          gap: 8,
        }}
      >
        {showIcon && (
          <span
            data-testid={`${testId ?? "inline-chat-card"}-icon`}
            aria-hidden="true"
            style={{
              color: accent,
              fontSize: 14,
              lineHeight: 1.2,
              flexShrink: 0,
              marginTop: 1,
              display: "inline-flex",
              alignItems: "center",
            }}
          >
            {iconContent}
          </span>
        )}
        <strong
          data-testid={`${testId ?? "inline-chat-card"}-title`}
          style={{
            fontSize: 13,
            fontWeight: 600,
            color: "#f3f4f6",
            flex: 1,
            wordBreak: "break-word",
          }}
        >
          {title}
        </strong>
      </div>

      {/* Body — string or arbitrary node */}
      {body !== undefined && body !== null && body !== "" && (
        <div
          data-testid={`${testId ?? "inline-chat-card"}-body`}
          style={{
            color: "#d1d5db",
            fontSize: 12,
            lineHeight: 1.5,
            wordBreak: "break-word",
          }}
        >
          {body}
        </div>
      )}

      {/* Action row */}
      {actions && actions.length > 0 && (
        <div
          data-testid={`${testId ?? "inline-chat-card"}-actions`}
          style={{
            display: "flex",
            gap: 6,
            flexWrap: "wrap",
            marginTop: 2,
          }}
        >
          {actions.map((a, idx) => {
            const tone = a.tone ?? (idx === 0 ? "primary" : "secondary");
            return (
              <button
                key={`${a.label}-${idx}`}
                type="button"
                data-testid={a.testId}
                aria-label={a.ariaLabel ?? a.label}
                onClick={a.onClick}
                disabled={a.disabled}
                style={btnStyle(tone, accent, !!a.disabled)}
              >
                {a.label}
              </button>
            );
          })}
        </div>
      )}

      {footer && (
        <div
          data-testid={`${testId ?? "inline-chat-card"}-footer`}
          style={{ color: "#6b7280", fontSize: 11 }}
        >
          {footer}
        </div>
      )}
    </div>
    </>
  );
}
