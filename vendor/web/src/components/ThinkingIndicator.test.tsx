// GRACE-2 web — ThinkingIndicator component tests (wave-4-10 thinking-state).
//
// Per memory `feedback_thinking_state_ephemeral`:
//   - Renders italic muted-gray "Thinking…" text when `active` is true
//   - NO card chrome — no border, no background tint, no padding
//   - Subtle opacity pulse animation (respect prefers-reduced-motion)
//   - Renders nothing when `active` is false
//
// The active/inactive PARENT computation is tested in Chat.test.tsx via
// isThinkingActive; this suite verifies the component's render contract.

import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { ThinkingIndicator } from "./ThinkingIndicator";

describe("ThinkingIndicator", () => {
  it("renders 'Thinking…' text when active=true", () => {
    const { getByTestId } = render(<ThinkingIndicator active={true} />);
    const el = getByTestId("thinking-indicator");
    expect(el).toBeInTheDocument();
    expect(el.textContent).toContain("Thinking");
    // Horizontal ellipsis (U+2026), not three dots — consistent across fonts.
    expect(el.textContent).toContain("…");
  });

  it("renders nothing when active=false", () => {
    const { queryByTestId } = render(<ThinkingIndicator active={false} />);
    expect(queryByTestId("thinking-indicator")).toBeNull();
  });

  it("uses italic font style (matches ChatGPT/Claude convention)", () => {
    const { getByTestId } = render(<ThinkingIndicator active={true} />);
    const el = getByTestId("thinking-indicator");
    expect(el.style.fontStyle).toBe("italic");
  });

  it("has NO card chrome — no border, no background tint, no padding", () => {
    const { getByTestId } = render(<ThinkingIndicator active={true} />);
    const el = getByTestId("thinking-indicator");
    // Memory spec: indicator is presence not history → no box.
    expect(el.style.background).toBe("transparent");
    // happy-dom serializes the `border: none` shorthand as "none none" (style
    // + width tokens). The contract is "no border" — any form whose effective
    // border-style is "none" satisfies the spec.
    expect(el.style.border.startsWith("none")).toBe(true);
    expect(el.style.padding).toBe("0px");
  });

  it("uses the system muted color (#888) matching other chat-muted elements", () => {
    const { getByTestId } = render(<ThinkingIndicator active={true} />);
    const el = getByTestId("thinking-indicator");
    // Color via inline style — happy-dom normalizes #888 → 'rgb(136, 136, 136)'.
    const c = el.style.color;
    expect(c === "#888" || c === "rgb(136, 136, 136)").toBe(true);
  });

  it("exposes aria-live='polite' for screen-reader assistive announcements", () => {
    const { getByTestId } = render(<ThinkingIndicator active={true} />);
    const el = getByTestId("thinking-indicator");
    expect(el.getAttribute("role")).toBe("status");
    expect(el.getAttribute("aria-live")).toBe("polite");
  });

  it("snapshot: no box / no card chrome", () => {
    // Snapshot guards against a future regression that re-introduces card
    // chrome (border, background, padding) onto the thinking indicator.
    const { getByTestId } = render(<ThinkingIndicator active={true} />);
    const el = getByTestId("thinking-indicator");
    expect(el.outerHTML).toMatchInlineSnapshot(
      `"<div data-testid="thinking-indicator" data-active="true" role="status" aria-live="polite" style="color: #888; font-style: italic; font-size: 13px; line-height: 1.5; animation: grace2-thinking-pulse 1.6s ease-in-out infinite; background: transparent; border: none none; padding: 0px; margin: 0px; font-family: system-ui, sans-serif;">Thinking<span aria-hidden="true">…</span></div>"`,
    );
  });
});
