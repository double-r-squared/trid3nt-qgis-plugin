// GRACE-2 web — useSaveGate hook tests (job-0143, sprint-12-mega Wave 4).
//
// Verifies:
//   1. When isSignedIn=true, gateAction runs the action immediately.
//   2. When isSignedIn=false, gateAction defers the action and opens the gate.
//   3. confirmContinue() runs the deferred action and closes the gate.
//   4. requestSignIn() invokes onSignInRequest and closes the gate.
//   5. dismiss() closes the gate WITHOUT running the action.

import { describe, it, expect, vi, afterEach } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import { useSaveGate } from "./hooks/useSaveGate";

afterEach(() => {
  cleanup();
  // job-0276: the gate remembers "Continue anyway" in sessionStorage for
  // the browser session — clear it so each test starts un-accepted.
  sessionStorage.clear();
});

describe("useSaveGate", () => {
  it("runs the action immediately when signed in", () => {
    const action = vi.fn();
    const { result } = renderHook(() =>
      useSaveGate({ isSignedIn: true, onSignInRequest: vi.fn() }),
    );
    act(() => result.current.gateAction(action, "save")());
    expect(action).toHaveBeenCalledTimes(1);
    expect(result.current.isOpen).toBe(false);
  });

  it("defers the action when anonymous + opens the gate", () => {
    const action = vi.fn();
    const { result } = renderHook(() =>
      useSaveGate({ isSignedIn: false, onSignInRequest: vi.fn() }),
    );
    act(() => result.current.gateAction(action, "Create a Case")());
    expect(action).not.toHaveBeenCalled();
    expect(result.current.isOpen).toBe(true);
    expect(result.current.pendingKind).toBe("Create a Case");
  });

  it("confirmContinue runs the deferred action and closes", () => {
    const action = vi.fn();
    const { result } = renderHook(() =>
      useSaveGate({ isSignedIn: false, onSignInRequest: vi.fn() }),
    );
    act(() => result.current.gateAction(action, "save")());
    act(() => result.current.confirmContinue());
    expect(action).toHaveBeenCalledTimes(1);
    expect(result.current.isOpen).toBe(false);
  });

  it("requestSignIn closes and invokes onSignInRequest (action NOT run)", () => {
    const action = vi.fn();
    const onSignInRequest = vi.fn();
    const { result } = renderHook(() =>
      useSaveGate({ isSignedIn: false, onSignInRequest }),
    );
    act(() => result.current.gateAction(action, "save")());
    act(() => result.current.requestSignIn());
    expect(onSignInRequest).toHaveBeenCalledTimes(1);
    expect(action).not.toHaveBeenCalled();
    expect(result.current.isOpen).toBe(false);
  });

  it("dismiss closes WITHOUT running the action", () => {
    const action = vi.fn();
    const { result } = renderHook(() =>
      useSaveGate({ isSignedIn: false, onSignInRequest: vi.fn() }),
    );
    act(() => result.current.gateAction(action, "save")());
    act(() => result.current.dismiss());
    expect(action).not.toHaveBeenCalled();
    expect(result.current.isOpen).toBe(false);
  });
});

describe("useSaveGate one-time acceptance (job-0276)", () => {
  it("after Continue anyway, later gated actions run without the modal", () => {
    const action1 = vi.fn();
    const action2 = vi.fn();
    const { result } = renderHook(() =>
      useSaveGate({ isSignedIn: false, onSignInRequest: vi.fn() }),
    );
    act(() => result.current.gateAction(action1, "Create a new Case")());
    expect(result.current.isOpen).toBe(true);
    act(() => result.current.confirmContinue());
    expect(action1).toHaveBeenCalledTimes(1);

    // Second gated action: no modal, runs immediately (no re-trap).
    act(() => result.current.gateAction(action2, "Rename Case")());
    expect(result.current.isOpen).toBe(false);
    expect(action2).toHaveBeenCalledTimes(1);
  });

  it("dismiss does NOT remember acceptance — the gate re-arms", () => {
    const action = vi.fn();
    const { result } = renderHook(() =>
      useSaveGate({ isSignedIn: false, onSignInRequest: vi.fn() }),
    );
    act(() => result.current.gateAction(action, "Create a new Case")());
    act(() => result.current.dismiss());
    expect(action).not.toHaveBeenCalled();
    act(() => result.current.gateAction(action, "Create a new Case")());
    expect(result.current.isOpen).toBe(true);
  });
});
