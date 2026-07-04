// GRACE-2 web — Agent markdown message renderer (job-0153 Part 1).
//
// Renders the agent's streamed text as Markdown using react-markdown +
// remark-gfm (tables, strikethrough, autolinks). The block has no border,
// no background, no card chrome — the content lives directly in the chat
// panel so it reads as natural prose.
//
// Streaming semantics: text arrives incrementally via agent-message-chunk
// deltas (Appendix A.4). While the message is still streaming (done=false)
// we append a faint cursor glyph the same way the prior plain-text path did,
// after the rendered markdown block.
//
// Styling rationale (kickoff Part 1):
//   - Headings (h1–h3) get reasonable top margin to separate sections.
//   - Code blocks: subtle dark background + monospace.
//   - Inline code: same monospace + slight contrast pill.
//   - Links: blue, underlined on hover, opens in a new tab.
//   - Lists: native bullets/numbers; tight gap between items.
//   - Margins between paragraphs / list items / heading blocks are tuned so
//     the chat reads at ~13–14px without feeling cramped.
//
// We expose the renderer as a `<AgentMessage text done />` component so the
// Chat scroll-stream can keep the same per-message map as today.
//
// Invariant 1 (determinism boundary): pure consumer — every character it
// renders came from the agent. Tool-call / numerical content is just text;
// we add no fallback values, defaults, or client-computed glyphs.

import { CSSProperties } from "react";
import ReactMarkdown, { Components } from "react-markdown";
import remarkGfm from "remark-gfm";

export interface AgentMessageProps {
  /** Streamed agent text. May be partial markdown if done=false. */
  text: string;
  /** Whether the stream has finalized (agent-message-chunk.done). */
  done: boolean;
}

const WRAPPER_STYLE: CSSProperties = {
  // No card chrome — transparent, full-width within the scroll column.
  background: "transparent",
  border: "none",
  padding: 0,
  color: "#eee",
  fontSize: 13,
  lineHeight: 1.5,
  // Override react-markdown's default block spacing for a tighter feel.
  // Children carry their own margins (set below).
};

// react-markdown lets us swap node renderers per element. We override the
// blocks that need custom styling; everything else falls back to the default
// HTML element (which inherits WRAPPER_STYLE).
const COMPONENTS: Components = {
  h1: ({ children }) => (
    <h1 style={{ fontSize: 18, fontWeight: 600, margin: "10px 0 6px" }}>
      {children}
    </h1>
  ),
  h2: ({ children }) => (
    <h2 style={{ fontSize: 16, fontWeight: 600, margin: "10px 0 6px" }}>
      {children}
    </h2>
  ),
  h3: ({ children }) => (
    <h3 style={{ fontSize: 14, fontWeight: 600, margin: "8px 0 4px" }}>
      {children}
    </h3>
  ),
  p: ({ children }) => (
    <p style={{ margin: "0 0 8px" }}>{children}</p>
  ),
  ul: ({ children }) => (
    <ul style={{ margin: "0 0 8px", paddingLeft: 20 }}>{children}</ul>
  ),
  ol: ({ children }) => (
    <ol style={{ margin: "0 0 8px", paddingLeft: 20 }}>{children}</ol>
  ),
  li: ({ children }) => (
    <li style={{ margin: "2px 0" }}>{children}</li>
  ),
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      style={{ color: "#7cb7ff", textDecoration: "underline" }}
    >
      {children}
    </a>
  ),
  code: ({ className, children, ...rest }) => {
    // react-markdown passes a className like `language-xyz` for fenced code
    // blocks (rendered inside <pre>); inline code has no className. We use
    // that to switch between an inline pill and a block treatment.
    const isInline = !className;
    if (isInline) {
      return (
        <code
          style={{
            background: "rgba(255,255,255,0.08)",
            padding: "1px 4px",
            borderRadius: 3,
            fontFamily:
              'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
            fontSize: 12,
          }}
          {...rest}
        >
          {children}
        </code>
      );
    }
    return (
      <code
        className={className}
        style={{
          fontFamily:
            'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
          fontSize: 12,
        }}
        {...rest}
      >
        {children}
      </code>
    );
  },
  pre: ({ children }) => (
    <pre
      style={{
        background: "rgba(255,255,255,0.05)",
        padding: "8px 10px",
        borderRadius: 6,
        overflowX: "auto",
        margin: "0 0 8px",
      }}
    >
      {children}
    </pre>
  ),
  blockquote: ({ children }) => (
    <blockquote
      style={{
        margin: "0 0 8px",
        padding: "4px 0 4px 10px",
        borderLeft: "3px solid rgba(255,255,255,0.15)",
        color: "#ccc",
      }}
    >
      {children}
    </blockquote>
  ),
  hr: () => (
    <hr
      style={{
        border: "none",
        borderTop: "1px solid rgba(255,255,255,0.12)",
        margin: "10px 0",
      }}
    />
  ),
  table: ({ children }) => (
    <table
      style={{
        borderCollapse: "collapse",
        margin: "0 0 8px",
        fontSize: 12,
      }}
    >
      {children}
    </table>
  ),
  th: ({ children }) => (
    <th
      style={{
        borderBottom: "1px solid rgba(255,255,255,0.2)",
        padding: "4px 8px",
        textAlign: "left",
      }}
    >
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td
      style={{
        borderBottom: "1px solid rgba(255,255,255,0.08)",
        padding: "4px 8px",
      }}
    >
      {children}
    </td>
  ),
};

export function AgentMessage({ text, done }: AgentMessageProps): JSX.Element {
  return (
    <div
      data-testid="agent-message"
      data-role="agent"
      data-done={done ? "true" : "false"}
      style={WRAPPER_STYLE}
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={COMPONENTS}>
        {text}
      </ReactMarkdown>
      {!done && <TypingCaret />}
    </div>
  );
}

// CSS-drawn blinking caret for the streaming state. This is NOT a unicode
// glyph and NOT an icon — it's a thin solid block drawn with a styled <span>
// (background + width/height) that blinks via a keyframe animation. We respect
// prefers-reduced-motion by holding the caret steady (no blink) for users who
// have requested reduced motion.
const CARET_BLINK_CSS = `
@keyframes grace2-caret-blink {
  0%, 49% { opacity: 1; }
  50%, 100% { opacity: 0; }
}
.grace2-agent-caret {
  display: inline-block;
  width: 2px;
  height: 1em;
  margin-left: 2px;
  vertical-align: text-bottom;
  background: #888;
  border-radius: 1px;
  animation: grace2-caret-blink 1s step-end infinite;
}
@media (prefers-reduced-motion: reduce) {
  .grace2-agent-caret {
    animation: none;
    opacity: 1;
  }
}
`;

function TypingCaret(): JSX.Element {
  return (
    <>
      <style>{CARET_BLINK_CSS}</style>
      <span
        data-testid="agent-cursor"
        className="grace2-agent-caret"
        aria-hidden="true"
      />
    </>
  );
}
