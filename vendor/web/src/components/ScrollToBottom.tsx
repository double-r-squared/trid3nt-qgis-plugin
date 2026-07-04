// GRACE-2 web — Scroll-to-bottom affordance (job-0153 Part 3).
//
// A floating down-arrow button shown above the chat input wrapper when the
// user has scrolled up from the bottom of the conversation. Click smooth-
// scrolls to the bottom; the button auto-hides when the user reaches the
// bottom (within a ~50px threshold).
//
// Presentation:
//   - 32px circle, transparent background, subtle white border + chevron
//     icon. Smooth fade in/out over 200ms via opacity transition.
//   - The parent positions the button (Chat.tsx anchors it just above the
//     ChatInput overlay).
//
// Interaction model:
//   - Parent owns the scroll container ref and tracks scroll position. It
//     passes `visible` and `onClick` props. This component is pure
//     presentation so it can be tested in isolation without simulating a
//     real scroll container.
//
// Invariant 1 (determinism boundary): pure presentation — emits intent
// only.

import { CSSProperties } from "react";

export interface ScrollToBottomProps {
  /** Whether the button should be visible. Parent decides via scroll handler. */
  visible: boolean;
  /** Called when the user clicks the button. Parent smooth-scrolls. */
  onClick: () => void;
}

const BUTTON_STYLE: CSSProperties = {
  width: 32,
  height: 32,
  borderRadius: 16,
  background: "rgba(20,20,25,0.6)",
  border: "1px solid rgba(255,255,255,0.2)",
  color: "#fff",
  cursor: "pointer",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  padding: 0,
  // Fade in/out — parent controls visibility but we apply the transition so
  // the toggle isn't jarring.
  transition: "opacity 200ms ease, transform 200ms ease",
};

export function ScrollToBottom({
  visible,
  onClick,
}: ScrollToBottomProps): JSX.Element {
  // We render the button even when hidden (opacity 0 + pointer-events none)
  // so the fade animation works in both directions. When the parent toggles
  // visibility, opacity transitions over 200ms.
  return (
    <button
      data-testid="scroll-to-bottom"
      data-visible={visible ? "true" : "false"}
      aria-label="Scroll to bottom"
      onClick={onClick}
      tabIndex={visible ? 0 : -1}
      style={{
        ...BUTTON_STYLE,
        opacity: visible ? 1 : 0,
        pointerEvents: visible ? "auto" : "none",
        transform: visible ? "translateY(0)" : "translateY(4px)",
      }}
    >
      <svg
        width="14"
        height="14"
        viewBox="0 0 16 16"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
        aria-hidden="true"
        style={{ display: "block" }}
      >
        <path
          d="M4 6L8 10L12 6"
          stroke="#fff"
          strokeWidth="1.75"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </button>
  );
}
