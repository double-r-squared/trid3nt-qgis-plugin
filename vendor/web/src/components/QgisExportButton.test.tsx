// GRACE-2 web - QgisExportButton unit tests (user-driven QGIS export,
// NATE 2026-07-06).
//
// Tests (mocked global fetch - the component talks to the agent's :8766
// /api/export-qgis surface directly, local-first, no config gate):
//   1. Renders the idle menu item unconditionally (no probe fetch on mount).
//   2. Click -> POST {case_id} to <httpBase()>/api/export-qgis; success ->
//      "QGIS project ready (N layers)" label + output_dir secondary line +
//      a .qgz download via GET /api/export-qgis/file?path=...
//   3. Typed 4xx failure -> honest inline error line from {"error": ...}.
//   4. Network failure (fetch rejects) -> honest inline error line.

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, waitFor } from "@testing-library/react";
import { QgisExportButton } from "./QgisExportButton";

const SUCCESS_PAYLOAD = {
  status: "ok",
  qgz_path: "/exports/case-abc/project.qgz",
  gpkg_path: "/exports/case-abc/export.gpkg",
  exported_vector_count: 2,
  exported_raster_count: 1,
  skipped: [],
  output_dir: "/exports/case-abc",
};

function mockFetchOnce(response: {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
}): ReturnType<typeof vi.fn> {
  const fn = vi.fn().mockResolvedValue(response);
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("QgisExportButton", () => {
  it("renders the idle menu item without any probe fetch", () => {
    const fetchFn = vi.fn();
    vi.stubGlobal("fetch", fetchFn);
    render(<QgisExportButton caseId="01CASE" asMenuItem />);
    const item = screen.getByTestId("grace2-case-qgis-export-menuitem");
    expect(item.textContent).toContain("Export to QGIS");
    // Local-first: no config probe / gate call on mount.
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it("POSTs the case_id, shows the layer count + output_dir, and downloads the .qgz", async () => {
    const fetchFn = mockFetchOnce({
      ok: true,
      status: 200,
      json: async () => SUCCESS_PAYLOAD,
    });
    // Spy the transient download anchor click (jsdom would otherwise try to
    // navigate to the href).
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => {});

    render(<QgisExportButton caseId="01CASE" asMenuItem />);
    fireEvent.click(screen.getByTestId("grace2-case-qgis-export-menuitem"));

    await waitFor(() =>
      expect(
        screen.getByTestId("grace2-case-qgis-export-menuitem").textContent,
      ).toContain("QGIS project ready (3 layers)"),
    );

    // The POST hit the agent's export endpoint with the case_id body.
    expect(fetchFn).toHaveBeenCalledTimes(1);
    const [url, init] = fetchFn.mock.calls[0] as [string, { method: string; body: string }];
    expect(String(url)).toMatch(/\/api\/export-qgis$/);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ case_id: "01CASE" });

    // Secondary line: the export folder for users who want more than the .qgz.
    expect(
      screen.getByTestId("grace2-case-qgis-export-status").textContent,
    ).toBe("/exports/case-abc");

    // The .qgz download was triggered through the path-guarded file route.
    expect(clickSpy).toHaveBeenCalledTimes(1);
  });

  it("shows the endpoint's honest error text on a typed 4xx", async () => {
    mockFetchOnce({
      ok: false,
      status: 404,
      json: async () => ({ error: "case '01GONE' not found." }),
    });

    render(<QgisExportButton caseId="01GONE" asMenuItem />);
    fireEvent.click(screen.getByTestId("grace2-case-qgis-export-menuitem"));

    await waitFor(() =>
      expect(
        screen.getByTestId("grace2-case-qgis-export-menuitem").textContent,
      ).toContain("QGIS export failed, try again"),
    );
    expect(
      screen.getByTestId("grace2-case-qgis-export-status").textContent,
    ).toBe("case '01GONE' not found.");
  });

  it("shows an inline honest error when the agent is unreachable", async () => {
    const fetchFn = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    vi.stubGlobal("fetch", fetchFn);

    render(<QgisExportButton caseId="01CASE" asMenuItem />);
    fireEvent.click(screen.getByTestId("grace2-case-qgis-export-menuitem"));

    await waitFor(() =>
      expect(
        screen.getByTestId("grace2-case-qgis-export-menuitem").textContent,
      ).toContain("QGIS export failed, try again"),
    );
    expect(
      screen.getByTestId("grace2-case-qgis-export-status").textContent,
    ).toContain("Agent unreachable");
  });
});
