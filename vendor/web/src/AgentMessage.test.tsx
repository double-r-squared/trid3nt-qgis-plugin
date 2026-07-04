// GRACE-2 web — AgentMessage markdown rendering tests (job-0153 Part 1).
//
// Verifies the markdown renderer wires through react-markdown + remark-gfm
// and produces real HTML elements (no raw markdown characters left over).

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { AgentMessage } from "./components/AgentMessage";

describe("AgentMessage (job-0153 Part 1)", () => {
  it("renders an h1 from `# heading` markdown", () => {
    const { container } = render(
      <AgentMessage text={"# Flood scenario"} done={true} />,
    );
    const h1 = container.querySelector("h1");
    expect(h1).not.toBeNull();
    expect(h1!.textContent).toBe("Flood scenario");
  });

  it("renders bold via <strong>", () => {
    const { container } = render(
      <AgentMessage text={"This is **important** text"} done={true} />,
    );
    const strong = container.querySelector("strong");
    expect(strong).not.toBeNull();
    expect(strong!.textContent).toBe("important");
  });

  it("renders italic via <em>", () => {
    const { container } = render(
      <AgentMessage text={"This is *emphasized* text"} done={true} />,
    );
    const em = container.querySelector("em");
    expect(em).not.toBeNull();
    expect(em!.textContent).toBe("emphasized");
  });

  it("renders a fenced code block (pre > code)", () => {
    const md = ["```python", "x = 1", "```"].join("\n");
    const { container } = render(<AgentMessage text={md} done={true} />);
    const pre = container.querySelector("pre");
    expect(pre).not.toBeNull();
    expect(pre!.querySelector("code")).not.toBeNull();
    expect(pre!.textContent).toContain("x = 1");
  });

  it("renders inline code as <code>", () => {
    const { container } = render(
      <AgentMessage text={"use `git status` first"} done={true} />,
    );
    const inline = container.querySelector("code");
    expect(inline).not.toBeNull();
    expect(inline!.textContent).toBe("git status");
  });

  it("renders unordered lists with multiple items", () => {
    const md = ["- first", "- second", "- third"].join("\n");
    const { container } = render(<AgentMessage text={md} done={true} />);
    const ul = container.querySelector("ul");
    expect(ul).not.toBeNull();
    expect(ul!.querySelectorAll("li").length).toBe(3);
  });

  it("renders ordered lists with multiple items", () => {
    const md = ["1. one", "2. two", "3. three"].join("\n");
    const { container } = render(<AgentMessage text={md} done={true} />);
    const ol = container.querySelector("ol");
    expect(ol).not.toBeNull();
    expect(ol!.querySelectorAll("li").length).toBe(3);
  });

  it("renders links as anchor tags with target=_blank", () => {
    const { container } = render(
      <AgentMessage text={"See [docs](https://example.com)"} done={true} />,
    );
    const a = container.querySelector("a");
    expect(a).not.toBeNull();
    expect(a!.getAttribute("href")).toBe("https://example.com");
    expect(a!.getAttribute("target")).toBe("_blank");
  });

  it("renders GFM tables via remark-gfm", () => {
    const md = [
      "| col1 | col2 |",
      "| --- | --- |",
      "| a | b |",
    ].join("\n");
    const { container } = render(<AgentMessage text={md} done={true} />);
    expect(container.querySelector("table")).not.toBeNull();
    expect(container.querySelectorAll("th").length).toBe(2);
    expect(container.querySelectorAll("td").length).toBe(2);
  });

  it("uses transparent background (no card chrome)", () => {
    render(<AgentMessage text={"hello"} done={true} />);
    const wrapper = screen.getByTestId("agent-message");
    expect(wrapper.style.background).toBe("transparent");
    // 'border: none' may be normalized by the DOM to "none none" or similar;
    // assert it carries no visible border by checking the border-style is
    // either empty or 'none'.
    const borderStr = wrapper.style.border.toLowerCase();
    expect(borderStr === "" || borderStr.includes("none")).toBe(true);
  });

  it("shows a streaming cursor when not done", () => {
    render(<AgentMessage text={"streaming…"} done={false} />);
    expect(screen.queryByTestId("agent-cursor")).not.toBeNull();
  });

  it("hides the streaming cursor when done", () => {
    render(<AgentMessage text={"final"} done={true} />);
    expect(screen.queryByTestId("agent-cursor")).toBeNull();
  });

  it("carries role=agent + done markers", () => {
    render(<AgentMessage text={"hi"} done={true} />);
    const wrapper = screen.getByTestId("agent-message");
    expect(wrapper.getAttribute("data-role")).toBe("agent");
    expect(wrapper.getAttribute("data-done")).toBe("true");
  });

  it("does NOT leave raw markdown characters when rendering", () => {
    render(<AgentMessage text={"# heading\n**bold**"} done={true} />);
    const wrapper = screen.getByTestId("agent-message");
    // Raw '#' and '**' should not appear in the visible text once parsed.
    expect(wrapper.textContent).not.toContain("**");
    expect(wrapper.textContent).not.toMatch(/^# /);
  });
});
