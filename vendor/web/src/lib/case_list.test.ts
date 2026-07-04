// GRACE-2 web - lib/case_list.ts tests (sleep/wake STAGE 2, NATE 2026-06-19).
//
// Verifies the cases-list COLD-LOAD client (the SINGLE-GET sibling of
// case_view.test.ts):
//   - caseListUrl() precedence: VITE_GRACE2_CASE_LIST_URL > PUBLIC_BASE(/case-list) > null.
//   - caseListConfigured() reflects caseListUrl() presence.
//   - fetchCaseList():
//       * disabled (no endpoint)      -> null, no fetch.
//       * happy path (single GET)     -> returns the parsed CaseListEnvelopePayload.
//       * non-2xx                     -> null.
//       * malformed payload           -> null (validation).
//       * throw on the GET            -> null (never throws).
//       * empty cases array           -> returned (a user with no cases is valid).
//       * forwards an auth bearer header to the GET.
//
// Env is read INSIDE the helpers; we resetModules + dynamic-import per case.

import { describe, it, expect, afterEach, vi } from "vitest";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
  vi.restoreAllMocks();
});

const ENDPOINT = "https://abc.execute-api.us-west-2.amazonaws.com/case-list";

function okJson(body: unknown) {
  return { ok: true, status: 200, json: async () => body };
}

describe("caseListUrl / caseListConfigured", () => {
  it("returns null (disabled) when nothing is configured", async () => {
    const { caseListUrl, caseListConfigured } = await import("./case_list");
    expect(caseListUrl()).toBeNull();
    expect(caseListConfigured()).toBe(false);
  });

  it("uses VITE_GRACE2_CASE_LIST_URL verbatim (trailing slashes trimmed)", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_LIST_URL", `${ENDPOINT}/`);
    const { caseListUrl, caseListConfigured } = await import("./case_list");
    expect(caseListUrl()).toBe(ENDPOINT);
    expect(caseListConfigured()).toBe(true);
  });

  it("derives <public-base>/case-list when only PUBLIC_BASE is set", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_PUBLIC_BASE", "https://d123.cloudfront.net");
    const { caseListUrl } = await import("./case_list");
    expect(caseListUrl()).toBe("https://d123.cloudfront.net/case-list");
  });

  it("VITE_GRACE2_CASE_LIST_URL beats VITE_GRACE2_PUBLIC_BASE", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_PUBLIC_BASE", "https://d123.cloudfront.net");
    vi.stubEnv("VITE_GRACE2_CASE_LIST_URL", ENDPOINT);
    const { caseListUrl } = await import("./case_list");
    expect(caseListUrl()).toBe(ENDPOINT);
  });
});

describe("fetchCaseList", () => {
  it("returns null and issues no fetch when cold-load is unconfigured", async () => {
    const { fetchCaseList } = await import("./case_list");
    const fetchFn = vi.fn();
    expect(await fetchCaseList(fetchFn)).toBeNull();
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it("happy path: single GET returns the parsed case-list payload", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_LIST_URL", ENDPOINT);
    const { fetchCaseList } = await import("./case_list");
    const payload = {
      envelope_type: "case-list",
      cases: [
        { case_id: "CASE123", title: "Cold Case" },
        { case_id: "CASE456", title: "Second Case" },
      ],
    };
    const fetchFn = vi.fn().mockResolvedValueOnce(okJson(payload));
    const got = await fetchCaseList(fetchFn);
    expect(got).toEqual(payload);
    expect(fetchFn).toHaveBeenCalledTimes(1);
    // The case-list endpoint verbatim (no query mangling).
    expect(fetchFn.mock.calls[0]![0]).toBe(ENDPOINT);
  });

  it("returns null when the GET is non-2xx", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_LIST_URL", ENDPOINT);
    const { fetchCaseList } = await import("./case_list");
    const fetchFn = vi.fn(async () => ({
      ok: false,
      status: 500,
      json: async () => ({ error: "boom" }),
    }));
    expect(await fetchCaseList(fetchFn)).toBeNull();
    expect(fetchFn).toHaveBeenCalledTimes(1);
  });

  it("returns null for a malformed payload (cases not an array)", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_LIST_URL", ENDPOINT);
    const { fetchCaseList } = await import("./case_list");
    const fetchFn = vi.fn().mockResolvedValueOnce(okJson({ cases: "nope" }));
    expect(await fetchCaseList(fetchFn)).toBeNull();
  });

  it("returns null for a malformed payload (no cases key)", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_LIST_URL", ENDPOINT);
    const { fetchCaseList } = await import("./case_list");
    const fetchFn = vi.fn().mockResolvedValueOnce(okJson({ junk: true }));
    expect(await fetchCaseList(fetchFn)).toBeNull();
  });

  it("accepts an empty cases array (a user with no cases)", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_LIST_URL", ENDPOINT);
    const { fetchCaseList } = await import("./case_list");
    const payload = { envelope_type: "case-list", cases: [] };
    const fetchFn = vi.fn().mockResolvedValueOnce(okJson(payload));
    expect(await fetchCaseList(fetchFn)).toEqual(payload);
  });

  it("returns null (never throws) when the GET rejects", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_LIST_URL", ENDPOINT);
    const { fetchCaseList } = await import("./case_list");
    const fetchFn = vi.fn(async () => {
      throw new Error("network down");
    });
    expect(await fetchCaseList(fetchFn)).toBeNull();
  });

  it("forwards an auth bearer header to the GET", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_LIST_URL", ENDPOINT);
    const { fetchCaseList } = await import("./case_list");
    const payload = { cases: [{ case_id: "CASE123" }] };
    const fetchFn = vi.fn().mockResolvedValueOnce(okJson(payload));
    await fetchCaseList(fetchFn, "tok-abc");
    const init = fetchFn.mock.calls[0]![1] ?? {};
    expect(init.headers?.authorization).toBe("Bearer tok-abc");
  });
});
