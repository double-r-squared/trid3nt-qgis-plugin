// GRACE-2 web — BottomRowButtons tests (job-0143, sprint-12-mega Wave 4).
//
// job-0321 F29 — the standalone [🔑 Secrets] pill is retired: API-key
// management now lives INSIDE the Settings popup. `onOpenSecrets` is now
// OPTIONAL; the Secrets pill renders ONLY when a caller still supplies it.
// These tests cover both: the new default (Settings-only) and the legacy
// path (Secrets pill still rendered when `onOpenSecrets` is passed).

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { BottomRowButtons } from "./components/BottomRowButtons";

afterEach(() => cleanup());

describe("BottomRowButtons", () => {
  it("renders the Settings button (no Secrets pill by default)", () => {
    render(<BottomRowButtons onOpenSettings={vi.fn()} />);
    expect(screen.getByTestId("grace2-bottom-row-buttons")).toBeTruthy();
    expect(screen.getByTestId("grace2-bottom-row-settings")).toBeTruthy();
    // job-0321 F29 — Secrets pill is gone unless onOpenSecrets is supplied.
    expect(screen.queryByTestId("grace2-bottom-row-secrets")).toBeNull();
  });

  it("Settings button invokes onOpenSettings", () => {
    const onOpenSettings = vi.fn();
    render(<BottomRowButtons onOpenSettings={onOpenSettings} />);
    fireEvent.click(screen.getByTestId("grace2-bottom-row-settings"));
    expect(onOpenSettings).toHaveBeenCalledTimes(1);
  });

  // job-0321 F29 — legacy callers that still pass onOpenSecrets keep the pill.
  it("renders the Secrets pill ONLY when onOpenSecrets is supplied", () => {
    const onOpenSecrets = vi.fn();
    render(
      <BottomRowButtons
        onOpenSettings={vi.fn()}
        onOpenSecrets={onOpenSecrets}
      />,
    );
    const secrets = screen.getByTestId("grace2-bottom-row-secrets");
    expect(secrets).toBeTruthy();
    fireEvent.click(secrets);
    expect(onOpenSecrets).toHaveBeenCalledTimes(1);
  });

  // job-0278 — mobile drawer footer variant.
  it("defaults to the floating (absolute bottom-left) variant", () => {
    render(<BottomRowButtons onOpenSettings={vi.fn()} />);
    const row = screen.getByTestId("grace2-bottom-row-buttons");
    expect(row).toHaveAttribute("data-variant", "floating");
    expect(row.style.position).toBe("absolute");
  });

  it("inline variant renders in normal flow (mobile drawer footer)", () => {
    render(
      <BottomRowButtons onOpenSettings={vi.fn()} variant="inline" />,
    );
    const row = screen.getByTestId("grace2-bottom-row-buttons");
    expect(row).toHaveAttribute("data-variant", "inline");
    expect(row.style.position).toBe("");
    // Settings pill still present + wired.
    expect(screen.getByTestId("grace2-bottom-row-settings")).toBeTruthy();
  });

  // NATE 2026-06-22 — desktop Settings is now a SQUARE icon-only button (drop
  // the label, match the square button / expander-icon family), still on the
  // rail-surface hairline border. Supersedes the job-0283 full-pill assertion.
  it("floating variant Settings is a square icon-only button (no label, hairline border)", () => {
    render(<BottomRowButtons onOpenSettings={vi.fn()} />);
    const btn = screen.getByTestId("grace2-bottom-row-settings");
    expect(btn.style.borderRadius).toBe("8px");
    expect(btn.style.width).toBe("34px");
    expect(btn.style.height).toBe("34px");
    expect(btn.style.border.replace(/\s/g, "")).toContain(
      "rgba(255,255,255,0.08)",
    );
    // Icon-only on desktop: no "Settings" text label rendered.
    expect(screen.queryByText("Settings")).toBeNull();
    // The accessible name is preserved via aria-label.
    expect(btn.getAttribute("aria-label")).toBe("Open settings");
  });

  it("inline (mobile drawer) variant KEEPS the Settings text label", () => {
    render(<BottomRowButtons onOpenSettings={vi.fn()} variant="inline" />);
    expect(screen.getByText("Settings")).toBeTruthy();
  });

  // job-0284 — mobile map-centric pass: the inline (drawer footer) pills
  // float directly over the map (drawer surface is transparent now), so
  // they joined the translucent hairline-card family. Deliberate update of
  // the job-0280 pin (radius 14 / #444) — this job IS the mobile pass.
  it("inline variant pills float as translucent hairline cards (job-0284)", () => {
    render(
      <BottomRowButtons onOpenSettings={vi.fn()} variant="inline" />,
    );
    const pill = screen.getByTestId("grace2-bottom-row-settings");
    expect(pill.style.borderRadius).toBe("999px");
    expect(pill.style.border.replace(/\s/g, "")).toContain(
      "rgba(255,255,255,0.10)",
    );
    // Translucent (alpha < 1) so the map reads through.
    expect(pill.style.background.replace(/\s/g, "")).toContain(
      "rgba(18,19,24,0.85)",
    );
  });
});
