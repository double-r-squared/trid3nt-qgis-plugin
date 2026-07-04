// GRACE-2 web — LayerPanel (FR-WC-4, FR-WC-6 partial).
//
// Renders the project's loaded layers from `session-state.loaded_layers`
// (Appendix D.2 / D.6) and applies live `map-command` updates from the
// agent. Each row has:
//
//   - drag handle (drag-and-drop reorder via @dnd-kit/sortable)
//   - visibility checkbox
//   - opacity slider (0..1)
//   - name + attribution
//
// The ▲/▼ nudge buttons were dropped in job-0173 Part 4 — they were
// redundant with @dnd-kit's drag-and-drop reorder (which also provides
// the keyboard reorder a11y path the nudge buttons were nominally for).
//
// job-0258 (LAYER CONTROLS DEAD root-cause fix): user-side clicks now emit
// real `map-command` payloads through the optional `onMapCommand` prop —
// App.tsx wires it to the shared LayerPanelBus so MapView applies them to
// the live MapLibre instance (setPaintProperty / setLayoutProperty /
// moveLayer). Before this job the handlers below ONLY dispatched to the
// panel's local reducer + console.debug "intent" logs (the M3 stubs), so
// the opacity slider and drag-reorder visibly did nothing on the map.
// Agent-side persistence of these intents remains future work (the bus is
// client-local; nothing is sent to the agent yet).
//
// The panel renders the layer list **top-of-stack-first** (top of list =
// rendered on top). `z_index` from ProjectLayerSummary is INTERPRETED:
// higher z_index = higher in the stack = earlier in the list. This matches
// MapLibre's add-layer-on-top semantics.
//
// Drag-and-drop library choice: @dnd-kit/sortable. Surfaced as Open Question
// — alternatives are hand-rolled HTML5 DnD or react-dnd. @dnd-kit chosen
// because: (a) actively maintained, (b) full keyboard a11y out of the box
// (the extra up/down buttons are belt+suspenders), (c) zero global state.

import { memo, useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  DragEndEvent,
} from "@dnd-kit/core";
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import {
  CaseOpenEnvelopePayload,
  compareLayersTopFirst,
  MapCommandPayload,
  ProjectLayerSummary,
  SessionStatePayload,
} from "./contracts";
import { ConfirmationDialog } from "./components/ConfirmationDialog";
import type { ScreenRect } from "./lib/legend_snap";
import {
  IconClose,
  IconDelete,
  IconEye,
  IconEyeOff,
  IconChevronDown,
  IconChevronRight,
  IconPlay,
  IconPause,
  IconWaves,
} from "./components/icons";
// job-0179 (per-Case client cache + view-state durability — "the seatbelt").
// The LayerPanel write-throughs the user's opacity / visibility / drag-reorder
// edits into the shared cache so they survive a panel unmount->remount (mobile
// drawer collapse) and a WS reconnect — even when the Map is not mounted to
// receive the bus map-command. This SUBSUMES the localStorage
// `grace2.layerVisibility` map (writeLayerVisibilityOverride is still called for
// back-compat). The cache resolves the active Case via its own `.activeCaseId`,
// which App.tsx keeps in lockstep.
import { getLayerCache } from "./lib/layer_cache";
// JOB WEB-ANIM (#157.1) — the module-level playback controller + its React
// binding. The LayerPanel pushes its detected sequential groups into the
// controller and reads playback state from it (instead of owning the `playing`
// flag + the advance interval), so the animation survives a panel unmount.
import { getAnimationController } from "./lib/animation_controller";
import { useAnimationState } from "./lib/use_animation_controller";

// --- Reducer + state shape --------------------------------------------- //
//
// Internal view-model = the rendered ordered list of layers. The list is
// kept sorted top-of-stack-first; reducer actions translate from external
// session-state / map-command envelopes onto this representation.

interface LayerPanelState {
  layers: ProjectLayerSummary[]; // top-of-stack first
}

type LayerPanelAction =
  | { type: "session-state"; payload: SessionStatePayload }
  | { type: "map-command"; payload: MapCommandPayload }
  | { type: "local-reorder"; layer_ids: string[] }
  | { type: "local-visibility"; layer_id: string; visible: boolean }
  | { type: "local-opacity"; layer_id: string; opacity: number };

// Exported for the BUG 2 cross-surface determinism test (panel path).
export function sortTopFirst(layers: ProjectLayerSummary[]): ProjectLayerSummary[] {
  // BUG 2 (random-reorder): sort via the SHARED deterministic comparator (z_index
  // descending, layer_id tiebreak) - NOT a bare `b.z_index - a.z_index`, which
  // gave `NaN` (no total order) on the agent's null z_index and let this panel
  // render the SAME set in a DIFFERENT order than the map / App surfaces.
  return [...layers].sort(compareLayersTopFirst);
}

/**
 * ux-batch-1 J3 (F22) — collapse duplicate layer_ids, keeping the LAST
 * occurrence (a republish of the same layer_id appends the newer version).
 * Without this, an undeduped loaded_layers list (e.g. from a recompute that
 * re-published the same layer_id) rendered TWO rows sharing one React key —
 * a key collision that made their opacity sliders move together ("connected
 * sliders"). Order is preserved by last-seen position so the subsequent
 * sortTopFirst is stable. Exported for unit testing.
 */
export function dedupeByLayerId(
  layers: ProjectLayerSummary[],
): ProjectLayerSummary[] {
  const byId = new Map<string, ProjectLayerSummary>();
  for (const l of layers) byId.set(l.layer_id, l); // last write wins
  return Array.from(byId.values());
}

/**
 * LANE C (flicker): structural equality of two already-sorted layer lists by the
 * fields that drive a render - layer_id + visible + opacity + z_index. The server
 * re-emits a FULL authoritative session-state on every keepalive resume (~25s)
 * and on every turn push; each rebuilt a brand-new array with new object refs,
 * so the whole layers list re-rendered periodically (the visible "flashing" of
 * the layers section). When the incoming set is structurally identical we return
 * the SAME state ref from the reducer so useReducer bails out (no re-render).
 */
function layerSetsEqual(
  a: ProjectLayerSummary[],
  b: ProjectLayerSummary[],
): boolean {
  if (a.length !== b.length) return false;
  // FLASH FIX (Lane 1a): ORDER-INSENSITIVE compare keyed by layer_id. The merged
  // list arrives in Map-insertion order and a re-publish of an existing layer_id
  // can shuffle EQUAL-z layers frame-to-frame; a positional compare returned
  // false on such a reorder and forced a full re-render. Keying by layer_id makes
  // an equal SET (same ids + {visible,opacity,z_index}) compare equal regardless
  // of array order, so the reducer short-circuit holds across the reshuffle.
  const byId = new Map<
    string,
    Pick<ProjectLayerSummary, "visible" | "opacity" | "z_index">
  >();
  for (const x of a) {
    if (!x) return false;
    byId.set(x.layer_id, {
      visible: x.visible,
      opacity: x.opacity,
      z_index: x.z_index,
    });
  }
  for (const y of b) {
    if (!y) return false;
    const x = byId.get(y.layer_id);
    if (
      !x ||
      x.visible !== y.visible ||
      x.opacity !== y.opacity ||
      x.z_index !== y.z_index
    ) {
      return false;
    }
  }
  return true;
}

function reducer(state: LayerPanelState, action: LayerPanelAction): LayerPanelState {
  switch (action.type) {
    case "session-state": {
      // ITEM 3 (NATE 2026-06-24) - SEATBELT the panel reducer's session-state
      // seed through the SAME durability cache + `replace_layers` honor that
      // App.tsx's `layers` already uses, so a PARTIAL / transient reconnect /
      // keepalive frame can never shrink a frame series below the >=2-member
      // grouping threshold (which would un-group the series and let its frames
      // "escape" into the layer list as individual rows on the mobile
      // background/foreground / refocus path). App's `layers` was already
      // protected by this merge (App.tsx); the panel's OWN reducer was not, so
      // the rows un-grouped while the map/legend stayed grouped. Routing through
      // mergeSnapshot makes the panel rows byte-identical to App's merged set, so
      // detectSequentialGroups re-forms the group every time. At the root (no
      // active Case) there is nothing to cache against -> use the raw list.
      const cache = getLayerCache();
      const caseId = cache.activeCaseId;
      const raw = action.payload.loaded_layers ?? [];
      const authoritativeReplace =
        (action.payload as { replace_layers?: boolean }).replace_layers !== false;
      const incoming =
        caseId == null
          ? raw
          : cache.mergeSnapshot(caseId, raw, { authoritativeReplace });
      // F22: dedupe by layer_id BEFORE sorting so a duplicate-id republish
      // can never render two rows with the same React key (the connected-
      // sliders bug).
      // F55 (job-0325): re-apply the user's persisted visibility overrides on
      // top of the server `visible` so a layer the user hid stays hidden across
      // a panel unmount->remount (mobile drawer collapse re-seeds from a fresh
      // session-state). No override => server value verbatim.
      const next = sortTopFirst(
        applyVisibilityOverrides(dedupeByLayerId(incoming)),
      );
      // LANE C (flicker): the server re-emits the full authoritative
      // session-state on every keepalive resume (~25s) + every turn push. When
      // the layer set is UNCHANGED (same ids/visible/opacity/z_index), return the
      // SAME state ref so useReducer bails and the whole list does NOT re-render
      // (the periodic "flashing of the layers section" NATE reports).
      if (layerSetsEqual(state.layers, next)) return state;
      return { layers: next };
    }
    case "map-command": {
      const cmd = action.payload;
      switch (cmd.command) {
        case "load-layer": {
          // Replace or append by layer_id.
          const without = state.layers.filter(
            (l) => l.layer_id !== cmd.layer.layer_id,
          );
          // C3 (job-0356) — apply the user's persisted visibility override to the
          // (re)published layer, exactly like the `session-state` seed at ~124.
          // A republish of a layer_id the user explicitly HID arrives with
          // visible:true from the server (no per-user visibility state there), so
          // without this the panel row would snap back to visible. The
          // hasOwnProperty guard inside applyVisibilityOverrides means a
          // never-toggled layer keeps the server value verbatim.
          const [overridden] = applyVisibilityOverrides([cmd.layer]);
          const next = [...without, overridden ?? cmd.layer];
          return { layers: sortTopFirst(next) };
        }
        case "remove-layer": {
          return {
            layers: state.layers.filter((l) => l.layer_id !== cmd.layer_id),
          };
        }
        case "set-layer-visibility": {
          return {
            layers: state.layers.map((l) =>
              l.layer_id === cmd.layer_id ? { ...l, visible: cmd.visible } : l,
            ),
          };
        }
        case "set-layer-opacity": {
          return {
            layers: state.layers.map((l) =>
              l.layer_id === cmd.layer_id
                ? { ...l, opacity: clamp01(cmd.opacity) }
                : l,
            ),
          };
        }
        case "set-layer-order": {
          // Agent-provided ordering, top-of-stack first. Reassign z_index
          // monotonically so the local view matches the order verbatim.
          const idToLayer = new Map(state.layers.map((l) => [l.layer_id, l]));
          const next: ProjectLayerSummary[] = [];
          cmd.layer_ids.forEach((id, idx) => {
            const layer = idToLayer.get(id);
            if (layer) {
              next.push({ ...layer, z_index: cmd.layer_ids.length - idx });
            }
          });
          // Preserve layers not named in the command (defensive — agent should
          // always send a full list, but the client should not lose state).
          state.layers.forEach((l) => {
            if (!cmd.layer_ids.includes(l.layer_id)) next.push(l);
          });
          return { layers: next };
        }
      }
      // exhaustive: MapCommandPayload union is the 5 M3-active sub-
      // discriminants only (zoom-to / set-temporal-config / start-animation /
      // stop-animation / invalidate-tiles deferred to M4–M5 per kickoff §6).
      return state;
    }
    case "local-reorder": {
      const idToLayer = new Map(state.layers.map((l) => [l.layer_id, l]));
      const next: ProjectLayerSummary[] = [];
      action.layer_ids.forEach((id, idx) => {
        const layer = idToLayer.get(id);
        if (layer) {
          next.push({ ...layer, z_index: action.layer_ids.length - idx });
        }
      });
      return { layers: next };
    }
    case "local-visibility": {
      return {
        layers: state.layers.map((l) =>
          l.layer_id === action.layer_id ? { ...l, visible: action.visible } : l,
        ),
      };
    }
    case "local-opacity": {
      return {
        layers: state.layers.map((l) =>
          l.layer_id === action.layer_id
            ? { ...l, opacity: clamp01(action.opacity) }
            : l,
        ),
      };
    }
    default:
      return state;
  }
}

function clamp01(x: number): number {
  if (Number.isNaN(x)) return 0;
  if (x < 0) return 0;
  if (x > 1) return 1;
  return x;
}

// --- Sequential-layer grouping (NATE: enumerated temporal raster stacks) --- //
//
// NATE's ask: enumerated temporal raster sequences — e.g. 3 HRRR forecast hours
// "...F+01h" / "...F+03h" / "...F+06h" — should COLLAPSE into ONE "sequential
// group" row you can step through (LEFT/RIGHT + a bottom scrubber) instead of N
// near-identical rows. Stepping shows ONE frame at a time by toggling layer
// visibility through the EXISTING LayerPanel visibility callback (client-side
// only; no backend, no Map.tsx edits).
//
// Detection is deliberately CONSERVATIVE: we only form a group when there is a
// CLEAR monotonic series of >=2 layers that (a) share a common source/tool +
// AOI and (b) carry a parseable lead-time / step / index token whose values are
// strictly increasing. Everything else stays an ordinary row.

/** A parsed frame token: the numeric position + the verbatim label to show. */
interface FrameToken {
  /** Monotonic numeric position (e.g. lead hours, step index). */
  value: number;
  /** Short human label for the frame, e.g. "F+03h" / "t+2" / "step 4". */
  label: string;
  /** The common "stem" (name with the token stripped) — the grouping key. */
  stem: string;
}

// Ordered token patterns over a layer name. First match wins. Each captures a
// numeric position and yields a normalized short label + the stem (name minus
// the matched token) so layers in one series share a stem. Tokens are matched
// near the END of the name (where enumerations live) but anywhere is accepted.
const FRAME_PATTERNS: ReadonlyArray<{
  rx: RegExp;
  label: (m: RegExpMatchArray) => string;
}> = [
  // Forecast lead hour: "F+01h", "f+12h", "F+1 h", "+06h"
  { rx: /\bf?\+?\s*(\d{1,3})\s*h\b/i, label: (m) => `F+${pad2(m[1])}h` },
  // Hour token: "hour 3", "hr 06", "h12"
  { rx: /\bh(?:ou)?r?\s*\+?(\d{1,3})\b/i, label: (m) => `hr ${stripZeros(m[1])}` },
  // Step/frame/index: "step 4", "frame 02", "t+2", "t2", "#3"
  { rx: /\b(?:step|frame|idx|index)\s*\+?(\d{1,4})\b/i, label: (m) => `step ${stripZeros(m[1])}` },
  { rx: /\bt\s*\+\s*(\d{1,4})\b/i, label: (m) => `t+${stripZeros(m[1])}` },
  { rx: /#\s*(\d{1,4})\b/i, label: (m) => `#${stripZeros(m[1])}` },
  // Day token: "day 1", "d+3"
  { rx: /\bd(?:ay)?\s*\+?(\d{1,3})\b/i, label: (m) => `day ${stripZeros(m[1])}` },
];

// An ISO-8601 UTC valid-time substring, e.g. "2026-06-22T18:05:00Z". The
// satellite fire-animation frames (GOES GeoColor / Fire Temperature, VIIRS Day
// Fire) carry a "step <N>" monotonic token (the grouping value) PLUS their real
// UTC valid-time. The raw ISO is not itself a recognized frame token, but when a
// frame DOES carry a step/index token we prefer the ISO as the human-readable
// per-frame label and strip it from the grouping stem (otherwise the per-frame
// time would vary the stem and break the series into singletons).
const ISO_TIME_RX = /\b(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2})?Z?\b/;

function pad2(s: string | undefined): string {
  const n = Number(s ?? "0");
  return Number.isFinite(n) ? String(n).padStart(2, "0") : (s ?? "");
}
function stripZeros(s: string | undefined): string {
  const n = Number(s ?? "0");
  return Number.isFinite(n) ? String(n) : (s ?? "");
}

/**
 * Parse a frame token out of a layer name. Returns null when no monotonic
 * lead-time / step / index token is present. Exported for unit testing.
 */
export function parseFrameToken(name: string): FrameToken | null {
  if (!name) return null;
  for (const { rx, label } of FRAME_PATTERNS) {
    const m = name.match(rx);
    if (m && m[1] != null) {
      const value = Number(m[1]);
      if (!Number.isFinite(value)) continue;
      // Stem = the name with the matched token removed + whitespace collapsed.
      // Series members differ ONLY in the token, so they share a stem. When the
      // name also carries an ISO valid-time (satellite fire-animation frames),
      // strip it too: it varies per frame, so leaving it in the stem would split
      // the series into singletons. The ISO then becomes the per-frame LABEL.
      let body = name
        .slice(0, m.index)
        .concat(name.slice((m.index ?? 0) + m[0].length));
      const iso = body.match(ISO_TIME_RX);
      if (iso) body = body.replace(iso[0], " ");
      const stem = body
        .replace(/\s+/g, " ")
        .replace(/[\s,(\-–—]+$/g, "")
        .replace(/^[\s,(\-–—]+/g, "")
        .trim()
        .toLowerCase();
      // Prefer the real UTC valid-time as the frame label when present (the
      // satellite-frame case); else the synthetic token label (F+03h / step 4).
      const frameLabel = iso ? `${iso[1]} ${iso[2]}Z` : label(m);
      return { value, label: frameLabel, stem };
    }
  }
  return null;
}

/** A detected sequential group: the ordered member layers + their frame labels. */
export interface SequentialGroup {
  /** Stable key for the group (shared stem + bbox signature). */
  key: string;
  /** Human label for the group, derived from the shared stem / first member. */
  label: string;
  /** Member layers in series order (ascending frame value). */
  layers: ProjectLayerSummary[];
  /** Per-member short frame labels, parallel to `layers`. */
  frameLabels: string[];
}

/**
 * BUG 2(B) - a RUN-INDEPENDENT series signature so a RE-RUN of the same scenario
 * maps to the SAME group key (it used to mint a NEW key per run, which made the
 * re-run look like a brand-new group: setGroups then re-seeded frame 0 = the
 * "spurious autoplay / scrubber jumps to frame 0" symptom).
 *
 * The URI prefix is the only AOI/source proxy on ProjectLayerSummary, but it
 * embeds a per-run id segment (`.../runs/<run_id>/<tool>/frameNN.tif`), so we
 * canonicalize: drop the per-frame filename, then drop any path segment that
 * looks like a per-run id (a ULID / UUID / long hex / pure-digit run token). What
 * remains is the run-INDEPENDENT structure (bucket + tool dirs), so sibling
 * frames in a run AND the frames of a later re-run share one signature. Falls
 * back to the whole URI when there is nothing run-like to strip.
 */
function seriesSignature(layer: ProjectLayerSummary): string {
  const uri = layer.uri ?? "";
  const lastSlash = uri.lastIndexOf("/");
  const dir = lastSlash >= 0 ? uri.slice(0, lastSlash) : uri;
  // A segment is "run-like" if it is a ULID (26 Crockford base32), a UUID, a long
  // hex blob, or a pure-digit/epoch token - all of which change every run.
  const isRunLike = (seg: string): boolean =>
    /^[0-9A-HJKMNP-TV-Z]{26}$/i.test(seg) || // ULID
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(seg) || // UUID
    /^[0-9a-f]{16,}$/i.test(seg) || // long hex run id
    /^\d{6,}$/.test(seg) || // epoch / numeric run id
    /(^|[-_])(run|job)[-_]?[0-9a-z]+$/i.test(seg); // run-<id> / job_<id>
  const stable = dir
    .split("/")
    .filter((seg) => seg.length > 0 && !isRunLike(seg))
    .join("/");
  return stable.length > 0 ? stable : dir;
}

/** Titleize a lowercased stem for display ("hrrr forecast" → "Hrrr Forecast"). */
function titleizeStem(stem: string, fallback: string): string {
  const s = stem.trim();
  if (!s) return fallback;
  return s.replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * ITEM 3 (NATE 2026-06-22) - true for the STATIC peak / primary "max-depth"
 * layer that rides ALONGSIDE a time-series animation, so it is NEVER swept into
 * the frame group. The engines emit a `role="primary"` peak (e.g. name "Peak
 * flood depth" / "Peak wave height", layer_id `<engine>-depth-peak-<run>`) plus
 * N `role="context"` frames ("Flood depth step N"). `role` does NOT survive into
 * `ProjectLayerSummary`, so we detect the peak from the two SURVIVING signals:
 * the layer_id's `-peak-` segment (authoritative - every engine stamps it) and a
 * leading "peak"/"max"/"maximum" name token. Excluding the peak keeps it an
 * INDEPENDENT ordinary row with its own toggle: hiding the animation sequence
 * (the scrubber/frame group) no longer hides the static max-depth layer.
 * Exported for unit testing.
 */
export function isPeakLayer(layer: ProjectLayerSummary): boolean {
  const id = (layer.layer_id ?? "").toLowerCase();
  if (id.includes("-peak-") || id.endsWith("-peak")) return true;
  const name = (layer.name ?? "").trim().toLowerCase();
  // A leading peak/max(imum) token ("Peak flood depth", "Max flood depth").
  return /^(?:peak|max(?:imum)?)\b/.test(name);
}

/**
 * Detect sequential groups among an ordered layer list. CONSERVATIVE: a group
 * forms only when >=2 layers share (stem + source/AOI signature + style_preset)
 * AND carry strictly-increasing, DISTINCT frame values. Members are returned in
 * ascending frame order. Layers that don't qualify are simply absent from the
 * result (the caller renders them as ordinary rows). Exported for unit testing.
 */
export function detectSequentialGroups(
  layers: ProjectLayerSummary[],
): SequentialGroup[] {
  const buckets = new Map<
    string,
    { token: FrameToken; layer: ProjectLayerSummary }[]
  >();
  for (const layer of layers) {
    // ITEM 3 - the static peak/primary "max-depth" layer is INDEPENDENT of the
    // animation: never let it join a frame group (so hiding the sequence keeps
    // it visible with its own toggle). Belt to the name-token suspenders below -
    // a peak whose name happened to carry a digit must still never group.
    if (isPeakLayer(layer)) continue;
    const token = parseFrameToken(layer.name);
    if (!token) continue;
    // Group key: stem + RUN-INDEPENDENT series signature + preset. All three must
    // match so we never fuse two unrelated series that share a token shape, while
    // the run-independent signature (BUG 2(B)) makes a RE-RUN of the same scenario
    // map to the SAME key (so the controller preserves the user's frame instead of
    // re-seeding frame 0 = the spurious-autoplay symptom).
    const key = [
      token.stem,
      seriesSignature(layer),
      (layer.style_preset ?? layer.layer_type ?? "").toLowerCase(),
    ].join("§");
    const arr = buckets.get(key) ?? [];
    arr.push({ token, layer });
    buckets.set(key, arr);
  }

  const groups: SequentialGroup[] = [];
  for (const [key, members] of buckets) {
    if (members.length < 2) continue;
    // Dedupe by frame value FIRST. A re-run of the same scenario appends a
    // SECOND full series under fresh run_ids: the new frames are distinct COGs,
    // so add_loaded_layer never collapses them, and the case ends up carrying
    // [step1, step1, step2, step2, ...]. Those duplicate values would fail the
    // strict-monotonic check below, so the WHOLE series gets rejected and every
    // frame falls back to its own legend key (the duplicate-legend explosion) +
    // a double-length scrubber that animates twice as fast. Keep the LAST
    // occurrence per value: members are in load order, so newest-last == the
    // most recent run, which is the one the user wants to see.
    const byValue = new Map<
      number,
      { token: FrameToken; layer: ProjectLayerSummary }
    >();
    for (const m of members) byValue.set(m.token.value, m);
    const deduped = [...byValue.values()];
    if (deduped.length < 2) continue;
    // Sort by frame value ascending; values are now distinct, so the series is
    // strictly increasing by construction (the guard below stays as a safety
    // net against any non-monotonic noise from an unexpected token shape).
    const sorted = deduped.sort((a, b) => a.token.value - b.token.value);
    let monotonic = true;
    for (let i = 1; i < sorted.length; i++) {
      const cur = sorted[i];
      const prev = sorted[i - 1];
      if (!cur || !prev || cur.token.value <= prev.token.value) {
        monotonic = false;
        break;
      }
    }
    if (!monotonic) continue;
    const first = sorted[0];
    if (!first) continue;
    groups.push({
      key,
      label: titleizeStem(first.token.stem, first.layer.name),
      layers: sorted.map((m) => m.layer),
      frameLabels: sorted.map((m) => m.token.label),
    });
  }
  // Stable order: by the group's first member's z_index (top-of-stack first),
  // matching the rest of the panel's ordering. BUG 2: null-coerce the z (??0, not
  // a bare subtraction that yields NaN) and add a key tiebreak so the group order
  // is deterministic even when every group's lead z is null.
  groups.sort(
    (a, b) =>
      (b.layers[0]?.z_index ?? 0) - (a.layers[0]?.z_index ?? 0) ||
      a.key.localeCompare(b.key),
  );
  return groups;
}

// --- Kind chip (job-0264 polish) --------------------------------------- //
//
// A short, color-coded chip that classifies the layer at a glance — the
// kickoff names flood / plume / hillshade / vector as the canonical examples.
// Derivation is presentation-only (no new data flow): the kind is inferred
// from `style_preset` first (most specific), then `layer_type`. Unknown
// presets fall back to the raster/vector type so every row still gets a chip.
// The label is a single lowercase word; the color tints the chip background.

interface LayerKind {
  label: string;
  color: string; // chip text + border accent (background is a faint tint of it)
}

// Ordered substring rules over style_preset — first match wins.
const KIND_RULES: ReadonlyArray<readonly [RegExp, LayerKind]> = [
  [/flood|inundation|depth|nfhl|slr|surge/, { label: "flood", color: "#4aa3ff" }],
  [/plume|dispersion|smoke|ash|concentration/, { label: "plume", color: "#c084fc" }],
  [/hillshade/, { label: "hillshade", color: "#b9a06a" }],
  [/relief|slope|aspect|dem|elevation/, { label: "terrain", color: "#b9a06a" }],
  [/fire|burn|firms|mtbs|nifc/, { label: "fire", color: "#ff7a45" }],
  [/damage|pelicun|hazus|impact/, { label: "damage", color: "#ff5d6c" }],
  [/precip|rain|qpe|streamflow|discharge|nwm/, { label: "water", color: "#36c5d6" }],
  [/population|building|impervious|density|nsi/, { label: "exposure", color: "#f6c453" }],
  [/landcover|nlcd|fuel|landfire/, { label: "landcover", color: "#5fc27e" }],
  [/gbif|inaturalist|ebird|iucn|wdpa|movebank|species|habitat/, { label: "biodiversity", color: "#5fc27e" }],
  [/admin|boundaries|roads|osm|levee|dam/, { label: "vector", color: "#9aa7b8" }],
  [/alert|storm|weather|metar|asos|raws/, { label: "weather", color: "#36c5d6" }],
];

export function layerKind(layer: ProjectLayerSummary): LayerKind {
  const preset = (layer.style_preset ?? "").toLowerCase();
  if (preset) {
    for (const [rx, kind] of KIND_RULES) {
      if (rx.test(preset)) return kind;
    }
  }
  // Fallback to the broad geometry type so every row carries a chip.
  switch (layer.layer_type) {
    case "vector":
    case "geojson":
      return { label: "vector", color: "#9aa7b8" };
    case "wms":
    case "wmts":
      return { label: "tiles", color: "#9aa7b8" };
    case "raster":
    default:
      return { label: "raster", color: "#9aa7b8" };
  }
}

function hexToRgba(hex: string, alpha: number): string {
  const h = hex.replace("#", "");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

// --- Subscription wiring ----------------------------------------------- //
//
// The App layer wires `subscribeSessionState` and `subscribeMapCommand` to
// the WebSocket; this component subscribes on mount and unsubscribes on
// unmount. Stays decoupled from the GraceWs class so tests can inject a
// stub bus.

export type SessionStateSubscriber = (p: SessionStatePayload) => void;
export type MapCommandSubscriber = (p: MapCommandPayload) => void;

// ux-batch-1 J1 (F11) — the Layers panel is user-resizable: grab its right
// border and drag to size it (the panel is left-anchored at 16, so it grows
// rightward). Width persists to localStorage. Mirrors the chat-width model.
const LAYERS_WIDTH_DEFAULT_PX = 288;
const LAYERS_WIDTH_MIN_PX = 240;
const LAYERS_WIDTH_MAX_PX = 560;
const LS_LAYERS_WIDTH = "grace2.layersWidthPx";

/** Clamp a desired layers-panel width to [min, max]; non-finite → default. */
export function clampLayersWidth(px: number): number {
  if (!Number.isFinite(px)) return LAYERS_WIDTH_DEFAULT_PX;
  return Math.max(
    LAYERS_WIDTH_MIN_PX,
    Math.min(LAYERS_WIDTH_MAX_PX, Math.round(px)),
  );
}

/** Read the persisted layers-panel width (px); default ~288 on unset/garbage. */
export function readLayersWidth(): number {
  try {
    const raw = localStorage.getItem(LS_LAYERS_WIDTH);
    if (raw === null) return LAYERS_WIDTH_DEFAULT_PX;
    return clampLayersWidth(Number(raw));
  } catch {
    return LAYERS_WIDTH_DEFAULT_PX;
  }
}

/** Persist the layers-panel width (px). Non-fatal on failure. */
export function writeLayersWidth(px: number): void {
  try {
    localStorage.setItem(LS_LAYERS_WIDTH, String(clampLayersWidth(px)));
  } catch {
    /* non-fatal */
  }
}

// --- F55 (job-0325): per-layer visibility persistence ------------------- //
//
// Root cause being fixed: on MOBILE the LayerPanel lives inside a MobileDrawer
// that returns null when collapsed (MobileDrawer.tsx). Collapsing the drawer
// UNMOUNTS the panel, discarding the useReducer state (each layer's `visible`).
// Re-opening re-seeds from session-state where `visible` is the SERVER value
// (always true from add_loaded_layer), so a layer the user had hidden snapped
// back to visible. Desktop collapse also unmounts, so the fix must be
// unmount-proof rather than relying on component lifetime.
//
// Fix: persist the user's explicit visibility toggles to localStorage keyed by
// layer_id, and apply that override on top of the incoming server `visible`
// whenever we (re-)seed the reducer. The override is PURELY ADDITIVE — it only
// exists for a layer_id the user explicitly toggled. When no override exists
// the server value is used verbatim, so a never-toggled layer (the desktop
// resting case) renders byte-identically to before this change.
const LS_LAYER_VISIBILITY = "grace2.layerVisibility";

/** Read the full {layer_id: visible} override map; {} on unset/garbage. */
export function readLayerVisibilityOverrides(): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(LS_LAYER_VISIBILITY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const out: Record<string, boolean> = {};
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof v === "boolean") out[k] = v;
    }
    return out;
  } catch {
    return {};
  }
}

/** Persist one layer's user-chosen visibility into the override map. Non-fatal. */
export function writeLayerVisibilityOverride(layerId: string, visible: boolean): void {
  try {
    const map = readLayerVisibilityOverrides();
    map[layerId] = visible;
    localStorage.setItem(LS_LAYER_VISIBILITY, JSON.stringify(map));
  } catch {
    /* non-fatal */
  }
}

/**
 * Apply persisted visibility overrides on top of a server-provided layer list.
 * For each layer: if the user explicitly toggled it before (an override key
 * exists), use the stored value; otherwise keep the server `visible` verbatim.
 * Pure (returns a new list) so it is safe to call inside a reducer / useMemo.
 * Exported for unit testing.
 */
export function applyVisibilityOverrides(
  layers: ProjectLayerSummary[],
  overrides: Record<string, boolean> = readLayerVisibilityOverrides(),
): ProjectLayerSummary[] {
  if (Object.keys(overrides).length === 0) return layers;
  return layers.map((l) => {
    // Under noUncheckedIndexedAccess `overrides[id]` is `boolean | undefined`;
    // the hasOwnProperty guard guarantees presence, so coerce to a strict
    // boolean for the `visible` field.
    const override = overrides[l.layer_id];
    return Object.prototype.hasOwnProperty.call(overrides, l.layer_id) &&
      typeof override === "boolean"
      ? { ...l, visible: override }
      : l;
  });
}

// --- F53 (job-0326): mobile swipe-right-to-delete gesture REMOVED ------- //
//
// An earlier iteration (job-0322) added a mobile swipe-RIGHT-to-delete gesture
// alongside the per-row trash control. NATE reversed that call: the swipe
// gesture is dropped ENTIRELY (swipeStartRef / swipeDx state, the touch/pointer
// swipe handlers, the visual swipe nudge, and the isHorizontalSwipeRight
// predicate are all gone). The EXPLICIT trash (delete) icon control on each row
// is now the sole delete affordance on BOTH desktop and mobile. It still opens
// the ConfirmationDialog (setPendingDeleteId path); only confirm deletes.

export interface LayerPanelProps {
  initialLayers?: ProjectLayerSummary[];
  subscribeSessionState?: (cb: SessionStateSubscriber) => () => void;
  subscribeMapCommand?: (cb: MapCommandSubscriber) => () => void;
  /** Called whenever the layer list changes (used by App.tsx to drive LayerLegend). */
  onLayersChange?: (layers: ProjectLayerSummary[]) => void;
  /** Called when the user clicks the × close button (job-0068). */
  onClose?: () => void;
  /**
   * job-0258: outbound map-command emission for user layer-control intents
   * (set-layer-opacity / set-layer-visibility / set-layer-order). App.tsx
   * wires this to `bus.pushMapCommand`, which fans out to MapView (applies
   * to the MapLibre instance) AND back into this panel's own reducer (an
   * idempotent echo — the local dispatch below already applied the same
   * change, so the echo is a no-op re-set of identical values).
   */
  onMapCommand?: (cmd: MapCommandPayload) => void;
  /**
   * F53 (job-0325) — per-layer delete. Fired with the layer_id when the user
   * clicks a row's delete (trash) control. App.tsx wires this to
   * `wsRef.current.sendDeleteLayer(id)`, which emits the `layer-delete`
   * envelope; the server removes the layer from the session's loaded_layers,
   * persists authoritatively, and echoes a fresh session-state (which removes
   * the map overlay via replace-not-reconcile). Optional so existing callers
   * that haven't wired it yet don't break — without it the row still removes
   * itself optimistically via the local remove-layer dispatch below, but the
   * deletion would not survive a reload (no server round-trip).
   */
  onDeleteLayer?: (layerId: string) => void;
  /**
   * ux-batch-1 J1 (F11) — optional controlled width (px). When provided it
   * seeds/mirrors the internal width; when omitted the panel reads/persists its
   * own width via localStorage.
   */
  width?: number;
  /** Fired with the new px width when the user drags the right border. */
  onWidthChange?: (widthPx: number) => void;
  /**
   * ux-batch-1 J1 — mobile drawer mode: the panel fills the drawer column at
   * the fixed default width and renders no resize handle (drag-sizing is a
   * desktop affordance only). Default false (desktop, draggable).
   */
  mobile?: boolean;
  /**
   * Optional TRUE projected AOI screen rectangle {left,top,right,bottom} in
   * absolute map-container coords (= viewport coords). When provided the
   * SequenceScrubber pins bottom-center of the AOI bbox instead of the
   * viewport center (item 3 — bbox snap). Threaded straight through to the
   * SequenceScrubber; LayerPanel does not use it for its own layout.
   */
  aoiRect?: ScreenRect | null;
  /**
   * Item b (NATE 2026-06-20) — an optional control node (the mobile legend
   * show/hide toggle) rendered INSIDE the expanded Layers section, at the top
   * of the panel body. The mobile legend toggle moved here off the chat
   * composer (App passes <MobileLegendToggle/>). Desktop passes nothing.
   */
  legendControl?: React.ReactNode;
}

function LayerPanelImpl({
  initialLayers,
  subscribeSessionState,
  subscribeMapCommand,
  onLayersChange,
  onClose,
  onMapCommand,
  onDeleteLayer,
  width,
  onWidthChange,
  mobile = false,
  legendControl,
  // aoiRect is kept in the props contract (App still passes it) but the
  // SequenceScrubber it fed now lives in App.tsx (JOB WEB-ANIM #157.2), so the
  // panel no longer consumes it. Intentionally not destructured.
}: LayerPanelProps): JSX.Element | null {
  const initial = useMemo<LayerPanelState>(
    // F55 (job-0325): apply persisted visibility overrides at first mount too,
    // so a remount (mobile drawer reopen) that seeds via initialLayers — not
    // the bus — also restores the user's last visibility choice.
    () => ({
      layers: sortTopFirst(
        applyVisibilityOverrides(dedupeByLayerId(initialLayers ?? [])),
      ),
    }),
    // intentionally only on mount; initialLayers is a seed, not a reactive source.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  const [state, dispatch] = useReducer(reducer, initial);

  // ux-batch-1 J1 (F11) — user-draggable panel width.
  const [panelWidth, setPanelWidth] = useState<number>(() =>
    width ?? readLayersWidth(),
  );
  useEffect(() => {
    if (typeof width === "number") setPanelWidth(clampLayersWidth(width));
  }, [width]);
  const panelWidthRef = useRef<number>(panelWidth);
  panelWidthRef.current = panelWidth;
  // The panel is left-anchored at 16, so width = pointerX - 16; clamped.
  const beginWidthDrag = useCallback(
    (e: React.PointerEvent): void => {
      e.preventDefault();
      const onMove = (ev: PointerEvent): void => {
        const next = clampLayersWidth(ev.clientX - 16);
        panelWidthRef.current = next;
        setPanelWidth(next);
        onWidthChange?.(next);
      };
      const onUp = (): void => {
        writeLayersWidth(panelWidthRef.current);
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        document.body.style.userSelect = "";
        document.body.style.cursor = "";
      };
      document.body.style.userSelect = "none";
      document.body.style.cursor = "ew-resize";
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },
    [onWidthChange],
  );
  const nudgeWidth = useCallback(
    (deltaPx: number): void => {
      setPanelWidth((prev) => {
        const next = clampLayersWidth(prev + deltaPx);
        panelWidthRef.current = next;
        writeLayersWidth(next);
        onWidthChange?.(next);
        return next;
      });
    },
    [onWidthChange],
  );

  useEffect(() => {
    const unsubs: Array<() => void> = [];
    if (subscribeSessionState) {
      unsubs.push(
        subscribeSessionState((p) => dispatch({ type: "session-state", payload: p })),
      );
    }
    if (subscribeMapCommand) {
      unsubs.push(
        subscribeMapCommand((p) => dispatch({ type: "map-command", payload: p })),
      );
    }
    return () => {
      unsubs.forEach((u) => u());
    };
  }, [subscribeSessionState, subscribeMapCommand]);

  // Notify parent of layer-list changes so App.tsx can drive LayerLegend.
  useEffect(() => {
    onLayersChange?.(state.layers);
  }, [state.layers, onLayersChange]);

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  function onDragEnd(event: DragEndEvent): void {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    // ITEM 3 (NATE 2026-06-22)  -  reorder over the INTERLEAVED panel-item list
    // (groups + single layers), not the raw layer list, so a sequential group can
    // be dragged above/below ordinary layers. After the move we EXPAND each item
    // back into concrete layer_ids (a group -> its member ids, top-of-stack first
    // = z descending) to produce the full top-first id order the map re-stacks
    // against (set-layer-order). A single layer expands to itself.
    const activeId = String(active.id);
    const overId = String(over.id);
    const oldIndex = panelItems.findIndex((it) => it.id === activeId);
    const newIndex = panelItems.findIndex((it) => it.id === overId);
    if (oldIndex === -1 || newIndex === -1) return;
    const movedItems = arrayMove(panelItems, oldIndex, newIndex);
    const reorderedIds: string[] = [];
    for (const it of movedItems) {
      if (it.kind === "group") {
        // Members are ascending-by-frame; emit them top-of-stack first
        // (z descending) so the group occupies one contiguous z-band at its new
        // panel position and frame stacking within the band is preserved.
        const memberIdsTopFirst = [...it.group.layers]
          .sort((a, b) => (b.z_index ?? 0) - (a.z_index ?? 0))
          .map((l) => l.layer_id);
        reorderedIds.push(...memberIdsTopFirst);
      } else {
        reorderedIds.push(it.id);
      }
    }
    dispatch({ type: "local-reorder", layer_ids: reorderedIds });
    // job-0179 — write-through the new z-order into the shared cache so the
    // drag-reorder survives a panel remount / reconnect. Top-first list: the
    // first element gets the HIGHEST z (renders on top). Mirrors the reducer's
    // `local-reorder` z_index assignment (length - idx).
    {
      const cache = getLayerCache();
      reorderedIds.forEach((id, idx) => {
        cache.setOverride(cache.activeCaseId, id, {
          zIndex: reorderedIds.length - idx,
        });
      });
    }
    // job-0258: emit the real map-command so MapView re-stacks the MapLibre
    // layers (moveLayer). `reorderedIds` is top-of-stack first — the
    // set-layer-order contract (contracts.ts SetLayerOrderCommand).
    onMapCommand?.({ command: "set-layer-order", layer_ids: reorderedIds });
    // eslint-disable-next-line no-console
    console.debug("[LayerPanel] reorder intent:", reorderedIds);
  }

  function onVisibilityToggle(layerId: string, visible: boolean): void {
    dispatch({ type: "local-visibility", layer_id: layerId, visible });
    // F55 (job-0325): persist the explicit choice so it survives a panel
    // unmount->remount (mobile drawer collapse). Reads back in
    // applyVisibilityOverrides at the next seed.
    writeLayerVisibilityOverride(layerId, visible);
    // job-0179 — write-through into the shared cache (the durable seatbelt; it
    // also covers the case where the Map is unmounted and never sees the bus
    // command). The localStorage write above stays for back-compat.
    {
      const cache = getLayerCache();
      cache.setOverride(cache.activeCaseId, layerId, { visible });
    }
    // job-0258: emit so MapView flips layout visibility on the live map.
    onMapCommand?.({ command: "set-layer-visibility", layer_id: layerId, visible });
    // eslint-disable-next-line no-console
    console.debug("[LayerPanel] visibility intent:", { layerId, visible });
  }

  function onOpacityChange(layerId: string, opacity: number): void {
    const clamped = clamp01(opacity);
    dispatch({ type: "local-opacity", layer_id: layerId, opacity: clamped });
    // job-0179 — write-through into the shared cache so the opacity edit
    // survives a panel remount / WS reconnect (persisted to IndexedDB).
    {
      const cache = getLayerCache();
      cache.setOverride(cache.activeCaseId, layerId, { opacity: clamped });
    }
    // job-0258: emit so MapView updates the paint properties on the live map.
    onMapCommand?.({ command: "set-layer-opacity", layer_id: layerId, opacity: clamped });
    // eslint-disable-next-line no-console
    console.debug("[LayerPanel] opacity intent:", { layerId, opacity: clamped });
  }

  // F53 (job-0326): delete is gated behind a ConfirmationDialog ("Confirmation
  // before consequence" Memory invariant — matches CasesPanel's delete UX).
  // `pendingDeleteId` holds the layer awaiting confirmation; the per-row trash
  // button (the sole delete affordance, desktop + mobile) sets it to open the
  // dialog. The actual destructive path only runs on confirm.
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);

  // The layer object behind the pending delete (for the dialog's name in copy).
  const pendingDeleteLayer = useMemo(
    () => state.layers.find((l) => l.layer_id === pendingDeleteId) ?? null,
    [state.layers, pendingDeleteId],
  );

  /** Open the confirm dialog for a layer (trash button — desktop + mobile). */
  const requestDelete = useCallback((layerId: string): void => {
    setPendingDeleteId(layerId);
  }, []);

  /** Cancel a pending delete — clears the dialog, layer stays. */
  const cancelDelete = useCallback((): void => {
    setPendingDeleteId(null);
  }, []);

  /**
   * Run the (previously immediate) delete path AFTER the user confirms.
   *
   * F53 (job-0325): optimistic local removal so the row disappears instantly
   * without waiting for the server round-trip. The authoritative session-state
   * echo (sans the deleted layer) then confirms it; the local map-command also
   * tells MapView to drop the overlay immediately. Then send the server-
   * authoritative delete (persists + emits new session-state).
   */
  const confirmDelete = useCallback((): void => {
    const layerId = pendingDeleteId;
    setPendingDeleteId(null);
    if (!layerId) return;
    dispatch({ type: "map-command", payload: { command: "remove-layer", layer_id: layerId } });
    onMapCommand?.({ command: "remove-layer", layer_id: layerId });
    onDeleteLayer?.(layerId);
    // eslint-disable-next-line no-console
    console.debug("[LayerPanel] delete confirmed:", { layerId });
  }, [pendingDeleteId, onMapCommand, onDeleteLayer]);

  // --- Sequential-layer grouping (NATE) -------------------------------- //
  //
  // Detect enumerated temporal raster sequences among the active layers and
  // collapse each into ONE "sequential group" row + drive a bottom scrubber.
  //
  // JOB WEB-ANIM (#157.1): the PLAYBACK state (active group / per-group frame
  // index / playing) + the advance interval used to live HERE in LayerPanel, so
  // closing/unmounting the panel (mobile drawer collapse, desktop close) killed
  // the animation and dropped the scrubber. They now live in the module-level
  // AnimationController (lib/animation_controller.ts); the LayerPanel is a
  // CONTROL over it. The controller drives the MAP frame visibility directly
  // (via the emitter registered in Map.tsx), so frames keep advancing while the
  // panel is closed. The LayerPanel still mirrors the active frame into its OWN
  // reducer + the persisted overrides so the panel rows reflect the current
  // frame when it IS open — but it no longer emits the map-command for frame
  // stepping (the controller is the single map driver) to avoid double-toggling.
  const groups = useMemo(
    () => detectSequentialGroups(state.layers),
    [state.layers],
  );
  // Set of layer_ids that belong to SOME group (so ordinary-row render skips
  // them — they live in the group row instead).
  const groupedIds = useMemo(() => {
    const s = new Set<string>();
    for (const g of groups) for (const l of g.layers) s.add(l.layer_id);
    return s;
  }, [groups]);

  // ITEM 3 (NATE 2026-06-22)  -  a single z-ordered list interleaving sequential
  // GROUP rows with ordinary single-LAYER rows, so the animation group can be
  // dragged ABOVE/BELOW any other layer in the LayerPanel and that order is
  // reflected in the MapLibre stack. Each panel item is sortable; the group's
  // sortable id is the synthetic `group:<key>` (distinct from any layer_id) and
  // a single layer's id is its layer_id. The representative z for ordering is the
  // group's top member z (groups already sort their members ascending by frame,
  // top-first by z), matching the rest of the panel's top-of-stack-first order.
  type PanelItem =
    | { kind: "group"; id: string; z: number; group: SequentialGroup }
    | { kind: "layer"; id: string; z: number; layer: ProjectLayerSummary };
  const panelItems = useMemo<PanelItem[]>(() => {
    const items: PanelItem[] = [];
    for (const g of groups) {
      // The group's representative z = the MAX member z (its top-of-stack member),
      // so a group sits where its highest frame would sit among single layers.
      const z = g.layers.reduce(
        (max, l) => Math.max(max, l.z_index ?? 0),
        Number.NEGATIVE_INFINITY,
      );
      items.push({
        kind: "group",
        id: `group:${g.key}`,
        z: Number.isFinite(z) ? z : 0,
        group: g,
      });
    }
    for (const l of state.layers) {
      if (groupedIds.has(l.layer_id)) continue;
      items.push({ kind: "layer", id: l.layer_id, z: l.z_index ?? 0, layer: l });
    }
    // Top-of-stack first (z descending), matching sortTopFirst. BUG 2: tiebreak
    // on the synthetic id so the interleaved group/layer order is deterministic
    // when z values tie (e.g. all-null z_index), mirroring compareLayersTopFirst.
    items.sort((a, b) => b.z - a.z || a.id.localeCompare(b.id));
    return items;
  }, [groups, state.layers, groupedIds]);

  // The shared playback controller (process-global; survives panel unmount).
  const animController = useMemo(() => getAnimationController(), []);
  // Subscribe so the panel re-renders when playback state changes (the scrubber
  // play button outside the panel, auto-advance ticks, etc.).
  const animState = useAnimationState(animController);
  const { activeGroupKey, playing } = animState;

  // Push the detected groups into the controller whenever they change. This is
  // the panel's role as a CONTROL: it owns detection (it has the layer list);
  // the controller owns playback. setGroups keeps the active key valid + seeds
  // each new group's default frame (last frame) + stops play when none remain.
  useEffect(() => {
    animController.setGroups(
      groups.map((g) => ({
        key: g.key,
        label: g.label,
        layerIds: g.layers.map((l) => l.layer_id),
        frameLabels: g.frameLabels,
      })),
    );
  }, [groups, animController]);

  // ITEM 2 (NATE 2026-06-24) - keep the controller's hidden-group set in lockstep
  // with the actual layer visibility, so frame advancing STOPS whenever a group's
  // layers are all hidden - regardless of HOW they were hidden (the group eye, an
  // individual-frame toggle that empties the group, or a persisted-override
  // re-seed on a panel remount). A group is "hidden" iff NONE of its members are
  // visible; "shown" iff at least one is. This single derived sync also covers
  // the case where the group eye path and the controller momentarily disagree.
  useEffect(() => {
    for (const g of groups) {
      const anyVisible = g.layers.some((l) => l.visible);
      const shouldHide = !anyVisible;
      if (animController.isGroupHidden(g.key) !== shouldHide) {
        animController.setGroupHidden(g.key, shouldHide);
      }
    }
  }, [groups, animState, animController]);

  // Which groups are expanded (collapsible) — purely a panel-local concern, so
  // it stays in component state. Collapsed by default (shrink N rows to one).
  const [expandedGroups, setExpandedGroups] = useState<Record<string, boolean>>({});

  /** The resolved active frame index for a group (default = last frame). */
  const frameIndexFor = useCallback(
    (g: SequentialGroup): number => {
      return animController.frameIndexFor(g.key);
    },
    [animController, animState],
  );

  /**
   * Mirror the active frame into the panel's OWN reducer + persisted overrides
   * (show frame i, hide every sibling) WITHOUT emitting a map-command — the
   * AnimationController emitter (registered in Map.tsx) is the single driver of
   * the map's frame visibility. Keeps the panel rows + the legend in sync with
   * the current frame when the panel is open.
   */
  const syncFrameVisibilityLocal = useCallback(
    (g: SequentialGroup, index: number): void => {
      const clamped = Math.max(0, Math.min(g.layers.length - 1, index));
      g.layers.forEach((layer, i) => {
        const wantVisible = i === clamped;
        if (layer.visible !== wantVisible) {
          dispatch({
            type: "local-visibility",
            layer_id: layer.layer_id,
            visible: wantVisible,
          });
          writeLayerVisibilityOverride(layer.layer_id, wantVisible);
          getLayerCache().setOverride(getLayerCache().activeCaseId, layer.layer_id, {
            visible: wantVisible,
          });
        }
      });
    },
    // dispatch is stable; g + index are passed in. No deps needed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  /**
   * Step a group to frame `index`: tell the controller (which records the frame
   * + drives the map) AND mirror into the panel's reducer/overrides. The
   * controller becomes the active-group target as a side effect of stepGroupTo.
   */
  const stepGroupTo = useCallback(
    (g: SequentialGroup, index: number): void => {
      const clamped = Math.max(0, Math.min(g.layers.length - 1, index));
      animController.stepGroupTo(g.key, clamped);
      syncFrameVisibilityLocal(g, clamped);
    },
    [animController, syncFrameVisibilityLocal],
  );

  /**
   * ITEM 4 (NATE 2026-06-22)  -  toggle the WHOLE animation group's visibility via
   * the group row's far-left eye (matching how an ordinary layer's eye works).
   *   - OFF: hide EVERY member (incl. the currently-shown active frame) so the
   *     animation disappears from the map entirely.
   *   - ON: restore the animation by showing ONLY the active frame (single-frame
   *     invariant) and hiding the rest, exactly like a normal frame step.
   * Each per-member change flows through the SAME single-layer visibility path
   * (dispatch + persisted overrides + cache write-through + map-command) so the
   * map, the panel rows, and the durability seatbelt all stay in lockstep.
   */
  const onGroupVisibilityToggle = useCallback(
    (g: SequentialGroup, visible: boolean): void => {
      // ITEM 2 (NATE 2026-06-24) - tell the AnimationController the group is
      // hidden/shown so it STOPS advancing the hidden group's frames (and the
      // App-level scrubber halts / re-points to a visible group). Showing it
      // resumes from the CURRENT frame (no force-restart). Done first so the
      // controller's interval is torn down before the per-layer visibility
      // writes ripple through.
      animController.setGroupHidden(g.key, !visible);
      if (!visible) {
        // Hide all members (the active frame included).
        g.layers.forEach((layer) => {
          if (layer.visible) onVisibilityToggle(layer.layer_id, false);
        });
        return;
      }
      // Show only the active frame, hide the rest (single-frame invariant).
      const active = animController.frameIndexFor(g.key);
      g.layers.forEach((layer, i) => {
        const wantVisible = i === active;
        if (layer.visible !== wantVisible) {
          onVisibilityToggle(layer.layer_id, wantVisible);
        }
      });
    },
    // animController is a stable singleton; onVisibilityToggle is a per-render
    // function declaration, so include it so we never capture a stale closure.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [animController],
  );

  // When a group first becomes active, make sure exactly ONE frame is visible.
  // Runs once per group key appearing — collapses a freshly-detected N-visible
  // stack down to a single visible frame (both on the map, via the controller,
  // and in the panel reducer). Guarded so it only fires when the group is NOT
  // already single-framed (>1 visible). Mirrors the controller's seeded default
  // frame (last frame) so the panel + map agree.
  const initializedGroupsRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    for (const g of groups) {
      if (initializedGroupsRef.current.has(g.key)) continue;
      initializedGroupsRef.current.add(g.key);
      const visibleCount = g.layers.filter((l) => l.visible).length;
      if (visibleCount !== 1) {
        stepGroupTo(g, frameIndexFor(g));
      }
    }
    // Drop keys for groups that no longer exist so re-formed groups re-init.
    const live = new Set(groups.map((g) => g.key));
    for (const k of Array.from(initializedGroupsRef.current)) {
      if (!live.has(k)) initializedGroupsRef.current.delete(k);
    }
  }, [groups, stepGroupTo, frameIndexFor]);

  // JOB WEB-ANIM (#157.1) — mirror the CONTROLLER's active frame back into the
  // panel's reducer whenever the controller changes it externally (an auto-play
  // tick or a step from the App-owned scrubber, which happen even while this
  // panel is closed). This keeps the panel's group rows / radio dots showing the
  // right frame when the panel IS open, without the panel owning the frame state.
  // It does NOT emit map-commands (the controller already drove the map); it only
  // updates the local reducer + persisted overrides via syncFrameVisibilityLocal.
  useEffect(() => {
    for (const g of groups) {
      const idx = animController.frameIndexFor(g.key);
      const visibleCount = g.layers.filter((l) => l.visible).length;
      const activeVisible = g.layers[idx]?.visible === true;
      // ITEM 4 (NATE 2026-06-22)  -  DO NOT re-show a group the user deliberately
      // hid via the group eye. A fully-hidden group has visibleCount === 0; the
      // old condition (`visibleCount !== 1`) treated that as "needs collapsing"
      // and re-showed the active frame, fighting the hide-all toggle. We now
      // re-sync ONLY when there is MORE than one frame visible (collapse a freshly
      // -published N-visible stack to one) OR exactly one is visible but it's the
      // WRONG one (the controller advanced the active frame). A zero-visible group
      // is the user's hide intent and is left alone.
      const wrongSingle = visibleCount === 1 && !activeVisible;
      if (visibleCount > 1 || wrongSingle) {
        syncFrameVisibilityLocal(g, idx);
      }
    }
  }, [animState, groups, animController, syncFrameVisibilityLocal]);

  // Tweak 2 (job-0065): hide the panel entirely when no layers are loaded.
  // Hooks must all run before this conditional return.
  if (state.layers.length === 0) return null;

  return (
    <aside
      data-testid="grace2-layer-panel"
      style={{
        position: "absolute",
        left: 16,
        top: 16,
        bottom: 16,
        // Desktop: user-dragged width. Mobile drawer: fixed default (no drag).
        width: mobile ? LAYERS_WIDTH_DEFAULT_PX : clampLayersWidth(panelWidth),
        // Subtle gradient + hairline border + soft shadow for a sleeker,
        // more modern panel than the flat slab (job-0264 polish).
        // LANE C (flicker): the gradient below is already ~0.96 alpha (near
        // opaque), so the backdrop-filter blur added almost no visual depth but
        // forced a backdrop RE-COMPOSITE against the continuously-repainting map
        // on every frame - a classic backdrop-filter shimmer/flash over animated
        // content (worse under the v5 globe's more-frequent repaints). Dropped.
        background:
          "linear-gradient(180deg, rgba(26,27,33,0.96) 0%, rgba(18,19,24,0.96) 100%)",
        color: "#e8e8ec",
        borderRadius: 12,
        border: "1px solid rgba(255,255,255,0.06)",
        boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
        display: "flex",
        flexDirection: "column",
        fontFamily:
          "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
        fontSize: 13,
        overflow: "hidden",
      }}
    >
      {/* ux-batch-1 J1 (F11) — right-border resize grab strip. The panel is
          left-anchored, so dragging this rightward widens it. role=separator +
          arrow-key nudge for keyboard a11y. Desktop only. */}
      {!mobile && (
      <div
        data-testid="grace2-layer-panel-resize-handle"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize layers panel (drag, or use arrow keys)"
        tabIndex={0}
        onPointerDown={beginWidthDrag}
        onKeyDown={(e) => {
          if (e.key === "ArrowRight") { e.preventDefault(); nudgeWidth(24); }
          else if (e.key === "ArrowLeft") { e.preventDefault(); nudgeWidth(-24); }
        }}
        style={{
          position: "absolute",
          right: 0,
          top: 0,
          bottom: 0,
          width: 6,
          cursor: "ew-resize",
          zIndex: 6,
          touchAction: "none",
        }}
      />
      )}
      <header
        style={{
          padding: "12px 14px",
          borderBottom: "1px solid rgba(255,255,255,0.06)",
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        <strong
          style={{ fontSize: 13, letterSpacing: 0.3, fontWeight: 600 }}
        >
          Layers
        </strong>
        <span
          data-testid="grace2-layer-panel-count"
          style={{
            color: "#7d8794",
            fontSize: 11,
            background: "rgba(255,255,255,0.06)",
            borderRadius: 999,
            padding: "1px 8px",
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {state.layers.length}
        </span>
        <span style={{ flex: 1 }} />
        {onClose && (
          <button
            data-testid="grace2-layer-panel-close"
            aria-label="Close layer panel"
            onClick={onClose}
            style={{
              background: "none",
              border: "none",
              color: "#8a929e",
              cursor: "pointer",
              lineHeight: 1,
              padding: "0 2px",
              display: "flex",
              alignItems: "center",
              transition: "color 120ms ease",
            }}
            onMouseEnter={(e) => (e.currentTarget.style.color = "#e8e8ec")}
            onMouseLeave={(e) => (e.currentTarget.style.color = "#8a929e")}
          >
            <IconClose size={16} />
          </button>
        )}
      </header>
      <div
        style={{
          flex: 1,
          overflowY: "auto",
          padding: 8,
          display: "flex",
          flexDirection: "column",
          gap: 6,
        }}
      >
        {/* Item b (NATE 2026-06-20) — the MOBILE legend show/hide toggle lives
            here, at the top of the expanded Layers section, off the chat
            composer. Desktop passes nothing (legend keeps its own pill). */}
        {legendControl ? (
          <div data-testid="grace2-layer-panel-legend-control">{legendControl}</div>
        ) : null}
        {state.layers.length === 0 && (
          <p
            data-testid="grace2-layer-panel-empty"
            style={{
              color: "#6b7280",
              margin: "auto",
              fontSize: 12,
              fontStyle: "italic",
            }}
          >
            No layers yet
          </p>
        )}
        <DndContext
          sensors={sensors}
          collisionDetection={closestCenter}
          onDragEnd={onDragEnd}
        >
          {/* ITEM 3 (NATE 2026-06-22)  -  ONE interleaved sortable list of panel
              items: sequential GROUP rows and ordinary single-LAYER rows share a
              single z-order, so a group can be dragged above/below any other
              layer. Each item is a sortable (the group's id is `group:<key>`). */}
          <SortableContext
            items={panelItems.map((it) => it.id)}
            strategy={verticalListSortingStrategy}
          >
            {panelItems.map((it) =>
              it.kind === "group" ? (
                <SortableGroupRow
                  key={it.id}
                  sortableId={it.id}
                  group={it.group}
                  activeIndex={frameIndexFor(it.group)}
                  expanded={!!expandedGroups[it.group.key]}
                  isScrubberTarget={it.group.key === activeGroupKey}
                  playing={playing && it.group.key === activeGroupKey}
                  onPlayToggle={() => {
                    // Make this group the playback target, then toggle play on the
                    // shared controller (interval lives outside the panel).
                    animController.setActiveGroup(it.group.key);
                    animController.togglePlaying();
                  }}
                  onToggleExpand={() =>
                    setExpandedGroups((prev) => ({
                      ...prev,
                      [it.group.key]: !prev[it.group.key],
                    }))
                  }
                  onVisibilityToggle={(visible) =>
                    onGroupVisibilityToggle(it.group, visible)
                  }
                  onStep={(idx) => {
                    // stepGroupTo already sets the controller's active group.
                    stepGroupTo(it.group, idx);
                  }}
                  onOpacityChange={onOpacityChange}
                  onRequestDelete={requestDelete}
                />
              ) : (
                <SortableRow
                  key={it.id}
                  layer={it.layer}
                  onVisibilityToggle={onVisibilityToggle}
                  onOpacityChange={onOpacityChange}
                  onRequestDelete={requestDelete}
                />
              ),
            )}
          </SortableContext>
        </DndContext>
      </div>
      {/* JOB WEB-ANIM (#157.2): the bottom-center SCRUBBER is NO LONGER rendered
          from inside LayerPanel. It now lives in App.tsx and renders WHENEVER a
          sequence group is active on the shared AnimationController — regardless
          of whether the Layers panel is open. (Previously it only showed when the
          panel was mounted, so closing the panel dropped the scrubber AND, since
          the play interval lived in the scrubber, killed the animation.) The
          panel keeps the group ROWS (with their own play button + frame readout);
          the floating scrubber is App-owned now. */}
      {/* F53 (job-0326): confirm-before-delete. The per-row trash control (the
          sole delete affordance, desktop + mobile) opens this dialog; the
          destructive path runs only on confirm. The dialog itself portals to
          document.body (ConfirmationDialog) so it overlays full-screen above
          this absolutely-positioned, backdrop-filtered panel. Distinct testId
          so tests + screen readers don't collide with the Cases delete dialog. */}
      {pendingDeleteLayer && (
        <ConfirmationDialog
          testId="grace2-layer-delete-dialog"
          title="Delete layer?"
          message={`Remove "${pendingDeleteLayer.name}" from this case? This cannot be undone.`}
          confirmLabel="Delete"
          cancelLabel="Cancel"
          onConfirm={confirmDelete}
          onCancel={cancelDelete}
        />
      )}
    </aside>
  );
}

// FLASH FIX (Lane 1a): MEMOIZE the panel so it does NOT re-render whenever App
// re-renders (e.g. an unrelated App state change, or a session-state heartbeat
// that App already short-circuits). All props App passes are now stable: the
// bus subscribe/push fns come from a useMemo'd bus, callbacks are useCallback'd
// (onClose=collapseLeft, onDeleteLayer=handleDeleteLayer), the setters are
// stable, and `initialLayers`/`layers` are ref-stable across identical
// heartbeats (mergeSnapshot identity fix). The panel's OWN layer state is driven
// by its bus subscription + reducer (with layerSetsEqual short-circuit), so
// memoizing the parent-driven render path is safe and kills the heartbeat flash.
export const LayerPanel = memo(LayerPanelImpl);

// --- Sortable row ----------------------------------------------------- //

interface SortableRowProps {
  layer: ProjectLayerSummary;
  onVisibilityToggle: (layerId: string, visible: boolean) => void;
  onOpacityChange: (layerId: string, opacity: number) => void;
  /** Open the delete-confirm dialog for this row (the trash control). */
  onRequestDelete: (layerId: string) => void;
}

function SortableRow({
  layer,
  onVisibilityToggle,
  onOpacityChange,
  onRequestDelete,
}: SortableRowProps): JSX.Element {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } =
    useSortable({ id: layer.layer_id });

  // Hover (or active drag) reveals the compact opacity slider — keeps the
  // resting row clean while controls stay one gesture away. The row also
  // stays "expanded" while the pointer is over it.
  const [hovered, setHovered] = useState(false);
  // ITEM 2 (NATE 2026-06-22) - a far-left EXPAND control in the grabber slot,
  // matching the sequential-group row. Clicking it STICKILY reveals the row's
  // detail (the opacity slider) so a single layer + a frame group share the same
  // row anatomy. Hover still reveals opacity transiently; the chevron makes it
  // persistent (and is the keyboard/touch path where hover doesn't exist).
  const [expanded, setExpanded] = useState(false);
  const showOpacity = hovered || expanded || isDragging;
  const kind = layerKind(layer);
  const dimmed = !layer.visible;
  // ux-batch-1 J3 (F8) — a layer with undefined/NaN opacity left the range
  // input uncontrolled, so the browser parked the thumb at its default CENTRE
  // (0.5) while the label still read 0% (value↔position mismatch the user
  // reported). Resolve to a finite [0,1] value once, defaulting to fully
  // opaque, and feed it to BOTH the slider value and the % label so they
  // always agree. A real 0 (transparent) is preserved.
  const safeOpacity =
    typeof layer.opacity === "number" && Number.isFinite(layer.opacity)
      ? clamp01(layer.opacity)
      : 1;

  // dnd-kit's transform drives the row's position during a vertical reorder
  // drag; null for a row at rest.
  const dndTransform = CSS.Transform.toString(transform) || undefined;

  const style: React.CSSProperties = {
    transform: dndTransform,
    transition:
      transition ?? "background 140ms ease, border-color 140ms ease, transform 160ms ease",
    background: isDragging
      ? "rgba(70,110,170,0.28)"
      : hovered
        ? "rgba(255,255,255,0.06)"
        : "rgba(255,255,255,0.03)",
    border: `1px solid ${
      isDragging ? "rgba(120,160,220,0.5)" : "rgba(255,255,255,0.06)"
    }`,
    borderRadius: 8,
    padding: "7px 9px",
    display: "flex",
    flexDirection: "column",
    gap: showOpacity ? 7 : 0,
    opacity: isDragging ? 0.9 : 1,
    boxShadow: isDragging ? "0 6px 18px rgba(0,0,0,0.45)" : "none",
  };

  return (
    <div
      ref={setNodeRef}
      style={{ ...style, cursor: "grab", touchAction: "none" }}
      data-testid="layer-row"
      data-layer-id={layer.layer_id}
      data-expanded={expanded}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      // ITEM 2 (NATE 2026-06-22) - the whole card body is the drag handle (the
      // same model the sequential-group row uses), so single layers + frame
      // groups share one row anatomy: pointer-down on any non-button region
      // starts the reorder. Buttons (expand / eye / delete) stopPropagation.
      {...attributes}
      {...listeners}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
        {/* ITEM 2 - far-left EXPAND control in the grabber slot, aligned with the
            sequential-group row's chevron so the eye column lines up across all
            rows. Toggles the sticky opacity detail. A button -> stopPropagation
            so it never starts a card drag. */}
        <button
          type="button"
          data-testid="layer-expand"
          data-legend-no-drag=""
          aria-label={expanded ? `collapse ${layer.name}` : `expand ${layer.name}`}
          aria-expanded={expanded}
          title={expanded ? "Hide opacity" : "Show opacity"}
          onPointerDown={(e) => e.stopPropagation()}
          onClick={(e) => {
            e.stopPropagation();
            setExpanded((v) => !v);
          }}
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            width: 16,
            height: 22,
            flexShrink: 0,
            padding: 0,
            background: "transparent",
            border: "none",
            color: hovered ? "#8a929e" : "#5a626d",
            cursor: "pointer",
            transition: "color 120ms ease",
          }}
        >
          {expanded ? <IconChevronDown size={13} /> : <IconChevronRight size={13} />}
        </button>
        {/* Eye toggle. The checkbox input is visually hidden (overlaid) so the
            existing data-testid + a11y contract are preserved while the
            user sees the shared Phosphor eye icon (IconEye / IconEyeOff).
            stopPropagation on pointer-down so toggling never starts a drag. */}
        <label
          data-legend-no-drag=""
          onPointerDown={(e) => e.stopPropagation()}
          style={{
            position: "relative",
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            width: 22,
            height: 22,
            flexShrink: 0,
            cursor: "pointer",
            color: layer.visible ? "#cfd4db" : "#5a626d",
            transition: "color 120ms ease",
          }}
          title={layer.visible ? "Hide layer" : "Show layer"}
        >
          <input
            type="checkbox"
            checked={layer.visible}
            onChange={(e) => onVisibilityToggle(layer.layer_id, e.target.checked)}
            aria-label={`visibility for ${layer.name}`}
            data-testid="layer-visibility"
            style={{
              position: "absolute",
              inset: 0,
              margin: 0,
              opacity: 0,
              cursor: "pointer",
            }}
          />
          {layer.visible ? <IconEye size={15} /> : <IconEyeOff size={15} />}
        </label>
        <span
          style={{
            flex: 1,
            minWidth: 0,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            fontSize: 12.5,
            color: dimmed ? "#8a929e" : "#e8e8ec",
            transition: "color 120ms ease",
          }}
          title={layer.name}
        >
          {layer.name}
        </span>
        <span
          data-testid="layer-kind-chip"
          data-kind={kind.label}
          title={layer.style_preset ?? kind.label}
          style={{
            flexShrink: 0,
            fontSize: 9.5,
            fontWeight: 600,
            letterSpacing: 0.3,
            textTransform: "uppercase",
            color: kind.color,
            background: hexToRgba(kind.color, 0.14),
            border: `1px solid ${hexToRgba(kind.color, 0.32)}`,
            borderRadius: 5,
            padding: "1px 6px",
            lineHeight: "15px",
          }}
        >
          {kind.label}
        </span>
        {/* F53 (job-0326): per-row delete control — the SOLE delete affordance
            on BOTH desktop and mobile (the mobile swipe gesture was dropped).
            Revealed on hover (like the opacity row) to keep the resting row
            clean. `onPointerDown` stopPropagation guards against the dnd-kit
            PointerSensor treating a delete press as the start of a drag. Clicking
            OPENS the confirm dialog (onRequestDelete); only confirm deletes. */}
        <button
          aria-label={`delete layer ${layer.name}`}
          title="Delete layer"
          data-testid="layer-delete"
          onPointerDown={(e) => e.stopPropagation()}
          onClick={(e) => {
            e.stopPropagation();
            onRequestDelete(layer.layer_id);
          }}
          style={{
            flexShrink: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            width: 22,
            height: 22,
            padding: 0,
            background: "transparent",
            border: "none",
            borderRadius: 5,
            color: hovered ? "#a8616b" : "transparent",
            cursor: "pointer",
            transition: "color 120ms ease, background 120ms ease",
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.color = "#ff5d6c";
            e.currentTarget.style.background = "rgba(255,93,108,0.12)";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.color = hovered ? "#a8616b" : "transparent";
            e.currentTarget.style.background = "transparent";
          }}
        >
          <IconDelete size={14} />
        </button>
      </div>
      {/* Opacity row: collapses to 0-height when not hovered for a clean
          resting state, expands smoothly on hover. Always mounted so the
          slider's data-testid + value are stable for tests + screen readers. */}
      <div
        data-testid="layer-opacity-row"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          overflow: "hidden",
          maxHeight: showOpacity ? 24 : 0,
          opacity: showOpacity ? 1 : 0,
          transition: "max-height 160ms ease, opacity 160ms ease",
        }}
      >
        <input
          type="range"
          min={0}
          max={1}
          step={0.01}
          value={safeOpacity}
          // ITEM 2 - the card body is now the drag handle, so stop the slider's
          // pointer-down from starting a reorder while the user drags the thumb.
          data-legend-no-drag=""
          onPointerDown={(e) => e.stopPropagation()}
          onChange={(e) =>
            onOpacityChange(layer.layer_id, Number(e.target.value))
          }
          aria-label={`opacity for ${layer.name}`}
          data-testid="layer-opacity"
          style={{
            flex: 1,
            // 16px box so the native thumb (~14-16px) fits INSIDE the
            // element — at 4px it overflowed and the row's overflow:hidden
            // clipped the dot top+bottom (user-reported). The track itself
            // still renders thin; only the hit/box height grows.
            height: 16,
            accentColor: kind.color,
            cursor: "pointer",
          }}
        />
        <span
          style={{
            fontSize: 10,
            color: "#9aa1ab",
            width: 30,
            textAlign: "right",
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {(safeOpacity * 100).toFixed(0)}%
        </span>
      </div>
    </div>
  );
}

// --- Sequential group row (sortable) ---------------------------------- //
//
// One consolidated row standing in for N enumerated temporal frames (e.g. the
// 3 HRRR forecast hours). Collapsible: expand to reveal each member frame as a
// compact sub-row (visibility/opacity/delete still work on the individual
// frames via the same callbacks).
//
// ITEM 2/3/4 (NATE 2026-06-22) redesign:
//   - The row now LOOKS like an ordinary layer card: header is
//     [EYE] [NAME] [X/N counter] [PLAY] left->right.
//   - ITEM 4: the FAR-LEFT eye toggles the WHOLE group's visibility (all frames
//     hide/show), aligned with ordinary layers' visibility eye.
//   - ITEM 2: there is NO dedicated drag-grabber. The whole card body (any
//     NON-button region) is the drag handle (dnd-kit attributes+listeners on the
//     card div), so pointer-down on the name/blank area starts a reorder.
//   - ITEM 3: the row is a SORTABLE item (useSortable on `sortableId`), so it can
//     be dragged ABOVE/BELOW any other layer in the panel z-order.
//   - A subtle expand chevron remains (after the counter) so per-frame sub-rows
//     are still reachable; it stop-propagates so it never starts a drag.

interface SortableGroupRowProps {
  /** dnd-kit sortable id for this group (`group:<key>`). */
  sortableId: string;
  group: SequentialGroup;
  activeIndex: number;
  expanded: boolean;
  isScrubberTarget: boolean;
  onToggleExpand: () => void;
  onStep: (index: number) => void;
  onOpacityChange: (layerId: string, opacity: number) => void;
  onRequestDelete: (layerId: string) => void;
  /** ITEM 4  -  toggle the whole group's visibility (all frames). */
  onVisibilityToggle: (visible: boolean) => void;
  /** Whether the sequence is auto-playing (drives the play/pause icon). */
  playing: boolean;
  /** Toggle auto-play. */
  onPlayToggle: () => void;
}

function SortableGroupRow({
  sortableId,
  group,
  activeIndex,
  expanded,
  isScrubberTarget,
  playing,
  onPlayToggle,
  onToggleExpand,
  onStep,
  onOpacityChange,
  onRequestDelete,
  onVisibilityToggle,
}: SortableGroupRowProps): JSX.Element {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } =
    useSortable({ id: sortableId });
  const n = group.layers.length;
  const idx = Math.max(0, Math.min(n - 1, activeIndex));
  // The active member's kind drives the chip accent (same family as the rows).
  // Falls back to the first member then a synthetic raster so the row never
  // crashes if `idx` momentarily outruns the (always >=2) member list.
  const activeLayer = group.layers[idx] ?? group.layers[0];
  const kind = activeLayer
    ? layerKind(activeLayer)
    : { label: "raster", color: "#9aa7b8" };
  // ITEM 4  -  the group is "visible" when ANY member is visible (the active frame
  // shows). Toggling drives onVisibilityToggle(!groupVisible).
  const groupVisible = group.layers.some((l) => l.visible);
  // Per-group opacity readout (drives every member together). Resolve to a
  // finite [0,1] once — same defaulting rule as the per-row slider.
  const groupOpacity =
    activeLayer &&
    typeof activeLayer.opacity === "number" &&
    Number.isFinite(activeLayer.opacity)
      ? clamp01(activeLayer.opacity)
      : 1;

  const dndTransform = CSS.Transform.toString(transform) || undefined;

  return (
    <div
      ref={setNodeRef}
      data-testid="layer-group-row"
      data-group-key={group.key}
      data-frame-count={n}
      data-active-index={idx}
      // ITEM 2  -  the whole card body is the drag handle: dnd-kit
      // attributes+listeners ride on the card div, so pointer-down on any
      // non-button area starts the reorder (buttons stopPropagation below).
      {...attributes}
      {...listeners}
      style={{
        transform: dndTransform,
        transition:
          transition ?? "background 140ms ease, border-color 140ms ease, transform 160ms ease",
        background: isDragging
          ? "rgba(70,110,170,0.28)"
          : isScrubberTarget
            ? "rgba(74,163,255,0.10)"
            : "rgba(255,255,255,0.03)",
        border: `1px solid ${
          isDragging
            ? "rgba(120,160,220,0.5)"
            : isScrubberTarget
              ? "rgba(74,163,255,0.35)"
              : "rgba(255,255,255,0.08)"
        }`,
        borderRadius: 8,
        padding: "7px 9px",
        display: "flex",
        flexDirection: "column",
        gap: expanded ? 6 : 0,
        // ITEM 2  -  the card is a drag handle; show the grab cursor on the body.
        cursor: "grab",
        touchAction: "none",
        opacity: isDragging ? 0.9 : 1,
        boxShadow: isDragging ? "0 6px 18px rgba(0,0,0,0.45)" : "none",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
        {/* ITEM 2 (NATE 2026-06-22)  -  FAR-LEFT expand chevron in the grabber
            slot, aligned with the single-layer row's expand control so the eye
            column lines up across all rows (single + group share one anatomy).
            Toggles the per-frame sub-rows. A button -> stopPropagation so it
            never starts a card drag. */}
        <button
          type="button"
          data-testid="layer-group-expand"
          data-legend-no-drag=""
          aria-label={expanded ? "Collapse sequence" : "Expand sequence"}
          aria-expanded={expanded}
          title={expanded ? "Collapse sequence" : "Expand sequence"}
          onPointerDown={(e) => e.stopPropagation()}
          onClick={(e) => {
            e.stopPropagation();
            onToggleExpand();
          }}
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            width: 16,
            height: 22,
            flexShrink: 0,
            padding: 0,
            background: "transparent",
            border: "none",
            color: "#8a929e",
            cursor: "pointer",
          }}
        >
          {expanded ? <IconChevronDown size={13} /> : <IconChevronRight size={13} />}
        </button>
        {/* ITEM 2/4  -  the group visibility eye, in the SAME column as the
            single-layer row's eye (chevron to its left). Toggles the WHOLE
            group's visibility (all frames). The visually-hidden checkbox
            preserves the a11y + test contract. stopPropagation on pointer-down
            so toggling visibility never starts a card drag. */}
        <label
          data-legend-no-drag=""
          onPointerDown={(e) => e.stopPropagation()}
          style={{
            position: "relative",
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            width: 22,
            height: 22,
            flexShrink: 0,
            cursor: "pointer",
            color: groupVisible ? "#cfd4db" : "#5a626d",
            transition: "color 120ms ease",
          }}
          title={groupVisible ? "Hide animation" : "Show animation"}
        >
          <input
            type="checkbox"
            checked={groupVisible}
            onChange={(e) => onVisibilityToggle(e.target.checked)}
            aria-label={`visibility for ${group.label} sequence`}
            data-testid="layer-group-visibility"
            style={{
              position: "absolute",
              inset: 0,
              margin: 0,
              opacity: 0,
              cursor: "pointer",
            }}
          />
          {groupVisible ? <IconEye size={15} /> : <IconEyeOff size={15} />}
        </label>
        {/* Sequence glyph — signals this row is a temporal stack. */}
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            color: kind.color,
            flexShrink: 0,
          }}
          title="Sequential layer group"
        >
          <IconWaves size={15} />
        </span>
        {/* Group NAME. */}
        <span
          style={{
            flex: 1,
            minWidth: 0,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            fontSize: 12.5,
            color: groupVisible ? "#e8e8ec" : "#8a929e",
          }}
          title={group.label}
        >
          {group.label}
        </span>
        {/* X/N frame counter (current/total, e.g. "7/24"). */}
        <span
          data-testid="layer-group-frame-label"
          style={{
            flexShrink: 0,
            fontSize: 11,
            color: "#9aa1ab",
            fontVariantNumeric: "tabular-nums",
            minWidth: 36,
            textAlign: "right",
          }}
        >
          {idx + 1}/{n}
        </span>
        {/* PLAY/pause button at the END (item 2 layout). */}
        <button
          type="button"
          data-testid="layer-group-play"
          data-legend-no-drag=""
          aria-label={playing ? "Pause sequence" : "Play sequence"}
          title={playing ? "Pause" : "Play"}
          onPointerDown={(e) => e.stopPropagation()}
          onClick={(e) => {
            e.stopPropagation();
            onPlayToggle();
          }}
          disabled={n <= 1}
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            width: 22,
            height: 22,
            flexShrink: 0,
            padding: 0,
            background: "rgba(255,255,255,0.06)",
            border: "1px solid rgba(255,255,255,0.08)",
            borderRadius: 6,
            color: n <= 1 ? "#5a626d" : "#cfd4db",
            cursor: n <= 1 ? "default" : "pointer",
          }}
        >
          {playing ? <IconPause size={12} /> : <IconPlay size={12} />}
        </button>
        {/* Hidden chip kept for test compatibility (data-testid
            layer-group-count-chip). Not rendered visibly; the count is in x/N. */}
        <span
          data-testid="layer-group-count-chip"
          title={`${n} frames`}
          style={{ display: "none" }}
        >
          {n}f
        </span>
      </div>
      {/* Expanded: each member frame as a compact sub-row. The radio-like dot
          shows + selects the active frame; the trash deletes that one frame. */}
      {expanded && (
        <div
          data-testid="layer-group-frames"
          style={{ display: "flex", flexDirection: "column", gap: 4, paddingLeft: 25 }}
        >
          {group.layers.map((layer, i) => (
            <div
              key={layer.layer_id}
              data-testid="layer-group-frame"
              data-layer-id={layer.layer_id}
              data-active={i === idx}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 7,
                fontSize: 11.5,
                color: i === idx ? "#e8e8ec" : "#8a929e",
              }}
            >
              <button
                type="button"
                data-testid="layer-group-frame-select"
                data-legend-no-drag=""
                aria-label={`show frame ${group.frameLabels[i]}`}
                aria-pressed={i === idx}
                onPointerDown={(e) => e.stopPropagation()}
                onClick={(e) => {
                  e.stopPropagation();
                  onStep(i);
                }}
                style={{
                  width: 14,
                  height: 14,
                  flexShrink: 0,
                  borderRadius: "50%",
                  padding: 0,
                  cursor: "pointer",
                  background: i === idx ? kind.color : "transparent",
                  border: `1px solid ${i === idx ? kind.color : "rgba(255,255,255,0.25)"}`,
                }}
              />
              <span
                style={{
                  flex: 1,
                  minWidth: 0,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                  fontVariantNumeric: "tabular-nums",
                }}
                title={layer.name}
              >
                {group.frameLabels[i]}
              </span>
              <button
                type="button"
                data-testid="layer-group-frame-delete"
                data-legend-no-drag=""
                aria-label={`delete frame ${group.frameLabels[i]}`}
                title="Delete this frame"
                onPointerDown={(e) => e.stopPropagation()}
                onClick={(e) => {
                  e.stopPropagation();
                  onRequestDelete(layer.layer_id);
                }}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  width: 20,
                  height: 20,
                  flexShrink: 0,
                  padding: 0,
                  background: "transparent",
                  border: "none",
                  color: "#7d8794",
                  cursor: "pointer",
                }}
              >
                <IconDelete size={12} />
              </button>
            </div>
          ))}
          {/* Per-group opacity — drives ALL frames together so the sequence
              reads at one transparency as you scrub. Applies to every member. */}
          <div
            data-testid="layer-group-opacity-row"
            style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 2 }}
          >
            <input
              type="range"
              min={0}
              max={1}
              step={0.01}
              value={groupOpacity}
              data-legend-no-drag=""
              onPointerDown={(e) => e.stopPropagation()}
              onChange={(e) => {
                const v = Number(e.target.value);
                group.layers.forEach((l) => onOpacityChange(l.layer_id, v));
              }}
              aria-label={`opacity for ${group.label} sequence`}
              data-testid="layer-group-opacity"
              style={{ flex: 1, height: 16, accentColor: kind.color, cursor: "pointer" }}
            />
          </div>
        </div>
      )}
    </div>
  );
}


// --- Test-injectable global bus ---------------------------------------- //
//
// The browser console uses this hook to inject a session-state envelope for
// local-dev verification:
//
//   window.__grace2InjectSessionState({ loaded_layers: [...] })
//
// This is debug-only; production builds remove it via the strip-comments
// path (vite minification preserves the function but the App.tsx attaches
// it only in dev — see App.tsx).

export interface LayerPanelBus {
  pushSessionState: (p: SessionStatePayload) => void;
  pushMapCommand: (p: MapCommandPayload) => void;
  pushCaseOpen: (p: CaseOpenEnvelopePayload) => void;
}

export function createLayerPanelBus(): LayerPanelBus & {
  subscribeSessionState: (cb: SessionStateSubscriber) => () => void;
  subscribeMapCommand: (cb: MapCommandSubscriber) => () => void;
  subscribeCaseOpen: (cb: (p: CaseOpenEnvelopePayload) => void) => () => void;
} {
  const sessionSubs = new Set<SessionStateSubscriber>();
  const mapSubs = new Set<MapCommandSubscriber>();
  const caseOpenSubs = new Set<(p: CaseOpenEnvelopePayload) => void>();
  return {
    pushSessionState: (p) => sessionSubs.forEach((s) => s(p)),
    pushMapCommand: (p) => mapSubs.forEach((s) => s(p)),
    pushCaseOpen: (p) => caseOpenSubs.forEach((s) => s(p)),
    subscribeSessionState: (cb) => {
      sessionSubs.add(cb);
      return () => sessionSubs.delete(cb);
    },
    subscribeMapCommand: (cb) => {
      mapSubs.add(cb);
      return () => mapSubs.delete(cb);
    },
    subscribeCaseOpen: (cb) => {
      caseOpenSubs.add(cb);
      return () => caseOpenSubs.delete(cb);
    },
  };
}
