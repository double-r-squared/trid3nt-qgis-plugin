// GRACE-2 web — CasesPanel + ConfirmationDialog + useCases tests
// (job-0137 + job-0143).
//
// Verifies:
//   1. CasesPanel renders the empty state when cases=[].
//   2. CasesPanel renders one row per CaseSummary with title + bbox + hazard
//      + relative timestamp.
//   3. Active-case highlight: only the row whose id matches activeCaseId
//      gets data-active="true".
//   4. "+ New Case" button calls onCreate.
//   5. Row click calls onSelect with the row's case_id.
//   6. Pencil → inline edit → Enter calls onRename with the new title.
//   7. Archive button calls onArchive with the row's case_id.
//   8. Delete button opens ConfirmationDialog; Confirm calls onDelete; Cancel
//      does NOT call onDelete.
//   9. ConfirmationDialog: Esc cancels; backdrop click cancels.
//      (job-0143: PersistenceChip removed — auth state lives in Settings.)
//  11. useCases: createCase emits case-command(create) with optional title arg.
//  12. useCases: selectCase emits case-command(select, case_id).
//  13. useCases: renameCase emits case-command(rename, case_id, {title}).
//  14. useCases: deleteCase emits case-command(delete, case_id).
//  15. useCases: onCaseList updates cases list and clears in-flight.
//  16. useCases: onCaseOpen with session_state hydrates activeCaseId +
//      activeSession; null clears them.
//  17. useCases: persistenceState transitions
//      anonymous → saved → saving → saved.
//  18. formatRelative pure function: "just now", "5m ago", "2h ago", "3d ago",
//      "Jun 4" (over a week).
//  19. formatBbox pure function: SW corner formatted with hemispheres.

import { describe, it, expect, vi, afterEach } from "vitest";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  act,
} from "@testing-library/react";
import { renderHook } from "@testing-library/react";
import {
  CasesPanel,
  formatRelative,
  formatBbox,
} from "./components/CasesPanel";
import { ConfirmationDialog } from "./components/ConfirmationDialog";
import { MobileDrawer } from "./components/MobileDrawer";
import { useCases } from "./hooks/useCases";
import type {
  CaseListEnvelopePayload,
  CaseOpenEnvelopePayload,
  CaseSessionState,
  CaseSummary,
} from "./contracts";

afterEach(() => cleanup());

// --- Fixtures ----------------------------------------------------------- //

const NOW = new Date("2026-06-08T12:00:00.000Z");

const CASE_FORT_MYERS: CaseSummary = {
  schema_version: "v1",
  case_id: "01ABCDEFGHJKMNPQRSTVWX0001",
  title: "Hurricane Ian — Fort Myers",
  created_at: "2026-06-05T10:00:00.000Z",
  updated_at: "2026-06-08T11:55:00.000Z",
  status: "active",
  bbox: [-82.0, 26.5, -81.7, 26.8],
  primary_hazard: "flood",
  layer_summary: ["layer-1", "layer-2"],
  qgs_project_uri: "gs://grace-2-hazard-prod-qgs/case-1.qgs",
};

const CASE_NORCAL_FIRE: CaseSummary = {
  schema_version: "v1",
  case_id: "01ABCDEFGHJKMNPQRSTVWX0002",
  title: "NorCal fire 2020",
  created_at: "2026-06-01T10:00:00.000Z",
  updated_at: "2026-06-07T10:00:00.000Z",
  status: "active",
  bbox: [-123.5, 38.0, -122.0, 39.5],
  primary_hazard: "wildfire",
  layer_summary: [],
  qgs_project_uri: null,
};

const CASE_OLD_ARCHIVE: CaseSummary = {
  schema_version: "v1",
  case_id: "01ABCDEFGHJKMNPQRSTVWX0003",
  title: "Old archive",
  created_at: "2026-05-01T10:00:00.000Z",
  updated_at: "2026-05-15T10:00:00.000Z",
  status: "archived",
};

// --- CasesPanel render tests ------------------------------------------- //

describe("CasesPanel", () => {
  it("renders the empty state when cases is empty", () => {
    render(
      <CasesPanel
        cases={[]}
        activeCaseId={null}
        onCreate={vi.fn()}
        onSelect={vi.fn()}
        onRename={vi.fn()}
        onArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    expect(screen.getByTestId("grace2-cases-empty")).toBeTruthy();
    expect(screen.getByTestId("grace2-cases-empty").textContent).toMatch(
      /Start a Case/i,
    );
  });

  // BUG 1 (late spinner): while the FIRST list load is in flight (loading) and
  // the rail is empty, the panel shows a loading spinner IMMEDIATELY - NOT the
  // "no cases" empty stub. The empty stub flashing before the list arrived read
  // as a frozen list (NATE: "the loading icon doesn't show up for a little bit").
  it("BUG1: shows the loading spinner (not the empty stub) while loading + empty", () => {
    render(
      <CasesPanel
        cases={[]}
        activeCaseId={null}
        loading
        onCreate={vi.fn()}
        onSelect={vi.fn()}
        onRename={vi.fn()}
        onArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    // Spinner present; empty stub ABSENT (no "no cases" flash while loading).
    expect(screen.getByTestId("grace2-cases-loading")).toBeTruthy();
    expect(screen.getByTestId("grace2-cases-loading").textContent).toMatch(
      /Loading cases/i,
    );
    expect(screen.queryByTestId("grace2-cases-empty")).toBeNull();
  });

  it("BUG1: shows the empty stub (not the spinner) once SETTLED to zero", () => {
    render(
      <CasesPanel
        cases={[]}
        activeCaseId={null}
        loading={false}
        onCreate={vi.fn()}
        onSelect={vi.fn()}
        onRename={vi.fn()}
        onArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    expect(screen.getByTestId("grace2-cases-empty")).toBeTruthy();
    expect(screen.queryByTestId("grace2-cases-loading")).toBeNull();
  });

  it("BUG1: a POPULATED rail never spins even while loading is true", () => {
    render(
      <CasesPanel
        cases={[CASE_FORT_MYERS]}
        activeCaseId={null}
        loading
        onCreate={vi.fn()}
        onSelect={vi.fn()}
        onRename={vi.fn()}
        onArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("grace2-cases-loading")).toBeNull();
    expect(screen.queryByTestId("grace2-cases-empty")).toBeNull();
    expect(screen.getAllByTestId("grace2-case-row").length).toBe(1);
  });

  it("renders the +New Case button and fires onCreate when clicked", () => {
    const onCreate = vi.fn();
    render(
      <CasesPanel
        cases={[]}
        activeCaseId={null}
        onCreate={onCreate}
        onSelect={vi.fn()}
        onRename={vi.fn()}
        onArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("grace2-cases-new"));
    expect(onCreate).toHaveBeenCalledTimes(1);
  });

  it("renders one row per case with title + hazard + bbox + updated", () => {
    render(
      <CasesPanel
        cases={[CASE_FORT_MYERS, CASE_NORCAL_FIRE]}
        activeCaseId={null}
        onCreate={vi.fn()}
        onSelect={vi.fn()}
        onRename={vi.fn()}
        onArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    const rows = screen.getAllByTestId("grace2-case-row");
    expect(rows).toHaveLength(2);
    // Titles
    const titles = screen.getAllByTestId("grace2-case-row-title");
    expect(titles.map((n) => n.textContent)).toEqual(
      expect.arrayContaining(["Hurricane Ian — Fort Myers", "NorCal fire 2020"]),
    );
    // Hazards
    const hazards = screen.getAllByTestId("grace2-case-row-hazard");
    expect(hazards.map((n) => n.textContent)).toEqual(
      expect.arrayContaining(["flood", "wildfire"]),
    );
    // Bbox indicator at least exists for Fort Myers row.
    const bbox = screen.getAllByTestId("grace2-case-row-bbox");
    expect(bbox.length).toBeGreaterThan(0);
  });

  it("highlights only the active-case row", () => {
    render(
      <CasesPanel
        cases={[CASE_FORT_MYERS, CASE_NORCAL_FIRE]}
        activeCaseId={CASE_FORT_MYERS.case_id}
        onCreate={vi.fn()}
        onSelect={vi.fn()}
        onRename={vi.fn()}
        onArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    const activeRows = screen.getAllByTestId("grace2-case-row").filter(
      (r) => r.getAttribute("data-active") === "true",
    );
    expect(activeRows).toHaveLength(1);
    expect(activeRows[0]!.getAttribute("data-case-id")).toBe(
      CASE_FORT_MYERS.case_id,
    );
  });

  it("clicking a row calls onSelect with that row's case_id", () => {
    const onSelect = vi.fn();
    render(
      <CasesPanel
        cases={[CASE_FORT_MYERS, CASE_NORCAL_FIRE]}
        activeCaseId={null}
        onCreate={vi.fn()}
        onSelect={onSelect}
        onRename={vi.fn()}
        onArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    const rows = screen.getAllByTestId("grace2-case-row");
    const norcalRow = rows.find(
      (r) => r.getAttribute("data-case-id") === CASE_NORCAL_FIRE.case_id,
    )!;
    fireEvent.click(norcalRow);
    expect(onSelect).toHaveBeenCalledWith(CASE_NORCAL_FIRE.case_id);
  });

  it("inline rename via kebab → Rename → Enter calls onRename with new title", () => {
    const onRename = vi.fn();
    render(
      <CasesPanel
        cases={[CASE_FORT_MYERS]}
        activeCaseId={null}
        onCreate={vi.fn()}
        onSelect={vi.fn()}
        onRename={onRename}
        onArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("grace2-case-row-menu-button"));
    fireEvent.click(screen.getByTestId("grace2-case-row-menu-rename"));
    const input = screen.getByTestId(
      "grace2-case-row-rename-input",
    ) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "Ian Lee County" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onRename).toHaveBeenCalledWith(
      CASE_FORT_MYERS.case_id,
      "Ian Lee County",
    );
  });

  it("inline rename commit via the check button calls onRename", () => {
    const onRename = vi.fn();
    render(
      <CasesPanel
        cases={[CASE_FORT_MYERS]}
        activeCaseId={null}
        onCreate={vi.fn()}
        onSelect={vi.fn()}
        onRename={onRename}
        onArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("grace2-case-row-menu-button"));
    fireEvent.click(screen.getByTestId("grace2-case-row-menu-rename"));
    const input = screen.getByTestId(
      "grace2-case-row-rename-input",
    ) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "Lee County" } });
    fireEvent.click(screen.getByTestId("grace2-case-row-rename-commit"));
    expect(onRename).toHaveBeenCalledWith(CASE_FORT_MYERS.case_id, "Lee County");
  });

  it("kebab → Archive calls onArchive with the row's case_id", () => {
    const onArchive = vi.fn();
    render(
      <CasesPanel
        cases={[CASE_FORT_MYERS]}
        activeCaseId={null}
        onCreate={vi.fn()}
        onSelect={vi.fn()}
        onRename={vi.fn()}
        onArchive={onArchive}
        onDelete={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("grace2-case-row-menu-button"));
    fireEvent.click(screen.getByTestId("grace2-case-row-menu-archive"));
    expect(onArchive).toHaveBeenCalledWith(CASE_FORT_MYERS.case_id);
  });

  it("kebab → Delete opens the confirmation dialog; Cancel does NOT fire onDelete", () => {
    const onDelete = vi.fn();
    render(
      <CasesPanel
        cases={[CASE_FORT_MYERS]}
        activeCaseId={null}
        onCreate={vi.fn()}
        onSelect={vi.fn()}
        onRename={vi.fn()}
        onArchive={vi.fn()}
        onDelete={onDelete}
      />,
    );
    expect(screen.queryByTestId("grace2-case-delete-dialog")).toBeNull();
    fireEvent.click(screen.getByTestId("grace2-case-row-menu-button"));
    fireEvent.click(screen.getByTestId("grace2-case-row-menu-delete"));
    expect(screen.getByTestId("grace2-case-delete-dialog")).toBeTruthy();
    fireEvent.click(screen.getByTestId("grace2-case-delete-dialog-cancel"));
    expect(onDelete).not.toHaveBeenCalled();
  });

  it("kebab → Delete → Confirm calls onDelete with the row's case_id", () => {
    const onDelete = vi.fn();
    render(
      <CasesPanel
        cases={[CASE_FORT_MYERS]}
        activeCaseId={null}
        onCreate={vi.fn()}
        onSelect={vi.fn()}
        onRename={vi.fn()}
        onArchive={vi.fn()}
        onDelete={onDelete}
      />,
    );
    fireEvent.click(screen.getByTestId("grace2-case-row-menu-button"));
    fireEvent.click(screen.getByTestId("grace2-case-row-menu-delete"));
    fireEvent.click(screen.getByTestId("grace2-case-delete-dialog-confirm"));
    expect(onDelete).toHaveBeenCalledWith(CASE_FORT_MYERS.case_id);
  });

  it("EXCLUDES archived/deleted cases from the rail (job-0266)", () => {
    const CASE_DELETED = {
      ...CASE_NORCAL_FIRE,
      case_id: "01ABCDEFGHJKMNPQRSTVWX0009",
      title: "Deleted case",
      status: "deleted" as const,
    };
    render(
      <CasesPanel
        cases={[CASE_OLD_ARCHIVE, CASE_FORT_MYERS, CASE_NORCAL_FIRE, CASE_DELETED]}
        activeCaseId={null}
        onCreate={vi.fn()}
        onSelect={vi.fn()}
        onRename={vi.fn()}
        onArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    const rows = screen.getAllByTestId("grace2-case-row");
    // Only the two ACTIVE cases render; archived + deleted are filtered out.
    expect(rows).toHaveLength(2);
    const ids = rows.map((r) => r.getAttribute("data-case-id"));
    expect(ids).not.toContain(CASE_OLD_ARCHIVE.case_id);
    expect(ids).not.toContain(CASE_DELETED.case_id);
    expect(ids).toContain(CASE_FORT_MYERS.case_id);
    expect(ids).toContain(CASE_NORCAL_FIRE.case_id);
  });

  // job-0322 F52 — CasesPanel is mounted as a child of MobileDrawer on mobile.
  // The drawer's tap-to-dismiss guard (e.target === e.currentTarget on the
  // column) must NOT swallow Case-row selection or the delete dialog: those
  // events have e.target on a CasesPanel descendant, so they reach their own
  // handlers and the drawer stays open until App explicitly closes it.
  describe("inside MobileDrawer (F52 tap-dismiss coexistence)", () => {
    it("selecting a row still calls onSelect (drawer onClose NOT triggered)", () => {
      const onSelect = vi.fn();
      const onClose = vi.fn();
      render(
        <MobileDrawer open={true} onClose={onClose}>
          <CasesPanel
            cases={[CASE_FORT_MYERS, CASE_NORCAL_FIRE]}
            activeCaseId={null}
            onCreate={vi.fn()}
            onSelect={onSelect}
            onRename={vi.fn()}
            onArchive={vi.fn()}
            onDelete={vi.fn()}
          />
        </MobileDrawer>,
      );
      const rows = screen.getAllByTestId("grace2-case-row");
      const norcalRow = rows.find(
        (r) => r.getAttribute("data-case-id") === CASE_NORCAL_FIRE.case_id,
      )!;
      fireEvent.click(norcalRow);
      expect(onSelect).toHaveBeenCalledWith(CASE_NORCAL_FIRE.case_id);
      // The drawer's column guard must NOT have fired onClose for a row tap.
      expect(onClose).not.toHaveBeenCalled();
    });

    it("opening + confirming the delete dialog still works inside the drawer", () => {
      const onDelete = vi.fn();
      const onClose = vi.fn();
      render(
        <MobileDrawer open={true} onClose={onClose}>
          <CasesPanel
            cases={[CASE_FORT_MYERS]}
            activeCaseId={null}
            onCreate={vi.fn()}
            onSelect={vi.fn()}
            onRename={vi.fn()}
            onArchive={vi.fn()}
            onDelete={onDelete}
          />
        </MobileDrawer>,
      );
      // Open the dialog via the kebab menu — the menu interaction bubbles to
      // the column but its e.target is a CasesPanel descendant, so the drawer
      // does not close.
      fireEvent.click(screen.getByTestId("grace2-case-row-menu-button"));
      fireEvent.click(screen.getByTestId("grace2-case-row-menu-delete"));
      expect(screen.getByTestId("grace2-case-delete-dialog")).toBeTruthy();
      expect(onClose).not.toHaveBeenCalled();
      // The dialog's own fixed backdrop cancel path must still work: clicking
      // the dialog BODY stops propagation, then Confirm fires onDelete.
      fireEvent.click(screen.getByTestId("grace2-case-delete-dialog"));
      expect(onClose).not.toHaveBeenCalled();
      fireEvent.click(screen.getByTestId("grace2-case-delete-dialog-confirm"));
      expect(onDelete).toHaveBeenCalledWith(CASE_FORT_MYERS.case_id);
      expect(onClose).not.toHaveBeenCalled();
    });

    it("a tap on the backdrop DOES close the drawer (backdrop owns close)", () => {
      // job-0329 — the F52 model changed: the old `target === currentTarget`
      // guard on the drawer COLUMN was replaced by the pointer-events
      // fall-through design. The transparent column is `pointerEvents: "none"`,
      // so empty-gutter taps pass THROUGH to the full-screen invisible backdrop
      // (z=40, onClick=onClose) which now owns dismiss. (We assert on the
      // backdrop directly because happy-dom does NOT honor `pointer-events:
      // none` for synthetic clicks, so a click dispatched at the column would
      // not realistically fall through in jsdom/happy-dom.)
      const onSelect = vi.fn();
      const onClose = vi.fn();
      render(
        <MobileDrawer open={true} onClose={onClose}>
          <CasesPanel
            cases={[CASE_FORT_MYERS]}
            activeCaseId={null}
            onCreate={vi.fn()}
            onSelect={onSelect}
            onRename={vi.fn()}
            onArchive={vi.fn()}
            onDelete={vi.fn()}
          />
        </MobileDrawer>,
      );

      // The backdrop is the close affordance now — clicking it fires onClose
      // and never touches CasesPanel handlers.
      fireEvent.click(screen.getByTestId("grace2-mobile-drawer-backdrop"));
      expect(onClose).toHaveBeenCalledTimes(1);
      expect(onSelect).not.toHaveBeenCalled();
    });

    it("encodes the pointer-events fall-through design (column none, backdrop owns onClick)", () => {
      // The NEW model relies on a specific pointer-events layout: the column is
      // click-transparent (so gutter taps reach the backdrop) and carries NO
      // onClick of its own; the backdrop is the only element with the close
      // handler. We assert that contract structurally so a regression that
      // re-adds an onClick to the column (or makes it `pointer-events: auto`)
      // is caught even though happy-dom can't simulate the fall-through.
      const onClose = vi.fn();
      render(
        <MobileDrawer open={true} onClose={onClose}>
          <CasesPanel
            cases={[CASE_FORT_MYERS]}
            activeCaseId={null}
            onCreate={vi.fn()}
            onSelect={vi.fn()}
            onRename={vi.fn()}
            onArchive={vi.fn()}
            onDelete={vi.fn()}
          />
        </MobileDrawer>,
      );

      const column = screen.getByTestId("grace2-mobile-drawer");
      const backdrop = screen.getByTestId("grace2-mobile-drawer-backdrop");

      // The column is click-transparent (pointer-events: none) so gutter taps
      // fall through to the backdrop. The backdrop sits BELOW the column on the
      // z-axis (z=40 vs z=41), which is only reachable because the column does
      // not intercept the click — encode both halves of that contract.
      expect(column.style.pointerEvents).toBe("none");
      expect(Number(backdrop.style.zIndex)).toBeLessThan(
        Number(column.style.zIndex),
      );

      // The column carries NO onClick of its own — a click dispatched directly
      // on it must NOT close the drawer (regression guard against re-adding the
      // old `target === currentTarget` column handler).
      fireEvent.click(column);
      expect(onClose).not.toHaveBeenCalled();

      // The backdrop is the sole close owner.
      fireEvent.click(backdrop);
      expect(onClose).toHaveBeenCalledTimes(1);
    });
  });

  it("sorts the rail most-recently-updated first (job-0266)", () => {
    render(
      <CasesPanel
        cases={[CASE_NORCAL_FIRE, CASE_FORT_MYERS]}
        activeCaseId={null}
        onCreate={vi.fn()}
        onSelect={vi.fn()}
        onRename={vi.fn()}
        onArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    const rows = screen.getAllByTestId("grace2-case-row");
    // Fort Myers (updated 2026-06-08) above NorCal (updated 2026-06-07).
    expect(rows[0]!.getAttribute("data-case-id")).toBe(CASE_FORT_MYERS.case_id);
    expect(rows[1]!.getAttribute("data-case-id")).toBe(CASE_NORCAL_FIRE.case_id);
  });

  // --- F57 kebab overflow menu ---------------------------------------- //
  describe("F57 kebab overflow menu", () => {
    function renderOneRow(overrides: Partial<Record<string, () => void>> = {}) {
      const handlers = {
        onCreate: vi.fn(),
        onSelect: vi.fn(),
        onRename: vi.fn(),
        onArchive: vi.fn(),
        onDelete: vi.fn(),
        ...overrides,
      };
      render(
        <CasesPanel
          cases={[CASE_FORT_MYERS]}
          activeCaseId={null}
          {...(handlers as Required<typeof handlers>)}
        />,
      );
      return handlers;
    }

    it("the kebab button opens the menu with Rename / Archive / Delete items", () => {
      renderOneRow();
      // Menu is closed initially.
      expect(screen.queryByTestId("grace2-case-row-menu")).toBeNull();
      const kebab = screen.getByTestId("grace2-case-row-menu-button");
      expect(kebab.getAttribute("aria-haspopup")).toBe("menu");
      expect(kebab.getAttribute("aria-expanded")).toBe("false");
      fireEvent.click(kebab);
      const menu = screen.getByTestId("grace2-case-row-menu");
      expect(menu.getAttribute("role")).toBe("menu");
      expect(kebab.getAttribute("aria-expanded")).toBe("true");
      expect(screen.getByTestId("grace2-case-row-menu-rename")).toBeTruthy();
      expect(screen.getByTestId("grace2-case-row-menu-archive")).toBeTruthy();
      expect(screen.getByTestId("grace2-case-row-menu-delete")).toBeTruthy();
      // All three items expose the proper menuitem role.
      expect(
        screen
          .getByTestId("grace2-case-row-menu-delete")
          .getAttribute("role"),
      ).toBe("menuitem");
    });

    it("opening / using the kebab does NOT select the row (stopPropagation)", () => {
      const onSelect = vi.fn();
      renderOneRow({ onSelect });
      fireEvent.click(screen.getByTestId("grace2-case-row-menu-button"));
      expect(onSelect).not.toHaveBeenCalled();
      fireEvent.click(screen.getByTestId("grace2-case-row-menu-archive"));
      // Archive ran, but the row was never selected.
      expect(onSelect).not.toHaveBeenCalled();
    });

    it("clicking outside the menu closes it (outside-click)", () => {
      renderOneRow();
      fireEvent.click(screen.getByTestId("grace2-case-row-menu-button"));
      expect(screen.getByTestId("grace2-case-row-menu")).toBeTruthy();
      // pointerdown on the panel region (outside the menu wrapper) dismisses.
      fireEvent.pointerDown(screen.getByTestId("grace2-cases-panel"));
      expect(screen.queryByTestId("grace2-case-row-menu")).toBeNull();
    });

    it("pressing Esc closes the menu", () => {
      renderOneRow();
      fireEvent.click(screen.getByTestId("grace2-case-row-menu-button"));
      expect(screen.getByTestId("grace2-case-row-menu")).toBeTruthy();
      fireEvent.keyDown(window, { key: "Escape" });
      expect(screen.queryByTestId("grace2-case-row-menu")).toBeNull();
    });

    it("selecting a menu item closes the menu", () => {
      renderOneRow();
      fireEvent.click(screen.getByTestId("grace2-case-row-menu-button"));
      fireEvent.click(screen.getByTestId("grace2-case-row-menu-archive"));
      expect(screen.queryByTestId("grace2-case-row-menu")).toBeNull();
    });
  });

  // --- cases-panel-layout (NATE 2026-06-20): kebab popover portals to body --
  // The bottom-row kebab menu was being CLIPPED by the grace2-cases-list
  // overflow (overflowY:auto on the list, overflow:hidden on the panel root +
  // wrapper). The fix renders the popover via createPortal(document.body) with
  // position:fixed anchored to the kebab's viewport rect (mirrors
  // ConfirmationDialog), so it floats above every scroll clip. The outside-
  // click / Esc dismiss must keep working with the portaled node included in
  // the inside test.
  describe("cases-panel-layout - kebab popover portals to body (no clip)", () => {
    function renderOneRow(overrides: Partial<Record<string, () => void>> = {}) {
      const handlers = {
        onCreate: vi.fn(),
        onSelect: vi.fn(),
        onRename: vi.fn(),
        onArchive: vi.fn(),
        onDelete: vi.fn(),
        ...overrides,
      };
      render(
        <CasesPanel
          cases={[CASE_FORT_MYERS]}
          activeCaseId={null}
          {...(handlers as Required<typeof handlers>)}
        />,
      );
      return handlers;
    }

    it("the open menu is portaled OUTSIDE the cases-list scroll container", () => {
      renderOneRow();
      fireEvent.click(screen.getByTestId("grace2-case-row-menu-button"));
      const menu = screen.getByTestId("grace2-case-row-menu");
      const list = screen.getByTestId("grace2-cases-list");
      const panel = screen.getByTestId("grace2-cases-panel");
      // The menu must NOT be nested inside the list (the overflow clip) nor the
      // panel root - it lives at document.body so it can never be cut off.
      expect(list.contains(menu)).toBe(false);
      expect(panel.contains(menu)).toBe(false);
      expect(document.body.contains(menu)).toBe(true);
    });

    it("the portaled menu uses position:fixed (escapes the scroll clip)", () => {
      renderOneRow();
      fireEvent.click(screen.getByTestId("grace2-case-row-menu-button"));
      const menu = screen.getByTestId("grace2-case-row-menu");
      // position:fixed anchors to the viewport, not the clipped scroll parent.
      expect(menu.style.position).toBe("fixed");
    });

    it("an outside click (on the panel) still dismisses the portaled menu", () => {
      renderOneRow();
      fireEvent.click(screen.getByTestId("grace2-case-row-menu-button"));
      expect(screen.getByTestId("grace2-case-row-menu")).toBeTruthy();
      // The panel is outside both the kebab wrapper AND the portaled menu node,
      // so a pointerdown there dismisses.
      fireEvent.pointerDown(screen.getByTestId("grace2-cases-panel"));
      expect(screen.queryByTestId("grace2-case-row-menu")).toBeNull();
    });

    it("a click on a portaled menu ITEM does NOT self-dismiss before it runs", () => {
      // Regression guard: the portaled menu lives outside menuWrapRef, so the
      // outside-click test must treat clicks inside the portaled node (menuRef)
      // as "inside" - otherwise the menu would close before the item handler
      // fires. We assert the item still triggers its action (Archive).
      const onArchive = vi.fn();
      renderOneRow({ onArchive });
      fireEvent.click(screen.getByTestId("grace2-case-row-menu-button"));
      // pointerdown on the item (capture-phase outside-click listener) then the
      // click handler - the item must run and the menu closes AFTER acting.
      const item = screen.getByTestId("grace2-case-row-menu-archive");
      fireEvent.pointerDown(item);
      fireEvent.click(item);
      expect(onArchive).toHaveBeenCalledWith(CASE_FORT_MYERS.case_id);
      expect(screen.queryByTestId("grace2-case-row-menu")).toBeNull();
    });

    it("Esc still dismisses the portaled menu", () => {
      renderOneRow();
      fireEvent.click(screen.getByTestId("grace2-case-row-menu-button"));
      expect(screen.getByTestId("grace2-case-row-menu")).toBeTruthy();
      fireEvent.keyDown(window, { key: "Escape" });
      expect(screen.queryByTestId("grace2-case-row-menu")).toBeNull();
    });

    it("opening Delete from the portaled menu still gates the confirmation dialog", () => {
      // The full delete flow must survive the portal change: kebab -> Delete ->
      // ConfirmationDialog -> Confirm fires onDelete.
      const onDelete = vi.fn();
      renderOneRow({ onDelete });
      fireEvent.click(screen.getByTestId("grace2-case-row-menu-button"));
      fireEvent.click(screen.getByTestId("grace2-case-row-menu-delete"));
      expect(screen.getByTestId("grace2-case-delete-dialog")).toBeTruthy();
      fireEvent.click(screen.getByTestId("grace2-case-delete-dialog-confirm"));
      expect(onDelete).toHaveBeenCalledWith(CASE_FORT_MYERS.case_id);
    });
  });

  // --- cases-panel-layout (NATE 2026-06-20): empty stub nudged down --------
  describe("cases-panel-layout - empty stub nudged down", () => {
    it("the 'Start a Case' stub has a top margin (sits below the header)", () => {
      render(
        <CasesPanel
          cases={[]}
          activeCaseId={null}
          onCreate={vi.fn()}
          onSelect={vi.fn()}
          onRename={vi.fn()}
          onArchive={vi.fn()}
          onDelete={vi.fn()}
        />,
      );
      const stub = screen.getByTestId("grace2-cases-empty");
      // Nudged down a bit (NATE) - a non-zero top margin separates it from the
      // pinned "Cases" header.
      expect(parseInt(stub.style.marginTop, 10)).toBeGreaterThan(0);
      expect(stub.textContent).toMatch(/Start a Case/i);
    });
  });

  // --- job-0330 mobile portrait clip fix ------------------------------- //
  // The mobile-drawer column is min(320px,85vw) with overflow:hidden. The bug:
  // a fixed-width / shrink-wrapped panel let a long Case title (whiteSpace:
  // nowrap, flex:1) expand the row past the column width, pushing the kebab
  // under the clip in PORTRAIT (landscape's wider 85vw masked it). The fix
  // makes the title ellipsis-truncate (flex:1 + min-width:0) and pins the kebab
  // (flex-shrink:0) so it is always visible inside the row.
  describe("job-0330 — row never clips the kebab (mobile portrait)", () => {
    const LONG_TITLE_CASE: CaseSummary = {
      ...CASE_FORT_MYERS,
      case_id: "01ABCDEFGHJKMNPQRSTVWX00AA",
      title:
        "Hurricane Ian catastrophic storm-surge inundation across Lee County and the barrier islands",
    };

    function renderRow(c: CaseSummary = LONG_TITLE_CASE) {
      render(
        <CasesPanel
          cases={[c]}
          activeCaseId={null}
          onCreate={vi.fn()}
          onSelect={vi.fn()}
          onRename={vi.fn()}
          onArchive={vi.fn()}
          onDelete={vi.fn()}
        />,
      );
    }

    it("the title ellipsis-truncates (flex:1 + min-width:0, not nowrap overflow)", () => {
      renderRow();
      const title = screen.getByTestId("grace2-case-row-title");
      // flex:1 lets it grow/shrink; min-width:0 is the load-bearing bit — a
      // flex item defaults to min-width:auto and refuses to shrink below its
      // content width, which defeats the ellipsis and pushes the kebab out.
      expect(title.style.minWidth).toBe("0");
      expect(title.style.flex).toMatch(/^1\b/);
      expect(title.style.overflow).toBe("hidden");
      expect(title.style.textOverflow).toBe("ellipsis");
      expect(title.style.whiteSpace).toBe("nowrap");
    });

    it("the row header is width-capped (100% + min-width:0) so it can't overflow the column", () => {
      renderRow();
      // The header is the flex container holding [title | kebab]. It must be
      // capped at the row width with min-width:0 so the title's ellipsis
      // engages instead of the row growing past the drawer's overflow:hidden.
      const title = screen.getByTestId("grace2-case-row-title");
      const header = title.parentElement as HTMLElement;
      expect(header.style.display).toBe("flex");
      expect(header.style.width).toBe("100%");
      expect(header.style.minWidth).toBe("0");
    });

    it("the kebab wrapper is flex-shrink:0 (pinned, never pushed off the clip)", () => {
      renderRow();
      const kebab = screen.getByTestId("grace2-case-row-menu-button");
      const wrapper = kebab.parentElement as HTMLElement;
      expect(wrapper.style.flexShrink).toBe("0");
      // It still anchors its popover (position:relative) under the button.
      expect(wrapper.style.position).toBe("relative");
    });

    it("the kebab stays in the DOM and openable even with a very long title", () => {
      renderRow();
      // The kebab is present (not clipped out of existence) and still opens its
      // menu — the actual user-facing guarantee.
      const kebab = screen.getByTestId("grace2-case-row-menu-button");
      expect(kebab).toBeTruthy();
      fireEvent.click(kebab);
      expect(screen.getByTestId("grace2-case-row-menu")).toBeTruthy();
    });
  });

  // job-0330 — inside the mobile drawer the CasesPanel must fill the column
  // (full width), NOT shrink-wrap. A `fit-content` hugger let the row expand to
  // the (nowrap) title's intrinsic width and clip the kebab. We assert the
  // panel renders at the drawer column's full width here.
  describe("job-0330 — CasesPanel fills the mobile drawer column", () => {
    it("the panel gets the mobile-touch scope (width override) inside the drawer", () => {
      render(
        <MobileDrawer open={true} onClose={vi.fn()}>
          {/* mirror App.tsx's full-width hugger (NOT fit-content) */}
          <div style={{ width: "100%", pointerEvents: "auto" }}>
            <CasesPanel
              cases={[CASE_FORT_MYERS]}
              activeCaseId={null}
              onCreate={vi.fn()}
              onSelect={vi.fn()}
              onRename={vi.fn()}
              onArchive={vi.fn()}
              onDelete={vi.fn()}
            />
          </div>
        </MobileDrawer>,
      );
      const panel = screen.getByTestId("grace2-cases-panel");
      const hugger = panel.parentElement as HTMLElement;
      // The hugger must be full-width (NOT fit-content): a shrink-wrap hugger
      // would let a long (nowrap) Case title widen the row past the column.
      // job-0337: the panel itself is now a FIXED 288px (global.css
      // .grace2-mobile-touch override) rather than width:auto — see the
      // job-0337 describe block below for the fixed-width contract.
      expect(hugger.style.width).toBe("100%");
      // The drawer applies the touch scope whose CSS pins the panel width —
      // assert the panel rides inside that scope.
      const drawer = screen.getByTestId("grace2-mobile-drawer");
      expect(drawer.className).toContain("grace2-mobile-touch");
      expect(drawer.contains(panel)).toBe(true);
    });
  });

  // --- job-0337 — fixed Cases-panel width (== LayerPanel) + larger header --
  // job-0335 set the panel root to width:100% but global.css still forced
  // width:auto !important inside the mobile drawer, so the panel sized to
  // content / varied with viewport (the "dynamically sized + cuts off" report).
  // The fix pins the panel to a FIXED 288px (== LAYERS_WIDTH_DEFAULT_PX, the
  // LayerPanel mobile column width) on every surface so it never grows with a
  // long title nor varies with the viewport; box-sizing:border-box + max-width
  // keep it inside narrow drawer columns. The "Cases" header is also enlarged
  // so it reads as a section title.
  describe("job-0337 — fixed width + larger header", () => {
    const LONG_TITLE_CASE: CaseSummary = {
      ...CASE_FORT_MYERS,
      title:
        "Hurricane Ian catastrophic storm-surge inundation across Lee County and the barrier islands and the gulf shoreline",
    };

    function renderPanel(c: CaseSummary = CASE_FORT_MYERS) {
      render(
        <CasesPanel
          cases={[c]}
          activeCaseId={null}
          onCreate={vi.fn()}
          onSelect={vi.fn()}
          onRename={vi.fn()}
          onArchive={vi.fn()}
          onDelete={vi.fn()}
        />,
      );
    }

    it("the panel root declares a FIXED 288px width (== LayerPanel column), box-sized", () => {
      // The inline root width is the fixed, non-content/non-viewport-driven
      // base. FIX 1 (NATE 2026-06-17): the desktop-rail override AND the
      // mobile-touch override now BOTH pin it to 288px — the LayerPanel
      // LAYERS_WIDTH_DEFAULT_PX — so the Cases panel reads as the same width as
      // the Layers panel on every surface (was 280 desktop, an 8px mismatch
      // that clipped a long title sooner). The inline base is what guarantees
      // it is never `auto`/fit-content when no scope class is present.
      renderPanel();
      const panel = screen.getByTestId("grace2-cases-panel");
      expect(panel.style.width).toBe("288px");
      expect(panel.style.maxWidth).toBe("100%");
      expect(panel.style.boxSizing).toBe("border-box");
    });

    it("FIX 1 — the inline base width matches the Layers panel default (parity)", () => {
      // The Layers panel's desktop default is LAYERS_WIDTH_DEFAULT_PX = 288px
      // (LayerPanel.tsx). The Cases panel inline base must be the SAME literal
      // so the two rails are one width family (the global.css desktop-rail +
      // mobile-touch overrides were aligned to 288 to match).
      renderPanel();
      const panel = screen.getByTestId("grace2-cases-panel");
      expect(panel.style.width).toBe("288px"); // == LAYERS_WIDTH_DEFAULT_PX
    });

    it("the fixed width does NOT grow with a very long Case title", () => {
      // Same fixed 288px regardless of title length — the panel must never
      // shrink-wrap or expand to content. The long title is absorbed by the
      // row title's ellipsis (asserted in the job-0330 block above).
      renderPanel(LONG_TITLE_CASE);
      const panel = screen.getByTestId("grace2-cases-panel");
      expect(panel.style.width).toBe("288px");
    });

    it("the 'Cases' header label uses a larger, bold section-title font", () => {
      renderPanel();
      const header = screen.getByTestId("grace2-cases-header-label");
      expect(header.textContent).toBe("Cases");
      // Larger than the previous 13px body size so it reads as a heading.
      expect(parseInt(header.style.fontSize, 10)).toBeGreaterThanOrEqual(17);
      // Still bold.
      expect(header.style.fontWeight).toBe("700");
    });

    it("a long title still ellipsis-truncates within the fixed-width panel", () => {
      // Re-assert the title contract holds alongside the fixed width (so a
      // future width change can't silently drop the ellipsis path).
      renderPanel(LONG_TITLE_CASE);
      const title = screen.getByTestId("grace2-case-row-title");
      expect(title.style.minWidth).toBe("0");
      expect(title.style.flex).toMatch(/^1\b/);
      expect(title.style.overflow).toBe("hidden");
      expect(title.style.textOverflow).toBe("ellipsis");
      expect(title.style.whiteSpace).toBe("nowrap");
      // And the kebab is still pinned (flex-shrink:0) → never pushed out.
      const kebab = screen.getByTestId("grace2-case-row-menu-button");
      const wrapper = kebab.parentElement as HTMLElement;
      expect(wrapper.style.flexShrink).toBe("0");
    });
  });

  // --- F83 — shorter rows: timestamp inline on the title row ----------- //
  // The "x hr ago" updated_at timestamp moved from the lower meta row UP to the
  // title row (right-aligned, just before the kebab). Removing it from the meta
  // row + trimming the row padding/gap makes each row shorter so more Cases fit.
  describe("F83 — inline timestamp on the title row (shorter rows)", () => {
    function renderRow(c: CaseSummary = CASE_FORT_MYERS) {
      render(
        <CasesPanel
          cases={[c]}
          activeCaseId={null}
          onCreate={vi.fn()}
          onSelect={vi.fn()}
          onRename={vi.fn()}
          onArchive={vi.fn()}
          onDelete={vi.fn()}
        />,
      );
    }

    it("renders exactly one updated-at timestamp per row", () => {
      renderRow();
      const stamps = screen.getAllByTestId("grace2-case-row-updated");
      expect(stamps).toHaveLength(1);
      // It still shows the relative-time text (formatRelative output).
      expect(stamps[0]!.textContent).toBeTruthy();
    });

    it("the timestamp lives on the TITLE row (same flex container as the title + kebab)", () => {
      renderRow();
      const stamp = screen.getByTestId("grace2-case-row-updated");
      const title = screen.getByTestId("grace2-case-row-title");
      const kebab = screen.getByTestId("grace2-case-row-menu-button");
      // Title row = the title's parent flex container. The timestamp must be a
      // direct child of that SAME container (was previously on the meta row).
      const titleRow = title.parentElement as HTMLElement;
      expect(stamp.parentElement).toBe(titleRow);
      // The kebab wrapper is also in the title row — confirms all three
      // (title | timestamp | kebab) share one row.
      const kebabWrapper = kebab.parentElement as HTMLElement;
      expect(kebabWrapper.parentElement).toBe(titleRow);
    });

    it("the timestamp is NOT on the meta row (hazard + bbox strip)", () => {
      renderRow();
      const stamp = screen.getByTestId("grace2-case-row-updated");
      const hazard = screen.getByTestId("grace2-case-row-hazard");
      const metaRow = hazard.parentElement as HTMLElement;
      // The hazard/bbox meta row must no longer carry the timestamp.
      expect(metaRow.contains(stamp)).toBe(false);
    });

    it("the inline timestamp is shrink-pinned + nowrap (never collapses the title's ellipsis)", () => {
      renderRow();
      const stamp = screen.getByTestId("grace2-case-row-updated");
      // flex-shrink:0 keeps it from collapsing; the title (flex:1, min-width:0)
      // absorbs slack via ellipsis so a long title can't push the timestamp out.
      expect(stamp.style.flexShrink).toBe("0");
      expect(stamp.style.whiteSpace).toBe("nowrap");
    });

    it("the row uses tightened vertical padding + gap (shorter rows)", () => {
      renderRow();
      const row = screen.getByTestId("grace2-case-row");
      // F83 trimmed padding 8 → 6px vertical and the column gap 4 → 2px so each
      // row is shorter and more Cases fit in the list.
      // padding shorthand "6px 8px" → top/bottom padding is 6px.
      expect(row.style.paddingTop).toBe("6px");
      expect(row.style.paddingBottom).toBe("6px");
      expect(parseInt(row.style.gap, 10)).toBeLessThanOrEqual(2);
    });

    it("the long title still ellipsis-truncates with the inline timestamp present", () => {
      const LONG: CaseSummary = {
        ...CASE_FORT_MYERS,
        title:
          "Hurricane Ian catastrophic storm-surge inundation across Lee County and the barrier islands",
      };
      renderRow(LONG);
      const title = screen.getByTestId("grace2-case-row-title");
      expect(title.style.minWidth).toBe("0");
      expect(title.style.flex).toMatch(/^1\b/);
      expect(title.style.overflow).toBe("hidden");
      expect(title.style.textOverflow).toBe("ellipsis");
      expect(title.style.whiteSpace).toBe("nowrap");
      // Timestamp still present and the kebab still openable.
      expect(screen.getByTestId("grace2-case-row-updated")).toBeTruthy();
      const kebab = screen.getByTestId("grace2-case-row-menu-button");
      fireEvent.click(kebab);
      expect(screen.getByTestId("grace2-case-row-menu")).toBeTruthy();
    });

    it("renaming hides the inline timestamp (edit mode owns the title row)", () => {
      renderRow();
      // Enter rename mode via the kebab → Rename.
      fireEvent.click(screen.getByTestId("grace2-case-row-menu-button"));
      fireEvent.click(screen.getByTestId("grace2-case-row-menu-rename"));
      // The input replaces the title; the timestamp yields the row to the
      // single commit affordance while editing.
      expect(screen.getByTestId("grace2-case-row-rename-input")).toBeTruthy();
      expect(screen.queryByTestId("grace2-case-row-updated")).toBeNull();
    });
  });

  it("the +New Case button is icon-only (no text label) but keeps its aria-label", () => {
    render(
      <CasesPanel
        cases={[]}
        activeCaseId={null}
        onCreate={vi.fn()}
        onSelect={vi.fn()}
        onRename={vi.fn()}
        onArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    const newBtn = screen.getByTestId("grace2-cases-new");
    expect(newBtn.getAttribute("aria-label")).toBe("Create a new Case");
    // No visible text content — the plus icon is the only child (an SVG).
    expect(newBtn.textContent).toBe("");
    expect(newBtn.querySelector("svg")).not.toBeNull();
  });

  // --- scrollable list (mobile + desktop fix) --------------------------------
  // Root cause: grace2-cases-list had no flex:1 / minHeight:0 / overflowY, so
  // flex children expanded past the panel's maxHeight instead of scrolling.
  // Fix: list div is the scroll container (flex:1 + minHeight:0 + overflowY:
  // auto); panel root clips via overflow:hidden; header is flex-shrink:0 so it
  // stays pinned. A gradient mask (maskImage / WebkitMaskImage) on the list
  // fades the top+bottom edges instead of hard-clipping.
  describe("scrollable list + gradient fade mask", () => {
    function renderPanel(caseCount = 2) {
      const cases: CaseSummary[] = Array.from({ length: caseCount }, (_, i) => ({
        schema_version: "v1" as const,
        case_id: `01ABCDEFGHJKMNPQRSTVWX00${String(i).padStart(2, "0")}`,
        title: `Case ${i}`,
        created_at: "2026-06-01T00:00:00.000Z",
        updated_at: `2026-06-0${Math.max(1, i + 1)}T00:00:00.000Z`,
        status: "active" as const,
      }));
      render(
        <CasesPanel
          cases={cases}
          activeCaseId={null}
          onCreate={vi.fn()}
          onSelect={vi.fn()}
          onRename={vi.fn()}
          onArchive={vi.fn()}
          onDelete={vi.fn()}
        />,
      );
    }

    it("the panel root does NOT itself scroll (overflow:hidden, not auto)", () => {
      // The panel root must not be the scroll container — if it were, the
      // header would scroll away with the list. Only the inner list div scrolls.
      renderPanel();
      const panel = screen.getByTestId("grace2-cases-panel");
      expect(panel.style.overflow).toBe("hidden");
    });

    it("the panel root FILLS its parent's bound (height:100% + minHeight:0), NOT a 100vh maxHeight", () => {
      // Root-cause fix: the root previously asserted maxHeight: calc(100vh -
      // 24px), which sized the panel to content (capped at ~full viewport)
      // and ignored the drawer header/footer, so the inner list's flex:1 was
      // never squeezed below content and overflowY:auto had nothing to scroll.
      // The panel now FILLS its parent's bounded height (the desktop rail
      // wrapper / the mobile drawer hugger) via height:100% + minHeight:0 so
      // the list (flex:1) can be squeezed and actually scroll on both paths.
      renderPanel();
      const panel = screen.getByTestId("grace2-cases-panel");
      expect(panel.style.height).toBe("100%");
      expect(panel.style.minHeight).toBe("0");
      // The old viewport-sized cap must be gone — it is the wrong reference
      // (ignores the drawer header/footer) and re-adding it would reintroduce
      // the content-sized panel that never lets the list scroll.
      expect(panel.style.maxHeight).toBe("");
    });

    it("the list div is the scroll container (overflowY:auto)", () => {
      // The list must carry overflowY:auto so rows scroll rather than overflow.
      renderPanel();
      const list = screen.getByTestId("grace2-cases-list");
      expect(list.style.overflowY).toBe("auto");
    });

    it("the list div is flex:1 + minHeight:0 (can shrink below its content height)", () => {
      // flex:1 takes remaining panel space after the header.
      // minHeight:0 is load-bearing: without it a flex child defaults to
      // min-height:auto and refuses to shrink below its content height, so
      // it overflows the panel instead of scrolling.
      renderPanel();
      const list = screen.getByTestId("grace2-cases-list");
      expect(list.style.flex).toMatch(/^1\b/);
      expect(list.style.minHeight).toBe("0");
    });

    it("the list div carries a gradient mask image (transparent fade at edges)", () => {
      // The mask fades the top+bottom edges of the scroll region so the
      // cutoff is visually clean rather than a hard clip.
      renderPanel();
      const list = screen.getByTestId("grace2-cases-list");
      // Either the standard or webkit-prefixed property must be set.
      const hasMask =
        list.style.maskImage !== "" ||
        list.style.webkitMaskImage !== "" ||
        (list.style as CSSStyleDeclaration & Record<string, string>)[
          "WebkitMaskImage"
        ] !== "" ||
        (list.style as CSSStyleDeclaration & Record<string, string>)[
          "-webkit-mask-image"
        ] !== "";
      expect(hasMask).toBe(true);
    });

    it("the mask top is FULLY OPAQUE from 0px (first case not dimmed by the top fade)", () => {
      // GRADIENT NUDGE (NATE): the old top-fade band (`transparent 0px ->
      // black 20px`) dimmed the FIRST case row. The mask now starts opaque at
      // 0px so the first case renders at full opacity; only the bottom fades.
      renderPanel();
      const list = screen.getByTestId("grace2-cases-list");
      const mask =
        list.style.maskImage ||
        list.style.webkitMaskImage ||
        (list.style as CSSStyleDeclaration & Record<string, string>)[
          "WebkitMaskImage"
        ] ||
        "";
      // No leading transparent->black top band (the band that covered case #1).
      expect(mask).not.toContain("transparent 0px");
      // Top stop is opaque at 0px.
      expect(mask).toContain("black 0px");
      // Bottom fade is preserved.
      expect(mask).toContain("transparent 100%");
    });

    it("the header is flex-shrink:0 (stays pinned; only list scrolls)", () => {
      // flex-shrink:0 prevents the header from collapsing when the list
      // flex:1 expands to take remaining panel height.
      renderPanel();
      const header = screen.getByTestId("grace2-cases-header");
      expect(header.style.flexShrink).toBe("0");
    });

    it("rows still render and are selectable inside the scrollable list", () => {
      const onSelect = vi.fn();
      render(
        <CasesPanel
          cases={[CASE_FORT_MYERS, CASE_NORCAL_FIRE]}
          activeCaseId={null}
          onCreate={vi.fn()}
          onSelect={onSelect}
          onRename={vi.fn()}
          onArchive={vi.fn()}
          onDelete={vi.fn()}
        />,
      );
      const list = screen.getByTestId("grace2-cases-list");
      const rows = list.querySelectorAll('[data-testid="grace2-case-row"]');
      expect(rows).toHaveLength(2);
      fireEvent.click(
        Array.from(rows).find(
          (r) => r.getAttribute("data-case-id") === CASE_NORCAL_FIRE.case_id,
        )!,
      );
      expect(onSelect).toHaveBeenCalledWith(CASE_NORCAL_FIRE.case_id);
    });

    // --- single-scroll-container invariant (mobile drawer chain) ----------
    // The visibly-broken path was the mobile drawer. The bug was a DOUBLE
    // scroll container: the App.tsx hugger had overflowY:auto AND the
    // CasesPanel list has overflowY:auto. The hugger won (it scrolled the
    // whole panel including the pinned header), the internal list never
    // engaged, and the root's 100vh maxHeight (taller than the hugger's
    // bounded slot) meant the panel sized to content and never squeezed the
    // list. The fix demotes the hugger to overflow:hidden so the LIST is the
    // SINGLE scroll container. We mirror App.tsx's mobile chain here and
    // assert exactly one overflowY:auto element in the cases-panel subtree.
    describe("single scroll container on the mobile drawer path", () => {
      function renderMobileChain(caseCount = 8) {
        const cases: CaseSummary[] = Array.from(
          { length: caseCount },
          (_, i) => ({
            schema_version: "v1" as const,
            case_id: `01ABCDEFGHJKMNPQRSTVWX00${String(i).padStart(2, "0")}`,
            title: `Case ${i}`,
            created_at: "2026-06-01T00:00:00.000Z",
            updated_at: `2026-06-0${Math.max(1, i + 1)}T00:00:00.000Z`,
            status: "active" as const,
          }),
        );
        render(
          // Mirror App.tsx mobile slot EXACTLY: MobileDrawer > hugger
          // (flex:1 + minHeight:0 + overflow:hidden + pointerEvents:none) >
          // inner div (width:100% + flex:1 + minHeight:0 + display:flex +
          // flexDirection:column + pointerEvents:auto) > CasesPanel.
          //
          // MOBILE CASES-SCROLL FIX (NATE 2026-06-20): the inner wrapper used to
          // be `width:100% + pointerEvents:auto` ONLY — a content-sized
          // (height:auto) block that broke the height chain between the bounded
          // hugger and CasesPanel (height:100% resolved against auto). It is now
          // a bounded flex column so the hugger's real height passes THROUGH to
          // CasesPanel and the inner list scrolls.
          <MobileDrawer open={true} onClose={vi.fn()}>
            <div
              data-testid="test-mobile-hugger"
              style={{
                flex: 1,
                minHeight: 0,
                overflow: "hidden",
                pointerEvents: "none",
              }}
            >
              <div
                data-testid="test-mobile-inner"
                style={{
                  width: "100%",
                  flex: 1,
                  minHeight: 0,
                  display: "flex",
                  flexDirection: "column",
                  pointerEvents: "auto",
                }}
              >
                <CasesPanel
                  cases={cases}
                  activeCaseId={null}
                  onCreate={vi.fn()}
                  onSelect={vi.fn()}
                  onRename={vi.fn()}
                  onArchive={vi.fn()}
                  onDelete={vi.fn()}
                />
              </div>
            </div>
          </MobileDrawer>,
        );
      }

      it("the hugger does NOT double-scroll (overflow:hidden, not overflowY:auto)", () => {
        // The hugger is a bounded flex passthrough, NOT a scroll container —
        // it must not compete with the CasesPanel list. A regression that
        // re-adds overflowY:auto here reintroduces the double-scroll that
        // scrolled the pinned header and defeated the mask-gradient.
        renderMobileChain();
        const hugger = screen.getByTestId("test-mobile-hugger");
        expect(hugger.style.overflow).toBe("hidden");
        expect(hugger.style.overflowY).toBe("");
        // The F52 pointer-events click-through is preserved.
        expect(hugger.style.pointerEvents).toBe("none");
      });

      it("there is EXACTLY ONE overflowY:auto scroll container in the cases-panel subtree", () => {
        // The single scroll container must be the inner cases-list div (so the
        // header stays pinned + the mask-gradient applies). Walk the whole
        // mobile drawer subtree and count overflowY:auto elements.
        renderMobileChain();
        const drawer = screen.getByTestId("grace2-mobile-drawer");
        const scrollers = Array.from(
          drawer.querySelectorAll<HTMLElement>("*"),
        ).filter((el) => el.style.overflowY === "auto");
        expect(scrollers).toHaveLength(1);
        // And it is the cases-list div, not the hugger / panel root.
        expect(scrollers[0]!.getAttribute("data-testid")).toBe(
          "grace2-cases-list",
        );
      });

      it("the inner wrapper is a BOUNDED flex passthrough (flex:1 + minHeight:0 + flex column)", () => {
        // MOBILE CASES-SCROLL FIX (NATE 2026-06-20): the wrapper BETWEEN the
        // bounded hugger and CasesPanel must propagate the hugger's bounded
        // height. A content-sized (height:auto) block here makes CasesPanel's
        // height:100% resolve against auto, so the panel sizes to content, the
        // list (flex:1) is never squeezed, and the mobile list does not scroll.
        // It must be flex:1 + minHeight:0 + display:flex + flexDirection:column.
        renderMobileChain();
        const inner = screen.getByTestId("test-mobile-inner");
        expect(inner.style.flex).toMatch(/^1\b/);
        expect(inner.style.minHeight).toBe("0");
        expect(inner.style.display).toBe("flex");
        expect(inner.style.flexDirection).toBe("column");
        // F52 click-through on the interactive card is preserved.
        expect(inner.style.pointerEvents).toBe("auto");
      });

      it("the panel root fills the hugger bound (height:100% + minHeight:0) inside the drawer", () => {
        // With the hugger bounded (flex:1 + minHeight:0), the inner wrapper a
        // bounded flex passthrough, and CasesPanel height:100%, the list
        // (flex:1) gets squeezed below content so its overflowY:auto engages —
        // the mechanism that actually makes the list scroll on the mobile path.
        renderMobileChain();
        const panel = screen.getByTestId("grace2-cases-panel");
        expect(panel.style.height).toBe("100%");
        expect(panel.style.minHeight).toBe("0");
        expect(panel.style.overflow).toBe("hidden");
      });
    });

    // --- cases-panel-layout (NATE 2026-06-20): DESKTOP rail converged onto ----
    // the mobile cases-section presentation. The desktop wrapper used to stretch
    // top:12 -> bottom:12 (full-viewport-height) so the panel ran the entire
    // left edge and OVERLAPPED the bottom-left Settings pill. It now CONVERGES
    // on the mobile look: content-sized but CAPPED with a maxHeight that stops
    // above the Settings pill, and overflow:hidden so the inner list is the
    // SINGLE scroll container (matching the mobile hugger). We mirror App.tsx's
    // desktop rail wrapper EXACTLY here and assert the converged contract.
    describe("single scroll container on the desktop rail path", () => {
      function renderDesktopChain(caseCount = 8) {
        const cases: CaseSummary[] = Array.from(
          { length: caseCount },
          (_, i) => ({
            schema_version: "v1" as const,
            case_id: `01ABCDEFGHJKMNPQRSTVWX00${String(i).padStart(2, "0")}`,
            title: `Case ${i}`,
            created_at: "2026-06-01T00:00:00.000Z",
            updated_at: `2026-06-0${Math.max(1, i + 1)}T00:00:00.000Z`,
            status: "active" as const,
          }),
        );
        render(
          // Mirror App.tsx desktop cases-list rail wrapper EXACTLY: a
          // position:absolute, content-sized + maxHeight-capped flex column with
          // overflow:hidden (NO bottom anchor -> does not stretch over the
          // Settings pill).
          <div
            data-testid="test-desktop-rail"
            className="grace2-desktop-rail"
            style={{
              position: "absolute",
              top: 12,
              left: 16,
              maxHeight: "calc(100vh - 84px)",
              zIndex: 20,
              display: "flex",
              flexDirection: "column",
              minHeight: 0,
              overflow: "hidden",
            }}
          >
            <CasesPanel
              cases={cases}
              activeCaseId={null}
              onCreate={vi.fn()}
              onSelect={vi.fn()}
              onRename={vi.fn()}
              onArchive={vi.fn()}
              onDelete={vi.fn()}
            />
          </div>,
        );
      }

      it("the desktop rail wrapper is content-sized + CAPPED (maxHeight, no bottom stretch)", () => {
        // Converged onto the mobile look: NOT a full-height (top->bottom)
        // stretch. A maxHeight caps it; with no fixed height a short list hugs
        // its content. The absence of a `bottom` anchor is what keeps it ABOVE
        // the Settings pill (no overlap).
        renderDesktopChain();
        const wrap = screen.getByTestId("test-desktop-rail");
        expect(wrap.style.maxHeight).not.toBe("");
        expect(wrap.style.height).toBe(""); // content-sized, not stretched
        expect(wrap.style.bottom).toBe(""); // no bottom anchor -> clears Settings
        // overflow:hidden so the cap clips to the inner list's scroll region.
        expect(wrap.style.overflow).toBe("hidden");
        // flex column + minHeight:0 so the panel can be squeezed and its inner
        // list scrolls.
        expect(wrap.style.display).toBe("flex");
        expect(wrap.style.flexDirection).toBe("column");
        expect(wrap.style.minHeight).toBe("0");
      });

      it("the wrapper does NOT itself scroll (the LIST is the single scroller)", () => {
        renderDesktopChain();
        const wrap = screen.getByTestId("test-desktop-rail");
        // The wrapper clips (overflow:hidden) but is not a scroll container.
        expect(wrap.style.overflowY).toBe("");
      });

      it("there is EXACTLY ONE overflowY:auto scroll container in the desktop rail subtree", () => {
        // Same single-scroll-container invariant as the mobile path: the only
        // scroller is the inner cases-list div, so the header stays pinned and
        // the mask-gradient applies.
        renderDesktopChain();
        const wrap = screen.getByTestId("test-desktop-rail");
        const scrollers = Array.from(
          wrap.querySelectorAll<HTMLElement>("*"),
        ).filter((el) => el.style.overflowY === "auto");
        expect(scrollers).toHaveLength(1);
        expect(scrollers[0]!.getAttribute("data-testid")).toBe(
          "grace2-cases-list",
        );
      });

      it("the panel root fills the capped wrapper (height:100% + minHeight:0 + overflow:hidden)", () => {
        // Same panel contract on both surfaces - the convergence point: the
        // panel fills the bounded slot and its inner list (flex:1 + minHeight:0)
        // is what scrolls.
        renderDesktopChain();
        const panel = screen.getByTestId("grace2-cases-panel");
        expect(panel.style.height).toBe("100%");
        expect(panel.style.minHeight).toBe("0");
        expect(panel.style.overflow).toBe("hidden");
      });

      it("ALL desktop functionality survives: create / select / rename / delete reachable", () => {
        // Convergence must drop ZERO desktop functionality. Assert each control
        // is present + wired inside the converged rail.
        const onCreate = vi.fn();
        const onSelect = vi.fn();
        const onRename = vi.fn();
        const onDelete = vi.fn();
        render(
          <div
            data-testid="test-desktop-rail-fn"
            className="grace2-desktop-rail"
            style={{
              position: "absolute",
              top: 12,
              left: 16,
              maxHeight: "calc(100vh - 84px)",
              display: "flex",
              flexDirection: "column",
              minHeight: 0,
              overflow: "hidden",
            }}
          >
            <CasesPanel
              cases={[CASE_FORT_MYERS]}
              activeCaseId={null}
              onCreate={onCreate}
              onSelect={onSelect}
              onRename={onRename}
              onArchive={vi.fn()}
              onDelete={onDelete}
            />
          </div>,
        );
        // Create
        fireEvent.click(screen.getByTestId("grace2-cases-new"));
        expect(onCreate).toHaveBeenCalledTimes(1);
        // Select (row click)
        fireEvent.click(screen.getByTestId("grace2-case-row"));
        expect(onSelect).toHaveBeenCalledWith(CASE_FORT_MYERS.case_id);
        // Rename (kebab -> Rename -> Enter)
        fireEvent.click(screen.getByTestId("grace2-case-row-menu-button"));
        fireEvent.click(screen.getByTestId("grace2-case-row-menu-rename"));
        const input = screen.getByTestId(
          "grace2-case-row-rename-input",
        ) as HTMLInputElement;
        fireEvent.change(input, { target: { value: "Renamed" } });
        fireEvent.keyDown(input, { key: "Enter" });
        expect(onRename).toHaveBeenCalledWith(CASE_FORT_MYERS.case_id, "Renamed");
        // Delete (kebab -> Delete -> Confirm)
        fireEvent.click(screen.getByTestId("grace2-case-row-menu-button"));
        fireEvent.click(screen.getByTestId("grace2-case-row-menu-delete"));
        fireEvent.click(screen.getByTestId("grace2-case-delete-dialog-confirm"));
        expect(onDelete).toHaveBeenCalledWith(CASE_FORT_MYERS.case_id);
      });
    });
  });
});

// --- ConfirmationDialog ------------------------------------------------ //

describe("ConfirmationDialog", () => {
  it("Esc triggers onCancel", () => {
    const onCancel = vi.fn();
    const onConfirm = vi.fn();
    render(
      <ConfirmationDialog
        title="Delete?"
        message="Are you sure?"
        confirmLabel="Delete"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("Enter triggers onConfirm", () => {
    const onCancel = vi.fn();
    const onConfirm = vi.fn();
    render(
      <ConfirmationDialog
        title="Delete?"
        message="Are you sure?"
        confirmLabel="Delete"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );
    fireEvent.keyDown(window, { key: "Enter" });
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("backdrop click triggers onCancel; dialog click does not", () => {
    const onCancel = vi.fn();
    render(
      <ConfirmationDialog
        title="Delete?"
        message="Are you sure?"
        confirmLabel="Delete"
        onConfirm={vi.fn()}
        onCancel={onCancel}
      />,
    );
    // Click backdrop
    fireEvent.click(screen.getByTestId("grace2-confirmation-dialog-backdrop"));
    expect(onCancel).toHaveBeenCalledTimes(1);
    // Click dialog body — should NOT bubble to backdrop
    fireEvent.click(screen.getByTestId("grace2-confirmation-dialog"));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});

// --- useCases hook ------------------------------------------------------ //

describe("useCases", () => {
  it("createCase emits case-command(create) with no title hint", () => {
    const send = vi.fn();
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: send, isSignedIn: true }),
    );
    act(() => result.current.createCase());
    expect(send).toHaveBeenCalledWith("create", null, {});
  });

  it("createCase emits case-command(create) WITH title hint when provided", () => {
    const send = vi.fn();
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: send, isSignedIn: true }),
    );
    act(() => result.current.createCase("Ian"));
    expect(send).toHaveBeenCalledWith("create", null, { title: "Ian" });
  });

  it("selectCase emits case-command(select, case_id)", () => {
    const send = vi.fn();
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: send, isSignedIn: true }),
    );
    act(() => result.current.selectCase("01CASEID0000000000000XYZAB"));
    expect(send).toHaveBeenCalledWith(
      "select",
      "01CASEID0000000000000XYZAB",
      {},
    );
  });

  it("renameCase emits case-command(rename) with trimmed title", () => {
    const send = vi.fn();
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: send, isSignedIn: true }),
    );
    act(() => result.current.renameCase("01CASEID0000000000000XYZAB", "  Ian  "));
    expect(send).toHaveBeenCalledWith(
      "rename",
      "01CASEID0000000000000XYZAB",
      { title: "Ian" },
    );
  });

  it("renameCase with empty title does NOT emit", () => {
    const send = vi.fn();
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: send, isSignedIn: true }),
    );
    act(() => result.current.renameCase("01CASEID0000000000000XYZAB", "   "));
    expect(send).not.toHaveBeenCalled();
  });

  it("archiveCase + deleteCase emit the corresponding case-commands", () => {
    const send = vi.fn();
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: send, isSignedIn: true }),
    );
    act(() => result.current.archiveCase("01CASEID0000000000000XYZAB"));
    act(() => result.current.deleteCase("01CASEID0000000000000XYZAB"));
    expect(send).toHaveBeenNthCalledWith(
      1,
      "archive",
      "01CASEID0000000000000XYZAB",
      {},
    );
    expect(send).toHaveBeenNthCalledWith(
      2,
      "delete",
      "01CASEID0000000000000XYZAB",
      {},
    );
  });

  it("onCaseList updates cases list", () => {
    const send = vi.fn();
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: send, isSignedIn: true }),
    );
    expect(result.current.cases).toHaveLength(0);
    act(() => {
      const env: CaseListEnvelopePayload = {
        envelope_type: "case-list",
        cases: [CASE_FORT_MYERS, CASE_NORCAL_FIRE],
      };
      result.current.onCaseList(env);
    });
    expect(result.current.cases).toHaveLength(2);
    expect(result.current.cases[0]!.case_id).toBe(CASE_FORT_MYERS.case_id);
  });

  // "Cases vanish on refresh" - an EMPTY case-list that arrives over the live
  // WS while identity is still settling (a reconnect heartbeat, or the moment
  // before the server re-binds the anon User) is NON-authoritative: it must NOT
  // clear a populated rail (that is the flash-empty NATE reported). Only an
  // EXPLICIT empty result AFTER identity is confirmed bound (the authoritative
  // cold FETCH, isAuthoritative=true) clears it.
  it("an EMPTY non-authoritative case-list during settle does NOT clear a populated rail", () => {
    const send = vi.fn();
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: send, isSignedIn: true }),
    );
    // Rail is populated (a good frame landed).
    act(() => {
      result.current.onCaseList({
        envelope_type: "case-list",
        cases: [CASE_FORT_MYERS, CASE_NORCAL_FIRE],
      });
    });
    expect(result.current.cases).toHaveLength(2);
    // A reconnect/settle empty frame arrives over the live WS (default
    // isAuthoritative=false). The rail MUST be preserved.
    act(() => {
      result.current.onCaseList({
        envelope_type: "case-list",
        cases: [],
      });
    });
    expect(result.current.cases).toHaveLength(2);
    expect(result.current.cases[0]!.case_id).toBe(CASE_FORT_MYERS.case_id);
  });

  it("an EMPTY AUTHORITATIVE case-list (cold fetch after identity bound) DOES clear the rail", () => {
    const send = vi.fn();
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: send, isSignedIn: true }),
    );
    act(() => {
      result.current.onCaseList({
        envelope_type: "case-list",
        cases: [CASE_FORT_MYERS],
      });
    });
    expect(result.current.cases).toHaveLength(1);
    // Authoritative empty = genuine zero-cases answer; clears the rail.
    act(() => {
      result.current.onCaseList(
        { envelope_type: "case-list", cases: [] },
        true,
      );
    });
    expect(result.current.cases).toHaveLength(0);
  });

  it("onCaseOpen with session_state hydrates active case + session", () => {
    const send = vi.fn();
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: send, isSignedIn: true }),
    );
    const session: CaseSessionState = {
      case: CASE_FORT_MYERS,
      chat_history: [],
      loaded_layers: [],
      pipeline_history: [],
      current_pipeline: null,
    };
    act(() => {
      const env: CaseOpenEnvelopePayload = {
        envelope_type: "case-open",
        session_state: session,
      };
      result.current.onCaseOpen(env);
    });
    expect(result.current.activeCaseId).toBe(CASE_FORT_MYERS.case_id);
    expect(result.current.activeSession).not.toBeNull();
    expect(result.current.activeSession!.case.title).toBe(
      "Hurricane Ian — Fort Myers",
    );
  });

  it("onCaseOpen with null session_state clears active case + session", () => {
    const send = vi.fn();
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: send, isSignedIn: true }),
    );
    const session: CaseSessionState = {
      case: CASE_FORT_MYERS,
    };
    act(() => {
      result.current.onCaseOpen({
        envelope_type: "case-open",
        session_state: session,
      });
    });
    expect(result.current.activeCaseId).toBe(CASE_FORT_MYERS.case_id);
    act(() => {
      result.current.onCaseOpen({
        envelope_type: "case-open",
        session_state: null,
      });
    });
    expect(result.current.activeCaseId).toBeNull();
    expect(result.current.activeSession).toBeNull();
  });

  it("persistenceState=anonymous when isSignedIn=false", () => {
    const send = vi.fn();
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: send, isSignedIn: false }),
    );
    expect(result.current.persistenceState).toBe("anonymous");
  });

  it("persistenceState transitions: saved → saving (after emit) → saved (after case-list)", () => {
    const send = vi.fn();
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: send, isSignedIn: true }),
    );
    expect(result.current.persistenceState).toBe("saved");
    act(() => result.current.createCase("Ian"));
    expect(result.current.persistenceState).toBe("saving");
    act(() => {
      result.current.onCaseList({
        envelope_type: "case-list",
        cases: [CASE_FORT_MYERS],
      });
    });
    expect(result.current.persistenceState).toBe("saved");
  });
});

// --- formatRelative pure function -------------------------------------- //

describe("formatRelative", () => {
  it("returns 'just now' for <30s", () => {
    expect(
      formatRelative(new Date(NOW.getTime() - 10_000).toISOString(), NOW),
    ).toBe("just now");
  });
  it("returns Xm ago for minutes", () => {
    expect(
      formatRelative(new Date(NOW.getTime() - 5 * 60_000).toISOString(), NOW),
    ).toBe("5m ago");
  });
  it("returns Xh ago for hours", () => {
    expect(
      formatRelative(
        new Date(NOW.getTime() - 2 * 60 * 60_000).toISOString(),
        NOW,
      ),
    ).toBe("2h ago");
  });
  it("returns Xd ago for days <7", () => {
    expect(
      formatRelative(
        new Date(NOW.getTime() - 3 * 24 * 60 * 60_000).toISOString(),
        NOW,
      ),
    ).toBe("3d ago");
  });
  it("returns a date label for >7 days", () => {
    const old = new Date(NOW.getTime() - 30 * 24 * 60 * 60_000).toISOString();
    const result = formatRelative(old, NOW);
    // Locale-dependent but must NOT be "Xd ago".
    expect(result).not.toMatch(/d ago$/);
  });
});

// --- formatBbox pure function ------------------------------------------ //

describe("formatBbox", () => {
  it("formats SW corner with hemispheres for the Fort Myers bbox", () => {
    expect(formatBbox([-82.0, 26.5, -81.7, 26.8])).toBe("82.0°W 26.5°N");
  });
  it("returns null for null/undefined input", () => {
    expect(formatBbox(null)).toBeNull();
    expect(formatBbox(undefined)).toBeNull();
  });
});
