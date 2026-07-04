// GRACE-2 web - staged-AOI bus (NATE map/loading-UX polish, item 4).
//
// The ALWAYS-ON "Draw AOI" map control lets the user draw a bbox rectangle on
// the live map AT ANY TIME (not only during a #170 case-create or an agent-
// requested spatial-input). The drawn box STAGES as the analysis extent for the
// NEXT prompt - non-destructive, nothing runs until the user actually prompts -
// and is easily cleared. This module is the module-level pub/sub that carries
// that staged state between the Map subtree (which owns the control + the draw
// gesture + the on-map staged rectangle) and the Chat subtree (which owns the
// outbound prompt and reads the staged bbox on the next send).
//
// Mirrors region_choice_bus / spatial_input_bus: a passive event emitter with a
// stable singleton. It is REQUEST-FREE (no agent round-trip; the box may be
// asleep) - unlike spatial_input_bus, which is driven by an agent request.
//
// NO-CLOBBER (NATE): the draw gesture is armed ONLY by an explicit user action
// on the control (setArmed(true)); it is never an ambient free-draw, so an
// LLM-set AOI / a plain map drag is never clobbered. The staged bbox is purely
// additive client state - it does not mutate any loaded layer or the camera.

import type { BBox } from "./bbox_draw";

/** The synchronized staged-AOI state both surfaces render from. */
export interface AoiStageBusState {
  /**
   * Whether the draw gesture is ARMED (the user tapped "Draw AOI" and is about
   * to / is dragging a rectangle). Map.tsx attaches the drag gesture while true.
   */
  armed: boolean;
  /**
   * The currently-staged analysis extent bbox [minLon,minLat,maxLon,maxLat], or
   * null when nothing is staged. Set on draw completion; cleared by the user.
   */
  bbox: BBox | null;
}

export type AoiStageListener = (state: AoiStageBusState) => void;

const INITIAL_STATE: AoiStageBusState = { armed: false, bbox: null };

class AoiStageBus {
  private state: AoiStageBusState = { ...INITIAL_STATE };
  private readonly listeners = new Set<AoiStageListener>();

  /** Current snapshot. */
  getState(): AoiStageBusState {
    return this.state;
  }

  /** Subscribe; fires immediately with the current state. Returns unsubscribe. */
  subscribe(listener: AoiStageListener): () => void {
    this.listeners.add(listener);
    listener(this.state);
    return () => {
      this.listeners.delete(listener);
    };
  }

  private emit(): void {
    for (const l of this.listeners) l(this.state);
  }

  /** Arm / disarm the draw gesture (the control toggles this). */
  setArmed(armed: boolean): void {
    if (this.state.armed === armed) return;
    this.state = { ...this.state, armed };
    this.emit();
  }

  /**
   * Stage a drawn bbox as the next-prompt analysis extent. Disarms the gesture
   * (the draw is complete). A null clears the staged extent.
   */
  setBbox(bbox: BBox | null): void {
    this.state = { armed: false, bbox };
    this.emit();
  }

  /** Clear the staged extent AND disarm (the user tapped "Clear" / cancelled). */
  clear(): void {
    if (!this.state.armed && this.state.bbox === null) return;
    this.state = { armed: false, bbox: null };
    this.emit();
  }
}

/** Process-global singleton (mirrors region_choice_bus / spatial_input_bus). */
export const aoiStageBus = new AoiStageBus();
