# ADR 0092 -- Approved folds: fetch_population (WorldPop) + fetch_glm_lightning (frames)

Status: accepted (2026-08-03)
Follows: ADR 0083/0088 (fetch_population STOP chain -- the ACS-drop + whole-object-
window blockers), ADR 0078/0088 (fetch_glm_lightning STOP chain -- the single-vs-list
output-variant blocker). Both STOPs are dissolved by an EXPLICIT NATE approval, so
the two folds land this wave. Campaign coded-data-fetcher counter 9 -> 7.

## Context

Two twins stood STOP-RULED on approval-gated blockers, not on missing machinery:

- **fetch_population** (ADR 0076/0083): a raster-vs-vector VARIANT dispatch by
  dataset prefix. The `worldpop_*` leg is a real raster; the `acs_*` leg was
  HALF-BUILT (geometry=None "follow-up" + heuristic 15-state FIPS / 9-country ISO3
  tables). The router has no output-shape-switch-by-param mechanism, and building one
  for a half-built leg is speculative. WorldPop also needs a whole-object-download-
  then-window raster access mode (WorldPop serves HTTP 200 to range requests, so
  `/vsicurl` and `direct_window`/`multi_url` -- which need byte-range -- cannot window
  it).
- **fetch_glm_lightning** (ADR 0088): the accumulation (list) mode was a clean
  `animation_frames` fold, but the DEFAULT output was a SINGLE accumulated `LayerURI`
  and `animation_frames` ALWAYS returns `list[LayerURI]`, so a pure fold would change
  the default output contract (`layer` -> `[layer]`).

## Decision

**NATE approved (2026-08-03):**

1. **fetch_population** -- DROP the half-built ACS leg (flag-not-copy, the river-
   NHDPlus precedent); fold the WorldPop raster leg only.
2. **fetch_glm_lightning** -- CHANGE the output contract: the default output becomes a
   frames LIST (the single-accumulation case = a one-frame list). This dissolves the
   ADR 0088 single-vs-list STOP -- no new single-vs-list variant machinery is needed;
   the frames-list shape carries the single case as N=1 for free.

### fetch_population -> WorldPop library_delegate raster

The WorldPop leg folds onto the EXISTING `library_delegate` raster mode (ADR 0074), no
new access mode: the whole-object-download-then-window logic + the bespoke ISO3-
resolve + URL-compose are exactly the "a source's socket + discovery is bespoke"
delegate case. Two pure-ish hooks (`hooks/worldpop.py`):

- `worldpop.validate` -- the pre-cache vintage gate (parse + range-check the
  `worldpop_<YYYY>` year against the published Global_2000_2020 window; reject `acs_*`
  and anything non-worldpop with the typed `POPULATION_INPUT_INVALID`). Offline-
  testable, byte-identical to the twin's parse-time validation.
- `worldpop.read` -- the delegate socket: resolve ISO3 from the bbox center, compose
  the country/year/resolution URL, download the whole GeoTIFF once (WorldPop has no
  range support), rasterio-window the AOI, mask nodata -> NaN, return `(array,
  transform, crs)` for the shared COG writer.

**ACS-removal surface change (approved):** the `acs_*` datasets are REMOVED from the
surface. `dataset` shrinks to the `worldpop_*` set; the docstring is rewritten at the
source (front-loaded routing block, Bedrock 1000-char truncation). A user asking for
census/tract population is routed to the dedicated `fetch_census_acs` tool (already
registered) -- named explicitly in the new docstring + the input-error message. An
`acs_*` request gets the standard typed unknown-dataset input error, pre-network.

### fetch_glm_lightning -> animation_frames (frames-list default)

Folds onto `shape: animation_frames` via `hooks/glm.py` (`glm.frames_plan` +
`glm.frame_bytes`) over the shared `imagery._goes_archive_core` grid + RGBA writer.
The twin's parameter NAMES are stable (`accumulation_window_s` still fans the window
into N buckets); the ONLY contract change is the default RETURN shape:

- `frames_plan` splits the window into accumulation buckets (single mode -> ONE bucket
  -> a one-frame list; `accumulation_window_s` -> N buckets, even-subsampled to the
  frame cap) and stamps each frame's `step <N>` scrubber name-token + the twin's byte-
  identical per-frame cache_params. No network (each bucket lists its own S3 window in
  frame_bytes).
- `frame_bytes` builds ONE bucket's COG: list GLM-L2-LCFA granules (anonymous NOAA
  S3), download, bin GROUP energy onto the ABI-co-registered EPSG:4326 grid via
  `numpy.add.at` (GLM lat/lon carry parallax -> bin directly, NEVER warp), bake the
  purple log-ramp RGBA. A bucket with no granules / no in-AOI groups / an upstream
  failure raises `FrameDegraded`; the executor's honesty floor raises the typed
  `GLM_EMPTY` only when EVERY bucket degrades -- so a single empty window (one-frame
  list) still surfaces as a hard typed no-data, byte-for-byte the twin's per-bucket
  skip.

**GLM contract change (what consumers see):** `fetch_glm_lightning(...)` now ALWAYS
returns `list[LayerURI]` (the promoted signature return annotation is `list`). The
single-accumulation default is a ONE-frame list (a static overlay, NOT a scrubber
group -- `group_frame_layers` needs >=2 frames); `accumulation_window_s` returns an
ordered multi-frame scrubber group. The single-mode `LayerURI.name` now carries a
`step 1` token (previously a bare `<iso>..<iso>` label). No in-repo consumer imported
the twin's single-`LayerURI` shape (audited: only `_ALWAYS_OFFLOAD_SYNC_TOOLS` name-
membership, category tables, corpus queries, and tests -- all name-keyed or migrated).

## Divergences (accepted)

1. **ACS leg dropped** (approved removal, not a parity break) -- documented as a
   deliberate divergence: `acs_*` -> typed input error; tract population -> fetch_census_acs.
2. **GLM default output** `layer` -> `[layer]` (approved contract change).
3. GLM `bbox=None` -> the router's uniform `GLM_INPUT_INVALID` (not the twin's
   distinct `BBOX_REQUIRED`); the out-of-range/unknown-satellite/window errors all map
   to the source-stamped `GLM_INPUT_INVALID` / `GLM_EMPTY` (the fold's uniform A.6
   stamping, indistinguishable in class/retryable to the twin's typed errors).
4. Population out-of-range vintage is now a `POPULATION_INPUT_INVALID` at the validate
   gate (the twin raised an `UpstreamAPIError` at parse time -- same pre-network, same
   non-retryable spirit, honestly reclassified as input).

## Consequences

- Coded tools 98 -> 96, coded fetchers 11 -> 9, campaign coded-data-fetcher counter
  9 -> 7, spec-served 88 -> 90; registry UNCHANGED (2 twins died, 2 specs took names).
- No new SourceSpec field / access mode / HookSpec field: population reuses
  `library_delegate` raster, glm reuses `animation_frames` -- both proven modes.
- `compute_exposure_summary._fetch_population_layer` re-pointed to the registry closure
  (the buildings-seam pattern); it reads only `.uri` as a raster (WorldPop single-band
  COG), so the repoint is mechanical. Consumer test suite green.
- No flood-seam re-point (grep-verified: neither twin is on the flood canary path);
  compute_exposure_summary is an exposure composer, not a flood-solver leg.
