// GRACE-2 web — CredentialCard unit tests (credential-request flow; SRS §F.3).
//
// Tests (per job requirements):
//   1. Renders from a credential-request envelope: provider label + message.
//   2. Signup link uses signup_url, opens in a new tab (target=_blank,
//      rel=noopener noreferrer).
//   3. Save routes the typed key to onSave (the consumer wires this to the
//      secret-add path); the input is cleared after Save.
//   4. Save is disabled until a non-empty key is entered.
//   5. Decline routes to onDecline.
//   6. signup_url absent/null → no signup link rendered.
//   7. resolved="saved" / "declined" collapse to a terminal footer (form
//      hidden), so a resolved prompt can't be re-submitted.

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { CredentialCard } from "./CredentialCard";
import { CredentialRequestPayload } from "../contracts";

// 7 / 12 amended (NATE 2026-06-17 fold redesign): once resolved, the WHOLE card
// folds into a COMPACT one-line tool-card-style summary ("<provider> key saved"
// / "<provider> credential declined") — the form, message, and signup link all
// collapse away (so the prompt can't be re-submitted), and a chevron re-expands
// the read-only detail. The compact card is marked data-variant="compact".

const BASE_REQUEST: CredentialRequestPayload = {
  envelope_type: "credential-request",
  request_id: "01J0000000000000000000REQ1",
  provider_id: "ebird",
  provider_label: "eBird",
  signup_url: "https://ebird.org/api/keygen",
  secret_key_name: "EBIRD_API_KEY",
  message: "eBird needs an API key to fetch bird observations.",
  tool_name: "fetch_ebird_observations",
};

afterEach(() => cleanup());

describe("CredentialCard", () => {
  it("renders the provider label + message from the envelope", () => {
    render(
      <CredentialCard
        request={BASE_REQUEST}
        onSave={() => {}}
        onDecline={() => {}}
      />,
    );
    // Title carries the provider label.
    const title = screen.getByTestId(
      `credential-card-title-${BASE_REQUEST.request_id}`,
    );
    expect(title.textContent).toContain("eBird");
    expect(title.textContent).toContain("needs an API key");
    // The agent's user-facing message renders verbatim.
    expect(
      screen.getByTestId(`credential-card-message-${BASE_REQUEST.request_id}`)
        .textContent,
    ).toBe(BASE_REQUEST.message);
  });

  it("renders the secret key name as the input label", () => {
    render(
      <CredentialCard
        request={BASE_REQUEST}
        onSave={() => {}}
        onDecline={() => {}}
      />,
    );
    expect(screen.getByText("EBIRD_API_KEY")).toBeTruthy();
  });

  it("signup link uses signup_url and opens in a new tab", () => {
    render(
      <CredentialCard
        request={BASE_REQUEST}
        onSave={() => {}}
        onDecline={() => {}}
      />,
    );
    const link = screen.getByTestId(
      `credential-card-signup-${BASE_REQUEST.request_id}`,
    ) as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe(BASE_REQUEST.signup_url);
    expect(link.getAttribute("target")).toBe("_blank");
    // Security: noopener noreferrer on the new-tab link.
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(link.getAttribute("rel")).toContain("noreferrer");
  });

  it("hides the signup link when signup_url is null", () => {
    render(
      <CredentialCard
        request={{ ...BASE_REQUEST, signup_url: null }}
        onSave={() => {}}
        onDecline={() => {}}
      />,
    );
    expect(
      screen.queryByTestId(`credential-card-signup-${BASE_REQUEST.request_id}`),
    ).toBeNull();
  });

  it("Save is disabled until a non-empty key is entered", () => {
    render(
      <CredentialCard
        request={BASE_REQUEST}
        onSave={() => {}}
        onDecline={() => {}}
      />,
    );
    const save = screen.getByTestId(
      `credential-card-save-${BASE_REQUEST.request_id}`,
    ) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.change(
      screen.getByTestId(`credential-card-input-${BASE_REQUEST.request_id}`),
      { target: { value: "my-secret-key" } },
    );
    expect(save.disabled).toBe(false);
  });

  it("Save routes the typed key to onSave and clears the input", () => {
    const onSave = vi.fn();
    render(
      <CredentialCard
        request={BASE_REQUEST}
        onSave={onSave}
        onDecline={() => {}}
      />,
    );
    const input = screen.getByTestId(
      `credential-card-input-${BASE_REQUEST.request_id}`,
    ) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "the-real-key" } });
    fireEvent.click(
      screen.getByTestId(`credential-card-save-${BASE_REQUEST.request_id}`),
    );
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave).toHaveBeenCalledWith("the-real-key");
    // Decision F: the key must not linger in the DOM after Save.
    expect(input.value).toBe("");
  });

  it("submitting the form (Enter) routes the key to onSave", () => {
    const onSave = vi.fn();
    render(
      <CredentialCard
        request={BASE_REQUEST}
        onSave={onSave}
        onDecline={() => {}}
      />,
    );
    fireEvent.change(
      screen.getByTestId(`credential-card-input-${BASE_REQUEST.request_id}`),
      { target: { value: "enter-key" } },
    );
    fireEvent.submit(
      screen.getByTestId(`credential-card-form-${BASE_REQUEST.request_id}`),
    );
    expect(onSave).toHaveBeenCalledWith("enter-key");
  });

  it("does not call onSave for a whitespace-only key", () => {
    const onSave = vi.fn();
    render(
      <CredentialCard
        request={BASE_REQUEST}
        onSave={onSave}
        onDecline={() => {}}
      />,
    );
    fireEvent.change(
      screen.getByTestId(`credential-card-input-${BASE_REQUEST.request_id}`),
      { target: { value: "   " } },
    );
    fireEvent.submit(
      screen.getByTestId(`credential-card-form-${BASE_REQUEST.request_id}`),
    );
    expect(onSave).not.toHaveBeenCalled();
  });

  it("Decline routes to onDecline", () => {
    const onDecline = vi.fn();
    render(
      <CredentialCard
        request={BASE_REQUEST}
        onSave={() => {}}
        onDecline={onDecline}
      />,
    );
    fireEvent.click(
      screen.getByTestId(`credential-card-decline-${BASE_REQUEST.request_id}`),
    );
    expect(onDecline).toHaveBeenCalledTimes(1);
  });

  it("folds to a compact 'key saved' summary once resolved (form + message + signup gone)", () => {
    render(
      <CredentialCard
        request={BASE_REQUEST}
        resolved="saved"
        onSave={() => {}}
        onDecline={() => {}}
      />,
    );
    // Compact summary line carries the provider label + "key saved".
    const resolved = screen.getByTestId(
      `credential-card-resolved-${BASE_REQUEST.request_id}`,
    );
    expect(resolved.textContent).toContain("eBird");
    expect(resolved.textContent).toContain("key saved");
    // The whole card is the compact variant.
    expect(
      screen
        .getByTestId(`credential-card-${BASE_REQUEST.request_id}`)
        .getAttribute("data-variant"),
    ).toBe("compact");
    // The entry form is gone so the prompt can't be re-submitted.
    expect(
      screen.queryByTestId(`credential-card-form-${BASE_REQUEST.request_id}`),
    ).toBeNull();
    // The full message + signup link folded away too (compact, not full).
    expect(
      screen.queryByTestId(`credential-card-message-${BASE_REQUEST.request_id}`),
    ).toBeNull();
    expect(
      screen.queryByTestId(`credential-card-signup-${BASE_REQUEST.request_id}`),
    ).toBeNull();
  });

  it("folds to a compact 'declined' summary once declined", () => {
    render(
      <CredentialCard
        request={BASE_REQUEST}
        resolved="declined"
        onSave={() => {}}
        onDecline={() => {}}
      />,
    );
    const resolved = screen.getByTestId(
      `credential-card-resolved-${BASE_REQUEST.request_id}`,
    );
    expect(resolved.textContent).toContain("declined");
    expect(
      screen
        .getByTestId(`credential-card-${BASE_REQUEST.request_id}`)
        .getAttribute("data-variant"),
    ).toBe("compact");
    expect(
      screen.queryByTestId(`credential-card-form-${BASE_REQUEST.request_id}`),
    ).toBeNull();
  });

  it("re-expands the read-only detail via the chevron on a folded card", () => {
    render(
      <CredentialCard
        request={BASE_REQUEST}
        resolved="saved"
        onSave={() => {}}
        onDecline={() => {}}
      />,
    );
    // Detail is folded away by default.
    expect(
      screen.queryByTestId(`credential-card-detail-${BASE_REQUEST.request_id}`),
    ).toBeNull();
    // Chevron reveals the read-only detail (message + key name) — but never the
    // form (a resolved prompt can't be re-submitted).
    fireEvent.click(
      screen.getByTestId(`credential-card-expand-${BASE_REQUEST.request_id}`),
    );
    const detail = screen.getByTestId(
      `credential-card-detail-${BASE_REQUEST.request_id}`,
    );
    expect(detail.textContent).toContain(BASE_REQUEST.message);
    expect(detail.textContent).toContain(BASE_REQUEST.secret_key_name);
    expect(
      screen.queryByTestId(`credential-card-form-${BASE_REQUEST.request_id}`),
    ).toBeNull();
  });
});
