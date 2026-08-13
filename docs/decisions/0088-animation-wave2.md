# 0088 - Animation wave 2: the netcdf_cf_object per-frame mode; goes_archive_animation + goes_active_fire folded; goes_satellite + glm STOPPED with refined residuals

Context: ADR 0087 (animation wave 1) BUILT the frames-list output shape
(`shape: animation_frames` + `HookSpec.frames_plan`/`frame_bytes` +
`FramePlan`/`FrameDegraded` + `executors/animation_frames.py`) and folded the three
SLIDER-stitch animations onto it, but STOP-ruled the AWS-archive cluster: the raw
`ABI-L2-MCMIPC` netCDF sources (`fetch_goes_archive_animation`,
`fetch_goes_active_fire`) need a per-frame COG builder the SLIDER mode does not
provide -- the `netcdf_cf_object` CF-scaled netCDF access mode
`fetch_goes_satellite` also STOPs on (ADR 0078), plus the GLM `point-gridding`
mode. The frames-list shape was HALF-open. This wave builds the archive per-frame
builder and closes the two archive-served animation sources.

Decision (2026-08-03):

## BUILT: the netcdf_cf_object per-frame mode (`hooks/goes_archive.py`)

The archive per-frame builder is the exact CF-scaled MCMIPC read the STOP named:
list the in-window `ABI-L2-MCMIPC` S3 keys (anonymous public archive, plain HTTPS
`?list-type=2`), download ONE netCDF per frame, apply the per-band CF
`scale_factor`/`add_offset`/`_FillValue` + `valid_range`, reproject each band to
EPSG:4326, and composite the Fire-Temperature / true-color / hotspot-RGBA / baked
product. It lands as ONE `frames_plan`/`frame_bytes` hook pair -- NOT a new router
mode -- over a relocated substrate:

- `imagery/_goes_archive_core.py` (NEW, no registered tool): the ENTIRE reusable
  body of the deleted `fetch_goes_archive_animation.py` twin -- the S3 window lister
  (`_list_archive_keys_in_window`), the shared CF netCDF band read + reproject
  (`_read_archive_bands` / `_warp_band_to_physical`), the composite math
  (`_fire_temperature_rgb` / `_true_color_rgb` / `_detect_active_fire_mask` /
  `_fire_hotspots_rgba` / `_bake_fire_over_base`), the RGBA COG writer, the
  per-frame COG builder (`_fetch_archive_frame_cog_bytes`), the constants, and the
  typed errors. Mirrors the wave-1 `_satellite_slider` substrate: the tool dies, the
  substrate lives on for the hooks (and for `fetch_glm_lightning`, re-pointed here).
- `hooks/goes_archive.py`: ONE `frames_plan` + `frame_bytes` pair serving BOTH specs;
  the `ingest.archive.mode` flag selects the `full` band-selectable surface
  (144-frame cap, ~6.5h default window) vs the `hotspots` split-window surface
  (fixed `fire_hotspots` band, 24-frame cap, ~20min default window, distinct
  cache-param shape + label). `frame_bytes` delegates to
  `core._fetch_archive_frame_cog_bytes` and raises `FrameDegraded` on an empty /
  off-disk / upstream-failed frame (the executor's honesty floor raises the typed
  `GOES_ARCHIVE_EMPTY` only when EVERY frame degrades).

Two strictly-additive `FramePlan` fields carry what the SLIDER (ts-addressed) frames
never needed and what a single spec-level output cannot hold:

- `fetch_context: dict` -- OUT-OF-cache-key fetch inputs: the opaque MCMIPC S3
  object key + the raw (unrounded) fetch args. An archive frame is addressed by a
  per-scan object key, NOT a reconstructable ts; putting it in `cache_params` would
  break the byte-identical cache key, so it rides beside it. Defaulted empty -> a
  strict no-op for the wave-1 SLIDER frames.
- `style_preset: str | None` -- a per-frame preset override (the archive's per-band
  `goes_rgb_animation` for the RGB composites vs `goes_fire_hotspots_rgba` for the
  transparent hotspot RGBA) a single `spec.output.style_preset` cannot carry. The
  executor uses `frame.style_preset or spec.output.style_preset`. None -> the spec
  preset (strict no-op for the single-preset SLIDER sources).

## FOLDED: fetch_goes_archive_animation + fetch_goes_active_fire

Both fold onto `shape: animation_frames` (source_class `goes_animation`, error_prefix
`GOES_ARCHIVE`) via the shared hooks. Per-frame `cache_params` are BYTE-identical to
the twins' per-frame param dicts (archive: `{bbox, product, satellite, ts_start,
gamma:1, res_deg[, bt_c07_min_k, bt_diff_min_k]}`; active_fire: `{bbox,
product:"fire_hotspots", satellite, ts_start, bt_c07_min_k, bt_diff_min_k,
tool:"fetch_goes_active_fire"}`), so the fold is value-identical AND reuses any
already-cached frame (the cache key is `source_class||params||vintage`, never the
tool name -- the `tool` param is exactly what keeps the two hotspot keyspaces
distinct). Docstrings carried VERBATIM (6277 / 2930 ASCII chars); signatures
twin-identical; metadata TWIN-identical (`dynamic-1h` / `goes_animation` /
`cacheable=True` / `supports_global_query=False` / `tier=general`). Satellite
spelling-zoo normalization (`_normalize_satellite`) + band aliasing
(`natural_color`/`geocolor_raw` -> `true_color`) reproduced in the hook. Retrieval
UNSHIFTED (3/3 corpus phrasings top-8, model-free `retrieve_visible_tools`).

LIVE proofs (real anonymous noaa-goes18 archive -> CF-scaled netCDF -> composite ->
COG -> MinIO trid3nt-cache): a 3-frame Fire-Temperature archive animation over a Utah
AOI (one scrubber group, real ISO valid-times, readback = a valid 3-band uint8
EPSG:4326 COG) + a 3-frame active-fire hotspot run over a N.California AOI (real
split-window hot-pixel detections, `goes_fire_hotspots_rgba`). Value coverage ->
`test_router_goes_archive.py` (23 tests: cache-param byte-identity, naming, per-band
preset, scrubber grouping via the plugin `group_frame_layers`, the all-degrade +
empty-window honesty floor, satellite/band input errors, and the relocated
band-math core).

Non-gating divergences (both, carried from the wave-1 pattern): (a) a `bbox=None`
request stamps `GOES_ARCHIVE_INPUT_INVALID` where the twins stamped a bare
`BBOX_REQUIRED` (same non-retryable actionability; the router's required-bbox gate
cannot split "required" from a specific code). (b) The list-return twins registered
NO payload estimator; the router's mandatory seam gets a small per-AOI `bbox_area`
estimate that never warns on a normal fire AOI. (c) A genuinely-unknown satellite
raises the shared `GOESInputError` (`GOES_INPUT_INVALID`) directly, as both twins did.

## STOP-RULED (refined residuals): fetch_goes_satellite + fetch_glm_lightning

`fetch_goes_satellite` -- the netcdf_cf_object READ plumbing now exists (in the core),
but the tool's OUTPUT surface does not fit the frames-list shape: it returns a SINGLE
`LayerURI` (a `raster-cog` shape, not a per-timestamp list), a SINGLE-band float32
PHYSICAL-units COG (reflectance / K -- NOT the uint8 RGB composite the archive builder
produces), with distinct "most-recent-frame" semantics (no window; a 15-minute
`valid_time` cache-rounding), a CONUS-sector pre-gate, and a non-ASCII em-dash in its
LayerURI name. Folding it needs a `raster-cog` + `library_delegate`-style access mode
returning a single float32 band + a `pre_resolve` for the valid_time rounding + a
name-string divergence (the em-dash cannot be reproduced under the ASCII rule). A
THIRD distinct access surface, not the archive builder. STOP with that named residual.

`fetch_glm_lightning` -- the GLM `point-gridding` per-frame math (download GLM-L2-LCFA
granules via anonymous boto3 UNSIGNED, `numpy.add.at`-bin group energy onto the
2 km grid, log-ramp purple RGBA) is a clean `frame_bytes` for the LIST path, BUT the
twin's DEFAULT output is a SINGLE accumulated `LayerURI` and only its opt-in
`accumulation_window_s` mode returns `list[LayerURI]`. The `animation_frames` executor
ALWAYS returns a list, so a pure fold would change the default output contract
(`layer -> [layer]`). Closing it needs a single-vs-list output VARIANT on the shape
(new machinery for ONE source, the ADR 0056 bar) -- distinct from the archive fold.
STOP with that named residual; the frames-list list-mode is ready the moment the
variant lands. (glm's imports were re-pointed to `_goes_archive_core`; it stays coded.)

## Metrics

Coded fetchers (module under `.fetchers.`) 13 -> 11 (-`fetch_goes_archive_animation`
-`fetch_goes_active_fire`); coded tools 100 -> 98; the universal-ingest campaign's
coded-data-fetcher counter 11 -> 9. Registry UNCHANGED at 186 (2 coded twins died, 2
spec-driven surfaces took their names). Spec-served sources 86 -> 88 (+2);
`test_catalog_surfacing`: n_specs 86->88, model-facing index 85->87, arm-ON declarable
delta -85->-87 (the expected promote metrics, not regressions). Offline baseline
UNCHANGED (the two additive `FramePlan` fields + the `frame.style_preset` fallback are
strict no-ops for the wave-1 frames and the 86 priors).

Consequence: the archive-served half of the ADR 0078 animation cluster is now CLOSED
-- the raw MCMIPC per-frame builder folds two sources value-identically over a shared
substrate, the frames-list shape is fully general (SLIDER-stitch + netCDF-CF), and the
two remaining GOES-family holdouts (`fetch_goes_satellite`'s single-band raster-cog
shape, `fetch_glm_lightning`'s single-vs-list output variant) are each characterized
to one precise unbuilt mechanism. Extends the tier-3 contract (ADR 0056/.../0087) with
the `fetch_context` + per-frame `style_preset` FramePlan carriers; supersedes nothing.
