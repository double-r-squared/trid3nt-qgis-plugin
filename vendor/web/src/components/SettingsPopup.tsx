// GRACE-2 web — SettingsPopup (job-0143, sprint-12-mega Wave 4).
//
// Full-screen overlay surfacing user-facing settings:
//   - Account: email or "Anonymous mode" + sign-in CTA, sign-out.
//   - Appearance: light/dark theme toggle.
//   - About: build version + commit SHA.
//
// Centralises auth controls that previously lived in the floating top-right
// identity chip. The chip is removed entirely by job-0143; Settings is the
// only place to view/change identity now.
//
// Render pattern matches AuthGate / Mode2OfferModal: full-viewport dim
// backdrop, centred card, Esc / click-backdrop / X to dismiss.

import { useEffect, useState } from "react";
import { IconClose, IconPause } from "./icons";
import type { MapTheme } from "../Map";
import { SecretsPanel } from "./SecretsPanel";
import type { ProviderID, SecretRecord } from "../contracts";
// Agent sleep (NATE 2026-06-18; reworked 2026-06-29 for the SHARED box). The box
// is now shared by multiple users, so "sleep" must NOT stop the box out from
// under everyone. It is a PER-SESSION pause: App tears down THIS session's WS +
// clears the loaded layers and surfaces the asleep composer, but never POSTs a
// box stop. The box still auto-stops server-side once ALL sessions are idle.
// `wakeConfigured()` still gates the section so dev/LAN (no wake endpoint, box
// never auto-stops -> no asleep/wake affordance) hides it, exactly as before.
import { wakeConfigured } from "../lib/wake";
// TRID3NT LOCAL (F5, live-feedback 2026-07-08): the local build is a
// single-user product with NO auth -- the whole Account section (sign-in CTA,
// sign-out, "Anonymous mode" identity copy) gates OFF on the deployment seam.
// Cloud rendering is byte-identical when the flag is unset.
import { isLocalDeployment } from "../lib/deployment";
// job-0322 F56 — chat-opacity control. The SHARED localStorage key + the tier
// type + the read/write helpers are OWNED by Chat.tsx (Group B), which also
// applies the resulting alpha to both the desktop chat container and the
// mobile bottom-sheet. Settings only WRITES the per-user tier here; importing
// the helpers (rather than re-declaring the key) keeps the persistence
// contract single-sourced — same pattern as readChatWidth / writeChatWidth.
import {
  readChatOpacity,
  writeChatOpacity,
  type ChatOpacityTier,
} from "../Chat";
// NATE map/loading-UX polish item 1 - the bbox loading-animation enable flag.
// Settings owns the persistence write here (mirroring the chat-opacity pattern);
// App re-reads via readBboxAnimationsEnabled after the onBboxAnimationsChange
// callback fires. DEFAULT ON.
import {
  readBboxAnimationsEnabled,
  writeBboxAnimationsEnabled,
} from "../lib/bbox_progress";
// "3D terrain viz" first cut - the 3D-terrain enable toggle + the (stubbed)
// contour toggle. Settings owns the persistence write (mirroring the bbox /
// chat-opacity pattern); App re-reads via the onTerrain3dChange callback and
// threads the flags into MapView, which applies MapLibre terrain.
import {
  readTerrain3dEnabled,
  writeTerrain3dEnabled,
  readContoursEnabled,
} from "../lib/terrain_3d";
// TRID3NT LOCAL (live-feedback 2026-07-08) - "Show model thinking" toggle.
// LOCAL-ONLY (gated on isLocalDeployment below): streams the local model's
// reasoning-channel tokens into the chat as a greyed thinking block. Settings
// owns the persistence write (mirroring the bbox / terrain pattern); Chat.tsx
// reads the effective pref per turn via showThinkingPref(). DEFAULT ON.
import {
  readShowThinking,
  writeShowThinking,
} from "../lib/thinking_pref";

export interface SettingsPopupProps {
  /** Email of the signed-in user, or null if anonymous. */
  userEmail: string | null;
  /** Whether the user is signed in with a real Firebase identity. */
  isSignedIn: boolean;
  /** Current theme — drives the visible toggle state. */
  theme: MapTheme;
  /** Toggle theme handler. */
  onToggleTheme: () => void;
  /**
   * NATE item 1 - optional notify when the bbox loading-animation enable flag is
   * toggled. SettingsPopup writes the persisted flag itself; this callback lets
   * App re-read it (re-deriving the overlay state). Kept OPTIONAL so pre-existing
   * fixtures that don't plumb it still render (the toggle just won't notify App).
   */
  onBboxAnimationsChange?: (enabled: boolean) => void;
  /**
   * "3D terrain viz" first cut - optional notify when the 3D-terrain and/or
   * contour toggles flip. SettingsPopup writes the persisted flags itself; this
   * callback lets App re-read them (re-deriving the props it threads into
   * MapView). Called with the current (terrain3d, contours) flags. OPTIONAL so
   * pre-existing fixtures that don't plumb it still render (the toggles just
   * won't notify App).
   */
  onTerrain3dChange?: (state: { terrain3d: boolean; contours: boolean }) => void;
  /** Sign-out handler. App.tsx wires the real auth.signOut call. */
  onSignOut: () => void;
  /** Sign-in handler — only invoked when isSignedIn=false. */
  onSignInRequest: () => void;
  /** Close handler. App.tsx clears local visible state. */
  onClose: () => void;
  /**
   * Wave 4.10 C1: optional "View tools catalog" hook. When provided, a
   * link is surfaced under a "Tools" section that opens the ToolsCatalogPopup
   * (mounted by App.tsx). Kept optional so the existing test fixtures that
   * pre-date Wave 4.10 don't need to plumb the prop.
   */
  onOpenToolsCatalog?: () => void;
  /**
   * Wave 4.11 M7: optional "Routing quality" hook. When provided, a link
   * under the Tools section opens the RoutingQualityDashboard (mounted by
   * App.tsx). Surfaces aggregated tool-routing telemetry over the last 30
   * sessions. Kept optional for backwards-compat with older fixtures.
   */
  onOpenRoutingDashboard?: () => void;
  /**
   * job-0321 F29: optional embedded API-Keys section. When `onSecretAdd` is
   * supplied, Settings renders the SecretsPanel inline under an "API Keys"
   * section header — bundling key management INSIDE Settings so it is
   * reachable on mobile (where Settings now lives top-right). Kept OPTIONAL
   * so pre-existing SettingsPopup.test.tsx fixtures that don't plumb these
   * still render (the section is guarded on `onSecretAdd && ...`).
   *
   * Shape mirrors SecretsPopup's props exactly so App.tsx can pass the same
   * `secrets` / `currentCaseId` / `handleSecretAdd` / `handleSecretRevoke`
   * wires it previously fed to the standalone SecretsPopup.
   */
  secrets?: SecretRecord[];
  /** Active case id for the embedded SecretsPanel scope (null = user-wide). */
  caseId?: string | null;
  /** Emits the `secret-add` envelope (App wraps it on the WS). */
  onSecretAdd?: (payload: {
    provider: ProviderID;
    case_id: string | null;
    label: string | null;
    key_value: string;
  }) => void;
  /** Emits the `secret-revoke` envelope for the given secret id. */
  onSecretRevoke?: (secretId: string) => void;
  /**
   * SHARED-BOX SLEEP (NATE 2026-06-29): per-session "pause" handler. App owns
   * the teardown - close THIS session's WS cleanly, clear the loaded layers /
   * live case-view state, and surface the asleep composer - WITHOUT POSTing a
   * box stop (the shared box auto-stops server-side only once ALL sessions are
   * idle). The Agent section renders only when this is wired AND wake is
   * configured AND the user is signed in. OPTIONAL so legacy fixtures that don't
   * plumb it render unchanged (the section just stays hidden).
   */
  onSleepSession?: () => void;
}

const overlayStyle: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "rgba(0,0,0,0.55)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 9_500,
  fontFamily:
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
};

const cardStyle: React.CSSProperties = {
  background: "rgba(20,22,30,0.98)",
  // job-0283 — hairline border joins the modal family (was solid #444).
  border: "1px solid rgba(255,255,255,0.10)",
  borderRadius: 12,
  width: "min(480px, 92vw)",
  maxHeight: "85vh",
  overflowY: "auto",
  color: "#e8eaf0",
  boxShadow: "0 24px 64px rgba(0,0,0,0.55)",
  position: "relative",
  padding: "28px 30px 24px",
};

const closeBtnStyle: React.CSSProperties = {
  position: "absolute",
  top: 12,
  right: 12,
  background: "transparent",
  border: "none",
  color: "#aaa",
  fontSize: 18,
  cursor: "pointer",
  width: 28,
  height: 28,
  borderRadius: 8,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
};

const headerStyle: React.CSSProperties = {
  fontSize: 20,
  fontWeight: 600,
  margin: "0 0 16px",
  color: "#e8eaf0",
};

const sectionStyle: React.CSSProperties = {
  // job-0283 — hairline section divider (was #333), modal family.
  borderTop: "1px solid rgba(255,255,255,0.08)",
  paddingTop: 14,
  marginTop: 16,
};

const sectionTitleStyle: React.CSSProperties = {
  fontSize: 11,
  textTransform: "uppercase",
  letterSpacing: "0.06em",
  color: "#888",
  marginBottom: 8,
};

const valueStyle: React.CSSProperties = {
  fontSize: 13,
  color: "#cfd3dc",
  lineHeight: 1.55,
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: 12,
  flexWrap: "wrap",
};

const buttonStyle: React.CSSProperties = {
  background: "rgba(40,42,52,0.9)",
  // job-0283 — hairline border + 8px radius (modal-family buttons).
  border: "1px solid rgba(255,255,255,0.14)",
  borderRadius: 8,
  color: "#ddd",
  padding: "5px 12px",
  fontSize: 12,
  cursor: "pointer",
  fontFamily: "inherit",
};

const primaryButtonStyle: React.CSSProperties = {
  ...buttonStyle,
  background: "#3b82f6",
  borderColor: "#3b82f6",
  color: "#fff",
  fontWeight: 600,
};

const ctaStyle: React.CSSProperties = {
  fontSize: 11,
  color: "#7aa7ff",
  background: "transparent",
  border: "none",
  cursor: "pointer",
  padding: 0,
  textDecoration: "underline",
  fontFamily: "inherit",
};

// job-0322 F56 — the ordered tier set + their human labels for the chat-opacity
// segmented control. The tier→alpha mapping itself lives in Chat.tsx (Group B);
// Settings only chooses WHICH tier is active. "Medium" is the default and is
// deliberately MORE opaque/frosted than the historical alphas.
const OPACITY_TIERS: readonly ChatOpacityTier[] = ["low", "medium", "high"];
const OPACITY_TIER_LABELS: Record<ChatOpacityTier, string> = {
  low: "Low",
  medium: "Medium",
  high: "High",
};

const segmentStyle: React.CSSProperties = {
  display: "inline-flex",
  // job-0283 modal-family hairline border + 8px radius, matching buttonStyle.
  border: "1px solid rgba(255,255,255,0.14)",
  borderRadius: 8,
  overflow: "hidden",
};

const segmentBtnBase: React.CSSProperties = {
  background: "rgba(40,42,52,0.9)",
  border: "none",
  color: "#aab0bd",
  padding: "5px 12px",
  fontSize: 12,
  cursor: "pointer",
  fontFamily: "inherit",
};

const segmentBtnActive: React.CSSProperties = {
  ...segmentBtnBase,
  background: "#3b82f6",
  color: "#fff",
  fontWeight: 600,
};

/** Build version label. Falls back to "dev" when VITE_BUILD_SHA isn't set. */
function buildSha(): string {
  const v = (import.meta.env.VITE_BUILD_SHA as string | undefined) ?? "";
  if (!v) return "dev";
  // Display the short SHA only (first 7 chars), matching git's default.
  return v.length > 7 ? v.slice(0, 7) : v;
}

export function SettingsPopup({
  userEmail,
  isSignedIn,
  theme,
  onToggleTheme,
  onBboxAnimationsChange,
  onTerrain3dChange,
  onSignOut,
  onSignInRequest,
  onClose,
  onOpenToolsCatalog,
  onOpenRoutingDashboard,
  secrets,
  caseId,
  onSecretAdd,
  onSecretRevoke,
  onSleepSession,
}: SettingsPopupProps): JSX.Element {
  // job-0322 F56 — chat-opacity tier. Initialised from the persisted per-user
  // value (default "medium"); changing it writes through to the SHARED key so
  // Chat.tsx re-reads the new alpha on its next render. Per-USER, NOT per-case
  // — no caseId is threaded in here by design.
  const [opacityTier, setOpacityTier] = useState<ChatOpacityTier>(() =>
    readChatOpacity(),
  );

  function onSelectOpacity(tier: ChatOpacityTier): void {
    setOpacityTier(tier);
    writeChatOpacity(tier);
  }

  // The loading-grid enable flag (DEFAULT ON). Persisted here; App re-reads via
  // onBboxAnimationsChange. NATE 2026-06-24 simplification: there is now a SINGLE
  // loading visual - the polished GRID that shows only when there are truly zero
  // layers loaded (the sweeping scan was removed). This toggle governs that grid.
  const [bboxAnimEnabled, setBboxAnimEnabled] = useState<boolean>(() =>
    readBboxAnimationsEnabled(),
  );

  function onToggleBboxAnimations(): void {
    const next = !bboxAnimEnabled;
    setBboxAnimEnabled(next);
    writeBboxAnimationsEnabled(next);
    onBboxAnimationsChange?.(next);
  }

  // "3D terrain viz" first cut - the 3D-terrain enable flag (DEFAULT OFF) + the
  // contour-overlay flag (DEFAULT OFF, rendering stubbed). Persisted here; App
  // re-reads via onTerrain3dChange and threads the flags into MapView. The
  // contour toggle only renders when 3D is ON (it overlays the terrain DEM).
  const [terrain3dEnabled, setTerrain3dEnabled] = useState<boolean>(() =>
    readTerrain3dEnabled(),
  );
  // Contour overlay flag retained (read-only, default off) only to keep the
  // onTerrain3dChange payload shape; the broken/stub toggle UI was removed.
  const [contoursEnabled] = useState<boolean>(() => readContoursEnabled());

  function onToggleTerrain3d(): void {
    const next = !terrain3dEnabled;
    setTerrain3dEnabled(next);
    writeTerrain3dEnabled(next);
    onTerrain3dChange?.({ terrain3d: next, contours: contoursEnabled });
  }

  // TRID3NT LOCAL - "Show model thinking" flag (DEFAULT ON). Persisted here;
  // Chat.tsx reads the effective pref per turn via showThinkingPref(), so no
  // App callback is needed. The toggle renders only on the LOCAL build.
  const [showThinking, setShowThinking] = useState<boolean>(() =>
    readShowThinking(),
  );

  function onToggleShowThinking(): void {
    const next = !showThinking;
    setShowThinking(next);
    writeShowThinking(next);
  }

  // SHARED-BOX SLEEP (NATE 2026-06-29). Two-step: first click ARMS a confirm,
  // the second performs a PER-SESSION pause. This is a PURE client action now -
  // no network, no token, no box stop: App closes THIS session's WS, clears the
  // loaded layers, and surfaces the asleep composer. The shared box keeps
  // serving any other connected users and auto-stops server-side only once ALL
  // sessions are idle. `paused` latches so the inline honest message persists
  // and the button reads "Workspace paused" until Settings is reopened.
  const [sleepConfirming, setSleepConfirming] = useState(false);
  const [paused, setPaused] = useState(false);

  function onSleepClick(): void {
    if (paused) return;
    if (!sleepConfirming) {
      setSleepConfirming(true);
      return;
    }
    // Confirmed - per-session pause. Synchronous + local; App owns the teardown.
    setSleepConfirming(false);
    setPaused(true);
    onSleepSession?.();
  }

  // The agent-sleep section only makes sense when (a) the user is signed in,
  // (b) a wake endpoint is configured (dev/LAN never auto-stops the box, so
  // there is no asleep/wake affordance to pair the pause with), and (c) App has
  // wired the per-session teardown handler.
  const showSleepSection = isSignedIn && wakeConfigured() && !!onSleepSession;

  // Esc-to-close (memory rule "Cancellation is first-class").
  useEffect(() => {
    function onKey(e: KeyboardEvent): void {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      data-testid="grace2-settings-popup"
      role="dialog"
      aria-modal="true"
      aria-label="Settings"
      style={overlayStyle}
      onClick={onClose}
    >
      <div
        data-testid="grace2-settings-popup-card"
        style={cardStyle}
        onClick={(e) => e.stopPropagation()}
      >
        <button
          data-testid="grace2-settings-popup-close"
          aria-label="Close settings"
          onClick={onClose}
          style={closeBtnStyle}
        >
          <IconClose size={18} />
        </button>
        <h2 style={headerStyle}>Settings</h2>

        {/* Show model thinking (LOCAL only, F8): stream the local model's
            reasoning tokens as a grey foldable block. Default ON. */}
        {isLocalDeployment() && (
          <div style={sectionStyle}>
            <div style={sectionTitleStyle}>Model thinking</div>
            <label
              style={{ ...valueStyle, cursor: "pointer" }}
              data-testid="grace2-settings-show-thinking"
            >
              <span>Show model thinking</span>
              <input
                type="checkbox"
                checked={showThinking}
                onChange={onToggleShowThinking}
                aria-label="Show model thinking tokens"
              />
            </label>
          </div>
        )}

        {/* Account section. Hidden entirely in the LOCAL build (F5): single
            user, no login -- an account/sign-in surface would be a cloud
            fingerprint and a lie. */}
        {!isLocalDeployment() && (
          <div style={sectionStyle}>
            <div style={sectionTitleStyle}>Account</div>
            <div style={valueStyle}>
              <span data-testid="grace2-settings-account-label">
                {isSignedIn && userEmail
                  ? userEmail
                  : "Anonymous mode"}
              </span>
              {isSignedIn ? (
                <button
                  data-testid="grace2-settings-signout"
                  onClick={onSignOut}
                  style={buttonStyle}
                  aria-label="Sign out"
                >
                  Sign out
                </button>
              ) : (
                <button
                  data-testid="grace2-settings-signin"
                  onClick={onSignInRequest}
                  style={primaryButtonStyle}
                  aria-label="Sign in to save your work"
                >
                  Sign in
                </button>
              )}
            </div>
            {!isSignedIn && (
              <div
                data-testid="grace2-settings-account-cta"
                style={{
                  fontSize: 11,
                  color: "#9aa0ad",
                  marginTop: 4,
                  lineHeight: 1.5,
                }}
              >
                Sign in to save your work and sync Cases across devices.
              </div>
            )}
          </div>
        )}

        {/* Appearance section */}
        <div style={sectionStyle}>
          <div style={sectionTitleStyle}>Appearance</div>
          <div style={valueStyle}>
            <span>Theme</span>
            <button
              data-testid="grace2-settings-theme-toggle"
              onClick={onToggleTheme}
              style={buttonStyle}
              aria-pressed={theme === "dark"}
              aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
            >
              {theme === "dark" ? "Dark" : "Light"}
            </button>
          </div>
          {/* job-0322 F56 — Chat opacity. Discrete 3-state segmented toggle
              (low / medium / high) mapping cleanly onto Chat.tsx's documented
              alpha bands — deliberately NOT a free-form slider. */}
          <div style={{ ...valueStyle, marginTop: 10 }}>
            <span id="grace2-settings-opacity-label">Chat opacity</span>
            <div
              data-testid="grace2-settings-chat-opacity"
              role="radiogroup"
              aria-labelledby="grace2-settings-opacity-label"
              style={segmentStyle}
            >
              {OPACITY_TIERS.map((tier) => {
                const active = opacityTier === tier;
                return (
                  <button
                    key={tier}
                    data-testid={`grace2-settings-chat-opacity-${tier}`}
                    role="radio"
                    aria-checked={active}
                    aria-label={`Chat opacity ${OPACITY_TIER_LABELS[tier]}`}
                    onClick={() => onSelectOpacity(tier)}
                    style={active ? segmentBtnActive : segmentBtnBase}
                  >
                    {OPACITY_TIER_LABELS[tier]}
                  </button>
                );
              })}
            </div>
          </div>
          {/* Map loading-grid toggle (DEFAULT ON). Governs the single polished
              GRID that paints over the AOI only when there are truly zero layers
              loaded yet (the sweeping scan was removed, NATE 2026-06-24). */}
          <div style={{ ...valueStyle, marginTop: 10 }}>
            <span>Map loading animations</span>
            <button
              data-testid="grace2-settings-bbox-animations-toggle"
              onClick={onToggleBboxAnimations}
              style={buttonStyle}
              role="switch"
              aria-checked={bboxAnimEnabled}
              aria-label={`Turn map loading animations ${bboxAnimEnabled ? "off" : "on"}`}
            >
              {bboxAnimEnabled ? "On" : "Off"}
            </button>
          </div>
          {/* "3D terrain viz" first cut - 3D terrain toggle (DEFAULT OFF).
              When on, MapView enables MapLibre terrain (terrain-RGB DEM +
              hillshade + sky) and unlocks two-finger pitch/rotate. */}
          <div style={{ ...valueStyle, marginTop: 10 }}>
            <span>3D terrain</span>
            <button
              data-testid="grace2-settings-terrain-3d-toggle"
              onClick={onToggleTerrain3d}
              style={buttonStyle}
              role="switch"
              aria-checked={terrain3dEnabled}
              aria-label={`Turn 3D terrain ${terrain3dEnabled ? "off" : "on"}`}
            >
              {terrain3dEnabled ? "On" : "Off"}
            </button>
          </div>
        </div>

        {/* Agent section (NATE 2026-06-29, SHARED box) - "Put agent to sleep"
            is now a PER-SESSION pause, not a box-wide stop. It tears down THIS
            session (WS + loaded layers) and surfaces the asleep composer while
            leaving the shared box up for anyone else; the box auto-stops
            server-side only once ALL sessions are idle. Signed-in + wake
            configured + handler wired. Two-step confirm; the honest outcome is
            surfaced inline. */}
        {showSleepSection && (
          <div style={sectionStyle} data-testid="grace2-settings-agent">
            <div style={sectionTitleStyle}>Agent</div>
            <div style={valueStyle}>
              <span>
                {paused
                  ? "Your workspace is paused."
                  : sleepConfirming
                    ? "Pause your workspace? Your loaded layers clear here and you reconnect when you return."
                    : "Pause your workspace to free up resources. Your session here clears; reconnect anytime."}
              </span>
              <button
                data-testid="grace2-settings-agent-sleep"
                onClick={onSleepClick}
                disabled={paused}
                style={{
                  ...buttonStyle,
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                  opacity: paused ? 0.6 : 1,
                  cursor: paused ? "default" : "pointer",
                }}
                aria-label={
                  paused
                    ? "Workspace paused"
                    : sleepConfirming
                      ? "Confirm pausing your workspace"
                      : "Put agent to sleep"
                }
              >
                <IconPause size={14} />
                {paused
                  ? "Workspace paused"
                  : sleepConfirming
                    ? "Confirm pause"
                    : "Put agent to sleep"}
              </button>
            </div>
            {paused && (
              <div
                data-testid="grace2-settings-agent-sleep-status"
                role="status"
                style={{
                  fontSize: 11,
                  marginTop: 6,
                  lineHeight: 1.5,
                  color: "#7fd18a",
                }}
              >
                Workspace paused. Your session is cleared here -- reopen to
                reconnect. The shared agent stays available for anyone else who
                is connected.
              </div>
            )}
          </div>
        )}

        {/* Tools section (Wave 4.10 C1 + Wave 4.11 M7) — only when at least
            one tools-area hook is wired. */}
        {(onOpenToolsCatalog || onOpenRoutingDashboard) && (
          <div style={sectionStyle}>
            <div style={sectionTitleStyle}>Tools</div>
            {onOpenToolsCatalog && (
              <div style={valueStyle}>
                <span>Browse the agent's tool catalog</span>
                <button
                  data-testid="grace2-settings-open-tools-catalog"
                  onClick={onOpenToolsCatalog}
                  style={buttonStyle}
                  aria-label="View all tools"
                >
                  View all tools
                </button>
              </div>
            )}
            {onOpenRoutingDashboard && (
              <div style={{ ...valueStyle, marginTop: 10 }}>
                <span>Inspect tool-routing telemetry</span>
                <button
                  data-testid="grace2-settings-open-routing-dashboard"
                  onClick={onOpenRoutingDashboard}
                  style={buttonStyle}
                  aria-label="Routing quality"
                >
                  Routing quality
                </button>
              </div>
            )}
          </div>
        )}

        {/* API Keys section (job-0321 F29) — bundles the per-Case Tier-2
            key-entry surface INSIDE Settings so it is reachable from the
            mobile top-right Settings entry (the standalone SecretsPopup is
            retired). Guarded on `onSecretAdd` so legacy fixtures that don't
            plumb the secrets props render unchanged. */}
        {onSecretAdd && onSecretRevoke && (
          <div style={sectionStyle} data-testid="grace2-settings-api-keys">
            <div style={sectionTitleStyle}>API Keys</div>
            <SecretsPanel
              secrets={secrets ?? []}
              caseId={caseId ?? null}
              onSecretAdd={onSecretAdd}
              onSecretRevoke={onSecretRevoke}
            />
          </div>
        )}

        {/* About section */}
        <div style={sectionStyle}>
          <div style={sectionTitleStyle}>About</div>
          <div style={valueStyle}>
            <span>TRID3NT</span>
            <span
              data-testid="grace2-settings-build-sha"
              style={{ fontFamily: "monospace", fontSize: 12, color: "#aaa" }}
            >
              {buildSha()}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

export { ctaStyle };
