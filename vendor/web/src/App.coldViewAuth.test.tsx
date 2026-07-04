// GRACE-2 web - #147 Feature B GAP B2: forward the signed-in owner's Cognito
// bearer token to the COLD case-VIEW signer hop so the view_sign Lambda
// owner-gates it and mints the 12h OWNER-tier pre-signed URL instead of the anon
// 15min TTL.
//
// THE GAP: App's cold case-VIEW load effect (App.tsx ~line 1075) called
// `fetchCaseView(activeCaseId)` with NO 3rd (authToken) arg, so even a signed-in
// owner cold-loaded over the ANON tier. lib/case_view.ts already accepted +
// forwarded an `authToken` 3rd arg (Authorization: Bearer ...) to the signer
// hop; this was purely a caller-side wiring gap.
//
// THE FIX: the effect now fetches the Cognito ID token from the SAME source
// ws.ts uses for the `auth-token` handshake (getIdToken from ./auth) and passes
// it as the 3rd arg. A signed-in owner -> Bearer token on the signer hop; an
// anonymous user (no token) -> `undefined`, so the anon tier is byte-unchanged.
//
// This harness reproduces the cold-view effect's token-threading logic EXACTLY
// (getIdToken -> blank-collapse to undefined -> fetchCaseView 3rd arg) and
// drives the REAL fetchCaseView against an injectable fake fetch so we can read
// back the Authorization header the signer hop actually received. It does NOT
// mount maplibre/WebGL (the collapse-shell pattern from App.test.tsx).

import {
  describe, it, expect, vi, beforeEach, afterEach,
} from "vitest";
import { render, act, cleanup, waitFor } from "@testing-library/react";
import { useEffect } from "react";

// ── Mock ./auth.getIdToken so a test can simulate signed-in vs anonymous ──── //
let tokenProvider: () => Promise<string | null> = async () => null;
vi.mock("./auth", () => ({
  getIdToken: (): Promise<string | null> => tokenProvider(),
}));

import { getIdToken } from "./auth";
import { fetchCaseView, type FetchLike } from "./lib/case_view";

const SIGNER = "https://abc.execute-api.us-west-2.amazonaws.com/case-view-url";
const PRESIGNED = "https://s3.example/bucket/case-view/CASE123.json?sig=abc";

function okJson(body: unknown) {
  return { ok: true, status: 200, json: async () => body };
}

const COLD_PAYLOAD = {
  envelope_type: "case-open",
  session_state: {
    case: { case_id: "CASE123", title: "Cold Case" },
    loaded_layers: [],
    chat_history: [],
  },
};

// ── Harness: App.tsx's cold case-VIEW token-threading, verbatim ───────────── //
// Mirrors the effect body in App.tsx: getIdToken() -> blank-collapse to
// undefined -> fetchCaseView(caseId, fetchFn, authToken). The fake fetch is
// injected as the 2nd arg here so the test can read back the signer hop's
// headers; in App the 2nd arg is `undefined` (the DOM fetch). The load-bearing
// assertion is the 3rd arg (authToken) the caller supplies.
function ColdViewHarness({ fetchFn }: { fetchFn: FetchLike }): JSX.Element {
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const rawToken = await getIdToken().catch(() => null);
      if (cancelled) return;
      const authToken =
        rawToken != null && rawToken.trim() !== "" ? rawToken : undefined;
      await fetchCaseView("CASE123", fetchFn, authToken);
    })();
    return () => {
      cancelled = true;
    };
  }, [fetchFn]);
  return <div data-testid="cold-view-harness" />;
}

beforeEach(() => {
  vi.stubEnv("VITE_GRACE2_CASE_VIEW_URL", SIGNER);
  tokenProvider = async () => null;
});
afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("#147 Feature B GAP B2 - cold case-VIEW forwards the owner auth token", () => {
  it("signed-in owner: the signer hop carries the Cognito Bearer token (12h owner tier)", async () => {
    tokenProvider = async () => "owner-tok-xyz";
    const fetchFn = vi
      .fn()
      .mockResolvedValueOnce(okJson({ url: PRESIGNED, mode: "owner" })) // signer
      .mockResolvedValueOnce(okJson(COLD_PAYLOAD)); // S3

    await act(async () => {
      render(<ColdViewHarness fetchFn={fetchFn as unknown as FetchLike} />);
    });

    await waitFor(() => expect(fetchFn).toHaveBeenCalled());
    // Hop 1 is the signer; its init carries Authorization: Bearer <token>.
    const signerInit = fetchFn.mock.calls[0]![1] ?? {};
    expect(signerInit.headers?.authorization).toBe("Bearer owner-tok-xyz");
    // The pre-signed S3 GET (hop 2) carries its own query-string signature, so
    // NO Authorization header is added there (adding one invalidates SigV4).
    await waitFor(() => expect(fetchFn).toHaveBeenCalledTimes(2));
    const s3Init = fetchFn.mock.calls[1]![1] ?? {};
    expect(s3Init.headers).toBeUndefined();
  });

  it("anonymous user (no token): NO Authorization header -> anon tier unchanged", async () => {
    tokenProvider = async () => null;
    const fetchFn = vi
      .fn()
      .mockResolvedValueOnce(okJson({ url: PRESIGNED, mode: "anon" }))
      .mockResolvedValueOnce(okJson(COLD_PAYLOAD));

    await act(async () => {
      render(<ColdViewHarness fetchFn={fetchFn as unknown as FetchLike} />);
    });

    await waitFor(() => expect(fetchFn).toHaveBeenCalled());
    const signerInit = fetchFn.mock.calls[0]![1] ?? {};
    expect(signerInit.headers?.authorization).toBeUndefined();
  });

  it("blank/whitespace token collapses to undefined (anon tier, no Bearer)", async () => {
    tokenProvider = async () => "   ";
    const fetchFn = vi
      .fn()
      .mockResolvedValueOnce(okJson({ url: PRESIGNED, mode: "anon" }))
      .mockResolvedValueOnce(okJson(COLD_PAYLOAD));

    await act(async () => {
      render(<ColdViewHarness fetchFn={fetchFn as unknown as FetchLike} />);
    });

    await waitFor(() => expect(fetchFn).toHaveBeenCalled());
    const signerInit = fetchFn.mock.calls[0]![1] ?? {};
    expect(signerInit.headers?.authorization).toBeUndefined();
  });

  it("getIdToken throwing degrades gracefully to the anon tier (no Bearer)", async () => {
    tokenProvider = async () => {
      throw new Error("auth subsystem down");
    };
    const fetchFn = vi
      .fn()
      .mockResolvedValueOnce(okJson({ url: PRESIGNED, mode: "anon" }))
      .mockResolvedValueOnce(okJson(COLD_PAYLOAD));

    await act(async () => {
      render(<ColdViewHarness fetchFn={fetchFn as unknown as FetchLike} />);
    });

    await waitFor(() => expect(fetchFn).toHaveBeenCalled());
    const signerInit = fetchFn.mock.calls[0]![1] ?? {};
    expect(signerInit.headers?.authorization).toBeUndefined();
  });
});
