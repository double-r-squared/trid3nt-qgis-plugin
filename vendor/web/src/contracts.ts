// GRACE-2 web — TS mirror of the Appendix-A WebSocket contracts.
// The pydantic-v2 schemas live in
// `packages/contracts/schemas/*.json` (job-0013, `grace2-contracts` v0.1.0).
//
// Decision (M1 stub / M3 web skeleton): hand-mirror, not codegen. Rationale:
//
//   The full set is 35 JSON schemas; we mirror only the payload shapes the
//   client actually consumes. M1 lands 6 envelopes; M3 (this job) adds the
//   session-state + map-command surface scoped to the FIVE M3-active sub-
//   discriminants per the job-0025 kickoff §6 (zoom-to / set-temporal-config
//   / start-animation / stop-animation / invalidate-tiles are explicitly
//   deferred to M4–M5). Aggregate target ~12–14 payload types after this
//   job; the codegen-promotion trigger remains ~20 (see OQ-W-1 from
//   job-0016). If we exceed 18 here, surface a refined OQ-W-1.
//
//   This file is the single point of contract truth on the web side. Any
//   divergence from `packages/contracts/schemas/ws_*.json` is a bug — every
//   field name and enum literal here matches the pydantic schema verbatim.
//
//   Pipeline-domain types (PipelineSnapshot beyond M1 step shape, etc.) are
//   reserved for job-0026; this file deliberately leaves them out.
//
//   job-0026 update: pipeline surface formalized below. The M1-era
//   `PipelineStep` (a M1-only stub from job-0016) is renamed to the canonical
//   Appendix D.6 name `PipelineStepSummary`. `PipelineSnapshot` (Appendix D.6)
//   is added to enable `session-state.current_pipeline` and `pipeline_history`
//   reconstruction. The pydantic D.6 `PipelineStepSummary` does NOT carry
//   `progress_percent`, `error_code`, or `error_message`; the FR-WC-8
//   acceptance criteria require them for the running-progress and failed-
//   step renders. The fields are added here as `?` optionals with a
//   consumer-pushback OQ filed against schema (`OQ-W-26-PIPELINE-STEP-FIELDS`,
//   see report.md). Until schema lands the Appendix D.6 amendment, the agent
//   service in M4 will need to either (a) carry the fields out-of-band on
//   `tool-call-failed` (already in A.4) and have us correlate by step_id, or
//   (b) extend D.6 — see the OQ for the recommendation.

// --- A.1 Envelope -------------------------------------------------------- //

export interface Envelope<P> {
  type: string;          // kebab-case discriminator
  id: string;            // ULID
  ts: string;            // ISO-8601 with literal Z suffix
  session_id: string;    // ULID
  payload: P;
}

// --- A.3 client -> agent payloads --------------------------------------- //

export type ResearchMode = "research" | "deep_research";

export interface UserMessagePayload {
  text: string;
  research_mode?: ResearchMode; // default "research" (FR-WC-15 toggle carrier; A1 amendment)
  /** In-chat model selector — Bedrock model id (NATE 2026-06-17). Null → server keeps its current selection. */
  model_id?: string | null;
  /**
   * LOCAL build only (live-feedback 2026-07-08): when true the server enables
   * + forwards the local model's reasoning-channel ("thinking") tokens for
   * this turn as `agent-thinking-chunk` envelopes. Omitted entirely on cloud
   * (older/cloud server builds never see the key).
   */
  show_thinking?: boolean;
  /**
   * Client's CURRENT active Case (useCases.activeCaseId), stamped on every
   * outbound user-message so the SERVER binds the turn to the case the client
   * is actually looking at — closing the two-sources-of-truth gap where the
   * server's in-memory `_SESSION_ACTIVE_CASE` could lag behind the client
   * (e.g. a case-select tapped mid-reconnect that never reached the server).
   * `null` / absent = root view (no active Case); the server keeps its own
   * notion. Mirrors `UserMessagePayload.case_id` in
   * packages/contracts/src/grace2_contracts/ws.py (same name, same shape).
   */
  case_id?: string | null;
}

export interface CancelPayload {
  reason?: string | null;
}

// `session-resume` historically carried `payload: {}` literally on the wire.
// It now carries an OPTIONAL `case_id` (the client's CURRENT active Case) so a
// reconnect-time replay re-asserts the client's case as the authority instead
// of the server snapping back to its stale in-memory active case
// (`_SESSION_ACTIVE_CASE`). `null` / absent = root view (no active Case), which
// preserves the historical empty-payload wire shape exactly. Mirrors
// `SessionResumePayload.case_id` in
// packages/contracts/src/grace2_contracts/ws.py (same name, same shape).
//
// Modeled as an interface (no longer `Record<string, never>`) so the single
// optional field is expressible while a bare `{}` still validates (every field
// optional). The agent's pydantic model is likewise empty-by-default.
export interface SessionResumePayload {
  case_id?: string | null;
}

// --- A.4 agent -> client payloads --------------------------------------- //

export interface AgentMessageChunkPayload {
  message_id: string;
  delta: string;
  done?: boolean; // terminal frame is `done: true`
}

/**
 * `agent-thinking-chunk` (LOCAL build, live-feedback 2026-07-08) — the local
 * model's reasoning-channel tokens, streamed live BEFORE the same bubble's
 * answer text. Wire shape is IDENTICAL to `agent-message-chunk`: the server
 * mints ONE message_id per narration segment and uses the SAME message_id for
 * that segment's thinking chunks and its later `agent-message-chunk` text; a
 * final chunk with `delta:"" done:true` closes the thinking stream for that
 * bubble (sent when the answer text starts or the segment ends).
 */
export type AgentThinkingChunkPayload = AgentMessageChunkPayload;

export type PipelineStepState =
  | "pending"
  | "running"
  | "complete"
  | "failed"
  | "cancelled";

// PipelineStepSummary — canonical name per Appendix D.6 (`collections.py`).
//
// The pydantic D.6 model carries:
//   step_id, name, tool_name, state, started_at?, completed_at?
//
// The `pipeline-state` envelope (A.4) `PipelineStep` model carries
// additionally `progress_percent?`. The FR-WC-8 acceptance also wants
// `error_code` + `error_message` on failed steps (currently only on the
// distinct `tool-call-failed` envelope, A.4). Per the kickoff "DO NOT parse
// out of strings, DO NOT invent fields client-side": these fields are
// modeled as `?` optionals here and the gap is filed as
// OQ-W-26-PIPELINE-STEP-FIELDS (schema consumer pushback) — proposed
// resolution: extend Appendix D.6 PipelineStepSummary with
// `progress_percent?: int (0..100) | None`, `error_code?: str | None`,
// `error_message?: str | None` so both the wire envelope and the persisted
// snapshot align. Until the amendment lands, the client renders whatever
// the agent populates; absent fields simply hide their UI affordance
// (no fabrication).
export interface PipelineStepSummary {
  step_id: string;
  name: string;
  tool_name: string;
  state: PipelineStepState;
  progress_percent?: number | null;
  started_at?: string | null;
  completed_at?: string | null;
  // Below: consumer-pushback fields (OQ-W-26-PIPELINE-STEP-FIELDS). Optional
  // here so the M3 client can render a failed step's reason if the agent
  // populates them either directly on the step or after the proposed D.6
  // amendment lands. Never fabricated client-side.
  error_code?: string | null;
  error_message?: string | null;
  // duration_ms (job-0264, ELEVATED tool-timer requirement): authoritative
  // wall-clock elapsed time the agent stamps on the TERMINAL transition
  // (complete / failed / cancelled), derived deterministically from
  // completed_at - started_at. `None` for pending/running — PipelineCard
  // shows a cosmetic live ticker until this lands, then locks to this value.
  // Never fabricated client-side. Mirrors PipelineStep.duration_ms (ws.py).
  duration_ms?: number | null;
  // Two-card sim observability (task-149). `role` discriminates the card KIND:
  // "tool" (default) is an on-box atomic-tool card; "compute" is the off-box
  // solver card bound to an AWS Batch job. A "compute" card additionally carries
  // `batch_job_id` (the Batch jobId it tracks) and `batch_status` (the last
  // DescribeJobs status verbatim — SUBMITTED/RUNNABLE/STARTING/RUNNING/
  // SUCCEEDED/FAILED, never an LLM estimate). All three are optional; the
  // defaults (role="tool", null ids) keep every existing tool card byte-identical
  // on the wire. Mirrors PipelineStep / PipelineStepSummary (ws.py + collections.py).
  role?: "tool" | "compute";
  batch_job_id?: string | null;
  batch_status?: string | null;
  // Nested sub-step visibility (task-168). A composer's INTERNAL atomic-tool
  // calls (fetch_*, run_solver, publish_layer, compute_*, ...) surface as
  // nested CHILD steps under the parent workflow card. All four fields are
  // OPTIONAL + default null on the wire so every pre-task-168 serialization
  // stays byte-compatible. Mirrors PipelineStep (ws.py) /
  // PipelineStepSummary (collections.py) + the persisted twins
  // (pipeline_step_summary.json + session_document.json) so reconnect /
  // cold-case replay nests too.
  //
  //   - parent_step_id: set on a CHILD step. When non-null this step is a
  //     CHILD of that parent step_id; the client NESTS it under the parent
  //     and does NOT render it as a top-level interleaved card.
  //   - substep_label: set on the PARENT - the RAW tool name of the currently
  //     -running child (the web humanizes it). The server CLEARS it (back to
  //     null) on the parent's own terminal transition, so the live breadcrumb
  //     disappears and the card collapses to the chevron + nested timeline.
  //   - substep_index: set on the PARENT - 1-based index of the currently
  //     -running child (>= 1).
  //   - substep_total: set on the PARENT - planned child count (>= 1), or null
  //     when the plan size is unknown (web shows just the label + index then).
  parent_step_id?: string | null;
  substep_label?: string | null;
  substep_index?: number | null;
  substep_total?: number | null;
}

// PipelineSnapshot — Appendix D.6 (`collections.py` PipelineSnapshot). Carried
// inline as `session-state.current_pipeline` and as entries in
// `session-state.pipeline_history`. The `pipeline-state` (A.4) envelope is the
// same shape minus `final_state`/`completed_at` (those are set only when the
// pipeline terminates and the snapshot moves to history).
export interface PipelineSnapshot {
  pipeline_id: string;
  started_at?: string | null;
  completed_at?: string | null;
  final_state?: "complete" | "failed" | "cancelled" | null;
  steps: PipelineStepSummary[];
  // Future: `run_id?: string | null;` per kickoff D.6 hint. The D.6 model
  // does not currently carry `run_id`; if the engine wants it, file under
  // the OQ above. Not added here speculatively.
}

// PipelineStatePayload — Appendix A.4 `pipeline-state` envelope. Replace-not-
// reconcile per Appendix A.7: each new envelope wholesale replaces the local
// view-model; never merge/diff. The payload IS the snapshot; the optional
// `steps` field in the M1 stub is replaced here by the canonical D.6 shape:
// `steps` is a list of `PipelineStepSummary`, defaulting to empty.
export interface PipelineStatePayload {
  pipeline_id: string;
  steps?: PipelineStepSummary[];
}

// ToolIoPayload — `tool-io` envelope (tool-card-expand-output spec). Mirrors
// `ToolIoPayload` in packages/contracts/src/grace2_contracts/ws.py. The
// `pipeline-state` PipelineStep carries only the humanized label + state +
// timing; this additive sidecar carries the RAW input args + the RAW
// function_response (the dict Gemini reads back) for one tool dispatch, keyed
// by the dispatch's `step_id`. The web merges it into the matching tool card's
// expander so a server-side / upstream-API failure the agent's narration hides
// becomes directly visible.
//
// `raw_args` / `function_response` are pre-serialized JSON STRINGS (the agent
// json-dumps + pretty-prints them; a non-serializable value degraded to its
// repr). Large payloads are TRUNCATED at the agent to a per-field byte cap
// (large-payload norm); `*_truncated` flags it and `*_bytes` carries the
// ORIGINAL byte length so the UI renders an honest "truncated, N bytes" note.
// `is_error` mirrors the honesty-floor signal (function_response had
// `status == "error"` or the dispatch raised) so the expander styles the
// response block red without re-parsing the JSON.
export interface ToolIoPayload {
  step_id: string;
  tool_name: string;
  raw_args: string;
  function_response: string;
  is_error: boolean;
  args_truncated: boolean;
  response_truncated: boolean;
  args_bytes: number;
  response_bytes: number;
}

export type ErrorCode =
  | "AUTH_FAILED"
  | "RATE_LIMITED"
  | "INTERNAL_ERROR"
  | "LLM_UNAVAILABLE"
  | "TOOL_NOT_FOUND"
  | "TOOL_PARAMS_INVALID"
  | "TOOL_TIMEOUT"
  | "DEM_SOURCE_UNAVAILABLE"
  | "SOLVER_FAILED"
  | "CONFIRMATION_TIMEOUT"
  | "SPATIAL_INPUT_TIMEOUT"
  | "DISAMBIGUATION_TIMEOUT"
  | "CLARIFICATION_TIMEOUT"
  | "USER_INPUT_CANCELLED"
  | "CANCELLED";

export interface ErrorPayload {
  error_code: ErrorCode;
  message: string;
  retryable?: boolean;
  retry_after_seconds?: number | null;
  // Card-render hardening (NATE 2026-06-22): the registry tool whose dispatch
  // raised this error, when the agent can attribute it. Optional + forward-
  // compatible - older agents omit it (null/absent). When present the web
  // force-flips THAT tool's card to failed (matched by tool_name) instead of
  // the "latest running step", so an error from tool A cannot turn tool B's
  // card red under concurrent solves.
  tool_name?: string | null;
}

// --- Appendix D.2: ProjectLayerSummary --------------------------------- //
//
// A row in `session-state.loaded_layers`. The agent serializes the worker /
// QGIS Server project's authoritative layer list into this shape and pushes
// it on connect / reconnect. The client reads it; it never invents one.
//
// `layer_type` is open-enum-ish (raster | vector | wms | wmts | geojson);
// surface as Open Question if a new value appears at runtime. The web side
// renders all known values; unknown values render the row but disable the
// type-specific affordances.

export type ProjectLayerType =
  | "raster"
  | "vector"
  | "wms"
  | "wmts"
  | "geojson";

// --- Data-driven render legend (the colormap KEY that comes from the data) -- //
//
// Mirrors `grace2_contracts.execution.LegendKey` / `LegendClass` field-for-field
// (both are in `execution.__all__`). THE PRINCIPLE (NATE): the gradient/key comes
// FROM THE DATA at fetch time so it MEANS something rather than being a retroactive
// hardcoded guess. The producer (publish_layer / pipeline_emitter / pelicun) emits a
// `LegendKey` from values it already computed (the p2/p98 percentile range, the GDAL
// color table, the per-feature `value_field`); the frontend renders ANY key
// generically, so a new tool that emits a `LegendKey` needs ZERO web changes.
//
// Additive + optional everywhere: `legend` absent => legacy `style_preset` rendering
// (the existing preset + URL-rescale + preset-fallback path stays as the fallback),
// so legacy layers render exactly as before.

/**
 * One swatch in a CATEGORICAL `LegendKey` (NLCD class, drought D0-D4, Pelicun
 * damage state, etc.). A class addresses the data it colors in ONE of two ways;
 * exactly one form is populated per class:
 *   - `value` -- a single discrete value the swatch matches (GDAL color-table
 *     entry, NLCD class code, the "D2" drought label). Number or string.
 *   - `value_min` / `value_max` -- a numeric bin the swatch covers (graduated
 *     buckets, e.g. damage-state mean 0.5..1.5).
 * `color` is an "#rrggbb" hex; `label` is the human-readable caption (verbatim).
 */
export interface LegendClass {
  value?: number | string | null;
  value_min?: number | null;
  value_max?: number | null;
  color: string; // "#rrggbb"
  label: string;
}

/**
 * The DATA-DRIVEN render key for a layer -- the colormap/legend the frontend
 * draws and the raster/vector colors are driven by. Mirrors the Python
 * `grace2_contracts.execution.LegendKey`.
 *
 * The colormap CHOICE stays the semantic per-variable decision (drought ramps
 * tan->dark-red, temperature rdylbu, etc.); the RANGE (`vmin`/`vmax`) is the REAL
 * data range by default (the p2/p98 percentile read the producer already computes)
 * UNLESS a variable pins a canonical fixed scale. The legend AND the render MUST
 * agree on the same range.
 */
export interface LegendKey {
  /** REQUIRED discriminator: "continuous" (rasters + graduated vectors) vs
   *  "categorical" (discrete classes: NLCD, drought, damage states). */
  kind: "continuous" | "categorical";
  /** continuous only. A NAMED ramp the web resolves to stops via
   *  titiler_colormap COLORMAP_STOPS (e.g. "reds"/"viridis"), OR explicit stops
   *  as `[[stop_0to1, "#rrggbb"], ...]`. Serializes as JSON arrays (pydantic
   *  round-trips inner pairs as tuples). null/absent for purely-categorical keys. */
  colormap?: string | Array<[number, string]> | null;
  /** continuous: REAL data low (the p2 the producer computes) or a pinned scale
   *  min. null/absent when unknown. */
  vmin?: number | null;
  /** continuous: REAL data high (p98) or a pinned scale max. null/absent when unknown. */
  vmax?: number | null;
  /** categorical: ordered swatches. null/absent for continuous keys. */
  classes?: LegendClass[] | null;
  /** VECTOR layers only: the GeoJSON feature property the color is driven by
   *  (e.g. "ds_mean"). null/absent for rasters (the band IS the value). */
  value_field?: string | null;
  /** Data units the legend annotates (e.g. "meters", "mg/L"). null/absent for
   *  unitless/categorical. */
  units?: string | null;
  /** Optional legend title (e.g. "Flood depth"). */
  label?: string | null;
}

export interface ProjectLayerSummary {
  layer_id: string;        // ULID assigned by the agent / worker
  name: string;            // human-readable, e.g. "Storm-surge max" or "Basemap"
  layer_type: ProjectLayerType;
  uri: string;             // gs://... GCS file pointer (COG / FlatGeobuf / GeoParquet)
  wms_url?: string | null; // QGIS Server WMS endpoint for MapLibre tile registration (job-0072, OQ-62-LAYERURI-URI-FIELD)
  attribution?: string | null;  // displayed in the LayerPanel row
  visible: boolean;        // initial visibility from the project state
  opacity: number;         // 0..1, clamped on render
  // BUG 2 (random-reorder): the agent emits z_index=null for layers it does not
  // explicitly stack, so the wire type is HONESTLY nullable. The prior
  // `z_index: number` lied: a raw `b.z_index - a.z_index` over a null became
  // `undefined - undefined = NaN`, which gives sort() NO total order, so the
  // three surfaces (LayerPanel / Map overlay stack / App layers) each rendered
  // the SAME set in a DIFFERENT order. Always sort via `compareLayersTopFirst`
  // below (never a bare subtraction) so a null/undefined z_index coerces to 0
  // and the layer_id tiebreak guarantees a deterministic TOTAL order everywhere.
  z_index?: number | null; // integer; lower draws first (bottom of stack); null = unstacked
  temporal?: TemporalConfig | null; // null for non-temporal layers
  // style_preset formally defined in D.2 via job-0072 (closes OQ-W-65-STYLE-PRESET).
  // Optional here because older documents may omit it; UI hides legend affordance gracefully.
  style_preset?: string | null;
  // DATA-DRIVEN render legend (mirrors execution.LayerURI.legend /
  // collections.ProjectLayerSummary.legend). The producer emits it from the values
  // it already computed (percentile range, GDAL color table, per-feature value_field).
  // When present the LayerLegend renders the colorbar DIRECTLY from it and Map.tsx
  // builds the vector fill expression generically. Absent => legacy style_preset
  // rendering (the URL-rescale + preset fallback path is unchanged).
  legend?: LegendKey | null;
}

/**
 * BUG 2 (random-reorder): the ONE shared, deterministic, TOTAL-ORDER comparator
 * for layer stacking. Top-of-stack FIRST (higher z_index first). A null/undefined
 * z_index coerces to 0 (NOT NaN, which would defeat the sort), and the layer_id
 * tiebreak makes the order a pure function of the SET - identical across all three
 * render surfaces (LayerPanel rows, the Map overlay stack, App's `layers`)
 * regardless of the input array order. Every layer sort in the app routes through
 * this so the panel order == the map order == the App order, ALWAYS.
 */
export function compareLayersTopFirst(
  a: Pick<ProjectLayerSummary, "z_index" | "layer_id">,
  b: Pick<ProjectLayerSummary, "z_index" | "layer_id">,
): number {
  return (
    (b.z_index ?? 0) - (a.z_index ?? 0) || a.layer_id.localeCompare(b.layer_id)
  );
}

// Appendix D.6 temporal block (subset web reads). Driven by WMS TIME param.
export interface TemporalConfig {
  start: string;           // ISO-8601 UTC with Z
  end: string;             // ISO-8601 UTC with Z
  step_seconds: number;    // animation cadence (FR-QS-4)
}

// --- Appendix D.6: MapView --------------------------------------------- //
//
// The persisted camera position on session-state. The web client applies it
// on session-resume (instant jump if it matches current; flyTo otherwise).

export interface MapView {
  center: [number, number]; // [lng, lat] in EPSG:4326
  zoom: number;
  bearing?: number;         // always 0 in v0.1 (Decision I: 2D camera lock)
  pitch?: number;           // always 0 in v0.1 (Decision I)
}

// --- A.4 session-state -------------------------------------------------- //
//
// Replaces the M1 stub's `unknown[]` placeholders with the real list-of-
// `ProjectLayerSummary` shape this job consumes. `chat_history` and
// `pipeline_history` stay as `unknown[]` here — chat is M1's domain and
// pipeline-history reconstruction is job-0026's domain (it will refine).
// `current_pipeline` likewise typed-loose until job-0026 lands.

export interface SessionStatePayload {
  chat_history?: unknown[];
  loaded_layers?: ProjectLayerSummary[];
  pipeline_history?: unknown[];
  current_pipeline?: unknown | null;
  map_view?: MapView | null;
  /**
   * job-0357 (per-Case layer DURABILITY) — CLIENT-ONLY hint, never on the wire.
   * The agent never sets this; it is stamped by App.tsx as it pushes a
   * `session-state` onto the LayerPanel bus, to tell Map.tsx whether this
   * snapshot is an AUTHORITATIVE layer REPLACE (Appendix A.7 replace-not-
   * reconcile — remove every tracked overlay absent from `loaded_layers`) or
   * a NON-AUTHORITATIVE top-up that may ADD/reconcile layers but must NOT
   * tear down durable overlays absent from it.
   *
   *   - ``true``  → full replace-not-reconcile. Set on an explicit Case
   *                 SWITCH / EXIT and on every server snapshot received while
   *                 the WebSocket is healthy (`connected`) — live layer adds
   *                 AND deletes apply normally.
   *   - ``false`` / absent → additive reconcile. Set for server snapshots
   *                 received while the socket is NOT `connected`
   *                 (disconnect / reconnecting window) so a transient EMPTY or
   *                 partial snapshot during a bare WS reconnect cannot wipe the
   *                 active Case's already-rendered layers. The agent's resume
   *                 replay carries the FULL persisted layer set and reconciles
   *                 idempotently regardless of which mode it lands in.
   *
   * Absent on snapshots produced by older code paths / unit fixtures, which
   * Map.tsx treats as ``true`` (the historical replace-not-reconcile default)
   * so nothing that relied on the prior behavior regresses.
   */
  replace_layers?: boolean;
}

// --- A.4 map-command --------------------------------------------------- //
//
// One envelope type (`map-command`) carries an internal `command`
// discriminator. The kickoff (job-0025 audit.md §6) explicitly scopes M3 to
// the FIVE active sub-discriminants — load-layer / remove-layer /
// set-layer-visibility / set-layer-opacity / set-layer-order — and
// explicitly states that the other five (zoom-to / set-temporal-config /
// start-animation / stop-animation / invalidate-tiles) are deferred to
// M4–M5 and NOT mirrored here. Round-1 revision: dropping the 5 deferred
// shapes (they were mirrored speculatively in the v1 ship and flagged as a
// scope-drift blocker by the reviewer).

export interface LoadLayerCommand {
  command: "load-layer";
  layer: ProjectLayerSummary;
  position?: "top" | "bottom" | number; // integer z-index slot
}

export interface RemoveLayerCommand {
  command: "remove-layer";
  layer_id: string;
}

export interface SetLayerVisibilityCommand {
  command: "set-layer-visibility";
  layer_id: string;
  visible: boolean;
}

export interface SetLayerOpacityCommand {
  command: "set-layer-opacity";
  layer_id: string;
  opacity: number; // 0..1
}

export interface SetLayerOrderCommand {
  command: "set-layer-order";
  layer_ids: string[]; // full ordered list, top-of-stack first
}

export type MapCommandPayload =
  | LoadLayerCommand
  | RemoveLayerCommand
  | SetLayerVisibilityCommand
  | SetLayerOpacityCommand
  | SetLayerOrderCommand;

// --- Per-Case secrets (job-0125, sprint-12-mega Wave 2) ----------------- //
//
// Mirrors packages/contracts/src/grace2_contracts/secrets.py (the canonical
// pydantic shapes). Closed Literal vocabulary; broadening the set requires
// the corresponding SRS §F.3 amendment + secrets.py update.
//
// Wire shapes:
//   - secrets-list (server -> client): list of SecretRecord
//   - secret-add (client -> server): { provider, case_id, label, key_value }
//   - secret-revoke (client -> server): { secret_id }
//
// Decision F: `key_value` is transient on the wire; the server writes it to
// the vault on receipt and never echoes it back. The web client clears the
// key from local React state immediately after submit.
// Invariant 9 (no cost theater): no cost / quota / usage-count field.

export type ProviderID =
  | "ebird"
  | "iucn_red_list"
  | "movebank"
  // Hazard / earth-observation keyed fetchers (credential-pipeline-generic).
  | "firms"        // NASA FIRMS active-fire (FIRMS_MAP_KEY)
  | "ecmwf_cds"    // Copernicus CDS — ERA5 reanalysis + GTSM share one CDS key
  | "gtsm"         // GTSM tide/surge — alias scope (resolves alongside ecmwf_cds)
  | "nws"
  | "openweathermap"
  | "openai"
  | "anthropic"
  | "google_genai"
  | "mapbox"
  | "maptiler"
  // Generic name-only fallback — the credential card for ANY keyed endpoint with
  // no dedicated provider above. Server derives the credential name from the
  // failing tool and emits a NAME-ONLY card (signup_url null — never a fabricated
  // URL). Surfaces the secret-entry form for any endpoint instead of a dead-end.
  | "generic";

export interface SecretRecord {
  schema_version?: "v1";
  secret_id: string;        // ULID
  provider: ProviderID;
  case_id?: string | null;  // null = user-level (M6+ identity required)
  vault_ref: string;        // opaque vault URI (never the key value)
  label?: string | null;
  added_at: string;         // ISO-8601 Z
  last_used_at?: string | null;
  is_active: boolean;
}

export interface SecretsListPayload {
  envelope_type?: "secrets-list";
  secrets: SecretRecord[];
}

export interface SecretAddPayload {
  envelope_type?: "secret-add";
  provider: ProviderID;
  case_id?: string | null;
  label?: string | null;
  key_value: string;        // transient — cleared after submit
}

export interface SecretRevokePayload {
  envelope_type?: "secret-revoke";
  secret_id: string;
}

// --- Credential-request flow (just-in-time secrets prompt; §F.3 amendment) - //
//
// Mirrors packages/contracts/src/grace2_contracts/secrets.py
// (CredentialRequestEnvelopePayload / CredentialProvidedEnvelopePayload).
//
// Flow:
//   1. A tool dispatch hits a missing/invalid credential for a keyed provider.
//      The agent pauses the tool and emits `credential-request` (server ->
//      client) naming the provider + the secret key it needs + (when one
//      exists) a signup URL. NATE no-URL fallback (2026-06-18): `signup_url`
//      is OPTIONAL — when the provider has no reliable self-serve URL (or the
//      failing tool isn't even in the credential registry) the envelope omits
//      it (null/absent) and the client renders a NAME-ONLY card (provider
//      label / credential name + the secret-entry form), NEVER a fabricated
//      link. The registry is the ONLY source of a signup URL; the client must
//      not invent one. The card stays fully usable name-only (Enter -> submit).
//   2. The client surfaces a credential-entry affordance and SAVES the key via
//      the EXISTING `secret-add` path (SecretAddPayload) — that is the only
//      envelope that ever carries the raw key value (Decision F).
//   3. After the save succeeds, the client emits `credential-provided` (client
//      -> server) echoing the request_id so the agent retries the paused tool.
//      A declined prompt rides back as `credential-provided` with
//      `provided: false` (agent narrates honestly, abandons the paused tool —
//      no silent dead-end, no hallucinated success).
//
// Neither envelope carries key material; only `secret-add` does.

export interface CredentialRequestPayload {
  envelope_type?: "credential-request";
  request_id: string;        // ULID; echoed back on credential-provided
  provider_id: ProviderID;   // closed Literal; scopes the secret-add reply
  provider_label: string;    // human-readable, e.g. "eBird"
  // Where to obtain a key. OPTIONAL: null / undefined / "" (empty) all mean
  // "no reliable self-serve URL" — the card renders NAME-ONLY (no link) and
  // stays fully usable. The registry is the ONLY source of this URL; the
  // client never fabricates one (NATE no-hallucinate-URL rule 2026-06-18).
  signup_url?: string | null;
  secret_key_name: string;   // canonical key name, e.g. "EBIRD_API_KEY"
  message: string;           // agent's user-facing explanation
  tool_name: string;         // the registry tool that paused
}

export interface CredentialProvidedPayload {
  envelope_type?: "credential-provided";
  request_id: string;        // the CredentialRequestPayload this answers
  secret_id?: string | null; // ULID minted by the preceding secret-add; null when provided=false
  provided?: boolean;        // true = key saved, retry; false = user declined (default true)
}

// --- Region-disambiguation picker (state-bbox-fallback narrowing) -------- //
//
// Mirrors packages/contracts/src/grace2_contracts/region_choice.py
// (RegionChoiceRequestEnvelopePayload / RegionChoiceProvidedEnvelopePayload /
// RegionCandidate). Field names + types match the pydantic schema VERBATIM.
//
// Flow (analogous to the credential-request pause/resume seam):
//   1. A `geocode_location` result comes back as a state-snap
//      (source == "state-bbox-fallback"): a vague/regional query ("south
//      Florida") that had no precise OSM match snapped to the WHOLE state bbox.
//      That whole-state bbox is the honest DEFAULT the headless path uses.
//   2. The agent emits `region-choice-request` (server -> client): the
//      whole-state bbox + the candidate sub-regions (default: counties) + an
//      honest prompt naming that it snapped to the whole state and is OFFERING
//      a narrower pick. The agent PAUSES the turn awaiting the reply (fail-open:
//      a timeout / no client keeps the whole-state default).
//   3. The client renders the candidates BOTH as an in-chat card LIST and as a
//      tappable county CHOROPLETH on the map (both synced to the same
//      request_id). Either affordance answers via `region-choice-provided`.
//   4. The client emits `region-choice-provided` (client -> server) echoing the
//      request_id: choice="region" + selected_region_id + selected_bbox for a
//      narrowed pick, or choice="whole_state" to keep the default. The agent
//      re-resolves authoritatively by selected_region_id against its candidate
//      set (a tampered selected_bbox cannot redirect the workflow), falling
//      back to selected_bbox only when the id is unknown.
//
// Invariant 9 (no cost theater): no cost / quota field. Invariant 8
// (cancellation is first-class): no per-envelope timeout/cancel field — a
// whole_state reply IS the decline path; a hard cancel rides the A.3 `cancel`.

/** Closed Literal of admin granularities a candidate can be drawn at. `county`
 * is the v0.1 shipping default; coarser/finer levels are an explicit amendment
 * (each level has its own agent-side fetch plumbing). */
export type RegionAdminLevel = "county";

/** EPSG:4326 bbox tuple [minLon, minLat, maxLon, maxLat] (mirrors pydantic
 * BBox). Reused alias so the region-choice types read against one bbox shape. */
export type RegionBBox = [number, number, number, number];

/** One selectable sub-region of the snapped state (mirrors RegionCandidate). */
export interface RegionCandidate {
  region_id: string;          // stable id (TIGER GEOID-derived); echoed verbatim on reply
  name: string;               // human label, e.g. "Lee County"
  bbox: RegionBBox;           // EPSG:4326 total bounds of the region polygon
  admin_level: RegionAdminLevel; // "county" (default)
}

/** `region-choice-request` (A.4) — server -> client narrow-the-region prompt. */
export interface RegionChoiceRequestPayload {
  envelope_type?: "region-choice-request";
  request_id: string;         // ULID; echoed back on region-choice-provided
  state_name: string;         // detected state's full name, e.g. "Florida"
  state_code: string;         // 2-letter state code, e.g. "FL"
  state_bbox: RegionBBox;     // whole-state EPSG:4326 bbox (the use_whole_state extent)
  candidates: RegionCandidate[]; // candidate sub-regions (may be empty → whole-state only)
  default_action: "use_whole_state"; // closed Literal — the fail-open default
  message: string;            // honest prompt: snapped to whole state, offering a narrower pick
}

/** `region-choice-provided` (A.3) — client -> server the user's region pick. */
export interface RegionChoiceProvidedPayload {
  envelope_type?: "region-choice-provided";
  request_id: string;         // echoes the RegionChoiceRequestPayload this answers
  choice: "region" | "whole_state"; // narrowed to a sub-region vs kept the whole-state default
  selected_region_id?: string | null; // region_id of the chosen candidate when choice=="region"
  selected_bbox?: RegionBBox | null;   // chosen region's bbox (echoed) when choice=="region"
}

// --- Spatial input (pick-mode + FR-WC-16 urban vector-draw) ---------------- //
//
// Mirrors packages/contracts/src/grace2_contracts/ws.py
// (SpatialInputRequestPayload / SpatialInputResponsePayload / SuggestedView /
// ReferenceLayer). Field names + types match the pydantic schema VERBATIM so the
// two contracts stay byte-for-byte equivalent in shape.
//
// Flow (same future-based pause/resume seam as region-choice / credential):
//   1. The agent emits `spatial-input-request` (server -> client) and PAUSES the
//      turn awaiting the reply. `mode` selects the client affordance:
//        - "point"       -> single click; reply carries coordinates=[lon, lat].
//        - "bbox"        -> drag rectangle; reply carries
//                           coordinates=[minLon, minLat, maxLon, maxLat].
//        - "vector_draw" -> FR-WC-16 urban vector-draw: the client opens a
//                           terra-draw surface (rectangle / polygon / polyline +
//                           select-edit); reply carries `features` (a GeoJSON
//                           FeatureCollection of the drawn geometry).
//   2. The client emits `spatial-input-response` (client -> server) echoing
//      request_id. For vector_draw, each Feature.properties carries a `role`
//      ("aoi" | "barrier" | "point"); a "barrier" LineString also carries
//      `barrier_type` ("wall" | "flap_gate"), and a "flap_gate" may carry an
//      optional `flap_direction` ("in" | "out" | numeric bearing) +
//      `protected_side` ("left" | "right"). The role=="barrier" subset is
//      field-for-field the tagged-LineString FeatureCollection the urban (SWMM)
//      engine's `barriers` kwarg accepts.
//
// Large-payload note: a drawn FeatureCollection is small by construction (a few
// short LineString/Polygon rings — kilobytes), so NO payload-warning gate (the
// 25 MB warn / 250 MB hard-block discipline governs TOOL-OUTPUT payloads, not
// this small user-drawn input). No cap is imposed here beyond shape validation.

/** A minimal GeoJSON position: [lon, lat] (EPSG:4326, lon-first). */
export type GeoJSONPosition = [number, number] | number[];

/** A minimal GeoJSON geometry (Point / LineString / Polygon — the shapes the
 * vector-draw surface produces). Loosely typed (coordinates nest by geometry
 * type) to mirror the pydantic `dict[str, Any]` structural-only validation. */
export interface GeoJSONGeometry {
  type: "Point" | "LineString" | "Polygon" | string;
  coordinates: unknown;
}

/** The `role` a drawn feature plays (mirrors the ws.py validator vocabulary).
 * "line" is a NEUTRAL elevation/section LineString (compute_terrain_profile /
 * compute_cross_section) -- a drawn line with no barrier semantics, never tagged
 * wall/flap_gate. */
export type SpatialDrawRole = "aoi" | "barrier" | "point" | "line";

/** Per-segment barrier tag on a role=="barrier" LineString (mirrors
 * swmm_contracts.BarrierType). */
export type BarrierType = "wall" | "flap_gate";

/** Optional one-way orientation of a flap gate: a closed enum OR a numeric
 * bearing in degrees. */
export type FlapDirection = "in" | "out" | number;

/** Properties carried on a drawn Feature. `role` is required; the barrier
 * fields are present only on role=="barrier" features. */
export interface SpatialDrawFeatureProperties {
  role: SpatialDrawRole;
  barrier_type?: BarrierType;         // role=="barrier": "wall" | "flap_gate"
  flap_direction?: FlapDirection;     // role=="barrier" && flap_gate: optional
  protected_side?: "left" | "right";  // role=="barrier": optional dry-side hint
  [key: string]: unknown;             // forward-compatible extra props
}

/** One drawn GeoJSON Feature with role-tagged properties. */
export interface SpatialDrawFeature {
  type: "Feature";
  geometry: GeoJSONGeometry;
  properties: SpatialDrawFeatureProperties;
}

/** The drawn FeatureCollection round-tripped on a vector_draw response. */
export interface SpatialDrawFeatureCollection {
  type: "FeatureCollection";
  features: SpatialDrawFeature[];
}

/** An optional helper layer shown only during a spatial-input request
 * (mirrors ws.ReferenceLayer). */
export interface ReferenceLayer {
  layer_id: string;
  wms_url: string;
  style_preset: string;
}

/** Where the client zooms to make picking easier (mirrors ws.SuggestedView). */
export interface SuggestedView {
  bbox: RegionBBox;  // EPSG:4326 [minLon, minLat, maxLon, maxLat]
  zoom: number;
}

/** `spatial-input-request` (A.4) — server -> client asks the user for geometry. */
export interface SpatialInputRequestPayload {
  envelope_type?: "spatial-input-request";
  request_id: string;                          // ULID; echoed back on the response
  mode: "point" | "bbox" | "vector_draw";      // pick affordance / draw surface
  title: string;
  description: string;
  // vector_draw only. "barrier" (default, SWMM walls/flap-gates -- drawn lines
  // MUST be tagged), "line" (a NEUTRAL elevation/section line for
  // compute_terrain_profile -- drawn line submitted plain, no tagging required),
  // or "aoi" (area-of-interest selection -- only rect/polygon tools shown, no
  // line/tagging, submit gates on >= 1 polygon drawn).
  // Optional/absent => "barrier" (the existing flow is byte-for-byte unchanged).
  purpose?: "barrier" | "line" | "aoi";
  suggested_view?: SuggestedView | null;       // camera hint for the pick
  reference_layers?: ReferenceLayer[];          // optional helper layers
  default_timeout_seconds?: number;             // fail-open timeout (default 300)
}

/** `spatial-input-response` (A.4b) — client -> server the user's geometry, or
 * a cancellation. point/bbox set `coordinates`; vector_draw sets `features`. */
export interface SpatialInputResponsePayload {
  envelope_type?: "spatial-input-response";
  request_id: string;                                   // echoes the request
  geometry_type?: "point" | "bbox" | "vector_draw" | null;
  coordinates?: number[] | null;                        // point=[lon,lat]; bbox=[minLon,minLat,maxLon,maxLat]
  features?: SpatialDrawFeatureCollection | null;       // vector_draw: drawn geometry
  cancelled?: boolean;                                  // true = user dismissed
}

// --- Case persistence envelopes (job-0137, sprint-12-mega Wave 3 — FR-MP-6) //
//
// Mirrors packages/contracts/src/grace2_contracts/case.py (the canonical
// pydantic shapes). The Case is the user-facing left-rail entity; the storage
// model name "project" stays canonical, but the wire envelopes use "case".
//
// Wire shapes:
//   - case-list (server -> client): list of CaseSummary; emitted on connect
//     and after every successful case-command.
//   - case-open (server -> client): CaseSessionState | null; emitted on
//     case-command(create|select) when the rehydration succeeds; null when
//     the server cannot rehydrate (archived/deleted between list+select).
//   - case-command (client -> server): one of create / select / rename /
//     archive / delete; carries optional case_id (REQUIRED for every command
//     except create) and an args dict (e.g. { title: "..." } for rename and
//     create-hint).
//
// Invariant 9 (no cost theater): no cost / quota / quote field anywhere.
// Invariant 8 (cancellation is first-class): no cancel field on case-command;
// cancellation flows through the standard `cancel` envelope (A.3).

export type CaseStatus = "active" | "archived" | "deleted";

export interface CaseSummary {
  schema_version?: "v1";
  case_id: string;          // ULID; maps 1:1 to projects._id (FR-MP-6)
  title: string;
  created_at: string;       // ISO-8601 UTC Z
  updated_at: string;       // ISO-8601 UTC Z
  status: CaseStatus;
  bbox?: [number, number, number, number] | null; // [minLon, minLat, maxLon, maxLat]
  primary_hazard?: string | null;
  layer_summary?: string[]; // flat list of layer_ids
  // job-0172 Part B: per-Case ``ProjectLayerSummary`` snapshots persisted
  // server-side so a Case re-open rehydrates ``loaded_layers``. The web
  // client reads ``CaseSessionState.loaded_layers`` on case-open rather
  // than this field; it's exposed on the summary for forward compatibility.
  loaded_layer_summaries?: ProjectLayerSummary[];
  qgs_project_uri?: string | null;
}

// PersistedSubStepRecord - task-168 read-only persistence: the replayable
// terminal record of ONE nested CHILD step (a composer's internal atomic-tool
// call) under a top-level ToolCardRecord. Mirrors the child fields the agent
// persists alongside the parent so Case reopen (warm via the agent) AND the
// box-off cold view rebuild the SAME nested sub-step timeline the live feature
// renders - with NO re-execution. Additive: a parent row with no `children`
// (every pre-task-168 document) replays exactly as before.
//
// Field names reuse PipelineStepSummary (collections.py) verbatim so the replay
// path can synthesize a PipelineStepSummary directly off this record:
//   - step_id / parent_step_id: the persisted ids. The replay path RE-PARENTS
//     children to the synthesized replay parent step_id (the persisted ids are
//     wire-only and absent from the replayed snapshot), so these are carried for
//     fidelity/keying but parenting is rebuilt deterministically on replay.
//   - name / tool_name: the child's raw tool name (the web humanizes it).
//   - state: the CLOSED terminal two-value enum (cancelled children persist
//     nothing, matching the parent contract; pending/running are wire-only).
//   - duration_ms: authoritative wall-clock elapsed time (null on old docs).
//   - error_code / error_message: present on a failed child (honesty floor).
//   - raw_args / function_response / is_error / *_truncated / *_bytes: the
//     SAME tool-io fields as ToolCardRecord (reuse, do NOT invent names) so a
//     child's IO drop-down rehydrates on replay too. All optional; absent IO ->
//     the child's chevron stays absent (no fabrication).
export interface PersistedSubStepRecord {
  step_id: string;
  parent_step_id?: string | null;
  name?: string | null;
  tool_name: string;
  state: "complete" | "failed";
  duration_ms?: number | null;
  error_code?: string | null;
  error_message?: string | null;
  raw_args?: string | null;
  function_response?: string | null;
  is_error?: boolean | null;
  args_truncated?: boolean | null;
  response_truncated?: boolean | null;
  args_bytes?: number | null;
  response_bytes?: number | null;
}

// ToolCardRecord — job-0267 (full-stream persistence): the replayable
// terminal record of ONE tool dispatch inside a Case turn. Mirrors
// `grace2_contracts.case.ToolCardRecord`. The live tool cards render from
// `pipeline-state` envelopes (wire-only); this record is their persisted
// twin so a Case reopen re-renders the inline cards. `state` walks the durable
// card lifecycle: an off-box SOLVE card is persisted `running` at mint and
// UPDATED IN PLACE to its terminal state (so a mid-run reconnect/reopen replays
// it — "nothing about the chat is transient"); `cancelled` is persisted for a
// stopped solve so it stays traceable. Atomic-tool cards still persist only at
// terminal (`complete`/`failed`).
//
// C1 tool-card IO persistence (A1 produces, W2 consumes): the live IO
// drop-down (input args + function_response chevron) is driven by the
// `tool-io` envelope (ToolIoPayload), which is wire-only and was LOST on Case
// reopen — the chevron went blank. A1 now persists the SAME IO fields on this
// record under the SAME field names the live `ToolIoPayload` uses (reuse, do
// NOT invent new names): `raw_args`, `function_response`, `is_error`,
// `args_truncated`, `response_truncated`, `args_bytes`, `response_bytes`. The
// replay path (replayStreamFromChatHistory) reads them off this record and
// rebuilds a ToolIoPayload keyed by the synthesized replay step_id so the
// drop-down rehydrates exactly like the live render. All optional so pre-C1
// documents (no IO fields) validate + replay unchanged (the chevron simply
// stays absent for an old card with no persisted IO).
export interface ToolCardRecord {
  schema_version?: "v1";
  tool_name: string;
  state: "running" | "complete" | "failed" | "cancelled";
  started_at?: string | null;
  duration_ms?: number | null;
  label?: string | null;
  // C1 — persisted tool-io fields (same names as ToolIoPayload). Present only
  // when A1 captured the dispatch's IO; absent on pre-C1 / IO-less cards.
  raw_args?: string | null;
  function_response?: string | null;
  is_error?: boolean | null;
  args_truncated?: boolean | null;
  response_truncated?: boolean | null;
  args_bytes?: number | null;
  response_bytes?: number | null;
  // task-168 nested sub-step persistence - the ordered CHILD steps (a
  // composer's internal atomic-tool calls) under this top-level card. Additive
  // + optional: absent / empty on every pre-task-168 row (which then replays as
  // a plain top-level card, no nested timeline). The replay path rebuilds the
  // nested timeline from these so warm reopen + box-off cold view nest exactly
  // like the live render, READ-ONLY (no re-dispatch). Ordered chronologically.
  children?: PersistedSubStepRecord[] | null;
}

// CaseChatMessage — one persisted chat exchange in a Case session. The
// rehydration replay reconstructs the chat panel from a list of these.
// `map_command_emissions` is kept as `unknown[]` here because the agent
// validates each entry against the MapCommandPayload union before write; the
// web side replays them through the existing map-command dispatch path.
// job-0267: `role` gains "tool" — one row per dispatched registry tool,
// interleaved with user/agent turns by `created_at`; the typed payload is
// `tool_card` (content carries the same record as a JSON string).
export interface CaseChatMessage {
  schema_version?: "v1";
  message_id: string;
  case_id: string;
  role: "user" | "agent" | "system" | "tool";
  content: string;
  pipeline_id?: string | null;
  tool_card?: ToolCardRecord | null; // set IFF role === "tool" (job-0267)
  layer_emissions?: string[];
  map_command_emissions?: MapCommandPayload[]; // typed-loose union; agent validates
  created_at: string;
}

// CaseSessionState — the rehydration envelope returned when a user opens
// a Case. Mirrors the server-side CaseSessionState from case.py: the client
// rebuilds chat from chat_history, the LayerPanel from loaded_layers, and
// the map jumps to the Case bbox.
export interface CaseSessionState {
  schema_version?: "v1";
  case: CaseSummary;
  chat_history?: CaseChatMessage[];
  loaded_layers?: ProjectLayerSummary[];
  pipeline_history?: PipelineSnapshot[];
  current_pipeline?: PipelineSnapshot | null;
}

export interface CaseListEnvelopePayload {
  envelope_type?: "case-list";
  cases: CaseSummary[];
}

export interface CaseOpenEnvelopePayload {
  envelope_type?: "case-open";
  session_state: CaseSessionState | null;
}

export type CaseCommand =
  | "create"
  | "select"
  | "deselect" // job-0269: client navigated out of the Case to the Cases root
  | "rename"
  | "archive"
  | "delete";

export interface CaseCommandEnvelopePayload {
  envelope_type?: "case-command";
  command: CaseCommand;
  case_id?: string | null;
  args?: Record<string, unknown>;
}

// --- Tool payload-warning envelopes (job-0127, sprint-12-mega Wave 2) ---- //
//
// Mirrors packages/contracts/src/grace2_contracts/payload_warning.py.
// Agent emits `tool-payload-warning` before dispatching a tool whose
// estimated response payload exceeds the warning threshold (default 25 MB).
// Client renders an inline chat card with [Proceed] [Cancel] [Narrow scope]
// buttons. The user's selection rides back on `tool-payload-confirmation`.
//
// Invariant 9 (no cost theater): `estimated_mb` is a payload-size estimate,
// not a dollar / latency / quota figure. No cost field anywhere.

export type PayloadWarningOption = "proceed" | "cancel" | "narrow_scope";

// Pre-run mesh-granularity suggestion (#154 granularity gate, sprint-16).
// Mirrors GranularitySuggestion in
// packages/contracts/src/grace2_contracts/payload_warning.py. OPTIONAL
// enrichment on a `tool-payload-warning`: when present, the client renders the
// resolution ladder + estimated cells / solve time / compute class and lets the
// user override the rung before the heavy solver run. The override rides back on
// the existing `tool-payload-confirmation` (decision="narrow_scope" +
// revised_args carrying the chosen resolution_param value).
//
// Invariant 9 (no cost theater): cells / seconds / vCPUs / instance label are
// capacity + capability descriptors, NOT dollar figures. No dollar field.
export interface GranularitySuggestion {
  // NATE 2026-06-26: widened to mirror the Python contract so the #154 gate can
  // also describe a FETCHER resolution choice (dem / topobathy fetch, key
  // "resolution_m"), not just the SWMM/SFINCS solver mesh. compute_class stays
  // a free string -> a fetch gate sets compute_class="fetch" (no union change).
  engine: "swmm" | "sfincs" | "dem" | "topobathy";
  resolution_param: "target_resolution_m" | "grid_resolution_m" | "resolution_m";
  suggested_resolution_m: number;
  resolution_choices: number[];
  estimated_active_cells: number;
  estimated_solve_seconds: number;
  vcpus: number;
  compute_class: string;
  cell_cap: number;
  coarsened: boolean;
  reason: string;
  spot_label?: string | null;
}

// Pre-run TIME-SCALE (animation cadence + window) suggestion (combined
// run-settings gate, sprint-16). Mirrors TimeScaleSuggestion in
// packages/contracts/src/grace2_contracts/payload_warning.py. The sibling of
// GranularitySuggestion: where granularity makes the SPATIAL resolution a user
// lever, this makes the TEMPORAL resolution one (minutes-per-frame + the
// simulation window). When present ALONGSIDE a granularity block on a
// `tool-payload-warning`, the client renders ONE combined "Run settings" card
// to review + override BOTH; the chosen cadence/window rides back on the
// existing `tool-payload-confirmation` (decision="narrow_scope" + revised_args
// carrying `output_interval_min` and/or `duration_hr`). None on the pluvial
// path (hourly cadence is fixed) -> the card degrades to the resolution gate.
//
// Invariant 9 (no cost theater): frames / minutes / hours are capacity +
// capability descriptors, NOT dollar figures. No dollar field.
export interface TimeScaleSuggestion {
  cadence_param: "output_interval_min";
  suggested_interval_min: number;
  interval_choices: number[];
  duration_param: "duration_hr";
  suggested_duration_hr: number;
  estimated_frame_count: number;
  max_frames: number;
  min_interval_min: number;
  is_coastal: boolean;
  reason: string;
}

export interface PayloadWarningEnvelopePayload {
  envelope_type?: "tool-payload-warning";
  warning_id: string;
  tool_name: string;
  tool_args: Record<string, unknown>;
  estimated_mb: number;
  threshold_mb: number;
  recommendation: string;
  alternative_args?: Record<string, unknown> | null;
  options: PayloadWarningOption[];
  ttl_seconds?: number;
  granularity?: GranularitySuggestion | null;
  time_scale?: TimeScaleSuggestion | null;
}

export type PayloadConfirmationDecision = "proceed" | "cancel" | "narrow_scope";

export interface PayloadConfirmationEnvelopePayload {
  envelope_type?: "tool-payload-confirmation";
  warning_id: string;
  decision: PayloadConfirmationDecision;
  revised_args?: Record<string, unknown> | null;
}

// --- Layer-delete envelope (job-0325 F53) ------------------------------- //
//
// Client -> server: the user clicked the per-row delete control in the
// LayerPanel. The server drops the layer from the session's loaded_layers,
// persists the post-deletion list AUTHORITATIVELY (replace semantics, NOT the
// union merge used for loaded-layer adds — a union would resurrect the deleted
// layer on the next turn / Case reopen), and emits a fresh `session-state`
// without the layer. Map.tsx then removes the overlay via replace-not-reconcile
// (Appendix A.7), and the agent's loaded-layers awareness (build_layers_present_note)
// stops listing it because it is gone from both the in-memory emitter and the
// persisted summaries.
//
// This is a NEW direction from the inbound server->client `map-command`
// `remove-layer` discriminant (RemoveLayerCommand above): `map-command` is
// outbound-only today, so reusing that discriminant would overload the
// direction semantics. A dedicated `layer-delete` envelope keeps client->server
// intent distinct from the server->client map mutations.
export interface LayerDeletePayload {
  envelope_type?: "layer-delete";
  layer_id: string;
}

// --- Live big-sim solve-progress envelope (NATE 2026-06-17) -------------- //
//
// Server -> client enrichment for a RUNNING heavy-compute solver (SFINCS /
// MODFLOW / Pelicun on the external per-job execution substrate — AWS Batch
// big sims). It is emitted alongside the `pipeline-state` snapshots while the
// solver burns wall-clock so the running tool / pipeline card can surface a
// live readout ("Modeling flood · SFINCS · 100 m · ~46k cells · 8 vCPU ·
// 1:12 · est ~70s") instead of an opaque spinner.
//
// The aggregation + emit side (telemetry summary's `solve_telemetry`, this
// live envelope) is owned by the CONCURRENT AGENT TRACK; this is the type seam
// the web side compiles against. It carries a `run_id` (the external job's id)
// and the live progress numbers. `eta_seconds` is null when the backend can't
// estimate one yet (cold start / unknown total). Web matches it to the
// currently-running solver step and renders the readout in place, updating as
// envelopes arrive and clearing when the step reaches a terminal state.
//
// Invariant 9 (no cost theater): the readout is a physical-progress + resource
// surface (resolution / cell count / vCPU / wall-clock), never a dollar figure.
export interface SolveProgressPayload {
  envelope_type?: "solve-progress";
  /** External execution job id (e.g. the AWS Batch run). */
  run_id: string;
  /** Solver family — e.g. "SFINCS", "MODFLOW", "Pelicun". Display + match key. */
  solver: string;
  /** Grid resolution in metres (e.g. 100). null when not yet known (pre-build). */
  grid_resolution_m: number | null;
  /** Active (computed) cell count — wet/active grid cells. null when not yet estimated. */
  active_cell_count: number | null;
  /** vCPUs allocated to the run. null when not yet known. */
  vcpus: number | null;
  /** Wall-clock seconds elapsed so far. */
  elapsed_seconds: number;
  /** Estimated seconds remaining; null when the backend cannot estimate yet. */
  eta_seconds?: number | null;
  /**
   * Two-card sim observability (task-149). The DescribeJobs status this live
   * progress tick reflects (same vocabulary as PipelineStepSummary.batch_status:
   * SUBMITTED/RUNNABLE/STARTING/RUNNING/SUCCEEDED/FAILED), or null/absent for
   * ticks not bound to a Batch job. Optional + back-compatible. Mirrors
   * SolveProgressPayload.phase (ws.py).
   */
  phase?: string | null;
}

// --- Turn-complete / idle signal (C2 terminal-state durability) ---------- //
//
// C2 (A1 produces, W2 consumes): a tool card could hang in `running` forever
// if its terminal `pipeline-state` frame was lost on a socket drop. A1 now
// emits an explicit end-of-turn `turn-complete` envelope (and re-emits the
// turn's terminal pipeline-state on session-resume). W2 treats this as the
// authoritative "the turn is over" signal: any card still rendering `running`
// when it arrives is force-completed so no card hangs after the turn ends.
//
// The signal is intentionally minimal. `pipeline_id` is the turn's pipeline
// when the agent has one (so a future consumer could scope the force-complete
// to that pipeline); absent / null means "the whole turn idled" and every
// running card is settled. Both fields are optional so the agent can emit a
// bare `{}` payload and W2 still treats it as a turn-end. This is ADDITIVE to
// the existing live idle signal (`session-state` with `current_pipeline ===
// null`), which W2 ALSO honors — the two converge on the same force-complete.
export interface TurnCompletePayload {
  envelope_type?: "turn-complete";
  /** The turn's pipeline id, when the agent ran one. null/absent → whole-turn idle. */
  pipeline_id?: string | null;
  /** Final settle state hint for the turn ("complete" | "failed"); cosmetic. */
  final_state?: "complete" | "failed" | null;
}

// --- Outbound message constructors -------------------------------------- //

/** Generate a fresh ULID-like 26-char Crockford base32 id.
 *
 * Stub substitute for `python-ulid`. The agent's contracts package uses real
 * ULIDs; the web client only needs an opaque time-sortable string the agent
 * accepts as the envelope `id` / `session_id`. We use crypto.randomUUID()'s
 * entropy folded into Crockford base32 — preserves the 26-char ULID shape
 * the contracts package validates. A real ULID library is a clean upgrade
 * (surfaced as OQ-W-2).
 */
export function newUlid(): string {
  const crockford = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
  // 48-bit timestamp ms (10 chars) + 80-bit randomness (16 chars) = 26 chars.
  const ms = Date.now();
  let timeHex = ms.toString(16).padStart(12, "0");
  let out = "";
  // encode the 48-bit timestamp into 10 Crockford chars
  let n = BigInt("0x" + timeHex);
  for (let i = 9; i >= 0; i--) {
    out = crockford[Number(n & 31n)] + out;
    n >>= 5n;
  }
  // 16 random Crockford chars (80 bits)
  const rnd = new Uint8Array(10);
  crypto.getRandomValues(rnd);
  let randHex = "";
  for (const b of rnd) randHex += b.toString(16).padStart(2, "0");
  let r = BigInt("0x" + randHex);
  let rs = "";
  for (let i = 15; i >= 0; i--) {
    rs = crockford[Number(r & 31n)] + rs;
    r >>= 5n;
  }
  void timeHex;
  return out + rs;
}

export function nowZ(): string {
  // ISO-8601 with literal Z suffix — matches the contracts UTCDatetime
  // serializer (`.replace("+00:00", "Z")`-style; Date#toISOString already
  // emits Z).
  return new Date().toISOString();
}

export function envelope<P>(
  type: string,
  sessionId: string,
  payload: P,
): Envelope<P> {
  return {
    type,
    id: newUlid(),
    ts: nowZ(),
    session_id: sessionId,
    payload,
  };
}
