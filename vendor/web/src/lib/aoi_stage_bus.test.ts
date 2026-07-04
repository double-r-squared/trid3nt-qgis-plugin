// GRACE-2 web - staged-AOI bus unit tests (NATE item 4).

import { describe, it, expect, beforeEach } from "vitest";
import { aoiStageBus } from "./aoi_stage_bus";

beforeEach(() => {
  aoiStageBus.clear();
});

describe("aoiStageBus", () => {
  it("starts disarmed with no staged bbox", () => {
    expect(aoiStageBus.getState()).toEqual({ armed: false, bbox: null });
  });

  it("fires the subscriber immediately with the current state", () => {
    const seen: Array<{ armed: boolean; bbox: unknown }> = [];
    const unsub = aoiStageBus.subscribe((s) => seen.push({ ...s }));
    expect(seen).toHaveLength(1);
    expect(seen[0]).toEqual({ armed: false, bbox: null });
    unsub();
  });

  it("setArmed toggles + notifies", () => {
    const seen: boolean[] = [];
    const unsub = aoiStageBus.subscribe((s) => seen.push(s.armed));
    aoiStageBus.setArmed(true);
    expect(aoiStageBus.getState().armed).toBe(true);
    expect(seen[seen.length - 1]).toBe(true);
    // Idempotent: re-arming to the same value does not re-notify.
    const before = seen.length;
    aoiStageBus.setArmed(true);
    expect(seen.length).toBe(before);
    unsub();
  });

  it("setBbox stages the extent + disarms (draw complete)", () => {
    aoiStageBus.setArmed(true);
    aoiStageBus.setBbox([1, 2, 3, 4]);
    expect(aoiStageBus.getState()).toEqual({ armed: false, bbox: [1, 2, 3, 4] });
  });

  it("clear() drops the staged extent + disarms", () => {
    aoiStageBus.setBbox([1, 2, 3, 4]);
    aoiStageBus.clear();
    expect(aoiStageBus.getState()).toEqual({ armed: false, bbox: null });
  });

  it("unsubscribe stops further notifications", () => {
    const seen: number[] = [];
    const unsub = aoiStageBus.subscribe(() => seen.push(1));
    const before = seen.length;
    unsub();
    aoiStageBus.setArmed(true);
    expect(seen.length).toBe(before);
  });
});
