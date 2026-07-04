// GRACE-2 web - AoiPickerCard tests (#170 manual-case onboarding, redesigned).
//
// Covers the request-free, two-step onboarding card + the draw-mode controls:
//   1. STEP 1 (name) -> STEP 2 (aoi) transition via Next; Back returns.
//   2. STEP 2 Skip relays the name with no bbox; Cancel aborts (no create).
//   3. STEP 2 "Draw AOI" DISMISSES the card (the key fix: map clear) and arms a
//      map drag gesture; the card itself is gone (no aoi-picker-card in the DOM).
//   4. DRAW: SAVE is gated until a rectangle is drawn (no half-drawn limbo);
//      after a drag SAVE forwards (bbox, name).
//   5. DRAW: RETRY clears the drawn rect (SAVE re-disabled, stay in draw mode).
//   6. DRAW: CANCEL discards the bbox and returns to the STEP 2 prompt.
//   7. NO-CLOBBER: the drag gesture is armed ONLY in draw mode (detached on the
//      transitions out of draw), so a stray drag can't clobber a committed AOI.
//
// The card draws onto a live MapLibre map; happy-dom has no WebGL, so we inject
// a minimal map stub covering only the methods the card + bbox_draw helpers
// touch (same shape SpatialDrawSurface.test.tsx uses).

import { describe, it, expect, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { AoiPickerCard } from "./AoiPickerCard";
import type { Map as MapLibreMap } from "maplibre-gl";

interface FakeMap extends MapLibreMap {
  __handlers: Record<string, (e: unknown) => void>;
}

function makeFakeMap(): FakeMap {
  const canvas = { style: { cursor: "" } };
  const handlers: Record<string, (e: unknown) => void> = {};
  const m = {
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
    // project() is read while tracking the drawn bbox's on-screen rect.
    project: ({ 0: lng, 1: lat }: number[]) => ({ x: (lng ?? 0) * 10, y: (lat ?? 0) * 10 }),
    dragPan: { enable: vi.fn(), disable: vi.fn() },
  } as unknown as FakeMap;
  return m;
}

function renderCard(map: MapLibreMap | null = makeFakeMap()) {
  const onConfirm = vi.fn<(b: [number, number, number, number], name: string) => void>();
  const onSkip = vi.fn<(name: string) => void>();
  const onCancel = vi.fn();
  render(
    <AoiPickerCard map={map} onConfirm={onConfirm} onSkip={onSkip} onCancel={onCancel} />,
  );
  return { onConfirm, onSkip, onCancel };
}

/** Advance from the NAME step to the AOI step, optionally typing a name first. */
function gotoAoiStep(name?: string): void {
  if (name !== undefined) {
    fireEvent.change(screen.getByTestId("aoi-name-input"), { target: { value: name } });
  }
  fireEvent.click(screen.getByTestId("aoi-name-next"));
}

/** Simulate dragging a rectangle on the fake map (down -> up). */
function dragRect(map: FakeMap, a: [number, number], b: [number, number]): void {
  act(() => {
    map.__handlers.mousedown!({ lngLat: { lng: a[0], lat: a[1] } });
    map.__handlers.mouseup!({ lngLat: { lng: b[0], lat: b[1] } });
  });
}

describe("AoiPickerCard - two-step onboarding", () => {
  it("starts on the NAME step", () => {
    renderCard();
    expect(screen.getByTestId("aoi-step-name")).toBeTruthy();
    expect(screen.queryByTestId("aoi-step-aoi")).toBeNull();
  });

  it("Next advances NAME -> AOI; Back returns", () => {
    renderCard();
    gotoAoiStep("Mexico Beach surge");
    expect(screen.getByTestId("aoi-step-aoi")).toBeTruthy();
    expect(screen.queryByTestId("aoi-step-name")).toBeNull();
    // The chosen name surfaces in the AOI prompt.
    expect(screen.getByTestId("aoi-step-aoi").textContent).toContain("Mexico Beach surge");
    fireEvent.click(screen.getByTestId("aoi-back"));
    expect(screen.getByTestId("aoi-step-name")).toBeTruthy();
  });

  it("Skip on the AOI step relays the trimmed name with NO bbox", () => {
    const { onSkip, onConfirm } = renderCard();
    gotoAoiStep("  Idaho flood  ");
    fireEvent.click(screen.getByTestId("aoi-skip"));
    expect(onSkip).toHaveBeenCalledTimes(1);
    expect(onSkip.mock.calls[0]![0]).toBe("Idaho flood");
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("Cancel aborts the onboarding (no create at any step)", () => {
    const { onCancel, onConfirm, onSkip } = renderCard();
    fireEvent.click(screen.getByTestId("aoi-cancel")); // name step cancel
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
    expect(onSkip).not.toHaveBeenCalled();
  });
});

describe("AoiPickerCard - draw mode (dismiss + save/retry/cancel)", () => {
  it("Draw AOI DISMISSES the card (map clear) and arms the drag gesture", () => {
    const map = makeFakeMap();
    renderCard(map);
    gotoAoiStep("MyCase");
    fireEvent.click(screen.getByTestId("aoi-draw"));
    // THE KEY FIX: the onboarding card is gone so the map is fully visible.
    expect(screen.queryByTestId("aoi-picker-card")).toBeNull();
    // The bbox-anchored controls are mounted (with the draw hint until a drag).
    expect(screen.getByTestId("aoi-draw-controls")).toBeTruthy();
    expect(screen.getByTestId("aoi-draw-hint")).toBeTruthy();
    // The drag gesture armed (down/up listeners attached).
    expect(map.__handlers.mousedown).toBeTruthy();
    expect(map.__handlers.mouseup).toBeTruthy();
  });

  it("SAVE is gated until a rectangle is drawn, then forwards (bbox, name)", () => {
    const map = makeFakeMap();
    const { onConfirm } = renderCard(map);
    gotoAoiStep("MyCase");
    fireEvent.click(screen.getByTestId("aoi-draw"));

    // No rectangle yet -> SAVE disabled (no half-drawn limbo / no create).
    expect((screen.getByTestId("aoi-save") as HTMLButtonElement).disabled).toBe(true);

    dragRect(map, [-85.31, 35.04], [-85.3, 35.05]);

    const saveBtn = screen.getByTestId("aoi-save") as HTMLButtonElement;
    expect(saveBtn.disabled).toBe(false);
    fireEvent.click(saveBtn);
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onConfirm.mock.calls[0]![0]).toEqual([-85.31, 35.04, -85.3, 35.05]);
    expect(onConfirm.mock.calls[0]![1]).toBe("MyCase");
  });

  it("RETRY clears the drawn rectangle and re-disables SAVE (stays in draw mode)", () => {
    const map = makeFakeMap();
    renderCard(map);
    gotoAoiStep();
    fireEvent.click(screen.getByTestId("aoi-draw"));
    dragRect(map, [-85.31, 35.04], [-85.3, 35.05]);
    expect((screen.getByTestId("aoi-save") as HTMLButtonElement).disabled).toBe(false);

    act(() => {
      fireEvent.click(screen.getByTestId("aoi-retry"));
    });
    // Still in draw mode (controls present), SAVE disabled again.
    expect(screen.getByTestId("aoi-draw-controls")).toBeTruthy();
    expect((screen.getByTestId("aoi-save") as HTMLButtonElement).disabled).toBe(true);
  });

  it("CANCEL discards the bbox and returns to the AOI prompt (no create)", () => {
    const map = makeFakeMap();
    const { onConfirm } = renderCard(map);
    gotoAoiStep();
    fireEvent.click(screen.getByTestId("aoi-draw"));
    dragRect(map, [-85.31, 35.04], [-85.3, 35.05]);
    fireEvent.click(screen.getByTestId("aoi-draw-cancel"));
    // Back on the STEP 2 prompt; nothing created.
    expect(screen.getByTestId("aoi-step-aoi")).toBeTruthy();
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("NO-CLOBBER: leaving draw mode (CANCEL) detaches the drag gesture", () => {
    const map = makeFakeMap();
    renderCard(map);
    gotoAoiStep();
    fireEvent.click(screen.getByTestId("aoi-draw"));
    const offBefore = (map.off as ReturnType<typeof vi.fn>).mock.calls.length;
    fireEvent.click(screen.getByTestId("aoi-draw-cancel"));
    // attachBboxDrag's detach removes the mousedown/move/up listeners, so the
    // gesture can never fire (and clobber a committed AOI) outside draw mode.
    const offCalls = (map.off as ReturnType<typeof vi.fn>).mock.calls
      .slice(offBefore)
      .map((c) => c[0]);
    expect(offCalls).toContain("mousedown");
    expect(offCalls).toContain("mouseup");
  });
});
