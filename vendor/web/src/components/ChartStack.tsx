// GRACE-2 web — ChartStack (sprint-13, conversational analysis layer, job-0231;
// job-0294 full-chat-width redesign).
//
// Renders a group of chart-emission payloads that share the same ``created_turn_id``
// as an inline preview in the chat scroll. Layout (job-0294):
//
//   - The visible top chart spans the ENTIRE chat column width (the parent's
//     content box) and renders legibly inline via Vega-Lite (vega-embed).
//   - The Vega chart RE-FITS to whatever width the container currently has — a
//     ResizeObserver re-embeds on container resize (so the desktop chat-expand
//     toggle in item C, or a window resize, reflows the chart cleanly).
//   - Additional charts in the same stack appear as offset card "shadows" behind
//     the visible one (4 px offset each), giving a tangible "N charts here" cue.
//   - When the stack has more than 3 charts total, a "+N more" badge appears in the
//     bottom-right corner of the top card.
//   - Clicking anywhere on the stack (card or shadows) opens ChartGallery.
//
// A singleton stack (1 chart with no siblings) renders identically to the top card
// of a multi-chart stack — the shadows simply do not appear.
//
// Stack grouping is performed by the parent (App.tsx / Chat.tsx) via
// ``created_turn_id``; this component receives an already-grouped array and renders
// it. It does NOT perform grouping itself (single-responsibility).
//
// Vega-embed note: we let the library render into a ref'd div. We re-embed on
// ``created_turn_id`` / ``charts`` change AND on a width change observed via
// ResizeObserver. This is idiomatic for vega-embed in React — the library is
// not React-native, so we use the DOM seam cleanly.

import { useCallback, useEffect, useRef, useState } from "react";
import type { Result as VegaEmbedResult } from "vega-embed";

/** Minimal wire shape — matches ChartEmissionPayload from chart_contracts.py. */
export interface ChartPayload {
  chart_id: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  vega_lite_spec: Record<string, any>;
  title: string;
  caption?: string | null;
  source_layer_uri?: string | null;
  created_turn_id?: string | null;
}

export interface ChartStackProps {
  /** One or more charts that share the same ``created_turn_id`` (or are singletons). */
  charts: ChartPayload[];
  /** Called when the user clicks the stack, to open the gallery at ``initialIndex``. */
  onOpenGallery: (charts: ChartPayload[], initialIndex: number) => void;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Maximum shadow cards rendered (the rest are counted in the +N badge). */
const MAX_SHADOW_CARDS = 2;
/** Pixel offset between stacked shadow cards. */
const SHADOW_OFFSET_PX = 4;
/** Chart drawing height (px). Width is fluid — it tracks the chat column. */
const CHART_HEIGHT = 220;
/** Horizontal padding inside the top card (left + right), used to size the embed. */
const CARD_PAD_X = 12;
/** Fallback width when the container hasn't been measured yet (SSR / first paint). */
const FALLBACK_WIDTH = 320;
/** Vega internal padding. */
const VEGA_PADDING = 4;
/**
 * F51 (iOS Safari blank-chart fix): the embed effect must not run vega-embed into
 * a container that has a *committed* width of 0 — on iOS the first paint can report
 * `clientWidth === 0` before the inline card lays out, and embedding into a 0-size
 * box builds an empty/invalid scenegraph ("undefined is not an object (evaluating
 * 'l.marktype')"). We instead defer via rAF, letting the ResizeObserver deliver a
 * real width. But measurement can be permanently impossible (jsdom/happy-dom report
 * clientWidth 0 and never fire layout) — so after this many rAF retries we stop
 * waiting and embed at FALLBACK_WIDTH rather than skipping the chart forever.
 */
const MAX_ZERO_WIDTH_RETRIES = 3;

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const containerStyle: React.CSSProperties = {
  position: "relative",
  // job-0294 — full chat-column width (was inline-block ~200px).
  display: "block",
  width: "100%",
  cursor: "pointer",
};

function shadowStyle(index: number): React.CSSProperties {
  // index 0 = first shadow (second chart in stack), etc.
  const offset = (index + 1) * SHADOW_OFFSET_PX;
  return {
    position: "absolute",
    top: offset,
    left: offset,
    right: -offset,
    // job-0294 — shadows span the full width too, peeking out behind the top
    // card by the offset on each side. Bottom is anchored so they hug the card.
    height: "100%",
    background: "rgba(30,32,42,0.75)",
    border: "1px solid #3a3d49",
    borderRadius: 8,
    // Shadows sit BELOW the top card (negative z-index relative to parent).
    zIndex: -(index + 1),
  };
}

const topCardStyle: React.CSSProperties = {
  position: "relative",
  background: "rgba(20,22,30,0.96)",
  border: "1px solid #444",
  borderRadius: 8,
  padding: `8px ${CARD_PAD_X}px 6px`,
  boxShadow: "0 4px 16px rgba(0,0,0,0.35)",
  zIndex: 1,
  width: "100%",
  boxSizing: "border-box",
};

const titleStyle: React.CSSProperties = {
  fontSize: 12,
  fontWeight: 600,
  color: "#dde5f5",
  marginBottom: 6,
  whiteSpace: "nowrap",
  overflow: "hidden",
  textOverflow: "ellipsis",
};

const captionStyle: React.CSSProperties = {
  fontSize: 11,
  color: "#9aa0ad",
  marginTop: 6,
  lineHeight: 1.35,
};

const badgeStyle: React.CSSProperties = {
  position: "absolute",
  bottom: 8,
  right: 8,
  background: "rgba(60,80,120,0.92)",
  border: "1px solid #4a6096",
  borderRadius: 10,
  padding: "1px 6px",
  fontSize: 10,
  color: "#9fcfff",
  fontWeight: 600,
  pointerEvents: "none",
};

const chartAreaStyle: React.CSSProperties = {
  width: "100%",
  // F51 — belt-and-suspenders: a hard pixel floor so the container is never a
  // true 0-width box at embed time on iOS, even before the flex row lays out.
  minWidth: 80,
  height: CHART_HEIGHT,
  overflow: "hidden",
  borderRadius: 4,
  background: "rgba(12,14,20,0.8)",
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/** Dynamically import vega-embed to avoid bloating the initial bundle. */
async function embedChart(
  el: HTMLElement,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  spec: Record<string, any>,
  width: number,
): Promise<VegaEmbedResult> {
  const { default: embed } = await import("vega-embed");
  return embed(el, spec as Parameters<typeof embed>[1], {
    actions: false,
    renderer: "svg",
    // job-0294 — fluid width: the chart re-fits to the measured container.
    width: Math.max(80, width - VEGA_PADDING * 2),
    height: CHART_HEIGHT - VEGA_PADDING * 2,
    padding: VEGA_PADDING,
    config: {
      background: "transparent",
      axis: { labelColor: "#9aa0ad", titleColor: "#9aa0ad", gridColor: "#2a2d35" },
      title: { color: "#dde5f5", fontSize: 12 },
      legend: { labelColor: "#9aa0ad", titleColor: "#9aa0ad" },
      view: { stroke: "transparent" },
    },
  });
}

export function ChartStack({ charts, onOpenGallery }: ChartStackProps): JSX.Element | null {
  const chartAreaRef = useRef<HTMLDivElement | null>(null);
  const vegaResultRef = useRef<VegaEmbedResult | null>(null);
  const [embedError, setEmbedError] = useState<string | null>(null);
  // job-0294 — the live measured drawing width (container content box). Drives
  // a re-embed when the chat column widens/narrows (item C toggle, resize).
  // F51 — seeded to 0 (UNMEASURED), not FALLBACK_WIDTH: a non-zero seed would mask
  // an unlaid-out container on iOS and embed into a 0-size box. The embed effect
  // treats 0 as "defer until a real width arrives (ResizeObserver / rAF)".
  const [embedWidth, setEmbedWidth] = useState<number>(0);

  const topChart = charts[0];

  // Observe the chart-area width and debounce-set embedWidth. The
  // ResizeObserver callback fires on the chat-expand toggle, window resize, and
  // first layout. happy-dom (vitest) lacks ResizeObserver, so we guard for it.
  useEffect(() => {
    const el = chartAreaRef.current;
    if (!el) return;
    // Seed from the current layout immediately.
    if (el.clientWidth > 0) setEmbedWidth(el.clientWidth);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const w = entry.contentRect.width;
        if (w > 0) setEmbedWidth((prev) => (Math.abs(prev - w) > 1 ? w : prev));
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Embed or re-embed whenever the top spec OR the measured width changes.
  //
  // F51 — iOS Safari blank-chart guard: never embed into a container whose
  // *committed* width is 0. On iOS the first paint can report clientWidth 0 before
  // the inline card lays out; embedding then yields an empty scenegraph and the
  // "undefined is not an object (evaluating 'l.marktype')" crash. We defer via rAF
  // and let the ResizeObserver deliver a real width — but cap the wait so a
  // genuinely unmeasurable environment (jsdom/happy-dom) still embeds (at
  // FALLBACK_WIDTH) rather than skipping the chart forever.
  useEffect(() => {
    if (!chartAreaRef.current || !topChart) return;
    let cancelled = false;
    let rafId: number | null = null;
    setEmbedError(null);

    const doEmbed = async (drawWidth: number): Promise<void> => {
      // Finalize the previous embed before starting a new one (no double-embed).
      if (vegaResultRef.current) {
        try { vegaResultRef.current.finalize(); } catch { /* ignore */ }
        vegaResultRef.current = null;
      }
      if (!chartAreaRef.current || cancelled) return;
      try {
        const result = await embedChart(
          chartAreaRef.current,
          topChart.vega_lite_spec,
          drawWidth,
        );
        if (!cancelled) {
          vegaResultRef.current = result;
        } else {
          try { result.finalize(); } catch { /* ignore */ }
        }
      } catch (err) {
        if (!cancelled) {
          // Keep a non-zero min-size (see chartAreaStyle.minWidth) so the error
          // text below is visible even when the embed itself failed.
          setEmbedError(err instanceof Error ? err.message : "chart render error");
        }
      }
    };

    // Resolve the drawing width: prefer the live committed clientWidth, fall back
    // to the observed embedWidth state. If BOTH are 0 (container not laid out yet),
    // defer for up to MAX_ZERO_WIDTH_RETRIES animation frames before giving up and
    // using FALLBACK_WIDTH so the chart is never permanently skipped.
    const resolveAndEmbed = (retries: number): void => {
      if (cancelled || !chartAreaRef.current) return;
      const live = chartAreaRef.current.clientWidth;
      const measured = live > 0 ? live : embedWidth > 0 ? embedWidth : 0;
      if (measured > 0) {
        void doEmbed(measured);
        return;
      }
      // Container has no committed width yet.
      if (retries < MAX_ZERO_WIDTH_RETRIES && typeof requestAnimationFrame === "function") {
        rafId = requestAnimationFrame(() => resolveAndEmbed(retries + 1));
        return;
      }
      // Measurement impossible (or exhausted) — embed at the fallback width so the
      // chart still renders rather than staying blank.
      void doEmbed(FALLBACK_WIDTH);
    };

    resolveAndEmbed(0);

    return () => {
      cancelled = true;
      if (rafId !== null && typeof cancelAnimationFrame === "function") {
        cancelAnimationFrame(rafId);
      }
    };
    // re-embed when the top chart changes OR the container width changes.
  }, [topChart?.chart_id, topChart?.vega_lite_spec, embedWidth]);

  // Finalize on unmount.
  useEffect(() => {
    return () => {
      if (vegaResultRef.current) {
        try { vegaResultRef.current.finalize(); } catch { /* ignore */ }
        vegaResultRef.current = null;
      }
    };
  }, []);

  const handleClick = useCallback(() => {
    onOpenGallery(charts, 0);
  }, [charts, onOpenGallery]);

  if (!topChart) return null;

  // Shadows: render at most MAX_SHADOW_CARDS behind the top card.
  const shadowCount = Math.min(charts.length - 1, MAX_SHADOW_CARDS);
  // Badge: "+N" where N = total charts beyond the MAX_SHADOW_CARDS + 1 visible.
  const hiddenCount = charts.length - (MAX_SHADOW_CARDS + 1);
  const showBadge = hiddenCount > 0;

  return (
    <div
      data-testid="chart-stack"
      data-chart-count={charts.length}
      data-top-chart-id={topChart.chart_id}
      style={containerStyle}
      onClick={handleClick}
      role="button"
      aria-label={`Chart: ${topChart.title}${charts.length > 1 ? ` (+${charts.length - 1} more)` : ""}. Click to open gallery.`}
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          handleClick();
        }
      }}
    >
      {/* Shadow cards behind the top card (reversed so shadow[0] is closest to top) */}
      {Array.from({ length: shadowCount }, (_, i) => (
        <div
          key={`shadow-${i}`}
          data-testid="chart-stack-shadow"
          style={shadowStyle(shadowCount - 1 - i)}
          aria-hidden="true"
        />
      ))}

      {/* Top card */}
      <div
        data-testid="chart-stack-top-card"
        style={topCardStyle}
      >
        <div style={titleStyle} title={topChart.title}>
          {topChart.title}
        </div>

        <div
          ref={chartAreaRef}
          data-testid="chart-embed-area"
          style={chartAreaStyle}
        >
          {embedError && (
            <div
              style={{
                color: "#f9c1c1",
                fontSize: 11,
                padding: 6,
                lineHeight: 1.4,
              }}
            >
              Chart render error: {embedError}
            </div>
          )}
        </div>

        {topChart.caption && (
          <div style={captionStyle} title={topChart.caption}>
            {topChart.caption}
          </div>
        )}

        {showBadge && (
          <div
            data-testid="chart-stack-badge"
            style={badgeStyle}
          >
            +{hiddenCount} more
          </div>
        )}
      </div>
    </div>
  );
}
