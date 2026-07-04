// GRACE-2 web — ChartGallery (sprint-13, conversational analysis layer, job-0231).
//
// Full-viewport overlay that shows chart-emission payloads one at a time with
// prev/next navigation, save-as-PNG, and Esc / backdrop-click to dismiss.
//
// Visual conventions match RoutingQualityDashboard.tsx (dark theme, rgba panel,
// rounded card, ✕ close button at top-right, backdrop at rgba(0,0,0,0.55)).
//
// Navigation:
//   - Left/Right arrow buttons
//   - Keyboard ← / → to navigate, Esc to close
//
// Save-as-PNG:
//   - vega-embed exposes a ``view.toImageURL("png")`` API; we use it to export
//     the rendered chart to a downloadable PNG anchored to the chart title.
//   - Falls back to a no-op with a console.warn if the view is not yet ready.
//
// Backdrop close: clicking the dark overlay (not the card) closes the gallery.
// Esc key: closes the gallery from any focused child.

import { useCallback, useEffect, useRef, useState } from "react";
import type { Result as VegaEmbedResult } from "vega-embed";
import type { ChartPayload } from "./ChartStack";
import { IconClose, IconChevronLeft, IconChevronRight } from "./icons";

export interface ChartGalleryProps {
  /** All charts to browse (typically the full session chart list). */
  charts: ChartPayload[];
  /** The chart index to open first (0-based). */
  initialIndex: number;
  /** Called to close the gallery. */
  onClose: () => void;
}

// ---------------------------------------------------------------------------
// Layout constants
// ---------------------------------------------------------------------------

const CHART_GALLERY_WIDTH = 600;
const CHART_GALLERY_HEIGHT = 400;
/**
 * F51 (iOS Safari blank-chart fix): the popup card can mount with a container that
 * has a committed width of 0 on the first paint (the flex card has not laid out yet),
 * and embedding vega into a 0-size box builds an empty/invalid scenegraph
 * ("undefined is not an object (evaluating 'l.marktype')"). We defer the embed via
 * rAF until the container reports a real width, but cap the wait so an environment
 * that can never measure (jsdom/happy-dom: clientWidth always 0) still embeds rather
 * than skipping the chart forever.
 */
const MAX_ZERO_WIDTH_RETRIES = 3;

// ---------------------------------------------------------------------------
// Styles — mirrors RoutingQualityDashboard dark modal conventions
// ---------------------------------------------------------------------------

const overlayStyle: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "rgba(0,0,0,0.65)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 10_000,
  fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
};

const cardStyle: React.CSSProperties = {
  background: "rgba(20,22,30,0.98)",
  border: "1px solid #444",
  borderRadius: 12,
  width: `min(${CHART_GALLERY_WIDTH + 48}px, 96vw)`,
  maxHeight: "92vh",
  display: "flex",
  flexDirection: "column",
  color: "#e8eaf0",
  boxShadow: "0 24px 64px rgba(0,0,0,0.55)",
  position: "relative",
  padding: "20px 24px 20px",
  overflow: "hidden",
};

const headerRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  marginBottom: 12,
  gap: 8,
  minHeight: 28,
};

const titleStyle: React.CSSProperties = {
  fontSize: 15,
  fontWeight: 600,
  color: "#e8eaf0",
  flex: 1,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  margin: 0,
};

const closeBtnStyle: React.CSSProperties = {
  background: "transparent",
  border: "none",
  color: "#aaa",
  fontSize: 18,
  cursor: "pointer",
  width: 28,
  height: 28,
  borderRadius: 6,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  flexShrink: 0,
};

const chartAreaStyle: React.CSSProperties = {
  width: "100%",
  // F51 — belt-and-suspenders explicit pixel floor so the embed container is never
  // a true 0-width box before the flex card lays out on iOS Safari.
  minWidth: CHART_GALLERY_WIDTH,
  height: CHART_GALLERY_HEIGHT,
  borderRadius: 6,
  background: "rgba(12,14,20,0.8)",
  overflow: "hidden",
  flexShrink: 0,
};

const captionStyle: React.CSSProperties = {
  fontSize: 11,
  color: "#9aa0ad",
  marginTop: 8,
  lineHeight: 1.4,
  minHeight: 16,
};

const navRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  marginTop: 12,
  gap: 8,
};

const navBtnStyle: React.CSSProperties = {
  background: "rgba(40,42,52,0.9)",
  border: "1px solid #555",
  borderRadius: 6,
  color: "#ddd",
  padding: "6px 14px",
  fontSize: 13,
  cursor: "pointer",
  fontFamily: "inherit",
  display: "flex",
  alignItems: "center",
  gap: 4,
};

const navBtnDisabledStyle: React.CSSProperties = {
  ...navBtnStyle,
  opacity: 0.4,
  cursor: "default",
};

const counterStyle: React.CSSProperties = {
  fontSize: 12,
  color: "#9aa0ad",
  fontVariantNumeric: "tabular-nums",
};

const saveBtnStyle: React.CSSProperties = {
  background: "rgba(30,50,90,0.85)",
  border: "1px solid #3b6ab5",
  borderRadius: 6,
  color: "#9fcfff",
  padding: "6px 14px",
  fontSize: 12,
  cursor: "pointer",
  fontFamily: "inherit",
};

// ---------------------------------------------------------------------------
// Embed helper
// ---------------------------------------------------------------------------

async function embedGalleryChart(
  el: HTMLElement,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  spec: Record<string, any>,
): Promise<VegaEmbedResult> {
  const { default: embed } = await import("vega-embed");
  return embed(el, spec as Parameters<typeof embed>[1], {
    actions: false,
    renderer: "svg",
    width: CHART_GALLERY_WIDTH,
    height: CHART_GALLERY_HEIGHT - 16,
    padding: 8,
    config: {
      background: "transparent",
      axis: { labelColor: "#9aa0ad", titleColor: "#9aa0ad", gridColor: "#2a2d35" },
      title: { color: "#dde5f5", fontSize: 13 },
      legend: { labelColor: "#9aa0ad", titleColor: "#9aa0ad" },
      view: { stroke: "transparent" },
    },
  });
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ChartGallery({
  charts,
  initialIndex,
  onClose,
}: ChartGalleryProps): JSX.Element | null {
  const [currentIndex, setCurrentIndex] = useState<number>(
    Math.max(0, Math.min(initialIndex, charts.length - 1)),
  );
  const [saveError, setSaveError] = useState<string | null>(null);
  const [embedError, setEmbedError] = useState<string | null>(null);

  const chartAreaRef = useRef<HTMLDivElement | null>(null);
  const vegaResultRef = useRef<VegaEmbedResult | null>(null);

  const currentChart = charts[currentIndex] ?? null;

  // Keyboard handling: Esc = close, ← / → = navigate.
  useEffect(() => {
    function onKey(e: KeyboardEvent): void {
      if (e.key === "Escape") { onClose(); return; }
      if (e.key === "ArrowLeft") {
        setCurrentIndex((i) => Math.max(0, i - 1));
      } else if (e.key === "ArrowRight") {
        setCurrentIndex((i) => Math.min(charts.length - 1, i + 1));
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, charts.length]);

  // Embed (or re-embed) when the current chart changes.
  //
  // F51 — iOS Safari blank-chart guard: do not run vega-embed until the popup card
  // has actually laid out (clientWidth > 0). On iOS the first paint can report
  // clientWidth 0 for the embed area before the flex card resolves its width, and
  // embedding then yields an empty scenegraph + the "undefined is not an object
  // (evaluating 'l.marktype')" crash. We defer via rAF until a real width arrives,
  // but cap the wait so an unmeasurable env (jsdom/happy-dom) still embeds.
  useEffect(() => {
    if (!chartAreaRef.current || !currentChart) return;
    let cancelled = false;
    let rafId: number | null = null;
    setEmbedError(null);
    setSaveError(null);

    const doEmbed = async (): Promise<void> => {
      // Finalize previous embed (no double-embed / flicker on re-run).
      if (vegaResultRef.current) {
        try { vegaResultRef.current.finalize(); } catch { /* ignore */ }
        vegaResultRef.current = null;
      }
      if (!chartAreaRef.current || cancelled) return;
      try {
        const result = await embedGalleryChart(chartAreaRef.current, currentChart.vega_lite_spec);
        if (cancelled) {
          try { result.finalize(); } catch { /* ignore */ }
        } else {
          vegaResultRef.current = result;
        }
      } catch (err) {
        if (!cancelled) {
          setEmbedError(err instanceof Error ? err.message : "chart render error");
        }
      }
    };

    // Wait for the card to have a committed width before embedding. Defer up to
    // MAX_ZERO_WIDTH_RETRIES frames, then embed regardless (the explicit
    // CHART_GALLERY_WIDTH passed to embed + chartAreaStyle.minWidth keep it sane).
    const waitForLayoutThenEmbed = (retries: number): void => {
      if (cancelled || !chartAreaRef.current) return;
      const laidOut = chartAreaRef.current.clientWidth > 0;
      if (laidOut || retries >= MAX_ZERO_WIDTH_RETRIES || typeof requestAnimationFrame !== "function") {
        void doEmbed();
        return;
      }
      rafId = requestAnimationFrame(() => waitForLayoutThenEmbed(retries + 1));
    };

    waitForLayoutThenEmbed(0);

    return () => {
      cancelled = true;
      if (rafId !== null && typeof cancelAnimationFrame === "function") {
        cancelAnimationFrame(rafId);
      }
    };
  }, [currentChart?.chart_id, currentChart?.vega_lite_spec]);

  // Finalize on unmount.
  useEffect(() => {
    return () => {
      if (vegaResultRef.current) {
        try { vegaResultRef.current.finalize(); } catch { /* ignore */ }
        vegaResultRef.current = null;
      }
    };
  }, []);

  const handleSavePng = useCallback(async () => {
    const result = vegaResultRef.current;
    if (!result) {
      setSaveError("Chart not ready");
      return;
    }
    setSaveError(null);
    try {
      const url = await result.view.toImageURL("png", 2);
      const a = document.createElement("a");
      const safeName = (currentChart?.title ?? "chart")
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "");
      a.href = url;
      a.download = `${safeName || "chart"}.png`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "PNG export failed");
    }
  }, [currentChart?.title]);

  const handlePrev = useCallback(() => {
    setCurrentIndex((i) => Math.max(0, i - 1));
  }, []);

  const handleNext = useCallback(() => {
    setCurrentIndex((i) => Math.min(charts.length - 1, i + 1));
  }, [charts.length]);

  if (!currentChart) return null;

  const canPrev = currentIndex > 0;
  const canNext = currentIndex < charts.length - 1;

  return (
    <div
      data-testid="chart-gallery"
      role="dialog"
      aria-modal="true"
      aria-label="Chart gallery"
      style={overlayStyle}
      onClick={onClose}
    >
      <div
        data-testid="chart-gallery-card"
        style={cardStyle}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header row: title + close */}
        <button
          data-testid="chart-gallery-close"
          aria-label="Close chart gallery"
          onClick={onClose}
          style={{ ...closeBtnStyle, position: "absolute", top: 12, right: 12 }}
        >
          <IconClose size={18} />
        </button>
        <div style={headerRowStyle}>
          <h2
            data-testid="chart-gallery-title"
            style={titleStyle}
          >
            {currentChart.title}
          </h2>
        </div>

        {/* Chart embed area */}
        <div
          ref={chartAreaRef}
          data-testid="chart-gallery-embed-area"
          style={chartAreaStyle}
        >
          {embedError && (
            <div
              style={{
                color: "#f9c1c1",
                fontSize: 11,
                padding: 8,
                lineHeight: 1.5,
              }}
            >
              Chart render error: {embedError}
            </div>
          )}
        </div>

        {/* Caption */}
        <div
          data-testid="chart-gallery-caption"
          style={captionStyle}
        >
          {currentChart.caption ?? ""}
        </div>

        {/* Nav + save row */}
        <div style={navRowStyle}>
          <button
            data-testid="chart-gallery-prev"
            aria-label="Previous chart"
            onClick={handlePrev}
            disabled={!canPrev}
            style={canPrev ? navBtnStyle : navBtnDisabledStyle}
          >
            <IconChevronLeft size={14} />
            Prev
          </button>

          <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 4 }}>
            <span
              data-testid="chart-gallery-counter"
              style={counterStyle}
            >
              {currentIndex + 1} / {charts.length}
            </span>
            {saveError && (
              <span
                style={{ fontSize: 10, color: "#f9c1c1" }}
                data-testid="chart-gallery-save-error"
              >
                {saveError}
              </span>
            )}
            <button
              data-testid="chart-gallery-save-png"
              aria-label="Save chart as PNG"
              onClick={() => void handleSavePng()}
              style={saveBtnStyle}
            >
              Save as PNG
            </button>
          </div>

          <button
            data-testid="chart-gallery-next"
            aria-label="Next chart"
            onClick={handleNext}
            disabled={!canNext}
            style={canNext ? navBtnStyle : navBtnDisabledStyle}
          >
            Next
            <IconChevronRight size={14} />
          </button>
        </div>
      </div>
    </div>
  );
}
