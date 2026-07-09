// GRACE-2 web — useSaveGate hook (job-0143, sprint-12-mega Wave 4).
//
// Intercepts save-triggering actions for anonymous users and surfaces a
// one-shot inline disclaimer at the moment of attempt rather than blanket
// "Sign in to save" copy on every render. Replaces the always-visible
// "Sign in to save" PersistenceChip removed in job-0143.
//
// The hook returns:
//   - `gateAction(actionFn)`: wraps an action so it either runs immediately
//     (signed-in) or surfaces the save-gate modal (anonymous).
//   - `pendingAction`, `isOpen`: drive the modal render.
//   - `confirmContinue()`: dismiss the modal and run the pending action.
//   - `requestSignIn()`: invokes the sign-in callback and clears the modal.
//   - `dismiss()`: cancel the gated action.
//
// Invariants honored:
//   - 8 (cancellation is first-class): every gated action can be cancelled
//     without consequence — `dismiss()` runs no callback.
//   - 9 (no cost theater): copy refers to persistence only, never cost.

import { useCallback, useState } from "react";
// TRID3NT LOCAL (F5, live-feedback 2026-07-08): the local build is single-user
// with no sign-in, and everything persists locally -- the "Sign in to save"
// gate is meaningless there, so gated actions always run immediately. Cloud
// behavior is byte-identical when the flag is unset.
import { isLocalDeployment } from "../lib/deployment";

export interface UseSaveGateOptions {
  /** Whether the active user can persist work (Firebase non-anonymous). */
  isSignedIn: boolean;
  /** Invoked when the user clicks "Sign in" inside the gate. */
  onSignInRequest: () => void;
}

export interface UseSaveGateReturn {
  /** Wrap an action so it gates on save-capability. */
  gateAction: (action: () => void, kind?: string) => () => void;
  /** True while the modal is visible. */
  isOpen: boolean;
  /** Friendly label for the action being gated (e.g. "Create a new Case"). */
  pendingKind: string | null;
  /** Cancel the gate (run nothing). */
  dismiss: () => void;
  /** Dismiss the gate AND run the pending action ("Continue anyway"). */
  confirmContinue: () => void;
  /** Dismiss the gate AND invoke `onSignInRequest`. */
  requestSignIn: () => void;
}

/**
 * Intercept save-triggering actions for anonymous users.
 *
 * Usage in App.tsx:
 *
 *   const saveGate = useSaveGate({ isSignedIn, onSignInRequest: handleSignIn });
 *   <CasesPanel onCreate={saveGate.gateAction(createCase, "Create a new Case")} />
 *   {saveGate.isOpen && <SaveGateModal {...saveGate} />}
 */
// job-0276: once the user chooses "Continue anyway" the choice sticks for
// the browser session. Pre-fix the gate re-armed on EVERY anonymous
// create/rename/archive/delete — live-reproduced trap: the delete flow
// stacked the gate ON TOP of the delete ConfirmationDialog, and an
// unnoticed gate silently ate the next click (including Case rows — the
// user's "can't get back into the Case"). The disclaimer's job is done
// after one informed acknowledgement.
const ACCEPTED_KEY = "grace2-save-gate-accepted";

function gateAccepted(): boolean {
  try {
    return sessionStorage.getItem(ACCEPTED_KEY) === "1";
  } catch {
    return false;
  }
}

function rememberAccepted(): void {
  try {
    sessionStorage.setItem(ACCEPTED_KEY, "1");
  } catch {
    // storage unavailable — fall back to per-action gating
  }
}

export function useSaveGate(opts: UseSaveGateOptions): UseSaveGateReturn {
  const { isSignedIn, onSignInRequest } = opts;
  const [pendingAction, setPendingAction] = useState<(() => void) | null>(null);
  const [pendingKind, setPendingKind] = useState<string | null>(null);

  const gateAction = useCallback(
    (action: () => void, kind: string = "Save your work") =>
      () => {
        if (isSignedIn || isLocalDeployment() || gateAccepted()) {
          action();
          return;
        }
        // Anonymous user, first gated action — defer behind the gate.
        setPendingAction(() => action);
        setPendingKind(kind);
      },
    [isSignedIn],
  );

  const dismiss = useCallback(() => {
    setPendingAction(null);
    setPendingKind(null);
  }, []);

  const confirmContinue = useCallback(() => {
    rememberAccepted(); // job-0276: never re-trap this session
    const a = pendingAction;
    setPendingAction(null);
    setPendingKind(null);
    if (a) a();
  }, [pendingAction]);

  const requestSignIn = useCallback(() => {
    setPendingAction(null);
    setPendingKind(null);
    onSignInRequest();
  }, [onSignInRequest]);

  return {
    gateAction,
    isOpen: pendingAction !== null,
    pendingKind,
    dismiss,
    confirmContinue,
    requestSignIn,
  };
}
