// GRACE-2 web — CaseView (job-0143, sprint-12-mega Wave 4).
//
// Left-rail surface shown when a Case is active. Replaces the CasesPanel
// list view; renders:
//   1. Breadcrumb "← Cases / <Case Title>" — clicking the arrow returns
//      to the Cases-list view (deselects the active Case).
//   2. The LayerPanel below the breadcrumb so a user sees the layers
//      loaded for this Case directly.
//
// CasesPanel is hidden in this mode (no scrolled list underneath).

import { useEffect, useState } from "react";
import { IconArrowLeft } from "./icons";

export interface CaseViewProps {
  /** Title of the active Case (displayed in the breadcrumb). */
  caseTitle: string;
  /** Called when the user clicks the breadcrumb back-arrow. */
  onBack: () => void;
  /**
   * job-0284 — mobile presentation flag (the drawer passes true; the desktop
   * rail never sets it, so desktop renders byte-identical). On mobile the
   * "Cases" breadcrumb link IS the back affordance — the ← arrow button is
   * NOT rendered, leaving exactly ONE way back, labeled "Cases".
   */
  mobile?: boolean;
  /** Rendered below the breadcrumb — typically <LayerPanel> bound to the Case session. */
  children?: React.ReactNode;
}

const wrapStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 8,
  // 288 == LAYERS_WIDTH_DEFAULT_PX and the cases-list panel width — keeps the
  // desktop left rail the same width across cases-list <-> opened-case <->
  // Layers, so it does not visibly jump on Case open/close (job-0348).
  width: 288,
  fontFamily:
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
};

const breadcrumbStyle: React.CSSProperties = {
  background: "rgba(15,15,20,0.92)",
  border: "1px solid #333",
  borderRadius: 8,
  padding: "8px 10px",
  display: "flex",
  alignItems: "center",
  gap: 6,
  color: "#ddd",
  fontSize: 12,
  // Bound the row to its parent so a long case title can't push the card
  // wider than the rail / mobile sheet; the title span ellipsizes within it.
  // boxSizing so the 10px horizontal padding is INCLUDED in the 100% width
  // budget (else the padded card overshoots the rail and the title hard-clips
  // at the right edge — the recurring mid-glyph cutoff).
  minWidth: 0,
  maxWidth: "100%",
  boxSizing: "border-box",
  overflow: "hidden",
};

// The fixed leading controls (← arrow, "Cases" link, "/" separator) must NOT
// shrink: only the title flexes/ellipsizes. Without flexShrink:0 the browser
// can squeeze these intrinsic-width items first, which both mangles them AND
// (combined with min-width:auto quirks) lets the title overrun — the recurring
// breadcrumb cutoff. Locking them at flexShrink:0 makes the title the sole
// shrink target so text-overflow:ellipsis engages cleanly.
const backBtnStyle: React.CSSProperties = {
  background: "transparent",
  border: "none",
  color: "#7aa7ff",
  fontSize: 14,
  cursor: "pointer",
  padding: "2px 4px",
  borderRadius: 4,
  fontFamily: "inherit",
  lineHeight: 1,
  flexShrink: 0,
};

const linkStyle: React.CSSProperties = {
  background: "transparent",
  border: "none",
  color: "#7aa7ff",
  fontSize: 12,
  cursor: "pointer",
  padding: 0,
  fontFamily: "inherit",
  flexShrink: 0,
  whiteSpace: "nowrap",
};

const separatorStyle: React.CSSProperties = {
  color: "#666",
  flexShrink: 0,
};

const titleStyle: React.CSSProperties = {
  color: "#eee",
  fontWeight: 600,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  // flex-basis:0 (the "0" in "1 1 0") so the title's measured width does NOT
  // seed from its (long) content — it grows only from the leftover space after
  // the fixed leading controls. Pairing this with min-width:0 is what makes
  // text-overflow:ellipsis reliably engage instead of hard-clipping.
  flex: "1 1 0",
  // REQUIRED for text-overflow:ellipsis on a flex child — without it the flex
  // item's default min-width:auto refuses to shrink below content size, so a
  // long case title overflows the card and hard-clips with NO ellipsis
  // (the exact cutoff bug in the breadcrumb header). job-0350.
  minWidth: 0,
};

export function CaseView({
  caseTitle,
  onBack,
  mobile = false,
  children,
}: CaseViewProps): JSX.Element {
  // Esc-to-back so the user can navigate without grabbing the mouse.
  // Guarded by `mounted` so the listener only fires while CaseView is rendered.
  const [mounted, setMounted] = useState(true);
  useEffect(() => {
    setMounted(true);
    return () => setMounted(false);
  }, []);
  useEffect(() => {
    if (!mounted) return;
    function onKey(e: KeyboardEvent): void {
      if (e.key === "Escape") {
        // Only fire when there's no other modal in front of us (modals stop
        // propagation in their own handlers).
        onBack();
      }
    }
    // Escape rebound disabled per kickoff §1 — only the arrow click goes back.
    // (Esc-to-back risks interfering with chat input + Settings/Secrets
    // popups that already own Esc.) Keeping the effect guard so a future
    // re-enable lands here.
    void onKey;
  }, [mounted, onBack]);

  // NATE 2026-06-19: on MOBILE the fixed 288px wrap overflowed the narrow drawer
  // (min(320px,85vw) — only ~272px on a 320px phone), so the breadcrumb clipped
  // at the screen edge. Mobile fills its parent (100%, border-box, min-width:0)
  // so the breadcrumb's own maxWidth:100% bounds to the real drawer width and
  // the title ellipsizes. Desktop keeps the fixed 288px rail.
  const effectiveWrapStyle: React.CSSProperties = mobile
    ? {
        ...wrapStyle,
        width: "100%",
        maxWidth: "100%",
        minWidth: 0,
        boxSizing: "border-box",
      }
    : wrapStyle;

  return (
    <div data-testid="grace2-case-view" style={effectiveWrapStyle}>
      <div
        data-testid="grace2-case-view-breadcrumb"
        style={breadcrumbStyle}
        role="navigation"
        aria-label="Case navigation"
      >
        {/* job-0284 — mobile drops the ← arrow: the "Cases" link below is
            the SINGLE back affordance (user: "cases should be the back
            button, no need for another one"). Desktop keeps both. */}
        {!mobile && (
          <button
            data-testid="grace2-case-view-back"
            onClick={onBack}
            style={{ ...backBtnStyle, display: "flex", alignItems: "center" }}
            aria-label="Back to Cases"
            title="Back to Cases"
          >
            <IconArrowLeft size={16} />
          </button>
        )}
        <button
          data-testid="grace2-case-view-cases-link"
          onClick={onBack}
          style={linkStyle}
          aria-label="Back to Cases list"
        >
          Cases
        </button>
        <span style={separatorStyle}>/</span>
        <span
          data-testid="grace2-case-view-title"
          style={titleStyle}
          title={caseTitle}
        >
          {caseTitle}
        </span>
      </div>
      {children}
    </div>
  );
}
