// GRACE-2 web — ScrollToBottom tests (job-0153 Part 3).
//
// Verifies the floating scroll-to-bottom button presentation + click handler
// + visibility toggling. Auto-hide behavior (scroll-position tracking) is
// owned by Chat.tsx — this component is pure presentation.

import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ScrollToBottom } from "./components/ScrollToBottom";

describe("ScrollToBottom (job-0153 Part 3)", () => {
  it("renders the down-chevron button", () => {
    render(<ScrollToBottom visible={true} onClick={() => {}} />);
    const btn = screen.getByTestId("scroll-to-bottom");
    expect(btn.tagName).toBe("BUTTON");
    expect(btn.getAttribute("aria-label")).toBe("Scroll to bottom");
    // SVG chevron present.
    expect(btn.querySelector("svg")).not.toBeNull();
  });

  it("calls onClick when clicked while visible", () => {
    const onClick = vi.fn();
    render(<ScrollToBottom visible={true} onClick={onClick} />);
    fireEvent.click(screen.getByTestId("scroll-to-bottom"));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("opacity = 1 when visible", () => {
    render(<ScrollToBottom visible={true} onClick={() => {}} />);
    const btn = screen.getByTestId("scroll-to-bottom");
    expect(btn.style.opacity).toBe("1");
    expect(btn.style.pointerEvents).toBe("auto");
    expect(btn.getAttribute("data-visible")).toBe("true");
  });

  it("opacity = 0 when hidden (still in DOM for fade transition)", () => {
    render(<ScrollToBottom visible={false} onClick={() => {}} />);
    const btn = screen.getByTestId("scroll-to-bottom");
    expect(btn.style.opacity).toBe("0");
    expect(btn.style.pointerEvents).toBe("none");
    expect(btn.getAttribute("data-visible")).toBe("false");
  });

  it("uses transition: opacity (≥150ms) so the fade is smooth", () => {
    render(<ScrollToBottom visible={true} onClick={() => {}} />);
    const transition = screen.getByTestId("scroll-to-bottom").style.transition;
    expect(transition).toContain("opacity");
    // Extract the ms value(s) and confirm one of them is ≥150.
    const msMatches = transition.match(/(\d+)ms/g) ?? [];
    const anyOver150 = msMatches.some((m) => parseInt(m, 10) >= 150);
    expect(anyOver150).toBe(true);
  });

  it("circular shape (border-radius ≥ width/2)", () => {
    render(<ScrollToBottom visible={true} onClick={() => {}} />);
    const style = screen.getByTestId("scroll-to-bottom").style;
    const width = parseInt(style.width, 10);
    const radius = parseInt(style.borderRadius, 10);
    expect(radius * 2).toBeGreaterThanOrEqual(width);
  });

  it("tabIndex=-1 when hidden so it can't be focused", () => {
    render(<ScrollToBottom visible={false} onClick={() => {}} />);
    expect(screen.getByTestId("scroll-to-bottom").getAttribute("tabindex"))
      .toBe("-1");
  });
});
