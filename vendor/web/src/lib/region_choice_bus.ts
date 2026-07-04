// GRACE-2 web — region-choice sync bus (mirrors the agent's region-choice flow).
//
// The region-disambiguation picker is BOTH-synced: the candidate regions render
// as an in-chat card LIST (owned by Chat.tsx — it also owns the WebSocket reply)
// AND as a tappable county CHOROPLETH on the map (owned by Map.tsx). Hovering /
// selecting a region in EITHER surface must highlight the polygon in the OTHER,
// and tapping a polygon must select it exactly like clicking the list row.
//
// Chat and Map are sibling React subtrees with no shared parent state owner
// (Map is fed by a LayerPanelBus; Chat owns its own GraceWs). To sync them
// without a giant prop lift, this module is a tiny module-level pub/sub bus —
// the SAME pattern ws.ts uses for the per-session fan-out hub (SESSION_HUB).
//
// One ACTIVE region-choice request at a time (the agent pauses the turn on a
// single request_id). The bus holds:
//
//   - ``request``  — the active RegionChoiceRequestPayload (null = no active
//                    pick; the choropleth is cleaned up). Published by Chat when
//                    a region-choice-request arrives, cleared when it resolves.
//   - ``hoveredRegionId`` — the region the user is hovering in EITHER surface
//                    (card row or map polygon). Drives the highlight on both.
//   - ``selectedRegionId`` — the region the user has clicked/tapped (pre-commit
//                    selection echo so the card row + polygon both show the
//                    chosen state until the reply lands and clears ``request``).
//
// Map TAPS publish a ``pick`` intent (region_id) the bus relays to Chat, which
// owns the WebSocket and sends the region-choice-provided reply (so the map
// tap and the card-row click funnel through the exact same Chat handler — one
// reply path, one re-resolution by region_id, no forked logic).
//
// The bus is a passive event emitter; subscribers are the mounted Chat + Map.
// Everything is keyed to the request's ``request_id`` (the agent's correlation
// id) so a stale subscriber update for a superseded request is a no-op.

import type { RegionChoiceRequestPayload } from "../contracts";

/** The shared, synchronized region-choice state both surfaces render from. */
export interface RegionChoiceBusState {
  /** The active request (null = no pick in flight; choropleth cleared). */
  request: RegionChoiceRequestPayload | null;
  /** Region hovered in either surface (null = none). */
  hoveredRegionId: string | null;
  /** Region clicked/tapped in either surface (pre-reply echo; null = none). */
  selectedRegionId: string | null;
}

export type RegionChoiceListener = (state: RegionChoiceBusState) => void;

/**
 * A map TAP funnels back to Chat (the WebSocket owner) through this listener so
 * the reply path is identical to a card-row click. ``regionId`` is the tapped
 * candidate's ``region_id``; Chat re-resolves it against the active request's
 * candidate set, sends ``region-choice-provided``, and resolves the card.
 */
export type RegionPickListener = (regionId: string) => void;

const INITIAL_STATE: RegionChoiceBusState = {
  request: null,
  hoveredRegionId: null,
  selectedRegionId: null,
};

class RegionChoiceBus {
  private state: RegionChoiceBusState = { ...INITIAL_STATE };
  private listeners = new Set<RegionChoiceListener>();
  private pickListeners = new Set<RegionPickListener>();

  /** Current snapshot (immutable copy). */
  getState(): RegionChoiceBusState {
    return { ...this.state };
  }

  /** Subscribe to state changes. Returns an unsubscribe fn. Fires immediately
   * with the current state so a late subscriber (e.g. Map mounting after the
   * request arrived) paints the choropleth without waiting for the next emit. */
  subscribe(fn: RegionChoiceListener): () => void {
    this.listeners.add(fn);
    fn(this.getState());
    return () => {
      this.listeners.delete(fn);
    };
  }

  /** Subscribe to map-tap pick intents (Chat owns the reply). Returns unsub. */
  subscribePick(fn: RegionPickListener): () => void {
    this.pickListeners.add(fn);
    return () => {
      this.pickListeners.delete(fn);
    };
  }

  /** Chat publishes the active request when a region-choice-request arrives. */
  setRequest(request: RegionChoiceRequestPayload | null): void {
    // New / cleared request resets the transient hover + selection.
    this.state = {
      request,
      hoveredRegionId: null,
      selectedRegionId: null,
    };
    this.emit();
  }

  /** Clear the active request iff it matches ``requestId`` (resolve path). A
   * stale clear for a superseded request is ignored so a late reply can't wipe
   * a freshly-arrived second request. */
  clearRequest(requestId: string): void {
    if (this.state.request?.request_id !== requestId) return;
    this.state = { ...INITIAL_STATE };
    this.emit();
  }

  /** Set the hovered region (from card row OR map polygon). null clears. */
  setHovered(regionId: string | null): void {
    if (this.state.hoveredRegionId === regionId) return;
    this.state = { ...this.state, hoveredRegionId: regionId };
    this.emit();
  }

  /** Set the pre-reply selected region echo (from card row OR map polygon). */
  setSelected(regionId: string | null): void {
    if (this.state.selectedRegionId === regionId) return;
    this.state = { ...this.state, selectedRegionId: regionId };
    this.emit();
  }

  /** Map TAP → relay a pick intent to Chat (the WebSocket reply owner). Also
   * sets the selection echo so the polygon + card row reflect the tap instantly,
   * before the reply round-trips. */
  pickRegion(regionId: string): void {
    this.setSelected(regionId);
    for (const fn of this.pickListeners) fn(regionId);
  }

  private emit(): void {
    const snapshot = this.getState();
    for (const fn of this.listeners) fn(snapshot);
  }

  /** Test-only full reset. */
  __reset(): void {
    this.state = { ...INITIAL_STATE };
    this.listeners.clear();
    this.pickListeners.clear();
  }
}

/** Singleton — both Chat and Map import this same instance. */
export const regionChoiceBus = new RegionChoiceBus();
