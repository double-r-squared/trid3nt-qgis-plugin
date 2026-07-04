// GRACE-2 web - not-connected composer overlay (auto-stop/wake infra, NATE
// 2026-06-17; redesigned 2026-06-19 per `project_wake_composer_redesign`).
//
// The always-on AGENT box (EC2 t3.large) is eligible to be STOPPED by an
// idle-check Lambda after N consecutive zero-connection polls. A stopped box
// answers nothing, so the WebSocket cannot connect until the box is started.
//
// This is the SINGLE overlay that renders ALL THREE not-connected composer
// treatments (NATE's locked redesign - supersedes the prior separate
// "composer-connecting" div + the card-in-card waking surface). The parent
// (Chat.tsx) feeds `phase`; the overlay renders ONE card with nothing
// underneath (the composer + sheet chrome are hidden by the parent), so the
// surface is MID-TRANSPARENCY and the page background reads through it:
//
//   - "hidden"     → connected (or below the failure threshold): render
//                    nothing (after a brief opacity fade-out if it was
//                    previously visible - reads as "the agent woke up").
//   - "connecting" → NOT connected and not (yet) classified asleep: a quiet
//                    "Connecting" card with a YELLOW shimmer edge + a simple
//                    spinner. NEVER auto-wakes.
//   - "asleep"     → the box looks stopped AND a wake endpoint is configured:
//                    a tappable "Wake up" card with a STATIC model-color edge.
//                    Tapping POSTs the wake endpoint (App wires `onWake`).
//   - "waking"     → a wake POST is in flight / the box is booting: a "Waking
//                    up" card with a RAINBOW shimmer edge (no icon). Same card
//                    shape as the others - NOT a card-in-card.
//
// Edge-shimmer ONLY (NATE redesign): no loading icon, no upward-wave, no power
// glyph, no subtext sub-lines - just the words. The state reads off the EDGE
// treatment + (connecting) a small spinner.
//
// prefers-reduced-motion: the shimmer + spinner are suppressed; every phase
// falls back to a STATIC edge (the model-color edge), and the state is still
// communicated via the word ("Connecting" / "Wake up" / "Waking up").
//
// Pure presentational. No network I/O, no WebSocket coupling - the parent owns
// the wake POST (lib/wake.ts) and the phase machine. SSR-safe.

import { CSSProperties, useEffect, useRef, useState } from "react";

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
// Two motions, both EDGE-only per the redesign:
//   - edge-shimmer: a light sweep travels around the card's border (the
//     conic-gradient border-image rotates), 2.6s linear. Used by "connecting"
//     (yellow tones) + "waking" (rainbow tones).
//   - spin: the simple "connecting" spinner ring, 0.9s linear.

const KEYFRAMES_ID = "grace2-wake-overlay-keyframes";

function ensureKeyframes(): void {
  if (typeof document === "undefined") return;
  if (document.getElementById(KEYFRAMES_ID)) return;
  const style = document.createElement("style");
  style.id = KEYFRAMES_ID;
  style.textContent = `
@keyframes grace2-wake-edge-shimmer {
  0%   { background-position: 0% 50%; }
  100% { background-position: 200% 50%; }
}
@keyframes grace2-wake-spin {
  0%   { transform: rotate(0deg); }
  100% { transform: rotate(360deg); }
}
`;
  document.head.appendChild(style);
}

ensureKeyframes();

// --- Phase ---------------------------------------------------------------- //

export type WakePhase = "hidden" | "connecting" | "asleep" | "waking";

export interface WakeOverlayProps {
  /**
   * Overlay state, owned by App.tsx / Chat.tsx:
   *   - "hidden": connected / below failure threshold → renders nothing
   *     (after a brief fade-out if it was previously visible).
   *   - "connecting": not connected, not classified asleep → "Connecting"
   *     card with a yellow shimmer edge + a simple spinner.
   *   - "asleep": box looks stopped → tappable "Wake up" card with a static
   *     model-color edge.
   *   - "waking": a wake POST is in flight / the box is booting → "Waking up"
   *     card with a rainbow shimmer edge.
   */
  phase: WakePhase;
  /**
   * Called when the user TAPS the "Wake up" card. App.tsx wires this to its
   * AgentWaker (resets the debounce so a manual tap always fires) and flips
   * `phase` to "waking". No-op-safe: the overlay also calls it on Enter /
   * Space for keyboard users. Only the "asleep" phase is tappable.
   */
  onWake: () => void;
  /**
   * Per-model accent color (Chat feeds getModelById(selectedModelId)
   * .accentColor). Drives the STATIC edge for the "asleep"/"wake" phase AND
   * the reduced-motion fallback edge for every phase. Defaults to a neutral
   * slate so the overlay still renders if a caller omits it.
   */
  accentColor?: string;
  /**
   * NATE 2026-06-19: the box must be the SAME SIZE as the live composer (text
   * form). Chat feeds its measured composer height (``inputHeightPx``); the box
   * uses it as its min-height so the swap is seamless. Falls back to the
   * composer default when unset.
   */
  boxHeight?: number;
}

/**
 * Fade duration (ms) for the opacity transition. Kept in sync with the inline
 * `transition` below; exported so tests can assert the value if needed.
 */
export const WAKE_FADE_MS = 420;

/** Fallback edge color when no per-model accent is supplied. */
const DEFAULT_ACCENT = "#5c7fa3";

/** Composer resting height fallback (mirrors Chat DEFAULT_INPUT_HEIGHT_PX). */
const DEFAULT_BOX_HEIGHT = 68;

export function WakeOverlay({
  phase,
  onWake,
  accentColor = DEFAULT_ACCENT,
  boxHeight = DEFAULT_BOX_HEIGHT,
}: WakeOverlayProps): JSX.Element | null {
  const reduced = prefersReducedMotion();

  // Keep the overlay mounted through the fade-out: when phase flips to "hidden"
  // we render one last frame at opacity 0, then unmount after the transition.
  const [mounted, setMounted] = useState<boolean>(phase !== "hidden");
  const unmountTimer = useRef<number | null>(null);

  useEffect(() => {
    if (unmountTimer.current !== null) {
      window.clearTimeout(unmountTimer.current);
      unmountTimer.current = null;
    }
    if (phase !== "hidden") {
      setMounted(true);
      return;
    }
    // phase === "hidden": fade out, then unmount. With reduced motion (no
    // transition) unmount immediately so we don't leave a click-blocking layer.
    if (reduced) {
      setMounted(false);
      return;
    }
    unmountTimer.current = window.setTimeout(() => {
      setMounted(false);
      unmountTimer.current = null;
    }, WAKE_FADE_MS);
    return () => {
      if (unmountTimer.current !== null) {
        window.clearTimeout(unmountTimer.current);
        unmountTimer.current = null;
      }
    };
  }, [phase, reduced]);

  if (!mounted) return null;

  const visible = phase !== "hidden";
  const waking = phase === "waking";
  const connecting = phase === "connecting";
  const asleep = phase === "asleep";

  // NATE 2026-06-19 FIX: this is NOT an overlay. It REPLACES the composer's
  // content IN PLACE — the parent (Chat.tsx) renders EITHER <ChatInput> OR this,
  // never both — so there is NOTHING underneath and NO card-in-card. This
  // wrapper is a plain IN-FLOW block spanning the composer slot; the box below
  // is styled to look like the composer box with its content swapped.
  const overlayStyle: CSSProperties = {
    position: "relative",
    width: "100%",
    display: "flex",
    justifyContent: "center",
    opacity: visible ? 1 : 0,
    transition: reduced ? undefined : `opacity ${WAKE_FADE_MS}ms ease`,
    pointerEvents: visible ? "auto" : "none",
    fontFamily:
      "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
  };

  // The EDGE treatment is a gradient border painted via a padding-box/border-box
  // mask trick so the card interior stays clean. We build the border gradient
  // per phase:
  //   - reduced motion → STATIC accent edge (all phases).
  //   - connecting      → yellow shimmer edge (animated).
  //   - waking          → rainbow shimmer edge (animated).
  //   - asleep/wake     → STATIC accent edge.
  const animatedEdge = !reduced && (connecting || waking);
  const edgeGradient = reduced
    ? `linear-gradient(${accentColor}, ${accentColor})`
    : connecting
      ? "linear-gradient(90deg, #f5c542, #ffe08a, #f5c542, #ffe08a, #f5c542)"
      : waking
        ? "linear-gradient(90deg, #FF6B6B, #FFD93D, #6BCB77, #4D96FF, #B266FF, #FF6B6B)"
        : `linear-gradient(${accentColor}, ${accentColor})`;

  const edgeStyle: CSSProperties = {
    // Paint the gradient only in the border region (border-box minus
    // padding-box) so the card surface underneath stays the panel color.
    // Use backgroundImage (not the `background` shorthand) so the gradient
    // reflects reliably in the CSSOM (and so tests can read it).
    backgroundImage: edgeGradient,
    backgroundSize: animatedEdge ? "200% 100%" : undefined,
    animation: animatedEdge
      ? "grace2-wake-edge-shimmer 2.6s linear infinite"
      : undefined,
    WebkitMask:
      "linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0)",
    WebkitMaskComposite: "xor",
    mask: "linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0)",
    maskComposite: "exclude",
    position: "absolute",
    inset: 0,
    padding: 1.5,
    borderRadius: 14,
    pointerEvents: "none",
  };

  // The box reads as the SAME box as <ChatInput> with its content swapped: full
  // width, the composer's radius (14), the SAME height as the live composer
  // (boxHeight), and the colored EDGE as its border. NATE 2026-06-19: FROSTED
  // GLASS fill (not solid, not fully transparent) - a translucent surface that
  // blurs the map behind it; ``-webkit-backdrop-filter`` is required for iOS
  // Safari. A text-shadow keeps the word legible over the frost.
  const cardStyle: CSSProperties = {
    position: "relative",
    overflow: "hidden",
    width: "100%",
    boxSizing: "border-box",
    minHeight: boxHeight,
    borderRadius: 14,
    background: "rgba(18,20,26,0.45)",
    backdropFilter: "blur(14px) saturate(1.3)",
    WebkitBackdropFilter: "blur(14px) saturate(1.3)",
    color: "#e7ecf5",
    display: "flex",
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 10,
    padding: "14px 16px",
    cursor: asleep ? "pointer" : "default",
    textAlign: "center",
  };

  const label = waking ? "Waking up" : connecting ? "Connecting" : "Wake up";

  const handleWake = (): void => {
    if (!asleep) return; // only the asleep card taps to wake
    onWake();
  };

  return (
    <div
      data-testid="wake-overlay"
      data-phase={phase}
      style={overlayStyle}
      aria-hidden={!visible}
    >
      <div
        data-testid="wake-overlay-rect"
        role={asleep ? "button" : "status"}
        aria-live={asleep ? undefined : "polite"}
        tabIndex={asleep ? 0 : -1}
        aria-label={
          waking ? "Waking up agent" : asleep ? "Wake up agent" : "Connecting"
        }
        onClick={asleep ? handleWake : undefined}
        onKeyDown={
          asleep
            ? (e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  handleWake();
                }
              }
            : undefined
        }
        style={cardStyle}
      >
        {/* Animated / static gradient EDGE (no icon glyph inside the card). */}
        <span data-testid="wake-overlay-edge" aria-hidden="true" style={edgeStyle} />
        {connecting && <ConnectingSpinner reduced={reduced} accentColor={accentColor} />}
        <div
          style={{
            fontSize: 16,
            fontWeight: 700,
            letterSpacing: 0.2,
            // legible on the transparent fill over a varied map background.
            textShadow: "0 1px 3px rgba(0,0,0,0.7)",
          }}
        >
          {label}
        </div>
      </div>
    </div>
  );
}

// --- Connecting spinner (the ONLY motion glyph; connecting phase only) --- //
//
// A simple rotating ring. Suppressed under reduced motion (the static edge
// + the "Connecting" word communicate the state without motion).

function ConnectingSpinner({
  reduced,
  accentColor,
}: {
  reduced: boolean;
  accentColor: string;
}): JSX.Element | null {
  if (reduced) return null;
  return (
    <span
      data-testid="wake-overlay-spinner"
      aria-hidden="true"
      style={{
        width: 18,
        height: 18,
        borderRadius: "50%",
        border: "2px solid rgba(255,255,255,0.18)",
        borderTopColor: accentColor,
        animation: "grace2-wake-spin 0.9s linear infinite",
      }}
    />
  );
}
