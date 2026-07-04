// GRACE-2 web - landing page content tests (v2).
//
// Pins the LOAD-BEARING contract of the public landing page (EntryRouter and
// the live-verify tooling depend on these and they must not drift):
//   - the hero CTA targets "/app" and toggles Launch <-> Resume on hasSession;
//   - the privacy-policy link exists and points at "/privacy" (OAuth consent
//     screen prerequisite).
//
// It also pins the v2 EDITORIAL direction NATE asked for: impact-led, plain
// English, and NO vendor name-drops or count-bragging on the page. We assert
// those omissions so the marketing copy cannot silently regress to the old
// "8 physics engines, all real" / "Powered by Anthropic Claude on AWS" style.

import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { Landing } from "./Landing";

afterEach(cleanup);

describe("Landing - hero CTA contract", () => {
  it("renders the primary CTA pointing at /app with 'Launch TRID3NT'", () => {
    render(<Landing />);
    const cta = screen.getByTestId("grace2-landing-cta");
    expect(cta).toHaveAttribute("href", "/app");
    expect(cta).toHaveTextContent(/launch trid3nt/i);
  });

  it("renders the 'Resume session' CTA variant when hasSession is true", () => {
    render(<Landing hasSession />);
    const cta = screen.getByTestId("grace2-landing-cta");
    expect(cta).toHaveAttribute("href", "/app");
    expect(cta).toHaveTextContent(/resume session/i);
  });

  it("sets a multi-hazard document title", () => {
    render(<Landing />);
    expect(document.title).toMatch(/TRID3NT/);
    expect(document.title).toMatch(/multi-hazard/i);
  });
});

describe("Landing - impact-led capabilities (v2)", () => {
  it("renders a capability card for each plain-English hazard outcome", () => {
    render(<Landing />);
    expect(
      screen.getAllByTestId("grace2-landing-capability").length,
    ).toBeGreaterThanOrEqual(6);
  });

  it("describes the headline hazards in plain language", () => {
    render(<Landing />);
    expect(
      screen.getByText(/coastal flooding and storm surge/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/river and compound flooding/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/groundwater and contamination/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/earthquake and terrain hazard/i),
    ).toBeInTheDocument();
  });

  it("leads on impact and real simulation, not heuristics", () => {
    render(<Landing />);
    expect(
      screen.getByText(/grounded in real numerical simulation/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/weeks to minutes/i)).toBeInTheDocument();
  });
});

describe("Landing - how it works", () => {
  it("renders the three-step describe -> run -> read flow", () => {
    render(<Landing />);
    expect(
      screen.getByRole("heading", { level: 3, name: /describe it/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 3, name: /it runs the science/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 3, name: /read the result/i }),
    ).toBeInTheDocument();
  });
});

describe("Landing - footer + privacy", () => {
  it("links to the privacy policy in the footer", () => {
    render(<Landing />);
    const link = screen.getByTestId("grace2-landing-privacy-link");
    expect(link).toHaveAttribute("href", "/privacy");
  });
});

describe("Landing - professional tone (no vendor name-drops / count-bragging)", () => {
  it("does not advertise specific vendors or model names", () => {
    const { container } = render(<Landing />);
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/anthropic/i);
    expect(text).not.toMatch(/\bclaude\b/i);
    expect(text).not.toMatch(/\bbedrock\b/i);
    expect(text).not.toMatch(/\bnova\b/i);
    // No "powered by <X>" vendor framing.
    expect(text).not.toMatch(/powered by/i);
  });

  it("does not brag about engine / tool counts", () => {
    const { container } = render(<Landing />);
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/\d+\s*physics engines/i);
    expect(text).not.toMatch(/all real/i);
    expect(text).not.toMatch(/\d+\+\s*(agent\s*)?tools/i);
  });

  it("renders the contour-field hero backdrop", () => {
    render(<Landing />);
    expect(
      screen.getByTestId("grace2-landing-contours"),
    ).toBeInTheDocument();
  });
});
