# ADR 0110 -- The fetch-time provenance channel + the fetch_topobathy fold

Status: accepted (2026-08-04)
Follows: ADR 0089 (the topobathy STOP, sharpened to a GATE-1 failure: the four
`TopobathyResult` fields are fetch-time provenance unrecoverable from the final COG,
and `route()` has no fetch_fn->envelope provenance channel -- decisive because a
cache hit never runs `fetch_fn`), ADR 0097 (the dispatch seam + the dem
delegate/hook fold patterns reused here), ADR 0091 (the loud/user-gated
cross-dataset fallback norm + its follow-up row characterizing topobathy's own
silent internal fallbacks). FETCHER FINALE WAVE 1.

## Context

`fetch_topobathy` was the last flood-adjacent coded data-fetcher. ADR 0089 stopped
its fold on ONE decisive, gate-level blocker (not a judgement call): its result
carries four FETCH-TIME provenance fields -- `bathymetry_present`,
`fallback_warning`, `cudem_tile_count`, `regional_tile_count` -- recording WHICH of
the four heterogeneous legs (CUDEM 1/9" tiles, NCEI regional 1 m tiles, the ETOPO
2022 global fallback, 3DEP land) actually painted the merge. Those facts are NOT
recoverable from the produced single-band float32 COG, the router's only
post-serialize seam (the envelope hook) is pure over the final bytes, and on a
cache hit `route()` never calls `fetch_fn` at all -- so a fold could not reproduce
the twin field-for-field. ADR 0089 named the unblock precisely: "a fetch-time
provenance channel (bytes + sidecar, cache-replayable)". This wave builds that ONE
general capability and lands the fold on it.

## Decision -- part 1: the fetch-time provenance channel (general machinery)

A cache-replayable sidecar from fetch to envelope, kept to the MINIMAL general
thing so any future spec can declare it (topobathy is consumer #1; the
storm_tracks / goes_satellite / nwm finale waves get it for free).

- `cache.py` gains a `ProvenanceRecorder` (a single-slot sink) + a contextvar +
  `record_provenance(dict)`. During a NON-cached fetch the executor/delegate calls
  `record_provenance({...})`; the recorder is bound (contextvar) by `read_through`
  around `fetch_fn`, so the byte-only `fetch_fn` signature is untouched.
- `read_through(..., provenance=recorder)` persists the recorded dict as a SIBLING
  object next to the artifact -- `<key>.provenance.json` under the SAME cache key --
  on a miss, and REPLAYS it from that sidecar on a hit (the executor never runs, so
  the sidecar is the only source of truth). `read_through` is the SOLE cache-prefix
  writer, so the sidecar lives there, not in the router. `ReadThroughResult` gains a
  `provenance` field.
- `route()` creates a recorder when `output.provenance` is set, threads it into
  `read_through`, and hands `result.provenance` to the envelope hook. `_apply_envelope`
  passes it ONLY to a hook that DECLARES a `provenance` parameter (signature
  inspection), so the four ADR-0073 envelope hooks are called with their original
  4-arg signature -- strictly additive.
- `source_spec.py`: `OutputSpec.provenance: bool = False`. `registration._validate_hooks`
  requires a `provenance` spec also declare `hooks.envelope` (the consumer).

Rules honoured: ADDITIVE (a spec without `provenance` gets recorder=None ->
byte-identical `read_through`, no sidecar object, no extra I/O -- the priors are
stash-proof, proven by the offline suite unchanged + a dedicated no-op test);
REPLAYABLE (a cache hit returns the SAME dict the original fetch recorded);
SIZE-BOUNDED (a small dict); NEVER SECRET-BEARING (source-attribution counts +
honest warnings only). A cache object written BEFORE the channel has no sidecar ->
provenance replays as `None` and the envelope hook's DECLARED DEFAULTS hold --
byte-identical to the twin's own cache-hit behaviour (which reverted to
`bathymetry_present=True` / no warning / counts 0 because `fetch_fn` did not run).

## Decision -- part 2: the fetch_topobathy fold

`fetch_topobathy` folds onto a `library_delegate` raster spec (`source.yaml`) with
the bespoke 4-leg discovery + heterogeneous UTM warp-merge + NAVD88 datum gate in
`hooks/topobathy.py`:

- `topobathy.validate` (delegate_validate) -- the US-coastal-envelope + finiteness
  (offset / timeout / min_pixel) gate raised pre-cache as `TopobathyInputError`. The
  router's generic bbox validation already stamps `TOPOBATHY_INPUT_INVALID` for
  shape / range / degenerate bboxes (via `error_prefix=TOPOBATHY`,
  `input_error_suffix=INPUT_INVALID`), so the code is identical either path.
- `topobathy.read` (delegate) -- the CUDEM -> regional -> ETOPO -> 3DEP-land select
  + datum gate + precedence warp-merge, returning the composite `(array, transform,
  crs)` for the shared COG writer (the accepted ADR-0074 re-encode divergence class:
  same array / CRS / NaN-nodata, DEFLATE vs the twin's LZW). It RECORDS the four
  provenance fields via the channel.
- `topobathy.envelope` -- the twin's exact `topobathy-{w}-{s}-{e}-{n}` layer_id and
  name, plus the four provenance fields read back from the channel (declared
  defaults on a pre-channel cache object).

`TopobathyResult` moves to `contracts/execution.py` + `LAYER_RESULT_MODELS`. The
`TopobathyError` classes move to `hooks/topobathy.py` with base `FetchError` (so
`library_delegate.invoke`'s `except FetchError: raise` passthrough preserves the
pinned `error_code` -- e.g. `TOPOBATHY_DATUM_MISMATCH` -- through the delegate
wrapper, the ADR-0097 pattern; still a `RuntimeError`, so `isinstance` holds).

## Decision -- part 3: the loud-fallback norm applied in the fold (ADR 0091 row)

Applied rather than preserved (NATE doctrine: fix the dishonesty in the fold):
- (a) the 3DEP land leg's SILENT swallow (`fetch_dem` failure -> `None` -> silent
  bathy-only merge with NO warning) becomes a LABELED `land_absent` degrade -- a
  provenance entry + a `fallback_warning` naming `land_absent` (BATHYMETRY-ONLY,
  onshore cells nodata). No silent land drop.
- (b) the CUDEM -> ETOPO substitution stays PROCEED-AND-WARN (GLOBAL-FALLBACK
  bathymetry), and the warning is verified to reach the envelope on every path (via
  the channel, so a cache hit carries it too).
NOT hard-gated this wave: coastal flood scenarios depend on best-effort terrain, so
labeling (not a pause-and-ask gate) is the 0091-agreed treatment for this consumer.

## Consequences

- fetch_topobathy twin DELETED (`fetch_topobathy.py`, ~1,587 LOC); DEM/merge tests
  migrated to `test_router_topobathy.py` (+ the channel proofs); consumers re-pointed
  (flood.py keeps a module-level `fetch_topobathy` registry-closure shim + imports
  `TopobathyError` from the hook module; inundation.py / wave_field.py resolve the
  registry closure; the coastal-flood test imports `TopobathyResult` from contracts).
- Registry UNCHANGED at 173 (one twin died, one spec took its name). spec-served
  91 -> 92. Campaign coded-data-fetcher counter 4 -> 3.
- New contract surface: `OutputSpec.provenance`; `ReadThroughResult.provenance` +
  `ProvenanceRecorder` + `record_provenance`; `TopobathyResult` in
  `LAYER_RESULT_MODELS`. All strict no-ops for every prior spec.
- Offline baseline preserved at EXACTLY 9 by SET (`fetch_resolution_gate` x4 +
  `run_river_dye_scenario` x5); the `[fetch_topobathy-topobathy]` gate member fails
  IDENTICALLY pre/post. Retrieval unshifted (docstring carried verbatim into the
  spec). Daemon boot clean.
- FLOOD CANARY mandated + run (flood.py DEM seam re-pointed): a direct-call
  `sfincs_flood` coastal drive proves status=ok + depth COG + a sane envelope.
- The DELETION_LEDGER `fetch_topobathy fold` row resolves (DELETED, the 0089 chain
  closes) and the `fetch_topobathy internal source fallbacks vs the gated-fallback
  norm` follow-up row resolves (the land_absent labeled degrade + the channelled
  CUDEM->ETOPO warning land here).

The provenance channel is the general capability ADR 0089 named; topobathy is its
first consumer, and it unblocks the same fetch-time-provenance shape wherever a
future composite needs it.
