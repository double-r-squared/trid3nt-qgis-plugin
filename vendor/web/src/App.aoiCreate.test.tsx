// GRACE-2 web - AOI-first create-action seam tests (#170 redesigned onboarding).
//
// The full App mounts Chat (WebSocket) + MapView (WebGL), neither of which runs
// in happy-dom, so - mirroring App.test.tsx's CollapseShell pattern - we exercise
// the create-action SEAM here through a minimal harness that wires the SAME
// pieces App.tsx wires:
//
//   - the real useCases hook (with a stubbed sendCaseCommand),
//   - the "+ New Case" button -> onCreate opens the AOI onboarding overlay (it
//     does NOT create immediately),
//   - the real AoiPickerCard rendered while the overlay is open,
//   - confirm(bbox, name) -> createCase(name, bbox); skip(name) -> createCase(name).
//
// Asserts:
//   1. "+ New Case" opens the overlay (no case-command fired yet).
//   2. Name -> draw -> Save forwards the captured bbox + name into create.
//   3. Skip preserves the no-bbox path (create command with just the name).

import { describe, it, expect, vi } from "vitest";
import { useState } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { AoiPickerCard } from "./components/AoiPickerCard";
import { useCases, type CaseCommandEmitter } from "./hooks/useCases";
import type { BBox } from "./lib/bbox_draw";
import type { Map as MapLibreMap } from "maplibre-gl";

interface FakeMap extends MapLibreMap {
  __handlers: Record<string, (e: unknown) => void>;
}

function makeFakeMap(): FakeMap {
  const canvas = { style: { cursor: "" } };
  const handlers: Record<string, (e: unknown) => void> = {};
  return {
    __handlers: handlers,
    fitBounds: vi.fn(),
    isStyleLoaded: () => true,
    getCanvas: () => canvas,
    getSource: () => undefined,
    addSource: vi.fn(),
    getLayer: () => undefined,
    addLayer: vi.fn(),
    removeLayer: vi.fn(),
    removeSource: vi.fn(),
    on: vi.fn((ev: string, cb: (e: unknown) => void) => {
      handlers[ev] = cb;
    }),
    off: vi.fn(),
    once: vi.fn(),
    project: ({ 0: lng, 1: lat }: number[]) => ({ x: (lng ?? 0) * 10, y: (lat ?? 0) * 10 }),
    dragPan: { enable: vi.fn(), disable: vi.fn() },
  } as unknown as FakeMap;
}

// Minimal harness replicating App.tsx's create-action seam (~App.tsx create
// seam) + the Map.tsx AoiPickerCard mount gate, sans WS/WebGL.
function CreateSeamHarness({
  sendCaseCommand,
  map,
}: {
  sendCaseCommand: CaseCommandEmitter;
  map: MapLibreMap | null;
}): JSX.Element {
  const { createCase } = useCases({ sendCaseCommand, isSignedIn: true });
  const [aoiCaptureOpen, setAoiCaptureOpen] = useState(false);

  // The create-action seam: open the overlay instead of creating immediately.
  const onCreate = (): void => setAoiCaptureOpen(true);
  const onConfirm = (bbox: BBox, name: string): void => {
    setAoiCaptureOpen(false);
    createCase(name || null, bbox);
  };
  const onSkip = (name: string): void => {
    setAoiCaptureOpen(false);
    createCase(name || null);
  };
  const onCancel = (): void => setAoiCaptureOpen(false);

  return (
    <div>
      <button data-testid="new-case" onClick={onCreate}>
        + New Case
      </button>
      {aoiCaptureOpen ? (
        <AoiPickerCard map={map} onConfirm={onConfirm} onSkip={onSkip} onCancel={onCancel} />
      ) : null}
    </div>
  );
}

describe("AOI-first create-action seam (#170)", () => {
  it("opens the AOI overlay on + New Case WITHOUT creating immediately", () => {
    const send = vi.fn();
    render(<CreateSeamHarness sendCaseCommand={send} map={null} />);
    // No overlay until the button is pressed.
    expect(screen.queryByTestId("aoi-picker-card")).toBeNull();
    fireEvent.click(screen.getByTestId("new-case"));
    expect(screen.getByTestId("aoi-picker-card")).toBeTruthy();
    // Crucially: no case-command fired just from opening the overlay.
    expect(send).not.toHaveBeenCalled();
  });

  it("name -> draw -> Save creates the Case WITH the captured bbox + name", () => {
    const send = vi.fn();
    const map = makeFakeMap();
    render(<CreateSeamHarness sendCaseCommand={send} map={map} />);
    fireEvent.click(screen.getByTestId("new-case"));

    // STEP 1: name.
    fireEvent.change(screen.getByTestId("aoi-name-input"), {
      target: { value: "Chattanooga flood" },
    });
    fireEvent.click(screen.getByTestId("aoi-name-next"));
    // STEP 2: draw.
    fireEvent.click(screen.getByTestId("aoi-draw"));
    act(() => {
      map.__handlers.mousedown!({ lngLat: { lng: -85.31, lat: 35.04 } });
      map.__handlers.mouseup!({ lngLat: { lng: -85.3, lat: 35.05 } });
    });
    fireEvent.click(screen.getByTestId("aoi-save"));

    expect(send).toHaveBeenCalledTimes(1);
    expect(send.mock.calls[0]).toEqual([
      "create",
      null,
      { title: "Chattanooga flood", bbox: [-85.31, 35.04, -85.3, 35.05] },
    ]);
    // Overlay closed after save.
    expect(screen.queryByTestId("aoi-picker-card")).toBeNull();
  });

  it("Skip preserves the no-bbox path (create with just the name)", () => {
    const send = vi.fn();
    render(<CreateSeamHarness sendCaseCommand={send} map={null} />);
    fireEvent.click(screen.getByTestId("new-case"));
    fireEvent.change(screen.getByTestId("aoi-name-input"), { target: { value: "Quick look" } });
    fireEvent.click(screen.getByTestId("aoi-name-next"));
    fireEvent.click(screen.getByTestId("aoi-skip"));

    expect(send).toHaveBeenCalledTimes(1);
    expect(send.mock.calls[0]).toEqual(["create", null, { title: "Quick look" }]);
    expect(screen.queryByTestId("aoi-picker-card")).toBeNull();
  });

  it("Skip with an empty name preserves the byte-identical no-args create", () => {
    const send = vi.fn();
    render(<CreateSeamHarness sendCaseCommand={send} map={null} />);
    fireEvent.click(screen.getByTestId("new-case"));
    fireEvent.click(screen.getByTestId("aoi-name-next"));
    fireEvent.click(screen.getByTestId("aoi-skip"));
    expect(send.mock.calls[0]).toEqual(["create", null, {}]);
  });

  it("Cancel dismisses the overlay and creates nothing", () => {
    const send = vi.fn();
    render(<CreateSeamHarness sendCaseCommand={send} map={null} />);
    fireEvent.click(screen.getByTestId("new-case"));
    fireEvent.click(screen.getByTestId("aoi-cancel"));
    expect(send).not.toHaveBeenCalled();
    expect(screen.queryByTestId("aoi-picker-card")).toBeNull();
  });
});
