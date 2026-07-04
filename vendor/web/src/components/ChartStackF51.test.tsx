// GRACE-2 web — F51 regression tests (job-0321, Group D).
//
// F51: the Pelicun damage-state chart rendered BLANK on iOS Safari with
// "undefined is not an object (evaluating 'l.marktype')". Root cause: the
// vega-embed container had a *committed* width of 0 at embed time on mobile
// (popup/inline card not laid out yet), so vega built an empty/invalid
// scenegraph. The fix hardens BOTH embed paths (ChartStack inline preview +
// ChartGallery popup) to:
//   (1) never embed into a 0-committed-width box — defer via rAF until a real
//       width arrives (ResizeObserver / next frame),
//   (2) but NEVER permanently skip the embed: after a bounded number of rAF
//       retries (or when measurement is impossible, as in jsdom/happy-dom),
//       embed at the fallback / explicit width,
//   (3) keep renderer:"svg" and a non-zero min-size so the error path is visible.
//
// These tests assert the *behaviour*: embed is called with a positive, explicit
// width+height; the deferred path eventually fires; the error path surfaces; and
// the container carries a non-zero min-width floor.
//
// vega-embed is mocked — happy-dom has no SVG engine, and (critically) happy-dom
// reports clientWidth === 0 for every element with a real ResizeObserver stub that
// never fires. That is the same "unmeasurable" condition the fix must survive, so
// these tests exercise the genuine fallback path.

import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, act, cleanup } from "@testing-library/react";
import { ChartStack, type ChartPayload } from "./ChartStack";
import { ChartGallery } from "./ChartGallery";

// ---------------------------------------------------------------------------
// Mock vega-embed and capture the call so we can assert on its options.
// ---------------------------------------------------------------------------

const embedMock = vi.fn();

vi.mock("vega-embed", () => ({
  default: (el: HTMLElement, spec: unknown, opts: unknown) => embedMock(el, spec, opts),
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const BAR_SPEC = {
  $schema: "https://vega.github.io/schema/vega-lite/v5.json",
  mark: { type: "bar" },
  encoding: { x: { field: "bin_label", type: "ordinal" } },
  data: { values: [{ bin_label: "DS1", n: 3 }] },
  width: "container",
};

function makeChart(id: string, title: string, turnId: string | null = null): ChartPayload {
  return { chart_id: id, vega_lite_spec: BAR_SPEC, title, caption: null, created_turn_id: turnId };
}

/** Override clientWidth for ALL elements (simulates a laid-out desktop container). */
function withClientWidth(px: number): () => void {
  const orig = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth");
  Object.defineProperty(HTMLElement.prototype, "clientWidth", {
    configurable: true,
    get() { return px; },
  });
  return () => {
    if (orig) Object.defineProperty(HTMLElement.prototype, "clientWidth", orig);
    else delete (HTMLElement.prototype as unknown as Record<string, unknown>).clientWidth;
  };
}

/** Default resolved embed result (a finalize-able view). */
function okResult() {
  return {
    finalize: vi.fn(),
    view: { toImageURL: vi.fn().mockResolvedValue("data:image/png;base64,x") },
  };
}

/** Flush the deferred (rAF) + awaited (dynamic import + embed) chain to completion. */
async function flushDeferredEmbed(): Promise<void> {
  await act(async () => {
    await vi.runAllTimersAsync();
  });
}

beforeEach(() => {
  embedMock.mockReset();
  embedMock.mockResolvedValue(okResult());
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

// ---------------------------------------------------------------------------
// ChartStack (inline preview) — F51
// ---------------------------------------------------------------------------

describe("F51 — ChartStack inline embed never blanks", () => {
  it("embeds with a POSITIVE explicit width+height (svg renderer)", async () => {
    render(<ChartStack charts={[makeChart("c1", "Damage states")]} onOpenGallery={vi.fn()} />);
    await flushDeferredEmbed();

    expect(embedMock).toHaveBeenCalled();
    const opts = embedMock.mock.calls.at(-1)![2] as Record<string, unknown>;
    expect(typeof opts.width).toBe("number");
    expect(opts.width as number).toBeGreaterThan(0);
    expect(typeof opts.height).toBe("number");
    expect(opts.height as number).toBeGreaterThan(0);
    expect(opts.renderer).toBe("svg");
  });

  it("falls back to a real width when the container is UNMEASURABLE (clientWidth 0)", async () => {
    // happy-dom default: clientWidth === 0 + ResizeObserver never fires. The
    // chart must STILL embed (the iOS-blank bug was the chart being skipped).
    render(<ChartStack charts={[makeChart("c2", "Bars")]} onOpenGallery={vi.fn()} />);
    await flushDeferredEmbed();

    expect(embedMock).toHaveBeenCalledTimes(1);
    const opts = embedMock.mock.calls[0]![2] as Record<string, unknown>;
    // FALLBACK_WIDTH (320) minus 2*VEGA_PADDING(4) => 312; clamped to >= 80.
    expect(opts.width as number).toBeGreaterThanOrEqual(80);
  });

  it("uses the MEASURED width when the container is laid out (desktop synchronous path)", async () => {
    const restore = withClientWidth(900);
    try {
      render(<ChartStack charts={[makeChart("c3", "Bars")]} onOpenGallery={vi.fn()} />);
      await flushDeferredEmbed();
      const opts = embedMock.mock.calls.at(-1)![2] as Record<string, unknown>;
      // 900 - 2*VEGA_PADDING(4) = 892.
      expect(opts.width as number).toBe(892);
    } finally {
      restore();
    }
  });

  it("surfaces embedError text when vega-embed rejects", async () => {
    embedMock.mockRejectedValueOnce(new Error("undefined is not an object (evaluating 'l.marktype')"));
    render(<ChartStack charts={[makeChart("c4", "Bars")]} onOpenGallery={vi.fn()} />);
    await flushDeferredEmbed();

    const area = screen.getByTestId("chart-embed-area");
    expect(area.textContent).toContain("Chart render error");
    expect(area.textContent).toContain("l.marktype");
  });

  it("chart embed area carries a non-zero min-width floor (belt-and-suspenders)", () => {
    render(<ChartStack charts={[makeChart("c5", "Bars")]} onOpenGallery={vi.fn()} />);
    const area = screen.getByTestId("chart-embed-area");
    // jsdom serialises minWidth to a px string.
    expect(area.style.minWidth).not.toBe("");
    expect(area.style.minWidth).not.toBe("0px");
  });

  it("does not double-embed for a single chart (one finalize-able embed)", async () => {
    render(<ChartStack charts={[makeChart("c6", "Bars")]} onOpenGallery={vi.fn()} />);
    await flushDeferredEmbed();
    expect(embedMock).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// ChartGallery (popup) — F51
// ---------------------------------------------------------------------------

describe("F51 — ChartGallery popup embed never blanks", () => {
  const CHARTS = [makeChart("g1", "Damage states", "TG")];

  it("defers but eventually embeds even when popup container is unmeasurable", async () => {
    render(<ChartGallery charts={CHARTS} initialIndex={0} onClose={vi.fn()} />);
    await flushDeferredEmbed();
    expect(embedMock).toHaveBeenCalledTimes(1);
  });

  it("embeds with the explicit gallery width+height (svg renderer)", async () => {
    render(<ChartGallery charts={CHARTS} initialIndex={0} onClose={vi.fn()} />);
    await flushDeferredEmbed();
    const opts = embedMock.mock.calls.at(-1)![2] as Record<string, unknown>;
    expect(opts.width as number).toBeGreaterThan(0);
    expect(opts.height as number).toBeGreaterThan(0);
    expect(opts.renderer).toBe("svg");
  });

  it("embeds synchronously (no rAF wait) when the card is already laid out", async () => {
    const restore = withClientWidth(620);
    try {
      render(<ChartGallery charts={CHARTS} initialIndex={0} onClose={vi.fn()} />);
      // Even without advancing timers, the awaited microtask chain resolves the
      // embed because clientWidth>0 takes the immediate path (no rAF deferral).
      await act(async () => { await Promise.resolve(); await Promise.resolve(); });
      expect(embedMock).toHaveBeenCalledTimes(1);
    } finally {
      restore();
    }
  });

  it("surfaces embedError text in the gallery when vega-embed rejects", async () => {
    embedMock.mockRejectedValueOnce(new Error("l.marktype boom"));
    render(<ChartGallery charts={CHARTS} initialIndex={0} onClose={vi.fn()} />);
    await flushDeferredEmbed();
    const area = screen.getByTestId("chart-gallery-embed-area");
    expect(area.textContent).toContain("Chart render error");
    expect(area.textContent).toContain("l.marktype");
  });

  it("gallery embed area carries a non-zero min-width floor", () => {
    render(<ChartGallery charts={CHARTS} initialIndex={0} onClose={vi.fn()} />);
    const area = screen.getByTestId("chart-gallery-embed-area");
    expect(area.style.minWidth).not.toBe("");
    expect(area.style.minWidth).not.toBe("0px");
  });
});
