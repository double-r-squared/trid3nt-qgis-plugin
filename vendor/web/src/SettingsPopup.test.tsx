// GRACE-2 web — SettingsPopup tests (job-0143, sprint-12-mega Wave 4).
//
// Verifies:
//   1. SettingsPopup renders Account / Appearance / About sections.
//   2. Email is displayed when isSignedIn=true; "Anonymous mode" + Sign-in
//      CTA when isSignedIn=false.
//   3. Sign-out button calls onSignOut.
//   4. Theme toggle calls onToggleTheme.
//   5. Close (X) button calls onClose.
//   6. Click on backdrop closes the popup; click on card does NOT close.
//   7. Esc keypress closes the popup.

import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { SettingsPopup } from "./components/SettingsPopup";
// job-0322 F56 — round-trip access to the SHARED chat-opacity helpers.
// Chat.tsx (Group B) OWNS these; importing them here lets the tests assert the
// real persisted tier rather than poking a hard-coded localStorage key, so the
// test stays correct even if Group B renames the underlying key.
import { readChatOpacity, writeChatOpacity } from "./Chat";
// NATE item 1 - the bbox loading-animation enable flag helpers (real impl; the
// SettingsPopup toggle persists through these).
import {
  readBboxAnimationsEnabled,
  writeBboxAnimationsEnabled,
} from "./lib/bbox_progress";
// "3D terrain viz" first cut - the 3D-terrain + contour enable-flag helpers
// (real impl; the SettingsPopup toggles persist through these).
import {
  readTerrain3dEnabled,
  writeTerrain3dEnabled,
  readContoursEnabled,
} from "./lib/terrain_3d";

// job-0322 F56 — mock Chat.tsx with a localStorage-backed fake implementing the
// AGREED per-user opacity contract (tiers low|medium|high, default "medium").
// This decouples SettingsPopup's wiring test from Group B's landing order:
// SettingsPopup imports readChatOpacity/writeChatOpacity from "../Chat", and we
// verify it (a) initialises from the persisted tier, (b) defaults to "medium",
// and (c) writes the chosen tier back through the shared helper. The full
// tier→alpha application lives in Chat.tsx and is exercised by Chat's own tests.
// job-0322 F56 reactivity fix — the fake writeChatOpacity MUST mirror the real
// one's same-tab reactivity contract: persist the tier AND dispatch
// CHAT_OPACITY_CHANGED_EVENT on window (a plain localStorage write does not fire
// `storage` in the same tab, so Chat can't react without this event). The
// "dispatches on tier click" test below asserts SettingsPopup's onClick reaches
// this dispatch path.
const CHAT_OPACITY_CHANGED_EVENT = "grace2:chat-opacity-changed";
vi.mock("./Chat", () => {
  const LS_KEY = "grace2.chatOpacityTier";
  const EVT = "grace2:chat-opacity-changed";
  const TIERS = ["low", "medium", "high"] as const;
  type Tier = (typeof TIERS)[number];
  const clamp = (t: unknown): Tier =>
    (TIERS as readonly string[]).includes(t as string) ? (t as Tier) : "medium";
  return {
    CHAT_OPACITY_CHANGED_EVENT: EVT,
    readChatOpacity(): Tier {
      try {
        return clamp(localStorage.getItem(LS_KEY));
      } catch {
        return "medium";
      }
    },
    writeChatOpacity(tier: Tier): void {
      const normalized = clamp(tier);
      try {
        localStorage.setItem(LS_KEY, normalized);
      } catch {
        /* non-fatal */
      }
      try {
        window.dispatchEvent(
          new CustomEvent(EVT, { detail: normalized }),
        );
      } catch {
        /* non-fatal */
      }
    },
  };
});

// SHARED-BOX SLEEP (NATE 2026-06-29) - force wake CONFIGURED so the Agent
// section renders (it is gated on wakeConfigured(), which reads import.meta.env
// and is false by default in tests). SettingsPopup imports ONLY wakeConfigured
// from "../lib/wake" now (the box-stop requestSleep call was removed - sleep is
// a pure client per-session pause), so this minimal mock is sufficient.
vi.mock("./lib/wake", () => ({
  wakeConfigured: () => true,
}));

afterEach(() => cleanup());
beforeEach(() => localStorage.clear());

const defaultProps = {
  userEmail: "user@example.com",
  isSignedIn: true,
  theme: "light" as const,
  onToggleTheme: vi.fn(),
  onSignOut: vi.fn(),
  onSignInRequest: vi.fn(),
  onClose: vi.fn(),
};

describe("SettingsPopup", () => {
  it("renders Account / Appearance / About sections", () => {
    render(<SettingsPopup {...defaultProps} />);
    expect(screen.getByTestId("grace2-settings-popup")).toBeTruthy();
    expect(screen.getByText("Account")).toBeTruthy();
    expect(screen.getByText("Appearance")).toBeTruthy();
    expect(screen.getByText("About")).toBeTruthy();
  });

  it("displays the user email when isSignedIn=true", () => {
    render(<SettingsPopup {...defaultProps} userEmail="alice@example.com" isSignedIn={true} />);
    expect(
      screen.getByTestId("grace2-settings-account-label").textContent,
    ).toBe("alice@example.com");
    expect(screen.getByTestId("grace2-settings-signout")).toBeTruthy();
  });

  it("displays Anonymous mode + Sign-in CTA when isSignedIn=false", () => {
    render(
      <SettingsPopup {...defaultProps} userEmail={null} isSignedIn={false} />,
    );
    expect(
      screen.getByTestId("grace2-settings-account-label").textContent,
    ).toBe("Anonymous mode");
    expect(screen.getByTestId("grace2-settings-signin")).toBeTruthy();
    expect(
      screen.getByTestId("grace2-settings-account-cta").textContent,
    ).toMatch(/Sign in to save/);
  });

  it("Sign-out button invokes onSignOut", () => {
    const onSignOut = vi.fn();
    render(<SettingsPopup {...defaultProps} onSignOut={onSignOut} />);
    fireEvent.click(screen.getByTestId("grace2-settings-signout"));
    expect(onSignOut).toHaveBeenCalledTimes(1);
  });

  it("theme toggle invokes onToggleTheme", () => {
    const onToggleTheme = vi.fn();
    render(<SettingsPopup {...defaultProps} onToggleTheme={onToggleTheme} />);
    fireEvent.click(screen.getByTestId("grace2-settings-theme-toggle"));
    expect(onToggleTheme).toHaveBeenCalledTimes(1);
  });

  // NATE item 1 - the map loading-animation toggle (DEFAULT ON).
  it("map loading animations toggle defaults ON and persists OFF on click", () => {
    const onBboxAnimationsChange = vi.fn();
    render(
      <SettingsPopup
        {...defaultProps}
        onBboxAnimationsChange={onBboxAnimationsChange}
      />,
    );
    const toggle = screen.getByTestId("grace2-settings-bbox-animations-toggle");
    // DEFAULT ON.
    expect(toggle).toHaveAttribute("aria-checked", "true");
    expect(toggle.textContent).toBe("On");
    fireEvent.click(toggle);
    // Persisted OFF + the change callback fired with false.
    expect(readBboxAnimationsEnabled()).toBe(false);
    expect(onBboxAnimationsChange).toHaveBeenCalledWith(false);
    expect(
      screen.getByTestId("grace2-settings-bbox-animations-toggle"),
    ).toHaveAttribute("aria-checked", "false");
  });

  it("map loading animations toggle initialises from the persisted OFF value", () => {
    writeBboxAnimationsEnabled(false);
    render(<SettingsPopup {...defaultProps} />);
    expect(
      screen.getByTestId("grace2-settings-bbox-animations-toggle"),
    ).toHaveAttribute("aria-checked", "false");
  });

  // "3D terrain viz" first cut - the 3D-terrain toggle (DEFAULT OFF).
  it("3D terrain toggle defaults OFF and persists ON on click", () => {
    const onTerrain3dChange = vi.fn();
    render(
      <SettingsPopup {...defaultProps} onTerrain3dChange={onTerrain3dChange} />,
    );
    const toggle = screen.getByTestId("grace2-settings-terrain-3d-toggle");
    // DEFAULT OFF.
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(toggle.textContent).toBe("Off");
    fireEvent.click(toggle);
    // Persisted ON + the change callback fired with terrain3d:true.
    expect(readTerrain3dEnabled()).toBe(true);
    expect(onTerrain3dChange).toHaveBeenCalledWith({
      terrain3d: true,
      contours: false,
    });
    expect(
      screen.getByTestId("grace2-settings-terrain-3d-toggle"),
    ).toHaveAttribute("aria-checked", "true");
  });

  it("3D terrain toggle initialises from the persisted ON value", () => {
    writeTerrain3dEnabled(true);
    render(<SettingsPopup {...defaultProps} />);
    expect(
      screen.getByTestId("grace2-settings-terrain-3d-toggle"),
    ).toHaveAttribute("aria-checked", "true");
  });

  it("contour toggle is hidden until 3D terrain is ON, then persists + stays a stub", () => {
    render(<SettingsPopup {...defaultProps} />);
    // Hidden while 3D is off (it overlays the terrain DEM).
    expect(
      screen.queryByTestId("grace2-settings-contours-toggle"),
    ).toBeNull();
    // Turn 3D on -> the contour toggle appears (default OFF).
    fireEvent.click(screen.getByTestId("grace2-settings-terrain-3d-toggle"));
    const contour = screen.getByTestId("grace2-settings-contours-toggle");
    expect(contour).toHaveAttribute("aria-checked", "false");
    // Toggling it persists the flag (rendering is stubbed in MapView).
    fireEvent.click(contour);
    expect(readContoursEnabled()).toBe(true);
  });

  it("close button invokes onClose", () => {
    const onClose = vi.fn();
    render(<SettingsPopup {...defaultProps} onClose={onClose} />);
    fireEvent.click(screen.getByTestId("grace2-settings-popup-close"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("backdrop click closes; card click does NOT close", () => {
    const onClose = vi.fn();
    render(<SettingsPopup {...defaultProps} onClose={onClose} />);
    // Click the backdrop.
    fireEvent.click(screen.getByTestId("grace2-settings-popup"));
    expect(onClose).toHaveBeenCalledTimes(1);
    // Click the card itself — should NOT bubble.
    fireEvent.click(screen.getByTestId("grace2-settings-popup-card"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("Esc keypress closes the popup", () => {
    const onClose = vi.fn();
    render(<SettingsPopup {...defaultProps} onClose={onClose} />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("renders a build SHA in the About section", () => {
    render(<SettingsPopup {...defaultProps} />);
    expect(screen.getByTestId("grace2-settings-build-sha").textContent).toBeTruthy();
  });

  // job-0321 F29 — API-key entry bundled inside Settings.
  describe("embedded API Keys section (F29)", () => {
    it("does NOT render the API Keys section when secrets props are absent", () => {
      // Legacy fixtures (defaultProps) don't plumb the secrets props — the
      // section must stay hidden so they render unchanged.
      render(<SettingsPopup {...defaultProps} />);
      expect(screen.queryByTestId("grace2-settings-api-keys")).toBeNull();
      expect(screen.queryByTestId("grace2-secrets-panel")).toBeNull();
    });

    it("renders the embedded SecretsPanel under an 'API Keys' header when wired", () => {
      render(
        <SettingsPopup
          {...defaultProps}
          secrets={[]}
          caseId={null}
          onSecretAdd={vi.fn()}
          onSecretRevoke={vi.fn()}
        />,
      );
      expect(screen.getByTestId("grace2-settings-api-keys")).toBeTruthy();
      expect(screen.getByText("API Keys")).toBeTruthy();
      // The SecretsPanel itself is rendered inline (its own data-testid).
      expect(screen.getByTestId("grace2-secrets-panel")).toBeTruthy();
    });

    it("passes secrets + caseId through to the embedded SecretsPanel", () => {
      const secrets = [
        {
          secret_id: "s1",
          provider: "ebird" as const,
          label: "my-ebird-key",
          is_active: true,
          last_used_at: null,
          vault_ref: "vault://abc",
          case_id: "case-7",
          added_at: "2026-01-01T00:00:00Z",
        },
      ];
      render(
        <SettingsPopup
          {...defaultProps}
          secrets={secrets}
          caseId="case-7"
          onSecretAdd={vi.fn()}
          onSecretRevoke={vi.fn()}
        />,
      );
      // The active secret row surfaces inside the embedded panel.
      expect(screen.getByTestId("grace2-secret-row-s1")).toBeTruthy();
      // The "This Case" scope radio is enabled because a caseId is present.
      const caseRadio = screen.getByTestId(
        "grace2-secret-scope-case",
      ) as HTMLInputElement;
      expect(caseRadio.disabled).toBe(false);
    });

    it("forwards add/revoke callbacks from the embedded panel", () => {
      const onSecretRevoke = vi.fn();
      const secrets = [
        {
          secret_id: "s2",
          provider: "nws" as const,
          label: null,
          is_active: true,
          last_used_at: null,
          vault_ref: "vault://def",
          case_id: null,
          added_at: "2026-01-01T00:00:00Z",
        },
      ];
      render(
        <SettingsPopup
          {...defaultProps}
          secrets={secrets}
          caseId={null}
          onSecretAdd={vi.fn()}
          onSecretRevoke={onSecretRevoke}
        />,
      );
      fireEvent.click(screen.getByTestId("grace2-secret-revoke-s2"));
      expect(onSecretRevoke).toHaveBeenCalledWith("s2");
    });

    it("only requires onSecretAdd AND onSecretRevoke together to show the section", () => {
      // onSecretAdd alone (no revoke) should not render the section — both
      // wires are required so the embedded panel is fully functional.
      render(
        <SettingsPopup
          {...defaultProps}
          secrets={[]}
          caseId={null}
          onSecretAdd={vi.fn()}
        />,
      );
      expect(screen.queryByTestId("grace2-settings-api-keys")).toBeNull();
    });
  });

  // job-0322 F56 — Chat opacity control (Settings side: renders + writes the
  // shared per-user tier; Chat.tsx owns reading & applying the alpha).
  describe("Chat opacity control (F56)", () => {
    it("renders a 3-state segmented control inside Appearance", () => {
      render(<SettingsPopup {...defaultProps} />);
      expect(screen.getByTestId("grace2-settings-chat-opacity")).toBeTruthy();
      expect(screen.getByTestId("grace2-settings-chat-opacity-low")).toBeTruthy();
      expect(
        screen.getByTestId("grace2-settings-chat-opacity-medium"),
      ).toBeTruthy();
      expect(screen.getByTestId("grace2-settings-chat-opacity-high")).toBeTruthy();
      // Labelled "Chat opacity" so it reads cleanly next to Theme.
      expect(screen.getByText("Chat opacity")).toBeTruthy();
    });

    it("defaults to MEDIUM when nothing is persisted", () => {
      // localStorage is cleared by beforeEach — the control falls back to the
      // documented MEDIUM default (more opaque/frosted than the legacy alphas).
      render(<SettingsPopup {...defaultProps} />);
      const medium = screen.getByTestId("grace2-settings-chat-opacity-medium");
      expect(medium.getAttribute("aria-checked")).toBe("true");
      expect(
        screen
          .getByTestId("grace2-settings-chat-opacity-low")
          .getAttribute("aria-checked"),
      ).toBe("false");
      expect(
        screen
          .getByTestId("grace2-settings-chat-opacity-high")
          .getAttribute("aria-checked"),
      ).toBe("false");
    });

    it("initialises from the persisted tier (not the default)", () => {
      // A previously-saved "high" tier must drive the initial active segment.
      writeChatOpacity("high");
      render(<SettingsPopup {...defaultProps} />);
      expect(
        screen
          .getByTestId("grace2-settings-chat-opacity-high")
          .getAttribute("aria-checked"),
      ).toBe("true");
      expect(
        screen
          .getByTestId("grace2-settings-chat-opacity-medium")
          .getAttribute("aria-checked"),
      ).toBe("false");
    });

    it("persists the chosen tier through the shared helper (round-trip)", () => {
      render(<SettingsPopup {...defaultProps} />);
      // Choose LOW.
      fireEvent.click(screen.getByTestId("grace2-settings-chat-opacity-low"));
      expect(readChatOpacity()).toBe("low");
      expect(
        screen
          .getByTestId("grace2-settings-chat-opacity-low")
          .getAttribute("aria-checked"),
      ).toBe("true");
      // Switch to HIGH — the shared key updates again.
      fireEvent.click(screen.getByTestId("grace2-settings-chat-opacity-high"));
      expect(readChatOpacity()).toBe("high");
      expect(
        screen
          .getByTestId("grace2-settings-chat-opacity-high")
          .getAttribute("aria-checked"),
      ).toBe("true");
      // And the previously-active LOW is now deselected.
      expect(
        screen
          .getByTestId("grace2-settings-chat-opacity-low")
          .getAttribute("aria-checked"),
      ).toBe("false");
    });

    it("exposes the control as a radiogroup of radios for a11y", () => {
      render(<SettingsPopup {...defaultProps} />);
      const group = screen.getByTestId("grace2-settings-chat-opacity");
      expect(group.getAttribute("role")).toBe("radiogroup");
      const radios = screen.getAllByRole("radio");
      // Exactly the three opacity tiers.
      expect(radios.length).toBe(3);
    });

    // job-0322 F56 FIX — clicking a tier must reach the same-tab reactivity bus
    // so a mounted Chat re-applies the alpha LIVE (no reload). SettingsPopup
    // calls writeChatOpacity (mocked here to mirror the real dispatch), so a
    // window listener for CHAT_OPACITY_CHANGED_EVENT must fire with the picked
    // tier in detail. This is the Settings half of the reactive contract Chat's
    // useEffect subscribes to.
    it("dispatches the chat-opacity-changed event when a tier is clicked", () => {
      const details: (string | null)[] = [];
      const onChange = (e: Event): void => {
        details.push((e as CustomEvent<string>).detail ?? null);
      };
      window.addEventListener(CHAT_OPACITY_CHANGED_EVENT, onChange);
      try {
        render(<SettingsPopup {...defaultProps} />);
        fireEvent.click(screen.getByTestId("grace2-settings-chat-opacity-low"));
        fireEvent.click(screen.getByTestId("grace2-settings-chat-opacity-high"));
      } finally {
        window.removeEventListener(CHAT_OPACITY_CHANGED_EVENT, onChange);
      }
      // One dispatch per click, each carrying the chosen tier — proving the
      // live re-apply path is reachable from the Settings control.
      expect(details).toEqual(["low", "high"]);
    });
  });

  // SHARED-BOX SLEEP (NATE 2026-06-29) - "Put agent to sleep" is now a
  // PER-SESSION pause (no box-wide stop). The Agent section renders only when
  // signed in + wake configured (mocked true here) + onSleepSession wired, and
  // a two-step confirm fires the local teardown handler exactly once.
  describe("Agent sleep is a per-session pause (shared box)", () => {
    it("hides the Agent section unless signed in AND onSleepSession is wired", () => {
      // Signed in but no handler -> hidden (App always wires it; legacy
      // fixtures that don't render unchanged).
      const { rerender } = render(<SettingsPopup {...defaultProps} />);
      expect(screen.queryByTestId("grace2-settings-agent")).toBeNull();
      // Handler wired but signed OUT -> still hidden.
      rerender(
        <SettingsPopup
          {...defaultProps}
          isSignedIn={false}
          onSleepSession={vi.fn()}
        />,
      );
      expect(screen.queryByTestId("grace2-settings-agent")).toBeNull();
    });

    it("renders the Agent section when signed in + handler wired", () => {
      render(<SettingsPopup {...defaultProps} onSleepSession={vi.fn()} />);
      expect(screen.getByTestId("grace2-settings-agent")).toBeTruthy();
      expect(screen.getByTestId("grace2-settings-agent-sleep")).toBeTruthy();
    });

    it("two-step confirm fires onSleepSession once and shows the honest paused message", () => {
      const onSleepSession = vi.fn();
      render(
        <SettingsPopup {...defaultProps} onSleepSession={onSleepSession} />,
      );
      const btn = screen.getByTestId("grace2-settings-agent-sleep");
      // First click ARMS the confirm - no teardown yet.
      fireEvent.click(btn);
      expect(onSleepSession).not.toHaveBeenCalled();
      expect(btn.textContent).toMatch(/Confirm pause/);
      // Second click performs the per-session pause exactly once.
      fireEvent.click(btn);
      expect(onSleepSession).toHaveBeenCalledTimes(1);
      // Honest message: paused for THEM, session cleared, shared agent stays up.
      const status = screen.getByTestId("grace2-settings-agent-sleep-status");
      expect(status.textContent).toMatch(/Workspace paused/);
      expect(status.textContent).toMatch(/session is cleared/);
      expect(status.textContent).toMatch(/shared agent stays available/);
      // The button latches to a disabled "Workspace paused" state.
      expect(screen.getByTestId("grace2-settings-agent-sleep")).toHaveProperty(
        "disabled",
        true,
      );
    });

    it("does not fire again once paused", () => {
      const onSleepSession = vi.fn();
      render(
        <SettingsPopup {...defaultProps} onSleepSession={onSleepSession} />,
      );
      const btn = screen.getByTestId("grace2-settings-agent-sleep");
      fireEvent.click(btn); // arm
      fireEvent.click(btn); // confirm -> paused
      fireEvent.click(btn); // disabled no-op
      expect(onSleepSession).toHaveBeenCalledTimes(1);
    });
  });
});
