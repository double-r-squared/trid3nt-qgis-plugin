// GRACE-2 web — ChartStack + ChartGallery + buildChartStacks unit tests
// (sprint-13 job-0231: conversational analysis layer).
//
// Tests:
//   1. buildChartStacks — groups by created_turn_id; singletons each own a group.
//   2. ChartStack — renders top-card title; correct shadow count; +N badge logic.
//   3. ChartGallery — open/nav/close keyboard/arrow behaviour; counter text.
//
// vega-embed is mocked (happy-dom has no SVG rendering engine) — we verify
// the embed area div is mounted, not the actual Vega output.

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, act, cleanup } from "@testing-library/react";
import { useState } from "react";
import { buildChartStacks } from "../Chat";
import { ChartStack, type ChartPayload } from "./ChartStack";
import { ChartGallery } from "./ChartGallery";

// ---------------------------------------------------------------------------
// Mock vega-embed — happy-dom cannot render SVGs; we just need the embed
// call to succeed without error so ChartStack mounts cleanly.
// ---------------------------------------------------------------------------

vi.mock("vega-embed", () => ({
  default: vi.fn().mockResolvedValue({
    finalize: vi.fn(),
    view: {
      toImageURL: vi.fn().mockResolvedValue("data:image/png;base64,abc"),
    },
  }),
}));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const SPEC_A = {
  "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
  "mark": { "type": "bar" },
  "encoding": { "x": { "field": "bin_label", "type": "ordinal" } },
  "data": { "values": [] },
  "width": "container",
};

function makeChart(
  id: string,
  title: string,
  turnId: string | null = null,
): ChartPayload {
  return {
    chart_id: id,
    vega_lite_spec: SPEC_A,
    title,
    caption: `Caption for ${title}`,
    created_turn_id: turnId,
  };
}

// Two charts sharing turn "T1", two singletons (null turn_id)
const CHART_A = makeChart("chart-a", "Chart A", "T1");
const CHART_B = makeChart("chart-b", "Chart B", "T1");
const CHART_C = makeChart("chart-c", "Chart C", null);
const CHART_D = makeChart("chart-d", "Chart D", null);

// ---------------------------------------------------------------------------
// buildChartStacks
// ---------------------------------------------------------------------------

describe("buildChartStacks — grouping by created_turn_id", () => {
  it("groups charts with the same created_turn_id into one stack", () => {
    const stacks = buildChartStacks([CHART_A, CHART_B]);
    expect(stacks).toHaveLength(1);
    expect(stacks[0]).toHaveLength(2);
    expect(stacks[0]![0]!.chart_id).toBe("chart-a");
    expect(stacks[0]![1]!.chart_id).toBe("chart-b");
  });

  it("singletons (null created_turn_id) each get their own stack", () => {
    const stacks = buildChartStacks([CHART_C, CHART_D]);
    expect(stacks).toHaveLength(2);
    expect(stacks[0]![0]!.chart_id).toBe("chart-c");
    expect(stacks[1]![0]!.chart_id).toBe("chart-d");
  });

  it("preserves insertion order of group first-appearance", () => {
    const stacks = buildChartStacks([CHART_A, CHART_C, CHART_B, CHART_D]);
    // Groups: T1 (A+B), singleton-C, singleton-D
    expect(stacks).toHaveLength(3);
    expect(stacks[0]![0]!.chart_id).toBe("chart-a"); // T1 first appears at index 0
    expect(stacks[1]![0]!.chart_id).toBe("chart-c"); // singleton-C at index 1
    expect(stacks[2]![0]!.chart_id).toBe("chart-d"); // singleton-D at index 2
  });

  it("returns [] for empty input", () => {
    expect(buildChartStacks([])).toHaveLength(0);
  });

  it("returns a single singleton group for a single chart with null turn_id", () => {
    const stacks = buildChartStacks([CHART_C]);
    expect(stacks).toHaveLength(1);
    expect(stacks[0]![0]!.chart_id).toBe("chart-c");
  });
});

// ---------------------------------------------------------------------------
// ChartStack render
// ---------------------------------------------------------------------------

describe("ChartStack — render and badge logic", () => {
  afterEach(() => cleanup());

  it("renders the top chart title", async () => {
    const onOpen = vi.fn();
    render(
      <ChartStack
        charts={[CHART_A]}
        onOpenGallery={onOpen}
      />,
    );
    expect(screen.getByTestId("chart-stack")).toBeTruthy();
    expect(screen.getByTestId("chart-stack-top-card").textContent).toContain("Chart A");
  });

  it("renders no shadows for a singleton stack", () => {
    const onOpen = vi.fn();
    render(<ChartStack charts={[CHART_A]} onOpenGallery={onOpen} />);
    const shadows = screen.queryAllByTestId("chart-stack-shadow");
    expect(shadows).toHaveLength(0);
  });

  it("renders one shadow for a two-chart stack", () => {
    const onOpen = vi.fn();
    render(<ChartStack charts={[CHART_A, CHART_B]} onOpenGallery={onOpen} />);
    const shadows = screen.queryAllByTestId("chart-stack-shadow");
    expect(shadows).toHaveLength(1);
  });

  it("renders no +N badge for a 3-chart stack (≤ MAX_SHADOW_CARDS + 1 = 3)", () => {
    const charts = [
      makeChart("c1", "C1", "TX"),
      makeChart("c2", "C2", "TX"),
      makeChart("c3", "C3", "TX"),
    ];
    const onOpen = vi.fn();
    render(<ChartStack charts={charts} onOpenGallery={onOpen} />);
    expect(screen.queryByTestId("chart-stack-badge")).toBeNull();
  });

  it("renders a +1 badge for a 4-chart stack", () => {
    const charts = [
      makeChart("c1", "C1", "TX"),
      makeChart("c2", "C2", "TX"),
      makeChart("c3", "C3", "TX"),
      makeChart("c4", "C4", "TX"),
    ];
    const onOpen = vi.fn();
    render(<ChartStack charts={charts} onOpenGallery={onOpen} />);
    const badge = screen.getByTestId("chart-stack-badge");
    expect(badge.textContent).toBe("+1 more");
  });

  it("calls onOpenGallery with charts and index 0 on click", () => {
    const onOpen = vi.fn();
    render(<ChartStack charts={[CHART_A, CHART_B]} onOpenGallery={onOpen} />);
    fireEvent.click(screen.getByTestId("chart-stack"));
    expect(onOpen).toHaveBeenCalledOnce();
    expect(onOpen).toHaveBeenCalledWith([CHART_A, CHART_B], 0);
  });

  it("calls onOpenGallery on Enter key press", () => {
    const onOpen = vi.fn();
    render(<ChartStack charts={[CHART_A]} onOpenGallery={onOpen} />);
    fireEvent.keyDown(screen.getByTestId("chart-stack"), { key: "Enter" });
    expect(onOpen).toHaveBeenCalledOnce();
  });

  it("data-testid attributes carry chart metadata", () => {
    const onOpen = vi.fn();
    render(<ChartStack charts={[CHART_A, CHART_B]} onOpenGallery={onOpen} />);
    const el = screen.getByTestId("chart-stack");
    expect(el.dataset.chartCount).toBe("2");
    expect(el.dataset.topChartId).toBe("chart-a");
  });
});

// ---------------------------------------------------------------------------
// ChartGallery render, navigation, and close
// ---------------------------------------------------------------------------

describe("ChartGallery — open / nav / close", () => {
  afterEach(() => cleanup());

  const THREE_CHARTS = [
    makeChart("g1", "Gallery 1", "TG"),
    makeChart("g2", "Gallery 2", "TG"),
    makeChart("g3", "Gallery 3", "TG"),
  ];

  it("renders the gallery card with initial chart title", () => {
    const onClose = vi.fn();
    render(
      <ChartGallery
        charts={THREE_CHARTS}
        initialIndex={0}
        onClose={onClose}
      />,
    );
    expect(screen.getByTestId("chart-gallery")).toBeTruthy();
    expect(screen.getByTestId("chart-gallery-title").textContent).toBe("Gallery 1");
  });

  it("counter shows '1 / 3' for initial index 0 in a 3-chart list", () => {
    const onClose = vi.fn();
    render(
      <ChartGallery charts={THREE_CHARTS} initialIndex={0} onClose={onClose} />,
    );
    expect(screen.getByTestId("chart-gallery-counter").textContent).toBe("1 / 3");
  });

  it("Next button advances to chart 2", () => {
    const onClose = vi.fn();
    render(
      <ChartGallery charts={THREE_CHARTS} initialIndex={0} onClose={onClose} />,
    );
    fireEvent.click(screen.getByTestId("chart-gallery-next"));
    expect(screen.getByTestId("chart-gallery-title").textContent).toBe("Gallery 2");
    expect(screen.getByTestId("chart-gallery-counter").textContent).toBe("2 / 3");
  });

  it("Prev button goes back to chart 1", () => {
    const onClose = vi.fn();
    render(
      <ChartGallery charts={THREE_CHARTS} initialIndex={1} onClose={onClose} />,
    );
    fireEvent.click(screen.getByTestId("chart-gallery-prev"));
    expect(screen.getByTestId("chart-gallery-title").textContent).toBe("Gallery 1");
    expect(screen.getByTestId("chart-gallery-counter").textContent).toBe("1 / 3");
  });

  it("Prev button is disabled at index 0", () => {
    const onClose = vi.fn();
    render(
      <ChartGallery charts={THREE_CHARTS} initialIndex={0} onClose={onClose} />,
    );
    const prevBtn = screen.getByTestId("chart-gallery-prev");
    expect(prevBtn).toBeDisabled();
  });

  it("Next button is disabled at last index", () => {
    const onClose = vi.fn();
    render(
      <ChartGallery charts={THREE_CHARTS} initialIndex={2} onClose={onClose} />,
    );
    const nextBtn = screen.getByTestId("chart-gallery-next");
    expect(nextBtn).toBeDisabled();
  });

  it("Close button calls onClose", () => {
    const onClose = vi.fn();
    render(
      <ChartGallery charts={THREE_CHARTS} initialIndex={0} onClose={onClose} />,
    );
    fireEvent.click(screen.getByTestId("chart-gallery-close"));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("Backdrop click calls onClose", () => {
    const onClose = vi.fn();
    render(
      <ChartGallery charts={THREE_CHARTS} initialIndex={0} onClose={onClose} />,
    );
    fireEvent.click(screen.getByTestId("chart-gallery"));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("Esc key calls onClose", () => {
    const onClose = vi.fn();
    render(
      <ChartGallery charts={THREE_CHARTS} initialIndex={0} onClose={onClose} />,
    );
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("ArrowRight key navigates to next chart", () => {
    const onClose = vi.fn();
    render(
      <ChartGallery charts={THREE_CHARTS} initialIndex={0} onClose={onClose} />,
    );
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight" }));
    });
    expect(screen.getByTestId("chart-gallery-counter").textContent).toBe("2 / 3");
  });

  it("ArrowLeft key navigates to previous chart", () => {
    const onClose = vi.fn();
    render(
      <ChartGallery charts={THREE_CHARTS} initialIndex={2} onClose={onClose} />,
    );
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft" }));
    });
    expect(screen.getByTestId("chart-gallery-counter").textContent).toBe("2 / 3");
  });

  it("renders caption from chart payload", () => {
    const onClose = vi.fn();
    render(
      <ChartGallery charts={THREE_CHARTS} initialIndex={0} onClose={onClose} />,
    );
    expect(screen.getByTestId("chart-gallery-caption").textContent).toBe(
      "Caption for Gallery 1",
    );
  });

  it("opens at the correct initialIndex", () => {
    const onClose = vi.fn();
    render(
      <ChartGallery charts={THREE_CHARTS} initialIndex={2} onClose={onClose} />,
    );
    expect(screen.getByTestId("chart-gallery-counter").textContent).toBe("3 / 3");
    expect(screen.getByTestId("chart-gallery-title").textContent).toBe("Gallery 3");
  });
});

// ---------------------------------------------------------------------------
// Rehydration repopulation (via dev-seam pattern)
// ---------------------------------------------------------------------------

describe("Chart rehydration — dev seam injection accumulates charts", () => {
  afterEach(() => cleanup());

  // Minimal shell that mirrors App.tsx's chart state + dev seam.
  function ChartShell(): JSX.Element {
    const [chartList, setChartList] = useState<ChartPayload[]>([]);
    const [galleryOpen, setGalleryOpen] = useState(false);
    const [galleryCharts, setGalleryCharts] = useState<ChartPayload[]>([]);

    // Wire the inject seam.
    // (In production App.tsx does this; here we test the same pattern.)
    const handleInject = (p: ChartPayload) => {
      setChartList((prev) =>
        prev.some((c) => c.chart_id === p.chart_id) ? prev : [...prev, p],
      );
    };

    return (
      <div>
        <span
          data-testid="chart-count"
          data-value={String(chartList.length)}
        />
        <button
          data-testid="inject-a"
          onClick={() => handleInject(CHART_A)}
        >
          inject A
        </button>
        <button
          data-testid="inject-a-again"
          onClick={() => handleInject(CHART_A)}
        >
          inject A again (dup)
        </button>
        <button
          data-testid="inject-b"
          onClick={() => handleInject(CHART_B)}
        >
          inject B
        </button>
        {chartList.map((c) => (
          <span key={c.chart_id} data-testid={`chart-${c.chart_id}`} />
        ))}
        {galleryOpen && (
          <ChartGallery
            charts={galleryCharts}
            initialIndex={0}
            onClose={() => setGalleryOpen(false)}
          />
        )}
        <button
          data-testid="open-gallery"
          onClick={() => { setGalleryCharts(chartList); setGalleryOpen(true); }}
        >
          open gallery
        </button>
      </div>
    );
  }

  it("accumulates charts from multiple injections", () => {
    render(<ChartShell />);
    fireEvent.click(screen.getByTestId("inject-a"));
    fireEvent.click(screen.getByTestId("inject-b"));
    expect(screen.getByTestId("chart-count").dataset.value).toBe("2");
    expect(screen.getByTestId("chart-chart-a")).toBeTruthy();
    expect(screen.getByTestId("chart-chart-b")).toBeTruthy();
  });

  it("de-duplicates charts with the same chart_id", () => {
    render(<ChartShell />);
    fireEvent.click(screen.getByTestId("inject-a"));
    fireEvent.click(screen.getByTestId("inject-a-again"));
    expect(screen.getByTestId("chart-count").dataset.value).toBe("1");
  });

  it("gallery opens with the accumulated chart list", () => {
    render(<ChartShell />);
    fireEvent.click(screen.getByTestId("inject-a"));
    fireEvent.click(screen.getByTestId("inject-b"));
    fireEvent.click(screen.getByTestId("open-gallery"));
    // Gallery should show counter "1 / 2".
    expect(screen.getByTestId("chart-gallery-counter").textContent).toBe("1 / 2");
  });
});
