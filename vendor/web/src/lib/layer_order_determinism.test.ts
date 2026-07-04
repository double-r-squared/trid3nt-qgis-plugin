// GRACE-2 web - BUG 2 cross-surface ordering determinism.
//
// ROOT CAUSE the fix addresses: the agent emits z_index=null; the old wire type
// lied (`z_index: number`), so a bare `b.z_index - a.z_index` over a null became
// `undefined - undefined = NaN` -> sort() had NO total order -> the three render
// surfaces (LayerPanel rows, the Map overlay stack, App's `layers` from the
// cache) each rendered the SAME set in a DIFFERENT order. The fix routes ALL
// THREE through the ONE shared comparator `compareLayersTopFirst` (z desc with a
// layer_id tiebreak), so the order is a pure function of the SET regardless of
// the input array order or a null z_index.
//
// This test feeds the SAME set in THREE different input orders and asserts that
// all three code paths produce the IDENTICAL ordered layer_id sequence:
//   1. LayerPanel  -> sortTopFirst()
//   2. Map stack   -> compareLayersTopFirst (the comparator Map.tsx sorts
//      currentLayers + applyLayerOrder by, so the overlay stack matches)
//   3. App layers  -> LayerCache.mergeSnapshot() (returns values pre-sorted by
//      the same comparator)

import { describe, it, expect } from "vitest";
import { compareLayersTopFirst, type ProjectLayerSummary } from "../contracts";
import { sortTopFirst } from "../LayerPanel";
import { LayerCache } from "./layer_cache";

function mk(
  id: string,
  z: number | null,
): ProjectLayerSummary {
  return {
    layer_id: id,
    name: id,
    layer_type: "raster",
    uri: `s3://bucket/${id}.tif`,
    visible: true,
    opacity: 1,
    z_index: z,
  };
}

/** The Map overlay-stack order: Map.tsx sorts currentLayers by this comparator. */
function mapStackOrder(layers: ProjectLayerSummary[]): string[] {
  return [...layers].sort(compareLayersTopFirst).map((l) => l.layer_id);
}

/** The App `layers` order: the cache returns mergeSnapshot pre-sorted. */
function appLayersOrder(layers: ProjectLayerSummary[]): string[] {
  const cache = new LayerCache({ backend: { load: async () => ({}), save: async () => {} } });
  return cache
    .mergeSnapshot("case-1", layers, { authoritativeReplace: true })
    .map((l) => l.layer_id);
}

/** The LayerPanel row order. */
function panelOrder(layers: ProjectLayerSummary[]): string[] {
  return sortTopFirst(layers).map((l) => l.layer_id);
}

describe("BUG 2 - 3-surface ordering determinism", () => {
  // A set with a mix of real z, ties, and the agent's NULL z_index (the trigger).
  const base = [
    mk("alpha", null),
    mk("bravo", 5),
    mk("charlie", null),
    mk("delta", 5), // ties with bravo on z=5 -> layer_id tiebreak decides
    mk("echo", 2),
  ];

  // Three DIFFERENT input array orders of the SAME set.
  const order1 = [base[0]!, base[1]!, base[2]!, base[3]!, base[4]!];
  const order2 = [base[4]!, base[3]!, base[2]!, base[1]!, base[0]!]; // reversed
  const order3 = [base[2]!, base[0]!, base[4]!, base[1]!, base[3]!]; // shuffled

  it("the shared comparator is a deterministic TOTAL order (null z -> 0, id tiebreak)", () => {
    const a = mapStackOrder(order1);
    const b = mapStackOrder(order2);
    const c = mapStackOrder(order3);
    expect(a).toEqual(b);
    expect(a).toEqual(c);
    // z=5 wins (bravo,delta - id tiebreak: bravo<delta), then z=2 (echo), then the
    // null-z pair (alpha,charlie - coerced to 0, id tiebreak: alpha<charlie).
    expect(a).toEqual(["bravo", "delta", "echo", "alpha", "charlie"]);
  });

  it("panel == map == App order for ALL THREE input orders (same set)", () => {
    for (const input of [order1, order2, order3]) {
      const panel = panelOrder(input);
      const map = mapStackOrder(input);
      const app = appLayersOrder(input);
      // The key invariant: panel order == map order == App order, always.
      expect(panel).toEqual(map);
      expect(map).toEqual(app);
      // ...and equal to the canonical deterministic order.
      expect(panel).toEqual(["bravo", "delta", "echo", "alpha", "charlie"]);
    }
  });

  it("a null z_index never produces NaN (would defeat the sort)", () => {
    // Two all-null layers: a bare `b.z_index - a.z_index` => NaN => no reorder.
    // The comparator coerces null->0 and tiebreaks on layer_id deterministically.
    const allNull = [mk("zulu", null), mk("alpha", null), mk("mike", null)];
    const expected = ["alpha", "mike", "zulu"]; // pure layer_id order
    expect(panelOrder(allNull)).toEqual(expected);
    expect(mapStackOrder(allNull)).toEqual(expected);
    expect(appLayersOrder(allNull)).toEqual(expected);
  });
});
