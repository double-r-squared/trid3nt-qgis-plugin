// GRACE-2 web — spatial-input sync bus (FR-WC-13 pick-mode + FR-WC-16 urban
// vector-draw). Mirrors `region_choice_bus.ts` — the proven module-level
// pub/sub pattern for syncing the Chat subtree (which owns the WebSocket reply)
// with the Map subtree (which owns the on-map pick / draw surface).
//
// The spatial-input flow is BOTH-synced exactly like region-choice:
//
//   - The agent emits a `spatial-input-request` (server -> client). Chat.tsx
//     receives it (session-scoped fan-out, ws.ts SESSION_SCOPED_TYPES), renders
//     an inline prompt CARD, and publishes the request to THIS bus.
//   - Map.tsx subscribes to the bus and, on a non-null request, enters the
//     matching surface:
//       * mode "point"       -> single-click pick mode (one marker).
//       * mode "bbox"        -> drag-rectangle pick mode.
//       * mode "vector_draw" -> the terra-draw surface (rectangle / polygon /
//                               polyline + select-edit + segment tagging).
//   - The user completes the pick / draw on the MAP. Map.tsx writes the result
//     into the bus (`setResult`) — a flat coordinates list for point/bbox, or a
//     role-tagged GeoJSON FeatureCollection for vector_draw — and the bus relays
//     a `submit` (or `cancel`) intent to Chat.tsx, which OWNS the WebSocket and
//     sends the `spatial-input-response` reply. So the map submit and any
//     chat-side affordance funnel through the SAME Chat reply path (one
//     re-resolution by request_id, no forked WS logic) — mirroring how a map
//     county-tap and a card-row click both funnel through Chat in region-choice.
//
// One ACTIVE spatial-input request at a time (the agent pauses the turn on a
// single request_id). Everything is keyed to `request_id` so a stale update for
// a superseded request is a no-op.
//
// The bus is a passive event emitter; subscribers are the mounted Chat + Map.

import type {
  SpatialDrawFeatureCollection,
  SpatialInputRequestPayload,
} from "../contracts";

/** The result a completed pick / draw carries back to Chat (the reply owner). */
export interface SpatialInputResult {
  /** Echoes the active request's request_id. */
  requestId: string;
  /** Mirrors the request mode (drives which response fields Chat sends). */
  geometryType: "point" | "bbox" | "vector_draw";
  /** point=[lon,lat]; bbox=[minLon,minLat,maxLon,maxLat]. Null for vector_draw. */
  coordinates: number[] | null;
  /** Drawn role-tagged FeatureCollection. Null for point/bbox. */
  features: SpatialDrawFeatureCollection | null;
}

/** The shared, synchronized spatial-input state both surfaces render from. */
export interface SpatialInputBusState {
  /** The active request (null = no pick/draw in flight; surface torn down). */
  request: SpatialInputRequestPayload | null;
}

export type SpatialInputListener = (state: SpatialInputBusState) => void;

/**
 * A completed pick / draw funnels back to Chat (the WebSocket owner) through
 * this listener so the reply path stays single-sourced. Chat sends
 * `spatial-input-response` with the carried geometry and resolves the card.
 */
export type SpatialInputSubmitListener = (result: SpatialInputResult) => void;

/**
 * A cancellation (the user dismissed the on-map surface) funnels back to Chat so
 * it sends `spatial-input-response` with `cancelled=true` (Invariant 8 —
 * cancellation is first-class) and folds the card.
 */
export type SpatialInputCancelListener = (requestId: string) => void;

const INITIAL_STATE: SpatialInputBusState = {
  request: null,
};

class SpatialInputBus {
  private state: SpatialInputBusState = { ...INITIAL_STATE };
  private listeners = new Set<SpatialInputListener>();
  private submitListeners = new Set<SpatialInputSubmitListener>();
  private cancelListeners = new Set<SpatialInputCancelListener>();

  /** Current snapshot (immutable copy). */
  getState(): SpatialInputBusState {
    return { ...this.state };
  }

  /** Subscribe to state changes. Returns an unsubscribe fn. Fires immediately
   * with the current state so a late subscriber (e.g. Map mounting after the
   * request arrived) opens the surface without waiting for the next emit. */
  subscribe(fn: SpatialInputListener): () => void {
    this.listeners.add(fn);
    fn(this.getState());
    return () => {
      this.listeners.delete(fn);
    };
  }

  /** Subscribe to a map-side SUBMIT (Chat owns the reply). Returns unsub. */
  subscribeSubmit(fn: SpatialInputSubmitListener): () => void {
    this.submitListeners.add(fn);
    return () => {
      this.submitListeners.delete(fn);
    };
  }

  /** Subscribe to a map-side CANCEL (Chat owns the reply). Returns unsub. */
  subscribeCancel(fn: SpatialInputCancelListener): () => void {
    this.cancelListeners.add(fn);
    return () => {
      this.cancelListeners.delete(fn);
    };
  }

  /** Chat publishes the active request when a spatial-input-request arrives. */
  setRequest(request: SpatialInputRequestPayload | null): void {
    this.state = { request };
    this.emit();
  }

  /** Clear the active request iff it matches `requestId` (resolve path). A stale
   * clear for a superseded request is ignored so a late reply can't wipe a
   * freshly-arrived second request. */
  clearRequest(requestId: string): void {
    if (this.state.request?.request_id !== requestId) return;
    this.state = { ...INITIAL_STATE };
    this.emit();
  }

  /**
   * Map -> relay a completed pick / draw to Chat (the WebSocket reply owner).
   * Ignored when there is no active request OR the result is for a stale
   * request_id (a late submit for a superseded request).
   */
  submit(result: SpatialInputResult): void {
    if (this.state.request?.request_id !== result.requestId) return;
    for (const fn of this.submitListeners) fn(result);
  }

  /**
   * Map -> relay a cancellation to Chat. Ignored for a stale request_id. The
   * caller (Chat) clears the request after sending the cancel reply.
   */
  cancel(requestId: string): void {
    if (this.state.request?.request_id !== requestId) return;
    for (const fn of this.cancelListeners) fn(requestId);
  }

  private emit(): void {
    const snapshot = this.getState();
    for (const fn of this.listeners) fn(snapshot);
  }

  /** Test-only full reset. */
  __reset(): void {
    this.state = { ...INITIAL_STATE };
    this.listeners.clear();
    this.submitListeners.clear();
    this.cancelListeners.clear();
  }
}

/** Singleton — both Chat and Map import this same instance. */
export const spatialInputBus = new SpatialInputBus();
