// GRACE-2 web — LayerCache unit tests (job-0179, "the seatbelt").
//
// Covers the durability invariants:
//   - a STALE non-empty snapshot OMITTING a layer does NOT evict it
//     (allowsEvict stays false for the omitted-but-tracked layer);
//   - an EXPLICIT case switch / delete DOES evict;
//   - setOverride remembers + re-supplies opacity / visibility / zIndex;
//   - the LRU caps the in-memory entry count at maxCases;
//   - the IndexedDB override round-trip (load -> hydrate restores edits) via a
//     mock persistence backend.

import { describe, it, expect, beforeEach } from "vitest";
import {
  LayerCache,
  getLayerCache,
  setLayerCache,
  type OverridePersistenceBackend,
  type PersistedOverrides,
} from "./layer_cache";
import type { ProjectLayerSummary } from "../contracts";

function mkLayer(
  id: string,
  over: Partial<ProjectLayerSummary> = {},
): ProjectLayerSummary {
  return {
    layer_id: id,
    name: id,
    layer_type: "raster",
    uri: `s3://bucket/${id}.tif`,
    visible: true,
    opacity: 1,
    z_index: 0,
    ...over,
  };
}

/** A fully in-memory persistence backend so the IDB path is testable. */
function makeMockBackend(): OverridePersistenceBackend & {
  store: PersistedOverrides;
  saves: number;
} {
  const self = {
    store: {} as PersistedOverrides,
    saves: 0,
    async load(): Promise<PersistedOverrides> {
      // deep clone so the cache can't mutate the stored blob by reference.
      return JSON.parse(JSON.stringify(self.store)) as PersistedOverrides;
    },
    async save(all: PersistedOverrides): Promise<void> {
      self.saves += 1;
      self.store = JSON.parse(JSON.stringify(all)) as PersistedOverrides;
    },
  };
  return self;
}

const noopBackend: OverridePersistenceBackend = {
  async load() {
    return {};
  },
  async save() {
    /* no-op */
  },
};

describe("LayerCache — layer-set durability (the seatbelt)", () => {
  it("a stale non-empty snapshot omitting a layer does NOT evict it", () => {
    const cache = new LayerCache({ backend: noopBackend });
    const caseId = "case-A";
    // Authoritative full set establishes two layers.
    cache.mergeSnapshot(caseId, [mkLayer("L1"), mkLayer("L2")], {
      authoritativeReplace: true,
    });
    expect(cache.layersFor(caseId).map((l) => l.layer_id)).toEqual([
      "L1",
      "L2",
    ]);

    // A STALE / partial reconnect frame omits L2 (and is NON-authoritative).
    const merged = cache.mergeSnapshot(caseId, [mkLayer("L1")], {
      authoritativeReplace: false,
    });
    // L2 is RETAINED — the blip never blanks it.
    expect(merged.map((l) => l.layer_id).sort()).toEqual(["L1", "L2"]);
    // The teardown gate refuses to evict the still-tracked omitted layer.
    expect(cache.allowsEvict(caseId, "L2")).toBe(false);
    expect(cache.allowsEvict(caseId, "L1")).toBe(false);
  });

  it("an authoritative replace DOES drop layers absent from the new set", () => {
    const cache = new LayerCache({ backend: noopBackend });
    const caseId = "case-A";
    cache.mergeSnapshot(caseId, [mkLayer("L1"), mkLayer("L2")], {
      authoritativeReplace: true,
    });
    // A genuine authoritative replace that omits L2 -> L2 evicted.
    const merged = cache.mergeSnapshot(caseId, [mkLayer("L1")], {
      authoritativeReplace: true,
    });
    expect(merged.map((l) => l.layer_id)).toEqual(["L1"]);
    expect(cache.allowsEvict(caseId, "L2")).toBe(true);
    expect(cache.allowsEvict(caseId, "L1")).toBe(false);
  });

  it("an empty authoritative frame does NOT evict a populated case", () => {
    // The cold-open hazard: opening a case cold feeds an empty/short
    // authoritative session-state frame; it must be a NO-OP, never a blank.
    const cache = new LayerCache({ backend: noopBackend });
    const caseId = "case-A";
    cache.mergeSnapshot(caseId, [mkLayer("L1"), mkLayer("L2")], {
      authoritativeReplace: true,
    });
    // An EMPTY authoritative frame arrives — must NOT blank the populated case.
    const merged = cache.mergeSnapshot(caseId, [], {
      authoritativeReplace: true,
    });
    expect(merged.map((l) => l.layer_id).sort()).toEqual(["L1", "L2"]);
    expect(cache.allowsEvict(caseId, "L1")).toBe(false);
    expect(cache.allowsEvict(caseId, "L2")).toBe(false);
  });

  it("an empty authoritative frame on an EMPTY case stays empty", () => {
    // No layers tracked yet -> an empty authoritative frame is honored as a
    // (still-empty) full set, not silently turned into a tracked phantom.
    const cache = new LayerCache({ backend: noopBackend });
    const caseId = "case-A";
    const merged = cache.mergeSnapshot(caseId, [], {
      authoritativeReplace: true,
    });
    expect(merged).toEqual([]);
    expect(cache.layersFor(caseId)).toEqual([]);
  });

  it("a non-empty authoritative frame still evicts omitted layers", () => {
    // The guard must not weaken a genuine non-empty authoritative replace.
    const cache = new LayerCache({ backend: noopBackend });
    const caseId = "case-A";
    cache.mergeSnapshot(caseId, [mkLayer("L1"), mkLayer("L2")], {
      authoritativeReplace: true,
    });
    const merged = cache.mergeSnapshot(caseId, [mkLayer("L1")], {
      authoritativeReplace: true,
    });
    expect(merged.map((l) => l.layer_id)).toEqual(["L1"]);
    expect(cache.allowsEvict(caseId, "L2")).toBe(true);
    expect(cache.allowsEvict(caseId, "L1")).toBe(false);
  });

  it("an explicit case switch (evictCase) DOES evict the whole Case", () => {
    const cache = new LayerCache({ backend: noopBackend });
    cache.mergeSnapshot("case-A", [mkLayer("L1")], {
      authoritativeReplace: true,
    });
    expect(cache.allowsEvict("case-A", "L1")).toBe(false);
    cache.evictCase("case-A");
    // The Case is gone -> every layer is now evictable, and layersFor is empty.
    expect(cache.allowsEvict("case-A", "L1")).toBe(true);
    expect(cache.layersFor("case-A")).toEqual([]);
  });

  it("an explicit single-layer delete evicts only that layer", () => {
    const cache = new LayerCache({ backend: noopBackend });
    cache.mergeSnapshot("case-A", [mkLayer("L1"), mkLayer("L2")], {
      authoritativeReplace: true,
    });
    cache.deleteLayer("case-A", "L1");
    expect(cache.allowsEvict("case-A", "L1")).toBe(true);
    expect(cache.allowsEvict("case-A", "L2")).toBe(false);
    expect(cache.layersFor("case-A").map((l) => l.layer_id)).toEqual(["L2"]);
  });

  it("a null (root) Case caches nothing and always allows evict", () => {
    const cache = new LayerCache({ backend: noopBackend });
    const merged = cache.mergeSnapshot(null, [mkLayer("L1")], {
      authoritativeReplace: false,
    });
    // Passed through verbatim, nothing tracked.
    expect(merged.map((l) => l.layer_id)).toEqual(["L1"]);
    expect(cache.layersFor(null)).toEqual([]);
    expect(cache.allowsEvict(null, "L1")).toBe(true);
  });
});

describe("LayerCache - mergeSnapshot identity stability (flash fix, Lane 1a)", () => {
  it("returns the SAME array instance for a byte-identical heartbeat", () => {
    const cache = new LayerCache({ backend: noopBackend });
    const caseId = "case-A";
    const first = cache.mergeSnapshot(caseId, [mkLayer("L1"), mkLayer("L2")], {
      authoritativeReplace: true,
    });
    // An identical heartbeat re-ships a FRESH array of FRESH object refs.
    const second = cache.mergeSnapshot(
      caseId,
      [mkLayer("L1"), mkLayer("L2")],
      { authoritativeReplace: true },
    );
    // The cache hands back the SAME array reference so React's setState bails
    // (no panel/scrubber re-render on the ~25s keepalive).
    expect(second).toBe(first);
  });

  it("KEEPS the existing object ref for a structurally-identical layer", () => {
    const cache = new LayerCache({ backend: noopBackend });
    const caseId = "case-A";
    const first = cache.mergeSnapshot(caseId, [mkLayer("L1")], {
      authoritativeReplace: true,
    });
    const storedL1 = first[0];
    // A non-authoritative top-up re-ships L1 with a NEW object ref but identical
    // fields -> the cache keeps the stored ref (no identity churn).
    cache.mergeSnapshot(caseId, [mkLayer("L1")], {
      authoritativeReplace: false,
    });
    expect(cache.layersFor(caseId)[0]).toBe(storedL1);
  });

  it("returns a NEW array when a layer field actually changes", () => {
    const cache = new LayerCache({ backend: noopBackend });
    const caseId = "case-A";
    const first = cache.mergeSnapshot(caseId, [mkLayer("L1", { opacity: 1 })], {
      authoritativeReplace: true,
    });
    const second = cache.mergeSnapshot(
      caseId,
      [mkLayer("L1", { opacity: 0.4 })],
      { authoritativeReplace: true },
    );
    expect(second).not.toBe(first);
    expect(second[0]?.opacity).toBe(0.4);
  });

  it("returns a NEW array when a layer is added or removed", () => {
    const cache = new LayerCache({ backend: noopBackend });
    const caseId = "case-A";
    const a = cache.mergeSnapshot(caseId, [mkLayer("L1")], {
      authoritativeReplace: true,
    });
    const b = cache.mergeSnapshot(caseId, [mkLayer("L1"), mkLayer("L2")], {
      authoritativeReplace: true,
    });
    expect(b).not.toBe(a);
    // Removing L2 (authoritative) also yields a fresh array.
    const c = cache.mergeSnapshot(caseId, [mkLayer("L1")], {
      authoritativeReplace: true,
    });
    expect(c).not.toBe(b);
    expect(c.map((l) => l.layer_id)).toEqual(["L1"]);
  });

  it("a single-layer delete invalidates the cached array (no stale return)", () => {
    const cache = new LayerCache({ backend: noopBackend });
    const caseId = "case-A";
    cache.mergeSnapshot(caseId, [mkLayer("L1"), mkLayer("L2")], {
      authoritativeReplace: true,
    });
    cache.deleteLayer(caseId, "L1");
    // A subsequent identical-to-remaining heartbeat must reflect the deletion,
    // not hand back a cached array that still contains L1.
    const after = cache.mergeSnapshot(caseId, [mkLayer("L2")], {
      authoritativeReplace: true,
    });
    expect(after.map((l) => l.layer_id)).toEqual(["L2"]);
  });
});

describe("LayerCache — view-override durability", () => {
  it("setOverride remembers opacity / visibility / zIndex and re-supplies them", () => {
    const cache = new LayerCache({ backend: noopBackend });
    const caseId = "case-A";
    cache.mergeSnapshot(caseId, [mkLayer("L1")], {
      authoritativeReplace: true,
    });
    cache.setOverride(caseId, "L1", { opacity: 0.3 });
    cache.setOverride(caseId, "L1", { visible: false });
    cache.setOverride(caseId, "L1", { zIndex: 7 });
    // Partial merges accumulate (each field set independently).
    expect(cache.getOverride(caseId, "L1")).toEqual({
      opacity: 0.3,
      visible: false,
      zIndex: 7,
    });
  });

  it("the override SURVIVES a re-render (a fresh authoritative snapshot re-add)", () => {
    const cache = new LayerCache({ backend: noopBackend });
    const caseId = "case-A";
    cache.mergeSnapshot(caseId, [mkLayer("L1", { opacity: 1, visible: true })], {
      authoritativeReplace: true,
    });
    cache.setOverride(caseId, "L1", { opacity: 0.25, visible: false });
    // Simulate the server re-shipping the layer with default opacity/visibility
    // (the server keeps no per-user view state) — the OVERRIDE must persist.
    cache.mergeSnapshot(caseId, [mkLayer("L1", { opacity: 1, visible: true })], {
      authoritativeReplace: true,
    });
    expect(cache.getOverride(caseId, "L1")).toEqual({
      opacity: 0.25,
      visible: false,
    });
  });

  it("opacity overrides are clamped to [0,1]", () => {
    const cache = new LayerCache({ backend: noopBackend });
    cache.setOverride("c", "L1", { opacity: 1.7 });
    expect(cache.getOverride("c", "L1")?.opacity).toBe(1);
    cache.setOverride("c", "L1", { opacity: -2 });
    expect(cache.getOverride("c", "L1")?.opacity).toBe(0);
  });

  it("overrides survive evictCase via the persisted backend (re-open restores)", async () => {
    const backend = makeMockBackend();
    const cache = new LayerCache({ backend });
    cache.setOverride("case-A", "L1", { opacity: 0.5, visible: false });
    // Evicting the in-memory entry must NOT wipe the persisted override.
    cache.evictCase("case-A");
    expect(cache.getOverride("case-A", "L1")).toBeUndefined(); // gone in-memory
    // A fresh cache hydrates the persisted blob -> the edit is restored.
    const fresh = new LayerCache({ backend });
    await fresh.hydrate();
    expect(fresh.getOverride("case-A", "L1")).toEqual({
      opacity: 0.5,
      visible: false,
    });
  });
});

describe("LayerCache — LRU eviction", () => {
  it("caps the in-memory entry count at maxCases (default 2)", () => {
    const cache = new LayerCache({ backend: noopBackend, maxCases: 2 });
    cache.mergeSnapshot("c1", [mkLayer("L1")], { authoritativeReplace: true });
    cache.mergeSnapshot("c2", [mkLayer("L2")], { authoritativeReplace: true });
    cache.mergeSnapshot("c3", [mkLayer("L3")], { authoritativeReplace: true });
    // c1 is the least-recently-used -> evicted; c2 + c3 remain.
    expect(cache.layersFor("c1")).toEqual([]);
    expect(cache.layersFor("c2").map((l) => l.layer_id)).toEqual(["L2"]);
    expect(cache.layersFor("c3").map((l) => l.layer_id)).toEqual(["L3"]);
  });

  it("touching a Case (layersFor) keeps it MRU so it is not evicted first", () => {
    const cache = new LayerCache({ backend: noopBackend, maxCases: 2 });
    cache.mergeSnapshot("c1", [mkLayer("L1")], { authoritativeReplace: true });
    cache.mergeSnapshot("c2", [mkLayer("L2")], { authoritativeReplace: true });
    // Re-touch c1 so it becomes MRU; c2 is now the LRU.
    cache.layersFor("c1");
    cache.mergeSnapshot("c3", [mkLayer("L3")], { authoritativeReplace: true });
    expect(cache.layersFor("c1").map((l) => l.layer_id)).toEqual(["L1"]);
    expect(cache.layersFor("c2")).toEqual([]); // evicted
    expect(cache.layersFor("c3").map((l) => l.layer_id)).toEqual(["L3"]);
  });
});

describe("LayerCache — IndexedDB override round-trip (mock idb backend)", () => {
  it("persists every setOverride and re-loads them into a fresh cache", async () => {
    const backend = makeMockBackend();
    const a = new LayerCache({ backend });
    a.setOverride("case-A", "L1", { opacity: 0.2 });
    a.setOverride("case-A", "L2", { visible: false, zIndex: 3 });
    a.setOverride("case-B", "L9", { opacity: 0.9 });

    // The blob is mirrored to the backend (deep-cloned, multi-case).
    expect(backend.saves).toBeGreaterThan(0);
    expect(backend.store["case-A"]?.["L1"]).toEqual({ opacity: 0.2 });
    expect(backend.store["case-A"]?.["L2"]).toEqual({
      visible: false,
      zIndex: 3,
    });
    expect(backend.store["case-B"]?.["L9"]).toEqual({ opacity: 0.9 });

    // A brand-new cache over the same backend restores everything on hydrate.
    const b = new LayerCache({ backend });
    await b.hydrate();
    expect(b.getOverride("case-A", "L1")).toEqual({ opacity: 0.2 });
    expect(b.getOverride("case-A", "L2")).toEqual({ visible: false, zIndex: 3 });
    expect(b.getOverride("case-B", "L9")).toEqual({ opacity: 0.9 });
  });

  it("a delete removes the persisted override too (no resurrection)", async () => {
    const backend = makeMockBackend();
    const a = new LayerCache({ backend });
    a.mergeSnapshot("case-A", [mkLayer("L1"), mkLayer("L2")], {
      authoritativeReplace: true,
    });
    a.setOverride("case-A", "L1", { opacity: 0.1 });
    a.setOverride("case-A", "L2", { opacity: 0.2 });
    a.deleteLayer("case-A", "L1");
    expect(backend.store["case-A"]?.["L1"]).toBeUndefined();
    expect(backend.store["case-A"]?.["L2"]).toEqual({ opacity: 0.2 });

    const b = new LayerCache({ backend });
    await b.hydrate();
    expect(b.getOverride("case-A", "L1")).toBeUndefined();
    expect(b.getOverride("case-A", "L2")).toEqual({ opacity: 0.2 });
  });

  it("hydrate ignores garbage / malformed persisted blobs (best-effort)", async () => {
    const backend: OverridePersistenceBackend = {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      async load() {
        return {
          "case-A": {
            L1: { opacity: "nope", visible: 1, junk: true } as unknown as never,
            L2: { visible: true },
          },
          bad: "not-an-object" as unknown as never,
        } as PersistedOverrides;
      },
      async save() {
        /* no-op */
      },
    };
    const cache = new LayerCache({ backend });
    await cache.hydrate();
    // L1's fields were all the wrong type -> dropped entirely.
    expect(cache.getOverride("case-A", "L1")).toBeUndefined();
    // L2's valid boolean survives.
    expect(cache.getOverride("case-A", "L2")).toEqual({ visible: true });
    // The non-object case entry is ignored.
    expect(cache.layersFor("bad")).toEqual([]);
  });

  it("hydrate is idempotent + never throws on a rejecting backend", async () => {
    const backend: OverridePersistenceBackend = {
      async load() {
        throw new Error("boom");
      },
      async save() {
        throw new Error("boom");
      },
    };
    const cache = new LayerCache({ backend });
    await expect(cache.hydrate()).resolves.toBeUndefined();
    await expect(cache.hydrate()).resolves.toBeUndefined(); // idempotent
    // A setOverride with a throwing save must not bubble.
    expect(() => cache.setOverride("c", "L1", { opacity: 0.5 })).not.toThrow();
    expect(cache.getOverride("c", "L1")).toEqual({ opacity: 0.5 });
  });
});

describe("LayerCache — shared singleton accessor", () => {
  beforeEach(() => {
    // Reset the process-global to a backend-free instance per test.
    setLayerCache(new LayerCache({ backend: noopBackend }));
  });

  it("getLayerCache returns the same instance across calls", () => {
    expect(getLayerCache()).toBe(getLayerCache());
  });

  it("setLayerCache swaps the shared instance", () => {
    const replacement = new LayerCache({ backend: noopBackend });
    setLayerCache(replacement);
    expect(getLayerCache()).toBe(replacement);
  });

  it("the shared cache tracks an activeCaseId field App keeps in lockstep", () => {
    const cache = getLayerCache();
    expect(cache.activeCaseId).toBeNull();
    cache.activeCaseId = "case-Z";
    expect(cache.activeCaseId).toBe("case-Z");
  });
});
