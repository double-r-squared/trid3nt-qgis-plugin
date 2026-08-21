# ADR 0291 -- fallback ladders, wave F1c: the merge consumes its own paint, exemption semantics, honest walker refusals

Status: LANDED. Date: 2026-08-20. Follows ADR 0290 (F1b), which a second 4-lens
adversarial panel re-refuted. Corrects two claims 0290 made (see "What 0290 got
wrong").

## What 0290 got wrong

1. "the ladder walks, or the fetch refuses" is FALSE in two windows.

   - `_composite_sources_to_array` returns one painted flag per source, but
     `_select_and_merge` read only the CUDEM slice; ETOPO's flag was discarded.
     `bathymetry_present`, the PARTIAL-CUDEM warning and `has_etopo` all keyed on
     SELECTION.
   - TOTAL CUDEM LOSS: when every intersecting tile drops (datum gate, or one
     failed HTTPS GET on the tile index), auto-ETOPO engages -- but
     `_mask_land_leg_ocean_fill` ran only under `force_bathy_base`, so the raw
     3DEP land leg's flat 0 m ocean fill CLOBBERED the ETOPO column at higher
     precedence. The fetch returned SUCCESS with `bathymetry_present=True` and a
     `cudem_nearshore / primary / coverage=1.0` row over water that was land
     fill. That is the exact disease the coverage gate exists to cure.

2. "no coverage claim under an exemption" did not hold on the path the ladder
   actually walks. The `etopo_bathy_base` rung declares
   `params={"force_bathy_base": True}`, and `force_bathy_base` is also in
   `coverage_exempt_params` -- but the walker computes the exemption from the
   CALLER's params only, so promise-vs-paint reconciliation never ran on the rung
   attempt. Rows could read 89/11 (the footprint promise) while paint was 44/56.

The serving rung's coverage was `1.0 - <what the previous rung promised>` --
arithmetic, not evidence. And `Activation.coverage_unverified` was set only when
the PRIMARY served an exempted request; an alternative serving one still stamped
numbers.

## What changed

### 1. The merge consumes ALL of its paint evidence

`_select_and_merge` now derives every provenance field from the per-source
painted flags: `bathymetry_present`, `cudem_tile_count`, `regional_tile_count`,
`land_absent` and the warning's `has_etopo`. `_merge_topobathy_to_array` (the
byte-helper the merge tests drive) does the same. `_merge_sources` had no
callers and is DELETED (ledger row).

### 2. The land leg is masked whenever an ETOPO base is present

Not only under `force_bathy_base`. An ETOPO base is in the merge precisely when
CUDEM could not carry the nearshore, and the 3DEP leg sits ABOVE it in
precedence -- so without the mask the coarse-but-real bed the
`GLOBAL-FALLBACK BATHYMETRY` warning promises was being overwritten by flat 0 m
ocean. The warning is now true.

### 3. Painted-short refuses even under an exemption

`cudem_short and not etopo_painted` raises `TOPOBATHY_COVERAGE_GAP`
unconditionally: `force_bathy_base` / `skip_cudem` / `include_regional_fine` buy
a COARSER bed, never a FAKE one. A nearshore request never returns 3DEP land
fill over water as a success. (Where CUDEM never intersected the AOI at all
there is no composite to have a gap in -- that path keeps its
`GLOBAL-FALLBACK` / `BATHYMETRY ABSENT` warnings unchanged.)

### 4. Rows report MEASURED paint

A result may report `rung_coverage` -- rung name -> the fraction of the request
that rung's source PAINTED. `TopobathyResult` carries it (provenance-channel
borne, so it survives a cache hit) and the walker reconciles its promise-derived
shares against it, appending rows for rungs the capability measured but the walk
never descended to. This covers item 2 above: a rung-injected param no longer
exempts that rung's own attempt from paint accounting.

### 5. An exemption is visible, never silent and never numeric

The exemption is the REQUEST's, so it applies to whichever rung serves, not just
the primary. Under one the activation stamps NO number (as 0290 intended) and
now carries `Activation.unverified_note`, which rides `fallback_note` onto the
layer: "served with force_bathy_base set ... the per-rung share of this result is
UNMEASURED". `summarize_tool_result` hoists `fallback_warning` alongside
`fallback_note`, so the labeled PARTIAL-CUDEM text reaches the model instead of
dying in the 200-char result clip -- the remedy the gap message advertises is now
followable AND self-labeling end to end (GeoClaw tsunami, SCHISM pahm_surge).

### 6. AUTO never asks

`confirm_fallback` keyed the ask on the presence of an emitter, so an AUTO run
with a live session presented a card and blocked for the full 300 s TTL. Mode
decides: `user_gated` asks; `auto` / headless applies the labeled default
immediately (synthetic -> refuse, law 9) -- the `input_review` sibling's
semantics, where auto refuses a physics demo default without asking.

### 7. Walker refusals stop laundering

- The DECLINE branch fires only when a gap was actually recorded. A retryable
  primary error (CUDEM 503) plus a declined gate now surfaces the PRIMARY's typed
  error with its `retryable` intact, instead of a non-retryable
  `TOPOBATHY_COVERAGE_GAP`. The decline still rides the activation.
- An UNTYPED failure under any rung wraps as `FALLBACK_LADDER_ERROR`, NOT the
  capability's coverage code, with the original `retryable` inherited. Cache,
  transport and validation faults raise bare exceptions under a rung, and the
  tightened composer excepts were reading them as terminal bathymetry refusals.
- The PRIMARY's error is preferred over an alternative's; an alternative's
  `LadderGap` no longer surfaces bare in its place, and a `LadderGap` carrying no
  `error_code` wears the ladder's terminal code rather than escaping untyped.

### 8. The cache no longer replays a stale account of its own bytes

`compute_cache_key` is params + TTL-vintage only, so a cache object written
before a provenance fix replayed its sidecar verbatim for the rest of the 30-day
bucket -- a fixed AOI kept serving the degraded reading (rows `[]`,
`fallback_warning: null`; proven live on object `7b0bad3d...`). The sidecar now
carries `provenance_schema`; a hit whose sidecar is missing or predates
`PROVENANCE_SCHEMA` is a MISS and refetches. Bumping the constant is how a
provenance fix reaches already-cached AOIs. The four provenance-bearing sources
(`fetch_topobathy`, `fetch_storm_tracks`, `fetch_goes_satellite`,
`fetch_noaa_nwm_streamflow`) each refetch once. A source with no recorder is
untouched.

### 9. Seam agreement + emission scope

`route()` deferred the emit-on-fetch surfacing but guarded it on
`isinstance(result, LayerURI)` while `_stamp_activation` handles list and record
shapes. It now surfaces every `LayerURI` in the result, and -- when a rung with
its own `source` / `call` served without going through `_route_once` -- falls back
to the request's own emit arguments. Without that the user_supplied bed was the
one input that never reached the map.

SWAN's `_stamp_swan_provenance` now merges its bed rows through
`stamp_fallbacks` instead of re-implementing the merge; 0290's item-8
justification for the hand-stamp was wrong (GeoClaw / SCHISM / SFINCS stamp
return-value layers through the shared seam too). The `Wave bed:` prefix is
dropped for the shared seam's dedupe and idempotency.

## What the panel raised and this ADR REFUSES to change

- **"flood.py stamps `_bathy_activation` on every published layer including the
  peak-wave layer."** Traced and NOT confirmed. `published_layers` is
  `role="primary"` only, and for SFINCS those are the fields of ONE solve on ONE
  bed: `workers/_raster_postprocess/postprocess.py` maps the depth and waves
  postprocess kinds to `flood_depth` / `wave_height` in the same run's
  `outputs.json`, and the wave field is SnapWave riding the same quadtree bed the
  ladder painted. The activation describes the bed under both. No change made
  rather than a wrong narrowing.

- **"the `RungRecord.declined` row is read by nothing in production."** Half
  true, corrected here rather than over-claimed. A decline that ENDS the walk
  cannot persist through `persist_run_activations`: the refusal happens at fetch
  time and no run prefix exists yet -- its visibility is the refusal message,
  which names the declined rungs verbatim and reaches the model through the
  composer's failed envelope (`error_detail=str(exc)`). A decline the walk
  DESCENDS PAST does reach production: the row rides `to_contract()` onto the
  layer and into `fallback_activations.json`. Pinned by test.

## Consequences

- `TopobathyResult` gains `rung_coverage` (additive, defaults `None`).
- `LadderRefused` gains a `retryable` kwarg; `FALLBACK_LADDER_ERROR` is a new
  code a composer may see from a ladder-governed fetch.
- A bare `LadderGap` no longer escapes the walker -- callers dispatch on
  `error_code`, so the terminal code is what surfaces.
- A one-time cache refetch for the four provenance-bearing sources.
- The exhibit AOI A/B is unchanged: gap 0.8889, rows 89/11 -- now MEASURED
  (`rung_coverage` = 0.8889 / 0.1111) rather than promise arithmetic.

## Evidence

Exhibit AOI `(-85.55,29.70,-85.40,29.85)`, live:

- A (no `fallback=`): `TOPOBATHY_COVERAGE_GAP`, `covered_fraction=0.8888888...`,
  `retryable=False`.
- B (`fallback=("etopo_bathy_base",)`): rows
  `cudem_nearshore/primary/0.888889` + `etopo_bathy_base/cross_dataset/0.111111`,
  `rung_coverage={'cudem_nearshore': 0.8888..., 'etopo_bathy_base': 0.1111...}`,
  `cudem_tile_count=3`.
- `force_bathy_base=true`: `fallbacks=[]`, `fallback_note` = the UNMEASURED note,
  and `summarize_tool_result` keys
  `['fallback_note', 'fallback_warning', 'result', 'status', 'tool']`.
- Stale cache: object `7b0bad3d137a27b1944ce336252f12bc` logged
  "provenance sidecar STALE (schema None != 2) -- treating the cached object as a
  MISS", refetched and rewrote its sidecar 116 -> 570 bytes, now carrying the
  PARTIAL-CUDEM warning and `rung_coverage`.

Suite: four slices at baseline (4 `fetch_resolution` + 2 `river_dye`), contracts
721, `ws_smoke` `all_passed=True`, flood canary `status=ok`. No `workers/` path
touched, so no image rebuild.

## Record correction: the F1b commit narrative

Commit `3be519bd` claims "SFINCS demo AOI (-85.75,29.55,-85.25,30.20) now SERVES
85% cudem + 15% etopo". Both runs it cites actually ran the EXHIBIT AOI at 89/11
-- `s3://trid3nt-runs/01M0GNM2MR8FVHVMW1WEB540CV/fallback_activations.json` and
`.../01M0GNS0Q1G4HF7CTMA7YKXJMQ/...` both record
`coverage=0.8888888888888888` over `AOI (-85.55, 29.7, -85.4, 29.85)`. The demo
AOI was never solved, and the topobathy URI quoted alongside was the
`resolution_m=30` variant, not the run's. The landed COMMIT is not rewritten; the
truth is recorded here.

## Noted, not fixed (F2 or later)

- The MinIO cache holds DUPLICATE ~414 MB topobathy objects for the same AOI at
  different `resolution_m` (`0070dac8...`, `6bd57d98...`). The key is
  resolution-sensitive by design; whether a coarse request should re-derive from a
  finer cached grid is a cache-design question, not a correctness one.
- Interior nodata inside a CUDEM tile is still measured by neither the footprint
  promise nor the painted-tile fraction (stated in
  `_assert_nearshore_coverage`'s contract since 0290).
