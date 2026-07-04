// GRACE-2 web — SaveGateModal (job-0143, sprint-12-mega Wave 4).
//
// One-shot disclaimer rendered when an anonymous user attempts a
// save-triggering action (create / rename / archive / delete a Case;
// add a layer; etc.). Driven by useSaveGate. Replaces the always-on
// "Sign in to save" persistence chip.

import { useEffect } from "react";

export interface SaveGateModalProps {
  /** Friendly label of the action being gated (e.g. "Create a new Case"). */
  pendingKind: string | null;
  onSignIn: () => void;
  onContinueAnyway: () => void;
  onDismiss: () => void;
}

const overlayStyle: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "rgba(0,0,0,0.5)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 9_800,
  fontFamily:
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
};

const cardStyle: React.CSSProperties = {
  background: "rgba(20,22,30,0.98)",
  // job-0283 — hairline border joins the modal family (was solid #444).
  border: "1px solid rgba(255,255,255,0.10)",
  borderRadius: 12,
  padding: "22px 24px",
  width: "min(420px, 92vw)",
  color: "#e8eaf0",
  boxShadow: "0 24px 64px rgba(0,0,0,0.55)",
};

const titleStyle: React.CSSProperties = {
  margin: "0 0 6px",
  fontSize: 16,
  fontWeight: 600,
  color: "#e8eaf0",
};

const bodyStyle: React.CSSProperties = {
  fontSize: 13,
  color: "#c8ccd6",
  lineHeight: 1.55,
  marginBottom: 18,
};

const rowStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "row",
  justifyContent: "flex-end",
  gap: 8,
  flexWrap: "wrap",
};

const buttonBase: React.CSSProperties = {
  // job-0283 — hairline border + 8px radius (modal-family buttons).
  border: "1px solid rgba(255,255,255,0.14)",
  borderRadius: 8,
  padding: "7px 14px",
  fontSize: 13,
  fontFamily: "inherit",
  cursor: "pointer",
};

const primaryStyle: React.CSSProperties = {
  ...buttonBase,
  background: "#3b82f6",
  borderColor: "#3b82f6",
  color: "#fff",
  fontWeight: 600,
};

const secondaryStyle: React.CSSProperties = {
  ...buttonBase,
  background: "rgba(40,42,52,0.9)",
  color: "#ddd",
};

const dismissStyle: React.CSSProperties = {
  ...buttonBase,
  background: "transparent",
  color: "#aaa",
  borderColor: "transparent",
  marginRight: "auto",
};

export function SaveGateModal({
  pendingKind,
  onSignIn,
  onContinueAnyway,
  onDismiss,
}: SaveGateModalProps): JSX.Element {
  useEffect(() => {
    function onKey(e: KeyboardEvent): void {
      if (e.key === "Escape") onDismiss();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onDismiss]);

  return (
    <div
      data-testid="grace2-save-gate-modal"
      role="dialog"
      aria-modal="true"
      aria-label="Sign in to save"
      style={overlayStyle}
      onClick={onDismiss}
    >
      <div
        data-testid="grace2-save-gate-modal-card"
        style={cardStyle}
        onClick={(e) => e.stopPropagation()}
      >
        <h3 style={titleStyle}>
          {pendingKind ?? "Save your work"}
        </h3>
        <div style={bodyStyle} data-testid="grace2-save-gate-modal-body">
          Anonymous Cases don&apos;t sync to your account. Sign in to save?
        </div>
        <div style={rowStyle}>
          <button
            data-testid="grace2-save-gate-modal-dismiss"
            onClick={onDismiss}
            style={dismissStyle}
            aria-label="Cancel"
          >
            Cancel
          </button>
          <button
            data-testid="grace2-save-gate-modal-continue"
            onClick={onContinueAnyway}
            style={secondaryStyle}
            aria-label="Continue anyway without saving"
          >
            Continue anyway
          </button>
          <button
            data-testid="grace2-save-gate-modal-signin"
            onClick={onSignIn}
            style={primaryStyle}
            aria-label="Sign in"
          >
            Sign in
          </button>
        </div>
      </div>
    </div>
  );
}
