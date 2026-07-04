// GRACE-2 web - bbox progress-animation STATE MACHINE + settings persistence.
//
// NATE map/loading-UX SIMPLIFICATION (2026-06-24). Originally there were TWO
// loading visuals over the AOI bbox: a sweeping SCAN-BORDER and a FILL-GRID
// shimmer, selected per state (first-fetch / subsequent / connecting / sim).
// NATE: "just stop the scan animations for loading layers and just use the grid
// one and make sure it is polished, and only appears when there are truly no
// layers loaded."
//
// So the state machine is now DEAD SIMPLE - exactly ONE loading visual, the
// polished GRID (mode "fill"), shown ONLY when there are TRULY zero layers
// loaded for the active Case and a fetch is in progress:
//
//   - hasBbox AND layersLoading AND layerCount === 0  -> FILL (grid).
//   - everything else (>=1 layer, connecting, a running sim, idle, 3D, replay)
//     -> NONE.
//
// The moment the first layer paints (layerCount >= 1) the grid disappears. The
// "scan" cases (connecting / sim / subsequent-load) are GONE: connecting is a
// transport cue carried elsewhere (the wake/connecting chrome), and a running
// sim no longer paints a box animation. The grid never appears merely because
// the WS is connecting or a Case was just selected if layers are already loaded.
//
// This module is PURE (no React / MapLibre / DOM beyond the localStorage seam):
// `resolveBboxProgress` maps the live signals -> a render descriptor, and the
// settings helpers read/write the persisted enable flag. Everything is trivially
// unit-testable.

/**
 * The visual mode the overlay paints, or "none" when nothing animates. Only
 * "fill" (the polished grid) is ever produced now; "scan" is retained in the
 * union purely so the BboxProgressOverlay's exhaustive switch + older callers /
 * fixtures still type-check (the scan render branch is removed). The resolver
 * never returns "scan".
 */
export type BboxProgressMode = "none" | "fill" | "scan";

/** A color family kept for the descriptor shape. The grid is always "blue". */
export type BboxProgressTone = "blue" | "purple";

/** The live signals the overlay state machine reads (all owned by App). */
export interface BboxProgressSignals {
  /** Whether a geolocated AOI bbox is projected on screen (the anchor). */
  hasBbox: boolean;
  /** Count of layers currently on the map for the active Case. */
  layerCount: number;
  /** True while a Case's layers are loading (App's `layersLoading`). */
  layersLoading: boolean;
  /** True while the WS is connecting / reconnecting (transport not healthy). */
  connecting: boolean;
  /** True while a long-running simulation pipeline is in progress. */
  simRunning: boolean;
  /** The user's persisted enable flag for the loading animations. */
  animationsEnabled: boolean;
  /**
   * LANE E (3D): true when MapLibre 3D terrain is enabled. In 3D the camera is
   * pitched/rotated so the AXIS-ALIGNED 2D DOM overlay no longer traces the
   * tilted AOI box; the in-map line-layer pulse-glow (terrain_3d.ts) takes over
   * instead. So 3D suppresses the 2D overlay (mode "none") for the loading
   * states. Optional (defaults false) so existing callers / tests are unchanged.
   */
  terrain3d?: boolean;
  /**
   * LANE B #4 (no-replay): true when the active Case + bbox are UNCHANGED since
   * the last paint AND layers are already present, i.e. a re-enter / same-bbox
   * switch where nothing genuinely new is being fetched. When set, the
   * subsequent-load loading visual (fill / scan) is suppressed so the loading
   * shimmer does NOT replay over already-rendered layers. The CONNECTING cue and
   * a running SIM still show (they are real in-progress signals, not replays).
   * Optional (defaults false) so existing callers / tests are unchanged.
   */
  suppressLoadingReplay?: boolean;
}

/** The render descriptor `resolveBboxProgress` returns. */
export interface BboxProgressState {
  /** Which animation to paint ("none" hides the overlay entirely). */
  mode: BboxProgressMode;
  /** Scan-border tone (only meaningful when mode === "scan"). */
  tone: BboxProgressTone;
  /**
   * True when this state is exempt from the user enable toggle (the CONNECTING
   * scan border is always on). Surfaced so the caller / tests can assert it.
   */
  toggleExempt: boolean;
}

const NONE: BboxProgressState = { mode: "none", tone: "blue", toggleExempt: false };

/**
 * Resolve the live signals into a single render descriptor. NATE's 2026-06-24
 * simplification: there is exactly ONE loading visual, the polished GRID
 * ("fill"), and it appears ONLY when there are TRULY zero layers loaded.
 *
 *   1. No bbox anchor                -> nothing to anchor to -> none.
 *   2. 3D terrain mode               -> none (the 2D axis-aligned grid cannot
 *                                       trace a tilted box; the AOI line stays
 *                                       statically visible in 3D instead).
 *   3. animations disabled by user   -> none (the grid is decorative chrome).
 *   4. At least one layer is loaded  -> none. The grid is for the "truly no
 *                                       layers yet" state; the moment >=1 layer
 *                                       is present it disappears - regardless of
 *                                       connecting / sim / a re-fetch.
 *   5. Actively loading + zero layers-> FILL grid (the one polished visual).
 *   6. otherwise                     -> none.
 *
 * Note the previous SCAN cases (connecting / running-sim / subsequent-load) are
 * intentionally GONE: connecting is a transport cue carried by the wake chrome,
 * and a running sim no longer animates the box. `connecting` and `simRunning`
 * are still accepted in the signal shape (back-compat) but they no longer
 * produce any box animation on their own - only "truly zero layers + loading"
 * does. This also means the grid is NOT shown merely because the socket is
 * connecting or a Case was just selected when layers are already present.
 */
export function resolveBboxProgress(s: BboxProgressSignals): BboxProgressState {
  // 1. Nothing to anchor against.
  if (!s.hasBbox) return NONE;

  // 2. 3D terrain: the 2D DOM grid is axis-aligned and cannot trace a pitched /
  //    rotated AOI box, so it floats off and "looks weird". Suppress it; in 3D
  //    the real on-map AOI line layer stays statically visible (terrain_3d.ts no
  //    longer pulse-scales it), which is the cue. (No-op when 3D is off.)
  if (s.terrain3d) return NONE;

  // 3. The user turned the loading animation off (the grid is decorative).
  if (!s.animationsEnabled) return NONE;

  // 4. STRICT ZERO-LAYERS GATE (NATE 2026-06-24): the grid is ONLY for the
  //    "truly no layers loaded yet" state. The instant the active Case has >=1
  //    layer painted, the grid must disappear - even if a re-fetch / reconnect /
  //    sim is in flight. This single guard subsumes the old suppressLoadingReplay
  //    special-case: any layers-present context is a no-show.
  if (s.layerCount > 0) return NONE;

  // 5. Actively loading AND zero layers -> the polished FILL grid (the one and
  //    only loading visual). It is faint / translucent and sits inside the box.
  if (s.layersLoading) {
    return { mode: "fill", tone: "blue", toggleExempt: false };
  }

  // 6. Idle (bbox present, nothing loading, no layers) -> none.
  return NONE;
}

// --- Settings persistence (localStorage, like the other web settings) ----- //

/**
 * localStorage key for the bbox loading-animation enable flag. DEFAULT ON: an
 * absent / unparseable value reads as enabled, so a fresh user sees the
 * animations (NATE's default). Mirrors the LS_THEME / chat-opacity persistence
 * pattern (a single per-user key, read-with-default, write-through).
 */
export const LS_BBOX_ANIM = "grace2.bboxLoadingAnimations";

/** Read the persisted enable flag. Default ON (absent / bad value -> true). */
export function readBboxAnimationsEnabled(): boolean {
  try {
    const v = localStorage.getItem(LS_BBOX_ANIM);
    // Only the explicit string "false" disables it; anything else (incl. null)
    // is the default-ON behavior.
    return v !== "false";
  } catch {
    return true;
  }
}

/** Persist the enable flag. */
export function writeBboxAnimationsEnabled(enabled: boolean): void {
  try {
    localStorage.setItem(LS_BBOX_ANIM, enabled ? "true" : "false");
  } catch {
    /* storage unavailable (private mode / SSR) - non-fatal */
  }
}

/**
 * Derive the long-running-sim signal from a session-state `current_pipeline`
 * snapshot. A pipeline is "running" iff it exists, has NOT terminated
 * (`final_state` is null/undefined), and carries at least one step still in the
 * `running` state. Tolerant of loose/undefined shapes (the contract types the
 * field loosely on some envelopes) - any parse miss reads as not-running.
 */
export function isPipelineRunning(currentPipeline: unknown): boolean {
  if (!currentPipeline || typeof currentPipeline !== "object") return false;
  const p = currentPipeline as {
    final_state?: unknown;
    steps?: unknown;
  };
  // A terminated pipeline (complete / failed / cancelled) is never "running".
  if (p.final_state) return false;
  if (!Array.isArray(p.steps)) return false;
  return p.steps.some(
    (st) =>
      st != null &&
      typeof st === "object" &&
      (st as { state?: unknown }).state === "running",
  );
}
