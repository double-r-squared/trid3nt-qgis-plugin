// GRACE-2 web — UserBubble tests (job-0153 Part 2).
//
// Verifies the user message bubble renders right-aligned with the subtle
// grey background + white text + bounded width called out in the kickoff.

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { UserBubble } from "./components/UserBubble";

describe("UserBubble (job-0153 Part 2)", () => {
  it("renders the user text verbatim", () => {
    render(<UserBubble text="Hello TRID3NT" />);
    expect(screen.getByTestId("user-bubble").textContent).toBe(
      "Hello TRID3NT",
    );
  });

  it("preserves newlines in the user text (pre-wrap)", () => {
    render(<UserBubble text={"line1\nline2"} />);
    const el = screen.getByTestId("user-bubble");
    expect(el.style.whiteSpace).toBe("pre-wrap");
  });

  it("right-aligns via flex-end self-align", () => {
    render(<UserBubble text="hi" />);
    expect(screen.getByTestId("user-bubble").style.alignSelf).toBe("flex-end");
  });

  it("uses subtle grey background + white text on dark theme", () => {
    render(<UserBubble text="hi" />);
    const style = screen.getByTestId("user-bubble").style;
    // Background is a low-alpha white overlay — kickoff Part 2 calls for
    // rgba(255,255,255,0.08) on dark theme.
    expect(style.background.replace(/\s/g, "")).toContain("rgba(255,255,255");
    // Text is explicit white.
    expect(style.color).toMatch(/^(#fff|#ffffff|rgb\(255,255,255\))$/i);
  });

  it("bounds bubble width to ≤80% so long messages wrap", () => {
    render(<UserBubble text={"x".repeat(400)} />);
    expect(screen.getByTestId("user-bubble").style.maxWidth).toBe("80%");
  });

  it("rounds corners (≥10px) to read as a 'bubble'", () => {
    render(<UserBubble text="hi" />);
    const radius = parseInt(
      screen.getByTestId("user-bubble").style.borderRadius,
      10,
    );
    expect(radius).toBeGreaterThanOrEqual(10);
  });

  it("carries role=user marker for downstream tests", () => {
    render(<UserBubble text="hi" />);
    expect(
      screen.getByTestId("user-bubble").getAttribute("data-role"),
    ).toBe("user");
  });
});
