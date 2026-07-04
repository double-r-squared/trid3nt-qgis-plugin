// GRACE-2 web - lib/export.ts tests (data export, NATE 2026-06-19).
//
// Verifies the case data-EXPORT client (the SINGLE-GET sibling of
// case_list.test.ts):
//   - caseExportUrl() precedence: VITE_GRACE2_CASE_EXPORT_URL > PUBLIC_BASE(/case-export-url) > null.
//   - caseExportConfigured() reflects caseExportUrl() presence.
//   - requestCaseExport():
//       * disabled (no endpoint)      -> null, no fetch.
//       * empty caseId                -> null, no fetch.
//       * happy path (single GET)     -> returns the parsed { url, size_bytes, layer_count }.
//       * non-2xx                     -> null.
//       * 403                         -> null.
//       * malformed payload           -> null (validation).
//       * throw on the GET            -> null (never throws).
//       * forwards an auth bearer header to the GET.
//
// Env is read INSIDE the helpers; we resetModules + dynamic-import per case.

import { describe, it, expect, afterEach, vi } from "vitest";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
  vi.restoreAllMocks();
});

const ENDPOINT = "https://abc.execute-api.us-west-2.amazonaws.com/case-export-url";
const CASE_ID = "CASE123";

function okJson(body: unknown) {
  return { ok: true, status: 200, json: async () => body };
}

describe("caseExportUrl / caseExportConfigured", () => {
  it("returns null (disabled) when nothing is configured", async () => {
    const { caseExportUrl, caseExportConfigured } = await import("./export");
    expect(caseExportUrl()).toBeNull();
    expect(caseExportConfigured()).toBe(false);
  });

  it("uses VITE_GRACE2_CASE_EXPORT_URL verbatim (trailing slashes trimmed)", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_EXPORT_URL", `${ENDPOINT}/`);
    const { caseExportUrl, caseExportConfigured } = await import("./export");
    expect(caseExportUrl()).toBe(ENDPOINT);
    expect(caseExportConfigured()).toBe(true);
  });

  it("derives <public-base>/case-export-url when only PUBLIC_BASE is set", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_PUBLIC_BASE", "https://d123.cloudfront.net");
    const { caseExportUrl } = await import("./export");
    expect(caseExportUrl()).toBe("https://d123.cloudfront.net/case-export-url");
  });

  it("VITE_GRACE2_CASE_EXPORT_URL beats VITE_GRACE2_PUBLIC_BASE", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_PUBLIC_BASE", "https://d123.cloudfront.net");
    vi.stubEnv("VITE_GRACE2_CASE_EXPORT_URL", ENDPOINT);
    const { caseExportUrl } = await import("./export");
    expect(caseExportUrl()).toBe(ENDPOINT);
  });
});

describe("requestCaseExport", () => {
  it("returns null and issues no fetch when export is unconfigured", async () => {
    const { requestCaseExport } = await import("./export");
    const fetchFn = vi.fn();
    expect(await requestCaseExport(CASE_ID, fetchFn)).toBeNull();
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it("returns null and issues no fetch for an empty caseId", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_EXPORT_URL", ENDPOINT);
    const { requestCaseExport } = await import("./export");
    const fetchFn = vi.fn();
    expect(await requestCaseExport("", fetchFn)).toBeNull();
    expect(await requestCaseExport("   ", fetchFn)).toBeNull();
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it("happy path: single GET returns the parsed export result", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_EXPORT_URL", ENDPOINT);
    const { requestCaseExport } = await import("./export");
    const body = {
      url: "https://s3.amazonaws.com/bucket/CASE123.zip?sig=abc",
      size_bytes: 12_400_000,
      layer_count: 4,
    };
    const fetchFn = vi.fn().mockResolvedValueOnce(okJson(body));
    const got = await requestCaseExport(CASE_ID, fetchFn);
    expect(got).toEqual(body);
    expect(fetchFn).toHaveBeenCalledTimes(1);
    // The export endpoint with the case_id query appended.
    expect(fetchFn.mock.calls[0]![0]).toBe(`${ENDPOINT}?case_id=${CASE_ID}`);
  });

  it("returns null when the GET is non-2xx", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_EXPORT_URL", ENDPOINT);
    const { requestCaseExport } = await import("./export");
    const fetchFn = vi.fn(async () => ({
      ok: false,
      status: 500,
      json: async () => ({ error: "boom" }),
    }));
    expect(await requestCaseExport(CASE_ID, fetchFn)).toBeNull();
    expect(fetchFn).toHaveBeenCalledTimes(1);
  });

  it("returns null on a 403 (forbidden)", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_EXPORT_URL", ENDPOINT);
    const { requestCaseExport } = await import("./export");
    const fetchFn = vi.fn(async () => ({
      ok: false,
      status: 403,
      json: async () => ({ error: "forbidden" }),
    }));
    expect(await requestCaseExport(CASE_ID, fetchFn)).toBeNull();
  });

  it("returns null for a malformed payload (no url)", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_EXPORT_URL", ENDPOINT);
    const { requestCaseExport } = await import("./export");
    const fetchFn = vi
      .fn()
      .mockResolvedValueOnce(okJson({ size_bytes: 100, layer_count: 1 }));
    expect(await requestCaseExport(CASE_ID, fetchFn)).toBeNull();
  });

  it("returns null for a malformed payload (size_bytes not a number)", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_EXPORT_URL", ENDPOINT);
    const { requestCaseExport } = await import("./export");
    const fetchFn = vi
      .fn()
      .mockResolvedValueOnce(okJson({ url: "https://x", size_bytes: "big", layer_count: 1 }));
    expect(await requestCaseExport(CASE_ID, fetchFn)).toBeNull();
  });

  it("returns null (never throws) when the GET rejects", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_EXPORT_URL", ENDPOINT);
    const { requestCaseExport } = await import("./export");
    const fetchFn = vi.fn(async () => {
      throw new Error("network down");
    });
    expect(await requestCaseExport(CASE_ID, fetchFn)).toBeNull();
  });

  it("forwards an auth bearer header to the GET", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_CASE_EXPORT_URL", ENDPOINT);
    const { requestCaseExport } = await import("./export");
    const body = { url: "https://x", size_bytes: 1, layer_count: 1 };
    const fetchFn = vi.fn().mockResolvedValueOnce(okJson(body));
    await requestCaseExport(CASE_ID, fetchFn, "tok-abc");
    const init = fetchFn.mock.calls[0]![1] ?? {};
    expect(init.headers?.authorization).toBe("Bearer tok-abc");
  });
});
