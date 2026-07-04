// GRACE-2 web — sequence animation controller (panel-independent playback).
//
// ROOT CAUSE this fixes (NATE live mobile test 2026-06-20): the frame-advance
// playback for a sequential layer group (the `playing` flag + the interval that
// advances the visible frame) used to live INSIDE LayerPanel / SequenceScrubber.
// On mobile the LayerPanel lives in a MobileDrawer that UNMOUNTS when collapsed,
// and even on desktop the panel can be closed — either way the React state and
// the interval were torn down, so closing the Layers panel KILLED the animation
// and dropped the scrubber.
//
// FIX: lift the playback state + the advance interval into THIS module-level
// singleton (mirroring the getLayerCache() pattern in lib/layer_cache.ts). The
// controller holds {activeGroupKey, frameIndex (per group), playing}, runs the
// advance interval at module scope (survives any component unmount), and drives
// per-frame map visibility through an injected emitter (App wires this to the
// LayerPanelBus.pushMapCommand, which Map.tsx already subscribes to — Map.tsx
// stays mounted, so frames keep advancing on the map while the panel is closed).
//
// LayerPanel + SequenceScrubber become CONTROLS over this controller:
//   - LayerPanel pushes its detected sequential groups in (setGroups) and is the
//     source of frame-stepping intents.
//   - SequenceScrubber reads {playing, frameIndex} and toggles play.
//   - App subscribes to render the scrubber WHENEVER a sequence is animating
//     (active group present), regardless of whether the Layers panel is open.
//
// This module is PURE w.r.t. React / MapLibre (no imports of either). The only
// side effects are the setInterval (injectable via a timer seam for tests) and
// the emitter callback. Unit-testable with fake timers + a stub emitter.

/** Minimal shape of a sequential group the controller needs to advance frames. */
export interface AnimGroup {
  /** Stable key (matches SequentialGroup.key in LayerPanel). */
  key: string;
  /** Human label (for the scrubber). */
  label: string;
  /** Member layer ids in series order (ascending frame value). */
  layerIds: string[];
  /** Per-frame short labels, parallel to layerIds. */
  frameLabels: string[];
}

/** A read-only snapshot of controller state for subscribers. */
export interface AnimState {
  /** All known groups (latest pushed by LayerPanel). */
  groups: AnimGroup[];
  /** The group the scrubber drives + the play interval advances. null = none. */
  activeGroupKey: string | null;
  /** Per-group active frame index (groupKey -> frameIndex). */
  frameByGroup: Record<string, number>;
  /** Whether the active group is auto-advancing. */
  playing: boolean;
}

/**
 * Emit one frame's visibility intent: show `visibleLayerId`, hide the rest of
 * the group's members. The controller calls this whenever the active frame
 * changes (step / auto-advance). App wires it to bus.pushMapCommand so Map.tsx
 * (always mounted) flips the MapLibre layer visibility — independent of the
 * LayerPanel's lifetime. layerIds is the FULL group member list; visibleIndex
 * is which one should be visible.
 */
export type FrameVisibilityEmitter = (
  layerIds: string[],
  visibleIndex: number,
) => void;

/**
 * BUG 1 (memory crash): a seam Map.tsx registers to RELEASE a group's warmed
 * raster frames (flip the out-of-window frames to visibility:none so MapLibre
 * frees their SourceCache/textures). The controller fires it for the group's full
 * member list + the frame to keep visible: on scrubber-stop / reset() (keepIndex
 * = the current frame) and when the active group changes (the OLD group, so its
 * warmed frames don't leak while a new group plays). Mirrors the emitter seam.
 */
export type FrameReleaseEmitter = (
  layerIds: string[],
  keepVisibleIndex: number,
) => void;

/** Injectable timer seam so tests can drive the interval deterministically. */
export interface AnimTimers {
  setInterval(cb: () => void, ms: number): number;
  clearInterval(id: number): void;
}

const defaultTimers: AnimTimers = {
  setInterval: (cb, ms) =>
    typeof window !== "undefined"
      ? window.setInterval(cb, ms)
      : (setInterval(cb, ms) as unknown as number),
  clearInterval: (id) =>
    typeof window !== "undefined"
      ? window.clearInterval(id)
      : clearInterval(id as unknown as ReturnType<typeof setInterval>),
};

export interface AnimControllerOptions {
  /** Auto-advance cadence in ms while playing. Default 1100 (matches scrubber). */
  intervalMs?: number;
  /** Timer seam (tests inject a fake). Default = window.setInterval. */
  timers?: AnimTimers;
  /**
   * ITEM 5 (NATE 2026-06-22) - reduced-motion seam. When this returns true the
   * controller does NOT auto-start playback on a newly-seen group (the user
   * prefers no motion), though manual play still works. Default consults the
   * `prefers-reduced-motion` media query; tests inject a deterministic stub.
   */
  prefersReducedMotion?: () => boolean;
  /**
   * AUTOPLAY-OFF (NATE 2026-06-24) - opt-in auto-play. NATE reversed the ITEM 5
   * default: a freshly-loaded multi-frame group now shows its FIRST frame
   * statically and waits for the user to press play, instead of auto-sweeping on
   * load. Default false (playback is opt-in). When true (and reduced-motion is
   * not set) setGroups auto-starts playback as before. The scrubber's play button
   * remains the user's control either way.
   */
  autoPlay?: boolean;
}

/** SSR/test-safe default reduced-motion probe (mirrors PipelineCard's). */
function defaultPrefersReducedMotion(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

function clampIndex(i: number, n: number): number {
  if (n <= 0) return 0;
  return Math.max(0, Math.min(n - 1, i));
}

/** Wrap `i` into [0, n) so auto-advance loops cleanly past the last frame. */
function wrap(i: number, n: number): number {
  if (n <= 0) return 0;
  return ((i % n) + n) % n;
}

/**
 * Module-level playback controller for sequential layer groups. Holds the
 * play/frame state and runs the advance interval OUTSIDE the React tree so the
 * animation survives a LayerPanel unmount (the keystone fix). Drives map frame
 * visibility through the injected emitter.
 */
export class AnimationController {
  private groups: AnimGroup[] = [];
  private activeGroupKey: string | null = null;
  private frameByGroup: Record<string, number> = {};
  private playing = false;

  private readonly subs = new Set<(s: AnimState) => void>();
  private emitter: FrameVisibilityEmitter | null = null;
  // BUG 1: release seam (Map.tsx wires it to releaseWarmedFrames). Fired on
  // group-change + scrubber-stop/reset to free a group's warmed raster tiles.
  private releaseEmitter: FrameReleaseEmitter | null = null;
  // Cached snapshot — kept STABLE (same reference) between mutations so
  // useSyncExternalStore's Object.is comparison does not loop. Invalidated to
  // null on every state change; rebuilt lazily on the next snapshot() read.
  private cachedSnapshot: AnimState | null = null;

  private readonly intervalMs: number;
  private readonly timers: AnimTimers;
  private timerId: number | null = null;
  // ITEM 5 - reduced-motion probe (auto-play suppressed when it returns true).
  private readonly prefersReducedMotion: () => boolean;
  // AUTOPLAY-OFF (NATE 2026-06-24) - opt-in auto-play (default off). When false,
  // a newly-seen group seeds + shows frame 0 statically but does NOT start
  // playing; the user presses the scrubber play button to animate.
  private readonly autoPlay: boolean;
  // ITEM 5 - groups we have already auto-played, so re-pushing the same group
  // set (LayerPanel re-detects on every session-state frame) does not restart
  // playback after the user paused it. Keyed by group key.
  private autoPlayedKeys = new Set<string>();
  // BUG 2(B) - frame-index GRACE buffer. When a group key transiently LEAVES the
  // live set (a 1-heartbeat detection dropout, e.g. mid-re-run), we stash its
  // frame index here with a small countdown instead of forgetting it outright. If
  // the SAME key reappears within the grace window, setGroups restores the user's
  // frame instead of re-seeding frame 0 (the spurious "scrubber jumps to frame 0
  // / autoplay" symptom). { groupKey -> { frame, ttl } }.
  private frameGrace: Record<string, { frame: number; ttl: number }> = {};
  // How many setGroups cycles a pruned frame index survives before it is dropped.
  private static readonly FRAME_GRACE_TTL = 3;
  // ITEM 2 (NATE 2026-06-24) - group keys whose layer(s) the user HID via the
  // LayerPanel visibility toggle. A hidden group MUST NOT auto-advance (its
  // frames are off the map, so advancing them is invisible churn) and MUST NOT
  // be the active playback target. Showing the group again resumes from the
  // CURRENT frame (we never reset the frame index on hide/show). Kept as a set
  // so multiple groups can be independently hidden.
  private hiddenGroups = new Set<string>();

  constructor(opts: AnimControllerOptions = {}) {
    this.intervalMs = Math.max(50, opts.intervalMs ?? 1100);
    this.timers = opts.timers ?? defaultTimers;
    this.prefersReducedMotion =
      opts.prefersReducedMotion ?? defaultPrefersReducedMotion;
    // AUTOPLAY-OFF: default OFF (opt-in). Playback is user-driven.
    this.autoPlay = opts.autoPlay ?? false;
  }

  // --- emitter wiring --------------------------------------------------- //

  /**
   * Register the frame-visibility emitter (App wires it to bus.pushMapCommand).
   * Returns an unregister fn. Only one emitter is active at a time — a re-register
   * replaces the prior one (App re-runs the effect when the bus identity changes).
   */
  setEmitter(emitter: FrameVisibilityEmitter | null): () => void {
    this.emitter = emitter;
    return () => {
      if (this.emitter === emitter) this.emitter = null;
    };
  }

  /**
   * BUG 1 (memory crash): register the frame-RELEASE emitter (Map.tsx wires it to
   * releaseWarmedFrames). Returns an unregister fn; a re-register replaces the
   * prior one. Optional - when unset the controller simply skips the release
   * (tests that don't exercise the seam are unaffected).
   */
  setReleaseEmitter(emitter: FrameReleaseEmitter | null): () => void {
    this.releaseEmitter = emitter;
    return () => {
      if (this.releaseEmitter === emitter) this.releaseEmitter = null;
    };
  }

  /** BUG 1: fire the release seam for a group's members, keeping `keepIndex`. */
  private emitRelease(g: AnimGroup, keepIndex: number): void {
    if (this.releaseEmitter) this.releaseEmitter(g.layerIds, keepIndex);
  }

  // --- subscription ----------------------------------------------------- //

  /** Subscribe to state changes. Immediately invoked with the current state. */
  subscribe(cb: (s: AnimState) => void): () => void {
    this.subs.add(cb);
    cb(this.snapshot());
    return () => {
      this.subs.delete(cb);
    };
  }

  /**
   * Current snapshot of state. The SAME object reference is returned across
   * repeated calls until a mutation invalidates it — required by
   * useSyncExternalStore (Object.is identity, else it loops forever).
   */
  snapshot(): AnimState {
    if (this.cachedSnapshot === null) {
      this.cachedSnapshot = {
        groups: this.groups,
        activeGroupKey: this.activeGroupKey,
        frameByGroup: { ...this.frameByGroup },
        playing: this.playing,
      };
    }
    return this.cachedSnapshot;
  }

  private notify(): void {
    this.cachedSnapshot = null; // invalidate so the next snapshot() rebuilds.
    const s = this.snapshot();
    for (const cb of this.subs) cb(s);
  }

  // --- group registration (LayerPanel pushes detected groups) ----------- //

  /**
   * Replace the known group set (LayerPanel calls this whenever its detected
   * sequential groups change). Keeps the active key valid (defaults to the
   * first group), prunes frame indices for vanished groups, and stops playback
   * when no groups remain.
   *
   * ITEM 5 (NATE 2026-06-22): a newly-loaded animation group defaults its frame
   * to the FIRST frame (index 0, not the last) and emits it so the map shows the
   * first frame immediately.
   *
   * AUTOPLAY-OFF (NATE 2026-06-24): auto-play is now OPT-IN (this.autoPlay,
   * default false). By default the group seeds + shows frame 0 STATICALLY and
   * waits for the user to press the scrubber play button - NATE reversed the
   * ITEM 5 auto-sweep default. When autoPlay is true (and reduced-motion is not
   * set) playback auto-starts as before, at most ONCE per group key
   * (autoPlayedKeys) so a re-push of the same group set after a pause does not
   * restart it.
   */
  setGroups(groups: AnimGroup[]): void {
    // BUG 1 (memory crash): snapshot the OLD active group (key + members) before
    // we swap in the new set, so if the active group CHANGES below (its key
    // vanished, or a newly-seen group takes over) we can release the OLD group's
    // warmed raster frames instead of leaking their SourceCaches.
    const prevActiveKey = this.activeGroupKey;
    const prevActiveGroup = prevActiveKey
      ? this.groups.find((g) => g.key === prevActiveKey) ?? null
      : null;
    const releaseIfActiveChanged = (): void => {
      if (
        prevActiveGroup &&
        prevActiveGroup.key !== this.activeGroupKey
      ) {
        this.emitRelease(prevActiveGroup, -1); // -1 => release ALL its frames.
      }
    };

    this.groups = groups;

    // Prune frame state for groups that no longer exist. Also forget their
    // auto-play marker so a genuinely NEW group of the same key later re-plays.
    const live = new Set(groups.map((g) => g.key));
    for (const k of Object.keys(this.frameByGroup)) {
      if (!live.has(k)) {
        // BUG 2(B): a group key that LEFT the live set may be a transient 1-
        // heartbeat detection dropout (e.g. mid-re-run). Stash its frame index in
        // the grace buffer so a reappearance within the window RESTORES the user's
        // frame instead of re-seeding frame 0 (the spurious-autoplay symptom),
        // then drop the live record.
        this.frameGrace[k] = {
          frame: this.frameByGroup[k]!,
          ttl: AnimationController.FRAME_GRACE_TTL,
        };
        delete this.frameByGroup[k];
      }
    }
    for (const k of [...this.autoPlayedKeys]) {
      if (!live.has(k)) this.autoPlayedKeys.delete(k);
    }
    // ITEM 2 - drop the hidden marker for a group key that no longer exists, so
    // a genuinely-new group of the same key later starts VISIBLE (the session-
    // state re-emits the layer with the server `visible`, and a fresh group is
    // not the same user-hidden one). A key still present keeps its hidden state.
    for (const k of [...this.hiddenGroups]) {
      if (!live.has(k)) this.hiddenGroups.delete(k);
    }
    // BUG 2(B): age the grace buffer one cycle; drop entries whose window expired
    // (and re-stash live keys' graces are not needed - a live key holds its own
    // frameByGroup). A key that came BACK is consumed below (deleted from grace).
    for (const k of Object.keys(this.frameGrace)) {
      if (live.has(k)) continue; // consumed in the seed loop below.
      const g = this.frameGrace[k]!;
      g.ttl -= 1;
      if (g.ttl <= 0) delete this.frameGrace[k];
    }

    // Seed a default frame index (FIRST frame) for any newly-seen group, and
    // collect the newly-seen multi-frame group keys for auto-play below.
    const newlySeen: string[] = [];
    for (const g of groups) {
      if (this.frameByGroup[g.key] === undefined) {
        // BUG 2(B): if this key is in the grace buffer it is a REAPPEARANCE after
        // a transient dropout, NOT a genuinely-new group - RESTORE the user's
        // frame (and do NOT treat it as newlySeen, so it does not re-emit frame 0
        // or re-trigger auto-play). Otherwise it is genuinely new: seed frame 0.
        const grace = this.frameGrace[g.key];
        if (grace !== undefined) {
          this.frameByGroup[g.key] = clampIndex(grace.frame, g.layerIds.length);
          delete this.frameGrace[g.key];
        } else {
          this.frameByGroup[g.key] = 0; // ITEM 5: default to the FIRST frame.
          if (g.layerIds.length > 1) newlySeen.push(g.key);
        }
      }
    }

    if (groups.length === 0) {
      if (this.activeGroupKey !== null) this.activeGroupKey = null;
      releaseIfActiveChanged(); // BUG 1: free the vanished group's warmed frames.
      if (this.playing) this.setPlaying(false); // also clears the interval
      this.notify();
      return;
    }

    // Keep activeGroupKey valid; default to the first VISIBLE group.
    // ITEM 2 - default the active group to the first VISIBLE group (skip ones
    // the user hid), so re-detection while a group is hidden never re-points the
    // scrubber back at the hidden sequence.
    // TASK F (NATE 2026-06-26): when EVERY group is hidden there is NOTHING to
    // drive, so the active group must go NULL (the App-level scrubber unmounts).
    // The previous `?? groups[0]` fallback re-activated the hidden group on the
    // very next session-state re-detect (the LayerPanel re-pushes the same group
    // set on every heartbeat), which is exactly why hiding the animation layer
    // did NOT hide the scrubber live: setGroupHidden nulled the active key, then
    // the next setGroups silently re-pointed it back at the hidden sequence.
    const firstVisible =
      groups.find((g) => !this.hiddenGroups.has(g.key)) ?? null;
    const activeStillValid =
      this.activeGroupKey != null &&
      groups.some((g) => g.key === this.activeGroupKey) &&
      !this.hiddenGroups.has(this.activeGroupKey);
    if (!activeStillValid) {
      // Re-point at the first visible group, or null when none is visible (so
      // the scrubber disappears instead of clinging to a hidden sequence).
      this.activeGroupKey = firstVisible ? firstVisible.key : null;
    }

    // AUTOPLAY-OFF (NATE 2026-06-24): on a freshly-loaded multi-frame group make
    // it the active group and EMIT frame 0 so the map shows the first frame
    // immediately, but do NOT auto-start playback unless auto-play is explicitly
    // opted in (this.autoPlay) and reduced-motion is not set. By default the
    // scrubber appears, shows frame 0, and sits PAUSED until the user presses
    // play. Mark the group seen either way so a re-push of the same set does not
    // re-attempt auto-play after a manual pause.
    // ITEM 2 - only consider VISIBLE newly-seen groups for activation/auto-play;
    // a hidden group must never grab the active pointer or auto-start.
    const newlySeenVisible = newlySeen.filter(
      (k) => !this.hiddenGroups.has(k),
    );
    if (newlySeenVisible.length > 0) {
      const autoKey =
        this.activeGroupKey && newlySeenVisible.includes(this.activeGroupKey)
          ? this.activeGroupKey
          : newlySeenVisible[0]!;
      this.activeGroupKey = autoKey;
      const g = this.groups.find((gr) => gr.key === autoKey);
      if (g) this.emitFrame(g, 0); // show the first frame now (static).
      if (this.autoPlay && !this.prefersReducedMotion()) {
        this.autoPlayedKeys.add(autoKey);
        releaseIfActiveChanged(); // BUG 1: release the prior active group.
        // setPlaying arms the interval (syncInterval) and notifies.
        this.setPlaying(true);
        return;
      }
      // Auto-play off (default) or reduced motion: mark it seen so we don't
      // re-attempt, but leave it PAUSED on frame 0 (the first frame, static).
      this.autoPlayedKeys.add(autoKey);
    }

    // BUG 1 (memory crash): if the active group changed (vanished key defaulted to
    // a different group, or a newly-seen group took over) release the OLD group's
    // warmed raster frames. A re-push of the SAME active key is a no-op (the
    // BUG 2 fix keeps the key stable across re-runs, so this rarely fires).
    releaseIfActiveChanged();
    this.notify();
  }

  // --- queries ---------------------------------------------------------- //

  getGroups(): AnimGroup[] {
    return this.groups;
  }

  getActiveGroup(): AnimGroup | null {
    if (this.activeGroupKey === null) return null;
    return this.groups.find((g) => g.key === this.activeGroupKey) ?? null;
  }

  isPlaying(): boolean {
    return this.playing;
  }

  /** Resolved active frame index for a group key (default = FIRST frame). */
  frameIndexFor(key: string): number {
    const g = this.groups.find((gr) => gr.key === key);
    if (!g) return 0;
    const raw = this.frameByGroup[key];
    // ITEM 5 (NATE 2026-06-22): default to the FIRST frame (0), not the last,
    // so a group seen before its frame index is recorded reads from the start.
    const idx = typeof raw === "number" ? raw : 0;
    return clampIndex(idx, g.layerIds.length);
  }

  // --- commands (controls call these) ----------------------------------- //

  /** Make a group the scrubber/playback target. */
  setActiveGroup(key: string | null): void {
    if (this.activeGroupKey === key) return;
    // BUG 1 (memory crash): the active group is CHANGING - release the OLD
    // group's warmed raster frames so its SourceCaches don't leak while the new
    // group plays. keepIndex = -1 (nothing in window) releases ALL of its frames.
    const prev = this.getActiveGroup();
    if (prev && prev.key !== key) this.emitRelease(prev, -1);
    this.activeGroupKey = key;
    this.notify();
  }

  /**
   * Step a group to frame `index`. Sets the active group to this group, records
   * the frame, and emits the frame-visibility intent (show frame i, hide the
   * rest) so the map updates even if the panel is closed.
   */
  stepGroupTo(key: string, index: number): void {
    const g = this.groups.find((gr) => gr.key === key);
    if (!g) return;
    const clamped = clampIndex(index, g.layerIds.length);
    this.activeGroupKey = key;
    this.frameByGroup[key] = clamped;
    this.emitFrame(g, clamped);
    this.notify();
  }

  /** Advance the ACTIVE group by `delta` frames (wraps). No-op if none active. */
  advanceActive(delta: number): void {
    const g = this.getActiveGroup();
    if (!g) return;
    // ITEM 2 (NATE 2026-06-24) - a HIDDEN active group must not advance: its
    // frames are off the map, so cycling them is invisible churn (and would
    // also keep the scrubber index moving). The heartbeat / auto paths funnel
    // through here too, so this single guard halts all advance for a hidden
    // group regardless of caller.
    if (this.hiddenGroups.has(g.key)) return;
    const cur = this.frameIndexFor(g.key);
    const next = wrap(cur + delta, g.layerIds.length);
    this.frameByGroup[g.key] = next;
    this.emitFrame(g, next);
    this.notify();
  }

  /** Set the playing flag (arms/clears the advance interval). */
  setPlaying(playing: boolean): void {
    if (this.playing === playing) {
      // Still ensure the interval matches the desired state (idempotent).
      this.syncInterval();
      return;
    }
    this.playing = playing;
    this.syncInterval();
    this.notify();
  }

  /** Toggle playing. */
  togglePlaying(): void {
    this.setPlaying(!this.playing);
  }

  /**
   * ITEM 2 (NATE 2026-06-24) - mark a group HIDDEN (or shown) because the user
   * toggled its layer visibility off (or on) in the LayerPanel. A HIDDEN group:
   *   - STOPS auto-advancing: syncInterval will not arm while the active group
   *     is hidden, and advanceActive is a no-op for a hidden active group, so
   *     its frames do not keep cycling off-screen (and the heartbeat / auto
   *     paths can't drive it either - they go through advanceActive).
   *   - is not a valid ACTIVE / playback target: hiding the active group halts
   *     the scrubber (stops play + tears the interval down) and moves the active
   *     pointer to the first still-VISIBLE group (or null when none remain), so
   *     the App-level scrubber switches to a visible sequence or disappears.
   * Showing the group again does NOT force-restart playback or reset the frame:
   * the frame index is preserved, so it resumes from the CURRENT frame; the user
   * presses play (or it becomes active again) to continue. Idempotent.
   */
  setGroupHidden(key: string, hidden: boolean): void {
    const was = this.hiddenGroups.has(key);
    if (was === hidden) return; // no change.
    if (hidden) {
      this.hiddenGroups.add(key);
      // If the hidden group is the active playback target, halt + re-point the
      // active group at the first still-visible group (or null).
      if (this.activeGroupKey === key) {
        if (this.playing) {
          this.playing = false; // stop auto-advance for the hidden group.
        }
        const nextVisible =
          this.groups.find((g) => !this.hiddenGroups.has(g.key)) ?? null;
        this.activeGroupKey = nextVisible ? nextVisible.key : null;
      }
    } else {
      this.hiddenGroups.delete(key);
      // Showing a group when nothing is active makes it the active target again
      // (so the scrubber reappears for it), but does NOT auto-start playback -
      // it resumes PAUSED at the current frame (NATE: "resumes from the current
      // frame, no force-restart").
      if (this.activeGroupKey === null && this.groups.some((g) => g.key === key)) {
        this.activeGroupKey = key;
      }
    }
    // Re-sync the interval against the (possibly changed) active group + playing
    // flag, then notify subscribers (scrubber + panel re-read).
    this.syncInterval();
    this.notify();
  }

  /** ITEM 2 - true when a group is currently hidden (user toggled it off). */
  isGroupHidden(key: string): boolean {
    return this.hiddenGroups.has(key);
  }

  // --- internals -------------------------------------------------------- //

  private emitFrame(g: AnimGroup, visibleIndex: number): void {
    if (this.emitter) this.emitter(g.layerIds, visibleIndex);
  }

  /** Arm the advance interval when playing + a multi-frame VISIBLE group is active. */
  private syncInterval(): void {
    const active = this.getActiveGroup();
    const shouldRun =
      this.playing &&
      active !== null &&
      active.layerIds.length > 1 &&
      // ITEM 2 - never advance a hidden group's frames off-screen.
      !this.hiddenGroups.has(active.key);
    if (shouldRun && this.timerId === null) {
      this.timerId = this.timers.setInterval(() => {
        this.advanceActive(1);
      }, this.intervalMs);
    } else if (!shouldRun && this.timerId !== null) {
      this.timers.clearInterval(this.timerId);
      this.timerId = null;
    }
  }

  /**
   * Item c (NATE 2026-06-20) — fully clear playback state. Used on CASE-EXIT /
   * CASE-SWITCH: when a Case is closed the LayerPanel unmounts (the left rail
   * shows the Cases list, not CaseView), so it never pushes `setGroups([])` to
   * clear the controller — the old Case's groups would linger and the
   * App-level scrubber would keep rendering for a Case you've left. App calls
   * this to drop ALL groups + the active key + frame state + stop playback (and
   * tear the interval down), so the scrubber vanishes on Case exit. The new
   * Case's LayerPanel re-pushes its own groups on mount.
   */
  reset(): void {
    // BUG 1 (memory crash): release the active group's warmed raster frames
    // BEFORE we drop the group state, so a case-switch / scrubber-stop frees the
    // SourceCaches that swapFrameWithHold left visible (warmed) instead of leaking
    // them. Keep the current frame visible (it is about to be torn down by the
    // session-state reconcile anyway, but releasing the OTHERS is the win).
    const active = this.getActiveGroup();
    if (active) this.emitRelease(active, this.frameIndexFor(active.key));
    this.groups = [];
    this.activeGroupKey = null;
    this.frameByGroup = {};
    this.frameGrace = {}; // BUG 2(B): a new Case starts with no carried-over frames.
    this.playing = false;
    this.hiddenGroups.clear(); // ITEM 2: a new Case's groups start visible.
    this.autoPlayedKeys.clear(); // ITEM 5: a new Case may re-auto-play its groups.
    this.dispose(); // stop the advance interval
    this.notify();
  }

  /** Tear the interval down (tests / explicit reset). */
  dispose(): void {
    if (this.timerId !== null) {
      this.timers.clearInterval(this.timerId);
      this.timerId = null;
    }
  }
}

// --- Shared singleton --------------------------------------------------- //
//
// Process-global so the panel-independent playback survives ANY component
// unmount: LayerPanel pushes groups + steps, SequenceScrubber + App subscribe.
// Created lazily on first access (mirrors getLayerCache) so a test can replace
// it before App mounts.

let shared: AnimationController | null = null;

/** The process-global AnimationController. Lazily created with defaults. */
export function getAnimationController(): AnimationController {
  if (shared === null) shared = new AnimationController();
  return shared;
}

/** Replace the process-global AnimationController (tests / explicit re-init). */
export function setAnimationController(c: AnimationController): void {
  shared?.dispose();
  shared = c;
}
