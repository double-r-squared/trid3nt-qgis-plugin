# 0087 - Animation wave 1: the frames-list output shape + SLIDER-stitch per-frame mode; goes/viirs SLIDER animations folded

Context: ADR 0078 named the satellite family's systemic gap -- the ANIMATION cluster
(`fetch_goes_animation`, `fetch_goes_blend_animation`, `fetch_goes_archive_animation`,
`fetch_goes_active_fire`, `fetch_viirs_day_fire`, and `fetch_glm_lightning` in its
`accumulation_window_s` mode) returns an ORDERED `list[LayerURI]`, one layer per
timestamp, which the router's single-`LayerURI`/`dict` `route()` cannot produce (the
two existing transforms MERGE frames -- `fan_out` -> one FGB, `tiled_mosaic` -> one
COG). ADR 0078 STOP-RULED the whole cluster as ONE interlocked build: the frames-list
output shape ALONE folds zero sources (speculative infra, the ADR 0056 bar), because
each source ALSO needs its own unbuilt per-frame COG builder. This wave builds the
frames-list shape TOGETHER with the SLIDER-stitch per-frame mode -- the pairing that
folds the three SLIDER-tile animations -- and characterizes the archive path.

Decision (2026-08-02):

## BUILT: the frames-list output shape (`shape: animation_frames`)

An animation source is an ordered per-timestamp sequence: each frame is its OWN cache
entry + its OWN `LayerURI`, so it cannot flow through the single top-level
`read_through` every other shape uses. The new shape is minimal, opt-in, and a strict
no-op for the 83 priors:

- `SourceShape` gains `animation_frames`; `HookSpec` gains `frames_plan` +
  `frame_bytes`; a cross-field validator ties the shape to `layer_type: raster` + both
  hooks (contract `source_spec.py`).
- `_router/hooks/__init__.py` gains the `FramePlan` PURE data class (per-frame
  `cache_params` + the scrubber `name` token + `layer_id` + `bbox`) and the
  `FrameDegraded` signal.
- `executors/animation_frames.py` owns the loop: it calls `frames_plan` for the
  ordered frame set, drives ONE `read_through` per frame (fetch_fn = `frame_bytes`),
  catches `FrameDegraded` to RECORD-and-drop a single transparent/off-swath/
  upstream-failed frame (never a silent gap), and raises the source's typed EMPTY only
  when EVERY frame degrades OR the window matched no frames (the honesty floor). It
  returns `list[LayerURI]` -- `route()` gains an early `animation_frames` branch that
  returns the list directly.
- `registration.py`: `_validate_hooks` requires both frame hooks for the shape; the
  promoted signature's return annotation is `list` for an animation source.

THE SCRUBBER NAME-TOKEN CONTRACT: `frames_plan` stamps each frame `name` with the
monotonic `step <N>` token + the ISO valid-time the plugin `render/temporal.py
group_frame_layers` groups on; a distinct product-label STEM keeps sibling products in
separate scrubber groups. PROVEN on EVERY fold by running the pure-python
`group_frame_layers` over the REAL produced names (offline, in the migrated tests):
GeoColor + Fire Temperature form TWO synchronized groups (`step <N>` -> the same
valid-time in both), the blend forms ONE, VIIRS forms ONE.

## FOLDED: the three SLIDER-stitch animations (`_satellite_slider` per-frame mode)

`fetch_goes_animation`, `fetch_goes_blend_animation`, `fetch_viirs_day_fire` all pull
CIRA/RAMMB SLIDER tiles and stitch->reproject->COG per frame via the SHARED
`_satellite_slider` substrate (UNCHANGED -- `stitch_slider_mosaic` +
`mosaic_to_cog_bytes`, the sanctioned per-frame tile-fetch impurity the `frame_bytes`
hook owns, like the library_delegate). Two hook modules:

- `hooks/goes_animation.py` (`frames_plan` + `frame_bytes`) serves BOTH GOES specs: a
  `band` in the blend-token set (or the band-less blend-delegate spec, whose
  `frames_plan` defaults to blend) builds ONE composite group (product =
  `geocolor_fire_temperature_blend`, `frame_bytes` fetches the co-temporal GeoColor +
  Fire Temperature single frames cache-mediated then
  `blend_geocolor_fire_temperature`); `geocolor` / `fire_temperature` build their own
  group. The GOES spelling zoo normalizes via the shared `_normalize_satellite`
  (unknown bird -> the loud `GOESInputError`; unserved-but-valid GOES ->
  `GOES_ANIM_INPUT_INVALID`).
- `hooks/viirs_day_fire.py` serves the JPSS polar Day-Fire animation (day/night
  local-solar-time filter + multi-satellite merge/sort).

Per-frame `cache_params` are byte-identical to the twins' (`source_class` +
`ttl_class: dynamic-1h` + the exact param dict), so the fold is value-identical AND
reuses any already-cached frames. Docstrings carried VERBATIM; metadata TWIN-identical
(`dynamic-1h` / `goes_animation` | `viirs_satellite` / `cacheable=True` /
`supports_global_query=False` / `tier=general`). Retrieval UNSHIFTED (3/3 corpus
phrasings top-8, model-free `retrieve_visible_tools`). `fetch_goes_blend_animation`
keeps its own registered name (a second `source.yaml`, same `source_class`) for the
backward-compat routing bench + cases.

LIVE proofs (small, polite): a few-frame real SLIDER animation per folded source with
frames published + the scrubber grouping proven on the real produced names.

Non-gating divergences (all three): (a) a `bbox=None` request stamps the source
`*_INPUT_INVALID` where the twin stamped a bare `BBOX_REQUIRED` -- both are
non-retryable input errors with the same server actionability, and the router's bbox
gate cannot split "required" from "degenerate" onto two distinct codes. (b) The router's
mandatory payload seam gets a small per-AOI `bbox_area` estimate where the list-return
twins registered none; it never warns on a normal fire AOI.

## STOP-RULED (characterized): fetch_goes_archive_animation -> wave 2

`fetch_goes_archive_animation` is AWS-archive-served, NOT SLIDER: its per-frame builder
reads a raw noaa-goes18 MCMIPC netCDF subdataset per band from S3, applies the CF
`scale_factor`/`add_offset`/`_FillValue`, and composites the Fire-Temperature/
true-color/hotspot RGB-RGBA via `netCDF4.Dataset` + warp. The frames-list shape (BUILT
here) is HALF of what it needs; the OTHER half is the `netcdf_cf_object` CF-scaled
netCDF access mode `fetch_goes_satellite` ALSO STOPs on (ADR 0078). It folds cleanly
in wave 2 as a thin archive `frame_bytes` hook over that mode (shared S3-list +
netCDF-band plumbing with `fetch_goes_satellite` + `fetch_goes_active_fire`). STOP with
that named residual. `fetch_goes_active_fire` (reuses the archive builder) +
`fetch_glm_lightning` (point-gridding mode) similarly await their per-frame access
modes -- the frames-list shape now unblocks them the moment those land.

## Metrics

Coded fetchers 14 -> 11 (-`fetch_goes_animation` -`fetch_goes_blend_animation`
-`fetch_viirs_day_fire`). Registry UNCHANGED at 186 (3 coded twins died, 3 spec-driven
surfaces took their names). Spec-served sources 83 -> 86 (+3);
`test_catalog_surfacing`: n_specs 83->86, non-internal/model-facing 82->85 (the
expected promote metrics, not regressions). Offline baseline UNCHANGED (the shape +
hooks are strict no-op for the 83 priors -- a new route() branch + executor gated on
`shape == "animation_frames"`).

Consequence: `route()` now returns `LayerURI | dict | list[LayerURI]`; the router
serves an ORDERED per-timestamp animation via a per-frame `read_through` loop with a
per-frame graceful-degrade honesty floor and the scrubber name-token contract, and the
3 SLIDER-stitch animations fold value-identically. The interlocked animation cluster
(ADR 0078) is now HALF-open: the shared shape is built; the two remaining per-frame
access modes (`netcdf_cf_object`, GLM point-gridding) each unblock their siblings when
they land. Extends the tier-3 contract (ADR 0056/.../0086) with the frames-list output
shape; supersedes nothing.
