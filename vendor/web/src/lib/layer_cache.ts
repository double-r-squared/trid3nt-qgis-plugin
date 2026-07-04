// GRACE-2 web — per-Case client layer cache + view-state durability ("the
// seatbelt").
//
// GOAL (NATE decision): a WS reconnect, an iOS zombie-socket re-pull, or a
// stale-but-non-empty server snapshot must NEVER blank a Case's rendered
// layers, nor reset the user's per-layer opacity / visibility / z-order. Only
// an EXPLICIT case switch / case exit / per-layer delete may evict.
//
// SCOPE (NATE decision):
//   - The LAYER SET (the list of ProjectLayerSummary for a Case) is held
//     IN-MEMORY only. It is cheaply re-derived from the agent's authoritative
//     session-state on every reconnect, and persisting it risks resurrecting a
//     server-deleted layer on the next cold load — so it is deliberately NOT
//     persisted.
//   - The VIEW-OVERRIDES (per-layer {opacity, visible, zIndex} the USER set
//     via the LayerPanel) ARE persisted to IndexedDB (best-effort, never
//     throws) so they survive a full page reload, not just a WS blip.
//
// This module is PURE (no React, no MapLibre) and unit-testable. The IndexedDB
// side-effect is funnelled through a small injectable backend so the persistence
// path can be exercised in tests without a real IndexedDB (happy-dom has none).
//
// This SUBSUMES the older localStorage `grace2.layerVisibility` override map
// (LayerPanel.readLayerVisibilityOverrides): the cache is now the single source
// of truth for view-overrides. The localStorage seam is kept working for
// backward compatibility (callers that still read it), but new write-through
// flows through here.

import {
  compareLayersTopFirst,
  type ProjectLayerSummary,
  type TemporalConfig,
} from "../contracts";

/** A user-set view override for one layer. All fields optional / partial. */
export interface LayerViewOverride {
  opacity?: number;
  visible?: boolean;
  zIndex?: number;
}

/** The persisted shape: caseId -> (layerId -> override). */
export type PersistedOverrides = Record<
  string,
  Record<string, LayerViewOverride>
>;

// --- IndexedDB persistence backend (injectable) ------------------------- //
//
// A minimal async key/value contract over the per-case override blob. The
// default implementation talks to the native `indexedDB`; tests inject a fake.
// EVERY method is best-effort and MUST NOT throw — a rejected/absent backend
// degrades the cache to in-memory only.

export interface OverridePersistenceBackend {
  /** Load the full caseId -> overrides blob. Resolves {} on any failure. */
  load(): Promise<PersistedOverrides>;
  /** Persist the full caseId -> overrides blob. Resolves (never rejects). */
  save(all: PersistedOverrides): Promise<void>;
}

const IDB_NAME = "grace2-layer-cache";
const IDB_STORE = "view-overrides";
const IDB_KEY = "overrides";
const IDB_VERSION = 1;

/** True when a usable native IndexedDB is present (browser, not happy-dom). */
function hasIndexedDb(): boolean {
  return (
    typeof indexedDB !== "undefined" &&
    indexedDB !== null &&
    typeof indexedDB.open === "function"
  );
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    let req: IDBOpenDBRequest;
    try {
      req = indexedDB.open(IDB_NAME, IDB_VERSION);
    } catch (err) {
      reject(err);
      return;
    }
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(IDB_STORE)) {
        db.createObjectStore(IDB_STORE);
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
    req.onblocked = () => reject(new Error("idb open blocked"));
  });
}

/**
 * Default backend over the native IndexedDB. All operations swallow errors and
 * degrade to a no-op / empty result so the cache never throws on a private-mode
 * / quota-exceeded / missing-IDB browser.
 */
export const indexedDbOverrideBackend: OverridePersistenceBackend = {
  async load(): Promise<PersistedOverrides> {
    if (!hasIndexedDb()) return {};
    try {
      const db = await openDb();
      return await new Promise<PersistedOverrides>((resolve) => {
        let tx: IDBTransaction;
        try {
          tx = db.transaction(IDB_STORE, "readonly");
        } catch {
          resolve({});
          return;
        }
        const getReq = tx.objectStore(IDB_STORE).get(IDB_KEY);
        getReq.onsuccess = () => {
          const val = getReq.result as unknown;
          resolve(sanitizePersisted(val));
        };
        getReq.onerror = () => resolve({});
        tx.oncomplete = () => db.close();
      });
    } catch {
      return {};
    }
  },
  async save(all: PersistedOverrides): Promise<void> {
    if (!hasIndexedDb()) return;
    try {
      const db = await openDb();
      await new Promise<void>((resolve) => {
        let tx: IDBTransaction;
        try {
          tx = db.transaction(IDB_STORE, "readwrite");
        } catch {
          resolve();
          return;
        }
        tx.objectStore(IDB_STORE).put(all, IDB_KEY);
        tx.oncomplete = () => {
          db.close();
          resolve();
        };
        tx.onerror = () => resolve();
        tx.onabort = () => resolve();
      });
    } catch {
      /* best-effort — never throws */
    }
  },
};

/** Coerce an unknown persisted value into a well-typed PersistedOverrides. */
function sanitizePersisted(val: unknown): PersistedOverrides {
  if (!val || typeof val !== "object" || Array.isArray(val)) return {};
  const out: PersistedOverrides = {};
  for (const [caseId, perLayer] of Object.entries(
    val as Record<string, unknown>,
  )) {
    if (!perLayer || typeof perLayer !== "object" || Array.isArray(perLayer)) {
      continue;
    }
    const layers: Record<string, LayerViewOverride> = {};
    for (const [layerId, ov] of Object.entries(
      perLayer as Record<string, unknown>,
    )) {
      const clean = sanitizeOverride(ov);
      if (clean) layers[layerId] = clean;
    }
    if (Object.keys(layers).length > 0) out[caseId] = layers;
  }
  return out;
}

/** Keep only the recognized override fields with correct types; null if empty. */
function sanitizeOverride(ov: unknown): LayerViewOverride | null {
  if (!ov || typeof ov !== "object" || Array.isArray(ov)) return null;
  const src = ov as Record<string, unknown>;
  const out: LayerViewOverride = {};
  if (typeof src.opacity === "number" && Number.isFinite(src.opacity)) {
    out.opacity = clamp01(src.opacity);
  }
  if (typeof src.visible === "boolean") out.visible = src.visible;
  if (typeof src.zIndex === "number" && Number.isFinite(src.zIndex)) {
    out.zIndex = src.zIndex;
  }
  return Object.keys(out).length > 0 ? out : null;
}

function clamp01(x: number): number {
  if (Number.isNaN(x)) return 0;
  if (x < 0) return 0;
  if (x > 1) return 1;
  return x;
}

// --- Per-case in-memory entry ------------------------------------------- //

interface CaseEntry {
  /** layerId -> the most-recent ProjectLayerSummary seen for this Case. */
  layers: Map<string, ProjectLayerSummary>;
  /** layerId -> the user's view override (opacity / visible / zIndex). */
  overrides: Map<string, LayerViewOverride>;
  /**
   * FLASH FIX (Lane 1a): the LAST array instance mergeSnapshot returned for this
   * Case. When a fresh snapshot resolves to a structurally-identical set we hand
   * back THIS same reference so App's `setLayers` is a no-op (React bails) and the
   * panel/scrubber subtree does not re-render on every ~25s keepalive heartbeat.
   * Invalidated (set null) whenever the tracked set actually changes.
   */
  lastReturned: ProjectLayerSummary[] | null;
}

function emptyEntry(): CaseEntry {
  return { layers: new Map(), overrides: new Map(), lastReturned: null };
}

/**
 * FLASH FIX (Lane 1a): structural equality of two ProjectLayerSummary objects by
 * every field the renderer reads. When an incoming layer matches the stored one
 * by value we KEEP the stored object ref (do not `set` a new one), so identical
 * heartbeats never churn object identity downstream. Mirrors LayerPanel's
 * layerSetsEqual field set, plus the remaining display fields.
 */
function layersStructurallyEqual(
  a: ProjectLayerSummary,
  b: ProjectLayerSummary,
): boolean {
  return (
    a.layer_id === b.layer_id &&
    a.name === b.name &&
    a.layer_type === b.layer_type &&
    a.uri === b.uri &&
    (a.wms_url ?? null) === (b.wms_url ?? null) &&
    (a.attribution ?? null) === (b.attribution ?? null) &&
    a.visible === b.visible &&
    a.opacity === b.opacity &&
    a.z_index === b.z_index &&
    (a.style_preset ?? null) === (b.style_preset ?? null) &&
    temporalEqual(a.temporal ?? null, b.temporal ?? null)
  );
}

function temporalEqual(
  a: TemporalConfig | null,
  b: TemporalConfig | null,
): boolean {
  if (a === b) return true;
  if (a === null || b === null) return false;
  return (
    a.start === b.start &&
    a.end === b.end &&
    a.step_seconds === b.step_seconds
  );
}

// --- LayerCache --------------------------------------------------------- //

export interface LayerCacheOptions {
  /** Max number of distinct cases held in memory (LRU). Default 2. */
  maxCases?: number;
  /** Persistence backend for view-overrides. Default = native IndexedDB. */
  backend?: OverridePersistenceBackend;
}

/**
 * Per-Case layer + view-override cache. LRU-bounded in memory (default 2
 * cases). The layer SET is in-memory only; the view-overrides are mirrored to
 * the injected persistence backend (best-effort).
 */
export class LayerCache {
  private readonly entries = new Map<string, CaseEntry>();
  private readonly order: string[] = []; // LRU: most-recently-used LAST.
  private readonly maxCases: number;
  private readonly backend: OverridePersistenceBackend;
  /** Hydrated copy of the full persisted blob (kept in sync on every write). */
  private persisted: PersistedOverrides = {};
  private hydrated = false;

  constructor(opts: LayerCacheOptions = {}) {
    this.maxCases = Math.max(1, opts.maxCases ?? 2);
    this.backend = opts.backend ?? indexedDbOverrideBackend;
  }

  /**
   * Load persisted view-overrides from the backend into memory. Idempotent and
   * best-effort: a failure leaves the cache empty-but-usable. Call once at app
   * start; it MERGES into any overrides already set in-memory (in-memory wins on
   * a key collision so a user edit made before hydration is not clobbered).
   */
  async hydrate(): Promise<void> {
    if (this.hydrated) return;
    this.hydrated = true;
    let loaded: PersistedOverrides = {};
    try {
      // Sanitize defensively: the default backend already cleans its result,
      // but a custom / misbehaving backend could hand back malformed entries.
      // Dropping wrong-typed fields here keeps the in-memory state well-formed.
      loaded = sanitizePersisted(await this.backend.load());
    } catch {
      loaded = {};
    }
    this.persisted = { ...loaded };
    for (const [caseId, perLayer] of Object.entries(loaded)) {
      const entry = this.ensure(caseId, /*touch*/ false);
      for (const [layerId, ov] of Object.entries(perLayer)) {
        if (!entry.overrides.has(layerId)) {
          entry.overrides.set(layerId, { ...ov });
        }
      }
    }
  }

  /** Ordered layer list for a Case (top-of-stack first). [] if unknown. */
  layersFor(caseId: string | null): ProjectLayerSummary[] {
    if (caseId == null) return [];
    const entry = this.entries.get(caseId);
    if (!entry) return [];
    this.touch(caseId);
    // BUG 2 (random-reorder): emit pre-sorted by the SHARED comparator so the
    // cache, App's `layers`, and the map agree on order BY CONSTRUCTION (rather
    // than handing back raw Map-insertion order, which differed from the panel /
    // map sort and let the same set render in three different orders).
    return Array.from(entry.layers.values()).sort(compareLayersTopFirst);
  }

  /**
   * Merge a server/session snapshot for a Case.
   *
   *   - ADDS / REFRESHES every layer in `layers` (by layer_id).
   *   - When `authoritativeReplace` is TRUE, layers ABSENT from `layers` are
   *     EVICTED (a genuine full replace — case switch / a healthy non-empty
   *     server frame). When FALSE (a stale / partial / reconnect frame), absent
   *     layers are KEPT (the seatbelt: a blip can never blank the map).
   *
   * Returns the resulting ordered layer list for convenience.
   */
  mergeSnapshot(
    caseId: string | null,
    layers: ReadonlyArray<ProjectLayerSummary>,
    opts: { authoritativeReplace: boolean },
  ): ProjectLayerSummary[] {
    if (caseId == null) {
      // Untagged/root frame — no Case to cache against; pass through verbatim.
      return Array.from(layers);
    }
    const entry = this.ensure(caseId, /*touch*/ true);
    const incomingIds = new Set(layers.map((l) => l.layer_id));

    // FLASH FIX (Lane 1a): track whether the tracked SET actually changed (an
    // add, a remove, or a per-layer field change). When nothing changed we hand
    // back the SAME array reference we returned last time so App's setLayers is a
    // no-op (React bails) and the panel/scrubber subtree never re-renders on an
    // identical ~25s keepalive heartbeat.
    let changed = false;

    // Full replace: drop tracked layers the authoritative set omits — BUT
    // guard the cold-open hazard: an EMPTY authoritative frame for a Case that
    // already has tracked layers is a NO-OP, never a blank. (Opening a case
    // cold transiently feeds an empty/short session-state frame; without this
    // gate it would evict every populated layer.) An empty frame is still
    // honored when the Case has nothing tracked yet (an EMPTY case stays
    // empty), and a NON-empty frame still evicts omitted layers as before.
    // Genuine clears (case exit / explicit delete / real switch) go through
    // evictCase / deleteLayer directly, so guarding the omission-evict is safe.
    if (
      opts.authoritativeReplace &&
      (layers.length > 0 || entry.layers.size === 0)
    ) {
      for (const id of Array.from(entry.layers.keys())) {
        if (!incomingIds.has(id)) {
          entry.layers.delete(id);
          changed = true;
        }
      }
    }
    // Additive in both modes: add / refresh every incoming layer. IDENTITY-STABLE
    // (Lane 1a): when an incoming layer is structurally identical to the stored
    // one KEEP the existing object ref so a byte-identical heartbeat does not
    // create new object refs downstream; only replace the ref (and mark changed)
    // when a field the renderer reads actually differs or the layer is new.
    for (const layer of layers) {
      const existing = entry.layers.get(layer.layer_id);
      if (existing && layersStructurallyEqual(existing, layer)) {
        continue; // keep the stored ref - no churn.
      }
      entry.layers.set(layer.layer_id, layer);
      changed = true;
    }

    // When the set is UNCHANGED return the previously-returned array instance so
    // the caller's ref-equality check (and React's setState bail) short-circuits.
    if (!changed && entry.lastReturned !== null) {
      return entry.lastReturned;
    }
    // BUG 2 (random-reorder): RETURN the values pre-sorted by the SHARED
    // comparator (z_index desc, layer_id tiebreak) so App's `layers` and the map
    // overlay stack agree on order by construction. Map-insertion order differed
    // from the panel / map sort, which - combined with the agent's null z_index -
    // let the same set render in three different orders.
    const out = Array.from(entry.layers.values()).sort(compareLayersTopFirst);
    entry.lastReturned = out;
    return out;
  }

  /**
   * The teardown gate. Returns TRUE only when `layerId` may be evicted from the
   * map for this Case. Snapshot OMISSION alone is NEVER a reason to evict — only
   * an explicit case-switch / case-delete (which call evictCase / the
   * authoritative mergeSnapshot above, removing the entry from `layers`).
   *
   * Concretely: a layer is evictable iff the cache no longer tracks it for the
   * active Case (it was authoritatively removed). If it is still tracked, an
   * omission from some partial frame must NOT tear it down.
   */
  allowsEvict(caseId: string | null, layerId: string): boolean {
    if (caseId == null) return true; // root view — no durable Case to protect.
    const entry = this.entries.get(caseId);
    if (!entry) return true; // Case not cached (already evicted) — safe to tear.
    return !entry.layers.has(layerId);
  }

  /** Read the user's view override for a layer (undefined if none). */
  getOverride(
    caseId: string | null,
    layerId: string,
  ): LayerViewOverride | undefined {
    if (caseId == null) return undefined;
    const entry = this.entries.get(caseId);
    return entry?.overrides.get(layerId);
  }

  /** All overrides for a Case as a plain {layerId: override} record. */
  overridesFor(caseId: string | null): Record<string, LayerViewOverride> {
    if (caseId == null) return {};
    const entry = this.entries.get(caseId);
    if (!entry) return {};
    const out: Record<string, LayerViewOverride> = {};
    for (const [id, ov] of entry.overrides) out[id] = { ...ov };
    return out;
  }

  /**
   * Record (merge) a user view override for a layer + persist it. Partial: only
   * the provided fields change; unspecified fields keep their prior value. The
   * persist is best-effort (never throws).
   */
  setOverride(
    caseId: string | null,
    layerId: string,
    partial: LayerViewOverride,
  ): void {
    if (caseId == null) return;
    const entry = this.ensure(caseId, /*touch*/ true);
    const prev = entry.overrides.get(layerId) ?? {};
    const next: LayerViewOverride = { ...prev };
    if (partial.opacity !== undefined && Number.isFinite(partial.opacity)) {
      next.opacity = clamp01(partial.opacity);
    }
    if (partial.visible !== undefined) next.visible = partial.visible;
    if (partial.zIndex !== undefined && Number.isFinite(partial.zIndex)) {
      next.zIndex = partial.zIndex;
    }
    entry.overrides.set(layerId, next);
    this.persistCase(caseId);
  }

  /**
   * Explicit eviction (case switch / case exit). Drops the whole Case entry
   * from memory. The PERSISTED overrides are intentionally KEPT so re-opening
   * the Case later restores the user's view edits across a full reload.
   */
  evictCase(caseId: string | null): void {
    if (caseId == null) return;
    this.entries.delete(caseId);
    const idx = this.order.indexOf(caseId);
    if (idx >= 0) this.order.splice(idx, 1);
  }

  /**
   * Explicit single-layer delete for a Case (the trash control). Removes the
   * layer from the tracked set AND its persisted override (so a deleted layer
   * never resurrects, nor leaves a dangling override blob).
   */
  deleteLayer(caseId: string | null, layerId: string): void {
    if (caseId == null) return;
    const entry = this.entries.get(caseId);
    if (!entry) return;
    if (entry.layers.delete(layerId)) {
      // FLASH FIX (Lane 1a): the tracked set changed - invalidate the cached
      // return array so the next mergeSnapshot rebuilds (and a no-op-return
      // can't hand back a stale array that still contains the deleted layer).
      entry.lastReturned = null;
    }
    if (entry.overrides.delete(layerId)) this.persistCase(caseId);
  }

  // --- internals -------------------------------------------------------- //

  private ensure(caseId: string, touch: boolean): CaseEntry {
    let entry = this.entries.get(caseId);
    if (!entry) {
      entry = emptyEntry();
      this.entries.set(caseId, entry);
      this.order.push(caseId);
      this.evictLruIfNeeded();
    }
    if (touch) this.touch(caseId);
    return entry;
  }

  private touch(caseId: string): void {
    const idx = this.order.indexOf(caseId);
    if (idx >= 0) this.order.splice(idx, 1);
    this.order.push(caseId);
  }

  private evictLruIfNeeded(): void {
    while (this.order.length > this.maxCases) {
      const lru = this.order.shift();
      if (lru !== undefined) this.entries.delete(lru);
      // NOTE: LRU eviction drops the IN-MEMORY entry only; the persisted
      // overrides for that Case remain on disk so re-opening it later (after a
      // cold load) restores the user's view edits.
    }
  }

  /**
   * The Case the UI currently considers active. App.tsx keeps this in lockstep
   * with `activeCaseId` so the bus-subscribing consumers (Map.tsx, LayerPanel)
   * — which have no caseId prop of their own — can resolve the right Case for
   * allowsEvict / getOverride without threading caseId through the bus payload.
   * null at the root (no Case open).
   */
  activeCaseId: string | null = null;

  /** Mirror one Case's overrides into the persisted blob + flush best-effort. */
  private persistCase(caseId: string): void {
    const entry = this.entries.get(caseId);
    if (!entry) return;
    if (entry.overrides.size === 0) {
      delete this.persisted[caseId];
    } else {
      const rec: Record<string, LayerViewOverride> = {};
      for (const [id, ov] of entry.overrides) rec[id] = { ...ov };
      this.persisted[caseId] = rec;
    }
    // Snapshot the blob so a later in-memory mutation can't race the async save.
    const snapshot: PersistedOverrides = {};
    for (const [cid, rec] of Object.entries(this.persisted)) {
      snapshot[cid] = { ...rec };
    }
    void this.backend.save(snapshot).catch(() => {
      /* best-effort — never throws */
    });
  }
}

// --- Shared singleton --------------------------------------------------- //
//
// The layer SET + view-overrides cache is process-global so the three
// bus-subscribing surfaces (App.tsx orchestrates, Map.tsx reconciles,
// LayerPanel edits) all read/write the SAME instance without threading it
// through the LayerPanelBus payload contract. Created lazily on first access so
// a test can replace it (setLayerCache) before App mounts.

let sharedCache: LayerCache | null = null;

/** The process-global LayerCache. Lazily created with default options. */
export function getLayerCache(): LayerCache {
  if (sharedCache === null) sharedCache = new LayerCache();
  return sharedCache;
}

/** Replace the process-global LayerCache (tests / explicit re-init). */
export function setLayerCache(cache: LayerCache): void {
  sharedCache = cache;
}
