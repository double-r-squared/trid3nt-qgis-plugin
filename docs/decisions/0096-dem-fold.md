# ADR 0096 -- fetch_dem fold: STOP (blocker 2 CLEARS; the source="copernicus" cross-tool leg is the decisive standing residual)

Status: accepted (2026-08-03)
Follows: ADR 0090 (fetch_dem STOP, four blockers) and ADR 0091 (gated-fallback
wave -- dissolved blocker 1, the cross-tool provenance restamp). This wave
re-audited the fold at HEAD 736140a against the CURRENT router surface with the
explicit goal of folding fetch_dem and driving the campaign coded-data-fetcher
counter 5 -> 4. The py3dep leg was found genuinely foldable (blocker 2 clears);
the fold STOPS on a different, still-standing blocker. No code change.

## Context

The campaign coded-data-fetcher counter stands at 5 (ADR 0095 new basis: the
remaining coded data-fetchers IN the fetchers package = topobathy, dem,
storm_tracks, goes_satellite, nwm). `fetch_dem` was to fold this wave (5 -> 4).

The fold was re-characterized end-to-end against `router.py::route`,
`select_executor`, `executors/library_delegate.py`, `executors/raster_cog.py`
(the `library_delegate` raster path + `array_to_cog_bytes`), `errors.py`, the
`HookSpec` / `SourceSpec` contract (`contracts/src/trid3nt_contracts/source_spec.py`),
`registration.py`, and the `pfdf_raster` delegate exemplar
(`fetch_3dep_extra/source.yaml` + `hooks/pfdf_raster.py`). GATE 1 (offline
edge-matrix parity vs the twin, field-for-field) was assessed per twin behavior.

## Decision -- STOP. One blocker CLEARS; the copernicus leg has no router surface.

### Blocker 2 (the bespoke py3dep leg) -- CLEARS this wave

ADR 0090 blocker 2 read: "a hook is PURE (no I/O), so neither the reproject
coverage check nor the timeout thread is a pure hook." That framing was
over-broad. The `library_delegate` DELEGATE hook is the ONE sanctioned impurity
(`docs/decisions/0074`; `HookSpec.delegate`: `(spec, params, *, timeout_s) ->
(array, transform, crs)`, "the hook OWNS the library socket"). Everything blocker
2 named lives inside such a hook:

- `py3dep.get_dem(bbox, resolution=...)` -- a genuine external library (NOT the
  pfdf `tnm.dem` path), the sanctioned socket. A NEW `py3dep.read` delegate hook,
  a sibling of `pfdf_3dep.read`.
- the hard wall-clock bounded-timeout DAEMON thread (`DemPrimaryTimeoutError`):
  py3dep exposes no timeout arg, but the hook RECEIVES `timeout_s` and can run
  the twin's own `_fetch_3dep_dem_bytes_bounded` daemon-thread + `join(timeout)`
  watchdog internally (the hook's own code -- allowed, it owns the socket).
- the reproject-bounds partial-coverage gate (`_dem_wgs84_bounds` / `_bbox_covers`
  -> `DemPartialCoverageError`, retryable): pure-ish compute over the returned
  raster, expressible inside the hook.

The COG-bytes divergence (the twin serializes py3dep's EPSG:5070 DataArray via
`rio.to_raster(driver="COG", compress="LZW")`; the router's `array_to_cog_bytes`
writes DEFLATE from the `(array, transform, crs)` the hook returns) is the SAME,
already-accepted ADR 0074 divergence class the `pfdf_3dep.read` fold carries
(same array / CRS / nodata, re-encoded). Not a blocker.

So blocker 2 is dissolved by leaning fully on the delegate-hook exception. Recorded
as a genuine advance; it does not by itself unblock the fold.

### The DECISIVE standing blocker -- source="copernicus" is a cross-registered-tool verbatim return with NO router surface

The twin's `source` enum routes THREE ways in one registered name:

```python
if src in _DEM_SOURCE_COPERNICUS_ALIASES:
    return TOOL_REGISTRY["fetch_copernicus_dem"].fn(bbox=bbox)   # <-- verbatim
```

`source="copernicus"` returns `fetch_copernicus_dem`'s OWN `LayerURI`: cached
under `source_class="copernicus_dem"` (uri `.../cache/static-30d/copernicus_dem/
<key>.tif`), `layer_id`/`name` built by the router from `copernicus_dem`, a
`stac_float` PC-STAC GLO-30 mosaic. GATE-1 parity on the `source="copernicus"`
explicit path therefore requires the folded tool to return THAT tool's layer,
byte-for-byte.

The router has NO seam to do this. Verified at HEAD 736140a:

- `route()` runs ONE pipeline and emits ONE `LayerURI` stamped with THIS spec's
  `source_class` ("dem"), cached under THIS spec's prefix. There is no
  short-circuit-and-return-another-tool's-LayerURI step.
- `select_executor` dispatches on `spec.shape` / `ingest.access` -- a spec
  CONSTANT, not a per-param switch. There is no `access_by_param` to route
  `source="copernicus"` to a `stac_float` leg and `source="3dep"` to the
  `library_delegate` leg within one spec.
- `grep` for `TOOL_REGISTRY` / `alias_of` / `dispatch_to` / `access_by_param` /
  `source_by_param` across `_router/` returns only the stratified-pool builder and
  registration idempotency -- no cross-tool dispatch anywhere.
- A `pre_resolve` / `delegate` hook cannot express it either: a delegate returns
  `(array, transform, crs)` which the router RE-SERIALIZES + RE-CACHES under
  `source_class="dem"` -> a DIFFERENT uri / layer_id / name / cache entry than
  `fetch_copernicus_dem`'s. Pixel-identical, envelope-divergent -> GATE 1 fails
  on that path AND double-caches the same GLO-30 mosaic the three OTHER copernicus
  consumers (`_hydrology_common`, `model_debris_flow`, `compute_sediment_yield`)
  read under `copernicus_dem`.

This leg is LOAD-BEARING, not incidental: ADR 0091's `DemAutoFallbackGateError`
(`DEM_FALLBACK_GATE`) and `DemOutOfCoverageError` (`DEM_OUT_OF_COVERAGE`) both
NAME `source="copernicus"` as the user's explicit retry and put it in
`.suggestions`. The whole gated-cross-dataset-fallback UX depends on
`fetch_dem(source="copernicus")` working on the SAME tool. It cannot be dropped
or diverged. This is ADR 0090 blocker 3 (the source-enum pin incl. the internal
`fetch_copernicus_dem` seam) -- which ADR 0091 explicitly LEFT STANDING
("blockers 2-4 stand").

The steelman (make copernicus the dem spec's OWN `stac_float` leg via a new
`access_by_param`, delete `fetch_copernicus_dem`, re-point its 3 other consumers)
does NOT rescue the fold: it (a) needs a new `access_by_param` router mode, (b) is
a multi-tool refactor + parity gates + a debris-flow/sediment-yield canary well
beyond folding one tool, and (c) STILL changes `source="copernicus"`'s emitted
layer shape (dem-cached vs copernicus_dem-cached) -> a documented parity
divergence. That is a scoped composite/refactor job, not a fold wave -- the exact
disposition ADR 0089/0090 reached for topobathy and storm_tracks.

### Secondary residuals (real, but subordinate to the copernicus blocker)

- NAMING. The twin emits `layer_id="dem-{lon:.4f}-{lat:.4f}-{res}m"` and
  `name="USGS 3DEP DEM ({res}m)"` (+ the coarsen note). `build_layer_uri`
  hardcodes `layer_id=f"{source_class}-{variable}"` / `name=f"{source_class}
  {variable}"`; the ONLY override seam is the `envelope` hook, which
  `registration._validate_hooks` REQUIRES be declared TOGETHER with
  `output.result_model` (a `LayerURI` subclass in `LAYER_RESULT_MODELS`). The twin
  returns a PLAIN `LayerURI` with no business fields, so naming parity forces
  either a no-field `DemLayerURI` contract subclass or a relaxation of the
  envelope/result_model pairing -- added machinery for zero business value.
- AUTO-COARSEN. The pixel-budget auto-coarsen (compute `effective_res`, re-quantize
  bbox on the coarser grid, stash `requested_res` for the name) IS expressible as a
  pure `pre_resolve` hook. Not a blocker on its own.
- EXCEPTION-CLASS COLLISION (blocker 4 detail). The 8 consumers import the
  `fetch_dem` FUNCTION directly (flood.py at module top; compute_contours /
  fetch_topobathy / swmm / geoclaw / landslide / extract lazily) and the standalone
  tool surface must keep `error_code` in {`DEM_PARTIAL_COVERAGE`,
  `DEM_PRIMARY_TIMEOUT`, `DEM_FALLBACK_GATE`, `DEM_OUT_OF_COVERAGE`} (test_data_fetch
  pins). The `Dem*Error` classes subclass `UpstreamAPIError` (a `FetchError`), NOT
  `RouterError`; `library_delegate.invoke` re-raises only `except RouterError` and
  WRAPS every other exception into a generic `<PREFIX>_UPSTREAM_ERROR` -- so a hook
  raising the twin's `Dem*Error` would LOSE the pinned typed code. Resolvable
  (broaden invoke's passthrough to `except FetchError`, keep `Dem*Error` in a stable
  home) -- but only worth doing inside a viable fold, which this is not.

## Consequences

- No code change. `fetch_dem.py` untouched; router / spec / contracts untouched.
  Registry UNCHANGED at 175 (in-process). coded tools 85 / coded fetchers 7
  (package column) / spec-served 90 all unchanged. Campaign coded-data-fetcher
  counter UNCHANGED at 5 (target 5 -> 4 honestly deferred -- the same disposition
  as ADR 0089/0090 for topobathy/storm_tracks).
- Offline baseline unchanged by construction (docs-only wave): EXACTLY 9 failures
  (`test_fetch_resolution_gate` x4 + `test_run_river_dye_scenario` x5). Captured
  this wave: `test_fetch_resolution_gate` = 4 failed / 19 passed; the
  `[fetch_dem-dem]` and `[fetch_topobathy-topobathy]` members fail IDENTICALLY
  pre/post with the pre-existing `assert 'local' == 'fetch'` signature (unrelated
  to fetch_dem code -- no fetch_dem/router code changed -- so the diff is empty).
- No FLOOD CANARY was run: no seam was re-pointed (zero code change), so the
  flood-consumer leg is untouched and no canary is mandated (the ADR 0090
  "seam UNTOUCHED -> no canary" rule; distinct from ADR 0091, which DID change
  fetch_dem and ran one).
- DELETION_LEDGER `fetch_dem fold` row updated: blocker 2 recorded CLEARED (the
  delegate-hook insight), the decisive residual sharpened to the
  `source="copernicus"` cross-registered-tool verbatim return, remains QUEUED.
- metrics.md gains a rolling docs-only STOP row (2026-08-03i).

### Unblock condition (sharpened)

A single router seam is missing: a cross-SIBLING-SOURCE dispatch -- a spec-served
tool that, for one param value, returns a DIFFERENT registered source's `LayerURI`
verbatim (foreign `source_class` cache prefix + foreign `layer_id`/`name`),
cache-consistent so a hit reproduces it. Concretely a `hooks.dispatch(spec, params)
-> LayerURI | None` pre-flight that short-circuits `route()`. This is the FIRST
router pattern where a spec-served tool would call ANOTHER registered tool, i.e. a
tool composing a tool -- which the atomic-tools doctrine ("analysis is playground,
not tools; atomic tools = data fetchers + irreducible primitives ONLY") deliberately
avoids. Whether the architecture should carry it is a NATE decision, not a fold-wave
build. WITH it, the fold also needs the naming seam (a `DemLayerURI` result model or
an envelope/result_model relaxation) + the `library_delegate.invoke` passthrough
broadened to `FetchError` so the `Dem*Error` typed codes survive. Three new-machinery
pieces for one fold = a scoped job, not a fold.

An honest STOP with a named, gate-level residual (and a genuine blocker-2 advance
recorded) beats a forced fold that cannot reproduce the twin's
`source="copernicus"` path field-for-field.
