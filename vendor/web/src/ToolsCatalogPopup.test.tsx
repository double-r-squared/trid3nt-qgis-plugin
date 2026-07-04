// GRACE-2 web — ToolsCatalogPopup tests (Wave 4.10 Stage 3 — job C1).
//
// Verifies:
//   1. Renders the catalog title, search box, category grid, and tool list
//      when an initialCatalog is provided.
//   2. Category click filters the tool list; click-again clears.
//   3. Search input filters by name + description (debounced).
//   4. Badges are derived correctly from MCP annotations (default-quiet,
//      non-default surfaced with the expected label).
//   5. Sample queries render as italic copy-on-click chips.
//   6. Click-to-copy writes to navigator.clipboard.
//   7. Empty state when no tools match.
//   8. Loading state on initial mount (no initialCatalog).
//   9. Error state when fetch fails.
//   10. Close (X) + Esc + backdrop dismiss invoke onClose.
//   11. supports_global_query badge differs by tool.

import {
  describe,
  it,
  expect,
  vi,
  afterEach,
} from "vitest";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  waitFor,
  act,
} from "@testing-library/react";
import {
  ToolsCatalogPopup,
  type ToolCatalogPayload,
  type ToolCatalogTool,
  deriveBadges,
} from "./components/ToolsCatalogPopup";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function makeTool(overrides: Partial<ToolCatalogTool> = {}): ToolCatalogTool {
  return {
    name: "fetch_dem",
    description: "Fetch a digital elevation model raster.",
    description_full:
      "Fetch a digital elevation model raster.\n\nLonger detail goes here. " +
      "This text is longer than 200 chars to make sure the Show more " +
      "button surfaces. We just keep typing and typing to push past the " +
      "limit comfortably so the test branch fires deterministically. " +
      "Yes that is a lot of words.",
    category_id: "terrain_elevation",
    secondary_category_ids: [],
    supports_global_query: false,
    annotations: {
      read_only_hint: true,
      open_world_hint: true,
      destructive_hint: false,
      idempotent_hint: true,
    },
    estimate_payload_mb_default: null,
    ttl_class: "static-30d",
    source_class: "dem",
    cacheable: true,
    sample_queries: [
      "show me elevation around Houston",
      "give me a DEM for Yellowstone",
    ],
    ...overrides,
  };
}

const FAKE_CATALOG: ToolCatalogPayload = {
  categories: [
    {
      id: "terrain_elevation",
      name: "Terrain and elevation",
      description: "Digital elevation models and derivatives.",
      tool_count: 1,
    },
    {
      id: "weather_atmosphere",
      name: "Weather and atmosphere",
      description: "Active weather alerts, radar, forecasts.",
      tool_count: 1,
    },
    {
      id: "hazard_modeling",
      name: "Hazard modeling",
      description: "End-to-end hazard simulation workflows.",
      tool_count: 1,
    },
  ],
  tools: [
    makeTool(),
    makeTool({
      name: "fetch_nws_alerts_conus",
      description: "Active NWS CONUS weather alerts.",
      description_full: "Active NWS CONUS weather alerts.",
      category_id: "weather_atmosphere",
      supports_global_query: true,
      annotations: {
        read_only_hint: true,
        open_world_hint: true,
        destructive_hint: false,
        idempotent_hint: true,
      },
      sample_queries: ["what alerts are active now"],
    }),
    makeTool({
      name: "publish_layer",
      description: "Publish a layer to the case QGIS project.",
      description_full: "Publish a layer to the case QGIS project.",
      category_id: "hazard_modeling",
      annotations: {
        read_only_hint: false,
        open_world_hint: false,
        destructive_hint: true,
        idempotent_hint: false,
      },
      sample_queries: [],
    }),
  ],
};

describe("ToolsCatalogPopup", () => {
  it("renders header, search box, categories, and tool list", () => {
    render(
      <ToolsCatalogPopup
        onClose={() => undefined}
        initialCatalog={FAKE_CATALOG}
      />,
    );
    expect(screen.getByTestId("grace2-tools-catalog-popup")).toBeTruthy();
    expect(screen.getByTestId("grace2-tools-catalog-search")).toBeTruthy();
    expect(screen.getByTestId("grace2-tools-catalog-categories")).toBeTruthy();
    expect(screen.getByTestId("grace2-tools-catalog-list")).toBeTruthy();
    // Three tools listed.
    expect(screen.getAllByTestId("grace2-tools-catalog-row").length).toBe(3);
    // Categories rendered.
    expect(
      screen.getByTestId("grace2-tools-catalog-category-terrain_elevation"),
    ).toBeTruthy();
  });

  it("category click filters tools; click-again clears", () => {
    render(
      <ToolsCatalogPopup
        onClose={() => undefined}
        initialCatalog={FAKE_CATALOG}
      />,
    );
    expect(screen.getAllByTestId("grace2-tools-catalog-row").length).toBe(3);
    fireEvent.click(
      screen.getByTestId("grace2-tools-catalog-category-weather_atmosphere"),
    );
    const rowsFiltered = screen.getAllByTestId("grace2-tools-catalog-row");
    expect(rowsFiltered.length).toBe(1);
    expect(rowsFiltered[0]!.getAttribute("data-tool-name")).toBe(
      "fetch_nws_alerts_conus",
    );
    // Clear via click-again on same chip.
    fireEvent.click(
      screen.getByTestId("grace2-tools-catalog-category-weather_atmosphere"),
    );
    expect(screen.getAllByTestId("grace2-tools-catalog-row").length).toBe(3);
  });

  it("search filters by name and description (debounced)", async () => {
    vi.useFakeTimers();
    render(
      <ToolsCatalogPopup
        onClose={() => undefined}
        initialCatalog={FAKE_CATALOG}
      />,
    );
    const input = screen.getByTestId("grace2-tools-catalog-search") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "publish" } });
    act(() => {
      vi.advanceTimersByTime(300);
    });
    const rows = screen.getAllByTestId("grace2-tools-catalog-row");
    expect(rows.length).toBe(1);
    expect(rows[0]!.getAttribute("data-tool-name")).toBe("publish_layer");
    vi.useRealTimers();
  });

  it("shows the empty-state when no tools match", async () => {
    vi.useFakeTimers();
    render(
      <ToolsCatalogPopup
        onClose={() => undefined}
        initialCatalog={FAKE_CATALOG}
      />,
    );
    const input = screen.getByTestId("grace2-tools-catalog-search") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "no-such-tool-xyz" } });
    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(screen.getByTestId("grace2-tools-catalog-empty")).toBeTruthy();
    vi.useRealTimers();
  });

  it("derives badges: read-only quiet, writes/open-world/destructive surfaced", () => {
    // fetch_dem: read_only=true (default — quiet), open_world=true (surfaced),
    // destructive=false (quiet), idempotent=true (quiet).
    const fetchDemBadges = deriveBadges(makeTool());
    const labels = fetchDemBadges.map((b) => b.label);
    expect(labels).toContain("open-world");
    expect(labels).toContain("terrain_elevation");
    expect(labels).not.toContain("writes");
    expect(labels).not.toContain("destructive");
    expect(labels).not.toContain("non-idempotent");

    // publish_layer: read_only=false → "writes"; destructive=true → "destructive";
    // idempotent=false → "non-idempotent".
    const publishLayer = makeTool({
      name: "publish_layer",
      annotations: {
        read_only_hint: false,
        open_world_hint: false,
        destructive_hint: true,
        idempotent_hint: false,
      },
    });
    const publishBadges = deriveBadges(publishLayer).map((b) => b.label);
    expect(publishBadges).toContain("writes");
    expect(publishBadges).toContain("destructive");
    expect(publishBadges).toContain("non-idempotent");
    expect(publishBadges).not.toContain("open-world");
  });

  it("surfaces the supports_global_query indicator", () => {
    render(
      <ToolsCatalogPopup
        onClose={() => undefined}
        initialCatalog={FAKE_CATALOG}
      />,
    );
    // fetch_dem: supports_global_query=false → "scoped"
    const demGlobal = screen.getByTestId("grace2-tools-catalog-globalq-fetch_dem");
    expect(demGlobal.textContent).toMatch(/scoped/);
    // fetch_nws_alerts_conus: supports_global_query=true → "global ok"
    const nwsGlobal = screen.getByTestId(
      "grace2-tools-catalog-globalq-fetch_nws_alerts_conus",
    );
    expect(nwsGlobal.textContent).toMatch(/global ok/);
  });

  it("click-to-copy writes a sample query to navigator.clipboard", async () => {
    const writeTextSpy = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: writeTextSpy },
    });
    render(
      <ToolsCatalogPopup
        onClose={() => undefined}
        initialCatalog={FAKE_CATALOG}
      />,
    );
    const samplesRoot = screen.getByTestId(
      "grace2-tools-catalog-samples-fetch_dem",
    );
    const firstSampleSpan = samplesRoot.querySelectorAll("span")[0] as HTMLElement;
    expect(firstSampleSpan).toBeTruthy();
    fireEvent.click(firstSampleSpan);
    await waitFor(() => {
      expect(writeTextSpy).toHaveBeenCalledWith(
        "show me elevation around Houston",
      );
    });
  });

  it("loading state appears when no initialCatalog is supplied", () => {
    // Stub global fetch so the in-flight request hangs forever.
    const fetchMock = vi.fn(() => new Promise(() => undefined));
    vi.stubGlobal("fetch", fetchMock);
    render(<ToolsCatalogPopup onClose={() => undefined} />);
    expect(screen.getByTestId("grace2-tools-catalog-loading")).toBeTruthy();
  });

  it("error state when fetch throws", async () => {
    const fetchMock = vi.fn(() => Promise.reject(new Error("network down")));
    vi.stubGlobal("fetch", fetchMock);
    render(<ToolsCatalogPopup onClose={() => undefined} />);
    await waitFor(() => {
      expect(screen.getByTestId("grace2-tools-catalog-error")).toBeTruthy();
    });
    expect(screen.getByTestId("grace2-tools-catalog-error").textContent).toMatch(
      /network down/,
    );
  });

  it("error state when fetch returns 500", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify({ error: "boom" }), {
          status: 500,
          statusText: "Internal Server Error",
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ToolsCatalogPopup onClose={() => undefined} />);
    await waitFor(() => {
      expect(screen.getByTestId("grace2-tools-catalog-error")).toBeTruthy();
    });
  });

  it("times out a never-resolving fetch and shows an asleep-aware error", async () => {
    // NATE 2026-06-26: /api/tool-catalog is BOX-LOCAL, so when the EC2 agent is
    // asleep a bare fetch hangs forever. With the 10s AbortController bound, the
    // popup must leave loading and surface an honest timeout/asleep error.
    vi.useFakeTimers();
    // Never-resolving fetch that rejects with an AbortError when aborted.
    const fetchMock = vi.fn(
      (_url: string, init?: { signal?: AbortSignal }) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            reject(new DOMException("Aborted", "AbortError"));
          });
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ToolsCatalogPopup onClose={() => undefined} />);
    // Initially loading.
    expect(screen.getByTestId("grace2-tools-catalog-loading")).toBeTruthy();
    // COLD-FIRST (NATE 2026-06-27): the default path tries the cold static
    // snapshot THEN the live agent, each bounded by its own 10s timeout. Both
    // hang here, so advance through BOTH (cold timeout, then live timeout) to
    // exhaust every source and reach the honest asleep-aware error.
    await act(async () => {
      vi.advanceTimersByTime(10_000); // cold attempt aborts
      await Promise.resolve();
    });
    await act(async () => {
      vi.advanceTimersByTime(10_000); // live attempt aborts -> error
      await Promise.resolve();
    });
    const errorEl = screen.getByTestId("grace2-tools-catalog-error");
    expect(errorEl).toBeTruthy();
    expect(errorEl.textContent).toMatch(/timed out after 10s/);
    expect(errorEl.textContent).toMatch(/agent may be asleep/);
    expect(screen.queryByTestId("grace2-tools-catalog-loading")).toBeNull();
    vi.useRealTimers();
  });

  // COLD-FIRST read-only route (NATE 2026-06-27: "I shouldn't have to start an
  // agent to see tools"). The popup loads the durable STATIC catalog snapshot
  // from the public web bucket first, falling back to the live box-local
  // endpoint only if the snapshot is unreachable.
  it("loads the COLD static snapshot first (no agent needed)", async () => {
    const calls: string[] = [];
    const fetchMock = vi.fn((url: string) => {
      calls.push(url);
      return Promise.resolve(
        new Response(JSON.stringify(FAKE_CATALOG), { status: 200 }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ToolsCatalogPopup onClose={() => undefined} />);
    await waitFor(() => {
      expect(screen.getByText("fetch_dem")).toBeTruthy();
    });
    // The FIRST (and only, on success) fetch hits the cold web-bucket snapshot,
    // NOT the live :8766 agent endpoint.
    expect(calls[0]).toMatch(/grace2-hazard-web.*\/catalog\/tool-catalog\.json/);
    expect(calls).toHaveLength(1);
    expect(calls[0]).not.toMatch(/\/api\/tool-catalog/);
  });

  it("falls back to the LIVE agent endpoint when the cold snapshot 404s", async () => {
    const calls: string[] = [];
    const fetchMock = vi.fn((url: string) => {
      calls.push(url);
      if (url.includes("grace2-hazard-web")) {
        // Cold snapshot missing (e.g. first-ever deploy) -> fast 404.
        return Promise.resolve(new Response("nope", { status: 404 }));
      }
      return Promise.resolve(
        new Response(JSON.stringify(FAKE_CATALOG), { status: 200 }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ToolsCatalogPopup onClose={() => undefined} />);
    await waitFor(() => {
      expect(screen.getByText("fetch_dem")).toBeTruthy();
    });
    // Tried cold first, then the live agent endpoint.
    expect(calls).toHaveLength(2);
    expect(calls[0]).toMatch(/grace2-hazard-web/);
    expect(calls[1]).toMatch(/\/api\/tool-catalog/);
  });

  it("errors only when BOTH the cold snapshot and the live agent fail", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(new Response("down", { status: 503 })),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ToolsCatalogPopup onClose={() => undefined} />);
    await waitFor(() => {
      expect(screen.getByTestId("grace2-tools-catalog-error")).toBeTruthy();
    });
    // Both sources attempted before surfacing the error.
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("close button + backdrop + Esc invoke onClose", () => {
    const onClose = vi.fn();
    render(
      <ToolsCatalogPopup onClose={onClose} initialCatalog={FAKE_CATALOG} />,
    );
    // X button
    fireEvent.click(screen.getByTestId("grace2-tools-catalog-popup-close"));
    expect(onClose).toHaveBeenCalledTimes(1);
    // Backdrop
    fireEvent.click(screen.getByTestId("grace2-tools-catalog-popup"));
    expect(onClose).toHaveBeenCalledTimes(2);
    // Card click does NOT bubble
    fireEvent.click(screen.getByTestId("grace2-tools-catalog-popup-card"));
    expect(onClose).toHaveBeenCalledTimes(2);
    // Esc
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(3);
  });

  it("Show more / Show less toggles the full description", () => {
    render(
      <ToolsCatalogPopup
        onClose={() => undefined}
        initialCatalog={FAKE_CATALOG}
      />,
    );
    const expandBtn = screen.getByTestId(
      "grace2-tools-catalog-expand-fetch_dem",
    );
    expect(expandBtn.textContent).toBe("Show more");
    fireEvent.click(expandBtn);
    expect(expandBtn.textContent).toBe("Show less");
  });
});
