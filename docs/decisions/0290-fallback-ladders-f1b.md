# ADR 0290 -- fallback ladders, wave F1b: the adversarial-panel fixes + NATE's migrate-all ruling

Status: LANDED. Date: 2026-08-20. Supersedes the "Consequences" paragraph of
ADR 0289 (see "What 0289 got wrong", below). Implements the confirmed findings of
the 4-lens adversarial panel that refuted F1 3-of-4.

## What 0289 got wrong

0289 closed with "no existing caller (SFINCS coastal, GeoClaw inundation, SCHISM
surge) changes behavior". That sentence was FALSE. The coverage gate lives in
`topobathy.validate` and fires for EVERY `fetch_topobathy` caller, declared rung
or not. Only SWAN declared one, so on a partial-CUDEM AOI the other three each
hit the gap error and handled it badly:

- SFINCS coastal turned it into a FAILED ENVELOPE -- a coastal flood run that
  worked before F1 now refused.
- GeoClaw's non-tsunami path caught it in a broad `except`, logged at INFO, and
  degraded to `fetch_dem` -- a LAND-ONLY 3DEP DEM. That paints flat 0 m ocean
  over 100% of the water, which is worse than the disease the gate exists to
  cure.
- SCHISM `tidal_hydro` had the identical broad-except degrade to `fetch_dem`.

The 0289 caller inventory was also incomplete: it named SCHISM "surge" but the
exposed caller is `tidal_hydro`, and it omitted GeoClaw's non-tsunami branch.

## NATE's ruling

Keep the coverage gate UNCONDITIONAL and migrate all three exposed callers onto
declared ladders in this wave. A gate that only fires for callers who opted in is
not a floor.

## What changed

### 1. All four exposed callers declare their rung

`_SFINCS_BATHY_FALLBACK` / `_GEOCLAW_BATHY_FALLBACK` / `_SCHISM_BATHY_FALLBACK`
join `_SWAN_BATHY_FALLBACK`, each `("etopo_bathy_base",)` -- a loud
`cross_dataset` rung. Full inventory of `fetch_topobathy` call sites:

| caller | gate | policy |
|---|---|---|
| `swan/wave_field` `_fetch_bathy_for_swan` | fires | declares `etopo_bathy_base` (F1) |
| `sfincs/flood` coastal branch | fires | declares `etopo_bathy_base` (F1b) |
| `geoclaw/inundation` `_fetch_topo_for_geoclaw` | fires (non-tsunami) | declares `etopo_bathy_base` (F1b) |
| `schism/tidal_hydro` `_fetch_bathymetry_cog` | fires | declares `etopo_bathy_base` (F1b) |
| `geoclaw/inundation` `_fetch_fine_nearshore_for_geoclaw` | EXEMPT (`include_regional_fine=True`) | best-effort nested layer |
| `mesh/generate_mesh` `_fetch_topobathy` | never reaches it | misnamed -- it calls `fetch_dem` (see "Parked") |

GeoClaw's and SCHISM's broad `except` are TIGHTENED: `LadderGap` /
`LadderRefused` are re-raised as the composer's own typed error
(`GEOCLAW_NO_BATHYMETRY` / `SCHISM_BATHYMETRY_UNAVAILABLE`) and never fall
through to the land-only `fetch_dem` leg. SFINCS's coastal handler catches
`LadderRefused` alongside `TopobathyError`.

### 2. Promise vs paint: the merge reconciles what actually painted

`cudem_coverage_fraction` is a pre-fetch FILENAME-FOOTPRINT union. Tiles can drop
AFTER it: `_assert_navd88` skips an unreadable header, and
`_composite_sources_to_array` skips an unreadable / empty source. ETOPO only
engaged at ZERO surviving tiles, so a MID-MERGE loss silently land-filled the
hole with the gate silent -- the exact class F1 was built to kill.

`_composite_sources_to_array` now returns ONE PAINTED FLAG PER INPUT SOURCE
(positional, because a source path is rewritten between selection and merge by
`_apply_vertical_offset`). `_select_and_merge` reconciles the footprint promise
against the CUDEM tiles that actually painted and raises the SAME typed gap when
they fall short -- so the ladder walks, or the fetch refuses.

Interior nodata remains unmeasured (a tile counts as covering its whole
0.25-degree square even where its own pixels are holes). That blind spot is now
STATED in `_assert_nearshore_coverage`'s contract rather than left implied.

### 3. The walker stops laundering errors

- The PRIMARY's typed error is what surfaces at REFUSE; later rung failures chain
  through `__cause__`. Before, the LAST rung's error surfaced, flipping
  `error_code` / `retryable` on the caller.
- An UNTYPED exception from any rung is wrapped in `LadderRefused` -- callers
  dispatch on `error_code` and a bare exception reads to them as an internal
  fault.
- Primary-always-attempted is an explicit check, not an assumption in a comment.

### 4. No false provenance under an exemption

`force_bathy_base` / `skip_cudem` / `include_regional_fine` exempt the coverage
check, so nothing measures what each source painted -- yet the walker stamped
`cudem_nearshore / primary / coverage=1.0` on a raster that is partly ETOPO. The
ladder now declares `coverage_exempt_params`; when one is present and the primary
serves, the activation stamps NOTHING (`Activation.coverage_unverified`). The
composite is instead labeled by a new `PARTIAL-CUDEM BATHYMETRY`
`fallback_warning` naming each source's share -- honest, and it makes the gap
error's remediation followable (below).

### 5-6. The refusal text tells the truth

- `skip_land`: the gap error cited the 3DEP land fill the caller had explicitly
  disabled. The message now branches -- with `skip_land` the uncovered water is
  NODATA, and that is what it says.
- DECLINE: declining re-raised the gap error verbatim, which instructed the user
  to permit the rung they had just declined, and `Activation.to_contract` dropped
  the coverage-0 row so the decline left NO trace. A decline now raises its own
  `LadderRefused` ("declined at the fallback gate ...") and `RungRecord.declined`
  keeps the row on the contract.

### 7. Gate timeout aligns with the gate it rides

`confirm_fallback` treated an unanswered card as the LABELED DEFAULT (proceed,
for `cross_dataset`) while `input_review` treats a timeout as cancel. Labeled
defaults now apply ONLY where there is nobody to ask (auto/headless, no bound
loop). Once the card is on a live `user_gated` session, silence is a DECLINE. The
card text says so.

### 8. Visibility seams

- `emission/layer_uri_emit.stamp_fallbacks` + a `fallbacks=` param on
  `emit_layer_uri` / `publish_input_layer` / `publish_raster_input_cog`: a layer
  rebuilt from a bare uri regains its rows instead of silently losing them.
- `route()` now DEFERS the emit-on-fetch surfacing until after `_stamp_activation`
  (via `_route_once(pending_emit=...)`), so the layer on the map carries the same
  rows as the layer the composer holds. Before, the surfaced input was a pre-stamp
  copy.
- `_stamp_activation` stamps EVERY frame of an `animation_frames` result; a
  `record` dict has no envelope to stamp, so a degraded activation is logged
  LOUDLY there rather than dropped in silence.
- `fallbacks/persist.py::persist_run_activations` writes
  `s3://<runs>/<run_id>/fallback_activations.json` next to `completion.json`. A
  SIDECAR rather than a field in `publish_manifest.json` because that file is
  WORKER-written (inert until an image rebuild) while the activations are a
  server-side fact about the inputs. All four composers call it.
- SWAN's `_stamp_swan_provenance` KEEPS its hand-stamp: the peak it stamps is the
  composer's RETURN VALUE, not a layer passing through the emission seam, so the
  seam param does not make it redundant.

### 9. The tool contract

`fetch_topobathy`'s `source.yaml` Fallback section claimed the old
CUDEM -> ETOPO -> land ladder and its Errors block never mentioned
`TOPOBATHY_COVERAGE_GAP`. Both corrected, plus the stale caveat. The remediation
is now FOLLOWABLE by the model: `fallback=` is a router kwarg outside the
inputSchema, but `force_bathy_base=true` IS a declared param, lays the same ETOPO
bed, and (per item 4) now produces the `PARTIAL-CUDEM` warning the message
promises. The composer-level `fallback=("etopo_bathy_base",)` is still named, as
what a COMPOSER does. `validate_topobathy`'s docstring no longer claims to be
pre-network -- it runs one memoized manifest GET.

## Consequences

- Every exposed `fetch_topobathy` caller now has a DECLARED policy at the call
  site. A partial-CUDEM coastal AOI SERVES (loudly labeled) for SFINCS, GeoClaw,
  SCHISM and SWAN; it refuses only for a caller that says nothing.
- A nearshore coverage gap can no longer reach a land-only DEM through any
  composer. The two paths that did are typed refusals.
- `LayerURI.fallbacks` is still not evidence of absence: an empty list means "no
  ladder governs this fetch" OR "coverage was unverifiable under an exemption".
  The `fallback_warning` carries the second case.
- `_composite_sources_to_array` returns a 4-tuple. Its only callers are in this
  module (plus `_merge_sources`, which nothing calls -- see "Parked").

## Parked (not touched here)

- `spec.fallback` (`registration.py:406`, consumed at `vector_fgb.py:537` and the
  `http_json` chain) is a PRE-EXISTING, NAME-COLLIDING substitution mechanism: 32
  specs declare it, and it is undeclared-and-ungated by the ladder machinery. In
  `vector_fgb` it names alternate ENDPOINTS of the same dataset (`same_data`); in
  `fetch_gridmet` it names a SIBLING TOOL (`fetch_era5_reanalysis`) -- a genuine
  cross-dataset swap riding silent. Registered as an F2 migration row in
  `docs/design/fallback-audit.md`.
- `mesh/generate_mesh._fetch_topobathy` is a misnomer: it calls
  `fetch_dem(source="3dep")` and writes the LAND-ONLY result to
  `topobathy.tif`, which is then sampled as a mesh bed. It never reaches the
  coverage gate because it never reaches `fetch_topobathy`.
- `topobathy._merge_sources` has no callers.
