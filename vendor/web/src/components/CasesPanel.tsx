// GRACE-2 web — CasesPanel (job-0137, sprint-12-mega Wave 3 — FR-MP-6).
//
// Left-rail panel of the user's Cases. Renders:
//   - A "+ New Case" button at the top.
//   - One row per CaseSummary: title, bbox indicator, primary_hazard chip,
//     updated_at relative timestamp.
//   - Per-row actions: select (click row), rename (pencil → inline edit),
//     archive, delete (with confirmation modal).
//   - Active-case highlight on the matching row.
//   - Friendly empty state when no Cases exist.
//
// Per-row actions emit through prop callbacks the parent wires into the
// useCases hook (which itself wraps GraceWs.sendCaseCommand). The panel
// itself owns ONLY local UI state (which row is in rename mode, which
// is the pending-delete target).
//
// Invariants:
//   - 1 (determinism boundary): no number computed here; we only render
//     received CaseSummary fields verbatim. The relative timestamp string
//     is a display-only formatting of `updated_at`.
//   - 8 (cancellation is first-class): no destructive action fires without
//     a clear cancel affordance (delete: confirmation modal; rename: Esc).
//   - 9 (no cost theater): no cost / quota / quote field anywhere.
//
// Memory rule "Confirmation before consequence": the delete row action
// opens a ConfirmationDialog before emitting `case-command(delete)`.

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { CaseSummary } from "../contracts";
import { ConfirmationDialog } from "./ConfirmationDialog";
import { ExportButton } from "./ExportButton";
import {
  IconKebab,
  IconRename,
  IconArchive,
  IconDelete,
  IconCheck,
  IconBbox,
  IconAdd,
} from "./icons";

export interface CasesPanelProps {
  /** Left-rail list from the useCases hook. */
  cases: CaseSummary[];
  /** Currently-active Case id, or null when no Case is open. */
  activeCaseId: string | null;
  /**
   * BUG 1 (late spinner). True while the FIRST case-list load is still in
   * flight (no list frame has settled yet). When true AND the rail is empty we
   * render a loading spinner IMMEDIATELY instead of the empty stub, so the user
   * never sees a momentary "no cases" flash that reads as frozen. The empty
   * stub shows ONLY when the list has settled to a genuine zero. Optional +
   * defaults to false so callers / tests that don't pass it behave exactly as
   * before (settled).
   */
  loading?: boolean;

  // Emitters (parent wires these to useCases / GraceWs).
  onCreate: () => void;
  onSelect: (caseId: string) => void;
  onRename: (caseId: string, newTitle: string) => void;
  onArchive: (caseId: string) => void;
  onDelete: (caseId: string) => void;
}

// --- Helpers ------------------------------------------------------------- //

/**
 * Human-friendly relative timestamp. Pure display formatting — no math the
 * caller cares about. Examples: "just now", "5m ago", "2h ago", "3d ago",
 * "Jun 4". `now` is injectable for testability.
 */
export function formatRelative(
  isoTs: string,
  now: Date = new Date(),
): string {
  const t = new Date(isoTs);
  if (Number.isNaN(t.getTime())) return "";
  const deltaMs = now.getTime() - t.getTime();
  if (deltaMs < 30_000) return "just now";
  const mins = Math.floor(deltaMs / 60_000);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  // Older than 7 days → date label.
  return t.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** Render the bbox compactly: "[-82.0, 26.5, -81.7, 26.8]" → "82.0°W 26.5°N…". */
export function formatBbox(
  bbox: [number, number, number, number] | null | undefined,
): string | null {
  if (!bbox || bbox.length !== 4) return null;
  const [minLon, minLat] = bbox;
  // Compact lon/lat at the SW corner only — fits in the 1-line meta strip.
  const lonStr = `${Math.abs(minLon).toFixed(1)}°${minLon < 0 ? "W" : "E"}`;
  const latStr = `${Math.abs(minLat).toFixed(1)}°${minLat < 0 ? "S" : "N"}`;
  return `${lonStr} ${latStr}`;
}

// --- Sub-components ------------------------------------------------------ //

interface CaseRowProps {
  c: CaseSummary;
  active: boolean;
  onSelect: () => void;
  onRenameSubmit: (next: string) => void;
  onArchive: () => void;
  onRequestDelete: () => void;
}

function CaseRow({
  c,
  active,
  onSelect,
  onRenameSubmit,
  onArchive,
  onRequestDelete,
}: CaseRowProps): JSX.Element {
  const [editing, setEditing] = useState(false);
  const [draftTitle, setDraftTitle] = useState(c.title);
  const [menuOpen, setMenuOpen] = useState(false);
  // cases-panel-layout (NATE 2026-06-20) - the kebab popover is PORTALED to
  // document.body (createPortal) and positioned `position:fixed` against the
  // kebab button's viewport rect, so it is never clipped by the
  // overflowY:auto/overflow:hidden of the grace2-cases-list (the bottom-row
  // menu was being cut off by that scroll clip on BOTH desktop + mobile). This
  // anchor rect is captured when the menu opens. Mirrors ConfirmationDialog's
  // createPortal(document.body) pattern.
  const [menuRect, setMenuRect] = useState<DOMRect | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const menuWrapRef = useRef<HTMLDivElement | null>(null);
  const kebabRef = useRef<HTMLButtonElement | null>(null);
  // The portaled menu surface lives OUTSIDE menuWrapRef's subtree (it is a
  // child of document.body), so the outside-click test must also treat a click
  // inside this node as "inside" or the menu would dismiss itself on its own
  // item clicks.
  const menuRef = useRef<HTMLDivElement | null>(null);
  const firstMenuItemRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!editing) setDraftTitle(c.title);
  }, [c.title, editing]);

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  // F57 — overflow menu: close on outside-click and Esc; move focus to the
  // first item when the menu opens (keyboard accessibility). The pointerdown
  // listener fires before click so a tap outside dismisses without also
  // selecting/deselecting the row.
  useEffect(() => {
    if (!menuOpen) return;
    firstMenuItemRef.current?.focus();
    function onPointerDown(ev: PointerEvent): void {
      const target = ev.target as Node;
      // The popover is portaled to document.body (outside menuWrapRef), so a
      // click inside EITHER the kebab wrapper OR the portaled menu node counts
      // as "inside" - otherwise tapping a menu item would dismiss the menu
      // before the item's own click handler runs.
      const insideWrap = menuWrapRef.current?.contains(target) ?? false;
      const insideMenu = menuRef.current?.contains(target) ?? false;
      if (!insideWrap && !insideMenu) {
        setMenuOpen(false);
      }
    }
    function onKey(ev: KeyboardEvent): void {
      if (ev.key === "Escape") {
        ev.preventDefault();
        ev.stopPropagation();
        setMenuOpen(false);
        kebabRef.current?.focus();
      }
    }
    // The fixed-positioned popover is anchored to the kebab's viewport rect;
    // if the page scrolls or resizes while it is open, re-measure so it stays
    // glued to the button (then close on scroll would also be acceptable, but
    // re-anchoring is less jarring for a short list scroll).
    function reanchor(): void {
      const r = kebabRef.current?.getBoundingClientRect();
      if (r) setMenuRect(r);
    }
    window.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", reanchor, true);
    window.addEventListener("scroll", reanchor, true);
    return () => {
      window.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("keydown", onKey, true);
      window.removeEventListener("resize", reanchor, true);
      window.removeEventListener("scroll", reanchor, true);
    };
  }, [menuOpen]);

  function startEdit(): void {
    setDraftTitle(c.title);
    setEditing(true);
  }
  function cancelEdit(): void {
    setEditing(false);
    setDraftTitle(c.title);
  }
  function commitEdit(): void {
    const trimmed = draftTitle.trim();
    if (trimmed.length === 0 || trimmed === c.title) {
      cancelEdit();
      return;
    }
    onRenameSubmit(trimmed);
    setEditing(false);
  }

  const bboxStr = formatBbox(c.bbox ?? null);

  return (
    <div
      data-testid="grace2-case-row"
      data-case-id={c.case_id}
      data-active={active ? "true" : "false"}
      style={{
        background: active ? "rgba(59,130,246,0.15)" : "rgba(20,20,25,0.65)",
        border: active ? "1px solid #3b82f6" : "1px solid #333",
        borderRadius: 6,
        // F83 — shorter rows so more Cases fit: trim vertical padding (8 → 6)
        // and the title↔meta gap (4 → 2). The "x hr ago" timestamp moved up to
        // the title row (right-aligned, before the kebab), so the lower meta row
        // no longer needs to reserve its own line of height for it.
        padding: "6px 8px",
        display: "flex",
        flexDirection: "column",
        gap: 2,
        cursor: editing ? "default" : "pointer",
      }}
      onClick={() => {
        if (!editing) onSelect();
      }}
      role="button"
      aria-pressed={active}
      aria-label={`Case ${c.title}`}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          // job-0330 — the row header must never overflow its column: cap it at
          // the row width and let the title (flex:1, min-width:0) ellipsis-
          // truncate so the kebab (flex-shrink:0) is always visible. Without
          // this, a long title pushed the kebab past the mobile drawer column's
          // overflow:hidden clip in portrait.
          minWidth: 0,
          width: "100%",
        }}
      >
        {editing ? (
          <input
            ref={inputRef}
            data-testid="grace2-case-row-rename-input"
            value={draftTitle}
            onChange={(e) => setDraftTitle(e.target.value)}
            onClick={(e) => e.stopPropagation()}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                commitEdit();
              } else if (e.key === "Escape") {
                e.preventDefault();
                cancelEdit();
              }
            }}
            onBlur={() => commitEdit()}
            style={{
              flex: 1,
              background: "#111",
              color: "#eee",
              border: "1px solid #555",
              borderRadius: 4,
              padding: "3px 6px",
              fontSize: 13,
              // job-0166 — form controls don't inherit font-family by default.
              fontFamily: "inherit",
            }}
          />
        ) : (
          <strong
            data-testid="grace2-case-row-title"
            style={{
              flex: 1,
              // job-0330 — a flex item defaults to min-width:auto, which refuses
              // to shrink below its intrinsic content width and so defeats the
              // ellipsis (the title would instead push the kebab out of the
              // clip). min-width:0 lets it shrink and the ellipsis engage.
              minWidth: 0,
              fontSize: 13,
              color: "#eee",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {c.title}
          </strong>
        )}
        {!editing && (
          // F83 — the "x hr ago" timestamp now lives INLINE on the title row,
          // right-aligned just before the kebab (was on the lower meta row,
          // which forced each row taller). flex-shrink:0 so it never collapses;
          // the title (flex:1, min-width:0) absorbs all slack via its ellipsis.
          <span
            data-testid="grace2-case-row-updated"
            style={{
              flexShrink: 0,
              fontSize: 10,
              color: "#999",
              whiteSpace: "nowrap",
            }}
          >
            {formatRelative(c.updated_at)}
          </span>
        )}
        {editing ? (
          // While renaming, the inline-edit mode owns the row — a single
          // commit affordance replaces the overflow menu.
          <button
            data-testid="grace2-case-row-rename-commit"
            aria-label={`Save name for ${c.title}`}
            title="Save"
            onClick={(e) => {
              e.stopPropagation();
              commitEdit();
            }}
            style={iconBtnStyle}
          >
            <IconCheck size={14} />
          </button>
        ) : (
          // NATE 2026-06-19: the per-row action cluster is JUST the F57 kebab
          // overflow menu now. Export moved INSIDE that popover (below) instead
          // of sitting as a standalone row icon. ExportButton still self-gates
          // (signed-in AND endpoint configured) so the menu item simply doesn't
          // appear when export is unavailable.
          <>
          {/* F57 - single kebab overflow button -> popover menu. */}
          <div
            ref={menuWrapRef}
            // job-0330 — the kebab must never shrink or be pushed off the row's
            // right edge (the bug: a long title clipped it under the mobile
            // drawer's overflow:hidden in portrait). flex-shrink:0 pins it; the
            // title (flex:1, min-width:0) absorbs all the slack via ellipsis.
            style={{
              position: "relative",
              display: "inline-flex",
              flexShrink: 0,
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <button
              ref={kebabRef}
              data-testid="grace2-case-row-menu-button"
              aria-label={`Actions for ${c.title}`}
              title="Actions"
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              onClick={(e) => {
                e.stopPropagation();
                // Capture the kebab's viewport rect on OPEN so the portaled,
                // fixed-position popover anchors to it (never clipped by the
                // cases-list scroll region). `menuOpen` is the current value in
                // this render - safe to read for the toggle direction.
                if (!menuOpen) {
                  setMenuRect(
                    kebabRef.current?.getBoundingClientRect() ?? null,
                  );
                }
                setMenuOpen((v) => !v);
              }}
              onKeyDown={(e) => {
                if (e.key === "ArrowDown" && !menuOpen) {
                  e.preventDefault();
                  setMenuRect(
                    kebabRef.current?.getBoundingClientRect() ?? null,
                  );
                  setMenuOpen(true);
                }
              }}
              style={iconBtnStyle}
            >
              <IconKebab size={16} />
            </button>
            {menuOpen &&
              createPortal(
                // cases-panel-layout (NATE 2026-06-20) - the popover is PORTALED
                // to document.body and `position:fixed`, anchored to the kebab's
                // viewport rect (menuRect). This lifts it OUT of the
                // grace2-cases-list overflow clip so the bottom-row menu is never
                // cut off (the reported clipping, desktop + mobile). It is
                // right-aligned to the kebab's right edge (the old
                // right:0/absolute behaviour) and opens downward; the
                // outside-click handler treats clicks inside this node (via
                // menuRef) as "inside". Mirrors ConfirmationDialog's
                // createPortal(document.body) pattern.
                <div
                  ref={menuRef}
                  data-testid="grace2-case-row-menu"
                  role="menu"
                  aria-label={`Actions for ${c.title}`}
                  // Don't let clicks inside the portaled menu bubble to the map
                  // / document (the menu lives at body root, so without this a
                  // bubbled click could be read as an outside-the-row click).
                  onClick={(e) => e.stopPropagation()}
                  style={fixedMenuStyle(menuRect)}
                >
                  <button
                    ref={firstMenuItemRef}
                    data-testid="grace2-case-row-menu-rename"
                    role="menuitem"
                    onClick={(e) => {
                      e.stopPropagation();
                      setMenuOpen(false);
                      startEdit();
                    }}
                    style={menuItemStyle()}
                  >
                    <IconRename size={14} />
                    <span>Rename</span>
                  </button>
                  {/* Export (NATE 2026-06-19) - in-menu item; self-gates on
                      signed-in + endpoint configured, runs in place. */}
                  <ExportButton
                    caseId={c.case_id}
                    asMenuItem
                    itemStyle={menuItemStyle()}
                  />
                  <button
                    data-testid="grace2-case-row-menu-archive"
                    role="menuitem"
                    onClick={(e) => {
                      e.stopPropagation();
                      setMenuOpen(false);
                      onArchive();
                    }}
                    style={menuItemStyle()}
                  >
                    <IconArchive size={14} />
                    <span>Archive</span>
                  </button>
                  <button
                    data-testid="grace2-case-row-menu-delete"
                    role="menuitem"
                    onClick={(e) => {
                      e.stopPropagation();
                      setMenuOpen(false);
                      onRequestDelete();
                    }}
                    style={menuItemStyle("#f87171")}
                  >
                    <IconDelete size={14} />
                    <span>Delete</span>
                  </button>
                </div>,
                document.body,
              )}
          </div>
          </>
        )}
      </div>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          fontSize: 10,
          color: "#999",
        }}
      >
        {c.primary_hazard && (
          <span
            data-testid="grace2-case-row-hazard"
            style={{
              background: "rgba(59,130,246,0.2)",
              border: "1px solid #3b82f6",
              borderRadius: 10,
              padding: "1px 6px",
              color: "#bdd",
              fontSize: 10,
            }}
          >
            {c.primary_hazard}
          </span>
        )}
        {bboxStr && (
          <span
            data-testid="grace2-case-row-bbox"
            title={`bbox: ${(c.bbox ?? []).join(", ")}`}
            style={{
              fontFamily: "monospace",
              display: "inline-flex",
              alignItems: "center",
              gap: 3,
            }}
          >
            <IconBbox size={11} />
            {bboxStr}
          </span>
        )}
      </div>
    </div>
  );
}

const iconBtnStyle: React.CSSProperties = {
  background: "transparent",
  border: "none",
  color: "#aaa",
  cursor: "pointer",
  fontSize: 12,
  padding: 2,
  width: 22,
  height: 22,
  // job-0330 — icon buttons (kebab, rename-commit) must hold their declared
  // size and never shrink, so the title's ellipsis (not the control) absorbs a
  // narrow row. Mirrors the flex-shrink:0 on the kebab wrapper.
  flexShrink: 0,
  borderRadius: 4,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  // job-0166 — buttons need explicit fontFamily so they don't fall back to UA serif.
  fontFamily: "inherit",
};

// F57 / cases-panel-layout - popover menu surface. PORTALED to document.body
// and `position:fixed`, anchored to the kebab button's VIEWPORT rect so it is
// never clipped by the grace2-cases-list overflow (the reported bottom-row
// clipping). Right-aligned to the kebab's right edge (preserves the old
// right:0 look) and opens downward just below the button. zIndex sits above
// the rails (z=20-22) but below the ConfirmationDialog backdrop (z=2000) so a
// delete-confirm still overlays it. A null rect (no measurement yet) falls
// back to the top-left so it is still reachable rather than off-screen.
const MENU_MIN_WIDTH = 132;
function fixedMenuStyle(rect: DOMRect | null): React.CSSProperties {
  const top = rect ? rect.bottom + 4 : 8;
  // Right-align the menu's right edge to the kebab's right edge: use `left`
  // computed as right-edge minus the menu min-width, clamped to >= 8px so it
  // never spills off the left of the viewport on a narrow column.
  const left = rect
    ? Math.max(8, rect.right - MENU_MIN_WIDTH)
    : 8;
  return {
    position: "fixed",
    top,
    left,
    minWidth: MENU_MIN_WIDTH,
    background: "rgba(28,28,34,0.98)",
    border: "1px solid rgba(255,255,255,0.12)",
    borderRadius: 8,
    padding: 4,
    display: "flex",
    flexDirection: "column",
    gap: 2,
    boxShadow: "0 6px 20px rgba(0,0,0,0.55)",
    zIndex: 1500,
  };
}

function menuItemStyle(color = "#ddd"): React.CSSProperties {
  return {
    background: "transparent",
    border: "none",
    color,
    cursor: "pointer",
    fontSize: 12,
    textAlign: "left",
    padding: "6px 8px",
    borderRadius: 5,
    display: "flex",
    alignItems: "center",
    gap: 8,
    width: "100%",
    // job-0166 — buttons need explicit fontFamily.
    fontFamily: "inherit",
  };
}

// --- CasesPanel --------------------------------------------------------- //

export function CasesPanel({
  cases,
  activeCaseId,
  loading = false,
  onCreate,
  onSelect,
  onRename,
  onArchive,
  onDelete,
}: CasesPanelProps): JSX.Element {
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);

  // job-0266 — the rail lists ACTIVE Cases only. Archived / deleted Cases
  // are EXCLUDED client-side (the server's case-list may still carry them;
  // the user saw a deleted Case linger in the rail). Sort: most-recently
  // updated first.
  const sortedCases = useMemo(() => {
    return cases
      .filter((c) => c.status === "active")
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  }, [cases]);

  const pendingCase = pendingDeleteId
    ? cases.find((c) => c.case_id === pendingDeleteId) ?? null
    : null;

  return (
    <div
      data-testid="grace2-cases-panel"
      role="region"
      aria-label="Cases"
      style={{
        background: "rgba(15,15,20,0.92)",
        border: "1px solid #333",
        borderRadius: 8,
        padding: 10,
        // job-0337 — fixed width: never content/viewport-driven. The desktop
        // left-rail override (global.css .grace2-desktop-rail) and the mobile
        // drawer override (global.css .grace2-mobile-touch) both pin this to a
        // FIXED width (280 desktop / 288 mobile == the LayerPanel column) so
        // the panel reads as part of one rail family and never grows with a
        // long Case title. box-sizing:border-box keeps the 10px padding inside
        // the declared width on every surface.
        width: 288,
        maxWidth: "100%",
        boxSizing: "border-box",
        color: "#eee",
        fontSize: 12,
        fontFamily:
          "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
        display: "flex",
        flexDirection: "column",
        // The panel is a flex column: header is fixed-height, list is the
        // scrollable region (flex:1 + minHeight:0 + overflowY:auto on the list
        // wrapper below). The panel FILLS its parent's bounded height (the
        // desktop rail wrapper / the mobile drawer hugger) rather than
        // asserting a viewport-sized maxHeight: calc(100vh - 24px). That old
        // cap ignored the drawer header/footer (and on desktop was looser than
        // the rail wrapper's own cap), so the panel sized to content and the
        // list's flex:1 never got squeezed below content -> overflowY:auto had
        // nothing to scroll. height:100% + minHeight:0 lets the list (flex:1)
        // be squeezed below content so its overflowY:auto actually engages on
        // BOTH paths. The parent now owns the height bound.
        height: "100%",
        minHeight: 0,
        // overflow:hidden (not auto): the panel itself must NOT scroll.
        // Only the inner grace2-cases-list div scrolls so the header stays
        // pinned at the top. Without this, overflow:auto on the outer div
        // scrolls the header too AND the flex children (no minHeight:0)
        // expand past the bound anyway instead of being clipped.
        overflow: "hidden",
      }}
    >
      <div
        // job-0284 — testid only so the mobile drawer scope (global.css) can
        // give this header its own floating-card surface; desktop rendering
        // is untouched (attribute carries no style).
        data-testid="grace2-cases-header"
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          // cases-spacing (NATE 2026-06-21) - the case ROWS sat 4px under the
          // "Cases" label and read cramped (the empty-stub got breathing room
          // on 2026-06-20 but the row list never did). Give the list real space
          // below the heading.
          marginBottom: 14,
          // flex-shrink:0 keeps the header at its natural height — the list
          // (flex:1) absorbs all remaining space.
          flexShrink: 0,
        }}
      >
        <strong
          data-testid="grace2-cases-header-label"
          // job-0337 — the section title reads as a heading, not body text.
          // Bumped 13 → 18 (was easily lost above the list); stays bold.
          style={{ fontSize: 18, fontWeight: 700, color: "#ddd" }}
        >
          Cases
        </strong>
        <button
          data-testid="grace2-cases-new"
          aria-label="Create a new Case"
          title="Create a new Case"
          onClick={onCreate}
          style={{
            background: "#3b82f6",
            color: "#fff",
            border: "none",
            borderRadius: 6,
            // F58 — icon-only; comfortable >=40px touch target.
            width: 40,
            height: 40,
            cursor: "pointer",
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            // job-0166 — buttons need explicit fontFamily.
            fontFamily: "inherit",
          }}
        >
          <IconAdd size={20} weight="bold" />
        </button>
      </div>

      {/* BUG 1 (late spinner) - while the FIRST case-list load is in flight and
          the rail is still empty, show a loading spinner IMMEDIATELY rather than
          the empty stub. This prevents the "no cases" flash (which read as a
          frozen list) before the list arrives. The empty stub renders ONLY when
          the list has settled (loading === false) to a genuine zero. The spinner
          reuses the global `grace2-spin` keyframe (App.tsx). */}
      {sortedCases.length === 0 && loading && (
        <div
          data-testid="grace2-cases-loading"
          role="status"
          aria-live="polite"
          style={{
            color: "#999",
            background: "rgba(255,255,255,0.03)",
            border: "1px solid #444",
            borderRadius: 6,
            padding: 12,
            textAlign: "center",
            lineHeight: 1.4,
            marginTop: 4,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 8,
          }}
        >
          <span
            aria-hidden="true"
            style={{
              width: 12,
              height: 12,
              borderRadius: "50%",
              border: "2px solid rgba(255,255,255,0.2)",
              borderTopColor: "#bbb",
              animation: "grace2-spin 0.8s linear infinite",
              flexShrink: 0,
            }}
          />
          <span>Loading cases...</span>
        </div>
      )}

      {sortedCases.length === 0 && !loading && (
        <div
          data-testid="grace2-cases-empty"
          style={{
            color: "#999",
            background: "rgba(255,255,255,0.03)",
            border: "1px dashed #444",
            borderRadius: 6,
            padding: 12,
            textAlign: "center",
            lineHeight: 1.4,
            // cases-panel-layout (NATE 2026-06-20) - nudge the empty stub down
            // a bit so it sits below the pinned "Cases" header rather than
            // hugging it. Reduced 16 -> 4 now the header owns the 14px gap
            // (cases-spacing NATE 2026-06-21) so empty + populated match.
            marginTop: 4,
          }}
        >
          Start a Case to save your work and chat history.
        </div>
      )}

      {/* Scroll container for the case rows.
          - flex:1 + minHeight:0: lets it take remaining panel height and
            shrink below its content height (without minHeight:0 a flex child
            refuses to shrink below its intrinsic content size, so the list
            would overflow the panel instead of scrolling).
          - overflowY:auto: the actual scroll surface for the row list.
          - maskImage / WebkitMaskImage: transparent gradient fade at the
            bottom (and a small top fade when scrolled) so the cutoff is clean
            rather than a hard clip. The TOP is now FULLY OPAQUE from 0px (no
            top fade band): the previous 20px top-fade dimmed the FIRST case at
            rest (NATE: "the gradient covers the first case"), so the first row
            now renders at full opacity. 32px bottom-fade stays (always
            present, signals more content below). transparent-to-opaque so only
            the bottom edge content fades - the rest renders at full opacity. */}
      <div
        data-testid="grace2-cases-list"
        style={{
          flex: 1,
          minHeight: 0,
          overflowY: "auto",
          display: "flex",
          flexDirection: "column",
          gap: 6,
          // Gradient fade mask: bottom edge always fades (signals more rows).
          // The TOP is FULLY OPAQUE from 0px so the FIRST case is never dimmed
          // by the fade (NATE: the old `transparent 0px -> black 20px` top band
          // covered the first row). black = fully opaque, transparent =
          // invisible. The mask maps the scroll viewport edges, not the
          // content - so it stays fixed as the user scrolls.
          WebkitMaskImage:
            "linear-gradient(to bottom, black 0px, black calc(100% - 32px), transparent 100%)",
          maskImage:
            "linear-gradient(to bottom, black 0px, black calc(100% - 32px), transparent 100%)",
          // Small bottom padding so the last row is visible above the fade.
          paddingBottom: 6,
        }}
      >
        {sortedCases.map((c) => (
          <CaseRow
            key={c.case_id}
            c={c}
            active={c.case_id === activeCaseId}
            onSelect={() => onSelect(c.case_id)}
            onRenameSubmit={(next) => onRename(c.case_id, next)}
            onArchive={() => onArchive(c.case_id)}
            onRequestDelete={() => setPendingDeleteId(c.case_id)}
          />
        ))}
      </div>

      {pendingCase && (
        <ConfirmationDialog
          testId="grace2-case-delete-dialog"
          title="Delete Case?"
          message={`This permanently removes "${pendingCase.title}" from your Cases list. Layers and chat history will no longer be recoverable from the left rail.`}
          confirmLabel="Delete"
          onConfirm={() => {
            onDelete(pendingCase.case_id);
            setPendingDeleteId(null);
          }}
          onCancel={() => setPendingDeleteId(null)}
        />
      )}
    </div>
  );
}
