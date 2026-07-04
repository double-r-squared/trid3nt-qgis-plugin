// GRACE-2 web - snap-on-select tests (NATE map/loading-UX polish, item 3).
//
// On case-select, App fits the map to the case SUMMARY bbox (CaseSummary.bbox,
// already on the cases list) IMMEDIATELY - before the full case/layers round-trip
// - so the camera moves + the analysis extent draws (and the bbox loading
// animation arms via the projected AOI rect) the instant the user taps, instead
// of dead air until the whole case loads.
//
// App.tsx's `onSelectCase` wraps useCases.selectCase: it looks up the tapped
// case's summary bbox, pushes a `zoom-to` map-command when valid, THEN calls
// selectCase. This mirrors that wrapper as a focused harness (the App.test.tsx
// "CaseExitShell" pattern) so the snap contract is verified without booting the
// full App shell (WS / auth / map).

import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";
import { useEffect } from "react";
import { asBbox } from "./lib/case_zoom";
import type { CaseSummary } from "./contracts";

interface RecordedCommand {
  command: string;
  args?: { bbox?: number[] };
}

function makeRecordingBus() {
  const commandPushes: RecordedCommand[] = [];
  return {
    commandPushes,
    pushMapCommand: (p: RecordedCommand) => commandPushes.push(p),
  };
}

/** Mirror of App.tsx's `onSelectCase` snap-on-select wrapper. */
function SnapOnSelectShell({
  bus,
  cases,
  selectCase,
  selectId,
}: {
  bus: { pushMapCommand: (p: RecordedCommand) => unknown };
  cases: CaseSummary[];
  selectCase: (id: string) => void;
  selectId: string;
}): JSX.Element {
  useEffect(() => {
    // The exact onSelectCase body.
    const summary = cases.find((c) => c.case_id === selectId);
    const previewBbox = summary ? asBbox(summary.bbox) : null;
    if (previewBbox) {
      bus.pushMapCommand({
        command: "zoom-to",
        args: { bbox: previewBbox },
      });
    }
    selectCase(selectId);
    // run once for the chosen select.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return <div data-testid="snap-shell" />;
}

function makeCase(id: string, bbox: CaseSummary["bbox"]): CaseSummary {
  return {
    case_id: id,
    title: `Case ${id}`,
    created_at: "2026-06-22T00:00:00Z",
    updated_at: "2026-06-22T00:00:00Z",
    status: "active",
    bbox,
  };
}

describe("App - snap-on-select (item 3)", () => {
  it("fits the SUMMARY bbox via zoom-to BEFORE select when the summary has a bbox", () => {
    const commandPushes: RecordedCommand[] = [];
    const order: string[] = [];
    const selectCase = vi.fn();
    const bus = {
      commandPushes,
      pushMapCommand: (p: RecordedCommand) => {
        order.push("zoom");
        commandPushes.push(p);
      },
    };
    const tracked = (id: string) => {
      order.push("select");
      selectCase(id);
    };
    const cases = [
      makeCase("c1", [-122.5, 37.7, -122.3, 37.85]),
      makeCase("c2", null),
    ];
    render(
      <SnapOnSelectShell
        bus={bus}
        cases={cases}
        selectCase={tracked}
        selectId="c1"
      />,
    );
    // A zoom-to with the summary bbox was pushed.
    expect(commandPushes).toEqual([
      { command: "zoom-to", args: { bbox: [-122.5, 37.7, -122.3, 37.85] } },
    ]);
    // selectCase was still called.
    expect(selectCase).toHaveBeenCalledWith("c1");
    // The snap (zoom) happens BEFORE the select (so the camera moves first).
    expect(order).toEqual(["zoom", "select"]);
  });

  it("does NOT push a zoom-to when the summary has no bbox (older / no-AOI Case)", () => {
    const bus = makeRecordingBus();
    const selectCase = vi.fn();
    const cases = [makeCase("c2", null)];
    render(
      <SnapOnSelectShell
        bus={bus}
        cases={cases}
        selectCase={selectCase}
        selectId="c2"
      />,
    );
    expect(bus.commandPushes).toHaveLength(0);
    expect(selectCase).toHaveBeenCalledWith("c2");
  });

  it("does NOT push a zoom-to for a malformed / non-finite summary bbox", () => {
    const bus = makeRecordingBus();
    const selectCase = vi.fn();
    // A non-finite bbox must be rejected by asBbox (no broken fitBounds).
    const cases = [
      makeCase("c3", [Number.NaN, 1, 2, 3] as unknown as CaseSummary["bbox"]),
    ];
    render(
      <SnapOnSelectShell
        bus={bus}
        cases={cases}
        selectCase={selectCase}
        selectId="c3"
      />,
    );
    expect(bus.commandPushes).toHaveLength(0);
    expect(selectCase).toHaveBeenCalledWith("c3");
  });
});
