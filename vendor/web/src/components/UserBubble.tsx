// GRACE-2 web — User chat bubble (job-0153 Part 2).
//
// User messages render as a subtle grey rounded bubble, right-aligned,
// max 80% of chat width. This matches the Claude Code convention where the
// user appears on the right and the agent renders unaligned/full-width.
//
// Styling rationale (kickoff Part 2):
//   - Background: rgba(255,255,255,0.08) — subtle on dark theme.
//   - Text: #fff — explicit white.
//   - Border radius: 12px — matches the inline pipeline card / input wrapper
//     softness, distinct enough to read as a "bubble".
//   - Max width: 80% of chat width — leaves breathing room.
//   - whiteSpace: pre-wrap so the user's newlines (Shift+Enter) survive.
//
// Invariant 1 (determinism boundary): pure presentation — renders the text
// it was given, computes no agent-facing content.

import { CSSProperties } from "react";

export interface UserBubbleProps {
  /** Text content of the user message. May include newlines. */
  text: string;
}

const STYLE: CSSProperties = {
  alignSelf: "flex-end",
  maxWidth: "80%",
  background: "rgba(255,255,255,0.08)",
  color: "#fff",
  padding: "8px 12px",
  borderRadius: 12,
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
  fontSize: 13,
  lineHeight: 1.45,
};

export function UserBubble({ text }: UserBubbleProps): JSX.Element {
  return (
    <div data-testid="user-bubble" data-role="user" style={STYLE}>
      {text}
    </div>
  );
}
