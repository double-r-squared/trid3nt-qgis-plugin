// GRACE-2 web — lib/case_view.ts tests (sleep/wake STAGE 2, NATE 2026-06-18).
//
// Verifies the COLD-LOAD client:
//   - caseViewUrl() precedence: VITE_GRACE2_CASE_VIEW_URL > PUBLIC_BASE(/case-view-url) > null.
//   - caseViewConfigured() reflects caseViewUrl() presence.
//   - fetchCaseView():
//       * disabled (no endpoint)      -> null, no fetch.
//       * happy path (two-hop)        -> returns the parsed CaseOpenEnvelopePayload.
//       * signer 404 (no snapshot)    -> null (treated as "no cold snapshot").
//       * signer non-2xx / no url     -> null.
//       * S3 hop non-2xx              -> null.
//       * malformed payload           -> null (validation).
//       * null session_state          -> returned (empty-case snapshot is valid).
//       * throw on either hop         -> null (never throws).
//       * forwards ?case_id + auth header to the SIGNER, none to the S3 GET.
//
// Env is read INSIDE the helpers; we resetModules + dynamic-import per case.

import { describe, it, expect, afterEach, vi } from "vitest";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
  vi.restoreAllMocks();
});

const SIGNER = "https://abc.execute-api.us-west-2.amazonaws.com/case-view-url";
const PRESIGNED = "https://s3.example/bucket/case-view/CASE123.json?sig=abc";

function okJson(body: unknown) {
  return { ok: true, status: 200, json: async () => body };
}

describe("caseViewUrl / caseViewConfigured", () => {
  it("returns null (disabled) when nothing is configured", async () => {
    const { caseViewUrl, caseViewConfigured } = await import("./case_view");
    expect(caseViewUrl()).toBeNull();
    expect(caseViewConfigured()).toBe(false);
  });

  it("uses VITE_GRACE2_CASE_VIEW_URL verbatim (trailing slashes trimmed)", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_VIEW_URL", `${SIGNER}/`);
    const { caseViewUrl, caseViewConfigured } = await import("./case_view");
    expect(caseViewUrl()).toBe(SIGNER);
    expect(caseViewConfigured()).toBe(true);
  });

  it("derives <public-base>/case-view-url when only PUBLIC_BASE is set", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_PUBLIC_BASE", "https://d123.cloudfront.net");
    const { caseViewUrl } = await import("./case_view");
    expect(caseViewUrl()).toBe("https://d123.cloudfront.net/case-view-url");
  });

  it("VITE_GRACE2_CASE_VIEW_URL beats VITE_GRACE2_PUBLIC_BASE", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_PUBLIC_BASE", "https://d123.cloudfront.net");
    vi.stubEnv("VITE_GRACE2_CASE_VIEW_URL", SIGNER);
    const { caseViewUrl } = await import("./case_view");
    expect(caseViewUrl()).toBe(SIGNER);
  });
});

describe("fetchCaseView", () => {
  it("returns null and issues no fetch when cold-load is unconfigured", async () => {
    const { fetchCaseView } = await import("./case_view");
    const fetchFn = vi.fn();
    expect(await fetchCaseView("CASE123", fetchFn)).toBeNull();
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it("happy path: two-hop fetch returns the parsed case-open payload", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_VIEW_URL", SIGNER);
    const { fetchCaseView } = await import("./case_view");
    const payload = {
      envelope_type: "case-open",
      session_state: {
        case: { case_id: "CASE123", title: "Cold Case" },
        loaded_layers: [{ source_id: "flood", layer_type: "raster" }],
        chat_history: [],
      },
    };
    const fetchFn = vi
      .fn()
      .mockResolvedValueOnce(okJson({ url: PRESIGNED, mode: "anon" })) // signer
      .mockResolvedValueOnce(okJson(payload)); // S3
    const got = await fetchCaseView("CASE123", fetchFn);
    expect(got).toEqual(payload);
    expect(fetchFn).toHaveBeenCalledTimes(2);
    // Hop 1: signer with the case_id query.
    expect(fetchFn.mock.calls[0]![0]).toBe(`${SIGNER}?case_id=CASE123`);
    // Hop 2: the pre-signed S3 url verbatim (no extra query mangling).
    expect(fetchFn.mock.calls[1]![0]).toBe(PRESIGNED);
  });

  it("treats a signer 404 as 'no cold snapshot' (returns null, no S3 hop)", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_VIEW_URL", SIGNER);
    const { fetchCaseView } = await import("./case_view");
    const fetchFn = vi.fn(async () => ({
      ok: false,
      status: 404,
      json: async () => ({ error: "no snapshot" }),
    }));
    expect(await fetchCaseView("CASE123", fetchFn)).toBeNull();
    // Only the signer hop was attempted.
    expect(fetchFn).toHaveBeenCalledTimes(1);
  });

  it("returns null when the signer body has no url", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_VIEW_URL", SIGNER);
    const { fetchCaseView } = await import("./case_view");
    const fetchFn = vi.fn(async () => okJson({ mode: "anon" }));
    expect(await fetchCaseView("CASE123", fetchFn)).toBeNull();
    expect(fetchFn).toHaveBeenCalledTimes(1);
  });

  it("returns null when the S3 hop is non-2xx", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_VIEW_URL", SIGNER);
    const { fetchCaseView } = await import("./case_view");
    const fetchFn = vi
      .fn()
      .mockResolvedValueOnce(okJson({ url: PRESIGNED }))
      .mockResolvedValueOnce({ ok: false, status: 403, json: async () => ({}) });
    expect(await fetchCaseView("CASE123", fetchFn)).toBeNull();
    expect(fetchFn).toHaveBeenCalledTimes(2);
  });

  it("returns null for a malformed payload (no session_state key)", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_VIEW_URL", SIGNER);
    const { fetchCaseView } = await import("./case_view");
    const fetchFn = vi
      .fn()
      .mockResolvedValueOnce(okJson({ url: PRESIGNED }))
      .mockResolvedValueOnce(okJson({ junk: true }));
    expect(await fetchCaseView("CASE123", fetchFn)).toBeNull();
  });

  it("returns null when session_state lacks case.case_id", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_VIEW_URL", SIGNER);
    const { fetchCaseView } = await import("./case_view");
    const fetchFn = vi
      .fn()
      .mockResolvedValueOnce(okJson({ url: PRESIGNED }))
      .mockResolvedValueOnce(okJson({ session_state: { loaded_layers: [] } }));
    expect(await fetchCaseView("CASE123", fetchFn)).toBeNull();
  });

  it("accepts a null session_state (empty/never-opened case snapshot)", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_VIEW_URL", SIGNER);
    const { fetchCaseView } = await import("./case_view");
    const payload = { envelope_type: "case-open", session_state: null };
    const fetchFn = vi
      .fn()
      .mockResolvedValueOnce(okJson({ url: PRESIGNED }))
      .mockResolvedValueOnce(okJson(payload));
    expect(await fetchCaseView("CASE123", fetchFn)).toEqual(payload);
  });

  it("returns null (never throws) when a hop rejects", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_VIEW_URL", SIGNER);
    const { fetchCaseView } = await import("./case_view");
    const fetchFn = vi.fn(async () => {
      throw new Error("network down");
    });
    expect(await fetchCaseView("CASE123", fetchFn)).toBeNull();
  });

  it("returns null for an empty caseId", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_VIEW_URL", SIGNER);
    const { fetchCaseView } = await import("./case_view");
    const fetchFn = vi.fn();
    expect(await fetchCaseView("", fetchFn)).toBeNull();
    expect(await fetchCaseView("   ", fetchFn)).toBeNull();
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it("forwards an auth bearer to the SIGNER hop but NOT the S3 hop", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_VIEW_URL", SIGNER);
    const { fetchCaseView } = await import("./case_view");
    const payload = {
      session_state: { case: { case_id: "CASE123" }, loaded_layers: [] },
    };
    const fetchFn = vi
      .fn()
      .mockResolvedValueOnce(okJson({ url: PRESIGNED }))
      .mockResolvedValueOnce(okJson(payload));
    await fetchCaseView("CASE123", fetchFn, "tok-abc");
    const signerInit = fetchFn.mock.calls[0]![1] ?? {};
    const s3Init = fetchFn.mock.calls[1]![1] ?? {};
    expect(signerInit.headers?.authorization).toBe("Bearer tok-abc");
    // The pre-signed S3 GET carries its own query-string signature; adding an
    // Authorization header can invalidate the SigV4 pre-sign, so we send none.
    expect(s3Init.headers).toBeUndefined();
  });
});
