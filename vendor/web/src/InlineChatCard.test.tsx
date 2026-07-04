// GRACE-2 web — InlineChatCard primitive tests (job-0145, sprint-12-mega Wave 4).
//
// Verifies the common inline informational card primitive that
// PayloadWarningInline + SourceSuggestionInline share:
//   1. Renders the variant accent in the icon color (warning / info / success).
//   2. Renders title and string body.
//   3. Renders actions and invokes onClick.
//   4. Disabled actions don't invoke onClick.
//   5. Variant -> ARIA role: status (info/warning/success), alert (danger).
//   6. Optional icon override / suppression works.
//   7. Footer renders when provided.

import { describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach } from "vitest";
import { InlineChatCard, InlineChatCardVariant } from "./components/InlineChatCard";

afterEach(() => {
  cleanup();
});

describe("InlineChatCard — variants", () => {
  const variants: InlineChatCardVariant[] = [
    "warning",
    "danger",
    "info",
    "success",
  ];

  it.each(variants)(
    "renders the %s variant with the expected role + glyph",
    (variant) => {
      render(
        <InlineChatCard
          variant={variant}
          title={`A ${variant} card`}
          body={`Some ${variant} body text`}
          testId={`card-${variant}`}
        />,
      );
      const card = screen.getByTestId(`card-${variant}`);
      expect(card).toBeInTheDocument();
      expect(card).toHaveAttribute("data-variant", variant);
      const expectedRole = variant === "danger" ? "alert" : "status";
      expect(card).toHaveAttribute("role", expectedRole);
      // Glyph rendered.
      expect(screen.getByTestId(`card-${variant}-icon`)).toBeInTheDocument();
      // Title + body rendered.
      expect(screen.getByTestId(`card-${variant}-title`)).toHaveTextContent(
        `A ${variant} card`,
      );
      expect(screen.getByTestId(`card-${variant}-body`)).toHaveTextContent(
        `Some ${variant} body text`,
      );
    },
  );
});

describe("InlineChatCard — actions", () => {
  it("renders action buttons and invokes onClick", () => {
    const onPrimary = vi.fn();
    const onSecondary = vi.fn();
    render(
      <InlineChatCard
        variant="info"
        title="Pick an action"
        actions={[
          { label: "Confirm", onClick: onPrimary, testId: "act-confirm" },
          {
            label: "Dismiss",
            onClick: onSecondary,
            tone: "secondary",
            testId: "act-dismiss",
          },
        ]}
      />,
    );
    fireEvent.click(screen.getByTestId("act-confirm"));
    expect(onPrimary).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId("act-dismiss"));
    expect(onSecondary).toHaveBeenCalledTimes(1);
  });

  it("disabled action does not invoke onClick", () => {
    const onClick = vi.fn();
    render(
      <InlineChatCard
        variant="warning"
        title="Disabled action card"
        actions={[
          {
            label: "Sent",
            onClick,
            disabled: true,
            testId: "act-disabled",
          },
        ]}
      />,
    );
    const btn = screen.getByTestId("act-disabled") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    fireEvent.click(btn);
    expect(onClick).not.toHaveBeenCalled();
  });
});

describe("InlineChatCard — body & footer", () => {
  it("renders ReactNode body", () => {
    render(
      <InlineChatCard
        variant="info"
        title="Composed body"
        body={<span data-testid="composed-body-marker">composed</span>}
        testId="card-composed"
      />,
    );
    expect(screen.getByTestId("composed-body-marker")).toBeInTheDocument();
  });

  it("omits body element when body is empty", () => {
    render(
      <InlineChatCard variant="info" title="No body" testId="card-nobody" />,
    );
    expect(
      screen.queryByTestId("card-nobody-body"),
    ).not.toBeInTheDocument();
  });

  it("renders footer when provided", () => {
    render(
      <InlineChatCard
        variant="info"
        title="With footer"
        footer={<span data-testid="footer-marker">Sent: proceed</span>}
        testId="card-footer"
      />,
    );
    expect(screen.getByTestId("footer-marker")).toBeInTheDocument();
    expect(screen.getByTestId("card-footer-footer")).toBeInTheDocument();
  });
});

describe("InlineChatCard — accessibility", () => {
  it("danger variant has role=alert (assertive)", () => {
    render(
      <InlineChatCard
        variant="danger"
        title="Critical"
        testId="card-danger"
      />,
    );
    expect(screen.getByTestId("card-danger")).toHaveAttribute("role", "alert");
  });

  it("aria-label applied when provided", () => {
    render(
      <InlineChatCard
        variant="info"
        title="Labeled card"
        ariaLabel="A friendly card"
        testId="card-aria"
      />,
    );
    expect(screen.getByTestId("card-aria")).toHaveAttribute(
      "aria-label",
      "A friendly card",
    );
  });
});

describe("InlineChatCard — icon override", () => {
  it("respects custom icon", () => {
    render(
      <InlineChatCard
        variant="info"
        title="Icon override"
        icon="★"
        testId="card-iconov"
      />,
    );
    expect(screen.getByTestId("card-iconov-icon")).toHaveTextContent("★");
  });

  it("suppresses the icon when icon is empty string", () => {
    render(
      <InlineChatCard
        variant="info"
        title="No icon"
        icon=""
        testId="card-noicon"
      />,
    );
    expect(screen.queryByTestId("card-noicon-icon")).not.toBeInTheDocument();
  });
});
