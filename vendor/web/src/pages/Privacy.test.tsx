// GRACE-2 web — privacy policy content tests (job-0285).
//
// This page is the public privacy-policy URL for the OAuth consent screen, so
// the tests pin the disclosures that must not silently disappear: effective
// date, the plain-language section set, the storage/processing parties
// (MongoDB Atlas / Amazon S3 / AWS Bedrock — Anthropic Claude), the no-sale
// commitment, and the contact email. The product runs on AWS, not Google.

import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { Privacy } from "./Privacy";

afterEach(cleanup);

describe("Privacy — required disclosures", () => {
  it("renders the title and effective date", () => {
    render(<Privacy />);
    expect(
      screen.getByRole("heading", { level: 1, name: /privacy policy/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/effective date:/i)).toBeInTheDocument();
    expect(screen.getByText("June 11, 2026")).toBeInTheDocument();
  });

  it("renders all plain-language sections", () => {
    render(<Privacy />);
    for (const name of [
      /data we collect/i,
      /how we use it/i,
      /storage & third parties/i,
      /your choices/i,
      /contact/i,
    ]) {
      expect(
        screen.getByRole("heading", { level: 2, name }),
      ).toBeInTheDocument();
    }
  });

  it("discloses the storage and processing parties", () => {
    render(<Privacy />);
    expect(screen.getByText(/amazon dynamodb/i)).toBeInTheDocument();
    expect(screen.getByText(/amazon s3/i)).toBeInTheDocument();
    expect(
      screen.getByText(/aws bedrock \(anthropic claude\)/i),
    ).toBeInTheDocument();
  });

  it("states that personal data is not sold", () => {
    render(<Privacy />);
    expect(
      screen.getByText(/we do not sell personal data\./i),
    ).toBeInTheDocument();
  });

  it("mentions anonymous sessions today and Google sign-in coming", () => {
    render(<Privacy />);
    expect(screen.getByText(/session identifiers\./i)).toBeInTheDocument();
    expect(screen.getByText(/anonymous sessions/i)).toBeInTheDocument();
    // "Google sign-in" appears in both Data-we-collect and Changes sections.
    expect(
      screen.getAllByText(/google sign-in/i).length,
    ).toBeGreaterThanOrEqual(1);
  });

  it("links the contact email", () => {
    render(<Privacy />);
    const mail = screen.getByRole("link", {
      name: "natealmanza3@gmail.com",
    });
    expect(mail).toHaveAttribute("href", "mailto:natealmanza3@gmail.com");
  });

  it("links back to the landing page and to the app", () => {
    render(<Privacy />);
    expect(
      screen.getByRole("link", { name: /back to trid3nt/i }),
    ).toHaveAttribute("href", "/");
    expect(screen.getByRole("link", { name: /launch app/i })).toHaveAttribute(
      "href",
      "/app",
    );
  });

  it("sets the document title", () => {
    render(<Privacy />);
    expect(document.title).toBe("Privacy Policy - TRID3NT");
  });
});
