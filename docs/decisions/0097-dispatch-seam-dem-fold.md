# ADR 0097 -- Cross-sibling dispatch seam + the fetch_dem fold

Status: accepted (2026-08-03)
Follows: ADR 0090 (fetch_dem STOP, four blockers), ADR 0091 (gated-fallback wave --
dissolved blocker 1), ADR 0096 (re-audit -- cleared blocker 2, sharpened the
residual to exactly three machinery pieces). This wave BUILDS those three pieces
and lands the fold, driving the campaign coded-data-fetcher counter 5 -> 4.

## Context

ADR 0096 stopped the fetch_dem fold on three named, gate-level residuals (not a
judgement call):

1. The `source="copernicus"` leg returns `fetch_copernicus_dem`'s OWN `LayerURI`
   verbatim (its `copernicus_dem` cache prefix + `layer_id`/`name`), and the router
   had NO cross-registered-tool dispatch seam -- `route()` emits ONE `LayerURI`
   stamped with THIS spec's `source_class`.
2. The `Dem*Error` twins (`UpstreamAPIError` subclasses with PINNED `error_code`s)
   were WRAPPED into a generic `<PREFIX>_UPSTREAM_ERROR` by
   `library_delegate.invoke`'s `except RouterError` passthrough, dropping the
   test-pinned codes.
3. The twin's `layer_id="dem-{lon}-{lat}-{Nm}"` / `name="USGS 3DEP DEM (Nm)"` (+
   the coarsen stamp) needs the `envelope` hook, which `registration._validate_hooks`
   forces be declared TOGETHER with `output.result_model`.

NATE approved building all three (a NATE architecture decision: the dispatch seam
is the FIRST tool-composes-tool router pattern, deliberately narrow, gated here).

## Decision -- three machinery pieces, then the fold

### 1. Cross-sibling DISPATCH seam (`spec.dispatch`, `router.try_dispatch`)

A spec-declared, SINGLE-TARGET pre-flight short-circuit. `route()` calls
`try_dispatch(spec, raw_params)` as its FIRST step (before validate/gate/cache/fetch):
for one declared `DispatchSpec` whose `param` value matches (post-normalize) its
`equals_any` set, the router resolves the named sibling registered tool and returns
`entry.fn(**pass_args)` VERBATIM -- that tool's own `source_class` cache prefix, its
own `layer_id`/`name`, re-caching NOTHING under this spec. Byte-identical to the
twin's `TOOL_REGISTRY["fetch_copernicus_dem"].fn(bbox=bbox)` leg.

Constraints ENFORCED (the seam stays as narrow as stated -- the atomic-tools
doctrine avoids tool-composes-tool; this is the one sanctioned exception):
- ONE target per condition (`to` is a single string, not a list).
- SPEC-DECLARED (`to` / `equals_any` are literals -- never hook-computed).
- NO CHAINS -- a dispatched target that itself declares a `dispatch` block is
  REFUSED at dispatch time (`router_upstream_error`), so a dispatch always returns
  exactly ONE sibling's verbatim output; there is no A->B->C fan.
- PRE-FLIGHT ONLY -- evaluated on RAW params before any validation, so it is
  byte-identical to the twin (which dispatched first-thing on the raw `source`
  arg with zero prior validation). A `source="copernicus"` request therefore never
  runs fetch_dem's own bbox validation / continent ceiling; the copernicus target
  applies its own 4-deg^2 gate, exactly as the twin's leg did.

Rationale for the constraints: a single fixed target + no chains keeps the
composition acyclic and statically inspectable (the whole graph is one edge, in
the YAML). Pre-flight-only keeps the two tools' cache/validation domains disjoint
(no double-caching of the shared GLO-30 mosaic the three other copernicus consumers
read under `copernicus_dem`, the ADR 0096 double-cache blocker).

### 2. FetchError PASSTHROUGH (`library_delegate.invoke` / `resolve`)

Broadened `except RouterError: raise` to `except FetchError: raise`. `RouterError`
IS a `FetchError`, so this still passes every router-mapped A.6 error through, AND
now also passes any source-specific `FetchError` subclass carrying a pinned
`error_code` (the `Dem*Error` twins). A NON-`FetchError` library exception still
hits the generic upstream backstop -- verified unchanged for every other delegate
source (pfdf 3dep/statsgo, dataretrieval, HRRR-Zarr). The `Dem*Error` classes move
to a stable importable home in the delegate-hook module `hooks/dem_3dep.py` (the
natural owner now that the coded twin is deleted); no consumer catches them by name
(grep-verified), so the relocation touches only the migrated tests.

### 3. NAMING/ENVELOPE seam (a no-field `DemLayerURI`)

Chosen the LESS invasive of ADR 0096's two options: a zero-field `DemLayerURI(LayerURI)`
subclass in `contracts/execution.py` + `LAYER_RESULT_MODELS`, NOT a relaxation of the
`envelope`<->`result_model` pairing validator (which every other envelope spec depends
on -- relaxing it is a shared-validator change with a wider blast radius for zero
gain). `DemLayerURI` carries no business fields (the twin returned a plain `LayerURI`);
it serializes field-for-field like the base and `isinstance` holds, so the 8 consumers
that read `.uri`/`.bbox` off the result are unaffected. The `dem_3dep.envelope` hook
overrides `layer_id`/`name` (the router's only naming-override seam) with the twin's
exact forms + the coarsen stamp.

## The fold

`fetch_dem` is a `library_delegate` raster spec (`source.yaml`), py3dep owning the
socket. Four hooks in `hooks/dem_3dep.py`:
- `dem_3dep.validate` (delegate_validate) -- continent-ceiling `BboxInvalidError`
  (the twin's exact "5,000,000 km^2" message) + the auto-path US out-of-coverage
  `DemOutOfCoverageError` (generous CONUS+AK+HI+PR envelope; pinned 3dep is exempt),
  both pre-cache/pre-network.
- `dem_3dep.coarsen` (pre_resolve) -- the pixel-budget auto-coarsen: recompute the
  effective resolution + re-quantize the bbox to that coarser grid so the cache key
  keys on the DELIVERED grid. `requested_res_m` is merged ONLY when coarsening
  happened, so a non-coarsened request keeps the twin's exact `{bbox, resolution_m}`
  cache key BYTE-FOR-BYTE (the underscore-free key name is pruned when absent).
- `dem_3dep.read` (delegate) -- `py3dep.get_dem` under the twin's env-tunable
  wall-clock watchdog (`TRID3NT_DEM_PRIMARY_TIMEOUT_S`, default 90 s, daemon-thread
  join + abandon-discard) + the reproject-bounds partial-coverage gate
  (`DemPartialCoverageError`) + the SOURCE-CONDITIONAL error gating (partial ->
  propagate; pinned 3dep -> a plain suggesting `UpstreamAPIError`; auto ->
  `DemAutoFallbackGateError`). Returns `(array, transform, crs)`; the shared COG
  writer re-encodes it DEFLATE (the accepted ADR 0074 divergence class -- same
  array/CRS/nodata as the twin's LZW/5070 COG). The array is nodata-masked to NaN
  (the pfdf_3dep pattern).
- `dem_3dep.envelope` -- the `dem-{lon}-{lat}-{Nm}` / `USGS 3DEP DEM (Nm)` naming
  override + coarsen stamp.

The docstring is carried VERBATIM from the 0091 rewrite (byte-identical, so the
retrieval index is unshifted); the sibling `corpus.yaml` is retained (the loader
lifts it). Metadata is twin-identical (`source_class=dem`, `static-30d`, cacheable,
`auto_publish=False`).

## Consequences

- fetch_dem twin DELETED (`fetch_dem.py`, ~729 LOC); its DEM tests migrated to
  `test_router_dem.py` with the 0091 pins intact. The 8 direct importers repoint to
  the registry closure (`TOOL_REGISTRY["fetch_dem"].fn`, keyword-only -- the promoted
  `**kwargs` closure rejects positional args); flood.py + compute_contours keep a
  module-level `fetch_dem` registry-closure indirection so their tests' module-attr
  patch seam survives.
- Registry UNCHANGED at 175 (one twin died, one spec took its name). coded tools
  85 -> 84, coded fetchers 7 -> 6, spec-served 90 -> 91. Campaign coded-data-fetcher
  counter 5 -> 4 (the target).
- New contract surface: `DispatchSpec` + `SourceSpec.dispatch`; `DemLayerURI` in
  `LAYER_RESULT_MODELS`. Both are strict no-ops for every prior spec.
- Offline baseline unchanged: the `[fetch_dem-dem]` / `[fetch_topobathy-topobathy]`
  gate members of `test_fetch_resolution_gate` fail IDENTICALLY pre/post
  (4 failed / 19 passed, `assert 'local' == 'fetch'`, empty diff).
- FLOOD CANARY mandated (the flood DEM seam was re-pointed): a direct-call
  `sfincs_flood` drive proves status=ok + depth COG + a sane envelope.

## The seam's boundary (do NOT widen without a NATE decision)

`dispatch` is the first and only tool-composes-tool router pattern. It is capped at
one fixed target, one hop, pre-flight. A composed ANALYSIS (multi-layer, computed)
still belongs in the code_exec playground, never a dispatch chain. A second dispatch
consumer must re-justify the atomic-tools exception.
